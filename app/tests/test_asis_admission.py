"""호스트 실측에 따라 동시 진입을 조절하고 실행 중 작업은 보존하는 계약 시험."""
import asyncio
import importlib.util
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_memory_pressure_blocks_new_calls_without_canceling_active_work():
    assert importlib.util.find_spec('asis_admission') is not None, 'adaptive resource admission is missing'
    from asis_admission import AdaptiveAdmission
    readings = {'total_memory_bytes': 10000, 'available_memory_bytes': 8500,
                'cpu_percent': 10, 'logical_cpu_count': 4, 'worker_tree_rss_bytes': 1000,
                'sample_valid': True}
    gate = AdaptiveAdmission(sampler=lambda: dict(readings), interval=.005, reserve_fraction=.20)
    seen = []
    async def run():
        async with gate.slot('model'):
            seen.append('first-start')
            readings['available_memory_bytes'] = 500
            async def second():
                async with gate.slot('model'):
                    seen.append('second-start')
            waiting = asyncio.create_task(second())
            await asyncio.sleep(.03)
            assert not waiting.done() and seen == ['first-start']
            assert gate.active == 1
            readings['available_memory_bytes'] = 8500
            await waiting
            assert gate.active == 1
        assert gate.active == 0
    asyncio.run(run())
    assert seen == ['first-start', 'second-start']
    assert any(row['reason'] == 'memory_reserve' for row in gate.history)


def test_admission_expands_beyond_two_with_measured_host_headroom():
    from asis_admission import AdaptiveAdmission
    readings = {'total_memory_bytes': 100000, 'available_memory_bytes': 85000,
                'cpu_percent': 8, 'logical_cpu_count': 2, 'worker_tree_rss_bytes': 1000,
                'sample_valid': True}
    gate = AdaptiveAdmission(sampler=lambda: dict(readings), interval=.002)
    async def exercise():
        async def work():
            async with gate.slot('model'):
                await asyncio.sleep(.04)
        await asyncio.gather(*(work() for _ in range(12)))
    asyncio.run(exercise())
    assert gate.peak_active > 2, 'host headroom must expand the old shared two-call cap'
    assert gate.peak_active <= 12
    assert gate.active == gate.waiting == 0


def test_cpu_and_unavailable_samples_cannot_be_reported_as_free_capacity():
    from asis_admission import AdaptiveAdmission
    readings = {'total_memory_bytes': 10000, 'available_memory_bytes': 8500,
                'cpu_percent': 96, 'logical_cpu_count': 4, 'worker_tree_rss_bytes': 1000,
                'sample_valid': True}
    gate = AdaptiveAdmission(sampler=lambda: dict(readings), interval=.001, max_wait=.01)
    async def exercise():
        import pytest
        with pytest.raises(RuntimeError, match='resource_admission_wait_exhausted'):
            async with gate.slot('model'):
                raise AssertionError('CPU-pressure operation was admitted')
        assert gate.active == gate.waiting == 0
        readings['sample_valid'] = False
        with pytest.raises(RuntimeError, match='resource_admission_wait_exhausted'):
            async with gate.slot('model'):
                raise AssertionError('unknown capacity operation was admitted')
    asyncio.run(exercise())
    assert {'cpu_pressure'} <= {row.get('reason') for row in gate.history}


def test_pending_allocations_reserve_memory_before_process_rss_increases():
    """아직 실제 RSS가 늘지 않은 동시 작업도 예정 메모리로 과입장을 막는다."""
    from asis_admission import AdaptiveAdmission
    readings = {'total_memory_bytes': 10000, 'available_memory_bytes': 5000,
                'cpu_percent': 8, 'logical_cpu_count': 4, 'worker_tree_rss_bytes': 1000,
                'sample_valid': True}
    gate = AdaptiveAdmission(sampler=lambda: dict(readings), interval=.001)
    started = []
    async def exercise():
        release = asyncio.Event()
        async def work(index):
            async with gate.slot('model', extra_bytes=1000):
                started.append(index)
                await release.wait()
        tasks = [asyncio.create_task(work(index)) for index in range(6)]
        try:
            await asyncio.sleep(.03)
            assert len(started) == 3, 'unmaterialized allocations were admitted beyond free memory'
            assert gate.active == 3 and gate.waiting == 3
        finally:
            release.set()
            await asyncio.gather(*tasks)
    asyncio.run(exercise())
    assert gate.active == gate.waiting == 0


