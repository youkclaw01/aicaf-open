"""요약(summary) 입력: 승인된 영역·방향·과제를 ID와 핵심 문장으로 연결하되 모델 입력 한도를 넘지 않는다.

운영 실측(2026-10-16): 통합 후 요약 입력이 286만 자(110만 토큰)로 모델 상한(92.2만 토큰)을 넘어 서비스가 400으로 거절했고 개정이 중지됐다.
미확인 본문 83만 자와 배경 context 112만 자가 원인이었다. 입력은 실제 계약을 만족하는 합성 분석 결과(asis_analysis.analyze)를 부풀려 만든다.
"""
from copy import deepcopy
from pathlib import Path
import json, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
import asis_analysis
import asis_model_view as mv
from asis_pipeline import Pipeline
from test_asis_connection import synthetic_input


def summary_data(tmp_path, unresolved=300, directions=60, fact_chars=30_000):
    seed = asis_analysis.analyze(synthetic_input(tmp_path))
    data = deepcopy(seed['input'])
    areas, dirs, acts = deepcopy(seed['stages'][0]), deepcopy(seed['stages'][1]), deepcopy(seed['stages'][2])
    unres = [{'topic': '미확인 %d' % i, 'reason': 'r' * 500, 'required_evidence': 'e' * 200, 'fact_refs': [data['facts'][0]['id']]} for i in range(unresolved)]
    for block in (areas, dirs, acts):
        block['unresolved'] = deepcopy(unres)
    base = dirs['directions'][0]
    dirs['directions'] = [dict(deepcopy(base), id='D_X%04d' % i, text='본문 ' * 200, **{'x_only_in_canonical': 'y' * 500}) for i in range(directions)]
    for fact in data['facts']:
        # 값이 있는 사실(수집됨)만 크게 만든다. 값이 null이어야 하는 사실(미확인)은 계약상 그대로 둔다.
        if fact.get('value') is not None:
            fact['value'] = {'blob': 'x' * fact_chars}
    data['context']['collection_basis_matches'] = [{'x': 'y' * 1000}] * 400
    data['context']['action_item_plan'] = {'consolidation_trace': {'diagnoses': [{'t': 'z' * 500}] * 300}}
    data['context']['analysis_plan'] = {'issues': [{'id': 'p%d' % i, 'reason': 'r' * 500} for i in range(255)]}
    data['stage_data'] = {'mode': 'summary', 'approved_areas': areas, 'approved_directions': dirs, 'approved_actions': acts}
    return data


def test_kind_of_recognises_the_summary_call_and_no_other_stage(tmp_path):
    data = summary_data(tmp_path)
    assert mv.kind_of(4, data) == 'summary_author'
    assert mv.kind_of(3, data) is None and mv.kind_of(5, data) is None


def test_unresolved_items_stay_listed_by_topic_and_their_text_is_left_to_the_ledger(tmp_path):
    view = mv.summary_view(summary_data(tmp_path, unresolved=300))
    for block in ('approved_areas', 'approved_directions', 'approved_actions'):
        rows = view['stage_data'][block]['unresolved']
        assert len(rows) == 300, 'no unresolved item disappears from the summary input'
        assert all(set(row) == {'topic', 'fact_ref_count'} for row in rows)
        assert view['stage_data'][block]['unresolved_note']['total'] == 300


def test_background_context_and_plan_are_not_sent_and_cited_facts_stay_listed(tmp_path):
    data = summary_data(tmp_path)
    view = mv.summary_view(data)
    for name in ('collection_basis_matches', 'action_item_plan', 'analysis_plan'):
        assert name not in view['context']
    cited = set()
    for block in ('approved_areas', 'approved_directions', 'approved_actions'):
        cited |= mv.refs_in(data['stage_data'][block], 'fact_refs')
    ids = {row['id'] for row in view['facts']}
    assert cited and cited <= ids, 'every fact the approved results cite stays in the input'
    for row in view['facts']:
        if isinstance(row['value'], dict) and row['value'].get('blob'):
            raise AssertionError('large cited fact body must not be sent: ' + row['id'])
        assert row['value'] is None or row['value'].get('not_inlined') or mv._chars(row['value']) <= mv.SUMMARY_FACT_INLINE_CHARS


