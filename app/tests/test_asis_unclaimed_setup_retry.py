"""모델 미진입 준비 실패만 동일 승인·1회 모델 한도를 보존하여 1회 재대기한다."""
from copy import deepcopy
from pathlib import Path
import sys
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from asis_view import Controller,atomic
from asis_resume import file_sha
from asis_runtime import policy
import asis_runtime


def fixture(tmp_path,monkeypatch):
    controller=Controller(ROOT=tmp_path/'state',assessment=None,infra_case_dir=None,db=None)
    aid=24;revision='r-20000101T000000-0000000000000004';folder=controller.job_dir(aid,revision)
    run={'assessment_id':aid,'analysis_revision':revision,'source_bundle_sha256':'s'*64}
    snapshot={'run':run};atomic(folder/'snapshot.json',snapshot)
    checkpoint=folder/'checkpoints/preserved/accepted.json';atomic(checkpoint,{'candidate':{'untouched':True}})
    atomic(folder/'task-ledger.json',{'run':run,'tasks':{'preserved':{'status':'completed'}}})
    row={'run':run,'revision_id':revision,'status':'failed','pid':None,'retry_count':1,
        'failed_scope_resume_count':1,'unstarted_review_continuation_count':1,
        'unstarted_review_continuation_id':'original-once','unstarted_review_continuation_implementation_sha256':'b'*64,
        'requested_model_policy':policy(),'snapshot_sha256':file_sha(folder/'snapshot.json')}
    atomic(folder/'job.json',row);atomic(controller.folder(aid)/'state.json',{'latest_revision_id':revision})
    record={'schema':'asis-unstarted-review-continuation/1','approval_id':'original-once','run':run,
        'target_session_id':'review-target','target_author_session_id':'author-target','review_call_budget':1,
        'implementation_sha256':'b'*64,'snapshot_sha256':row['snapshot_sha256'],
        'job_before_sha256':'0'*64,'task_ledger_before_sha256':'1'*64,
        'retry_count_preserved':1,'failed_scope_resume_count_preserved':1,
        'protected_checkpoints':{str(checkpoint.relative_to(folder)):file_sha(checkpoint)}}
    root=folder/'unstarted-review-continuations/original-once'
    atomic(root/'approval.json',record);atomic(folder/'unstarted-review-continuation.json',record)
    failure='runtime-failures/current.json'
    atomic(folder/failure,{'schema':'asis-runtime-failure/1','session_id':'review-target','role':'content_review',
        'failure_boundary':'session_setup','model_request_started':False,'error_type':'TimeoutError'})
    controller.runtime_ready=lambda:True;controller.collect=lambda *args,**kwargs:deepcopy(snapshot)
    monkeypatch.setattr(asis_runtime,'implementation_fingerprint',lambda:'f'*64)
    arguments={'approval_id':'original-once','setup_retry_id':'explicit-small-unit-once',
        'expected_job_sha256':file_sha(folder/'job.json'),
        'expected_approval_sha256':file_sha(root/'approval.json'),
        'expected_implementation_sha256':'f'*64,'failure_path':failure}
    return controller,aid,revision,folder,arguments


def test_unclaimed_setup_retry_requeues_without_replacing_approval_or_budget(tmp_path,monkeypatch):
    controller,aid,revision,folder,arguments=fixture(tmp_path,monkeypatch)
    before={p:p.read_bytes() for p in folder.rglob('*.json')}
    queued=controller.retry_unclaimed_review_setup(aid,revision,**arguments)
    assert queued['status']=='queued' and queued['revision_id']==revision
    assert queued['retry_count']==queued['failed_scope_resume_count']==queued['unstarted_review_continuation_count']==1
    assert queued['unstarted_review_setup_retry_count']==1
    assert queued['unstarted_review_setup_retry_id']=='explicit-small-unit-once'
    assert queued['unstarted_review_setup_retry_implementation_sha256']=='f'*64
    assert queued['unstarted_review_continuation_implementation_sha256']=='b'*64
    assert all(p.read_bytes()==raw for p,raw in before.items() if p.name!='job.json')
    root=folder/'unstarted-review-setup-retries/explicit-small-unit-once'
    assert (root/'job-before.json').read_bytes()==before[folder/'job.json']
    assert controller.retry_unclaimed_review_setup(aid,revision,**arguments)==queued


@pytest.mark.parametrize('case,code',[
    ('claim','setup_retry_model_claim_already_consumed'),
    ('sent','setup_retry_requires_unstarted_preparation_failure'),
    ('wrong_role','setup_retry_requires_unstarted_preparation_failure'),
    ('wrong_session','setup_retry_requires_unstarted_preparation_failure'),
    ('checkpoint','setup_retry_protected_checkpoint_changed'),
    ('job','setup_retry_job_changed'),
    ('approval','setup_retry_original_request_changed'),
    ('source','setup_retry_operating_source_changed')])
def test_unclaimed_setup_retry_refuses_drift_before_queue(tmp_path,monkeypatch,case,code):
    controller,aid,revision,folder,arguments=fixture(tmp_path,monkeypatch)
    import json
    if case=='claim':atomic(folder/'unstarted-review-call-once.json',{'already_claimed':True})
    elif case in ('sent','wrong_role','wrong_session'):
        path=folder/arguments['failure_path'];row=json.loads(path.read_text(encoding='utf-8'))
        row.update({'model_request_started':True} if case=='sent' else
                   {'role':'author'} if case=='wrong_role' else {'session_id':'other-session'})
        atomic(path,row)
    elif case=='checkpoint':atomic(folder/'checkpoints/preserved/accepted.json',{'changed':True})
    elif case=='job':
        path=folder/'job.json';row=json.loads(path.read_text(encoding='utf-8'))
        row['requirements']='changed';atomic(path,row)
    elif case=='approval':atomic(folder/'unstarted-review-continuation.json',{'changed':True})
    elif case=='source':controller.collect=lambda *args,**kwargs:{'run':{'source_bundle_sha256':'z'*64}}
    before={p:p.read_bytes() for p in folder.rglob('*.json')}
    with pytest.raises(ValueError,match=code):
        controller.retry_unclaimed_review_setup(aid,revision,**arguments)
    assert all(p.read_bytes()==raw for p,raw in before.items())
    assert not (folder/'unstarted-review-setup-retries').exists()


def test_failed_setup_retry_cannot_be_requeued_again_under_same_or_new_id(tmp_path,monkeypatch):
    controller,aid,revision,folder,arguments=fixture(tmp_path,monkeypatch)
    row=controller.retry_unclaimed_review_setup(aid,revision,**arguments)
    row['status']='failed';atomic(folder/'job.json',row)
    before=(folder/'job.json').read_bytes()
    for setup_retry_id in ('explicit-small-unit-once','another-retry'):
        with pytest.raises(ValueError,match='unclaimed_setup_retry_budget_exhausted'):
            controller.retry_unclaimed_review_setup(aid,revision,**{**arguments,'setup_retry_id':setup_retry_id})
    assert (folder/'job.json').read_bytes()==before
