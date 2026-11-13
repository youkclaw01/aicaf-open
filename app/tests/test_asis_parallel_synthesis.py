"""가변 종합 방향을 범위 계획 후 독립 작성·검토와 코드 병합으로 실행한다."""
from copy import deepcopy
from pathlib import Path
import asyncio
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_analysis
from asis_pipeline import Pipeline
from test_asis_connection import synthetic_input
from asis_model import digest


def synthesis_fixture(tmp_path):
    seed = asis_analysis.analyze(synthetic_input(tmp_path))
    data = deepcopy(seed['input'])
    areas = deepcopy(seed['stages'][0])
    areas['areas'] = areas['areas'][:3]
    issues = []
    for i, area in enumerate(areas['areas'][:3]):
        implication = area['implication']
        issues.append({'id': 'PLAN_P'+str(i+1).zfill(4), 'direction_id': 'D_P'+str(i+1).zfill(4),
                       'lead_area': area['area_id'], 'area_refs': [area['area_id']],
                       'observation_id': 'O_PLAN'+str(i+1), 'scope': '합성 결정 범위 '+str(i+1),
                       'reason': '원천과 기준의 합성 관계 시험',
                       **{key: deepcopy(implication[key]) for key in ('fact_refs','criterion_refs','goal_refs')}})
    data['context']['analysis_plan'] = {'schema': 'asis-issue-plan/1', 'issues': issues}
    data['allocated_ids']['directions'] = [row['direction_id'] for row in issues]
    data['stage_data'] = {'mode': 'synthesis', 'approved_areas': deepcopy(areas),
                          'direction_slots': data['allocated_ids']['directions']}
    slots = []
    for positions in ([0,1], [2]):
        refs = {key: list(dict.fromkeys(ref for i in positions for ref in issues[i][key]))
                for key in ('fact_refs','criterion_refs','goal_refs')}
        slots.append({'scope': '합성 종합 '+str(len(slots)+1), 'reason': '상호 관련한 판단 범위를 합성한다.',
                      'issue_refs': [issues[i]['id'] for i in positions],
                      'area_implication_refs': [areas['areas'][i]['implication']['id'] for i in positions], **refs})
    return data, {'directions': slots}


class DirectionRuntime:
    def __init__(self, plan):
        self.plan = plan
        self.calls = []
        self.active = self.peak = 0

    async def ask(self, role, message, **kwargs):
        payload = json.loads(message.rsplit('\n입력:\n',1)[-1])
        stage = payload['stage_data']
        self.calls.append({'role': role, 'input': deepcopy(payload), **kwargs})
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(.01)
        try:
            if role == 'interpret' and stage.get('mode') == 'synthesis_second_pass_grouping':
                return json.dumps({'groups': []}), {'test_only': True, 'role': role}
            if role == 'interpret' and stage.get('mode') == 'synthesis_cross_area_grouping':
                # 통합 계획 호출: 시험 기본 응답은 묶을 후보가 없다는 답이다. 묶음 동작은 test_asis_cross_area_grouping이 직접 시험한다.
                value = {'groups': []}
            elif role == 'interpret' and stage.get('plan_assignment'):
                # 영역 소단위 호출: 전체 계획 중 해당 영역이 주도하는 이슈에 연결된 방향만 그 영역 범위로 반환한다.
                issues = {row['id'] for row in payload['context']['analysis_plan']['issues']}
                implications = {row['implication']['id'] for row in stage['approved_areas']['areas']}
                rows = []
                for slot in self.plan['directions']:
                    mine = [ref for ref in slot['issue_refs'] if ref in issues]
                    if mine:
                        rows.append({**deepcopy(slot),'issue_refs':mine,
                            'area_implication_refs':[ref for ref in slot['area_implication_refs'] if ref in implications] or sorted(implications)})
                value = {'directions': rows}
            elif role == 'interpret':
                value = deepcopy(self.plan)
            elif role in ('author', 'direction_author'):
                assignment = stage['direction_assignment']
                slot = assignment['slot']
                row = {key: deepcopy(slot[key]) for key in ('id','area_implication_refs','fact_refs','criterion_refs','goal_refs')}
                row.update(title=slot['scope'], text='합성 원천에 따른 판단이다.',
                           customer_impact='합성 고객 영향', change_direction='합성 검증 방향')
                value = {'stage': 'synthesis', 'run': payload['run'], 'directions': [row], 'edges': [], 'unresolved': []}
            else:
                value = {'stage': 'review', 'run': payload['run'], 'verdict': 'pass',
                         'content_checked': True, 'render_checked': False, 'defects': [], 'unresolved': []}
            return json.dumps(value,ensure_ascii=False), {'test_only':True,'role':role,'session_id':kwargs['session_id']}
        finally:
            self.active -= 1


