"""종합 방향 2차 묶기: 영역 간 묶기를 통과하고도 남은 같은 판단 주제를 한 번 더 묶는다(보수 규칙: 묶음당 최대 4개, 같은 영역 포함).

운영 실측(2026-10-15): 통합 검토가 접근 시험·키 보호·공유 용량 같은 중복을 라운드마다 지적해 지적 수 6,3,6,4,3으로 정체하고 개정이 중단됐다.
실제 모델로 129개 후보를 묶으면 27개 묶음(63개 감소)이 나왔고, 멤버 5개 이상인 큰 묶음 5개는 서로 다른 단계의 확인 항목을 합칠 위험이 있어 허용 크기를 4로 제한한다.
"""
from pathlib import Path
import sys, json, asyncio
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
import asis_synthesis as S


def candidates(n=6):
    return [{'scope': '범위 %d' % i, 'reason': '이유 %d' % i, 'issue_refs': ['P_%d' % i], 'area_implication_refs': ['I_a'], 'fact_refs': ['F_%d' % i],
             'criterion_refs': ['C_%d' % i], 'goal_refs': []} for i in range(n)]


def test_a_group_of_two_to_four_candidates_is_accepted_even_inside_one_area():
    rows = candidates()
    assert S.validate_second_pass_groups(rows, {'groups': [{'members': [3, 1], 'reason': '같은 판단'}, {'members': [0, 2, 4, 5], 'reason': '같은 입력'}]}) == [[1, 3], [0, 2, 4, 5]]
    assert S.validate_second_pass_groups(rows, {'groups': []}) == []


@pytest.mark.parametrize('groups,error', [
    ([{'members': [0, 1, 2, 3, 4], 'reason': 'x'}], 'invalid_second_pass_group'),       # 5개 이상은 너무 큰 묶음(서로 다른 판단을 삼킬 위험)
    ([{'members': [0], 'reason': 'x'}], 'invalid_second_pass_group'),
    ([{'members': [0, 9], 'reason': 'x'}], 'invalid_second_pass_group'),
    ([{'members': [0, '1'], 'reason': 'x'}], 'invalid_second_pass_group'),
    ([{'members': [0, 0], 'reason': 'x'}], 'invalid_second_pass_group'),
    ([{'members': [0, 1], 'reason': ' '}], 'invalid_second_pass_group'),
    ([{'members': [0, 1], 'reason': 'a'}, {'members': [1, 2], 'reason': 'b'}], 'second_pass_group_member_reused'),
])
def test_oversized_tiny_unknown_duplicate_or_reused_groups_are_rejected(groups, error):
    with pytest.raises(ValueError, match=error):
        S.validate_second_pass_groups(candidates(), {'groups': groups})


def test_merging_preserves_every_reference_and_keeps_the_candidate_with_the_most_references():
    rows = candidates(4)
    rows[2]['fact_refs'] = ['F_2', 'F_x', 'F_y']
    merged, report = S.apply_cross_area_groups(rows, [[1, 2, 3]])
    assert len(merged) == 2
    kept = merged[1]
    assert kept['scope'] == '범위 2', 'the candidate with the most references supplies the wording'
    assert set(kept['fact_refs']) == {'F_1', 'F_2', 'F_x', 'F_y', 'F_3'} and set(kept['issue_refs']) == {'P_1', 'P_2', 'P_3'}


def test_the_instruction_states_the_size_limit_that_the_code_enforces():
    assert str(S.SECOND_PASS_MAX_MEMBERS) in S.SECOND_PASS_GROUPING_INSTRUCTION
    assert '합치면 사라지는 판단이 없어야 한다' in S.SECOND_PASS_GROUPING_INSTRUCTION
