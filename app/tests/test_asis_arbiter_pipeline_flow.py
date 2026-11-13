"""실제 Pipeline 경로: 검토자가 blocked를 내면 조정자가 조치를 고르고, 조치별 흐름이 맞다."""
from pathlib import Path
import asyncio, json, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from asis_pipeline import Pipeline
from test_asis_item_agents import ItemRuntime, item_fixture
from test_asis_item_graph_contract import GraphlessWriter  # noqa: F401  (same synthetic transport)


def _arbiter_reply(data, action, quote_source_field='reason'):
    items = data['blocked_items']
    quote = str(items[0][quote_source_field])[:20]
    return json.dumps({'decisions': [{'index': i, 'action': action, 'evidence_ids': [], 'quote': str(row[quote_source_field])[:20],
                                      'reason': '합성 판정'} for i, row in enumerate(items)]}, ensure_ascii=False)


class BlockingReviewer(ItemRuntime):
    """첫 검토는 blocked, 이후는 정상 검토. 조정자 호출은 지정한 조치를 반환한다."""
    def __init__(self, action):
        super().__init__()
        self.action = action
        self.reviews = 0
        self.arbiter_calls = 0

    async def ask(self, role, message, **kwargs):
        if role == 'content_review' and kwargs.get('session_id', '').startswith('asis-arbiter-'):
            self.arbiter_calls += 1
            payload = json.loads(message.rsplit('\n입력:\n', 1)[-1])
            return _arbiter_reply(payload, self.action), {'test_only': True, 'arbiter': True}
        raw, receipt = await super().ask(role, message, **kwargs)
        if role == 'content_review':
            self.reviews += 1
            if self.reviews == 1:
                data = json.loads(message.rsplit('\n입력:\n', 1)[-1])
                review = json.loads(raw)
                review.update(verdict='blocked', defects=[], content_checked=True, unresolved=[{
                    'topic': '합성 확인 불가', 'reason': '합성 반례: 담당 항목 밖의 근거 자료가 제공되지 않아 확인할 수 없다.',
                    'required_evidence': '합성 근거 자료 원문', 'fact_refs': []}])
                raw = json.dumps(review, ensure_ascii=False)
        return raw, receipt


def _run(tmp_path, action):
    from asis_item_agents import allocate_plan, item_input
    from test_asis_item_agents import metadata_plan
    data = allocate_plan(item_fixture(), metadata_plan(), 'synthetic-plan')
    scoped = item_input(data, 'diagnosis', data['context']['action_item_plan']['diagnoses'][0])
    runtime = BlockingReviewer(action)
    pipe = Pipeline(runtime, tmp_path / 'run')
    return runtime, asyncio.run(pipe.reviewed_stage(scoped, 3, 'diagnosis-J_ITEM_0001'))


def test_author_repair_decision_returns_the_candidate_to_the_writer_and_continues(tmp_path):
    runtime, candidate = _run(tmp_path, 'author_repair')
    assert runtime.arbiter_calls == 1
    assert len([c for c in runtime.calls if c['role'] in ('author', 'direction_author')]) == 2, 'writer must get one more try with the arbiter defect'
    assert candidate


def test_upheld_decision_keeps_the_block(tmp_path):
    with pytest.raises(RuntimeError, match='independent_review_blocked'):
        _run(tmp_path, 'upheld')


def test_decision_is_recorded_with_authority_and_input_hash(tmp_path):
    _run(tmp_path, 'author_repair')
    files = list((tmp_path / 'run' / 'checkpoints').rglob('arbiter-decision.json'))
    assert files
    record = json.loads(files[0].read_text(encoding='utf-8'))
    assert record['authority'] == 'classify_only_cannot_declare_pass' and record['action'] == 'author_repair' and record['input_sha256']
