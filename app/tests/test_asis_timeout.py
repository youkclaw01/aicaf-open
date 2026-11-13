"""모든 현황분석 역할의 호출·세션 재개에서 승인된 유한 대기 시간을 검증한다."""
from pathlib import Path
import asyncio
import sys
import types

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_runtime


@pytest.mark.parametrize('role', tuple(asis_runtime.policy()['models']))
def test_default_timeout_reaches_every_role_and_resumed_sdk_session(role, monkeypatch):
    """모델 대기는 1200초이며 종료 유예·도구 금지·모델·리즈닝은 유지한다."""
    creations = []
    observed = []
    deadlines = []
    original_wait_for = asyncio.wait_for

    async def recording_wait_for(awaitable, timeout):
        deadlines.append(timeout)
        return await original_wait_for(awaitable, timeout)

    monkeypatch.setattr(asis_runtime.asyncio, 'wait_for', recording_wait_for)

    class Client:
        async def start(self):
            pass

        async def stop(self):
            pass

    class Agent:
        def __init__(self, **kwargs):
            self.options = kwargs['default_options']
            creations.append(self)

        async def run(self, message, *, session, options=None):
            observed.append({**self.options, **(options or {})})
            if session.service_session_id is None:
                session.service_session_id = 'sdk-unit-session'
            else:
                assert session.service_session_id == 'sdk-unit-session'
            return types.SimpleNamespace(
                text='{"unit_only":true}', response_id='timeout-unit-only',
                additional_properties={'model': asis_runtime.model_for(role)}, usage_details={})

    async def exercise():
        async with asis_runtime.Runtime(client=Client(), agent_factory=Agent) as runtime:
            assert runtime.timeout == 1200
            await runtime.ask(role, 'first unit input', session_id='unit-timeout-' + role)
            await runtime.ask(role, 'resumed unit input', session_id='unit-timeout-' + role)
        assert len(runtime.calls) == 2

    asyncio.run(exercise())
    # 역할별 SDK 종료에도 45초 유예를 적용한다. 모델 대기 1200초는 변경하지 않는다.
    assert deadlines == [45, 1215, 1215, 45]
    assert len(observed) == 2 and len(creations) == 1
    assert observed[0]['session_id'] == 'unit-timeout-' + role
    assert 'session_id' not in observed[1]
    for options in observed:
        assert options['timeout'] == 1200
        # 모델·추론 강도는 역할 정책을 따른다(방향 작성만 luna high). 타임아웃·도구 금지는 전 역할 공통이다.
        assert options['model'] == asis_runtime.model_for(role)
        assert options['reasoning_effort'] == asis_runtime.effort_for(role)
        assert options['available_tools'] == []
        assert options['enable_config_discovery'] is False
