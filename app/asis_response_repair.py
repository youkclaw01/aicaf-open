"""모델 응답 끝에 붙은 불필요한 닫는 괄호를 코드가 정리한다.

운영 사례(2026-10-18, 프로젝트 25 방향 작성): 완전한 JSON 객체 뒤에 '}' 한 글자가 더 붙은 응답이 strict_load 의 'Extra data' 로 거부돼
보완 예산 3회를 소진했다. 본문은 완전했다. 정리는 다음 조건을 모두 만족할 때만 한다.
- 앞부분이 JSON 파서가 끝까지 읽은 완전한 값(객체 또는 배열) 한 개다.
- 그 뒤에 남은 문자가 닫는 괄호('}', ']')와 공백뿐이다(여는 괄호·따옴표·쉼표·글자·두 번째 값이 하나라도 있으면 그대로 둔다).
잘린 응답, 다른 잔여물, 문자열 값 안의 괄호는 건드리지 않는다. 원문은 바뀌지 않고 제거한 잔여물을 호출부가 기록한다.
"""
import json

_CLOSERS = set('}] \t\r\n')


def strip_trailing_closers(raw):
    """(정리된 문자열, 제거한 잔여물). 정리할 것이 없으면 (원문, '')."""
    if not isinstance(raw, str):
        return raw, ''
    start = len(raw) - len(raw.lstrip())
    if start >= len(raw) or raw[start] not in '{[':
        return raw, ''
    try:
        _, end = json.JSONDecoder().raw_decode(raw, start)
    except ValueError:
        return raw, ''
    tail = raw[end:]
    if not tail or not tail.strip():
        return raw, ''           # 잔여물이 없거나 공백뿐이면 원래 파서가 허용한다
    if any(ch not in _CLOSERS for ch in tail):
        return raw, ''
    return raw[:end], tail
