"""모델에 보내는 입력의 공통 전송 계층.

정본 입력(data)은 검증·체크포인트 키·원장의 기준으로 그대로 두고, 모델에 보내는 뷰만 호출 종류의 명세에 따라 만든다.
모델은 해당 항목의 판단에 필요한 내용만 받고, 나머지는 ID로 참조한다. 명세 밖의 내용은 보내지 않으며
상한을 넘으면 모델 호출 전에 실패시킨다(조용히 보내지 않는다).

운영 실측(방향 D_P0001): 작성 입력이 154만 자였고 인용하지 않는 사실 106개, 관찰 본문 11만 자, 미확인 917개가 매번 실렸다.
"""
from copy import deepcopy
import json

# 호출 종류별 상한(자). 넘으면 모델 호출 전에 실패한다. 현재 실측 기준의 안전 상한이며 명세가 개선되면 낮춘다.
LIMITS = {'document_review': 900_000, 'visual_review': 900_000, 'item_visual_review': 900_000, 'summary_author': 900_000, 'summary_review': 900_000, 'direction_author': 900_000, 'direction_review': 900_000, 'item_author': 900_000, 'item_review': 900_000, 'synthesis_shard_review': 900_000, 'synthesis_integration_review': 900_000}
# 모델 입력 한도는 토큰이다(운영 실측 2026-10-18: 요약 입력이 82.7만 자로 문자 한도 90만을 통과했지만 34.3만 토큰으로 모델 한도 27.2만을 넘었다).
# 자/토큰 비는 입력에 따라 다르다(한국어 서술문이 많은 요약 입력 2.41, 과제 입력은 영수증 실측 약 3.9). 비는 실측값이며 요약 단계의 2단계 목표를 잡는 데만 쓴다.
MODEL_TOKEN_LIMIT = 272_000
CHARS_PER_TOKEN = {'summary_author': 2.41, 'summary_review': 2.41}

# 값이 이 길이를 넘는 사실은 슬롯이 직접 인용하지 않으면 본문을 생략하고 ID·출처·크기만 둔다.
LARGE_FACT_CHARS = 20_000

# 항목 1개 입력에서 승인 영역·방향 각각의 미확인 본문에 줄 수 있는 크기(합 14만 자). 넘으면 겹침이 큰 것부터 채우고 나머지는 주제만 남긴다.
UNRESOLVED_BUDGET_CHARS = 70_000

# 전체 미확인에서 이 횟수 이하로만 나오는 사실은 특정 항목에 고유한 것으로 본다.
RARE_FACT_MAX_UNRESOLVED = 20

# 종합 구간 검토가 읽지 않는 계획 정본(분석 계획·진단·과제 계획·수집 기준 매칭). 검토는 담당 방향의 근거·범위·상충만 본다. 정본에는 그대로 있다.
SHARD_DROP_CONTEXT = ('analysis_plan', 'action_item_plan', 'collection_basis_matches')

# 방향 작성·검토에서 모델이 쓰지 않는 배경 원장. 정본에는 그대로 있다.
DROP_CONTEXT = ('source_documents', 'collection_basis', 'collection_basis_workbook', 'criterion_assessments',
                'dataset_register', 'dataset_index', 'derived_fact_register')

DIRECTION_SCOPE_RULE = ('모든 배정 이슈의 판단 범위를 본문에서 다룬다. 공통 판단은 통합할 수 있으나 서로 다른 원인·의사결정 조건·미확인 사항을 대표 이슈 하나로 대체하지 않는다. '
                        '근거가 없는 범위는 결론을 만들지 말고 미확인 내용과 필요한 근거로 표시한다. 이슈 ID나 참조 합집합만 나열한 것은 본문 판단을 대신하지 않는다.')


def assigned_issue_scopes(data, issue_refs):
    """배정 순서에 따라 원계획의 이슈 범위·이유를 그대로 연결하며 새 판단을 만들지 않는다."""
    plan = data.get('context', {}).get('analysis_plan', {})
    issues = plan.get('issues', []) if isinstance(plan, dict) else []
    indexed = {row['id']: row for row in issues if isinstance(row, dict) and 'id' in row}
    return [{key: deepcopy(indexed[ref][key]) for key in ('id', 'scope', 'reason') if key in indexed[ref]}
            for ref in issue_refs if ref in indexed]


def _chars(value):
    return len(json.dumps(value, ensure_ascii=False))


def _walk_refs(value, name, found):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == name and isinstance(child, list):
                found.update(v for v in child if isinstance(v, str))
            else:
                _walk_refs(child, name, found)
    elif isinstance(value, list):
        for child in value:
            _walk_refs(child, name, found)


def refs_in(value, name):
    found = set()
    _walk_refs(value, name, found)
    return found


def _inline_refs(facts, direct):
    """이 뷰가 본문을 보내기로 한 사실. prompt_view는 4.5만 자를 넘는 사실 중 inline 목록에 없는 것을 지우므로,
    우리가 의도해 보낸 사실(직접 인용 또는 작은 사실)은 여기에 명시한다. 이미 생략 표시한 사실은 넣지 않는다."""
    return sorted(row['id'] for row in facts if not (isinstance(row['value'], dict) and row['value'].get('not_inlined')))


def related_unresolved(rows, mine, all_rows=None):
    """이 항목과 관련 있는 미확인만 고른다. 미확인은 '확인되지 않은 것을 정상·결함으로 확정하지 않는다'는 안전장치라 보수적으로 고른다.

    ① 이 항목의 사실과 절반 이상 겹치는 미확인, ② 여러 항목에 두루 걸리지 않는 드문 사실(전체 미확인에서 20회 이하)과 겹치는 미확인의 합집합.
    사실 하나만 겹친다고 모두 보내면 인터뷰 전사 같은 흔한 사실 하나가 수십 개 미확인을 끌어온다(운영 실측: 411개 18만 자 → 이 기준 수십 개).
    """
    universe = all_rows if all_rows is not None else rows
    frequency = {}
    for row in universe:
        for fact in row.get('fact_refs', []):
            frequency[fact] = frequency.get(fact, 0) + 1
    rare = {fact for fact in mine if frequency.get(fact, 0) <= RARE_FACT_MAX_UNRESOLVED}
    kept = []
    for row in rows:
        facts = set(row.get('fact_refs', []))
        shared = facts & mine
        if not shared:
            continue
        if len(shared) * 2 >= max(2, min(len(mine), len(facts))) or (shared & rare):
            kept.append(row)
    return kept


