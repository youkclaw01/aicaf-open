"""한도를 넘는 과제 슬롯을 계획·승인 결과를 건드리지 않고 둘로 나눈다(코드가 결정적으로 한다, 모델 호출 없음).

운영 2026-10-18(프로젝트 25 A_ITEM_0001): 연결 방향 17개·이슈 37개·진단 21개·사실 87개를 한 과제로 묶어, 이름·숫자·직접 인용 사실을 지키는 요약으로도
모델 입력 한도에 들어가지 못했다. 영석님 지시: 과제는 권장 20개, 최대 30개까지 할 수 있다.

운영 실측: 이슈는 한 방향에만 연결되지만(241개 중 1개만 예외) 방향 하나가 이슈를 여러 개 거느린다(최대 9개). 이슈를 쪼갤 수 없으므로(계획 검증 coverage 가 이슈를 정확히
한 번씩 과제와 연결한다) 방향과 그 방향의 이슈 묶음이 함께 움직인다.

원칙:
- 같은 영역의 방향은 한 과제에 함께 둔다(읽기 쉽고 두 과제가 같은 영역을 중복 서술하지 않는다). 영역이 하나뿐이면 방향 개수로 나눈다.
- 원 슬롯은 같은 ID 로 앞 절반을 유지하고, 새 슬롯은 기존 마지막 번호 + 1 로 뒤에 붙인다. 다른 슬롯의 ID·순서·내용은 변하지 않는다.
- 진단 참조는 이슈로 연결한다. 어느 쪽 이슈에도 연결되지 않는 진단은 원 슬롯에 남기고, 한쪽에 진단이 없으면 원 진단을 공유한다.
"""
import copy
import re

MAXIMUM = 30


def _partition(directions, direction_area):
    """영역 단위 묶음을 방향 수 내림차순·영역 이름 오름차순으로 한쪽씩 번갈아 담는다(결정적). 영역이 하나면 방향 개수로 반분한다."""
    groups = {}
    for ref in directions:
        groups.setdefault(direction_area.get(ref, '?'), []).append(ref)
    if len(groups) == 1:
        half = (len(directions) + 1) // 2
        return list(directions[:half]), list(directions[half:])
    first, second = [], []
    for area in sorted(groups, key=lambda name: (-len(groups[name]), name)):
        (first if len(first) <= len(second) else second).extend(groups[area])
    keep_first, keep_second = set(first), set(second)
    return [ref for ref in directions if ref in keep_first], [ref for ref in directions if ref in keep_second]


def split_action_slot(plan, identifier, *, direction_area, issues_of_direction):
    """(새 계획, 나눔 기록). plan 은 변경하지 않는다. 반환 계획은 actions 끝에 새 슬롯이 추가된다.

    direction_area: 방향 ID -> 영역 키. issues_of_direction: 방향 ID -> 그 방향이 거느린 이슈 ID 목록."""
    actions = plan['actions']
    if len(actions) >= MAXIMUM:
        raise ValueError('action_split_over_maximum:%d' % len(actions))
    index = next((i for i, row in enumerate(actions) if row['id'] == identifier), None)
    if index is None:
        raise ValueError('action_split_unknown_slot:' + identifier)
    original = actions[index]
    directions = list(original['direction_refs'])
    if len(directions) < 2:
        raise ValueError('action_split_single_direction:' + identifier)
    first_dirs, second_dirs = _partition(directions, direction_area)
    if not first_dirs or not second_dirs:
        raise ValueError('action_split_empty_side:' + identifier)
    owned = {issue for ref in directions for issue in issues_of_direction.get(ref, [])}
    first_issues = {issue for ref in first_dirs for issue in issues_of_direction.get(ref, [])}
    second_issues = {issue for ref in second_dirs for issue in issues_of_direction.get(ref, [])} - first_issues
    # 어느 방향에도 속하지 않은 이슈(방향 연결이 없다)는 원 슬롯(앞 절반)에 남긴다.
    first_issues |= {issue for issue in original['issue_refs'] if issue not in owned}
    first_issue_refs = [issue for issue in original['issue_refs'] if issue in first_issues]
    second_issue_refs = [issue for issue in original['issue_refs'] if issue in second_issues]
    if not second_issue_refs:
        raise ValueError('action_split_empty_side:' + identifier)
    diag_issues = {row['id']: set(row.get('issue_refs', [])) for row in plan.get('diagnoses', [])}
    keep_first, keep_second = set(first_issue_refs), set(second_issue_refs)
    first_diag = [ref for ref in original['diagnosis_refs'] if diag_issues.get(ref, set()) & keep_first]
    second_diag = [ref for ref in original['diagnosis_refs'] if diag_issues.get(ref, set()) & keep_second]
    unlinked = [ref for ref in original['diagnosis_refs'] if ref not in first_diag and ref not in second_diag]
    first_diag = [ref for ref in original['diagnosis_refs'] if ref in set(first_diag) | set(unlinked)]
    if not first_diag:
        first_diag = list(second_diag)
    if not second_diag:
        second_diag = list(first_diag)         # 연결 정보가 없으면 두 슬롯이 같은 진단을 근거로 한다(진단은 공유할 수 있다)
    numbers = [int(m.group(1)) for row in actions for m in [re.search(r'(\d+)$', row['id'])] if m]
    new_id = 'A_ITEM_' + str(max(numbers) + 1).zfill(4)
    first = copy.deepcopy(original)
    first.update(issue_refs=first_issue_refs, direction_refs=first_dirs, diagnosis_refs=first_diag, scope=original['scope'] + ' (1/2)')
    second = copy.deepcopy(original)
    second.update(id=new_id, issue_refs=second_issue_refs, direction_refs=second_dirs, diagnosis_refs=second_diag, scope=original['scope'] + ' (2/2)')
    new_plan = copy.deepcopy(plan)
    new_plan['actions'][index] = first
    new_plan['actions'].append(second)
    record = {'schema': 'asis-action-split/1', 'original_id': identifier, 'new_id': new_id, 'reason': 'model_input_over_limit_after_level_two_summary',
              'first': {'issue_refs': first_issue_refs, 'direction_refs': first_dirs, 'diagnosis_refs': first_diag},
              'second': {'issue_refs': second_issue_refs, 'direction_refs': second_dirs, 'diagnosis_refs': second_diag},
              'action_count_before': len(actions), 'action_count_after': len(new_plan['actions'])}
    from asis_model import digest
    record.update(schema='asis-action-split/2', plan_before_sha256=digest(plan), plan_after_sha256=digest(new_plan))
    return new_plan, record
