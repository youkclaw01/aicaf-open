"""종합 통합 검토(synthesis-integration)의 전송 뷰 시험.

통합 검토의 지시: "방향 간 중복·상충·누락·우선순위 일관성과 목표 연결만 검토한다. 본문은 요약되어 있다."
평가 24 실측: 통합 검토 105건이 정본 3.28MB(영수증 입력 중앙 763K 토큰)를 보냈다. 호출부가 사실·기준은 비웠지만 영역 승인본 1.12MB와
배경 계획 context 1.2MB는 정본 그대로였다.

계약:
- 통합 검토는 방향 요약본(제목·핵심 문장·시사점 참조·사실 수)과 목표만 받는다. 관찰 본문·미확인 본문·계획 context는 받지 않는다.
- 영역 시사점은 방향 간 일관성을 대조하는 데 필요하므로 제목과 시사점 본문은 남긴다(관찰·미확인 본문만 뺀다).
- 미확인은 사라지지 않는다. 주제만 남기고 위치(/unresolved/N)는 유지된다.
"""
import json
import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_model_view as mv
from test_asis_synthesis_shard_view_is_sent import body, merged_for, project, review_input


def integration(tmp_path, unresolved=0):
    data, pipe = project(tmp_path)
    merged = merged_for(data, 60)
    facts = [f['id'] for f in data['facts']]
    if unresolved:
        merged['unresolved'] = [{'topic': 'u%d' % i, 'reason': 'r' * 300, 'required_evidence': 'e' * 100, 'fact_refs': [facts[i % len(facts)]]} for i in range(unresolved)]
        data['stage_data']['approved_areas']['unresolved'] = deepcopy(merged['unresolved'])
    rd, draft = review_input(data, merged, list(range(60)), summary=True)
    for key in ('analysis_plan', 'action_item_plan', 'collection_basis_matches'):
        rd['context'][key] = {'padding': 'x' * 6000}
    return data, pipe, merged, rd


def test_the_integration_review_is_classified_so_its_view_can_apply(tmp_path):
    data, pipe, merged, rd = integration(tmp_path)
    assert mv.kind_of(5, rd) == 'synthesis_integration_review'


def test_the_integration_view_drops_plans_observations_and_unresolved_bodies(tmp_path):
    data, pipe, merged, rd = integration(tmp_path, unresolved=700)
    view = mv.build(5, rd)[1]
    for key in ('analysis_plan', 'action_item_plan', 'collection_basis_matches'):
        assert key not in view['context']
    for area in view['stage_data']['approved_areas']['areas']:
        assert area['observations'] == [], 'observation bodies are not needed to compare directions'
    assert sum(len(json.dumps(r, ensure_ascii=False)) for r in view['stage_data']['approved_areas']['unresolved'] if not r.get('omitted')) <= mv.UNRESOLVED_BUDGET_CHARS + 2_000
    assert sum(len(json.dumps(r, ensure_ascii=False)) for r in view['stage_data']['draft']['unresolved'] if not r.get('omitted')) <= mv.UNRESOLVED_BUDGET_CHARS + 2_000


def test_the_integration_view_keeps_every_implication_and_every_direction_summary(tmp_path):
    data, pipe, merged, rd = integration(tmp_path)
    view = mv.build(5, rd)[1]
    assert [a['implication']['id'] for a in view['stage_data']['approved_areas']['areas']] == [a['implication']['id'] for a in rd['stage_data']['approved_areas']['areas']]
    assert [r['id'] for r in view['stage_data']['draft']['directions']] == [r['id'] for r in merged['directions']]
    assert view['stage_data']['draft']['directions'] == rd['stage_data']['draft']['directions'], 'the direction summaries are sent as the caller built them'


def test_integration_unresolved_positions_stay_equal_to_the_canonical_list(tmp_path):
    data, pipe, merged, rd = integration(tmp_path, unresolved=700)
    view = mv.build(5, rd)[1]
    assert len(view['stage_data']['draft']['unresolved']) == 700
    summary = view['stage_data']['draft']['omitted_unresolved']
    assert summary['omitted_count'] > 0 and summary['omitted_topics']


def test_the_integration_prompt_is_far_smaller_than_the_canonical_render(tmp_path):
    data, pipe, merged, rd = integration(tmp_path, unresolved=700)
    old = pipe.render_prompt(5, rd, model_view=False)[1]
    new = pipe.render_prompt(5, rd)[1]
    assert len(new) < len(old) * 0.5, (len(old), len(new))
    assert body(new)['context'].get('model_view', {}).get('kind') == 'synthesis_integration_review'


def test_the_reuse_key_render_of_an_integration_review_is_unchanged(tmp_path):
    data, pipe, merged, rd = integration(tmp_path)
    first = pipe.render_prompt(5, rd, model_view=False)[1]
    assert pipe.render_prompt(5, rd, model_view=False)[1] == first
    assert 'model_view' not in body(first)['context']


def test_a_shard_review_is_not_mistaken_for_an_integration_review(tmp_path):
    from test_asis_synthesis_shard_view_is_sent import merged_for as mf, review_input as ri
    data, pipe = project(tmp_path)
    rd, draft = ri(data, mf(data, 60), list(range(24, 48)))
    assert mv.kind_of(5, rd) == 'synthesis_shard_review'


def test_the_canonical_input_is_not_modified_by_the_integration_view(tmp_path):
    data, pipe, merged, rd = integration(tmp_path, unresolved=300)
    before = json.dumps(rd, ensure_ascii=False, sort_keys=True)
    mv.build(5, rd)
    assert json.dumps(rd, ensure_ascii=False, sort_keys=True) == before


def test_a_synthesis_review_that_is_neither_shard_nor_integration_keeps_the_ordinary_transport(tmp_path):
    """방향 후보를 가진 다른 종합 검토(예: 통합 모드가 아닌 단일 검토 mode='synthesis')는 통합 검토로 오분류되면 안 된다.
    통합 뷰는 관찰·미확인 본문을 빼므로, 오분류되면 그 검토가 근거를 잃는다."""
    data, pipe, merged, rd = integration(tmp_path)
    rd['stage_data']['mode'] = 'synthesis'
    assert mv.kind_of(5, rd) not in ('synthesis_integration_review', 'synthesis_shard_review')
    kind, view = mv.build(5, rd)
    assert kind != 'synthesis_integration_review'


def test_an_item_or_direction_review_carrying_a_direction_list_is_not_taken_for_integration(tmp_path):
    data, pipe, merged, rd = integration(tmp_path)
    rd['stage_data']['item_assignment'] = {'kind': 'diagnosis', 'item_id': 'J_ITEM_0001', 'slot': {}}
    assert mv.kind_of(5, rd) == 'item_review'
    rd2 = deepcopy(rd)
    rd2['stage_data'].pop('item_assignment')
    rd2['stage_data']['direction_assignment'] = {'slot': {}}
    assert mv.kind_of(5, rd2) == 'direction_review'
