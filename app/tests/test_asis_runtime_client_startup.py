"""역할별 SDK 클라이언트 기동이 동시 호출의 병목·실패 원인이 되지 않는 계약 시험(합성 SDK, 모델 호출 없음).

2026-10-17 운영 사고(프로젝트 25, 7단계 방향 작성):
- 방향 작성 역할(direction_author)의 SDK 프로세스는 첫 호출 때 기동되는데, 87개 호출이 동시에 도착했다.
- 기동에 153초가 걸려 client_for_role 의 45초 제한을 넘었고, 한 호출이 모델 요청 전 설정 단계에서 TimeoutError 로 실패했다.
- 그 실패 하나가 작성 보완 예산을 쓰고 개정 전체(87개 중 86개 승인)를 failed 로 만들었다.

계약:
1. 개정 시작(__aenter__) 때 모든 역할의 클라이언트를 기동한다. 호출이 몰리기 전에 기동을 끝낸다.
2. 기동 제한은 기동 자체에만 적용한다. 다른 호출이 잠금을 쥔 채 기동하는 동안의 대기는 제한에 포함하지 않는다.
3. 기동이 실패·시간 초과되면 반쯤 기동된 클라이언트를 정리하고 보관하지 않아 다음 호출이 다시 기동할 수 있다.
4. 모델 요청 전 설정 단계(session_setup, model_request_started=False) 실패는 같은 호출을 제한된 횟수만 다시 시도하고, 작성 보완 예산을 쓰지 않는다.
"""
from pathlib import Path
import asyncio
import sys
import time

import pytest

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import asis_runtime
from asis_admission import AdaptiveAdmission

GB = 1 << 30


def gate():
    return AdaptiveAdmission(sampler=lambda: {'sample_valid': True, 'total_memory_bytes': 32 * GB, 'available_memory_bytes': 28 * GB,
                                               'cpu_percent': 1.0, 'worker_tree_rss_bytes': 1 << 20,
                                               'commit_limit_bytes': 40 * GB, 'commit_available_bytes': 38 * GB}, interval=0.001)


class SlowClient:
    """기동이 start_seconds 걸리는 SDK 클라이언트 대역. fail_first 번은 기동 중 시간 초과로 취소된다."""
    started_total = 0
    stopped_total = 0
    instances = []

    def __init__(self, **options):
        self.options = options
        self.sessions = {}
        self.state = 'new'
        SlowClient.instances.append(self)

    async def start(self):
        self.state = 'connecting'
        await asyncio.sleep(SlowClient.start_seconds)
        SlowClient.started_total += 1
        self.state = 'connected'

    async def stop(self):
        SlowClient.stopped_total += 1
        self.state = 'stopped'


class Reply:
    def __init__(self, model):
        self.text = '{"ok": true}'
        self.response_id = 'r'
        self.additional_properties = {'model': model}
        self.usage_details = {'input_token_count': 5, 'output_token_count': 2}


class Agent:
    def __init__(self, *, client, name, instructions, tools, default_options):
        self.client = client
        self.model = default_options['model']

    async def run(self, message, *, session, options):
        assert self.client.state == 'connected', 'client_not_started'
        session.service_session_id = session.service_session_id or 'svc-' + session.session_id
        return Reply(self.model)


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    monkeypatch.setattr(asis_runtime.Runtime, 'SETUP_RETRY_DELAY', (0.0, 0.0))
    SlowClient.started_total = SlowClient.stopped_total = 0
    SlowClient.instances = []
    SlowClient.start_seconds = 0.05
    monkeypatch.setenv('GITHUB_TOKEN', 'x')


def runtime(tmp_path, **kw):
    return asis_runtime.Runtime(client_factory=SlowClient, agent_factory=Agent, admission=gate(), base_directory=tmp_path / 'sessions', **kw)


def test_enter_starts_every_role_client_before_any_call(tmp_path):
    async def go():
        async with runtime(tmp_path) as rt:
            return set(rt.role_clients)
    started = asyncio.run(go())
    assert started == set(asis_runtime._ROLES), '개정 시작 때 모든 역할 클라이언트가 기동돼야 한다: %r' % sorted(started)


