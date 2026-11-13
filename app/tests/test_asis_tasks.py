"""중앙 task의 동적 계획·실행 소유·무효화·완료 증거 계약 시험."""
import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from asis_tasks import TaskManager, task


def test_registering_child_tasks_preserves_active_parent_completion(tmp_path):
    """진행 중인 부모 객체를 계획 확장으로 교체하면 실제 완료가 원장에 반영되지 않는다."""
    manager = TaskManager(tmp_path, {'revision':'synthetic'}, specs=[task('parent','부모')])
    async def run_parent():
        manager.register([task('child','자식')])
        await manager.perform('child',lambda:'child result')
        return 'parent result'
    assert asyncio.run(manager.perform('parent',run_parent)) == 'parent result'
    assert manager.ledger['tasks']['parent']['status'] == 'completed'
    assert manager.progress()['completed_tasks'] == 2


def test_windows_report_accepts_same_file_with_mixed_native_path_prefixes(tmp_path):
    """일반 경로와 긴 경로의 같은 파일은 동일 원장으로 확인하되 외부 경로는 거부한다."""
    import os,pytest
    from asis_pipeline import history_path
    from asis_model import digest
    if os.name!='nt':pytest.skip('native Windows report path boundary')
    manager=TaskManager(history_path(tmp_path/'ledger'),{},specs=[task('item','원장 경로')])
    manager.bind_pair('item',author_session='path-writer',reviewer_session='path-reviewer')
    evidence=tmp_path/'ledger'/'candidate.json';evidence.write_text('{}',encoding='utf-8')
    binding=digest({'candidate':'synthetic'})
    def operation():
        for role,session,path in (('author','path-writer',evidence),('reviewer','path-reviewer',history_path(evidence))):
            checks={'deterministic_passed':True} if role in ('author', 'direction_author') else {'verdict':'pass','content_checked':True,'defects':[],'unresolved':[]}
            manager.report('item',role,session_id=session,candidate_sha256=binding,evidence=[path],checks=checks,summary='합성 경로 검증')
    asyncio.run(manager.perform('item',operation))
    assert manager.ledger['tasks']['item']['status']=='completed'
    assert manager.ledger['tasks']['item']['reports']['author']['evidence'][0]['path']=='candidate.json'
    outside=tmp_path/'outside.json';outside.write_text('{}',encoding='utf-8')
    with pytest.raises(ValueError):
        manager.report('item','author',session_id='path-writer',candidate_sha256=binding,evidence=[history_path(outside)],checks={'deterministic_passed':True},summary='외부 경로 거부')


def test_rework_invalidates_only_dependent_completions_and_preserves_evidence(tmp_path):
    """상위 후보 재작업은 후속 승인을 무효화하며 독립 작업·이전 증거는 보존한다."""
    manager=TaskManager(tmp_path,{'revision':'synthetic'},specs=[
        task('upstream','상위'),task('child','연결',depends_on=['upstream']),
        task('independent','독립')])
    async def run():
        await manager.perform('upstream',lambda:'v1')
        await manager.perform('child',lambda:'child v1')
        await manager.perform('independent',lambda:'independent')
        manager.invalidate(['upstream'],reason='verified_candidate_revised')
        assert manager.ledger['tasks']['child']['status']=='invalidated'
        assert manager.ledger['tasks']['independent']['status']=='completed'
        assert manager.ledger['tasks']['child']['attempts'][0]['status']=='completed'
        assert manager.progress()['completed_tasks']==1
        await manager.perform('upstream',lambda:'v2')
        await manager.perform('child',lambda:'child v2')
    asyncio.run(run())
    assert manager.progress()['completed_tasks']==3


def test_failed_owner_is_diagnosed_and_gets_non_retry_resolution(tmp_path):
    """공급자 오류는 원 역할의 실상태로 진단하며 무한 재실행 대신 근거 있는 보류 지시를 전달한다."""
    import pytest
    manager=TaskManager(tmp_path,{'revision':'synthetic'},specs=[task('item','항목',executor='model')])
    manager.bind_pair('item',author_session='writer',reviewer_session='reviewer')
    class Runtime:
        activity={'writer':{'state':'failed','error_type':'ProviderLimited','provider_backpressure':True}}
    manager.runtime=Runtime()
    def operation():raise RuntimeError('provider_model_call_failed')
    with pytest.raises(RuntimeError):asyncio.run(manager.perform('item',operation))
    row=manager.ledger['tasks']['item']
    assert row['diagnoses'][-1]['reason']=='provider_backpressure'
    assert row['resolutions'][-1]['action']=='wait_provider_capacity'
    assert row['resolutions'][-1]['target_session_id']=='writer'
    assert row['resolutions'][-1]['automatic_retry_allowed'] is False


def test_superseded_reviews_are_preserved_but_not_counted_as_current_work(tmp_path):
    """이전 렌더의 반려 증거를 보존하면서 현재 분모·완료 상태와 구분한다."""
    manager=TaskManager(tmp_path,{},specs=[task('old','이전 검토'),task('current','현재 검토')])
    manager.ledger['tasks']['old']['status']='superseded'
    asyncio.run(manager.perform('current',lambda:'current result'))
    assert manager.progress()['overall_percent']==100
    assert manager.progress()['total_tasks']==1
    assert manager.progress()['superseded_tasks']==1


def test_pair_cannot_complete_with_failed_deterministic_checks(tmp_path):
    """검수자 PASS만으로 구조 검사 실패나 미완료 지적을 덮지 않는다."""
    import json,pytest
    from asis_model import digest
    manager=TaskManager(tmp_path,{},specs=[task('pair','작성·검수',executor='model')])
    manager.bind_pair('pair',author_session='writer',reviewer_session='reviewer')
    p=tmp_path/'evidence.json';p.write_text(json.dumps({'candidate':True}),encoding='utf-8')
    def operation():
        manager.report('pair','author',session_id='writer',candidate_sha256=digest({'candidate':True}),
            evidence=[p],checks={'deterministic_passed':False},summary='검사 실패 후보')
        manager.report('pair','reviewer',session_id='reviewer',candidate_sha256=digest({'candidate':True}),
            evidence=[p],checks={'verdict':'pass','content_checked':True},summary='검토 결과')
    with pytest.raises(RuntimeError,match='task_completion_deterministic_failed'):
        asyncio.run(manager.perform('pair',operation))
