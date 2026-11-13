"""12단계 문서 전체 검토의 입력 한도 대응: 종합·과제 샤드의 구간 분할과 통합 검토 입력 축약(모델 호출 없음, 순수 함수).

운영 2026-10-19(프로젝트 25 12단계): 종합 샤드 88.9만 자(36.9만 토큰)·과제 샤드 89.2만 자(37.0만 토큰)·통합 85.0만 자(35.3만 토큰)가 모델 한도(27.2만 토큰)를 넘었다.
글자 수 한도(90만)만 있고 토큰 한도가 없어 통과 후 모델이 거절한다. 구간 분할은 종합 단계(asis_synthesis._review_merged_synthesis)와 같은 방식이며,
지적 위치(JSON pointer)는 원본 문서의 위치와 같다(담당 외 항목은 {id, omitted:true} 로 자리만 유지).
"""
import json
from copy import deepcopy

CHARS_PER_TOKEN = 2.41             # 영수증 실측(프로젝트 25 요약 입력 82.7만 자 = 34.3만 토큰)
MODEL_TOKEN_LIMIT = 272_000
BUDGET_FRACTION = 0.90             # 한도의 90% 안에 들도록 나눈다(검토 호출은 지시문·후보가 더해진다)
# 단계 번호 -> (항목 목록 키, 같은 모양으로 유지하는 보조 키들). 4개 단계는 영역(0)·종합(1)·진단·과제(2)·요약(3)이다.
ITEMS = {1: 'directions', 2: 'diagnoses'}


def _chars(value):
    return len(json.dumps(value, ensure_ascii=False))


def token_budget_chars(token_budget=None, fraction=None):
    # 기본값은 호출 시점의 모듈 상수를 읽는다(함수 정의 시점의 값으로 굳으면 한도를 바꿔도 반영되지 않는다).
    token_budget = MODEL_TOKEN_LIMIT if token_budget is None else token_budget
    fraction = BUDGET_FRACTION if fraction is None else fraction
    return int(token_budget * fraction * CHARS_PER_TOKEN)


def fixed_overhead_chars(data, stages):
    """구간·후보와 무관하게 모든 검토 호출에 실리는 글자 수: 사실·기준·측정·판단·목표·배경(투영 후)의 실제 크기.

    실측(프로젝트 25 12단계): 사실 14.6만·기준 4.0만·측정 1.8만·판단 0.8만·배경 1.5만·ID 0.8만 = 약 23.7만 자. 입력(data)에서 그대로 합산한다
    (후보가 인용한 사실만 실리는 투영이라 구간에 따라 줄 수 있으나, 구간이 모든 사실을 인용할 수 있으므로 보수적으로 전부 센다)."""
    total = 0
    for key in ('facts', 'criteria', 'measurements', 'judgments', 'goals', 'allocated_ids'):
        value = data.get(key)
        if value is not None:
            total += _chars(value)
    total += _chars(data.get('context', {}).get('model_scope', {})) if isinstance(data.get('context'), dict) else 0
    return min(total, 260_000)


def _topics(rows):
    """미확인 본문 없이 주제와 사실 건수만 남긴다(다른 구간이 담당하는 본문이다)."""
    return [{'topic': row.get('topic', ''), 'fact_ref_count': len(row.get('fact_refs', []))} for row in rows]


def unresolved_owner(stage, key, row, group_index, group_count, groups):
    """미확인 한 행의 본문을 가질 구간 번호.

    행의 사실(fact_refs)을 인용한 항목이 있는 구간이 후보다. 후보가 하나면 그 구간, 여럿이면(공통 사실을 가리키는 행) 행의 위치를 후보 수로 나눈 나머지로
    돌아가며 배정한다(결정적). 어느 구간의 항목과도 겹치지 않으면 마지막 구간이다. 실측: 735행 중 706행이 첫 구간에 몰려 그 구간 입력이 27.9만 토큰이었다."""
    refs = set(row.get('fact_refs', []))
    candidates = [index for index, ids in enumerate(groups)
                  if any(refs & set(stage[key][i].get('fact_refs', [])) for i in ids if not stage[key][i].get('omitted'))]
    if not candidates:
        return group_count - 1
    if len(candidates) == 1:
        return candidates[0]
    order = [id(r) for r in stage.get('unresolved', [])]
    position = next((n for n, r in enumerate(stage.get('unresolved', [])) if r is row), 0)
    return candidates[position % len(candidates)]


