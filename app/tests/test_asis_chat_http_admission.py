"""실제 설치 MAF/OpenAI 클라이언트의 429 전달을 통제된 HTTP 응답으로 재현한다. 외부 요청은 없다."""
from pathlib import Path
from contextlib import nullcontext
import asyncio
import json
import sys
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.mark.parametrize('pre_admitted', [False, True])
def test_http_rejection_reuses_exact_wire_body_and_one_framework_turn(tmp_path, pre_admitted):
    httpx = pytest.importorskip('httpx2')
    from openai import AsyncOpenAI
    from agent_framework import AgentSession, Message, Content
    from asis_chat_transport import ChatBackend
    async def scenario():
        sent=[]
        async def handler(request):
            sent.append(request.content)
            if len(sent)==1:
                return httpx.Response(429,headers={'retry-after':'0.001'},json={'error':{'message':'too many requests','type':'rate_limit_error'}})
            return httpx.Response(200,json={'id':'controlled-response','object':'chat.completion','created':1,
                'model':'gemini-4-argon','choices':[{'index':0,'message':{'role':'assistant','content':'{"ok":true}'},'finish_reason':'stop'}],
                'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}})
        policy={'models':{'author':'gemini-4-argon'},'reasoning_by_role':{'author':'high'}}
        backend=ChatBackend(model_policy=policy,base_directory=tmp_path)
        backend._credential='controlled-test-credential';backend._expires=float('inf');backend.endpoint='https://api.githubcopilot.com'
        backend.api=AsyncOpenAI(api_key=backend.api_key,base_url=backend.endpoint,max_retries=0,
            http_client=httpx.AsyncClient(transport=backend._provider_transport(httpx.MockTransport(handler))))
        agent=backend.agent_factory(client=backend,name='asis_author',instructions='요청한 JSON만 반환한다.',tools=None,
            default_options={'model':'gemini-4-argon','reasoning_effort':'high'})
        session=AgentSession(session_id='one-logical-turn')
        async with (backend.invocation_admission() if pre_admitted else nullcontext()):
            result=await agent.run(Message('user',contents=[Content.from_text('동일한 원문 요청')]),session=session)
        assert json.loads(result.text)=={'ok':True}
        assert len(sent)==2 and sent[0]==sent[1]
        assert backend.turns[('author','one-logical-turn')]==1
        await backend.stop()
    asyncio.run(asyncio.wait_for(scenario(),2))
