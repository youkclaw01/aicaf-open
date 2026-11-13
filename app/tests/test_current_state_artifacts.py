"""선행 현황분석의 Excel 동결·승인 템플릿·보고서 역할을 검증한다."""
from pathlib import Path
import importlib
import json
from copy import deepcopy

from openpyxl import load_workbook
from pptx import Presentation
import pytest


def result(tmp_path):
    from asis_model import normalize
    import asis_analysis
    source = tmp_path/'Source'; source.mkdir(exist_ok=True)
    (source/'topology.json').write_text(json.dumps({'resources': [
        {'id': '/subscriptions/unit/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/unitdata',
         'name': 'unitdata', 'type': 'Microsoft.Storage/storageAccounts', 'location': 'koreacentral'}],
        'configurations': {}, 'verification': {'resourceCount': {'complete': True, 'collected': 1, 'uniqueIds': 1}}}), encoding='utf-8')
    data = normalize(source, {'id': 97, 'infra_case_id': 'unit-97', 'customer': '구조 검증용 합성 자료', 'track': 'new'}, [], 'unit')
    value = asis_analysis.analyze(data)
    value['stages'] = value['stages'][:1]
    value['schema'] = 'assessment-current-state-analysis/1'
    value['input']['context']['report_kind'] = 'current_state'
    return value


def test_freeze_roundtrip_preserves_all_current_analysis(tmp_path):
    m = importlib.import_module('assessment_flow_artifacts')
    value = result(tmp_path)
    path = tmp_path/'Current.xlsx'
    receipt = m.freeze_current_state(value, path)
    recovered = m.consume_current_state(path, receipt)
    assert recovered == value
    book = load_workbook(path, read_only=True)
    assert {'개요', '수집현황', '현황분석', '근거원장', '기준원장', '_frozen_analysis'} <= set(book.sheetnames)
    assert book['_frozen_analysis'].sheet_state != 'visible'
    book.close()


def test_native_current_state_has_no_final_diagnosis_or_action_section(tmp_path):
    m = importlib.import_module('assessment_flow_artifacts')
    value = result(tmp_path)
    path = tmp_path/'Current.pptx'
    pages = m.presentation_current_state(value, path)
    deck = Presentation(path)
    text = '\n'.join(shape.text for slide in deck.slides for shape in slide.shapes if shape.has_text_frame)
    assert 'AS-IS 현황분석 보고서' in text
    assert '구조 검증용 합성 자료' in text
    assert '현황분석 종합 및 시사점 및 개선과제 보고서' not in text
    assert '진단·개선과제' not in text
    assert len(deck.slides) == pages
    for area in value['stages'][0]['areas']:
        assert area['headline'] in text
    assert m.verify_current_state(value, path, _workbook(m, value, tmp_path))['passed'] is True


def _workbook(m, value, tmp_path):
    path = tmp_path/'Current.xlsx';m.freeze_current_state(value, path);return path


def test_frozen_analysis_tampering_is_rejected(tmp_path):
    m = importlib.import_module('assessment_flow_artifacts')
    value = result(tmp_path)
    path = tmp_path/'Current.xlsx'
    receipt = m.freeze_current_state(value, path)
    book = load_workbook(path);book['_frozen_analysis'].cell(2, 1).value = '{}';book.save(path);book.close()
    with pytest.raises(ValueError, match='frozen'):
        m.consume_current_state(path, receipt)


def test_first_report_rejects_integrated_candidate(tmp_path):
    m = importlib.import_module('assessment_flow_artifacts')
    value = result(tmp_path)
    value['stages'].append({'stage': 'actions', 'actions': []})
    with pytest.raises(ValueError, match='current_state'):
        m.freeze_current_state(value, tmp_path/'Wrong.xlsx')


def test_pair_check_detects_changed_visible_excel_analysis_not_only_hidden_json(tmp_path):
    m = importlib.import_module('assessment_flow_artifacts');value = result(tmp_path)
    xlsx = tmp_path/'Current.xlsx';pptx = tmp_path/'Current.pptx'
    m.freeze_current_state(value, xlsx);m.presentation_current_state(value, pptx)
    book = load_workbook(xlsx);book['현황분석'].cell(2, 4).value = '원장 본문 변경';book.save(xlsx);book.close()
    with pytest.raises(ValueError, match='current_state_visible_excel'):
        m.verify_current_state(value, pptx, xlsx)


def test_pair_check_detects_missing_ppt_body_even_when_notes_keep_all_ids(tmp_path):
    m = importlib.import_module('assessment_flow_artifacts');value = result(tmp_path)
    xlsx = tmp_path/'Current.xlsx';pptx = tmp_path/'Current.pptx'
    m.freeze_current_state(value, xlsx);m.presentation_current_state(value, pptx)
    needle = value['stages'][0]['areas'][0]['observations'][0]['text']
    compact = lambda text: ''.join(text.split())
    deck = Presentation(pptx);found = False
    for slide in deck.slides:
        for shape in slide.shapes:
            if shape.has_text_frame and compact(shape.text) == compact(needle):
                shape.text = 'UNIT_MISSING_APPROVED_BODY';found = True;break
        if found:break
    assert found, 'tamper fixture must modify the actual visible observation'
    deck.save(pptx)
    with pytest.raises(ValueError, match='current_state_visible_ppt'):
        m.verify_current_state(value, pptx, xlsx)
