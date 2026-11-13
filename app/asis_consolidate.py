"""진단·과제 통합 계획: 영역별 진단을 상한 개수의 주제로 묶고, 과제는 영역 경계를 넘어 상한 개수로 묶는다.

모델은 후보 번호의 묶음만 정하고, 참조 합집합·ID·전수 처리는 코드가 확인한다. 운영 실측: 진단 213개·과제 371개를 항목마다
장표로 만들면 1,100장 이상이 나온다. 목표는 영역별 진단·시사점 장표가 영역당 10장 이하, 과제 20개 미만, 전체 권장 100장·상한 150장이다.

이 모듈의 함수는 모델 호출 없이 입력·검증·합치기만 한다(순수 함수). 호출은 asis_item_agents가 한다.
"""
from copy import deepcopy

AREA_DIAGNOSIS_LIMIT = 10      # 영역당 진단·시사점 장표 상한(기본)
AREA_LIMIT_OVERRIDES = {'secgov': 20}   # 주요 영역은 별도 상한(영석님 지시: secgov 20장까지)


def area_limit(area_id):
    return AREA_LIMIT_OVERRIDES.get(area_id, AREA_DIAGNOSIS_LIMIT)
ACTION_RECOMMENDED = 20        # 과제 권장 개수(영석님 지시 2026-10-18)
ACTION_LIMIT = 30              # 과제 최대 개수. 한도를 넘는 큰 과제는 나눌 수 있다(asis_action_split)
DECK_RECOMMENDED_PAGES = 100   # 권장 장수
DECK_MAX_PAGES = 150           # 상한 장수(초과 시 생성 실패)
REFERENCE_KEYS = ('issue_refs', 'direction_refs', 'fact_refs', 'criterion_refs', 'goal_refs')

DIAGNOSIS_GROUPING_INSTRUCTION = (
    '한 영역의 진단 후보 목록이다. 고객이 의사결정에 쓸 핵심 진단 주제로 통합한다. 같은 대상·같은 판단 질문에 답하는 진단은 하나로 묶고, '
    '중요도가 낮거나 유사한 진단은 가장 가까운 주제에 흡수한다. 주제의 개수는 {limit}개 이하다. 모든 진단 후보는 정확히 한 주제에 속한다. '
    '후보의 본문·참조·ID를 새로 쓰지 않고 후보 번호(index)만 사용한다. 주제마다 제목(title)과 묶는 이유(reason)를 쓴다. '
    '미확인을 결함·정상으로 승격하지 않는다. 응답은 {{"topics":[{{"title":"주제","reason":"묶는 이유","members":[후보 번호,...]}}]}} JSON이다. 도구는 사용하지 않는다.')

ACTION_GROUPING_INSTRUCTION = (
    '영역별 진단 주제에 연결된 개선과제 후보 목록이다. 영역 경계를 넘어 같은 실행 단위(같은 변경·같은 선행조건·같은 담당)이면 하나의 과제로 묶는다. '
    '과제의 권장 개수는 20개다. 같은 실행 단위로 묶을 수 있으면 20개 안팎으로 묶고, 실행 단위가 실제로 다르면 20개를 넘길 수 있지만 {limit}개를 넘지 않는다. '
    '{limit}개는 한도이지 목표가 아니다(나중에 큰 과제를 나눌 여유를 남긴다). 모든 과제 후보는 정확히 한 과제에 속한다. 후보의 본문·참조·ID를 새로 쓰지 않고 후보 번호(index)만 사용한다. '
    '과제마다 제목(title)과 묶는 이유(reason)를 쓴다. 응답은 {{"actions":[{{"title":"과제","reason":"묶는 이유","members":[후보 번호,...]}}]}} JSON이다. 도구는 사용하지 않는다.')


