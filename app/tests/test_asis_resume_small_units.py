"""보존 후보의 동기 재검증이 같은 루프의 새 SDK 준비 제한시간을 소진하지 않는다."""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import time

import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import asis_pipeline
import asis_item_agents
from asis_model import encoded
from asis_pipeline import Pipeline
from asis_resume import prepare_failed_resume
from test_asis_item_agents import ItemRuntime,item_fixture


def test_preserved_revalidation_finishes_before_new_sdk_setup(tmp_path):
    """실제 항목 스케줄러·보존 계약을 쓰고 외부 SDK의 짧은 준비 I/O만 합성한다."""
    target='A_ITEM_0001'
    class InitialFailure(ItemRuntime):
        async def ask(self,role,message,**kwargs):
            value=json.loads(message.rsplit('\n입력:\n',1)[-1])
            if role in ('author', 'direction_author') and value['stage_data'].get('item_assignment',{}).get('item_id')==target:
                raise RuntimeError('synthetic_session_setup_failure')
            return await super().ask(role,message,**kwargs)
    data=item_fixture();folder=tmp_path/'run'
    with pytest.raises(RuntimeError,match='action_item_generation_failed:A_ITEM_0001'):
        asyncio.run(Pipeline(InitialFailure(),folder).reviewed_actions(data))
    sources={name:(Path(asis_pipeline.__file__).parent/name).read_bytes()
             for name in ('asis_pipeline.py','asis_item_agents.py')}
    (folder/'snapshot.json').write_bytes(encoded(data))
    (folder/'job.json').write_bytes(encoded({'run':data['run'],'revision_id':data['run']['analysis_revision'],
        'status':'failed','retry_count':1}))
    approval=prepare_failed_resume(folder,approval_id='bounded-cache-before-sdk',
        expected_job_sha256=hashlib.sha256((folder/'job.json').read_bytes()).hexdigest(),
        expected_snapshot_sha256=hashlib.sha256((folder/'snapshot.json').read_bytes()).hexdigest(),
        legacy_code=sources,implementation_sha256='e'*64)
    assert set(approval['completed_item_ids'])=={'J_ITEM_0001','J_ITEM_0002','A_ITEM_0002','A_ITEM_0003'}
    before={p:p.read_bytes() for p in (folder/'checkpoints').rglob('accepted.json')}
    lifecycle=[]
    class ShortPreparationRuntime(ItemRuntime):
        async def ask(self,role,message,**kwargs):
            value=json.loads(message.rsplit('\n입력:\n',1)[-1])
            assignment=value['stage_data'].get('item_assignment',{})
            if role in ('author', 'direction_author') and assignment.get('item_id')==target:
                lifecycle.append('sdk_setup_started')
                # 모델 전송 전의 정상 비동기 I/O다. 고객/운영 모델은 호출하지 않는다.
                await asyncio.wait_for(asyncio.sleep(.001),.2)
                lifecycle.append('sdk_setup_completed')
            return await super().ask(role,message,**kwargs)
    class MeasuredCachePipeline(Pipeline):
        async def _reviewed_stage(self,data,stage,key,*,candidate_check=None,author_budget=None):
            if self.resume and key in self.resume.preserved:
                lifecycle.append('cache_started:'+key)
                # 실제 SDK 제한시간은 변경하지 않는다. Windows 타이머 해상도보다 큰 축소 실험이다.
                time.sleep(.25)
                result=await super()._reviewed_stage(data,stage,key,candidate_check=candidate_check,author_budget=author_budget)
                lifecycle.append('cache_completed:'+key)
                return result
            return await super()._reviewed_stage(data,stage,key,candidate_check=candidate_check,author_budget=author_budget)
    runtime=ShortPreparationRuntime()
    prepared,result=asyncio.run(MeasuredCachePipeline(runtime,folder).reviewed_actions(data))
    assert len(result['diagnoses'])==2 and len(result['actions'])==3
    cache_finishes=[i for i,event in enumerate(lifecycle) if event.startswith('cache_completed:')]
    assert len(cache_finishes)==4 and max(cache_finishes)<lifecycle.index('sdk_setup_started')
    assert lifecycle.count('sdk_setup_started')==lifecycle.count('sdk_setup_completed')==1
    assert len([call for call in runtime.calls if call['role'] in ('author', 'direction_author')])==1
    assert all(p.read_bytes()==raw for p,raw in before.items())
    assert [row['id'] for row in result['actions']]==['A_ITEM_0001','A_ITEM_0002','A_ITEM_0003']
