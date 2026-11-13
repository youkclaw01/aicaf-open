"""자원 슬롯이 풀릴 때 새 호출이 오래된 대기자를 앞지르지 않는 계약."""
import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from asis_admission import AdaptiveAdmission


def test_new_call_cannot_barge_ahead_of_existing_resource_waiter():
    """모델 전송 없이 실제 진입 제어기의 기아 현상을 재현한다."""
    reading = {'total_memory_bytes': 10000, 'available_memory_bytes': 3000,
               'cpu_percent': 0, 'worker_tree_rss_bytes': 1000, 'sample_valid': True}
    gate = AdaptiveAdmission(sampler=lambda: dict(reading), interval=.04, max_wait=.5)
    order = []

    async def run():
        async def work(name):
            async with gate.slot('model'):
                order.append(name)
                await asyncio.sleep(.002)
        async with gate.slot('model'):
            older = asyncio.create_task(work('older'))
            await asyncio.sleep(.005)
            assert gate.waiting == 1
        newer = asyncio.create_task(work('newer'))
        await asyncio.gather(older, newer)
        assert gate.active == gate.waiting == 0

    asyncio.run(run())
    assert order == ['older', 'newer'], 'new calls stole released capacity from an older resource waiter'
