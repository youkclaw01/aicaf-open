"""목표 의미 해석의 동일 원문 중복을 조용히 병합하지 않고 원문·직전 후보를 보존해 재요청한다."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from asis_pipeline import Pipeline
from test_asis_connection import synthetic_input


def test_intent_contract_repair_preserves_source_and_prior_duplicate_quote(tmp_path):
    data=synthetic_input(tmp_path);quote=next(f['value'] for f in data['facts'] if f['id']=='F_IV_Q1_transcript')
    goal={'source_fact_id':'F_IV_Q1_transcript','quote':quote,'interpretation':'원문 기반 검색 도입과 권한 보존','area_refs':['data','secgov'],'ambiguity':''}
    class InvalidFirst:
        def __init__(self):self.inputs=[]
        async def ask(self,role,message,**kwargs):
            assert role=='interpret'
            payload=json.loads(message.rsplit('\n입력:\n',1)[-1]);self.inputs.append(deepcopy(payload))
            reply={'goals':[deepcopy(goal),deepcopy(goal)] if len(self.inputs)==1 else [deepcopy(goal)],'requirements':[]}
            return json.dumps(reply,ensure_ascii=False),{'test_only':True,'role':role}
    runtime=InvalidFirst();engine=Pipeline(runtime,tmp_path/'run')
    bound=asyncio.run(engine.interpret_input(data))
    assert len(bound['goals'])==1 and bound['goals'][0]['quote']==quote
    assert len(runtime.inputs)==2
    assert runtime.inputs[0]['facts']==runtime.inputs[1]['facts']
    assert runtime.inputs[1]['stage_data']['prior_output']['goals']==[goal,goal]
    assert runtime.inputs[1]['stage_data']['contract_error']=='duplicate_goal_quote'
