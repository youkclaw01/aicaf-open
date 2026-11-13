"""종합 검토의 전송 전 문자 제한도 원문을 보존하는 구간 분할로 복구한다."""
from collections import Counter
from copy import deepcopy
from pathlib import Path
import asyncio
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asis_model_view as model_view
import asis_synthesis
from asis_model import digest
from asis_pipeline import Pipeline
from test_asis_model_view import heavy_data
from test_asis_parallel_synthesis import DirectionRuntime
from test_asis_synthesis_shard_view_is_sent import body, merged_for


def oversized_case(tmp_path):
    """현재 검토 계약을 만족하는 정본에서 방향별 인용 원문만 크게 만든다."""
    data, _ = heavy_data(tmp_path, extra_facts=60)
    for row in data['facts']:
        if row['id'].startswith('F_X_'):
            row['value'] = {'blob': ('원문근거-' + row['id']) * 4000}
    merged = merged_for(data, count=27)
    for index, row in enumerate(merged['directions']):
        row['fact_refs'] = ['F_X_%03d' % (index * 2), 'F_X_%03d' % (index * 2 + 1)]
    seen = []

    class Runtime(DirectionRuntime):
        async def ask(self, role, message, **kwargs):
            payload = body(message)
            mode = str(payload['stage_data'].get('mode'))
            if role == 'content_review' and mode.startswith('synthesis-'):
                seen.append(deepcopy(payload))
            return await super().ask(role, message, **kwargs)

    pipe = Pipeline(Runtime({'directions': []}), tmp_path / 'run')
    return data, merged, pipe, seen


def test_character_preflight_splits_without_losing_directions_or_cited_fact_bodies(tmp_path):
    data, merged, pipe, seen = oversized_case(tmp_path)
    before = digest(data), digest(merged), dict(model_view.LIMITS)
    review = asyncio.run(asis_synthesis._review_merged_synthesis(pipe, data, merged))

    assert review['verdict'] == 'pass'
    shards = [value for value in seen if value['stage_data']['mode'].startswith('synthesis-shard-')]
    assert len(shards) > 2, '실제 전송 전 크기 검사로 큰 구간이 다시 분할되어야 한다'
    checked = Counter()
    facts = {row['id']: row for row in data['facts']}
    for payload in shards:
        assert model_view._chars(payload) <= model_view.LIMITS['synthesis_shard_review']
        supplied = {row['id']: row for row in payload['facts']}
        for row in payload['stage_data']['draft']['directions']:
            if row.get('omitted'):
                continue
            checked[row['id']] += 1
            for identifier in row['fact_refs']:
                assert supplied[identifier]['value'] == facts[identifier]['value'], '인용 원문은 발췌하지 않는다'
    assert checked == Counter({row['id']: 1 for row in merged['directions']})
    assert sum(value['stage_data']['mode'] == 'synthesis-integration' for value in seen) == 1
    assert (digest(data), digest(merged), model_view.LIMITS) == before


def test_split_parent_keeps_failure_history_without_remaining_an_active_failed_review(tmp_path):
    data, merged, pipe, _ = oversized_case(tmp_path)
    asyncio.run(asis_synthesis._review_merged_synthesis(pipe, data, merged))
    manager = pipe.task_manager(data)
    tasks = manager.ledger['tasks']
    assert not manager.progress()['blocked_tasks'], '분할 전 크기 거절 작업이 활성 실패로 남으면 안 된다'
    replaced = [row for row in tasks.values() if row['status'] == 'superseded']
    assert replaced
    for row in replaced:
        assert row['attempts'][-1]['status'] == 'failed', '원래 실패 이력을 지우지 않는다'
        assert not row.get('completion') and not row['reports'], '검토하지 않은 큰 입력을 통과로 바꾸지 않는다'
        assert row['supersession']['reason'] == 'review_input_partitioned'
        assert row['supersession']['replacements']
        for identifier in row['supersession']['replacements']:
            assert tasks[identifier]['status'] == 'completed'
            assert tasks[identifier]['reports']['reviewer']['checks']['verdict'] == 'pass'
