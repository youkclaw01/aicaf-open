"""모델 미진입 검토의 별도 승인 1회 한도는 재시작·다른 역할로 우회할 수 없다."""
import json
from pathlib import Path
import pytest
import asis_resume
from asis_pipeline import history_path

TARGET='asis-review-target'

@pytest.fixture(autouse=True)
def deterministic_fake_provider_admission(monkeypatch):
    # 이 파일은 가짜 provider 호출 수·원문 바인딩·파일 보호를 검증한다.
    # 실제 호스트 자원 압박으로 가짜 요청이 기다리지 않도록 자원 표본만 결정적으로 고정한다.
    # 실제 Windows SDK/native 증명은 별도 실행이며 운영 admission 코드는 바꾸지 않는다.
    from asis_admission import HostSampler
    monkeypatch.setattr(HostSampler,'sample',lambda self:{'sample_valid':True,
        'total_memory_bytes':16*1024**3,'available_memory_bytes':12*1024**3,
        'worker_tree_rss_bytes':64*1024**2,'cpu_percent':10.,'logical_cpu_count':10})

def request(folder):
    value={'schema':'asis-unstarted-review-continuation/1','approval_id':'approved-once',
           'target_session_id':TARGET,'target_author_session_id':'asis-author-target',
           'review_call_budget':1,'candidate_sha256':'c'*64,
           'canonical_input_sha256':__import__('hashlib').sha256(b'prompt').hexdigest(),
           'instructions_sha256':__import__('hashlib').sha256(b'instructions').hexdigest()}
    (folder/'unstarted-review-continuation.json').write_text(json.dumps(value),encoding='utf-8')
    return value

@pytest.mark.parametrize('message,instructions',[('changed','instructions'),('prompt','changed')])
def test_approved_target_message_and_instructions_cannot_drift(tmp_path,message,instructions):
    request(tmp_path)
    with pytest.raises(ValueError,match='unstarted_review_input_binding_changed'):
        asis_resume.claim_unstarted_review(tmp_path,'content_review',TARGET,message,instructions,image=None)
    assert not (tmp_path/'unstarted-review-call-once.json').exists()

def test_unstarted_review_claim_is_durable_across_runtime_instances(tmp_path):
    request(tmp_path)
    first=asis_resume.claim_unstarted_review(tmp_path,'content_review',TARGET,'prompt','instructions',image=None)
    assert first['approval_id']=='approved-once'
    assert (tmp_path/'unstarted-review-call-once.json').is_file()
    with pytest.raises(RuntimeError,match='unstarted_review_call_budget_exhausted'):
        asis_resume.claim_unstarted_review(tmp_path,'content_review',TARGET,'prompt','instructions',image=None)

def test_target_writer_cannot_consume_or_bypass_one_review_budget(tmp_path):
    request(tmp_path)
    with pytest.raises(RuntimeError,match='unstarted_review_reauthor_forbidden'):
        asis_resume.claim_unstarted_review(tmp_path,'author','asis-author-target','prompt','instructions',image=None)
    assert not (tmp_path/'unstarted-review-call-once.json').exists()

def test_pending_downstream_and_native_image_review_keep_normal_budgets(tmp_path):
    request(tmp_path)
    assert asis_resume.claim_unstarted_review(tmp_path,'author','summary-writer','prompt','instructions',image=None) is None
    assert asis_resume.claim_unstarted_review(tmp_path,'content_review',TARGET,'prompt','instructions',image=b'image') is None
    assert not (tmp_path/'unstarted-review-call-once.json').exists()

def test_corrupt_continuation_fails_closed(tmp_path):
    value=request(tmp_path);value['review_call_budget']=2
    (tmp_path/'unstarted-review-continuation.json').write_text(json.dumps(value),encoding='utf-8')
    with pytest.raises(ValueError,match='invalid_unstarted_review_budget'):
        asis_resume.claim_unstarted_review(tmp_path,'content_review',TARGET,'prompt','instructions',image=None)


