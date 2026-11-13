"""현황보고서의 핵심 편집과 전체 원장 보존을 구분하는 명시적 합성 시험이다."""
from copy import deepcopy
import importlib
import importlib.util
import pytest

from test_current_state_artifacts import result as original_result


def source(tmp_path):
    value=original_result(tmp_path)
    area=value['stages'][0]['areas'][0]
    facts=area['observations'][0]['fact_refs']
    criteria=area['observations'][0]['criterion_refs']
    area['observations']=[
        {'id':'O_UNIT_1','text':'합성 계정은 사설 접근으로 구성되어 있다. 실제 업무 호출은 미확인이다.','fact_refs':facts,'criterion_refs':criteria,'goal_refs':[],'judgment_refs':[]},
        {'id':'O_UNIT_2','text':'합성 연결의 진단 설정은 확인되지만 로그 보존 기간은 미확인이다.','fact_refs':facts,'criterion_refs':criteria,'goal_refs':[],'judgment_refs':[]},
        {'id':'O_UNIT_3','text':'합성 인벤토리의 일반 필드와 반복 설명은 상세 원장에 남긴다.','fact_refs':facts,'criterion_refs':criteria,'goal_refs':[],'judgment_refs':[]}]
    value['stages'][0]['areas']=[area]
    return value


def proposal(value):
    area=value['stages'][0]['areas'][0]
    return {'area_id':area['area_id'],'headline':'구성은 확인되나 실제 사용과 보존 범위는 미확인이다.',
        'groups':[{'title':'사설 접근 구성과 관측 공백','current_state':'합성 계정의 사설 접근과 연결의 진단 설정이 확인된다.',
            'technical_meaning':'구성의 존재와 실제 운영 효과를 구분해야 한다.',
            'limitation':'실제 업무 호출과 로그 보존 기간은 미확인이다.',
            'observation_refs':['O_UNIT_1','O_UNIT_2'],
            'source_quotes':[{'observation_ref':'O_UNIT_1','quote':'사설 접근으로 구성되어 있다.'},
                             {'observation_ref':'O_UNIT_2','quote':'로그 보존 기간은 미확인이다.'}]}],
        'appendix_only':[{'observation_ref':'O_UNIT_3','reason':'반복되는 인벤토리 상세는 전체 Excel 원장에 유지한다.'}]}


def module():
    assert importlib.util.find_spec('assessment_current_state_presentation') is not None,'핵심 편집과 상세 원장을 분리하는 공통 생성 계약이 없다.'
    return importlib.import_module('assessment_current_state_presentation')


def test_editorial_selection_binds_every_observation_without_rewriting_original_analysis(tmp_path):
    value=source(tmp_path);before=deepcopy(value);draft=proposal(value);m=module()
    view=m.editorial_input(value,value['stages'][0]['areas'][0]['area_id'],slide_budget=12)
    accepted=m.validate_area(view,draft)
    assert value==before
    assert accepted['groups'][0]['observation_refs']==['O_UNIT_1','O_UNIT_2']
    assert [r['observation_ref'] for r in accepted['appendix_only']]==['O_UNIT_3']
    assert accepted['groups'][0]['fact_refs']==sorted(set(value['stages'][0]['areas'][0]['observations'][0]['fact_refs']))
    assert m.MAX_SLIDES==150


def projected(value):
    from asis_model import digest
    m=module();area_id=value['stages'][0]['areas'][0]['area_id']
    candidate=m.validate_area(m.editorial_input(value,area_id,slide_budget=12),proposal(value))
    review={'verdict':'pass','content_checked':True,'render_checked':False,'defects':[],
            'candidate_sha256':digest(candidate),'execution_classification':'synthetic_unit_test_only'}
    value['presentation']=m.bind_projection(value,[candidate],{area_id:review})
    return value


@pytest.mark.parametrize('change,error',[
    ('unknown','unknown_observation'),('missing','incomplete_coverage'),
    ('duplicate','duplicate_coverage'),('fabricated_quote','source_quote_not_found'),
    ('new_number','new_numeric_claim'),('oversize','over_layout_capacity')])
def test_editorial_rejects_silent_omission_invented_evidence_and_over_capacity(tmp_path,change,error):
    value=source(tmp_path);draft=proposal(value);m=module()
    if change=='unknown':draft['groups'][0]['observation_refs'][0]='O_UNKNOWN'
    if change=='missing':draft['appendix_only']=[]
    if change=='duplicate':draft['appendix_only'].append({'observation_ref':'O_UNIT_1','reason':'반복'})
    if change=='fabricated_quote':draft['groups'][0]['source_quotes'][0]['quote']='원문에 없는 설정'
    if change=='new_number':draft['groups'][0]['current_state']='합성 계정은 987654개다.'
    if change=='oversize':draft['groups'][0]['current_state']='요약하지 않은 과도한 본문이다. '*300
    with pytest.raises(ValueError,match=error):
        m.validate_area(m.editorial_input(value,value['stages'][0]['areas'][0]['area_id'],slide_budget=12),draft)