def test_variable_synthesis_uses_independent_direction_pairs_and_code_merge(tmp_path):
    """세 이슈가 두 종합 방향으로 묶여도 전수 범위와 현재 원장·후보를 보존한다."""
    data, plan = synthesis_fixture(tmp_path)
    original = deepcopy(data)
    runtime = DirectionRuntime(plan)
    pipe = Pipeline(runtime,tmp_path/'run')
    assert hasattr(pipe, 'reviewed_synthesis'), 'parallel synthesis entrypoint is missing'
    result = asyncio.run(pipe.reviewed_synthesis(data))
    assert data == original
    assert len(result['directions']) == len(plan['directions']) == 2
    assert runtime.peak >= 2
    authors = [c for c in runtime.calls if c['role'] in ('author', 'direction_author')]
    assert len(authors)==2
    assert len({c['session_id'] for c in authors})==2
    for call in authors:
        slot = call['input']['stage_data']['direction_assignment']['slot']
        key = 'direction-'+slot['id']
        task = pipe.tasks.ledger['tasks'][key]
        assert task['status']=='completed'
        assert task['reports']['author']['candidate_sha256']==task['reports']['reviewer']['candidate_sha256']
        assert task['depends_on']==['synthesis-plan']
    assert result['directions'][0]['area_implication_refs']==plan['directions'][0]['area_implication_refs']
    assert any(c['role']=='content_review' and c['input']['stage_data'].get('draft')==result for c in runtime.calls)
    asis_analysis.contract().validate_output(data,result)
    row = pipe.tasks.ledger['tasks']['synthesis']
    assert row['status']=='completed'
    assert pipe.tasks.ledger['tasks']['synthesis']['status']=='completed'
    assert row['completion']['candidate_sha256']==digest(result)


def test_full_product_path_consumes_parallel_synthesis_instead_of_batch_author(tmp_path):
    """새 모듈 존재가 아니라 기존 compose 소비자가 실제 종합 방향별 호출을 사용한다."""
    from test_asis_connection import SyntheticRuntime
    runtime = SyntheticRuntime()
    pipe = Pipeline(runtime,tmp_path/'run')
    result = asyncio.run(pipe.analyze(synthetic_input(tmp_path)))
    authors = [call for call in runtime.calls if call['role'] in ('author', 'direction_author') and call['mode']=='synthesis']
    assert authors and all('direction_assignment' in call['input']['stage_data'] for call in authors), 'compose still batch-authors synthesis'
    assert len(authors)==len(result['stages'][1]['directions'])
    for call in authors:
        identifier=call['input']['stage_data']['direction_assignment']['item_id']
        assert pipe.tasks.ledger['tasks']['direction-'+identifier]['status']=='completed'


def test_whole_synthesis_rejection_repairs_only_affected_direction_with_same_pair(tmp_path):
    """종합 전체 검토가 한 항목을 반려해도 독립 완료 항목을 다시 작성하지 않는다."""
    from collections import Counter
    data,plan = synthesis_fixture(tmp_path)
    class RejectedOnce(DirectionRuntime):
        def __init__(self,plan):
            super().__init__(plan)
            self.whole=0
        async def ask(self,role,message,**kwargs):
            payload=json.loads(message.rsplit('\n입력:\n',1)[-1]);stage=payload['stage_data']
            if role=='content_review' and stage.get('mode')=='synthesis' and not stage.get('direction_assignment'):
                self.whole+=1
                if self.whole==1:
                    old=stage['draft']['directions'][0]['text']
                    self.calls.append({'role':role,'input':deepcopy(payload),**kwargs})
                    value={'stage':'review','run':payload['run'],'verdict':'revise','content_checked':True,
                        'render_checked':False,'defects':[{'where':'/directions/0/text','quote':old,
                        'why':'합성 범위 표현을 보완한다.','fix':'동일 사실의 합성 범위를 명시한다.','evidence_refs':[]}],'unresolved':[]}
                    return json.dumps(value,ensure_ascii=False),{'test_only':True,'role':role}
            raw,receipt=await super().ask(role,message,**kwargs)
            if role in ('author', 'direction_author') and stage.get('prior_output'):
                assert stage['prior_output']['directions'][0]['text']=='합성 원천에 따른 판단이다.'
                assert stage['accumulated_defects'][0]['where']=='/directions/0/text'
                candidate=json.loads(raw)
                candidate['directions'][0]['text']='관측 시점의 합성 원천에 따른 판단이다.'
                raw=json.dumps(candidate,ensure_ascii=False)
            return raw,receipt
    runtime=RejectedOnce(plan);pipe=Pipeline(runtime,tmp_path/'run')
    result=asyncio.run(pipe.reviewed_synthesis(data))
    authors=[c for c in runtime.calls if c['role'] in ('author', 'direction_author')]
    counts=Counter(c['input']['stage_data']['direction_assignment']['item_id'] for c in authors)
    assert sorted(counts.values())==[1,2]
    changed=result['directions'][0]['id']
    assert len({c['session_id'] for c in authors if c['input']['stage_data']['direction_assignment']['item_id']==changed})==1
    assert runtime.whole==2
    assert result['directions'][0]['text']=='관측 시점의 합성 원천에 따른 판단이다.'
