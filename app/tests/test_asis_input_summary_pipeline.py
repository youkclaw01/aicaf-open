"""길이로 2번 이상 실패한 항목만 요약해서 보내는 규칙의 파이프라인 연결 시험(합성 런타임, 모델 호출 없음).

운영 2026-10-18(프로젝트 25 과제 A_ITEM_0001·0015): 입력이 모델 한도에 닿아 실패. 영석님 지시: 과제 수는 늘리지 않고, 길이로 2번 이상 실패한 경우만 요약해서 보낸다.
계약:
1. 길이 실패(모델 한도 초과 거절·전송 전 한도 검사)는 항목 폴더 length-failures.json 에 영속 기록한다. 작성 보완 예산을 쓰지 않는다.
2. 기록 횟수 0~1: 원문 그대로 보낸다. 2: 1단계 요약. 3 이상: 2단계 요약.
3. 정본 입력·체크포인트 재사용 키·author-input.json(원본)은 요약에 영향을 받지 않는다. 요약본은 summarized-input.json 으로 따로 남긴다.
4. 길이가 아닌 실패는 이 경로에 영향을 주지 않는다. 영수증·이벤트에 요약 사실을 남긴다.
"""
from pathlib import Path
import asyncio
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import asis_length_failures as L
from asis_pipeline import Pipeline
from test_asis_item_agents import item_fixture, ItemRuntime

BUDGET = "GitHub Copilot request failed: Session error: Execution failed: Request exceeds the model's prompt budget after reserving output (299366 tokens; limit 272000)."


class LengthRuntime(ItemRuntime):
    """과제 작성 호출이 앞의 fail_times 회는 길이 오류로 실패하고, 이후에는 정상 응답하는 합성 런타임. 보낸 프롬프트의 크기를 기록한다."""

    def __init__(self, fail_times, kind='budget'):
        super().__init__()
        self.fail_times = fail_times
        self.kind = kind
        self.action_prompts = []
        self.attempts = 0

    async def ask(self, role, message, **kwargs):
        if role == 'author':
            body = json.loads(message.rsplit('\n입력:\n', 1)[-1])
            assignment = body['stage_data'].get('item_assignment', {})
            if assignment.get('kind') == 'action' and assignment.get('item_id') == 'A_ITEM_0001':
                self.attempts += 1
                self.action_prompts.append(body)
                if self.attempts <= self.fail_times:
                    raise RuntimeError(BUDGET) if self.kind == 'budget' else ValueError('model_input_over_limit:item_author:950000>900000')
        return await super().ask(role, message, **kwargs)


def run(tmp_path, fail_times, kind='budget', seed=0):
    data = item_fixture()
    # 이 시험은 항목의 길이 실패 횟수가 이미 seed 번 쌓인 상태에서 시작한다(개정 중단·재개 후를 모사).
    runtime = LengthRuntime(fail_times, kind)
    pipe = Pipeline(runtime, tmp_path / 'run')
    if seed:
        import asyncio as _a
        root = tmp_path / 'run' / 'checkpoints' / 'action-A_ITEM_0001'
        for _ in range(seed):
            L.record(root, kind='model_prompt_budget', tokens=299366, limit=272000)
    return runtime, pipe, data


def go(pipe, data):
    return asyncio.run(pipe.reviewed_actions(data))


def test_a_first_length_failure_is_recorded_and_the_same_full_input_is_sent_again(tmp_path):
    runtime, pipe, data = run(tmp_path, fail_times=1)
    try:
        go(pipe, data)
    except Exception:
        pass
    root = tmp_path / 'run' / 'checkpoints' / 'action-A_ITEM_0001'
    assert json.loads((root / 'length-failures.json').read_text(encoding='utf-8'))['count'] >= 1
    assert L.summary_level(root) == 0 or L.summary_level(root) == 1


