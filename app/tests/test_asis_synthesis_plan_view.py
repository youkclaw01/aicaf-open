"""종합 계획(영역별 방향 슬롯 계획) 전송 입력의 축소 시험.

배경(평가 24 실측, 2026-10-17): 영역 1개의 종합 계획 호출이 1.32MB(≈340K 토큰)였다. 호출이 실제로 쓰는 영역 본문은 8KB인데
 - approved_areas.unresolved 917건 0.69MB(모든 영역의 미확인 전체)가 그대로 실렸고,
 - collection_basis_matches 248행 0.36MB가 전부 실렸다(영역 행만 남기는 분기가 area_id가 없어 실행되지 않았다).
영수증 317건 중 312건이 272K를 넘어 Long 단가였다.

계약:
- 미확인은 사라지지 않는다. 이 영역 이슈·시사점이 인용한 사실과 겹치는 것부터 예산(UNRESOLVED_BUDGET_CHARS) 안에서 본문을 채우고
  나머지는 자리(omitted)와 주제만 남겨 위치(/unresolved/N)를 정본과 같게 둔다.
- 영역 이슈가 인용한 사실·기준, 영역 시사점이 인용한 사실·기준은 하나도 빠지지 않는다(계획 검증의 허용 참조 집합이 이 범위다).
- 수집 기준 매칭은 이 영역 행과 이 영역이 인용한 기준 행만 보낸다.
- 정본 data는 변경하지 않는다. 재사용 키·검증은 전체 data 기준이다.
"""
import json
import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_model_view as mv
import asis_synthesis as sy
from test_asis_model_view import heavy_data


def plan_project(tmp_path, unresolved=900, matches=240):
    data, plan = heavy_data(tmp_path)
    areas = data['stage_data']['approved_areas']['areas']
    facts = [f['id'] for f in data['facts']]
    common = facts[0]
    data['stage_data']['approved_areas']['unresolved'] = [
        {'topic': 'u%d' % i, 'reason': 'r' * 300, 'required_evidence': 'e' * 100, 'fact_refs': [common, facts[(i * 7 + 3) % len(facts)]]} for i in range(unresolved)]
    # 영역마다 이슈를 두고, 한 영역(areas[0])이 이끄는 이슈가 일부 사실을 인용한다.
    issues = []
    for a, area in enumerate(areas):
        for k in range(3):
            fid = facts[(a * 11 + k * 5 + 1) % len(facts)]
            issues.append({'id': 'PLAN_P%02d%02d' % (a, k), 'direction_id': 'D_P%02d%02d' % (a, k), 'lead_area': area['area_id'], 'area_refs': [area['area_id']],
                           'scope': 's', 'reason': 'r', 'fact_refs': [fid], 'criterion_refs': [], 'goal_refs': []})
    data['context']['analysis_plan'] = {'schema': 'asis-issue-plan/1', 'issues': issues}
    ids = [c['id'] for c in data['criteria']]
    data['context']['collection_basis'] = {'criterionMatches': [
        {'area': areas[i % len(areas)]['area_id'], 'criterion_id': ids[i % len(ids)], 'status': 's', 'related_resource_ids': ['R%d' % j for j in range(8)]} for i in range(matches)],
        'source_artifacts': {}}
    return data


def sent_view(data, area_id):
    scoped = sy.area_plan_scope(data, area_id)
    payload = deepcopy(scoped)
    payload['stage_data'].update(prior_output=None, accumulated_defects=[])
    return scoped, sy.plan_request_view(scoped, payload)


def test_unresolved_in_a_plan_call_stays_within_the_input_budget(tmp_path):
    data = plan_project(tmp_path)
    area = data['stage_data']['approved_areas']['areas'][0]['area_id']
    scoped, sent = sent_view(data, area)
    rows = sent['stage_data']['approved_areas']['unresolved']
    body = sum(len(json.dumps(r, ensure_ascii=False)) for r in rows if not r.get('omitted'))
    assert body <= mv.UNRESOLVED_BUDGET_CHARS + 2_000, 'kept unresolved text %d exceeds the budget' % body


def test_every_unresolved_position_is_kept_and_omitted_ones_leave_their_topic(tmp_path):
    data = plan_project(tmp_path)
    area = data['stage_data']['approved_areas']['areas'][0]['area_id']
    scoped, sent = sent_view(data, area)
    rows = sent['stage_data']['approved_areas']['unresolved']
    assert len(rows) == len(data['stage_data']['approved_areas']['unresolved']), 'positions must equal the canonical list'
    summary = sent['stage_data']['approved_areas']['omitted_unresolved']
    assert summary['omitted_count'] > 0 and summary['omitted_topics']


def test_unresolved_items_sharing_the_most_facts_with_this_area_keep_their_body(tmp_path):
    data = plan_project(tmp_path)
    area_row = data['stage_data']['approved_areas']['areas'][0]
    mine = sorted(set(area_row['implication']['fact_refs']))
    assert len(mine) >= 2
    strong = {'topic': 'strong', 'reason': 'r' * 300, 'required_evidence': 'e', 'fact_refs': mine[:2]}
    rows = data['stage_data']['approved_areas']['unresolved']
    rows.insert(450, strong)
    scoped, sent = sent_view(data, area_row['area_id'])
    out = sent['stage_data']['approved_areas']['unresolved']
    assert not out[450].get('omitted') and out[450]['topic'] == 'strong'


