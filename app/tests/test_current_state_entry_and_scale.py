"""현재 현황만의 진입 경로와 가변 규모 원천의 무손실 동결을 검증한다. 실제 모델·실포털 시험은 아니다."""
import asyncio
from copy import deepcopy
import inspect
import json
from types import SimpleNamespace

from openpyxl import load_workbook
import pytest

import asis_analysis
import asis_model
from asis_pipeline import Pipeline
from assessment_flow_artifacts import freeze_current_state, consume_current_state


def input_data(tmp_path, count):
    source = tmp_path/'Source';source.mkdir()
    resources = [{'id':f'/subscriptions/unit/resourceGroups/unit/providers/Microsoft.Storage/storageAccounts/unit{i}',
                  'name':f'unit{i}', 'type':'Microsoft.Storage/storageAccounts', 'location':'koreacentral'}
                 for i in range(count)]
    topology = {'resources':resources, 'verification':{'resourceCount':{
        'complete':True,'collected':len(resources),'uniqueIds':len(resources)}}}
    (source/'topology.json').write_text(json.dumps(topology),encoding='utf-8')
    (source/'aicaf-evidence.json').write_text(json.dumps({'sections':{}}),encoding='utf-8')
    a = {'id':619997,'customer':'규모 검증용 합성 자료','title':'실제 고객 자료 아님',
         'track':'new','infra_case_id':'unit-variable-size'}
    data = asis_model.normalize(source,a,[],'unit-current-state')
    data['context']['report_kind'] = 'current_state'
    return data, resources


@pytest.mark.parametrize('count',[0,17,1500])
def test_variable_inventory_is_frozen_and_restored_without_sampling_or_zero_substitution(tmp_path,count):
    data,resources = input_data(tmp_path,count)
    result = asis_analysis.analyze(data)
    result['stages'] = result['stages'][:1]
    result['schema'] = 'assessment-current-state-analysis/1'
    path = tmp_path/'Current-State.xlsx'
    receipt = freeze_current_state(result,path)
    assert consume_current_state(path,receipt) == result
    book = load_workbook(path,read_only=True,data_only=False)
    rows = list(book['리소스전체현황'].iter_rows(values_only=True))
    headers = rows[0]
    if resources:
        id_index = headers.index('id')
        assert {row[id_index] for row in rows[1:]} == {r['id'] for r in resources}
        assert len(rows)-1 == len(resources)
    else:
        text = '\n'.join(str(value) for row in rows for value in row if value is not None)
        assert '수집 범위 확인' in text
        assert '미수집·수집 실패·범위 내 부재는 수집현황 원장에서 구분' in text
    book.close()


def test_current_state_entry_never_asks_for_customer_goal_interpretation_or_integrated_stages(tmp_path):
    data,resources = input_data(tmp_path,1)
    pipe = Pipeline(SimpleNamespace(),tmp_path/'pipeline')
    calls = []
    async def direct_task(_data,key,operation):
        calls.append(key)
        value = operation()
        return await value if inspect.isawaitable(value) else value
    async def forbidden_interpretation(_data):
        raise AssertionError('고객 진술이 없는데 실제 고객 목표를 추정해서는 안 된다.')
    async def plan(base):
        return base
    async def compose(base,baseline,original,**kwargs):
        assert kwargs['current_only'] is True
        assert base.get('goals') == []
        assert not any(row['id'].startswith('F_IV_') for row in base['facts'])
        calls.append('current-only-compose')
        return base,[deepcopy(baseline['stages'][0])]
    pipe.run_task = direct_task
    pipe.interpret_input = forbidden_interpretation
    pipe.plan_analysis = plan
    pipe.compose_stages = compose
    result = asyncio.run(pipe.analyze_current_state(data))
    assert len(result['stages']) == 1 and result['stages'][0]['stage'] == 'area'
    assert 'current-only-compose' in calls
    assert result['input']['context']['report_kind'] == 'current_state'
    assert result['input'].get('goals') == []
