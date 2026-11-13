"""실제 native 렌더 이후 내용 반려를 작성·독립 재검토로 되돌리는 합성 전송 시험."""
from copy import deepcopy
from collections import Counter
from pathlib import Path
import asyncio
import json
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_asis_connection import SyntheticRuntime,synthetic_input
import asis_artifacts
import asis_service


def test_visual_content_defect_rewrites_summary_then_renders_exact_new_candidate(tmp_path):
    data=synthetic_input(tmp_path);observed=[];events=[];built=[]
    class VisualRejectedOnce(SyntheticRuntime):
        def __init__(self,**kwargs):super().__init__();self.visuals=0;observed.append(self)
        async def __aenter__(self):return self
        async def __aexit__(self,*args):return None
        async def ask(self,role,message,**kwargs):
            payload=json.loads(message.rsplit('\n입력:\n',1)[-1])
            if 'stage_data' not in payload:
                # 영역별 진단·과제 계획 호출(소형 입력)은 기본 합성 대역이 처리한다.
                return await super().ask(role, message, **kwargs)
            stage=payload['stage_data']
            if role=='visual_review':
                self.visuals+=1
                if self.visuals==1:
                    draft=stage['draft']
                    assert 'stages' in draft,'visual review must bind canonical text as well as actual pages'
                    defect={'where':'/stages/3/summary/headline','quote':draft['stages'][3]['summary']['headline'],
                        'why':'합성 시험: 요약 제목에 입력 범위를 구분해야 한다.',
                        'fix':'합성 검증 범위를 명시한다.','evidence_refs':[]}
                else:defect=None
                self.calls.append({'role':role,'mode':'visual','input':deepcopy(payload)})
                value={'stage':'review','run':payload['run'],'verdict':'revise' if defect else 'pass',
                    'content_checked':True,'render_checked':True,'defects':[defect] if defect else [],'unresolved':[]}
                return json.dumps(value,ensure_ascii=False),{'test_only':True,'role':role}
            raw,receipt=await super().ask(role,message,**kwargs)
            if role in ('author', 'direction_author') and stage.get('mode')=='summary' and stage.get('prior_output'):
                assert stage['accumulated_defects']
                value=json.loads(raw);value['summary']['headline']='합성 입력 범위를 구분한 최종 현황 판단'
                raw=json.dumps(value,ensure_ascii=False)
            return raw,receipt
    def build(result,folder):
        built.append(deepcopy(result));return asis_artifacts.build(__import__('visual_spec_stub').with_visual_specs(result),folder)
    result,manifest=asyncio.run(asis_service.execute(data,tmp_path/'run',builder=build,
        runtime_factory=VisualRejectedOnce,progress=events.append))
    assert len(built)==2
    assert result['stages'][3]['summary']['headline']=='합성 입력 범위를 구분한 최종 현황 판단'
    author_modes=Counter(c['mode'] for c in observed[0].calls if c['role'] in ('author', 'direction_author'))
    assert author_modes['summary'] == 2
    assert author_modes['synthesis'] == len(result['stages'][1]['directions'])
    assert all(c['input']['stage_data'].get('direction_assignment') for c in observed[0].calls
               if c['role'] in ('author', 'direction_author') and c['mode']=='synthesis')
    assert author_modes['actions'] == len(result['stages'][2]['diagnoses']) + len(result['stages'][2]['actions'])
    assert manifest['quality_review']['all_render_pages_reviewed']
    assert manifest['quality_review']['pptx_sha256']!=asis_service.artifact_record(
        json.loads((tmp_path/'run/artifact-rounds/round-001/artifact-manifest.json').read_text(encoding='utf-8')),'asis_report_pptx')['sha256']
    assert any(e['phase']=='repair' for e in events)
