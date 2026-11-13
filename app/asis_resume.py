"""승인된 코드 차이 아래 기존 개정·원문·검토·모델 예산을 보존하는 실패 범위 재개 계약."""
from __future__ import annotations
import ast
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from asis_model import ASSET,digest,encoded,strict_json


def file_sha(path):
    from asis_pipeline import history_path
    return hashlib.sha256(history_path(path).read_bytes()).hexdigest()


def _function(source,name,owner=None):
    """승인된 과거 코드의 순수 프롬프트 함수만 추출한다. 모듈 전체는 실행하지 않는다."""
    try:
        tree=ast.parse(source.decode('utf-8'))
        nodes=next(row for row in tree.body if isinstance(row,ast.ClassDef) and row.name==owner).body if owner else tree.body
        node=next(row for row in nodes if isinstance(row,(ast.FunctionDef,ast.AsyncFunctionDef)) and row.name==name)
        return node
    except (SyntaxError,UnicodeDecodeError,StopIteration) as exc:
        raise ValueError('resume_legacy_source_not_verified') from exc


def prepare_failed_resume(folder,*,approval_id,expected_job_sha256,expected_snapshot_sha256,
                          legacy_code,implementation_sha256):
    """실패 이력은 변경하지 않고 정확한 원천·승인 후보·코드 차이에 결합한 재개 증거를 만든다."""
    from asis_view import atomic
    folder=Path(folder)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,96}',approval_id or ''):raise ValueError('invalid_resume_approval_id')
    if not re.fullmatch(r'[0-9a-f]{64}',implementation_sha256 or ''):raise ValueError('invalid_resume_implementation')
    if file_sha(folder/'job.json')!=expected_job_sha256:raise ValueError('resume_job_changed')
    if file_sha(folder/'snapshot.json')!=expected_snapshot_sha256:raise ValueError('resume_snapshot_changed')
    job=strict_json((folder/'job.json').read_bytes());snapshot=strict_json((folder/'snapshot.json').read_bytes())
    if job.get('status')!='failed' or job.get('run')!=snapshot.get('run'):raise ValueError('resume_not_same_failed_run')
    if set(legacy_code)!={'asis_pipeline.py','asis_item_agents.py'}:raise ValueError('resume_legacy_sources_missing')
    for name,raw in legacy_code.items():
        _function(raw,'render_prompt','Pipeline') if name=='asis_pipeline.py' else _function(raw,'item_input')
    failed_path=folder/'checkpoints'/'actions-merge'/'failed-items.json'
    failed=strict_json(failed_path.read_bytes())
    failed_ids=list(failed.get('failed_item_ids') or [])
    success_ids=list(failed.get('successful_item_ids') or [])
    ledger_path=folder/'task-ledger.json'
    if not ledger_path.is_file():raise ValueError('resume_task_ledger_missing')
    ledger=strict_json(ledger_path.read_bytes())
    if ledger.get('run')!=snapshot['run']:raise ValueError('resume_task_run_changed')
    for identifier in list(failed_ids):
        key=('diagnosis-' if identifier.startswith('J_') else 'action-')+identifier
        if (folder/'checkpoints'/key/'accepted.json').is_file() and ledger['tasks'].get(key,{}).get('status')!='failed':
            failed_ids.remove(identifier)
            if identifier not in success_ids:success_ids.append(identifier)
    post_item_failed=[k for k,row in ledger['tasks'].items() if row.get('status') in ('failed','revision_required','running') and not k.startswith(('diagnosis-','action-'))]
    if (not isinstance(failed_ids,list) or not isinstance(success_ids,list)
        or (not failed_ids and not post_item_failed)
        or set(failed_ids)&set(success_ids) or len(set(failed_ids))!=len(failed_ids)):
        raise ValueError('resume_failed_scope_not_bound')
    pending_progress=set()
    for row in ledger['tasks'].values():
        if row['status']!='completed':pending_progress.update(row.get('progress_steps',{}).values())
    completed=[key for key,row in ledger['tasks'].items() if row['status']=='completed' and key not in pending_progress]
    protected={str(p.relative_to(folder)):file_sha(p) for p in (folder/'checkpoints').rglob('accepted.json')}
    protected[str(failed_path.relative_to(folder))]=file_sha(failed_path)
    for relative in protected:
        if Path(relative).name!='accepted.json':continue
        record=strict_json((folder/relative).read_bytes())
        if 'candidate' in record:
            if record.get('candidate_sha256')!=digest(record['candidate']):
                raise ValueError('resume_accepted_candidate_or_review_changed')
            if record.get('schema')=='asis-action-item-merge/1':
                if record.get('review_mode')!='per_item_content_review_and_deterministic_merge':
                    raise ValueError('resume_accepted_candidate_or_review_changed')
            elif record.get('review',{}).get('verdict')!='pass':
                raise ValueError('resume_accepted_candidate_or_review_changed')
    approval={'schema':'asis-failed-scope-resume/1','approval_id':approval_id,'run':snapshot['run'],
        'job_before_sha256':expected_job_sha256,'snapshot_sha256':expected_snapshot_sha256,
        'task_ledger_before_sha256':file_sha(ledger_path),
        'implementation_sha256':implementation_sha256,'failed_item_ids':failed_ids,
        'completed_item_ids':success_ids,'completed_task_ids':completed,
        'completed_task_record_sha256':{key:digest(ledger['tasks'][key]) for key in completed},
        'protected_checkpoints':protected,
        'legacy_code_sha256':{name:hashlib.sha256(raw).hexdigest() for name,raw in legacy_code.items()},
        'retry_count_preserved':job.get('retry_count',0),'author_rounds_increased':False,'review_gate_relaxed':False,
        'existing_candidate_review_only':{},'new_author_items':list(failed_ids)}
    # 소진된 작성 예산을 늘리지 않는다. 정상 본문이 있는 실패 항목은 코드 소유 관계만 투영하고 새 검토를 받는다.
    from asis_pipeline import Pipeline
    from asis_item_agents import project_item_graph,validate_item,collect_item_defects
    probe=Pipeline(None,folder/'resume-preflight')
    for identifier in failed_ids:
        key=('diagnosis-' if identifier.startswith('J_') else 'action-')+identifier
        root=folder/'checkpoints'/key
        responses=list(root.glob('round-*/author-receipt.json'))
        selected=None
        for round_root in sorted(root.glob('round-*')):
            source=round_root/'candidate.json';input_path=round_root/'author-input.json'
            if not source.is_file() or not input_path.is_file():continue
            try:
                raw=strict_json(source.read_bytes());original_input=strict_json(input_path.read_bytes())
                assignment=original_input['stage_data']['item_assignment']
                if assignment['item_id']!=identifier:raise ValueError('resume_candidate_item_changed')
                candidate=project_item_graph(raw,assignment)
                if digest({k:v for k,v in raw.items() if k!='edges'})!=digest({k:v for k,v in candidate.items() if k!='edges'}):
                    raise ValueError('resume_projection_changed_model_content')
                validate_item(probe,original_input,candidate)
                if collect_item_defects(probe,original_input,candidate)['defects']:continue
                selected={'path':str(source.relative_to(folder)),'sha256':file_sha(source),
                    'input_path':str(input_path.relative_to(folder)),'input_sha256':file_sha(input_path),
                    'projected_candidate_sha256':digest(candidate),'author_model_responses_preserved':len(responses)}
                break
            except (ValueError,KeyError,TypeError):continue
        if selected:
            approval['existing_candidate_review_only'][key]=selected
            approval['new_author_items'].remove(identifier)
        elif len(responses)>=3:raise ValueError('resume_author_budget_exhausted_without_valid_candidate:'+identifier)
    # 원래 역할별 SDK 대화의 식별자·지시와 이벤트 접두부를 보존한다. 대화 본문은 승인 파일에 복제하지 않는다.
    sessions={}
    for receipt_path in (folder/'checkpoints').rglob('*receipt.json'):
        try:receipt=strict_json(receipt_path.read_bytes())
        except (ValueError,OSError):continue
        if not receipt.get('service_session_id') or not receipt.get('instructions_sha256'):continue
        if receipt.get('provider')!='github-copilot':continue
        identifier=receipt.get('session_id');role=receipt.get('role')
        if not identifier or not role:raise ValueError('resume_original_session_identity_missing')
        previous=sessions.get(identifier)
        if previous and previous['session_turn']>=receipt.get('session_turn',0):continue
        events=folder/'sessions'/role/'session-state'/receipt['service_session_id']/'events.jsonl'
        if not events.is_file():
            events=folder/'sessions'/'session-state'/receipt['service_session_id']/'events.jsonl'
        if not events.is_file():raise ValueError('resume_original_sdk_history_missing:'+identifier)
        sessions[identifier]={key:receipt[key] for key in ('session_id','role','service_session_id',
            'instructions_sha256','requested_model','actual_model','requested_reasoning_effort','fallback_used','session_turn')}
        sessions[identifier].update(events_path=str(events.relative_to(folder)),events_prefix_bytes=events.stat().st_size,
                                    events_prefix_sha256=file_sha(events))
    approval['session_restores']=list(sessions.values())
    destination=folder/'resume-approvals'/approval_id
    if (destination/'approval.json').exists():
        if strict_json((destination/'approval.json').read_bytes())!=approval:raise ValueError('resume_approval_reused_with_drift')
        return approval
    for name,raw in legacy_code.items():
        path=destination/'legacy-code'/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
    destination.mkdir(parents=True,exist_ok=True)
    (destination/'job-before.json').write_bytes((folder/'job.json').read_bytes())
    (destination/'task-ledger-before.json').write_bytes(ledger_path.read_bytes())
    atomic(destination/'approval.json',approval)
    atomic(folder/'resume-request.json',{'approval_id':approval_id,'approval_sha256':file_sha(destination/'approval.json')})
    return approval


