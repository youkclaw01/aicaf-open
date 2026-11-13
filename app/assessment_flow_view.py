"""세 보고서의 공통 저장소·선행 조건·실제 파일 검증·프로젝트 API를 연결한다."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
from pathlib import Path
import urllib.parse

from aiohttp import web

from asis_model import digest, normalize
from asis_view import Controller, MIME, atomic, read
import assessment_workflow as contract


class Flow:
    def __init__(self, deps, integrated):
        self.deps = deps
        self.integrated = integrated
        args = {key: deps[key] for key in ('ROOT', 'assessment', 'infra_case_dir', 'db')}
        self.current_state = Controller(**args, report_kind='current_state')
        self.interview = Controller(**args, report_kind='interview')
        self.controllers = {'current_state': self.current_state, 'interview': self.interview, 'integrated': integrated}
        self.interview.workflow_provider = lambda aid, data: self.prepare(aid, data, 'interview')
        self.interview.workflow_guard = lambda aid: self.require(aid, 'interview')
        integrated.workflow_provider = lambda aid, data: self.prepare(aid, data, 'integrated')
        integrated.workflow_guard = lambda aid: self.require(aid, 'integrated')

    def answers(self, aid):
        with self.deps['db']() as conn:
            return {row['qid']: dict(row) for row in conn.execute(
                'select qid,transcript,summary,score from answers where assessment_id=? order by qid', (aid,))}

    def _completed_overview(self, folder, kind, manifest):
        try:
            analysis = read(folder/'analysis.json') or {}
            stages = analysis.get('stages') or []
            if not stages:
                return None
            import asis_analysis
            area_names = asis_analysis.AREA_NAMES
            st0 = stages[0] if len(stages) > 0 else {}
            areas = [{'area_id': a.get('area_id', ''), 'name': area_names.get(a.get('area_id', ''), a.get('area_id', '')),
                      'headline': a.get('headline', ''), 'observations': len(a.get('observations') or [])}
                     for a in (st0.get('areas') or [])]
            out = {'pages': manifest.get('render', {}).get('page_count'),
                   'reviewed_pages': manifest.get('quality_review', {}).get('reviewed_pages'),
                   'areas': areas,
                   'unresolved_count': len(st0.get('unresolved') or [])}
            if kind == 'integrated' and len(stages) >= 4:
                st1, st2, st3 = stages[1], stages[2], stages[3]
                summary = st3.get('summary') or {}
                out.update({
                    'headline': summary.get('headline', ''),
                    'message': summary.get('message', ''),
                    'decision_points': list(summary.get('decision_points') or []),
                    'direction_count': len(st1.get('directions') or []),
                    'diagnosis_count': len(st2.get('diagnoses') or []),
                    'action_count': len(st2.get('actions') or []),
                    'directions': [{'id': d.get('id', ''), 'title': d.get('title', '')} for d in (st1.get('directions') or [])],
                    'actions': [{'id': a.get('id', ''), 'title': a.get('title', ''), 'target': a.get('target', ''),
                                 'priority_level': a.get('priority_level', ''), 'deliverable': a.get('deliverable', '')}
                                for a in (st2.get('actions') or [])],
                })
            return out
        except Exception:
            return None

    def record(self, aid, kind):
        controller = self.controllers[kind]
        state = controller.state(aid)
        latest = controller.job(aid, state['latest_revision_id']) if state.get('latest_revision_id') else None
        revision = state.get('candidate_revision_id')
        if not revision:
            return ({'kind': kind, 'assessment_id': aid, 'status': latest['status'], 'job': latest,
                     'revision_id': latest['revision_id']} if latest else None)
        job = controller.job(aid, revision)
        folder = controller.job_dir(aid, revision)
        raw = (folder/'artifact-manifest.json').read_bytes()
        if hashlib.sha256(raw).hexdigest() != job.get('artifact_manifest_sha256'):
            return {'kind': kind, 'assessment_id': aid, 'status': 'invalid', 'error': 'artifact_manifest_changed'}
        manifest = read(folder/'artifact-manifest.json')
        quality = manifest.get('quality_review', {})
        if (job['status'] != 'generated_candidate' or manifest.get('report_kind') != kind
                or manifest.get('run', {}).get('assessment_id') != aid
                or manifest.get('quality_status') != 'automated_review_passed'
                or quality.get('content_review_passed') is not True
                or quality.get('all_render_pages_reviewed') is not True
                or quality.get('reviewed_pages') != manifest.get('render', {}).get('page_count')):
            return {'kind': kind, 'assessment_id': aid, 'status': 'invalid', 'job': latest or job}
        for file_kind in ('pptx', 'xlsx', 'pdf'):
            controller.artifact(aid, revision, file_kind)
        linkage = manifest.get('workflow', {})
        source_sha = (manifest['run']['source_bundle_sha256'] if kind == 'current_state'
                      else linkage.get('source_sha256'))
        return {'kind': kind, 'assessment_id': aid, 'status': 'ready', 'revision_id': revision,
                'source_sha256': source_sha, 'sha256': job['artifact_manifest_sha256'],
                'content_checked': True, 'render_checked': True, 'files_verified': True,
                'questionnaire_sha256': linkage.get('questionnaire_sha256'),
                'answers_sha256': linkage.get('answers_sha256'), 'upstream': linkage.get('upstream', {}),
                'pages': manifest['render']['page_count'], 'overview': self._completed_overview(folder, kind, manifest),
                'job': latest or job}

    def questionnaire(self, aid, record):
        if not record or record.get('status') != 'ready':return None
        folder = self.current_state.job_dir(aid, record['revision_id'])
        value = read(folder/'questionnaire.json')
        job = self.current_state.job(aid, record['revision_id'])
        if value is None:return None
        if digest(value) != job.get('questionnaire_sha256'):
            raise web.HTTPConflict(text='질문 원장의 해시가 승인 기록과 다릅니다. 기존 파일을 보존하고 복구해야 합니다.')
        return value

    def state(self, aid):
        assessment = self.deps['assessment'](aid)
        source = None;source_error = None
        try:
            candidate = self.current_state.collect(aid)
            if candidate['context']['input_files']:source = candidate
        except web.HTTPConflict as exc:
            source_error = exc.text
        except (ValueError, OSError) as exc:
            source_error = type(exc).__name__+':'+str(exc)[:300]
        records = {}
        for kind in contract.REPORT_ORDER:
            try:records[kind] = self.record(aid, kind)
            except (OSError, ValueError, KeyError, web.HTTPException) as exc:
                records[kind] = {'kind': kind, 'assessment_id': aid, 'status': 'invalid',
                                 'error': type(exc).__name__+':'+str(exc)[:250]}
        questionnaire = self.questionnaire(aid, records['current_state'])
        answers = self.answers(aid)
        value = contract.describe(assessment, source_sha256=source['run']['source_bundle_sha256'] if source else None,
             current_state=records['current_state'], questionnaire=questionnaire,
             interview=records['interview'], integrated=records['integrated'], answers=answers)
        value['source_error'] = source_error
        value['runtime_ready'] = self.current_state.runtime_ready()
        value['source_files'] = source['context']['input_files'] if source else []
        value['questionnaire_sha256'] = digest(questionnaire) if questionnaire else None
        for report in value['reports']:
            report['can_generate'] = report['can_generate'] and value['runtime_ready']
            if not value['runtime_ready']:
                report['blocked_reason'] = '공통 생성 환경의 현재 코드·모델·렌더 검증을 먼저 완료해야 합니다.'
        if value['questionnaire_ready']:
            value['_questions'] = questionnaire['questions']
        return value

    def questions(self, aid, *, required=False):
        state = self.state(aid)
        if not state['questionnaire_ready']:
            if required:raise web.HTTPConflict(text='AS-IS 현황분석 보고서와 공식 지침 기반 질문 구성을 먼저 완료해야 합니다.')
            return None
        return state['_questions']

    def require(self, aid, kind):
        if kind not in self.controllers:raise web.HTTPNotFound()
        state = self.state(aid)
        index = contract.REPORT_ORDER.index(kind)
        if not state['reports'][index]['prerequisites_ready'] or not state['runtime_ready']:
            raise web.HTTPConflict(text=state['reports'][index]['blocked_reason'] or '선행 보고서의 완료 조건을 확인해야 합니다.')
        return state

    def prepare(self, aid, original, kind):
        state = self.require(aid, kind)
        assessment = self.deps['assessment'](aid)
        current = state['reports'][0]['record']
        q = self.questionnaire(aid, current)
        if not q:raise web.HTTPConflict(text='현재 현황분석에 결합된 질문 원장이 없습니다.')
        answers = self.answers(aid)
        missing = contract.unanswered(q['questions'], answers)
        if missing:raise web.HTTPConflict(text=f'실제 인터뷰 답변이 필요한 문항이 {len(missing)}개 남아 있습니다.')
        selected = [answers[row['id']] for row in q['questions']]
        first_folder = self.current_state.job_dir(aid, current['revision_id'])
        first_analysis = read(first_folder/'analysis.json')
        source = (self.current_state.source_folder(assessment) if self.current_state.source_folder else
                  self.deps['infra_case_dir'](assessment['infra_case_id'])/'Source')
        input_source = normalize(source, assessment, [], original['run']['analysis_revision'])
        if input_source['run']['source_bundle_sha256'] != state['source_sha256']:
            raise web.HTTPConflict(text='원천이 변경되었습니다. 현황분석 보고서와 같은 개정을 다시 확인해야 합니다.')
        upstream = {'current_state': current['sha256']}
        if kind == 'integrated':
            second = state['reports'][1]['record']
            second_analysis = read(self.interview.job_dir(aid, second['revision_id'])/'analysis.json')
            snapshot = second_analysis['input']['context']['interview_snapshot']
            if (digest(snapshot['questionnaire']) != digest(q)
                    or contract.answer_binding(q['questions'], snapshot['answers']) != contract.answer_binding(q['questions'], answers)):
                raise web.HTTPConflict(text='인터뷰 보고서와 현재 질문·답변이 일치하지 않습니다.')
            selected = [snapshot['answers'][row['id']] for row in q['questions']]
            upstream['interview'] = second['sha256']
        data = normalize(source, assessment, selected, original['run']['analysis_revision'],
                         original['context'].get('requirements', ''))
        linkage = {'schema': contract.SCHEMA, 'assessment_id': aid, 'source_sha256': state['source_sha256'],
                   'questionnaire_sha256': digest(q), 'answers_sha256': contract.answer_binding(q['questions'], answers),
                   'upstream': upstream}
        old_run = deepcopy(data['run'])
        source_hash = digest({'source_with_answers': old_run['source_bundle_sha256'], 'workflow': linkage})
        data['run'].update(source_revision=source_hash, source_bundle_sha256=source_hash)
        new_run = deepcopy(data['run'])
        def rebind(value):
            if isinstance(value, dict):
                if value.get('run') == old_run:value['run'] = deepcopy(new_run)
                for child in value.values():rebind(child)
            elif isinstance(value, list):
                for child in value:rebind(child)
        rebind(data)
        data['context']['report_kind'] = kind
        data['context']['assessment_metadata'] = deepcopy(assessment)
        data['context']['assessment_workflow'] = linkage
        data['context']['interview_snapshot'] = {'questionnaire': q, 'answers': {r['qid']: r for r in selected}}
        data['context']['prior_current_state_analysis'] = {'run': first_analysis['input']['run'],
                 'areas': first_analysis['stages'][0]['areas'], 'unresolved': first_analysis['stages'][0]['unresolved']}
        return data

    def regenerate_questions(self, aid):
        state = self.state(aid)
        current = state['reports'][0]['record']
        if not current or current.get('status') != 'ready':raise web.HTTPConflict(text='현황분석 보고서가 먼저 필요합니다.')
        folder = self.current_state.job_dir(aid, current['revision_id'])
        result = read(folder/'analysis.json')
        q = contract.derive_questions(result['input'], {'revision_id': current['revision_id'], 'sha256': current['sha256']}, current_analysis=result)
        with self.current_state.mutation(aid):
            path = folder/'questionnaire.json'
            previous = read(path)
            if previous is not None:atomic(folder/'questionnaire-history'/(digest(previous)+'.json'), previous)
            atomic(path, q)
            job = self.current_state.job(aid, current['revision_id'])
            job['questionnaire_sha256'] = digest(q);job.pop('questionnaire_error', None)
            atomic(folder/'job.json', job)
        return {'questionnaire_sha256': digest(q), 'questions': len(q['questions'])}


def register(app, deps, integrated):
    flow = Flow(deps, integrated);tasks = set()
    def auth(req, edit=False):
        try:aid = int(req.match_info['aid'])
        except (ValueError, TypeError):raise web.HTTPNotFound()
        deps['need_project'](req, aid, 'interviewer' if edit else 'viewer')
        return aid
    async def state(req):
        aid = auth(req);value = await asyncio.to_thread(flow.state, aid)
        value.pop('_questions', None)
        value['can_edit'] = deps['need_project'](req, aid) in ('manager', 'interviewer')
        return web.json_response(value)
    async def input_state(req):
        aid = auth(req);kind = req.match_info['kind'];flow.require(aid, kind)
        return web.json_response(await asyncio.to_thread(flow.controllers[kind].current, aid))
    async def start(req):
        aid = auth(req, True);kind = req.match_info['kind'];flow.require(aid, kind)
        body = await req.json();controller = flow.controllers[kind]
        row = await asyncio.to_thread(controller.create, aid, body)
        if row['status'] == 'queued':
            task = asyncio.create_task(asyncio.to_thread(controller.launch, aid, row['revision_id']))
            tasks.add(task);task.add_done_callback(tasks.discard)
        deps['audit'](req['user']['upn'], aid, None, 'workflow-start-'+kind, row['revision_id'])
        return web.json_response(row, status=202)
    async def questions(req):
        aid = auth(req, True)
        return web.json_response(await asyncio.to_thread(flow.regenerate_questions, aid))
    async def file(req):
        aid = auth(req);kind = req.match_info['kind'];file_kind = req.match_info['file_kind']
        if kind not in flow.controllers or file_kind not in ('pptx', 'xlsx', 'pdf'):raise web.HTTPNotFound()
        revision = req.match_info['revision']
        raw, found = await asyncio.to_thread(flow.controllers[kind].artifact, aid, revision, file_kind)
        name = dict(contract.REPORTS)[kind]+f'_{aid}_{revision}.{file_kind}'
        deps['audit'](req['user']['upn'], aid, None, 'workflow-file-'+kind, revision+':'+file_kind)
        return web.Response(body=raw, content_type=MIME[file_kind], headers={'Cache-Control': 'no-store',
            'X-Content-SHA256': found['sha256'], 'X-Content-Type-Options': 'nosniff',
            'Content-Disposition': "attachment; filename*=UTF-8''"+urllib.parse.quote(name)})
    base = '/api/aicaf/assessments/{aid}/workflow'
    app.add_routes([web.get(base, state), web.get(base+'/reports/{kind}', input_state),
                    web.post(base+'/reports/{kind}/start', start), web.post(base+'/questions', questions),
                    web.get(base+'/reports/{kind}/revisions/{revision}/files/{file_kind}', file)])
    return flow