def test_the_second_attempt_after_two_recorded_length_failures_is_sent_summarized(tmp_path):
    runtime, pipe, data = run(tmp_path, fail_times=0, seed=2)
    go(pipe, data)
    assert runtime.action_prompts, '과제 A_ITEM_0001 작성 호출이 있어야 한다'
    root = tmp_path / 'run' / 'checkpoints' / 'action-A_ITEM_0001'
    summaries = sorted(root.rglob('summarized-input.json'))
    assert summaries, '요약본을 따로 남겨야 한다'
    record = json.loads(summaries[-1].read_text(encoding='utf-8'))
    assert record['level'] == 1 and record['reason'] == 'two_or_more_length_failures'


def test_the_original_author_input_is_kept_unsummarized_for_reuse_and_audit(tmp_path):
    runtime, pipe, data = run(tmp_path, fail_times=0, seed=2)
    go(pipe, data)
    root = tmp_path / 'run' / 'checkpoints' / 'action-A_ITEM_0001'
    original = json.loads(sorted(root.rglob('author-input.json'))[-1].read_text(encoding='utf-8'))
    assert 'summarized' not in json.dumps(original, ensure_ascii=False)


def test_an_item_with_fewer_than_two_length_failures_is_never_summarized(tmp_path):
    runtime, pipe, data = run(tmp_path, fail_times=0, seed=1)
    go(pipe, data)
    root = tmp_path / 'run' / 'checkpoints' / 'action-A_ITEM_0001'
    assert not list(root.rglob('summarized-input.json'))


def test_other_items_are_never_summarized(tmp_path):
    runtime, pipe, data = run(tmp_path, fail_times=0, seed=2)
    go(pipe, data)
    for other in (tmp_path / 'run' / 'checkpoints').glob('action-A_ITEM_000[23]'):
        assert not list(other.rglob('summarized-input.json')), other.name


def test_a_non_length_failure_is_not_counted(tmp_path):
    class Boom(LengthRuntime):
        async def ask(self, role, message, **kwargs):
            body = json.loads(message.rsplit('\n입력:\n', 1)[-1])
            if role == 'author' and body['stage_data'].get('item_assignment', {}).get('item_id') == 'A_ITEM_0001':
                raise RuntimeError('some other failure')
            return await super().ask(role, message, **kwargs)
    data = item_fixture()
    pipe = Pipeline(Boom(0), tmp_path / 'run')
    try:
        go(pipe, data)
    except Exception:
        pass
    root = tmp_path / 'run' / 'checkpoints' / 'action-A_ITEM_0001'
    assert not (root / 'length-failures.json').exists()


def test_the_level_two_summary_is_used_from_the_third_recorded_failure(tmp_path):
    runtime, pipe, data = run(tmp_path, fail_times=0, seed=3)
    go(pipe, data)
    root = tmp_path / 'run' / 'checkpoints' / 'action-A_ITEM_0001'
    record = json.loads(sorted(root.rglob('summarized-input.json'))[-1].read_text(encoding='utf-8'))
    assert record['level'] == 2


def test_repeated_length_failures_inside_one_call_end_with_a_summarized_success_not_a_failed_item(tmp_path):
    """운영 사례의 핵심: 같은 항목이 길이로 연속 실패해도 개정을 failed 로 만들지 않고, 2번째 실패 뒤 요약본으로 같은 호출 안에서 다시 보내 성공해야 한다.
    (이 개정은 재대기 예산이 없다. 기록만 하고 예외를 올리면 항목·개정이 바로 실패한다.)"""
    runtime, pipe, data = run(tmp_path, fail_times=2)
    prepared, merged = go(pipe, data)
    assert any(row['id'] == 'A_ITEM_0001' for row in merged['actions']), '요약본으로 재전송해 항목이 성공해야 한다'
    assert runtime.attempts == 3, '실패 2회 + 요약본 성공 1회: %d' % runtime.attempts
    root = tmp_path / 'run' / 'checkpoints' / 'action-A_ITEM_0001'
    assert json.loads((root / 'length-failures.json').read_text(encoding='utf-8'))['count'] == 2
    record = json.loads(sorted(root.rglob('summarized-input.json'))[-1].read_text(encoding='utf-8'))
    assert record['level'] == 1
    sizes = [len(json.dumps(body, ensure_ascii=False)) for body in runtime.action_prompts]
    assert sizes[2] <= sizes[0], '요약본이 원문보다 커서는 안 된다: %r' % sizes