def test_directions_carry_ids_and_core_sentences_only(tmp_path):
    view = mv.summary_view(summary_data(tmp_path))
    for row in view['stage_data']['approved_directions']['directions']:
        assert 'x_only_in_canonical' not in row and {'id', 'title', 'text'} <= set(row)


def test_the_canonical_input_is_not_modified(tmp_path):
    data = summary_data(tmp_path)
    before = json.dumps(data, sort_keys=True, ensure_ascii=False)
    mv.summary_view(data)
    assert json.dumps(data, sort_keys=True, ensure_ascii=False) == before


def test_the_real_render_path_sends_the_reduced_view_and_stays_under_the_limit(tmp_path):
    data = summary_data(tmp_path, unresolved=900, directions=93, fact_chars=60_000)
    assert len(json.dumps(data, ensure_ascii=False)) > mv.LIMITS['summary_author'], 'the raw input alone exceeds the limit'
    engine = Pipeline.__new__(Pipeline)
    engine.validator = asis_analysis.contract()
    system, prompt = engine.render_prompt(4, data)
    sent = prompt.split('입력:\n', 1)[-1]
    assert len(sent) < mv.LIMITS['summary_author']
    assert 'consolidation_trace' not in sent and 'collection_basis_matches' not in sent


def test_an_input_that_is_still_too_large_fails_before_the_model_call(tmp_path):
    data = summary_data(tmp_path, directions=200)
    old = mv.LIMITS['summary_author']
    mv.LIMITS['summary_author'] = 50_000
    try:
        with pytest.raises(ValueError, match='model_input_over_limit:summary_author'):
            mv.build(4, data)
    finally:
        mv.LIMITS['summary_author'] = old


def review_of(data):
    """요약 검토 호출의 입력: 작성과 같은 승인 결과 + 검토할 후보(draft)."""
    reviewed = deepcopy(data)
    # 후보만 인용하는 사실을 고른다: 승인된 영역·방향·과제가 인용하지 않는 사실이어야 '후보가 인용한 사실도 전송한다'는 규칙을 시험할 수 있다.
    cited = set()
    for block in ('approved_areas', 'approved_directions', 'approved_actions'):
        cited |= mv.refs_in(reviewed['stage_data'][block], 'fact_refs')
    first_fact = next(row['id'] for row in reviewed['facts'] if row['id'] not in cited)
    reviewed['stage_data']['draft'] = {'run': reviewed['run'], 'stage': 'summary',
                                       'summary': {'headline': 'h', 'message': 'm', 'key_points': [{'id': 'K1', 'text': 't', 'fact_refs': [first_fact], 'criterion_refs': [], 'goal_refs': []}],
                                                   'direction_refs': [], 'action_refs': [], 'decision_points': []}, 'unresolved': []}
    return reviewed


def test_the_summary_review_call_is_recognised_and_reduced_like_the_author_call(tmp_path):
    """작성만 줄이면 검토 호출이 같은 한도를 넘는다(운영 2026-10-16 13:00: 작성 입력은 한도 안, 검토 입력 129만 토큰)."""
    data = review_of(summary_data(tmp_path, unresolved=900, directions=93, fact_chars=60_000))
    assert mv.kind_of(5, data) == 'summary_review'
    engine = Pipeline.__new__(Pipeline)
    engine.validator = asis_analysis.contract()
    system, prompt = engine.render_prompt(5, data)
    sent = prompt.split('입력:\n', 1)[-1]
    assert len(sent) < mv.LIMITS['summary_review'] < len(json.dumps(data, ensure_ascii=False))
    view = json.loads(sent[sent.index('{'):])
    assert isinstance(view['stage_data']['draft'], dict), 'the candidate under review is kept whole'
    candidate_fact = data['stage_data']['draft']['summary']['key_points'][0]['fact_refs'][0]
    assert candidate_fact in {row['id'] for row in view['facts']}, 'a fact only the candidate cites is still sent'


def test_other_review_calls_are_not_treated_as_summary_reviews(tmp_path):
    data = review_of(summary_data(tmp_path))
    data['stage_data']['mode'] = 'actions'
    assert mv.kind_of(5, data) is None
