"""실제 원장 기반 네이티브 PPT/XLSX 생성·재계산 시험. 현재 운영 고객 자료라는 주장을 하지 않는다."""
from pathlib import Path
import importlib.util
import json
import sys
from openpyxl import load_workbook
from pptx import Presentation

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from private_archive_source import SOURCE,PRIVATE_ARCHIVE_REQUIRED
pytestmark=PRIVATE_ARCHIVE_REQUIRED


def test_native_pair_has_one_run_and_actual_cached_counts(tmp_path):
    import asis_model, asis_analysis
    assessment = json.loads((SOURCE/'interview_E.json').read_text(encoding='utf-8'))['assessment']
    result = asis_analysis.analyze(asis_model.normalize(SOURCE, assessment, [], 'test-r1', classification='archived_test_snapshot'))
    assert importlib.util.find_spec('asis_artifacts') is not None, 'native pair adapter missing'
    import asis_artifacts
    manifest = asis_artifacts.build(__import__('visual_spec_stub').with_visual_specs(result), tmp_path)
    assert manifest['run'] == result['input']['run']
    assert manifest['quality_status'] == 'not_independently_reviewed'
    assert set(f['role'] for f in manifest['artifacts']) >= {'asis_report_pptx', 'asis_backdata_xlsx', 'asis_render_pdf'}
    ppt = next(f for f in manifest['artifacts'] if f['role'] == 'asis_report_pptx')
    xlsx = next(f for f in manifest['artifacts'] if f['role'] == 'asis_backdata_xlsx')
    deck = Presentation(tmp_path/ppt['name'])
    assert len(deck.slides) >= len(result['stages'][0]['areas'])
    assert all(len(s.shapes) > 0 for s in deck.slides)
    book = load_workbook(tmp_path/xlsx['name'], data_only=True)
    assert book['실행 정보']['B7'].value == len(result['input']['facts'])
    assert book['실행 정보']['B8'].value == len(result['stages'][2]['actions'])
    assert book['실행 정보']['B3'].value == result['input']['run']['case_id']