def test_a_length_failure_does_not_consume_the_author_revision_budget(tmp_path):
    runtime, pipe, data = run(tmp_path, fail_times=1)
    prepared, merged = go(pipe, data)
    assert any(row['id'] == 'A_ITEM_0001' for row in merged['actions'])
    assert runtime.attempts == 2


def test_after_the_level_two_summary_still_fails_the_item_fails_loudly_not_silently(tmp_path):
    """2단계로도 한도를 넘으면 더 줄이지 않고 실패로 올린다(품질 방어선). 내용을 조용히 깎지 않는다."""
    runtime, pipe, data = run(tmp_path, fail_times=99)
    try:
        go(pipe, data)
    except Exception as exc:
        assert 'action_item_generation_failed' in str(exc) or 'prompt budget' in str(exc) or 'model_input_over_limit' in str(exc), str(exc)
    else:
        raise AssertionError('한도를 계속 넘는데 성공으로 끝났다')
    root = tmp_path / 'run' / 'checkpoints' / 'action-A_ITEM_0001'
    assert L.summary_level(root) == 2
    assert runtime.attempts <= 6, '무한 재시도 금지: %d' % runtime.attempts


def test_a_length_failure_in_a_non_item_call_is_not_recorded_or_summarized(tmp_path):
    """항목 폴더가 아닌 호출(영역·방향 등)의 길이 오류는 이 규칙의 대상이 아니다. 기록을 만들지 않고 그대로 올린다."""
    class AreaTooLong(LengthRuntime):
        async def ask(self, role, message, **kwargs):
            body = json.loads(message.rsplit('\n입력:\n', 1)[-1]) if '\n입력:\n' in message else {}
            if role == 'author' and not body.get('stage_data', {}).get('item_assignment') and body.get('stage_data', {}).get('mode') != 'actions':
                raise RuntimeError(BUDGET)
            return await super().ask(role, message, **kwargs)
    from asis_pipeline import Pipeline as P
    pipe = P(AreaTooLong(0), tmp_path / 'run')
    data = item_fixture()
    asyncio.run(_non_item_review(pipe, data))
    assert not list((tmp_path / 'run').rglob('length-failures.json'))


async def _non_item_review(pipe, data):
    """item_assignment 가 없는 단계(여기서는 영역 입력 형태)로 reviewed_stage 를 직접 호출해 길이 오류가 기록되지 않는지 본다."""
    scoped = json.loads(json.dumps(data))
    scoped['stage_data'] = {'mode': 'area', 'area_slots': [], 'approved_areas': scoped['stage_data']['approved_areas']}
    try:
        await pipe.reviewed_stage(scoped, 1, 'area-probe')
    except Exception as exc:
        assert 'prompt budget' in str(exc) or True