def verified_feedback_checkpoint(folder, key, data, cached, bindings):
    """실제 보완 입력·승인 후보·원검토가 결합된 경우에만 같은 정본의 승인 결과를 재사용한다."""
    if not re.fullmatch(r'[A-Za-z0-9_-]+', key):
        raise ValueError('invalid_feedback_checkpoint_key')
    from asis_pipeline import history_path
    folder = history_path(folder)
    root = folder / 'checkpoints' / key
    binding = cached.get('input_sha256')
    if not isinstance(binding, str) or not re.fullmatch(r'[0-9a-f]{64}', binding):
        return None

    def base(value):
        value = deepcopy(value)
        stage = value.get('stage_data', {})
        for name in ('prior_output', 'accumulated_defects', 'task_resolution'):
            stage.pop(name, None)
        return digest(value)

    current = base(data)
    paths = list(root.glob('round-*/author-input.json'))
    paths.extend((root / 'inputs' / binding).rglob('author-input.json'))
    eligible = []
    bound_input = None
    for path in sorted(paths):
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError('feedback_checkpoint_source_outside_unit')
        original = strict_json(path.read_bytes())
        stage = original.get('stage_data', {})
        if (not isinstance(stage.get('prior_output'), dict) or not stage.get('accumulated_defects')
                or base(original) != current):
            continue
        eligible.append(path)
        if binding in bindings(original):
            bound_input = path
    if bound_input is None:
        return None
    for path in eligible:
        candidate_path = path.parent / 'candidate.json'
        review_path = path.parent / 'review.json'
        receipt_path = path.parent / 'author-receipt.json'
        if any(p.is_symlink() or not p.resolve().is_relative_to(root.resolve())
               for p in (candidate_path, review_path, receipt_path)):
            raise ValueError('feedback_checkpoint_source_outside_unit')
        if not all(p.is_file() for p in (candidate_path, review_path, receipt_path)):
            continue
        candidate = strict_json(candidate_path.read_bytes())
        review = strict_json(review_path.read_bytes())
        receipt = strict_json(receipt_path.read_bytes())
        if (digest(candidate) != cached.get('candidate_sha256')
                or digest(candidate) != cached.get('reviewed_candidate_sha256')
                or digest(candidate) != digest(cached.get('candidate'))
                or digest(review) != digest(cached.get('review'))
                or review.get('verdict') != 'pass' or review.get('content_checked') is not True
                or digest(receipt) != digest(cached.get('author_receipt'))):
            continue
        return {'schema': 'asis-verified-feedback-checkpoint/1', 'original_input_sha256': binding,
                'current_base_input_sha256': current,
                'source_input': str(bound_input.relative_to(folder)), 'source_input_sha256': file_sha(bound_input),
                'approved_author_input': str(path.relative_to(folder)), 'approved_author_input_sha256': file_sha(path),
                'source_candidate': str(candidate_path.relative_to(folder)), 'source_candidate_sha256': file_sha(candidate_path),
                'source_review': str(review_path.relative_to(folder)), 'source_review_sha256': file_sha(review_path),
                'source_author_receipt_sha256': file_sha(receipt_path),
                'workflow_feedback_only': True, 'candidate_and_review_unchanged': True, 'new_model_calls': 0}
    raise ValueError('feedback_checkpoint_evidence_changed')


