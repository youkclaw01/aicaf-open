"""분석 담당 채팅의 실제 HTTP 경계: 조회는 viewer, 결정은 interviewer 이상, 잘못된 결정은 상태를 바꾸지 않는다."""
from pathlib import Path
import asyncio, json, sys, tempfile, unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import asis_view
from asis_chat import Chat

REVISION = 'r-20261014T025532-450c857f53afbacf'
BASE = '/api/aicaf/assessments/24/asis/revisions/' + REVISION + '/chat'
OPTIONS = [{'id': 'defer', 'label': '병합 검토로 넘긴다', 'action': 'defer_to_merge'},
           {'id': 'retry', 'label': '작성자에게 되돌린다', 'action': 'retry_author'}]


class ChatHttp(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.role = 'manager'
        self.audit = []

        def need_project(request, aid, role='viewer'):
            rank = {'viewer': 0, 'interviewer': 1, 'manager': 2}
            actual = request['user']['role']
            if rank[actual] < rank[role]:
                raise web.HTTPForbidden(text='권한이 없습니다.')
            return actual

        @web.middleware
        async def identity(request, handler):
            request['user'] = {'upn': 'fixture-' + self.role, 'role': self.role}
            return await handler(request)

        app = web.Application(middlewares=[identity])
        asis_view.register(app, ROOT=self.root, need_project=need_project,
                           assessment=lambda aid: {'customer': 'synthetic', 'track': 'existing', 'infra_case_id': None},
                           infra_case_dir=lambda case_id: self.root / 'unused', db=lambda: None,
                           audit=lambda *args: self.audit.append(args))
        self.folder = self.root / 'asis' / '24' / 'revisions' / REVISION
        self.folder.mkdir(parents=True)
        self.chat = Chat(self.folder)
        self.question = self.chat.ask(sender='area_owner', task_key='area-cost-O_P0226', summary='검토자가 막혔습니다.',
                                      options=OPTIONS, evidence=[{'path': 'checkpoints/x'}])
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def test_viewer_can_read_but_cannot_decide(self):
        self.role = 'viewer'
        read = await self.client.get(BASE)
        self.assertEqual(read.status, 200)
        body = await read.json()
        self.assertEqual(len(body['open_questions']), 1)
        self.assertFalse(body['closed'])
        denied = await self.client.post(BASE + '/questions/' + self.question['id'] + '/decision', json={'option_id': 'defer'})
        self.assertEqual(denied.status, 403)
        self.assertEqual(Chat(self.folder).question(self.question['id'])['status'], 'open')
        self.assertEqual(self.audit, [], 'a denied decision must not be audited as an applied change')

    async def test_interviewer_decision_is_recorded_once_and_audited(self):
        self.role = 'interviewer'
        url = BASE + '/questions/' + self.question['id'] + '/decision'
        first = await self.client.post(url, json={'option_id': 'defer'})
        self.assertEqual(first.status, 200)
        self.assertEqual((await first.json())['decision']['action'], 'defer_to_merge')
        resend = await self.client.post(url, json={'option_id': 'defer'})
        self.assertEqual(resend.status, 200)
        conflict = await self.client.post(url, json={'option_id': 'retry'})
        self.assertEqual(conflict.status, 409)
        self.assertEqual(len([m for m in Chat(self.folder).messages() if m['kind'] == 'decision']), 1)
        self.assertEqual(self.audit[0][3], 'asis-chat-decision')

    async def test_bad_decisions_return_client_errors_and_leave_the_question_open(self):
        self.role = 'manager'
        url = BASE + '/questions/' + self.question['id'] + '/decision'
        self.assertEqual((await self.client.post(url, json={'option_id': 'made-up'})).status, 400)
        self.assertEqual((await self.client.post(url, json={'option_id': 'defer', 'free_text': 'both'})).status, 400)
        self.assertEqual((await self.client.post(url, json=['not', 'an', 'object'])).status, 400)
        self.assertEqual((await self.client.post(BASE + '/questions/q-unknown/decision', json={'option_id': 'defer'})).status, 404)
        self.assertEqual(Chat(self.folder).question(self.question['id'])['status'], 'open')

    async def test_revision_id_is_validated_and_polling_returns_only_new_messages(self):
        self.role = 'viewer'
        self.assertEqual((await self.client.get('/api/aicaf/assessments/24/asis/revisions/not-a-revision/chat')).status, 404)
        first = await (await self.client.get(BASE)).json()
        last = first['messages'][-1]['seq']
        self.chat.post(sender='analysis_owner', text='새 메시지')
        later = await (await self.client.get(BASE + '?after=' + str(last))).json()
        self.assertEqual([m['text'] for m in later['messages']], ['새 메시지'])
        self.assertEqual((await self.client.get(BASE + '?after=abc')).status, 400)


if __name__ == '__main__':
    unittest.main()
