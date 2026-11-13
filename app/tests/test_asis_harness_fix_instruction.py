"""방향 작성 하네스가 결함 종류에 맞는 수정 지시를 작성자에게 주는지 확인하는 계약 시험(모델 호출 없음).

2026-10-18 운영(프로젝트 25, 7단계 방향 D_P0081): 하네스 H4 incomplete_scope_assertion
('수집 범위가 완전하지 않은 사실을 확정 서술') 결함이 3라운드 연속 되돌려졌고 작성자는 고치지 못해 방향이 failed 가 됐다.
원인은 H4 가 name_grounding·number_grounding·incomplete_scope_assertion 모두에 같은 지시
'입력에 없는 이름·숫자를 지우거나 입력에 있는 표기로 바꾼다' 를 붙이기 때문이다. 부분 수집 사실의 확정 서술은 이름·숫자 문제가 아니다.
작성자는 존재하지 않는 이름·숫자를 찾으려다 같은 문장을 다시 냈고(라운드 2 에는 한자까지 섞었다) 입력은 라운드마다 커졌다.

계약(통과 기준은 바꾸지 않는다. 바꾸는 것은 작성자에게 주는 수정 방법뿐이다):
1. 검사별 지시문: name_grounding·number_grounding 은 기존 지시를 유지하고, incomplete_scope_assertion 은 부분 수집 한계를 쓰라는 지시를 준다.
2. incomplete_scope_assertion 지시에는 결함 문장(quote)이 부분 수집 사실 목록과 함께 이어져 작성자가 어떤 문장을 고칠지 알 수 있다.
3. 어떤 경우에도 hard 결함의 개수·판정(통과 기준)은 지시문 변경으로 달라지지 않는다.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_direction_harness as H


class FakeGrader:
    """지정한 검사만 실패하는 채점기 대역."""

    def __init__(self, failing):
        self.failing = failing

    def grade(self, cand, gi, reference=None):
        names = ('name_grounding', 'number_grounding', 'incomplete_scope_assertion', 'language')
        return {'checks': [{'check': n, 'passed': n not in self.failing, 'skipped': False,
                            'reason': ('근거: ' + n) if n in self.failing else ''} for n in names]}


def scoped_and_candidate():
    slot = {'id': 'D_P0001', 'area_implication_refs': ['I_a'], 'fact_refs': ['F_1'], 'criterion_refs': ['C_1'], 'goal_refs': [], 'issue_refs': []}
    scoped = {'stage_data': {'direction_assignment': {'item_id': 'D_P0001', 'slot': slot}}, 'facts': [{'id': 'F_1', 'value': {'a': 1}, 'scope_complete': False}],
              'criteria': [{'id': 'C_1'}], 'goals': [], 'context': {}}
    candidate = {'stage': 'synthesis', 'directions': [{'id': 'D_P0001', 'text': '배포는 운영 중이다.', 'change_direction': ['x'], 'fact_refs': ['F_1'],
                                                      'criterion_refs': ['C_1'], 'area_implication_refs': ['I_a']}], 'unresolved': []}
    return scoped, candidate


def fixes(failing):
    scoped, candidate = scoped_and_candidate()
    defects = H.hard_defects(scoped, candidate, FakeGrader(failing))
    return [(d['why'].split(' ', 1)[1], d['fix']) for d in defects if d['why'].startswith('harness:H4')]


def test_name_and_number_grounding_keep_the_removal_instruction():
    got = dict(fixes({'name_grounding', 'number_grounding'}))
    assert '입력에 없는 이름·숫자' in got['name_grounding 실패']
    assert '입력에 없는 이름·숫자' in got['number_grounding 실패']


def test_incomplete_scope_assertion_tells_the_author_to_add_the_collection_limit():
    got = dict(fixes({'incomplete_scope_assertion'}))
    fix = got['incomplete_scope_assertion 실패']
    assert '이름·숫자를 지우' not in fix, '이름·숫자 지시를 부분 수집 결함에 붙이면 작성자가 엉뚱한 곳을 고친다'
    assert '수집' in fix and ('한계' in fix or '검증' in fix or '한정' in fix), fix
    assert '문장' in fix, '고칠 대상이 문장임을 알려야 한다'


def test_the_pass_fail_decision_does_not_change_with_the_instruction_text():
    for failing in ({'incomplete_scope_assertion'}, {'name_grounding'}, {'number_grounding'}, {'name_grounding', 'incomplete_scope_assertion'}):
        assert len(fixes(failing)) == len(failing)
    assert fixes(set()) == []


def test_hard_defects_expose_the_specific_instruction_to_the_pipeline():
    scoped, candidate = scoped_and_candidate()
    defects = H.hard_defects(scoped, candidate, FakeGrader({'incomplete_scope_assertion'}))
    rows = [d for d in defects if 'incomplete_scope_assertion' in d['why']]
    assert len(rows) == 1
    assert '이름·숫자를 지우' not in rows[0]['fix'] and '수집' in rows[0]['fix']
