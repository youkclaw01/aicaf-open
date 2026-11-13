"""동일 실패 개정의 기존 성공·검토·예산을 보존하고 승인한 실패 범위만 재개한다."""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import asis_item_agents
import asis_pipeline
from asis_pipeline import Pipeline
from asis_model import digest,encoded
from test_asis_item_agents import ItemRuntime,item_fixture


def failed_fixture(tmp_path,monkeypatch,revision=None):
    sources={name:(Path(asis_pipeline.__file__).parent/name).read_bytes()
             for name in ('asis_pipeline.py','asis_item_agents.py')}
    alternate=tmp_path/'modules';alternate.mkdir()
    for name,raw in sources.items():(alternate/name).write_bytes(raw)
    monkeypatch.setattr(asis_pipeline,'__file__',str(alternate/'asis_pipeline.py'))
    monkeypatch.setattr(asis_item_agents,'__file__',str(alternate/'asis_item_agents.py'))
    class WaitingFailure(ItemRuntime):
        async def ask(self,role,message,**kwargs):
            data=json.loads(message.rsplit('\n입력:\n',1)[-1])
            from area_item_plan_stub import new_stage_answer
            _common = new_stage_answer(data)
            if _common is not None:
                return json.dumps(_common, ensure_ascii=False), {'test_only': True, 'role': role, 'session_id': kwargs.get('session_id')}
            if 'stage_data' not in data:
                # 영역별 진단·과제 계획 호출(소형 입력)은 기본 합성 대역이 처리한다.
                return await super().ask(role, message, **kwargs)
            assignment=data['stage_data'].get('item_assignment',{})
            if role in ('author', 'direction_author') and assignment.get('item_id')=='J_ITEM_0002':
                raise RuntimeError('resource_admission_wait_exhausted')
            return await super().ask(role,message,**kwargs)
    data=item_fixture();folder=tmp_path/'run'
    if revision:
        data['run']['analysis_revision']=revision
        def rebind_fixture(value):
            if isinstance(value,dict):
                if isinstance(value.get('run'),dict):value['run']=deepcopy(data['run'])
                for key,child in value.items():
                    if key!='run':rebind_fixture(child)
            elif isinstance(value,list):
                for child in value:rebind_fixture(child)
        rebind_fixture(data)
    with pytest.raises(RuntimeError,match='action_item_generation_failed'):
        asyncio.run(Pipeline(WaitingFailure(),folder).reviewed_actions(data))
    (folder/'snapshot.json').write_bytes(encoded(data))
    (folder/'job.json').write_bytes(encoded({'run':data['run'],'revision_id':data['run']['analysis_revision'],
        'status':'failed','retry_count':1,'progress':{'overall_percent':55.2},'requested_model_policy':__import__('asis_runtime').policy()}))
    return folder,data,alternate,sources


def test_approved_code_delta_reuses_completed_nodes_without_model_rewriting(tmp_path,monkeypatch):
    from asis_resume import prepare_failed_resume
    folder,data,alternate,sources=failed_fixture(tmp_path,monkeypatch)
    before={str(p.relative_to(folder)):p.read_bytes() for p in (folder/'checkpoints').rglob('*.json')
            if p.name in ('accepted.json','failed-items.json','author-input.json','author-receipt.json')}
    for name in sources:
        with (alternate/name).open('a',encoding='utf-8') as stream:stream.write('\n# 승인된 보존형 재개 시험 변경\n')
    manifest=prepare_failed_resume(folder,approval_id='approved-failed-scope',
        expected_job_sha256=hashlib.sha256((folder/'job.json').read_bytes()).hexdigest(),
        expected_snapshot_sha256=hashlib.sha256((folder/'snapshot.json').read_bytes()).hexdigest(),
        legacy_code=sources,implementation_sha256='a'*64)
    assert set(manifest['failed_item_ids'])=={'J_ITEM_0002','A_ITEM_0003'}
    runtime=ItemRuntime();pipe=Pipeline(runtime,folder)
    prepared,result=asyncio.run(pipe.reviewed_actions(data))
    assert len(result['diagnoses'])==2 and len(result['actions'])==3
    author_items=[v['input']['stage_data']['item_assignment']['item_id'] for v in runtime.calls if v['role'] in ('author', 'direction_author')]
    assert set(author_items)=={'J_ITEM_0002','A_ITEM_0003'}
    assert all(v['role']!='interpret' for v in runtime.calls),'the accepted plan was regenerated'
    for name,raw in before.items():assert (folder/name).read_bytes()==raw,name
    assert pipe.rounds==3
    verified=json.loads((folder/'resume-approvals'/'approved-failed-scope'/'revalidation.json').read_text(encoding='utf-8'))
    assert set(verified['completed_item_ids'])=={'J_ITEM_0001','A_ITEM_0001','A_ITEM_0002'}
    assert verified['fresh_model_calls_for_preserved_items']==0


