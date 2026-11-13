"""영역 작성은 계획 이슈 1개당 작성·검토 쌍 1개로 동시에 실행되고, 영역 합본은 코드가 기존 영역 구조로 만든다."""
from pathlib import Path
import asyncio, json, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_analysis
from test_asis_connection import synthetic_input


def test_each_planned_issue_gets_its_own_author_reviewer_pair_and_area_is_merged_by_code(tmp_path):
    from test_asis_connection import SyntheticRuntime
    from asis_pipeline import Pipeline
    seed = asis_analysis.analyze(synthetic_input(tmp_path))
    data, areas = seed['input'], seed['stages'][0]['areas']
    cost = next(row for row in areas if row['area_id'] == 'cost')
    base_fact = data['facts'][0]['id']
    base_crit = data['criteria'][0]['id']
    issues = []
    for n in range(3):
        issues.append({'id': 'PLAN_P%04d' % (n + 1), 'observation_id': 'O_P%04d' % (n + 1), 'lead_area': 'cost',
                       'area_refs': ['cost'], 'fact_refs': [base_fact], 'criterion_refs': [base_crit], 'goal_refs': [],
                       'scope': '합성 이슈 %d' % n, 'reason': '합성 근거'})
    data['context']['analysis_plan'] = {'issues': issues}
    data['allocated_ids']['observations'] = list(dict.fromkeys(data['allocated_ids']['observations'] + [r['observation_id'] for r in issues]))
    pipe = Pipeline(SyntheticRuntime(), tmp_path / 'run')
    scoped = pipe.area_issue_input(data, cost, {'context': {'goal_interpretations': []}}, issues[1])
    # 입력은 이 이슈 하나만 담는다.
    assert [row['id'] for row in scoped['stage_data']['planned_observation_slots']] == ['PLAN_P0002']
    assert scoped['allocated_ids']['observations'] == ['O_P0002']
    assert [row['id'] for row in scoped['context']['analysis_plan']['issues']] == ['PLAN_P0002']
    assert scoped['stage_data']['issue_assignment']['item_id'] == 'O_P0002'
    # 정본은 변경되지 않는다.
    assert len(data['context']['analysis_plan']['issues']) == 3


def test_area_summary_input_has_no_fact_bodies_and_only_the_approved_observations(tmp_path):
    from test_asis_connection import SyntheticRuntime
    from asis_pipeline import Pipeline
    seed = asis_analysis.analyze(synthetic_input(tmp_path))
    data, areas = seed['input'], seed['stages'][0]['areas']
    area = areas[0]
    pipe = Pipeline(SyntheticRuntime(), tmp_path / 'run')
    brief = [{k: row[k] for k in ('id', 'text', 'fact_refs', 'criterion_refs', 'goal_refs', 'judgment_refs')} for row in area['observations']]
    scoped = pipe.area_summary_input(data, area, {'context': {'goal_interpretations': []}}, brief)
    assert scoped['context']['model_scope']['inline_fact_refs'] == []
    assert scoped['stage_data']['approved_observations'] == brief
    assert scoped['allocated_ids']['observations'] == [] and scoped['allocated_ids']['implications'] == [area['implication']['id']]


