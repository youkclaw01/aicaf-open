"""광고된 Chat Completions 모델을 MAF의 고정 에이전트·직렬화 세션으로 실행한다."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from copy import deepcopy
from email.utils import parsedate_to_datetime
from hashlib import sha256
import inspect
import json
import math
import os
from pathlib import Path
import time
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse
import urllib.error
import urllib.request
from uuid import uuid4


def normalize_usage(usage):
    """표시 출력과 별도 출력의 합을 보존한다. 공급자 총량보다 작게 계량하지 않는다."""
    value = dict(usage or {})
    names = ('input_token_count', 'output_token_count', 'total_token_count')
    if any(type(value.get(key)) is not int or value[key] < 0 for key in names):
        raise ValueError('invalid_chat_usage')
    inp, out, total = (value[key] for key in names)
    cache = value.get('cache_read_input_token_count', value.get('prompt/cached_tokens', 0)) or 0
    if type(cache) is not int or not 0 <= cache <= inp or total < inp + out:
        raise ValueError('invalid_chat_usage')
    value.update(provider_reported_output_token_count=out, output_token_count=total-inp,
                 non_display_output_token_count=total-inp-out, cache_read_input_token_count=cache,
                 output_accounting_rule='provider_total_minus_input')
    return value


def _hash(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def _atomic(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + str(os.getpid()) + '.' + uuid4().hex + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    for attempt in range(7):
        try:
            os.replace(temp, path); return
        except PermissionError:
            if attempt == 6:
                raise
            time.sleep(.05 * (attempt + 1))


def _trusted_endpoint(endpoint):
    parsed = urlparse(endpoint)
    if (parsed.scheme != 'https' or not parsed.hostname
            or not (parsed.hostname == 'api.githubcopilot.com' or parsed.hostname.endswith('.githubcopilot.com'))
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/')):
        raise ValueError('untrusted_copilot_chat_endpoint')
    return endpoint.rstrip('/')


class ProviderAdmission:
    """호스트 예약과 별도로 실제 HTTP 진입만 조절한다. 성공 시 확대하고 429 수신 시 축소한다."""
    def __init__(self, *, timeout, resource_admission=None, before_send=None, state_path=None, binding=None):
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise ValueError('invalid_provider_deadline')
        self.timeout = float(timeout)
        self.resource = resource_admission; self.before_send = before_send
        self.state_path = Path(state_path) if state_path else None
        self.binding = binding
        self.capacity = 1; self.active = 0; self.successes = 0; self.blocked_until = 0.
        self.pressure_generation = 0
        self.model_lease: ContextVar[dict[str, Any] | None] = ContextVar('provider_model_lease', default=None)
        self.condition = asyncio.Condition(); self.queue = []
        if self.state_path and self.state_path.is_file():
            old = json.loads(self.state_path.read_text(encoding='utf-8'))
            if old.get('binding') == binding and type(old.get('capacity')) is int and old['capacity'] > 0:
                self.capacity = old['capacity']

    def _record(self, event, **details):
        if self.state_path:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            row = {'at_epoch': time.time(), 'event': event, 'active_http': self.active,
                   'waiting_http': len(self.queue), 'capacity': self.capacity, **details}
            with self.state_path.with_name('provider-admission.jsonl').open('a', encoding='utf-8') as file:
                file.write(json.dumps(row, ensure_ascii=True) + '\n')
            if event in ('provider_limited', 'provider_transient_http_error', 'capacity_increased'):
                _atomic(self.state_path, {'binding': self.binding, 'capacity': self.capacity})

    def _resource_target(self):
        target = getattr(self.resource, 'target', self.capacity + 1)
        return target if type(target) is int and target > 0 else self.capacity

    @asynccontextmanager
    async def _slot(self, deadline):
        ticket = object(); acquired = False
        async with self.condition:
            self.queue.append(ticket)
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('provider_admission_wait_exhausted')
                    pause = max(0., self.blocked_until - time.monotonic())
                    if self.queue[0] is ticket and not pause and self.active < min(self.capacity, self._resource_target()):
                        self.queue.pop(0); self.active += 1; acquired = True; break
                    try:
                        await asyncio.wait_for(self.condition.wait(), min(remaining, pause) if pause else remaining)
                    except TimeoutError:
                        continue
            finally:
                if not acquired:
                    self.queue.remove(ticket); self.condition.notify_all()
        try:
            yield
        finally:
            async with self.condition:
                self.active -= 1; self.condition.notify_all()

    @asynccontextmanager
    async def model_slot(self, queue_timeout):
        """진입 대기는 호출 마감 시작 전 별도의 기존 자원 대기 예산으로 보호한다."""
        if self.model_lease.get() is not None:
            raise RuntimeError('nested_provider_model_lease')
        queued = time.time()
        manager = self._slot(time.monotonic() + queue_timeout)
        await manager.__aenter__()
        lease = {'manager': manager, 'active': True}
        token = self.model_lease.set(lease)
        try:
            yield {'queued_at_epoch': queued, 'admitted_at_epoch': time.time(), 'queue_timeout_seconds': queue_timeout}
        finally:
            self.model_lease.reset(token)
            if lease['active']:
                lease['active'] = False
                await manager.__aexit__(None, None, None)

    @asynccontextmanager
    async def _attempt_slot(self, deadline):
        lease = self.model_lease.get()
        if lease is not None and lease['active']:
            yield
        else:
            async with self._slot(deadline):
                yield

    def _retry_delay(self, headers, rejected):
        try:
            if headers.get('retry-after-ms') is not None:
                value = float(headers['retry-after-ms']) / 1000
            else:
                value = float(headers.get('retry-after', 'nan'))
            if math.isfinite(value) and value > 0:
                return value
        except (ValueError, TypeError):
            pass
        raw = headers.get('retry-after')
        if raw:
            try:
                value = parsedate_to_datetime(raw).timestamp() - time.time()
                if value > 0:
                    return value
            except (ValueError, TypeError, OverflowError):
                pass
        return min(self.timeout, 2. ** min(rejected - 1, 16))

    async def send(self, dispatch, *, metadata=None):
        deadline = time.monotonic() + self.timeout; rejected = 0
        while True:
            async with self._attempt_slot(deadline):
                admitted_generation = self.pressure_generation
                if self.before_send is not None:
                    result = self.before_send()
                    if inspect.isawaitable(result):
                        await result
                self._record('request_dispatched', rejected_attempts=rejected, **(metadata or {}))
                try:
                    response = await dispatch()
                except Exception as exc:
                    self._record('transport_exception', error_type=type(exc).__name__,
                                 rejected_attempts=rejected, **(metadata or {}))
                    raise
                req_id = response.headers.get('x-github-request-id') or response.headers.get('x-request-id')
                if response.status_code not in (429, 502, 503, 504):
                    if response.status_code >= 400:
                        self._record('http_error_response', status_code=response.status_code,
                                     provider_request_id=req_id, rejected_attempts=rejected, **(metadata or {}))
                    elif 200 <= response.status_code < 300 and admitted_generation == self.pressure_generation and time.monotonic() >= self.blocked_until:
                        async with self.condition:
                            self.successes += 1
                            if self.successes >= self.capacity and self.capacity < self._resource_target():
                                self.capacity += 1; self.successes = 0
                                self._record('capacity_increased')
                            self.condition.notify_all()
                    return response
                if response.status_code != 429 and rejected >= 2:
                    self._record('http_error_response', status_code=response.status_code,
                                 provider_request_id=req_id, rejected_attempts=rejected, **(metadata or {}))
                    return response
                rejected += 1; delay = self._retry_delay(response.headers, rejected)
                event_name = 'provider_limited' if response.status_code == 429 else 'provider_transient_http_error'
                async with self.condition:
                    self.pressure_generation += 1
                    self.capacity = max(1, min(self.capacity, self.active) // 2); self.successes = 0
                    self.blocked_until = max(self.blocked_until, time.monotonic() + delay)
                    self._record(event_name, status_code=response.status_code, retry_after_seconds=delay,
                                 provider_request_id=req_id,
                                 rejected_attempts=rejected, **(metadata or {}))
                    self.condition.notify_all()
                if self.resource is not None:
                    self.resource.provider_backpressure(delay)
                if delay >= deadline - time.monotonic():
                    return response
            lease = self.model_lease.get()
            if lease is not None and lease['active']:
                lease['active'] = False
                await lease['manager'].__aexit__(None, None, None)
            await response.aread(); await response.aclose()


class ChatBackend:
    """클라이언트는 공유하되 에이전트·이력·바인딩은 호출자 역할·세션마다 격리한다."""
    transport_kind = 'chat-completions'

    def __init__(self, *, model_policy, base_directory, timeout=1200, resource_admission=None):
        self.model_policy = deepcopy(model_policy)
        self.base_directory = Path(base_directory) if base_directory else None
        self.timeout = timeout
        self.resource_admission = resource_admission
        self.request_guard = None
        self.provider_admission = None
        self.raw_token = next((os.environ[name].strip() for name in ('COPILOT_GITHUB_TOKEN', 'GH_TOKEN', 'GITHUB_TOKEN')
                               if os.environ.get(name, '').strip()), None)
        self._credential = None; self._expires = 0.; self.endpoint = None
        self.auth_lock = asyncio.Lock()
        self.catalog = None; self.api = None
        self.turns = {}; self.session_restore_flags = {}

    def _credentials(self):
        if not self.raw_token:
            raise RuntimeError('copilot_credential_missing')
        req = urllib.request.Request('https://api.github.com/copilot_internal/v2/token', method='GET', headers={
            'Authorization': 'token ' + self.raw_token, 'User-Agent': 'GitHubCopilotChat/0.26.7',
            'Accept': 'application/json', 'Editor-Version': 'vscode/1.104.1'})
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                data = json.loads(response.read().decode('utf-8'))
            key = data.get('token')
            if not isinstance(key, str) or not key:
                raise RuntimeError('copilot_exchange_empty_credential')
            endpoint = _trusted_endpoint(str((data.get('endpoints') or {}).get('api') or 'https://api.githubcopilot.com'))
            return key, float(data.get('expires_at') or time.time() + 1800), endpoint, 'exchanged_existing_credential'
        except urllib.error.HTTPError as error:
            # 이 계정의 기존 GitHub 토큰 직접 인증은 실제 네이티브 호출로 검증했다. 다른 모델로 대체하지 않는다.
            if error.code != 404:
                raise
            return self.raw_token, float('inf'), 'https://api.githubcopilot.com', 'direct_existing_credential'

    async def api_key(self):
        async with self.auth_lock:
            if self._credential is None or time.time() >= self._expires - 120:
                key, expires, endpoint, route = await asyncio.to_thread(self._credentials)
                if self.endpoint is not None and endpoint != self.endpoint:
                    raise RuntimeError('copilot_account_endpoint_changed')
                self._credential, self._expires, self.endpoint, self.credential_route = key, expires, endpoint, route
            return self._credential

    @staticmethod
    def headers():
        return {'Editor-Version': 'vscode/1.104.1', 'User-Agent': 'AICAF/1.0',
                'Copilot-Integration-Id': 'vscode-chat', 'Openai-Intent': 'conversation-edits',
                'x-initiator': 'agent', 'Copilot-Vision-Request': 'true'}

    async def start(self):
        from openai import AsyncOpenAI
        import httpx2
        key = await self.api_key()
        endpoint = self.endpoint
        if not isinstance(endpoint, str):
            raise RuntimeError('chat_endpoint_not_initialized')
        def fetch():
            headers = {**self.headers(), 'Authorization': 'Bearer ' + key}
            with urllib.request.urlopen(urllib.request.Request(endpoint + '/models', headers=headers), timeout=30) as response:
                return json.loads(response.read().decode('utf-8'))
        payload = await asyncio.to_thread(fetch)
        self.catalog = payload.get('data', payload.get('models', [])) if isinstance(payload, dict) else payload
        if not isinstance(self.catalog, list):
            raise RuntimeError('invalid_chat_model_catalog')
        for role, model in self.model_policy['models'].items():
            entries = [row for row in self.catalog if row.get('id') == model]
            if len(entries) != 1 or '/chat/completions' not in entries[0].get('supported_endpoints', []):
                raise RuntimeError('chat_model_not_advertised')
            supports = (entries[0].get('capabilities') or {}).get('supports') or {}
            if self.model_policy['reasoning_by_role'][role] not in supports.get('reasoning_effort', []):
                raise RuntimeError('chat_reasoning_not_advertised')
            if (entries[0].get('policy') or {}).get('state') != 'enabled':
                raise RuntimeError('chat_model_not_enabled')
        async def observe(request):
            if request.method != 'POST':
                return
            body = json.loads(request.content)
            if not request.url.path.endswith('/chat/completions') or body.get('model') not in self.model_policy['models'].values():
                raise RuntimeError('unexpected_chat_transmission')
            if body.get('reasoning_effort') not in self.model_policy['reasoning_by_role'].values():
                raise RuntimeError('unexpected_chat_reasoning_transmission')
            if body.get('tools'):
                raise RuntimeError('document_model_tools_not_allowed')
        self.api = AsyncOpenAI(api_key=self.api_key, base_url=self.endpoint, timeout=self.timeout, max_retries=0,
            default_headers=self.headers(), http_client=httpx2.AsyncClient(
                transport=self._provider_transport(httpx2.AsyncHTTPTransport()),event_hooks={'request': [observe]}))
        if self.base_directory:
            _atomic(self.base_directory/'chat-model-catalog.json', {'models': self.catalog, 'credential_route': self.credential_route,
                    'credential_values_logged': False, 'transport': self.transport_kind})

    def _provider_transport(self, inner):
        """429 대기·재진입은 MAF 실행 바깥이 아니라 동일 HTTP 본문의 전송 안에서만 수행한다."""
        import httpx2
        owner = self
        if self.provider_admission is None:
            self.provider_admission = ProviderAdmission(timeout=self.timeout, resource_admission=self.resource_admission,
                before_send=lambda: self.request_guard() if callable(self.request_guard) else None,
                state_path=self.base_directory/'provider-capacity.json' if self.base_directory else None,
                binding=_hash({'policy': self.model_policy, 'endpoint': self.endpoint}))
        admission = self.provider_admission
        if admission is None:
            raise RuntimeError('provider_admission_not_initialized')
        class AdmittedTransport(httpx2.AsyncBaseTransport):
            async def handle_async_request(self, request):
                if request.method != 'POST' or not request.url.path.endswith('/chat/completions'):
                    return await inner.handle_async_request(request)
                body = request.content
                async def dispatch():
                    # 갱신 가능한 인증만 바꾼다. 모델·대화·이미지를 포함한 요청 본문은 바이트 그대로 재사용한다.
                    clone = httpx2.Request(request.method, request.url, headers=request.headers,
                                          content=body, extensions=dict(request.extensions))
                    clone.headers['Authorization'] = 'Bearer ' + await owner.api_key()
                    return await inner.handle_async_request(clone)
                return await admission.send(dispatch,
                    metadata={'request_body_sha256': sha256(body).hexdigest()})
            async def aclose(self):
                await inner.aclose()
        return AdmittedTransport()

    def invocation_admission(self):
        if self.provider_admission is None:
            raise RuntimeError('provider_admission_not_initialized')
        queue_timeout = getattr(self.resource_admission, 'max_wait', self.timeout)
        return self.provider_admission.model_slot(queue_timeout)

    async def stop(self):
        if self.api is not None:
            await self.api.close()

    async def list_models(self):
        if self.catalog is None:
            raise RuntimeError('chat_backend_not_started')
        return deepcopy(self.catalog)

    def _state_path(self, role, session_id):
        if not self.base_directory:
            return None
        return self.base_directory/role/'maf-state'/(sha256(session_id.encode('utf-8')).hexdigest() + '.json')

    def _binding(self, role, instructions):
        if role not in self.model_policy['models']:
            raise ValueError('invalid_document_role')
        return {'role': role, 'model': self.model_policy['models'][role],
                'reasoning_effort': self.model_policy['reasoning_by_role'][role],
                'instructions_sha256': sha256(instructions.encode('utf-8')).hexdigest()}

    def restore_session(self, role, session_id, instructions):
        from agent_framework import AgentSession
        path = self._state_path(role, session_id)
        if path is None or not path.is_file():
            return None
        record = json.loads(path.read_text(encoding='utf-8'))
        if (record.get('schema') != 'asis-maf-chat-session/1' or record.get('binding') != self._binding(role, instructions)
                or record.get('session_sha256') != _hash(record.get('session'))
                or record.get('session', {}).get('session_id') != session_id
                or record.get('session', {}).get('service_session_id') is not None
                or type(record.get('turn')) is not int or record['turn'] < 1):
            raise RuntimeError('chat_session_restore_binding_changed')
        session = AgentSession.from_dict(record['session'])
        self.turns[(role, session_id)] = record['turn']
        self.session_restore_flags[(role, session_id)] = True
        return session, record['turn']

    def save_session(self, role, session, instructions):
        key = (role, session.session_id)
        turn = self.turns.get(key, 0) + 1
        self.turns[key] = turn
        path = self._state_path(role, session.session_id)
        if path:
            state = session.to_dict()
            _atomic(path, {'schema': 'asis-maf-chat-session/1', 'binding': self._binding(role, instructions),
                    'session': state, 'session_sha256': _hash(state), 'turn': turn,
                    'history_scope': 'local_framework_session_not_provider_service_session'})

    def agent_factory(self, *, client, name, instructions, tools, default_options):
        if client is not self or tools:
            raise ValueError('invalid_chat_agent_boundary')
        role = name.removeprefix('asis_')
        if (role not in self.model_policy['models'] or default_options.get('model') != self.model_policy['models'][role]
                or default_options.get('reasoning_effort') != self.model_policy['reasoning_by_role'][role]):
            raise ValueError('chat_agent_model_binding_changed')
        return ChatAgent(self, role, name, instructions)


class ChatAgent:
    """실제 MAF Agent를 동일 호출자 세션에 고정하며 공급자 응답 메타데이터만 반환한다."""
    def __init__(self, backend, role, name, instructions):
        self.backend = backend; self.role = role; self.name = name; self.instructions = instructions
        self.agent = None; self.session_id = None
        self.observation: dict[str, Any] | None = None

    async def run(self, messages, *, session, options=None):
        from agent_framework import Agent, ChatMiddleware
        from agent_framework.openai import OpenAIChatCompletionClient
        if self.session_id is not None and self.session_id != session.session_id:
            raise RuntimeError('chat_agent_cross_session_use')
        self.session_id = session.session_id
        owner = self
        if self.agent is None:
            class Trace(ChatMiddleware):
                async def process(self, context, call_next):
                    await call_next()
                    response = context.result
                    if response.model != owner.backend.model_policy['models'][owner.role]:
                        raise RuntimeError('unexpected_response_model')
                    if owner.observation is not None:
                        raise RuntimeError('unexpected_multiple_model_calls')
                    owner.observation = {'model': response.model, 'response_id': response.response_id,
                                         'usage': dict(response.usage_details or {})}
            client = OpenAIChatCompletionClient(model=self.backend.model_policy['models'][self.role],
                                                 async_client=self.backend.api, middleware=[Trace()])
            stable_id = 'asis-chat-' + sha256((self.role + ':' + session.session_id).encode('utf-8')).hexdigest()[:40]
            model_id = self.backend.model_policy['models'][self.role]
            cat_entry = next((m for m in (self.backend.catalog or []) if isinstance(m, dict) and m.get('id') == model_id), {})
            max_out = ((cat_entry.get('capabilities') or {}).get('limits') or {}).get('max_output_tokens')
            default_opts = {'reasoning_effort': self.backend.model_policy['reasoning_by_role'][self.role]}
            if type(max_out) is int and max_out > 0:
                default_opts['max_tokens'] = max_out
            self.agent = Agent(client, id=stable_id, name=self.name, instructions=self.instructions, tools=None,
                               default_options=default_opts)
        self.observation = None
        response = await self.agent.run(messages, session=session)
        if not self.observation or not self.observation['response_id']:
            raise RuntimeError('chat_provider_response_identity_missing')
        usage = normalize_usage(self.observation['usage'])
        self.backend.save_session(self.role, session, self.instructions)
        return SimpleNamespace(text=response.text, response_id=self.observation['response_id'], usage_details=usage,
            additional_properties={'model': self.observation['model']}, finish_reason='stop')
