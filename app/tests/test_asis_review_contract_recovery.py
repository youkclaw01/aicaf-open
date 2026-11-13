"""실제 발견한 pass+렌더미검토 보류 응답을 승인하지 않고 검토자에게만 계약 보완을 요청한다."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import asis_analysis
from asis_pipeline import Pipeline
from test_asis_connection import SyntheticRuntime,synthetic_input


def test_stage_review_repairs_invalid_evidence_reference_without_reauthoring(tmp_path):
    """검토 응답의 계약 오류는 같은 후보를 대상으로 검토자에게만 재전달한다."""
    data, candidate = asis_analysis.contract().synthetic_fixture()
    calls = []
    class InvalidStageReview:
        async def ask(self, role, message, **kwargs):
            payload = json.loads(message.rsplit('\n입력:\n', 1)[-1])
            calls.append((role, deepcopy(payload)))
            if role in ('author', 'direction_author'):
                value = deepcopy(candidate)
            else:
                review_number = sum(r == 'content_review' for r, _ in calls)
                value = {'stage':'review','run':data['run'],'verdict':'revise' if review_number == 1 else 'pass',
                    'content_checked':True,'render_checked':False,'unresolved':[],
                    'defects':[{'where':'/areas/0/headline','quote':candidate['areas'][0]['headline'],
                        'why':'합성 계약 오류','fix':'허용된 실제 Fact ID로 검토한다.',
                        'evidence_refs':['I_NOT_A_SOURCE_FACT']}] if review_number == 1 else []}
            return json.dumps(value), {'test_only':True,'role':role}
    engine = Pipeline(InvalidStageReview(), tmp_path/'run')
    actual = asyncio.run(engine.reviewed_stage(data, 1, 'stage-review-contract'))
    assert actual == candidate
    assert [r for r, _ in calls] == ['author','content_review','content_review']
    first, second = calls[1][1], calls[2][1]
    assert first['stage_data']['draft'] == second['stage_data']['draft'] == candidate
    assert second['stage_data']['review_contract_error'] == 'invalid_reference:evidence_refs'
    assert second['stage_data']['prior_review']['defects'][0]['evidence_refs'] == ['I_NOT_A_SOURCE_FACT']
    receipt = json.loads((tmp_path/'run/checkpoints/stage-review-contract/accepted.json').read_text(encoding='utf-8'))
    assert receipt['review']['verdict'] == 'pass'
    assert receipt['candidate_sha256'] == receipt['reviewed_candidate_sha256']


def test_independent_review_repairs_invalid_contract_without_rewriting_draft(tmp_path):
    baseline=asis_analysis.analyze(synthetic_input(tmp_path));data=baseline['input'];draft={'stages':baseline['stages']}
    class InvalidFirst(SyntheticRuntime):
        def __init__(self):super().__init__();self.reviews=[]
        async def ask(self,role,message,**kwargs):
            payload=json.loads(message.rsplit('\n입력:\n',1)[-1]);self.reviews.append(deepcopy(payload))
            assert role=='content_review'
            value={'stage':'review','run':data['run'],'verdict':'pass','content_checked':True,'render_checked':False,'defects':[],
                'unresolved':[{'topic':'render_not_checked','reason':'No actual render supplied to content review.',
                    'required_evidence':'PPT render belongs to later visual review.','fact_refs':[]}]} if len(self.reviews)==1 else {
                'stage':'review','run':data['run'],'verdict':'pass','content_checked':True,'render_checked':False,'defects':[],'unresolved':[]}
            return json.dumps(value),{'test_only':True,'role':role,'actual_model':'synthetic_not_a_provider'}
    runtime=InvalidFirst();engine=Pipeline(runtime,tmp_path/'run')
    result=asyncio.run(engine.independent_review(data,draft,'contract-unit'))
    assert result['verdict']=='pass' and result['unresolved']==[]
    assert len(runtime.reviews)==2
    assert runtime.reviews[0]['stage_data']['draft']==runtime.reviews[1]['stage_data']['draft']==draft
    assert runtime.reviews[1]['stage_data']['prior_review']['unresolved'][0]['topic']=='render_not_checked'
    assert runtime.reviews[1]['stage_data']['review_contract_error']=='pass_with_open_defects'