def kind_of(stage, data):
    """이 전송이 어떤 종류의 호출인지 정본 입력으로 판별한다. 해당 없으면 None(기존 전송 유지)."""
    sd = data.get('stage_data', {})
    if stage == 4 and sd.get('mode') == 'summary' and isinstance(sd.get('approved_actions'), dict):
        return 'summary_author'
    # 문서 검토(영역·공통·종합·과제·요약 샤드와 통합): 후보(draft.stages)가 검토 대상의 전부다. 배경 원장은 검토자가 쓰지 않는다.
    if stage == 5 and isinstance(sd.get('draft'), dict) and isinstance(sd['draft'].get('stages'), list) and 'item_assignment' not in sd \
            and is_document_review_mode(sd.get('mode')):
        return 'document_review'
    # 16단계 장표(이미지) 묶음 검토: 후보 draft 는 장표 텍스트(pages)와 전체 stages 다. 승인 블록·전체 stages 는 같은 본문을 되풀이한다(운영 사전 측정: 장표 4장 입력 594만 자 = 246만 토큰).
    if stage == 5 and isinstance(sd.get('draft'), dict) and isinstance(sd['draft'].get('pages'), list) and isinstance(sd['draft'].get('stages'), list) \
            and 'item_assignment' not in sd and isinstance(sd.get('mode'), str) and sd['mode'].startswith('visual-'):
        return 'visual_review'
    # 요약 검토: 작성과 같은 승인 결과를 보고 후보(draft)를 검토한다. 작성만 줄이고 검토를 두면 검토 호출이 같은 한도를 넘는다(운영 2026-10-16 13:00).
    if stage == 5 and sd.get('mode') == 'summary' and isinstance(sd.get('draft'), dict) and isinstance(sd.get('approved_actions'), dict):
        return 'summary_review'
    # 종합 구간 검토: 담당 방향 외에는 omitted 자리만 둔 draft를 받는다. 모드 이름은 asis_synthesis._review_merged_synthesis가 정한다.
    if stage == 5 and isinstance(sd.get('draft'), dict) and isinstance(sd['draft'].get('directions'), list) \
            and isinstance(sd.get('mode'), str) and sd['mode'].startswith('synthesis-shard-') and 'item_assignment' not in sd \
            and 'direction_assignment' not in sd:
        return 'synthesis_shard_review'
    # 종합 통합 검토: 방향 요약본(제목·핵심 문장·사실 수)만 받는다. 모드 이름은 _review_merged_synthesis가 정한다.
    if stage == 5 and isinstance(sd.get('draft'), dict) and isinstance(sd['draft'].get('directions'), list) \
            and sd.get('mode') == 'synthesis-integration' and 'item_assignment' not in sd and 'direction_assignment' not in sd:
        return 'synthesis_integration_review'
    if isinstance(sd.get('direction_assignment'), dict):
        if stage == 2:
            return 'direction_author'
        if stage == 5 and isinstance(sd.get('draft'), dict):
            return 'direction_review'
        return None
    if isinstance(sd.get('item_assignment'), dict):
        if stage == 3:
            return 'item_author'
        # 17단계 항목별 장표(이미지) 검토: 작성·내용 검토와 같은 항목 입력을 쓰지만 후보에 장표(pages)가 있다. 이미지 검토는 사실 값 대조가 아니다.
        if stage == 5 and isinstance(sd.get('draft'), dict) and isinstance(sd['draft'].get('pages'), list):
            return 'item_visual_review'
        if stage == 5 and isinstance(sd.get('draft'), dict):
            return 'item_review'
    return None


def direction_view(data, kind):
    """방향 1개 작성·검토에 필요한 내용만 담은 전송용 뷰를 만든다. 정본 data는 변경하지 않는다."""
    view = deepcopy(data)
    sd = view['stage_data']
    slot = sd['direction_assignment']['slot']
    draft = sd.get('draft')
    scopes = assigned_issue_scopes(data, slot.get('issue_refs', []))
    if scopes:
        sd['assigned_issue_scopes'] = scopes
        sd['direction_scope_rule'] = DIRECTION_SCOPE_RULE

    cited_facts = set(slot['fact_refs'])
    cited_criteria = set(slot['criterion_refs'])
    areas = sd.get('approved_areas', {}).get('areas', [])
    for area in areas:
        cited_facts |= set(area['implication']['fact_refs'])
        cited_criteria |= set(area['implication']['criterion_refs'])
    if draft is not None:
        cited_facts |= refs_in(draft, 'fact_refs')
        cited_criteria |= refs_in(draft, 'criterion_refs')

    # 영역 시사점: 방향이 읽는 것은 시사점의 결론이다. 관찰 본문은 시사점이 인용한 ID로만 이어지므로 보내지 않는다.
    if isinstance(sd.get('approved_areas'), dict):
        approved = sd['approved_areas']
        for area in approved.get('areas', []):
            area['observations'] = []
        # 미확인은 이 방향의 사실과 겹치는 항목만 보낸다.
        mine = set(slot['fact_refs']) | (refs_in(draft, 'fact_refs') if draft is not None else set())
        approved['unresolved'] = related_unresolved(approved.get('unresolved', []), mine)

    for name in DROP_CONTEXT:
        view['context'].pop(name, None)

    direct = set(slot['fact_refs']) | (refs_in(draft, 'fact_refs') if draft is not None else set())
    facts = []
    for fact in view['facts']:
        if fact['id'] not in cited_facts:
            continue
        if _chars(fact['value']) > LARGE_FACT_CHARS and fact['id'] not in direct:
            fact['value'] = {'not_inlined': True, 'observed_chars': _chars(fact['value']),
                             'reason': 'large_fact_not_cited_by_this_item_directly'}
        facts.append(fact)
    view['facts'] = facts
    view['criteria'] = [row for row in view['criteria'] if row['id'] in cited_criteria]
    fact_ids = {row['id'] for row in facts}
    criterion_ids = {row['id'] for row in view['criteria']}
    view['judgments'] = [row for row in view['judgments']
                         if set(row['fact_refs']) <= fact_ids and set(row['criterion_refs']) <= criterion_ids]
    view['measurements'] = [row for row in view['measurements'] if set(row['fact_refs']) <= fact_ids]
    # 이 호출이 쓰는 ID만 남긴다(방향 1개 작성에 전체 방향 ID 목록은 필요하지 않다).
    own = slot['id']
    view['allocated_ids'] = {key: [i for i in ids if i == own] if key == 'directions' else [] for key, ids in view['allocated_ids'].items()}
    scope = view['context'].setdefault('model_scope', {})
    scope.update(projection_schema='asis-model-scope/1',
                 fact_refs=sorted(fact_ids), criterion_refs=sorted(criterion_ids),
                 goal_refs=[row['id'] for row in view.get('goals', [])],
                 inline_fact_refs=_inline_refs(facts, direct))
    view['context']['model_view'] = {'kind': kind, 'rule': 'items cited by this direction only; omitted content stays in the canonical ledger'}
    return view


