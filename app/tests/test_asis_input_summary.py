"""길이로 2번 이상 실패한 항목만 코드가 결정적으로 요약해 보내는 규칙의 계약 시험(모델 호출 없음).

운영 2026-10-18(프로젝트 25, 10단계 과제 A_ITEM_0015·A_ITEM_0001): 입력이 모델 한도에 닿아 실패했고 무손실로 줄일 곳이 없었다.
영석님 지시: 과제 수는 늘리지 않는다. 이름·숫자 등 변하면 안 되는 내용을 제외하고, 길이로 2번 이상 실패한 경우는 요약해서 보낸다.
"""
from pathlib import Path
import copy
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_input_summary as S


def sample_view():
    long_text = ('이 방향은 운영 전반의 변경 관리 체계를 정비하는 것이다. 현재 구성은 부분 수집 상태이며 검증되지 않았다. 관련 배포 gpt-6-astra 의 토큰은 1,234,567 건이다. '
                 '조직은 장기적으로 여러 환경에서 일관된 절차를 갖추는 것이 바람직하다. 이는 일반적인 권고이며 세부 항목은 별도 문서에 둔다. 담당 체계는 추후 정한다.')
    rows = [{'a': i, 'b': 'x' * 400, 'c': None} for i in range(10)]
    return {
        'facts': [{'id': 'F_direct', 'value': rows}, {'id': 'F_indirect', 'value': rows}, {'id': 'F_text', 'value': '고객 진술 원문 문장이다. ' * 30}],
        'stage_data': {
            'item_assignment': {'slot': {'fact_refs': ['F_direct']}},
            'approved_directions': {'directions': [{'id': 'D_P0001', 'text': long_text, 'change_direction': long_text, 'customer_impact': long_text, 'fact_refs': ['F_direct']}],
                                    'unresolved': [{'topic': '키 운영', 'reason': long_text, 'fact_refs': ['F_indirect']}]},
            'approved_areas': {'areas': [{'area_id': 'ai', 'implication': {'id': 'I_ai', 'text': long_text, 'impact': long_text, 'fact_refs': []}}], 'unresolved': []},
            'approved_diagnoses': {'diagnoses': [{'id': 'J_ITEM_0001', 'text': long_text, 'impact': long_text}], 'unresolved': []},
        },
    }


def size(value):
    return len(json.dumps(value, ensure_ascii=False).encode('utf-8'))


def test_level_one_shortens_approved_narratives_but_keeps_every_name_and_number():
    view = sample_view()
    out, report = S.summarize_item_input(view, 1, ['F_direct'])
    assert report['reverted_fields'] == 0 and report['narratives'] >= 5 and size(out) < size(view)
    text = out['stage_data']['approved_directions']['directions'][0]['text']
    for must in ('gpt-6-astra', '1,234,567', '부분 수집', '검증되지 않았다'):
        assert must in text, must
    assert S.tokens(view['stage_data']['approved_directions']) <= S.tokens(out['stage_data']['approved_directions'])


def test_level_one_leaves_facts_and_unresolved_untouched():
    view = sample_view()
    out, _ = S.summarize_item_input(view, 1, ['F_direct'])
    assert out['facts'] == view['facts']
    assert out['stage_data']['approved_directions']['unresolved'] == view['stage_data']['approved_directions']['unresolved']


def test_level_two_also_summarizes_unresolved_reasons_and_indirect_list_facts():
    view = sample_view()
    out, report = S.summarize_item_input(view, 2, ['F_direct'])
    assert report['list_facts'] == 1 and report['unresolved_reasons'] == 1
    facts = {f['id']: f for f in out['facts']}
    assert facts['F_direct']['value'] == view['facts'][0]['value'], '슬롯이 직접 인용한 사실의 값은 요약하지 않는다'
    indirect = facts['F_indirect']['value']
    assert indirect['summarized_list'] and indirect['row_count'] == 10 and len(indirect['sample_rows']) == 3 and indirect['fields'] == ['a', 'b', 'c']
    assert facts['F_text']['value'] == view['facts'][2]['value'], '고객 진술 등 문자열 사실은 요약하지 않는다'
    assert size(out) < size(S.summarize_item_input(view, 1, ['F_direct'])[0])


def test_the_source_view_is_never_modified():
    view = sample_view()
    before = copy.deepcopy(view)
    S.summarize_item_input(view, 2, ['F_direct'])
    assert view == before


