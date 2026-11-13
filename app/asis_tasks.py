"""현황분석의 필수 작업 원장과 단일 실행·진행률 관리자."""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime, timezone
import asyncio
import inspect
from pathlib import Path
import re
import hashlib
import threading
from contextvars import ContextVar


active_task_manager = ContextVar('asis_active_task_manager', default=None)

from asis_model import digest


def task(identifier, title, *, executor='code', depends_on=(), weight=1,
         scheduling='parallel', resource=None, group='analysis', pair_key=None):
    """코드가 생성하는 작업 규약이다. 모델은 작업 상태·완료율을 직접 쓰지 않는다."""
    return {'id': identifier, 'title': title, 'executor': executor,
            'depends_on': list(depends_on), 'weight': weight,
            'scheduling': scheduling, 'resource': resource, 'group': group,
            'pair_key': pair_key}


def initial_graph(areas):
    """항목 수 확정 전에도 분석·동결·장표·실렌더·최종 검증 전체 의존 그래프를 만든다."""
    rows = [task('interpret','고객 목표·요구 해석',executor='model',scheduling='prerequisite')]
    plan_units = ['issue-plan-area-'+key for key in areas]
    rows += [task('issue-plan-area-'+key,label+' 이슈 계획',executor='model',depends_on=['interpret'],pair_key='issue-plan-area-'+key)
             for key,label in areas.items()]
    rows += [task('issue-plan-area-'+key+'-review',label+' 계획 독립 검토',executor='model',depends_on=['interpret'])
             for key,label in areas.items()]
    rows += [task('issue-plan','분석 이슈 계획 통합·확정(영역 간 중복은 코드가 정리)',executor='model',depends_on=plan_units,scheduling='prerequisite')]
    rows += [task('area-'+key,label+' 작성·독립 검토',executor='model',depends_on=['issue-plan'],pair_key='area-'+key)
             for key,label in areas.items()]
    rows += [task('synthesis','시사점 종합 작성·검토',executor='model',depends_on=['area-'+key for key in areas]),
             task('action-item-plan','진단·과제별 계획',executor='model',depends_on=['synthesis'],scheduling='prerequisite'),
             task('actions','항목별 작성·검토 및 원장 병합',depends_on=['synthesis']),
             task('summary','최종 요약 작성·검토',executor='model',depends_on=['actions']),
             task('document','분석 전체 독립 검토(분할·통합)',executor='model',depends_on=['summary']),
             task('visual-specs','장표 시각 명세 작성(영역별 형식·핵심 글)',executor='model',depends_on=['document']),
             task('analysis-content','동일 분석 개정 확정',depends_on=['visual-specs']),
             task('workbook-freeze','Excel 생성·재계산·검증·동결',depends_on=['analysis-content'],scheduling='exclusive',resource='office',group='native'),
             task('native-presentation','동결 Excel 기반 승인 PPT 생성',depends_on=['workbook-freeze'],scheduling='exclusive',resource='native-package',group='native'),
             task('native_pair','동일 개정 Excel·PPT 생성 실행',depends_on=['analysis-content'],group='native'),
             task('actual_render','PPT 전장 실제 렌더',depends_on=['native-presentation'],scheduling='exclusive',resource='office',group='native'),
             task('visual_pages','전장·항목별 시각 검토 확정',depends_on=['actual_render','native_pair'],group='visual'),
             task('pair_consistency','최종 원장·산출물 대조',depends_on=['visual_pages'],scheduling='exclusive',resource='final-package',group='final')]
    return rows