def item_view(data, kind):
    """진단·과제 항목 1개 작성·검토에 필요한 내용만 담은 전송용 뷰. 정본 data는 변경하지 않는다.

    슬롯이 연결한 방향과 그 방향이 인용한 영역 시사점, 슬롯·방향·진단이 인용한 사실·기준만 보낸다.
    관찰 본문은 시사점이 인용한 ID로만 이어지므로 보내지 않고, 미확인은 슬롯 사실과 겹치는 것만 보낸다."""
    view = deepcopy(data)
    sd = view['stage_data']
    slot = sd['item_assignment']['slot']
    draft = sd.get('draft')
    directions = sd.get('approved_directions', {}).get('directions', [])
    linked_implications = {ref for row in directions for ref in row.get('area_implication_refs', [])}

    cited_facts = set(slot['fact_refs']); cited_criteria = set(slot['criterion_refs'])
    for row in directions:
        cited_facts |= set(row.get('fact_refs', [])); cited_criteria |= set(row.get('criterion_refs', []))
    for block in ('approved_diagnoses',):
        if isinstance(sd.get(block), dict):
            cited_facts |= refs_in(sd[block], 'fact_refs'); cited_criteria |= refs_in(sd[block], 'criterion_refs')
    if draft is not None:
        cited_facts |= refs_in(draft, 'fact_refs'); cited_criteria |= refs_in(draft, 'criterion_refs')

    if isinstance(sd.get('approved_areas'), dict):
        approved = sd['approved_areas']
        approved['areas'] = [area for area in approved.get('areas', []) if area['implication']['id'] in linked_implications]
        for area in approved['areas']:
            area['observations'] = []
            cited_facts |= set(area['implication']['fact_refs']); cited_criteria |= set(area['implication']['criterion_refs'])
        mine = set(slot['fact_refs']) | (refs_in(draft, 'fact_refs') if draft is not None else set())
        approved['unresolved'], omitted = budgeted_unresolved(related_unresolved(approved.get('unresolved', []), mine), mine, UNRESOLVED_BUDGET_CHARS)
        if omitted:
            approved['omitted_unresolved'] = omitted
    # 승인 방향의 미확인 목록도 이 항목과 겹치는 것만 보낸다. 전체 목록은 정본 승인 결과에 그대로 있다.
    mine_all = set(slot['fact_refs']) | (refs_in(draft, 'fact_refs') if draft is not None else set())
    if isinstance(sd.get('approved_directions'), dict):
        approved_dirs = sd['approved_directions']
        approved_dirs['unresolved'], omitted_dirs = budgeted_unresolved(related_unresolved(approved_dirs.get('unresolved', []), mine_all), mine_all, UNRESOLVED_BUDGET_CHARS)
        if omitted_dirs:
            approved_dirs['omitted_unresolved'] = omitted_dirs

    for name in DROP_CONTEXT:
        view['context'].pop(name, None)
    direct = set(slot['fact_refs']) | (refs_in(draft, 'fact_refs') if draft is not None else set())
    facts = []
    for fact in view['facts']:
        if fact['id'] not in cited_facts:
            continue
        if _chars(fact['value']) > LARGE_FACT_CHARS and fact['id'] not in direct:
            fact['value'] = {'not_inlined': True, 'observed_chars': _chars(fact['value']),
                             'reason': 'large_fact_not_cited_by_this_item_directly'}
        facts.append(fact)
    view['facts'] = facts
    view['criteria'] = [row for row in view['criteria'] if row['id'] in cited_criteria]
    fact_ids = {row['id'] for row in facts}; criterion_ids = {row['id'] for row in view['criteria']}
    view['judgments'] = [row for row in view['judgments'] if set(row['fact_refs']) <= fact_ids and set(row['criterion_refs']) <= criterion_ids]
    view['measurements'] = [row for row in view['measurements'] if set(row['fact_refs']) <= fact_ids]
    own = sd['item_assignment']['item_id']
    view['allocated_ids'] = {key: [i for i in ids if i == own] for key, ids in view['allocated_ids'].items()}
    scope = view['context'].setdefault('model_scope', {})
    scope.update(projection_schema='asis-model-scope/1', fact_refs=sorted(fact_ids), criterion_refs=sorted(criterion_ids),
                 goal_refs=[row['id'] for row in view.get('goals', [])], inline_fact_refs=_inline_refs(facts, direct))
    view['context']['model_view'] = {'kind': kind, 'rule': 'items cited by this item and its linked direction only; omitted content stays in the canonical ledger'}
    return view


