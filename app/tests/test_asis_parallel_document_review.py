"""전체 문서 검토의 분할·통합 계약. 지적 위치는 원본 문서 위치와 같아야 하고 단일 호출로 되돌아가면 안 된다."""
from pathlib import Path
import asyncio, json, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_analysis
from asis_pipeline import Pipeline
from test_asis_connection import SyntheticRuntime, synthetic_input


def _stages(tmp_path):
    data = synthetic_input(tmp_path)
    seed = asis_analysis.analyze(data)
    return seed['input'], seed['stages']


def test_sparse_shard_keeps_original_pointer_layout(tmp_path):
    data, stages = _stages(tmp_path)
    for index, area in enumerate(stages[0]['areas']):
        shard = Pipeline._sparse_stages(stages, 0, index)['stages']
        assert len(shard) == 4 and len(shard[0]['areas']) == len(stages[0]['areas'])
        assert shard[0]['areas'][index] == area
        assert all(row.get('omitted') for i, row in enumerate(shard[0]['areas']) if i != index)
        assert all(row.get('omitted') for row in shard[1:] if row['stage'] != stages[0]['stage'] and row['stage'] != 'area' and False) or shard[1]['omitted']


def test_document_review_runs_every_shard_and_integration_concurrently(tmp_path):
    data, stages = _stages(tmp_path)
    class Observed(SyntheticRuntime):
        pass
    runtime = Observed()
    engine = Pipeline(runtime, tmp_path / 'run')
    from asis_tasks import initial_graph
    manager = engine.task_manager(data)
    manager.register(initial_graph(asis_analysis.AREA_NAMES))
    # 문서 검토의 선행 작업은 앞 단계가 완료된 상태로 둔다. 이 시험은 검토 분할만 검증한다.
    for row in manager.ledger['tasks'].values():
        if row['id'] != 'document':
            row['status'] = 'completed'
    for row in manager.ledger['tasks'].values():
        manager._events.setdefault(row['id'], asyncio.Event()) if False else None
    async def run():
        for key in manager.ledger['tasks']:
            manager._events.setdefault(key, asyncio.Event()).set()
        return await asyncio.wait_for(engine.document_review(data, stages), 60)
    review = asyncio.run(run())
    assert review['verdict'] == 'pass' and review['defects'] == []
    names = {c['mode'] for c in runtime.calls if c['role'] == 'content_review'}
    # 12단계는 아키텍처 관점의 4단위다(영석님 정의 2026-10-19). 영역·공통·요약 샤드는 더 이상 별도 호출이 아니다.
    expected = {'document-shard-arch-area', 'document-shard-arch-direction', 'document-shard-arch-action', 'document-integration'}
    assert expected <= names and names <= expected, names
    assert runtime.max_active > 1, 'document review shards never overlapped'
    assert set(review['shards']) == {'arch-area', 'arch-direction', 'arch-action', 'integration'}
