"""참조 합집합과 실제 서술 범위를 구분해 모든 배정 이슈의 독립 판단 범위를 검토한다."""
from copy import deepcopy
from pathlib import Path
import asyncio
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
import asis_model_view as mv
import asis_synthesis as synthesis
from asis_pipeline import Pipeline
from test_asis_parallel_synthesis import synthesis_fixture, DirectionRuntime
from test_asis_synthesis_review_shards import _merged


def assigned_issues():
    return [{'id': 'PLAN_SCOPE_1', 'scope': '시간초과의 발생 구간과 재처리', 'reason': '시간초과 원인은 다른 응답과 구분한다.'},
            {'id': 'PLAN_SCOPE_2', 'scope': '요청 제한 응답과 유입·용량 대안', 'reason': '시간초과와 원인 및 조치가 같다고 가정하지 않는다.'},
            {'id': 'PLAN_SCOPE_3', 'scope': '호출 종료 주체와 업무 완료', 'reason': '별도 원인과 완료 검증 조건을 유지한다.'}]


def test_direction_sender_exposes_every_assigned_judgment_scope_not_just_the_lead_label():
    rows = assigned_issues()
    data = {'context': {'analysis_plan': {'issues': rows}},
            'stage_data': {'direction_assignment': {'slot': {'id': 'D_SCOPE', 'scope': rows[0]['scope'],
                'fact_refs': [], 'criterion_refs': [], 'issue_refs': [row['id'] for row in rows]}},
                'approved_areas': {'areas': [], 'unresolved': []}},
            'facts': [], 'criteria': [], 'goals': [], 'judgments': [], 'measurements': [],
            'allocated_ids': {'directions': ['D_SCOPE']}}
    before = deepcopy(data)
    sent = mv.direction_view(data, 'direction_author')
    assert sent['stage_data'].get('assigned_issue_scopes') == rows
    assert '모든 배정 이슈' in sent['stage_data']['direction_scope_rule']
    assert data == before


@pytest.mark.parametrize('shard_size', [1, 24])
def test_scope_coverage_metadata_survives_the_real_shard_sender(tmp_path, shard_size):
    data, plan = synthesis_fixture(tmp_path)
    issues = assigned_issues()
    data['context']['analysis_plan'] = {'issues': issues}
    slots = [{'id': 'D_0000', 'scope': issues[0]['scope'], 'reason': '공통 상위 방향이나 판단은 분리한다.',
              'issue_refs': [issues[0]['id'], issues[1]['id']]},
             {'id': 'D_0001', 'scope': issues[2]['scope'], 'reason': issues[2]['reason'], 'issue_refs': [issues[2]['id']]},
             {'id': 'D_0002', 'scope': '운영 경계', 'reason': '기존 별도 방향', 'issue_refs': [issues[2]['id']]}]
    data['context']['synthesis_plan'] = {'directions': slots}
    merged = {**_merged(3), 'run': data['run']}
    original = deepcopy(data)
    captured = []

    class ScopeRuntime(DirectionRuntime):
        async def ask(self, role, message, **kwargs):
            payload = json.loads(message.rsplit('\n입력:\n', 1)[-1])
            mode = payload['stage_data'].get('mode')
            if role == 'content_review' and mode != 'synthesis-integration':
                captured.append(deepcopy(payload))
            return await super().ask(role, message, **kwargs)

    pipe = Pipeline(ScopeRuntime(plan), tmp_path/'run')
    review = asyncio.run(synthesis._review_merged_synthesis(pipe, data, merged, shard_size=shard_size))
    assert review['verdict'] == 'pass' and captured
    expected = {row['id']: row for row in slots}
    source_issues = {row['id']: row for row in issues}
    for payload in captured:
        sd = payload['stage_data']
        visible = {row['id'] for row in sd['draft']['directions'] if not row.get('omitted')}
        coverage = sd.get('direction_issue_scope')
        assert coverage, 'the old sender discarded the analysis plan and never sent the assigned judgment scopes'
        assert {row['direction_id'] for row in coverage} == visible
        for row in coverage:
            slot = expected[row['direction_id']]
            assert row['issues'] == [source_issues[key] for key in slot['issue_refs']]
            assert row['assigned_scope'] == slot['scope']
        assert '참조 합집합' in sd['direction_scope_review_rule']
    assert data == original
