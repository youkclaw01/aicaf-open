"""작성·독립 검토의 실제 보고와 중앙 task 완료·해결지시 소비 계약 시험."""
from copy import deepcopy
from pathlib import Path
import asyncio
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_analysis
from asis_model import digest
from asis_pipeline import Pipeline


def test_pair_reports_exact_candidate_to_preexisting_task_and_manager_completes_it(tmp_path):
    data, draft = asis_analysis.contract().synthetic_fixture()
    calls = []
    class SyntheticRuntime:
        async def ask(self, role, message, *, session_id, **kwargs):
            ledger = json.loads((tmp_path/'task-ledger.json').read_text(encoding='utf-8'))
            assert ledger['tasks']['area-fixture']['status'] == 'running'
            calls.append((role, session_id))
            value = deepcopy(draft) if role in ('author', 'direction_author') else {
                'stage':'review', 'run':data['run'], 'verdict':'pass',
                'content_checked':True, 'render_checked':False, 'defects':[], 'unresolved':[]}
            return json.dumps(value, ensure_ascii=False), {
                'test_only':True, 'role':role, 'session_id':session_id,
                'response_sha256':digest(value)}
    engine = Pipeline(SyntheticRuntime(), tmp_path)
    result = asyncio.run(engine.reviewed_stage(data, 1, 'area-fixture'))
    assert result == draft
    ledger = json.loads((tmp_path/'task-ledger.json').read_text(encoding='utf-8'))
    row = ledger['tasks']['area-fixture']
    assert row['status'] == 'completed'
    assert row['reports']['author']['candidate_sha256'] == digest(draft)
    assert row['reports']['reviewer']['candidate_sha256'] == digest(draft)
    assert row['reports']['reviewer']['checks']['content_checked'] is True
    assert row['reports']['author']['session_id'] != row['reports']['reviewer']['session_id']
    assert row['completion']['confirmed_by'] == 'central_task_manager'
    assert any(n['task_id']=='area-fixture' and n['event']=='task_completed'
               for n in ledger['notifications'])
    assert len(calls) == 2


def test_manager_delivers_all_literal_defects_to_same_writer_and_records_ack(tmp_path):
    """미완료 검토를 중앙이 조사하고 원 후보·전체 지적을 같은 작성자에게 전달한다."""
    data, draft = asis_analysis.contract().synthetic_fixture()
    corrected = deepcopy(draft)
    corrected['areas'][0]['headline'] = '관측 범위를 보존한 합성 분석'
    defect = {'where':'/areas/0/headline','quote':draft['areas'][0]['headline'],
              'why':'관측 범위 명시 누락','fix':'수집 범위를 제목에 명시한다.','evidence_refs':['F1']}
    calls = []
    class SyntheticRuntime:
        async def ask(self, role, message, *, session_id, **kwargs):
            payload = json.loads(message.rsplit('\n입력:\n',1)[-1])
            calls.append((role,session_id,payload))
            count = sum(c[0] == role for c in calls)
            if role in ('author', 'direction_author'):
                if count == 2:
                    directive = payload['stage_data']['task_resolution']
                    assert directive['action'] == 'revise_candidate'
                    assert directive['target_session_id'] == session_id
                    assert directive['candidate_sha256'] == digest(draft)
                    assert directive['defects'] == [defect]
                    assert payload['stage_data']['prior_output'] == draft
                value = draft if count == 1 else corrected
            else:
                value = {'stage':'review','run':data['run'],'verdict':'revise' if count==1 else 'pass',
                         'content_checked':True,'render_checked':False,
                         'defects':[defect] if count==1 else [],'unresolved':[]}
            return json.dumps(value,ensure_ascii=False), {'test_only':True,'role':role,'session_id':session_id}
    engine = Pipeline(SyntheticRuntime(),tmp_path)
    assert asyncio.run(engine.reviewed_stage(data,1,'area-repair')) == corrected
    row = engine.tasks.ledger['tasks']['area-repair']
    assert row['status'] == 'completed'
    assert row['resolutions'][0]['action'] == 'revise_candidate'
    assert row['resolutions'][0]['status'] == 'acknowledged'
    assert row['diagnoses'][0]['reason'] == 'review_revision_required'
    assert len({c[1] for c in calls if c[0]=='author'}) == 1
    assert len({c[1] for c in calls if c[0]=='content_review'}) == 1


