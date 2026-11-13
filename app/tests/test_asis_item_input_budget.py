"""항목 작성 입력의 미확인 예산: 큰 과제에서도 입력이 한도를 넘지 않되, 본문을 못 넣는 미확인의 존재(주제·사실 개수)는 사라지지 않는다.

운영 실측(2026-10-16): 통합 후 항목 입력이 모델 호출 전에 한도를 넘어 진단 100건 중 77건, 과제 19건 중 3건이 실패했다. 원인은 통합 추적표·통합 전 계획 전체·연결되지 않은 이슈 255개와
미확인 본문(항목당 약 42만 자)이었다.
"""
from copy import deepcopy
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
import asis_model_view as mv
import asis_item_agents as ia
from test_asis_model_view import heavy_data


def rows(n, chars=600, mine_every=5):
    """mine_every마다 하나씩 이 항목의 사실(F_MINE)과 겹친다. 값이 1이면 전부 겹쳐서 예산을 넘는 상황을 만든다."""
    return [{'topic': '주제 %03d' % i, 'reason': 'r' * chars, 'required_evidence': 'e', 'fact_refs': ['F_%d' % (i % 7)] + (['F_MINE'] if i % mine_every == 0 else [])}
            for i in range(n)]


def test_text_within_budget_is_returned_unchanged():
    data = rows(5, 100)
    kept, omitted = mv.budgeted_unresolved(data, {'F_MINE'}, 1_000_000)
    assert kept == data and omitted is None


def test_over_budget_keeps_the_most_overlapping_text_and_leaves_every_omitted_topic_visible():
    data = rows(400, 600)
    kept, omitted = mv.budgeted_unresolved(data, {'F_MINE'}, 70_000)
    assert mv._chars(kept) <= 70_000
    assert omitted['kept_count'] == len(kept) and omitted['omitted_count'] == 400 - len(kept)
    assert len(omitted['omitted_topics']) == omitted['omitted_count']
    assert {row['topic'] for row in kept} | {row['topic'] for row in omitted['omitted_topics']} == {row['topic'] for row in data}, 'nothing disappears'
    # 이 항목의 사실과 겹치는 미확인이 먼저 채워진다
    overlapping = [row for row in data if 'F_MINE' in row['fact_refs']]
    assert sum(1 for row in kept if 'F_MINE' in row['fact_refs']) == min(len(overlapping), len(kept))
    assert all('reason' not in item for item in omitted['omitted_topics']), 'omitted entries carry no body'


def test_the_item_view_budgets_both_unresolved_lists_and_records_what_was_omitted(tmp_path):
    data, plan = heavy_data(tmp_path, extra_facts=0)
    slot = dict(plan['directions'][0]); slot['id'] = data['allocated_ids']['directions'][0]
    data['stage_data']['approved_areas']['unresolved'] = rows(500, 600, mine_every=1)
    # 항목 뷰는 승인 영역·방향의 미확인을 줄이므로 item_view를 직접 시험한다
    scoped = deepcopy(data)
    scoped['stage_data'] = {'mode': 'actions', 'item_assignment': {'kind': 'diagnosis', 'item_id': 'J_ITEM_0001',
                            'slot': {'fact_refs': ['F_MINE'], 'criterion_refs': [], 'goal_refs': [], 'direction_refs': []}},
                            'approved_areas': deepcopy(data['stage_data']['approved_areas']), 'approved_directions': {'directions': [], 'edges': [], 'unresolved': rows(500, 600, mine_every=1)}}
    scoped['allocated_ids'] = {'diagnoses': ['J_ITEM_0001'], 'actions': []}
    view = mv.item_view(scoped, 'item_author')
    for block in ('approved_areas', 'approved_directions'):
        sent = view['stage_data'][block]
        assert mv._chars(sent.get('unresolved', [])) <= mv.UNRESOLVED_BUDGET_CHARS
    for block in ('approved_areas', 'approved_directions'):
        assert view['stage_data'][block]['omitted_unresolved']['omitted_count'] > 0, block


def test_the_canonical_input_is_never_modified_by_the_item_view_or_item_input(tmp_path):
    data, plan = heavy_data(tmp_path, extra_facts=0)
    data['stage_data']['approved_areas']['unresolved'] = rows(300, 600)
    before = mv._chars(data['stage_data']['approved_areas']['unresolved'])
    plan_context = data['context'].setdefault('action_item_plan', {'consolidation_trace': {'diagnoses': [{'x': 'y' * 1000}] * 50, 'actions': []}, 'diagnoses': [], 'actions': []})
    slot = dict(plan['directions'][0]); slot['id'] = data['allocated_ids']['directions'][0]
    if data['stage_data'].get('approved_directions'):
        ia.item_input(data, 'diagnosis', {'id': 'J_ITEM_0001', 'scope': 's', 'reason': 'r', 'issue_refs': [], 'direction_refs': [slot['id']], 'fact_refs': [],
                                          'criterion_refs': [], 'goal_refs': []}, {})
    assert mv._chars(data['stage_data']['approved_areas']['unresolved']) == before
    assert 'consolidation_trace' in data['context']['action_item_plan'], 'the ledger keeps the trace used for the Excel source link'


def test_item_input_leaves_out_the_consolidation_trace_and_the_unlinked_issues(tmp_path):
    data, plan = heavy_data(tmp_path, extra_facts=0)
    issues = [{'id': 'PLAN_P%04d' % i, 'scope': 's%d' % i, 'reason': 'r' * 400, 'lead_area': 'ai'} for i in range(255)]
    data['context']['analysis_plan'] = {'issues': issues, 'schema': 'x'}
    data['context']['action_item_plan'] = {'consolidation_trace': {'diagnoses': [{'t': 'z' * 500}] * 100, 'actions': []}, 'diagnoses': [{'q': 'w' * 500}] * 100}
    dirs = data['stage_data'].setdefault('approved_directions', {'directions': [], 'edges': [], 'unresolved': []})
    scoped = ia.item_input(data, 'diagnosis', {'id': 'J_ITEM_0001', 'scope': 's', 'reason': 'r', 'issue_refs': ['PLAN_P0003'], 'direction_refs': [],
                                                'fact_refs': [], 'criterion_refs': [], 'goal_refs': []}, {})
    assert 'action_item_plan' not in scoped['context']
    assert [row['id'] for row in scoped['context']['analysis_plan']['issues']] == ['PLAN_P0003']
    assert scoped['context']['analysis_plan']['model_scope_note']['total_issues'] == 255
    assert len(data['context']['analysis_plan']['issues']) == 255 and 'consolidation_trace' in data['context']['action_item_plan']
