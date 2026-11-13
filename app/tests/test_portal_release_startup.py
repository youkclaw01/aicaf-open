"""운영 기준과 통합한 실제 포털의 기동·라우트 등록·인증 경계를 검증한다."""
from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer
import pytest


def test_real_portal_registers_workflow_once_without_bypassing_auth(tmp_path, monkeypatch):
    """모델·고객 저장소 없이 실제 서버를 기동하고 비인증 접근이 차단되는지 확인한다."""
    infra_code = Path(os.environ.get('AICAF_TEST_INFRA_CODE', 'C:/INFRA' if os.name == 'nt' else str(Path(__file__).resolve().parents[2]/'infra')))
    if not (infra_code/'collector/assessment_api').is_dir():
        pytest.skip('installed shared collector dependency is required for native portal startup')
    monkeypatch.syspath_prepend(str(infra_code/'collector'))
    monkeypatch.setenv('AICAF_INFRA_ROOT', str(tmp_path/'synthetic-infra'))
    monkeypatch.setenv('AICAF_DATA', str(tmp_path/'synthetic-data'))
    monkeypatch.setenv('AICAF_AUTH', 'local')
    monkeypatch.setenv('AICAF_FD_ENDPOINT', 'https://unit.invalid')
    monkeypatch.setenv('AICAF_FD_RESOURCE_ID', '/synthetic-unit-resource')
    path = Path(__file__).resolve().parents[1]/'server.py'
    spec = importlib.util.spec_from_file_location('portal_release_startup_unit', path)
    assert spec is not None and spec.loader is not None
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    app = server.make_app()
    route = '/api/aicaf/assessments/{aid}/workflow'
    matches = [r for r in app.router.routes() if r.method == 'GET' and r.resource.canonical == route]
    assert len(matches) == 1
    assert server._WORKFLOW is not None
    assert server._WORKFLOW.current_state.root != server._WORKFLOW.interview.root

    async def exercise():
        async with TestClient(TestServer(app)) as client:
            response = await client.get('/api/aicaf/assessments/97/workflow', allow_redirects=False)
            assert response.status in (302, 401, 403)
            assert 'reports' not in await response.text()
            view_resp = await client.get('/api/aicaf/assessments/26/arch/AS-IS/D1/view?rev=legacy',
                                         headers={'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                                                  'Sec-Fetch-Mode': 'navigate'},
                                         allow_redirects=False)
            assert view_resp.status == 302
            assert view_resp.headers.get('Location', '').startswith('/login?next=')
            assert 'arch/AS-IS/D1/view' in view_resp.headers.get('Location', '')
            response = await client.get('/login', allow_redirects=False)
            assert response.status == 200
    asyncio.run(exercise())
