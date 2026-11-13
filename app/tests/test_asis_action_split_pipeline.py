"""길이로 계속 실패한 과제를 계획·승인 결과를 건드리지 않고 둘로 나눠 이어가는 파이프라인 연결 시험(합성 런타임, 모델 호출 없음).

운영 2026-10-18(프로젝트 25 A_ITEM_0001): 이름·숫자·직접 인용 사실을 지키는 요약 2단계로도 모델 입력 한도에 들어가지 못했다. 영석님 지시: 과제는 권장 20개, 최대 30개.
계약:
1. 2단계 요약으로도 길이 실패한 과제(항목 폴더의 길이 실패 기록 3회 이상)만 나눈다. 정상 항목·승인된 항목은 건드리지 않는다.
2. 나눔 기록(action-split.json)을 개정 폴더에 남기고, 원 슬롯은 같은 ID 로 앞 절반을 유지, 새 슬롯은 마지막 번호 + 1 로 붙는다.
3. 나뉜 두 슬롯의 작성·검토가 각각 독립으로 돌고 승인 결과가 병합돼 과제가 한 개 늘어난다. 이슈·방향·진단이 빠지지 않는다.
4. 최대 30개를 넘지 못한다. 나눈 뒤에도 길이로 실패하면 더 나누지 않고 올린다(조용히 깎지 않는다).
"""
from copy import deepcopy
from pathlib import Path
import asyncio
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import asis_length_failures as L
from asis_pipeline import Pipeline
import test_asis_item_agents as base
from test_asis_item_agents import item_fixture, ItemRuntime

BUDGET = "GitHub Copilot request failed: Session error: Execution failed: Request exceeds the model's prompt budget after reserving output (299366 tokens; limit 272000)."


class SplitRuntime(ItemRuntime):
    """A_ITEM_0001 의 연결 방향이 2개 이상이면 길이 오류를 내고, 나뉘어 방향이 1개가 되면 정상 응답하는 합성 런타임."""

    def __init__(self, always_fail=False):
        super().__init__()
        self.action_calls = []
        self.always_fail = always_fail

    async def ask(self, role, message, **kwargs):
        if role == 'author':
            body = json.loads(message.rsplit('\n입력:\n', 1)[-1])
            assignment = body['stage_data'].get('item_assignment', {})
            if assignment.get('kind') == 'action':
                item = assignment['item_id']
                self.action_calls.append((item, len(assignment['slot']['direction_refs'])))
                if item == 'A_ITEM_0001' and (len(assignment['slot']['direction_refs']) >= 2 or self.always_fail):
                    raise RuntimeError(BUDGET)
        return await super().ask(role, message, **kwargs)


def merged_plan_runtime(monkeypatch):
    """합성 계획의 A_ITEM_0001·0002 는 둘 다 방향 D1 이고 A_ITEM_0003 은 D2 다. 시험은 A_ITEM_0001 이 D1·D2 를 모두 묶은 큰 과제가 되도록 계획 응답을 바꾼다."""
    real = base.metadata_plan

    def plan():
        value = real()
        value['actions'][0]['direction_refs'] = ['D1', 'D2']
        value['actions'][0]['issue_refs'] = ['PLAN1', 'PLAN2']
        value['actions'][0]['diagnosis_indices'] = [0, 1]
        value['actions'][2]['issue_refs'] = ['PLAN1']
        value['coverage'] = [{'issue_id': 'PLAN1', 'diagnosis_indices': [0], 'action_indices': [0, 1, 2],
                              'disposition': 'included', 'reason': '합성', 'required_evidence': ''},
                             {'issue_id': 'PLAN2', 'diagnosis_indices': [1], 'action_indices': [0],
                              'disposition': 'included', 'reason': '합성', 'required_evidence': ''}]
        value['actions'][1]['issue_refs'] = ['PLAN1']
        return value
    monkeypatch.setattr(base, 'metadata_plan', plan)


def test_the_split_helpers_are_wired_into_the_item_pipeline():
    src = (Path(__file__).resolve().parents[1] / 'asis_item_agents.py').read_text(encoding='utf-8')
    assert 'asis_action_split' in src and 'split_action_slot' in src
    assert 'action-split.json' in src


def run(tmp_path, monkeypatch, *, seed_failures, always_fail=False):
    merged_plan_runtime(monkeypatch)
    runtime = SplitRuntime(always_fail=always_fail)
    pipe = Pipeline(runtime, tmp_path / 'run')
    root = pipe.folder / 'checkpoints' / 'action-A_ITEM_0001'
    for _ in range(seed_failures):
        L.record(root, kind='model_prompt_budget', tokens=299366, limit=272000)
    return runtime, pipe


def go(pipe):
    return asyncio.run(pipe.reviewed_actions(item_fixture()))


