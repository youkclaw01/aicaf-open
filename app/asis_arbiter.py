"""검토자의 blocked를 실패로 끝내지 않고 조치로 분류하는 조정자.

설계 원칙
- 코드가 먼저 결정적으로 분류할 수 있는 것은 코드가 처리한다(asis_pipeline._cross_item_only).
- 코드가 분류하지 못한 blocked만 조정자 모델이 판정하고, 판정 형식·인용·권한은 코드가 검증한다.
- 조정자는 blocked를 pass로 바꾸지 못한다. 다음 단위(병합 검토)로 넘기거나, 작성자에게 되돌리거나, 근거를 입력에 더해 재검토하게 하거나, 차단을 유지하게 할 뿐이다.
"""
from __future__ import annotations

import json
import re

ACTIONS = ('defer_to_merge', 'supply_evidence', 'author_repair', 'upheld')

INSTRUCTION = (
    '너는 독립 검토자의 blocked 판정을 분류하는 조정자다. 검토자의 결론을 뒤집거나 pass를 선언하지 않는다. '
    '입력의 blocked_items 각각에 대해 아래 네 가지 중 하나를 고른다.\n'
    '1. defer_to_merge: 담당 항목 하나만 받은 호출이 다른 항목·영역의 본문과 대조할 수 없다는 이유다. 그 대조는 모든 항목을 받는 병합 검토의 몫이다.\n'
    '2. supply_evidence: 검토자가 값을 확인하지 못했다고 한 사실·기준이 draft가 실제로 인용했지만 이번 입력에 값이 전달되지 않은 경우다. '
    'evidence_ids에 그 ID를 그대로 적는다. 입력의 supplied_fact_ids에 이미 있는 ID나 draft가 인용하지 않은 ID는 적지 않는다.\n'
    '3. author_repair: 검토자가 지적한 문제가 draft 문장의 근거 없는 단정·참조 오류·범위 초과여서 작성자가 고쳐야 한다.\n'
    '4. upheld: 위 셋이 아니고 검토자의 차단이 타당하다.\n'
    '출력은 JSON 하나만이다: {"decisions":[{"index":0,"action":"...","evidence_ids":[],"quote":"blocked 항목에서 그대로 옮긴 8자 이상 문구","reason":"한 문장"}]}. '
    'index는 blocked_items의 순서다. quote는 해당 항목의 reason 또는 required_evidence에 실제로 있는 연속 문구여야 한다. '
    '모든 항목을 빠짐없이 한 번씩 판정한다.')


def build_request(*, key, candidate, unresolved, supplied_fact_ids, cited_fact_ids, own_id):
    """조정자에게 전달할 최소 입력. 후보 전체와 검토자의 미확인 항목, 코드가 계산한 공급 상태만 담는다."""
    return {
        'item': key,
        'own_id': own_id,
        'blocked_items': [dict(row) for row in unresolved],
        'supplied_fact_ids': sorted(supplied_fact_ids),
        'cited_fact_ids': sorted(cited_fact_ids),
        'draft': candidate,
    }


def parse_decisions(raw, *, unresolved, supplied_fact_ids, cited_fact_ids):
    """모델 응답을 검증한다. 형식·인용·ID 규칙을 하나라도 어기면 ValueError. 반환값은 항목별 판정 목록이다."""
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError('arbiter_not_json') from exc
    if not isinstance(payload, dict) or set(payload) != {'decisions'} or not isinstance(payload['decisions'], list):
        raise ValueError('arbiter_shape')
    decisions = payload['decisions']
    if len(decisions) != len(unresolved):
        raise ValueError('arbiter_decision_count')
    seen = set()
    result = []
    for row in decisions:
        if not isinstance(row, dict) or set(row) != {'index', 'action', 'evidence_ids', 'quote', 'reason'}:
            raise ValueError('arbiter_decision_shape')
        index = row['index']
        if type(index) is not int or not 0 <= index < len(unresolved) or index in seen:
            raise ValueError('arbiter_decision_index')
        seen.add(index)
        if row['action'] not in ACTIONS:
            raise ValueError('arbiter_action')
        if not isinstance(row['reason'], str) or not row['reason'].strip():
            raise ValueError('arbiter_reason')
        source = ' '.join(str(unresolved[index].get(field, '')) for field in ('reason', 'required_evidence', 'topic'))
        quote = row['quote']
        if not isinstance(quote, str) or len(quote.strip()) < 8 or quote.strip() not in source:
            raise ValueError('arbiter_quote_not_in_item')
        ids = row['evidence_ids']
        if not isinstance(ids, list) or any(not isinstance(v, str) for v in ids) or len(ids) != len(set(ids)):
            raise ValueError('arbiter_evidence_ids')
        if row['action'] == 'supply_evidence':
            if not ids or any(v not in cited_fact_ids or v in supplied_fact_ids for v in ids):
                raise ValueError('arbiter_supply_ids_not_cited_or_already_supplied')
        elif ids:
            raise ValueError('arbiter_ids_only_for_supply')
        result.append({'index': index, 'action': row['action'], 'evidence_ids': list(ids),
                       'quote': quote.strip(), 'reason': row['reason'].strip()})
    return sorted(result, key=lambda r: r['index'])


