"""정규화 단계에서 파생 집계를 생성하여 분석 원장의 참조 범위를 유지한다."""
from pathlib import Path
import sys,json
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import asis_model,asis_evidence_views


def test_normalized_input_contains_derived_facts_and_interview_order_is_preserved(tmp_path):
    (tmp_path/'topology.json').write_text(json.dumps({'resources':[{'type':'microsoft.compute/virtualmachines'}],'configurations':{}}),encoding='utf-8')
    (tmp_path/'aicaf-evidence.json').write_text(json.dumps({'sections':{'costMonthlyTotals':{'records':[{'Cost':0,'Currency':'USD','month':'2026-01','costType':'ActualCost'}]}}}),encoding='utf-8')
    data=asis_model.normalize(tmp_path,{'id':999995,'customer':'합성','track':'new','infra_case_id':'SYNTHETIC'},[{'qid':'Q1','transcript':'합성 원문','summary':'','score':None}],'test')
    assert any(x['id']=='F_D_cost_monthly' for x in data['facts'])
    assert data['facts'][-3]['id']=='F_IV_Q1_transcript'
    enriched=asis_evidence_views.enrich(data)
    assert enriched['facts']==data['facts']
    assert enriched['measurements']==data['measurements']
