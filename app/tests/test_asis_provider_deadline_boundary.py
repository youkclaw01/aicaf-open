"""공급자 진입은 실제 모델 호출의 마감보다 먼저 끝나야 한다. 기존 모델 제한값은 늘리지 않는다."""
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
import asyncio
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from asis_runtime import Runtime
from asis_admission import AdaptiveAdmission


def test_provider_admission_finishes_before_agent_run_and_its_deadline(tmp_path):
    events=[]
    class Client:
        transport_kind='chat-completions'
        inside=False
        async def start(self):pass
        async def stop(self):pass
        def restore_session(self,*args):return None
        @asynccontextmanager
        async def invocation_admission(self):
            events.append('provider_wait')
            await asyncio.sleep(.001)
            self.inside=True;events.append('provider_admitted')
            try:yield {'queued_at':'controlled-queued','admitted_at':'controlled-admitted'}
            finally:self.inside=False;events.append('provider_released')
    class Agent:
        def __init__(self,**kwargs):self.kwargs=kwargs
        async def run(self,*args,**kwargs):
            assert self.kwargs['client'].inside,'provider_queue_must_be_outside_agent_deadline'
            events.append('agent_run')
            return SimpleNamespace(text='{"ok":true}',response_id='controlled-response',
                additional_properties={'model':self.kwargs['default_options']['model']},
                usage_details={'input_token_count':1,'output_token_count':2,'total_token_count':3})
    sampler=lambda:{'sample_valid':True,'total_memory_bytes':16*1024**3,'available_memory_bytes':14*1024**3,
                    'cpu_percent':0,'worker_tree_rss_bytes':128*1024**2}
    async def scenario():
        gate=AdaptiveAdmission(sampler=sampler,interval=.001)
        async with Runtime(client=Client(),agent_factory=Agent,admission=gate,base_directory=tmp_path/'sessions',timeout=1) as runtime:
            text,receipt=await runtime.ask('author','controlled original input',session_id='controlled-provider-boundary',instructions='고정 지시')
            assert receipt['response_id']=='controlled-response'
    asyncio.run(scenario())
    assert events==['provider_wait','provider_admitted','agent_run','provider_released']
