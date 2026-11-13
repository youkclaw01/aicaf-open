"""작성·검토·시각 역할의 SDK 프로세스 격리와 종료 예외 보존을 검증한다."""
import asyncio,sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from asis_runtime import Runtime

class Client:
    def __init__(self,**kw):self.kw=kw;self.starts=0;self.stops=0
    async def start(self):self.starts+=1
    async def stop(self):self.stops+=1

def test_role_clients_isolate_sdk_history_and_stay_stable(tmp_path,monkeypatch):
    monkeypatch.setenv('GITHUB_TOKEN','unit-test-token')
    async def run():
        async with Runtime(client_factory=Client,agent_factory=lambda **kw:None,base_directory=tmp_path/'copilot') as r:
            author=await r.client_for_role('author');review=await r.client_for_role('content_review');visual=await r.client_for_role('visual_review')
            assert author is await r.client_for_role('author')
            assert author is not review and review is not visual
            assert author.kw['base_directory'].endswith('author')
            assert review.kw['base_directory'].endswith('content_review')
            assert author.starts==review.starts==visual.starts==1
        assert author.stops==review.stops==visual.stops==1
    asyncio.run(run())

def test_sdk_shutdown_failure_does_not_hide_original_error():
    class Bad(Client):
        async def stop(self):raise RuntimeError('stop-failed')
    async def run():
        with pytest.raises(ValueError,match='original-failure'):
            async with Runtime(client=Bad(),agent_factory=lambda **kw:None):raise ValueError('original-failure')
    asyncio.run(run())


def test_completed_context_survives_detach_timeout_after_sdk_force_stop(tmp_path):
    """실제 장애 경계: 성공 결과를 종료 정리 시간초과로 바꾸지 않는다. SDK 대역 시험이다."""
    class TimeoutClient(Client):
        def __init__(self):super().__init__();self.forces=0
        async def stop(self):self.stops+=1;raise TimeoutError('detach-timeout')
        async def force_stop(self):self.forces+=1
    async def run():
        client=TimeoutClient();r=Runtime(client=client,agent_factory=lambda **kw:None,base_directory=tmp_path/'sessions')
        async with r:result={'already_verified':True,'artifact_sha256':'synthetic-unit-only'}
        assert result['already_verified'] is True
        assert client.stops==1 and client.forces==1
        assert r.shutdown_records[0]['normal_stop']=='TimeoutError'
        assert r.shutdown_records[0]['force_stop']=='completed'
        record=__import__('json').loads((tmp_path/'sessions/shutdown.json').read_text(encoding='utf-8'))
        assert record['context_body_failed'] is False and record['all_clients_closed'] is True
        assert record['quality_records_modified'] is False
    asyncio.run(run())


def test_real_stop_deadline_uses_force_stop_without_extending_timeout():
    class Blocking(Client):
        def __init__(self):super().__init__();self.forces=0
        async def stop(self):await asyncio.Event().wait()
        async def force_stop(self):self.forces+=1
    async def run():
        client=Blocking();r=Runtime(client=client,agent_factory=lambda **kw:None)
        r.stop_timeout_seconds=0.01;r.force_stop_timeout_seconds=0.1
        async with r:pass
        assert client.forces==1 and r.shutdown_records[0]['normal_stop']=='TimeoutError'
    asyncio.run(run())


def test_force_stop_failure_is_not_hidden_as_success():
    class Broken(Client):
        async def stop(self):raise TimeoutError('normal-stop-failed')
        async def force_stop(self):raise RuntimeError('force-stop-failed')
    async def run():
        r=Runtime(client=Broken(),agent_factory=lambda **kw:None)
        with pytest.raises(TimeoutError,match='normal-stop-failed'):
            async with r:pass
        assert r.shutdown_records[0]['force_stop']=='RuntimeError'
    asyncio.run(run())


def test_original_task_failure_remains_original_when_forced_cleanup_succeeds():
    class BrokenStop(Client):
        def __init__(self):super().__init__();self.forces=0
        async def stop(self):raise TimeoutError('detach-timeout')
        async def force_stop(self):self.forces+=1
    async def run():
        client=BrokenStop();r=Runtime(client=client,agent_factory=lambda **kw:None)
        with pytest.raises(ValueError,match='actual-task-failure'):
            async with r:raise ValueError('actual-task-failure')
        assert client.forces==1
    asyncio.run(run())


def test_healthy_stop_does_not_force_close_client():
    class Healthy(Client):
        async def force_stop(self):raise AssertionError('unnecessary_force_stop')
    async def run():
        r=Runtime(client=Healthy(),agent_factory=lambda **kw:None)
        async with r:pass
        assert r.shutdown_records[0]['normal_stop']=='completed'
        assert r.shutdown_records[0]['force_stop']=='not_needed'
    asyncio.run(run())


def test_unavailable_force_stop_still_fails_completed_context():
    class MissingForce(Client):
        async def stop(self):raise TimeoutError('cannot-close-owned-client')
    async def run():
        r=Runtime(client=MissingForce(),agent_factory=lambda **kw:None)
        with pytest.raises(TimeoutError,match='cannot-close-owned-client'):
            async with r:pass
        assert r.shutdown_records[0]['force_stop']=='unavailable'
    asyncio.run(run())


def test_force_stop_only_targets_failed_role_and_closes_remaining_roles():
    class RoleClient(Client):
        def __init__(self,broken=False):super().__init__();self.broken=broken;self.forces=0
        async def stop(self):
            self.stops+=1
            if self.broken:raise TimeoutError('role-detach-timeout')
        async def force_stop(self):self.forces+=1
    async def run():
        roles={'author':RoleClient(True),'content_review':RoleClient(),'visual_review':RoleClient()}
        r=Runtime(client_factory=Client,agent_factory=lambda **kw:None)
        r.role_clients=roles
        await r.__aexit__(None,None,None)
        assert all(c.stops==1 for c in roles.values())
        assert {k:c.forces for k,c in roles.items()}=={'author':1,'content_review':0,'visual_review':0}
        assert len(r.shutdown_records)==3
    asyncio.run(run())
