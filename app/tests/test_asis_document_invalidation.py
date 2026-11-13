"""실제 문서 반려 소비자는 변경된 원천의 후속 승인만 먼저 무효화한다."""
from copy import deepcopy
from pathlib import Path
import asyncio
import json
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from asis_pipeline import Pipeline
from test_asis_connection import SyntheticRuntime, synthetic_input


def test_actual_document_repair_invalidates_dependents_before_corrective_author(tmp_path):
    class RejectOnce(SyntheticRuntime):
        documents=0
        engine=None
        observation=None
        async def ask(self,role,message,**kwargs):
            data=json.loads(message.rsplit('\n입력:\n',1)[-1])
            if 'stage_data' not in data:
                # 영역별 진단·과제 계획 호출(소형 입력)은 기본 합성 대역이 처리한다.
                return await super().ask(role, message, **kwargs)
            stage=data['stage_data']
            if role=='content_review' and stage.get('mode')=='document-shard-arch-area':
                self.documents+=1
                if self.documents==1:
                    defect={'where':'/stages/0/areas/0/headline','quote':stage['draft']['stages'][0]['areas'][0]['headline'],
                        'why':'합성 수집 시점 설명 보완','fix':'동일 원천의 수집 시점을 명시','evidence_refs':[]}
                    return json.dumps({'stage':'review','run':data['run'],'verdict':'revise','content_checked':True,
                        'render_checked':False,'defects':[defect],'unresolved':[]},ensure_ascii=False),{'test_only':True}
            if role in ('author', 'direction_author') and stage.get('mode')=='area' and stage.get('prior_output'):
                self.observation=deepcopy(self.engine.tasks.ledger)
            raw,receipt=await super().ask(role,message,**kwargs)
            if role in ('author', 'direction_author') and stage.get('mode')=='area' and stage.get('prior_output'):
                result=json.loads(raw);result['areas'][0]['headline']='수집 시점의 합성 구성을 기준으로 현황을 판정한다.'
                raw=json.dumps(result,ensure_ascii=False)
            return raw,receipt
    runtime=RejectOnce();engine=Pipeline(runtime,tmp_path/'run');runtime.engine=engine
    result=asyncio.run(engine.analyze(synthetic_input(tmp_path)))
    observed=runtime.observation
    assert observed is not None
    assert all(observed['tasks'][key]['status']=='invalidated' for key in ('synthesis','actions','summary','document')), 'whole rework retained current downstream completion'
    assert observed['tasks']['area-compute']['status']=='completed'
    assert observed['tasks']['area-network']['status']=='running'
    invalidations=[event for event in observed['notifications'] if event['event']=='tasks_invalidated']
    assert invalidations and 'area-compute' not in invalidations[-1]['task_ids']
    assert result['quality_status']=='content_review_passed'