def test_commit_pressure_blocks_new_work_even_with_free_physical_memory():
    """Windows 커밋 한도 압박을 물리 메모리 여유로 덮지 않는다."""
    from asis_admission import AdaptiveAdmission
    readings = {'total_memory_bytes': 10000, 'available_memory_bytes': 8500,
                'commit_limit_bytes': 15000, 'commit_available_bytes': 500,
                'cpu_percent': 8, 'logical_cpu_count': 4, 'worker_tree_rss_bytes': 1000,
                'sample_valid': True}
    gate = AdaptiveAdmission(sampler=lambda: dict(readings), interval=.001, max_wait=.01)
    async def exercise():
        import pytest
        with pytest.raises(RuntimeError, match='resource_admission_wait_exhausted'):
            async with gate.slot('model'):
                raise AssertionError('commit-constrained work was admitted')
    asyncio.run(exercise())
    assert any(row.get('reason') == 'commit_reserve' for row in gate.history)
    assert gate.active == gate.waiting == 0


def test_zero_tick_cpu_sample_does_not_turn_known_pressure_into_idle(monkeypatch):
    """한 OS tick 안의 표본은 직전 실측 부하를 유지한다."""
    from asis_admission import HostSampler
    samples = iter([({'total_memory_bytes':10000,'available_memory_bytes':8500}, (0,100), 1000),
                    ({'total_memory_bytes':10000,'available_memory_bytes':8500}, (96,200), 1000),
                    ({'total_memory_bytes':10000,'available_memory_bytes':8500}, (96,200), 1000)])
    monkeypatch.setattr('asis_admission.platform.system', lambda:'Windows')
    monkeypatch.setattr(HostSampler, '_windows', staticmethod(lambda:next(samples)))
    sampler = HostSampler()
    assert sampler.sample()['cpu_percent'] == 96
    assert sampler.sample()['cpu_percent'] == 96, 'sub-tick reads were incorrectly labeled idle'


def test_admission_receipt_keeps_resources_and_provider_hold_without_retrying_work(tmp_path):
    """모델 공급자의 명시 제한은 새 진입에만 적용하고 실측 원장에 남긴다."""
    from asis_admission import AdaptiveAdmission
    import json
    readings = {'total_memory_bytes':100000,'available_memory_bytes':85000,
                'cpu_percent':8,'logical_cpu_count':4,'worker_tree_rss_bytes':1000,'sample_valid':True}
    gate = AdaptiveAdmission(sampler=lambda:dict(readings), interval=.002, folder=tmp_path)
    seen = []
    async def exercise():
        gate.provider_backpressure(.025)
        async with gate.slot('model') as receipt:
            seen.append(receipt)
            assert receipt['resource_sample']['sample_valid'] is True
    asyncio.run(exercise())
    assert len(seen) == 1
    records = [json.loads(line) for line in (tmp_path/'resource-admission.jsonl').read_text(encoding='utf-8').splitlines()]
    assert any(row.get('reason') == 'provider_backpressure' for row in records)
    assert any(row.get('event') == 'admitted' and row.get('kind') == 'model' for row in records)
    assert gate.snapshot()['peak_active'] == 1


def test_invalid_resource_numbers_fail_closed_without_entering_work():
    """손상·미확인 계수에 NaN·음수·잘못된 단위가 들어와도 여유로 처리하지 않는다."""
    from asis_admission import AdaptiveAdmission
    valid = {'total_memory_bytes':10000,'available_memory_bytes':8500,
             'cpu_percent':8,'worker_tree_rss_bytes':1000,'sample_valid':True}
    for key, value in (('cpu_percent',float('nan')),('available_memory_bytes',-1),
                       ('worker_tree_rss_bytes',0),('total_memory_bytes',0)):
        reading = {**valid,key:value}
        gate = AdaptiveAdmission(sampler=lambda:reading, interval=.001,max_wait=.005)
        async def exercise():
            import pytest
            with pytest.raises(RuntimeError,match='resource_admission_wait_exhausted'):
                async with gate.slot():
                    raise AssertionError('invalid resource sample admitted')
        asyncio.run(exercise())
        assert gate.active == gate.waiting == 0
