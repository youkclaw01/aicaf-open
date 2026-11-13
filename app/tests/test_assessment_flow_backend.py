"""세 보고서의 독립 저장·원문 유지·공식 질문·인터뷰 네이티브 쌍을 합성 단위 자료로 시험한다."""
from __future__ import annotations
import asyncio
from contextlib import contextmanager
from copy import deepcopy
import inspect
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from pptx import Presentation
from openpyxl import load_workbook

from asis_model import normalize, digest
from asis_view import Controller, atomic
import assessment_workflow as flow


def source(tmp_path):
    folder = tmp_path/'Source';folder.mkdir(exist_ok=True)
    (folder/'topology.json').write_text(json.dumps({'resources': [], 'configurations': {},
        'verification': {'resourceCount': {'complete': True, 'collected': 0, 'uniqueIds': 0}}}), encoding='utf-8')
    return folder


def metadata(aid=97, track='new'):
    return {'id': aid, 'customer': '합성 단위 시험 전용', 'title': '세 보고서 계약 시험',
            'track': track, 'infra_case_id': f'unit-{aid}', 'interviewer': '단위 시험', 'created': 0}


def interview_data(tmp_path, track='new'):
    a = metadata(track=track);data = normalize(source(tmp_path), a, [], 'unit')
    q = flow.derive_questions(data, {'revision_id': 'unit-first', 'sha256': 'a'*64})
    # 문항 수가 고정이라는 가정이 없는지 각 영역의 실제 문항 한 개로 네이티브 계약을 검증한다.
    q['questions'] = [next(x for x in q['questions'] if x['area'] == area) for area in 'ABCD']
    answers = {row['id']: {'qid': row['id'], 'transcript': '합성 원문: 실제 고객 답변이 아니다.',
               'summary': '합성 시험 자료이며 운영 사실로 사용할 수 없다.', 'score': '0'} for row in q['questions']}
    data['context'].update(report_kind='interview', assessment_metadata=a,
        interview_snapshot={'questionnaire': q, 'answers': answers},
        assessment_workflow={'schema': flow.SCHEMA, 'assessment_id': a['id'],
            'source_sha256': data['run']['source_bundle_sha256'], 'questionnaire_sha256': digest(q),
            'answers_sha256': flow.answer_binding(q['questions'], answers), 'upstream': {'current_state': 'a'*64}})
    return data


def test_current_state_never_reads_interview_database(tmp_path):
    folder = source(tmp_path);a = metadata()
    @contextmanager
    def forbidden_db():
        raise AssertionError('current_state_read_interview_database')
        yield
    controller = Controller(ROOT=tmp_path/'data', assessment=lambda aid: a,
        infra_case_dir=lambda cid: tmp_path, db=forbidden_db, source_folder=lambda assessment: folder,
        report_kind='current_state')
    data = controller.collect(a['id'])
    assert data['context']['report_kind'] == 'current_state'
    assert not any(f['id'].startswith('F_IV_') for f in data['facts'])
    assert not data['goals']
    assert controller.root.name == 'current-state'


def test_three_report_storage_is_not_the_same_latest_pointer(tmp_path):
    values = [Controller(ROOT=tmp_path, assessment=None, infra_case_dir=None, db=None, report_kind=kind)
              for kind in ('current_state', 'interview', None)]
    assert len({c.folder(97) for c in values}) == len(values)
    assert {c.root.name for c in values} == {'current-state', 'interview-reports', 'asis'}


def test_zero_score_is_an_actual_answer():
    assert flow.unanswered([{'id':'q'}], {'q':{'transcript':'0점에 해당하는 실제 답변', 'score':0}}) == []