def test_a_burst_of_first_calls_does_not_wait_on_a_cold_client(tmp_path):
    SlowClient.start_seconds = 0.4

    async def go():
        async with runtime(tmp_path) as rt:
            t0 = time.monotonic()
            await asyncio.gather(*(rt.ask('direction_author', '{"n": %d}' % i, session_id=f'asis-author-{i:024d}', instructions='i') for i in range(40)))
            return time.monotonic() - t0
    elapsed = asyncio.run(go())
    assert elapsed < 0.3, '첫 호출이 기동을 기다렸다: %.2fs' % elapsed


def test_startup_limit_applies_to_the_startup_not_to_waiting_behind_another_caller(tmp_path):
    """한 호출이 느린 기동을 하는 동안 잠금 뒤에서 기다린 호출들은 기동 제한(여기서는 0.3초)을 넘겨 기다려도 실패하지 않아야 한다."""
    SlowClient.start_seconds = 0.2

    async def go():
        rt = runtime(tmp_path)
        rt.startup_timeout = 0.3
        rt._client_options = dict(github_token='x', use_logged_in_user=False, mode='empty', env={}, log_level='error')
        rt.client_factory = SlowClient
        rt.agent_factory = Agent
        rt._entered = True
        got = await asyncio.gather(*(rt.client_for_role('direction_author') for _ in range(30)), return_exceptions=True)
        return got
    got = asyncio.run(go())
    assert all(not isinstance(g, BaseException) for g in got), [type(g).__name__ for g in got if isinstance(g, BaseException)][:3]
    assert len({id(g) for g in got}) == 1, '같은 역할은 클라이언트 하나를 공유해야 한다'
    assert SlowClient.started_total == 1


def test_a_start_that_times_out_is_cleaned_up_and_can_be_retried(tmp_path):
    SlowClient.start_seconds = 0.5

    async def go():
        rt = runtime(tmp_path)
        rt.startup_timeout = 0.1
        rt._client_options = dict(github_token='x', use_logged_in_user=False, mode='empty', env={}, log_level='error')
        rt._entered = True
        first = await asyncio.gather(rt.client_for_role('author'), return_exceptions=True)
        assert isinstance(first[0], BaseException)
        assert 'author' not in rt.role_clients, '실패한 기동의 클라이언트를 보관하면 다음 호출이 죽은 클라이언트를 쓴다'
        assert SlowClient.stopped_total >= 1, '반쯤 기동된 클라이언트를 정리하지 않았다'
        SlowClient.start_seconds = 0.01
        rt.startup_timeout = 5
        ok = await rt.client_for_role('author')
        return ok
    ok = asyncio.run(go())
    assert ok.state == 'connected'


def test_a_setup_stage_failure_is_retried_without_spending_the_author_budget(tmp_path):
    """모델 요청 전(session_setup) 실패는 작성자의 결함이 아니므로 같은 호출을 제한된 횟수만 다시 시도한다."""
    calls = {'n': 0}

    class Flaky(Agent):
        async def run(self, message, *, session, options):
            raise AssertionError('not used')

    async def go():
        async with runtime(tmp_path) as rt:
            real = rt.client_for_role
            failures = {'left': 2}

            async def flaky_client_for_role(role):
                calls['n'] += 1
                if failures['left'] > 0:
                    failures['left'] -= 1
                    raise TimeoutError('start timed out')
                return await real(role)
            rt.client_for_role = flaky_client_for_role
            text, receipt = await rt.ask('author', '{"n": 1}', session_id='asis-author-' + '1' * 24, instructions='i')
            return text, receipt
    text, receipt = asyncio.run(go())
    assert text and receipt['role'] == 'author'
    assert calls['n'] == 3, '설정 단계 실패 2회 후 3번째에 성공해야 한다: %d' % calls['n']


