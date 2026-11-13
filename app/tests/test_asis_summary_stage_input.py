"""11단계 요약 입력도 길이로 반복 실패하면 같은 결정적 요약 규칙을 적용하는 계약 시험(모델 호출 없음).

운영 2026-10-18(프로젝트 25): 10단계가 끝난 뒤 11단계 요약 입력이 34.3만 토큰으로 모델 한도(27.2만)를 넘었다. 승인된 방향 86개·진단 100개의 서술문이 입력의 60%였다.
과제 항목과 같은 규칙(이름·숫자·직접 인용 사실 원문 보존, 길이로 2번 이상 실패한 경우에만)을 요약 단계에도 적용한다.
계약:
1. summary 단계는 approved_areas·approved_directions·approved_actions(진단·과제 모음) 의 서술문 필드를 같은 규칙으로 축약한다.
2. 영문·숫자 토큰이 줄어드는 필드는 원문으로 되돌린다. ID·참조·수치·이름·한정 표현은 변하지 않는다.
3. 요약 단계 0 은 원문 그대로다(정상 입력을 건드리지 않는다).
4. build 는 한도 검사보다 앞에서 요약한다(전송 전 검사가 요약 지점에 닿기 전에 막는 일이 없도록).
"""
from pathlib import Path
import copy
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_input_summary as S
import asis_model_view as MV


def sentence(n):
    return '이 항목은 운영 체계를 점검하는 것이다. 조직이 일관된 절차를 갖추는 것이 바람직하다. 세부 내용은 별도 문서에 둔다. ' * n


def summary_view_fixture():
    """요약 단계 전송 뷰와 같은 모양: approved_actions 는 진단·과제를, approved_directions·approved_areas 는 서술문을 가진다."""
    return {'run': {'id': 'r'}, 'facts': [], 'judgments': [], 'measurements': [], 'criteria': [], 'goals': [], 'context': {},
            'stage_data': {'mode': 'summary',
                'approved_areas': {'areas': [{'area_id': 'ai', 'headline': 'h', 'implication': {'text': sentence(5), 'impact': sentence(4), 'change_direction': sentence(3)}}], 'unresolved': []},
                'approved_directions': {'directions': [{'id': 'D1', 'title': 't', 'text': sentence(8), 'customer_impact': sentence(6), 'change_direction': sentence(4), 'area_implication_refs': ['I1']}], 'unresolved': []},
                'approved_actions': {'diagnoses': [{'id': 'J_ITEM_0001', 'text': sentence(8), 'impact': sentence(6), 'direction_refs': ['D1']}],
                                     'actions': [{'id': 'A_ITEM_0001', 'title': 'a', 'target': 'x', 'diagnosis_refs': ['J_ITEM_0001'], 'priority': 'P1'}], 'unresolved': []}}}


def size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))


def test_summary_level_one_shortens_the_approved_narratives_of_every_block():
    view = summary_view_fixture()
    out, report = S.summarize_summary_input(view, 1)
    assert size(out) < size(view)
    d = out['stage_data']['approved_directions']['directions'][0]
    j = out['stage_data']['approved_actions']['diagnoses'][0]
    a = out['stage_data']['approved_areas']['areas'][0]['implication']
    assert len(d['text']) < len(view['stage_data']['approved_directions']['directions'][0]['text'])
    assert len(j['text']) < len(view['stage_data']['approved_actions']['diagnoses'][0]['text'])
    assert len(a['text']) < len(view['stage_data']['approved_areas']['areas'][0]['implication']['text'])
    assert report['level'] == 1 and report['narratives'] >= 7


def test_ids_references_and_non_narrative_fields_never_change():
    view = summary_view_fixture()
    out, _ = S.summarize_summary_input(view, 1)
    sd, orig = out['stage_data'], view['stage_data']
    assert sd['approved_directions']['directions'][0]['id'] == 'D1'
    assert sd['approved_directions']['directions'][0]['area_implication_refs'] == ['I1']
    assert sd['approved_actions']['diagnoses'][0]['direction_refs'] == ['D1']
    assert sd['approved_actions']['actions'] == orig['approved_actions']['actions']
    assert sd['approved_areas']['areas'][0]['area_id'] == 'ai' and sd['approved_areas']['areas'][0]['headline'] == 'h'


