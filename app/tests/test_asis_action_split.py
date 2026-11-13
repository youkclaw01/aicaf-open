"""한도를 넘는 과제를 계획·승인 결과를 건드리지 않고 둘로 나누는 규칙의 계약 시험(모델 호출 없음).

운영 2026-10-18(프로젝트 25 A_ITEM_0001): 연결 방향 17개·진단 21개·사실 87개를 한 과제로 묶어, 이름·숫자·직접 인용 사실을 지키는 요약으로도 모델 입력 한도(27.2만 토큰)에
들어가지 못했다. 영석님 지시: 과제는 권장 20개, 최대 30개까지 할 수 있다.
같은 개정에서 이어가려면 9단계 계획(해시에 상한이 들어 있다)과 승인된 과제 18개를 바꾸면 안 되므로, 한도를 넘는 과제만 둘로 나누고 새 슬롯은 마지막 번호 뒤에 붙인다.
계약:
1. 나눔은 코드가 결정적으로 한다(모델 호출 없음). 같은 입력은 항상 같은 결과다.
2. 원 슬롯의 이슈·방향·진단·사실·기준·목표 참조가 두 슬롯에 빠짐없이 나뉜다(합집합이 원본과 같다). 이슈는 쪼개지 않는다(coverage 검증 보존).
3. 원 슬롯은 같은 ID 로 앞 절반을 유지한다. 새 슬롯은 기존 마지막 번호 + 1 이다. 다른 슬롯의 ID·순서·내용은 변하지 않는다.
4. 과제 수는 최대 30개를 넘지 못한다.
"""
from pathlib import Path
import copy
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_action_split as SP


def slot(identifier, issues, directions, diagnoses, facts=None):
    return {'id': identifier, 'scope': '과제 ' + identifier, 'reason': '이유', 'issue_refs': list(issues), 'direction_refs': list(directions),
            'diagnosis_refs': list(diagnoses), 'fact_refs': list(facts or ['F1', 'F2']), 'criterion_refs': ['C1'], 'goal_refs': ['G1']}


def big_plan():
    """과제 3개. A_ITEM_0001 은 이슈 4개·방향 4개·진단 4개를 묶은 큰 과제다."""
    return {'diagnoses': [{'id': 'J_ITEM_%04d' % (i + 1), 'issue_refs': ['I%d' % (i + 1)]} for i in range(4)],
            'actions': [slot('A_ITEM_0001', ['I1', 'I2', 'I3', 'I4'], ['D1', 'D2', 'D3', 'D4'], ['J_ITEM_0001', 'J_ITEM_0002', 'J_ITEM_0003', 'J_ITEM_0004']),
                        slot('A_ITEM_0002', ['I5'], ['D5'], ['J_ITEM_0001']),
                        slot('A_ITEM_0003', ['I6'], ['D6'], ['J_ITEM_0002'])]}


DIRECTION_AREA = {'D1': 'ai', 'D2': 'ai', 'D3': 'cost', 'D4': 'cost', 'D5': 'ai', 'D6': 'cost'}
# 운영 실측(프로젝트 25): 이슈는 한 방향에만 연결되지만 한 방향은 이슈를 여러 개 거느린다(최대 9개). 이슈는 쪼갤 수 없으므로 방향 단위로 이슈 묶음이 함께 움직인다.
ISSUES_OF_DIRECTION = {'D1': ['I1', 'I2'], 'D2': ['I3'], 'D3': ['I4'], 'D4': [], 'D5': ['I5'], 'D6': ['I6']}


def split(plan, identifier='A_ITEM_0001'):
    return SP.split_action_slot(plan, identifier, direction_area=DIRECTION_AREA, issues_of_direction=ISSUES_OF_DIRECTION)


def test_the_split_keeps_every_reference_exactly_once_across_the_two_slots():
    plan = big_plan()
    new_plan, record = split(plan)
    first, second = new_plan['actions'][0], new_plan['actions'][-1]
    original = plan['actions'][0]
    for key in ('issue_refs', 'direction_refs', 'diagnosis_refs'):
        assert sorted(first[key] + second[key]) == sorted(original[key]), key
        assert not set(first[key]) & set(second[key]) or key == 'diagnosis_refs', key + ' 는 두 슬롯에 겹치면 안 된다(진단은 둘 다 필요하면 겹칠 수 있다)'
    assert set(first['direction_refs']) and set(second['direction_refs']), '한쪽이 비면 안 된다'


def test_the_original_slot_keeps_its_id_and_the_new_slot_is_appended_after_the_last_number():
    plan = big_plan()
    new_plan, record = split(plan)
    assert [row['id'] for row in new_plan['actions']] == ['A_ITEM_0001', 'A_ITEM_0002', 'A_ITEM_0003', 'A_ITEM_0004']
    assert record['original_id'] == 'A_ITEM_0001' and record['new_id'] == 'A_ITEM_0004'
    for untouched in ('A_ITEM_0002', 'A_ITEM_0003'):
        assert next(r for r in new_plan['actions'] if r['id'] == untouched) == next(r for r in plan['actions'] if r['id'] == untouched)


