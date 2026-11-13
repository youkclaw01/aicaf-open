"""근거 선택(코드): 한 단계(방향·관측)가 받을 입력을 근거 저장소에서 고정 규칙으로 고른다.

설계: docs/EVIDENCE-STORE-DESIGN-V2.md 4·5절, 실측은 13.7.
규칙(평가 24 실측으로 정함):
  1. 인터뷰 사실(F_IV_*)은 항상 통째로 준다(전체가 5천 자 수준).
  2. 목록형 사실은 직렬화 크기가 WHOLE_LIMIT 이하이면 통째로 준다.
  3. 초과하면 구조 요약(summary)과, 후보 텍스트의 식별 토큰이 직접 일치한 행만 준다(최대 ROW_LIMIT행).
  4. 어떤 사실이 어떤 방식으로 들어갔는지(mode)와 잘린 사실의 총 행 수·보낸 행 수를 함께 돌려준다.
     모델과 검증자는 "일부만 받았다"는 사실을 알 수 있어야 한다.
관계를 따라가 이웃 레코드를 더하는 규칙은 평가 24에서 누락을 11.1%→10.8%로 줄이는 데 그쳐 채택하지 않았다.
"""
from __future__ import annotations

import json
import re

WHOLE_LIMIT = 24_000
ROW_LIMIT = 20
TOKEN_LIMIT = 40

_TOKEN = re.compile(r'[A-Za-z][A-Za-z0-9._-]{3,}')


def _size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False))


def identifier_tokens(text: str) -> list[str]:
    """후보 텍스트에서 식별자로 보이는 토큰(하이픈·점·숫자·대소문자 혼합)만 고른다."""
    found = {t.lower() for t in _TOKEN.findall(text or '') if re.search(r'[-.\d]', t) or any(c.isupper() for c in t[1:])}
    return sorted(found)[:TOKEN_LIMIT]


def select_fact(handle, fact_id: str, value, candidate_text: str, *, whole_limit: int = WHOLE_LIMIT, row_limit: int = ROW_LIMIT) -> dict:
    """한 사실의 모델 입력 조각을 만든다. value는 통째 후보(인터뷰·작은 사실)에 쓰는 원본이다."""
    if fact_id.startswith('F_IV_') or not isinstance(value, list) or _size(value) <= whole_limit:
        return {'fact_id': fact_id, 'mode': 'whole', 'value': value}
    meta = handle.fact(fact_id)
    if meta is None:
        return {'fact_id': fact_id, 'mode': 'whole', 'value': value}
    dataset_id = meta['dataset_id']
    ids: list[str] = []
    for token in identifier_tokens(candidate_text):
        for found in handle.find(dataset_id, token, limit=5):
            if found not in ids:
                ids.append(found)
            if len(ids) >= row_limit:
                break
        if len(ids) >= row_limit:
            break
    records = handle.record(ids)
    return {'fact_id': fact_id, 'mode': 'summary_and_matched_rows', 'dataset_id': dataset_id,
            'total_rows': meta['record_count'], 'sent_rows': len(records),
            'summary': handle.summary(dataset_id, top=3), 'rows': list(records.values())}


def select_facts(handle, fact_ids, facts_by_id: dict, candidate_text: str, **kw) -> list[dict]:
    out = []
    for fid in fact_ids:
        fact = facts_by_id.get(fid)
        if fact is None:
            continue
        out.append(select_fact(handle, fid, fact['value'], candidate_text, **kw))
    return out


def measure(selected: list[dict]) -> dict:
    """입력 크기와 잘림 여부 요약. 입력 한도 검증과 보고에 쓴다."""
    return {'chars': _size(selected), 'facts': len(selected),
            'trimmed': [s['fact_id'] for s in selected if s['mode'] != 'whole'],
            'rows_sent': sum(s.get('sent_rows', 0) for s in selected),
            'rows_total_of_trimmed': sum(s.get('total_rows', 0) for s in selected)}
