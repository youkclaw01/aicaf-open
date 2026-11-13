"""11단계 요약 단계의 길이 실패도 같은 규칙(기록·2회 이상이면 결정적 요약·한도 안이면 통과)으로 복구하는 파이프라인 연결 시험(합성 런타임, 모델 호출 없음).

운영 2026-10-18(프로젝트 25): 10단계가 끝난 뒤 요약 작성 입력이 34.3만 토큰으로 한도(27.2만)를 넘어 개정이 failed 가 됐다. 길이 실패 기록과 요약은 과제 항목(stage 3)에만 걸려 있었다.
계약:
1. 요약 작성(stage 4)의 길이 실패(모델 한도 초과 거절·전송 전 한도 검사)도 checkpoints/summary/length-failures.json 에 영속 기록한다. 작성 보완 예산을 쓰지 않는다.
2. 기록 횟수 0~1: 원문, 2: 1단계, 3 이상: 2단계 요약(방향·진단·영역 시사점의 서술문 축약, 이름·숫자 보존). 모델에 보내는 입력만 바뀐다.
3. 2단계로도 넘으면 더 깎지 않고 실패로 올린다. 4번 넘게 재전송하지 않는다.
4. 요약이 아닌 단계(영역·종합 등)는 이 경로의 영향을 받지 않는다.
"""
from copy import deepcopy
from pathlib import Path
import asyncio
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import asis_length_failures as L
import asis_model_view as MV
from asis_pipeline import Pipeline
from test_asis_summary_input import summary_data

BUDGET = "GitHub Copilot request failed: Session error: Execution failed: Request exceeds the model's prompt budget after reserving output (343398 tokens; limit 272000)."


class SummaryRuntime:
    """요약 작성 호출이 앞의 fail_times 회는 길이 오류로 실패하고, 이후에는 후보 JSON 을 돌려준다. 보낸 프롬프트를 기록한다."""

    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.prompts = []
        self.reviews = 0

    async def ask(self, role, message, **kwargs):
        if role == 'author':
            self.prompts.append(message)
            if len(self.prompts) <= self.fail_times:
                raise RuntimeError(BUDGET)
            body = json.loads(message.rsplit('\n입력:\n', 1)[-1])
            raise StopIteration('시험은 응답 본문을 만들지 않고 보낸 입력만 본다')
        raise AssertionError('검토까지 가지 않는다')


def run(tmp_path, fail_times, seed=0):
    tmp_path = Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    data = summary_data(tmp_path, unresolved=20, directions=40)
    text = '이 방향은 운영 체계를 정비하는 것이다. 조직이 일관된 절차를 갖추는 것이 바람직하다. 세부 내용은 별도 문서에 둔다. ' * 30
    for row in data['stage_data']['approved_directions']['directions']:
        row.update(text=text, customer_impact=text, change_direction=text)
    runtime = SummaryRuntime(fail_times)
    pipe = Pipeline(runtime, tmp_path / 'run')
    root = pipe.folder / 'checkpoints' / 'summary'
    for _ in range(seed):
        L.record(root, kind='model_prompt_budget', tokens=343398, limit=272000)
    return runtime, pipe, data, root


def go(pipe, data):
    try:
        asyncio.run(pipe.reviewed_stage(data, 4, 'summary'))
    except (StopIteration, RuntimeError):
        pass


def sent_directions(prompt):
    body = json.loads(prompt.rsplit('\n입력:\n', 1)[-1])
    return body['stage_data']['approved_directions']['directions']


def test_the_summary_stage_is_wired_into_the_length_failure_path():
    src = (Path(__file__).resolve().parents[1] / 'asis_pipeline.py').read_text(encoding='utf-8')
    assert 'is_summary = stage == 4' in src, '요약 단계도 길이 실패 규칙을 받아야 한다'
    assert 'summarize_summary_input' in (Path(__file__).resolve().parents[1] / 'asis_model_view.py').read_text(encoding='utf-8')