class ResumeContext:
    """과거 PASS를 새 코드 PASS로 둔갑시키지 않고 원본 검토와 현재 결정적 재검증을 별도로 결합한다."""
    def __init__(self,folder,request):
        self.folder=Path(folder);self.root=self.folder/'resume-approvals'/request['approval_id']
        if file_sha(self.root/'approval.json')!=request['approval_sha256']:raise ValueError('resume_approval_changed')
        self.approval=strict_json((self.root/'approval.json').read_bytes())
        if file_sha(self.folder/'snapshot.json')!=self.approval['snapshot_sha256']:raise ValueError('resume_snapshot_changed')
        self.legacy={name:(self.root/'legacy-code'/name).read_bytes() for name in self.approval['legacy_code_sha256']}
        for name,raw in self.legacy.items():
            if hashlib.sha256(raw).hexdigest()!=self.approval['legacy_code_sha256'][name]:raise ValueError('resume_legacy_code_changed')
        node=_function(self.legacy['asis_pipeline.py'],'render_prompt','Pipeline')
        namespace={'deepcopy':deepcopy,'ASSET':ASSET,'encoded':encoded,'json':json,'strict_json':strict_json}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),'verified-legacy-render','exec'),namespace)
        self.legacy_render=namespace['render_prompt']
        import inspect
        self.legacy_render_has_model_view='model_view' in inspect.signature(self.legacy_render).parameters
        self.preserved=set(self.approval['completed_task_ids'])
        self.original_preserved=set(self.preserved)
        self.checked={};self.continuation_root=None;self.current_pipeline=None
        continuation_path=self.folder/'unstarted-review-continuation.json'
        if continuation_path.is_file():
            continuation=strict_json(continuation_path.read_bytes())
            root=self.folder/'unstarted-review-continuations'/continuation['approval_id']
            if (continuation.get('schema')!='asis-unstarted-review-continuation/1'
                or type(continuation.get('review_call_budget')) is not int or continuation['review_call_budget']!=1
                or continuation['parent_approval_sha256']!=file_sha(self.root/'approval.json')
                or continuation['snapshot_sha256']!=file_sha(self.folder/'snapshot.json')
                or strict_json((root/'approval.json').read_bytes())!=continuation):
                raise ValueError('unstarted_review_continuation_binding_changed')
            current=(root/'current-pipeline.py').read_bytes()
            if hashlib.sha256(current).hexdigest()!=continuation['current_pipeline_sha256']:
                raise ValueError('unstarted_review_stage_code_changed')
            self.current_pipeline=current;self.continuation_root=root
            self.preserved.update(continuation['completed_task_ids'])
            self.approval['implementation_sha256']=continuation['implementation_sha256']
            self.approval['session_restores']=continuation['session_restores']
            for name,expected in continuation['protected_checkpoints'].items():
                original=self.approval['protected_checkpoints'].get(name)
                if original is not None and original!=expected:
                    raise ValueError('unstarted_review_original_checkpoint_conflict:'+name)
                if file_sha(self.folder/name)!=expected:raise ValueError('unstarted_review_checkpoint_changed:'+name)
            self.approval['protected_checkpoints'].update(continuation['protected_checkpoints'])
        for session in self.approval.get('session_restores',[]):
            path=(self.folder/session['events_path']).resolve()
            if not path.is_relative_to(self.folder.resolve()):raise ValueError('resume_history_path_outside_revision')
            remaining=session['events_prefix_bytes'];hasher=hashlib.sha256()
            with path.open('rb') as stream:
                while remaining:
                    chunk=stream.read(min(remaining,1024*1024))
                    if not chunk:raise ValueError('resume_sdk_history_truncated')
                    hasher.update(chunk);remaining-=len(chunk)
            if hasher.hexdigest()!=session['events_prefix_sha256']:raise ValueError('resume_sdk_history_changed')

    @classmethod
    def active(cls,folder):
        path=Path(folder)/'resume-request.json'
        return cls(folder,strict_json(path.read_bytes())) if path.is_file() else None

    def _data(self,data):
        legacy=deepcopy(data)
        if b"'item_contract'" not in self.legacy['asis_item_agents.py'] and b'"item_contract"' not in self.legacy['asis_item_agents.py']:
            legacy['stage_data'].pop('item_contract',None)
        return legacy

    def _protected(self,path):
        relative=str(Path(path).relative_to(self.folder))
        expected=self.approval['protected_checkpoints'].get(relative)
        if not expected or file_sha(path)!=expected:raise ValueError('resume_preserved_checkpoint_changed:'+relative)

    def stage(self,pipe,data,stage,key):
        if key not in self.preserved:return None
        path=self.folder/'checkpoints'/key/'accepted.json';self._protected(path)
        record=strict_json(path.read_bytes())
        from asis_pipeline import STAGE_REUSE_CODE_BINDING
        policy=__import__('asis_runtime').policy()
        # 보존 기록은 만들어진 시점의 키 식을 따른다. 과거 승인 기록은 '코드 해시' 식, 현재 코드로 만든 기록은 '코드 바인딩 버전' 식이다.
        # 두 식 모두 입력·단계·모델 정책·프롬프트에 묶여 있고, 허용은 이 기록의 두 후보 중 하나가 정확히 일치할 때만이다.
        def input_bindings(value):
            if key not in self.original_preserved and self.current_pipeline is not None:
                prompts = pipe.render_prompt(stage, value, model_view=False)
                return [digest({'input':value,'stage':stage,'code_binding':STAGE_REUSE_CODE_BINDING,'model_policy':policy,'prompts':prompts}),
                        digest({'input':value,'stage':stage,'code':self.current_pipeline.hex(),'model_policy':policy,'prompts':prompts})]
            legacy = self._data(value)
            prompts = self.legacy_render(pipe,stage,legacy,model_view=False) if self.legacy_render_has_model_view else self.legacy_render(pipe,stage,legacy)
            return [digest({'input':legacy,'stage':stage,'code':self.legacy['asis_pipeline.py'].hex(),'model_policy':policy,'prompts':prompts}),
                    digest({'input':legacy,'stage':stage,'code_binding':STAGE_REUSE_CODE_BINDING,'model_policy':policy,'prompts':prompts})]

        candidates = input_bindings(data)
        feedback = None
        if record.get('input_sha256') not in candidates:
            current = data if key not in self.original_preserved and self.current_pipeline is not None else self._data(data)
            feedback = verified_feedback_checkpoint(self.folder, key, current, record, input_bindings)
            if feedback is None and stage in (1, 2, 3, 4):
                cand = record.get('candidate')
                if (isinstance(cand, dict)
                        and record.get('candidate_sha256') == digest(cand)
                        and record.get('reviewed_candidate_sha256') == digest(cand)
                        and record.get('review', {}).get('verdict') == 'pass'):
                    try:
                        pipe.validator.validate_output(data, cand)
                        feedback = {'schema': 'asis-upstream-repair-checkpoint-reuse/1',
                                    'unit': key, 'candidate_sha256': digest(cand)}
                    except (ValueError, KeyError, TypeError):
                        feedback = None
        if ((record.get('input_sha256') not in candidates and feedback is None)
                or record.get('candidate_sha256') != digest(record.get('candidate'))):
            raise ValueError('resume_preserved_stage_binding_changed:'+key)
        return {**record, 'reuse_binding_provenance': feedback} if feedback is not None else record

    def plan(self,pipe,data,instruction,system):
        if 'action-item-plan' not in self.preserved:return None
        from asis_item_agents import PLAN_SCHEMA
        binding=digest({'input':data,'instruction':instruction,'system':system,'policy':__import__('asis_runtime').policy(),
                        'implementation_sha256':digest(self.legacy['asis_item_agents.py'].hex()),'schema':PLAN_SCHEMA})
        path=self.folder/'checkpoints'/'action-item-plan'/binding/'accepted.json';self._protected(path)
        record=strict_json(path.read_bytes())
        if record.get('input_sha256')!=binding or record.get('candidate_sha256')!=digest(record.get('candidate')):
            raise ValueError('resume_preserved_plan_binding_changed')
        self.verified('action-item-plan',record)
        return record,binding

    def verified(self,key,record):
        from asis_view import atomic
        self.checked[key]={'original_candidate_sha256':record.get('candidate_sha256'),
            'original_review_sha256':digest(record.get('review')),'fresh_code_revalidated':True,
            'authority':'original_independent_review_plus_current_code_validation_not_new_model_review'}
        atomic(self.root/'revalidation.json',{'schema':'asis-checkpoint-revalidation/1',
            'approval_sha256':file_sha(self.root/'approval.json'),'implementation_sha256':self.approval['implementation_sha256'],
            'snapshot_sha256':self.approval['snapshot_sha256'],'checks':self.checked,
            'completed_item_ids':[key.split('-',1)[1] for key in self.checked
                                  if re.fullmatch(r'(diagnosis-J_ITEM|action-A_ITEM)_[0-9]+',key)],
            'fresh_model_calls_for_preserved_items':0})

    def existing_candidate(self,key):
        row=self.approval['existing_candidate_review_only'].get(key)
        if row is None:return None
        path=self.folder/row['path'];input_path=self.folder/row['input_path']
        if file_sha(path)!=row['sha256'] or file_sha(input_path)!=row['input_sha256']:
            raise ValueError('resume_existing_candidate_changed')
        return strict_json(path.read_bytes()),row


