"""영역 작성 입력은 그 영역이 주담당인 이슈의 사실·기준만 전달한다(다른 영역 주담당 이슈는 그 영역이 작성한다)."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_analysis
from asis_pipeline import Pipeline
from test_asis_connection import synthetic_input


class _Pipe(Pipeline):
    def __init__(self):
        pass


def test_area_input_carries_only_facts_of_issues_led_by_that_area(tmp_path):
    result = asis_analysis.analyze(synthetic_input(tmp_path))
    data, stages = result['input'], result['stages']
    areas = {row['area_id']: row for row in stages[0]['areas']}
    facts = [row['id'] for row in data['facts']]
    own_fact, other_fact = facts[0], facts[1]
    issues = [
        {'id': 'PLAN_A', 'observation_id': 'O_A', 'lead_area': 'cost', 'area_refs': ['cost'], 'fact_refs': [own_fact], 'criterion_refs': [], 'scope': 'a', 'reason': 'a'},
        {'id': 'PLAN_B', 'observation_id': 'O_B', 'lead_area': 'network', 'area_refs': ['network', 'cost'], 'fact_refs': [other_fact], 'criterion_refs': [], 'scope': 'b', 'reason': 'b'},
    ]
    data['context']['analysis_plan'] = {'issues': issues}
    scoped = _Pipe().area_input(data, areas['cost'], {'context': {'goal_interpretations': []}})
    inline = set(scoped['context']['model_scope']['inline_fact_refs'])
    assert own_fact in inline, 'lead issue facts must be sent'
    assert other_fact not in inline or other_fact in set(areas['cost']['implication']['fact_refs']), \
        'facts of issues led by another area must not be sent unless the area implication cites them'
    assert [row['id'] for row in scoped['stage_data']['planned_observation_slots']] == ['PLAN_A']
    assert data['facts'] and len(data['context']['analysis_plan']['issues']) == 2, 'canonical input changed'
