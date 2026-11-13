"""공통 전송 계층: 방향 1개의 작성·검토 입력은 그 방향이 인용한 내용만 담고, 정본·검증·재사용 키는 바뀌지 않는다.
운영 실측: 방향 D_P0001 작성 입력이 154만 자였고 인용하지 않는 사실 106개·관찰 본문·미확인 917개가 매번 실렸다."""
from copy import deepcopy
from pathlib import Path
import json, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
import asis_model_view as mv
from asis_pipeline import Pipeline
from asis_synthesis import direction_input
from test_asis_parallel_synthesis import synthesis_fixture, DirectionRuntime


def heavy_data(tmp_path, extra_facts=40):
    """방향 1개 입력을 인용하지 않는 사실·관찰·미확인·배경 원장으로 부풀린 정본."""
    data, plan = synthesis_fixture(tmp_path)
    run = data['run']
    for i in range(extra_facts):
        data['facts'].append({'id': 'F_X_%03d' % i, 'source_id': 'x.json', 'field_path': '/x/%d' % i, 'run': deepcopy(run),
                              'value': {'blob': 'x' * 3000}, 'collection_status': 'collected', 'scope_complete': True})
    data['context']['source_documents'] = {'topology': {'blob': 'y' * 50000}}
    data['context']['collection_basis'] = {'criterionMatches': [{'area': 'ai', 'criterion_id': 'C_1'}] * 50}
    data['context']['criterion_assessments'] = [{'criterion_id': 'C_1', 'area_refs': ['ai'], 'blob': 'z' * 5000}]
    for area in data['stage_data']['approved_areas']['areas']:
        area['observations'] = [{'id': 'O_%d' % i, 'text': '관찰 본문 ' * 200, 'fact_refs': [], 'criterion_refs': [], 'goal_refs': [], 'judgment_refs': []}
                                for i in range(30)]
    data['stage_data']['approved_areas']['unresolved'] = [{'topic': 't%d' % i, 'reason': 'r' * 300, 'required_evidence': 'e', 'fact_refs': ['F_X_%03d' % i]}
                                                          for i in range(extra_facts)]
    return data, plan


def first_slot(plan, data):
    slot = dict(plan['directions'][0]); slot['id'] = data['allocated_ids']['directions'][0]
    return slot


def scoped_of(tmp_path):
    data, plan = heavy_data(tmp_path)
    return data, direction_input(data, first_slot(plan, data))


def test_a_direction_author_view_carries_only_what_the_slot_and_its_implications_cite(tmp_path):
    data, scoped = scoped_of(tmp_path)
    kind, view = mv.build(2, scoped)
    assert kind == 'direction_author'
    slot = scoped['stage_data']['direction_assignment']['slot']
    sent_facts = {row['id'] for row in view['facts']}
    assert not any(i.startswith('F_X_') for i in sent_facts), 'facts nobody cited must not be sent'
    assert set(slot['fact_refs']) <= sent_facts
    for key in ('source_documents', 'collection_basis', 'criterion_assessments'):
        assert key not in view['context'], key + ' is a background ledger the item does not read'
    assert all(area['observations'] == [] for area in view['stage_data']['approved_areas']['areas']), 'observation bodies stay in the ledger'
    assert all(not any(r['topic'].startswith('t') and set(r['fact_refs']) - set(slot['fact_refs']) for r in [row]) for row in view['stage_data']['approved_areas']['unresolved']), 'only unresolved items touching this item'
    assert view['allocated_ids']['directions'] == [slot['id']]


def test_the_view_is_far_smaller_and_the_canonical_input_is_untouched(tmp_path):
    data, scoped = scoped_of(tmp_path)
    before = json.dumps(scoped, sort_keys=True)
    kind, view = mv.build(2, scoped)
    assert json.dumps(scoped, sort_keys=True) == before, 'canonical input must not change'
    assert len(json.dumps(view, ensure_ascii=False)) < len(json.dumps(scoped, ensure_ascii=False)) * 0.5