def item_visual_review_view(data):
    """17단계 항목별 장표 검토의 전송용 뷰. 항목 1개의 렌더 장표와 그 항목의 서술만 본다.

    운영 사전 측정(2026-10-19, 프로젝트 25): 항목 121개 중 13개의 입력이 모델 한도를 넘었다(최대 38.5만 토큰). 사실 본문 45.7만 자(큰 사실 정책 세트 7.6만 자 등)와
    승인 영역·방향 블록 27만 자가 입력의 대부분이었고, 이미지 검토는 이를 쓰지 않는다. 항목 입력의 기본 뷰(item_view)를 먼저 적용한 뒤 사실 본문·승인 블록·수집 상태를 뺀다.
    정본 data 는 바꾸지 않는다. 지시문·통과 조건은 바꾸지 않는다."""
    view = item_view(data, 'item_review')
    sd = view['stage_data']
    for key in [k for k in sd if k.startswith('approved_')]:
        del sd[key]
    view['facts'] = [fact if fact['value'] is None else {**fact, 'value': {'not_inlined': True, 'observed_chars': _chars(fact['value']), 'reason': 'item_visual_review_uses_ids_only'}}
                     for fact in view['facts']]
    for name in SUMMARY_DROP_CONTEXT:
        view['context'].pop(name, None)
    view['criteria'] = []; view['judgments'] = []; view['measurements'] = []
    scope = view['context'].setdefault('model_scope', {})
    scope.update(fact_refs=sorted(row['id'] for row in view['facts']), criterion_refs=[], inline_fact_refs=[])
    view['context']['model_view'] = {'kind': 'item_visual_review', 'rule': 'the rendered pages of one item and that item text only; bodies and approved blocks stay in the canonical ledger'}
    return view


def synthesis_shard_view(data, kind):
    """종합 방향 구간 검토 1개의 전송용 뷰. 담당 방향 본문과 그 방향이 인용한 사실·기준·시사점만 보낸다.

    구간 검토는 담당 방향의 근거·범위·상충을 본다. 관찰 본문은 시사점이 인용한 ID로만 이어지므로 보내지 않고,
    미확인은 담당 방향의 사실과 겹치는 것만 보낸다. 다른 구간의 방향은 draft에 omitted로 남아 위치가 유지된다."""
    view = deepcopy(data)
    sd = view['stage_data']
    draft = sd['draft']
    mine = [row for row in draft.get('directions', []) if not row.get('omitted')]
    cited_facts = refs_in(mine, 'fact_refs'); cited_criteria = refs_in(mine, 'criterion_refs')
    # draft의 합본 미확인·edges도 담당 방향 몫만 본문을 남기고 나머지는 자리(omitted)만 둔다. 지적 위치(/unresolved/N)가 정본과 같다.
    mine_ids = {row['id'] for row in mine}
    # 담당 방향의 사실과 가장 많이 겹치는 미확인부터 본문을 채우고(방향 입력과 같은 예산), 나머지는 자리만 남겨 위치(/unresolved/N)를 정본과 같게 둔다.
    # 단순 교집합은 공통 사실이 미확인에 널리 인용되는 프로젝트에서 사실상 전부를 유지했다(평가 24: 917건 중 834건).
    rows = draft.get('unresolved', [])
    kept_rows, omitted_summary = budgeted_unresolved(related_unresolved(rows, cited_facts, all_rows=rows) if rows else rows, cited_facts, UNRESOLVED_BUDGET_CHARS)
    kept_keys = {json.dumps(row, ensure_ascii=False, sort_keys=True) for row in kept_rows}
    draft['unresolved'] = [row if (isinstance(row, dict) and json.dumps(row, ensure_ascii=False, sort_keys=True) in kept_keys) else {'omitted': True}
                           for row in rows]
    if len(kept_rows) < len(rows):
        draft['omitted_unresolved'] = {'omitted_count': len(rows) - len(kept_rows), 'kept_count': len(kept_rows),
                                       'omitted_topics': [{'topic': r.get('topic', ''), 'fact_ref_count': len(r.get('fact_refs', []))}
                                                          for r in rows if isinstance(r, dict) and json.dumps(r, ensure_ascii=False, sort_keys=True) not in kept_keys],
                                       'reason': 'unresolved_text_over_input_budget_topics_kept_text_in_canonical_ledger'}
    draft['edges'] = [row for row in draft.get('edges', []) if row.get('to_id') in mine_ids]
    linked = {ref for row in mine for ref in row.get('area_implication_refs', [])}
    if isinstance(sd.get('approved_areas'), dict):
        approved = sd['approved_areas']
        approved['areas'] = [area for area in approved.get('areas', []) if area['implication']['id'] in linked]
        for area in approved['areas']:
            area['observations'] = []
            cited_facts |= set(area['implication']['fact_refs']); cited_criteria |= set(area['implication']['criterion_refs'])
        approved['unresolved'], omitted_areas = budgeted_unresolved(related_unresolved(approved.get('unresolved', []), cited_facts), cited_facts, UNRESOLVED_BUDGET_CHARS)
        if omitted_areas:
            approved['omitted_unresolved'] = omitted_areas
    for name in DROP_CONTEXT + SHARD_DROP_CONTEXT:
        view['context'].pop(name, None)
    direct = refs_in(mine, 'fact_refs')
    facts = []
    for fact in view['facts']:
        if fact['id'] not in cited_facts:
            continue
        if _chars(fact['value']) > LARGE_FACT_CHARS and fact['id'] not in direct:
            fact['value'] = {'not_inlined': True, 'observed_chars': _chars(fact['value']),
                             'reason': 'large_fact_not_cited_by_this_shard_directly'}
        facts.append(fact)
    view['facts'] = facts
    view['criteria'] = [row for row in view['criteria'] if row['id'] in cited_criteria]
    fact_ids = {row['id'] for row in facts}; criterion_ids = {row['id'] for row in view['criteria']}
    view['judgments'] = [row for row in view['judgments'] if set(row['fact_refs']) <= fact_ids and set(row['criterion_refs']) <= criterion_ids]
    view['measurements'] = [row for row in view['measurements'] if set(row['fact_refs']) <= fact_ids]
    scope = view['context'].setdefault('model_scope', {})
    scope.update(projection_schema='asis-model-scope/1', fact_refs=sorted(fact_ids), criterion_refs=sorted(criterion_ids),
                 goal_refs=[row['id'] for row in view.get('goals', [])], inline_fact_refs=_inline_refs(facts, direct))
    view['context']['model_view'] = {'kind': kind, 'rule': 'directions of this shard and the facts, criteria and implications they cite only'}
    return view