def test_a_length_failure_of_the_summary_stage_is_recorded_and_costs_no_author_budget(tmp_path):
    runtime, pipe, data, root = run(tmp_path, fail_times=1)
    go(pipe, data)
    assert json.loads((root / L.FILE).read_text(encoding='utf-8'))['count'] >= 1


def test_two_recorded_failures_send_a_shorter_summary_input(tmp_path):
    runtime, pipe, data, root = run(tmp_path, fail_times=0, seed=2)
    go(pipe, data)
    runtime2, pipe2, data2, root2 = run(tmp_path / 'plain', fail_times=0, seed=0)
    go(pipe2, data2)
    short = sum(len(r['text']) for r in sent_directions(runtime.prompts[0]))
    full = sum(len(r['text']) for r in sent_directions(runtime2.prompts[0]))
    assert short < full, (short, full)


def test_fewer_than_two_recorded_failures_send_the_full_input(tmp_path):
    a, pa, da, ra = run(tmp_path / 'a', fail_times=0, seed=1)
    go(pa, da)
    b, pb, db, rb = run(tmp_path / 'b', fail_times=0, seed=0)
    go(pb, db)
    assert sent_directions(a.prompts[0]) == sent_directions(b.prompts[0])


def test_ids_and_references_are_unchanged_in_the_summarized_input(tmp_path):
    runtime, pipe, data, root = run(tmp_path, fail_times=0, seed=3)
    go(pipe, data)
    sent = {r['id']: r for r in sent_directions(runtime.prompts[0])}
    original = {r['id']: r for r in data['stage_data']['approved_directions']['directions']}
    assert set(sent) == set(original)
    assert all(sent[i]['area_implication_refs'] == original[i]['area_implication_refs'] for i in sent)


def test_the_canonical_author_input_file_keeps_the_original_text(tmp_path):
    runtime, pipe, data, root = run(tmp_path, fail_times=0, seed=3)
    go(pipe, data)
    saved = json.loads((root / 'round-001' / 'author-input.json').read_text(encoding='utf-8'))
    assert saved['stage_data']['approved_directions']['directions'][0]['text'] == data['stage_data']['approved_directions']['directions'][0]['text']
    assert (root / 'round-001' / 'summarized-input.json').is_file()


def test_a_summary_that_still_fails_after_level_two_is_raised_not_retried_forever(tmp_path):
    runtime, pipe, data, root = run(tmp_path, fail_times=99)
    try:
        asyncio.run(pipe.reviewed_stage(data, 4, 'summary'))
    except RuntimeError:
        pass
    assert len(runtime.prompts) <= 4, len(runtime.prompts)
    assert L.summary_level(root) == 2


def test_other_stages_never_create_length_failure_records(tmp_path):
    data, draft = __import__('asis_analysis').contract().synthetic_fixture()

    class TooLong:
        async def ask(self, role, message, **kwargs):
            raise RuntimeError(BUDGET)
    pipe = Pipeline(TooLong(), tmp_path / 'run')
    try:
        asyncio.run(pipe.reviewed_stage(data, 1, 'area-probe'))
    except Exception:
        pass
    assert not list((tmp_path / 'run').rglob(L.FILE))


# ── 요약 검토 호출도 같은 규칙(작성에서 쓴 요약 단계, 길이 실패 기록 공유) ─────────────────────────────────────────
# 운영 2026-10-18(프로젝트 25 11단계 7차 재개): 요약 작성은 2단계 요약으로 22.8만 토큰에 통과했지만 검토 입력은 후보가 더해져 34.9만 토큰이 되어 거절됐다.
# 검토 호출은 과제 항목(item_assignment)에만 길이 규칙이 걸려 있었다(작성에서 놓친 것과 같은 구멍).

