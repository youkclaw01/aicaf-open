"""AS-IS 개정·생성·실물·다운로드 API. 프로젝트 원본과 이전 후보를 보존하며 품질 완료를 자동 선언하지 않는다."""
from __future__ import annotations

import asyncio
import subprocess
import sys
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
import urllib.parse

from aiohttp import web

import asis_analysis
import asis_artifacts
import asis_model


FILES = {'pptx':'asis_report_pptx','xlsx':'asis_backdata_xlsx','pdf':'asis_render_pdf'}
MIME = {'pptx':'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        'xlsx':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','pdf':'application/pdf','png':'image/png'}


def atomic(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+f'.tmp-{os.getpid()}-{secrets.token_hex(8)}')
    try:
        temporary.write_bytes(value if isinstance(value,bytes) else asis_model.encoded(value))
        for attempt in range(7):
            try:
                os.replace(temporary,path);return
            except PermissionError:
                if attempt==6:raise
                time.sleep(.05*(attempt+1))
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def read(path,default=None):
    for attempt in range(7):
        try:return asis_model.strict_json(Path(path).read_bytes())
        except FileNotFoundError:return default
        except PermissionError:
            if attempt==6:raise
            time.sleep(.05*(attempt+1))


CREATE_BREAKAWAY_FROM_JOB=0x01000000


def spawn_worker(command,*,log,cwd,env,windows):
    """분리 워커를 띄운다. 반환은 (프로세스, 작업 개체 이탈 여부). 이탈 여부는 Windows 에서만 True/False 이고 그 외는 None.

    Windows 의 SSH 세션·서비스 안 프로세스는 Job 개체에 묶여 있어 DETACHED_PROCESS 만으로는 부모 세션이 끝날 때 워커가 함께 종료된다
    (2026-10-17 프로젝트 25 개정이 시작 1초 만에 사라진 원인). 그래서 작업 개체 이탈을 먼저 요청하고,
    작업 개체가 이탈을 허용하지 않아 거부(PermissionError)되는 환경에서만 이탈 없이 다시 시도한다. 다른 오류는 삼키지 않는다.
    """
    common={'stdin':subprocess.DEVNULL,'stdout':log if log is not None else subprocess.DEVNULL,'stderr':subprocess.STDOUT,'cwd':cwd,'env':env}
    if not windows:
        return subprocess.Popen(command,start_new_session=True,**common),None
    base=subprocess.DETACHED_PROCESS|subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        return subprocess.Popen(command,creationflags=base|CREATE_BREAKAWAY_FROM_JOB,**common),True
    except PermissionError:
        return subprocess.Popen(command,creationflags=base,**common),False


