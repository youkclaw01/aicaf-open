"""AS-IS 제품 런타임의 MAF·모델 선택 계약을 검증한다. 실제 모델 시험은 별도 실행한다."""
from pathlib import Path
import importlib.util
import sys

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))


def test_asis_roles_use_maf_and_exact_requested_model_without_fallback():
    assert importlib.util.find_spec('asis_runtime') is not None, 'AS-IS MAF runtime is missing'
    import asis_runtime
    policy = asis_runtime.policy()
    assert policy['framework'] == 'Microsoft Agent Framework'
    assert policy['provider'] == 'github-copilot'
    # 방향 작성만 luna high, 나머지 역할은 sol xhigh(역할별 정책은 test_asis_role_model_policy.py 가 따로 고정한다).
    assert set(policy['models']) == {'interpret', 'author', 'content_review', 'visual_review', 'direction_author'}
    assert {r: m for r, m in policy['models'].items() if r != 'direction_author'} == {r: 'gpt-6.1-sol' for r in ('interpret', 'author', 'content_review', 'visual_review')}
    assert policy['models']['direction_author'] == 'gpt-6-luna'
    assert policy['fallback_allowed'] is False
    assert policy.get('reasoning_effort') == 'xhigh'
    assert policy.get('reasoning_by_role') == {'interpret': 'xhigh', 'author': 'xhigh', 'content_review': 'xhigh', 'visual_review': 'xhigh', 'direction_author': 'high'}


def test_runtime_passes_exact_model_and_denies_all_builtin_tools(tmp_path):
    import asyncio
    import types
    import asis_runtime
    observed = {}

    class Client:
        async def start(self):
            observed['started'] = True
        async def stop(self):
            observed['stopped'] = True

    class Agent:
        def __init__(self, **kwargs):
            observed.update(kwargs)
        async def run(self, messages, *, session, options=None):
            observed['session_id'] = session.session_id
            observed['run_options'] = options
            observed['messages'] = messages
            return types.SimpleNamespace(text='{"ok":true}', response_id='unit_transport_only',
                additional_properties={'model':'gpt-6.1-sol'}, finish_reason='stop', usage_details={})

    async def exercise():
        assert hasattr(asis_runtime, 'Runtime'), 'AS-IS MAF invocation is not connected'
        async with asis_runtime.Runtime(client=Client(), agent_factory=Agent, timeout=120) as runtime:
            text, receipt = await runtime.ask('content_review', 'unit payload', session_id='unit-isolated-review')
        assert text == '{"ok":true}'
        assert receipt['requested_model'] == receipt['actual_model'] == 'gpt-6.1-sol'
        assert receipt['fallback_used'] is False
        assert receipt.get('requested_reasoning_effort') == 'xhigh'
    asyncio.run(exercise())
    assert observed['default_options']['model'] == 'gpt-6.1-sol'
    assert observed['default_options'].get('reasoning_effort') == 'xhigh'
    assert observed['default_options']['available_tools'] == []
    assert observed['default_options']['enable_config_discovery'] is False
    assert observed['default_options']['enable_file_hooks'] is False
    assert observed['default_options']['mcp_servers'] == {}
    assert observed['tools'] is None
    assert observed['session_id'] == 'unit-isolated-review'
    assert observed['run_options']['session_id'] == 'unit-isolated-review'
    assert 'session_id' not in observed['default_options']
    assert observed['stopped'] is True


def test_runtime_rejects_different_model_without_another_call():
    import asyncio
    import types
    import pytest
    import asis_runtime
    calls = []
    class Client:
        async def start(self): pass
        async def stop(self): pass
    class Agent:
        def __init__(self, **kwargs): pass
        async def run(self, messages, *, session, options=None):
            calls.append(session.session_id)
            return types.SimpleNamespace(text='{"ok":true}', response_id='unit_wrong_model',
                additional_properties={'model':'different-model'})
    async def exercise():
        async with asis_runtime.Runtime(client=Client(), agent_factory=Agent) as runtime:
            with pytest.raises(RuntimeError, match='unexpected_response_model'):
                await runtime.ask('author','unit-only', session_id='unit-wrong-model')
    asyncio.run(exercise())
    assert len(calls) == 1


def test_runtime_resumes_its_own_maf_session_and_never_reuses_another_lane():
    import asyncio
    import types
    import asis_runtime
    seen = {}
    class Client:
        async def start(self): pass
        async def stop(self): pass
    class Agent:
        def __init__(self, **kwargs): pass
        async def run(self, messages, *, session, options=None):
            prior = seen.get(session.session_id)
            if prior is None:
                session.service_session_id = 'provider-' + session.session_id
                seen[session.session_id] = session
            else:
                assert session is prior, 'MAF conversation object was lost'
                assert session.service_session_id == 'provider-' + session.session_id
            return types.SimpleNamespace(text='{"ok":true}', response_id='unit_resume',
                additional_properties={'model':'gpt-6.1-sol'})
    async def exercise():
        async with asis_runtime.Runtime(client=Client(), agent_factory=Agent) as runtime:
            await asyncio.gather(runtime.ask('author','first A',session_id='lane-a'),
                                 runtime.ask('author','first B',session_id='lane-b'))
            await asyncio.gather(runtime.ask('author','resume A',session_id='lane-a'),
                                 runtime.ask('author','resume B',session_id='lane-b'))
        assert len(seen) == 2 and seen['lane-a'] is not seen['lane-b']
    asyncio.run(exercise())


def test_fresh_runtime_restores_approved_service_session_without_losing_history(tmp_path):
    """프로세스 재개 시 MAF가 새 SDK 세션을 만들지 않고 기존 service_session_id로 재개한다."""
    import asyncio,hashlib,types
    import asis_runtime
    seen=[]
    class Client:
        async def start(self):pass
        async def stop(self):pass
    class Agent:
        def __init__(self,**kwargs):pass
        async def run(self,messages,*,session,options=None):
            seen.append((session.session_id,session.service_session_id))
            assert session.service_session_id=='original-provider-session'
            return types.SimpleNamespace(text='{"ok":true}',response_id='fixture-restored',
                                         additional_properties={'model':asis_runtime.MODEL})
    instructions='approved original role instructions'
    entry={'session_id':'restored-author','role':'author','service_session_id':'original-provider-session',
        'instructions_sha256':hashlib.sha256(instructions.encode('utf-8')).hexdigest(),
        'requested_model':asis_runtime.MODEL,'actual_model':asis_runtime.MODEL,
        'requested_reasoning_effort':asis_runtime.REASONING_EFFORT,'fallback_used':False,'session_turn':3}
    async def exercise():
        async with asis_runtime.Runtime(client=Client(),agent_factory=Agent) as runtime:
            runtime.restore_existing_sessions([entry])
            _,receipt=await runtime.ask('author','resumed input',session_id=entry['session_id'],instructions=instructions)
            assert receipt['session_restored'] is True and receipt['session_turn']==4
            assert receipt['service_session_id']==entry['service_session_id']
    asyncio.run(exercise())
    assert seen==[('restored-author','original-provider-session')]
