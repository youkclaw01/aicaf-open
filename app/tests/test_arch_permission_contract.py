"""현재 권한 함수와 arch API를 격리 SQLite·합성 저권한 계정으로 실행한다.

운영 인증·브라우저 E2E가 아니다. server 전체 import의 디렉터리·비밀 생성 부작용을
피하기 위해 현재 원문에서 순수 권한 함수와 DB 조회 함수만 AST로 읽어 실행한다.
"""
import ast
from contextlib import closing
import hashlib
import inspect
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
V = __import__('arch_view')
if Path(V.__file__).resolve() != (APP / 'arch_view.py').resolve():
    raise AssertionError('current_arch_view_import_path_mismatch')


def current_authorization(database):
    """공유 소스는 수정·전체 import하지 않고 실제 조회·권한 코드만 재사용한다."""
    source = APP / 'server.py'
    original = source.read_bytes()
    tree = ast.parse(original.decode('utf-8'), filename=str(source))
    names = {'db', 'user_roles', 'is_admin', 'project_role', 'is_deleted', 'need_project'}
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    constants = [node for node in tree.body if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == 'PROJ_RANK' for target in node.targets)]
    if {node.name for node in functions} != names or len(constants) != 1:
        raise AssertionError('current_authorization_source_contract_changed')
    namespace = {'DB': database, 'json': json, 'sqlite3': sqlite3, 'web': web}
    selected_body: list[ast.stmt] = [*constants, *functions]
    selected = ast.Module(body=selected_body, type_ignores=[])
    exec(compile(selected, str(source), 'exec'), namespace)
    namespace['source_sha256'] = hashlib.sha256(original).hexdigest()
    return namespace