class Controller:
    def __init__(self, *, ROOT, assessment, infra_case_dir, db, source_folder=None, runtime_factory=None, test_analyzer=None, report_kind=None):
        if report_kind not in (None, 'current_state', 'interview'):
            raise ValueError('invalid_report_kind')
        self.report_kind = report_kind
        self.root=Path(ROOT)/({'current_state':'current-state','interview':'interview-reports'}.get(report_kind,'asis'));self.assessment=assessment;self.infra_case_dir=infra_case_dir;self.db=db
        self.source_folder=source_folder;self.builder=asis_artifacts.build;self.locks={};self.guard=threading.Lock()
        if report_kind == 'current_state':
            from assessment_flow_artifacts import build_current_state
            self.builder = build_current_state
        self.workflow_provider = None
        self.workflow_guard = None
        self.runtime_factory=runtime_factory;self.test_analyzer=test_analyzer
        default=(self.root.parent.parent/'.venv-asis-maf/Scripts/python.exe') if os.name=='nt' else Path(sys.executable)
        self.worker_python=Path(os.environ.get('AICAF_ASIS_PYTHON',str(default)))

    def runtime_ready(self):
        if self.test_analyzer is not None or self.runtime_factory is not None:return True
        from asis_runtime import policy,implementation_fingerprint
        ready=read(self.root.parent/'asis-runtime-ready.json',{})
        fingerprint=implementation_fingerprint()
        return bool(self.worker_python.is_file() and ready.get('verified') is True
                    and fingerprint is not None and ready.get('implementation_sha256')==fingerprint
                    and ready.get('model_policy')==policy() and ready.get('template_sha256')==asis_artifacts.TEMPLATE_HASH)

    def folder(self,aid):
        if isinstance(aid,bool) or not isinstance(aid,int) or aid<1:raise web.HTTPNotFound()
        return self.root/str(aid)

    def job_dir(self,aid,revision):
        if not isinstance(revision,str) or not re.fullmatch(r'r-[0-9]{8}T[0-9]{6}-[0-9a-f]{16}',revision):raise web.HTTPNotFound()
        return self.folder(aid)/'revisions'/revision

    @contextmanager
    def mutation(self,aid):
        with self.guard:lock=self.locks.setdefault(aid,threading.RLock())
        with lock:
            folder=self.folder(aid);folder.mkdir(parents=True,exist_ok=True)
            with open(folder/'mutation.lock','a+b') as handle:
                if os.name=='nt' and handle.tell()==0:handle.write(b'0');handle.flush()
                deadline=time.monotonic()+10
                while True:
                    try:
                        handle.seek(0)
                        if os.name=='nt':
                            import msvcrt
                            msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
                        else:
                            import fcntl
                            fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time.monotonic()>deadline:raise web.HTTPConflict(text='AS-IS 저장 잠금 대기 시간 초과')
                        time.sleep(.05)
                try:yield
                finally:
                    handle.seek(0)
                    if os.name=='nt':
                        import msvcrt
                        msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(),fcntl.LOCK_UN)

    def collect(self,aid,revision='current',requirements=''):
        assessment=self.assessment(aid)
        if assessment['id']!=aid:raise web.HTTPNotFound()
        cid=assessment.get('infra_case_id')
        if not cid:raise web.HTTPConflict(text='연결된 수집 케이스가 없습니다. 수집 파일을 먼저 올려야 합니다.')
        source=self.source_folder(assessment) if self.source_folder else self.infra_case_dir(cid)/'Source'
        answers = []
        if self.report_kind != 'current_state':
            with self.db() as conn:
                answers=[dict(r) for r in conn.execute('select qid,transcript,summary,score from answers where assessment_id=? order by qid',(aid,))]
        try:
            data = asis_model.normalize(source,assessment,answers,revision,requirements)
            if self.report_kind:
                data['context']['report_kind'] = self.report_kind
            return self.workflow_provider(aid, data) if self.workflow_provider is not None else data
        except ValueError as exc:raise web.HTTPConflict(text='수집·범위 바인딩 확인 필요: '+str(exc)) from exc

    def state(self,aid):
        try:return read(self.folder(aid)/'state.json',{})
        except (ValueError,OSError) as exc:raise web.HTTPConflict(text='AS-IS 개정 원장 손상: 이전 파일은 보존되며 명시적 복구가 필요합니다.') from exc

    def job(self,aid,revision):
        path=self.job_dir(aid,revision);row=read(path/'job.json')
        if not isinstance(row,dict) or row.get('run',{}).get('assessment_id')!=aid:raise web.HTTPNotFound()
        if row['status']=='running' and time.time()-row.get('heartbeat_epoch',row.get('started_epoch',time.time()))>90:
            from asis_worker import owner_proven_dead
            if owner_proven_dead(row):
                row={**row,'status':'interrupted','error':'실행 작업이 종료된 것으로 확인되었습니다. 입력·직전 후보·검토 기록은 보존됩니다.'}
        progress=row.get('progress')
        if isinstance(progress,dict):
            ledger=(progress.get('task_progress') or {}).get('stage_ledger')
            if isinstance(ledger,dict):
                import asis_stages
                progress['stage']=asis_stages.compute(ledger,job_status=row.get('status','running'))
            elif row.get('status')=='generated_candidate':
                import asis_stages
                progress['stage']=asis_stages.compute({},job_status='generated_candidate')
        return row

    def current(self,aid):
        data=self.collect(aid);state=self.state(aid)
        def job(key):
            revision=state.get(key)
            return self.job(aid,revision) if revision else None
        latest=job('latest_revision_id');candidate=job('candidate_revision_id')
        return {'schema':'ai-assessment-asis-portal/r2','input_sha256':data['run']['source_bundle_sha256'],
                'case_id':data['run']['case_id'],'track':data['run']['track'],'latest':latest,'candidate':candidate,
                'candidate_matches_current_input':bool(candidate and candidate['run']['source_bundle_sha256']==data['run']['source_bundle_sha256']),
                'can_generate':bool(data['context']['input_files']) and self.runtime_ready(),
                'runtime_ready':self.runtime_ready(),'area_names':asis_analysis.AREA_NAMES,
                'quality_status':'not_independently_reviewed',
                'source_files':data['context']['input_files'],'collection_states':data['context']['file_states']}

    def create(self,aid,body):
        if not isinstance(body,dict):raise web.HTTPBadRequest(text='AS-IS 요청 본문은 JSON 객체여야 합니다.')
        if self.workflow_guard is not None:
            self.workflow_guard(aid)
        if not self.runtime_ready():raise web.HTTPServiceUnavailable(text='현황분석 실행 환경의 운영 검증이 아직 완료되지 않았습니다.')
        nonce=body.get('nonce');requirements=body.get('requirements','');parent=body.get('parent_revision_id')
        if not isinstance(nonce,str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,100}',nonce):raise web.HTTPBadRequest(text='중복 방지 요청 ID가 필요합니다.')
        if not isinstance(requirements,str) or len(requirements)>6000:raise web.HTTPBadRequest(text='추가 요구사항의 형식/길이를 확인해야 합니다.')
        with self.mutation(aid):
            data=self.collect(aid,requirements=requirements)
            if body.get('input_sha256')!=data['run']['source_bundle_sha256']:raise web.HTTPConflict(text='수집 파일 또는 인터뷰가 변경되었습니다. 현재 입력을 다시 조회해야 합니다.')
            state=self.state(aid)
            fingerprint=asis_model.digest({'input':body['input_sha256'],'requirements':requirements,'parent':parent,
                                           'contract':asis_artifacts.sha(asis_model.ASSET/'contract/contract.json'),
                                           'visual':asis_artifacts.TEMPLATE_HASH,
                                           'model_policy':__import__('asis_runtime').policy()})
            reqpath=self.folder(aid)/'requests'/(hashlib.sha256(nonce.encode('utf-8')).hexdigest()+'.json')
            previous=read(reqpath)
            if previous:
                if previous['fingerprint']!=fingerprint:raise web.HTTPConflict(text='같은 요청 ID에 다른 입력을 사용할 수 없습니다.')
                return self.job(aid,previous['revision_id'])
            if parent!=state.get('candidate_revision_id'):raise web.HTTPConflict(text='직전 후보 개정이 변경되었습니다. 현재 원장을 다시 조회해야 합니다.')
            if state.get('latest_revision_id'):
                last=self.job(aid,state['latest_revision_id'])
                if last['status'] in ('queued','running'):raise web.HTTPConflict(text='이미 AS-IS 생성이 진행 중입니다.')
            revision='r-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+secrets.token_hex(8)
            data['run']['analysis_revision']=revision
            for fact in data['facts']:fact['run']=deepcopy(data['run'])
            # 승인 프롬프트와 직전 결과는 개정에 동결한다. 추가 요구를 실제 고객 답변으로 승격하지 않는다.
            prior=read(self.job_dir(aid,parent)/'analysis.json') if parent else None
            folder=self.job_dir(aid,revision);folder.mkdir(parents=True,exist_ok=False)
            atomic(folder/'snapshot.json',data)
            atomic(folder/'revision-context.json',{'requirements':requirements,'parent_revision_id':parent,'prior_output':prior,
                                                  'prompt_sha256':{p.name:asis_artifacts.sha(p) for p in (asis_model.ASSET/'contract/prompts').glob('*.txt')}})
            row={'revision_id':revision,'run':data['run'],'status':'queued','requirements':requirements,
                 'requested_model_policy':__import__('asis_runtime').policy(),
                 'parent_revision_id':parent,'created_at':datetime.now(timezone.utc).isoformat(),
                 'snapshot_sha256':asis_artifacts.sha(folder/'snapshot.json'),'quality_status':'not_independently_reviewed',
                 'files':[],'steps':[{'name':'snapshot','status':'ok'},{'name':'analysis','status':'pending'},{'name':'native_pair','status':'pending'},{'name':'actual_render','status':'pending'}]}
            atomic(folder/'job.json',row);atomic(reqpath,{'fingerprint':fingerprint,'revision_id':revision})
            atomic(self.folder(aid)/'state.json',{**state,'latest_revision_id':revision})
            return row

    def record_progress(self,aid,revision,event):
        """실제 호출·장표 검사 이벤트를 고객 진행 원장에 반영한다."""
        folder=self.job_dir(aid,revision)
        with self.mutation(aid):
            row=read(folder/'job.json')
            progress=row.setdefault('progress',{'units':[],'overall_percent':3})
            unit=event['unit'];phase=event['phase']
            if phase=='repair' and event['status']=='required':
                affected=set(event.get('affected_units',[unit]))
                invalid=set(event.get('invalidated_dependents',[]))|affected
                for target in affected:
                    if target.startswith('area-'):invalid.update(('synthesis','actions','summary','document'))
                    elif target=='synthesis':invalid.update(('actions','summary','document'))
                    elif target=='actions':invalid.update(('summary','document'))
                    elif target=='summary':invalid.add('document')
                invalid.update(('native_pair','actual_render','visual_pages','pair_consistency'))
                for item in progress['units']:
                    if item['unit'] in invalid and item['phase']!='repair':
                        item['status']='invalidated'
                        if item['unit']=='visual_pages':item['reviewed_pages']=0
                progress['invalidated_units']=sorted(invalid)
                row['quality_status']='not_independently_reviewed'
                for step in row.get('steps',[]):
                    if step['name'] in ('analysis','native_pair','actual_render'):step['status']='pending'
            prior=next((x for x in progress['units'] if x['unit']==unit and x['phase']==phase),None)
            value=dict(event);value['updated_at']=datetime.now(timezone.utc).isoformat()
            if prior is None:progress['units'].append(value)
            else:prior.update(value)
            completed={(v['unit'],v['phase']) for v in progress['units'] if v['status'] in ('completed','reused')}
            area_count=sum(1 for u,p in completed if u.startswith('area-') and p in ('author','content_review'))
            expected_areas=2*len(asis_analysis.AREA_NAMES)
            score=3+7*int(('interpret','interpret') in completed)+40*min(area_count,expected_areas)/expected_areas
            for name,weight in (('synthesis',8),('actions',10),('summary',7)):
                score+=weight*int((name,'content_review') in completed)
            score+=5*int(('document','content_review') in completed)+6*int(('native_pair','native') in completed)+4*int(('actual_render','render') in completed)
            visual=next((v for v in reversed(progress['units']) if v['unit']=='visual_pages' and v['phase']=='visual_review'),None)
            if visual and visual.get('total_pages'):
                score+=8*min(visual.get('reviewed_pages',0)/visual['total_pages'],1)
                progress['reviewed_pages']=visual.get('reviewed_pages',0);progress['total_pages']=visual['total_pages']
            score+=2*int(('pair_consistency','consistency') in completed)
            progress['overall_percent']=round(min(score,100),1)
            if phase=='tasks' and isinstance(event.get('task_progress'),dict):
                progress['task_progress']=deepcopy(event['task_progress'])
            central=progress.get('task_progress')
            if central and central.get('schema')=='asis-task-progress/1':
                progress.update({key:deepcopy(central[key]) for key in
                    ('overall_percent','completed_tasks','total_tasks','running_tasks','waiting_tasks','blocked_tasks','plan_version') if key in central})
                progress['progress_basis']='verified_central_tasks'
            ledger=(progress.get('task_progress') or {}).get('stage_ledger')
            if isinstance(ledger,dict):
                import asis_stages
                progress['stage']=asis_stages.compute(ledger,job_status=row.get('status','running'))
            row['updated_at']=value['updated_at'];row['heartbeat_epoch']=time.time()
            atomic(folder/'job.json',row)

    def retry_existing(self, aid, revision, *, retry_id, expected_snapshot_sha256,
                       expected_implementation_sha256):
        """승인된 실패 개정 한 번만 재대기한다. 최초 실패·원문·완료 체크포인트는 보존한다."""
        from asis_runtime import implementation_fingerprint, policy
        from asis_worker import process_identity,owner_proven_dead
        if not isinstance(retry_id, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,100}', retry_id):
            raise ValueError('invalid_retry_id')
        if any(not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value)
               for value in (expected_snapshot_sha256, expected_implementation_sha256)):
            raise ValueError('invalid_retry_binding')
        folder = self.job_dir(aid, revision)
        with self.mutation(aid):
            row = self.job(aid, revision)
            if self.state(aid).get('latest_revision_id') != revision:
                raise ValueError('retry_latest_revision_mismatch')
            if implementation_fingerprint() != expected_implementation_sha256:
                raise ValueError('retry_implementation_mismatch')
            if (row.get('snapshot_sha256') != expected_snapshot_sha256
                    or hashlib.sha256((folder/'snapshot.json').read_bytes()).hexdigest() != expected_snapshot_sha256):
                raise ValueError('retry_frozen_snapshot_mismatch')
            if row.get('requested_model_policy') != policy():
                raise ValueError('retry_model_policy_mismatch')
            # 같은 승인 ID의 재전달은 새 재시도나 프로세스 생성으로 확대하지 않는다.
            if row.get('retry_id') == retry_id:
                return row
            if row['status'] not in ('failed', 'interrupted'):
                raise ValueError('retry_requires_terminal_failure')
            if not owner_proven_dead(row):
                raise ValueError('retry_owner_not_proven_dead')
            if row.get('retry_count', 0) != 0:
                raise ValueError('retry_budget_exhausted')
            before = (folder/'job.json').read_bytes()
            atomic(folder/'retry-history'/retry_id/'job-before.json', before)
            for field in ('error', 'finished_at', 'started_epoch', 'heartbeat_epoch', 'launched_epoch'):
                row.pop(field, None)
            row.update(status='queued', pid=None, process_identity=None, retry_id=retry_id,
                       retry_count=1, retry_implementation_sha256=expected_implementation_sha256,
                       retry_requested_at=datetime.now(timezone.utc).isoformat())
            atomic(folder/'job.json', row)
            return row

    # 첫 재대기(retry_existing) 이후 실패 원인을 고친 코드가 배포될 때마다 한 번씩, 총 CODE_FIX_RETRY_MAX 회까지(운영 2026-10-18: 영석님이 같은 개정 이어가기를 지시. 2→3→4→5→6→7→8→9→10→11→12→13으로 올린 이유는 매번 새 원인의 결함이었고, 코드가 바뀐 경우에만 허용한다. 이번 13이 마지막이다).
    CODE_FIX_RETRY_MAX = 13

    def retry_after_code_fix(self, aid, revision, *, retry_id, expected_snapshot_sha256, expected_implementation_sha256):
        """첫 재대기(retry_existing)를 쓴 개정을 실패 원인을 고친 코드가 배포된 뒤에만 한 번 더 재대기한다.

        운영 2026-10-18: 프로젝트 25 개정이 7단계에서 서로 다른 결함 4종으로 failed 가 됐고 첫 재대기는 이미 썼다.
        완료 체크포인트(승인된 작성·검토 쌍)는 같은 입력이면 호출 없이 재사용되므로 새 개정(영역부터 재실행, 수십 달러·수십 분) 대신
        같은 개정에서 실패 단위부터 이어 간다. 조건: 첫 재대기가 있었고, 그때와 구현 지문이 달라졌고(코드 수정 배포),
        스냅샷·모델 정책 불변, 소유자 사망 증명, 총 2회 상한. 상한을 넘기거나 코드가 같으면 거부한다. 원문·실패 기록은 retry-history 에 보존한다.
        """
        from asis_runtime import implementation_fingerprint, policy
        from asis_worker import process_identity,owner_proven_dead
        if not isinstance(retry_id, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,100}', retry_id):
            raise ValueError('invalid_retry_id')
        if any(not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value)
               for value in (expected_snapshot_sha256, expected_implementation_sha256)):
            raise ValueError('invalid_retry_binding')
        folder = self.job_dir(aid, revision)
        with self.mutation(aid):
            row = self.job(aid, revision)
            if self.state(aid).get('latest_revision_id') != revision:
                raise ValueError('retry_latest_revision_mismatch')
            if implementation_fingerprint() != expected_implementation_sha256:
                raise ValueError('retry_implementation_mismatch')
            if (row.get('snapshot_sha256') != expected_snapshot_sha256
                    or hashlib.sha256((folder/'snapshot.json').read_bytes()).hexdigest() != expected_snapshot_sha256):
                raise ValueError('retry_frozen_snapshot_mismatch')
            if row.get('requested_model_policy') != policy():
                raise ValueError('retry_model_policy_mismatch')
            if row.get('retry_id') == retry_id and row.get('retry_count') in range(2, self.CODE_FIX_RETRY_MAX + 1):
                return row                                     # 같은 승인 ID 재전달은 확대하지 않는다
            if row['status'] not in ('failed', 'interrupted'):
                raise ValueError('retry_requires_terminal_failure')
            if not owner_proven_dead(row):
                raise ValueError('retry_owner_not_proven_dead')
            count = row.get('retry_count', 0)
            if count == 0:
                raise ValueError('retry_first_use_retry_existing')
            if count >= self.CODE_FIX_RETRY_MAX:
                raise ValueError('retry_budget_exhausted')
            if row.get('retry_implementation_sha256') == expected_implementation_sha256:
                raise ValueError('retry_code_not_changed')
            atomic(folder/'retry-history'/retry_id/'job-before.json', (folder/'job.json').read_bytes())
            for field in ('error', 'finished_at', 'started_epoch', 'heartbeat_epoch', 'launched_epoch'):
                row.pop(field, None)
            row.update(status='queued', pid=None, process_identity=None, retry_id=retry_id,
                       retry_count=count + 1, retry_implementation_sha256=expected_implementation_sha256,
                       retry_requested_at=datetime.now(timezone.utc).isoformat())
            atomic(folder/'job.json', row)
            return row

    def resume_failed_scope(self,aid,revision,*,approval_id,expected_snapshot_sha256,expected_implementation_sha256):
        """명시 코드 복구 승인을 같은 실패 개정에 한 번 결합한다. 기존 retry/예산·완료 상태는 바꾸지 않는다."""
        from asis_resume import ResumeContext,file_sha
        from asis_runtime import implementation_fingerprint,policy
        from asis_worker import process_identity,owner_proven_dead
        if not isinstance(approval_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,96}',approval_id):
            raise ValueError('invalid_failed_scope_approval')
        if any(not isinstance(value,str) or not re.fullmatch(r'[0-9a-f]{64}',value)
               for value in (expected_snapshot_sha256,expected_implementation_sha256)):
            raise ValueError('invalid_failed_scope_binding')
        folder=self.job_dir(aid,revision)
        with self.mutation(aid):
            row=read(folder/'job.json')
            if self.state(aid).get('latest_revision_id')!=revision:raise ValueError('resume_latest_revision_changed')
            if implementation_fingerprint()!=expected_implementation_sha256:raise ValueError('resume_implementation_changed')
            if (row.get('snapshot_sha256')!=expected_snapshot_sha256 or file_sha(folder/'snapshot.json')!=expected_snapshot_sha256):
                raise ValueError('resume_frozen_snapshot_changed')
            if row.get('requested_model_policy')!=policy():raise ValueError('resume_model_policy_changed')
            if row.get('failed_scope_resume_id')==approval_id:return row
            if not isinstance(row,dict) or row.get('status')!='failed' or not owner_proven_dead(row):
                raise ValueError('resume_requires_failed_dead_owner')
            resume_count = row.get('failed_scope_resume_count', 0)
            if type(resume_count) is not int or not 0 <= resume_count < max(self.CODE_FIX_RETRY_MAX, 8):
                raise ValueError('failed_scope_resume_limit_exceeded')
            if (resume_count > 0
                and row.get('failed_scope_resume_implementation_sha256') == expected_implementation_sha256
                and not str(row.get('error') or '').startswith('RuntimeError:ssh_session_terminated')):
                raise ValueError('resume_code_not_changed')
            if not self.runtime_ready():raise ValueError('resume_runtime_not_verified')
            resume=ResumeContext.active(folder)
            if resume is None or resume.approval['approval_id']!=approval_id:raise ValueError('resume_explicit_approval_missing')
            approval=resume.approval
            if (approval['implementation_sha256']!=expected_implementation_sha256
                or approval['snapshot_sha256']!=expected_snapshot_sha256 or approval['run']!=row['run']
                or approval['job_before_sha256']!=file_sha(folder/'job.json')
                or approval['task_ledger_before_sha256']!=file_sha(folder/'task-ledger.json')
                or approval['retry_count_preserved']!=row.get('retry_count',0)):
                raise ValueError('resume_approval_source_binding_changed')
            current=self.collect(aid,requirements=row.get('requirements',''))
            if current['run']['source_bundle_sha256']!=row['run']['source_bundle_sha256']:
                raise ValueError('resume_operating_source_changed')
            for relative in approval['protected_checkpoints']:resume._protected(folder/relative)
            if file_sha(resume.root/'job-before.json')!=approval['job_before_sha256']:
                raise ValueError('resume_failure_history_not_preserved')
            for field in ('error','finished_at','started_epoch','heartbeat_epoch','launched_epoch'):
                row.pop(field,None)
            row.update(status='queued',pid=None,process_identity=None,failed_scope_resume_id=approval_id,
                failed_scope_resume_count=resume_count+1,failed_scope_resume_implementation_sha256=expected_implementation_sha256,
                failed_scope_resume_requested_at=datetime.now(timezone.utc).isoformat(),
                failed_scope={'direct_and_dependent_item_ids':approval['failed_item_ids'],
                    'preserved_completed_item_ids':approval['completed_item_ids'],
                    'author_rounds_increased':False,'review_gate_relaxed':False,'new_revision_created':False})
            atomic(folder/'job.json',row)
            return row

    def continue_unstarted_review(self,aid,revision,*,approval_id,expected_implementation_sha256,expected_approval_sha256):
        """별도 승인한 모델 미진입 검토 한 번만 같은 개정에서 이어서 실행한다. 기존 재시도 예산은 유지한다."""
        from asis_resume import file_sha
        from asis_runtime import implementation_fingerprint,policy
        from asis_worker import process_identity,owner_proven_dead
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,96}',approval_id or ''):
            raise ValueError('invalid_unstarted_review_approval')
        if any(not re.fullmatch(r'[0-9a-f]{64}',x or '') for x in (expected_implementation_sha256,expected_approval_sha256)):
            raise ValueError('invalid_unstarted_review_binding')
        folder=self.job_dir(aid,revision);root=folder/'unstarted-review-continuations'/approval_id
        with self.mutation(aid):
            row=read(folder/'job.json')
            if self.state(aid).get('latest_revision_id')!=revision:raise ValueError('continuation_latest_revision_changed')
            if implementation_fingerprint()!=expected_implementation_sha256:raise ValueError('continuation_implementation_changed')
            if file_sha(root/'approval.json')!=expected_approval_sha256:raise ValueError('continuation_approval_changed')
            approval=read(root/'approval.json')
            if read(folder/'unstarted-review-continuation.json')!=approval:raise ValueError('continuation_request_changed')
            if row.get('unstarted_review_continuation_id')==approval_id:return row
            if not isinstance(row,dict) or row.get('status')!='failed' or not owner_proven_dead(row):
                raise ValueError('continuation_requires_failed_dead_owner')
            if row.get('unstarted_review_continuation_count',0)!=0 or (folder/'unstarted-review-call-once.json').exists():
                raise ValueError('unstarted_review_continuation_budget_exhausted')
            if (approval.get('schema')!='asis-unstarted-review-continuation/1' or approval.get('review_call_budget')!=1
                or approval['implementation_sha256']!=expected_implementation_sha256 or approval['run']!=row['run']
                or approval['snapshot_sha256']!=file_sha(folder/'snapshot.json')
                or approval['job_before_sha256']!=file_sha(folder/'job.json')
                or approval['task_ledger_before_sha256']!=file_sha(folder/'task-ledger.json')
                or approval['retry_count_preserved']!=row.get('retry_count',0)
                or approval['failed_scope_resume_count_preserved']!=row.get('failed_scope_resume_count',0)
                or row.get('requested_model_policy')!=policy() or not self.runtime_ready()):
                raise ValueError('unstarted_review_continuation_source_binding_changed')
            current=self.collect(aid,requirements=row.get('requirements',''))
            if current['run']['source_bundle_sha256']!=row['run']['source_bundle_sha256']:
                raise ValueError('continuation_operating_source_changed')
            if file_sha(root/'job-before.json')!=approval['job_before_sha256']:
                raise ValueError('continuation_failure_history_not_preserved')
            for field in ('error','finished_at','started_epoch','heartbeat_epoch','launched_epoch'):
                row.pop(field,None)
            row.update(status='queued',pid=None,process_identity=None,unstarted_review_continuation_id=approval_id,
                unstarted_review_continuation_count=1,unstarted_review_continuation_approval_sha256=expected_approval_sha256,
                unstarted_review_continuation_implementation_sha256=expected_implementation_sha256)
            atomic(folder/'job.json',row)
            return row

    def retry_unclaimed_review_setup(self,aid,revision,*,approval_id,setup_retry_id,
            expected_job_sha256,expected_approval_sha256,expected_implementation_sha256,failure_path):
        """명시 승인한 준비 재실행 1회만 재대기한다. 원승인·원문·모델 1회 한도는 교체하지 않는다."""
        from asis_resume import file_sha
        from asis_runtime import implementation_fingerprint,policy
        from asis_worker import process_identity,owner_proven_dead
        if any(not re.fullmatch(r'[A-Za-z0-9_-]{1,96}',x or '') for x in (approval_id,setup_retry_id)):
            raise ValueError('invalid_unclaimed_setup_retry_id')
        if any(not re.fullmatch(r'[0-9a-f]{64}',x or '') for x in
               (expected_job_sha256,expected_approval_sha256,expected_implementation_sha256)):
            raise ValueError('invalid_unclaimed_setup_retry_binding')
        if not re.fullmatch(r'runtime-failures/[A-Za-z0-9_-]{1,96}\.json',failure_path or ''):
            raise ValueError('invalid_unclaimed_setup_failure_path')
        folder=self.job_dir(aid,revision)
        original=folder/'unstarted-review-continuations'/approval_id/'approval.json'
        root=folder/'unstarted-review-setup-retries'/setup_retry_id
        with self.mutation(aid):
            row=read(folder/'job.json',{})
            if not isinstance(row,dict):raise ValueError('invalid_job_record')
            if self.state(aid).get('latest_revision_id')!=revision:raise ValueError('setup_retry_latest_revision_changed')
            if implementation_fingerprint()!=expected_implementation_sha256 or not self.runtime_ready():
                raise ValueError('setup_retry_implementation_not_verified')
            if file_sha(original)!=expected_approval_sha256:raise ValueError('setup_retry_original_approval_changed')
            approval=read(original,{})
            if read(folder/'unstarted-review-continuation.json')!=approval:
                raise ValueError('setup_retry_original_request_changed')
            if row.get('unstarted_review_setup_retry_id')==setup_retry_id:
                recorded=read(root/'retry.json',{})
                if (file_sha(root/'retry.json')!=row.get('unstarted_review_setup_retry_sha256')
                    or recorded.get('job_before_sha256')!=expected_job_sha256
                    or recorded.get('original_approval_sha256')!=expected_approval_sha256
                    or recorded.get('implementation_sha256')!=expected_implementation_sha256
                    or recorded.get('failure_path')!=failure_path):
                    raise ValueError('setup_retry_execution_binding_changed')
                if row.get('status') in ('queued','running','generated_candidate'):return row
                raise ValueError('unclaimed_setup_retry_budget_exhausted')
            if row.get('unstarted_review_setup_retry_count',0)!=0 or root.exists():
                raise ValueError('unclaimed_setup_retry_budget_exhausted')
            if not isinstance(row,dict) or row.get('status')!='failed' or not owner_proven_dead(row):
                raise ValueError('setup_retry_requires_failed_dead_owner')
            if file_sha(folder/'job.json')!=expected_job_sha256:raise ValueError('setup_retry_job_changed')
            if (folder/'unstarted-review-call-once.json').exists():raise ValueError('setup_retry_model_claim_already_consumed')
            if (approval.get('schema')!='asis-unstarted-review-continuation/1' or approval.get('review_call_budget')!=1
                or row.get('unstarted_review_continuation_id')!=approval_id
                or row.get('unstarted_review_continuation_count')!=1 or approval.get('run')!=row.get('run')
                or row.get('snapshot_sha256')!=file_sha(folder/'snapshot.json')
                or approval.get('snapshot_sha256')!=row.get('snapshot_sha256')
                or read(folder/'snapshot.json',{}).get('run')!=row.get('run')
                or row.get('requested_model_policy')!=policy()
                or row.get('retry_count',0)!=approval.get('retry_count_preserved')
                or row.get('failed_scope_resume_count',0)!=approval.get('failed_scope_resume_count_preserved')):
                raise ValueError('setup_retry_source_or_original_budget_changed')
            failure=read(folder/failure_path,{})
            if (failure.get('schema')!='asis-runtime-failure/1' or failure.get('role')!='content_review'
                or failure.get('session_id')!=approval.get('target_session_id')
                or failure.get('failure_boundary')!='session_setup' or failure.get('model_request_started') is not False):
                raise ValueError('setup_retry_requires_unstarted_preparation_failure')
            for path in (folder/'runtime-failures').glob('*.json'):
                observed=read(path,{})
                if observed.get('session_id')==approval['target_session_id'] and observed.get('model_request_started') is True:
                    raise ValueError('setup_retry_model_request_already_started')
            for name,sha256 in approval.get('protected_checkpoints',{}).items():
                if file_sha(folder/name)!=sha256:raise ValueError('setup_retry_protected_checkpoint_changed')
            current=self.collect(aid,requirements=row.get('requirements',''))
            if current['run']['source_bundle_sha256']!=row['run']['source_bundle_sha256']:
                raise ValueError('setup_retry_operating_source_changed')
            record={'schema':'asis-unclaimed-review-setup-retry/1','setup_retry_id':setup_retry_id,'run':row['run'],
                'original_approval_id':approval_id,'original_approval_sha256':expected_approval_sha256,
                'job_before_sha256':expected_job_sha256,'task_ledger_before_sha256':file_sha(folder/'task-ledger.json'),
                'snapshot_sha256':row['snapshot_sha256'],'failure_path':failure_path,'failure_sha256':file_sha(folder/failure_path),
                'implementation_sha256':expected_implementation_sha256,'target_session_id':approval['target_session_id'],
                'review_call_budget':1,'model_request_started':False,'original_model_claim_consumed':False,
                'author_rounds_increased':False,'review_gate_relaxed':False,'setup_retry_budget':1,
                'retry_count_preserved':row.get('retry_count',0),'failed_scope_resume_count_preserved':row.get('failed_scope_resume_count',0),
                'requested_at':datetime.now(timezone.utc).isoformat()}
            atomic(root/'job-before.json',(folder/'job.json').read_bytes())
            atomic(root/'task-ledger-before.json',(folder/'task-ledger.json').read_bytes())
            atomic(root/'failure-before.json',(folder/failure_path).read_bytes())
            atomic(root/'retry.json',record)
            if file_sha(folder/'job.json')!=expected_job_sha256:raise ValueError('setup_retry_job_changed_before_queue')
            for field in ('error','finished_at','started_epoch','heartbeat_epoch','launched_epoch'):
                row.pop(field,None)
            row.update(status='queued',pid=None,process_identity=None,unstarted_review_setup_retry_id=setup_retry_id,
                unstarted_review_setup_retry_count=1,unstarted_review_setup_retry_sha256=file_sha(root/'retry.json'),
                unstarted_review_setup_retry_implementation_sha256=expected_implementation_sha256)
            atomic(folder/'job.json',row)
            return row

    def launch(self,aid,revision):
        """허가된 동결 개정만 별도 제품 프로세스로 실행한다. 자격증명은 인자·파일에 쓰지 않는다."""
        from asis_worker import process_identity,owner_proven_dead
        folder=self.job_dir(aid,revision)
        with self.mutation(aid):
            row=self.job(aid,revision)
            if row['status']!='queued' or not owner_proven_dead(row):return row
            if not self.worker_python.is_file():raise RuntimeError('asis_worker_interpreter_unavailable')
            command=[str(self.worker_python),'-B',str(Path(__file__).with_name('asis_worker.py')),
                     '--data-dir',str(self.root.parent),'--assessment',str(aid),'--revision',revision]
            if self.report_kind:
                command += ['--report-kind', self.report_kind]
            keep={'PATH','PATHEXT','SYSTEMROOT','WINDIR','SYSTEMDRIVE','COMSPEC','TEMP','TMP','TMPDIR',
                  'USERPROFILE','HOME','HOMEDRIVE','HOMEPATH','APPDATA','LOCALAPPDATA','PROGRAMFILES',
                  'PROGRAMFILES(X86)','PROGRAMDATA','ALLUSERSPROFILE','LANG','LC_ALL','GITHUB_TOKEN',
                  'HTTP_PROXY','HTTPS_PROXY','NO_PROXY','SSL_CERT_FILE','REQUESTS_CA_BUNDLE'}
            env={k:v for k,v in os.environ.items() if k.upper() in keep}
            with open(folder/'worker.log','ab') as log:
                proc,breakaway=spawn_worker(command,log=log,cwd=str(Path(__file__).parent),env=env,windows=os.name=='nt')
            row.update(pid=proc.pid,process_identity=process_identity(proc.pid),launched_epoch=time.time())
            if breakaway is not None:row['launch_breakaway']=breakaway
            atomic(folder/'job.json',row)
            return row

    def heartbeat(self,aid,revision,stop):
        while not stop.wait(15):
            with self.mutation(aid):
                folder=self.job_dir(aid,revision);row=read(folder/'job.json',{})
                if row.get('status')!='running' or row.get('pid')!=os.getpid():return
                row['heartbeat_epoch']=time.time();atomic(folder/'job.json',row)

    def store_step(self,folder,name):
        """근거·분석 저장소 적재(파생물). 어떤 실패도 개정 상태에 영향을 주지 않고 job.json의 store 항목에만 남긴다."""
        try:
            import asis_store_link as link
            if name=='evidence':
                out=link.load_evidence(folder);summary={'ok':bool(out.get('ok')),'reused':bool(out.get('reused')),'sha256':out.get('sha256','')[:16]}
                if not out.get('ok'):summary['error']=str(out.get('error'))[:160]
            else:
                out=link.load_analysis(folder);summary={'ok':bool(out.get('ok'))}
                if out.get('ok'):
                    check=out.get('validate') or {};summary['validate_ok']=bool(check.get('ok'));summary['dangling']=len(check.get('dangling') or [])
                    audit=link.audit_citations(folder);summary['audit']={'ok':bool(audit.get('ok')),'cited':audit.get('cited',0),'missing':len(audit.get('missing_in_evidence') or [])}
                else:summary['error']=str(out.get('error'))[:160]
        except Exception as exc:
            summary={'ok':False,'error':(type(exc).__name__+':'+str(exc))[:160]}
        try:
            row=read(folder/'job.json',{})
            if not isinstance(row,dict):raise ValueError('invalid_job_record')
            store=dict(row.get('store') or {});store[name]=summary
            if name=='analysis' and 'audit' in summary:store['audit']=summary.pop('audit')
            row['store']=store;atomic(folder/'job.json',row)
        except Exception:
            pass
        return summary

    def report_parallelism(self,folder):
        """개정 종료 때 단계별 병렬도 보고서를 남기고 경고 요약을 job.json 에 싣는다. 실패해도 개정 결과는 바꾸지 않는다."""
        try:
            import asis_parallelism
            result=asis_parallelism.report_for_revision(folder)
            warnings=result.get('warnings') or []
            row=read(folder/'job.json',None)
            if isinstance(row,dict):
                row['parallelism_warnings']=[{k:w.get(k) for k in ('stage','cause','reason','independent_units','avg_concurrency','loss_seconds')} for w in warnings]
                atomic(folder/'job.json',row)
            return result
        except Exception:
            return None

    def generate(self,aid,revision):
        folder=self.job_dir(aid,revision)
        with self.mutation(aid):
            row=self.job(aid,revision)
            if row['status']!='queued':return row
            from asis_worker import process_identity,owner_proven_dead
            if row.get('pid') and row['pid']!=os.getpid():
                # Windows venv 실행기는 실제 Python의 부모다. 등록 PID·생성 신원이 같은 부모만 인계를 허용한다.
                launcher_identity=process_identity(row['pid'])
                if (row['pid']!=os.getppid() or not row.get('process_identity')
                        or launcher_identity in (None,'unverifiable')
                        or launcher_identity!=row['process_identity']):return row
            row.update(status='running',started_epoch=time.time(),heartbeat_epoch=time.time(),pid=os.getpid(),process_identity=process_identity(os.getpid()))
            atomic(folder/'job.json',row)
        stop=threading.Event();pulse=threading.Thread(target=self.heartbeat,args=(aid,revision,stop),daemon=True);pulse.start()
        try:
            raw=(folder/'snapshot.json').read_bytes()
            if hashlib.sha256(raw).hexdigest()!=row['snapshot_sha256']:raise ValueError('frozen_snapshot_hash_mismatch')
            data=asis_model.strict_json(raw)
            revision_context=read(folder/'revision-context.json')
            self.store_step(folder,'evidence')
            if self.test_analyzer is not None:
                # 테스트 전송 seam은 HTTP/환경설정으로 활성화할 수 없으며 제품 기본 경로에는 우회가 없다.
                result=self.test_analyzer(data,revision_context=revision_context)
                manifest=self.builder(result,folder)
            else:
                from asis_service import execute
                from asis_runtime import Runtime
                if self.report_kind == 'interview':
                    from assessment_flow_interview import execute
                result,manifest=asyncio.run(execute(data,folder,builder=self.builder,
                    progress=lambda event:self.record_progress(aid,revision,event),
                    revision_context=revision_context,runtime_factory=self.runtime_factory or Runtime))
            atomic(folder/'analysis.json',result)
            self.store_step(folder,'analysis')
            row=read(folder/'job.json')
            for step in row['steps']:step['status']='ok'
            applied = result.get('requirements_application')
            if applied is None:
                applied = result.get('input',{}).get('context',{}).get('requirements_application',{'items':[]})
            row.update(status='generated_candidate',finished_at=datetime.now(timezone.utc).isoformat(),
                       files=manifest['artifacts'],artifact_manifest_sha256=asis_artifacts.sha(folder/'artifact-manifest.json'),
                       quality_status=manifest['quality_status'],model_call_performed=result['model_call_performed'],
                       submission_approved=False,requirements_application=applied,
                       limitations=['수집 시점 이후 운영 변경·실제 동작 검증은 별도','자동 내용·실렌더 검토와 고객 제출 승인은 별도'])
            if self.report_kind == 'current_state':
                from assessment_workflow import derive_questions
                try:
                    questionnaire = derive_questions(result['input'], {'revision_id': revision, 'sha256': row['artifact_manifest_sha256']}, current_analysis=result)
                    atomic(folder/'questionnaire.json', questionnaire)
                    row['questionnaire_sha256'] = asis_model.digest(questionnaire)
                except (ValueError, KeyError, OSError) as exc:
                    row['questionnaire_error'] = type(exc).__name__+':'+str(exc)[:300]
            atomic(folder/'job.json',row)
            with self.mutation(aid):
                state=self.state(aid)
                atomic(self.folder(aid)/'state.json',{**state,'candidate_revision_id':revision})
        except Exception as exc:
            row=read(folder/'job.json',row)
            error=re.sub(r'(?:github_pat_|gh[opusr]_)[A-Za-z0-9_]+','[REDACTED]',str(exc))
            error=re.sub(r'Bearer\s+\S+','Bearer [REDACTED]',error,flags=re.I)
            row.update(status='failed',error=type(exc).__name__+':'+error[:500],finished_at=datetime.now(timezone.utc).isoformat())
            try:
                # 실패 위치를 나중에 추적할 수 있도록 예외 스택(비밀 제거)을 개정 폴더에 남긴다. 저장 실패는 원래 실패 처리를 막지 않는다.
                import traceback
                trace=''.join(traceback.format_exception(type(exc),exc,exc.__traceback__))
                trace=re.sub(r'(?:github_pat_|gh[opusr]_)[A-Za-z0-9_]+','[REDACTED]',trace)
                trace=re.sub(r'Bearer\s+\S+','Bearer [REDACTED]',trace,flags=re.I)
                failures=folder/'failure-traces'
                failures.mkdir(parents=True,exist_ok=True)
                name=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+type(exc).__name__+'.txt'
                (failures/name).write_text(trace[-60000:],encoding='utf-8')
                row['failure_trace']='failure-traces/'+name
            except (OSError,ValueError):
                pass
            atomic(folder/'job.json',row)
        finally:
            stop.set();pulse.join(timeout=1)
            self.report_parallelism(folder)
        return row

    def manifest(self,aid,revision):
        row=self.job(aid,revision);folder=self.job_dir(aid,revision);path=folder/'artifact-manifest.json'
        if row['status']!='generated_candidate' or not path.is_file():raise web.HTTPNotFound()
        if asis_artifacts.sha(path)!=row.get('artifact_manifest_sha256'):raise web.HTTPConflict(text='산출물 원장 무결성 불일치')
        manifest=read(path)
        if manifest['run']!=row['run']:raise web.HTTPConflict(text='프로젝트/개정 바인딩 불일치')
        return manifest

    def artifact(self,aid,revision,kind,page=None):
        manifest=self.manifest(aid,revision);folder=self.job_dir(aid,revision).resolve()
        if kind=='png':
            found=next((p for p in manifest['render']['previews'] if p['page']==page),None)
        else:found=next((a for a in manifest['artifacts'] if a['role']==FILES.get(kind)),None)
        if not found:raise web.HTTPNotFound()
        path=folder/found['name']
        if path.is_symlink() or not path.resolve().is_relative_to(folder) or not path.is_file():raise web.HTTPNotFound()
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=found['sha256']:raise web.HTTPConflict(text='산출물 파일 해시 불일치')
        return raw,found


