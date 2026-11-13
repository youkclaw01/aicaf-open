"""기존 인프라 시작 경로도 포털 수명과 분리된 실제 AS-IS worker를 실행해야 한다."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace


def test_legacy_infra_start_launches_durable_worker():
    source=(Path(__file__).resolve().parents[1]/'server.py').read_text(encoding='utf-8')
    module=ast.parse(source)
    function=next(n for n in module.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='api_infra_start')
    events=[]
    class Control:
        def create(self,aid,body):
            return {'status':'queued','revision_id':'r-test'}
        def launch(self,aid,revision):
            events.append(('launch',aid,revision))
        def generate(self,aid,revision):
            events.append(('in_portal_process',aid,revision))
    class Request:
        match_info={'aid':'9'}
        app={'asis_controller':Control(),'asis_tasks':set()}
        user={'upn':'synthetic-unit-user'}
        async def json(self):return {'nonce':'synthetic-unit-nonce'}
        def __getitem__(self,key):return self.user if key=='user' else None
    scope={'asyncio':asyncio,'need_project':lambda *args:None,'audit':lambda *args:None,
        'web':SimpleNamespace(json_response=lambda row,status: (row,status))}
    exec(compile(ast.Module(body=[function],type_ignores=[]),'legacy-route-test','exec'),scope)
    async def invoke():
        request=Request()
        response=await scope['api_infra_start'](request)
        await asyncio.gather(*list(request.app['asis_tasks']))
        assert response[1]==202
    asyncio.run(invoke())
    assert events==[('launch',9,'r-test')]