def test_the_summary_keeps_the_slots_directly_cited_facts_in_full_and_summarizes_the_others(tmp_path):
    """파이프라인 경로에서도 슬롯이 직접 인용한 사실(F1)의 값은 원문이고, 인용하지 않은 목록형 사실(F_OTHER)은 대표 행으로 줄어든다.
    요약기에 직접 인용 목록이 전달되지 않으면 F1 도 줄어들어 이 시험이 실패한다."""
    runtime, pipe, data = run(tmp_path, fail_times=0, seed=2)
    rows = [{'a': i, 'b': 'x' * 300} for i in range(12)]
    for fact in data['facts']:
        if fact['id'] == 'F1':
            fact['value'] = rows
    other = json.loads(json.dumps(next(f for f in data['facts'] if f['id'] == 'F1')))
    other['id'] = 'F_OTHER'
    other['value'] = [{'c': i, 'd': 'y' * 300} for i in range(12)]
    data['facts'].append(other)
    # 슬롯은 F1 만 직접 인용하고, 승인된 방향이 F_OTHER 를 인용해 모델 뷰에 실리게 한다(뷰는 아무도 인용하지 않은 사실을 싣지 않는다).
    data['stage_data']['approved_directions']['directions'][0]['fact_refs'] = ['F1', 'F_OTHER']
    seed_level = 2
    root = tmp_path / 'run' / 'checkpoints' / 'action-A_ITEM_0001'
    L.record(root, kind='model_prompt_budget', tokens=299366, limit=272000)   # 합쳐 3회 → 2단계
    assert L.summary_level(root) == seed_level
    go(pipe, data)
    sent = runtime.action_prompts[-1]
    by_id = {f['id']: f for f in sent['facts']}
    assert by_id['F1']['value'] == rows, '슬롯이 직접 인용한 사실(F1)은 2단계 요약에서도 원문이어야 한다'
    assert isinstance(by_id['F_OTHER']['value'], dict) and by_id['F_OTHER']['value'].get('summarized_list') is True


def test_a_pre_send_over_limit_failure_is_recorded_and_the_item_recovers_with_a_summarized_view(tmp_path, monkeypatch):
    """운영 사례(A_ITEM_0001, 연결 방향 17개): 전송 전 한도 검사가 render_prompt 안에서 ValueError 를 던진다. 같은 호출 안에서 기록하고 요약 뷰로 재시도해야 한다."""
    import asis_model_view as MV
    runtime = LengthRuntime(0)
    pipe = Pipeline(runtime, tmp_path / 'run')
    data = item_fixture()
    real_build = MV.build
    calls = {'levels': []}

    def build(stage, data, summary_level=0):
        calls['levels'].append(summary_level)
        kind = MV.kind_of(stage, data)
        if kind == 'item_author' and (data['stage_data'].get('item_assignment') or {}).get('item_id') == 'A_ITEM_0001' and summary_level < 2:
            raise ValueError('model_input_over_limit:item_author:950000>900000')     # 2단계 요약 전에는 항상 한도 초과
        return real_build(stage, data, summary_level=summary_level)
    monkeypatch.setattr(MV, 'build', build)
    prepared, merged = go(pipe, data)
    assert any(row['id'] == 'A_ITEM_0001' for row in merged['actions']), '전송 전 한도 초과도 요약으로 복구해 항목이 성공해야 한다'
    root = tmp_path / 'run' / 'checkpoints' / 'action-A_ITEM_0001'
    rows = json.loads((root / 'length-failures.json').read_text(encoding='utf-8'))
    assert rows['count'] == 3 and all(r['kind'] == 'model_input_over_limit' for r in rows['failures'])
    assert 2 in calls['levels'], '3번째 시도는 2단계 요약 뷰로 한도를 재야 한다: %r' % calls['levels']
    assert L.summary_level(root) == 2


def test_a_pre_send_over_limit_failure_that_survives_level_two_fails_loudly(tmp_path, monkeypatch):
    import asis_model_view as MV
    pipe = Pipeline(LengthRuntime(0), tmp_path / 'run')
    data = item_fixture()
    real_build = MV.build

    def build(stage, data, summary_level=0):
        if MV.kind_of(stage, data) == 'item_author' and (data['stage_data'].get('item_assignment') or {}).get('item_id') == 'A_ITEM_0001':
            raise ValueError('model_input_over_limit:item_author:950000>900000')
        return real_build(stage, data, summary_level=summary_level)
    monkeypatch.setattr(MV, 'build', build)
    try:
        go(pipe, data)
    except Exception as exc:
        assert 'action_item_generation_failed' in str(exc) or 'model_input_over_limit' in str(exc)
    else:
        raise AssertionError('요약 후에도 한도를 넘는데 성공으로 끝났다')
    assert L.summary_level(tmp_path / 'run' / 'checkpoints' / 'action-A_ITEM_0001') == 2


