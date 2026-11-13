"""다른 규모·구성의 프로젝트에서도 항목 입력 규칙이 성립하는지 보는 합성 스트레스 시험.

평가 24 한 건으로만 정한 임계값(큰 사실 2만 자, 미확인 7만 자 등)이 다른 프로젝트에서 무너지지 않는지 본다.
합성 변형은 '실제 다른 프로젝트'의 증거가 아니다. 이 시험이 보증하는 것은 다음 세 가지뿐이다.
  1) 항목 전송 입력은 사실 수·미확인 수가 늘어도 정본 입력 크기에 비례해 커지지 않는다(인용한 것만 보낸다).
  2) 슬롯·방향·진단이 인용한 사실·기준은 어떤 규모에서도 전송에서 빠지지 않는다(누락 0).
  3) 예산을 넘는 항목은 사전 점검이 모델 호출 전에 찾아낸다.
"""
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_input_budget as budget
import asis_model_view as mv
from asis_item_agents import item_input
from asis_pipeline import Pipeline
from test_asis_model_view import heavy_data
from test_asis_parallel_synthesis import DirectionRuntime


_COUNTER = [0]


def scaled(tmp_path, extra_facts, rows_per_fact=0, unresolved=0, areas_obs=0):
    """heavy_data를 기반으로 사실·미확인·관측을 늘린 입력과 그 입력에서 만든 항목 1개의 scoped를 돌려준다."""
    _COUNTER[0] += 1
    sub = tmp_path / ('case-%d' % _COUNTER[0])
    sub.mkdir()
    data, plan = heavy_data(sub, extra_facts=extra_facts)
    areas = data['stage_data']['approved_areas']
    if rows_per_fact:
        for f in data['facts'][:5]:
            f['value'] = [{'row': i, 'blob': 'x' * 200} for i in range(rows_per_fact)]
    for i in range(unresolved):
        areas['unresolved'].append({'topic': 'u%d' % i, 'reason': 'r' * 300, 'required_evidence': 'e' * 100,
                                    'fact_refs': [data['facts'][i % len(data['facts'])]['id']]})
    for area in areas['areas']:
        for j in range(areas_obs):
            area['observations'].append({'id': 'O_X%d_%d' % (len(area['observations']), j), 'text': 't' * 400, 'fact_refs': [], 'criterion_refs': []})
    direction = {'id': 'D_X1', 'title': '방향', 'text': 't', 'customer_impact': 'i', 'change_direction': 'c',
                 'area_implication_refs': [areas['areas'][0]['implication']['id']],
                 'fact_refs': list(areas['areas'][0]['implication']['fact_refs'])[:1],
                 'criterion_refs': list(areas['areas'][0]['implication']['criterion_refs'])[:1], 'goal_refs': []}
    data['stage_data'] = {'mode': 'actions', 'approved_areas': areas,
                          'approved_directions': {'stage': 'synthesis', 'run': deepcopy(data['run']), 'directions': [direction], 'edges': [],
                                                  'unresolved': deepcopy(areas['unresolved'])}}
    slot = {'id': 'J_ITEM_0001', 'scope': '진단', 'reason': 'r', 'issue_refs': ['PLAN_P0001'], 'direction_refs': ['D_X1'],
            'fact_refs': direction['fact_refs'][:1], 'criterion_refs': direction['criterion_refs'][:1], 'goal_refs': []}
    return data, item_input(data, 'diagnosis', slot)


def sent_of(tmp_path, scoped):
    pipe = Pipeline(DirectionRuntime({'directions': []}), tmp_path / 'run')
    prompt = pipe.render_prompt(3, scoped)[1]
    return len(prompt.encode('utf-8')), json.loads(prompt[prompt.index('\n입력:\n') + 5:])


@pytest.mark.parametrize('extra,rows,unres,obs', [(40, 0, 0, 0), (400, 0, 0, 0), (40, 300, 0, 0), (40, 0, 800, 0), (40, 0, 0, 40), (400, 300, 800, 40)])
def test_the_item_input_does_not_grow_with_the_project_when_the_item_cites_the_same_things(tmp_path, extra, rows, unres, obs):
    base_data, base_scoped = scaled(tmp_path, 40)
    base_size, _ = sent_of(tmp_path, base_scoped)
    data, scoped = scaled(tmp_path, extra, rows, unres, obs)
    size, _ = sent_of(tmp_path, scoped)
    canonical = len(json.dumps(scoped, ensure_ascii=False).encode('utf-8'))
    assert size < canonical * 0.5, 'the transport view must stay far below the canonical input (sent %d, canonical %d)' % (size, canonical)
    assert size < base_size * 2.5 + 60_000, 'a bigger project must not make the same item input several times bigger (base %d, now %d)' % (base_size, size)


