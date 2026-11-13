"""승인된 복구 세션은 작성 호출과 반복 검토를 금지한다."""
from test_asis_terminal_recovery import packet
import asyncio
import types
import pytest
import asis_runtime


def test_continuation_allows_only_one_content_review_across_runtime_restart(tmp_path):
    folder,session,text,instructions,binding,path,events=packet(tmp_path)
    calls=[]
    class Client:
        async def start(self):pass
        async def stop(self):pass
    class Agent:
        def __init__(self,**kwargs):pass
        async def run(self,*args,**kwargs):
            calls.append(1)
            return types.SimpleNamespace(text='{"ok":true}',response_id='review-unit',additional_properties={'model':'gpt-6.1-sol'},usage_details={})
    async def run():
        async with asis_runtime.Runtime(client=Client(),agent_factory=Agent,base_directory=folder) as runtime:
            await runtime.ask('content_review','candidate',session_id='review-one')
            with pytest.raises(RuntimeError,match='continuation_call_budget'):
                await runtime.ask('content_review','candidate',session_id='review-two')
        async with asis_runtime.Runtime(client=Client(),agent_factory=Agent,base_directory=folder) as runtime:
            with pytest.raises(RuntimeError,match='continuation_call_budget'):
                await runtime.ask('content_review','candidate',session_id='review-three')
    asyncio.run(run());assert calls==[1]


def test_continuation_prohibits_different_author_call(tmp_path):
    folder,session,text,instructions,binding,path,events=packet(tmp_path)
    class Client:
        async def start(self):pass
        async def stop(self):pass
    class Agent:
        def __init__(self,**kwargs):pass
        async def run(self,*args,**kwargs):raise AssertionError('author prohibited')
    async def run():
        async with asis_runtime.Runtime(client=Client(),agent_factory=Agent,base_directory=folder) as runtime:
            with pytest.raises(RuntimeError,match='continuation_call_budget'):
                await runtime.ask('author','new text',session_id='different-author')
    asyncio.run(run())