def register(app,**deps):
    controller=Controller(ROOT=deps['ROOT'],assessment=deps['assessment'],infra_case_dir=deps['infra_case_dir'],db=deps['db'])
    app['asis_controller']=controller;tasks=set();app['asis_tasks']=tasks
    async def auth(req,role='viewer'):
        aid=int(req.match_info['aid']);deps['need_project'](req,aid,role);return aid
    async def current(req):
        aid=await auth(req)
        value=await asyncio.to_thread(controller.current,aid)
        value['can_edit']=deps['need_project'](req,aid) in ('manager','interviewer')
        return web.json_response(value)
    async def start(req):
        aid=await auth(req,'interviewer');body=await req.json()
        row=await asyncio.to_thread(controller.create,aid,body)
        if row['status']=='queued':
            task=asyncio.create_task(asyncio.to_thread(controller.launch,aid,row['revision_id']))
            tasks.add(task);task.add_done_callback(tasks.discard)
        deps['audit'](req['user']['upn'],aid,None,'asis-start',row['revision_id'])
        return web.json_response(row,status=202)
    async def history(req):
        aid=await auth(req);base=controller.folder(aid)/'revisions';rows=[]
        for folder in sorted(base.iterdir(),reverse=True) if base.is_dir() else []:
            if folder.is_dir():rows.append(controller.job(aid,folder.name))
        return web.json_response({'runs':rows})
    async def job(req):
        aid=await auth(req);return web.json_response(controller.job(aid,req.match_info['revision']))
    async def file(req):
        aid=await auth(req);revision=req.match_info['revision'];kind=req.match_info['kind']
        if kind not in MIME:raise web.HTTPNotFound()
        try:page=int(req.query.get('page','0'))
        except ValueError:raise web.HTTPNotFound()
        raw,found=await asyncio.to_thread(controller.artifact,aid,revision,kind,page)
        headers={'X-Content-SHA256':found['sha256'],'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}
        if kind in ('pptx','xlsx') or req.query.get('download')=='1':
            name=f'AI_Assessment_ASIS_{aid}_{revision}.{kind}'
            headers['Content-Disposition']="attachment; filename*=UTF-8''"+urllib.parse.quote(name)
        deps['audit'](req['user']['upn'],aid,None,'asis-file',revision+':'+kind)
        return web.Response(body=raw,content_type=MIME[kind],headers=headers)
    def chat_for(aid,revision):
        import asis_chat
        return asis_chat.Chat(controller.job_dir(aid,revision))
    async def chat_state(req):
        aid=await auth(req);chat=chat_for(aid,req.match_info['revision'])
        try:after=int(req.query.get('after','0'))
        except ValueError:raise web.HTTPBadRequest(text='after는 정수여야 합니다.')
        def read():
            return {'messages':chat.messages(after=after),'open_questions':chat.open_questions(),'closed':chat.is_closed()}
        return web.json_response(await asyncio.to_thread(read))
    async def chat_decide(req):
        import asis_chat
        aid=await auth(req,'interviewer');chat=chat_for(aid,req.match_info['revision']);body=await req.json()
        if not isinstance(body,dict):raise web.HTTPBadRequest(text='결정 본문은 JSON 객체여야 합니다.')
        role=deps['need_project'](req,aid,'interviewer')
        try:
            record=await asyncio.to_thread(chat.decide,req.match_info['question'],option_id=body.get('option_id'),
                free_text=body.get('free_text'),decided_by=req['user']['upn'],can_decide=role in ('manager','interviewer'))
        except asis_chat.ChatError as exc:
            code=str(exc)
            status=404 if code=='question_not_found' else 409 if code in ('question_already_decided','chat_closed') else 403 if code=='decision_not_permitted' else 400
            return web.Response(status=status,text=code)
        deps['audit'](req['user']['upn'],aid,None,'asis-chat-decision',req.match_info['revision']+':'+req.match_info['question'])
        return web.json_response(record)
    base='/api/aicaf/assessments/{aid}/asis'
    app.add_routes([web.get(base,current),web.post(base+'/start',start),web.get(base+'/history',history),
                    web.get(base+'/revisions/{revision}',job),web.get(base+'/revisions/{revision}/files/{kind}',file),
                    web.get(base+'/revisions/{revision}/chat',chat_state),
                    web.post(base+'/revisions/{revision}/chat/questions/{question}/decision',chat_decide)])
    return controller