def test_failed_sdk_input_without_response_keeps_original_bytes_on_resume(tmp_path,monkeypatch):
    """SDK 진입 전에 저장된 실패 입력은 응답이 없어도 재개 입력으로 덮어쓰지 않는다."""
    from asis_resume import prepare_failed_resume
    folder,data,_,sources=failed_fixture(tmp_path,monkeypatch)
    original_path=folder/'checkpoints'/'diagnosis-J_ITEM_0002'/'round-001'/'author-input.json'
    original=b'\n '+original_path.read_bytes()+b'\n'
    original_path.write_bytes(original)
    assert json.loads(original)==json.loads(original_path.read_bytes())
    assert not original_path.with_name('author-response.txt').exists()
    prepare_failed_resume(folder,approval_id='preserve-pre-sdk-input',
        expected_job_sha256=hashlib.sha256((folder/'job.json').read_bytes()).hexdigest(),
        expected_snapshot_sha256=hashlib.sha256((folder/'snapshot.json').read_bytes()).hexdigest(),
        legacy_code=sources,implementation_sha256='a'*64)
    runtime=ItemRuntime()
    asyncio.run(Pipeline(runtime,folder).reviewed_actions(data))
    assert original_path.read_bytes()==original
    resumed=list(original_path.parents[1].glob('inputs/*/*/round-*/author-input.json'))
    assert resumed and all(path!=original_path for path in resumed)


@pytest.mark.parametrize('drift',['snapshot','job','accepted','unapproved_code'])
def test_failed_scope_resume_rejects_drift_before_any_model_call(tmp_path,monkeypatch,drift):
    from asis_resume import prepare_failed_resume
    folder,data,alternate,sources=failed_fixture(tmp_path,monkeypatch)
    original=(folder/'job.json').read_bytes()
    job_hash=hashlib.sha256(original).hexdigest();snapshot_hash=hashlib.sha256((folder/'snapshot.json').read_bytes()).hexdigest()
    if drift=='snapshot':(folder/'snapshot.json').write_bytes(b'{}')
    elif drift=='job':(folder/'job.json').write_bytes(encoded({'status':'running'}))
    elif drift=='accepted':
        candidate=next((folder/'checkpoints'/'diagnosis-J_ITEM_0001').glob('accepted.json'))
        row=json.loads(candidate.read_text(encoding='utf-8'));row['candidate']['diagnoses'][0]['text']='tampered'
        candidate.write_bytes(encoded(row))
    else:sources['asis_pipeline.py']=b'not approved executable source'
    with pytest.raises(ValueError):
        prepare_failed_resume(folder,approval_id='refused-delta',expected_job_sha256=job_hash,
            expected_snapshot_sha256=snapshot_hash,legacy_code=sources,implementation_sha256='a'*64)
    assert not (folder/'resume-approvals'/'refused-delta'/'approval.json').exists()


