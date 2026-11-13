"""Excel 선행 원장과 부분 수집 의미를 실제 정규화 경로에서 검증한다."""
from pathlib import Path
import json
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_model


def test_unfinished_pages_never_become_complete_customer_facts(tmp_path):
    topology={'resources': [], 'configurations': {}, 'verification': {'resourceCount': {'complete':True,'collected':0,'uniqueIds':0}}}
    evidence={'subscriptionId':'00000000-0000-0000-0000-000000000001','sections':{
        'limited':{'status':'collected','records':[{'id':'x'}], 'recordCount':1, 'errorCount':0, 'scope_complete':False, 'continuationRemaining':True},
        'zeroMetrics':{'status':'not_present','records':[], 'recordCount':0, 'errorCount':0,'api':'Monitor metrics P1D','purpose':'cost:A3'},
        'realEmpty':{'status':'not_present','records':[], 'recordCount':0, 'errorCount':0,'api':'ARM list','scope_complete':True},
    }}
    (tmp_path/'topology.json').write_text(json.dumps(topology),encoding='utf-8')
    (tmp_path/'aicaf-evidence.json').write_text(json.dumps(evidence),encoding='utf-8')
    data=asis_model.normalize(tmp_path, {'id':999998,'customer':'합성 시험','track':'existing','infra_case_id':'SYNTHETIC'}, [], 'test')
    facts={x['id']:x for x in data['facts']}
    assert facts['F_E_limited']['scope_complete'] is False
    assert facts['F_E_zeroMetrics']['collection_status']!='not_present'
    assert facts['F_E_realEmpty']['collection_status']=='not_present'
    assert facts['F_E_realEmpty']['scope_complete'] is True
    assert data['context']['dataset_index'], '수집 인덱스가 빠졌다'
    assert data['context']['source_documents']['evidence']==evidence
