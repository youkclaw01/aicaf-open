"""진단·과제 산출물 품질 채점기 시험.

계약(이전에 정한 원칙 유지):
- 정답은 입력의 실제 값과 코드 검사에서 계산한다. 모델 판단·승인 결과를 정답으로 쓰지 않는다.
- 합산 점수로 순위를 매기지 않는다. 항목별 통과/실패와 사유만 낸다.
- 대조군 산출물과의 비교는 '인용 보존율'로만 한다(문장 유사도로 품질을 재지 않는다).
- 모델 호출이 없다. 같은 입력에는 같은 결과가 나온다.
"""
import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_output_grader as g


def inputs():
    facts = [
        {'id': 'F_A', 'value': {'name': 'AG-HUB-01', 'sku': 'WAF_v2', 'capacity': 2}, 'scope_complete': True},
        {'id': 'F_B', 'value': [{'name': 'vm-web-01', 'count': 12}], 'scope_complete': False},
        {'id': 'F_C', 'value': {'policy': 'WAF-01', 'mode': 'Detection'}, 'scope_complete': True},
    ]
    criteria = [{'id': 'C_1'}, {'id': 'C_2'}]
    return {'facts': facts, 'criteria': criteria, 'goals': [{'id': 'G_1'}], 'directions': [{'id': 'D_1'}]}


def good():
    return {'id': 'J_1', 'text': 'AG-HUB-01의 WAF 정책 WAF-01은 Detection 모드로 확인된다. vm-web-01은 수집 범위가 완전하지 않아 판단을 보류한다.',
            'impact': '탐지만 하고 차단하지 않는 구성이다.', 'fact_refs': ['F_A', 'F_B', 'F_C'], 'criterion_refs': ['C_1'], 'goal_refs': ['G_1'], 'direction_refs': ['D_1']}


def by_name(result):
    return {row['check']: row for row in result['checks']}


def test_a_faithful_candidate_passes_every_check():
    result = g.grade(good(), inputs(), reference=good())
    assert all(row['passed'] for row in result['checks']), [row for row in result['checks'] if not row['passed']]
    assert 'score' not in result and 'rank' not in result, 'no aggregate score'


def test_a_reference_to_a_nonexistent_fact_is_a_failure():
    bad = good(); bad['fact_refs'] = ['F_A', 'F_ZZZ']
    row = by_name(g.grade(bad, inputs(), reference=good()))['citation_validity']
    assert not row['passed'] and 'F_ZZZ' in row['reason']


def test_a_reference_to_a_nonexistent_criterion_goal_or_direction_is_a_failure():
    for key, ghost in (('criterion_refs', 'C_X'), ('goal_refs', 'G_X'), ('direction_refs', 'D_X')):
        bad = good(); bad[key] = [ghost]
        row = by_name(g.grade(bad, inputs(), reference=good()))['citation_validity']
        assert not row['passed'] and ghost in row['reason'], key


def test_dropping_facts_the_reference_cited_lowers_citation_retention_with_the_missing_ids():
    cand = good(); cand['fact_refs'] = ['F_A']
    row = by_name(g.grade(cand, inputs(), reference=good()))['citation_retention']
    assert not row['passed'] and 'F_B' in row['reason'] and 'F_C' in row['reason']
    assert row['detail']['retained'] == 1 and row['detail']['reference'] == 3


def test_citing_extra_valid_facts_does_not_fail_retention():
    cand = good(); cand['fact_refs'] = ['F_A', 'F_B', 'F_C']
    ref = good(); ref['fact_refs'] = ['F_A']
    assert by_name(g.grade(cand, inputs(), reference=ref))['citation_retention']['passed']


def test_a_number_that_exists_nowhere_in_the_input_is_flagged():
    bad = good(); bad['text'] += ' 인스턴스는 47대다.'
    row = by_name(g.grade(bad, inputs(), reference=good()))['number_grounding']
    assert not row['passed'] and '47' in row['reason']


def test_numbers_present_in_the_cited_facts_are_accepted():
    ok = good(); ok['text'] += ' 용량은 2, vm-web-01은 12대로 보고된다.'
    assert by_name(g.grade(ok, inputs(), reference=good()))['number_grounding']['passed']


def test_a_resource_name_that_exists_nowhere_in_the_input_is_flagged():
    bad = good(); bad['text'] += ' 또한 AG-GHOST-99가 운영 중이다.'
    row = by_name(g.grade(bad, inputs(), reference=good()))['name_grounding']
    assert not row['passed'] and 'AG-GHOST-99' in row['reason']


