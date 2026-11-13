"""설치된 MAF와 Copilot 세션의 실제 재개·수명 계약을 오프라인 RPC로 검증한다."""
from pathlib import Path
import asyncio
from datetime import datetime, timezone
import sys
from uuid import uuid4

import pytest
from agent_framework.github import GitHubCopilotAgent
from copilot.session import CopilotSession
from copilot.session_events import session_event_from_dict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from asis_runtime import Runtime


@pytest.mark.parametrize('role', ('author', 'content_review'))
def test_installed_sdk_reuses_one_item_agent_and_service_conversation(role):
    """외부 모델 호출은 없으며 실제 SDK 생성·재개와 누적 대화를 시험한다."""
    agents, creates, resumes, timeouts, tasks, sessions = [], [], [], [], [], {}

    def event(kind, data):
        return session_event_from_dict({'id': str(uuid4()), 'timestamp': datetime.now(timezone.utc).isoformat(),
            'type': kind, 'data': data, 'parentId': None})

    class Connection:
        def __init__(self):
            self.history = []

        async def request(self, method, params):
            assert method == 'session.send'
            self.history.append(params)

            async def deliver():
                await asyncio.sleep(.001)
                self.session._dispatch_event(event('assistant.usage', {'model': 'gpt-6.1-sol',
                    'inputTokens': 1, 'outputTokens': 1, 'finishReason': 'stop'}))
                self.session._dispatch_event(event('assistant.message', {'content': '{"offline_only":true}',
                    'messageId': str(uuid4()), 'phase': 'final_answer', 'turnId': str(len(self.history))}))
                self.session._dispatch_event(event('assistant.turn_end', {'turnId': str(len(self.history))}))
                self.session._dispatch_event(event('session.idle', {}))
            tasks.append(asyncio.create_task(deliver()))
            return {'messageId': str(uuid4())}

    class Session(CopilotSession):
        async def send_and_wait(self, *args, **kwargs):
            timeouts.append(kwargs['timeout'])
            return await super().send_and_wait(*args, **kwargs)

    class Client:
        async def list_models(self):
            return [{'id':'gpt-6.1-sol','capabilities':{'limits':{
                'max_prompt_tokens':922000,'max_context_window_tokens':1050000}}}]

        async def start(self):
            pass

        async def stop(self):
            await asyncio.gather(*tasks)

        async def create_session(self, **kwargs):
            creates.append(kwargs)
            rpc = Connection()
            rpc.session = Session(kwargs['session_id'], rpc)
            sessions[rpc.session.session_id] = (rpc.session, rpc)
            return rpc.session

        async def resume_session(self, session_id, **kwargs):
            assert 'session_id' not in kwargs
            resumes.append((session_id, kwargs))
            return sessions[session_id][0]

    def factory(**kwargs):
        agent = GitHubCopilotAgent(**kwargs)
        agents.append(agent)
        return agent

    async def exercise():
        async with Runtime(client=Client(), agent_factory=factory) as runtime:
            records = []
            for turn in ('first item', 'all findings corrected', 'rendered item corrected'):
                _, receipt = await runtime.ask(role, turn, session_id='sdk-item-a-'+role)
                records.append(receipt)
            assert len({r['agent_instance_id'] for r in records}) == 1
            assert len({r['session_instance_id'] for r in records}) == 1
            assert [r['session_turn'] for r in records] == [1, 2, 3]
            assert [r['agent_reused'] for r in records] == [False, True, True]
        assert len(agents) == len(creates) == 1 and len(resumes) == 2
        assert timeouts == [1200, 1200, 1200]
        session, rpc = sessions['sdk-item-a-'+role]
        assert len(rpc.history) == 3
        assert all(options['model'] == 'gpt-6.1-sol' and options['reasoning_effort'] == 'xhigh'
                   and options['available_tools'] == [] for options in creates + [row[1] for row in resumes])
        for options in creates + [row[1] for row in resumes]:
            limits = options['model_capabilities'].limits
            assert limits.max_prompt_tokens == 922000 and limits.max_context_window_tokens == 1050000

    asyncio.run(exercise())
