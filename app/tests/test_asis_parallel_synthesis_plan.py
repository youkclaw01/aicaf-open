"""종합 방향 계획은 영역별 소단위로 동시 실행하고, 통합 검토 뒤 코드가 ID를 확정한다."""
from pathlib import Path
import asyncio, json, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_analysis
from asis_pipeline import Pipeline
from test_asis_connection import SyntheticRuntime, synthetic_input


def test_synthesis_plan_runs_per_area_units_concurrently(tmp_path):
    active = {'now': 0, 'peak': 0}
    calls = []

    class Runtime(SyntheticRuntime):
        async def ask(self, role, message, **kwargs):
            payload = json.loads(message.rsplit('\n입력:\n', 1)[-1])
            if 'stage_data' not in payload:
                # 영역별 진단·과제 계획 호출(소형 입력)은 기본 합성 대역이 처리한다.
                return await super().ask(role, message, **kwargs)
            stage = payload['stage_data']
            if role == 'interpret' and stage.get('mode') == 'synthesis_plan' and stage.get('plan_assignment'):
                calls.append(stage['plan_assignment']['area_id'])
                active['now'] += 1; active['peak'] = max(active['peak'], active['now'])
                try:
                    await asyncio.sleep(.05)
                    issues = payload['context']['analysis_plan']['issues']
                    areas = stage['approved_areas']['areas']
                    assert issues and all(row['lead_area'] == stage['plan_assignment']['area_id'] for row in issues)
                    return json.dumps({'directions': [{'scope': '합성 ' + areas[0]['area_id'], 'reason': '영역 소단위 종합 방향',
                        'issue_refs': [r['id'] for r in issues], 'area_implication_refs': [a['implication']['id'] for a in areas],
                        'fact_refs': sorted({f for a in areas for f in a['implication']['fact_refs']})[:1] or [payload['facts'][0]['id']],
                        'criterion_refs': [payload['criteria'][0]['id']], 'goal_refs': []}]}, ensure_ascii=False), {'test_only': True}
                finally:
                    active['now'] -= 1
            return await super().ask(role, message, **kwargs)

    result = asyncio.run(Pipeline(Runtime(), tmp_path / 'run').analyze(synthetic_input(tmp_path)))
    assert len(calls) == len(set(calls)) >= 2, calls
    assert active['peak'] >= 2, 'area synthesis plans were not concurrent'
    assert result['stages'][1]['directions']
