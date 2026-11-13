"""종합 검토에 실제 인용한 대량 원문이 전달되는지 확인한다. 미인용 원장은 생략 정책을 유지한다."""
from pathlib import Path
from copy import deepcopy
import sys,asyncio,json
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_asis_parallel_synthesis import synthesis_fixture,DirectionRuntime
from asis_pipeline import Pipeline
import asis_synthesis


def test_whole_synthesis_review_inlines_all_cited_bulk_evidence(tmp_path):
    data,plan=synthesis_fixture(tmp_path)
    cited=deepcopy(data['facts'][0]);cited.update(id='F_T_review_bulk',value=[{'actual_rule':'AllowRdpFromHome','raw':'a'*51000}])
    omitted=deepcopy(cited);omitted.update(id='F_T_unrelated_bulk',value=[{'raw':'b'*51000}])
    data['facts'].extend([cited,omitted])
    for slot in plan['directions']:slot['fact_refs'].append(cited['id'])
    runtime=DirectionRuntime(plan);pipe=Pipeline(runtime,tmp_path/'run')
    asyncio.run(pipe.reviewed_synthesis(data))
    merged=next(c['input'] for c in runtime.calls if c['role']=='content_review' and c['input']['stage_data'].get('mode')=='synthesis' and not c['input']['stage_data'].get('direction_assignment'))
    facts={f['id']:f for f in merged['facts']}
    assert facts[cited['id']]['value']==cited['value'],'whole-review omitted the actual cited source'
    assert facts[omitted['id']]['value'].get('not_inlined') is True
    assert data['facts'][-2:]==[cited,omitted]