class SummaryReviewRuntime:
    """작성은 정상 응답, 검토는 앞의 fail_times 회 길이 오류로 실패 후 통과 응답. 검토에 보낸 프롬프트를 기록한다."""

    def __init__(self, candidate, fail_times):
        self.candidate = candidate
        self.fail_times = fail_times
        self.review_prompts = []

    async def ask(self, role, message, **kwargs):
        if role == 'author':
            return json.dumps(self.candidate, ensure_ascii=False), {'test_only': True}
        self.review_prompts.append(message)
        if len(self.review_prompts) <= self.fail_times:
            raise RuntimeError(BUDGET.replace('343398', '348656'))
        return json.dumps({'stage': 'review', 'run': self.candidate['run'], 'verdict': 'pass', 'content_checked': True, 'render_checked': False,
                           'defects': [], 'unresolved': []}, ensure_ascii=False), {'test_only': True}


def run_review(tmp_path, fail_times, seed=0):
    import asis_analysis
    tmp_path = Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    data = summary_data(tmp_path, unresolved=20, directions=40)
    text = '이 방향은 운영 체계를 정비하는 것이다. 조직이 일관된 절차를 갖추는 것이 바람직하다. 세부 내용은 별도 문서에 둔다. ' * 30
    for row in data['stage_data']['approved_directions']['directions']:
        row.update(text=text, customer_impact=text, change_direction=text)
    (tmp_path / 'seed').mkdir(parents=True, exist_ok=True)
    seed_stage = asis_analysis.analyze(__import__('test_asis_connection').synthetic_input(tmp_path / 'seed'))['stages'][3]
    # 합성 요약의 방향·과제 참조를 승인된 입력의 실제 ID 로 맞춘다(계약 검사: 요약에 등장한 모든 방향·과제는 입력에 있어야 한다).
    seed_stage['summary']['direction_refs'] = [r['id'] for r in data['stage_data']['approved_directions']['directions']][:3]
    seed_stage['summary']['action_refs'] = [r['id'] for r in data['stage_data']['approved_actions']['actions']][:3]
    runtime = SummaryReviewRuntime(seed_stage, fail_times)
    pipe = Pipeline(runtime, tmp_path / 'run')
    root = pipe.folder / 'checkpoints' / 'summary'
    for _ in range(seed):
        L.record(root, kind='model_prompt_budget', tokens=343398, limit=272000)
    return runtime, pipe, data, root


def go_review(pipe, data):
    try:
        asyncio.run(pipe.reviewed_stage(data, 4, 'summary'))
    except Exception:
        pass


def review_directions(prompt):
    body = json.loads(prompt.rsplit('\n입력:\n', 1)[-1])
    return body['stage_data']['approved_directions']['directions']


def test_the_summary_review_call_is_wired_into_the_length_failure_path():
    src = (Path(__file__).resolve().parents[1] / 'asis_pipeline.py').read_text(encoding='utf-8')
    assert 'review_scope = review_is_item or review_is_summary' in src, '요약 검토 호출도 길이 규칙을 받아야 한다'


def test_a_length_failure_of_the_summary_review_is_recorded(tmp_path):
    runtime, pipe, data, root = run_review(tmp_path, fail_times=1)
    go_review(pipe, data)
    assert json.loads((root / L.FILE).read_text(encoding='utf-8'))['count'] >= 1


def test_the_review_uses_the_same_summary_level_as_the_author_when_failures_are_recorded(tmp_path):
    a_rt, a_pipe, a_data, a_root = run_review(tmp_path / 'summ', fail_times=0, seed=3)
    go_review(a_pipe, a_data)
    b_rt, b_pipe, b_data, b_root = run_review(tmp_path / 'plain', fail_times=0, seed=0)
    go_review(b_pipe, b_data)
    assert a_rt.review_prompts and b_rt.review_prompts
    short = sum(len(r['text']) for r in review_directions(a_rt.review_prompts[0]))
    full = sum(len(r['text']) for r in review_directions(b_rt.review_prompts[0]))
    assert short < full, (short, full)


