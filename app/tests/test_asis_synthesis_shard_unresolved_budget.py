"""종합 구간 검토 전송 뷰의 두 번째 축소(미확인 예산·배경 계획 제외) 시험.

배경(평가 24 실측, 2026-10-17): 구간 검토 뷰 연결 후에도 호출당 2.3~3.3MB였다. 구간 24개 호출의 85%가 담당 방향과 무관한 고정부였다.
 - draft.unresolved 917건 중 834건(91%)이 본문으로 남았다. 담당 방향 24개가 인용한 사실 101개 중 공통 사실(113건·86건…)이 미확인에 널리 인용돼
   '하나라도 겹치면 유지'가 사실상 전부를 유지했다. 방향 입력에는 같은 문제를 푸는 규칙(related_unresolved + budgeted_unresolved)이 이미 있다.
 - context의 분석 계획·진단·과제 계획·수집 기준 매칭(합 1.1MB)은 종합 구간 검토가 읽지 않는다.

계약:
- 미확인은 사라지지 않는다. 본문을 못 담는 항목도 자리(omitted)·주제는 남아 위치(/unresolved/N)가 정본과 같다.
- 담당 방향의 사실과 가장 많이 겹치는 미확인부터 본문을 채운다. 예산을 넘는 나머지는 본문 없이 주제만 남긴다.
- 담당 방향이 인용한 사실·기준·시사점은 하나도 빠지지 않는다.
"""
import json
import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_model_view as mv
from test_asis_synthesis_shard_view_is_sent import merged_for, project, review_input, wide_input


def with_many_unresolved(data, merged, count=900):
    """담당 방향이 인용하는 공통 사실을 대부분의 미확인이 인용하는 상황(평가 24와 같은 구조)을 만든다."""
    facts = [f['id'] for f in data['facts']]
    common = facts[0]
    merged['unresolved'] = [{'topic': 'u%d' % i, 'reason': 'r' * 300, 'required_evidence': 'e' * 100,
                             'fact_refs': [common, facts[(i * 7 + 3) % len(facts)]]} for i in range(count)]
    return merged


def shard_view(tmp_path, count=900):
    data, pipe = project(tmp_path)
    merged = with_many_unresolved(data, merged_for(data), count)
    ids = list(range(24, 48))
    for i in ids:
        merged['directions'][i]['fact_refs'] = list({*merged['directions'][i]['fact_refs'], data['facts'][0]['id']})
    rd, draft = wide_input(data, merged, ids)
    return data, merged, ids, rd, mv.build(5, rd)[1]


def test_unresolved_bodies_in_a_shard_review_stay_within_the_input_budget(tmp_path):
    data, merged, ids, rd, view = shard_view(tmp_path)
    rows = view['stage_data']['draft']['unresolved']
    body_chars = sum(len(json.dumps(r, ensure_ascii=False)) for r in rows if not r.get('omitted'))
    assert body_chars <= mv.UNRESOLVED_BUDGET_CHARS + 2_000, 'kept unresolved text %d exceeds the budget %d' % (body_chars, mv.UNRESOLVED_BUDGET_CHARS)


def test_every_unresolved_slot_keeps_its_position_so_review_pointers_stay_valid(tmp_path):
    data, merged, ids, rd, view = shard_view(tmp_path)
    rows = view['stage_data']['draft']['unresolved']
    assert len(rows) == len(merged['unresolved']), 'positions (/unresolved/N) must equal the canonical list'


def test_an_unresolved_item_dropped_from_the_body_still_leaves_its_topic(tmp_path):
    data, merged, ids, rd, view = shard_view(tmp_path)
    summary = view['stage_data']['draft'].get('omitted_unresolved')
    assert summary and summary['omitted_count'] > 0 and summary['omitted_topics'], 'omitted items must be listed by topic'
    assert summary['omitted_count'] + summary['kept_count'] <= len(merged['unresolved'])


