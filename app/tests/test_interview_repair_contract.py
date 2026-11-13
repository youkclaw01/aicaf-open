"""합성 요약 보완 응답의 전체 ID·원문 인용·불변 필드 계약을 검증한다."""
from copy import deepcopy
from typing import Any,cast
import json
import pytest
import assessment_flow_interview as report


def items():
    return [{'question_id':'UNIT_Q1','transcript':'합성 응답 하나의 원문','summary':'기존 요약 하나','score':'1','topic':'UNIT'},
            {'question_id':'UNIT_Q2','transcript':'합성 응답 둘의 원문','summary':'기존 요약 둘','score':'2','topic':'UNIT'}]


def function():
    fn=getattr(report,'summary_repair_candidate',None)
    assert callable(fn),'요약 보완 응답 계약 함수가 없다'
    return cast(Any,fn)


def response():
    return {'summaries':[{'question_id':'UNIT_Q1','summary':'합성 원문 하나를 요약한다','source_quote':'응답 하나'},
                         {'question_id':'UNIT_Q2','summary':'합성 원문 둘을 요약한다','source_quote':'응답 둘'}]}


def test_valid_summary_response_changes_only_summary():
    source=items();before=deepcopy(source);out=function()(source,json.dumps(response(),ensure_ascii=False))
    assert source==before and out is not source
    for old,new in zip(source,out):assert {k:v for k,v in old.items() if k!='summary'}=={k:v for k,v in new.items() if k!='summary'}
    assert out[0]['summary']!=source[0]['summary']


@pytest.mark.parametrize('kind',['missing','duplicate','foreign','quote','extra','json'])
def test_invalid_response_has_reproducible_feedback_and_no_partial_mutation(kind):
    source=items();before=deepcopy(source);value=response()
    if kind=='missing':value['summaries'].pop()
    elif kind=='duplicate':value['summaries'][1]=deepcopy(value['summaries'][0])
    elif kind=='foreign':value['summaries'][1]['question_id']='UNKNOWN'
    elif kind=='quote':value['summaries'][1]['source_quote']='원문에 없는 인용'
    elif kind=='extra':value['summaries'][1]['score']='4'
    raw='not-json' if kind=='json' else json.dumps(value,ensure_ascii=False)
    with pytest.raises(ValueError) as error:function()(source,raw)
    feedback=getattr(error.value,'feedback',None)
    assert isinstance(feedback,dict) and all(feedback.get(k) for k in ('where','quote','why','fix'))
    assert feedback['expected_question_ids']==['UNIT_Q1','UNIT_Q2']
    assert source==before


def test_approved_topic_rejects_changed_source_and_tampered_binding(tmp_path):
    source=items();candidate=function()(source,json.dumps(response(),ensure_ascii=False))
    review={'verdict':'pass','content_checked':True,'render_checked':False,'defects':[]}
    report.save_approved_topic(tmp_path,source,candidate,review)
    assert report.restore_topic_candidate(source,tmp_path)==candidate
    changed=deepcopy(source);changed[0]['transcript']='변경된 합성 원문'
    assert report.restore_topic_candidate(changed,tmp_path)==changed
    path=tmp_path/'approved-topic.json';saved=json.loads(path.read_text(encoding='utf-8'))
    saved['candidate'][0]['summary']='검토 이후 변조'
    path.write_text(json.dumps(saved,ensure_ascii=False),encoding='utf-8')
    with pytest.raises(ValueError,match='binding_invalid'):report.restore_topic_candidate(source,tmp_path)


def test_legacy_candidate_is_not_an_approval_and_never_changes_source(tmp_path):
    source=items();candidate=function()(source,json.dumps(response(),ensure_ascii=False))
    path=tmp_path/'author-1.json';path.write_text(json.dumps({'candidate':candidate,'receipt':{'fixture_only':True}},ensure_ascii=False),encoding='utf-8')
    assert report.restore_topic_candidate(source,tmp_path)==candidate
    assert not (tmp_path/'approved-topic.json').exists()
    changed=deepcopy(source);changed[0]['score']='4'
    assert report.restore_topic_candidate(changed,tmp_path)==changed
    with pytest.raises(ValueError,match='review_not_passed'):report.save_approved_topic(tmp_path,source,candidate,{'verdict':'revise'})


def test_replaced_diagnostic_preserves_exact_previous_bytes(tmp_path):
    path=tmp_path/'author-response-1.json'
    report.topic_checkpoint(path,{'raw_response':'합성 직전 후보'})
    before=path.read_bytes()
    report.topic_checkpoint(path,{'raw_response':'합성 다음 후보'})
    history=list((tmp_path/'checkpoint-history').glob('*.json'))
    assert len(history)==1 and history[0].read_bytes()==before
    assert json.loads(path.read_text(encoding='utf-8'))['raw_response']=='합성 다음 후보'