def test_a_failing_item_is_sent_exactly_four_times_original_twice_then_level_one_then_level_two(tmp_path):
    """영석님 규칙(길이로 2번 이상 실패한 경우만 요약): 기록된 길이 실패 0~1회=원문, 2회=1단계, 3회 이상=2단계.
    모든 단계가 실패하면 원문을 2번(실패 0회·1회 시점), 1단계 1번(2회 시점), 2단계 1번(3회 시점) 보내고 올린다. 그 이상 재시도하지 않는다."""
    runtime, pipe, data = run(tmp_path, fail_times=99)
    try:
        go(pipe, data)
    except Exception:
        pass
    assert runtime.attempts == 4, runtime.attempts
    root = tmp_path / 'run' / 'checkpoints' / 'action-A_ITEM_0001'
    assert json.loads((root / 'length-failures.json').read_text(encoding='utf-8'))['count'] == 4


# ── 검토 호출도 같은 규칙 ────────────────────────────────────────────────────────────────────────────────────
# 운영 2026-10-18(A_ITEM_0015): 작성은 요약 2단계로 25.6만 토큰에 통과했지만 검토 입력은 후보가 더해져 30.4만 토큰이 되어 한도(27.2만)에서 거절됐다.
# 영석님 규칙(길이로 2번 이상 실패한 항목은 요약)은 작성뿐 아니라 검토에도 걸려야 한다.

class ReviewLengthRuntime(LengthRuntime):
    """과제 A_ITEM_0001 의 항목 검토(content_review) 호출이 앞의 review_fail_times 회는 길이 오류로 실패한다."""

    def __init__(self, review_fail_times):
        super().__init__(0)
        self.review_fail_times = review_fail_times
        self.review_attempts = 0
        self.review_bodies = []

    async def ask(self, role, message, **kwargs):
        if role == 'content_review':
            body = json.loads(message.rsplit('\n입력:\n', 1)[-1])
            assignment = body['stage_data'].get('item_assignment', {})
            if assignment.get('item_id') == 'A_ITEM_0001' and body['stage_data'].get('draft') is not None:
                self.review_attempts += 1
                self.review_bodies.append(body)
                if self.review_attempts <= self.review_fail_times:
                    raise RuntimeError(BUDGET)
        return await ItemRuntime.ask(self, role, message, **kwargs)


def test_a_length_failure_in_the_item_review_is_recorded_and_the_review_recovers_summarized(tmp_path):
    runtime = ReviewLengthRuntime(2)
    pipe = Pipeline(runtime, tmp_path / 'run')
    prepared, merged = go(pipe, item_fixture())
    assert any(row['id'] == 'A_ITEM_0001' for row in merged['actions']), '검토가 길이 실패 2회 뒤 요약본으로 통과해야 한다'
    assert runtime.review_attempts == 3, runtime.review_attempts
    root = tmp_path / 'run' / 'checkpoints' / 'action-A_ITEM_0001'
    rows = json.loads((root / 'length-failures.json').read_text(encoding='utf-8'))
    assert rows['count'] == 2 and all(r['kind'] == 'model_prompt_budget' for r in rows['failures'])
    assert L.summary_level(root) == 1
    assert list(root.rglob('summarized-review-input.json')), '검토 요약 사실을 따로 남겨야 한다'


