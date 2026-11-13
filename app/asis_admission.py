"""호스트 CPU·가용 메모리와 실행 작업의 실측 비용에 따른 적응형 진입 제어."""
from __future__ import annotations
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import asyncio
import ctypes
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import time

_work_unit: ContextVar[dict | None] = ContextVar('asis_prepared_work_unit', default=None)


class HostSampler:
    """추가 패키지 없이 OS 계수기를 읽는다. 조회 실패를 자원 여유로 간주하지 않는다."""
    def __init__(self):
        self.system = platform.system()
        self.previous_cpu = None
        self.previous_cpu_percent = 0.
        self.sample()

    def sample(self):
        try:
            if self.system == 'Windows':
                memory, cpu, rss = self._windows()
            elif self.system == 'Linux':
                memory, cpu, rss = self._linux()
            elif self.system == 'Darwin':
                memory, cpu, rss = self._darwin()
            else:
                raise RuntimeError('unsupported_host_resource_sampler')
            busy, total = cpu
            prior = self.previous_cpu
            percent = self.previous_cpu_percent
            if prior is not None and total > prior[1] and busy >= prior[0]:
                percent = 100*(busy-prior[0])/(total-prior[1])
            self.previous_cpu = cpu
            self.previous_cpu_percent = max(0., min(100., percent))
            return {**memory, 'cpu_percent': max(0., min(100., percent)),
                    'logical_cpu_count': os.cpu_count() or 1, 'worker_tree_rss_bytes': rss,
                    'sample_valid': True, 'sampled_at': datetime.now(timezone.utc).isoformat()}
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            return {'sample_valid': False, 'sampled_at': datetime.now(timezone.utc).isoformat()}

    @staticmethod
    def _windows():
        from ctypes import wintypes as w
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        class Memory(ctypes.Structure):
            _fields_ = [('length', w.DWORD), ('load', w.DWORD)] + [
                (name, ctypes.c_ulonglong) for name in ('total', 'available', 'page_total', 'page_available',
                                                       'virtual_total', 'virtual_available', 'extended')]
        memory = Memory(); memory.length = ctypes.sizeof(memory)
        if not kernel.GlobalMemoryStatusEx(ctypes.byref(memory)):
            raise OSError('memory_sample_failed')
        idle, system, user = w.FILETIME(), w.FILETIME(), w.FILETIME()
        if not kernel.GetSystemTimes(ctypes.byref(idle), ctypes.byref(system), ctypes.byref(user)):
            raise OSError('cpu_sample_failed')
        number = lambda f: (f.dwHighDateTime << 32) | f.dwLowDateTime
        total = number(system)+number(user)
        class Entry(ctypes.Structure):
            _fields_ = [('size', w.DWORD), ('usage', w.DWORD), ('pid', w.DWORD),
                        ('heap', ctypes.c_size_t), ('module', w.DWORD), ('threads', w.DWORD),
                        ('parent', w.DWORD), ('priority', w.LONG), ('flags', w.DWORD), ('name', w.WCHAR*260)]
        class Counters(ctypes.Structure):
            _fields_ = [('size', w.DWORD), ('faults', w.DWORD)] + [(name, ctypes.c_size_t) for name in
                ('peak_rss', 'rss', 'peak_paged', 'paged', 'peak_nonpaged', 'nonpaged', 'pagefile', 'peak_pagefile')]
        kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
        kernel.OpenProcess.restype = w.HANDLE
        kernel.CloseHandle.argtypes = [w.HANDLE]
        kernel.Process32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
        kernel.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
        snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise OSError('process_tree_sample_failed')
        entries = []; entry = Entry(); entry.size = ctypes.sizeof(entry)
        try:
            present = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
            while present:
                entries.append((int(entry.pid), int(entry.parent)))
                present = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel.CloseHandle(snapshot)
        owned = {os.getpid()}
        while True:
            following = owned | {pid for pid, parent in entries if parent in owned}
            if following == owned: break
            owned = following
        psapi = ctypes.WinDLL('psapi', use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [w.HANDLE, ctypes.POINTER(Counters), w.DWORD]
        rss = 0
        for pid in owned:
            handle = kernel.OpenProcess(0x0400 | 0x0010, False, pid)
            if not handle: continue
            try:
                counters = Counters(); counters.size = ctypes.sizeof(counters)
                if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.size):
                    rss += int(counters.rss)
            finally:
                kernel.CloseHandle(handle)
        return {'total_memory_bytes': int(memory.total), 'available_memory_bytes': int(memory.available),
                'commit_limit_bytes': int(memory.page_total), 'commit_available_bytes': int(memory.page_available)}, (total-number(idle), total), rss

    @staticmethod
    def _tree_rss():
        run = subprocess.run(['ps', '-axo', 'pid=,ppid=,rss='], capture_output=True,
                             encoding='utf-8', timeout=5, check=True)
        rows = [tuple(map(int, line.split())) for line in run.stdout.splitlines() if line.strip()]
        owned = {os.getpid()}
        while True:
            following = owned | {pid for pid, parent, _ in rows if parent in owned}
            if following == owned: break
            owned = following
        return sum(rss*1024 for pid, _, rss in rows if pid in owned)

    def _linux(self):
        values = {line.split(':')[0]: int(line.split()[1])*1024 for line in
                  Path('/proc/meminfo').read_text(encoding='utf-8').splitlines()}
        ticks = list(map(int, Path('/proc/stat').read_text(encoding='utf-8').splitlines()[0].split()[1:]))
        total = sum(ticks[:8]); idle = ticks[3]+ticks[4]
        return {'total_memory_bytes': values['MemTotal'], 'available_memory_bytes': values['MemAvailable']}, (total-idle, total), self._tree_rss()

    def _darwin(self):
        total = int(subprocess.run(['sysctl', '-n', 'hw.memsize'], capture_output=True,
            encoding='utf-8', timeout=5, check=True).stdout.strip())
        output = subprocess.run(['vm_stat'], capture_output=True, encoding='utf-8', timeout=5, check=True).stdout
        import re
        page_size = int(re.search(r'page size of (\d+) bytes', output).group(1))
        stats = {key: int(value) for key, value in re.findall(r'^(Pages [^:]+):\s+(\d+)\.', output, re.M)}
        available = page_size*sum(stats.get(name, 0) for name in ('Pages free', 'Pages inactive', 'Pages speculative'))
        lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
        ticks = (ctypes.c_uint*4)(); count = ctypes.c_uint(4)
        if lib.host_statistics(lib.mach_host_self(), 3, ctypes.byref(ticks), ctypes.byref(count)):
            raise OSError('cpu_sample_failed')
        cpu_total = sum(ticks)
        return {'total_memory_bytes': total, 'available_memory_bytes': available}, (cpu_total-ticks[2], cpu_total), self._tree_rss()


