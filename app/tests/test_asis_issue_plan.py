"""분석 항목의 가변 개수와 참조를 코드가 봉인한다. 계획 전송만 합성이다."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import asis_analysis
from asis_pipeline import Pipeline
from test_asis_connection import SyntheticRuntime,synthetic_input


def test_issue_plan_allocates_evidence_bound_variable_actions_not_one_per_area(tmp_path):
    data=asis_analysis.analyze(synthetic_input(tmp_path))['input']
    fact=data['facts'][0]['id'];criterion=data['criteria'][0]['id']
    issues=[{'area_refs':['network'],'fact_refs':[fact],'criterion_refs':[criterion],'goal_refs':[],
        'scope':'합성 참조 구성의 이름 해석 경로','reason':'구성 참조와 실제 DNS 효과의 확인 범위를 분리한다.'},
        {'area_refs':['network','ai'],'fact_refs':[fact],'criterion_refs':[criterion],'goal_refs':[],
        'scope':'합성 모델 접근 경로','reason':'모델 배포와 접근 통제의 선행 조건을 함께 검증한다.'},
        {'area_refs':['operating'],'fact_refs':[fact],'criterion_refs':[criterion],'goal_refs':[],
        'scope':'합성 변경 승인 책임','reason':'운영 책임과 승인 조건을 별도 의사결정 대상으로 유지한다.'}]
    class Planned(SyntheticRuntime):
        async def ask(self,role,message,**kwargs):
            payload=json.loads(message.rsplit('\n입력:\n',1)[-1])
            assignment=payload['stage_data'].get('plan_assignment')
            if role=='interpret' and payload['stage_data'].get('mode')=='issue_plan' and assignment:
                # 영역별 계획 호출은 해당 영역이 첫 영역인 이슈만 제출하고 제공된 범위 안의 근거만 인용한다.
                mine=[dict(row,fact_refs=[payload['facts'][0]['id']],criterion_refs=[payload['criteria'][0]['id']])
                      for row in issues if row['area_refs'][0]==assignment['area_id']]
                if not mine:
                    mine=[{'area_refs':[assignment['area_id']],'fact_refs':[payload['facts'][0]['id']],
                           'criterion_refs':[payload['criteria'][0]['id']],'goal_refs':[],
                           'scope':'합성 '+assignment['area_id']+' 기본 범위','reason':'영역 계획 단위의 합성 응답이다.'}]
                return json.dumps({'issues':mine},ensure_ascii=False),{'test_only':True,'role':role}
            return await super().ask(role,message,**kwargs)
    engine=Pipeline(Planned(),tmp_path/'run')
    planned=asyncio.run(engine.plan_analysis(data))
    plan=planned['context']['analysis_plan']['issues']
    scopes=[row['scope'] for row in plan]
    assert issues[0]['scope'] in scopes and issues[2]['scope'] in scopes
    assert len(plan)>=len(issues) and len(set(scopes))==len(scopes)
    assert any(row['area_refs'][0]=='network' and 'ai' in row['area_refs'] for row in plan)
    assert len(planned['allocated_ids']['actions'])==len(plan)
    assert len(planned['allocated_ids']['directions'])==len(plan)
    assert len(planned['allocated_ids']['diagnoses'])==len(plan)
    assert planned['facts']==data['facts'] and planned['judgments']==data['judgments']
    plan=planned['context']['analysis_plan']['issues']
    assert len(planned['allocated_ids']['observations'])>=len(plan)
    for issue in plan:
        assert issue['observation_id'] in planned['allocated_ids']['observations']
        assert issue['lead_area'] == issue['area_refs'][0]
    assert len({issue['observation_id'] for issue in plan})==len(plan)


def test_planned_observations_and_relevant_evidence_reach_lead_area_prompt(tmp_path):
    """분석 계획의 가변 슬롯과 해당 근거는 실제 작성 호출 입력까지 연결된다."""
    data=asis_analysis.analyze(synthetic_input(tmp_path))['input']
    baseline=asis_analysis.analyze(data)
    issue={'id':'PLAN_P0001','lead_area':'network','observation_id':'O_P0001',
        'direction_id':'D_P0001','diagnosis_id':'J_P0001','action_id':'A_P0001',
        'area_refs':['network','ai'],'fact_refs':['F_T_subnets'],'criterion_refs':['WAF-AI-SE4'],
        'goal_refs':[],'scope':'합성 서브넷 정책 범위','reason':'수집 범위와 실효 통제는 구분한다.'}
    data['context']['analysis_plan']={'schema':'asis-issue-plan/1','issues':[issue]}
    data['allocated_ids']['observations'].append('O_P0001')
    engine=Pipeline(SyntheticRuntime(),tmp_path/'run')
    scoped=engine.area_input(data,baseline['stages'][0]['areas'][0],data)
    assert scoped['stage_data']['planned_observation_slots']==[issue]
    assert 'F_T_subnets' in scoped['context']['model_scope']['inline_fact_refs']
    assert scoped['context']['analysis_plan']['issues']==[issue]
    assert data['context']['analysis_plan']['issues']==[issue]
    _,prompt=engine.render_prompt(1,scoped)
    transmitted=json.loads(prompt.rsplit('\n입력:\n',1)[-1])
    assert transmitted['stage_data']['planned_observation_slots'][0]['observation_id']=='O_P0001'
    assert next(f['value'] for f in transmitted['facts'] if f['id']=='F_T_subnets')==[]
