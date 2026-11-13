"""실제 Runtime의 적응형 진입·역할 대화 잠금과 호출시각 계약 시험."""
import asyncio
from pathlib import Path
import sys
import types
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from asis_runtime import Runtime
from asis_admission import AdaptiveAdmission


def free_resources():
    return {'total_memory_bytes': 100000, 'available_memory_bytes': 85000,
            'cpu_percent': 8, 'logical_cpu_count': 2, 'worker_tree_rss_bytes': 1000,
            'sample_valid': True}


class Client:
    async def start(self): pass
    async def stop(self): pass


def test_default_runtime_adopts_resource_controller_not_fixed_two():
    runtime = Runtime(client=Client(), agent_factory=lambda **kw: None)
    assert isinstance(runtime.gate, AdaptiveAdmission), 'production still uses fixed call semaphore'
    assert runtime.gate.ceiling is None, 'production default retains an arbitrary call ceiling'


def test_adaptive_runtime_really_overlaps_more_than_two_and_keeps_same_session_serial():
    active = {'all': 0, 'peak': 0, 'same': {}}
    seen = []
    class Agent:
        def __init__(self, **kwargs): pass
        async def run(self, message, *, session, options=None):
            identifier = session.session_id
            active['same'][identifier] = active['same'].get(identifier, 0)+1
            assert active['same'][identifier] == 1, 'the same provider conversation ran twice concurrently'
            active['all'] += 1; active['peak'] = max(active['peak'], active['all'])
            await asyncio.sleep(.025)
            active['all'] -= 1; active['same'][identifier] -= 1
            session.service_session_id = 'offline-'+identifier
            seen.append(identifier)
            return types.SimpleNamespace(text='{"synthetic_only":true}', response_id='offline-'+str(len(seen)),
                additional_properties={'model':'gpt-6.1-sol'}, usage_details={})
    gate = AdaptiveAdmission(sampler=free_resources, interval=.001)
    async def exercise():
        async with Runtime(client=Client(), agent_factory=Agent, admission=gate) as runtime:
            await asyncio.gather(*(runtime.ask('author', 'synthetic request', session_id='lane-'+str(i))
                for i in range(10)), runtime.ask('author', 'same lane followup', session_id='lane-0'))
        assert len(runtime.calls) == 11
        assert all(row['queued_at'] <= row['admitted_at'] <= row['request_started_at'] <= row['completed_at']
                   for row in runtime.calls), 'queue and request clocks are not distinguishable'
        assert gate.active == gate.waiting == 0
    asyncio.run(exercise())
    assert active['peak'] > 2
    assert active['all'] == 0 and seen.count('lane-0') == 2


def test_runtime_marks_resource_admission_failure_before_sdk_and_preserves_safe_code(tmp_path):
    """SDK 진입 전 대기 실패를 실행 중으로 남기거나 원인을 누락하지 않는다."""
    import json
    import pytest
    executions = []
    class Agent:
        def __init__(self, **kwargs): pass
        async def run(self, *args, **kwargs):
            executions.append(True)
            raise AssertionError('SDK must not start while admission is unavailable')
    def unavailable():
        return {**free_resources(),'available_memory_bytes':0}
    gate = AdaptiveAdmission(sampler=unavailable, interval=.001, max_wait=.004)
    async def exercise():
        async with Runtime(client=Client(),agent_factory=Agent,admission=gate,base_directory=tmp_path/'sessions') as runtime:
            with pytest.raises(RuntimeError,match='resource_admission_wait_exhausted'):
                await runtime.ask('author','private-fixture-content-not-for-logs',session_id='waiting-writer')
            row = runtime.activity['waiting-writer']
            assert row['state'] == 'failed', 'resource admission errors leave their caller in resource_wait forever'
            assert row['failure_boundary'] == 'resource_admission'
            assert row['failure_code'] == 'resource_admission_wait_exhausted'
            assert not executions and runtime.calls == []
        records = list((tmp_path/'runtime-failures').glob('*.json'))
        assert len(records) == 1
        saved = json.loads(records[0].read_text(encoding='utf-8'))
        assert saved['failure_code'] == row['failure_code'] and saved['model_request_started'] is False
        assert 'private-fixture-content' not in str(saved)
        assert gate.active == gate.waiting == 0
    asyncio.run(exercise())
