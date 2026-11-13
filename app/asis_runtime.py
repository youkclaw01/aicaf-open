"""AS-IS 전용 런타임 계약. 기존 MAF와 승인 모델을 유지하며 다른 모듈 설정은 바꾸지 않는다."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re

MODEL = 'gpt-6.1-sol'
REASONING_EFFORT = 'xhigh'
# 역할별 모델·추론 강도. 기본값은 gemini-4-argon high 이며, legacy-gpt 프로필만 기존(sol xhigh / luna high)을 유지한다.
ROLE_POLICY = {
    'interpret': ('gpt-6.1-sol', 'xhigh'),
    'author': ('gpt-6.1-sol', 'xhigh'),
    'content_review': ('gpt-6.1-sol', 'xhigh'),
    'visual_review': ('gpt-6.1-sol', 'xhigh'),
    'direction_author': ('gpt-6-luna', 'high'),
}
MODEL_PROFILE = os.environ.get('AICAF_ASIS_MODEL_PROFILE', 'gemini-4-argon')
if MODEL_PROFILE == 'default':
    MODEL_PROFILE = 'gemini-4-argon'
if MODEL_PROFILE not in ('gemini-4-argon', 'legacy-gpt'):
    raise ValueError('invalid_asis_model_profile')
if MODEL_PROFILE == 'gemini-4-argon':
    MODEL = 'gemini-4-argon'
    REASONING_EFFORT = 'high'
    ROLE_POLICY = {role: (MODEL, REASONING_EFFORT) for role in ROLE_POLICY}
_ROLES = tuple(ROLE_POLICY)


def model_for(role):
    """역할의 요청 모델. 알 수 없는 역할은 기본 모델로 대체하지 않고 거부한다."""
    if role not in ROLE_POLICY:
        raise ValueError('invalid_document_role')
    return ROLE_POLICY[role][0]


def effort_for(role):
    if role not in ROLE_POLICY:
        raise ValueError('invalid_document_role')
    return ROLE_POLICY[role][1]


def policy():
    """제품 문서 역할의 모델 선택과 대체 금지 계약을 반환한다."""
    return deepcopy({
        'framework': 'Microsoft Agent Framework',
        'provider': 'github-copilot',
        'models': {role: model_for(role) for role in _ROLES},
        'reasoning_effort': REASONING_EFFORT,
        'reasoning_by_role': {role: effort_for(role) for role in _ROLES},
        'fallback_allowed': False,
        **({'transport': 'chat-completions'} if MODEL_PROFILE == 'gemini-4-argon' else {}),
    })


def implementation_files(root=None):
    """운영 준비 판정과 시험·배포에서 동일하게 봉인할 파일 집합을 반환한다."""
    root=Path(root).resolve() if root is not None else Path(__file__).resolve().parent
    names=('asis_runtime.py','asis_chat_transport.py','asis_resume.py','asis_admission.py','asis_tasks.py','asis_native.py','asis_pipeline.py','asis_service.py','asis_view.py','asis_worker.py',
           'asis_artifacts.py','asis_office.py','asis_model.py','asis_analysis.py','asis_plan.py','asis_item_agents.py','asis_synthesis.py','asis_synthesis_reuse.py','static/asis.js',
           'asis_evidence_views.py','asis_criteria_assessment.py','asis_approved_presentation.py','asis_excel_freeze.py','asis_report_visuals.py','asis_analysis_register.py','asis_collection_basis.py',
           'asis_model_view.py','asis_store_link.py','asis_evidence_store.py','asis_analysis_store.py','asis_input_budget.py',
           'asis_direction_harness.py','asis_output_grader.py','asis_language_guard.py','asis_stages.py','asis_parallelism.py','asis_response_repair.py','asis_input_summary.py','asis_length_failures.py','asis_action_split.py','asis_document_shards.py',
           'asis_visual_draw.py','asis_visual_forms.py','asis_consolidated_deck.py','asis_overview_architecture.py',
           'assessment_workflow.py','assessment_flow_view.py','assessment_flow_artifacts.py','assessment_flow_interview.py','assessment_current_state_presentation.py',
           'static/assessment-workflow.js','static/index.html','server.py','questions.py','scoring.py','interview_report.py','interview_template.pptx')
    paths=[root/name for name in names]
    paths.extend(root.glob('question*.py'))
    paths.extend(p for p in (root/'asis_assets').rglob('*')
                 if p.is_file() and p.suffix in ('.py','.json','.txt','.pptx','.md'))
    return tuple(sorted(set(paths)))


def implementation_fingerprint():
    """현재 생산 코드·프롬프트·근거·승인 자산에 운영 검증을 봉인한다."""
    root=Path(__file__).resolve().parent
    paths=implementation_files(root)
    if any(not p.is_file() for p in paths):return None
    rows=sorted(p.relative_to(root).as_posix()+'='+hashlib.sha256(p.read_bytes()).hexdigest() for p in set(paths))
    return hashlib.sha256('\n'.join(rows).encode('utf-8')).hexdigest()


def transport_message(message):
    """모델 뷰의 동일 run 반복만 참조로 인코딩한다. 정본·원문·다른 실행은 바꾸지 않는다."""
    prefix, sep, raw = message.rpartition('\n입력:\n')
    if not sep:
        return message
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return message
    if (not isinstance(data, dict) or not isinstance(data.get('run'), dict)
            or not isinstance(data.get('facts'), list) or not isinstance(data.get('context'), dict)):
        return message
    if 'shared_fact_run_reference' in data['context']:
        return message
    marker = {'$ref': '/run'}
    count = 0
    for fact in data['facts']:
        if isinstance(fact, dict) and fact.get('run') == data['run']:
            fact['run'] = deepcopy(marker)
            count += 1
    if not count:
        return message
    data['context']['shared_fact_run_reference'] = {
        'marker': marker, 'target': '/run',
        'meaning': 'Only facts whose full run equals the top-level run use this lossless reference. Other run values remain inline.'}
    compact = prefix + sep + json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    return compact if len(compact.encode('utf-8')) < len(message.encode('utf-8')) else message


def delivery_truncated(receipt):
    """전송 바이트에 비해 공급자가 받은 입력 토큰이 비정상적으로 적으면 True. 근거가 없으면(사용량 없음·작은 입력) False."""
    if not isinstance(receipt, dict):
        return False
    size = receipt.get('input_utf8_bytes')
    tokens = (receipt.get('usage') or {}).get('input_token_count')
    if type(size) is not int or type(tokens) is not int or size < 200_000 or tokens <= 0:
        return False
    return size / tokens > 10


class Runtime:
    """한 생성 작업의 MAF 클라이언트와 격리된 문서 역할 호출을 관리한다."""

    def __init__(self, *, client=None, client_factory=None, agent_factory=None, timeout=1200,
                 concurrency=None, base_directory=None, admission=None):
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise ValueError('invalid_model_timeout')
        if concurrency is not None and (type(concurrency) is not int or concurrency < 1):
            raise ValueError('invalid_model_concurrency')
        self.client = client
        self.client_factory = client_factory
        self._injected_client = client is not None
        self.role_clients = {}
        self.role_client_locks = {}
        self._client_options = None
        self.shutdown_errors = []
        self.shutdown_records = []
        self.stop_timeout_seconds = 45.0
        self.force_stop_timeout_seconds = 15.0
        self.startup_timeout = 45   # SDK 프로세스 기동 자체에만 적용한다(다른 호출이 기동하는 동안의 잠금 대기는 포함하지 않는다)
        self.agent_factory = agent_factory
        self.timeout = timeout
        self.base_directory = Path(base_directory) if base_directory else None
        from asis_admission import AdaptiveAdmission
        self.gate = admission or AdaptiveAdmission(ceiling=concurrency,
            folder=self.base_directory.parent if self.base_directory else None)
        self.calls = []
        self.sessions = {}
        self.session_locks = {}
        self.agents = {}
        self.agent_bindings = {}
        self.session_turns = {}
        self.activity = {}
        self.restored_sessions = {}
        self.document_materials_sha256 = None
        self.document_materials_binding_sha256 = None
        self.document_guidance = None
        self.model_catalog = None
        self.model_catalog_lock = asyncio.Lock()
        self._entered = False

    def load_document_materials(self):
        """승인된 저장 문서 스킬의 실제 본문을 고정 해시에 묶는다. 고객 근거로 전용하지 않는다."""
        if not self.base_directory:
            return
        folder=self.base_directory.parent
        source=folder/'document-materials.json';binding_path=folder/'document-materials-binding.json'
        if not source.exists() and not binding_path.exists():
            return
        if not source.is_file() or not binding_path.is_file():
            raise ValueError('document_materials_binding_missing')
        raw=source.read_bytes();binding_raw=binding_path.read_bytes()
        binding=json.loads(binding_raw.decode('utf-8'))
        digest=hashlib.sha256(raw).hexdigest()
        if (not isinstance(binding,dict) or binding.get('schema')!='asis-document-materials-binding/1'
            or binding.get('materials_sha256')!=digest
            or binding.get('classification')!='procedural_guidance_not_customer_evidence' or len(raw)>2*1024*1024):
            raise ValueError('document_materials_binding_changed')
        materials=json.loads(raw.decode('utf-8'))
        if (not isinstance(materials,dict) or materials.get('schema')!='asis-document-materials/1'
            or materials.get('source_type')!='stored_skills_explicitly_approved_for_this_final_report'
            or materials.get('template_content_is_not_customer_evidence') is not True
            or not isinstance(materials.get('files'),list) or not materials['files']):
            raise ValueError('document_materials_not_explicitly_approved')
        sections=[]
        for row in materials['files']:
            if (not isinstance(row,dict) or not isinstance(row.get('text'),str) or not row['text'].strip()
                or row.get('utf8_bytes')!=len(row['text'].encode('utf-8'))
                or not isinstance(row.get('source_path'),str)
                or not re.fullmatch(r'[0-9a-f]{64}',row.get('sha256',''))):
                raise ValueError('document_materials_actual_text_missing')
            sections.append('### '+row['source_path']+'\n'+row['text'])
        self.document_guidance=('문서 작성·검토의 절차 자료 — 고객 현황·측정·판정 근거가 아니다.\n'
            '최신 사용자 범위와 승인 템플릿·보고서 원형을 유지한다. 공통 문서 작성 규칙이 저장 스킬의 문체 제안보다 우선한다. '
            '스킬의 예시·합성 수치·샘플 고객 사실을 현재 고객 근거로 사용하지 않는다. 역할별 고정 지시는 교체하지 않는다.\n\n'
            +'\n\n'.join(sections)+'\n\n원래 승인된 작업 입력:\n')
        self.document_materials_sha256=digest
        self.document_materials_binding_sha256=hashlib.sha256(binding_raw).hexdigest()

    def document_materials_for_call(self,role,session_id,*,image=None):
        """새 호출에는 실제 본문을 전달하되 원문 고정된 미진입 검토 1회의 승인 메시지는 바꾸지 않는다."""
        if not self.base_directory:
            return None
        folder=self.base_directory.parent
        source=folder/'document-materials.json';binding=folder/'document-materials-binding.json'
        if self.document_guidance is None:
            if source.exists() or binding.exists():raise ValueError('document_materials_changed_during_runtime')
            return None
        if (not source.is_file() or not binding.is_file()
            or hashlib.sha256(source.read_bytes()).hexdigest()!=self.document_materials_sha256
            or hashlib.sha256(binding.read_bytes()).hexdigest()!=self.document_materials_binding_sha256):
            raise ValueError('document_materials_changed_during_runtime')
        protected=folder/'unstarted-review-continuation.json'
        if role=='content_review' and image is None and protected.is_file():
            approval=json.loads(protected.read_text(encoding='utf-8'))
            if approval.get('target_session_id')==session_id:
                return None
        return self.document_guidance

    def restore_existing_sessions(self, records):
        """원래 제공자·역할·지시·추론 수준이 같은 명시 세션만 기존 SDK 대화에 연결한다."""
        if not isinstance(records,list):raise ValueError('invalid_session_restore_records')
        for record in records:
            if (not isinstance(record,dict) or record.get('role') not in _ROLES
                or not re.fullmatch(r'[A-Za-z0-9._-]{1,180}',record.get('session_id',''))
                or not isinstance(record.get('service_session_id'),str) or not record['service_session_id']
                or not re.fullmatch(r'[0-9a-f]{64}',record.get('instructions_sha256',''))
                or record.get('requested_model')!=model_for(record['role']) or record.get('actual_model')!=model_for(record['role'])
                or record.get('requested_reasoning_effort')!=effort_for(record['role']) or record.get('fallback_used') is not False
                or type(record.get('session_turn')) is not int or record['session_turn']<1):
                raise ValueError('existing_session_restore_binding_failed')
            identifier=record['session_id']
            if identifier in self.sessions or (identifier in self.restored_sessions and self.restored_sessions[identifier]!=record):
                raise ValueError('existing_session_restore_changed')
            self.restored_sessions[identifier]=dict(record)

    async def __aenter__(self):
        self.load_document_materials()
        if MODEL_PROFILE == 'gemini-4-argon' and self.client is None and self.client_factory is None and self.agent_factory is None:
            from asis_chat_transport import ChatBackend
            self.client = ChatBackend(model_policy=policy(), base_directory=self.base_directory, timeout=self.timeout,
                                      resource_admission=self.gate)
            self._injected_client = True
            self.agent_factory = self.client.agent_factory
        if self.client is None:
            if self.client_factory is None:
                from copilot import CopilotClient
                self.client_factory = CopilotClient
            token = next((os.environ[name].strip() for name in
                ('COPILOT_GITHUB_TOKEN', 'GH_TOKEN', 'GITHUB_TOKEN')
                if os.environ.get(name, '').strip()), None)
            if not token:
                raise RuntimeError('copilot_credential_missing')
            # 호출 자격증명은 명시 주입하고 다른 프로필·CLI의 인증과 설정을 자동 탐색하지 않는다.
            env = {key: value for key, value in os.environ.items()
                   if key not in ('COPILOT_GITHUB_TOKEN', 'GH_TOKEN', 'GITHUB_TOKEN')
                   and not key.startswith(('GITHUB_COPILOT_', 'COPILOT_CLI_'))}
            if self.base_directory:
                self.base_directory.mkdir(parents=True, exist_ok=True)
            self._client_options = dict(github_token=token, use_logged_in_user=False,
                mode='empty', env=env, log_level='error')
            # 역할별 프로세스로 누적 SDK 이벤트·세션을 분산하며 동일 역할·세션 객체는 유지한다.
            self.client = await self.client_for_role('author')
            await self.start_role_clients()
        if self.agent_factory is None:
            from agent_framework.github import GitHubCopilotAgent
            self.agent_factory = GitHubCopilotAgent
        if self._injected_client:
            await asyncio.wait_for(self.client.start(), min(self.timeout, 45))
        self._entered = True
        return self

    async def client_for_role(self, role):
        """역할별 SDK 프로세스를 한 번 시작하고 문서의 동일 MAF 세션을 보존한다.

        2026-10-17 운영 사고: direction_author 클라이언트가 87개 호출이 몰린 순간에 처음 기동돼 153초가 걸렸고 45초 제한을 넘은 한 호출이
        모델 요청 전 단계에서 실패해 개정 전체를 failed 로 만들었다. 대응: ① 개정 시작 때 모든 역할을 미리 기동한다(__aenter__),
        ② 기동 제한은 기동 자체에만 적용한다(잠금 대기는 제외), ③ 기동이 실패하면 반쯤 기동된 클라이언트를 정리하고 보관하지 않는다.
        """
        if role not in _ROLES:
            raise ValueError('invalid_document_role')
        if self._injected_client:
            return self.client
        existing = self.role_clients.get(role)
        if existing is not None:
            return existing
        lock = self.role_client_locks.setdefault(role, asyncio.Lock())
        async with lock:
            if role in self.role_clients:
                return self.role_clients[role]
            if self.client_factory is None or self._client_options is None:
                raise RuntimeError('runtime_client_factory_not_ready')
            folder = self.base_directory/role if self.base_directory else None
            if folder:
                folder.mkdir(parents=True, exist_ok=True)
            options = dict(self._client_options)
            options.update(base_directory=str(folder) if folder else None,
                           working_directory=str(folder) if folder else None)
            client = self.client_factory(**options)
            try:
                await asyncio.wait_for(client.start(), min(self.timeout, self.startup_timeout))
            except BaseException:
                # 반쯤 기동된 SDK 프로세스를 남기지 않는다. 정리 실패는 원래 오류를 가리지 않는다.
                try:
                    await asyncio.wait_for(client.stop(), 15)
                except BaseException:
                    pass
                raise
            self.role_clients[role] = client
            return client

    async def model_limits_for(self, role):
        """현재 계정이 광고한 한도를 SDK에 결합해 구형 모델 기본값으로의 오판을 막는다."""
        from dataclasses import asdict, is_dataclass
        from typing import Any, Awaitable, cast
        client = self.client if self._injected_client else self.role_clients.get(role)
        if client is None:
            client = await self.client_for_role(role)
        query = getattr(client, 'list_models', None)
        # 외부 SDK 대신 명시적으로 주입한 합성 런타임의 기존 시험 계약은 유지한다.
        if not callable(query):
            return None
        async with self.model_catalog_lock:
            if self.model_catalog is None:
                models = await asyncio.wait_for(cast(Awaitable[list[Any]], query()), min(self.timeout, self.startup_timeout))
                self.model_catalog = [asdict(cast(Any, row)) if is_dataclass(row) else dict(row) for row in models]
            matches = [row for row in self.model_catalog if row.get('id') == model_for(role)]
            if len(matches) != 1:
                raise RuntimeError('model_catalog_model_unavailable')
            limits = (matches[0].get('capabilities') or {}).get('limits') or {}
            value = {name: limits.get(name) for name in ('max_prompt_tokens', 'max_context_window_tokens')}
            if (any(type(number) is not int or number <= 0 for number in value.values())
                    or cast(int, value['max_prompt_tokens']) > cast(int, value['max_context_window_tokens'])):
                raise RuntimeError('model_catalog_limits_invalid')
            return value

    async def start_role_clients(self):
        """모든 역할의 SDK 클라이언트를 병렬로 미리 기동한다. 호출이 몰리기 전에 기동을 끝내고, 기동 실패는 개정 시작 때 드러낸다."""
        if self._injected_client or self.client_factory is None or self._client_options is None:
            return
        results = await asyncio.gather(*(self.client_for_role(role) for role in _ROLES if role not in self.role_clients),
                                       return_exceptions=True)
        failures = [r for r in results if isinstance(r, BaseException)]
        if failures:
            # 일부 역할이 기동에 실패해도 개정을 막지 않는다. 그 역할의 첫 호출이 다시 기동을 시도한다(설정 단계 재시도 포함).
            self.startup_failures = [type(f).__name__ for f in failures]

    async def release_session(self, session_id):
        """끝난 세션의 메모리 객체(에이전트·SDK 세션·잠금·활동)를 해제한다. 영수증·디스크 세션 기록·재개 정보는 보존한다.

        2026-10-17 운영 실측: 개정 하나에서 워커 트리 메모리가 0.3GB 에서 10GB 까지 올랐고 작업이 끝나도 내려가지 않았다.
        항목마다 만든 세션·에이전트가 쌓였기 때문이다(SDK 세션은 클라이언트가 stop() 할 때까지 보관된다).
        disconnect 는 이벤트·도구 핸들러 메모리를 해제하고 디스크의 대화 기록은 남기므로, 이후 같은 service_session_id 로 재개할 수 있다.
        같은 세션이 보완 라운드에서 다시 쓰이면 새 에이전트를 만들고 service_session_id 로 이어 간다(session_turns 는 유지).
        """
        if not isinstance(session_id, str):
            return False
        lock = self.session_locks.get(session_id)
        if lock is not None and lock.locked():
            return False  # 호출 중인 세션은 해제하지 않는다
        session = self.sessions.get(session_id)
        service_id = getattr(session, 'service_session_id', None)
        released = False
        for client in ([self.client] if self._injected_client and self.client else list(self.role_clients.values())):
            sdk_sessions = getattr(client, '_sessions', None) or getattr(client, 'sessions', None)
            if not isinstance(sdk_sessions, dict) or not service_id:
                continue
            handle = sdk_sessions.get(service_id)
            if handle is None:
                continue
            try:
                await asyncio.wait_for(handle.disconnect(), 15)
            except Exception as error:
                self.shutdown_errors.append(type(error).__name__)
            try:
                lock_ = getattr(client, '_sessions_lock', None)
                if lock_ is not None:
                    with lock_:
                        sdk_sessions.pop(service_id, None)
                else:
                    sdk_sessions.pop(service_id, None)
            except Exception:
                pass
            released = True
        self.agents.pop(session_id, None)
        self.agent_bindings.pop(session_id, None)
        self.sessions.pop(session_id, None)
        self.session_locks.pop(session_id, None)
        self.activity.pop(session_id, None)
        if service_id:
            # 재개 경로: 해제한 세션을 다시 쓰면 디스크 기록의 서비스 세션 ID 로 이어 간다. 원래 바인딩 검증을 우회하지 않는다.
            self.released_sessions = getattr(self, 'released_sessions', {})
            self.released_sessions[session_id] = service_id
        return released

    async def __aexit__(self, exc_type, exc_value, traceback):
        """작업 결과와 자원 정리를 구분하며 SDK가 소유한 전송만 강제 종료한다."""
        from typing import Any,Awaitable,cast
        clients = [('injected',self.client)] if self._injected_client and self.client else list(self.role_clients.items())
        failures = []
        self.shutdown_records = []
        for role,client in clients:
            record={'role':role,'normal_stop':'pending','force_stop':'not_needed'}
            self.shutdown_records.append(record)
            try:
                await asyncio.wait_for(client.stop(),self.stop_timeout_seconds)
                record['normal_stop']='completed'
            except Exception as error:
                record['normal_stop']=type(error).__name__
                self.shutdown_errors.append(type(error).__name__)
                force_stop=getattr(client,'force_stop',None)
                if callable(force_stop):
                    try:
                        await asyncio.wait_for(cast(Awaitable[Any],force_stop()),self.force_stop_timeout_seconds)
                        record['force_stop']='completed'
                    except Exception as forced_error:
                        record['force_stop']=type(forced_error).__name__
                        self.shutdown_errors.append('force_stop:'+type(forced_error).__name__)
                        failures.append(error)
                else:
                    record['force_stop']='unavailable'
                    failures.append(error)
        self._entered = False
        diagnostic={'schema':'asis-runtime-shutdown/1','context_body_failed':exc_type is not None,
            'all_clients_closed':not failures,'clients':self.shutdown_records,
            'quality_records_modified':False}
        if self.base_directory:
            # 종료 진단은 모델·품질 원장과 별개다. 종료 오류나 진단 저장 오류로 원작업 예외를 가리지 않는다.
            try:
                path=self.base_directory/'shutdown.json';path.parent.mkdir(parents=True,exist_ok=True)
                temporary=path.with_name(path.name+'.'+str(os.getpid())+'.tmp')
                temporary.write_text(json.dumps(diagnostic,ensure_ascii=False,indent=2),encoding='utf-8')
                for attempt in range(4):
                    try:
                        os.replace(temporary,path);break
                    except PermissionError:
                        if attempt==3:raise
                        await asyncio.sleep(0.05*(2**attempt))
            except OSError as diagnostic_error:
                self.shutdown_errors.append('diagnostic:'+type(diagnostic_error).__name__)
        if failures and exc_value is None:
            raise failures[0]
        return False

    def recover_terminal_response(self, role, message, session_id, instructions):
        """명시 승인된 최종 응답만 원문 세션에서 복구한다. 호출 성공·검토 통과를 조작하지 않는다."""
        if not self.base_directory:
            return None
        path = self.base_directory.parent/'terminal-response-continuation.json'
        if not path.is_file():
            return None
        try:
            binding = json.loads(path.read_text(encoding='utf-8'))
            if role != 'author' or binding['session_id'] != session_id:
                return None
            sha = lambda value: hashlib.sha256(value.encode('utf-8')).hexdigest()
            if (binding.get('review_call_budget') != 1 or not binding.get('approval_id')
                    or sha(message) != binding['canonical_input_sha256']
                    or sha(instructions) != binding['instructions_sha256']):
                raise ValueError('approval_input_binding')
            events_path = self.base_directory/'session-state'/session_id/'events.jsonl'
            raw = events_path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != binding['events_sha256']:
                raise ValueError('events_hash')
            events = [json.loads(line) for line in raw.decode('utf-8').splitlines()]
            starts = [e for e in events if e['type'] == 'session.start']
            users = [e for e in events if e['type'] == 'user.message']
            finals = [e for e in events if e['type'] == 'assistant.message' and e['data'].get('content')]
            ends = [e for e in events if e['type'] == 'assistant.turn_end']
            if len(starts) != 1 or len(users) != 1 or len(finals) != 1 or len(ends) != 1:
                raise ValueError('ambiguous_turn')
            start, final, end = starts[0]['data'], finals[0], ends[0]
            data = final['data']
            if (start.get('sessionId') != session_id or start.get('selectedModel') != model_for(role)
                    or start.get('reasoningEffort') != effort_for(role) or data.get('model') != model_for(role)
                    or data.get('phase') != 'final_answer' or data.get('toolRequests')
                    or data.get('messageId') != binding['response_id']
                    or sha(data['content']) != binding['response_sha256']
                    or end.get('parentId') != final['id']
                    or not data.get('turnId') or end['data'].get('turnId') != data['turnId']):
                raise ValueError('final_event_binding')
            if any(e['type'] in ('session.error','tool.execution_start','permission.requested') for e in events):
                raise ValueError('pending_or_failed_work')
            expected = transport_message(message)
            actual = users[0]['data']['content']
            ep, es, ej = expected.rpartition('\n입력:\n')
            ap, ass, aj = actual.rpartition('\n입력:\n')
            if actual != expected and not (es and ass and ep == ap and json.loads(ej) == json.loads(aj)):
                raise ValueError('transmitted_input_binding')
            if not any(instructions in e['data'].get('content','') for e in events if e['type']=='system.message'):
                raise ValueError('effective_system_binding')
            # 복구 기록은 과거 호출의 성공 receipt가 아니라 현재 확인한 원문 출처다.
            receipt = {'role':role,'session_id':session_id,'framework':'Microsoft Agent Framework',
                'provider':'github-copilot','requested_model':model_for(role),'actual_model':data['model'],
                'requested_reasoning_effort':effort_for(role),'response_id':data['messageId'],
                'fallback_used':False,'completion_source':'preserved_sdk_final_and_turn_end',
                'model_call_performed_now':False,'original_call_status':'session_idle_timeout',
                'approval_id':binding['approval_id'],'events_sha256':binding['events_sha256'],
                'input_sha256':sha(actual),'canonical_input_sha256':sha(message),
                'instructions_sha256':sha(instructions),'response_sha256':sha(data['content']),
                'completed_at':datetime.now(timezone.utc).isoformat()}
            self.calls.append(receipt)
            return data['content'], receipt
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise RuntimeError('terminal_recovery_binding_failed:'+type(exc).__name__) from exc

    NON_RETRYABLE_SETUP_ERRORS = frozenset({'persistent_agent_role_or_instruction_binding_changed', 'persistent_agent_role_or_instruction_changed',
        'model_catalog_model_unavailable', 'model_catalog_limits_invalid',
        'existing_session_restore_binding_failed', 'runtime_not_started', 'copilot_credential_missing', 'runtime_client_factory_not_ready',
        'invalid_document_role', 'image_input_size_limit', 'unexpected_response_model', 'empty_model_response'})
    SETUP_RETRY_LIMIT = 3          # 모델 요청 전 설정 단계 실패를 같은 호출로 다시 시도하는 최대 횟수(총 시도 = 1 + 이 값이 아니라 이 값 이내)
    SETUP_RETRY_DELAY = (1.0, 3.0)  # 재시도 전 대기(초). 기동이 몰린 순간의 일시 실패를 피한다

    async def _ask_with_setup_retry(self, role, message, *, session_id, instructions='', image=None):
        """모델 요청이 나가기 전(session_setup)에 실패한 호출만 같은 입력으로 제한된 횟수 다시 시도한다.

        설정 단계 실패(SDK 프로세스 기동 시간 초과 등)는 작성자·모델의 결함이 아니고 요청이 나가지 않았으므로 이중 과금이 없다.
        요청이 한 번이라도 나간 뒤의 실패(request_started_at 기록)는 여기서 다시 보내지 않는다. 호출자의 기존 복구(무응답 재전송 등)가 판단한다.
        """
        attempt = 0
        while True:
            try:
                return await self._ask(role, message, session_id=session_id, instructions=instructions, image=image)
            except Exception as exc:
                row = self.activity.get(session_id) if isinstance(session_id, str) else None
                request_sent = bool(row and row.get('request_started_at'))
                # 모델 요청이 나가기 전의 실패인가: 활동 기록이 없거나(클라이언트 기동 단계), 대기·설정 상태에서 실패한 경우
                before_request = (row is None or row.get('state') in ('session_wait', 'resource_wait', 'failed')) and not request_sent
                # 요청 전 실패는 작성자·모델의 결함이 아니다. 시간 초과·연결 오류뿐 아니라 SDK 가 세션 설정 중 낸 RuntimeError(운영 D_P0080)도 다시 시도한다.
                # 입력·구성·권한 같은 결정적 오류는 반복해도 같으므로 제외한다.
                prefix = str(exc).split(':', 1)[0]
                deterministic = (isinstance(exc, (ValueError, TypeError, KeyError, PermissionError, AssertionError))
                                 or prefix in self.NON_RETRYABLE_SETUP_ERRORS)
                transient_type = isinstance(exc, Exception) and not deterministic
                setup_failure = before_request and transient_type
                if not setup_failure or attempt >= self.SETUP_RETRY_LIMIT:
                    raise
                attempt += 1
                self.setup_retries = getattr(self, 'setup_retries', []) + [{'role': role, 'session_id': session_id, 'attempt': attempt,
                    'error_type': type(exc).__name__, 'at': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()}]
                self.activity.pop(session_id, None)
                await asyncio.sleep(self.SETUP_RETRY_DELAY[min(attempt - 1, len(self.SETUP_RETRY_DELAY) - 1)])

    async def ask(self, role, message, *, session_id, instructions='', image=None):
        """자원·세션·SDK 경계의 실패를 비밀 없는 오류코드로 남기고 원래 예외를 유지한다."""
        from datetime import datetime, timezone
        import hashlib
        # 공급자가 끝내 응답하지 않는 호출(session.idle 대기 초과)은 같은 입력을 새 세션으로 한 번만 다시 보낸다.
        # 정상 호출도 최대 약 1,100초가 걸리므로 대기 시간은 줄이지 않는다. 응답이 온 호출은 다시 보내지 않는다.
        try:
            try:
                text,receipt=await self._ask_with_setup_retry(role,message,session_id=session_id,instructions=instructions,image=image)
                if (not session_id.endswith('-resend1') and delivery_truncated(receipt)):
                    # 큰 입력을 보냈는데 공급자가 받은 입력 토큰이 정상 비율의 1/10 미만이면 입력이 잘려 전달된 것이다
                    # (운영 실측: 정상 3.4~3.7바이트/토큰, 이상 1건 142바이트/토큰, 모델은 '입력 없음'이라 답함).
                    # 이 응답은 신뢰할 수 없으므로 버리고 같은 입력을 새 세션으로 한 번만 다시 보낸다.
                    self.agents.pop(session_id,None);self.agent_bindings.pop(session_id,None)
                    self.resent_after_truncated_delivery=getattr(self,'resent_after_truncated_delivery',[])+[{'role':role,'session_id':session_id,
                        'at':datetime.now(timezone.utc).isoformat(),'input_utf8_bytes':receipt.get('input_utf8_bytes'),
                        'input_tokens':(receipt.get('usage') or {}).get('input_token_count'),'reason':'input_truncated_in_delivery'}]
                    return await self._ask(role,message,session_id=session_id+'-resend1',instructions=instructions,image=image)
                return text,receipt
            except Exception as first:
                if isinstance(session_id,str):
                    budget=(re.search(r'prompt budget[^()]*\((\d+) tokens; limit (\d+)\)',str(first))
                            or re.search(r'prompt token count of (\d+) exceeds the limit of (\d+)',str(first)))
                    entity_large = re.search(r'Request Entity Too Large|too many images:|maximum allowed for model', str(first), re.I)
                    if budget or entity_large:
                        # 운영 2026-10-18 D_P0064·D_P0072 / 2026-10-21 A_ITEM_0001: 같은 세션에서 보완 라운드를 이어 가며 대화 이력이 쌓여 토큰 또는 HTTP 본문 크기 한도를 넘었다.
                        # 이 오류는 모델 응답 전에 거절돼 과금이 없다. 한도 초과가 난 호출만 이력 없는 새 세션으로 한 번 다시 보낸다.
                        self.agents.pop(session_id,None);self.agent_bindings.pop(session_id,None)
                        self.sessions.pop(session_id,None);self.restored_sessions.pop(session_id,None)
                        self.resent_after_prompt_budget=getattr(self,'resent_after_prompt_budget',[])+[{'role':role,'session_id':session_id,
                            'at':datetime.now(timezone.utc).isoformat(),'prompt_tokens':int(budget.group(1)) if budget else 1,'limit':int(budget.group(2)) if budget else 1,
                            'reason':'prompt_budget_exceeded_before_model_response'}]
                        fresh_idx = len([r for r in self.resent_after_prompt_budget if r.get('session_id') == session_id])
                        return await self._ask_with_setup_retry(role,message,session_id=f'{session_id}-budget{fresh_idx}',instructions=instructions,image=image)
                row=self.activity.get(session_id) if isinstance(session_id,str) else None
                hung=('waiting for session.idle' in str(first) or isinstance(first,TimeoutError)) and row is not None and row.get('request_started_at')
                if not hung or not isinstance(session_id,str) or session_id.endswith('-resend1'):raise
                self.agents.pop(session_id,None);self.agent_bindings.pop(session_id,None)
                self.resent_after_no_response=getattr(self,'resent_after_no_response',[])+[{'role':role,'session_id':session_id,
                    'at':datetime.now(timezone.utc).isoformat(),'reason':'no_response_before_idle_timeout'}]
                return await self._ask(role,message,session_id=session_id+'-resend1',instructions=instructions,image=image)
        except BaseException as exc:
            row=self.activity.get(session_id) if isinstance(session_id,str) else None
            if row is not None:
                boundary=('resource_admission' if row.get('state')=='resource_wait' else
                          'sdk_request' if row.get('request_started_at') else 'session_setup')
                known={'resource_admission_wait_exhausted','persistent_agent_role_or_instruction_changed',
                       'unexpected_response_model','empty_model_response','runtime_not_started'}
                inner = getattr(exc, 'inner_exception', None) or exc.__cause__ or exc.__context__
                status = getattr(exc, 'status_code', None) or getattr(inner, 'status_code', None)
                req_id = getattr(exc, 'request_id', None) or getattr(inner, 'request_id', None)
                if req_id is None:
                    resp = getattr(exc, 'response', None) or getattr(inner, 'response', None)
                    headers = getattr(resp, 'headers', None)
                    if headers is not None and hasattr(headers, 'get'):
                        req_id = headers.get('x-github-request-id') or headers.get('x-request-id')
                prefix=str(exc).split(':',1)[0]
                code=(prefix if prefix in known else 'provider_backpressure' if status==429 else
                      'model_request_timeout' if isinstance(exc,TimeoutError) or isinstance(inner,TimeoutError) else
                      'model_request_cancelled' if isinstance(exc,asyncio.CancelledError) else 'model_runtime_failure')
                row.update(state='cancelled' if isinstance(exc,asyncio.CancelledError) else 'failed',
                    failed_at=datetime.now(timezone.utc).isoformat(),error_type=type(exc).__name__,failure_boundary=boundary,failure_code=code)
                if inner is not None:
                    row['inner_error_type'] = type(inner).__name__
                if type(status) is int:
                    row['http_status_code'] = status
                if isinstance(req_id, str) and req_id:
                    row['provider_request_id'] = req_id[:120]
                try:setattr(exc,'asis_failure_code',code)
                except (AttributeError,TypeError):pass
                if self.base_directory:
                    try:
                        from asis_view import atomic
                        saved={key:row[key] for key in ('session_id','role','state','queued_at','admitted_at',
                            'request_started_at','failed_at','error_type','inner_error_type','http_status_code',
                            'provider_request_id','failure_boundary','failure_code') if key in row}
                        if inner is not None or type(status) is int:
                            raw_msg = str(inner if inner is not None else exc)
                            redacted = re.sub(r'(?:github_pat_|gh[opusr]_)[A-Za-z0-9_]+', '[REDACTED]', raw_msg)
                            redacted = re.sub(r'Bearer\s+\S+', 'Bearer [REDACTED]', redacted, flags=re.I)
                            saved['error_summary'] = redacted[:400]
                        saved.update(schema='asis-runtime-failure/1',model_request_started=bool(row.get('request_started_at')))
                        name=hashlib.sha256((session_id+'\n'+row['failed_at']).encode('utf-8')).hexdigest()[:24]+'.json'
                        atomic(self.base_directory.parent/'runtime-failures'/name,saved)
                    except (OSError,ValueError,ImportError):
                        row['failure_record_write_failed']=True
            raise

    async def _ask(self, role, message, *, session_id, instructions='', image=None):
        """역할별 명시 세션으로 호출하고 실제 응답 모델을 검사한다."""
        from agent_framework import AgentSession, Content, Message
        from typing import Any, AsyncContextManager, cast
        if not self._entered:
            raise RuntimeError('runtime_not_started')
        if role not in _ROLES:
            raise ValueError('invalid_document_role')
        if not isinstance(session_id, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,180}', session_id):
            raise ValueError('explicit_isolated_session_required')
        if not isinstance(message, str) or not message.strip():
            raise ValueError('empty_model_input')
        recovered = self.recover_terminal_response(role, message, session_id, instructions)
        if recovered is not None:
            if image is not None:
                raise RuntimeError('terminal_recovery_image_not_supported')
            return recovered
        if self.base_directory and (self.base_directory.parent/'terminal-response-continuation.json').is_file():
            # 운영 실패의 재시도 한도는 바꾸지 않는다. 명시 추가 승인도 검토 한 번으로 제한한다.
            approval = json.loads((self.base_directory.parent/'terminal-response-continuation.json').read_text(encoding='utf-8'))
            if role != 'content_review' or approval.get('review_call_budget') != 1:
                raise RuntimeError('continuation_call_budget_exhausted')
            claim = self.base_directory.parent/'content-review-call-once.json'
            try:
                with claim.open('x', encoding='utf-8') as file:
                    json.dump({'session_id':session_id,'approval_id':approval['approval_id'],
                               'claimed_at':datetime.now(timezone.utc).isoformat()},file)
            except FileExistsError as exc:
                raise RuntimeError('continuation_call_budget_exhausted') from exc
        canonical_message = message
        message = transport_message(message)
        document_guidance = self.document_materials_for_call(role,session_id,image=image)
        if document_guidance:
            message = document_guidance+message
        contents = [Content.from_text(message)]
        image_hash = None
        raw = b''
        if image is not None:
            raw = bytes(image)
            if not raw or len(raw) > 3 * 1024 * 1024:
                raise ValueError('image_input_size_limit')
            contents.append(Content.from_data(raw, media_type='image/png'))
            image_hash = hashlib.sha256(raw).hexdigest()
        restored=self.restored_sessions.get(session_id)
        effective_instructions=instructions or 'Return only the requested JSON. Never invent facts. Do not use tools.'
        if restored and (restored['role']!=role or restored['instructions_sha256']!=hashlib.sha256(effective_instructions.encode('utf-8')).hexdigest()):
            raise RuntimeError('existing_session_restore_binding_failed')
        if session_id not in self.sessions:
            framework_restore = getattr(self.client, 'restore_session', None)
            framework_state = (framework_restore(role, session_id, effective_instructions)
                if getattr(self.client, 'transport_kind', None) == 'chat-completions' and callable(framework_restore) else None)
            if framework_state is not None:
                if not isinstance(framework_state, tuple) or len(framework_state) != 2:
                    raise RuntimeError('invalid_framework_session_restore')
                self.sessions[session_id], self.session_turns[session_id] = framework_state[0], framework_state[1]
                self.framework_restored_sessions = getattr(self, 'framework_restored_sessions', set())
                self.framework_restored_sessions.add(session_id)
            else:
                self.sessions[session_id]=AgentSession(session_id=session_id,
                    service_session_id=restored['service_session_id'] if restored else None)
            if restored:self.session_turns[session_id]=restored['session_turn']
        session = self.sessions[session_id]
        lock = self.session_locks.setdefault(session_id, asyncio.Lock())
        started = datetime.now(timezone.utc).isoformat()
        self.activity[session_id] = {'role':role,'state':'session_wait','queued_at':started,
                                     'session_id':session_id,'provider_backpressure':False}
        async with lock:
            effective_instructions = instructions or 'Return only the requested JSON. Never invent facts. Do not use tools.'
            sdk_model_limits = await self.model_limits_for(role)
            binding = {'role': role, 'instructions_sha256': hashlib.sha256(effective_instructions.encode('utf-8')).hexdigest(),
                       'model': model_for(role), 'reasoning_effort': effort_for(role), 'sdk_model_limits': sdk_model_limits}
            agent_reused = session_id in self.agents
            if agent_reused and self.agent_bindings[session_id] != binding:
                raise RuntimeError('persistent_agent_role_or_instruction_binding_changed')
            if not agent_reused:
                options = {'model': model_for(role), 'timeout': self.timeout, 'reasoning_effort': effort_for(role),
                    'available_tools': [], 'enable_config_discovery': False,
                    'enable_file_hooks': False, 'mcp_servers': {}, 'skip_custom_instructions': True,
                    'enable_skills': False, 'plugin_directories': [], 'instruction_directories': [],
                    'skill_directories': []}
                if sdk_model_limits is not None and getattr(self.client, 'transport_kind', None) != 'chat-completions':
                    from copilot import ModelCapabilitiesOverride, ModelLimitsOverride
                    options['model_capabilities'] = ModelCapabilitiesOverride(limits=ModelLimitsOverride(**sdk_model_limits))
                factory = self.agent_factory
                if factory is None:
                    raise RuntimeError('runtime_agent_factory_not_started')
                self.agents[session_id] = factory(client=await self.client_for_role(role), name='asis_'+role,
                    instructions=effective_instructions, tools=None, default_options=options)
                self.agent_bindings[session_id] = binding
            agent = self.agents[session_id]
            # 동일 객체를 유지하되 생성에만 로컬 ID를 전달한다. 재개에는 SDK가 반환한 ID를 쓴다.
            run_options = {'session_id': session_id} if not session.service_session_id else {}
            activity = self.activity[session_id]
            activity['state'] = 'resource_wait'
            async with self.gate.slot('model', extra_bytes=len(message.encode('utf-8'))*4+(len(raw)*4 if image is not None else 0)) as admitted:
                admitted_at = admitted['admitted_at']
                request_started_at = None
                async def invoke_admitted():
                    nonlocal request_started_at
                    if self.base_directory:
                        from asis_resume import claim_unstarted_review
                        claim_unstarted_review(self.base_directory.parent,role,session_id,
                            canonical_message,effective_instructions,image=image)
                    request_started_at = datetime.now(timezone.utc).isoformat()
                    activity.update(state='running',admitted_at=admitted_at,request_started_at=request_started_at)
                    return await asyncio.wait_for(agent.run(
                        Message('user', contents=contents), session=session, options=run_options), self.timeout + 15)
                try:
                    provider_admission = getattr(self.client, 'invocation_admission', None)
                    if callable(provider_admission):
                        activity.update(state='provider_wait',provider_queued_at=datetime.now(timezone.utc).isoformat())
                        async with cast(AsyncContextManager[Any], provider_admission()) as provider_receipt:
                            activity['provider_admission'] = provider_receipt
                            reply = await invoke_admitted()
                    else:
                        reply = await invoke_admitted()
                except Exception as exc:
                    # 공급자 제한만 새 진입에 전달한다. 예외 메시지·자격증명은 저장하지 않는다.
                    inner = getattr(exc, 'inner_exception', None) or exc.__cause__ or exc.__context__
                    status = getattr(exc, 'status_code', None) or getattr(inner, 'status_code', None)
                    if status == 429 or re.search(r'\b429\b|rate.?limit|too many requests', str(exc), re.I):
                        delay = getattr(exc, 'retry_after', None) or getattr(inner, 'retry_after', 30.)
                        self.gate.provider_backpressure(float(delay) if isinstance(delay, (int,float)) else 30.)
                        activity['provider_backpressure'] = True
                    activity.update(state='failed',error_type=type(exc).__name__,failed_at=datetime.now(timezone.utc).isoformat())
                    raise
            turn = self.session_turns.get(session_id, 0) + 1
            self.session_turns[session_id] = turn
        actual = (getattr(reply, 'additional_properties', None) or {}).get('model')
        if actual != model_for(role):
            activity.update(state='failed',error_type='UnexpectedResponseModel',failed_at=datetime.now(timezone.utc).isoformat())
            raise RuntimeError('unexpected_response_model')
        text = str(getattr(reply, 'text', '') or '').strip()
        if not text or not getattr(reply, 'response_id', None):
            activity.update(state='failed',error_type='EmptyModelResponse',failed_at=datetime.now(timezone.utc).isoformat())
            raise RuntimeError('empty_model_response')
        receipt = {'role': role, 'session_id': session_id, 'framework': 'Microsoft Agent Framework',
            'provider': 'github-copilot', 'requested_model': model_for(role), 'actual_model': actual,
            'agent_reused': agent_reused, 'session_turn': turn,'session_restored':bool(restored),
            'agent_instance_id': str(id(agent)), 'session_instance_id': str(id(session)),
            'service_session_id': session.service_session_id,
            'response_id': reply.response_id, 'fallback_used': False, 'started_at': started,
            'queued_at': admitted.get('queued_at') or started, 'admitted_at': admitted_at, 'request_started_at': request_started_at,
            'queue_wait_seconds': admitted.get('queue_wait_seconds'), 'admission_hold_count': admitted.get('hold_count'),
            'admission_last_hold_reason': admitted.get('last_hold_reason'),
            'admission_mode': 'host_resource_adaptive',
            'requested_reasoning_effort': effort_for(role),
            'completed_at': datetime.now(timezone.utc).isoformat(),
            'input_sha256': hashlib.sha256(message.encode('utf-8')).hexdigest(),
            'canonical_input_sha256': hashlib.sha256(canonical_message.encode('utf-8')).hexdigest(),
            'input_utf8_bytes': len(message.encode('utf-8')),
            'document_materials_applied': bool(document_guidance),
            'sdk_model_limits': deepcopy(sdk_model_limits),
            'document_materials_sha256': self.document_materials_sha256,
            'instructions_sha256': hashlib.sha256(instructions.encode('utf-8')).hexdigest(),
            'response_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
            'image_sha256': image_hash, 'usage': getattr(reply, 'usage_details', None) or {}}
        if getattr(self.client, 'transport_kind', None) == 'chat-completions':
            receipt.update(transport='chat-completions', advertised_model_limits=deepcopy(sdk_model_limits), sdk_model_limits=None,
                session_restored=session_id in getattr(self, 'framework_restored_sessions', set()),
                history_scope='serialized_maf_session_not_remote_service_conversation',
                provider_admission=deepcopy(activity.get('provider_admission')))
        self.calls.append(receipt)
        activity.update(state='completed',completed_at=receipt['completed_at'],response_sha256=receipt['response_sha256'])
        return text, receipt