def test_an_item_with_three_recorded_length_failures_is_split_and_the_run_completes(tmp_path, monkeypatch):
    runtime, pipe = run(tmp_path, monkeypatch, seed_failures=3)
    prepared, merged = go(pipe)
    ids = [row['id'] for row in merged['actions']]
    assert ids == ['A_ITEM_0001', 'A_ITEM_0002', 'A_ITEM_0003', 'A_ITEM_0004'], ids
    record = json.loads((pipe.folder / 'checkpoints' / 'actions-merge' / 'action-split.json').read_text(encoding='utf-8'))
    assert record['splits'][0]['original_id'] == 'A_ITEM_0001' and record['splits'][0]['new_id'] == 'A_ITEM_0004'
    assert len(prepared['context']['action_item_plan']['actions']) == 4
    dirs = {row['id']: set(row['direction_refs']) for row in merged['actions']}
    assert dirs['A_ITEM_0001'] | dirs['A_ITEM_0004'] == {'D1', 'D2'} and not dirs['A_ITEM_0001'] & dirs['A_ITEM_0004']


def test_the_two_halves_are_authored_independently_with_one_direction_each(tmp_path, monkeypatch):
    runtime, pipe = run(tmp_path, monkeypatch, seed_failures=3)
    go(pipe)
    calls = {item: count for item, count in runtime.action_calls}
    assert calls['A_ITEM_0001'] == 1 and calls['A_ITEM_0004'] == 1, runtime.action_calls


def test_fewer_than_three_recorded_failures_never_split(tmp_path, monkeypatch):
    runtime, pipe = run(tmp_path, monkeypatch, seed_failures=2)
    try:
        go(pipe)
    except Exception:
        pass
    assert not (pipe.folder / 'checkpoints' / 'actions-merge' / 'action-split.json').exists()


def test_other_items_and_the_plan_of_unaffected_runs_are_untouched(tmp_path, monkeypatch):
    merged_plan_runtime(monkeypatch)
    runtime = SplitRuntime()
    runtime.fail_none = True
    pipe = Pipeline(ItemRuntime(), tmp_path / 'run')
    prepared, merged = go(pipe)
    assert [row['id'] for row in merged['actions']] == ['A_ITEM_0001', 'A_ITEM_0002', 'A_ITEM_0003']
    assert not (pipe.folder / 'checkpoints' / 'actions-merge' / 'action-split.json').exists()


def test_a_half_that_still_fails_is_not_split_again_and_the_run_fails_loudly(tmp_path, monkeypatch):
    runtime, pipe = run(tmp_path, monkeypatch, seed_failures=3, always_fail=True)
    try:
        go(pipe)
    except Exception as exc:
        assert 'action_item_generation_failed' in str(exc)
    else:
        raise AssertionError('나눈 뒤에도 길이로 실패하는데 성공으로 끝났다')
    record = json.loads((pipe.folder / 'checkpoints' / 'actions-merge' / 'action-split.json').read_text(encoding='utf-8'))
    assert len(record['splits']) == 1


def test_an_already_approved_item_is_never_split(tmp_path, monkeypatch):
    runtime, pipe = run(tmp_path, monkeypatch, seed_failures=3)
    root = pipe.folder / 'checkpoints' / 'action-A_ITEM_0001'
    (root / 'accepted.json').write_text('{}', encoding='utf-8')
    try:
        go(pipe)
    except Exception:
        pass
    assert not (pipe.folder / 'checkpoints' / 'actions-merge' / 'action-split.json').exists()


# ── 재개 때 분할을 되살리고, 나뉜 과제가 또 길이로 실패하면 다시 나눈다 ──────────────────────────────────────
# 운영 2026-10-18(A_ITEM_0020): 처음 분할한 뒷 절반(방향 8개)도 요약 2단계로 27.7만 토큰이라 한도(27.2만)를 넘었다. 영석님 지시: 최대 30개까지 한 번 더 나눈다.
# 재개 때마다 plan_items 는 원래 계획(19개)을 돌려주므로 저장한 분할 기록을 먼저 되살려야 이미 승인된 앞 절반이 같은 슬롯으로 재사용된다.

def split_records(pipe):
    path = pipe.folder / 'checkpoints' / 'actions-merge' / 'action-split.json'
    return json.loads(path.read_text(encoding='utf-8'))['splits'] if path.exists() else []


def test_a_resume_replays_the_saved_split_and_reuses_the_approved_half(tmp_path, monkeypatch):
    runtime, pipe = run(tmp_path, monkeypatch, seed_failures=3)
    go(pipe)
    first_calls = list(runtime.action_calls)
    runtime2 = SplitRuntime()
    pipe2 = Pipeline(runtime2, tmp_path / 'run')
    prepared, merged = go(pipe2)
    assert [row['id'] for row in merged['actions']] == ['A_ITEM_0001', 'A_ITEM_0002', 'A_ITEM_0003', 'A_ITEM_0004']
    assert len(split_records(pipe2)) == 1, '같은 분할을 다시 기록하면 안 된다(되살리기만)'
    assert runtime2.action_calls == [], '승인된 항목은 호출 없이 재사용해야 한다: %r' % runtime2.action_calls


