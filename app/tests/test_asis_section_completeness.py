"""행수 일치를 전체 수집 완전성으로 승격하지 않는 계약."""
from pathlib import Path
import sys,json
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import asis_model


def test_legacy_section_row_agreement_is_not_scope_completeness(tmp_path):
    (tmp_path/'topology.json').write_text(json.dumps({'resources':[],'verification':{'resourceCount':{'complete':True,'collected':0,'uniqueIds':0}},'configurations':{}}),encoding='utf-8')
    (tmp_path/'aicaf-evidence.json').write_text(json.dumps({'sections':{'bounded':{'status':'collected','recordCount':1,'errorCount':0,'records':[{'id':'a'}]}}}),encoding='utf-8')
    data=asis_model.normalize(tmp_path,{'id':999997,'customer':'합성 시험','track':'existing','infra_case_id':'SYNTHETIC'},[],'test')
    row=next(x for x in data['facts'] if x['id']=='F_E_bounded')
    assert row['collection_status']=='collected'
    assert row['scope_complete'] is False
    assert data['context']['collection_states']['F_E_bounded']['rows_reconciled'] is True