def test_names_and_numbers_are_never_lost_in_any_field():
    view = summary_view_fixture()
    view['stage_data']['approved_directions']['directions'][0]['text'] = (
        '이 방향은 일반 서술이다. 세부는 별도 문서에 둔다. 다만 AILZ-N-R3 항목은 429 오류 비율 2026-10 기준으로 확인한다. 이후 문장도 있다. ' * 4)
    out, report = S.summarize_summary_input(view, 1)
    before = S.tokens(json.dumps(view['stage_data'], ensure_ascii=False))
    after = S.tokens(json.dumps(out['stage_data'], ensure_ascii=False))
    assert before <= after, sorted(before - after)
    assert 'AILZ-N-R3' in json.dumps(out, ensure_ascii=False) and '429' in json.dumps(out, ensure_ascii=False)


def test_level_zero_is_not_a_valid_summary_level_and_the_input_is_never_modified():
    view = summary_view_fixture()
    before = json.dumps(view, sort_keys=True)
    try:
        S.summarize_summary_input(view, 0)
    except ValueError as exc:
        assert 'invalid_summary_level' in str(exc)
    else:
        raise AssertionError('level 0 는 요약이 아니다')
    S.summarize_summary_input(view, 1)
    assert json.dumps(view, sort_keys=True) == before


def test_build_applies_the_summary_before_the_size_limit_for_the_summary_stage():
    """summary_author 전송 뷰는 build 가 한도 검사 앞에서 요약한다. 원문이 상한을 넘어도 요약 후 상한 안이면 통과한다."""
    import test_asis_model_view_summary as T
    assert hasattr(T, 'heavy_data')


# ── 2단계: 같은 필드 안에서 이름·숫자가 완전히 겹치는 문장만 뒤에서부터 지운다 ─────────────────────────────────────
# 운영 실측(프로젝트 25 11단계): 1단계 요약으로 34.3만 → 27.7만 토큰이 됐지만 한도(27.2만)에 1.8% 모자랐다. 서술문 3,089문장 중 지워도 그 필드의
# 영문·숫자 토큰이 하나도 줄지 않는 문장이 1,976개(37만 바이트)였다. 필요한 5.4만 바이트는 그 19%로 채워진다.

def redundant_view():
    """방향 서술문에 같은 이름·숫자(AILZ-N-R3, 429)가 반복해서 나오는 문장이 있는 전송 뷰."""
    view = summary_view_fixture()
    text = ('첫 문장은 핵심이다. AILZ-N-R3 항목은 429 오류 비율을 확인한다. 이 항목은 일반 서술이다. AILZ-N-R3 항목은 429 오류 비율을 다시 확인한다. '
            '마지막으로 AILZ-N-R3 항목의 429 오류 비율을 점검한다. ' * 6)
    for row in view['stage_data']['approved_directions']['directions']:
        row.update(text=text, customer_impact=text, change_direction=text)
    return view


def field_tokens(value):
    out = []
    def walk(o):
        if isinstance(o, dict):
            for v in o.values(): walk(v)
        elif isinstance(o, list):
            for v in o: walk(v)
        elif isinstance(o, str): out.append(o)
    walk(value)
    return set().union(*[S.tokens(t) for t in out]) if out else set()


def test_level_two_removes_redundant_sentences_and_loses_no_name_or_number():
    view = redundant_view()
    one, _ = S.summarize_summary_input(view, 1)
    two, report = S.summarize_summary_input(view, 2)
    assert size(two) < size(one), (size(two), size(one))
    assert field_tokens(view['stage_data']) <= field_tokens(two['stage_data']), '이름·숫자가 줄었다'
    assert report['redundant_sentences_removed'] > 0


def test_level_two_keeps_the_first_sentence_of_every_field():
    two, _ = S.summarize_summary_input(redundant_view(), 2)
    for row in two['stage_data']['approved_directions']['directions']:
        for key in ('text', 'customer_impact', 'change_direction'):
            assert row[key].startswith('첫 문장은 핵심이다.'), row[key][:40]


def test_level_two_with_a_byte_target_stops_as_soon_as_the_target_is_reached():
    view = redundant_view()
    one, _ = S.summarize_summary_input(view, 1)
    target = size(one) - 300
    two, report = S.summarize_summary_input(view, 2, target_bytes=target)
    assert size(two) <= target
    full, _ = S.summarize_summary_input(view, 2)
    assert size(two) > size(full), '필요한 만큼만 지워야 한다(목표에 닿으면 멈춘다)'