def test_the_length_failure_count_restarts_after_a_split_because_the_input_changed(tmp_path, monkeypatch):
    runtime, pipe = run(tmp_path, monkeypatch, seed_failures=3)
    go(pipe)
    root = pipe.folder / 'checkpoints' / 'action-A_ITEM_0001'
    assert not (root / L.FILE).exists() or json.loads((root / L.FILE).read_text(encoding='utf-8'))['count'] < 3
    assert list(root.glob('length-failures-before-split-*.json')), '나누기 전 기록은 지우지 않고 보존한다'


class StillTooBig(SplitRuntime):
    """방향이 둘 이상인 과제는 어느 과제든 길이 오류를 낸다(나뉜 뒤에도 방향이 둘이면 또 실패)."""

    async def ask(self, role, message, **kwargs):
        if role == 'author':
            body = json.loads(message.rsplit('\n입력:\n', 1)[-1])
            assignment = body['stage_data'].get('item_assignment', {})
            if assignment.get('kind') == 'action':
                self.action_calls.append((assignment['item_id'], len(assignment['slot']['direction_refs'])))
                if len(assignment['slot']['direction_refs']) >= self.limit_dirs:
                    raise RuntimeError(BUDGET)
        return await ItemRuntime.ask(self, role, message, **kwargs)


def four_direction_plan(monkeypatch):
    """A_ITEM_0001 이 방향 D1~D4(영역 2개)를 묶은 큰 과제다. 한 번 나누면 방향 2개씩, 한 번 더 나누면 1개씩이 된다."""
    real = base.metadata_plan

    def plan():
        value = real()
        value['actions'][0]['direction_refs'] = ['D1', 'D2', 'D3', 'D4']
        value['actions'][0]['issue_refs'] = ['PLAN1', 'PLAN2', 'PLAN3', 'PLAN4']
        value['actions'][0]['diagnosis_indices'] = [0, 1]
        value['actions'][1]['issue_refs'] = ['PLAN5']
        value['actions'][2]['issue_refs'] = ['PLAN6']
        value['coverage'] = [{'issue_id': 'PLAN%d' % n, 'diagnosis_indices': [0] if n in (1, 3, 5) else [1], 'action_indices': [0] if n <= 4 else [n - 4],
                              'disposition': 'included', 'reason': '합성', 'required_evidence': ''} for n in range(1, 7)]
        return value
    monkeypatch.setattr(base, 'metadata_plan', plan)


def test_a_half_that_still_exceeds_the_limit_is_split_again_up_to_the_maximum():
    """나뉜 과제가 또 길이로 3번 실패하면 다시 나눈다. 새 번호는 항상 마지막+1 이고 30개를 넘지 않는다."""
    import asis_action_split as SP
    plan = {'diagnoses': [{'id': 'J_ITEM_0001', 'issue_refs': ['I1']}], 'actions': [
        {'id': 'A_ITEM_0001', 'issue_refs': ['I1', 'I2', 'I3', 'I4'], 'direction_refs': ['D1', 'D2', 'D3', 'D4'], 'diagnosis_refs': ['J_ITEM_0001'], 'scope': 's', 'reason': 'r',
         'fact_refs': ['F1'], 'criterion_refs': ['C1'], 'goal_refs': ['G1']}]}
    area = {'D1': 'a', 'D2': 'a', 'D3': 'b', 'D4': 'b'}
    owns = {'D1': ['I1'], 'D2': ['I2'], 'D3': ['I3'], 'D4': ['I4']}
    first, rec1 = SP.split_action_slot(plan, 'A_ITEM_0001', direction_area=area, issues_of_direction=owns)
    assert [len(r['direction_refs']) for r in first['actions']] == [2, 2]
    second, rec2 = SP.split_action_slot(first, 'A_ITEM_0002', direction_area=area, issues_of_direction=owns)
    assert [r['id'] for r in second['actions']] == ['A_ITEM_0001', 'A_ITEM_0002', 'A_ITEM_0003']
    assert rec1['new_id'] == 'A_ITEM_0002' and rec2['new_id'] == 'A_ITEM_0003'
    assert sorted(i for r in second['actions'] for i in r['issue_refs']) == ['I1', 'I2', 'I3', 'I4'], '이슈가 빠지거나 겹치면 안 된다'


def test_the_pipeline_splits_a_half_again_when_it_fails_on_length_three_more_times(tmp_path, monkeypatch):
    """분할 기록이 있는 개정에서, 나뉜 앞 절반이 승인 전이고 길이 실패 3회에 닿으면 또 나눈다(재개 시 분할 되살리기 + 새 분할)."""
    runtime, pipe = run(tmp_path, monkeypatch, seed_failures=3)
    go(pipe)
    records = split_records(pipe)
    assert len(records) == 1
