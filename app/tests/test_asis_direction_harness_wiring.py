"""방향 작성 하네스 운영 연결 계약 시험.

- 방향 작성(stage 2, 방향 슬롯 입력)만 direction_author 역할(luna high)로 호출하고, 그 지시문에 작업표와 가드가 붙는다.
- 작성 후보는 검토 전에 하네스 hard 검사를 거치고, 결함이 있으면 검토를 부르지 않고 같은 작성자에게 되돌린다(기존 결정적 결함 경로).
- 방향이 아닌 작성(영역·진단·과제)은 author 역할(sol xhigh)과 지시문이 그대로다.
- 검토는 content_review(sol xhigh)이며 전체 입력을 본다.
모델은 호출하지 않는다. 호출 인자를 기록하는 합성 런타임을 쓴다.
"""
from copy import deepcopy
from pathlib import Path
import asyncio
import json
import sys

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
import asis_direction_harness as H
import asis_pipeline


def scoped_input():
    slot = {'id': 'D_P0001', 'scope': '채점 응답시간과 한도 변경 전후 비교 조건', 'reason': '지연 경험을 실측 성능으로 전환하지 않는다. PTU 도입을 선결 조건으로 정하지 않는다.',
            'fact_refs': ['F_IV_1'], 'criterion_refs': ['C1'], 'goal_refs': ['G1'], 'area_implication_refs': ['I_x'], 'issue_refs': []}
    return {'facts': [{'id': 'F_IV_1', 'value': '[고객] 미리 확보하는 방식은 안 씁니다.', 'scope_complete': True, 'collection_status': 'collected'}],
            'criteria': [{'id': 'C1', 'quote': 'Pay for what you intend to use.', 'modality': 'should'}], 'goals': [{'id': 'G1'}],
            'stage_data': {'mode': 'synthesis', 'direction_assignment': {'item_id': 'D_P0001', 'slot': slot}}}


def candidate(text='고객은 미리 확보하는 방식은 안 쓴다고 진술했다.', extra=None):
    d = {'id': 'D_P0001', 'title': '제목', 'text': text, 'customer_impact': '영향', 'change_direction': '채점 응답시간과 한도 변경 전후 비교 조건을 확인한다.',
         'fact_refs': ['F_IV_1'], 'criterion_refs': ['C1'], 'goal_refs': ['G1'], 'area_implication_refs': ['I_x']}
    d.update(extra or {})
    return {'directions': [d], 'unresolved': [], 'edges': []}


def test_work_table_lists_slot_items_prohibitions_and_customer_statements():
    table = H.work_table_for(scoped_input())
    assert '채점 응답시간' in table and '한도 변경 전후 비교 조건' in table
    assert '선결 조건으로 정하지 않는다' in table, '슬롯 reason 의 금지 사항이 작업표 B 에 있어야 한다'
    assert '미리 확보하는 방식은 안 씁니다' in table and '부정/모름' in table, '고객 진술 원문과 극성 표시가 작업표 C 에 있어야 한다'


def test_hard_defects_empty_for_clean_candidate_and_non_empty_for_known_failures():
    assert H.hard_defects(scoped_input(), candidate()) == []
    missing = candidate(extra={'fact_refs': []})
    assert any('H2' in d['why'] for d in H.hard_defects(scoped_input(), missing))
    foreign = candidate(extra={'fact_refs': ['F_IV_1', 'F_NOPE']})
    assert any('H3' in d['why'] for d in H.hard_defects(scoped_input(), foreign))
    han = candidate(text='고객은 同일 업무라고 설명했다.')
    assert any('H5' in d['why'] for d in H.hard_defects(scoped_input(), han))
    added = candidate(text='고객은 배포 지역 선정 근거를 문서화했다고 설명했다.')
    assert any('H6' in d['why'] for d in H.hard_defects(scoped_input(), added))


def test_hard_defects_use_the_operational_defect_shape():
    for defect in H.hard_defects(scoped_input(), candidate(extra={'fact_refs': []})):
        assert set(defect) == {'where', 'quote', 'why', 'fix', 'evidence_refs'} and defect['why'].startswith('harness:')


def test_soft_signals_are_not_defects():
    soft = H.soft_signals(scoped_input(), candidate(text='고객은 미리 확보하는 방식을 쓴다고 했다.'))
    assert isinstance(soft, list)
    assert H.hard_defects(scoped_input(), candidate(text='고객은 미리 확보하는 방식을 쓴다고 했다.')) == [], 'soft 의심은 hard 결함이 아니다(검토자가 판단한다)'


def test_pipeline_detects_direction_stage_only_for_direction_slot_input():
    assert asis_pipeline.is_direction_input(2, scoped_input()) is True
    other = deepcopy(scoped_input())
    other['stage_data'] = {'mode': 'area'}
    assert asis_pipeline.is_direction_input(2, other) is False
    assert asis_pipeline.is_direction_input(3, scoped_input()) is False, '진단·과제 단계는 방향 작성이 아니다'


def test_author_role_and_instructions_follow_the_input_kind():
    role, extra = asis_pipeline.author_plan(2, scoped_input())
    assert role == 'direction_author' and '작업표' in extra and '추가 엄수 규칙' in extra
    area = deepcopy(scoped_input()); area['stage_data'] = {'mode': 'area'}
    assert asis_pipeline.author_plan(2, area) == ('author', '')
    assert asis_pipeline.author_plan(3, scoped_input()) == ('author', '')
