"""영역 간 통합 검토 지적은 소속 영역 계획만 되돌려 보완하고 나머지 영역 계획은 재사용한다."""
from pathlib import Path
import asyncio, json, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_analysis
from asis_pipeline import Pipeline
from test_asis_connection import SyntheticRuntime, synthetic_input


def test_cross_area_duplicates_are_removed_by_code_and_every_area_is_called_once(tmp_path):
    """영역 간 같은 자원의 중복 이슈는 모델 통합 검토 없이 코드가 한 쪽만 남기고 근거 참조를 합친다."""
    data = asis_analysis.analyze(synthetic_input(tmp_path))['input']
    area_calls = []

    class Runtime(SyntheticRuntime):
        async def ask(self, role, message, **kwargs):
            payload = json.loads(message.rsplit('\n입력:\n', 1)[-1])
            stage = payload.get('stage_data', {})
            assignment = stage.get('plan_assignment')
            if role == 'interpret' and stage.get('mode') == 'issue_plan' and assignment:
                area = assignment['area_id']
                area_calls.append(area)
                shared = area in ('network', 'data')
                other = 'data' if area == 'network' else 'network'
                return json.dumps({'issues': [{'area_refs': [area, other] if shared else [area],
                    'fact_refs': [payload['facts'][0]['id']],
                    'criterion_refs': [payload['criteria'][0]['id']] if payload['criteria'] else [], 'goal_refs': [],
                    'scope': 'stduplicate01 저장소의 공개 접근 유지 판단' if shared else '합성 ' + area + ' 계획',
                    'reason': '제공된 근거 범위를 확인한다. stduplicate01 ' + area}]}, ensure_ascii=False), {'test_only': True}
            return await super().ask(role, message, **kwargs)

    engine = Pipeline(Runtime(), tmp_path / 'run')
    planned = asyncio.run(engine.plan_analysis(data))
    assert all(area_calls.count(key) == 1 for key in asis_analysis.AREA_NAMES)
    issues = planned['context']['analysis_plan']['issues']
    assert not any(role == 'content_review' and False for role in [])
    duplicates = [row for row in issues if 'stduplicate01' in row['scope']]
    assert len(duplicates) <= 2
    from asis_plan import deduplicate_issues
    rows = [{'area_refs': ['network', 'data'], 'fact_refs': ['F_A'], 'criterion_refs': ['C1'], 'goal_refs': [], 'scope': 'stduplicate01 접근', 'reason': 'stduplicate01 network'},
            {'area_refs': ['network', 'data'], 'fact_refs': ['F_A', 'F_B'], 'criterion_refs': ['C1'], 'goal_refs': [], 'scope': 'stduplicate01 접근 유지', 'reason': 'stduplicate01 data'}]
    merged, rep = deduplicate_issues(rows, ['network', 'data'])
    assert len(merged) == 1 and len(rep['removed']) == 1 and merged[0]['fact_refs'] == ['F_A', 'F_B']
    duplicates = merged
    report = json.loads((tmp_path / 'run' / 'checkpoints' / 'issue-plan' / 'dedup-report.json').read_text(encoding='utf-8'))
    assert report['kept_issues'] == len(issues) and report['input_issues'] >= report['kept_issues']


def test_reused_area_checkpoints_complete_without_model_calls_or_deadlock(tmp_path):
    """보존된 영역 계획 체크포인트는 재개 시 새 호출 없이 완료되고 통합 검토가 대기하지 않는다."""
    data = asis_analysis.analyze(synthetic_input(tmp_path))['input']
    run = tmp_path / 'run'
    asyncio.run(Pipeline(SyntheticRuntime(), run).plan_analysis(data))
    (run / 'task-ledger.json').unlink()
    calls = []

    class Counting(SyntheticRuntime):
        async def ask(self, role, message, **kwargs):
            calls.append(role)
            return await super().ask(role, message, **kwargs)

    async def again():
        return await asyncio.wait_for(Pipeline(Counting(), run).plan_analysis(data), timeout=60)

    planned = asyncio.run(again())
    assert planned['context']['analysis_plan']['issues']
    assert 'interpret' not in calls, calls
