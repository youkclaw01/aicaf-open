"""입력 예산 사전 점검: 모델을 부르기 전에 항목별 실제 전송 크기·토큰 범위·단가 구간·예상 비용을 계산한다.

설계 근거: docs/EVIDENCE-STORE-DESIGN-V2.md 15.7~15.8.
- 별도 추정식을 쓰지 않는다. 운영 전송 경로(Pipeline.render_prompt)로 프롬프트를 만들어 그 바이트를 잰다.
- 토큰 환산은 영수증 실측 비(입력 토큰/전송 바이트)의 낮음~높음 범위로 한다. 범위가 272K에 걸치면 'uncertain'으로 보고한다.
- 모델 호출·파일 쓰기 없음. 입력 정본을 바꾸지 않는다.
단가(USD/1M 토큰, GitHub 공식 문서 2026-10-16, GPT-6.1 Sol): 입력 2.00 / 출력 10.00 (≤272K), 입력 4.00 / 출력 15.00 (>272K).
"""
from __future__ import annotations

import json
from copy import deepcopy

LONG_THRESHOLD = 272_000
PRICE = {'default': {'input': 2.00, 'output': 10.00}, 'long': {'input': 4.00, 'output': 15.00}}
# 영수증 실측(평가 24, 응답 ID 중복 제거): 진단 중앙 0.276·p95 0.300, 과제 작성 중앙 0.274, 전체 p95 0.42. 비 범위는 데이터에서 갱신할 수 있게 인자로 받는다.
DEFAULT_RATIO_LOW = 0.27
DEFAULT_RATIO_HIGH = 0.42


def tier(tokens_low: int, tokens_high: int) -> str:
    if tokens_high <= LONG_THRESHOLD:
        return 'default'
    if tokens_low > LONG_THRESHOLD:
        return 'long'
    return 'uncertain'


def cost_usd(input_tokens: int, output_tokens: int, tier: str) -> float:
    price = PRICE['long' if tier == 'long' else 'default']
    return input_tokens * price['input'] / 1e6 + output_tokens * price['output'] / 1e6


def measure_item(pipe, scoped, *, stage=3, ratio_low=DEFAULT_RATIO_LOW, ratio_high=DEFAULT_RATIO_HIGH) -> dict:
    """항목 1개의 실제 전송 프롬프트를 렌더해 크기와 직접 인용 사실의 크기를 잰다."""
    import asis_model_view as mv
    assignment = scoped['stage_data']['item_assignment']
    kind, view = mv.build(stage, scoped)
    _, prompt = pipe.render_prompt(stage, scoped)
    size = len(prompt.encode('utf-8'))
    low, high = round(size * ratio_low), round(size * ratio_high)
    direct = set(assignment['slot']['fact_refs'])
    direct_rows = {row['id']: len(json.dumps(row['value'], ensure_ascii=False)) for row in view['facts'] if row['id'] in direct}
    return {'item_id': assignment['item_id'], 'kind': kind, 'bytes': size, 'tokens_low': low, 'tokens_high': high,
            'tier': tier(low, high), 'direct_fact_ids': sorted(direct_rows), 'direct_fact_chars': sum(direct_rows.values())}


def summarize(rows: list[dict]) -> dict:
    over = []
    for row in rows:
        if row['tier'] == 'long':
            over.append({**row, 'over_by_low': row['tokens_low'] - LONG_THRESHOLD, 'over_by_high': row['tokens_high'] - LONG_THRESHOLD})
    return {'items': len(rows), 'default_tier': sum(1 for r in rows if r['tier'] == 'default'),
            'over_budget': over, 'uncertain': [r for r in rows if r['tier'] == 'uncertain']}
