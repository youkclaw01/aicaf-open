"""분류 원장이 합성 원천의 모든 데이터와 0·관측부재를 보존하는지 검증한다."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def test_cost_and_metric_views_preserve_basis_and_zero_without_double_counting():
    import asis_evidence_views as view
    data={'run':{'analysis_revision':'test'},'facts':[], 'measurements':[], 'context':{'source_documents':{
        'topology':{'resources':[], 'configurations':{}, 'relationships':[]},
        'evidence':{'costPeriod':{'fromUtc':'2026-01-01','toUtc':'2026-01-31'},'sections':{
          'costMonthlyTotals':{'records':[{'Cost':10.5,'Currency':'USD','costType':'ActualCost','month':'2026-01'}, {'Cost':10.5,'Currency':'USD','costType':'AmortizedCost','month':'2026-01'}]},
          'aiMetricsTokensDaily':{'records':[{'resourceId':'a','metric':'ProcessedPromptTokens','time':'2026-01-03','dimensions':{'modeldeploymentname':'m'},'total':0}, {'resourceId':'a','metric':'GeneratedTokens','time':'2026-01-03','dimensions':{'modeldeploymentname':'m'},'total':7}]},
          'other':{'records':[{'unknown_property':True}]}}}}}}
    result=view.enrich(data)
    assert result['context']['cost_summary'][0]['amount']==10.5
    assert len(result['context']['cost_summary'])==2
    assert result['context']['metric_summary'][0]['points']==1
    assert {r['metric']:r['total'] for r in result['context']['metric_summary']}=={'ProcessedPromptTokens':0,'GeneratedTokens':7}
    assert result['context']['source_documents']==data['context']['source_documents']
    assert any(x['pointer']=='/sections/other/records' for x in result['context']['dataset_register'])
    assert any(x['id'].startswith('F_D_') for x in result['facts'])
    assert len(view.prompt_view(result).get('context',{}).get('source_documents',{}))==0
