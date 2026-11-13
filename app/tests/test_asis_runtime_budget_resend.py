"""프롬프트 한도 초과 오류와 요청 전 실패를 작성 예산 소모 없이 복구하는 계약 시험(합성 SDK, 모델 호출 없음).

2026-10-18 운영(프로젝트 25, 7단계 방향 작성):
- D_P0064·D_P0072: 'Request exceeds the model's prompt budget after reserving output (144963 tokens; limit 128000)' 로 거절돼 방향이 failed 가 됐다.
  이 오류는 모델이 응답하기 전에 거절된 요청 전 실패라 과금이 없다. 실측(2026-10-18): 새 세션의 첫 요청은 같은 역할·모델로 139,625 토큰도 통과했다.
  같은 세션에서 보완 라운드를 이어 가며 대화 이력이 쌓여 6만→11.8만→17.5만 토큰이 되는 것이 한도를 넘는 구조다.
- D_P0080: 요청 전 RuntimeError(session_setup)가 작성 보완 예산을 소모했다.

계약:
1. 프롬프트 한도 초과 오류(예외 메시지에 'prompt budget'·'limit')는 요청이 나가지 않은 실패다. 같은 입력을 이력이 없는 새 세션 ID 로 한 번만 재전송한다.
   재전송 사실(원래 세션·오류의 토큰 수·한도)은 기록한다. 작성 보완 예산은 호출자가 쓰지 않는다(ask 안에서 복구).
2. 새 세션으로도 같은 오류면 두 번째 재전송은 하지 않고 오류를 그대로 올린다(무한 반복 금지).
3. 요청 전(request_started_at 없음) 실패는 TimeoutError 뿐 아니라 RuntimeError 도 제한된 횟수만 재시도한다. 요청이 나간 뒤의 실패는 재시도하지 않는다.
4. 영석님 방침: 한도 안에서는 같은 세션을 이어 쓴다(동일 작성 세션·이력 유지). 새 세션은 한도 초과 오류가 난 호출에만 쓴다.
"""
from pathlib import Path
import asyncio
import sys

import pytest

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import asis_runtime
from asis_admission import AdaptiveAdmission

GB = 1 << 30
BUDGET_ERROR = "GitHub Copilot request failed: Session error: Execution failed: Request exceeds the model's prompt budget after reserving output (144963 tokens; limit 128000). Reduce context or compact before continuing."


def gate():
    return AdaptiveAdmission(sampler=lambda: {'sample_valid': True, 'total_memory_bytes': 32 * GB, 'available_memory_bytes': 28 * GB,
                                               'cpu_percent': 1.0, 'worker_tree_rss_bytes': 1 << 20,
                                               'commit_limit_bytes': 40 * GB, 'commit_available_bytes': 38 * GB}, interval=0.001)


class Client:
    def __init__(self, **options):
        self.options = options
        self.sessions = {}
        self.state = 'new'

    async def start(self):
        self.state = 'connected'

    async def stop(self):
        self.state = 'stopped'


class Reply:
    def __init__(self, model):
        self.text = '{"ok": true}'
        self.response_id = 'r'
        self.additional_properties = {'model': model}
        self.usage_details = {'input_token_count': 5, 'output_token_count': 2}


class Agent:
    """session_id 별로 호출 횟수를 세고, fail_plan 에 따라 요청 전 오류를 낸다."""
    seen = []
    fail_plan = {}

    def __init__(self, *, client, name, instructions, tools, default_options):
        self.client = client
        self.model = default_options['model']

    async def run(self, message, *, session, options):
        sid = session.session_id
        Agent.seen.append(sid)
        plan = Agent.fail_plan.get(sid)
        if plan:
            plan['n'] -= 1
            if plan['n'] >= 0:
                raise plan['exc']
        session.service_session_id = session.service_session_id or 'svc-' + sid
        return Reply(self.model)


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    Agent.seen = []
    Agent.fail_plan = {}
    monkeypatch.setattr(asis_runtime.Runtime, 'SETUP_RETRY_DELAY', (0.0, 0.0))
    monkeypatch.setenv('GITHUB_TOKEN', 'x')


def runtime(tmp_path):
    return asis_runtime.Runtime(client_factory=Client, agent_factory=Agent, admission=gate(), base_directory=tmp_path / 'sessions')


SID = 'asis-author-' + 'a' * 24


def test_a_prompt_budget_error_is_resent_once_on_a_fresh_session(tmp_path):
    Agent.fail_plan = {SID: {'n': 1, 'exc': RuntimeError(BUDGET_ERROR)}}

    async def go():
        async with runtime(tmp_path) as rt:
            text, receipt = await rt.ask('author', '{"n": 1}', session_id=SID, instructions='i')
            return text, receipt, list(getattr(rt, 'resent_after_prompt_budget', []))
    text, receipt, resent = asyncio.run(go())
    assert text and receipt['role'] == 'author'
    assert Agent.seen[0] == SID and len(Agent.seen) == 2 and Agent.seen[1] != SID and Agent.seen[1].startswith(SID), Agent.seen
    assert len(resent) == 1 and resent[0]['session_id'] == SID
    assert resent[0]['prompt_tokens'] == 144963 and resent[0]['limit'] == 128000


