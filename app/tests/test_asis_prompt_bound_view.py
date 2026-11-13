"""모델용 전달 뷰는 중복 원문을 제외하되 canonical 의미와 별도임을 검증한다."""
from pathlib import Path
import asyncio, json, sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from asis_pipeline import Pipeline
from asis_evidence_views import prompt_view
from asis_model import encoded
from copy import deepcopy


def test_prompt_view_removes_duplicate_criteria_metadata_but_keeps_canonical_evidence(tmp_path):
    """원문과 수집 상태는 정본에 남고 동일 원문의 중복 전달만 제거된다."""
    data=synthetic_input(tmp_path)
    data['context']['criterion_assessments']=[{
        'id':'CJ_UNIT','criterion_id':'C_UNIT','status':'unknown','applicability_confirmed':False,
        'area_refs':['network'],'fact_refs':[data['facts'][0]['id']],
        'application_condition':'합성 적용 조건','conclusion':'합성 판정은 미확인이다.',
        'quote':'SYNTHETIC_DUPLICATE_QUOTE_'*1000,
        'source_url':'https://example.invalid/criteria','source_snapshot_sha256':'a'*64,
        'evidence_states':[{'duplicated':'SYNTHETIC_DUPLICATE_STATE_'*1000}]}]
    before=deepcopy(data)
    view=prompt_view(data)
    assert data==before
    row=view['context']['criterion_assessments'][0]
    assert row['id']=='CJ_UNIT' and row['status']=='unknown'
    assert row['application_condition']=='합성 적용 조건'
    assert row['fact_refs']==before['context']['criterion_assessments'][0]['fact_refs']
    assert 'quote' not in row and 'evidence_states' not in row
    assert len(encoded(view))<len(encoded(data))
from test_asis_connection import SyntheticRuntime,synthetic_input


def test_all_model_input_serialization_uses_compact_bound_view(tmp_path):
    data=synthetic_input(tmp_path)
    data['context']['source_documents']['duplicate_sentinel']='SYNTHETIC_RAW_NOT_TO_INLINE'
    runtime=SyntheticRuntime()
    result=asyncio.run(Pipeline(runtime,tmp_path/'run').analyze(data))
    assert result['input']['context']['source_documents']['duplicate_sentinel']=='SYNTHETIC_RAW_NOT_TO_INLINE'
    # 통합 계획 호출은 후보 번호·영역·범위·이유만 담은 소형 입력이라 context가 없다. 원문 source_documents가 어떤 모델 입력에도 인라인되지 않는다는 것이 이 시험의 요점이다.
    assert all('source_documents' not in c['input'].get('context', {}) for c in runtime.calls)
    assert all('SYNTHETIC_RAW_NOT_TO_INLINE' not in json.dumps(c['input'], ensure_ascii=False) for c in runtime.calls)