class AdaptiveAdmission:
    """가용 자원까지 진입을 확대하며 압박 시 새 진입만 보류한다. 고정 2건 한도는 없다."""
    def __init__(self, *, sampler=None, interval=1., reserve_fraction=.20, cpu_ceiling=90., max_wait=1800., ceiling=None, folder=None):
        self.sampler = sampler or HostSampler().sample
        self.interval = interval
        self.reserve_fraction = reserve_fraction
        self.cpu_ceiling = cpu_ceiling
        self.max_wait = max_wait
        self.ceiling = ceiling
        self.folder = Path(folder) if folder is not None else None
        self.active = 0
        self.waiting = 0
        self.target = 1
        self.history = []
        self.peak_active = 0
        self.cost_bytes = None
        self.baseline_rss = None
        self.reservations = {}
        self._reservation_sequence = 0
        self._sample = None
        self._sample_at = 0.
        self._last_growth = 0.
        self._mutex = asyncio.Lock()
        self._wait_queue = []
        self.provider_hold_until = 0.
        self.hold_reasons = {}   # 현재 대기 중인 호출들이 받은 최근 거절 사유별 횟수(병렬도 감시)

    def _decision(self, extra_bytes=0):
        now = time.monotonic()
        # 새 진입마다 최신 자원 상태를 확인한다. 직전 여유 표본으로 압박을 통과시키지 않는다.
        self._sample = self.sampler(); self._sample_at = now
        sample = self._sample
        reason = 'admitted'
        numeric = ('total_memory_bytes','available_memory_bytes','cpu_percent','worker_tree_rss_bytes')
        valid = sample.get('sample_valid') is True and all(type(sample.get(key)) in (int,float)
            and math.isfinite(sample[key]) and sample[key] >= 0 for key in numeric)
        valid = valid and sample['total_memory_bytes'] > 0 and sample['worker_tree_rss_bytes'] > 0
        if not valid:
            detail = {'sample_valid':False, 'reason':'resource_sample_unavailable','allowed':False,'active':self.active}
            self._record(detail)
            return False, 'resource_sample_unavailable', detail
        total = sample['total_memory_bytes']; available = sample['available_memory_bytes']
        reserve = total*self.reserve_fraction
        rss = sample['worker_tree_rss_bytes']
        if self.baseline_rss is None:
            self.baseline_rss = rss
            # 시작 시 실제 프로세스 트리 RSS를 보수적 초기 비용(시드)으로 사용한다. 첫 실측이 생기면 대체한다.
            self.cost_bytes = max(1, rss)
            self._cost_is_seed = True
        elif not self.active and not self.reservations:
            # 작업이 하나도 돌지 않고 아직 메모리에 나타나지 않은 예약도 없을 때의 프로세스 메모리는 '작업 비용'이 아니라 누적 상태다.
            # 이 값을 작업 1개의 증가분으로 오해하지 않도록 기준선을 현재로 다시 잡는다
            # (2026-10-17 운영: 영역 작성 후 9.1GB 가 쌓인 채 active 0 -> 1 이 되며 비용이 8.8GB 로 뛰어 동시 실행이 21 -> 2 로 줄었다).
            self.baseline_rss = rss
        if self.active:
            measured = max(0, rss-self.baseline_rss)/self.active
            if measured:
                measured = math.ceil(measured)
                self._observed = True
                if getattr(self, '_cost_is_seed', False):
                    # 보수적 시드는 첫 실측으로 대체한다(실측이 시드보다 작아도 시드를 유지하면 과소 진입이 아니라 과대 보류가 된다).
                    self.cost_bytes = max(1, measured); self._cost_is_seed = False
                else:
                    # 과거 최대값으로 영구 고정하지 않고 최근 관측으로 갱신한다. 급증은 즉시, 감소는 완만하게 반영해 진동을 막는다.
                    previous = self.cost_bytes or 1
                    self.cost_bytes = max(1, measured if measured >= previous else int(previous*.5 + measured*.5))
        # 교착 방지: 비용이 크게 잡힌 채 활성 작업이 0 이면 새 실측이 생기지 않는다. 마지막으로 실제 관측한 비용(최근 관측)만 쓰고
        # 관측이 없으면(시작 직후) 시드를 쓴다. 과거 고정값은 '실측이 있을 때만' 유지한다.
        if not self.active and not self.reservations and not getattr(self, '_observed', False):
            self.cost_bytes = max(1, min(int(self.cost_bytes or rss or 1), rss))
        cost = max(1, int(self.cost_bytes or rss or 1), extra_bytes)
        materialized = max(0, rss-self.baseline_rss)
        promised = sum(self.reservations.values())
        unmaterialized = max(0, promised-materialized)
        memory_slots = max(0, int((available-reserve-unmaterialized)/cost))
        commit_limit = sample.get('commit_limit_bytes')
        commit_available = sample.get('commit_available_bytes')
        commit_slots = None
        if commit_limit is not None and commit_available is not None:
            commit_slots = max(0, int((commit_available-commit_limit*self.reserve_fraction-unmaterialized)/cost))
            memory_slots = min(memory_slots, commit_slots)
        capacity = self.active+memory_slots
        if sample['cpu_percent'] >= self.cpu_ceiling:
            capacity = self.active; reason = 'cpu_pressure'
        elif commit_slots is not None and commit_slots < 1:
            capacity = self.active; reason = 'commit_reserve'
        elif memory_slots < 1:
            capacity = self.active; reason = 'memory_reserve'
        elif now < self.provider_hold_until:
            capacity = self.active; reason = 'provider_backpressure'
        elif now-self._last_growth >= self.interval:
            self.target = max(1, min(capacity, self.target+max(1, self.target)))
            self._last_growth = now
        effective = min(capacity, self.target)
        if self.ceiling is not None:
            effective = min(effective, self.ceiling)
        allowed = self.active < effective
        if not allowed and reason == 'admitted': reason = 'adaptive_ramp'
        detail = {**sample, 'reserve_bytes': int(reserve), 'estimated_incremental_bytes': cost,
                  'pending_allocation_bytes': int(unmaterialized),
                  'resource_capacity': capacity, 'admission_target': effective,
                  'active': self.active, 'reason': reason, 'allowed': allowed}
        self._record(detail)
        return allowed, reason, detail

    def _record(self, row):
        """자원 계수·결정·진입 사실만 저장한다. 프롬프트와 자격증명은 포함하지 않는다."""
        record = {'recorded_at':datetime.now(timezone.utc).isoformat(), **row}
        self.history.append(record)
        if len(self.history) > 500: self.history.pop(0)
        if self.folder is not None:
            self.folder.mkdir(parents=True, exist_ok=True)
            with (self.folder/'resource-admission.jsonl').open('a', encoding='utf-8') as file:
                file.write(json.dumps(record, ensure_ascii=True, allow_nan=False)+'\n')

    def snapshot(self):
        """자원을 점유한 소단위/직접 요청과 대기 수를 작업 완료율과 별도로 보고한다."""
        return {'mode':'host_resource_adaptive','active':self.active,'waiting':self.waiting,
                'target':self.target,'peak_active':self.peak_active,'sample':dict(self._sample or {}),
                'estimated_incremental_bytes':self.cost_bytes,'fixed_ceiling':self.ceiling,
                'hold_reasons':dict(self.hold_reasons)}

    @asynccontextmanager
    async def work_unit(self, kind, *, extra_bytes=0):
        """입력 복제 전에 자원을 예약한다. 같은 단위의 순차 모델 요청은 이 예약을 재사용한다."""
        async with self.slot('prepared-'+kind, extra_bytes=extra_bytes) as receipt:
            lease = {'gate':self, 'receipt':receipt, 'request_lock':asyncio.Lock(), 'closed':False}
            token = _work_unit.set(lease)
            try:
                yield receipt
            finally:
                lease['closed'] = True
                _work_unit.reset(token)

    @asynccontextmanager
    async def slot(self, kind='model', *, extra_bytes=0):
        lease = _work_unit.get()
        if kind == 'model' and lease is not None and lease['gate'] is self:
            if lease['closed']:
                raise RuntimeError('prepared_work_unit_closed')
            if int(extra_bytes) > lease['receipt']['resource_sample']['estimated_incremental_bytes']:
                # 예약보다 큰 요청은 실패시키지 않고, 초과분을 포함한 별도 진입 판정을 거쳐 실행한다(메모리 보호 유지).
                token = _work_unit.set(None)
                try:
                    async with self.slot(kind, extra_bytes=extra_bytes) as receipt:
                        yield receipt
                finally:
                    _work_unit.reset(token)
                return
            async with lease['request_lock']:
                if lease['closed']:
                    raise RuntimeError('prepared_work_unit_closed')
                start = time.monotonic()
                while time.monotonic() < self.provider_hold_until:
                    if time.monotonic()-start > self.max_wait:
                        raise RuntimeError('resource_admission_wait_exhausted')
                    await asyncio.sleep(self.interval)
                if lease['closed']:
                    raise RuntimeError('prepared_work_unit_closed')
                receipt = {**lease['receipt'], 'kind':kind, 'prepared_unit_reservation_reused':True,
                    'preparation_admitted_at':lease['receipt']['admitted_at'],
                    'admitted_at':datetime.now(timezone.utc).isoformat()}
                self._record({'event':'prepared_reservation_reused', **receipt})
                yield receipt
            return
        start = time.monotonic(); self.waiting += 1
        queued_at = datetime.now(timezone.utc).isoformat()
        hold_count = 0; last_hold = None
        acquired = False; reservation = None; detail = {}
        waiter = object()
        self._wait_queue.append(waiter)
        try:
            while not acquired:
                async with self._mutex:
                    # 이미 기다린 호출이 먼저 슬롯을 확인한다. 새 호출의 즉시 진입으로 기아를 만들지 않는다.
                    if self._wait_queue[0] is waiter:
                        acquired, reason, detail = self._decision(extra_bytes)
                        if not acquired:
                            hold_count += 1; last_hold = reason
                            self.hold_reasons[reason] = self.hold_reasons.get(reason, 0) + 1
                    if acquired:
                        self._wait_queue.pop(0)
                        self._reservation_sequence += 1
                        reservation = self._reservation_sequence
                        self.reservations[reservation] = detail['estimated_incremental_bytes']
                        self.active += 1; self.peak_active = max(self.peak_active, self.active)
                if not acquired:
                    if time.monotonic()-start > self.max_wait:
                        raise RuntimeError('resource_admission_wait_exhausted')
                    await asyncio.sleep(self.interval)
            self.waiting -= 1
            if not self.waiting:
                self.hold_reasons.clear()   # 대기가 없으면 사유 분포를 비운다(현재 상태만 보여 준다)
            receipt = {'kind': kind, 'active': self.active, 'admitted_at': datetime.now(timezone.utc).isoformat(),
                       'queued_at': queued_at, 'queue_wait_seconds': round(time.monotonic()-start, 3),
                       'hold_count': hold_count, 'last_hold_reason': last_hold,
                       'waiting_at_admission': self.waiting, 'resource_sample':detail}
            self._record({'event':'admitted', 'reason':'admitted', **receipt})
            yield receipt
        finally:
            if acquired:
                self.reservations.pop(reservation, None)
                self.active -= 1
            else:
                if waiter in self._wait_queue:
                    self._wait_queue.remove(waiter)
                self.waiting -= 1

    def provider_backpressure(self, delay=30.):
        """공급자의 명시적 제한은 호스트 여유와 별개다. 기존 요청은 취소하지 않는다."""
        self.provider_hold_until = max(self.provider_hold_until, time.monotonic()+delay)
        self.target = max(1, self.active//2)
        self._record({'event':'provider_backpressure','reason':'provider_backpressure',
                      'delay_seconds':delay,'target':self.target,'active':self.active})
