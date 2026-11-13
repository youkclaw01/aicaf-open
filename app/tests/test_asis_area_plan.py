"""영역별 호출 필요성 판정(코드) 시험. docs/EVIDENCE-STORE-DESIGN-V2.md 5.3의 규칙을 고정한다.

판정은 DB에서 읽은 수집 상태와 인터뷰 연결만으로 정한다. 모델이 정하지 않는다.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_area_plan as plan
import asis_evidence_store as store
from test_asis_evidence_store import sample, RID


def make_db(tmp_path, *, interviews=True, extra=None):
    s = sample()
    if extra:
        extra(s)
    path = tmp_path / 'e.db'
    store.load(path, facts=s['facts'], basis=s['basis'], criteria=s['criteria'], assessments=s['assessments'], run=s['run'])
    return store.open_project(path)


def test_each_area_lists_its_structured_facts_from_collector_area_refs(tmp_path):
    h = make_db(tmp_path)
    ai = plan.area_inputs(h, 'ai')
    assert [f['fact_id'] for f in ai['facts']] == ['F_T_foundryAccountConfiguration']
    cost = plan.area_inputs(h, 'cost')
    assert [f['fact_id'] for f in cost['facts']] == ['F_E_pricesModels']
    assert plan.area_inputs(h, 'secgov')['facts'] == [], 'secgov has no dataset in the sample'


def test_each_fact_carries_its_state_so_that_absence_and_missing_are_not_confused(tmp_path):
    h = make_db(tmp_path)
    t = {f['fact_id']: f for f in plan.area_inputs(h, 'telemetry')['facts']}['F_E_alertRules']
    assert t['state'] == 'not_collected'
    c = {f['fact_id']: f for f in plan.area_inputs(h, 'cost')['facts']}['F_E_pricesModels']
    assert c['state'] == 'observed_complete' and c['record_count'] == 3  # 표본의 가격표는 scope_complete=True


def test_state_classification_covers_every_combination():
    assert plan.fact_state('collected', True, 5) == 'observed_complete'
    assert plan.fact_state('collected', False, 5) == 'observed'
    assert plan.fact_state('not_present', True, 0) == 'absent_confirmed'
    assert plan.fact_state('collected', True, 0) == 'absent_confirmed'
    assert plan.fact_state('collected', False, 0) == 'empty_unconfirmed'
    assert plan.fact_state('not_collected', False, None) == 'not_collected'
    assert plan.fact_state('permission_denied', False, None) == 'not_collected'
    assert plan.fact_state('collection_failed', False, None) == 'not_collected'


def test_every_area_receives_all_interview_questions_because_one_area_cites_all_four_groups(tmp_path):
    """평가 24 실측: 승인된 관측이 인용한 인터뷰 889건 중 접두 표(XA~XD) 안에 있는 것은 46%뿐이었다.
    영역별로 문항을 고르면 필요한 진술의 절반 이상을 빠뜨리므로, 모든 영역이 전체 문항을 받는다."""
    h = make_db(tmp_path)
    for area in plan.AREAS:
        assert plan.area_inputs(h, area)['interview_ids'] == ['XA1-1', 'XB2-3'], area


def test_the_prefix_table_is_not_used_to_select_interviews():
    import inspect
    assert 'AREA_PREFIXES' not in inspect.getsource(plan.area_inputs)
    assert 'AREA_PREFIXES' not in inspect.getsource(plan.plan_areas)


def test_no_interview_and_all_facts_complete_means_no_model_call_is_needed():
    verdict = plan.decide({'facts': [{'state': 'observed_complete'}, {'state': 'absent_confirmed'}], 'interview_ids': []})
    assert verdict['model_call'] is False and verdict['reason'] == 'structured_only_complete'


def test_interviews_with_no_structured_fact_need_a_model_for_the_statement_only():
    verdict = plan.decide({'facts': [], 'interview_ids': ['XA1-1']})
    assert verdict['model_call'] is True and verdict['reason'] == 'interview_only' and verdict['input'] == 'interviews'


def test_interviews_with_structured_facts_need_a_model_for_the_comparison():
    verdict = plan.decide({'facts': [{'state': 'observed'}], 'interview_ids': ['XB1-1']})
    assert verdict['model_call'] is True and verdict['reason'] == 'interview_vs_structured' and verdict['input'] == 'pairs'


def test_structured_only_but_incomplete_still_needs_a_model_to_phrase_the_limits():
    verdict = plan.decide({'facts': [{'state': 'observed'}, {'state': 'not_collected'}], 'interview_ids': []})
    assert verdict['model_call'] is True and verdict['reason'] == 'structured_incomplete'


def test_nothing_at_all_is_reported_as_no_evidence_without_a_model_call():
    verdict = plan.decide({'facts': [], 'interview_ids': []})
    assert verdict['model_call'] is False and verdict['reason'] == 'no_evidence'


def test_plan_covers_all_nine_areas_and_reports_the_counts_behind_each_verdict(tmp_path):
    h = make_db(tmp_path)
    out = plan.plan_areas(h)
    assert [a['area'] for a in out['areas']] == ['network', 'compute', 'data', 'ai', 'secgov', 'telemetry', 'quality', 'operating', 'cost']
    for a in out['areas']:
        assert {'area', 'model_call', 'reason', 'fact_count', 'interview_count', 'state_counts'} <= set(a)
    assert out['summary']['areas'] == 9
    assert out['summary']['model_calls'] + out['summary']['code_only'] == 9


def test_plan_reads_only_through_the_project_handle(tmp_path):
    h = make_db(tmp_path)
    log = tmp_path / 'log.jsonl'
    h2 = store.open_project(h.path, access_log=log, task='plan')
    plan.plan_areas(h2)
    import json
    fns = {json.loads(x)['function'] for x in log.read_text(encoding='utf-8').splitlines()}
    assert fns <= {'area_facts', 'all_interview_ids'}


def test_decision_is_deterministic(tmp_path):
    h = make_db(tmp_path)
    assert plan.plan_areas(h) == plan.plan_areas(h)
