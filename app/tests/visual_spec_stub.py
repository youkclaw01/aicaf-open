"""PPT 시험용 시각 명세: 운영에서는 visual_specs 호출이 통합 진단 주제마다 만드는 값을, 시험 입력에서는 주제 ID별 표 형식 명세로 채운다.

진단이 인용한 사실이 있으면 architecture, 없으면 table을 골라 두 경로(아키텍처·표)를 모두 지난다. 계약 검증(validate_visual)을 통과한 값만 넣는다.
"""
import asis_consolidated_deck as deck
import asis_visual_forms as forms


def fit_legacy_actions(result):
    """이전 형식(장문)으로 보관된 시험 데이터의 과제를 새 장표 분량 계약에 맞춘다. 시험 입력만 바꾸며 제품 코드는 글을 자르지 않는다.

    운영에서는 작성 단계(validate_item)가 분량을 강제하므로 이런 과제는 나오지 않는다. 고객 요구사항 원문 인용('추가 검토 제약')이 붙은 완료 조건은 자르지 않는다."""
    from asis_consolidated_deck import ACTION_LIMITS, ITEMS_PER_COLUMN_CHARS
    def clip(value, limit):
        value = ' '.join(str(value).split())
        return value if len(value) <= limit else value[:limit - 1] + '…'
    for action in result['stages'][2]['actions']:
        action['target'] = clip(action['target'], ACTION_LIMITS['target'])
        action['rationale'] = clip(action['rationale'], ACTION_LIMITS['rationale'])
        action['deliverable'] = clip(action['deliverable'], ACTION_LIMITS['deliverable'])
        if '추가 검토 제약' not in action['verification_condition']:
            action['verification_condition'] = clip(action['verification_condition'], ACTION_LIMITS['condition'])
        action['prerequisites'] = [row if '추가 제약' in row else clip(row, ITEMS_PER_COLUMN_CHARS[min(3, max(1, len(action['prerequisites'])))]) for row in action['prerequisites']][:3]
        points = action['improvement_points'] or ['개선 포인트 확인']
        action['improvement_points'] = [clip(row, ITEMS_PER_COLUMN_CHARS[min(3, max(1, len(points)))]) for row in points][:3]
    return result


def with_visual_specs(result):
    fit_legacy_actions(result)
    specs = {}
    by_area = deck.topics_by_area(result)
    cited = {row['id']: set(row['fact_refs']) for row in result['stages'][2]['diagnoses']}
    for area_id, topics in by_area.items():
        for number, topic in enumerate(topics):
            if number % 2 == 0:
                visual = {'form': 'architecture', 'headline': '구성 개요', 'conclusion': '핵심 결론 한 줄', 'data': {'focus_layers': ['network'], 'gap_layers': ['network']}}
            else:
                visual = {'form': 'table', 'headline': '비교 개요', 'conclusion': '핵심 결론 한 줄',
                          'data': {'columns': ['대상', '현재', '갭'], 'rows': [['항목', '확인', '미확인']]}}
            specs[topic['id']] = forms.validate_visual(visual, cited[topic['id']])
    result['visual_specs'] = specs
    return result