def test_projected_ppt_shows_selected_core_not_raw_appendix_and_preserves_full_excel(tmp_path):
    from assessment_flow_artifacts import freeze_current_state,consume_current_state,presentation_current_state,verify_current_state
    from pptx import Presentation
    value=projected(source(tmp_path));xlsx=tmp_path/'full-register.xlsx';pptx=tmp_path/'core.pptx'
    freeze_current_state(value,xlsx);assert consume_current_state(xlsx)==value
    pages=presentation_current_state(value,pptx)
    body=''.join(s.text for slide in Presentation(pptx).slides for s in slide.shapes if s.has_text_frame)
    group=value['presentation']['areas'][0]['groups'][0]
    assert group['title'] in body
    assert value['stages'][0]['areas'][0]['observations'][2]['text'] not in body
    assert '사설 접근과 연결의 진단 설정이 확인된다.' in body
    assert pages<=150
    proof=verify_current_state(value,pptx,xlsx)
    assert proof['full_excel_observations']==3 and proof['selected_observations']==2


def test_projection_rejects_changed_original_analysis_and_unreviewed_selection(tmp_path):
    value=projected(source(tmp_path));m=module();m.validate_projection(value)
    changed=deepcopy(value);changed['stages'][0]['areas'][0]['observations'][0]['text']+=' 바뀐 원문'
    with pytest.raises(ValueError,match='source_analysis_changed'):m.validate_projection(changed)
    changed=deepcopy(value);area=changed['presentation']['areas'][0]['area_id']
    changed['presentation']['reviews'][area]['content_checked']=False
    with pytest.raises(ValueError,match='review_not_passed'):m.validate_projection(changed)


def test_editorial_is_part_of_the_portable_canonical_tree():
    from pathlib import Path
    from asis_runtime import implementation_files
    root=Path(__file__).resolve().parents[1]
    assert 'assessment_current_state_presentation.py' in {p.name for p in implementation_files(root)}


def test_review_must_point_to_a_real_candidate_quote(tmp_path):
    value=source(tmp_path);m=module()
    candidate=m.validate_area(m.editorial_input(value,value['stages'][0]['areas'][0]['area_id'],slide_budget=4),proposal(value))
    review={'verdict':'revise','content_checked':True,'render_checked':False,
        'defects':[{'where':'/groups/0/current_state','quote':'없는 문장','why':'인용 불일치','fix':'원문 대조'}]}
    with pytest.raises(ValueError,match='quote_not_found'):
        m._review_contract(candidate,review)


def test_review_cannot_pass_with_defects_or_claim_a_render_check(tmp_path):
    value=source(tmp_path);m=module()
    candidate=m.validate_area(m.editorial_input(value,value['stages'][0]['areas'][0]['area_id'],slide_budget=4),proposal(value))
    for review in ({'verdict':'pass','content_checked':True,'render_checked':True,'defects':[]},
        {'verdict':'pass','content_checked':True,'render_checked':False,'defects':[{}]}):
        with pytest.raises(ValueError):m._review_contract(candidate,review)


