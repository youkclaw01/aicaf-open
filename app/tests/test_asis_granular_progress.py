"""작성 근거·독립 검토·현재 후보 확정의 실제 세부 완료만 점진 진행률에 반영한다."""
from pathlib import Path
import asyncio
import json
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from asis_tasks import TaskManager,task
from asis_model import digest


def test_real_author_and_review_reports_increment_before_pair_acceptance(tmp_path):
    manager=TaskManager(tmp_path,{},specs=[task('pair','판단 작성·검토',executor='model',weight=6),task('next','후속 검증',weight=6)])
    total_before=sum(row['weight'] for row in manager.ledger['tasks'].values())
    manager.bind_pair('pair',author_session='writer',reviewer_session='reviewer')
    candidate={'text':'근거에 결합된 합성 후보'}
    path=tmp_path/'candidate.json';path.write_text(json.dumps(candidate),encoding='utf-8')
    observed=[]
    def work():
        assert manager.progress()['overall_percent']==0
        manager.report('pair','author',session_id='writer',candidate_sha256=digest(candidate),evidence=[path],
            checks={'deterministic_passed':True},summary='실제 작성 후보의 구조·참조 검증')
        observed.append(manager.progress())
        manager.report('pair','reviewer',session_id='reviewer',candidate_sha256=digest(candidate),evidence=[path],
            checks={'verdict':'pass','content_checked':True,'render_checked':False,'defects':[],'unresolved':[]},summary='독립 검토 통과')
        observed.append(manager.progress())
        return candidate
    asyncio.run(manager.perform('pair',work))
    assert 0<observed[0]['overall_percent']<observed[1]['overall_percent']<manager.progress()['overall_percent']<100, 'paired task still waits for both calls before all progress'
    assert total_before==sum(row['weight'] for row in manager.ledger['tasks'].values())
    assert observed[0]['completed_tasks']==1
    assert observed[1]['completed_tasks']==2
    assert manager.progress()['completed_tasks']==3
    steps=[row for row in manager.ledger['tasks'].values() if row.get('parent_task_id')=='pair']
    assert len(steps)==2 and all(row['completion']['candidate_sha256']==digest(candidate) for row in steps)


def test_rejected_review_and_new_candidate_cannot_reuse_previous_phase_completion(tmp_path):
    manager=TaskManager(tmp_path,{},specs=[task('pair','합성 쌍',executor='model')])
    manager.bind_pair('pair',author_session='writer',reviewer_session='reviewer')
    candidate={'text':'합성 후보'};path=tmp_path/'candidate.json';path.write_text(json.dumps(candidate),encoding='utf-8')
    def work():
        manager.report('pair','author',session_id='writer',candidate_sha256=digest(candidate),evidence=[path],checks={'deterministic_passed':True},summary='구조 확인')
        manager.report('pair','reviewer',session_id='reviewer',candidate_sha256=digest(candidate),evidence=[path],
            checks={'verdict':'revise','content_checked':True,'render_checked':False,'defects':[{'why':'보완'}],'unresolved':[]},summary='실제 보완 지적')
        assert manager.progress()['completed_tasks']==1
        assert manager.ledger['tasks']['pair']['status']=='running'
        manager.begin_pair_candidate('pair')
        assert manager.progress()['completed_tasks']==0
        assert manager.progress()['overall_percent']==0
        return candidate
    asyncio.run(manager.perform('pair',work,allow_review_revision=True))
    assert manager.ledger['tasks']['pair']['status']=='revision_required'


def test_registered_dependency_wait_is_not_reported_as_active_model(tmp_path):
    manager=TaskManager(tmp_path,{},specs=[task('upstream','선행 근거'),task('child','후속 판단',depends_on=['upstream'])])
    async def run():
        child=asyncio.create_task(manager.perform('child',lambda:'child'))
        await asyncio.sleep(.01)
        progress=manager.progress()
        assert not progress['running_tasks']
        assert progress['waiting_tasks']==[{'id':'child','title':'후속 판단','reason':'dependency_wait','dependencies':['upstream']}]
        await manager.perform('upstream',lambda:'ready')
        assert await child=='child'
    asyncio.run(run())
