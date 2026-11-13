"""기초 workbook 전송값과 원장 전체가 모델 입력에 중복 삽입되지 않는지 확인한다."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from asis_evidence_views import prompt_view
from asis_analysis_register import project


def sample():
    return {'run':{'source_bundle_sha256':'actual-fixture'},'facts':[], 'context':{
        'collection_basis_workbook':{'base64':'RAW_WORKBOOK_CANARY'},
        'collection_basis':{'schema':'ai-assessment-basis/v1','inventory':[{'record':{'raw':'RAW_INVENTORY_CANARY'}}],
            'criterionMatches':[{'criterion_id':'C1','assessment_status':'not_evaluated','target_id':'r1','unit':'resource'}],
            'source_artifacts':{'topology.json':{'sha256':'fixture-hash'}}}}}


def test_model_view_excludes_workbook_and_inventory_but_keeps_matching():
    raw=sample();view=prompt_view(raw);context=view['context']
    assert 'collection_basis_workbook' not in context
    assert 'collection_basis' not in context
    assert context['collection_basis_matches'][0]['assessment_status']=='not_evaluated'
    assert context['collection_basis_matches'][0]['target_id']=='r1'
    assert raw['context']['collection_basis_workbook']['base64']=='RAW_WORKBOOK_CANARY'


def test_report_projection_never_embeds_collector_workbook():
    result=project({'input':sample(),'stages':[]})
    assert 'collection_basis_workbook' not in result['input']['context']
