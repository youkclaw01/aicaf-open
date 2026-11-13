"""검토가 blocked 이면서 구체적 defects(where·quote·why·fix)를 함께 돌려준 경우의 파이프라인 시험(합성 런타임, 모델 호출 없음).

운영 2026-10-18(프로젝트 25 11단계 요약): 검토 응답이 verdict=blocked 이면서 defects 1건(렌더 미제공 문장을 지우라는 위치·인용·수정 방향 완비)과 unresolved 1건을 함께 담았다.
조정자 분기는 `not review['defects']` 조건이라 defects 가 하나라도 있으면 건너뛰고 곧바로 independent_review_blocked 로 실패했다. 기존 시험은 blocked 응답이 모두 defects=[] 라서
이 입력은 설계가 다룬 적이 없었다. 검토 계약(P5): blocked 는 '근거 부족으로 판단할 수 없을 때', revise 는 '검증 가능한 오류를 결함으로 기록'.
계약:
1. blocked + defects>=1 이면 defects 는 revise 처럼 작성자에게 돌려 재작성한다(작성 보완 예산 1회 사용). 통과 처리가 아니다.
2. 재작성된 후보는 새 독립 검토가 pass 를 내야만 승인된다(검토 게이트는 그대로).
3. defects 가 없는 blocked 는 기존 조정자 경로 그대로다(변경 없음).
4. 같은 defects 로 계속 blocked 되면 작성 라운드 상한으로 실패한다(무한 반복 없음).
5. 보완 예산이 없으면 실패한다.
"""
from pathlib import Path
import asyncio
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pytest
from asis_pipeline import Pipeline
from test_asis_item_agents import ItemRuntime, item_fixture


class MixedBlockReviewer(ItemRuntime):
    """처음 block_times 번의 검토는 blocked + defects(구체적 지적)를 돌려주고 이후는 정상 통과한다."""

    def __init__(self, block_times, with_unresolved=True):
        super().__init__()
        self.block_times = block_times
        self.with_unresolved = with_unresolved
        self.reviews = 0
        self.author_prompts = []

    async def ask(self, role, message, **kwargs):
        if role in ('author', 'direction_author'):
            self.author_prompts.append(message)
        raw, receipt = await super().ask(role, message, **kwargs)
        if role == 'content_review' and not kwargs.get('session_id', '').startswith('asis-arbiter-'):
            self.reviews += 1
            if self.reviews <= self.block_times:
                body = json.loads(message.rsplit('\n입력:\n', 1)[-1])
                draft = body['stage_data']['draft']
                review = json.loads(raw)
                quote = json.dumps(draft, ensure_ascii=False)[:30]
                review.update(verdict='blocked', content_checked=True, render_checked=False,
                    defects=[{'where': '/diagnoses/0/text', 'quote': draft['diagnoses'][0]['text'], 'why': '합성 지적: 이 문장은 후보 자신의 오류다.',
                              'fix': '합성 수정 방향: 이 문장을 바로잡는다.', 'evidence_refs': []}],
                    unresolved=[{'topic': '합성 확인 불가', 'reason': '합성: 제공되지 않은 근거 원문은 대조할 수 없다.', 'required_evidence': '합성 근거 원문',
                                 'fact_refs': []}] if self.with_unresolved else [])
                raw = json.dumps(review, ensure_ascii=False)
        return raw, receipt


def run(tmp_path, block_times, with_unresolved=True):
    from asis_item_agents import allocate_plan, item_input
    from test_asis_item_agents import metadata_plan
    data = allocate_plan(item_fixture(), metadata_plan(), 'synthetic-plan')
    scoped = item_input(data, 'diagnosis', data['context']['action_item_plan']['diagnoses'][0])
    runtime = MixedBlockReviewer(block_times, with_unresolved)
    pipe = Pipeline(runtime, tmp_path / 'run')
    return runtime, pipe, scoped


def go(pipe, scoped):
    return asyncio.run(pipe.reviewed_stage(scoped, 3, 'diagnosis-J_ITEM_0001'))


def test_a_blocked_review_with_concrete_defects_sends_the_candidate_back_to_the_writer(tmp_path):
    runtime, pipe, scoped = run(tmp_path, block_times=1)
    candidate = go(pipe, scoped)
    assert candidate
    assert len([p for p in runtime.author_prompts]) == 2, '작성자가 지적을 받아 한 번 더 써야 한다'


def test_the_defects_reach_the_writer_as_accumulated_defects(tmp_path):
    runtime, pipe, scoped = run(tmp_path, block_times=1)
    go(pipe, scoped)
    second = json.loads(runtime.author_prompts[1].rsplit('\n입력:\n', 1)[-1])
    defects = second['stage_data'].get('accumulated_defects') or []
    assert any('합성 수정 방향' in str(d.get('fix', '')) for d in defects), defects


def test_the_rewritten_candidate_still_needs_a_new_independent_review_to_pass(tmp_path):
    runtime, pipe, scoped = run(tmp_path, block_times=1)
    go(pipe, scoped)
    assert runtime.reviews == 2, '재작성 후 새 독립 검토가 있어야 승인된다'
    record = json.loads(next((tmp_path / 'run' / 'checkpoints').rglob('accepted.json')).read_text(encoding='utf-8'))
    assert record['review']['verdict'] == 'pass' and record['round'] == 2


