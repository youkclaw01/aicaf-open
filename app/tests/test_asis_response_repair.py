"""모델 응답 끝의 불필요한 닫는 괄호를 코드가 정리하는 계약 시험(모델 호출 없음).

2026-10-18 운영(프로젝트 25, 7단계 방향 작성): luna 응답 88건 중 3건이 완전한 JSON 객체 뒤에 '}' 한 글자가 더 붙어 있었다
(3462자에서 JSON 종료, 3463자째 '}'). strict_load 가 'Extra data' 로 거부해 같은 결함으로 보완 3회를 소진했고 방향 하나가 failed 가 됐다.
본문은 완전하고 의미도 정확했다. 모델 보완 예산을 쓰지 않고 코드가 복구할 수 있는 결함이다.

계약:
1. 완전한 JSON 객체(또는 배열) 한 개가 끝난 뒤 닫는 괄호(} ]) 와 공백만 남은 경우에만 그 잔여물을 제거한다.
2. 그 밖의 잔여물(다른 문자·두 번째 객체·여는 괄호·문장)은 건드리지 않고 그대로 두어 기존처럼 거부된다.
3. 앞부분이 완전한 JSON 이 아니면(잘린 응답) 아무것도 하지 않는다.
4. 원문은 바뀌지 않는다. 정리 결과는 (정리된 문자열, 제거한 잔여물) 로 돌려주고 호출부가 영수증·파일에 기록한다.
5. 파이프라인은 정리를 모델 응답 파싱 직전에 한 번만 적용하며 보완 예산을 쓰지 않는다. 정리한 사실이 'response-repairs.json' 으로 남는다.
"""
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_response_repair as R


def test_a_single_extra_closing_brace_after_a_complete_object_is_removed():
    body = '{"stage":"synthesis","directions":[{"id":"D_P0026"}]}'
    fixed, removed = R.strip_trailing_closers(body + '}')
    assert fixed == body and removed == '}'
    assert json.loads(fixed)['stage'] == 'synthesis'


def test_extra_closers_and_whitespace_after_a_complete_object_are_removed():
    body = '{"a":[1,2]}'
    fixed, removed = R.strip_trailing_closers(body + '}]\n ')
    assert fixed == body and removed == '}]\n '


def test_a_clean_response_is_returned_unchanged_with_nothing_removed():
    body = '{"a":1}'
    assert R.strip_trailing_closers(body) == (body, '')
    assert R.strip_trailing_closers(body + '\n') == (body + '\n', '')   # 끝 공백만 있으면 원래 파서가 허용하므로 손대지 않는다


def test_a_second_object_after_the_first_is_not_repaired():
    raw = '{"a":1}{"b":2}'
    assert R.strip_trailing_closers(raw) == (raw, '')


def test_prose_or_other_characters_after_the_object_are_not_repaired():
    for tail in ('}x', ' 이상입니다', '}\n끝', ',', '}{', '}"', '}0'):
        raw = '{"a":1}' + tail
        assert R.strip_trailing_closers(raw) == (raw, ''), tail


def test_a_truncated_response_is_never_repaired_by_adding_or_removing_brackets():
    raw = '{"a":[1,2'
    assert R.strip_trailing_closers(raw) == (raw, '')
    raw = '{"a":{"b":1}'            # 닫는 괄호가 부족한 응답
    assert R.strip_trailing_closers(raw) == (raw, '')


def test_closers_inside_a_string_value_are_not_mistaken_for_trailing_garbage():
    body = '{"text":"끝 }}} 와 ]]]","n":1}'
    assert R.strip_trailing_closers(body) == (body, '')
    assert R.strip_trailing_closers(body + '}') == (body, '}')


def test_a_top_level_array_with_extra_closer_is_repaired():
    body = '[{"a":1},{"b":2}]'
    assert R.strip_trailing_closers(body + ']') == (body, ']')


def test_empty_or_non_string_input_is_returned_as_is():
    assert R.strip_trailing_closers('') == ('', '')
    assert R.strip_trailing_closers('   ') == ('   ', '')


def test_the_exact_operating_failure_is_repaired():
    """D_P0026 라운드 2 의 응답 구조(운영 기록과 같은 모양): JSON 이 3462자에서 끝나고 3463자째 '}' 가 붙음."""
    body = '{"stage":"synthesis","pad":"' + 'ㄱ' * 3400 + '"}'
    raw = body + '}'
    assert len(raw) == len(body) + 1
    fixed, removed = R.strip_trailing_closers(raw)
    assert fixed == body and removed == '}'
    json.loads(fixed)


def test_the_pipeline_parses_the_repaired_text_and_records_the_repair():
    """파이프라인이 정리 함수를 실제로 쓰는지: 응답을 파싱하는 호출이 정리본(parse_text)을 읽고, 정리 사실을 response-repairs.json 으로 남긴다.
    동작은 test_asis_response_repair_pipeline 이 합성 런타임으로 끝까지 확인한다. 여기서는 연결이 끊기지 않았는지만 본다(소스 거리가 아니라 호출 관계)."""
    src = (Path(__file__).resolve().parents[1] / 'asis_pipeline.py').read_text(encoding='utf-8')
    assert 'from asis_response_repair import strip_trailing_closers' in src
    assert 'parse_text, removed_tail = strip_trailing_closers(raw)' in src
    assert 'self.validator.strict_load(parse_text)' in src
    assert "response-repairs.json" in src