class ArchitecturePermissionContract(unittest.IsolatedAsyncioTestCase):
    """실제 현재 권한 조회와 arch 핸들러를 서로 연결하되 운영 자원은 사용하지 않는다."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='a4-permission-unit-')
        self.root = Path(self.tmp.name)
        self.auth_db = self.root / 'authorization-fixture.sqlite3'
        with closing(sqlite3.connect(self.auth_db)) as database:
            with database:
                database.executescript('CREATE TABLE assessments(id INTEGER PRIMARY KEY, deleted_at TEXT);'
                                      'CREATE TABLE project_members(assessment_id INTEGER, username TEXT, role TEXT);')
                database.executemany('INSERT INTO assessments VALUES (?,?)', [(901, None), (902, None), (903, 'synthetic-deleted')])
                database.executemany('INSERT INTO project_members VALUES (?,?,?)',
                                     [(901, 'fixture-viewer', 'viewer'), (901, 'fixture-interviewer', 'interviewer'),
                                      (901, 'fixture-manager', 'manager'), (902, 'fixture-other', 'viewer'),
                                      (903, 'fixture-viewer', 'viewer')])
        self.authorization = current_authorization(self.auth_db)
        self.auth_connections=[]
        actual_db=self.authorization['db']
        def owned_db():
            connection=actual_db();self.auth_connections.append(connection);return connection
        self.authorization['db']=owned_db
        self.user: dict[str, str | list[str]] = {'upn': 'fixture-viewer', 'roles': '["aicaf.user"]'}
        self.audit = []
        self.launches = []
        self.state_patch = patch.object(V, 'S', {})
        self.state_patch.start()
        self.launch_patch = patch.object(V, '_launch', self.refuse_worker)
        self.launch_patch.start()

        @web.middleware
        async def fixture_identity(request, handler):
            """시험 계정의 신원을 주입하며 실제 로그인이라고 보고하지 않는다."""
            request['user'] = dict(self.user)
            return await handler(request)

        app = web.Application(middlewares=[fixture_identity])
        V.register(app, ROOT=self.root, need_project=self.authorization['need_project'],
                   assessment=lambda aid: {'customer': 'synthetic-permission-fixture', 'track': 'new', 'infra_case_id': None},
                   infra_case_dir=lambda case_id: self.root / 'unused-case',
                   audit=lambda *args: self.audit.append(args))
        self.files = {}
        for assessment in (901, 902):
            directory = self.root / 'arch' / str(assessment) / 'AS-IS_D1'
            directory.mkdir(parents=True)
            image = directory / 'image.png'
            image.write_bytes(('synthetic-original-assessment-' + str(assessment)).encode('ascii'))
            self.files[image] = image.read_bytes()
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    def refuse_worker(self, row):
        """권한 거부·자료 부재 시험에서는 작업 프로세스를 시작할 수 없다."""
        self.launches.append(row)
        raise AssertionError('worker_not_authorized_in_permission_unit')

    async def asyncTearDown(self):
        await self.client.close()
        self.launch_patch.stop()
        self.state_patch.stop()
        try:
            self.assertEqual(self.launches, [])
            self.assertEqual(self.audit, [])
            self.assertFalse((self.root / 'arch' / 'arch-store.sqlite3').exists())
            self.assertEqual({path: path.read_bytes() for path in self.files}, self.files)
            self.assertEqual(hashlib.sha256((APP / 'server.py').read_bytes()).hexdigest(),
                             self.authorization['source_sha256'])
        finally:
            for connection in self.auth_connections:
                connection.close()
                with self.assertRaises(sqlite3.ProgrammingError):connection.execute('SELECT 1')
            self.tmp.cleanup()

    async def test_viewer_can_read_owned_project_without_creating_arch_store(self):
        paths = ['/arch', '/arch/prompt', '/arch/AS-IS/D1/revisions',
                 '/arch/AS-IS/D1/view', '/arch/AS-IS/D1/file/image.png']
        for suffix in paths:
            with self.subTest(suffix=suffix):
                response = await self.client.get('/api/aicaf/assessments/901' + suffix)
                self.assertEqual(response.status, 200)
                await response.read()

    async def test_viewer_cannot_start_or_modify_prompt(self):
        start = await self.client.post('/api/aicaf/assessments/901/arch/AS-IS/D1/start', json={})
        self.assertEqual(start.status, 403)
        for roles in ('["aicaf.user"]', ['aicaf.user']):
            self.user['roles'] = roles
            with self.subTest(roles_type=type(roles).__name__):
                response = await self.client.put('/api/aicaf/assessments/901/arch/prompt', json={})
                self.assertEqual(response.status, 403)

    async def test_interviewer_can_enter_start_data_guard_but_not_approve_prompt(self):
        self.user = {'upn': 'fixture-interviewer', 'roles': ['aicaf.user']}
        start = await self.client.post('/api/aicaf/assessments/901/arch/AS-IS/D1/start', json={})
        self.assertEqual(start.status, 409)
        prompt = await self.client.put('/api/aicaf/assessments/901/arch/prompt', json={})
        self.assertEqual(prompt.status, 403)

    async def test_project_manager_without_system_admin_cannot_approve_prompt(self):
        self.user = {'upn': 'fixture-manager', 'roles': ['aicaf.user']}
        response = await self.client.put('/api/aicaf/assessments/901/arch/prompt', json={})
        self.assertEqual(response.status, 403)

    async def test_system_admin_reaches_validation_without_inventing_approved_prompt(self):
        self.user = {'upn': 'fixture-admin', 'roles': ['aicaf.admin']}
        response = await self.client.put('/api/aicaf/assessments/901/arch/prompt', json={})
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())['error'], 'invalid_prompt_id')

    async def test_unassigned_account_cannot_probe_reads_or_writes(self):
        self.user = {'upn': 'fixture-outsider', 'roles': ['aicaf.user']}
        for suffix in ('/arch', '/arch/prompt', '/arch/AS-IS/D1/revisions',
                       '/arch/AS-IS/D1/view', '/arch/AS-IS/D1/file/image.png'):
            with self.subTest(suffix=suffix):
                response = await self.client.get('/api/aicaf/assessments/901' + suffix)
                self.assertEqual(response.status, 404)
        start = await self.client.post('/api/aicaf/assessments/901/arch/AS-IS/D1/start', json={})
        prompt = await self.client.put('/api/aicaf/assessments/901/arch/prompt', json={})
        self.assertEqual(start.status, 404)
        self.assertEqual(prompt.status, 404)

    async def test_assigned_viewer_cannot_read_another_assessment(self):
        for suffix in ('/view', '/file/image.png'):
            with self.subTest(suffix=suffix):
                response = await self.client.get('/api/aicaf/assessments/902/arch/AS-IS/D1' + suffix)
                self.assertEqual(response.status, 404)

    async def test_deleted_project_is_hidden_from_assigned_viewer(self):
        response = await self.client.get('/api/aicaf/assessments/903/arch')
        self.assertEqual(response.status, 404)

    async def test_viewer_download_hash_conflict_preserves_original(self):
        base = '/api/aicaf/assessments/901/arch/AS-IS/D1/file/image.png'
        denied = await self.client.get(base + '?sha256=' + '0' * 64 + '&dl=1')
        self.assertEqual(denied.status, 409)
        payload = next(data for path, data in self.files.items() if '901' in path.parts)
        expected = hashlib.sha256(payload).hexdigest()
        allowed = await self.client.get(base + '?sha256=' + expected + '&dl=1')
        self.assertEqual(allowed.status, 200)
        self.assertEqual(await allowed.read(), payload)
        self.assertEqual(allowed.headers['X-Content-SHA256'], expected)
        self.assertTrue(allowed.headers['Content-Disposition'].startswith('attachment;'))

    async def test_arch_registration_reuses_current_viewer_interviewer_contract(self):
        signature = inspect.signature(self.authorization['need_project'])
        self.assertEqual(signature.parameters['at_least'].default, 'viewer')
        request = {'user': {'upn': 'fixture-viewer', 'roles': ['aicaf.user']}}
        self.assertEqual(self.authorization['need_project'](request, 901), 'viewer')
        with self.assertRaises(web.HTTPForbidden):
            self.authorization['need_project'](request, 901, 'interviewer')
        with self.assertRaises(web.HTTPNotFound):
            self.authorization['need_project'](request, 902)


if __name__ == '__main__':
    unittest.main()