def synthesis_integration_view(data, kind):
    """종합 통합 검토 1개의 전송용 뷰. 방향 간 중복·상충·누락·우선순위 일관성과 목표 연결만 본다.

    후보(draft)는 호출부가 방향 요약본으로 만들어 보낸다. 영역 시사점은 방향 간 일관성 대조에 필요하므로 남기되
    관찰 본문과 미확인 본문(예산 초과분)은 보내지 않고, 계획 정본(분석 계획·진단·과제 계획·수집 기준 매칭)도 보내지 않는다.
    미확인은 자리·주제를 남겨 위치(/unresolved/N)가 정본과 같다. 정본 data는 변경하지 않는다."""
    view = deepcopy(data)
    sd = view['stage_data']
    draft = sd['draft']
    rows = draft.get('unresolved', [])
    if rows:
        kept_rows, _ = budgeted_unresolved(rows, set(), UNRESOLVED_BUDGET_CHARS)
        kept_keys = {json.dumps(row, ensure_ascii=False, sort_keys=True) for row in kept_rows}
        draft['unresolved'] = [row if (isinstance(row, dict) and json.dumps(row, ensure_ascii=False, sort_keys=True) in kept_keys) else {'omitted': True} for row in rows]
        if len(kept_rows) < len(rows):
            draft['omitted_unresolved'] = {'omitted_count': len(rows) - len(kept_rows), 'kept_count': len(kept_rows),
                                           'omitted_topics': [{'topic': r.get('topic', ''), 'fact_ref_count': len(r.get('fact_refs', []))}
                                                              for r in rows if isinstance(r, dict) and json.dumps(r, ensure_ascii=False, sort_keys=True) not in kept_keys],
                                           'reason': 'unresolved_text_over_input_budget_topics_kept_text_in_canonical_ledger'}
    if isinstance(sd.get('approved_areas'), dict):
        approved = sd['approved_areas']
        for area in approved.get('areas', []):
            area['observations'] = []
        approved['unresolved'], omitted_areas = budgeted_unresolved(approved.get('unresolved', []), set(), UNRESOLVED_BUDGET_CHARS)
        if omitted_areas:
            approved['omitted_unresolved'] = omitted_areas
    for name in DROP_CONTEXT + SHARD_DROP_CONTEXT:
        view['context'].pop(name, None)
    scope = view['context'].setdefault('model_scope', {})
    scope.update(projection_schema='asis-model-scope/1', fact_refs=sorted(row['id'] for row in view.get('facts', [])),
                 criterion_refs=sorted(row['id'] for row in view.get('criteria', [])), goal_refs=[row['id'] for row in view.get('goals', [])],
                 inline_fact_refs=sorted(row['id'] for row in view.get('facts', [])))
    view['context']['model_view'] = {'kind': kind, 'rule': 'direction summaries, goals and implications only; observation and unresolved bodies stay in the canonical ledger'}
    return view


def budgeted_unresolved(rows, mine, budget_chars):
    """미확인 목록을 크기 예산 안으로 줄인다. 이 항목의 사실과 가장 많이 겹치는 미확인부터 본문을 채우고, 예산을 넘는 나머지는 본문 없이 주제만 남긴다.

    미확인은 '확인되지 않은 것을 정상·결함으로 확정하지 않는다'는 안전장치라 사라지면 안 된다. 그래서 본문을 못 넣는 항목도 존재와 주제는 남긴다
    (`omitted_unresolved`). 정본 승인 결과는 바꾸지 않고 전송용 사본에서만 줄인다. 예산이 넉넉하면 입력을 그대로 돌려준다."""
    if budget_chars is None or _chars(rows) <= budget_chars:
        return list(rows), None
    scored = sorted(range(len(rows)), key=lambda i: (-len(set(rows[i].get('fact_refs', [])) & mine), i))
    kept_idx, used = set(), 0
    for i in scored:
        size = _chars(rows[i])
        if used + size > budget_chars:
            continue
        kept_idx.add(i); used += size
    kept = [rows[i] for i in range(len(rows)) if i in kept_idx]
    omitted = [{'topic': rows[i].get('topic', ''), 'fact_ref_count': len(rows[i].get('fact_refs', []))} for i in range(len(rows)) if i not in kept_idx]
    return kept, {'omitted_count': len(omitted), 'kept_count': len(kept), 'omitted_topics': omitted,
                  'reason': 'unresolved_text_over_input_budget_topics_kept_text_in_canonical_ledger'}


SUMMARY_LIMIT = 900_000
SUMMARY_FACT_INLINE_CHARS = 4_000

# 요약 모델이 쓰지 않는 배경 원장·계획·수집 매칭. 요약은 승인된 영역·방향·과제의 ID와 핵심 문장으로 연결한다(정본에는 그대로 있다).
SUMMARY_DROP_CONTEXT = DROP_CONTEXT + ('collection_basis_matches', 'analysis_plan', 'action_item_plan', 'collection_states', 'dataset_register', 'dataset_index',
                                       'criterion_assessments', 'metric_summary', 'goal_interpretations', 'cost_summary', 'inventory_summary')


