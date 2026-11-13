"""항목별 '길이로 인한 실패' 횟수를 개정 폴더에 영속 기록하고, 그 횟수로 입력 요약 단계를 정한다.

영석님 지시(2026-10-18): 길이로 2번 이상 실패한 항목만 요약해서 보낸다. 길이 실패 두 종류는 모두 모델 호출 전 거절이라 비용이 없다.
  - 모델이 입력 한도 초과로 거절: 예외 문구 'Request exceeds the model's prompt budget ... (N tokens; limit L)'
  - 전송 전 코드 검사: ValueError('model_input_over_limit:<종류>:<크기>><상한>')
기록은 개정이 중단·재개돼도 유지돼 '2번 이상'이 의미를 가진다. 요약 단계: 0~1회=0(원문), 2회=1, 3회 이상=2.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

_BUDGET = re.compile(r'prompt budget[^()]*\((\d+) tokens; limit (\d+)\)')
_MAX_PROMPT = re.compile(r'prompt token count of (\d+) exceeds the limit of (\d+)')
_ENTITY_TOO_LARGE = re.compile(r'Request Entity Too Large|status_code=413|too many images:|maximum allowed for model', re.I)
_OVER = re.compile(r'model_input_over_limit:([^:]+):(\d+)>(\d+)')
FILE = 'length-failures.json'


def parse(exc):
    """길이 실패면 (종류, 크기, 한도), 아니면 None."""
    text = str(exc)
    match = _BUDGET.search(text) or _MAX_PROMPT.search(text)
    if match:
        return 'model_prompt_budget', int(match.group(1)), int(match.group(2))
    if _ENTITY_TOO_LARGE.search(text):
        return 'request_entity_too_large', 1, 1
    match = _OVER.search(text)
    if match:
        return 'model_input_over_limit', int(match.group(2)), int(match.group(3))
    return None


def is_length_failure(exc):
    return parse(exc) is not None


def _read(root):
    path = Path(root) / FILE
    if path.is_file():
        return json.loads(path.read_text(encoding='utf-8'))
    return {'schema': 'asis-length-failures/1', 'count': 0, 'failures': []}


def record(root, *, kind, tokens, limit):
    """길이 실패 한 건을 항목 체크포인트 폴더에 원자적으로 추가한다. 반환: 누적 횟수."""
    from asis_view import atomic
    root = Path(root)
    row = _read(root)
    row['failures'].append({'kind': kind, 'tokens': tokens, 'limit': limit, 'at': datetime.now(timezone.utc).isoformat()})
    row['count'] = len(row['failures'])
    root.mkdir(parents=True, exist_ok=True)
    atomic(root / FILE, row)
    return row['count']


def summary_level(root):
    """0 = 원문, 1 = 승인 서술문 축약, 2 = 1 + 미확인 이유·비직접 목록형 사실 요약."""
    count = _read(root)['count']
    return 0 if count < 2 else 1 if count == 2 else 2
