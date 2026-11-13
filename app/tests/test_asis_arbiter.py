"""조정자는 blocked를 조치로 분류만 한다. 형식·인용·ID 규칙은 코드가 검증하고, pass 선언 권한은 없다."""
from pathlib import Path
import json, sys
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_arbiter as arb

ITEM = {'topic': '공통 변경 통제의 영역 간 연결과 중복 해소 확인',
        'reason': 'D_P0019·D_P0121의 실제 내용이 제공되지 않아 공통 방향으로의 통합을 대조할 수 없다.',
        'required_evidence': 'D_P0019·D_P0121 본문과 연결 자료', 'fact_refs': []}
FACT_ITEM = {'topic': '근거 값 확인', 'reason': '인용한 F_T_a의 값이 제공되지 않아 본문 주장을 확인할 수 없다.',
             'required_evidence': 'F_T_a 수집 값', 'fact_refs': ['F_T_a']}


def _raw(decisions):
    return json.dumps({'decisions': decisions}, ensure_ascii=False)


def _d(index, action, quote, ids=(), reason='근거'):
    return {'index': index, 'action': action, 'evidence_ids': list(ids), 'quote': quote, 'reason': reason}


def test_valid_decisions_are_parsed_and_combined():
    out = arb.parse_decisions(_raw([_d(0, 'defer_to_merge', 'D_P0019·D_P0121의 실제 내용이 제공되지 않아')]),
                              unresolved=[ITEM], supplied_fact_ids=set(), cited_fact_ids=set())
    assert arb.combine(out) == 'defer_to_merge'


@pytest.mark.parametrize('raw,error', [
    ('not json', 'arbiter_not_json'),
    (json.dumps({'decisions': [], 'extra': 1}), 'arbiter_shape'),
    (_raw([]), 'arbiter_decision_count'),
    (_raw([_d(0, 'pass', 'D_P0019·D_P0121의 실제 내용이 제공되지 않아')]), 'arbiter_action'),
    (_raw([_d(0, 'defer_to_merge', '항목에 없는 문구를 인용했다')]), 'arbiter_quote_not_in_item'),
    (_raw([_d(0, 'defer_to_merge', '짧다')]), 'arbiter_quote_not_in_item'),
    (_raw([_d(1, 'defer_to_merge', 'D_P0019·D_P0121의 실제 내용이 제공되지 않아')]), 'arbiter_decision_index'),
])
def test_invalid_responses_are_rejected(raw, error):
    with pytest.raises(ValueError, match=error):
        arb.parse_decisions(raw, unresolved=[ITEM], supplied_fact_ids=set(), cited_fact_ids=set())


def test_supply_evidence_only_for_cited_and_not_yet_supplied_ids():
    quote = '인용한 F_T_a의 값이 제공되지 않아'
    ok = arb.parse_decisions(_raw([_d(0, 'supply_evidence', quote, ['F_T_a'])]), unresolved=[FACT_ITEM],
                             supplied_fact_ids=set(), cited_fact_ids={'F_T_a'})
    assert arb.evidence_ids(ok) == ['F_T_a']
    for supplied, cited in ((set(), set()), ({'F_T_a'}, {'F_T_a'})):
        with pytest.raises(ValueError, match='arbiter_supply_ids_not_cited_or_already_supplied'):
            arb.parse_decisions(_raw([_d(0, 'supply_evidence', quote, ['F_T_a'])]), unresolved=[FACT_ITEM],
                                supplied_fact_ids=supplied, cited_fact_ids=cited)
    with pytest.raises(ValueError, match='arbiter_ids_only_for_supply'):
        arb.parse_decisions(_raw([_d(0, 'upheld', quote, ['F_T_a'])]), unresolved=[FACT_ITEM],
                            supplied_fact_ids=set(), cited_fact_ids={'F_T_a'})


def test_combine_is_conservative():
    mk = lambda *actions: [{'action': a} for a in actions]
    assert arb.combine(mk('defer_to_merge', 'upheld')) == 'upheld'
    assert arb.combine(mk('defer_to_merge', 'author_repair')) == 'author_repair'
    assert arb.combine(mk('defer_to_merge', 'supply_evidence')) == 'supply_evidence'
    assert arb.combine(mk('defer_to_merge')) == 'defer_to_merge'


def test_arbiter_has_no_action_that_declares_pass():
    assert 'pass' not in arb.ACTIONS and set(arb.ACTIONS) == {'defer_to_merge', 'supply_evidence', 'author_repair', 'upheld'}


def test_pipeline_arbitration_is_bounded_and_records_authority():
    import inspect, asis_pipeline
    source = inspect.getsource(asis_pipeline.Pipeline._reviewed_stage)
    assert 'arbiter_rounds < ARBITER_ROUNDS_LIMIT' in source and asis_pipeline.ARBITER_ROUNDS_LIMIT == 2
    helper = inspect.getsource(asis_pipeline.Pipeline._arbitrate_blocked)
    assert 'classify_only_cannot_declare_pass' in helper and "return 'upheld', []" in helper