def test_actual_runtime_allows_only_one_target_provider_invocation(tmp_path):
    import asyncio,types
    from asis_runtime import Runtime
    request(tmp_path);calls=[]
    class Client:
        async def start(self):pass
        async def stop(self):pass
    class Agent:
        def __init__(self,**kwargs):pass
        async def run(self,messages,*,session,options=None):
            calls.append(session.session_id)
            return types.SimpleNamespace(text='{"ok":true}',response_id='synthetic-budget-check',
                additional_properties={'model':'gpt-6.1-sol'},finish_reason='stop',usage_details={})
    async def exercise():
        async with Runtime(client=Client(),agent_factory=Agent,base_directory=tmp_path/'sessions') as runtime:
            await runtime.ask('content_review','prompt',session_id=TARGET,instructions='instructions')
            with pytest.raises(RuntimeError,match='unstarted_review_call_budget_exhausted'):
                await runtime.ask('content_review','prompt',session_id=TARGET,instructions='instructions')
    asyncio.run(exercise())
    assert calls==[TARGET]


def test_continuation_refuses_model_started_failure_before_writing(tmp_path):
    failure={'schema':'asis-runtime-failure/1','session_id':TARGET,'role':'content_review',
        'failure_boundary':'sdk_request','model_request_started':True}
    (tmp_path/'runtime-failures').mkdir()
    (tmp_path/'runtime-failures/failure.json').write_text(json.dumps(failure),encoding='utf-8')
    with pytest.raises(ValueError,match='continuation_requires_unstarted_session_setup_failure'):
        asis_resume.prepare_unstarted_review_continuation(tmp_path,approval_id='explicit-approved-once',
            failure_path='runtime-failures/failure.json',setup_proof={},implementation_sha256='a'*64)
    assert not (tmp_path/'unstarted-review-continuation.json').exists()


def test_continuation_preserves_parent_and_all_completed_items(tmp_path):
    import asyncio,hashlib
    from asis_pipeline import Pipeline
    from asis_model import encoded,digest
    from asis_runtime import policy
    from test_asis_item_agents import ItemRuntime,item_fixture
    from asis_resume import prepare_failed_resume
    class SetupFailure(ItemRuntime):
        async def ask(self,role,message,**kwargs):
            data=json.loads(message.rsplit('\n입력:\n',1)[-1])
            if role=='content_review' and data['stage_data'].get('item_assignment',{}).get('item_id')=='A_ITEM_0003':
                raise TimeoutError('synthetic_session_setup_timeout')
            return await super().ask(role,message,**kwargs)
    data=item_fixture();folder=tmp_path/'run'
    with pytest.raises(RuntimeError,match='action_item_generation_failed'):
        asyncio.run(Pipeline(SetupFailure(),folder).reviewed_actions(data))
    (folder/'snapshot.json').write_bytes(encoded(data))
    (folder/'job.json').write_bytes(encoded({'run':data['run'],'status':'failed','retry_count':1,
        'failed_scope_resume_count':1,'failed_scope_resume_id':'parent','requested_model_policy':policy()}))
    sources={name:(Path(__file__).resolve().parents[1]/name).read_bytes() for name in ('asis_pipeline.py','asis_item_agents.py')}
    parent=prepare_failed_resume(folder,approval_id='parent',expected_job_sha256=hashlib.sha256((folder/'job.json').read_bytes()).hexdigest(),
        expected_snapshot_sha256=hashlib.sha256((folder/'snapshot.json').read_bytes()).hexdigest(),legacy_code=sources,implementation_sha256='a'*64)
    target='asis-review-'+digest({'run':data['run'],'key':'action-A_ITEM_0003'})[:24]
    (folder/'runtime-failures').mkdir(exist_ok=True)
    failure={'schema':'asis-runtime-failure/1','session_id':target,'role':'content_review',
        'failure_boundary':'session_setup','model_request_started':False}
    (folder/'runtime-failures/failure.json').write_bytes(encoded(failure))
    proof={'schema':'asis-real-session-setup-proof/1','verified':True,'provider_requests':0,'session_id':target,
        'original_service_session_preserved':True,'original_history_prefix_unchanged':True,'new_user_message_events':0,
        'canonical_input_sha256':'c'*64,'instructions_sha256':'d'*64}
    # 영역별 계획 검토 원장은 경로가 길어 Windows 임시 폴더에서 260자를 넘는다. 운영 쓰기와 같은 긴 경로 접두(history_path)로 읽는다.
    before={p:history_path(p).read_bytes() for p in folder.rglob('*.json')}
    record=asis_resume.prepare_unstarted_review_continuation(folder,approval_id='explicit-unstarted-once',
        failure_path='runtime-failures/failure.json',setup_proof=proof,implementation_sha256='b'*64)
    assert record['review_call_budget']==1 and record['author_rounds_increased'] is False
    assert record['canonical_input_sha256']==proof['canonical_input_sha256']
    assert record['instructions_sha256']==proof['instructions_sha256']
    assert record['parent_approval_sha256']==hashlib.sha256((folder/'resume-approvals/parent/approval.json').read_bytes()).hexdigest()
    assert len(record['completed_item_ids'])==4 and record['failed_item_ids']==['A_ITEM_0003']
    assert all(history_path(p).read_bytes()==raw for p,raw in before.items())
    runtime=ItemRuntime()
    resumed=Pipeline(runtime,folder)
    assert resumed.resume.approval['implementation_sha256']=='b'*64
    assert set(record['completed_task_ids']).issubset(resumed.resume.preserved)
    _,result=asyncio.run(resumed.reviewed_actions(data))
    assert len(result['diagnoses'])==2 and len(result['actions'])==3
    assert not [x for x in runtime.calls if x['role'] in ('author', 'direction_author')]
    assert len([x for x in runtime.calls if x['role']=='content_review'])==1
    assert all(p.read_bytes()==raw for p,raw in before.items() if p.name in ('author-input.json','author-receipt.json'))