def diagnosis_grouping_view(area_id, diagnoses):
    """영역 하나의 진단 후보를 모델에 보낼 소형 입력으로 만든다(번호·제목·이유만, 참조·본문 없음)."""
    return {'area': {'id': area_id}, 'limit': AREA_DIAGNOSIS_LIMIT,
            'candidates': [{'index': i, 'scope': row['scope'], 'reason': row['reason'][:300]} for i, row in enumerate(diagnoses)]}


def action_grouping_view(actions, topic_titles):
    """과제 후보를 영역 경계 없이 한 목록으로 보낸다. 연결된 진단 주제 제목을 함께 보여 같은 실행 단위를 알아보게 한다."""
    return {'limit': ACTION_LIMIT,
            'candidates': [{'index': i, 'areas': row['areas'], 'scope': row['scope'], 'reason': row['reason'][:240],
                            'topics': [topic_titles[t] for t in row['topic_ids'] if t in topic_titles]} for i, row in enumerate(actions)]}


def _validate_partition(count, response, key, limit):
    """모든 후보가 정확히 한 묶음에 속하고 묶음 수가 상한 이하인지 검증한다."""
    if not isinstance(response, dict) or set(response) != {key} or not isinstance(response[key], list) or not response[key]:
        raise ValueError('invalid_%s_shape' % key)
    used = set(); rows = []
    for item in response[key]:
        if (not isinstance(item, dict) or set(item) != {'title', 'reason', 'members'}
                or any(not isinstance(item[name], str) or not item[name].strip() for name in ('title', 'reason'))
                or not isinstance(item['members'], list) or not item['members']
                or any(type(i) is not int or not 0 <= i < count for i in item['members'])
                or len(set(item['members'])) != len(item['members'])):
            raise ValueError('invalid_%s_item' % key)
        if used & set(item['members']):
            raise ValueError('%s_member_reused' % key)
        used |= set(item['members'])
        rows.append({'title': item['title'].strip(), 'reason': item['reason'].strip(), 'members': sorted(item['members'])})
    if used != set(range(count)):
        raise ValueError('%s_coverage_mismatch:%d_of_%d' % (key, len(used), count))
    if len(rows) > limit:
        raise ValueError('%s_over_limit:%d>%d' % (key, len(rows), limit))
    return rows


def validate_diagnosis_topics(count, response, limit=AREA_DIAGNOSIS_LIMIT):
    return _validate_partition(count, response, 'topics', limit)


def validate_action_groups(count, response, limit=ACTION_LIMIT):
    return _validate_partition(count, response, 'actions', limit)


def merge_members(rows, members):
    """묶음에 속한 후보들의 참조를 합집합으로 보존한다(순서는 처음 나온 순). 판단 문구는 만들지 않는다."""
    merged = {key: [] for key in REFERENCE_KEYS}
    for index in members:
        for key in REFERENCE_KEYS:
            for ref in rows[index].get(key, []):
                if ref not in merged[key]:
                    merged[key].append(ref)
    return merged


def build_topics(area_id, diagnoses, groups):
    """검증된 묶음에서 영역의 진단 주제 목록을 만든다. 참조는 코드가 합집합으로 만든다."""
    return [{'id': '%s-T%02d' % (area_id, n + 1), 'area_id': area_id, 'title': group['title'], 'reason': group['reason'],
             'member_indices': group['members'], **merge_members(diagnoses, group['members'])} for n, group in enumerate(groups)]


def build_actions(actions, groups):
    """과제 후보 묶음에서 최종 과제 목록을 만든다. 영역과 연결 진단 주제는 구성원의 합집합이다."""
    out = []
    for n, group in enumerate(groups):
        members = [actions[i] for i in group['members']]
        refs = merge_members(actions, group['members'])
        out.append({'id': 'ACT-%02d' % (n + 1), 'title': group['title'], 'reason': group['reason'], 'member_indices': group['members'],
                    'areas': list(dict.fromkeys(a for row in members for a in row['areas'])),
                    'topic_ids': list(dict.fromkeys(t for row in members for t in row['topic_ids'])), **refs})
    return out


