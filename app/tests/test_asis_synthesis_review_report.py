"""병합 후 구간·통합 검토가 모두 통과하면 '종합' 작업의 검토 보고가 원장에 남아 작업이 완료될 수 있어야 한다.
운영 실측: 구간 검토가 여러 개인 첫 통과(통합 검토 pass)에서 task_completion_reports_missing:synthesis로 개정이 실패했다."""
from pathlib import Path
import asyncio, json, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_synthesis
from asis_tasks import task
from asis_pipeline import Pipeline
from test_asis_parallel_synthesis import synthesis_fixture, DirectionRuntime
from test_asis_synthesis_review_shards import _merged


def prepare(tmp_path, count):
    data, plan = synthesis_fixture(tmp_path)
    pipe = Pipeline(DirectionRuntime(plan), tmp_path / 'run')
    manager = pipe.task_manager(data)
    manager.register([task('synthesis', '시사점 종합 확정', executor='model')])
    session = 'asis-content_review-' + asis_synthesis.digest({'run': data['run'], 'key': 'synthesis'})[:24]
    manager.bind_review('synthesis', reviewer_session=session)
    return data, pipe, manager, {**_merged(count), 'run': data['run']}


def test_a_passing_sharded_review_leaves_a_synthesis_reviewer_report_in_the_ledger(tmp_path):
    data, pipe, manager, merged = prepare(tmp_path, 60)

    async def go():
        async def execute():
            review = await asis_synthesis._review_merged_synthesis(pipe, data, merged)
            assert review['verdict'] == 'pass'
            return merged
        return await manager.perform('synthesis', execute)
    result = asyncio.run(go())
    row = manager.ledger['tasks']['synthesis']
    assert row['status'] == 'completed', 'the synthesis task must complete when every shard and the integration review pass'
    report = row['reports']['reviewer']
    assert report['checks']['verdict'] == 'pass' and report['checks']['sharded_review'] == {'shards': 3, 'integration': True}
    assert len(report['evidence']) == 4, '3 shard reviews + 1 integration review are the evidence'
    assert result is merged


def test_a_single_review_without_shards_still_completes_as_before(tmp_path):
    data, pipe, manager, merged = prepare(tmp_path, 10)

    async def go():
        async def execute():
            review = await asis_synthesis._review_merged_synthesis(pipe, data, merged)
            assert review['verdict'] == 'pass'
            return merged
        return await manager.perform('synthesis', execute)
    asyncio.run(go())
    assert manager.ledger['tasks']['synthesis']['status'] == 'completed'


def test_a_revise_verdict_does_not_report_a_pass_for_the_synthesis(tmp_path):
    data, plan = synthesis_fixture(tmp_path)

    class Revise(DirectionRuntime):
        async def ask(self, role, message, **kwargs):
            payload = json.loads(message.rsplit('\n입력:\n', 1)[-1])
            if role == 'content_review' and payload['stage_data'].get('mode') == 'synthesis-integration':
                rows = payload['stage_data']['draft']['directions']
                value = {'stage': 'review', 'run': payload['run'], 'verdict': 'revise', 'content_checked': True, 'render_checked': False,
                         'defects': [{'where': '/directions/0/title', 'quote': rows[0]['title'], 'why': '중복', 'fix': '구분', 'evidence_refs': []}],
                         'unresolved': []}
                return json.dumps(value, ensure_ascii=False), {'test_only': True, 'role': role}
            return await super().ask(role, message, **kwargs)
    pipe = Pipeline(Revise(plan), tmp_path / 'run')
    manager = pipe.task_manager(data)
    manager.register([task('synthesis', '시사점 종합 확정', executor='model')])
    manager.bind_review('synthesis', reviewer_session='asis-content_review-' + asis_synthesis.digest({'run': data['run'], 'key': 'synthesis'})[:24])
    review = asyncio.run(asis_synthesis._review_merged_synthesis(pipe, data, {**_merged(60), 'run': data['run']}))
    assert review['verdict'] == 'revise'
    assert 'reviewer' not in manager.ledger['tasks']['synthesis']['reports'], 'a failed review must never leave a pass report on the synthesis task'
