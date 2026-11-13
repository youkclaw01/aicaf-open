"""진입 제어의 대기 시간·거절 사유·단계별 병렬도 기록 계약 시험.

2026-10-17 운영 사고 때 영수증 601건 중 556건이 queued_at == admitted_at == request_started_at 이었다(대기 시간이 기록되지 않음).
그래서 6단계(종합 계획)에서 약 30분을 잃은 사실을 사후 분석 전까지 알 수 없었다.

계약:
1. 진입 기록(admitted)에 이 호출이 진입 제어에서 기다린 시간(queue_wait_seconds)과 대기 중 마지막 거절 사유, 거절 횟수를 싣는다.
2. 영수증의 queued_at 은 진입 제어에 도착한 시각이고 admitted_at 보다 같거나 빠르다. 대기한 호출은 queued_at < admitted_at 이다.
3. 영수증에 queue_wait_seconds 를 싣는다.
4. 단계별 병렬도 감시: 준비 상태에서 대기하는 호출 수(waiting)와 진입 수(active)를 기록해 '독립 작업이 있는데 동시 수가 낮은' 상태를 사후에 계산할 수 있어야 한다.
"""
from pathlib import Path
import asyncio
import json
import sys

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from asis_admission import AdaptiveAdmission

GB = 1 << 30


class Host:
    def __init__(self, available=28 * GB, rss=int(.3 * GB), commit_available=32 * GB):
        self.available, self.rss, self.commit_available = available, rss, commit_available

    def sample(self):
        return {'sample_valid': True, 'total_memory_bytes': 32 * GB, 'available_memory_bytes': self.available, 'cpu_percent': 5.0,
                'worker_tree_rss_bytes': self.rss, 'commit_limit_bytes': 36 * GB, 'commit_available_bytes': self.commit_available}


def test_a_call_that_had_to_wait_records_how_long_and_why(tmp_path):
    """자원이 막힌 동안 도착한 호출은 자원이 풀릴 때까지 기다리고, 기다린 시간과 마지막 거절 사유를 영수증에 남긴다."""
    host = Host(available=int(1.0 * GB), commit_available=int(1.0 * GB))   # 시작부터 압박
    gate = AdaptiveAdmission(sampler=host.sample, interval=0.005, folder=tmp_path)

    async def waiter():
        async with gate.slot('model') as r:
            return r

    async def main():
        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.12)           # 압박 상태에서 대기
        assert gate.waiting == 1
        host.available, host.commit_available = 28 * GB, 32 * GB   # 해소
        return await task
    waited = asyncio.run(main())
    assert waited.get('queue_wait_seconds', 0) >= 0.1, '대기한 호출이 대기 시간을 기록하지 않았다: %r' % {k: waited.get(k) for k in waited if 'wait' in k or 'queue' in k or 'hold' in k}
    assert waited.get('queued_at') and waited['queued_at'] < waited['admitted_at'], 'queued_at 은 대기를 시작한 시각이어야 한다'
    assert waited.get('last_hold_reason') in ('memory_reserve', 'commit_reserve'), waited.get('last_hold_reason')
    assert waited.get('hold_count', 0) >= 2


def test_a_call_admitted_immediately_records_no_wait(tmp_path):
    host = Host()
    gate = AdaptiveAdmission(sampler=host.sample, interval=0.005, folder=tmp_path)

    async def one():
        async with gate.slot('model') as r:
            return r
    first = asyncio.run(one())
    assert first.get('queue_wait_seconds', 1) < 0.05 and first.get('hold_count', 1) == 0


def test_the_admitted_record_in_the_resource_log_carries_the_wait_fields(tmp_path):
    host = Host()
    gate = AdaptiveAdmission(sampler=host.sample, interval=0.001, folder=tmp_path)

    async def one():
        async with gate.slot('model'):
            pass
    asyncio.run(one())
    rows = [json.loads(line) for line in (tmp_path / 'resource-admission.jsonl').read_text(encoding='utf-8').splitlines()]
    admitted = [r for r in rows if r.get('event') == 'admitted']
    assert admitted and all('queue_wait_seconds' in r and 'hold_count' in r and 'queued_at' in r for r in admitted)


def test_a_reused_prepared_reservation_does_not_claim_a_new_wait(tmp_path):
    host = Host()
    gate = AdaptiveAdmission(sampler=host.sample, interval=0.001, folder=tmp_path)

    async def one():
        async with gate.work_unit('analysis-review'):
            async with gate.slot('model') as r:
                return r
    receipt = asyncio.run(one())
    assert receipt.get('prepared_unit_reservation_reused') is True
    assert 'queue_wait_seconds' in receipt


def test_snapshot_reports_waiting_and_active_for_parallelism_monitoring(tmp_path):
    host = Host(available=int(1.0 * GB), commit_available=int(1.0 * GB))
    gate = AdaptiveAdmission(sampler=host.sample, interval=0.005, folder=tmp_path)
    seen = {}

    async def blocked():
        async with gate.slot('model'):
            pass

    async def main():
        task = asyncio.create_task(blocked())
        await asyncio.sleep(0.05)
        seen.update(gate.snapshot())
        host.available = 28 * GB
        host.commit_available = 32 * GB
        await task
    asyncio.run(main())
    assert seen['waiting'] >= 1 and 'hold_reasons' in seen and seen['hold_reasons'], '대기 중 호출의 거절 사유 분포가 스냅샷에 있어야 한다: %r' % seen
