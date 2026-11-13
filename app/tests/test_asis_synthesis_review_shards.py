"""방향이 많은 종합 병합 후보는 묶음별 검토를 동시에 실행하고, 위치 보존과 통합 검토를 유지한다."""
from pathlib import Path
from copy import deepcopy
import asyncio, json, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_synthesis
from test_asis_parallel_synthesis import synthesis_fixture, DirectionRuntime
from asis_pipeline import Pipeline


def _merged(count):
    rows = [{'id': 'D_%04d' % i, 'title': '제목 %d' % i, 'text': '본문 ' * 200, 'customer_impact': '영향', 'change_direction': '방향',
             'area_implication_refs': ['I_x'], 'fact_refs': ['F_1', 'F_2'], 'criterion_refs': [], 'goal_refs': []} for i in range(count)]
    return {'stage': 'synthesis', 'run': {}, 'directions': rows, 'edges': [{'from_id': 'F_1', 'to_id': 'D_0000', 'relation': 'supports'}], 'unresolved': []}


def test_sparse_and_brief_views_keep_positions_and_shrink():
    merged = _merged(60)
    sparse = asis_synthesis._sparse_directions(merged, range(24, 48))
    assert len(sparse['directions']) == 60
    assert all('text' in sparse['directions'][i] for i in range(24, 48))
    assert all(sparse['directions'][i] == {'id': 'D_%04d' % i, 'omitted': True} for i in list(range(24)) + list(range(48, 60)))
    brief = asis_synthesis._brief_directions(merged)
    assert [row['id'] for row in brief['directions']] == [row['id'] for row in merged['directions']]
    assert len(json.dumps(brief)) < len(json.dumps(merged)) / 2


def test_many_directions_are_reviewed_by_concurrent_shards_plus_integration(tmp_path):
    data, plan = synthesis_fixture(tmp_path)
    merged = _merged(60)
    seen = []
    state = {'now': 0, 'peak': 0}

    class Runtime(DirectionRuntime):
        async def ask(self, role, message, **kwargs):
            payload = json.loads(message.rsplit('\n입력:\n', 1)[-1])
            mode = payload['stage_data'].get('mode')
            if role == 'content_review' and str(mode).startswith('synthesis-'):
                seen.append((mode, len(message.encode('utf-8'))))
                state['now'] += 1; state['peak'] = max(state['peak'], state['now'])
                try:
                    await asyncio.sleep(.05)
                finally:
                    state['now'] -= 1
            return await super().ask(role, message, **kwargs)

    pipe = Pipeline(Runtime(plan), tmp_path / 'run')
    review = asyncio.run(asis_synthesis._review_merged_synthesis(pipe, data, {**merged, 'run': data['run']}))
    modes = [mode for mode, _ in seen]
    assert sum(1 for m in modes if m.startswith('synthesis-shard-')) == 3 and 'synthesis-integration' in modes, modes
    assert state['peak'] >= 2
    assert review['verdict'] == 'pass'


def test_a_shard_over_the_prompt_budget_is_split_in_half_and_reviewed(tmp_path):
    data, plan = synthesis_fixture(tmp_path)
    merged = _merged(60)
    keys = []

    class Runtime(DirectionRuntime):
        async def ask(self, role, message, **kwargs):
            payload = json.loads(message.rsplit('\n입력:\n', 1)[-1])
            mode = str(payload['stage_data'].get('mode'))
            if role == 'content_review' and mode == 'synthesis-shard-0000':
                raise RuntimeError("Request exceeds the model's prompt budget after reserving output (274848 tokens; limit 272000)")
            if role == 'content_review' and mode.startswith('synthesis-shard-'):
                keys.append(mode)
            return await super().ask(role, message, **kwargs)

    pipe = Pipeline(Runtime(plan), tmp_path / 'run')
    review = asyncio.run(asis_synthesis._review_merged_synthesis(pipe, data, {**merged, 'run': data['run']}))
    assert review['verdict'] == 'pass'
    assert 'synthesis-shard-0000-n12' in keys and 'synthesis-shard-0012-n12' in keys and 'synthesis-shard-0024' in keys, keys
