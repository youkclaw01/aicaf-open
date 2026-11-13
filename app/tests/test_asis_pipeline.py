"""파이프라인 순서·정본 계약·누적 수정 시험. 모델 출력은 명시된 합성 전송 fixture다."""
from copy import deepcopy
from pathlib import Path
import asyncio
import json
import sys

APP=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(APP))
import asis_analysis
import asis_model
import asis_pipeline


def test_independent_review_is_bound_to_exact_candidate_and_resumes_checkpoint(tmp_path):
    data, draft = asis_analysis.contract().synthetic_fixture()
    events=[];inputs=[]
    class SyntheticRuntime:
        async def ask(self, role, message, *, session_id, instructions='', image=None):
            inputs.append((role,session_id,message))
            if role in ('author', 'direction_author'):
                value=deepcopy(draft)
            else:
                value={'stage':'review','run':data['run'],'verdict':'pass','content_checked':True,
                       'render_checked':False,'defects':[],'unresolved':[]}
            return json.dumps(value,ensure_ascii=False), {'test_only':True,'role':role,'session_id':session_id,
                'response_sha256':asis_model.digest(value)}
    async def exercise():
        assert hasattr(asis_pipeline,'Pipeline'), 'MAF writing path is not connected'
        engine=asis_pipeline.Pipeline(SyntheticRuntime(),tmp_path,progress=events.append)
        result=await engine.reviewed_stage(data,1,'area-fixture')
        assert result==draft
        resumed=await engine.reviewed_stage(data,1,'area-fixture')
        assert resumed==result
    asyncio.run(exercise())
    assert [x[0] for x in inputs]==['author','content_review']
    assert inputs[0][1]!=inputs[1][1]
    record=json.loads((tmp_path/'checkpoints/area-fixture/accepted.json').read_text(encoding='utf-8'))
    assert record['candidate_sha256']==asis_model.digest(draft)
    assert record['review']['verdict']=='pass'
    assert record['reviewed_candidate_sha256']==record['candidate_sha256']
    assert record['execution_classification']=='synthetic_unit_test'
    assert any(e['phase']=='content_review' for e in events)


def test_revision_receives_prior_candidate_and_accumulated_literal_defects(tmp_path):
    data, draft = asis_analysis.contract().synthetic_fixture()
    prompts=[]
    corrected=deepcopy(draft)
    corrected['areas'][0]['headline']='수집 범위에서 확인된 합성 구성'
    defect={'where':'/areas/0/headline','quote':draft['areas'][0]['headline'],
            'why':'합성 제목에 관측 범위가 빠졌다.', 'fix':'관측 범위를 제목에 명시한다.', 'evidence_refs':['F1']}
    class SyntheticRuntime:
        def __init__(self): self.authors=0; self.reviews=0
        async def ask(self,role,message,**kwargs):
            if role in ('author', 'direction_author'):
                self.authors+=1; prompts.append(message)
                value=draft if self.authors==1 else corrected
            else:
                self.reviews+=1
                value={'stage':'review','run':data['run'],
                    'verdict':'revise' if self.reviews==1 else 'pass',
                    'content_checked':True,'render_checked':False,
                    'defects':[defect] if self.reviews==1 else [],'unresolved':[]}
            return json.dumps(value,ensure_ascii=False), {'test_only':True}
    engine=asis_pipeline.Pipeline(SyntheticRuntime(),tmp_path)
    assert asyncio.run(engine.reviewed_stage(data,1,'repair-fixture'))==corrected
    second=json.loads((tmp_path/'checkpoints/repair-fixture/round-002/author-input.json').read_text(encoding='utf-8'))
    assert second['stage_data']['prior_output']==draft
    assert second['stage_data']['accumulated_defects']==[defect]
    assert len(prompts)==2