def test_a_review_view_carries_the_candidate_and_the_facts_it_cites(tmp_path):
    data, scoped = scoped_of(tmp_path)
    slot = scoped['stage_data']['direction_assignment']['slot']
    candidate = {'stage': 'synthesis', 'run': deepcopy(data['run']), 'directions': [{'id': slot['id'], 'title': 't', 'text': 'x', 'customer_impact': 'i', 'change_direction': 'c',
                 'area_implication_refs': slot['area_implication_refs'], 'fact_refs': slot['fact_refs'][:1] + ['F_X_001'], 'criterion_refs': slot['criterion_refs'],
                 'goal_refs': slot['goal_refs']}], 'edges': [], 'unresolved': []}
    scoped['stage_data'].update(draft=candidate, candidate_sha256='h', deterministic_check_result={'passed': True, 'candidate_sha256': 'h', 'defects': []})
    kind, view = mv.build(5, scoped)
    assert kind == 'direction_review'
    assert view['stage_data']['draft'] == candidate, 'the reviewer must see the candidate exactly'
    assert 'F_X_001' in {row['id'] for row in view['facts']}, 'a fact the candidate cites must be sent so the reviewer can check it'
    assert 'F_X_002' not in {row['id'] for row in view['facts']}


def test_a_large_fact_cited_only_by_an_implication_is_omitted_but_a_slot_fact_is_kept(tmp_path):
    data, scoped = scoped_of(tmp_path)
    slot = scoped['stage_data']['direction_assignment']['slot']
    big = {'id': 'F_BIG', 'source_id': 'b.json', 'field_path': '/b', 'run': deepcopy(data['run']), 'value': {'rows': ['r' * 100] * 400},
           'collection_status': 'collected', 'scope_complete': True}
    scoped['facts'].append(big)
    scoped['stage_data']['approved_areas']['areas'][0]['implication']['fact_refs'].append('F_BIG')
    kind, view = mv.build(2, scoped)
    got = next(row for row in view['facts'] if row['id'] == 'F_BIG')
    assert got['value']['not_inlined'] is True and got['value']['observed_chars'] > mv.LARGE_FACT_CHARS
    slot['fact_refs'].append('F_BIG')
    kind, view = mv.build(2, scoped)
    assert next(row for row in view['facts'] if row['id'] == 'F_BIG')['value'] == big['value'], 'a fact the slot itself cites is always sent in full'


def test_calls_that_are_not_direction_work_keep_the_existing_transport(tmp_path):
    data, plan = heavy_data(tmp_path)
    assert mv.build(1, data) == (None, data)
    assert mv.build(2, data) == (None, data), 'no direction assignment: not a direction call'
    assert mv.build(3, data) == (None, data)


def test_an_over_limit_view_fails_before_any_model_call(tmp_path, monkeypatch):
    data, scoped = scoped_of(tmp_path)
    monkeypatch.setitem(mv.LIMITS, 'direction_author', 100)
    with pytest.raises(ValueError, match='model_input_over_limit:direction_author'):
        mv.build(2, scoped)


def test_the_reuse_key_render_stays_on_the_old_transport_so_finished_work_is_not_invalidated(tmp_path):
    data, scoped = scoped_of(tmp_path)
    pipe = Pipeline(DirectionRuntime({'directions': []}), tmp_path / 'run')
    _, old_prompt = pipe.render_prompt(2, scoped, model_view=False)
    _, new_prompt = pipe.render_prompt(2, scoped)
    assert len(new_prompt) < len(old_prompt) * 0.6, 'the model receives the smaller view'
    again = pipe.render_prompt(2, scoped, model_view=False)[1]
    assert again == old_prompt, 'the reuse key render is unchanged and deterministic'


