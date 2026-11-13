"""추가 수집 원본은 분석 권위로 승격하지 않고 출처 원장에 보존한다."""
from pathlib import Path
import sys,json
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import asis_model


def test_wara_source_is_preserved_without_importing_findings(tmp_path):
    (tmp_path/'topology.json').write_text(json.dumps({'resources':[], 'configurations':{}}),encoding='utf-8')
    raw={'impactedResources':[{'recommendation':'SYNTHETIC_NOT_A_CUSTOMER_FINDING'}],'resourceInventory':{'a':1}}
    (tmp_path/'WARA-File-2099-01-01.json').write_text(json.dumps(raw),encoding='utf-8')
    data=asis_model.normalize(tmp_path,{'id':999996,'customer':'합성','track':'existing','infra_case_id':'SYNTHETIC'},[],'test')
    assert 'WARA-File-2099-01-01.json' in data['context']['input_files']
    assert data['context']['source_documents']['WARA-File-2099-01-01.json']==raw
    assert not data['judgments']
    assert all('SYNTHETIC_NOT_A_CUSTOMER_FINDING' not in str(f['value']) for f in data['facts'])
