"""소단위 준비 예약과 같은 단위의 순차 모델 요청은 자원 슬롯을 이중 점유하지 않는다."""
import asyncio
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from asis_admission import AdaptiveAdmission


def test_prepared_work_unit_does_not_deadlock_on_its_model_request():
    sample={'total_memory_bytes':10000,'available_memory_bytes':3200,'cpu_percent':5,
        'logical_cpu_count':4,'worker_tree_rss_bytes':1000,'sample_valid':True}
    gate=AdaptiveAdmission(sampler=lambda:dict(sample),interval=.001,max_wait=.03)
    assert callable(getattr(gate,'work_unit',None)), 'resource admission must start before input preparation'
    seen=[]
    async def run():
        async with gate.work_unit('analysis'):
            seen.append('prepared')
            assert gate.active==1
            for index in range(2):
                async with gate.slot('model',extra_bytes=256):
                    seen.append('model-'+str(index))
                    assert gate.active==1,'one sequential work unit must not consume another resource slot'
        assert gate.active==gate.waiting==0
    asyncio.run(run())
    assert seen==['prepared','model-0','model-1']
    assert gate.peak_active==1


def test_reused_request_waits_for_provider_backpressure():
    import time
    sample={'total_memory_bytes':10000,'available_memory_bytes':3200,'cpu_percent':5,
        'logical_cpu_count':4,'worker_tree_rss_bytes':1000,'sample_valid':True}
    gate=AdaptiveAdmission(sampler=lambda:dict(sample),interval=.005,max_wait=.2)
    async def run():
        async with gate.work_unit('analysis'):
            gate.provider_backpressure(.03)
            deadline=gate.provider_hold_until
            async with gate.slot('model'):
                assert time.monotonic()>=deadline,'prepared units must not bypass provider backpressure'
        assert gate.active==gate.waiting==0
    asyncio.run(run())


def test_model_admission_timestamp_is_not_preparation_timestamp():
    sample={'total_memory_bytes':10000,'available_memory_bytes':3200,'cpu_percent':5,
        'logical_cpu_count':4,'worker_tree_rss_bytes':1000,'sample_valid':True}
    gate=AdaptiveAdmission(sampler=lambda:dict(sample),interval=.001)
    async def run():
        async with gate.work_unit('analysis') as preparation:
            await asyncio.sleep(.025)
            async with gate.slot('model') as request:
                assert request['admitted_at']>preparation['admitted_at'],'model admission must not be backdated to input preparation'
    asyncio.run(run())
