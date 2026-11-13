"""전송 입력 축소 변환기: 품질 비교 실험의 조건 C·D를 코드로 고정한다. 정본 입력은 변경하지 않는다.

수준:
- A: 변환 없음.
- C: 사실마다 반복되는 run 객체를 context.fact_run 한 번으로 올린다(다른 run을 가진 사실은 자기 run을 유지). 값 손실이 없고 restore_run으로 복원된다.
- D: C에 더해, 직접 인용 대상(protected)이 아닌 큰 행 목록 사실의 값을 행 수·키·대표 1행·원본 해시 요약으로 바꾼다.
  작은 사실·객체형 사실·문자열 사실은 건드리지 않는다.
"""
from __future__ import annotations

import json
from copy import deepcopy

from asis_model import digest

LEVELS = ('A', 'C', 'D')
BIG_ROWS_MIN_ROWS = 20
BIG_ROWS_MIN_BYTES = 15_000


def _bytes(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))


def _common_run(facts):
    runs = [json.dumps(f['run'], sort_keys=True, ensure_ascii=False) for f in facts if isinstance(f.get('run'), dict)]
    if not runs:
        return None
    best = max(set(runs), key=runs.count)
    return json.loads(best) if runs.count(best) >= 2 else None


def _summary(value):
    keys = sorted({k for row in value if isinstance(row, dict) for k in row})
    return {'reduced': True, 'row_count': len(value), 'row_keys': keys, 'sample_row': deepcopy(value[0]),
            'canonical_value_sha256': digest(value),
            'note': 'full rows remain in the canonical ledger; this is a structural summary for an uncited fact'}


def reduce_input(data, *, level, protected):
    """level: A·C·D. protected: 값을 그대로 둘 사실 ID 집합(슬롯·방향·진단이 직접 인용한 사실 등)."""
    if level not in LEVELS:
        raise ValueError('unknown_reduction_level:' + str(level))
    out = deepcopy(data)
    if level == 'A':
        return out
    facts = out.get('facts', [])
    common = _common_run(facts)
    if common is not None:
        out.setdefault('context', {})['fact_run'] = deepcopy(common)
        for fact in facts:
            if fact.get('run') == common:
                del fact['run']
    if level == 'D':
        for fact in facts:
            value = fact.get('value')
            if fact['id'] in protected or not isinstance(value, list):
                continue
            if len(value) >= BIG_ROWS_MIN_ROWS and _bytes(value) >= BIG_ROWS_MIN_BYTES:
                fact['value'] = _summary(value)
    return out


def restore_run(reduced):
    """C·D 변환의 run 제거를 되돌린다. 요약된 값(D)은 복원 대상이 아니다."""
    out = deepcopy(reduced)
    common = (out.get('context') or {}).get('fact_run')
    if common is not None:
        for fact in out.get('facts', []):
            if 'run' not in fact:
                fact['run'] = deepcopy(common)
    return out
