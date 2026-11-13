"""동결 이후 PPT 입력이 별도 분석란으로 제한되는지 검증한다."""
from pathlib import Path
import sys,json
import openpyxl,pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import asis_analysis_register as register
from asis_model import digest
from asis_artifacts import sha
from asis_excel_freeze import consume


def prepare(tmp_path):
    raw={'input':{'run':{'source_bundle_sha256':'a'*64},'facts':[{'id':'RAW_CANARY'}],
        'context':{'source_documents':{'evidence':{'secret_marker':'RAW_ONLY'}}}},
        'stages':[{'stage':'area','areas':[]},{'stage':'summary','message':'검토된 원고'}]}
    book=tmp_path/'ASIS-Backdata.xlsx';b=openpyxl.Workbook();b.save(book)
    receipt=register.write(book,raw)
    (tmp_path/'analysis-frozen.json').write_text(json.dumps(raw),encoding='utf-8')
    freeze={'verified':True,'xlsx_name':book.name,'xlsx_sha256':sha(book),'analysis_sha256':digest(raw),
            'analysis_register_sha256':receipt['sha256'],'ppt_input_policy':'analysis_sheets_only'}
    (tmp_path/'freeze-receipt.json').write_text(json.dumps(freeze),encoding='utf-8')
    return raw,book,freeze


def test_ppt_consumes_registered_analysis_not_raw_frozen_input(tmp_path):
    raw,book,receipt=prepare(tmp_path)
    actual=consume(raw,tmp_path)
    assert actual==register.read(book)
    assert actual['input']['facts']==[]
    assert 'source_documents' not in actual['input']['context']
    assert actual['ppt_input_policy']=='analysis_sheets_only'


def test_missing_register_is_not_replaced_with_raw_frozen_input(tmp_path):
    raw,book,receipt=prepare(tmp_path)
    b=openpyxl.load_workbook(book);del b[register.SHEET];b.save(book)
    receipt['xlsx_sha256']=sha(book)
    (tmp_path/'freeze-receipt.json').write_text(json.dumps(receipt),encoding='utf-8')
    with pytest.raises(ValueError,match='analysis_register_missing'):consume(raw,tmp_path)


def test_registered_original_does_not_accept_new_coherent_analysis(tmp_path):
    raw,book,receipt=prepare(tmp_path)
    changed=json.loads(json.dumps(raw));changed['stages'][1]['message']='다른 분석'
    register.write(book,changed);receipt['xlsx_sha256']=sha(book)
    (tmp_path/'freeze-receipt.json').write_text(json.dumps(receipt),encoding='utf-8')
    with pytest.raises(ValueError,match='analysis_register_binding'):consume(raw,tmp_path)
