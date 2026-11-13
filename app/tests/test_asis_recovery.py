"""내용 반려를 실제 생산 조정기로 복구하는 계약 시험. 전송만 명시적 합성이다."""
from copy import deepcopy
from collections import Counter
import asyncio
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_asis_connection import SyntheticRuntime,synthetic_input
import asis_analysis
from asis_pipeline import Pipeline


def test_document_rejection_repairs_affected_area_and_rebuilds_dependents(tmp_path):
    data=synthetic_input(tmp_path);old=[];events=[]
    class RejectedOnce(SyntheticRuntime):
        def __init__(self):super().__init__();self.documents=0
        async def ask(self,role,message,**kwargs):
            payload=json.loads(message.rsplit('\n입력:\n',1)[-1])
            if 'stage_data' not in payload:
                # 영역별 진단·과제 계획 호출(소형 입력)은 기본 합성 대역이 처리한다.
                return await super().ask(role, message, **kwargs)
            stage=payload['stage_data'];mode=stage.get('mode')
            if role=='content_review' and mode=='document-shard-arch-area':
                self.documents+=1
                if self.documents==1:
                    # 아키텍처 검토 단위는 사실 인용(fact_refs 등)을 빼고 서술만 받는다(영석님 정의 2026-10-19). 검토자에게 보인 headline 이 지적 원문이다.
                    original=stage['draft']['stages'][0]['areas'][0];old.append({'headline':original['headline']})
                    defect={'where':'/stages/0/areas/0/headline','quote':original['headline'],
                        'why':'합성 시험: 수집 범위를 제목에서 명확히 구분해야 한다.',
                        'fix':'관측 시점과 합성 검증 범위를 제목에 명시한다.','evidence_refs':[]}
                    return json.dumps({'stage':'review','run':payload['run'],'verdict':'revise',
                        'content_checked':True,'render_checked':False,'defects':[defect],'unresolved':[]},ensure_ascii=False),{'test_only':True}
            raw,receipt=await super().ask(role,message,**kwargs)
            if role in ('author', 'direction_author') and mode=='area' and stage.get('prior_output'):
                # 작성자에게 돌아간 이전 후보는 정본 영역 그대로이고(검토용 축약본이 아니다), 지적 원문은 검토자에게 보인 headline 과 같다.
                assert stage['prior_output']['areas'][0]['headline']==old[0]['headline']
                assert 'fact_refs' in stage['prior_output']['areas'][0]['observations'][0]
                assert stage['accumulated_defects'][0]['quote']==old[0]['headline']
                candidate=json.loads(raw);candidate['areas'][0]['headline']='수집 시점의 합성 구성을 기준으로 현황을 판정한다.'
                raw=json.dumps(candidate,ensure_ascii=False)
            return raw,receipt
    runtime=RejectedOnce();engine=Pipeline(runtime,tmp_path/'run',progress=events.append)
    result=asyncio.run(engine.analyze(data))
    assert result['stages'][0]['areas'][0]['headline']=='수집 시점의 합성 구성을 기준으로 현황을 판정한다.'
    authors=[call for call in runtime.calls if call['role'] in ('author', 'direction_author')];counts=Counter(call['mode'] for call in authors)
    assert counts['area']==len(asis_analysis.AREA_NAMES)+1
    assert counts['summary'] == 2
    synthesis_authors = [call for call in authors if call['mode']=='synthesis']
    assert all(call['input']['stage_data'].get('direction_assignment') for call in synthesis_authors)
    by_direction = Counter(call['input']['stage_data']['direction_assignment']['item_id'] for call in synthesis_authors)
    # 종합 전체를 두 번 작성하지 않고 변경된 영역을 참조한 방향만 한 번 보완한다.
    assert counts['synthesis'] == len(result['stages'][1]['directions']) + 1
    assert sorted(by_direction.values()) == [1]*(len(by_direction)-1)+[2]
    assert counts['actions'] == 2 * (len(result['stages'][2]['diagnoses']) + len(result['stages'][2]['actions']))
    assert all(call['input']['stage_data'].get('item_assignment')
               for call in authors if call['mode'] == 'actions')
    assert any(e['unit']=='document' and e['phase']=='repair' and e['status']=='required' for e in events)
    assert result['quality_status']=='content_review_passed'