def test_provider_429_holds_other_requests_and_never_retries_failed_model_call(tmp_path):
    """공급자 제한을 실제 Runtime 예외 경로에서 연결한다. 작업의 재시도 예산은 바꾸지 않는다."""
    import pytest
    class Limited(Exception):
        status_code = 429
        retry_after = .03
    executions = []
    class Agent:
        def __init__(self, **kwargs): pass
        async def run(self, message, *, session, options=None):
            executions.append(session.session_id)
            raise Limited('sanitized_provider_limit')
    gate = AdaptiveAdmission(sampler=free_resources, interval=.001, folder=tmp_path)
    async def exercise():
        async with Runtime(client=Client(), agent_factory=Agent, admission=gate) as runtime:
            with pytest.raises(Limited):
                await runtime.ask('author','synthetic request',session_id='limited')
        assert len(runtime.calls) == 0
    asyncio.run(exercise())
    assert executions == ['limited']
    assert any(row.get('event') == 'provider_backpressure' for row in gate.history)
    assert gate.active == gate.waiting == 0


def test_runtime_records_role_state_and_sanitized_failed_call_without_retry():
    """중앙 관리자는 실제 역할의 대기·실행·오류를 확인하고 민감한 예외 원문은 저장하지 않는다."""
    class Limited(Exception):
        status_code=429
        retry_after=.01
    class Agent:
        def __init__(self,**kwargs):pass
        async def run(self,*args,**kwargs):raise Limited('credential-like-value-must-not-be-recorded')
    gate=AdaptiveAdmission(sampler=free_resources,interval=.001)
    async def exercise():
        import pytest
        async with Runtime(client=Client(),agent_factory=Agent,admission=gate) as runtime:
            with pytest.raises(Limited):await runtime.ask('author','test input',session_id='writer-state')
            row=runtime.activity['writer-state']
            assert row['state']=='failed' and row['role']=='author'
            assert row['provider_backpressure'] is True
            assert row['queued_at'] <= row['admitted_at'] <= row['request_started_at'] <= row['failed_at']
            assert 'credential-like-value' not in str(row)
            assert row['error_type']=='Limited'
    asyncio.run(exercise())


def test_runtime_readiness_fingerprint_includes_adaptive_admission(tmp_path, monkeypatch):
    """자원 스케줄러를 변경한 뒤 과거 readiness를 사용하지 않는다."""
    import asis_runtime
    import shutil
    app = Path(asis_runtime.__file__).resolve().parent
    # 운영 코드의 공식 봉인 대상을 사용해 신규 파일·자산도 빠뜨리지 않는다.
    from _sealed_app import copy_implementation_tree
    copy_implementation_tree(app,tmp_path)
    monkeypatch.setattr(asis_runtime, '__file__', str(tmp_path/'asis_runtime.py'))
    before=asis_runtime.implementation_fingerprint()
    with (tmp_path/'asis_admission.py').open('a',encoding='utf-8') as file:
        file.write('\n# 자원 진입 정책 변경 시험\n')
    assert before and before != asis_runtime.implementation_fingerprint()


def test_runtime_readiness_fingerprint_includes_central_task_contract(tmp_path, monkeypatch):
    """중앙 작업 원장 변경도 과거 운영 준비 판정을 무효화한다."""
    import asis_runtime,shutil
    app=Path(asis_runtime.__file__).resolve().parent
    from _sealed_app import copy_implementation_tree
    copy_implementation_tree(app,tmp_path)
    monkeypatch.setattr(asis_runtime,'__file__',str(tmp_path/'asis_runtime.py'))
    before=asis_runtime.implementation_fingerprint()
    with (tmp_path/'asis_tasks.py').open('a',encoding='utf-8') as file:file.write('\n# 중앙 완료 계약 변경 시험\n')
    assert before and before!=asis_runtime.implementation_fingerprint()
