"""응답이 끝내 오지 않은 호출은 새 세션으로 한 번만 다시 보내고, 응답이 온 호출·두 번째 실패는 그대로 둔다."""
from pathlib import Path
import asyncio, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from asis_runtime import Runtime as ModelRuntime


def _runtime(behaviour):
    runtime = object.__new__(ModelRuntime)
    runtime.activity = {}
    runtime.agents = {'s1': object()}
    runtime.agent_bindings = {'s1': {}}
    runtime.base_directory = None
    calls = []

    async def fake(role, message, *, session_id, instructions='', image=None):
        calls.append(session_id)
        runtime.activity[session_id] = {'request_started_at': 'x'}
        outcome = behaviour[len(calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, {'session_id': session_id}
    runtime._ask = fake
    return runtime, calls


def test_hung_call_is_resent_once_with_a_new_session():
    runtime, calls = _runtime([Exception('GitHub Copilot request failed: Timeout after 1200s waiting for session.idle'), 'OK'])
    text, receipt = asyncio.run(runtime.ask('author', 'm', session_id='s1'))
    assert text == 'OK' and calls == ['s1', 's1-resend1']
    assert runtime.resent_after_no_response[0]['reason'] == 'no_response_before_idle_timeout'
    assert 's1' not in runtime.agents


def test_second_hang_and_other_errors_are_not_retried_again():
    runtime, calls = _runtime([Exception('Timeout after 1200s waiting for session.idle')] * 2)
    try:
        asyncio.run(runtime.ask('author', 'm', session_id='s1'))
        raise AssertionError('expected failure')
    except Exception as exc:
        assert 'session.idle' in str(exc)
    assert calls == ['s1', 's1-resend1']
    runtime, calls = _runtime([RuntimeError('empty_model_response')])
    try:
        asyncio.run(runtime.ask('author', 'm', session_id='s1'))
        raise AssertionError('expected failure')
    except RuntimeError as exc:
        assert str(exc) == 'empty_model_response'
    assert calls == ['s1'], 'a call that returned must not be resent'