def test_the_split_is_deterministic_and_never_modifies_its_input():
    plan = big_plan()
    before = json.dumps(plan, sort_keys=True)
    a = split(plan)
    b = split(copy.deepcopy(plan))
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert json.dumps(plan, sort_keys=True) == before


def test_directions_of_the_same_area_stay_together_when_the_split_allows_it():
    new_plan, record = split(big_plan())
    first, second = new_plan['actions'][0], new_plan['actions'][-1]
    areas_first = {DIRECTION_AREA[d] for d in first['direction_refs']}
    areas_second = {DIRECTION_AREA[d] for d in second['direction_refs']}
    assert not (areas_first & areas_second), '같은 영역의 방향은 한 과제에 함께 있어야 읽기 쉽다: %r %r' % (areas_first, areas_second)


def test_an_issue_is_never_split_across_two_slots():
    new_plan, record = split(big_plan())
    first, second = new_plan['actions'][0], new_plan['actions'][-1]
    assert not set(first['issue_refs']) & set(second['issue_refs'])
    for d in first['direction_refs']:
        assert set(ISSUES_OF_DIRECTION[d]) <= set(first['issue_refs'])
    for d in second['direction_refs']:
        assert set(ISSUES_OF_DIRECTION[d]) <= set(second['issue_refs'])


def test_a_direction_that_owns_several_issues_moves_with_all_of_them():
    new_plan, record = split(big_plan())
    side = new_plan['actions'][0] if 'D1' in new_plan['actions'][0]['direction_refs'] else new_plan['actions'][-1]
    assert {'I1', 'I2'} <= set(side['issue_refs']), '방향 D1 의 이슈 I1·I2 는 함께 움직여야 한다'


def test_a_direction_without_any_issue_still_travels_with_its_area():
    new_plan, record = split(big_plan())
    side = new_plan['actions'][0] if 'D4' in new_plan['actions'][0]['direction_refs'] else new_plan['actions'][-1]
    assert 'D3' in side['direction_refs'], '같은 영역(cost)의 방향 D3·D4 는 같은 과제에 있어야 한다'


def test_a_slot_with_a_single_direction_cannot_be_split():
    plan = big_plan()
    try:
        split(plan, 'A_ITEM_0002')
    except ValueError as exc:
        assert 'action_split_single_direction' in str(exc)
    else:
        raise AssertionError('방향이 하나뿐인 과제는 나눌 수 없다')


def test_the_action_count_never_exceeds_the_maximum():
    plan = big_plan()
    plan['actions'] += [slot('A_ITEM_%04d' % n, ['I%d' % (n + 10)], ['D6'], ['J_ITEM_0002']) for n in range(4, 31)]
    assert len(plan['actions']) == 30
    try:
        split(plan)
    except ValueError as exc:
        assert 'action_split_over_maximum' in str(exc)
    else:
        raise AssertionError('과제는 최대 30개다')


def test_the_limits_are_twenty_recommended_and_thirty_maximum():
    import asis_consolidate as C
    assert C.ACTION_RECOMMENDED == 20 and C.ACTION_LIMIT == 30


def test_an_issue_that_no_direction_owns_stays_in_the_original_slot_and_is_never_lost():
    """방향이 거느리지 않는 이슈도 계획 coverage 가 요구하므로 어느 한 과제에는 반드시 있어야 한다(없으면 이슈가 사라진다)."""
    plan = big_plan()
    plan['actions'][0]['issue_refs'] = ['I1', 'I2', 'I3', 'I4', 'I_ORPHAN']
    new_plan, record = split(plan)
    first, second = new_plan['actions'][0], new_plan['actions'][-1]
    assert sorted(first['issue_refs'] + second['issue_refs']) == sorted(plan['actions'][0]['issue_refs'])
    assert 'I_ORPHAN' in first['issue_refs'], '원 슬롯(앞 절반)에 남아야 한다'


def test_an_issue_owned_by_directions_on_both_sides_goes_to_exactly_one_slot():
    """운영 실측에서 이슈 241개 중 1개는 방향 두 개에 연결된다. 이슈는 쪼개지 않으므로 한 과제에만 있어야 한다."""
    plan = big_plan()
    shared = dict(ISSUES_OF_DIRECTION)
    shared['D1'] = ['I1', 'I2', 'I_SHARED']
    shared['D3'] = ['I4', 'I_SHARED']          # D1(ai)과 D3(cost)는 서로 다른 쪽으로 갈 영역이다
    plan['actions'][0]['issue_refs'] = ['I1', 'I2', 'I3', 'I4', 'I_SHARED']
    new_plan, record = SP.split_action_slot(plan, 'A_ITEM_0001', direction_area=DIRECTION_AREA, issues_of_direction=shared)
    first, second = new_plan['actions'][0], new_plan['actions'][-1]
    holders = [row['id'] for row in (first, second) if 'I_SHARED' in row['issue_refs']]
    assert len(holders) == 1, holders
    assert not set(first['issue_refs']) & set(second['issue_refs'])
    assert sorted(first['issue_refs'] + second['issue_refs']) == sorted(plan['actions'][0]['issue_refs'])
