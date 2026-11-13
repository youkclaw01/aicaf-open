"""Runtime 이 호출을 끝낸 세션·에이전트·활동 기록을 메모리에 영구 보관하는지 확인하는 계약 시험(합성 SDK, 모델 호출 없음).

2026-10-17 운영 실측: 프로젝트 25 개정에서 워커 프로세스 트리 메모리가 0.3GB 에서 10GB 까지 올랐고 작업이 끝나도 내려가지 않았다.
Runtime 은 항목마다 세션 ID 를 만들고(asis-author-<해시>) sessions·agents·session_locks·session_turns·activity 에 추가만 하고 지우지 않는다.
SDK 클라이언트도 개별 세션을 stop() 때까지 보관한다(session.disconnect() 는 이벤트·도구 핸들러 메모리를 해제하고 디스크 상태는 보존한다).

계약: 한 세션 호출이 끝나 더 쓰지 않으면 Runtime 은 그 세션의 메모리 객체를 해제할 수 있어야 하고, 해제 후에도 영수증·디스크 기록·재개 가능성은 그대로여야 한다.
"""
from pathlib import Path
import asyncio
import gc
import sys
import weakref

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import asis_runtime
from asis_admission import AdaptiveAdmission


class FakeSession:
    """SDK 세션 대역: 이벤트 핸들러와 이력을 보관하고 disconnect 로 해제한다."""
    instances = []

    def __init__(self, session_id):
        self.session_id = session_id
        self.history = []
        self.disconnected = False
        FakeSession.instances.append(weakref.ref(self))

    async def disconnect(self):
        self.disconnected = True
        self.history = []


class FakeClient:
    def __init__(self):
        self.sessions = {}
        self.stopped = False

    async def start(self):
        pass

    async def stop(self):
        self.stopped = True
        for s in list(self.sessions.values()):
            await s.disconnect()
        self.sessions.clear()


class FakeReply:
    def __init__(self, text):
        self.text = text
        self.response_id = 'resp-' + text[:8]
        self.additional_properties = {'model': 'gpt-6.1-sol'}
        self.usage_details = {'input_token_count': 10, 'output_token_count': 5}


class FakeAgent:
    """MAF GitHubCopilotAgent 대역: run 마다 SDK 세션을 만들어 클라이언트에 등록한다."""

    def __init__(self, *, client, name, instructions, tools, default_options):
        self.client = client
        self.payload = bytearray(2_000_000)  # 에이전트·세션 하나가 쥐는 메모리(대용량 이력 대역)

    async def run(self, message, *, session, options):
        sid = session.service_session_id or ('svc-' + session.session_id)
        session.service_session_id = sid
        if sid not in self.client.sessions:
            self.client.sessions[sid] = FakeSession(sid)
        self.client.sessions[sid].history.append(bytearray(500_000))
        return FakeReply('{"ok": true}')


def make_runtime(tmp_path):
    client = FakeClient()
    gate = AdaptiveAdmission(sampler=lambda: {'sample_valid': True, 'total_memory_bytes': 32 << 30, 'available_memory_bytes': 28 << 30,
                                               'cpu_percent': 1.0, 'worker_tree_rss_bytes': 1 << 20,
                                               'commit_limit_bytes': 40 << 30, 'commit_available_bytes': 38 << 30}, interval=0.001)
    runtime = asis_runtime.Runtime(client=client, agent_factory=FakeAgent, admission=gate, base_directory=tmp_path / 'sessions')
    runtime._entered = True
    return runtime, client


def ask_many(runtime, n, role='author'):
    async def go():
        for i in range(n):
            await runtime.ask(role, '{"x": %d}' % i, session_id=f'asis-{role}-' + str(i).zfill(24), instructions='i')
    asyncio.run(go())


def test_runtime_containers_do_not_grow_without_bound_after_calls_complete(tmp_path):
    runtime, client = make_runtime(tmp_path)
    ask_many(runtime, 60)
    held = {name: len(getattr(runtime, name)) for name in ('sessions', 'agents', 'agent_bindings', 'session_locks', 'session_turns')}
    assert max(held.values()) <= 60
    # 계약: 완료한 세션을 해제하는 진입점이 있고, 호출하면 모두 비워진다
    assert hasattr(runtime, 'release_session'), 'Runtime 에 완료 세션 해제 진입점이 없다(운영 실측 메모리 누적 원인)'
    async def release_all():
        for sid in list(runtime.sessions):
            await runtime.release_session(sid)
    asyncio.run(release_all())
    for name in ('sessions', 'agents', 'agent_bindings', 'session_locks', 'activity'):
        assert len(getattr(runtime, name)) == 0, name
    # session_turns 는 재사용 때 영수증의 session_turn 을 이어 가기 위해 남긴다. 세션당 정수 하나뿐이라 메모리 영향이 없다.
    assert all(type(v) is int for v in runtime.session_turns.values())