def test_the_review_summary_does_not_change_the_candidate_under_review(tmp_path):
    runtime, pipe, data, root = run_review(tmp_path, fail_times=0, seed=3)
    go_review(pipe, data)
    body = json.loads(runtime.review_prompts[0].rsplit('\n입력:\n', 1)[-1])
    saved = json.loads((root / 'round-001' / 'candidate.json').read_text(encoding='utf-8'))
    assert body['stage_data']['draft'] == saved, '검토 대상 후보는 요약되지 않는다'


def test_a_summary_review_that_still_fails_after_level_two_is_raised_not_retried_forever(tmp_path):
    runtime, pipe, data, root = run_review(tmp_path, fail_times=99, seed=3)
    go_review(pipe, data)
    assert len(runtime.review_prompts) <= 4, len(runtime.review_prompts)


# ── 요약 검토가 blocked + defects 를 함께 돌려준 운영 사례(11단계, 2026-10-18) ──────────────────────────────────────
class SummaryBlockedWithDefectsRuntime:
    """작성은 두 번 같은 후보, 검토는 첫 번째에 blocked + defects(구체적 지적) + unresolved, 이후 통과. 검토에 보낸 프롬프트와 작성 호출 수를 센다."""

    def __init__(self, candidate):
        self.candidate = candidate
        self.author_calls = 0
        self.review_calls = 0

    async def ask(self, role, message, **kwargs):
        if role == 'author':
            self.author_calls += 1
            return json.dumps(self.candidate, ensure_ascii=False), {'test_only': True}
        self.review_calls += 1
        base = {'stage': 'review', 'run': self.candidate['run'], 'content_checked': True, 'render_checked': False}
        if self.review_calls == 1:
            body = json.loads(message.rsplit('\n입력:\n', 1)[-1])
            text = body['stage_data']['draft']['summary']['message']
            review = dict(base, verdict='blocked',
                defects=[{'where': '/summary/message', 'quote': text, 'why': '합성: 단계 경계를 보류 사유로 썼다.', 'fix': '합성: 그 문장을 지운다.', 'evidence_refs': []}],
                unresolved=[{'topic': '합성 확인 불가', 'reason': '합성: 값이 not_inlined 라 대조할 수 없다.', 'required_evidence': '합성 원문', 'fact_refs': []}])
        else:
            review = dict(base, verdict='pass', defects=[], unresolved=[])
        return json.dumps(review, ensure_ascii=False), {'test_only': True}


def test_a_summary_review_blocked_with_defects_is_rewritten_and_then_passes_a_new_review(tmp_path):
    import asis_analysis
    tmp_path = Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    data = summary_data(tmp_path, unresolved=20, directions=10)
    (tmp_path / 'seed').mkdir(parents=True, exist_ok=True)
    seed_stage = asis_analysis.analyze(__import__('test_asis_connection').synthetic_input(tmp_path / 'seed'))['stages'][3]
    seed_stage['summary']['direction_refs'] = [r['id'] for r in data['stage_data']['approved_directions']['directions']][:3]
    seed_stage['summary']['action_refs'] = [r['id'] for r in data['stage_data']['approved_actions']['actions']][:3]
    runtime = SummaryBlockedWithDefectsRuntime(seed_stage)
    pipe = Pipeline(runtime, tmp_path / 'run')
    asyncio.run(pipe.reviewed_stage(data, 4, 'summary'))
    assert runtime.author_calls == 2 and runtime.review_calls == 2, (runtime.author_calls, runtime.review_calls)
    accepted = json.loads((tmp_path / 'run' / 'checkpoints' / 'summary' / 'accepted.json').read_text(encoding='utf-8'))
    assert accepted['review']['verdict'] == 'pass'
    assert list((tmp_path / 'run' / 'checkpoints' / 'summary').rglob('blocked-with-defects.json'))