def _topics_only(rows):
    """미확인 본문 없이 주제와 사실 개수만 남긴다. 요약은 '무엇이 확인되지 않았는가'의 목록만 필요하다."""
    return [{'topic': row.get('topic', ''), 'fact_ref_count': len(row.get('fact_refs', []))} for row in rows]


def summary_view(data):
    """요약 작성·검토의 전송용 뷰. 승인된 방향·과제·영역의 미확인은 주제 목록으로 줄이고, 요약이 쓰지 않는 배경 context·사실 본문은 뺀다.

    운영 2026-10-16: 요약 입력이 286만 자(110만 토큰)로 모델 상한을 넘어 서비스가 400으로 거절했다. 미확인 본문 83만 자와 배경 context 112만 자가 원인이었다.
    정본 data는 바꾸지 않고, 전송 사본에서만 줄인다."""
    view = deepcopy(data)
    sd = view['stage_data']
    cited = set()
    for block in ('approved_areas', 'approved_directions', 'approved_actions', 'draft'):
        cited |= refs_in(sd.get(block), 'fact_refs')
    for block in ('approved_areas', 'approved_directions', 'approved_actions'):
        if isinstance(sd.get(block), dict) and isinstance(sd[block].get('unresolved'), list):
            full = sd[block]['unresolved']
            sd[block]['unresolved'] = _topics_only(full)
            sd[block]['unresolved_note'] = {'total': len(full), 'rule': 'topics_and_fact_counts_only_text_in_canonical_ledger'}
    # 요약은 승인된 방향·진단·과제의 ID와 핵심 문장으로 연결한다(기존 요약 전송 규칙). 정본 승인 결과는 그대로이고 전송 사본에서만 필드를 줄인다.
    keep = lambda rows, fields: [{key: deepcopy(row[key]) for key in fields if key in row} for row in rows]
    if isinstance(sd.get('approved_areas'), dict):
        sd['approved_areas'] = {**{k: v for k, v in sd['approved_areas'].items() if k != 'areas'},
            'areas': [{'area_id': row['area_id'], 'headline': row['headline'], 'implication': deepcopy(row['implication'])} for row in sd['approved_areas']['areas']]}
    if isinstance(sd.get('approved_directions'), dict):
        sd['approved_directions'] = {**{k: v for k, v in sd['approved_directions'].items() if k not in ('directions', 'edges')},
            'directions': keep(sd['approved_directions']['directions'], ('id', 'title', 'text', 'customer_impact', 'change_direction', 'area_implication_refs'))}
    if isinstance(sd.get('approved_actions'), dict):
        sd['approved_actions'] = {**{k: v for k, v in sd['approved_actions'].items() if k not in ('diagnoses', 'actions', 'edges')},
            'diagnoses': keep(sd['approved_actions']['diagnoses'], ('id', 'text', 'impact', 'direction_refs')),
            'actions': keep(sd['approved_actions']['actions'], ('id', 'title', 'target', 'diagnosis_refs', 'priority'))}
    for name in SUMMARY_DROP_CONTEXT:
        view['context'].pop(name, None)
    # 요약은 사실을 fact_refs의 ID로 인용하고 핵심 숫자는 measurements에서 가져온다. 본문이 4천 자를 넘는 사실은 ID·출처·크기만 보낸다(본문은 정본에 있다).
    view['facts'] = [{**fact, 'value': {'not_inlined': True, 'observed_chars': _chars(fact['value']), 'reason': 'summary_uses_cited_ids_and_measurements_only'}}
                     if _chars(fact['value']) > SUMMARY_FACT_INLINE_CHARS else fact for fact in view['facts'] if fact['id'] in cited]
    fact_ids = {row['id'] for row in view['facts']}
    view['judgments'] = [row for row in view['judgments'] if set(row['fact_refs']) <= fact_ids]
    view['measurements'] = [row for row in view['measurements'] if set(row['fact_refs']) <= fact_ids]
    scope = view['context'].setdefault('model_scope', {})
    scope.update(projection_schema='asis-model-scope/1', fact_refs=sorted(fact_ids), goal_refs=[row['id'] for row in view.get('goals', [])],
                 inline_fact_refs=sorted(row['id'] for row in view['facts'] if not (isinstance(row['value'], dict) and row['value'].get('not_inlined'))),
                 criterion_refs=sorted(row['id'] for row in view['criteria']))
    view['context']['model_view'] = {'kind': 'summary', 'rule': 'approved areas, directions and actions by id and sentence; unresolved as topics; omitted content stays in the canonical ledger'}
    return view


