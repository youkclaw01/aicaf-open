"""병합 후 통합 검토의 연장 규칙: 기본 보완 횟수 이후에는 지적 수가 지금까지의 최소 이하일 때만 최대 2배까지 연장하고, 늘어나면 연장하지 않는다.
운영 실측: 지적 수가 5,6,8 / 3,4 / 3,8,1로 오가다 3라운드 예산이 소진되어 개정이 중단됐다(synthesis_merge_revision_budget_exhausted)."""
from pathlib import Path
import asyncio, json, sys
from copy import deepcopy
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from asis_pipeline import Pipeline
from test_asis_parallel_synthesis import DirectionRuntime, synthesis_fixture


def build(plan, counts):
    """통합 검토가 라운드마다 counts[i]개 지적(방향 0·1의 제목)을 내고, counts가 끝나면 통과시킨다."""
    class Runtime(DirectionRuntime):
        def __init__(self, plan):
            super().__init__(plan)
            self.whole = 0

        async def ask(self, role, message, **kwargs):
            payload = json.loads(message.rsplit('\n입력:\n', 1)[-1]); stage = payload['stage_data']
            if role == 'content_review' and stage.get('mode') == 'synthesis' and not stage.get('direction_assignment'):
                self.whole += 1
                self.calls.append({'role': role, 'input': deepcopy(payload), **kwargs})
                if self.whole <= len(counts):
                    rows = stage['draft']['directions']
                    defects = [{'where': '/directions/%d/title' % i, 'quote': rows[i]['title'], 'why': '다른 방향과 판단이 중복된다.',
                                'fix': '고유한 판단으로 구분한다.', 'evidence_refs': []} for i in range(counts[self.whole - 1])]
                    value = {'stage': 'review', 'run': payload['run'], 'verdict': 'revise', 'content_checked': True, 'render_checked': False,
                             'defects': defects, 'unresolved': []}
                    return json.dumps(value, ensure_ascii=False), {'test_only': True, 'role': role}
            raw, receipt = await super().ask(role, message, **kwargs)
            if role in ('author', 'direction_author') and stage.get('prior_output'):
                candidate = json.loads(raw)
                candidate['directions'][0]['title'] = candidate['directions'][0]['title'] + '(구분)' * 1
                raw = json.dumps(candidate, ensure_ascii=False)
            return raw, receipt
    return Runtime(plan)


def run(tmp_path, counts):
    data, plan = synthesis_fixture(tmp_path)
    runtime = build(plan, counts)
    pipe = Pipeline(runtime, tmp_path / 'run')
    return runtime, pipe, data


def test_extends_beyond_the_old_cap_while_the_best_defect_count_keeps_improving(tmp_path):
    """운영 4,5,4,2,1,3처럼 최저 기록이 갱신되는 동안은 기존 6회 상한을 넘겨서도 계속한다."""
    runtime, pipe, data = run(tmp_path, [2, 2, 2, 1, 1, 1, 1, 1, 1])
    with pytest.raises(RuntimeError, match='synthesis_merge_revision_budget_exhausted'):
        asyncio.run(pipe.reviewed_synthesis(data))
    assert runtime.whole == 7, 'best=2 (round 1), new best=1 (round 4); stalled 3 rounds after round 4 -> stops after round 7: ' + str(runtime.whole)


def test_stops_when_the_best_count_has_not_improved_for_three_rounds(tmp_path):
    runtime, pipe, data = run(tmp_path, [1, 1, 1, 1, 1, 1, 1, 1])
    with pytest.raises(RuntimeError, match='synthesis_merge_revision_budget_exhausted'):
        asyncio.run(pipe.reviewed_synthesis(data))
    assert runtime.whole == 4, 'no new best for three rounds after the first: stop after round 4: ' + str(runtime.whole)


def test_a_late_pass_inside_the_extension_succeeds(tmp_path):
    runtime, pipe, data = run(tmp_path, [2, 2, 2, 1, 1])
    result = asyncio.run(pipe.reviewed_synthesis(data))
    assert result['directions'] and runtime.whole == 6


def test_stall_rule_unit():
    from asis_synthesis import merge_stalled
    assert merge_stalled([], 3) is False
    assert merge_stalled([4, 5, 4, 2, 1, 3], 3) is False, 'the real production history had not stalled'
    assert merge_stalled([2, 2, 2, 2], 3) is True
    assert merge_stalled([3, 2, 2, 2, 2], 3) is True
    assert merge_stalled([3, 2, 1, 3, 3, 3], 3) is True


def test_a_pass_within_the_base_rounds_is_unchanged(tmp_path):
    runtime, pipe, data = run(tmp_path, [1])
    asyncio.run(pipe.reviewed_synthesis(data))
    assert runtime.whole == 2


def test_a_direction_flagged_in_several_merge_rounds_gets_a_fresh_repair_budget_each_round(tmp_path):
    """운영 D_P0203: 방향 작성·검토 쌍이 라운드마다 한 번씩 보완을 요구받아도 예산을 이어 쓰지 않고 새로 받아야 한다."""
    data, plan = synthesis_fixture(tmp_path)

    class Strict(build(plan, [1, 1, 1]).__class__):
        def __init__(self, plan):
            super().__init__(plan)
            self.revised_for = None

        async def ask(self, role, message, **kwargs):
            payload = json.loads(message.rsplit('\n입력:\n', 1)[-1]); stage = payload['stage_data']
            if (role == 'content_review' and stage.get('direction_assignment') and self.revised_for != self.whole
                    and stage.get('draft', {}).get('directions')):
                self.revised_for = self.whole
                self.calls.append({'role': role, 'input': deepcopy(payload), **kwargs})
                row = stage['draft']['directions'][0]
                value = {'stage': 'review', 'run': payload['run'], 'verdict': 'revise', 'content_checked': True, 'render_checked': False,
                         'defects': [{'where': '/directions/0/title', 'quote': row['title'], 'why': '표현을 보완한다.', 'fix': '고친다.', 'evidence_refs': []}],
                         'unresolved': []}
                return json.dumps(value, ensure_ascii=False), {'test_only': True, 'role': role}
            return await super().ask(role, message, **kwargs)

    runtime = Strict(plan)
    pipe = Pipeline(runtime, tmp_path / 'run')
    result = asyncio.run(pipe.reviewed_synthesis(data))
    assert result['directions'] and runtime.whole == 4, 'three merge repairs must all be allowed to finish: ' + str(runtime.whole)