def test_a_small_unresolved_list_is_sent_unchanged(tmp_path):
    data = plan_project(tmp_path, unresolved=6)
    area = data['stage_data']['approved_areas']['areas'][0]['area_id']
    scoped, sent = sent_view(data, area)
    assert sent['stage_data']['approved_areas']['unresolved'] == data['stage_data']['approved_areas']['unresolved']
    assert 'omitted_unresolved' not in sent['stage_data']['approved_areas']


def test_the_collection_basis_matches_are_narrowed_to_this_area(tmp_path):
    data = plan_project(tmp_path)
    area = data['stage_data']['approved_areas']['areas'][0]['area_id']
    scoped, sent = sent_view(data, area)
    from asis_evidence_views import prompt_view
    view = prompt_view(deepcopy(sent))
    total = len(data['context']['collection_basis']['criterionMatches'])
    rows = view['context'].get('collection_basis_matches', [])
    assert 0 < len(rows) < total, 'only this area (and criteria it cites) must remain, got %d of %d' % (len(rows), total)
    cited = set(sent['context']['model_scope']['criterion_refs'])
    assert all(r.get('area') == area or r.get('criterion_id') in cited for r in rows)


def test_facts_and_criteria_cited_by_the_area_issues_and_implication_still_arrive(tmp_path):
    data = plan_project(tmp_path)
    for area in data['stage_data']['approved_areas']['areas']:
        scoped, sent = sent_view(data, area['area_id'])
        from asis_evidence_views import prompt_view
        view = prompt_view(deepcopy(sent))
        need_f = set(area['implication']['fact_refs']) | {f for i in scoped['context']['analysis_plan']['issues'] for f in i['fact_refs']}
        need_c = set(area['implication']['criterion_refs']) | {c for i in scoped['context']['analysis_plan']['issues'] for c in i['criterion_refs']}
        known_f = {f['id'] for f in data['facts']}
        known_c = {c['id'] for c in data['criteria']}
        assert (need_f & known_f) <= {f['id'] for f in view['facts']}, area['area_id']
        assert (need_c & known_c) <= {c['id'] for c in view['criteria']}, area['area_id']


def test_the_canonical_input_and_the_validation_scope_are_unchanged(tmp_path):
    data = plan_project(tmp_path)
    area = data['stage_data']['approved_areas']['areas'][0]['area_id']
    before = json.dumps(data, ensure_ascii=False, sort_keys=True)
    scoped, sent = sent_view(data, area)
    assert json.dumps(data, ensure_ascii=False, sort_keys=True) == before
    assert len(scoped['stage_data']['approved_areas']['unresolved']) == len(data['stage_data']['approved_areas']['unresolved']), \
        'validation and checkpoint binding keep the full canonical scoped input'


def test_the_review_call_view_matches_the_author_call_view(tmp_path):
    """검토 호출은 plan_request_view(scoped, scoped)를 쓴다. 작성과 같은 축소를 받아야 검토가 작성자와 같은 근거를 본다."""
    data = plan_project(tmp_path)
    area = data['stage_data']['approved_areas']['areas'][0]['area_id']
    scoped = sy.area_plan_scope(data, area)
    review = sy.plan_request_view(scoped, scoped)
    author = sy.plan_request_view(scoped, deepcopy(scoped))
    assert json.dumps(review['stage_data']['approved_areas'], sort_keys=True) == json.dumps(author['stage_data']['approved_areas'], sort_keys=True)


def test_the_body_budget_is_enforced_by_the_value_not_by_the_call_form(tmp_path):
    """예산 인자를 None으로 바꾸면(제한 없음) 본문이 한도를 넘는다. 값 자체가 지켜지는지 확인한다."""
    data = plan_project(tmp_path, unresolved=1500)
    area = data['stage_data']['approved_areas']['areas'][0]['area_id']
    scoped, sent = sent_view(data, area)
    rows = sent['stage_data']['approved_areas']['unresolved']
    kept = [r for r in rows if not r.get('omitted')]
    assert 0 < len(kept) < len(rows)
    assert sum(len(json.dumps(r, ensure_ascii=False)) for r in kept) <= mv.UNRESOLVED_BUDGET_CHARS + 2_000


def test_the_overlap_order_decides_which_unresolved_items_keep_their_body_under_pressure(tmp_path):
    """예산을 넘는 상황에서 이 영역과 겹치는 항목이 겹치지 않는 항목보다 먼저 본문을 얻는다."""
    data = plan_project(tmp_path, unresolved=0)
    area_row = data['stage_data']['approved_areas']['areas'][0]
    mine = sorted(set(area_row['implication']['fact_refs']))
    unrelated = [{'topic': 'x%d' % i, 'reason': 'r' * 300, 'required_evidence': 'e' * 100, 'fact_refs': ['F_unrelated_%d' % i]} for i in range(900)]
    related = [{'topic': 'm%d' % i, 'reason': 'r' * 300, 'required_evidence': 'e' * 100, 'fact_refs': [mine[0], mine[1]]} for i in range(30)]
    rows = unrelated[:450] + related + unrelated[450:]
    data['stage_data']['approved_areas']['unresolved'] = rows
    scoped, sent = sent_view(data, area_row['area_id'])
    out = sent['stage_data']['approved_areas']['unresolved']
    kept_topics = {r['topic'] for r in out if not r.get('omitted')}
    assert {'m%d' % i for i in range(30)} <= kept_topics, 'items overlapping this area must keep their body first'
    assert not any(t.startswith('x') for t in kept_topics), 'unrelated items must not displace overlapping ones'