def test_claiming_certainty_about_an_incomplete_scope_is_flagged():
    bad = good(); bad['text'] = 'vm-web-01은 모든 구독에서 전수 확인되었고 이상이 없다.'
    row = by_name(g.grade(bad, inputs(), reference=good()))['incomplete_scope_assertion']
    assert not row['passed'] and 'F_B' in row['reason']


def test_stating_the_incompleteness_is_not_flagged():
    ok = good(); ok['text'] = 'vm-web-01은 수집 범위가 완전하지 않아 확정하지 않는다. AG-HUB-01은 확인된다.'
    assert by_name(g.grade(ok, inputs(), reference=good()))['incomplete_scope_assertion']['passed']


def test_a_japanese_or_hanja_leak_is_flagged():
    bad = good(); bad['text'] += ' これは確認が必要だ。'
    assert not by_name(g.grade(bad, inputs(), reference=good()))['language']['passed']


def test_the_grader_is_deterministic_and_does_not_modify_its_inputs():
    a, b = good(), inputs()
    before = (deepcopy(a), deepcopy(b))
    r1 = g.grade(a, b, reference=good()); r2 = g.grade(a, b, reference=good())
    assert r1 == r2 and (a, b) == before


def test_a_candidate_with_empty_text_fails_structure():
    bad = good(); bad['text'] = ''
    assert not by_name(g.grade(bad, inputs(), reference=good()))['structure']['passed']


def test_the_grader_runs_without_a_reference_and_then_skips_retention_only():
    result = g.grade(good(), inputs(), reference=None)
    names = by_name(result)
    assert names['citation_retention']['skipped'] is True
    assert all(row['passed'] for row in result['checks'] if not row.get('skipped'))


def test_a_sentence_that_names_the_scope_and_states_it_is_unconfirmed_is_not_flagged():
    """단정 패턴(전수·모든 … 확인되었)과 부정 표현(완전하지 않아·보류)이 한 문장에 있으면 확정 서술이 아니다."""
    ok = good()
    ok['text'] = '모든 구독의 VM을 전수 확인했다고 보고되었으나 수집 범위가 완전하지 않아 확정하지 않는다.'
    assert g._ASSERT.search(ok['text']), 'the fixture must actually match the assertion pattern, otherwise this test proves nothing'
    assert by_name(g.grade(ok, inputs(), reference=good()))['incomplete_scope_assertion']['passed']


def test_a_sentence_with_the_same_assertion_and_no_negation_is_flagged():
    bad = good()
    bad['text'] = '모든 구독의 VM을 전수 확인했다.'
    assert g._ASSERT.search(bad['text'])
    assert not by_name(g.grade(bad, inputs(), reference=good()))['incomplete_scope_assertion']['passed']


def test_a_skipped_retention_check_is_reported_as_skipped_and_never_as_a_failure():
    row = by_name(g.grade(good(), inputs(), reference=None))['citation_retention']
    assert row['skipped'] is True and row['passed'] is True, 'a check that did not run must not look like a failure'


def test_a_criterion_code_quoted_in_the_prose_is_not_mistaken_for_a_resource_name():
    """WAF-AI-SE1·AILZ-REL-R1 같은 설계 원칙 코드는 기준(criteria)의 식별 체계다. 입력의 기준에 있으면 근거가 있는 이름이다."""
    data = inputs()
    data['criteria'] = [{'id': 'C_1', 'title': 'WAF-AI-SE1 보안 기준', 'quote': 'AILZ-REL-R1: 안정성 원칙'}, {'id': 'C_2'}]
    ok = good(); ok['text'] += ' WAF-AI-SE1과 AILZ-REL-R1 기준에 비추어 검토한다.'
    assert by_name(g.grade(ok, data, reference=good()))['name_grounding']['passed']


def test_a_criterion_code_that_is_not_in_the_input_criteria_is_still_flagged():
    data = inputs()
    data['criteria'] = [{'id': 'C_1', 'title': 'WAF-AI-SE1 보안 기준'}, {'id': 'C_2'}]
    bad = good(); bad['text'] += ' WAF-AI-ZZ9 기준에 비추어 검토한다.'
    row = by_name(g.grade(bad, data, reference=good()))['name_grounding']
    assert not row['passed'] and 'WAF-AI-ZZ9' in row['reason']


def test_a_goal_quote_name_is_grounded_by_the_goal_text():
    data = inputs()
    data['goals'] = [{'id': 'G_1', 'quote': '고객은 AG-EDGE-02를 확장하려 한다'}]
    ok = good(); ok['text'] += ' AG-EDGE-02 확장 계획과 연결된다.'
    assert by_name(g.grade(ok, data, reference=good()))['name_grounding']['passed']
