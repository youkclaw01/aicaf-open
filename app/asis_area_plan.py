"""영역별 분석 계획(코드): 영역마다 연결된 사실·인터뷰를 DB에서 읽고, 모델 호출이 필요한지 정한다.

설계: docs/EVIDENCE-STORE-DESIGN-V2.md 5.3. 판정은 수집 상태와 인터뷰 연결만으로 정하며 모델이 정하지 않는다.

인터뷰는 영역별로 고르지 않는다. 평가 24 실측에서 한 영역의 관측이 XA~XD 네 묶음의 문항을 모두 인용했고,
승인된 관측이 인용한 인터뷰 889건 중 접두로 영역을 나눈 표 안에 있는 것은 46%뿐이었다(설계서 13.6).
모든 영역이 전체 문항(평가 24: 100문항, transcript 5,116자)을 받는다. 줄일 대상은 인터뷰가 아니라 구조화 사실이다.
"""
from __future__ import annotations

AREAS = ['network', 'compute', 'data', 'ai', 'secgov', 'telemetry', 'quality', 'operating', 'cost']

def fact_state(collection_status: str, scope_complete: bool, record_count: int | None) -> str:
    """수집 상태를 모델이 혼동하지 않는 다섯 가지로 줄인다. 미수집과 부재를 섞지 않는다."""
    if collection_status in ('not_collected', 'permission_denied', 'collection_failed'):
        return 'not_collected'
    count = record_count or 0
    if count == 0:
        return 'absent_confirmed' if (scope_complete or collection_status == 'not_present') else 'empty_unconfirmed'
    return 'observed_complete' if scope_complete else 'observed'


def area_inputs(handle, area: str) -> dict:
    facts = []
    for row in handle.area_facts(area):
        facts.append({'fact_id': row['fact_id'], 'dataset_id': row['dataset_id'], 'record_count': row['record_count'],
                      'state': fact_state(row['collection_status'], row['scope_complete'], row['record_count'])})
    return {'area': area, 'facts': facts, 'interview_ids': handle.all_interview_ids()}


def decide(inputs: dict) -> dict:
    """호출 필요성. 같은 입력이면 항상 같은 결과를 낸다."""
    facts = inputs['facts']
    interviews = inputs['interview_ids']
    if not facts and not interviews:
        return {'model_call': False, 'reason': 'no_evidence', 'input': None}
    if interviews and not facts:
        return {'model_call': True, 'reason': 'interview_only', 'input': 'interviews'}
    if interviews:
        return {'model_call': True, 'reason': 'interview_vs_structured', 'input': 'pairs'}
    if all(f['state'] in ('observed_complete', 'absent_confirmed') for f in facts):
        return {'model_call': False, 'reason': 'structured_only_complete', 'input': None}
    return {'model_call': True, 'reason': 'structured_incomplete', 'input': 'facts'}


def plan_areas(handle) -> dict:
    areas = []
    for area in AREAS:
        inputs = area_inputs(handle, area)
        counts: dict[str, int] = {}
        for f in inputs['facts']:
            counts[f['state']] = counts.get(f['state'], 0) + 1
        verdict = decide(inputs)
        areas.append({'area': area, **verdict, 'fact_count': len(inputs['facts']), 'interview_count': len(inputs['interview_ids']),
                      'state_counts': dict(sorted(counts.items())), 'fact_ids': [f['fact_id'] for f in inputs['facts']],
                      'interview_ids': inputs['interview_ids']})
    calls = sum(1 for a in areas if a['model_call'])
    return {'areas': areas, 'summary': {'areas': len(areas), 'model_calls': calls, 'code_only': len(areas) - calls}}