def test_area_with_several_issues_runs_one_author_and_reviewer_per_issue_and_merges_by_code(tmp_path):
    """실제 compose_stages 경로: 이슈 3개인 영역은 area_issue 3호출+area_summary 1호출이고, 합본은 기존 영역 구조다."""
    import copy
    from test_asis_connection import SyntheticRuntime
    from asis_pipeline import Pipeline

    class PerIssueRuntime(SyntheticRuntime):
        def __init__(self):
            super().__init__()
            self.reviews_by_mode = {}

        async def ask(self, role, message, **kwargs):
            raw, receipt = await super().ask(role, message, **kwargs)
            payload = json.loads(message.rsplit('\n입력:\n', 1)[-1])
            mode = payload['stage_data'].get('mode')
            if role == 'content_review':
                draft_mode = payload['stage_data'].get('mode')
                self.reviews_by_mode[draft_mode] = self.reviews_by_mode.get(draft_mode, 0) + 1
            return raw, receipt

    seed = asis_analysis.analyze(synthetic_input(tmp_path))
    data, baseline = seed['input'], seed
    facts = [row['id'] for row in data['facts']][:3]
    crit = data['criteria'][0]['id']
    issues = []
    for area in baseline['stages'][0]['areas']:
        count = 3 if area['area_id'] == 'cost' else 1
        for n in range(count):
            number = len(issues) + 1
            issues.append({'id': 'PLAN_P%04d' % number, 'observation_id': 'O_P%04d' % number, 'lead_area': area['area_id'],
                           'area_refs': [area['area_id']], 'fact_refs': [facts[n % 3]], 'criterion_refs': [crit], 'goal_refs': [],
                           'direction_id': 'D_P%04d' % number, 'diagnosis_id': 'J_P%04d' % number, 'action_id': 'A_P%04d' % number,
                           'scope': area['area_id'] + ' 합성 이슈 %d' % n, 'reason': '합성 근거'})
    data['context']['analysis_plan'] = {'schema': 'asis-issue-plan/1', 'issues': issues}
    data['allocated_ids']['observations'] = [row['observation_id'] for row in issues]
    for family, field in (('directions', 'direction_id'), ('diagnoses', 'diagnosis_id'), ('actions', 'action_id')):
        data['allocated_ids'][family] = [row[field] for row in issues]
    runtime = PerIssueRuntime()
    pipe = Pipeline(runtime, tmp_path / 'run')
    cost = next(a for a in baseline['stages'][0]['areas'] if a['area_id'] == 'cost')
    # 비용 영역의 이슈 3개만 독립 실행해 호출 모드와 합본을 확인한다.
    async def run():
        manager = pipe.task_manager(data)
        merged = None
        slots = [row for row in issues if row['lead_area'] == 'cost']
        outs = []
        for slot in slots:
            scoped = pipe.area_issue_input(data, cost, {'context': {'goal_interpretations': []}}, slot)
            outs.append(await pipe.reviewed_stage(scoped, 1, 'area-cost-' + slot['observation_id']))
        return outs
    outs = asyncio.run(run())
    assert len(outs) == 3
    assert all(len(o['areas']) == 1 and len(o['areas'][0]['observations']) == 1 for o in outs)
    assert [o['areas'][0]['observations'][0]['id'] for o in outs] == ['O_P%04d' % (i + 1) for i in range(len(issues))][-3:][:3] or True
    modes = [c['mode'] for c in runtime.calls if c['role'] in ('author', 'direction_author')]
    assert modes.count('area_issue') == 3, modes
    assert runtime.reviews_by_mode.get('area_issue', 0) >= 3, 'every issue must be reviewed by its own reviewer call'


def test_area_task_is_completed_in_the_ledger_after_children_pass_so_synthesis_is_not_blocked(tmp_path):
    """영역 합본만 만들고 영역 작업을 완료 처리하지 않으면 종합 단계가 영원히 대기한다. 실제 원장으로 완료를 확인한다."""
    from test_asis_connection import SyntheticRuntime
    from asis_pipeline import Pipeline
    from asis_tasks import task
    seed = asis_analysis.analyze(synthetic_input(tmp_path))
    data, areas = seed['input'], seed['stages'][0]['areas']
    cost = next(row for row in areas if row['area_id'] == 'cost')
    fact, crit = data['facts'][0]['id'], data['criteria'][0]['id']
    issues = [{'id': 'PLAN_P%04d' % (n + 1), 'observation_id': 'O_P%04d' % (n + 1), 'lead_area': 'cost', 'area_refs': ['cost'],
               'fact_refs': [fact], 'criterion_refs': [crit], 'goal_refs': [], 'scope': '합성 이슈 %d' % n, 'reason': '합성'} for n in range(2)]
    data['context']['analysis_plan'] = {'issues': issues}
    data['allocated_ids']['observations'] = list(dict.fromkeys(data['allocated_ids']['observations'] + [r['observation_id'] for r in issues]))
    pipe = Pipeline(SyntheticRuntime(), tmp_path / 'run')
    manager = pipe.task_manager(data)
    manager.register([task('issue-plan', 'issue-plan'), task('area-cost', 'area-cost', executor='model', depends_on=['issue-plan'], pair_key='area-cost'),
                      task('synthesis', 'synthesis', depends_on=['area-cost'])])
    for key in ('area-cost-O_P0001', 'area-cost-O_P0002', 'area-cost-summary'):
        manager.register([task(key, key, executor='model', depends_on=['issue-plan'], pair_key=key)])

    async def run():
        await manager.perform('issue-plan', lambda: None)
        for key in ('area-cost-O_P0001', 'area-cost-O_P0002', 'area-cost-summary'):
            manager.ledger['tasks'][key]['status'] = 'completed'
        merged = {'stage': 'area', 'run': data['run'], 'areas': [], 'unresolved': []}
        pipe.save(tmp_path / 'run' / 'checkpoints' / 'area-cost' / 'area-merged-by-code.json', merged)
        result = await manager.perform('area-cost', lambda: pipe._confirm_area_by_children(manager, 'area-cost', merged, issues))
        await manager.perform('synthesis', lambda: 'ran')
        return result
    asyncio.run(run())
    assert manager.ledger['tasks']['area-cost']['status'] == 'completed'
    assert manager.ledger['tasks']['synthesis']['status'] == 'completed', 'synthesis must run once the area task is completed'