def _stage_overhead(stage, key):
    """구간과 무관하게 매 호출에 실리는 단계 안의 글자 수. 구간 검토에서 미확인 본문은 한 구간만 싣고, 관계선(edges)은 싣지 않으므로
    그 구간(최악)의 크기를 잡는다: 미확인 본문 전체 + 항목 목록 밖의 필드(관계선 제외)."""
    other = {k: v for k, v in stage.items() if k not in (key, 'edges', 'unresolved')}
    return _chars(other) + _chars(stage.get('unresolved', []))


def plan_shard_groups(stages, keep, token_budget=None, fixed_chars=0):
    """한 단계(keep)의 항목 번호를 예산 안에 드는 연속 구간으로 나눈다. 예산 안이면 구간 하나(전체)다.

    구간의 크기는 sparse_group 이 실제로 만드는 단계의 크기(항목 본문 + 그 구간이 맡는 미확인 본문 + 다른 구간 몫의 주제·건수)로 잰다.
    실측(프로젝트 25 12단계): 방향 본문 23.5만 자만 세면 종합 샤드가 예산 안으로 보였지만 실제 전송은 88.9만 자였다.
    fixed_chars 는 구간과 무관하게 항상 실리는 글자 수(사실·기준·목표·지시문)다."""
    stage = stages[keep]
    key = ITEMS[keep]
    count = len(stage[key])
    limit = token_budget_chars(token_budget)

    def size_of(groups, index):
        return fixed_chars + _chars(sparse_group(stages, keep, groups[index], group_index=index, group_count=len(groups), groups=groups)['stages'][keep])
    whole = [list(range(count))]
    if fixed_chars + _chars(stage) <= limit:
        return whole
    # 항목 수 n 을 늘려 가며 균등한 연속 구간으로 나눈다. 모든 구간이 실제 크기로 예산 안이면 채택한다. 항목 하나가 구간이어도 넘으면 가장 잘게 나눈 계획을 돌려준다.
    best = [[i] for i in range(count)]
    for n in range(2, count + 1):
        step = -(-count // n)
        groups = [list(range(s, min(s + step, count))) for s in range(0, count, step)]
        if all(size_of(groups, i) <= limit for i in range(len(groups))):
            return groups
    return best


def sparse_group(stages, keep, group, group_index=0, group_count=1, groups=None):
    """구간(group)의 항목만 본문을 남기고 나머지는 {id, omitted:true} 로 위치만 유지한다. 다른 단계는 omitted 자리 표시다(원본과 같은 위치 키).

    구간이 둘 이상이면: 미확인 본문은 첫 구간(group_index=0)만 싣고 나머지는 주제·건수만 싣는다(항목은 하나도 사라지지 않는다). 관계선(edges)은
    코드가 방향·진단의 참조에서 만드는 ID 쌍이라 구간 검토에서는 건수만 남기고 비운다. 구간이 하나면 단계 전체를 그대로 보낸다."""
    keep_set = set(group)
    result = []
    for index, stage in enumerate(stages):
        if index != keep:
            result.append({'stage': stage['stage'], 'omitted': True})
            continue
        value = deepcopy(stage)
        key = ITEMS[keep]
        value[key] = [row if i in keep_set else {'id': row['id'], 'omitted': True} for i, row in enumerate(stage[key])]
        if group_count > 1:
            # 미확인 본문은 그 행의 사실(fact_refs)을 인용한 항목이 있는 구간이 가진다. 어느 구간의 항목과도 겹치지 않는 행은 마지막 구간이 가진다.
            # 행마다 정확히 한 구간이 본문을 갖고, 나머지 구간은 주제·건수로 자리만 유지한다(실측: 과제 단계 첫 구간이 항목 1개인데 27.1만 토큰이었다).
            mine = [row for row in stage.get('unresolved', []) if unresolved_owner(stage, key, row, group_index, group_count, groups) == group_index] if groups else []
            value['unresolved'] = [row if unresolved_owner(stage, key, row, group_index, group_count, groups) == group_index else {'topic': row.get('topic', ''), 'fact_ref_count': len(row.get('fact_refs', []))}
                                   for row in stage.get('unresolved', [])]
            value['unresolved_note'] = {'total': len(stage.get('unresolved', [])), 'full_text_here': len(mine), 'rule': 'each_unresolved_text_is_in_exactly_one_group_the_rest_are_topics_only'}
            value['edges'] = []
            value['edges_note'] = {'total': len(stage.get('edges', [])), 'rule': 'edges_generated_by_code_from_refs_not_reviewed_per_group'}
        result.append(value)
    return {'stages': result}


def condensed_integration(integration):
    """통합 검토 입력의 방향·진단 서술문을 이름·숫자 보존 규칙으로 축약한다. ID·참조·과제 목록·미확인 주제는 그대로다.

    통합 검토는 영역 간 중복·상충·누락·우선순위 일관성을 본다. 본문 전체는 각 영역·종합·과제 검토 샤드가 이미 검토한다."""
    import asis_input_summary as S
    out = deepcopy(integration)
    report = {'narratives': 0, 'narrative_bytes_saved': 0, 'reverted_fields': 0}
    for stage in out.get('stages', []):
        if isinstance(stage, dict) and not stage.get('omitted'):
            S._condense_narratives(stage, report)
    # 영역·종합·과제 단계가 같은 미확인 주제를 각각 들고 있다(실측: 55.6k+52.8k+45.5k). 같은 주제는 처음 나온 단계에만 싣고 총 건수는 노트에 남긴다.
    # 대상은 영역·종합·과제(0~2번) 단계뿐이다. 요약(3번) 단계의 미확인은 그 단계 고유의 본문(예: 저장 암호화 대조)을 가지며 통합 검토가 요약을 검토하는 유일한 근거다.
    seen = set()
    for index, stage in enumerate(out.get('stages', [])):
        if index <= 2 and isinstance(stage, dict) and not stage.get('omitted') and isinstance(stage.get('unresolved'), list):
            total = len(stage['unresolved'])
            kept = []
            for row in stage['unresolved']:
                key = row.get('topic') if isinstance(row, dict) else None
                if key is not None and key in seen:
                    continue
                if key is not None:
                    seen.add(key)
                kept.append(row)
            if len(kept) != total:
                stage['unresolved'] = kept
                stage.setdefault('unresolved_note', {})
                stage['unresolved_note'] = {**stage['unresolved_note'], 'total': total, 'dedup': 'same_topic_listed_once_across_stages'}
    return out


def merge_group_reviews(reviews):
    """구간 검토 결과를 한 샤드의 검토 결과와 같은 모양으로 합친다. blocked > revise > pass. 지적은 위치·인용·이유가 같으면 한 번만 남긴다."""
    if not reviews:
        raise ValueError('no_group_reviews')
    defects, unresolved, seen = [], [], set()
    for review in reviews:
        for defect in review.get('defects', []):
            signature = (defect.get('where'), defect.get('quote'), defect.get('why'))
            if signature not in seen:
                seen.add(signature)
                defects.append(defect)
        for row in review.get('unresolved', []):
            if row not in unresolved:
                unresolved.append(row)
    verdict = 'blocked' if any(r['verdict'] == 'blocked' for r in reviews) else ('revise' if defects or any(r['verdict'] != 'pass' for r in reviews) else 'pass')
    return {**reviews[0], 'verdict': verdict, 'content_checked': all(r.get('content_checked') for r in reviews), 'render_checked': False,
            'defects': defects, 'unresolved': unresolved}


def integration_view(integration, level=0):
    """통합 검토 입력을 단계별로 줄인다(검토 호출에는 지시문·사실·기준이 더해지므로 호출부가 예산을 정한다).

    level 0: 원본 그대로(통합 후보). level 1: 서술문을 이름·숫자 보존 규칙으로 축약하고 같은 미확인 주제를 한 번만 싣는다.
    level 2: level 1 에 더해 서술문에서 같은 이름·숫자만 되풀이하는 문장을 제거한다(첫 문장은 항상 유지). 이름·숫자는 어느 단계에서도 잃지 않는다.
    반환: (입력, {'level': n})."""
    if level <= 0:
        return deepcopy(integration), {'level': 0}
    import asis_input_summary as S
    out = condensed_integration(integration)
    if level >= 2:
        # 검증된 기존 규칙을 쓴다: 첫 문장은 남기고, 지워도 그 필드의 영문·숫자 토큰이 하나도 줄지 않는 문장만 뒤에서부터 지운다.
        for stage in out.get('stages', []):
            if isinstance(stage, dict) and not stage.get('omitted'):
                S._drop_redundant_sentences(stage, {}, lambda: 0)
    return out, {'level': level}


def fit_integration(integration, budget_chars, fixed_chars=0):
    """예산에 드는 가장 낮은 단계의 통합 검토 입력을 고른다. 모두 넘으면 가장 줄인 단계를 돌려주고 info 에 over=True 를 남긴다."""
    last = None
    for level in (0, 1, 2):
        view, info = integration_view(integration, level)
        last = (view, info)
        if fixed_chars + _chars(view) <= budget_chars:
            return view, dict(info, over=False)
    return last[0], dict(last[1], over=True)



# ── 12단계 = 아키텍처 관점 정합성 검토(영석님 정의 2026-10-19): 세부 사실은 4·7·10단계에서 이미 검증됐다. 사실 본문을 보내지 않고 층별 서술만 본다 ──────────────

def architecture_units():
    """검토 단위 4개. note 는 검토자에게 주는 범위 안내이며 사실 대조를 시키지 않는다."""
    return [
        {'key': 'arch-area', 'stage': 0, 'note': '영역 사이의 결론 충돌·중복·우선순위 불일치와 고객 목표와의 연결을 아키텍처 관점에서 검토한다.'},
        {'key': 'arch-direction', 'stage': 1, 'note': '종합 방향 사이의 충돌·중복과 영역 시사점과의 방향 일치를 아키텍처 관점에서 검토한다.'},
        {'key': 'arch-action', 'stage': 2, 'note': '진단·과제 사이의 중복·범위 겹침과 방향→진단→과제 참조가 끊긴 항목을 아키텍처 관점에서 검토한다.'},
        {'key': 'integration', 'stage': None, 'note': '영역 간 중복·상충·누락·우선순위 일관성, 목표와 종합·과제의 연결, 요약과 본문의 일치를 아키텍처 관점에서 검토한다.'},
    ]


def _keep(rows, fields):
    return [{k: deepcopy(row[k]) for k in fields if k in row} for row in rows or []]


def _topic_only(rows):
    return [{'topic': row.get('topic', ''), 'fact_ref_count': len(row.get('fact_refs', []))} for row in rows or []]


def architecture_draft(stages, key):
    """원본과 같은 위치 구조를 유지한 채 담당 층의 서술만 남긴다. 사실 본문은 후보에 원래 없고, 인용 ID 도 개수만 남긴다(검토는 값을 대조하지 않는다)."""
    omitted = lambda stage: {'stage': stage['stage'], 'omitted': True}
    if key == 'integration':
        raise ValueError('integration_draft_comes_from_fit_integration')
    layer = {'arch-area': 0, 'arch-direction': 1, 'arch-action': 2}[key]
    result = []
    for index, stage in enumerate(stages):
        if index != layer:
            result.append(omitted(stage)); continue
        if layer == 0:
            result.append({'stage': stage['stage'], 'run': stage.get('run'),
                'areas': [{'area_id': a['area_id'], 'headline': a['headline'], 'implication': deepcopy(a['implication']),
                           'observations': _keep(a.get('observations'), ('id', 'text'))} for a in stage['areas']],
                'unresolved': _topic_only(stage.get('unresolved')),
                'unresolved_note': {'total': len(stage.get('unresolved') or []), 'rule': 'topics_only_detail_already_reviewed'}})
        elif layer == 1:
            result.append({**{k: deepcopy(v) for k, v in stage.items() if k not in ('directions', 'edges', 'unresolved')},
                'directions': _keep(stage['directions'], ('id', 'title', 'text', 'customer_impact', 'change_direction', 'area_implication_refs')),
                'unresolved': _topic_only(stage.get('unresolved')),
                'unresolved_note': {'total': len(stage.get('unresolved') or []), 'rule': 'topics_only_detail_already_reviewed'}})
        else:
            result.append({**{k: deepcopy(v) for k, v in stage.items() if k not in ('diagnoses', 'actions', 'edges', 'unresolved')},
                'diagnoses': _keep(stage['diagnoses'], ('id', 'text', 'impact', 'direction_refs')),
                'actions': _keep(stage['actions'], ('id', 'title', 'target', 'diagnosis_refs', 'priority')),
                'unresolved': _topic_only(stage.get('unresolved')),
                'unresolved_note': {'total': len(stage.get('unresolved') or []), 'rule': 'topics_only_detail_already_reviewed'}})
    return {'stages': result}
