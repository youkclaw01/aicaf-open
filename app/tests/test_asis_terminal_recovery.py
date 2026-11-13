"""완료 응답 복구는 원문·세션·정책 결합과 재작성 차단을 요구한다."""
from pathlib import Path
from copy import deepcopy
from datetime import datetime, timezone
import asyncio
import hashlib
import json
import sys
import types
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import asis_runtime


def packet(tmp_path):
    session='asis-author-unit-recovery'
    text='header\n입력:\n'+json.dumps({'run':{'id':1},'context':{},'facts':[]})
    instructions='immutable instructions'
    events=[{'id':'start','parentId':None,'type':'session.start','timestamp':'2000-01-01T00:00:00Z','data':{'sessionId':session,'selectedModel':asis_runtime.MODEL,'reasoningEffort':'xhigh'}},
      {'id':'system','parentId':'start','type':'system.message','data':{'content':'sdk defaults '+instructions}},
      {'id':'user','parentId':'system','type':'user.message','data':{'content':text}},
      {'id':'turn','parentId':'user','type':'assistant.turn_start','data':{'turnId':'0'}},
      {'id':'final','parentId':'turn','type':'assistant.message','data':{'content':'{"stage":"actions"}','phase':'final_answer','model':asis_runtime.MODEL,'messageId':'response','turnId':'0','toolRequests':[]}},
      {'id':'end','parentId':'final','type':'assistant.turn_end','data':{'turnId':'0'}}]
    folder=tmp_path/'sessions';path=folder/'session-state'/session/'events.jsonl';path.parent.mkdir(parents=True)
    raw='\n'.join(json.dumps(e) for e in events).encode();path.write_bytes(raw)
    binding={'session_id':session,'events_sha256':hashlib.sha256(raw).hexdigest(),
      'canonical_input_sha256':hashlib.sha256(text.encode()).hexdigest(),
      'instructions_sha256':hashlib.sha256(instructions.encode()).hexdigest(),
      'response_sha256':hashlib.sha256(events[-2]['data']['content'].encode()).hexdigest(),
      'response_id':'response','review_call_budget':1,'approval_id':'user-approved-once'}
    (tmp_path/'terminal-response-continuation.json').write_text(json.dumps(binding),encoding='utf-8')
    return folder,session,text,instructions,binding,path,events


def test_bound_terminal_response_is_recovered_without_author_model_call(tmp_path):
    folder,session,text,instructions,binding,path,events=packet(tmp_path)
    calls=[]
    class Client:
        async def start(self):pass
        async def stop(self):pass
    class Agent:
        def __init__(self,**kwargs):pass
        async def run(self,*args,**kwargs):
            calls.append(1);raise AssertionError('author must not be regenerated')
    async def run():
        async with asis_runtime.Runtime(client=Client(),agent_factory=Agent,base_directory=folder) as runtime:
            raw,receipt=await runtime.ask('author',text,session_id=session,instructions=instructions)
            assert raw=='{"stage":"actions"}'
            assert receipt['completion_source']=='preserved_sdk_final_and_turn_end'
            assert receipt['model_call_performed_now'] is False
            assert receipt['actual_model']==asis_runtime.MODEL
    asyncio.run(run());assert calls==[]


@pytest.mark.parametrize('change',['input','instructions','events','model','end','tools','extra_user'])
def test_recovery_rejects_drift_without_model_fallback(tmp_path,change):
    folder,session,text,instructions,binding,path,events=packet(tmp_path)
    if change=='input':text+=' '
    elif change=='instructions':instructions+=' changed'
    else:
        if change=='events':path.write_bytes(b'changed')
        else:
            if change=='model':events[-2]['data']['model']='different'
            elif change=='end':events[-1]['parentId']='wrong'
            elif change=='tools':events[-2]['data']['toolRequests']=[{'name':'shell'}]
            else:events.append({'id':'later','parentId':'end','type':'user.message','data':{'content':'later'}})
            raw='\n'.join(json.dumps(e) for e in events).encode();path.write_bytes(raw)
            binding['events_sha256']=hashlib.sha256(raw).hexdigest()
            (tmp_path/'terminal-response-continuation.json').write_text(json.dumps(binding),encoding='utf-8')
    class Client:
        async def start(self):pass
        async def stop(self):pass
    class Agent:
        def __init__(self,**kwargs):pass
        async def run(self,*args,**kwargs):raise AssertionError('fallback prohibited')
    async def run():
        async with asis_runtime.Runtime(client=Client(),agent_factory=Agent,base_directory=folder) as runtime:
            with pytest.raises(RuntimeError,match='terminal_recovery'):
                await runtime.ask('author',text,session_id=session,instructions=instructions)
    asyncio.run(run())