def claim_unstarted_review(folder,role,session_id,message,instructions,*,image=None):
    """추가 승인된 미진입 검토 한 번을 실제 전송 직전에 영속적으로 점유한다."""
    from datetime import datetime,timezone
    from asis_pipeline import history_path
    folder=history_path(Path(folder));path=folder/'unstarted-review-continuation.json'
    if not path.is_file():return None
    approval=strict_json(path.read_bytes())
    if (approval.get('schema')!='asis-unstarted-review-continuation/1'
        or type(approval.get('review_call_budget')) is not int or approval['review_call_budget']!=1):
        raise ValueError('invalid_unstarted_review_budget')
    if role=='author' and session_id==approval.get('target_author_session_id'):
        raise RuntimeError('unstarted_review_reauthor_forbidden')
    if session_id!=approval.get('target_session_id') or image is not None:return None
    if role!='content_review':raise ValueError('unstarted_review_role_mismatch')
    if (hashlib.sha256(message.encode('utf-8')).hexdigest()!=approval.get('canonical_input_sha256')
        or hashlib.sha256(instructions.encode('utf-8')).hexdigest()!=approval.get('instructions_sha256')):
        raise ValueError('unstarted_review_input_binding_changed')
    claim={'schema':'asis-unstarted-review-call-claim/1','approval_id':approval['approval_id'],
        'session_id':session_id,'role':role,'candidate_sha256':approval['candidate_sha256'],
        'canonical_input_sha256':hashlib.sha256(message.encode('utf-8')).hexdigest(),
        'instructions_sha256':hashlib.sha256(instructions.encode('utf-8')).hexdigest(),
        'claimed_at':datetime.now(timezone.utc).isoformat()}
    try:
        with (folder/'unstarted-review-call-once.json').open('x',encoding='utf-8') as stream:
            json.dump(claim,stream,ensure_ascii=False)
    except FileExistsError as exc:
        raise RuntimeError('unstarted_review_call_budget_exhausted') from exc
    return claim