def link_actions_to_topics(plan, area_of_slot, topic_of_diagnosis):
    """통합 전 과제 후보에 영역과 연결 진단 주제를 붙인다. plan은 진단·과제 계획 후보이다."""
    rows = []
    for index, action in enumerate(plan['actions']):
        topics = list(dict.fromkeys(topic_of_diagnosis[d] for d in action['diagnosis_indices']))
        rows.append({**deepcopy(action), 'areas': [area_of_slot(action)], 'topic_ids': topics})
    return rows


def deck_page_budget(topics_by_area, action_count, fixed_pages=8):
    """장표 수를 코드로 계산한다. 영역당 진단·시사점 장표 = 주제 수(상한 10), 과제 오버뷰 = 과제 수, 과제 도출·시사점 종합 각 1장.

    반환: (총 장수, 구성, 상태). 상태는 within_recommended(<=100), over_recommended(<=150), over_max(>150).
    150장 초과는 생성 실패 사유이고 101~150장은 사유를 기록한 채 통과한다."""
    area_pages = {area: min(len(topics), area_limit(area)) for area, topics in topics_by_area.items()}
    parts = {'fixed': fixed_pages, 'area_diagnosis_insight': sum(area_pages.values()), 'insight_synthesis': 1, 'action_derivation': 1,
             'action_overview': action_count}
    total = sum(parts.values())
    state = 'within_recommended' if total <= DECK_RECOMMENDED_PAGES else 'over_recommended' if total <= DECK_MAX_PAGES else 'over_max'
    return total, {**parts, 'by_area': area_pages}, state


def consolidated_plan(base, topics, actions):
    """통합 결과를 기존 진단·과제 계획 후보 형식(diagnoses·actions·coverage)으로 바꾼다.

    뒤 단계(항목별 작성·검토·계약 검증·Excel·PPT)는 이 형식을 그대로 쓴다. 개수만 줄어든 같은 구조다.
    base는 통합 전 계획 후보이고 coverage의 처분(included/deferred/observation_only)은 원본 이슈 기준으로 보존한다.
    진단 주제의 이슈 참조는 구성원 진단의 합집합이라 모든 이슈가 한 번 이상 처리된다."""
    diagnoses = []
    for topic in topics:
        diagnoses.append({'scope': topic['title'], 'reason': topic['reason'], 'issue_refs': list(topic['issue_refs']),
                          'direction_refs': list(topic['direction_refs']), 'fact_refs': list(topic['fact_refs']),
                          'criterion_refs': list(topic['criterion_refs']), 'goal_refs': list(topic['goal_refs'])})
    topic_index = {topic['id']: n for n, topic in enumerate(topics)}
    out_actions = []
    for action in actions:
        out_actions.append({'scope': action['title'], 'reason': action['reason'], 'issue_refs': list(action['issue_refs']),
                            'direction_refs': list(action['direction_refs']), 'fact_refs': list(action['fact_refs']),
                            'criterion_refs': list(action['criterion_refs']), 'goal_refs': list(action['goal_refs']),
                            'diagnosis_indices': sorted({topic_index[t] for t in action['topic_ids'] if t in topic_index})})
    return {'diagnoses': diagnoses, 'actions': out_actions, 'coverage': _coverage(base, diagnoses, out_actions)}


def _coverage(base, diagnoses, actions):
    """원본 이슈의 처분을 보존하되 슬롯 연결은 통합 후 항목 기준으로 다시 계산한다."""
    rows = []
    for row in base['coverage']:
        issue = row['issue_id']
        d = [i for i, slot in enumerate(diagnoses) if issue in slot['issue_refs']]
        a = [i for i, slot in enumerate(actions) if issue in slot['issue_refs']]
        disposition = row['disposition']
        if disposition == 'included' and not d:
            raise ValueError('consolidated_issue_lost:' + issue)
        rows.append({**{k: v for k, v in row.items() if k not in ('diagnosis_indices', 'action_indices')},
                     'diagnosis_indices': d if disposition == 'included' else [], 'action_indices': a if disposition == 'included' else []})
    return rows
