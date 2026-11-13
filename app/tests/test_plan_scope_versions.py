"""신규 프로젝트에서도 계획 혼용·전역 키 전파·중복 재생이 반복되지 않는지 검증한다."""
from copy import deepcopy
from types import SimpleNamespace
import json

import pytest
import asis_action_split as split
import asis_item_agents as items
from asis_model import digest


def plan(count=3):
    return {'diagnoses':[{'id':'J_ITEM_0001','issue_refs':['I1']},{'id':'J_ITEM_0002','issue_refs':['I2']}],
        'actions':[{'id':f'A_ITEM_{i:04d}','scope':'범용 계획 '+str(i),'reason':'시험',
                    'issue_refs':['I1','I2'] if i==1 else [f'I{i+2}'],
                    'direction_refs':['D1','D2'] if i==1 else [f'D{i+2}'],
                    'diagnosis_refs':['J_ITEM_0001','J_ITEM_0002'],'fact_refs':['F1'],
                    'criterion_refs':['C1'],'goal_refs':[]} for i in range(1,count+1)]}


AREA={'D1':'ai','D2':'cost'}
OWNS={'D1':['I1'],'D2':['I2']}


def test_split_record_is_bound_to_the_exact_before_and_after_plan():
    original=plan();after,record=split.split_action_slot(original,'A_ITEM_0001',direction_area=AREA,issues_of_direction=OWNS)
    assert record.get('plan_before_sha256')==digest(original)
    assert record.get('plan_after_sha256')==digest(after)


def test_old_split_cannot_touch_a_new_maximum_size_plan_with_reused_ids():
    old=plan();_,record=split.split_action_slot(old,'A_ITEM_0001',direction_area=AREA,issues_of_direction=OWNS)
    new=plan(30);new['actions'][0]['scope']='ID는 같지만 다른 계획'
    before=deepcopy(new)
    with pytest.raises(ValueError,match='split_plan_binding'):
        items.replay_splits(new,[record],AREA,OWNS)
    assert new==before


def test_replaying_an_already_applied_split_is_idempotent():
    original=plan();after,record=split.split_action_slot(original,'A_ITEM_0001',direction_area=AREA,issues_of_direction=OWNS)
    assert items.replay_splits(after,[record],AREA,OWNS)==after


def test_unbound_or_foreign_split_journal_is_preserved_but_not_replayed(tmp_path):
    class Pipe:
        folder=tmp_path
        def save(self,path,value):
            path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value),encoding='utf-8')
        def event(self,*args,**kwargs):pass
    pipe=Pipe();path=tmp_path/'checkpoints/actions-merge/action-split.json'
    pipe.save(path,{'schema':'asis-action-split-set/1','splits':[{'original_id':'A_ITEM_0001','new_id':'A_ITEM_0004'}]})
    current=plan(30)
    prepared,records,fresh=items.split_if_needed(pipe,{},current)
    assert prepared==current and records==[] and fresh==0
    histories=list(path.parent.glob('superseded-splits/*.json'))
    assert histories and json.loads(histories[0].read_text(encoding='utf-8'))['splits'][0]['new_id']=='A_ITEM_0004'


def data():
    return {'run':{'assessment_id':73,'source_revision':'unit'},
        'context':{'analysis_plan':{'issues':[{'id':'I1','scope':'AI 기반','reason':'확인된 범위'},
                                            {'id':'I2','scope':'비용 기준','reason':'확인된 범위'}]}},
        'stage_data':{'approved_directions':{'directions':[{'id':'D1','title':'AI 방향'},{'id':'D2','title':'비용 방향'}]}}}


def test_area_plan_cache_uses_its_actual_view_not_other_area_data():
    first=data();second=deepcopy(first);second['stage_data']['approved_directions']['directions'][1]['title']='비용 방향 변경'
    links={'I1':['D1'],'I2':['D2']}
    assert items.area_plan_binding(first,'ai',['I1'],links,binding='same-policy')==items.area_plan_binding(second,'ai',['I1'],links,binding='same-policy')
    assert items.area_plan_binding(first,'cost',['I2'],links,binding='same-policy')!=items.area_plan_binding(second,'cost',['I2'],links,binding='same-policy')