@pytest.mark.parametrize('track', ['new', 'existing'])
def test_guideline_derived_ids_do_not_change_score_definition(tmp_path, track):
    import questions, scoring
    data = normalize(source(tmp_path), metadata(track=track), [], 'unit')
    derived = flow.derive_questions(data, {'revision_id':'unit', 'sha256':'a'*64})['questions']
    bank = questions.questions_for(track)
    original = {q['id']:{'score':str(i%5)} for i,q in enumerate(bank)}
    mapped = {q['id']: original[q['origin_id']] for q in derived}
    assert scoring.compute(bank, original) == scoring.compute(derived, mapped)


@pytest.mark.parametrize('track', ['new', 'existing'])
def test_interview_native_pair_contains_real_questions_official_guidance_and_raw_answers(tmp_path, track):
    from assessment_flow_interview import build_pair, interview_items
    data = interview_data(tmp_path, track)
    value = {'schema':'assessment-interview-analysis/1', 'input':data, 'items':interview_items(data),
             'model_call_performed':False, 'quality_status':'unit_fixture_not_reviewed'}
    pair = build_pair(value, tmp_path/'native')
    deck = Presentation(pair['pptx'])
    text = '\n'.join(s.text for page in deck.slides for s in page.shapes if s.has_text_frame)
    assert '인터뷰 보고서' in text
    for item in value['items']:
        assert item['question'] in text
        assert item['guidelines'][0]['quote'] in text
    book = load_workbook(pair['xlsx'], read_only=True)
    assert {'질문과답변','공식지침','_frozen_analysis'} <= set(book.sheetnames)
    saved = json.loads(''.join(str(r[0] or '') for r in book['_frozen_analysis'].iter_rows(min_row=2,values_only=True)))
    book.close()
    assert saved == value
    assert all(a['transcript'] == '합성 원문: 실제 고객 답변이 아니다.' for a in saved['input']['context']['interview_snapshot']['answers'].values())
    assert value['quality_status'] == 'unit_fixture_not_reviewed'


def test_runtime_schema_is_in_message_not_an_invented_sdk_argument(tmp_path):
    from assessment_flow_interview import _review
    calls = []
    class UnitRuntime:
        # 실제 공개 Runtime.ask와 같은 인자만 허용한다. 제품 설정에는 주입하지 않는다.
        async def ask(self, role, message, *, session_id, instructions='', image=None):
            calls.append(json.loads(message))
            return json.dumps({'verdict':'pass','content_checked':True,'render_checked':False,'defects':[]}), {'unit_fixture':True}
    result = asyncio.run(_review(UnitRuntime(), {'items':[]}, tmp_path/'review', 'unit-session'))
    assert result['verdict'] == 'pass'
    assert calls[0]['output_json_schema']['properties']['verdict']['enum'] == ['pass','revise','blocked']


def test_review_repair_reuses_candidate_and_reports_invalid_quote(tmp_path):
    from assessment_flow_interview import _review
    calls = []
    class UnitRuntime:
        async def ask(self, role, message, *, session_id, instructions='', image=None):
            calls.append(json.loads(message))
            value = ({'verdict':'revise','content_checked':True,'render_checked':False,
                       'defects':[{'where':'/text','quote':'원문에 없는 문구','why':'unit','fix':'unit'}]}
                     if len(calls)==1 else {'verdict':'pass','content_checked':True,'render_checked':False,'defects':[]})
            return json.dumps(value), {'unit_fixture':True}
    actual = asyncio.run(_review(UnitRuntime(), {'text':'검토 원문'}, tmp_path/'review', 'same-session'))
    assert actual['verdict'] == 'pass'
    assert len(calls) == 2
    assert calls[1]['draft'] == calls[0]['draft']
    assert 'quote_not_at_pointer' in calls[1]['contract_error']


def test_frozen_report_material_is_not_duplicated_into_each_model_request(tmp_path):
    from asis_evidence_views import prompt_view
    data = interview_data(tmp_path)
    data['context']['prior_current_state_analysis'] = {'private_marker':'canonical-only'}
    projected = prompt_view(data)
    assert 'interview_snapshot' not in projected['context']
    assert 'prior_current_state_analysis' not in projected['context']
    assert 'interview_snapshot' in data['context']