def test_a_field_that_would_lose_a_name_or_number_is_reverted_even_if_the_same_value_exists_elsewhere(monkeypatch):
    """전체 집합 비교는 같은 숫자가 다른 필드에 남아 있으면 한 필드의 손실을 가린다. 필드마다 검사해 그 필드만 원문으로 되돌린다."""
    view = sample_view()
    direction = view['stage_data']['approved_directions']['directions'][0]
    original_text = direction['text']
    real = S.condense_text
    # 숫자를 지워 버리는 잘못된 요약기: 'text' 필드에서만 동작하도록 해 다른 필드(change_direction)에는 숫자가 남게 한다.
    monkeypatch.setattr(S, 'condense_text', lambda text: text.replace('1,234,567', '') if text == original_text and 'customer_impact' else real(text))
    out, report = S.summarize_item_input(view, 1, ['F_direct'])
    got = out['stage_data']['approved_directions']['directions'][0]
    assert got['text'] == original_text, '숫자를 지운 요약은 이 필드를 원문으로 되돌려야 한다'
    assert report['reverted_fields'] >= 1 and '1,234,567' in ' '.join(report['lost_tokens'])
    assert '1,234,567' in S.tokens(out['stage_data']['approved_directions'])


def test_every_field_keeps_all_of_its_own_tokens_after_a_normal_summary():
    view = sample_view()
    out, report = S.summarize_item_input(view, 2, ['F_direct'])
    assert report['reverted_fields'] == 0
    for name in ('approved_directions', 'approved_areas', 'approved_diagnoses'):
        a, b = view['stage_data'][name], out['stage_data'][name]
        for before, after in zip(json.dumps(a, ensure_ascii=False).split('"'), json.dumps(b, ensure_ascii=False).split('"')):
            pass
    d0, d1 = view['stage_data']['approved_directions']['directions'][0], out['stage_data']['approved_directions']['directions'][0]
    for key in ('text', 'change_direction', 'customer_impact'):
        assert S.tokens(d0[key]) <= S.tokens(d1[key]), key


def test_short_text_and_single_sentence_text_are_not_touched():
    assert S.condense_text('짧은 문장이다.') == '짧은 문장이다.'
    one = '문장 하나만 있고 길이는 길지만 마침표로 나뉘지 않는다 ' * 8
    assert S.condense_text(one) == one


def test_an_invalid_level_is_rejected():
    for level in (0, 3, None, '1'):
        try:
            S.summarize_item_input(sample_view(), level, [])
        except ValueError as exc:
            assert 'invalid_summary_level' in str(exc)
        else:
            raise AssertionError(level)


# ── 길이 실패 횟수와 요약 단계 ───────────────────────────────────────────────────────────────────────────────

def test_summary_level_follows_the_number_of_length_failures(tmp_path):
    import asis_length_failures as L
    root = tmp_path / 'checkpoints' / 'action-A_ITEM_0001'
    assert L.summary_level(root) == 0
    L.record(root, kind='model_prompt_budget', tokens=299366, limit=272000)
    assert L.summary_level(root) == 0, '1회 실패는 원문 그대로 다시 보낸다(영석님: 2번 이상 실패한 경우만 요약)'
    L.record(root, kind='model_input_over_limit', tokens=None, limit=None)
    assert L.summary_level(root) == 1
    L.record(root, kind='model_prompt_budget', tokens=290000, limit=272000)
    assert L.summary_level(root) == 2
    L.record(root, kind='model_prompt_budget', tokens=280000, limit=272000)
    assert L.summary_level(root) == 2, '2단계가 마지막이다'


def test_length_failures_survive_a_restart_and_keep_their_details(tmp_path):
    import asis_length_failures as L
    root = tmp_path / 'checkpoints' / 'action-A_ITEM_0015'
    L.record(root, kind='model_prompt_budget', tokens=299366, limit=272000)
    L.record(root, kind='model_prompt_budget', tokens=299368, limit=272000)
    rows = json.loads((root / 'length-failures.json').read_text(encoding='utf-8'))
    assert rows['count'] == 2 and [r['tokens'] for r in rows['failures']] == [299366, 299368] and all('at' in r for r in rows['failures'])
    assert L.summary_level(root) == 1


def test_only_length_failures_are_counted():
    import asis_length_failures as L
    assert L.is_length_failure(RuntimeError("Request exceeds the model's prompt budget after reserving output (299366 tokens; limit 272000)"))
    assert L.is_length_failure(ValueError('model_input_over_limit:item_author:950000>900000'))
    assert not L.is_length_failure(RuntimeError('some other failure'))
    assert not L.is_length_failure(TimeoutError('timeout'))
    assert L.parse(RuntimeError("Request exceeds the model's prompt budget after reserving output (299366 tokens; limit 272000)")) == ('model_prompt_budget', 299366, 272000)