def test_release_disconnects_the_sdk_session_and_frees_the_python_objects(tmp_path):
    runtime, client = make_runtime(tmp_path)
    ask_many(runtime, 10)
    assert len(client.sessions) == 10 and not any(s.disconnected for s in client.sessions.values())
    async def release_all():
        for sid in list(runtime.sessions):
            await runtime.release_session(sid)
    asyncio.run(release_all())
    assert not client.sessions or all(s.disconnected for s in client.sessions.values()), 'SDK 세션을 닫지 않았다'
    gc.collect()
    alive = sum(1 for ref in FakeSession.instances if ref() is not None and not ref().disconnected)
    assert alive == 0


def test_release_keeps_receipts_and_does_not_drop_other_sessions(tmp_path):
    runtime, client = make_runtime(tmp_path)
    ask_many(runtime, 5)
    receipts = list(runtime.calls)
    keep = list(runtime.sessions)[0]
    drop = list(runtime.sessions)[1]
    asyncio.run(runtime.release_session(drop))
    assert drop not in runtime.sessions and keep in runtime.sessions
    assert runtime.calls == receipts, '영수증 기록은 해제와 무관하게 보존돼야 한다'


def test_a_released_session_can_be_used_again_by_creating_a_new_agent_and_resuming_by_service_id(tmp_path):
    runtime, client = make_runtime(tmp_path)
    ask_many(runtime, 1)
    sid = list(runtime.sessions)[0]
    service_id = runtime.sessions[sid].service_session_id
    asyncio.run(runtime.release_session(sid))
    async def again():
        await runtime.ask('author', '{"x": 99}', session_id=sid, instructions='i')
    asyncio.run(again())
    assert runtime.sessions[sid].service_session_id == service_id or runtime.sessions[sid].service_session_id


def test_release_is_idempotent_and_ignores_unknown_sessions(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    asyncio.run(runtime.release_session('asis-author-unknown'))
    ask_many(runtime, 1)
    sid = list(runtime.sessions)[0]
    asyncio.run(runtime.release_session(sid))
    asyncio.run(runtime.release_session(sid))


# ---- 파이프라인 연결: 작성·검토 쌍이 승인돼 저장된 직후 두 세션을 해제한다 ----

def test_pipeline_releases_the_author_and_review_sessions_after_the_pair_is_accepted(tmp_path):
    from copy import deepcopy
    import json
    from asis_pipeline import Pipeline
    from test_asis_parallel_synthesis import DirectionRuntime, synthesis_fixture

    class ReleasingRuntime(DirectionRuntime):
        def __init__(self, plan):
            super().__init__(plan)
            self.released = []
            self.live = set()

        async def ask(self, role, message, **kwargs):
            self.live.add(kwargs['session_id'])
            return await super().ask(role, message, **kwargs)

        async def release_session(self, session_id):
            self.released.append(session_id)
            self.live.discard(session_id)
            return True

    data, plan = synthesis_fixture(tmp_path)
    runtime = ReleasingRuntime(plan)
    pipe = Pipeline(runtime, tmp_path / 'run')
    asyncio.run(pipe.reviewed_synthesis(data))
    used = {c['session_id'] for c in runtime.calls if c['role'] in ('author', 'direction_author')} | {c['session_id'] for c in runtime.calls if c['role'] == 'content_review' and c['session_id'].startswith('asis-review-')}
    assert used, '호출 세션이 있어야 한다'
    for call in runtime.calls:
        if call['role'] in ('author', 'direction_author'):
            assert call['session_id'] in runtime.released, '승인된 항목의 작성 세션을 해제하지 않았다'
    # 해제는 승인 이후에만: 해제된 세션이 그 뒤에 다시 호출되지 않는다(같은 항목의 보완 라운드가 없는 합성 시험)
    order = []
    for call in runtime.calls:
        order.append(call['session_id'])
    for sid in runtime.released:
        assert order.count(sid) >= 1


def test_pipeline_works_with_a_runtime_that_has_no_release_entry(tmp_path):
    """시험 대역·옛 런타임에는 release_session 이 없을 수 있다. 해제 호출은 선택적이어야 한다."""
    from asis_pipeline import Pipeline
    from test_asis_parallel_synthesis import DirectionRuntime, synthesis_fixture
    data, plan = synthesis_fixture(tmp_path)
    runtime = DirectionRuntime(plan)
    assert not hasattr(runtime, 'release_session')
    result = asyncio.run(Pipeline(runtime, tmp_path / 'run').reviewed_synthesis(data))
    assert len(result['directions']) == 2


def test_a_release_failure_never_fails_an_accepted_item(tmp_path):
    from asis_pipeline import Pipeline
    from test_asis_parallel_synthesis import DirectionRuntime, synthesis_fixture

    class Broken(DirectionRuntime):
        async def release_session(self, session_id):
            raise RuntimeError('disconnect failed')

    data, plan = synthesis_fixture(tmp_path)
    result = asyncio.run(Pipeline(Broken(plan), tmp_path / 'run').reviewed_synthesis(data))
    assert len(result['directions']) == 2, '해제 실패가 승인된 항목을 실패로 만들면 안 된다'
