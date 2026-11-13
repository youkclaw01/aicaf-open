"""F06 실제 판정과 고객용 기준 설명의 정합성을 신규·기구축에서 확인한다."""
import ast
import hashlib
import io
import json
import os
import re
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from openpyxl import load_workbook
from pptx import Presentation

APP = Path(os.environ.get('F06_SOURCE_DIR', str(Path(__file__).resolve().parents[1])))
UNIT = Path(__file__).resolve().parent / 'fixtures/interview'
sys.path.insert(0, str(UNIT))
sys.path.insert(0, str(APP))
import interview_report
import questions
import scoring
from f06_fixtures import CASES, PPT_CASES, make_case, rounding_probe

# 점수 알고리즘의 독립 판정 결과와 대조할 고객용 설명 계약이다. 운영 판정 함수가 아니다.
EXPECTED_GATES = {
    'Realizing': [('종합', 85), ('D', 80), ('전 영역', 70)],
    'Scaling': [('종합', 70), ('B', 70), ('C', 70)],
    'Implementing': [('종합', 50), ('B', 50), ('C', 40)],
    'Planning': [('종합', 30), ('A', 50)],
    'Exploring': [],
}
NOTE_CONTRACT = (
    '전체 문항 응답 전 판정 보류',
    '미확인 ÷ (응답 완료 − 해당없음)',
    '소수 셋째 자리 반올림 후 20% 초과 시 보류',
    '해당없음: 응답 완료 포함, 평균·비율 분모 제외',
    '미확인·해당없음·미응답은 평균에서 제외',
    'A·B·C·D 중 미산정 영역이 있으면 판정 보류',
    '필수 문항 0~1점: 최대 Planning',
    'Planning 조건 미충족 시 Exploring',
    '0점: 평균·필수 조건에 포함',
)


def criteria_text(result):
    prs = Presentation(str(interview_report.TEMPLATE))
    interview_report.criteria(prs, result, 15)
    return '\n'.join(shape.text for slide in prs.slides for shape in slide.shapes
                     if shape.has_text_frame)


def declared_gates(output):
    rows = output.splitlines()
    declared = {}
    for stage in EXPECTED_GATES:
        row = next((line for line in rows if line.startswith(stage + ' ·')), None)
        assert row is not None, ('stage_row_missing', stage, output)
        declared[stage] = [(key, int(value)) for key, value in
                           re.findall(r'(종합|전 영역|[ABCD]) ≥ (\d+)', row)]
    assert declared == EXPECTED_GATES, declared
    assert '상위부터 모든 조건을 만족하는 첫 단계' in output
    assert '위 조건을 충족하지 못한 경우' in output
    return declared


def report_decision(output, result):
    """실물 문구에서 읽은 조건만 적용한다. 누락 조건은 암묵적으로 보충하지 않는다."""
    for phrase in NOTE_CONTRACT:
        assert phrase in output, ('decision_note_missing', phrase)
    gates = declared_gates(output)
    if result['answered'] < result['total'] or result['unknown_ratio'] > 0.2:
        return None
    if any(result['areas'].get(area) is None for area in 'ABCD'):
        return None
    values = {**result['areas'], '종합': result['overall'], '전 영역': min(result['areas'].values())}
    for stage, conditions in gates.items():
        if result['must_low'] and stage in ('Realizing', 'Scaling', 'Implementing'):
            continue
        if all(values[key] >= limit for key, limit in conditions):
            return stage
    raise AssertionError('reported_stage_unreachable')


@pytest.mark.parametrize('track', ['new', 'existing'])
def test_realizing_description_requires_d80_at_high_overall(track):
    qs = questions.questions_for(track)
    answers = {q['id']: {'score': '3' if q['area'] == 'D' else '4'} for q in qs}
    result = scoring.compute(qs, answers)
    assert result['areas'] == {'A': 100.0, 'B': 100.0, 'C': 100.0, 'D': 75.0}
    assert result['overall'] == 93.8
    assert result['stage'] == 'Scaling'
    output = criteria_text(result)
    realizing_line = next(line for line in output.splitlines() if line.startswith('Realizing'))
    assert 'D ≥ 80' in realizing_line, realizing_line


@pytest.mark.parametrize('track', ['new', 'existing'])
def test_stage_description_lists_all_area_gates_and_not_exclusive_bands(track):
    qs = questions.questions_for(track)
    answers, _ = make_case(qs, 'normal')
    output = criteria_text(scoring.compute(qs, answers))
    assert declared_gates(output) == EXPECTED_GATES
    assert not re.search(r'(0~29|30~49|50~69|70~84)', output)


@pytest.mark.parametrize('track', ['new', 'existing'])
def test_hold_and_mandatory_conditions_are_explicit(track):
    output = criteria_text(scoring.compute(questions.questions_for(track), {}))
    for phrase in NOTE_CONTRACT:
        assert phrase in output, phrase
    assert '각 평균은 소수 첫째 자리 반올림' in output
    assert '50점 기준선은 영역·주제 비교에 사용' in output
    assert 'Microsoft 공식 점수나 인증이 아닙니다.' in output