def test_prepare_editorial_uses_fixed_pairs_and_reuses_only_bound_acceptance(tmp_path):
    import asyncio
    import json
    from asis_pipeline import Pipeline
    from asis_tasks import task
    from current_state_editorial_stub import answer
    value=source(tmp_path);area_id=value['stages'][0]['areas'][0]['area_id']
    class FakeRuntime:
        def __init__(self):self.calls=[]
        async def ask(self,role,message,*,session_id,instructions):
            payload=json.loads(message.split('\n입력:\n',1)[1]);self.calls.append((role,session_id,payload))
            response=answer(payload)
            assert response is not None
            if role=='author' and payload.get('accumulated_defects'):
                response['groups'][0]['current_state']='관측 구성과 실제 사용 범위를 분리한다.'
            return json.dumps(response,ensure_ascii=False),{'execution_classification':'synthetic_unit_test_only','model_call_performed':False}
    async def scenario():
        runtime=FakeRuntime();pipe=Pipeline(runtime,tmp_path/'editorial-run',rounds=3);pipe.report_kind='current_state'
        manager=pipe.task_manager(value['input']);key='area-'+area_id
        manager.register([task(key,'합성 승인 원문',executor='code')])
        await manager.perform(key,lambda:{'synthetic_source_approved':True})
        first=await module().prepare_editorial(pipe,value)
        assert len(runtime.calls)==2
        assert [role for role,_,_ in runtime.calls]==['author','content_review']
        assert runtime.calls[0][1]!=runtime.calls[1][1]
        assert runtime.calls[1][2]['source']['area_source']==value['stages'][0]['areas'][0]
        second=await module().prepare_editorial(pipe,value)
        assert len(runtime.calls)==2 and first==second
        assert first['stages']==value['stages']
        module().validate_projection(first)
        target=deepcopy(first)
        target['editorial_revision_context']={area_id:[{'where':'/presentation/areas/0/groups/0/current_state',
            'quote':'구성의 존재','why':'합성 재편집 경로 시험','fix':'관측과 실제 사용을 분리해 표현한다.'}]}
        third=await module().prepare_editorial(pipe,target)
        assert len(runtime.calls)==4,'시각 검토 지적은 이미 통과한 편집 캐시를 다시 돌려주면 안 된다.'
        assert runtime.calls[0][1]==runtime.calls[2][1] and runtime.calls[1][1]==runtime.calls[3][1]
        assert runtime.calls[2][2]['prior_candidate']
        assert runtime.calls[2][2]['accumulated_defects'][0]['quote']=='구성의 존재'
        assert third['presentation']['areas'][0]['groups'][0]['current_state']=='관측 구성과 실제 사용 범위를 분리한다.'
        assert third['stages']==first['stages']
    asyncio.run(scenario())


def test_legacy_current_renderer_also_refuses_to_write_an_over_limit_deck(tmp_path,monkeypatch):
    from assessment_flow_artifacts import presentation_current_state
    m=module();monkeypatch.setattr(m,'MAX_SLIDES',3)
    with pytest.raises(ValueError,match='slide_maximum_exceeded'):
        presentation_current_state(source(tmp_path),tmp_path/'over-limit.pptx')
    assert not (tmp_path/'over-limit.pptx').exists()


def test_current_state_service_really_calls_editorial_before_building_native_files():
    import ast
    import inspect
    import asis_service
    tree=ast.parse(inspect.getsource(asis_service.execute))
    calls=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='prepare_editorial']
    assert len(calls)==1,'편집 계약이 있어도 실제 운영 진입점에서 호출하지 않으면 원장 나열이 반복된다.'


def test_native_rounds_never_overwrite_the_preserved_533_slide_candidate(tmp_path):
    import asis_service
    old=tmp_path/'artifact-rounds'/'round-001';old.mkdir(parents=True)
    marker=old/'preserved-original.txt';marker.write_text('기존 원본 보존',encoding='utf-8')
    next_round=getattr(asis_service,'next_native_round',None)
    assert callable(next_round),'같은 개정의 재편집이 기존 533장 원본 폴더를 덮어쓰지 않아야 한다.'
    assert next_round(tmp_path)==2
    assert marker.read_text(encoding='utf-8')=='기존 원본 보존'
    (tmp_path/'artifact-rounds'/'round-009').mkdir()
    assert next_round(tmp_path)==10


def test_real_visual_model_view_keeps_full_approved_projection_and_selection_register(tmp_path):
    from asis_model_view import build
    value=projected(source(tmp_path));data=deepcopy(value['input'])
    group=value['presentation']['areas'][0]['groups'][0]
    data['stage_data']={'mode':'visual-r2-001','draft':{'pages':[{'page':1,'family':'current_core',
        'fact_refs':group['fact_refs'],'observation_ids':group['observation_refs']}],
        'stages':deepcopy(value['stages']),'presentation':deepcopy(value['presentation'])}}
    original=deepcopy(data);kind,view=build(5,data)
    assert kind=='visual_review'
    assert view['stage_data']['draft']['presentation']==value['presentation']
    assert data==original


def test_visual_defects_route_only_to_their_real_editorial_area(tmp_path):
    value=projected(source(tmp_path));m=module();area_id=value['presentation']['areas'][0]['area_id']
    defect={'where':'/presentation/areas/0/groups/0/current_state','quote':'합성 계정','why':'합성 지적','fix':'원문에 맞게 편집'}
    assert m.editorial_defects_by_area(value,[defect])=={area_id:[defect]}
    for bad in ({**defect,'quote':'원문에 없는 인용'},
                {**defect,'where':'/presentation/areas/-1/groups/0/current_state'},
                {**defect,'where':'/presentation/areas/99/groups/0/current_state'}):
        with pytest.raises(ValueError):m.editorial_defects_by_area(value,[bad])
