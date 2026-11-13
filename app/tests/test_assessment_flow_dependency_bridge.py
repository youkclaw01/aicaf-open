"""두 선행 보고서에서 종합 입력을 만드는 실제 경로를 격리된 합성 단위 자료로 검증한다."""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path

from aiohttp import web
import pytest

import asis_analysis
import asis_model
import asis_view
import assessment_workflow as contract
from assessment_flow_view import Flow

AID = 719997
FIRST = 'r-20000101T000000-0000000000000001'
SECOND = 'r-20000101T000000-0000000000000002'
THIRD = 'r-20000101T000000-0000000000000003'


@pytest.fixture
def bridge(tmp_path):
    assessment = {'id':AID, 'customer':'합성 단위 연결 시험', 'title':'운영 고객 자료 아님',
                  'interviewer':'unit', 'track':'new', 'infra_case_id':'unit-case'}
    case = tmp_path/'cases/unit-case'
    source = case/'Source';source.mkdir(parents=True)
    resource = {'id':'/subscriptions/unit/resourceGroups/unit/providers/Microsoft.Storage/storageAccounts/unitstore',
                'name':'unitstore', 'type':'Microsoft.Storage/storageAccounts', 'location':'koreacentral'}
    (source/'topology.json').write_text(json.dumps({'resources':[resource],
        'verification':{'resourceCount':{'complete':True,'collected':1,'uniqueIds':1}}}), encoding='utf-8')
    (source/'aicaf-evidence.json').write_text(json.dumps({'sections':{}}), encoding='utf-8')
    @contextmanager
    def db():
        raise AssertionError('이 단위 시험은 실제 DB에 접근하지 않는다.')
        yield
    deps = {'ROOT':tmp_path/'data','assessment':lambda aid:assessment,
            'infra_case_dir':lambda cid:case, 'db':db}
    legacy = asis_view.Controller(**deps)
    flow = Flow(deps, legacy)
    first_input = flow.current_state.collect(AID,FIRST)
    first_result = asis_analysis.analyze(first_input)
    first_result['stages'] = first_result['stages'][:1]
    first_record = {'revision_id':FIRST,'sha256':'a'*64}
    q = contract.derive_questions(first_input, first_record, current_analysis=first_result)
    q['questions'] = q['questions'][:2]
    answers = {row['id']:{'qid':row['id'], 'transcript':'합성 검증용 응답이며 실제 고객 진술이 아니다.',
                         'summary':'운영 검증 근거 없음', 'score':'0'} for row in q['questions']}
    answers['UNRELATED_LEGACY_ID'] = {'qid':'UNRELATED_LEGACY_ID','transcript':'UNRELATED_SHOULD_NOT_BE_CONSUMED','summary':'old','score':'4'}
    state = {'source_sha256':first_input['run']['source_bundle_sha256'],
             'reports':[{'record':first_record},{'record':{'revision_id':SECOND,'sha256':'b'*64}},{}]}
    flow.require = lambda aid,kind:deepcopy(state)
    flow.questionnaire = lambda aid,record:deepcopy(q)
    flow.answers = lambda aid:deepcopy(answers)
    first_folder = flow.current_state.job_dir(AID,FIRST)
    first_folder.mkdir(parents=True)
    asis_view.atomic(first_folder/'analysis.json',first_result)
    return flow, state, q, answers, source, first_result


def test_interview_input_uses_only_the_current_questionnaire_and_binds_every_run(bridge):
    flow,state,q,answers,source,first = bridge
    original = flow.current_state.collect(AID,SECOND)
    data = flow.prepare(AID,original,'interview')
    current = {row['id'] for row in q['questions']}
    assert set(data['context']['interview_snapshot']['answers']) == current
    assert 'UNRELATED_SHOULD_NOT_BE_CONSUMED' not in json.dumps(data,ensure_ascii=False)
    assert all(f['run'] == data['run'] for f in data['facts'])
    assert data['context']['report_kind'] == 'interview'
    assert data['context']['assessment_workflow']['upstream'] == {'current_state':'a'*64}
    assert data['context']['prior_current_state_analysis']['areas'] == first['stages'][0]['areas']


def test_integrated_input_consumes_the_frozen_second_report_not_live_row_metadata(bridge):
    flow,state,q,answers,source,first = bridge
    second_data = flow.prepare(AID,flow.current_state.collect(AID,SECOND),'interview')
    for row in second_data['context']['interview_snapshot']['answers'].values():
        row['capture_marker'] = 'FROZEN_SECOND_REPORT'
    second_folder = flow.interview.job_dir(AID,SECOND);second_folder.mkdir(parents=True)
    asis_view.atomic(second_folder/'analysis.json',{'input':second_data})
    for row in answers.values():row['capture_marker'] = 'LIVE_METADATA_MUST_NOT_REPLACE_FROZEN_INPUT'
    data = flow.prepare(AID,flow.current_state.collect(AID,THIRD),'integrated')
    assert data['context']['assessment_workflow']['upstream'] == {'current_state':'a'*64,'interview':'b'*64}
    assert all(row['capture_marker'] == 'FROZEN_SECOND_REPORT'
               for row in data['context']['interview_snapshot']['answers'].values())
    assert all(f['run'] == data['run'] for f in data['facts'])


def test_changed_live_answer_cannot_be_combined_with_an_old_interview_report(bridge):
    flow,state,q,answers,source,first = bridge
    second_data = flow.prepare(AID,flow.current_state.collect(AID,SECOND),'interview')
    folder = flow.interview.job_dir(AID,SECOND);folder.mkdir(parents=True)
    asis_view.atomic(folder/'analysis.json',{'input':second_data})
    answers[q['questions'][0]['id']]['transcript'] += '새로운 원문'
    with pytest.raises(web.HTTPConflict) as raised:
        flow.prepare(AID,flow.current_state.collect(AID,THIRD),'integrated')
    assert '인터뷰 보고서와 현재 질문·답변' in raised.value.text


def test_changed_source_is_rejected_before_any_interview_or_integrated_input_is_created(bridge):
    flow,state,q,answers,source,first = bridge
    path = source/'topology.json';doc=json.loads(path.read_text(encoding='utf-8'))
    doc['resources'][0]['location'] = 'koreasouth'
    path.write_text(json.dumps(doc),encoding='utf-8')
    with pytest.raises(web.HTTPConflict) as raised:
        flow.prepare(AID,flow.current_state.collect(AID,SECOND),'interview')
    assert '원천이 변경' in raised.value.text


def test_missing_actual_answer_is_not_filled_from_an_unrelated_or_legacy_answer(bridge):
    flow,state,q,answers,source,first = bridge
    answers.pop(q['questions'][0]['id'])
    with pytest.raises(web.HTTPConflict) as raised:
        flow.prepare(AID,flow.current_state.collect(AID,SECOND),'interview')
    assert '실제 인터뷰 답변' in raised.value.text