def test_controller_queues_same_failed_revision_after_approved_delta_without_resetting_retry_limit(tmp_path,monkeypatch):
    from asis_resume import prepare_failed_resume
    from asis_view import Controller,atomic
    import asis_runtime,shutil
    folder,data,alternate,sources=failed_fixture(tmp_path,monkeypatch,revision='r-20000101T000000-0000000000000002')
    fingerprint=asis_runtime.implementation_fingerprint()
    controller=Controller(ROOT=tmp_path/'controller',assessment=None,infra_case_dir=None,db=None)
    controller.collect=lambda aid,**kwargs:data
    controller.runtime_ready=lambda:True
    aid=data['run']['assessment_id'];revision=data['run']['analysis_revision']
    target=asis_pipeline.history_path(controller.job_dir(aid,revision))
    shutil.copytree(asis_pipeline.history_path(folder),target)
    atomic(controller.folder(aid)/'state.json',{'latest_revision_id':revision,'candidate_revision_id':None})
    row=json.loads((target/'job.json').read_text(encoding='utf-8'))
    row['snapshot_sha256']=hashlib.sha256((target/'snapshot.json').read_bytes()).hexdigest()
    atomic(target/'job.json',row)
    before=(target/'job.json').read_bytes()
    prepare_failed_resume(target,approval_id='approved-controller-resume',
        expected_job_sha256=hashlib.sha256(before).hexdigest(),expected_snapshot_sha256=row['snapshot_sha256'],
        legacy_code=sources,implementation_sha256=fingerprint)
    queued=controller.resume_failed_scope(aid,revision,approval_id='approved-controller-resume',
        expected_snapshot_sha256=row['snapshot_sha256'],expected_implementation_sha256=fingerprint)
    assert queued['status']=='queued' and queued['revision_id']==revision and queued['retry_count']==1
    assert queued['failed_scope_resume_count']==1 and queued['progress']==row['progress']
    assert (target/'resume-approvals'/'approved-controller-resume'/'job-before.json').read_bytes()==before
    assert controller.state(aid)['latest_revision_id']==revision
    repeated=controller.resume_failed_scope(aid,revision,approval_id='approved-controller-resume',
        expected_snapshot_sha256=row['snapshot_sha256'],expected_implementation_sha256=fingerprint)
    assert repeated==queued


def test_failed_candidate_with_spent_author_budget_gets_only_fresh_review_and_code_projection(tmp_path,monkeypatch):
    """합성 전송에서 잘못된 관계만으로 소진한 원래 3회 이력을 보존하고 작성 호출 없이 새 검토를 수행한다."""
    from asis_resume import prepare_failed_resume
    original_project=asis_item_agents.project_item_graph
    monkeypatch.setattr(asis_item_agents,'project_item_graph',
        lambda candidate,assignment:candidate if assignment['item_id']=='A_ITEM_0003' else original_project(candidate,assignment))
    class WrongRelation(ItemRuntime):
        async def ask(self,role,message,**kwargs):
            raw,receipt=await super().ask(role,message,**kwargs)
            data=json.loads(message.rsplit('\n입력:\n',1)[-1])
            if role in ('author', 'direction_author') and data['stage_data'].get('item_assignment',{}).get('item_id')=='A_ITEM_0003':
                value=json.loads(raw);value['edges']=[{'from_id':'A_ITEM_0003','to_id':'J_ITEM_0002','relation':'implements'}]
                return json.dumps(value),receipt
            return raw,receipt
    data=item_fixture();folder=tmp_path/'spent';source_root=Path(asis_pipeline.__file__).parent
    sources={name:(source_root/name).read_bytes() for name in ('asis_pipeline.py','asis_item_agents.py')}
    with pytest.raises(RuntimeError,match='action_item_generation_failed'):
        asyncio.run(Pipeline(WrongRelation(),folder).reviewed_actions(data))
    monkeypatch.setattr(asis_item_agents,'project_item_graph',original_project)
    assert len(list((folder/'checkpoints'/'action-A_ITEM_0003').glob('round-*/author-receipt.json')))==3
    (folder/'snapshot.json').write_bytes(encoded(data));(folder/'job.json').write_bytes(encoded({'run':data['run'],
        'status':'failed','revision_id':data['run']['analysis_revision'],'retry_count':1}))
    original_fail=(folder/'checkpoints'/'actions-merge'/'failed-items.json').read_bytes()
    manifest=prepare_failed_resume(folder,approval_id='code-owned-graph-only',
        expected_job_sha256=hashlib.sha256((folder/'job.json').read_bytes()).hexdigest(),
        expected_snapshot_sha256=hashlib.sha256((folder/'snapshot.json').read_bytes()).hexdigest(),
        legacy_code=sources,implementation_sha256='b'*64)
    assert manifest['new_author_items']==[]
    assert 'action-A_ITEM_0003' in manifest['existing_candidate_review_only']
    runtime=ItemRuntime();pipe=Pipeline(runtime,folder)
    _,result=asyncio.run(pipe.reviewed_actions(data))
    assert len(result['actions'])==3 and not any(v['role'] in ('author', 'direction_author') for v in runtime.calls)
    assert len(list((folder/'checkpoints'/'action-A_ITEM_0003').glob('round-*/author-receipt.json')))==3
    assert (folder/'checkpoints'/'actions-merge'/'failed-items.json').read_bytes()==original_fail
    assert pipe.rounds==3