def test_the_model_really_receives_the_view_not_the_canonical_input(tmp_path):
    import asyncio
    data, plan = heavy_data(tmp_path)
    runtime = DirectionRuntime(plan)
    pipe = Pipeline(runtime, tmp_path / 'run')
    sizes = []
    original_ask = runtime.ask

    async def spy(role, message, **kwargs):
        stage = json.loads(message.rsplit('\n입력:\n', 1)[-1])['stage_data']
        if stage.get('direction_assignment'):
            sizes.append((role, len(message)))
        return await original_ask(role, message, **kwargs)
    runtime.ask = spy
    result = asyncio.run(pipe.reviewed_synthesis(data))
    assert result['directions']
    roles = {r for r, _ in sizes}
    # 방향 작성은 direction_author 역할(luna high)로 바뀌었다. 작성자와 검토자가 모두 돌았는지가 이 시험의 의도다.
    assert {'direction_author', 'content_review'} <= roles, 'both the direction author and its reviewer ran'
    heavy = len(json.dumps(data, ensure_ascii=False))
    assert max(n for _, n in sizes) < heavy * 0.2, 'direction author/review messages must be built from the view (canonical input is %d chars, got %s)' % (heavy, sizes)


def item_scoped(tmp_path, kind='diagnosis'):
    """진단 항목 1개 작성 입력: 영역 시사점·방향·미확인이 많은 정본에서 항목 1개의 입력을 만든다."""
    from asis_item_agents import item_input
    data, plan = heavy_data(tmp_path)
    areas = data['stage_data']['approved_areas']
    direction = {'id': 'D_X1', 'title': '방향', 'text': 't', 'customer_impact': 'i', 'change_direction': 'c',
                 'area_implication_refs': [areas['areas'][0]['implication']['id']],
                 'fact_refs': list(areas['areas'][0]['implication']['fact_refs'])[:1], 'criterion_refs': list(areas['areas'][0]['implication']['criterion_refs'])[:1],
                 'goal_refs': []}
    data['stage_data'] = {'mode': 'actions', 'approved_areas': areas,
                          'approved_directions': {'stage': 'synthesis', 'run': deepcopy(data['run']), 'directions': [direction],
                                                  'edges': [], 'unresolved': deepcopy(areas['unresolved'])}}
    slot = {'id': 'J_ITEM_0001', 'scope': '진단', 'reason': 'r', 'issue_refs': ['PLAN_P0001'], 'direction_refs': ['D_X1'],
            'fact_refs': direction['fact_refs'][:1], 'criterion_refs': direction['criterion_refs'][:1], 'goal_refs': []}
    return data, item_input(data, kind, slot)


def test_an_item_author_view_carries_only_the_linked_direction_its_implication_and_cited_facts(tmp_path):
    data, scoped = item_scoped(tmp_path)
    kind, view = mv.build(3, scoped)
    assert kind == 'item_author'
    assert not any(row['id'].startswith('F_X_') for row in view['facts']), 'facts nobody cited must not be sent'
    for key in ('source_documents', 'collection_basis', 'criterion_assessments'):
        assert key not in view['context']
    kept_areas = view['stage_data']['approved_areas']['areas']
    linked = {ref for row in view['stage_data']['approved_directions']['directions'] for ref in row['area_implication_refs']}
    assert {area['implication']['id'] for area in kept_areas} == linked, 'only the implications the linked direction cites'
    assert all(area['observations'] == [] for area in kept_areas)
    assert len(json.dumps(view, ensure_ascii=False)) < len(json.dumps(scoped, ensure_ascii=False)) * 0.5


def test_an_item_review_view_keeps_the_draft_and_the_facts_it_cites(tmp_path):
    data, scoped = item_scoped(tmp_path)
    draft = {'stage': 'actions', 'run': deepcopy(data['run']), 'diagnoses': [{'id': 'J_ITEM_0001', 'text': 't', 'impact': 'i', 'direction_refs': ['D_X1'],
             'fact_refs': ['F_X_003'], 'criterion_refs': [], 'goal_refs': []}], 'actions': [], 'edges': [], 'unresolved': []}
    scoped['stage_data'].update(draft=draft, candidate_sha256='h', deterministic_check_result={'passed': True, 'candidate_sha256': 'h', 'defects': []})
    kind, view = mv.build(5, scoped)
    assert kind == 'item_review' and view['stage_data']['draft'] == draft
    assert 'F_X_003' in {row['id'] for row in view['facts']} and 'F_X_004' not in {row['id'] for row in view['facts']}