def test_added_completed_stage_uses_its_current_origin_not_initial_legacy_code(tmp_path):
    from types import SimpleNamespace
    from asis_model import digest,encoded
    from asis_runtime import policy
    from asis_resume import ResumeContext,file_sha
    key='action-newly-completed';data={'stage_data':{'item_contract':'current-contract'}}
    pipe=SimpleNamespace(render_prompt=lambda stage,value,*,model_view=True:('system','prompt'))
    candidate={'body':'preserved-current-origin'}
    current=b'current-stage-source';legacy=b'initial-stage-source'
    binding=digest({'input':data,'stage':3,'code':current.hex(),'model_policy':policy(),'prompts':pipe.render_prompt(3,data)})
    root=tmp_path/'checkpoints'/key;root.mkdir(parents=True)
    path=root/'accepted.json';record={'input_sha256':binding,'candidate':candidate,'candidate_sha256':digest(candidate)}
    path.write_bytes(encoded(record))
    context=object.__new__(ResumeContext)
    context.folder=tmp_path;context.preserved={key};context.original_preserved=set();context.current_pipeline=current
    context.legacy={'asis_pipeline.py':legacy,'asis_item_agents.py':b'legacy-item'}
    context.legacy_render=lambda *args:('legacy-system','legacy-prompt')
    context.approval={'protected_checkpoints':{str(path.relative_to(tmp_path)):file_sha(path)}}
    assert context.stage(pipe,data,3,key)==record