def test_unresolved_items_that_share_the_most_facts_with_the_shard_are_kept_first(tmp_path):
    data, pipe = project(tmp_path)
    merged = merged_for(data)
    ids = list(range(24, 48))
    own = sorted({ref for i in ids for ref in merged['directions'][i]['fact_refs']})
    assert len(own) >= 3
    strong = {'topic': 'strong', 'reason': 'r' * 300, 'required_evidence': 'e', 'fact_refs': own[:3]}
    filler = [{'topic': 'f%d' % i, 'reason': 'r' * 300, 'required_evidence': 'e', 'fact_refs': [own[0], 'F_other_%d' % i]} for i in range(900)]
    merged['unresolved'] = filler[:450] + [strong] + filler[450:]
    rd, draft = wide_input(data, merged, ids)
    view = mv.build(5, rd)[1]
    rows = view['stage_data']['draft']['unresolved']
    assert not rows[450].get('omitted') and rows[450]['topic'] == 'strong', 'the best overlap must keep its body'


def test_a_small_unresolved_list_is_sent_unchanged(tmp_path):
    data, pipe = project(tmp_path)
    merged = merged_for(data)
    ids = list(range(24, 48))
    merged['unresolved'] = [{'topic': 't%d' % i, 'reason': 'r', 'required_evidence': 'e', 'fact_refs': [merged['directions'][24]['fact_refs'][0]]} for i in range(5)]
    rd, draft = wide_input(data, merged, ids)
    view = mv.build(5, rd)[1]
    rows = view['stage_data']['draft']['unresolved']
    assert [r.get('topic') for r in rows] == ['t0', 't1', 't2', 't3', 't4'] and not view['stage_data']['draft'].get('omitted_unresolved')


def test_background_plans_the_shard_review_never_reads_are_not_sent(tmp_path):
    data, pipe = project(tmp_path)
    merged = merged_for(data)
    ids = list(range(24, 48))
    rd, draft = wide_input(data, merged, ids)
    for key in ('analysis_plan', 'action_item_plan', 'collection_basis_matches'):
        rd['context'][key] = {'padding': 'x' * 5000}
    view = mv.build(5, rd)[1]
    for key in ('analysis_plan', 'action_item_plan', 'collection_basis_matches'):
        assert key not in view['context'], key + ' is background the shard review does not use'


def test_the_cited_facts_criteria_and_implications_still_arrive_after_the_second_reduction(tmp_path):
    data, merged, ids, rd, view = shard_view(tmp_path)
    need_f = {ref for i in ids for ref in merged['directions'][i]['fact_refs']}
    known = {f['id'] for f in data['facts']}
    assert (need_f & known) <= {f['id'] for f in view['facts']}
    need_i = {ref for i in ids for ref in merged['directions'][i]['area_implication_refs']}
    assert need_i <= {a['implication']['id'] for a in view['stage_data']['approved_areas']['areas']}


def test_the_approved_area_unresolved_is_also_budgeted(tmp_path):
    data, pipe = project(tmp_path)
    merged = merged_for(data)
    ids = list(range(24, 48))
    facts = [f['id'] for f in data['facts']]
    data['stage_data']['approved_areas']['unresolved'] = [
        {'topic': 'a%d' % i, 'reason': 'r' * 300, 'required_evidence': 'e' * 100, 'fact_refs': [facts[0], facts[(i * 5 + 2) % len(facts)]]} for i in range(900)]
    for i in ids:
        merged['directions'][i]['fact_refs'] = list({*merged['directions'][i]['fact_refs'], facts[0]})
    rd, draft = wide_input(data, merged, ids)
    assert sum(len(json.dumps(r, ensure_ascii=False)) for r in rd['stage_data']['approved_areas']['unresolved']) > 3 * mv.UNRESOLVED_BUDGET_CHARS
    view = mv.build(5, rd)[1]
    approved = view['stage_data']['approved_areas']
    assert sum(len(json.dumps(r, ensure_ascii=False)) for r in approved['unresolved']) <= mv.UNRESOLVED_BUDGET_CHARS + 2_000
    assert approved['omitted_unresolved']['omitted_count'] > 0 and approved['omitted_unresolved']['omitted_topics'], 'omitted area items keep their topics'


def test_the_canonical_input_is_not_modified_by_the_view(tmp_path):
    data, merged, ids, rd, view = shard_view(tmp_path)
    before = json.dumps(rd, ensure_ascii=False, sort_keys=True)
    mv.build(5, rd)
    assert json.dumps(rd, ensure_ascii=False, sort_keys=True) == before
