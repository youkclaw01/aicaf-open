"""모델 외부의 숨은 2건 장벽 없이 서버 자원 제어기에 진입하는 제품 단계 계약."""
import asyncio
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from asis_pipeline import Pipeline


class Runtime:
    """모델을 호출하지 않는 작업 gate 단위시험용 객체."""
    pass


def test_stage_workers_do_not_keep_an_independent_fixed_two_limit(tmp_path):
    pipeline = Pipeline(Runtime(),tmp_path)
    seen = {'active':0,'peak':0}
    async def work():
        async with pipeline.item_gate:
            async with pipeline.author_gate:
                seen['active'] += 1; seen['peak'] = max(seen['peak'],seen['active'])
                await asyncio.sleep(.02)
                seen['active'] -= 1
    async def exercise():
        await asyncio.gather(*(work() for _ in range(8)))
    asyncio.run(exercise())
    assert seen['peak'] == 8, 'legacy worker gate hides ready work from adaptive admission'


def test_explicit_worker_limit_remains_available_for_scoped_probes(tmp_path):
    """운영 기본은 적응형이지만 명시한 격리 시험 한도는 유지한다."""
    pipeline = Pipeline(Runtime(), tmp_path, author_workers=1)
    seen = {'active':0, 'peak':0}
    async def work():
        async with pipeline.author_gate:
            seen['active'] += 1; seen['peak'] = max(seen['peak'], seen['active'])
            await asyncio.sleep(.005)
            seen['active'] -= 1
    async def exercise():
        await asyncio.gather(*(work() for _ in range(4)))
    asyncio.run(exercise())
    assert seen['peak'] == 1
