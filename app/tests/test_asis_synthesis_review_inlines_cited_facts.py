"""종합 병합 검토는 대상 방향이 인용한 큰 사실의 값도 검토자에게 전달한다(전달되지 않은 근거로 blocked가 나지 않도록)."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from asis_synthesis import _review_scope
from test_asis_connection import synthetic_input


def _big(data, fact_id, size):
    for fact in data['facts']:
        if fact['id'] == fact_id:
            fact['value'] = {'rows': ['x' * 200] * size}
            return fact


def test_cited_large_fact_is_inlined_and_uncited_large_fact_is_not(tmp_path):
    data = synthetic_input(tmp_path)
    cited, other = data['facts'][0]['id'], data['facts'][1]['id']
    _big(data, cited, 400)
    _big(data, other, 400)
    view = _review_scope(data, [{'fact_refs': [cited], 'criterion_refs': []}])
    by_id = {f['id']: f for f in view['facts']}
    assert cited in by_id and not (isinstance(by_id[cited]['value'], dict) and by_id[cited]['value'].get('not_inlined'))
    assert other not in by_id, 'uncited facts must stay out of the review input'
    assert data['facts'][0]['value']['rows'], 'canonical value changed'


def test_integration_summary_view_keeps_facts_out(tmp_path):
    data = synthetic_input(tmp_path)
    view = _review_scope(data, [{'fact_refs': [data['facts'][0]['id']], 'criterion_refs': []}], summary=True)
    assert view['facts'] == []


def test_overlapping_direction_candidates_from_two_areas_are_merged_by_code():
    """같은 자원 이름과 겹치는 근거를 가진 방향 후보는 하나로 합치고 모든 참조를 보존한다."""
    from asis_synthesis import _merge_overlapping_directions
    base = {'issue_refs': ['PLAN_A'], 'area_implication_refs': ['I_network'], 'goal_refs': []}
    slots = [
        {**base, 'scope': 'stmerge01 저장소의 공개 접근 유지 판단', 'reason': 'stmerge01 network', 'fact_refs': ['F1', 'F2'], 'criterion_refs': ['C1']},
        {'issue_refs': ['PLAN_B'], 'area_implication_refs': ['I_data'], 'goal_refs': [], 'scope': 'stmerge01 저장소의 접근 제한 조건',
         'reason': 'stmerge01 data', 'fact_refs': ['F2', 'F3'], 'criterion_refs': ['C1']},
        {**base, 'scope': '전혀 다른 대상 vmother01의 확인', 'reason': 'vmother01', 'fact_refs': ['F9'], 'criterion_refs': ['C9']},
    ]
    merged, report = _merge_overlapping_directions(slots)
    assert len(merged) == 2 and len(report['removed']) == 1
    kept = next(row for row in merged if 'stmerge01' in row['scope'])
    assert set(kept['fact_refs']) == {'F1', 'F2', 'F3'} and set(kept['issue_refs']) == {'PLAN_A', 'PLAN_B'}
    assert set(kept['area_implication_refs']) == {'I_network', 'I_data'}
    assert len(slots) == 3 and slots[0]['fact_refs'] == ['F1', 'F2'], 'input mutated'