def test_a_candidate_that_keeps_getting_blocked_with_defects_fails_when_rounds_run_out(tmp_path):
    runtime, pipe, scoped = run(tmp_path, block_times=99)
    with pytest.raises(RuntimeError, match='independent_review_blocked|revision_budget_exhausted'):
        go(pipe, scoped)
    assert runtime.reviews <= pipe.rounds, (runtime.reviews, pipe.rounds)


def test_a_blocked_review_without_defects_still_follows_the_existing_arbiter_path(tmp_path):
    src = (Path(__file__).resolve().parents[1] / 'asis_pipeline.py').read_text(encoding='utf-8')
    assert "review['unresolved'] and not review['defects']" in src, '조정자 경로 조건은 바뀌지 않는다'


def test_the_blocked_with_defects_route_is_recorded_for_audit(tmp_path):
    runtime, pipe, scoped = run(tmp_path, block_times=1)
    go(pipe, scoped)
    events = list((tmp_path / 'run').rglob('events*.jsonl'))
    text = ' '.join(p.read_text(encoding='utf-8') for p in events) if events else ''
    saved = list((tmp_path / 'run' / 'checkpoints').rglob('blocked-with-defects.json'))
    assert saved or 'blocked_with_defects' in text, '이 경로를 탔다는 기록이 남아야 한다'


def test_the_audit_record_never_declares_a_pass_and_keeps_the_original_verdict(tmp_path):
    runtime, pipe, scoped = run(tmp_path, block_times=1)
    go(pipe, scoped)
    row = json.loads(next((tmp_path / 'run' / 'checkpoints').rglob('blocked-with-defects.json')).read_text(encoding='utf-8'))
    assert row['pass_declared'] is False and row['verdict_unchanged'] == 'blocked'
    assert row['route'] == 'defects_returned_to_writer' and row['defects'] == 1 and row['unresolved'] == 1


def test_the_original_blocked_review_is_kept_in_the_ledger_not_overwritten_by_a_pass(tmp_path):
    runtime, pipe, scoped = run(tmp_path, block_times=1)
    go(pipe, scoped)
    first = json.loads(next((tmp_path / 'run' / 'checkpoints').rglob('round-001/review.json')).read_text(encoding='utf-8'))
    assert first['verdict'] == 'blocked' and first['defects'], '첫 검토의 blocked 원본은 그대로 남아야 한다'


def test_a_deterministic_defect_in_the_same_round_is_not_routed_this_way(tmp_path, monkeypatch):
    """코드의 결정적 검사가 결함을 냈는데 content 검토가 가능한 라운드: blocked 지적을 이 경로로 따로 쌓지 않고 기존 처리(결정적 결함 + 검토 결함)가 맡는다."""
    import asis_item_agents
    runtime, pipe, scoped = run(tmp_path, block_times=1)
    real = asis_item_agents.collect_item_defects
    state = {'n': 0}

    def with_defect(p, prompt_data, candidate):
        check = real(p, prompt_data, candidate)
        state['n'] += 1
        if state['n'] == 1:
            check = {**check, 'defects': [{'where': '/diagnoses/0/title', 'quote': 'x', 'why': '합성 결정적 결함', 'fix': '합성', 'evidence_refs': []}],
                     'content_review_possible': True}
        return check
    monkeypatch.setattr(asis_item_agents, 'collect_item_defects', with_defect)
    try:
        go(pipe, scoped)
    except Exception:
        pass
    assert state['n'] >= 1
    first_round = tmp_path / 'run' / 'checkpoints' / 'diagnosis-J_ITEM_0001' / 'round-001'
    assert not (first_round / 'blocked-with-defects.json').exists(), '결정적 결함이 있는 라운드는 이 경로를 타지 않는다'


def test_without_remaining_budget_the_blocked_review_fails_instead_of_looping(tmp_path):
    from asis_item_agents import allocate_plan, item_input
    from test_asis_item_agents import metadata_plan
    data = allocate_plan(item_fixture(), metadata_plan(), 'synthetic-plan')
    scoped = item_input(data, 'diagnosis', data['context']['action_item_plan']['diagnoses'][0])
    runtime = MixedBlockReviewer(99)
    pipe = Pipeline(runtime, tmp_path / 'run')
    with pytest.raises(RuntimeError):
        asyncio.run(pipe.reviewed_stage(scoped, 3, 'diagnosis-J_ITEM_0001', author_budget={'remaining': 1}))
    assert len(runtime.author_prompts) == 1, '예산 1회뿐이면 재작성 없이 실패해야 한다'
    assert runtime.reviews == 1


def test_each_blocked_with_defects_round_spends_exactly_one_author_budget_unit(tmp_path):
    """보완 예산 n 이면 작성 n 번·검토 n 번 뒤에 실패한다. 예산 조건은 while 루프 상한과 같은 결과를 내는 이중 방어이며, 이 시험이 그 동치를 고정한다."""
    for remaining in (1, 2):
        runtime, pipe, scoped = run(tmp_path / ('b%d' % remaining), block_times=99)
        with pytest.raises(RuntimeError, match='independent_review_blocked'):
            asyncio.run(pipe.reviewed_stage(scoped, 3, 'diagnosis-J_ITEM_0001', author_budget={'remaining': remaining}))
        assert len(runtime.author_prompts) == remaining and runtime.reviews == remaining, (remaining, len(runtime.author_prompts), runtime.reviews)
