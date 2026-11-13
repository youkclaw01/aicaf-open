"""공통 업무 순서·세 보고서·질문 근거의 계약을 실제 서버 함수와 대조한다."""
from __future__ import annotations

import ast
import asyncio
from contextlib import contextmanager
import importlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from aiohttp import web
import pytest

APP = Path(__file__).resolve().parents[1]
ORDER = ['current_state', 'interview', 'integrated']
NAMES = ['AS-IS 현황분석 보고서', '인터뷰 보고서', '현황분석 종합 및 시사점 및 개선과제 보고서']


def test_actual_assessment_api_exposes_the_same_report_order(tmp_path):
    """기존 API의 실제 본문을 실행한다. 프런트 문구만 바뀌는 수정은 통과하지 못한다."""
    path = tmp_path/'assessment.sqlite'
    with sqlite3.connect(path) as conn:
        conn.executescript('create table assessments(id integer,track text);'
                           'create table consents(assessment_id integer);'
                           'create table answers(assessment_id integer,qid text);'
                           "insert into assessments values(7,'new');")

    @contextmanager
    def db():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    source = (APP/'server.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'api_get')
    state = {'schema': 'assessment-workflow/1', 'report_order': ORDER,
             'reports': [{'kind': k, 'name': v} for k, v in zip(ORDER, NAMES)],
             'current_step': 'current_state'}
    namespace = {'db': db, 'need_project': lambda req, aid: 'manager', 'web': web,
                 'scoring': SimpleNamespace(compute=lambda qs, ans: {}), '_QCACHE': {'new': []},
                 'workflow_state': lambda aid: state}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(APP/'server.py'), 'exec'), namespace)
    request = SimpleNamespace(match_info={'aid': '7'})
    response = asyncio.run(namespace['api_get'](request))
    payload = json.loads(response.body)
    assert 'workflow' in payload, 'assessment API has no shared workflow contract'
    assert payload['workflow']['report_order'] == ORDER
    assert [r['name'] for r in payload['workflow']['reports']] == NAMES


def module():
    return importlib.import_module('assessment_workflow')


def assessment(aid=19, track='new'):
    return {'id': aid, 'customer': '공통 계약 합성 시험', 'track': track,
            'infra_case_id': 'unit-case-'+str(aid)}


@pytest.mark.parametrize('aid,track', [(19, 'new'), (83, 'existing')])
def test_new_projects_start_with_current_state_not_interview(aid, track):
    state = module().describe(assessment(aid, track), source_sha256='a'*64,
                              current_state=None, questionnaire=None, interview=None,
                              integrated=None, answers={})
    assert state['report_order'] == ORDER
    assert state['current_step'] == 'current_state'
    assert state['reports'][0]['can_generate'] is True
    assert state['reports'][1]['can_generate'] is False
    assert state['reports'][2]['can_generate'] is False


def test_as_is_source_is_independent_of_customer_answers(tmp_path):
    from asis_model import normalize
    source = tmp_path/'Source'; source.mkdir()
    (source/'topology.json').write_text(json.dumps({'resources': [], 'configurations': {},
        'verification': {'resourceCount': {'complete': True, 'collected': 0, 'uniqueIds': 0}}}), encoding='utf-8')
    first = normalize(source, assessment(), [], 'current-state')
    second = normalize(source, assessment(), [{'qid': 'A1-1', 'transcript': '고객 원문'}], 'interview')
    clean = module().current_state_input(second, assessment=assessment())
    assert not any(f['id'].startswith('F_IV_') for f in clean['facts'])
    assert clean['run']['source_bundle_sha256'] == first['run']['source_bundle_sha256']
    assert second['run']['source_bundle_sha256'] != first['run']['source_bundle_sha256']


def test_failed_or_unrendered_current_state_does_not_unlock_interview():
    for result in ({'status': 'failed'}, {'status': 'generated_candidate', 'content_checked': True,
                                         'render_checked': False}):
        state = module().describe(assessment(), source_sha256='a'*64, current_state=result,
                                  questionnaire=None, interview=None, integrated=None, answers={})
        assert not state['reports'][1]['can_generate']
        assert state['current_step'] == 'current_state'


def test_guideline_questions_are_derived_and_bound_to_actual_source():
    source = {'run': {'assessment_id': 19, 'source_bundle_sha256': 'a'*64, 'track': 'new'},
              'facts': [{'id': 'F_T_foundryAccountConfiguration', 'value': [{'name': 'unit-model',
                 'disableLocalAuth': True, 'publicNetworkAccess': 'Disabled'}],
                 'collection_status': 'collected', 'scope_complete': True}],
              'context': {'dataset_register': []}}
    catalog = module().derive_questions(source, {'report_id': 'state-19', 'sha256': 'b'*64})
    assert catalog['assessment_id'] == 19
    assert catalog['current_state_report']['sha256'] == 'b'*64
    assert catalog['questions']
    assert {q['area'] for q in catalog['questions']} == set('ABCD')
    for q in catalog['questions']:
        assert q['guidelines'], q['id']
        assert q['derivation_reason'] and q['evaluation_use'], q['id']
        assert q['guidelines'][0]['quote'] and q['guidelines'][0]['source_url'].startswith('https://learn.microsoft.com/')
        assert q['origin_id'] and q['id'].startswith('WF_')
    specific = [q for q in catalog['questions'] if 'F_T_foundryAccountConfiguration' in q['fact_refs']]
    assert specific and any(q['followups'] for q in specific)


def test_question_derivation_does_not_mutate_existing_bank():
    import questions
    before = questions.questions_for('existing')
    source = {'run': {'assessment_id': 83, 'source_bundle_sha256': 'c'*64, 'track': 'existing'},
              'facts': [], 'context': {}}
    module().derive_questions(source, {'report_id': 'state-83', 'sha256': 'd'*64})
    assert questions.questions_for('existing') == before


def test_same_question_meaning_reuses_id_but_changed_evidence_changes_only_related_questions():
    first = {'run': {'assessment_id': 19, 'source_bundle_sha256': 'a'*64, 'track': 'new'},
             'facts': [{'id': 'F_T_foundryAccountConfiguration', 'value': [{'disableLocalAuth': True}],
                        'collection_status': 'collected', 'scope_complete': True}], 'context': {}}
    from copy import deepcopy
    second = deepcopy(first)
    second['facts'][0]['value'][0]['disableLocalAuth'] = False
    second['run']['source_bundle_sha256'] = 'c'*64
    a = module().derive_questions(first, {'report_id': 'first', 'sha256': 'b'*64})
    b = module().derive_questions(second, {'report_id': 'second', 'sha256': 'd'*64})
    a = {q['origin_id']: q for q in a['questions']}
    b = {q['origin_id']: q for q in b['questions']}
    for origin in a:
        related = 'F_T_foundryAccountConfiguration' in a[origin]['fact_refs']
        assert (a[origin]['id'] != b[origin]['id']) is related, origin