def visual_review_view(data):
    """16단계 장표(이미지) 묶음 검토의 전송용 뷰. 검토는 렌더 이미지의 잘림·겹침·라벨·가독성과 장표 텍스트가 담당 항목의 서술과 맞는지를 본다.

    운영 사전 측정(2026-10-19, 프로젝트 25): 장표 4장 묶음 1회가 594만 자(246만 토큰)였다. 후보에 전체 stages(196만 자)가 실리고 승인 블록 3개(195만 자)가 같은 본문을 또 실었다.
    승인 블록을 빼고, 후보의 stages 는 장표가 속한 항목(item_refs)과 그 상위 연결의 서술만 남긴다. 사실 본문은 보내지 않는다(이미지 검토는 사실 값 대조가 아니다).
    정본 data 는 바꾸지 않는다. 지시문·통과 조건은 바꾸지 않는다."""
    view = deepcopy(data)
    sd = view['stage_data']
    for key in [k for k in sd if k.startswith('approved_')]:
        del sd[key]
    draft = sd['draft']
    owned = {ref for page in draft.get('pages', []) if isinstance(page, dict) for ref in page.get('item_refs', []) if isinstance(ref, str)}
    stages = draft.get('stages', [])
    kept = []
    for index, stage in enumerate(stages):
        if not isinstance(stage, dict) or stage.get('omitted'):
            kept.append(stage); continue
        copy = {k: v for k, v in stage.items() if k not in ('areas', 'directions', 'diagnoses', 'actions', 'edges', 'unresolved', 'summary')}
        if index == 2:
            copy['diagnoses'] = [row if row.get('id') in owned else {'id': row.get('id'), 'omitted': True} for row in stage.get('diagnoses', [])]
            copy['actions'] = [row if row.get('id') in owned else {'id': row.get('id'), 'omitted': True} for row in stage.get('actions', [])]
            linked = {ref for row in copy['diagnoses'] + copy['actions'] if not row.get('omitted') for ref in row.get('direction_refs', []) + row.get('diagnosis_refs', [])}
        elif index == 1:
            copy['directions'] = [{'id': row.get('id'), 'title': row.get('title')} for row in stage.get('directions', [])]
        elif index == 0:
            copy['areas'] = [{'area_id': row.get('area_id'), 'headline': row.get('headline')} for row in stage.get('areas', [])]
        elif index == 3:
            copy['summary'] = stage.get('summary')
        kept.append(copy)
    draft['stages'] = kept
    cited = refs_in(draft, 'fact_refs')
    view['facts'] = [fact if fact['value'] is None else {**fact, 'value': {'not_inlined': True, 'observed_chars': _chars(fact['value']), 'reason': 'visual_review_uses_ids_only'}}
                     for fact in view['facts'] if fact['id'] in cited]
    for name in SUMMARY_DROP_CONTEXT:
        view['context'].pop(name, None)
    view['criteria'] = []; view['judgments'] = []; view['measurements'] = []
    scope = view['context'].setdefault('model_scope', {})
    scope.update(projection_schema='asis-model-scope/1', fact_refs=sorted(row['id'] for row in view['facts']), criterion_refs=[],
                 goal_refs=[row['id'] for row in view.get('goals', [])], inline_fact_refs=[])
    view['context']['model_view'] = {'kind': 'visual_review', 'rule': 'rendered page images, the page texts and only the items the pages belong to; approved blocks and bodies stay in the canonical ledger'}
    return view


def is_document_review_mode(mode):
    """문서 검토의 작업 키: 샤드(`document-shard-<영역|shared|synthesis|actions|summary>`)와 영역 간 통합(`document-integration`)."""
    return isinstance(mode, str) and (mode == 'document-integration' or mode.startswith('document-shard-'))


def document_review_view(data):
    """문서 검토 호출의 전송용 뷰. 검토 대상은 draft.stages이고, 근거는 후보가 인용한 사실·기준만 필요하다.

    운영 2026-10-16: 문서 검토 15개 호출이 모두 한도를 넘어(96만~177만 토큰) 개정이 중지됐다. 후보는 9천 자인데 배경 context 113만 자와
    사실 50만 자가 실렸다. 정본 data는 바꾸지 않고 전송 사본에서만 줄인다."""
    view = deepcopy(data)
    sd = view['stage_data']
    # 승인 블록(approved_areas·approved_directions·approved_actions)은 검토 대상 draft 가 그대로 담은 같은 본문이다. 문서 검토는 draft 만 대조하므로 전송 사본에서 뺀다.
    # 운영 2026-10-19(프로젝트 25 12단계): 이 3개 블록 195만 자가 14개 호출 모두에 실려 영역 샤드(draft 4.7만 자)조차 209.7만 자였다. 정본 data 는 바꾸지 않는다.
    for key in [k for k in sd if k.startswith('approved_')]:
        del sd[key]
    draft = sd['draft']
    cited = refs_in(draft, 'fact_refs')
    criteria = refs_in(draft, 'criterion_refs')
    # 아키텍처 정합성 검토(영석님 정의 2026-10-19): 세부 사실은 4·7·10단계에서 이미 검증됐다. 문서 전체 검토 호출은 사실 본문·수집 상태·판정·측정을 싣지 않고
    # 승인된 층별 서술과 ID 연결만 본다. 운영 11·12차 재개에서 이를 다시 대조하게 해 입력 한도 초과·blocked·허위 반려가 났다.
    mode_key = str(sd.get('mode', ''))
    architecture = mode_key.startswith('document-shard-arch-') or mode_key == 'document-integration'
    if architecture:
        cited, criteria = set(), set()
    # 영역 검토는 수집 사유·수집 대상(collection_states·dataset_register·dataset_index)을 근거로 판단한다. 이 값은 사실 값이 아니라 context 에만 있다.
    # 운영 2026-10-19(프로젝트 25 11·12차 재개): 요약 뷰 규칙(SUMMARY_DROP_CONTEXT)이 이 키들을 함께 지워, 4단계에서 통과한 관찰 8건(no-provisioned-deployments·
    # AzureOpenAITimeToResponse P1D·no-parent-resources 등 원장에 있는 사유)을 12단계 영역 검토가 '수집 사유가 제시되지 않았다'며 반려했다. 인용한 사실의 것만 싣는다.
    mode_name = str(sd.get('mode', ''))
    keep_states = mode_name.startswith('document-shard-area-')
    kept = {name: view['context'][name] for name in ('collection_states', 'dataset_register', 'dataset_index') if keep_states and name in view['context']}
    for name in SUMMARY_DROP_CONTEXT:
        view['context'].pop(name, None)
    if isinstance(kept.get('collection_states'), dict):
        view['context']['collection_states'] = {fid: row for fid, row in kept['collection_states'].items() if fid in cited}
    for name in ('dataset_register', 'dataset_index'):
        if isinstance(kept.get(name), list):
            view['context'][name] = [row for row in kept[name] if isinstance(row, dict) and row.get('fact_id') in cited]
    # 값을 대조하는 검토(영역·종합·과제 구간)는 후보가 인용한 사실의 값을 크기와 무관하게 보낸다. 값을 못 받은 검토자는 작성자의 구체 값(배포 SKU·정책 참조·메트릭 차원)을
    # 대조할 수 없어 blocked 로 판정한다(운영 2026-10-19 프로젝트 25 12단계: 영역 검토 9개가 모두 blocked, 미확인 26건. 8단계 _review_scope 가 이미 같은 이유로 인용 사실 값을 보낸다).
    # 영역 간 일관성만 보는 통합 검토와 요약 검토는 값을 대조하지 않으므로 큰 사실을 ID·크기로 남긴다.
    # 값을 대조하는 단위는 영역 검토뿐이다. 종합·과제 구간은 방향·진단의 근거 관계를 보므로 큰 사실을 ID·크기로 남긴다(12차 재개: 값을 넓게 보냈더니 구간 입력이
    # 92.8만 자 > 90만으로 실패했다. 11차에서는 이 상태로 한도 안에서 모델에 도달했다).
    mode = str(sd.get('mode', ''))
    compares_values = mode.startswith('document-shard-area-')
    view['facts'] = [{**fact, 'value': {'not_inlined': True, 'observed_chars': _chars(fact['value']), 'reason': 'document_review_uses_cited_ids_only'}}
                     if (not compares_values and _chars(fact['value']) > SUMMARY_FACT_INLINE_CHARS) else fact for fact in view['facts'] if fact['id'] in cited]
    view['criteria'] = [row for row in view['criteria'] if row['id'] in criteria]
    fact_ids = {row['id'] for row in view['facts']}; criterion_ids = {row['id'] for row in view['criteria']}
    view['judgments'] = [row for row in view['judgments'] if set(row['fact_refs']) <= fact_ids and set(row['criterion_refs']) <= criterion_ids]
    view['measurements'] = [row for row in view['measurements'] if set(row['fact_refs']) <= fact_ids]
    scope = view['context'].setdefault('model_scope', {})
    scope.update(projection_schema='asis-model-scope/1', fact_refs=sorted(fact_ids), criterion_refs=sorted(criterion_ids),
                 goal_refs=[row['id'] for row in view.get('goals', [])],
                 inline_fact_refs=sorted(row['id'] for row in view['facts'] if not (isinstance(row['value'], dict) and row['value'].get('not_inlined'))))
    view['context']['model_view'] = {'kind': 'document_review', 'rule': 'candidate stages and the facts and criteria they cite; background ledgers stay in the canonical ledger'}
    return view


