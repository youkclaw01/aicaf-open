"""계획의 병렬 진입과 필수 통합 장벽을 실제 중앙 원장 계약으로 검증한다."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from asis_analysis import AREA_NAMES
from asis_tasks import initial_graph


def test_initial_graph_registers_independent_area_plans_before_global_plan_barrier():
    rows = {row['id']: row for row in initial_graph(AREA_NAMES)}
    units = ['issue-plan-area-' + area for area in AREA_NAMES]
    assert set(units) <= set(rows), 'a monolithic issue-plan still blocks every area before fan-out'
    assert rows['issue-plan']['depends_on'] == units
    assert 'issue-plan-review' not in rows, 'the model integration review was removed in favour of a code dedup rule'
    for identifier in units:
        assert rows[identifier]['depends_on'] == ['interpret']
        assert rows[identifier]['scheduling'] == 'parallel'
    assert all(rows['area-' + area]['depends_on'] == ['issue-plan'] for area in AREA_NAMES)


def test_plan_fans_out_under_preparation_gate_and_keeps_complete_canonical_inventory(tmp_path):
    import asyncio
    from copy import deepcopy
    import json
    import asis_analysis
    from asis_pipeline import Pipeline
    from test_asis_connection import SyntheticRuntime, synthetic_input

    class ScopedRuntime(SyntheticRuntime):
        def __init__(self):
            super().__init__()
            self.plan_views = []
            self.plan_active = 0
            self.plan_peak = 0

        async def ask(self, role, message, **kwargs):
            payload = json.loads(message.rsplit('\n입력:\n', 1)[-1])
            assignment = payload.get('stage_data', {}).get('plan_assignment')
            if role == 'interpret' and payload.get('stage_data', {}).get('mode') == 'issue_plan' and assignment:
                self.plan_views.append(payload)
                self.plan_active += 1
                self.plan_peak = max(self.plan_peak, self.plan_active)
                try:
                    await asyncio.sleep(0.02)
                    issues = [{'area_refs': [assignment['area_id']],
                        'fact_refs': [payload['facts'][0]['id']],
                        'criterion_refs': [payload['criteria'][0]['id']], 'goal_refs': [],
                        'scope': '합성 ' + assignment['area_id'] + ' 계획 단위',
                        'reason': '제공된 수집 근거와 공식 기준의 적용 범위를 확인한다.'}]
                    return json.dumps({'issues': issues}, ensure_ascii=False), {'test_only': True, 'role': role}
                finally:
                    self.plan_active -= 1
            return await super().ask(role, message, **kwargs)

    data = asis_analysis.analyze(synthetic_input(tmp_path))['input']
    before = deepcopy(data)
    runtime = ScopedRuntime()
    planned = asyncio.run(Pipeline(runtime, tmp_path/'fanout').plan_analysis(data))
    assert len(runtime.plan_views) >= len(AREA_NAMES), 'planning still has a single global model request'
    assert runtime.plan_peak > 1, 'independent area plans never overlap'
    assert {view['stage_data']['plan_assignment']['area_id'] for view in runtime.plan_views} == set(AREA_NAMES)
    assert {row['id'] for view in runtime.plan_views for row in view['facts']} == {row['id'] for row in data['facts']}
    assert {row['id'] for view in runtime.plan_views for row in view['criteria']} == {row['id'] for row in data['criteria']}
    assert all(len(view['facts']) < len(data['facts']) for view in runtime.plan_views)
    assert planned['facts'] == before['facts'] and data == before

def test_integration_defect_citing_other_area_evidence_extends_the_rewrite_scope(tmp_path):
    """통합 검토가 다른 영역의 기준·사실 ID를 근거로 지적하면 재작성 호출 범위에 그 근거가 포함된다."""
    from asis_plan import _extend_scope
    from test_asis_connection import synthetic_input
    data = synthetic_input(tmp_path)
    data['criteria'] = [{'id': 'WAF-AI-APP3', 'source_id': 's', 'quote': 'q', 'source_url': 'u'},
                        {'id': 'WAF-AI-APP1', 'source_id': 's', 'quote': 'q', 'source_url': 'u'}]
    fact_id = data['facts'][0]['id']
    scope = {'fact_refs': [], 'criterion_refs': ['WAF-AI-APP1'], 'goal_refs': []}
    defects = [{'where': '/issues/0/reason', 'why': '기준 WAF-AI-APP3와 사실 ' + fact_id + '가 제공되었다.', 'fix': 'x', 'quote': 'y'},
               {'where': '/issues/1', 'why': 'NOT_A_REAL_ID_999를 인용', 'fix': 'x', 'quote': 'y'}]
    extended = _extend_scope(data, scope, defects)
    assert 'WAF-AI-APP3' in extended['criterion_refs'] and fact_id in extended['fact_refs']
    assert extended['criterion_refs'].count('WAF-AI-APP1') == 1
    assert 'NOT_A_REAL_ID_999' not in extended['criterion_refs'] + extended['fact_refs']
    assert scope == {'fact_refs': [], 'criterion_refs': ['WAF-AI-APP1'], 'goal_refs': []}, 'input scope mutated'
