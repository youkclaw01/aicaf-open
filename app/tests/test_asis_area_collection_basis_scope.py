"""영역 단위 모델 호출은 그 영역의 수집 대상 연결만 받고, 정본 원장은 변경하지 않는다."""
from pathlib import Path
from copy import deepcopy
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from asis_evidence_views import prompt_view
from test_asis_connection import synthetic_input


def _data(tmp_path):
    data = synthetic_input(tmp_path)
    data['context']['collection_basis'] = {'criterionMatches': [
        {'area': area, 'criterion_id': 'C_%s_%d' % (area, i), 'related_resource_ids': ['r'] * 50}
        for area in ('network', 'cost', 'ai') for i in range(5)], 'source_artifacts': {}}
    return data


def test_area_view_keeps_only_own_area_matches_and_declares_the_scope(tmp_path):
    data = _data(tmp_path)
    original = deepcopy(data)
    data['context']['model_scope'] = {'projection_schema': 'asis-model-scope/1', 'area_id': 'ai',
        'fact_refs': [r['id'] for r in data['facts']], 'criterion_refs': [r['id'] for r in data['criteria']],
        'goal_refs': [r['id'] for r in data['goals']], 'inline_fact_refs': []}
    view = prompt_view(data)
    rows = view['context']['collection_basis_matches']
    assert rows and all(row['area'] == 'ai' for row in rows) and len(rows) == 5
    # 자원 ID 전체 목록은 건수와 3개 샘플로 줄이고 정본 원장에는 그대로 남는다.
    assert all('related_resource_ids' not in row and row['related_resource_count'] == 50 and len(row['related_resource_ids_sample']) == 3 for row in rows)
    declared = view['context']['collection_basis_match_scope']
    assert {k: declared[k] for k in ('area_id', 'selected', 'total')} == {'area_id': 'ai', 'selected': 5, 'total': 15}
    assert data['context']['collection_basis'] == original['context']['collection_basis'], 'canonical ledger changed'


def test_whole_document_views_still_receive_every_match(tmp_path):
    data = _data(tmp_path)
    view = prompt_view(data)
    assert len(view['context']['collection_basis_matches']) == 15
    assert 'collection_basis_match_scope' not in view['context']


def test_area_without_assessed_criteria_uses_collection_matches_not_every_criterion(tmp_path):
    """평가에 영역이 없는 영역도 수집 대상 연결이 가리키는 기준만 받고, 연결이 전혀 없을 때만 전체 기준을 받는다."""
    import asyncio, json
    from asis_pipeline import Pipeline
    from test_asis_connection import SyntheticRuntime
    data = synthetic_input(tmp_path)
    data['criteria'] = [{'id': 'C_ai_1', 'source_id': 's', 'quote': 'q', 'source_url': 'u'},
                        {'id': 'C_net_1', 'source_id': 's', 'quote': 'q', 'source_url': 'u'}]
    data['context']['collection_basis'] = {'criterionMatches': [
        {'area': 'ai', 'criterion_id': 'C_ai_1', 'related_resource_ids': ['r']},
        {'area': 'network', 'criterion_id': 'C_net_1', 'related_resource_ids': ['r']}], 'source_artifacts': {}}
    from asis_evidence_views import plan_scopes
    from asis_plan import AREA_NAMES
    scopes = plan_scopes(data, AREA_NAMES)
    # 수집 대상 연결이 기준을 영역에 연결하므로 각 영역은 자신의 기준만 받는다.
    assert scopes['ai']['criterion_refs'] == ['C_ai_1'] and scopes['network']['criterion_refs'] == ['C_net_1']
    assert set(scopes['cost']['criterion_refs']) == set()  # 연결이 없는 영역의 대체 범위는 계획 단계가 정한다.
    import asis_plan, inspect
    source = inspect.getsource(asis_plan.plan_analysis)
    assert 'criterionMatches' in source


def test_issue_plan_integration_view_summarizes_bulk_tables_and_keeps_cited_facts(tmp_path):
    """통합 검토는 인용 사실 전문과 판정·상태를 유지하고, 자원 ID 목록은 건수로 요약한다."""
    from asis_plan import _integration_view
    data = _data(tmp_path)
    original = deepcopy(data)
    fact_id = data['facts'][0]['id']
    scope = {'fact_refs': [fact_id], 'criterion_refs': [r['id'] for r in data['criteria']], 'goal_refs': [r['id'] for r in data['goals']]}
    view = _integration_view(data, scope)
    rows = view['context']['collection_basis_matches']
    assert rows and all('related_resource_ids' not in row and row['related_resource_count'] == 50 for row in rows)
    assert all({'area', 'criterion_id'} <= set(row) for row in rows)
    assert view['facts'] and view['facts'][0]['id'] == fact_id and view['facts'][0]['value'] == data['facts'][0]['value']
    assert data == original, 'canonical input changed'