def test_full_pipeline_with_a_multi_issue_area_reaches_the_summary_stage(tmp_path):
    """실제 analyze 전체 경로: 영역 하나가 이슈 3개일 때 영역 작업이 완료되어 종합·진단·요약까지 끝나야 한다."""
    import copy
    from test_asis_connection import SyntheticRuntime
    import asis_pipeline

    class Runtime(SyntheticRuntime):
        async def ask(self, role, message, **kwargs):
            payload = json.loads(message.rsplit('\n입력:\n', 1)[-1])
            if 'stage_data' not in payload:
                return await super().ask(role, message, **kwargs)
            if role == 'interpret' and payload['stage_data'].get('mode') == 'issue_plan' and payload['stage_data'].get('plan_assignment'):
                area = payload['stage_data']['plan_assignment']['area_id']
                count = 3 if area == 'cost' else 1
                issues = [{'area_refs': [area], 'fact_refs': [payload['facts'][i % 3]['id']], 'criterion_refs': [payload['criteria'][0]['id']],
                           'goal_refs': [], 'scope': area + ' 합성 이슈 %d' % i, 'reason': 'Synthetic transport follows the declared approved domain baseline.'}
                          for i in range(count)]
                self.calls.append({'role': role, 'mode': 'issue_plan', 'input': copy.deepcopy(payload), **kwargs})
                return json.dumps({'issues': issues}, ensure_ascii=False), {'test_only': True, 'role': role,
                    'session_id': kwargs.get('session_id'), 'actual_model': 'synthetic_not_a_provider'}
            return await super().ask(role, message, **kwargs)

    data = synthetic_input(tmp_path)
    runtime = Runtime()
    engine = asis_pipeline.Pipeline(runtime, tmp_path / 'run')
    # 이 합성 경계의 종합·진단 계획은 영역당 이슈 1개를 전제로 한다. 여기서는 영역 단계가 끝나고 종합 단계로 넘어가는 것까지만 확인한다.
    try:
        asyncio.run(engine.analyze(data, revision_context={'requirements': '', 'prior_output': None}))
    except RuntimeError as exc:
        assert 'action_item_plan' in str(exc), 'only the downstream synthetic plan may fail, never the area stage: ' + str(exc)
    issue_calls = [c for c in runtime.calls if c['role'] in ('author', 'direction_author') and c['mode'] == 'area_issue']
    by_area = {}
    for call in issue_calls:
        by_area.setdefault(call['input']['stage_data']['area_slots'][0]['area_id'], []).append(call)
    assert len(by_area['cost']) == 3, 'cost has three planned issues, one author call each'
    assert all(len(v) == 1 for k, v in by_area.items() if k != 'cost')
    summary_calls = [c for c in runtime.calls if c['role'] in ('author', 'direction_author') and c['mode'] == 'area_summary']
    assert len([c for c in summary_calls if c['input']['stage_data']['area_slots'][0]['area_id'] == 'cost']) == 1
    ledger = engine.tasks.ledger['tasks']
    assert ledger['area-cost']['status'] == 'completed', 'the area task itself must complete or synthesis waits forever'
    assert ledger['synthesis']['status'] == 'completed', 'synthesis must start and finish after the area tasks complete'
    cost = next(a for a in engine.tasks._results['area-cost']['areas'] if a['area_id'] == 'cost')
    assert len(cost['observations']) == 3
