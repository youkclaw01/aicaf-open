"""Excel이 먼저 동결되고 같은 바이트·분석만 PPT 소비자에 전달되는지 검증한다."""
from pathlib import Path
import sys,json
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_asis_connection import synthetic_input
import asis_analysis


def test_excel_freeze_rejects_any_post_validation_change(tmp_path):
    import asis_excel_freeze
    result=asis_analysis.analyze(synthetic_input(tmp_path))
    receipt=asis_excel_freeze.freeze(result,tmp_path/'freeze')
    assert receipt['verified']
    rebound=asis_excel_freeze.consume(result,tmp_path/'freeze')
    assert rebound['stages']==result['stages']
    assert rebound['input']['run']==result['input']['run']
    assert rebound['input']['facts']==[]
    assert 'source_documents' not in rebound['input']['context']
    assert rebound['ppt_input_policy']=='analysis_sheets_only'
    book=tmp_path/'freeze'/receipt['xlsx_name']
    book.write_bytes(book.read_bytes()+b'bad')
    import pytest
    with pytest.raises(ValueError):asis_excel_freeze.consume(result,tmp_path/'freeze')
