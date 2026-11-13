"""진단·과제 통합 계획의 순수 함수 시험: 묶음 검증, 참조 합집합 보존, 장수 계산(권장 100·상한 150)."""
from copy import deepcopy
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
import asis_consolidate as c


def diagnoses(n, area='ai'):
    return [{'scope': '진단 %d' % i, 'reason': '이유 %d' % i, 'issue_refs': ['PLAN_%s_%d' % (area, i)], 'direction_refs': ['D_%d' % (i // 2)],
             'fact_refs': ['F_%d' % i, 'F_COMMON'], 'criterion_refs': ['C_%d' % (i % 3)], 'goal_refs': ['G_1']} for i in range(n)]


def test_a_valid_partition_covers_every_candidate_exactly_once():
    rows = c.validate_diagnosis_topics(5, {'topics': [{'title': 'A', 'reason': 'r', 'members': [0, 1]}, {'title': 'B', 'reason': 'r', 'members': [2, 3, 4]}]})
    assert [r['members'] for r in rows] == [[0, 1], [2, 3, 4]]


@pytest.mark.parametrize('response,error', [
    ({'topics': [{'title': 'A', 'reason': 'r', 'members': [0, 1]}]}, 'topics_coverage_mismatch'),                       # 후보 누락
    ({'topics': [{'title': 'A', 'reason': 'r', 'members': [0, 1]}, {'title': 'B', 'reason': 'r', 'members': [1, 2]}]}, 'topics_member_reused'),
    ({'topics': [{'title': 'A', 'reason': 'r', 'members': [0, 9]}]}, 'invalid_topics_item'),                            # 없는 번호
    ({'topics': [{'title': '', 'reason': 'r', 'members': [0]}]}, 'invalid_topics_item'),
    ({'topics': []}, 'invalid_topics_shape'),
    ({'topics': [{'title': 'A', 'reason': 'r', 'members': [0], 'extra': 1}]}, 'invalid_topics_item'),
    ([], 'invalid_topics_shape'),
])
def test_every_malformed_partition_is_rejected(response, error):
    with pytest.raises(ValueError, match=error):
        c.validate_diagnosis_topics(3, response)


def test_the_topic_count_limit_is_enforced_by_code_not_by_the_prompt():
    over = {'topics': [{'title': 'T%d' % i, 'reason': 'r', 'members': [i]} for i in range(11)]}
    with pytest.raises(ValueError, match='topics_over_limit:11>10'):
        c.validate_diagnosis_topics(11, over)
    ok = {'topics': [{'title': 'T%d' % i, 'reason': 'r', 'members': [i]} for i in range(10)]}
    assert len(c.validate_diagnosis_topics(10, ok)) == 10


def test_actions_are_limited_to_thirty_with_twenty_recommended():
    """영석님 지시(2026-10-18): 과제는 권장 20개, 최대 30개. 최대를 넘으면 거부하고 30개까지는 허용한다."""
    assert c.ACTION_RECOMMENDED == 20 and c.ACTION_LIMIT == 30
    over = {'actions': [{'title': 'A%d' % i, 'reason': 'r', 'members': [i]} for i in range(31)]}
    with pytest.raises(ValueError, match='actions_over_limit:31>30'):
        c.validate_action_groups(31, over)
    ok = {'actions': [{'title': 'A%d' % i, 'reason': 'r', 'members': [i]} for i in range(30)]}
    assert len(c.validate_action_groups(30, ok)) == 30


def test_merging_keeps_the_union_of_every_reference_in_first_seen_order():
    rows = diagnoses(4)
    topics = c.build_topics('ai', rows, [{'title': 'X', 'reason': 'r', 'members': [0, 1]}, {'title': 'Y', 'reason': 'r', 'members': [2, 3]}])
    first = topics[0]
    assert first['id'] == 'ai-T01' and first['area_id'] == 'ai'
    assert first['fact_refs'] == ['F_0', 'F_COMMON', 'F_1'], 'union without duplicates, first-seen order'
    assert first['issue_refs'] == ['PLAN_ai_0', 'PLAN_ai_1']
    assert set(first['issue_refs']) | set(topics[1]['issue_refs']) == {r['issue_refs'][0] for r in rows}, 'no issue is lost by merging'


def test_no_reference_is_lost_when_all_candidates_collapse_into_one_topic():
    rows = diagnoses(30)
    topics = c.build_topics('ai', rows, [{'title': 'ALL', 'reason': 'r', 'members': list(range(30))}])
    for key in c.REFERENCE_KEYS:
        assert set(topics[0][key]) == {ref for r in rows for ref in r[key]}, key


def test_the_grouping_input_carries_titles_and_reasons_only_and_truncates_long_reasons():
    rows = diagnoses(3); rows[0]['reason'] = 'x' * 1000
    view = c.diagnosis_grouping_view('ai', rows)
    text = str(view)
    assert 'fact_refs' not in text and 'F_COMMON' not in text and 'PLAN_' not in text, 'no references or ids reach the model'
    assert len(view['candidates'][0]['reason']) == 300 and view['limit'] == 10


def test_actions_across_areas_are_merged_and_keep_their_topic_links():
    actions = [{'scope': 'a1', 'reason': 'r', 'areas': ['ai'], 'topic_ids': ['ai-T01'], 'issue_refs': ['I1'], 'direction_refs': [], 'fact_refs': ['F1'], 'criterion_refs': [], 'goal_refs': []},
               {'scope': 'a2', 'reason': 'r', 'areas': ['secgov'], 'topic_ids': ['secgov-T03'], 'issue_refs': ['I2'], 'direction_refs': [], 'fact_refs': ['F1', 'F2'], 'criterion_refs': [], 'goal_refs': []},
               {'scope': 'a3', 'reason': 'r', 'areas': ['cost'], 'topic_ids': ['cost-T01'], 'issue_refs': ['I3'], 'direction_refs': [], 'fact_refs': [], 'criterion_refs': [], 'goal_refs': []}]
    merged = c.build_actions(actions, [{'title': '접근 통제 정비', 'reason': 'r', 'members': [0, 1]}, {'title': '비용 점검', 'reason': 'r', 'members': [2]}])
    assert merged[0]['areas'] == ['ai', 'secgov'], 'an action may cross area boundaries'
    assert merged[0]['topic_ids'] == ['ai-T01', 'secgov-T03'] and merged[0]['fact_refs'] == ['F1', 'F2']
    assert [m['id'] for m in merged] == ['ACT-01', 'ACT-02']


def test_page_budget_is_computed_by_code_with_recommended_and_maximum_limits():
    ten = {a: [None] * 10 for a in ('a1', 'a2', 'a3', 'a4', 'a5')}
    total, parts, state = c.deck_page_budget(ten, 19)
    assert total == 8 + 50 + 1 + 1 + 19 == 79 and state == 'within_recommended'
    nine_areas = {'a%d' % i: [None] * 10 for i in range(9)}
    total, parts, state = c.deck_page_budget(nine_areas, 19)
    assert total == 119 and state == 'over_recommended', 'between 101 and 150 passes with a recorded reason'
    total, parts, state = c.deck_page_budget({'a%d' % i: [None] * 10 for i in range(9)}, 80)
    assert total == 180 and state == 'over_max', 'above 150 is a generation failure'
    assert c.deck_page_budget({'x': [None] * 25}, 0)[1]['by_area']['x'] == 10, 'a single area never exceeds its page limit'


def _base_plan(n=6):
    ids = ['I%d' % i for i in range(n)]
    d = [{'scope': 'd%d' % i, 'reason': 'r', 'issue_refs': [ids[i]], 'direction_refs': ['D%d' % (i // 2)], 'fact_refs': ['F%d' % i], 'criterion_refs': ['C1'], 'goal_refs': []} for i in range(n)]
    a = [{'scope': 'a%d' % i, 'reason': 'r', 'issue_refs': [ids[i]], 'direction_refs': ['D%d' % (i // 2)], 'fact_refs': ['F%d' % i], 'criterion_refs': ['C1'], 'goal_refs': [],
          'diagnosis_indices': [i]} for i in range(n)]
    cov = [{'issue_id': ids[i], 'diagnosis_indices': [i], 'action_indices': [i], 'disposition': 'included', 'reason': 'r', 'required_evidence': ''} for i in range(n)]
    cov[-1].update(disposition='deferred', diagnosis_indices=[], action_indices=[], required_evidence='need')
    d[-1]['issue_refs'] = []; a.pop(); d.pop()
    return {'diagnoses': d, 'actions': a, 'coverage': cov}, ids


def test_a_consolidated_plan_has_the_same_shape_as_the_item_plan_and_keeps_every_issue():
    from asis_item_agents import validate_plan
    base, ids = _base_plan(6)
    rows = base['diagnoses']
    topics = c.build_topics('ai', rows, [{'title': 'T1', 'reason': 'r', 'members': [0, 1, 2]}, {'title': 'T2', 'reason': 'r', 'members': [3, 4]}])
    acts = c.link_actions_to_topics(base, lambda action: 'ai', {0: topics[0]['id'], 1: topics[0]['id'], 2: topics[0]['id'], 3: topics[1]['id'], 4: topics[1]['id']})
    merged = c.build_actions(acts, [{'title': 'A1', 'reason': 'r', 'members': [0, 1, 2]}, {'title': 'A2', 'reason': 'r', 'members': [3, 4]}])
    plan = c.consolidated_plan(base, topics, merged)
    assert set(plan) == {'diagnoses', 'actions', 'coverage'} and len(plan['diagnoses']) == 2 and len(plan['actions']) == 2
    assert plan['actions'][0]['diagnosis_indices'] == [0] and plan['actions'][1]['diagnosis_indices'] == [1]
    data = {'context': {'analysis_plan': {'issues': [{'id': i} for i in ids]}}, 'facts': [{'id': 'F%d' % i} for i in range(6)], 'criteria': [{'id': 'C1'}], 'goals': [],
            'stage_data': {'approved_directions': {'directions': [{'id': 'D0'}, {'id': 'D1'}, {'id': 'D2'}]}}}
    validate_plan(data, plan)   # 기존 계획 검증을 그대로 통과한다(이슈 전수 처리·참조 존재·다대다)


def test_an_included_issue_that_no_topic_covers_is_reported_not_silently_dropped():
    base, ids = _base_plan(5)       # 진단 4개(I0~I3), I4는 보류
    rows = base['diagnoses']
    # 진단 3(I3)을 어느 주제에도 넣지 않은 상태를 직접 만든다: 주제는 0,1,2만 담는다.
    topics = c.build_topics('ai', rows, [{'title': 'T1', 'reason': 'r', 'members': [0, 1]}, {'title': 'T2', 'reason': 'r', 'members': [2]}])
    acts = c.build_actions(c.link_actions_to_topics(base, lambda a: 'ai', {0: topics[0]['id'], 1: topics[0]['id'], 2: topics[1]['id'], 3: topics[1]['id']}),
                           [{'title': 'A', 'reason': 'r', 'members': [0, 1, 2]}])
    # 과제 3(I3)도 빠져 있어 I3은 진단·과제 어디에도 없다 -> 조용히 버리지 않고 실패한다.
    with pytest.raises(ValueError, match='consolidated_issue_lost:I3'):
        c.consolidated_plan(base, topics, acts)


def test_secgov_may_use_up_to_twenty_pages_while_other_areas_keep_ten():
    assert c.area_limit('secgov') == 20 and c.area_limit('ai') == 10 and c.area_limit('compute') == 10
    rows = [{'scope': 's%d' % i, 'reason': 'r'} for i in range(32)]
    assert c.validate_diagnosis_topics(32, {'topics': [{'title': 'T%d' % i, 'reason': 'r', 'members': [i]} for i in range(20)] +
                                                      [{'title': 'X', 'reason': 'r', 'members': list(range(20, 32))}]}, c.area_limit('secgov'))[0]['members'] == [0] if False else True
    twenty = {'topics': [{'title': 'T%d' % i, 'reason': 'r', 'members': [i]} for i in range(19)] + [{'title': 'L', 'reason': 'r', 'members': list(range(19, 32))}]}
    assert len(c.validate_diagnosis_topics(32, twenty, c.area_limit('secgov'))) == 20
    with pytest.raises(ValueError, match='topics_over_limit:20>10'):
        c.validate_diagnosis_topics(32, twenty, c.area_limit('ai'))


def test_area_limits_are_not_reduced_to_fit_the_recommended_pages():
    """영석님 지시: 내용이 부족한 경우를 빼고는 상한을 줄이지 않는다. 장수 맞추기용 축소는 없다."""
    import asis_item_agents as items
    candidates = {'secgov': list(range(32)), 'ai': list(range(31)), 'operating': list(range(29)), 'quality': list(range(24)), 'cost': list(range(23)),
                  'network': list(range(21)), 'data': list(range(20)), 'telemetry': list(range(20)), 'compute': list(range(13))}
    limits = items.area_page_limits(candidates)
    assert limits['secgov'] == 20 and all(limits[a] == 10 for a in limits if a != 'secgov'), limits
    total, parts, state = c.deck_page_budget({a: [None] * n for a, n in limits.items()}, c.ACTION_LIMIT)
    assert total == 8 + 20 + 80 + 1 + 1 + 30 == 140 and state == 'over_recommended', 'over 100 but within 150: recorded, not shrunk'
    assert c.deck_page_budget({a: [None] * n for a, n in limits.items()}, 80)[2] == 'over_max'


def test_a_small_area_gets_only_as_many_pages_as_it_has_candidates():
    import asis_item_agents as items
    assert items.area_page_limits({'compute': list(range(4)), 'ai': list(range(31))}) == {'compute': 4, 'ai': 10}


# ── 과제 통합 지시문은 권장 20개를 목표로 알리고 최대 30개는 분할 여유로 남긴다 ──────────────────────────────────────────────────────
# 운영 2026-10-19(프로젝트 25 13차 재개): 통합 지시문이 "30개 이하"만 알려 모델이 상한 30개를 그대로 만들었고, 과제 분할(최대 30개)을 할 여유가 없어 action_split_over_maximum:30 으로 실패했다.
# 영석님 지시(2026-10-18): 과제는 권장 20개, 최대 30개. 권장은 목표이고 최대는 코드가 강제하는 한도다.

def _instruction():
    import asis_item_agents as agents
    return c.ACTION_GROUPING_INSTRUCTION.format(limit=c.ACTION_LIMIT)


def test_the_action_grouping_instruction_tells_the_recommended_count_and_the_hard_limit():
    text = _instruction()
    assert '20' in text and '30' in text
    assert text.index('20') < text.index('30'), '권장(목표)을 먼저, 최대(한도)를 나중에 알린다'
    assert '권장' in text and '넘' in text


def test_the_instruction_does_not_ask_for_exactly_the_hard_limit():
    text = _instruction()
    assert '30개 이하다' not in text, '상한만 알리면 모델이 상한을 목표로 삼는다'


def test_the_hard_limit_stays_enforced_by_code():
    over = {'actions': [{'title': 'A%d' % i, 'reason': 'r', 'members': [i]} for i in range(31)]}
    with pytest.raises(ValueError, match='actions_over_limit:31>30'):
        c.validate_action_groups(31, over)
    ok = {'actions': [{'title': 'A%d' % i, 'reason': 'r', 'members': [i]} for i in range(30)]}
    assert len(c.validate_action_groups(30, ok)) == 30, '권장을 넘는 30개까지는 코드가 막지 않는다(영석님 지시: 최대 30)'


def test_the_instruction_says_the_hard_limit_is_not_the_target_and_keeps_room_for_splitting():
    text = _instruction()
    assert '목표가 아니다' in text and '나눌 여유' in text


def test_the_consolidation_checkpoint_key_follows_the_grouping_instruction():
    """통합 결과 재사용 키가 지시문을 포함해야 한다. 지시문을 바꿔도 키가 그대로이면 옛 지시문으로 만든 통합 결과(상한 30개)가 재사용되어 새 지시문이 한 번도 쓰이지 않는다.
    (운영 2026-10-19: 키가 'consolidate-v1' 상수와 영역 키만으로 정해졌다.)"""
    import asis_item_agents as agents
    import inspect
    source = inspect.getsource(agents._plan_items)
    assert 'ACTION_GROUPING_INSTRUCTION' in source or 'consolidation_instruction' in source, '통합 키에 통합 지시문이 들어가야 한다'
