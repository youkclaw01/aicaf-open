"""영역 담당자(1차)가 upheld이면 전체 중앙 관리자(2차)로 올라가고, 둘 다 upheld이면 사람 보고로 실패한다."""
from pathlib import Path
import asyncio, json, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
import asis_arbiter as arb
from asis_pipeline import Pipeline
from test_asis_arbiter_pipeline_flow import _arbiter_reply
from test_asis_item_agents import ItemRuntime, item_fixture


def test_next_level_escalates_upheld_only():
    assert arb.next_level('area_owner', 'upheld') == 'global_manager'
    assert arb.next_level('global_manager', 'upheld') == 'human_report'
    for action in ('defer_to_merge', 'supply_evidence', 'author_repair'):
        assert arb.next_level('area_owner', action) is None
    assert 'pass' not in arb.ACTIONS


def test_level_instructions_share_the_output_contract_but_differ_in_role():
    owner, manager = arb.level_instruction('area_owner', '비용'), arb.level_instruction('global_manager')
    assert '비용 영역 담당자' in owner and '전체 중앙 관리자' in manager
    assert arb.INSTRUCTION in owner and arb.INSTRUCTION in manager


class TwoLevelRuntime(ItemRuntime):
    def __init__(self, owner_action, global_action):
        super().__init__()
        self.actions = {'area_owner': owner_action, 'global_manager': global_action}
        self.levels_called = []
        self.reviews = 0

    async def ask(self, role, message, **kwargs):
        if role == 'content_review' and kwargs.get('session_id', '').startswith('asis-arbiter-'):
            payload = json.loads(message.rsplit('\n입력:\n', 1)[-1])
            self.levels_called.append(payload['level'])
            if payload['level'] == 'global_manager':
                assert payload.get('previous_decisions'), 'the global manager must see the area owner decision'
            return _arbiter_reply(payload, self.actions[payload['level']]), {'test_only': True}
        raw, receipt = await super().ask(role, message, **kwargs)
        if role == 'content_review':
            self.reviews += 1
            if self.reviews == 1:
                review = json.loads(raw)
                review.update(verdict='blocked', defects=[], content_checked=True, unresolved=[{
                    'topic': '합성 확인 불가', 'reason': '합성 반례: 담당 항목 밖의 근거 자료가 제공되지 않아 확인할 수 없다.',
                    'required_evidence': '합성 근거 자료 원문', 'fact_refs': []}])
                raw = json.dumps(review, ensure_ascii=False)
        return raw, receipt


def _run(tmp_path, owner, manager):
    from asis_item_agents import allocate_plan, item_input
    from test_asis_item_agents import metadata_plan
    data = allocate_plan(item_fixture(), metadata_plan(), 'synthetic-plan')
    scoped = item_input(data, 'diagnosis', data['context']['action_item_plan']['diagnoses'][0])
    runtime = TwoLevelRuntime(owner, manager)
    return runtime, asyncio.run(Pipeline(runtime, tmp_path / 'run').reviewed_stage(scoped, 3, 'diagnosis-J_ITEM_0001'))


def test_area_owner_resolves_without_calling_the_global_manager(tmp_path):
    runtime, candidate = _run(tmp_path, 'author_repair', 'upheld')
    assert runtime.levels_called == ['area_owner'] and candidate


def test_upheld_by_area_owner_goes_to_global_manager_which_can_resolve(tmp_path):
    runtime, candidate = _run(tmp_path, 'upheld', 'author_repair')
    assert runtime.levels_called == ['area_owner', 'global_manager'] and candidate
    files = {p.name for p in (tmp_path / 'run' / 'checkpoints').rglob('arbiter-decision*.json')}
    assert {'arbiter-decision.json', 'arbiter-decision-global.json'} <= files


def test_both_levels_upheld_fails_with_human_report(tmp_path):
    with pytest.raises(RuntimeError, match='independent_review_blocked'):
        _run(tmp_path, 'upheld', 'upheld')