@pytest.mark.parametrize('extra,rows,unres,obs', [(40, 0, 0, 0), (400, 300, 800, 40)])
def test_cited_facts_and_criteria_are_never_dropped_at_any_scale(tmp_path, extra, rows, unres, obs):
    data, scoped = scaled(tmp_path, extra, rows, unres, obs)
    _, body = sent_of(tmp_path, scoped)
    slot = scoped['stage_data']['item_assignment']['slot']
    need_f = set(slot['fact_refs'])
    need_c = set(slot['criterion_refs'])
    for row in scoped['stage_data']['approved_directions']['directions']:
        need_f |= set(row['fact_refs'])
        need_c |= set(row['criterion_refs'])
    known_f = {r['id'] for r in scoped['facts']}
    known_c = {r['id'] for r in scoped['criteria']}
    assert (need_f & known_f) <= {r['id'] for r in body['facts']}
    assert (need_c & known_c) <= {r['id'] for r in body['criteria']}


def test_a_large_cited_fact_is_kept_in_full_for_the_item_that_cites_it_directly(tmp_path):
    """슬롯이 직접 인용한 사실은 크기와 무관하게 전문을 보낸다. 줄이면 근거 인용이 끊긴다(4.5만 자 초과분은 전송 마지막 단계가 처리)."""
    data, scoped = scaled(tmp_path, 40, rows_per_fact=300)
    slot = scoped['stage_data']['item_assignment']['slot']
    _, body = sent_of(tmp_path, scoped)
    canonical = {f['id']: f['value'] for f in scoped['facts']}
    assert slot['fact_refs'], 'the fixture must have a directly cited fact'
    for ref in slot['fact_refs']:
        row = next(r for r in body['facts'] if r['id'] == ref)
        assert not (isinstance(row['value'], dict) and row['value'].get('not_inlined')), ref + ' is directly cited and must not be withheld'
        assert row['value'] == canonical[ref], ref + ' must arrive unchanged'
        assert len(json.dumps(row['value'], ensure_ascii=False)) > mv.LARGE_FACT_CHARS, 'the fact is larger than the large-fact threshold'


def large_fact_project(tmp_path):
    """사실 3개를 모두 큰 값(2만 자 초과)으로 만들고, 슬롯 직접 인용·방향만 인용·아무도 인용하지 않음으로 나눈다."""
    data, scoped = scaled(tmp_path, 40)
    big = lambda: [{'row': i, 'blob': 'x' * 200} for i in range(300)]
    ids = ['F_T_subnets', 'F_T_resources', 'F_IV_Q1_summary']
    for f in scoped['facts']:
        if f['id'] in ids:
            f['value'] = big()
    slot = scoped['stage_data']['item_assignment']['slot']
    slot['fact_refs'] = ['F_T_subnets']
    scoped['stage_data']['approved_directions']['directions'][0]['fact_refs'] = ['F_T_resources']
    return scoped


def test_a_large_fact_cited_only_by_the_direction_is_withheld_but_listed(tmp_path):
    """방향만 인용한 큰 사실은 본문을 생략하되 존재(크기)는 남긴다. 슬롯이 직접 인용한 큰 사실은 전문을 보낸다."""
    scoped = large_fact_project(tmp_path)
    kind, view = mv.build(3, scoped)
    rows = {r['id']: r for r in view['facts']}
    assert 'F_IV_Q1_summary' not in rows, 'a fact nobody cites is not sent at all'
    only_direction = rows['F_T_resources']['value']
    assert isinstance(only_direction, dict) and only_direction['not_inlined'] is True and only_direction['observed_chars'] > mv.LARGE_FACT_CHARS
    direct = rows['F_T_subnets']['value']
    assert isinstance(direct, list) and len(direct) == 300, 'a directly cited large fact arrives in full'


def test_the_pre_flight_check_finds_a_project_that_cannot_fit_before_any_model_call(tmp_path, monkeypatch):
    data, scoped = scaled(tmp_path, 400, 300, 800, 40)
    pipe = Pipeline(DirectionRuntime({'directions': []}), tmp_path / 'run')
    row = budget.measure_item(pipe, scoped)
    monkeypatch.setattr(budget, 'LONG_THRESHOLD', max(1, row['tokens_low'] // 2))
    row2 = budget.measure_item(pipe, scoped)
    assert row2['tier'] == 'long'
    out = budget.summarize([row2])
    assert out['over_budget'] and out['over_budget'][0]['over_by_low'] > 0


def test_a_project_with_no_interviews_and_few_facts_still_produces_a_valid_small_input(tmp_path):
    data, scoped = scaled(tmp_path, 0)
    size, body = sent_of(tmp_path, scoped)
    assert size > 0 and body['stage_data']['item_assignment']['item_id'] == 'J_ITEM_0001'
