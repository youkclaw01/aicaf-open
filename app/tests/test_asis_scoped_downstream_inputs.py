"""종합 계획·진단과제 계획의 모델 전송 입력은 인용 근거로 투영되고, 정본·검증 범위는 줄지 않는다."""
from pathlib import Path
import asyncio, json, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_analysis
from asis_pipeline import Pipeline
from test_asis_connection import SyntheticRuntime, synthetic_input


def test_downstream_plan_requests_are_projected_not_whole_ledger(tmp_path):
    seen = {}
    class Runtime(SyntheticRuntime):
        async def ask(self, role, message, **kwargs):
            payload = json.loads(message.rsplit('\n입력:\n', 1)[-1])
            if 'stage_data' not in payload:
                seen.setdefault('action_item_plan', (len(message.encode('utf-8')), 0, 0))
                return await super().ask(role, message, **kwargs)
            mode = payload['stage_data'].get('mode')
            if mode in ('synthesis_plan', 'action_item_plan'):
                seen[mode] = (len(message.encode('utf-8')), len(payload['facts']), len(payload['criteria']))
            return await super().ask(role, message, **kwargs)
    result = asyncio.run(Pipeline(Runtime(), tmp_path / 'run').analyze(synthetic_input(tmp_path)))
    # 정본은 분석 후 원장의 전체 사실·기준을 유지한다. 전송은 인용된 범위만이다.
    full = (len(result['input']['facts']), len(result['input']['criteria']))
    assert full[1] > 0 and {'synthesis_plan', 'action_item_plan'} <= set(seen)
    for mode, (_, facts, criteria) in seen.items():
        assert facts <= full[0] and criteria <= full[1]
        assert criteria < full[1], mode + ' still sends every official criterion'


def test_synthesis_plan_request_omits_bulk_tables_and_keeps_reference_ids(tmp_path):
    """종합 범위 계획 호출은 대량 대조표·관찰 원문 없이 이슈·시사점 ID로 전수 연결한다."""
    seen = {}
    class Runtime(SyntheticRuntime):
        async def ask(self, role, message, **kwargs):
            payload = json.loads(message.rsplit('\n입력:\n', 1)[-1])
            if 'stage_data' not in payload:
                return await super().ask(role, message, **kwargs)
            if payload['stage_data'].get('mode') == 'synthesis_plan':
                seen.update(size=len(message.encode('utf-8')), context=sorted(payload['context']),
                            areas=payload['stage_data']['approved_areas']['areas'],
                            issues=payload['context']['analysis_plan']['issues'])
            return await super().ask(role, message, **kwargs)
    asyncio.run(Pipeline(Runtime(), tmp_path / 'run').analyze(synthetic_input(tmp_path)))
    assert seen
    for heavy in ('collection_basis_matches', 'criterion_assessments', 'collection_states'):
        assert heavy not in seen['context'], heavy
    assert all('observations' not in area and 'observation_ids' in area and area['implication']['id'] for area in seen['areas'])
    assert seen['issues'] and all(row['id'] for row in seen['issues'])