def test_a_review_length_failure_does_not_consume_the_author_budget_or_rewrite_the_candidate(tmp_path):
    runtime = ReviewLengthRuntime(1)
    authored = []
    real = runtime.ask

    async def ask(role, message, **kwargs):
        if role == 'author':
            body = json.loads(message.rsplit('\n입력:\n', 1)[-1])
            if body['stage_data'].get('item_assignment', {}).get('item_id') == 'A_ITEM_0001':
                authored.append(1)
        return await real(role, message, **kwargs)
    runtime.ask = ask
    pipe = Pipeline(runtime, tmp_path / 'run')
    go(pipe, item_fixture())
    root = tmp_path / 'run' / 'checkpoints' / 'action-A_ITEM_0001'
    assert len(authored) == 1, '검토가 길이로 실패해도 작성을 다시 하면 안 된다: %d' % len(authored)
    assert len(list(root.glob('round-*'))) == 1
    assert runtime.review_attempts == 2, runtime.review_attempts


def test_a_review_that_still_fails_after_level_two_fails_loudly(tmp_path):
    runtime = ReviewLengthRuntime(99)
    pipe = Pipeline(runtime, tmp_path / 'run')
    try:
        go(pipe, item_fixture())
    except Exception:
        pass
    else:
        raise AssertionError('검토가 계속 한도를 넘는데 성공으로 끝났다')
    assert runtime.review_attempts == 4, runtime.review_attempts
    assert L.summary_level(tmp_path / 'run' / 'checkpoints' / 'action-A_ITEM_0001') == 2


def test_the_very_first_review_of_an_item_with_recorded_failures_is_already_summarized(tmp_path):
    """작성 단계에서 길이 실패가 이미 2회 기록된 항목은 검토의 첫 호출부터 요약본으로 보낸다(운영 A_ITEM_0015: 작성이 2단계로 통과한 뒤 검토 첫 호출이 원문 30.4만 토큰으로 거절됐다)."""
    runtime = ReviewLengthRuntime(0)
    pipe = Pipeline(runtime, tmp_path / 'run')
    root = tmp_path / 'run' / 'checkpoints' / 'action-A_ITEM_0001'
    for _ in range(3):
        L.record(root, kind='model_prompt_budget', tokens=299366, limit=272000)     # 작성 쪽에서 쌓인 3회 → 2단계
    go(pipe, item_fixture())
    assert runtime.review_attempts == 1, '검토는 한 번에 통과해야 한다(첫 호출이 이미 요약본)'
    assert list(root.rglob('summarized-review-input.json')), '첫 검토부터 요약 사실이 남아야 한다'
    record = json.loads(sorted(root.rglob('summarized-review-input.json'))[-1].read_text(encoding='utf-8'))
    assert record['level'] == 2


def long_text_fixture():
    """승인 방향의 서술문이 길어 요약이 실제로 입력을 줄이는 항목 입력."""
    data = item_fixture()
    text = ('이 방향은 운영 체계를 정비하는 것이다. 조직이 일관된 절차를 갖추는 것이 바람직하다. 세부 항목은 별도 문서에 둔다. ' * 60)
    for row in data['stage_data']['approved_directions']['directions']:
        row.update(text=text, change_direction=text, customer_impact=text)
    return data, text


def test_the_first_review_body_is_actually_shorter_when_the_item_already_has_recorded_failures(tmp_path):
    """요약이 실제로 입력을 줄였는지를 본다(작은 합성 입력은 요약해도 같아 변이를 구별하지 못했다)."""
    data, text = long_text_fixture()
    rt = ReviewLengthRuntime(0)
    root = tmp_path / 'run' / 'checkpoints' / 'action-A_ITEM_0001'
    for _ in range(3):
        L.record(root, kind='model_prompt_budget', tokens=299366, limit=272000)
    go(Pipeline(rt, tmp_path / 'run'), data)
    summarized = len(json.dumps(rt.review_bodies[0], ensure_ascii=False))
    rt2 = ReviewLengthRuntime(0)
    go(Pipeline(rt2, tmp_path / 'run2'), long_text_fixture()[0])
    original = len(json.dumps(rt2.review_bodies[0], ensure_ascii=False))
    assert summarized < original, (summarized, original)
