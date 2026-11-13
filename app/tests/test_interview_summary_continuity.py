"""인터뷰 요약 보완이 다음 독립검토·네이티브 입력에 유지되는지 확인하는 합성 상태기계 시험."""
import asyncio
from copy import deepcopy
import json
import pytest
import assessment_flow_interview as report


@pytest.mark.parametrize('existing_rounds',[0,3])
def test_summary_repair_stays_in_next_review_and_native_input(tmp_path,monkeypatch,existing_rounds):
    originals=[]
    for number in range(1,existing_rounds+1):
        folder=tmp_path/'artifact-rounds'/f'round-{number:03d}';folder.mkdir(parents=True)
        marker=folder/'original-marker';marker.write_text('합성 원본 보존',encoding='utf-8');originals.append(marker)
    original={'question_id':'SYNTH-Q1','topic':'SYNTH-TOPIC','transcript':'이 문장은 합성 회복 시험 응답이다.',
        'summary':'수정 전 합성 요약','ai_score':2,'guidelines':'합성 기준 원문','evidence_refs':['SYNTH-F1']}
    fixed='합성 회복 시험 응답임을 확인했다.'
    source={'run':{'fixture':'synthetic_only'},'context':{'classification':'synthetic_test_only',
        'interview_snapshot':{'questionnaire':{'questions':[{'id':'SYNTH-Q1','topic':'SYNTH-TOPIC'}]},'answers':{}}}}
    before=deepcopy(source);reviews=[];author_calls=[];native=[]
    class NativeBoundaryReached(RuntimeError):pass
    class RuntimeStub:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):return False
        async def ask(self,role,message,**kwargs):
            assert role=='author';author_calls.append(json.loads(message))
            return json.dumps({'summaries':[{'question_id':'SYNTH-Q1','summary':fixed,
                'source_quote':'합성 회복 시험 응답'}]},ensure_ascii=False),{'classification':'unit_stub_no_provider_request'}
    def items(data,summaries=None):
        value=deepcopy(original)
        if summaries:value['summary']=summaries.get(value['question_id'],value['summary'])
        return [value]
    async def review(runtime,draft,folder,session,**kwargs):
        reviews.append(deepcopy(draft))
        if len(reviews)==1:
            return {'verdict':'revise','content_checked':True,'render_checked':False,
                'defects':[{'where':'/items/0/summary','quote':original['summary'],
                    'why':'합성 원문 범위를 표시해야 한다.','fix':'합성 응답이라는 원문 요약만 보완한다.'}]}
        assert draft['items'][0]['summary']==fixed
        assert draft['items'][0]['transcript']==original['transcript']
        assert draft['items'][0]['ai_score']==original['ai_score']
        return {'verdict':'pass','content_checked':True,'render_checked':False,'defects':[]}
    def build(result,folder):
        assert folder.name==f'round-{existing_rounds+1:03d}'
        native.append(deepcopy(result));raise NativeBoundaryReached('native_boundary_observed_not_rendered')
    monkeypatch.setattr(report.workflow,'unanswered',lambda *args:[])
    monkeypatch.setattr(report,'interview_items',items)
    monkeypatch.setattr(report,'_review',review)
    monkeypatch.setattr(report,'build_native',build)
    with pytest.raises(NativeBoundaryReached):
        asyncio.run(report.execute(source,tmp_path,builder=None,runtime_factory=RuntimeStub,rounds=3))
    assert len(author_calls)==1 and len(reviews)==2
    assert native[0]['items'][0]['summary']==fixed
    assert native[0]['items'][0]['transcript']==original['transcript']
    assert native[0]['items'][0]['ai_score']==original['ai_score']
    assert source==before
    assert all(p.read_text(encoding='utf-8')=='합성 원본 보존' for p in originals)