def test_a_second_budget_error_on_the_fresh_session_is_not_resent_again(tmp_path):
    Agent.fail_plan = {SID: {'n': 1, 'exc': RuntimeError(BUDGET_ERROR)},
                       SID + '-budget1': {'n': 9, 'exc': RuntimeError(BUDGET_ERROR)}}

    async def go():
        async with runtime(tmp_path) as rt:
            return await asyncio.gather(rt.ask('author', '{}', session_id=SID, instructions='i'), return_exceptions=True)
    got = asyncio.run(go())
    assert isinstance(got[0], RuntimeError) and 'prompt budget' in str(got[0])
    assert len(Agent.seen) == 2, '재전송은 한 번뿐이어야 한다: %r' % Agent.seen


def test_an_unrelated_runtime_error_is_not_mistaken_for_a_budget_error(tmp_path):
    Agent.fail_plan = {SID: {'n': 9, 'exc': RuntimeError('some other failure')}}

    async def go():
        async with runtime(tmp_path) as rt:
            return await asyncio.gather(rt.ask('author', '{}', session_id=SID, instructions='i'), return_exceptions=True)
    got = asyncio.run(go())
    assert isinstance(got[0], RuntimeError)
    assert not any(s.endswith('-budget1') for s in Agent.seen), Agent.seen


def test_within_the_limit_the_same_session_is_kept_across_rounds(tmp_path):
    """한도 안에서는 같은 세션을 이어 쓴다(영석님 방침). 새 세션은 한도 초과 오류가 난 호출에만 쓴다."""
    async def go():
        async with runtime(tmp_path) as rt:
            for i in range(3):
                await rt.ask('author', '{"round": %d}' % i, session_id=SID, instructions='i')
            return rt.session_turns[SID]
    turns = asyncio.run(go())
    assert Agent.seen == [SID, SID, SID] and turns == 3


def test_a_setup_stage_runtime_error_before_any_request_is_retried(tmp_path):
    """D_P0080: 요청이 나가기 전 RuntimeError 도 제한된 횟수 재시도한다."""
    calls = {'n': 0}

    async def go():
        async with runtime(tmp_path) as rt:
            real = rt.client_for_role

            async def flaky(role):
                calls['n'] += 1
                if calls['n'] <= 2:
                    raise RuntimeError('session setup failed')
                return await real(role)
            rt.client_for_role = flaky
            return await rt.ask('author', '{}', session_id=SID, instructions='i')
    text, receipt = asyncio.run(go())
    assert text and calls['n'] == 3


def test_a_runtime_error_after_the_request_started_is_not_retried(tmp_path):
    Agent.fail_plan = {SID: {'n': 9, 'exc': RuntimeError('boom after request')}}

    async def go():
        async with runtime(tmp_path) as rt:
            return await asyncio.gather(rt.ask('author', '{}', session_id=SID, instructions='i'), return_exceptions=True)
    got = asyncio.run(go())
    assert isinstance(got[0], RuntimeError)
    assert Agent.seen == [SID], '요청이 나간 뒤의 실패는 재시도하지 않는다: %r' % Agent.seen


def test_the_resent_session_id_cannot_be_resent_again_even_when_it_fails_on_every_call(tmp_path):
    """무한 재전송 방지: 한도 초과가 계속 나는 입력은 새 세션에서도 정확히 한 번만 더 시도하고 멈춘다(호출 수 상한)."""
    Agent.fail_plan = {SID: {'n': 99, 'exc': RuntimeError(BUDGET_ERROR)}, SID + '-budget1': {'n': 99, 'exc': RuntimeError(BUDGET_ERROR)},
                       SID + '-budget1-budget1': {'n': 99, 'exc': RuntimeError(BUDGET_ERROR)}}

    async def go():
        async with runtime(tmp_path) as rt:
            return await asyncio.wait_for(asyncio.gather(rt.ask('author', '{}', session_id=SID, instructions='i'), return_exceptions=True), 15)
    got = asyncio.run(go())
    assert isinstance(got[0], BaseException)
    assert len(Agent.seen) <= 2 and not any(s.endswith('-budget1-budget1') for s in Agent.seen), Agent.seen


def test_a_budget_error_is_never_retried_as_a_setup_failure_before_the_fresh_session_resend(tmp_path):
    """한도 초과는 같은 세션에 설정 재시도로 다시 보내면 같은 오류만 반복한다. 새 세션 재전송만 해야 한다(원래 세션 호출은 1회)."""
    Agent.fail_plan = {SID: {'n': 99, 'exc': RuntimeError(BUDGET_ERROR)}}

    async def go():
        async with runtime(tmp_path) as rt:
            return await rt.ask('author', '{}', session_id=SID, instructions='i')
    asyncio.run(go())
    assert Agent.seen.count(SID) == 1, '원래 세션으로 한도 오류를 반복해 보냈다: %r' % Agent.seen


def test_a_deterministic_setup_error_is_not_retried(tmp_path):
    """역할·지시문 바인딩이 바뀐 것 같은 결정적 오류는 반복해도 같으므로 재시도하지 않는다."""
    calls = {'n': 0}

    async def go():
        async with runtime(tmp_path) as rt:
            async def broken(role):
                calls['n'] += 1
                raise RuntimeError('persistent_agent_role_or_instruction_binding_changed')
            rt.client_for_role = broken
            return await asyncio.gather(rt.ask('author', '{}', session_id=SID, instructions='i'), return_exceptions=True)
    got = asyncio.run(go())
    assert isinstance(got[0], RuntimeError)
    assert calls['n'] == 1, '결정적 오류를 재시도했다: %d회' % calls['n']