@pytest.mark.parametrize('track', ['new', 'existing'])
@pytest.mark.parametrize('case', list(CASES))
def test_criteria_decision_matches_compute_on_boundaries(track, case):
    qs = questions.questions_for(track)
    answers, expected = make_case(qs, case)
    before = deepcopy(answers)
    result = scoring.compute(qs, answers)
    assert result['stage'] == expected, (case, result)
    if isinstance(CASES[case][0], dict):
        assert result['areas'] == CASES[case][0]
        assert result['must_low'] == 0
    assert report_decision(criteria_text(result), result) == result['stage']
    assert answers == before


@pytest.mark.parametrize('track', ['new', 'existing'])
@pytest.mark.parametrize('unknown,ratio,stage', [(501, 0.2, 'Realizing'), (502, 0.201, None)])
def test_unknown_ratio_gate_uses_rounded_fraction(track, unknown, ratio, stage):
    qs, answers = rounding_probe(questions.questions_for(track), unknown)
    result = scoring.compute(qs, answers)
    assert result['total'] == result['answered'] == 2500
    assert result['unknown_ratio'] == ratio
    assert result['stage'] == stage
    assert report_decision(criteria_text(result), result) == stage


@pytest.mark.parametrize('track', ['new', 'existing'])
@pytest.mark.parametrize('case', ['d75', 'd80', 'must_one', 'unanswered', 'unknown21', 'all_na'])
def test_current_result_reason_is_not_truncated(track, case):
    qs = questions.questions_for(track)
    answers, _ = make_case(qs, case)
    result = scoring.compute(qs, answers)
    output = criteria_text(result)
    assert result['reason'] in output, ('result_reason_truncated', result['reason'], output)
    assert f"이번 결과: {result['stage'] or '판정 보류'}" in output


@pytest.mark.parametrize('track', ['new', 'existing'])
@pytest.mark.parametrize('case', list(PPT_CASES))
def test_full_native_report_preserves_answers_scores_and_layout(track, case):
    qs = questions.questions_for(track)
    answers, _ = make_case(qs, case)
    result = scoring.compute(qs, answers)
    original = deepcopy((qs, answers, result))
    payload = interview_report.build({'customer': 'F06 합성 경계 시험', 'title': '실제 고객 결과 아님'},
                                     qs, answers, result, questions.TRACK_META[track])
    prs = Presentation(io.BytesIO(payload))
    assert interview_report.verify(prs) == []
    output = '\n'.join(shape.text for shape in prs.slides[-1].shapes if shape.has_text_frame)
    assert report_decision(output, result) == result['stage']
    assert (qs, answers, result) == original
    assert scoring.compute(qs, answers) == result


def test_scoring_questions_and_direct_contract_hashes_are_unchanged():
    freeze = json.loads((UNIT / 'CONTRACT_FREEZE.json').read_text(encoding='utf-8'))
    protected = {'scoring.py', 'questions.py', 'interview_question_details.py', 'interview_template.pptx'}
    for row in freeze['files']:
        path = Path(row['path'])
        if path.name in protected:
            source = APP / path.name
            assert hashlib.sha256(source.read_bytes()).hexdigest() == row['sha256'], str(source)
    before = json.loads((UNIT / 'baseline/question_contract_current.json').read_text(encoding='utf-8'))
    assert {track: questions.questions_for(track) for track in ('new', 'existing')} == before['questions']
    assert questions.TRACK_META == before['track_meta']


def test_production_change_is_limited_to_criteria():
    def without_criteria(path):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        tree.body = [node for node in tree.body if not (isinstance(node, ast.FunctionDef) and node.name == 'criteria')]
        return ast.dump(tree, include_attributes=False)
    assert without_criteria(APP / 'interview_report.py') == without_criteria(UNIT / 'baseline/interview_report.py')


@pytest.mark.parametrize('track', ['new', 'existing'])
def test_current_xlsx_keeps_id_question_confirmation_and_score_cells(track):
    relative = 'questionnaires/' + f'questionnaire-{track}.xlsx'
    path = UNIT / relative
    freeze = json.loads((UNIT / 'CONTRACT_FREEZE.json').read_text(encoding='utf-8'))
    expected_hash = next(row['sha256'] for row in freeze['files'] if row['path'] == relative)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_hash
    wb = load_workbook(path)
    ws = wb['질문지']
    qs = questions.questions_for(track)
    assert ws.max_row == 4 + len(qs)
    assert ws['I4'].value == '점수' and ws['J4'].value == '메모'
    for row, q in enumerate(qs, 5):
        values = [q['id'], f"{q['area']} {q.get('display_area_name', q['area_name'])}",
                  f"{q['topic']} {q.get('display_topic_name', q['topic_name'])}",
                  q['question'], q['point'], q['top'], '필수' if q['must'] else None, q['evidence']]
        assert [ws.cell(row, col).value for col in range(1, 9)] == values, q['id']
        assert ws.cell(row, 9).value is None and ws.cell(row, 10).value is None
    assert not [(sheet.title, cell.coordinate) for sheet in wb for row in sheet for cell in row
                if cell.data_type == 'f']
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_hash