class TaskManager:
    """작업을 먼저 저장하고 선행 조건·단독 자원·검증된 완료만으로 실행을 허용한다."""
    def __init__(self, folder, run, *, specs, progress=None, runtime=None):
        self.folder = Path(folder)
        self.run = deepcopy(run)
        self.runtime = runtime
        self.callback = progress or (lambda event: None)
        self._mutex = threading.RLock()
        self._events = {}
        self._locks = {}
        self._results = {}
        self._active = set()
        self.preserved_completed = set()
        try:self._loop = asyncio.get_running_loop()
        except RuntimeError:self._loop = None
        self.ledger = {'schema': 'asis-task-ledger/1', 'run': deepcopy(run),
                       'plan_version': 1, 'created_at': self.now(), 'tasks': {}, 'notifications': []}
        if (self.folder/'resume-request.json').is_file():
            from asis_resume import ResumeContext
            from asis_model import strict_json,digest
            resume=ResumeContext.active(self.folder)
            saved=strict_json((self.folder/'task-ledger.json').read_bytes())
            if saved.get('run')!=self.run:raise ValueError('resume_task_run_changed')
            for identifier,expected in resume.approval['completed_task_record_sha256'].items():
                if digest(saved.get('tasks',{}).get(identifier))!=expected:
                    raise ValueError('resume_completed_task_record_changed:'+identifier)
            self.ledger=saved
            self.preserved_completed=resume.preserved
        self.register(specs)

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat()

    def register(self, specs):
        """완료 사실과 가변 항목 수를 분리하고 전체 그래프를 원자적으로 검증·등록한다."""
        with self._mutex:
            proposed = deepcopy(self.ledger['tasks'])
            for spec in specs:
                row = deepcopy(spec)
                identifier = row.get('id')
                if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,220}', identifier):
                    raise ValueError('invalid_task_id')
                if identifier in proposed:
                    raise ValueError('duplicate_task_id:' + identifier)
                if row.get('executor') not in ('code', 'model') or row.get('scheduling') not in ('prerequisite', 'exclusive', 'parallel'):
                    raise ValueError('invalid_task_execution_contract')
                if not isinstance(row.get('title'), str) or not row['title'].strip():
                    raise ValueError('invalid_task_title')
                if type(row.get('weight')) not in (int, float) or row['weight'] <= 0:
                    raise ValueError('invalid_task_weight')
                if not isinstance(row.get('depends_on'), list) or len(row['depends_on']) != len(set(row['depends_on'])):
                    raise ValueError('invalid_task_dependencies')
                if row['scheduling'] == 'exclusive' and not row.get('resource'):
                    raise ValueError('exclusive_task_resource_required')
                row.update(status='pending', attempts=[], reports={}, created_at=self.now())
                proposed[identifier] = row
            visiting, visited = set(), set()
            def visit(identifier):
                if identifier in visited:
                    return
                if identifier in visiting:
                    raise ValueError('task_dependency_cycle')
                if identifier not in proposed:
                    raise ValueError('task_dependency_missing:' + identifier)
                visiting.add(identifier)
                for upstream in proposed[identifier]['depends_on']:
                    visit(upstream)
                visiting.remove(identifier)
                visited.add(identifier)
            for identifier in proposed:
                visit(identifier)
            # 검증용 복제는 저장하지 않는다. 실행 중 operation이 보유한 원장 행의 객체를 유지한다.
            self.ledger['tasks'].update({key: row for key, row in proposed.items()
                                        if key not in self.ledger['tasks']})
            self._persist()

    def _persist(self):
        from asis_view import atomic
        self.ledger['updated_at'] = self.now()
        atomic(self.folder/'task-ledger.json', self.ledger)
        self.callback({'unit': 'tasks', 'phase': 'tasks', 'status': 'updated',
                       'task_progress': self.progress()})

    def progress(self):
        """시간·심박·대기량은 완료 증거가 아니다. 등록 가중치와 완료 사실만 집계한다."""
        with self._mutex:
            all_rows = list(self.ledger['tasks'].values())
            rows = [row for row in all_rows if row['status'] != 'superseded']
            total = sum(row['weight'] for row in rows)
            done = sum(row['weight'] for row in rows if row['status'] == 'completed')
            waiting = [{'id':row['id'],'title':row['title'],'reason':'dependency_wait',
                        'dependencies':[key for key in row['depends_on'] if self.ledger['tasks'][key]['status']!='completed']}
                       for row in rows if row['id'] in self._active and row['status'] in ('pending','invalidated','waiting')
                       and any(self.ledger['tasks'][key]['status']!='completed' for key in row['depends_on'])]
            return {'schema': 'asis-task-progress/1', 'overall_percent': round(100*done/total, 2) if total else 0,
                    'completed_tasks': sum(row['status'] == 'completed' for row in rows),
                    'total_tasks': len(rows), 'running_tasks': [
                        {'id': row['id'], 'title': row['title'], 'executor': row['executor'],
                         'scheduling': row['scheduling']} for row in rows if row['status'] == 'running'],
                    'waiting_tasks':waiting,
                    'blocked_tasks': [row['id'] for row in rows if row['status'] in ('blocked', 'failed')],
                    'plan_version': self.ledger['plan_version'],
                    'superseded_tasks': len(all_rows)-len(rows),
                    # 포털 단계 표시(asis_stages)가 작업 키별 상태·가중치로 현재 단계를 계산한다. 본문·근거는 싣지 않는다.
                    'stage_ledger': {row['id']: {'status': row['status'], 'weight': row['weight']} for row in all_rows}}

    def bind_pair(self, identifier, *, author_session, reviewer_session, scope='content'):
        """같은 항목의 작성·검토 역할을 별도 대화에 고정한다."""
        if author_session == reviewer_session or scope not in ('content', 'native'):
            raise ValueError('independent_task_pair_required')
        pair = {'author': author_session, 'reviewer': reviewer_session, 'scope': scope}
        with self._mutex:
            row = self.ledger['tasks'][identifier]
            if row.get('pair') and row['pair'] != pair:
                raise ValueError('task_pair_binding_changed')
            row['pair'] = pair
            if not row.get('progress_steps'):
                # 기존 작업 가중치를 분할한다. 작성·검토·후보 확정의 완료를 중복 가산하지 않는다.
                original_weight = row['weight']
                step_ids = {role:identifier+'.'+role+'-verified' for role in ('author','reviewer')}
                specs = []
                for role,label in (('author','작성 후보 검증'),('reviewer','독립 검토 통과')):
                    spec = task(step_ids[role],row['title']+' · '+label,weight=original_weight/3,
                                depends_on=row['depends_on'],group=row['group'])
                    spec.update(parent_task_id=identifier,report_role=role)
                    specs.append(spec)
                self.register(specs)
                row['weight'] = original_weight/3
                row['progress_steps'] = step_ids
            self._persist()

    def begin_pair_candidate(self, identifier):
        """같은 담당 쌍의 새 후보 작성은 이전 세부 완료를 현재 진척으로 재사용하지 않는다."""
        with self._mutex:
            row = self.ledger['tasks'][identifier]
            for step_id in row.get('progress_steps',{}).values():
                step = self.ledger['tasks'][step_id]
                if step.get('completion'):
                    step.setdefault('attempts',[]).append({'completion':deepcopy(step['completion']),
                                                         'status':'invalidated','at':self.now()})
                step['status'] = 'pending'
                step.pop('completion',None)
            self._persist()

    def _report_progress_step(self, identifier, role, report):
        row = self.ledger['tasks'][identifier]
        step_id = row.get('progress_steps',{}).get(role)
        if not step_id:
            return
        valid = report['checks'].get('deterministic_passed') is True if role=='author' else (
            report['checks'].get('verdict')=='pass' and report['checks'].get('content_checked') is True
            and not report['checks'].get('defects') and not report['checks'].get('unresolved')
            and row['reports'].get('author',{}).get('candidate_sha256')==report['candidate_sha256']
            and (row['pair']['scope']!='native' or report['checks'].get('render_checked') is True))
        step = self.ledger['tasks'][step_id]
        step['status'] = 'completed' if valid else 'pending'
        if valid:
            step['completion'] = {'confirmed_by':'central_task_manager','candidate_sha256':report['candidate_sha256'],
                                  'evidence':deepcopy(report['evidence']),'confirmed_at':self.now()}
            self.ledger['notifications'].append({'event':'task_completed','task_id':step_id,
                                                 'confirmed_at':step['completion']['confirmed_at']})
        else:
            step.pop('completion',None)

    def report(self, identifier, role, *, session_id, candidate_sha256, evidence, checks, summary):
        """모델의 자기평가가 아니라 검증된 응답·원문 파일을 역할 완료 보고로 기록한다."""
        with self._mutex:
            row = self.ledger['tasks'][identifier]
            if role not in ('author', 'reviewer') or row.get('pair', {}).get(role) != session_id:
                raise ValueError('task_report_role_binding_failed')
            if not re.fullmatch(r'[0-9a-f]{64}', candidate_sha256) or not evidence:
                raise ValueError('task_report_evidence_required')
            files = []
            from asis_pipeline import history_path
            root = history_path(self.folder.resolve())
            for path in evidence:
                path = history_path(Path(path).resolve())
                relative = path.relative_to(root).as_posix()
                files.append({'path': relative, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
            if identifier in self.preserved_completed:
                if row.get('status')=='completed':
                    original=row.get('reports',{}).get(role)
                    if (not original
                            or original.get('candidate_sha256')!=candidate_sha256
                            or original.get('session_id')!=session_id or original.get('checks')!=checks):
                        raise ValueError('preserved_role_report_changed:'+identifier)
                    for original_file in original['evidence']:
                        path=history_path(root/original_file['path'])
                        if hashlib.sha256(path.read_bytes()).hexdigest()!=original_file['sha256']:
                            raise ValueError('preserved_role_report_evidence_changed:'+identifier)
                    return
            report = {'role': role, 'session_id': session_id, 'candidate_sha256': candidate_sha256,
                      'checks': deepcopy(checks), 'summary': summary, 'evidence': files,
                      'status': 'completed', 'reported_at': self.now()}
            row['reports'][role] = report
            self._report_progress_step(identifier,role,report)
            self.ledger['notifications'].append({'event':'role_reported', 'task_id':identifier,
                                                 'role':role, 'reported_at':report['reported_at']})
            self._persist()

    def report_output(self, identifier, *, input_sha256, result_sha256, result_path, evidence):
        """역할 쌍이 없는 계획·해석도 실제 입력·결과·보존 파일로 완료를 입증한다."""
        with self._mutex:
            row = self.ledger['tasks'][identifier]
            if row.get('pair') or row['executor'] != 'model':
                raise ValueError('unpaired_model_output_report_required')
            if any(not re.fullmatch(r'[0-9a-f]{64}', value) for value in (input_sha256,result_sha256)) or not evidence:
                raise ValueError('task_output_binding_required')
            from asis_pipeline import history_path
            root = history_path(self.folder.resolve())
            files = []
            for path in evidence:
                path = history_path(Path(path).resolve())
                files.append({'path':path.relative_to(root).as_posix(),
                              'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
            result_path = history_path(Path(result_path).resolve()).relative_to(root).as_posix()
            if result_path not in {file['path'] for file in files}:
                raise ValueError('task_output_result_evidence_missing')
            row['output_report'] = {'input_sha256':input_sha256,'result_sha256':result_sha256,
                                    'result_path':result_path,'evidence':files,'reported_at':self.now()}
            self._persist()

    def _confirm_output(self, identifier, value):
        row = self.ledger['tasks'][identifier]
        proof = row.get('output_report')
        if not proof or proof['result_sha256'] != digest(value):
            raise RuntimeError('task_completion_output_missing_or_mismatched:'+identifier)
        for file in proof['evidence']:
            if hashlib.sha256((self.folder/file['path']).read_bytes()).hexdigest() != file['sha256']:
                raise RuntimeError('task_completion_output_evidence_changed:'+identifier)
        from asis_model import strict_json
        if digest(strict_json((self.folder/proof['result_path']).read_bytes())) != proof['result_sha256']:
            raise RuntimeError('task_completion_output_result_changed:'+identifier)
        row['completion'] = {'confirmed_by':'central_task_manager','input_sha256':proof['input_sha256'],
                             'result_sha256':proof['result_sha256'],'confirmed_at':self.now()}
        self.ledger['notifications'].append({'event':'task_completed','task_id':identifier,
                                             'confirmed_at':row['completion']['confirmed_at']})

    def _confirm_pair(self, identifier):
        row = self.ledger['tasks'][identifier]
        reports = row['reports']
        required = set(row['pair'].get('required_reports',['author','reviewer']))
        if set(reports) != required:
            raise RuntimeError('task_completion_reports_missing:' + identifier)
        reviewer = reports['reviewer']
        candidate_hash = reviewer['candidate_sha256']
        if 'author' in reports and reports['author']['candidate_sha256'] != candidate_hash:
            raise RuntimeError('task_completion_candidate_mismatch:' + identifier)
        if 'author' in reports and reports['author']['checks'].get('deterministic_passed') is not True:
            raise RuntimeError('task_completion_deterministic_failed:' + identifier)
        checks = reviewer['checks']
        if (checks.get('verdict') != 'pass' or checks.get('content_checked') is not True
                or checks.get('defects') or checks.get('unresolved')):
            raise RuntimeError('task_completion_review_not_passed:' + identifier)
        if row['pair']['scope'] == 'native' and checks.get('render_checked') is not True:
            raise RuntimeError('task_completion_render_not_checked:' + identifier)
        for report in reports.values():
            for evidence in report['evidence']:
                path = self.folder/evidence['path']
                if hashlib.sha256(path.read_bytes()).hexdigest() != evidence['sha256']:
                    raise RuntimeError('task_completion_evidence_changed:' + identifier)
        row['completion'] = {'confirmed_by':'central_task_manager',
                             'candidate_sha256':candidate_hash, 'confirmed_at':self.now()}
        self.ledger['notifications'].append({'event':'task_completed', 'task_id':identifier,
                                             'confirmed_at':row['completion']['confirmed_at']})

    def bind_review(self, identifier, *, reviewer_session, scope='content'):
        """코드가 병합한 원본의 전체 검토에 존재하지 않는 작성자를 꾸며 넣지 않는다."""
        with self._mutex:
            row = self.ledger['tasks'][identifier]
            binding = {'reviewer':reviewer_session, 'scope':scope, 'required_reports':['reviewer']}
            if row.get('pair') and row['pair'] != binding:
                raise ValueError('task_pair_binding_changed')
            row['pair'] = binding
            self._persist()

    def supersede_review(self, identifier, replacements, *, reason):
        """유효한 하위 검토로 대체한 실패 단위만 퇴역시키고 원래 시도·판정 이력은 보존한다."""
        with self._mutex:
            row = self.ledger['tasks'][identifier]
            required = ['reviewer']
            if (identifier in self._active or row['status'] != 'failed'
                    or row.get('pair', {}).get('required_reports') != required
                    or not isinstance(reason, str) or not reason.strip()):
                raise ValueError('review_supersession_parent_invalid')
            if (not isinstance(replacements, list) or not replacements
                    or len(replacements) != len(set(replacements)) or identifier in replacements):
                raise ValueError('review_supersession_replacements_invalid')
            if any(identifier in other['depends_on'] and other['status'] != 'superseded'
                   for other in self.ledger['tasks'].values()):
                raise ValueError('review_supersession_has_dependents')
            for replacement in replacements:
                child = self.ledger['tasks'].get(replacement)
                if (child is None or replacement in self._active
                        or child['status'] not in ('completed', 'revision_required', 'blocked')
                        or child.get('pair', {}).get('required_reports') != required
                        or child['pair']['scope'] != row['pair']['scope']
                        or child.get('reports', {}).get('reviewer', {}).get('checks', {}).get('content_checked') is not True):
                    raise ValueError('review_supersession_child_not_reviewed')
            row['status'] = 'superseded'
            row['supersession'] = {'reason': reason, 'replacements': list(replacements),
                                   'confirmed_at': self.now()}
            self.ledger['notifications'].append({'event': 'task_superseded', 'task_id': identifier,
                                                **deepcopy(row['supersession'])})
            self._persist()

    def diagnose(self, identifier, *, runtime=None, reason=None):
        """미완료 원장과 담당 역할의 실제 대기·검토 결과를 대조한다. 시간만으로 실패를 만들지 않는다."""
        runtime = runtime or self.runtime
        with self._mutex:
            row = self.ledger['tasks'][identifier]
            dependencies = [key for key in row['depends_on']
                            if self.ledger['tasks'][key]['status'] != 'completed']
            roles = {role: {'session_id':session, 'report':deepcopy(row['reports'].get(role)),
                           'activity':deepcopy(getattr(runtime,'activity',{}).get(session,{}))}
                     for role, session in row.get('pair',{}).items() if role in ('author','reviewer')}
            review = row['reports'].get('reviewer',{}).get('checks',{})
            if reason is None:
                if dependencies: reason = 'dependency_wait'
                elif any(role['activity'].get('provider_backpressure') is True for role in roles.values()): reason = 'provider_backpressure'
                elif review.get('verdict') == 'revise': reason = 'review_revision_required'
                elif review.get('verdict') == 'blocked': reason = 'review_blocked'
                elif any(role['activity'].get('state') == 'resource_wait' for role in roles.values()): reason = 'resource_wait'
                elif any(role['activity'].get('state') == 'running' for role in roles.values()): reason = 'model_running'
                elif row['status'] == 'failed': reason = 'execution_failed'
                elif not row['reports'].get('author'): reason = 'author_report_pending'
                else: reason = 'review_report_pending'
            diagnosis = {'task_id':identifier,'reason':reason,'roles':roles,
                         'dependencies':dependencies,'status':row['status'],'checked_at':self.now()}
            row.setdefault('diagnoses',[]).append(diagnosis)
            self._persist()
            return diagnosis

    def resolve(self, identifier, *, role, action, candidate=None, defects=(), reason=None, runtime=None):
        """근거 있는 조치를 같은 담당 쌍에 보낸다. 실행 오류·근거 부족은 무조건 재작성하지 않는다."""
        with self._mutex:
            row = self.ledger['tasks'][identifier]
            diagnosis = self.diagnose(identifier,runtime=runtime,reason=reason)
            session = row.get('pair',{}).get(role)
            if not session:
                raise ValueError('task_resolution_owner_missing')
            instruction = {'task_id':identifier, 'target_role':role, 'target_session_id':session,
                'action':action,'candidate_sha256':digest(candidate) if candidate is not None else None,
                'defects':deepcopy(list(defects)), 'reason':diagnosis['reason'], 'status':'delivered',
                'created_at':self.now(),'preserve_prior_candidate':True,'preserve_pair_history':True,
                'automatic_retry_allowed':False}
            instruction['id'] = digest({k:v for k,v in instruction.items() if k not in ('created_at','status')})
            previous = next((v for v in row.setdefault('resolutions',[]) if v['id']==instruction['id']),None)
            if previous is not None:
                return deepcopy(previous)
            row['resolutions'].append(instruction)
            self.ledger['notifications'].append({'event':'resolution_delivered','task_id':identifier,
                                                'resolution_id':instruction['id'],'role':role})
            self._persist()
            return deepcopy(instruction)

    def acknowledge(self, identifier, resolution_id, session_id):
        """기존 담당 대화에 지시를 실제 투입한 경우만 소비 확인을 기록한다."""
        with self._mutex:
            instruction = next(v for v in self.ledger['tasks'][identifier]['resolutions'] if v['id']==resolution_id)
            if instruction['target_session_id'] != session_id:
                raise ValueError('task_resolution_ack_owner_mismatch')
            instruction.update(status='acknowledged', acknowledged_at=self.now())
            self._persist()

    def invalidate(self, identifiers, *, reason, downstream=True):
        """재작업의 실제 후속 승인만 무효화하고 완료 이력·독립 형제는 보존한다."""
        with self._mutex:
            affected=set(identifiers)
            if affected-set(self.ledger['tasks']):
                raise ValueError('task_invalidation_target_missing')
            affected |= {key for key,row in self.ledger['tasks'].items() if row.get('parent_task_id') in affected}
            if downstream:
                while True:
                    following=affected|{key for key,row in self.ledger['tasks'].items() if affected.intersection(row['depends_on'])}
                    if following==affected:break
                    affected=following
            if affected.intersection(self._active):
                raise RuntimeError('task_invalidation_active_owner')
            for identifier in affected:
                row=self.ledger['tasks'][identifier]
                row.setdefault('invalidations',[]).append({'reason':reason,'prior_status':row['status'],
                    'completion':deepcopy(row.get('completion')),'at':self.now()})
                row['status']='invalidated'
                row.pop('completion',None)
                self._results.pop(identifier,None)
                self.preserved_completed.discard(identifier)
                self._events.setdefault(identifier,asyncio.Event()).clear()
            self.ledger['notifications'].append({'event':'tasks_invalidated','task_ids':sorted(affected),
                                                 'reason':reason,'at':self.now()})
            self._persist()
            return sorted(affected)

    def parallel_sync(self, operations):
        """독립 네이티브 작업은 실행 중인 Runtime의 자원 진입·중앙 원장을 함께 경유한다."""
        for key in operations:
            row=self.ledger['tasks'][key]
            if row['executor']!='code' or row['scheduling']!='parallel':
                raise ValueError('parallel_native_task_contract_required:'+key)
        async def group():
            gate=getattr(self.runtime,'gate',None)
            async def one(key,operation):
                async def run():return await asyncio.to_thread(operation)
                if gate is None:return await self.perform(key,run)
                async with gate.slot('native'):
                    return await self.perform(key,run)
            values=await asyncio.gather(*(one(key,op) for key,op in operations.items()),return_exceptions=True)
            failure=next((v for v in values if isinstance(v,BaseException)),None)
            if failure is not None:raise failure
            return dict(zip(operations,values))
        loop=getattr(self,'_loop',None)
        try:running=asyncio.get_running_loop()
        except RuntimeError:running=None
        if running is not None:
            raise RuntimeError('parallel_sync_cannot_block_event_loop')
        if loop is not None and loop.is_running():
            return asyncio.run_coroutine_threadsafe(group(),loop).result(timeout=2100)
        return asyncio.run(group())

    def perform_sync(self, identifier, operation):
        """네이티브 작업 스레드도 중앙에서 실제 실행·선행 조건·단독 소유·완료를 확인한다."""
        with self._mutex:
            row = self.ledger['tasks'][identifier]
            if row['executor'] != 'code':
                raise ValueError('sync_task_requires_code')
            if identifier in self._active:
                raise RuntimeError('task_already_running:' + identifier)
            for upstream in row['depends_on']:
                if self.ledger['tasks'][upstream]['status'] != 'completed':
                    raise RuntimeError('task_dependency_not_completed:' + upstream)
            self._active.add(identifier)
            resource = row.get('resource')
            locks = getattr(self, '_sync_locks', None)
            if locks is None:
                self._sync_locks = locks = {}
            lock = locks.setdefault(resource, threading.RLock()) if resource else threading.RLock()
        try:
            with lock:
                with self._mutex:
                    row['status'] = 'running'
                    attempt = {'started_at':self.now(), 'status':'running'}
                    row['attempts'].append(attempt)
                    row.pop('completion',None)
                    self._persist()
                value = operation()
                if inspect.isawaitable(value):
                    raise ValueError('sync_task_cannot_return_coroutine')
                with self._mutex:
                    row['status'] = 'completed'
                    attempt.update(status='completed',completed_at=self.now())
                    row['completion'] = {'confirmed_by':'central_task_manager','executor':'code',
                                         'confirmed_at':self.now()}
                    self.ledger['notifications'].append({'event':'task_completed','task_id':identifier,
                                                         'confirmed_at':row['completion']['confirmed_at']})
                    self._results[identifier] = value
                    self._persist()
                return value
        except BaseException as exc:
            with self._mutex:
                row['status'] = 'failed'
                if row['attempts'] and row['attempts'][-1]['status']=='running':
                    row['attempts'][-1].update(status='failed',completed_at=self.now(),error_type=type(exc).__name__)
                self.diagnose(identifier)
                self._persist()
            raise
        finally:
            with self._mutex:
                self._active.discard(identifier)

    async def perform(self, identifier, operation, *, allow_review_revision=False):
        """모든 작업은 이 입구에서 의존 완료를 확인한 후 실제 구현을 실행한다."""
        if identifier not in self.ledger['tasks']:
            raise ValueError('unregistered_task:' + identifier)
        if identifier in self._active:
            raise RuntimeError('task_already_running:' + identifier)
        self._active.add(identifier)
        done = self._events.setdefault(identifier, asyncio.Event())
        row = self.ledger['tasks'][identifier]
        if identifier in getattr(self,'preserved_completed',set()) and row['status']=='completed':
            try:
                value=operation()
                if inspect.isawaitable(value):value=await value
                self._results[identifier]=value
                return value
            finally:
                self._active.discard(identifier)
                done.set()
        done.clear()
        if row.get('reports') or row.get('completion'):
            row.setdefault('report_history',[]).append({'reports':deepcopy(row['reports']),
                                                        'completion':row.get('completion')})
        row['reports'] = {}
        row.pop('completion',None)
        if row.get('progress_steps'):
            self.begin_pair_candidate(identifier)
        try:
            for upstream in row['depends_on']:
                event = self._events.setdefault(upstream, asyncio.Event())
                if self.ledger['tasks'][upstream]['status'] != 'completed':
                    await event.wait()
                if self.ledger['tasks'][upstream]['status'] != 'completed':
                    raise RuntimeError('task_dependency_not_completed:' + upstream)
            resource = row.get('resource')
            lock = self._locks.setdefault(resource, asyncio.Lock()) if resource else asyncio.Lock()
            async with lock:
                row['status'] = 'running'
                attempt = {'started_at': self.now(), 'status': 'running'}
                row['attempts'].append(attempt)
                self._persist()
                value = operation()
                if inspect.isawaitable(value):
                    value = await value
                if row.get('pair'):
                    review = row['reports'].get('reviewer',{}).get('checks',{})
                    if allow_review_revision and review.get('verdict') in ('revise','blocked'):
                        row['status'] = 'revision_required' if review['verdict']=='revise' else 'blocked'
                        attempt.update(status=row['status'],completed_at=self.now())
                        self.diagnose(identifier)
                        self._persist()
                        return value
                    self._confirm_pair(identifier)
                elif row['executor'] == 'model':
                    self._confirm_output(identifier,value)
                row['status'] = 'completed'
                attempt.update(status='completed', completed_at=self.now())
                self._results[identifier] = value
                self._persist()
                return value
        except BaseException as exc:
            row['status'] = 'blocked' if str(exc).startswith('task_dependency_') else 'failed'
            if row['attempts'] and row['attempts'][-1]['status'] == 'running':
                row['attempts'][-1].update(status=row['status'], completed_at=self.now(), error_type=type(exc).__name__)
            diagnosis = self.diagnose(identifier)
            failed_role = next((role for role,value in diagnosis['roles'].items() if value['activity'].get('state')=='failed'),None)
            if failed_role:
                action = 'wait_provider_capacity' if diagnosis['reason']=='provider_backpressure' else 'inspect_execution_error'
                self.resolve(identifier,role=failed_role,action=action,reason=diagnosis['reason'])
            self._persist()
            raise
        finally:
            self._active.discard(identifier)
            done.set()
