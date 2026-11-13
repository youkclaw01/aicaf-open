"""현황 원장 원문 보존 및 수식 비승격 회귀. 모든 입력은 명시적인 합성 단위 시험이다."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest
from openpyxl import load_workbook

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

import asis_analysis
import asis_artifacts
import asis_model


@pytest.mark.parametrize('value', ['=SUM(1,2)', '+raw', '-raw', '@raw', "'=kept", '한글 원문', ''])
def test_literal_preserves_original_characters(value):
    assert asis_artifacts.literal(value) == value


def synthetic_result(tmp_path, transcript):
    """실제 고객 또는 실제 Azure 조회로 오인할 수 없는 최소 입력을 구성한다."""
    source = tmp_path / 'synthetic-source'
    source.mkdir()
    topology = {
        'resources': [],
        'configurations': {},
        'verification': {'resourceCount': {'complete': True, 'collected': 0, 'uniqueIds': 0}},
        'collection': {'subscriptions': [], 'completedAtUtc': None},
    }
    (source / 'topology.json').write_text(json.dumps(topology), encoding='utf-8')
    assessment = {
        'id': 999001,
        'customer': 'SYNTHETIC UNIT TEST - NOT A CUSTOMER',
        'title': 'Literal preservation only',
        'infra_case_id': 'synthetic-backdata-unit',
        'track': 'new',
    }
    answers = [{'qid': 'UNIT-RAW', 'transcript': transcript, 'summary': '', 'score': None}]
    data = asis_model.normalize(source, assessment, answers, 'synthetic-unit-r1',
                                classification='synthetic_unit_test_not_customer')
    return asis_analysis.analyze(data)


@pytest.mark.parametrize('value', ['=SUM(1,2)', '+literal', '-literal', '@literal', "'=original"])
def test_actual_recalculated_workbook_preserves_text_and_does_not_promote_formula(tmp_path, value):
    result = synthetic_result(tmp_path, value)
    output = tmp_path / 'backdata.xlsx'
    asis_artifacts.workbook(result, output)
    book = load_workbook(output, data_only=False)
    rows = [row for row in book['사실원장'].iter_rows(min_row=7)
            if row[0].value == 'F_IV_UNIT-RAW_transcript']
    assert len(rows) == 1
    assert rows[0][5].value == value
    assert rows[0][5].data_type != 'f'
    assert book['실행 정보']['B7'].data_type == 'f'
    assert book['실행 정보']['B8'].data_type == 'f'
    cached = load_workbook(output, data_only=True)
    assert cached['실행 정보']['B7'].value == len(result['input']['facts'])
    assert cached['실행 정보']['B8'].value == len(result['stages'][2]['actions'])


def test_split_raw_text_is_complete_after_real_recalculation(tmp_path):
    value = '=' + '가' * 27999 + '@' + '나' * 8000
    result = synthetic_result(tmp_path, value)
    output = tmp_path / 'backdata.xlsx'
    asis_artifacts.workbook(result, output)
    book = load_workbook(output, data_only=False)
    parts = [row for row in book['원문 상세'].iter_rows(min_row=7)
             if row[0].value == 'F_IV_UNIT-RAW_transcript']
    assert parts
    assert ''.join(str(row[3].value or '') for row in sorted(parts, key=lambda row: int(str(row[1].value)))) == value
    assert all(row[3].data_type != 'f' for row in parts)