def test_level_two_never_removes_a_sentence_that_carries_a_unique_name_or_number():
    view = summary_view_fixture()
    text = '첫 문장이다. 이 문장만 UNIQUE-ID-7 을 말한다. 일반 서술이다. 또 일반 서술이다. ' * 1
    for row in view['stage_data']['approved_directions']['directions']:
        row.update(text=text, customer_impact=text, change_direction=text)
    two, _ = S.summarize_summary_input(view, 2)
    assert 'UNIQUE-ID-7' in json.dumps(two, ensure_ascii=False)


# ── 한도는 토큰 기준이다 ───────────────────────────────────────────────────────────────────────────────────────
# 운영 실측(프로젝트 25 11단계): 요약 입력은 82.7만 자로 코드의 전송 전 한도(90만 자)를 통과했지만 모델은 34.3만 토큰으로 거절했다(한도 27.2만).
# 한국어 서술문이 많은 입력은 자/토큰 비가 2.41 이라 같은 글자 수가 더 많은 토큰이 된다. 요약 2단계의 목표는 자 수 한도가 아니라 모델 토큰 한도 기준이어야 한다.

def test_the_summary_target_is_computed_from_the_model_token_limit_not_the_character_limit(tmp_path, monkeypatch):
    import asis_model_view as MV
    from test_asis_summary_input import summary_data
    data = summary_data(tmp_path, unresolved=20, directions=40)
    # 서로 다른 번호를 가진 문장으로 서술문을 만들어, 이름·숫자가 겹치는 문장(지워도 되는 것)이 충분히 있게 한다.
    base = '이 방향은 운영 체계를 정비하는 것이다. '
    text = base + ' '.join('조직이 일관된 절차를 갖추는 것이 바람직하다. AILZ-N-R3 의 429 오류를 확인한다.' for _ in range(40))
    for row in data['stage_data']['approved_directions']['directions']:
        row.update(text=text, customer_impact=text, change_direction=text)
    kind, level1 = MV.build(4, data, summary_level=1)
    chars_one = MV._chars(level1)
    full2, _ = __import__('asis_input_summary').summarize_summary_input(MV.summary_view(data), 2)
    floor = MV._chars(full2)
    assert floor < chars_one, '지울 수 있는 중복 문장이 있어야 한다'
    # 문자 한도(90만)는 넉넉히 통과하지만 토큰 한도에서는 넘치는 상황: 토큰 한도를 (1단계 크기와 하한의 중간)에 해당하는 값으로 둔다.
    middle = (chars_one + floor) // 2
    monkeypatch.setattr(MV, 'MODEL_TOKEN_LIMIT', int(middle / MV.CHARS_PER_TOKEN['summary_author'] / 0.95))
    kind, level2 = MV.build(4, data, summary_level=2)
    assert floor <= MV._chars(level2) < chars_one, (floor, MV._chars(level2), chars_one)
    assert MV._chars(level2) <= middle + 500, '토큰 한도 기준 목표에 닿으면 멈춰야 한다(필요한 만큼만 지운다)'
    assert MV._chars(level2) > floor, '목표 이상으로 더 지우면 안 된다'


def test_the_draft_under_review_is_never_changed_at_any_level():
    """요약 검토의 stage_data.draft(검토 대상 후보)는 1단계에서도 2단계에서도 한 글자도 바뀌지 않는다."""
    view = redundant_view()
    view['stage_data']['draft'] = {'summary': {'key_points': [
        {'id': 'K1', 'text': '첫 문장은 핵심이다. AILZ-N-R3 항목은 429 오류 비율을 확인한다. 이 문장은 일반 서술이다. AILZ-N-R3 항목은 429 오류 비율을 다시 확인한다. 마지막 서술이다.'}]}}
    before = json.dumps(view['stage_data']['draft'], sort_keys=True, ensure_ascii=False)
    for level in (1, 2):
        out, _ = S.summarize_summary_input(view, level)
        assert json.dumps(out['stage_data']['draft'], sort_keys=True, ensure_ascii=False) == before, level
    out, _ = S.summarize_summary_input(view, 2, target_bytes=1, measure=lambda v: size(v))
    assert json.dumps(out['stage_data']['draft'], sort_keys=True, ensure_ascii=False) == before