def test_missing_completion_report_cannot_reuse_previous_approval(tmp_path):
    """성공 후 재작업의 완료 보고가 빠지면 이전 PASS로 완료하지 않는다."""
    import pytest
    from asis_tasks import TaskManager, task
    manager = TaskManager(tmp_path, {'revision':'synthetic'}, specs=[task('item','합성 항목',executor='model')])
    manager.bind_pair('item',author_session='writer',reviewer_session='reviewer')
    candidate = {'text':'verified candidate'}
    (tmp_path/'candidate.json').write_text(json.dumps(candidate),encoding='utf-8')
    (tmp_path/'review.json').write_text(json.dumps({'verdict':'pass','content_checked':True}),encoding='utf-8')
    def completed():
        for role in ('author','reviewer'):
            manager.report('item',role,session_id='writer' if role in ('author', 'direction_author') else 'reviewer',
                candidate_sha256=digest(candidate),evidence=[tmp_path/('candidate.json' if role in ('author', 'direction_author') else 'review.json')],
                checks={'deterministic_passed':True} if role in ('author', 'direction_author') else {'verdict':'pass','content_checked':True},summary='합성 완료')
        return candidate
    asyncio.run(manager.perform('item',completed))
    with pytest.raises(RuntimeError,match='task_completion_reports_missing'):
        asyncio.run(manager.perform('item',lambda:candidate))
    assert manager.ledger['tasks']['item']['status'] == 'failed'
    assert manager.progress()['completed_tasks'] == 0


def test_full_graph_exists_before_first_call_and_every_planned_item_reports(tmp_path):
    """실행 전 전체 원장과 계획 후 가변 항목을 중앙 관리자 경유로 연결한다."""
    from test_asis_connection import SyntheticRuntime, synthetic_input
    class InspectedRuntime(SyntheticRuntime):
        async def ask(self,role,message,**kwargs):
            ledger = json.loads((tmp_path/'run/task-ledger.json').read_text(encoding='utf-8'))
            assert {'interpret','issue-plan','synthesis','action-item-plan','actions','summary',
                    'document','visual-specs','analysis-content','native_pair','actual_render','visual_pages','pair_consistency'} <= set(ledger['tasks'])
            assert any(row['status']=='running' for row in ledger['tasks'].values())
            return await super().ask(role,message,**kwargs)
    runtime = InspectedRuntime()
    engine = Pipeline(runtime,tmp_path/'run')
    result = asyncio.run(engine.analyze(synthetic_input(tmp_path)))
    manager = engine.tasks
    assert manager is not None
    for family,kind in (('diagnoses','diagnosis'),('actions','action')):
        for slot in result['input']['context']['action_item_plan'][family]:
            row = manager.ledger['tasks'][kind+'-'+slot['id']]
            assert row['status'] == 'completed' and set(row['reports']) == {'author','reviewer'}
            assert row['completion']['confirmed_by'] == 'central_task_manager'
    assert manager.ledger['tasks']['analysis-content']['status'] == 'completed'
    assert manager.ledger['tasks']['native_pair']['status'] == 'pending'
    assert manager.progress()['overall_percent'] < 100


def test_item_and_native_tasks_are_registered_before_item_models_enter(tmp_path):
    """가변 항목 계획의 진짜 진단 의존성과 최종 렌더 작업을 호출 전에 원장에 확장한다."""
    from test_asis_connection import SyntheticRuntime, synthetic_input
    class InspectedRuntime(SyntheticRuntime):
        async def ask(self,role,message,**kwargs):
            payload=json.loads(message.rsplit('\n입력:\n',1)[-1])
            if 'stage_data' not in payload:
                # 영역별 진단·과제 계획 호출(소형 입력)은 기본 합성 대역이 처리한다.
                return await super().ask(role, message, **kwargs)
            assignment=payload['stage_data'].get('item_assignment')
            if assignment and role in ('author', 'direction_author'):
                ledger=json.loads((tmp_path/'run/task-ledger.json').read_text(encoding='utf-8'))
                # 항목 모델 입력에는 통합 전 계획 전체가 없다(입력 한도). 계획은 승인된 계획 체크포인트에서 읽는다.
                import glob
                accepted=sorted(glob.glob(str(tmp_path/'run/checkpoints/action-item-plan/*/accepted.json')))
                plan=json.loads(open(accepted[-1],encoding='utf-8').read())['candidate']
                assert 'action_item_plan' not in payload['context'], 'item inputs must not carry the whole plan'
                for kind,family in (('diagnosis','diagnoses'),('action','actions')):
                    for n,slot in enumerate(plan[family]):
                        slot=dict(slot,id=('J_ITEM_' if kind=='diagnosis' else 'A_ITEM_')+str(n+1).zfill(4))
                        if kind=='action':
                            slot['diagnosis_refs']=['J_ITEM_'+str(i+1).zfill(4) for i in slot['diagnosis_indices']]
                        key=kind+'-'+slot['id']
                        row=ledger['tasks'][key]
                        expected=['action-item-plan']+(['diagnosis-'+ref for ref in slot['diagnosis_refs']] if kind=='action' else [])
                        assert row['depends_on']==expected
                        assert ledger['tasks']['native-'+key]['depends_on']==[key,'actual_render']
            return await super().ask(role,message,**kwargs)
    engine=Pipeline(InspectedRuntime(),tmp_path/'run')
    asyncio.run(engine.analyze(synthetic_input(tmp_path)))