def test_setup_retries_are_bounded_and_the_last_error_is_raised(tmp_path):
    async def go():
        async with runtime(tmp_path) as rt:
            async def always_fail(role):
                raise TimeoutError('start timed out')
            rt.client_for_role = always_fail
            return await asyncio.gather(rt.ask('author', '{}', session_id='asis-author-' + '2' * 24, instructions='i'), return_exceptions=True)
    got = asyncio.run(go())
    assert isinstance(got[0], BaseException)
    assert str(got[0]) or type(got[0]).__name__


def test_a_failure_after_the_model_request_started_is_not_retried_as_a_setup_failure(tmp_path):
    """모델 요청이 이미 나간 뒤의 실패를 설정 단계 재시도로 다시 보내면 같은 요청이 두 번 과금된다."""
    attempts = {'n': 0}

    class Hang(Agent):
        async def run(self, message, *, session, options):
            attempts['n'] += 1
            raise RuntimeError('boom after request')

    async def go():
        rt = asis_runtime.Runtime(client_factory=SlowClient, agent_factory=Hang, admission=gate(), base_directory=tmp_path / 'sessions')
        async with rt:
            return await asyncio.gather(rt.ask('author', '{}', session_id='asis-author-' + '3' * 24, instructions='i'), return_exceptions=True)
    got = asyncio.run(go())
    assert isinstance(got[0], RuntimeError)
    assert attempts['n'] == 1, '요청 이후 실패를 재시도하면 이중 과금: %d' % attempts['n']


def test_a_timeout_after_the_request_started_is_never_resent_as_a_setup_failure(tmp_path):
    """요청이 이미 나간 뒤의 TimeoutError(응답 대기 초과)는 설정 단계 재시도 대상이 아니다. 다시 보내면 같은 요청이 이중 과금된다."""
    attempts = {'n': 0}

    class Slow(Agent):
        async def run(self, message, *, session, options):
            attempts['n'] += 1
            raise TimeoutError('waiting for session.idle')

    async def go():
        rt = asis_runtime.Runtime(client_factory=SlowClient, agent_factory=Slow, admission=gate(), base_directory=tmp_path / 'sessions')
        async with rt:
            return await asyncio.gather(rt.ask('author', '{}', session_id='asis-author-' + '4' * 24, instructions='i'), return_exceptions=True)
    got = asyncio.run(go())
    assert isinstance(got[0], BaseException)
    # 기존 무응답 재전송(-resend1)은 호출자의 별도 복구로 한 번만 허용된다. 설정 단계 재시도가 더해지면 3번 이상이 된다.
    assert attempts['n'] <= 2, '요청 이후 TimeoutError 를 설정 단계 실패로 재시도했다: %d회' % attempts['n']


def test_setup_retries_stop_at_the_configured_limit(tmp_path):
    calls = {'n': 0}

    async def go():
        async with runtime(tmp_path) as rt:
            async def always_fail(role):
                calls['n'] += 1
                raise TimeoutError('start timed out')
            rt.client_for_role = always_fail
            return await asyncio.gather(rt.ask('author', '{}', session_id='asis-author-' + '5' * 24, instructions='i'), return_exceptions=True)
    got = asyncio.run(asyncio.wait_for(go(), 20))
    assert isinstance(got[0], BaseException)
    assert calls['n'] == asis_runtime.Runtime.SETUP_RETRY_LIMIT + 1, '시도 횟수는 최초 1회 + 재시도 상한이어야 한다: %d' % calls['n']


def test_a_started_role_client_is_returned_without_taking_the_startup_lock(tmp_path):
    """기동이 끝난 뒤의 호출이 다른 역할·다른 호출의 기동 잠금을 기다리면 안 된다(잠금을 쥔 채 느린 기동을 하는 동안에도 즉시 반환)."""
    async def go():
        async with runtime(tmp_path) as rt:
            lock = rt.role_client_locks.setdefault('author', asyncio.Lock())
            await lock.acquire()          # 누군가 이 역할의 기동 잠금을 쥐고 있다
            try:
                got = await asyncio.wait_for(rt.client_for_role('author'), 0.2)
            finally:
                lock.release()
            return got
    client = asyncio.run(go())
    assert client.state in ('connected', 'stopped')
