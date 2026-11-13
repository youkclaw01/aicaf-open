"""별도 Excel 분석 원고의 무손실 저장·재판독을 검증한다."""
import json,sys
from pathlib import Path
import openpyxl,pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

def sample():
    return {'input':{'run':{'source_bundle_sha256':'a'*64},'facts':[],'context':{'source_documents':{'evidence':{'canary':'RAW_ONLY'}}}},'stages':[{'stage':'area','areas':[]},{'stage':'synthesis','directions':[]},{'stage':'actions','diagnoses':[],'actions':[]},{'stage':'summary','message':'검토된 내용'}],'unresolved':[],'quality_status':'content_review_passed'}

def test_analysis_register_roundtrips_only_registered_content(tmp_path):
    import asis_analysis_register as r
    p=tmp_path/'report.xlsx';b=openpyxl.Workbook();b.active.title='전체현황';b.active['A1']='기초 원장 보존';b.save(p)
    c=sample();c['stages'][3]['message']='=1+2'+('검토한 본문'*8000)
    receipt=r.write(p,c)
    actual=r.read(p)
    assert actual['stages']==c['stages']
    assert actual['input']['context'].get('source_documents') in (None,{})
    assert actual['input']['facts']==[]
    assert actual['ppt_input_policy']=='analysis_sheets_only'
    b=openpyxl.load_workbook(p)
    assert b['전체현황']['A1'].value=='기초 원장 보존'
    assert all(cell.data_type!='f' for row in b[r.SHEET] for cell in row)
    assert receipt['source_bundle_sha256']=='a'*64

def test_register_tampering_and_missing_analysis_fail_closed(tmp_path):
    import asis_analysis_register as r
    p=tmp_path/'report.xlsx';b=openpyxl.Workbook();b.save(p)
    with pytest.raises(ValueError,match='analysis_register_missing'):r.read(p)
    r.write(p,sample());b=openpyxl.load_workbook(p);b[r.SHEET]['E3']='changed';b.save(p)
    with pytest.raises(ValueError):r.read(p)