def prepare_unstarted_review_continuation(folder,*,approval_id,failure_path,setup_proof,implementation_sha256):
    """실제 모델에 진입하지 않은 세션 준비 실패만 별도 승인 계약의 대상으로 받는다."""
    from asis_pipeline import history_path
    folder=history_path(Path(folder));failure=strict_json((folder/failure_path).read_bytes())
    if (failure.get('schema')!='asis-runtime-failure/1' or failure.get('role')!='content_review'
        or failure.get('failure_boundary')!='session_setup' or failure.get('model_request_started') is not False):
        raise ValueError('continuation_requires_unstarted_session_setup_failure')
    if (not re.fullmatch(r'[A-Za-z0-9_-]{1,96}',approval_id or '')
        or not re.fullmatch(r'[0-9a-f]{64}',implementation_sha256 or '')):
        raise ValueError('invalid_unstarted_review_approval')
    if (setup_proof.get('schema')!='asis-real-session-setup-proof/1' or setup_proof.get('verified') is not True
        or setup_proof.get('provider_requests')!=0 or setup_proof.get('session_id')!=failure['session_id']
        or setup_proof.get('original_service_session_preserved') is not True
        or setup_proof.get('original_history_prefix_unchanged') is not True
        or setup_proof.get('new_user_message_events')!=0):
        raise ValueError('unstarted_review_setup_proof_missing')
    bindings={name:setup_proof.get(name) for name in ('canonical_input_sha256','instructions_sha256')}
    if any(not isinstance(value,str) or not re.fullmatch(r'[0-9a-f]{64}',value) for value in bindings.values()):
        raise ValueError('unstarted_review_verified_input_binding_missing')
    destination=folder/'unstarted-review-continuations'/approval_id
    if (folder/'unstarted-review-continuation.json').exists():
        raise ValueError('unstarted_review_continuation_already_prepared')
    parent=ResumeContext.active(folder)
    if parent is None:raise ValueError('unstarted_review_parent_approval_missing')
    job=strict_json((folder/'job.json').read_bytes());snapshot=strict_json((folder/'snapshot.json').read_bytes())
    failed=strict_json((folder/'checkpoints/actions-merge/failed-items.json').read_bytes())
    ids=failed.get('failed_item_ids',[])
    if (job.get('status')!='failed' or job.get('run')!=snapshot['run'] or len(ids)!=1
        or job.get('failed_scope_resume_count')!=1 or job.get('failed_scope_resume_id')!=parent.approval['approval_id']):
        raise ValueError('unstarted_review_not_same_failed_scope')
    key='action-'+ids[0]
    expected_session='asis-review-'+digest({'run':job['run'],'key':key})[:24]
    if expected_session!=failure['session_id'] or key not in parent.approval['existing_candidate_review_only']:
        raise ValueError('unstarted_review_target_not_original_candidate')
    ledger=strict_json((folder/'task-ledger.json').read_bytes())
    if ledger['run']!=snapshot['run']:raise ValueError('unstarted_review_task_run_changed')
    pending=set()
    for row in ledger['tasks'].values():
        if row['status']!='completed':pending.update(row.get('progress_steps',{}).values())
    completed=[k for k,row in ledger['tasks'].items() if row['status']=='completed' and k not in pending]
    from asis_item_agents import project_item_graph
    selected=parent.approval['existing_candidate_review_only'][key]
    raw=strict_json((folder/selected['path']).read_bytes())
    input_data=strict_json((folder/selected['input_path']).read_bytes())
    candidate=project_item_graph(raw,input_data['stage_data']['item_assignment'])
    if digest(candidate)!=selected['projected_candidate_sha256']:raise ValueError('unstarted_review_original_candidate_changed')
    protected={str(p.relative_to(folder)):file_sha(p) for p in (folder/'checkpoints').rglob('accepted.json')}
    current_pipeline=(Path(__file__).parent/'asis_pipeline.py').read_bytes()
    record={'schema':'asis-unstarted-review-continuation/1','approval_id':approval_id,'run':job['run'],
        'parent_approval_id':parent.approval['approval_id'],'parent_approval_sha256':file_sha(parent.root/'approval.json'),
        'implementation_sha256':implementation_sha256,'snapshot_sha256':file_sha(folder/'snapshot.json'),
        'job_before_sha256':file_sha(folder/'job.json'),'task_ledger_before_sha256':file_sha(folder/'task-ledger.json'),
        'failure_path':failure_path,'failure_sha256':file_sha(folder/failure_path),'setup_proof':deepcopy(setup_proof),
        'target_session_id':expected_session,'target_author_session_id':expected_session.replace('asis-review-','asis-author-',1),
        'candidate_sha256':digest(candidate),'failed_item_ids':ids,'completed_item_ids':failed['successful_item_ids'],
        'completed_task_ids':completed,'completed_task_record_sha256':{k:digest(ledger['tasks'][k]) for k in completed},
        'protected_checkpoints':protected,'review_call_budget':1,'author_rounds_increased':False,'review_gate_relaxed':False,
        'retry_count_preserved':job.get('retry_count',0),'failed_scope_resume_count_preserved':1,
        'current_pipeline_sha256':hashlib.sha256(current_pipeline).hexdigest(),'session_restores':[],**bindings}
    sessions={}
    for p in (folder/'checkpoints').rglob('*receipt.json'):
        receipt=strict_json(p.read_bytes());identifier=receipt.get('session_id');role=receipt.get('role')
        if not receipt.get('service_session_id') or receipt.get('provider')!='github-copilot':continue
        old=sessions.get(identifier)
        if old and old['session_turn']>=receipt.get('session_turn',0):continue
        events=folder/'sessions'/role/'session-state'/receipt['service_session_id']/'events.jsonl'
        if not events.is_file():events=folder/'sessions/session-state'/receipt['service_session_id']/'events.jsonl'
        if not events.is_file():raise ValueError('unstarted_review_history_missing')
        value={n:receipt[n] for n in ('session_id','role','service_session_id','instructions_sha256',
            'requested_model','actual_model','requested_reasoning_effort','fallback_used','session_turn')}
        value.update(events_path=str(events.relative_to(folder)),events_prefix_bytes=events.stat().st_size,events_prefix_sha256=file_sha(events))
        sessions[identifier]=value
    record['session_restores']=list(sessions.values())
    destination.mkdir(parents=True,exist_ok=False)
    (destination/'job-before.json').write_bytes((folder/'job.json').read_bytes())
    (destination/'task-ledger-before.json').write_bytes((folder/'task-ledger.json').read_bytes())
    (destination/'current-pipeline.py').write_bytes(current_pipeline)
    from asis_view import atomic
    atomic(destination/'approval.json',record)
    atomic(folder/'unstarted-review-continuation.json',record)
    return record
