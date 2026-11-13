"""소단위 예약 종료·요청 직렬화·예약 비용 초과를 별개로 검사한다."""
import asyncio
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pytest
from asis_admission import AdaptiveAdmission


def gate():
    sample={'total_memory_bytes':10000,'available_memory_bytes':3200,'cpu_percent':5,
        'logical_cpu_count':4,'worker_tree_rss_bytes':1000,'sample_valid':True}
    return AdaptiveAdmission(sampler=lambda:dict(sample),interval=.001,max_wait=.1)


def test_inherited_reservation_is_invalid_after_unit_finishes():
    async def run():
        admission=gate();ready=asyncio.Event()
        async def escaped():
            await ready.wait()
            async with admission.slot('model'):pass
        async with admission.work_unit('item'):
            task=asyncio.create_task(escaped())
        ready.set()
        with pytest.raises(RuntimeError,match='prepared_work_unit_closed'):await task
        assert admission.active==admission.waiting==0
    asyncio.run(run())


def test_oversized_request_is_admitted_separately_not_failed():
    async def run():
        admission=gate()
        async with admission.work_unit('item'):
            try:
                async with admission.slot('model',extra_bytes=2000) as receipt:
                    assert not receipt.get('prepared_unit_reservation_reused')
            except RuntimeError as exc:
                assert str(exc)!='prepared_work_unit_request_exceeds_reservation'
        assert admission.active==admission.waiting==0
    asyncio.run(run())


def test_concurrent_requests_of_one_unit_are_serialized_without_extra_slot():
    async def run():
        admission=gate();active=0;peak=0
        async def request():
            nonlocal active,peak
            async with admission.slot('model'):
                active+=1;peak=max(peak,active)
                await asyncio.sleep(.005)
                active-=1
        async with admission.work_unit('item'):
            await asyncio.gather(*(request() for _ in range(3)))
        assert peak==1 and admission.peak_active==1
        assert admission.active==admission.waiting==0
    asyncio.run(run())
