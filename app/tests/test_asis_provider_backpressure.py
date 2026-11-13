"""공급자가 명시적으로 거절한 429만 같은 요청으로 재진입하고 의미 작성·검토 예산은 건드리지 않는다."""
import asyncio
from pathlib import Path
import sys
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from asis_chat_transport import ProviderAdmission


class Response:
    def __init__(self, status, headers=None):
        self.status_code=status;self.headers=headers or {};self.closed=False
    async def aread(self):return b'too many requests'
    async def aclose(self):self.closed=True


def test_explicit_429_is_waited_and_retried_inside_one_logical_request():
    async def scenario():
        calls=[];rejected=Response(429,{'retry-after':'0.001'})
        async def dispatch():
            calls.append(b'unchanged full serialized model request')
            return rejected if len(calls)==1 else Response(200)
        gate=ProviderAdmission(timeout=1)
        result=await gate.send(dispatch)
        assert result.status_code==200 and rejected.closed
        assert len(calls)==2 and calls[0]==calls[1]
    asyncio.run(scenario())


@pytest.mark.parametrize('status',[400,401,403,500])
def test_other_statuses_are_not_replayed(status):
    async def scenario():
        calls=[]
        async def dispatch():calls.append(1);return Response(status)
        result=await ProviderAdmission(timeout=1).send(dispatch)
        assert result.status_code==status and len(calls)==1
    asyncio.run(scenario())


def test_an_ambiguous_transport_failure_is_not_replayed():
    async def scenario():
        calls=[]
        async def dispatch():calls.append(1);raise TimeoutError('completion_unknown')
        with pytest.raises(TimeoutError):await ProviderAdmission(timeout=1).send(dispatch)
        assert len(calls)==1
    asyncio.run(scenario())


def test_provider_hint_beyond_the_original_deadline_does_not_extend_it():
    async def scenario():
        calls=[]
        async def dispatch():calls.append(1);return Response(429,{'retry-after':'60'})
        response=await ProviderAdmission(timeout=.01).send(dispatch)
        assert response.status_code==429 and len(calls)==1
    asyncio.run(scenario())


def test_budget_guard_is_checked_before_each_actual_attempt():
    async def scenario():
        attempted=[];checked=[]
        def guard():
            checked.append(1)
            if len(checked)>1:raise RuntimeError('approval_required')
        async def dispatch():attempted.append(1);return Response(429,{'retry-after':'0.001'})
        with pytest.raises(RuntimeError,match='approval_required'):
            await ProviderAdmission(timeout=1,before_send=guard).send(dispatch)
        assert len(attempted)==1
    asyncio.run(scenario())


def test_provider_capacity_grows_from_success_and_adapts_to_observed_rejection():
    async def scenario():
        state={'active':0,'peak':0,'rejected':0}
        class Resource:
            target=30
            def provider_backpressure(self,delay):pass
        async def dispatch():
            state['active']+=1;state['peak']=max(state['peak'],state['active'])
            try:
                await asyncio.sleep(.001)
                if state['active']>3:
                    state['rejected']+=1;return Response(429,{'retry-after':'0.001'})
                return Response(200)
            finally:state['active']-=1
        gate=ProviderAdmission(timeout=3,resource_admission=Resource())
        results=await asyncio.gather(*(gate.send(dispatch) for _ in range(30)))
        assert all(row.status_code==200 for row in results)
        assert state['peak']>1 and state['rejected']>0
        assert state['active']==0
    asyncio.run(scenario())


def test_success_already_in_flight_does_not_undo_a_new_capacity_reduction():
    async def scenario():
        class Resource:
            target=10
            def provider_backpressure(self,delay):pass
        gate=ProviderAdmission(timeout=.1,resource_admission=Resource())
        gate.capacity=2
        started=asyncio.Event();finish=asyncio.Event()
        async def old_success():
            started.set();await finish.wait();return Response(200)
        old=asyncio.create_task(gate.send(old_success));await started.wait()
        async def rejected():return Response(429,{'retry-after':'1'})
        response=await gate.send(rejected)
        assert response.status_code==429 and gate.capacity==1
        finish.set();assert (await old).status_code==200
        assert gate.capacity==1 and gate.active==0
    asyncio.run(scenario())
