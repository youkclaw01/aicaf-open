"""아키텍처 전용 경계 시험. 실제 운영/모델/브라우저 시험으로 표시하지 않는다."""
import json
import tempfile
import unittest
from pathlib import Path
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import arch_view as V


class RevisionViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        @web.middleware
        async def auth(req, handler):
            req['user'] = {'upn': 'fixture-admin', 'roles': '["aicaf.admin"]'}
            return await handler(req)
        def need(req, aid, role='viewer'):
            if aid != 501:
                raise web.HTTPNotFound()
            return 'manager'
        app = web.Application(middlewares=[auth])
        V.register(app, ROOT=self.root, need_project=need,
                   assessment=lambda aid: {'customer': '합성 시험', 'track': 'new', 'infra_case_id': None},
                   infra_case_dir=lambda cid: self.root / 'case', audit=lambda *a: None)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        p = self.root / 'arch/501/AS-IS_D1'
        p.mkdir(parents=True)
        (p / 'image.png').write_bytes(b'fixture-original-bytes')
        (p / 'job.json').write_text(json.dumps({'status': 'done', 'checks': {}}), encoding='utf-8')

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def test_preview_has_close_return_original_and_no_fake_ppt_preview(self):
        r = await self.client.get('/api/aicaf/assessments/501/arch/AS-IS/D1/view')
        self.assertEqual(r.status, 200)
        body = await r.text()
        self.assertIn('window.close()', body)
        self.assertIn('원래 화면', body)
        self.assertIn('이미지 원본', body)
        self.assertIn('PPT 변환본', body)
        self.assertNotIn('src="/api/aicaf/assessments/501/arch/AS-IS/D1/file/preview.png', body)

    async def test_new_preview_keeps_project_permission_boundary(self):
        r = await self.client.get('/api/aicaf/assessments/502/arch/AS-IS/D1/view')
        self.assertEqual(r.status, 404)

    async def test_file_sha_conflict_does_not_serve_changed_bytes(self):
        r = await self.client.get('/api/aicaf/assessments/501/arch/AS-IS/D1/file/image.png?sha256=' + '0' * 64)
        self.assertEqual(r.status, 409)

    async def test_prompt_api_exposes_readiness_not_invented_default(self):
        r = await self.client.get('/api/aicaf/assessments/501/arch/prompt')
        self.assertEqual(r.status, 200)
        data = await r.json()
        self.assertFalse(data['ready'])
        self.assertIsNone(data['prompt'])


if __name__ == '__main__':
    unittest.main()