def build(stage, data, summary_level=0):
    """(종류, 전송 뷰). 해당 없으면 (None, data). 상한을 넘으면 모델 호출 전에 실패한다.

    summary_level 1·2 는 길이로 2번 이상 실패한 항목(item_*)의 전송 뷰를 코드가 결정적으로 요약한 뒤 한도를 잰다(asis_input_summary).
    요약은 한도 검사보다 앞에서 걸려야 한다: 전송 전 검사가 요약 지점에 닿기 전에 항목을 실패시키던 운영 사례(2026-10-18, 연결 방향 17개 과제)의 수정이다.
    요약 후에도 넘으면 더 깎지 않고 ValueError 를 올린다. 정본 data 는 바꾸지 않는다."""
    kind = kind_of(stage, data)
    if kind is None:
        return None, data
    view = (document_review_view(data) if kind == 'document_review' else visual_review_view(data) if kind == 'visual_review' else item_visual_review_view(data) if kind == 'item_visual_review' else summary_view(data) if kind in ('summary_author', 'summary_review') else item_view(data, kind) if kind.startswith('item_')
            else synthesis_shard_view(data, kind) if kind == 'synthesis_shard_review' else synthesis_integration_view(data, kind) if kind == 'synthesis_integration_review' else direction_view(data, kind))
    if summary_level and kind.startswith('item_'):
        import asis_input_summary
        assignment = view.get('stage_data', {}).get('item_assignment') or {}
        view, _ = asis_input_summary.summarize_item_input(view, summary_level, (assignment.get('slot') or {}).get('fact_refs', []))
    elif summary_level and kind in ('summary_author', 'summary_review'):
        # 11단계 요약 입력도 길이로 2번 이상 실패하면 승인 서술문을 이름·숫자 보존 규칙으로 축약한다(운영 2026-10-18: 34.3만 토큰 > 27.2만).
        import asis_input_summary
        from asis_evidence_views import prompt_view as _prompt_view
        # 2단계는 한도(LIMITS)의 95% 안에 들 때까지만 중복 문장을 지운다(필요한 만큼만). 1단계 결과의 실제 전송 크기로 목표를 잡는다.
        target = None
        measure = lambda v: _chars(_prompt_view(deepcopy(v)))          # 한도와 같은 단위(글자 수)로 잰다
        if summary_level >= 2:
            first, _ = asis_input_summary.summarize_summary_input(view, 1)
            token_budget_chars = int(MODEL_TOKEN_LIMIT * 0.95 * CHARS_PER_TOKEN[kind])      # 모델 토큰 한도의 95% 에 해당하는 글자 수
            over = measure(first) - min(token_budget_chars, int(LIMITS[kind] * 0.95))
            if over > 0:
                target = max(0, measure(first) - over)
        view, _ = asis_input_summary.summarize_summary_input(view, summary_level, target_bytes=target, measure=measure if target is not None else None)
    # 한도는 모델에 실제로 가는 크기로 잰다. 전송 직전에 prompt_view가 가격 원천 같은 대형 사실의 본문을 해시·행 수로 바꾸므로, 가공 전 뷰의 크기로
    # 재면 모델에 보내지 않을 본문까지 세어 정상 호출을 거절한다(운영 2026-10-16: 실제 전송 18만~47만 자인 방향 6개가 145만~196만 자로 계산돼 실패).
    from asis_evidence_views import prompt_view
    size = _chars(prompt_view(deepcopy(view)))
    if size > LIMITS[kind]:
        raise ValueError('model_input_over_limit:%s:%d>%d' % (kind, size, LIMITS[kind]))
    return kind, view