def test_controller_continuation_keeps_original_retry_counters_and_history(tmp_path,monkeypatch):
    from asis_view import Controller,atomic
    from asis_runtime import policy
    from asis_resume import file_sha
    import asis_runtime
    controller=Controller(ROOT=tmp_path/'state',assessment=None,infra_case_dir=None,db=None)
    aid=24;revision='r-20000101T000000-0000000000000003';folder=controller.job_dir(aid,revision)
    run={'assessment_id':aid,'analysis_revision':revision,'source_bundle_sha256':'s'*64}
    atomic(folder/'snapshot.json',{'run':run})
    row={'run':run,'revision_id':revision,'status':'failed','pid':None,'retry_count':1,
         'failed_scope_resume_count':1,'requested_model_policy':policy(),'snapshot_sha256':file_sha(folder/'snapshot.json')}
    atomic(folder/'job.json',row);atomic(folder/'task-ledger.json',{'run':run,'tasks':{}})
    atomic(controller.folder(aid)/'state.json',{'latest_revision_id':revision})
    record={'schema':'asis-unstarted-review-continuation/1','approval_id':'explicit-once',
        'run':run,'implementation_sha256':'b'*64,'snapshot_sha256':row['snapshot_sha256'],
        'job_before_sha256':file_sha(folder/'job.json'),'task_ledger_before_sha256':file_sha(folder/'task-ledger.json'),
        'review_call_budget':1,'retry_count_preserved':1,'failed_scope_resume_count_preserved':1}
    root=folder/'unstarted-review-continuations/explicit-once'
    atomic(root/'approval.json',record);atomic(folder/'unstarted-review-continuation.json',record)
    before=(folder/'job.json').read_bytes();atomic(root/'job-before.json',before)
    controller.runtime_ready=lambda:True;controller.collect=lambda *args,**kwargs:{'run':run}
    monkeypatch.setattr(asis_runtime,'implementation_fingerprint',lambda:'b'*64)
    queued=controller.continue_unstarted_review(aid,revision,approval_id='explicit-once',
        expected_implementation_sha256='b'*64,expected_approval_sha256=file_sha(root/'approval.json'))
    assert queued['status']=='queued' and queued['revision_id']==revision
    assert queued['retry_count']==queued['failed_scope_resume_count']==1
    assert queued['unstarted_review_continuation_count']==1
    assert (root/'job-before.json').read_bytes()==before
    assert controller.continue_unstarted_review(aid,revision,approval_id='explicit-once',
        expected_implementation_sha256='b'*64,expected_approval_sha256=file_sha(root/'approval.json'))==queued


def test_child_checkpoint_map_cannot_replace_original_protected_hash(tmp_path):
    from asis_resume import ResumeContext,file_sha
    from asis_view import atomic
    from asis_model import encoded
    import hashlib
    folder=tmp_path/'run';parent=folder/'resume-approvals/parent';parent.mkdir(parents=True)
    source=Path(__file__).resolve().parents[1]/'asis_pipeline.py';raw=source.read_bytes()
    (parent/'legacy-code').mkdir();(parent/'legacy-code/asis_pipeline.py').write_bytes(raw)
    atomic(folder/'snapshot.json',{'run':{}})
    accepted=folder/'checkpoints/original/accepted.json';atomic(accepted,{'candidate':{},'review':{'verdict':'pass'}})
    relative=str(accepted.relative_to(folder));original=file_sha(accepted)
    approval={'snapshot_sha256':file_sha(folder/'snapshot.json'),'legacy_code_sha256':{'asis_pipeline.py':hashlib.sha256(raw).hexdigest()},
              'completed_task_ids':['original'],'protected_checkpoints':{relative:original},'session_restores':[]}
    atomic(parent/'approval.json',approval)
    child=folder/'unstarted-review-continuations/child';child.mkdir(parents=True)
    (child/'current-pipeline.py').write_bytes(raw)
    accepted.write_bytes(accepted.read_bytes()+b'\n')
    continuation={'schema':'asis-unstarted-review-continuation/1','approval_id':'child','review_call_budget':1,
        'parent_approval_sha256':file_sha(parent/'approval.json'),'snapshot_sha256':file_sha(folder/'snapshot.json'),
        'current_pipeline_sha256':hashlib.sha256(raw).hexdigest(),'completed_task_ids':['original'],
        'implementation_sha256':'b'*64,'session_restores':[],'protected_checkpoints':{relative:file_sha(accepted)}}
    atomic(child/'approval.json',continuation);atomic(folder/'unstarted-review-continuation.json',continuation)
    with pytest.raises(ValueError,match='unstarted_review_original_checkpoint_conflict'):
        ResumeContext(folder,{'approval_id':'parent','approval_sha256':file_sha(parent/'approval.json')})
