"""합성 상태기계로 형식 보완과 프로세스 재개를 검증하며 실제 모델·Office는 호출하지 않는다."""
import asyncio,json
from copy import deepcopy
from typing import Any,cast
import pytest
import assessment_flow_interview as report
from test_interview_repair_contract import items,response

class NativeBoundary(Exception):pass


@pytest.mark.parametrize('mode',['repair','always-invalid','restored-invalid'])
def test_invalid_author_response_uses_same_budget_and_valid_topic_survives_restart(tmp_path,monkeypatch,mode):
    source=items();calls=[];reviews=[];built=[]
    def selected(data,summaries=None):
        out=deepcopy(source)
        for row in out:row['summary']=(summaries or {}).get(row['question_id'],row['summary'])
        return out
    class Runtime:
        def __init__(self,**_):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*_):return False
        async def ask(self,role,message,**kwargs):
            payload=json.loads(message);calls.append((payload,kwargs))
            if len(calls)==1 and mode!='restored-invalid':return json.dumps({'summaries':response()['summaries'][:1]},ensure_ascii=False),{'fixture_only':True}
            assert payload['author_validation_feedback'] and payload['prior_author_response']
            assert payload['expected_question_ids']==['UNIT_Q1','UNIT_Q2']
            if mode=='always-invalid':return json.dumps({'summaries':response()['summaries'][:1]},ensure_ascii=False),{'fixture_only':True}
            return json.dumps(response(),ensure_ascii=False),{'fixture_only':True}
    async def review(runtime,draft,*_,**__):
        summaries=[r['summary'] for r in draft['items']];reviews.append(summaries)
        if len(reviews)==1:return {'verdict':'revise','content_checked':True,'render_checked':False,'defects':[{'where':'/items/0/summary','quote':source[0]['summary'],'why':'합성 보완','fix':'원문으로 요약'}]}
        assert summaries==[r['summary'] for r in response()['summaries']]
        return {'verdict':'pass','content_checked':True,'render_checked':False,'defects':[]}
    def native(result,path):
        built.append(deepcopy(result['items']))
        assert [r['summary'] for r in result['items']]==[r['summary'] for r in response()['summaries']]
        raise NativeBoundary()
    monkeypatch.setattr(report,'interview_items',selected)
    monkeypatch.setattr(report.workflow,'unanswered',lambda *_:[])
    monkeypatch.setattr(report,'_review',review)
    monkeypatch.setattr(report,'build_native',native)
    data={'run':{'assessment_id':99802,'analysis_revision':'unit'},'context':{'interview_snapshot':{'questionnaire':{'questions':[{'topic':'UNIT'}]},'answers':{}}}}
    if mode=='restored-invalid':
        bad=json.dumps({'summaries':response()['summaries'][:1]},ensure_ascii=False)
        with pytest.raises(ValueError) as caught:report.summary_repair_candidate(source,bad)
        report.atomic(tmp_path/'content-reviews/UNIT/repair-state.json',{'base_input_sha256':report.digest(source),
            'candidate_sha256':report.digest(source),'prior_author_response':bad,'validation_feedback':[caught.value.feedback]})
    if mode=='always-invalid':
        with pytest.raises(RuntimeError,match='interview_content_review_blocked'):asyncio.run(report.execute(data,tmp_path,runtime_factory=cast(Any,Runtime),rounds=3))
        assert len(calls)==2 and len(reviews)==1 and not built
        assert source==items()
        return
    for _ in range(2):
        with pytest.raises(NativeBoundary):asyncio.run(report.execute(data,tmp_path,runtime_factory=cast(Any,Runtime),rounds=3))
    assert len(calls)==(1 if mode=='restored-invalid' else 2) and len(reviews)==3
    if len(calls)>1:assert calls[0][1]['session_id']==calls[1][1]['session_id']
    assert built[0]==built[1]
    assert (tmp_path/'content-reviews/UNIT/author-response-1.json').exists()
    if mode!='restored-invalid':assert (tmp_path/'content-reviews/UNIT/author-validation-1.json').exists()
    assert source==items()
