"""통합 추적표가 Excel 상세 시트에 원본(통합 전 진단·과제)을 ID로 연결하는지, 어긋나면 실패하는지 확인한다."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from copy import deepcopy
from openpyxl import load_workbook
import asis_analysis
import asis_artifacts
from test_asis_configuration_delivery import configuration_case


def result_with_trace(tmp_path, mutate=None):
    result, _ = configuration_case(tmp_path)
    diagnoses, actions = result['stages'][2]['diagnoses'], result['stages'][2]['actions']
    trace = {'diagnoses': [{'topic_id': 'T%d' % n, 'area_id': 'network', 'title': '통합 주제 %d' % n,
                            'origin': [{'scope': '원본 진단 %d-%d' % (n, k), 'reason': '이유', 'issue_refs': ['P_%d' % k]} for k in range(1, 4)]}
                           for n, _ in enumerate(diagnoses)],
             'actions': [{'action_id': 'ACT-%d' % n, 'title': '통합 과제 %d' % n,
                          'origin': [{'scope': '원본 과제 %d-%d' % (n, k), 'reason': '이유', 'issue_refs': ['P_%d' % k]} for k in range(1, 3)]}
                         for n, _ in enumerate(actions)]}
    if mutate:
        mutate(trace)
    result['input']['context'].setdefault('action_item_plan', {})['consolidation_trace'] = trace
    return result


def test_the_workbook_lists_every_pre_consolidation_item_under_its_final_id(tmp_path):
    result = result_with_trace(tmp_path)
    path = tmp_path / 'book.xlsx'
    asis_artifacts.workbook(result, path)
    book = load_workbook(path, data_only=True)
    diagnoses = [r['id'] for r in result['stages'][2]['diagnoses']]
    rows = [r for r in book['통합 진단 원본'].iter_rows(min_row=7, values_only=True) if r[0]]
    assert sorted({r[0] for r in rows}) == sorted(diagnoses)
    assert len(rows) == 3 * len(diagnoses), 'every merged source row is kept'
    assert {r[3] for r in rows} >= {'원본 진단 0-1', '원본 진단 0-2', '원본 진단 0-3'}
    actions = [r['id'] for r in result['stages'][2]['actions']]
    arows = [r for r in book['통합 과제 원본'].iter_rows(min_row=7, values_only=True) if r[0]]
    assert sorted({r[0] for r in arows}) == sorted(actions) and len(arows) == 2 * len(actions)


def test_a_trace_that_does_not_match_the_final_items_fails_the_pair_verification(tmp_path):
    """verify_pair는 PPT와 Excel을 함께 대조한다. 여기서는 Excel의 통합 추적표 검사만 확인하므로 실제 PPT를 만들어 같은 입력으로 호출한다."""
    result = result_with_trace(tmp_path)
    ppt = tmp_path / 'deck.pptx'; xlsx = tmp_path / 'book.xlsx'
    asis_artifacts.workbook(result, xlsx)
    asis_artifacts.presentation(__import__('visual_spec_stub').with_visual_specs(result), ppt)
    asis_artifacts.verify_pair(result, ppt, xlsx)           # 정상 추적표는 통과한다
    broken = deepcopy(result)
    broken['input']['context']['action_item_plan']['consolidation_trace']['diagnoses'].pop()
    with pytest.raises(ValueError, match='consolidation_trace_count_mismatch'):
        asis_artifacts.verify_pair(broken, ppt, xlsx)