def test_unresolved_items_are_selected_conservatively_not_by_one_shared_common_fact():
    common = 'F_COMMON'
    mine = {common, 'F_A', 'F_B', 'F_C', 'F_D'}
    rows = ([{'topic': 'common-%d' % i, 'fact_refs': [common, 'F_other_%d' % i, 'F_x%d' % i, 'F_y%d' % i]} for i in range(30)]
            + [{'topic': 'mine', 'fact_refs': ['F_A', 'F_B', 'F_zzz']}, {'topic': 'rare', 'fact_refs': ['F_D', 'F_q', 'F_r', 'F_s']}])
    kept = mv.related_unresolved(rows, mine)
    topics = {row['topic'] for row in kept}
    assert 'mine' in topics and 'rare' in topics, 'items sharing most of their facts or a rare fact are kept'
    assert not any(t.startswith('common-') for t in topics), 'one very common fact must not drag in dozens of unrelated unresolved items'


def test_a_large_fact_the_candidate_cites_still_reaches_the_reviewer_after_the_prompt_view(tmp_path):
    """운영 실측: 방향 D_P0024 검토가 '비용 원장이 not_inlined 메타데이터뿐이라 확인할 수 없다'며 blocked를 냈다.
    전송 뷰는 사실 본문을 남겼지만 inline_fact_refs를 비워 두어, 모델에 보내는 마지막 단계(prompt_view)가 4.5만 자 초과 사실을 지웠다."""
    from asis_evidence_views import prompt_view
    data, plan = heavy_data(tmp_path)
    big = 'F_BIG_COST'
    data['facts'].append({'id': big, 'source_id': 'cost.json', 'field_path': '/cost/records', 'run': deepcopy(data['run']),
                          'value': [{'row': i, 'blob': 'c' * 400} for i in range(150)], 'collection_status': 'collected', 'scope_complete': True})
    scoped = direction_input(data, first_slot(plan, data))
    draft = {'stage': 'synthesis', 'run': deepcopy(data['run']), 'edges': [], 'unresolved': [],
             'directions': [{'id': scoped['stage_data']['direction_assignment']['slot']['id'], 'title': 't', 'text': 'x', 'customer_impact': 'i', 'change_direction': 'c',
                             'area_implication_refs': scoped['stage_data']['direction_assignment']['slot']['area_implication_refs'],
                             'fact_refs': [big], 'criterion_refs': [], 'goal_refs': []}]}
    scoped['stage_data'].update(draft=draft, candidate_sha256='h', deterministic_check_result={'passed': True, 'candidate_sha256': 'h', 'defects': []})
    kind, view = mv.build(5, scoped)
    assert kind == 'direction_review'
    sent = prompt_view(view)
    fact = next(row for row in sent['facts'] if row['id'] == big)
    assert isinstance(fact['value'], list) and len(fact['value']) == 150, 'the reviewer must see the rows the candidate cites'
    assert big not in {row['id'] for row in sent['context'].get('not_inlined_fact_register', [])}


def test_a_large_fact_nobody_cites_is_still_withheld_from_the_prompt(tmp_path):
    from asis_evidence_views import prompt_view
    data, plan = heavy_data(tmp_path)
    data['facts'].append({'id': 'F_BIG_UNCITED', 'source_id': 'c.json', 'field_path': '/c', 'run': deepcopy(data['run']),
                          'value': [{'row': i, 'blob': 'c' * 400} for i in range(150)], 'collection_status': 'collected', 'scope_complete': True})
    scoped = direction_input(data, first_slot(plan, data))
    kind, view = mv.build(2, scoped)
    assert 'F_BIG_UNCITED' not in {row['id'] for row in prompt_view(view)['facts']}, 'uncited facts never enter the view'