def combine(decisions):
    """항목별 판정을 한 조치로 합친다. 하나라도 upheld면 차단을 유지하고, 그 외에는 가장 엄격한 조치를 택한다."""
    actions = {row['action'] for row in decisions}
    if 'upheld' in actions:
        return 'upheld'
    if 'author_repair' in actions:
        return 'author_repair'
    if 'supply_evidence' in actions:
        return 'supply_evidence'
    return 'defer_to_merge'


def evidence_ids(decisions):
    return sorted({value for row in decisions if row['action'] == 'supply_evidence' for value in row['evidence_ids']})


def repair_defects(unresolved, decisions):
    """author_repair 판정을 작성자가 받을 결함 형식으로 바꾼다. where는 항목 전체('/')이며 quote는 판정이 인용한 문구다."""
    defects = []
    for row in decisions:
        if row['action'] != 'author_repair':
            continue
        item = unresolved[row['index']]
        defects.append({'where': '/', 'quote': row['quote'][:400], 'evidence_refs': [],
                        'why': '독립 검토가 확인하지 못한 항목: ' + row['reason'],
                        'fix': '근거가 입력에 없는 단정은 미확인으로 바꾸고 참조를 입력의 사실·기준 ID로 한정한다. 관련 항목은 ' +
                               str(item.get('topic', ''))[:80] + '이다.'})
    return defects


# ---- 2단계 구조: 영역 담당자(1차) → 전체 관리자(2차) → 사람 보고 ------------------------------------------
LEVELS = ('area_owner', 'global_manager')

AREA_OWNER_ROLE = (
    '너는 {area} 영역 담당자다. 이 영역 항목의 독립 검토가 막혔다(blocked). 영역 전체의 맥락을 알고 있으므로 '
    '검토자가 확인하지 못한 것이 이 영역 안의 다른 항목·근거로 해결되는지 먼저 판단한다. 해결안은 아래 네 조치 중 하나로만 제시한다.\n')

GLOBAL_MANAGER_ROLE = (
    '너는 전체 중앙 관리자다. 영역 담당자가 해결하지 못했거나 영역 간 충돌이 걸린 blocked 항목을 받는다. '
    '영역 담당자의 직전 판정(previous_decisions)을 읽고, 그것이 타당했는지 그리고 다른 영역과의 관계가 원인인지 판단한다. '
    '아래 네 조치 중 하나로만 답한다. 두 번째 시도에도 upheld이면 코드가 사람에게 보고한다.\n')


def level_instruction(level, area_name=''):
    """판정 지시문은 층마다 역할 문구만 다르고 출력 계약·검증 규칙은 같다."""
    if level == 'area_owner':
        return AREA_OWNER_ROLE.format(area=area_name or '담당') + INSTRUCTION
    if level == 'global_manager':
        return GLOBAL_MANAGER_ROLE + INSTRUCTION
    raise ValueError('unknown_arbiter_level')


def next_level(level, action):
    """판정 결과로 다음 층을 정한다. upheld는 한 층 위로 올리고, 마지막 층의 upheld는 사람 보고(None)다."""
    if action != 'upheld':
        return None
    order = list(LEVELS)
    if level not in order:
        raise ValueError('unknown_arbiter_level')
    index = order.index(level) + 1
    return order[index] if index < len(order) else 'human_report'
