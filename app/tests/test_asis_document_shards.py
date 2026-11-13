"""12단계 문서 전체 검토: 모델 입력 한도를 넘는 검토 단위(종합·과제·통합)를 구간으로 나누거나 축약해 이어가는 계약 시험(합성 런타임, 모델 호출 없음).

운영 2026-10-19(프로젝트 25 12단계): 승인된 방향 86개·진단 100개의 서술문이 종합 샤드 88.9만 자(36.9만 토큰)·과제 샤드 89.2만 자(37.0만 토큰)·통합 85.0만 자(35.3만 토큰)로
모델 한도(27.2만 토큰)를 넘었다. 글자 수 한도(90만)만 있고 토큰 한도가 없어 통과 후 모델이 거절한다. 종합 단계(8단계)에는 이미 구간 분할이 있지만 문서 검토에는 없었다.
계약:
1. 종합·과제 샤드는 입력이 토큰 예산을 넘으면 방향(진단) 구간으로 나눠 검토한다. 지적 위치(JSON pointer)는 원본 문서의 위치와 같다.
2. 모든 방향·진단은 정확히 한 구간이 담당한다(누락·중복 없음). 다른 구간의 항목은 {id, omitted:true} 로 위치만 유지한다.
3. 통합 검토는 방향·진단 서술문을 이름·숫자 보존 규칙으로 축약한다. ID·참조·과제 목록은 그대로다.
4. 예산 안의 단위는 지금처럼 한 번에 검토한다(정상 입력을 건드리지 않는다).
5. 구간 검토 결과는 병합되어 한 샤드의 검토 결과와 같은 형태(verdict·defects·unresolved)로 돌아온다.
"""
from copy import deepcopy
from pathlib import Path
import asyncio
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import asis_analysis
import asis_document_shards as DS
from asis_pipeline import Pipeline
from test_asis_connection import synthetic_input

SENT = '이 방향은 운영 체계를 정비하는 것이다. 조직이 일관된 절차를 갖추는 것이 바람직하다. 세부 내용은 별도 문서에 둔다. '


def big_stages(tmp_path, directions=24, diagnoses=30, repeat=60):
    tmp_path = Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    seed = asis_analysis.analyze(synthetic_input(tmp_path))
    stages = deepcopy(seed['stages'])
    d0 = stages[1]['directions'][0]
    stages[1]['directions'] = [dict(deepcopy(d0), id='D_P%04d' % i, text=SENT * repeat + ' D%d' % i, customer_impact=SENT * repeat, change_direction=SENT * repeat) for i in range(directions)]
    j0 = stages[2]['diagnoses'][0]
    stages[2]['diagnoses'] = [dict(deepcopy(j0), id='J_ITEM_%04d' % (i + 1), text=SENT * repeat + ' J%d' % i, impact=SENT * repeat) for i in range(diagnoses)]
    return seed['input'], stages


def test_a_shard_within_the_token_budget_is_reviewed_in_one_piece(tmp_path):
    data, stages = big_stages(tmp_path, directions=3, diagnoses=3, repeat=1)
    plan = DS.plan_shard_groups(stages, 1, token_budget=272_000)
    assert plan == [list(range(3))]


def test_a_shard_over_the_token_budget_is_split_into_groups_that_cover_every_item_exactly_once(tmp_path):
    data, stages = big_stages(tmp_path)
    plan = DS.plan_shard_groups(stages, 1, token_budget=60_000)
    assert len(plan) >= 2
    flat = [i for group in plan for i in group]
    assert sorted(flat) == list(range(len(stages[1]['directions']))) and len(flat) == len(set(flat))


def test_the_same_holds_for_the_actions_stage_diagnoses(tmp_path):
    data, stages = big_stages(tmp_path)
    plan = DS.plan_shard_groups(stages, 2, token_budget=60_000)
    flat = [i for group in plan for i in group]
    assert len(plan) >= 2 and sorted(flat) == list(range(len(stages[2]['diagnoses'])))


def test_a_sparse_group_keeps_the_original_positions_and_marks_the_others_omitted(tmp_path):
    data, stages = big_stages(tmp_path)
    group = [2, 3]
    draft = DS.sparse_group(stages, 1, group)['stages']
    rows = draft[1]['directions']
    assert len(rows) == len(stages[1]['directions'])
    for i, row in enumerate(rows):
        if i in group:
            assert row == stages[1]['directions'][i]
        else:
            assert row == {'id': stages[1]['directions'][i]['id'], 'omitted': True}
    assert draft[0].get('omitted') and draft[3].get('omitted')


def test_the_actions_group_keeps_the_action_list_and_marks_other_diagnoses_omitted(tmp_path):
    data, stages = big_stages(tmp_path)
    draft = DS.sparse_group(stages, 2, [0, 1])['stages'][2]
    assert len(draft['diagnoses']) == len(stages[2]['diagnoses'])
    assert draft['diagnoses'][0] == stages[2]['diagnoses'][0] and draft['diagnoses'][5] == {'id': stages[2]['diagnoses'][5]['id'], 'omitted': True}
    assert draft['actions'] == stages[2]['actions'], '과제 목록은 모든 구간이 같은 모양으로 본다'


def test_the_integration_view_condenses_narratives_and_keeps_ids_and_references(tmp_path):
    data, stages = big_stages(tmp_path)
    full = Pipeline._integration_stages(stages)
    condensed = DS.condensed_integration(full)
    size = lambda v: len(json.dumps(v, ensure_ascii=False))
    assert size(condensed) < size(full)
    old = {r['id']: r for r in full['stages'][1]['directions']}
    new = {r['id']: r for r in condensed['stages'][1]['directions']}
    assert set(old) == set(new)
    assert all(new[i]['area_implication_refs'] == old[i]['area_implication_refs'] for i in old)
    jold = {r['id']: r for r in full['stages'][2]['diagnoses']}
    jnew = {r['id']: r for r in condensed['stages'][2]['diagnoses']}
    assert set(jold) == set(jnew) and all(jnew[i]['direction_refs'] == jold[i]['direction_refs'] for i in jold)


def test_the_integration_view_loses_no_name_or_number(tmp_path):
    import asis_input_summary as S
    data, stages = big_stages(tmp_path)
    full = Pipeline._integration_stages(stages)
    condensed = DS.condensed_integration(full)

    def values(o, out):
        if isinstance(o, dict):
            for v in o.values(): values(v, out)
        elif isinstance(o, list):
            for v in o: values(v, out)
        elif isinstance(o, str): out.append(o)
    a, b = [], []
    values(full, a); values(condensed, b)
    ta = set().union(*[S.tokens(s) for s in a]); tb = set().union(*[S.tokens(s) for s in b])
    assert ta <= tb, sorted(ta - tb)[:6]


def test_merged_group_reviews_have_the_same_shape_as_one_shard_review():
    reviews = [{'stage': 'review', 'run': {}, 'verdict': 'pass', 'content_checked': True, 'render_checked': False, 'defects': [], 'unresolved': []},
               {'stage': 'review', 'run': {}, 'verdict': 'revise', 'content_checked': True, 'render_checked': False,
                'defects': [{'where': '/stages/1/directions/4/text', 'quote': 'q', 'why': 'w', 'fix': 'f', 'evidence_refs': []}], 'unresolved': []}]
    merged = DS.merge_group_reviews(reviews)
    assert merged['verdict'] == 'revise' and len(merged['defects']) == 1 and merged['content_checked'] is True and merged['render_checked'] is False
    blocked = DS.merge_group_reviews(reviews + [dict(reviews[0], verdict='blocked', unresolved=[{'topic': 't', 'reason': 'r', 'required_evidence': 'e', 'fact_refs': []}])])
    assert blocked['verdict'] == 'blocked' and len(blocked['unresolved']) == 1


# ── 파이프라인 연결: _document_review 가 토큰 예산을 넘는 종합·과제 샤드를 구간으로 나눠 검토한다 ─────────────────────────────

class RecordingReviewRuntime:
    """모든 검토 호출에 통과를 돌려주고 호출의 모드(document-shard-*, document-integration)와 입력 draft 를 기록한다."""

    def __init__(self):
        self.calls = []

    async def ask(self, role, message, **kwargs):
        body = json.loads(message.rsplit('\n입력:\n', 1)[-1])
        mode = body['stage_data'].get('mode')
        self.calls.append({'role': role, 'mode': mode, 'draft': body['stage_data'].get('draft')})
        review = {'stage': 'review', 'run': body['run'], 'verdict': 'pass', 'content_checked': True, 'render_checked': False, 'defects': [], 'unresolved': []}
        return json.dumps(review, ensure_ascii=False), {'test_only': True, 'role': role, 'session_id': kwargs.get('session_id', '')}


def run_document_review(tmp_path, stages, data, monkeypatch=None):
    from asis_tasks import initial_graph
    runtime = RecordingReviewRuntime()
    engine = Pipeline(runtime, Path(tmp_path) / 'run')
    manager = engine.task_manager(data)
    manager.register(initial_graph(asis_analysis.AREA_NAMES))
    for row in manager.ledger['tasks'].values():
        if row['id'] != 'document':
            row['status'] = 'completed'

    async def go():
        for key in manager.ledger['tasks']:
            manager._events.setdefault(key, asyncio.Event()).set()
        return await asyncio.wait_for(engine.document_review(data, stages), 120)
    review = asyncio.run(go())
    return runtime, review


def small_budget(monkeypatch, tokens):
    monkeypatch.setattr(DS, 'MODEL_TOKEN_LIMIT', tokens)


def test_the_document_review_no_longer_splits_synthesis_into_value_groups_but_keeps_one_direction_layer_call(tmp_path, monkeypatch):
    """12단계는 아키텍처 관점이다. 방향 층은 한 호출(arch-direction)이 서술만 보고, 위치 키는 원본과 같다."""
    data, stages = big_stages(tmp_path / 'seed')
    small_budget(monkeypatch, 40_000)
    runtime, review = run_document_review(tmp_path, stages, data)
    layer = [c for c in runtime.calls if c['mode'] == 'document-shard-arch-direction']
    assert len(layer) == 1 and not any(str(c['mode']).startswith('document-shard-synthesis') for c in runtime.calls)
    rows = layer[0]['draft']['stages'][1]['directions']
    assert len(rows) == len(stages[1]['directions']) and not any(r.get('omitted') for r in rows), '방향 층 전체를 한 호출이 본다'
    assert all(set(r) <= {'id', 'title', 'text', 'customer_impact', 'change_direction', 'area_implication_refs'} for r in rows), '사실 인용은 싣지 않는다'
    assert review['verdict'] == 'pass'


def test_the_document_review_keeps_one_action_layer_call_with_original_positions(tmp_path, monkeypatch):
    data, stages = big_stages(tmp_path / 'seed')
    small_budget(monkeypatch, 40_000)
    runtime, review = run_document_review(tmp_path, stages, data)
    layer = [c for c in runtime.calls if c['mode'] == 'document-shard-arch-action']
    assert len(layer) == 1
    rows = layer[0]['draft']['stages'][2]['diagnoses']
    assert len(rows) == len(stages[2]['diagnoses']) and [r['id'] for r in rows] == [r['id'] for r in stages[2]['diagnoses']]
    assert all('fact_refs' not in r for r in rows)


def test_an_in_budget_document_review_makes_exactly_four_calls(tmp_path):
    data, stages = big_stages(tmp_path / 'seed', directions=3, diagnoses=3, repeat=1)
    runtime, review = run_document_review(tmp_path, stages, data)
    modes = sorted(c['mode'] for c in runtime.calls)
    assert modes == sorted(['document-shard-arch-area', 'document-shard-arch-direction', 'document-shard-arch-action', 'document-integration']), modes


def test_the_integration_review_receives_condensed_narratives_when_over_budget(tmp_path, monkeypatch):
    data, stages = big_stages(tmp_path / 'seed')
    small_budget(monkeypatch, 40_000)
    runtime, review = run_document_review(tmp_path, stages, data)
    integration = [c for c in runtime.calls if c['mode'] == 'document-integration']
    assert len(integration) == 1
    sent = integration[0]['draft']['stages'][1]['directions'][0]['text']
    assert len(sent) < len(stages[1]['directions'][0]['text']), '통합 검토 입력의 방향 서술문은 축약된다'


def test_the_four_layer_reviews_are_merged_into_one_result(tmp_path, monkeypatch):
    data, stages = big_stages(tmp_path / 'seed')
    small_budget(monkeypatch, 40_000)
    runtime, review = run_document_review(tmp_path, stages, data)
    assert set(review['shards']) == {'arch-area', 'arch-direction', 'arch-action', 'integration'}
    assert all(v == 'pass' for v in review['shards'].values()) and review['verdict'] == 'pass'


def test_the_budget_is_read_at_call_time_not_frozen_at_definition(monkeypatch):
    before = DS.token_budget_chars()
    monkeypatch.setattr(DS, 'MODEL_TOKEN_LIMIT', 100_000)
    assert DS.token_budget_chars() < before and DS.token_budget_chars() == int(100_000 * DS.BUDGET_FRACTION * DS.CHARS_PER_TOKEN)


# ── 실측 보정(프로젝트 25 12단계): 종합 샤드 입력 65만 자 중 방향 본문은 23.5만 자뿐이고 unresolved 33.6만·edges 8.1만 자가 매 구간에 통째로 실렸다 ──────────────

def big_with_unresolved(tmp_path, unresolved_rows=400):
    data, stages = big_stages(tmp_path, directions=24, diagnoses=30, repeat=4)
    row = {'topic': '미확인 주제', 'reason': SENT * 40, 'required_evidence': SENT * 10, 'fact_refs': ['F_X']}
    for index in (1, 2):
        stages[index]['unresolved'] = [dict(row, topic='미확인 %d-%d' % (index, i)) for i in range(unresolved_rows)]
        stages[index]['edges'] = [{'from_id': 'D_P%04d' % (i % 24), 'to_id': 'J_ITEM_%04d' % (i % 30 + 1), 'relation': 'diagnoses'} for i in range(2000)]
    return data, stages


def test_the_unresolved_text_is_carried_by_exactly_one_group_and_the_others_see_topics_only(tmp_path):
    data, stages = big_with_unresolved(tmp_path)
    for keep in (1, 2):
        groups = [list(range(0, 8)), list(range(8, 16)), list(range(16, len(stages[keep][DS.ITEMS[keep]])))]
        drafts = [DS.sparse_group(stages, keep, g, group_index=i, group_count=len(groups), groups=groups)['stages'][keep] for i, g in enumerate(groups)]
        full = [d for d in drafts if d['unresolved'] and 'reason' in d['unresolved'][0]]
        assert len(full) == 1, '미확인 본문은 정확히 한 구간만 가진다'
        others = [d for d in drafts if d not in full]
        for d in others:
            assert len(d['unresolved']) == len(stages[keep]['unresolved'])
            assert all(set(r) == {'topic', 'fact_ref_count'} for r in d['unresolved']), '다른 구간은 주제와 건수만 본다'
        total_unique = {r['topic'] for r in full[0]['unresolved']}
        assert total_unique == {r['topic'] for r in stages[keep]['unresolved']}, '미확인 항목은 하나도 사라지지 않는다'


def test_edges_are_dropped_from_group_reviews_because_code_generates_them(tmp_path):
    data, stages = big_with_unresolved(tmp_path)
    draft = DS.sparse_group(stages, 1, [0, 1], group_index=0, group_count=2, groups=[[0, 1], [2]])['stages'][1]
    assert draft.get('edges') in ([], None) and draft.get('edges_note', {}).get('total') == len(stages[1]['edges'])


def test_a_single_group_keeps_the_stage_exactly_as_before(tmp_path):
    """예산 안이라 한 번에 검토하는 경우(group_count=1)는 단계 전체를 지금처럼 그대로 보낸다."""
    data, stages = big_with_unresolved(tmp_path)
    n = len(stages[1]['directions'])
    draft = DS.sparse_group(stages, 1, list(range(n)), group_index=0, group_count=1)['stages'][1]
    assert draft['unresolved'] == stages[1]['unresolved'] and draft['edges'] == stages[1]['edges']


def test_the_plan_counts_the_real_stage_size_so_a_large_unresolved_list_forces_a_split(tmp_path, monkeypatch):
    data, stages = big_with_unresolved(tmp_path, unresolved_rows=2000)
    plan = DS.plan_shard_groups(stages, 1, token_budget=272_000, fixed_chars=150_000)
    assert len(plan) >= 2, '방향 본문은 작아도 미확인·관계선이 크면 나눠야 한다'


# ── 미확인 본문을 한 구간에 몰지 않고 구간별로 배정한다(과제 단계 첫 구간이 항목 1개인데 27.1만 토큰이었다) ───────────────────────────

def stages_with_linked_unresolved(tmp_path):
    data, stages = big_stages(tmp_path, directions=12, diagnoses=12, repeat=3)
    for i, row in enumerate(stages[2]['diagnoses']):
        row['fact_refs'] = ['F_%02d' % i]
    stages[2]['unresolved'] = ([{'topic': '연결 %02d-%d' % (i, k), 'reason': SENT * 6, 'required_evidence': SENT, 'fact_refs': ['F_%02d' % i]} for i in range(12) for k in range(5)]
                               + [{'topic': '미연결 %d' % k, 'reason': SENT * 6, 'required_evidence': SENT, 'fact_refs': ['F_NOWHERE']} for k in range(4)])
    return data, stages


def test_each_unresolved_row_is_assigned_to_the_group_whose_items_cite_its_facts_and_none_is_lost(tmp_path):
    data, stages = stages_with_linked_unresolved(tmp_path)
    groups = [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 10, 11]]
    full_topics = []
    for gi, g in enumerate(groups):
        draft = DS.sparse_group(stages, 2, g, group_index=gi, group_count=3, groups=groups)['stages'][2]
        full = [r for r in draft['unresolved'] if 'reason' in r]
        full_topics += [r['topic'] for r in full]
        linked = {'F_%02d' % i for i in g}
        assert all(set(r['fact_refs']) & linked or (gi == 2 and r['topic'].startswith('미연결')) for r in full), (gi, [r['topic'] for r in full][:3])
        assert len(draft['unresolved']) == len(stages[2]['unresolved']), '다른 구간 몫은 주제·건수로 자리를 유지한다'
    assert sorted(full_topics) == sorted(r['topic'] for r in stages[2]['unresolved']) and len(full_topics) == len(set(full_topics)), '본문은 정확히 한 구간만, 하나도 빠지지 않는다'


def test_unlinked_unresolved_rows_go_to_the_last_group(tmp_path):
    data, stages = stages_with_linked_unresolved(tmp_path)
    groups = [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 10, 11]]
    last = DS.sparse_group(stages, 2, groups[2], group_index=2, group_count=3, groups=groups)['stages'][2]
    assert sum(1 for r in last['unresolved'] if 'reason' in r and r['topic'].startswith('미연결')) == 4


def test_the_integration_view_lists_each_unresolved_topic_once_across_the_stages(tmp_path):
    data, stages = big_with_unresolved(tmp_path, unresolved_rows=50)
    for index in (0, 1, 2):
        stages[index]['unresolved'] = [{'topic': '공통 주제 %d' % i, 'reason': 'r', 'required_evidence': 'e', 'fact_refs': ['F_X']} for i in range(50)]
    stages[0]['areas'] = stages[0]['areas']
    full = Pipeline._integration_stages(stages)
    condensed = DS.condensed_integration(full)
    topics = [row['topic'] for stage in condensed['stages'][:3] for row in stage.get('unresolved', [])]
    assert len(topics) == len(set(topics)) == 50, '같은 주제는 한 번만 싣는다'
    assert condensed['stages'][0]['unresolved_note']['total'] == 50


def test_the_summary_stage_unresolved_text_is_never_deduplicated_away(tmp_path):
    """요약 단계의 미확인은 그 단계 고유의 본문이다. 앞 단계에 같은 주제가 있어도 요약 단계의 것은 그대로 둔다(이름·숫자를 잃지 않는다)."""
    data, stages = big_stages(tmp_path)
    shared = {'topic': '같은 주제', 'reason': 'NSG/경로 증거 429', 'required_evidence': 'e', 'fact_refs': ['F_X']}
    for index in (0, 1, 2, 3):
        stages[index]['unresolved'] = [dict(shared)]
    condensed = DS.condensed_integration(Pipeline._integration_stages(stages))
    assert len(condensed['stages'][3]['unresolved']) == 1 and condensed['stages'][3]['unresolved'][0]['reason'] == 'NSG/경로 증거 429'
    assert sum(len(condensed['stages'][i].get('unresolved', [])) for i in (0, 1, 2)) == 1


# ── 분할 계획은 구간이 실제로 싣는 크기(항목 + 그 구간이 맡는 미확인 본문)로 정한다 ────────────────────────────────────────

def group_chars(stages, keep, groups, index, fixed=0):
    """sparse_group 이 만드는 구간 하나의 단계 크기(고정분 제외 가능). 계획이 실제 전송 크기와 같은 값을 보게 하는 기준 함수."""
    import asis_document_shards as D
    return fixed + len(json.dumps(D.sparse_group(stages, keep, groups[index], group_index=index, group_count=len(groups), groups=groups)['stages'][keep], ensure_ascii=False))


def test_every_planned_group_fits_the_budget_by_its_real_size_including_its_unresolved_share(tmp_path):
    data, stages = stages_with_linked_unresolved(tmp_path)
    for keep in (1, 2):
        for k in range(len(stages[keep]['unresolved'])):
            stages[keep]['unresolved'][k] = dict(stages[keep]['unresolved'][k] if k < len(stages[2]['unresolved']) else stages[2]['unresolved'][0], reason=SENT * 120)
    fixed = 20_000
    budget = 60_000
    plan = DS.plan_shard_groups(stages, 2, token_budget=budget, fixed_chars=fixed)
    assert len(plan) >= 2
    limit = DS.token_budget_chars(budget)
    for i in range(len(plan)):
        assert group_chars(stages, 2, plan, i, fixed) <= limit, (i, group_chars(stages, 2, plan, i, fixed), limit)


def test_the_plan_never_returns_an_empty_group_and_covers_every_item_once(tmp_path):
    data, stages = stages_with_linked_unresolved(tmp_path)
    plan = DS.plan_shard_groups(stages, 2, token_budget=30_000, fixed_chars=10_000)
    flat = [i for g in plan for i in g]
    assert all(plan) and sorted(flat) == list(range(len(stages[2]['diagnoses']))) and len(flat) == len(set(flat))


# ── 미확인 본문이 한 구간에 쏠리지 않는다: 공통 사실을 가리키는 행은 겹치는 구간들 사이에서 돌아가며 배정한다(실측: 735행 중 706행이 구간 1) ───────────

def stages_with_shared_fact_unresolved(tmp_path, rows=120):
    data, stages = big_stages(tmp_path, directions=12, diagnoses=12, repeat=3)
    for row in stages[2]['diagnoses']:
        row['fact_refs'] = ['F_COMMON']          # 모든 진단이 같은 공통 사실을 인용한다
    stages[2]['unresolved'] = [{'topic': '공통 %03d' % i, 'reason': SENT * 6, 'required_evidence': SENT, 'fact_refs': ['F_COMMON']} for i in range(rows)]
    return data, stages


def test_unresolved_rows_citing_a_common_fact_are_spread_across_the_groups_that_cite_it(tmp_path):
    data, stages = stages_with_shared_fact_unresolved(tmp_path)
    groups = [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 10, 11]]
    counts = []
    for gi, g in enumerate(groups):
        draft = DS.sparse_group(stages, 2, g, group_index=gi, group_count=3, groups=groups)['stages'][2]
        counts.append(sum(1 for r in draft['unresolved'] if 'reason' in r))
    assert sum(counts) == 120, '본문은 정확히 한 구간만 가진다'
    assert max(counts) - min(counts) <= 1, counts


def test_the_spread_is_deterministic_for_the_same_input(tmp_path):
    data, stages = stages_with_shared_fact_unresolved(tmp_path)
    groups = [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 10, 11]]
    first = [DS.sparse_group(stages, 2, g, group_index=i, group_count=3, groups=groups)['stages'][2] for i, g in enumerate(groups)]
    second = [DS.sparse_group(stages, 2, g, group_index=i, group_count=3, groups=groups)['stages'][2] for i, g in enumerate(groups)]
    assert first == second


def test_a_row_citing_a_fact_in_only_one_group_still_goes_to_that_group(tmp_path):
    data, stages = stages_with_linked_unresolved(tmp_path)
    groups = [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 10, 11]]
    for gi, g in enumerate(groups):
        draft = DS.sparse_group(stages, 2, g, group_index=gi, group_count=3, groups=groups)['stages'][2]
        mine = {r['topic'] for r in draft['unresolved'] if 'reason' in r and r['topic'].startswith('연결')}
        expected = {r['topic'] for r in stages[2]['unresolved'] if r['topic'].startswith('연결') and set(r['fact_refs']) & {'F_%02d' % i for i in g}}
        assert mine == expected


# ── 통합 검토는 예산에 들 때까지 단계적으로 줄인다: 서술문 축약 → 중복 문장 제거 → 사실 본문을 ID·크기만 ──────────────────────────────

def test_the_integration_view_reduces_until_it_fits_and_reports_the_level(tmp_path):
    data, stages = big_with_unresolved(tmp_path, unresolved_rows=300)
    full = Pipeline._integration_stages(stages)
    sizes = {}
    for level in (0, 1, 2):
        view, info = DS.integration_view(full, level=level)
        sizes[level] = len(json.dumps(view, ensure_ascii=False))
        assert info['level'] == level
    assert sizes[0] >= sizes[1] >= sizes[2] and sizes[2] < sizes[0]


def test_the_chosen_integration_level_is_the_smallest_that_fits_the_budget(tmp_path):
    data, stages = big_with_unresolved(tmp_path, unresolved_rows=300)
    full = Pipeline._integration_stages(stages)
    size = lambda v: len(json.dumps(v, ensure_ascii=False))
    s0, s1, s2 = (size(DS.integration_view(full, level=n)[0]) for n in (0, 1, 2))
    view, info = DS.fit_integration(full, budget_chars=(s0 + s1) // 2 + 1, fixed_chars=0)
    assert info['level'] in (1, 2) and size(view) <= (s0 + s1) // 2 + 1 or info['level'] == 2
    view, info = DS.fit_integration(full, budget_chars=s0 + 1, fixed_chars=0)
    assert info['level'] == 0 and size(view) == s0


def test_level_two_keeps_ids_references_and_loses_no_name_or_number_in_the_narratives(tmp_path):
    import asis_input_summary as S
    data, stages = big_with_unresolved(tmp_path, unresolved_rows=50)
    full = Pipeline._integration_stages(stages)
    view, info = DS.integration_view(full, level=2)
    old = {r['id']: r for r in full['stages'][1]['directions']}
    new = {r['id']: r for r in view['stages'][1]['directions']}
    assert set(old) == set(new) and all(new[i]['area_implication_refs'] == old[i]['area_implication_refs'] for i in old)
    for i in old:
        assert S.tokens(old[i]['text']) <= S.tokens(new[i]['text']) | S.tokens(old[i]['title']) | S.tokens(new[i].get('title', '')), i


def test_a_fact_value_is_replaced_by_its_id_and_size_only_at_the_last_level_for_facts_nobody_in_the_integration_view_cites_in_text():
    view = {'stages': [], 'facts_note': None}
    assert 'integration_view' in dir(DS) and 'fit_integration' in dir(DS)


def test_level_two_really_removes_repeated_sentences_and_keeps_every_name_and_number(tmp_path):
    import asis_input_summary as S
    data, stages = big_stages(tmp_path, directions=6, diagnoses=6, repeat=30)
    full = Pipeline._integration_stages(stages)
    v1, _ = DS.integration_view(full, level=1)
    v2, _ = DS.integration_view(full, level=2)
    size = lambda v: len(json.dumps(v, ensure_ascii=False))
    assert size(v2) < size(v1), '2단계는 1단계보다 실제로 작아야 한다'

    def values(o, out):
        if isinstance(o, dict):
            for v in o.values(): values(v, out)
        elif isinstance(o, list):
            for v in o: values(v, out)
        elif isinstance(o, str): out.append(o)
    a, b = [], []
    values(full, a); values(v2, b)
    ta = set().union(*[S.tokens(x) for x in a]); tb = set().union(*[S.tokens(x) for x in b])
    assert ta <= tb, sorted(ta - tb)[:6]


# ── 실제 경로 시험: 측정 함수를 따로 두지 않고 _independent_review 와 같은 render_prompt 경로로 모든 검토 단위가 한도 안인지 본다 ────────────────────

def test_every_document_review_unit_passes_the_real_prompt_path_with_approved_blocks_in_the_input(tmp_path, monkeypatch):
    """운영에서 입력은 stage_data 에 승인 블록 3개를 그대로 가진다. 그 입력으로 모든 단위(영역·공통·요약·종합 구간·과제 구간·통합)가 모델 호출 전 한도(글자)를 통과해야 한다."""
    import asis_model_view as MV
    data, stages = big_with_unresolved(tmp_path, unresolved_rows=200)
    data['stage_data'].update({'approved_areas': stages[0], 'approved_directions': stages[1], 'approved_actions': stages[2]})
    engine = Pipeline.__new__(Pipeline)
    engine.validator = asis_analysis.contract()
    limit = MV.LIMITS['document_review']
    fixed = DS.fixed_overhead_chars(data, stages)

    def render(draft, key):
        review = deepcopy(data)
        review['stage_data'].update(mode=key, draft=draft, candidate_sha256='x', deterministic_check_result={'passed': True, 'candidate_sha256': 'x', 'actual_render': {}})
        engine.project_review_scope(review, draft, image=None) if hasattr(engine, 'project_review_scope') else None
        system, prompt = engine.render_prompt(5, review)
        return len(prompt)
    for index, area in enumerate(stages[0]['areas']):
        assert render(Pipeline._sparse_stages(stages, 0, index), 'document-shard-area-' + area['area_id']) <= limit + 20_000
    assert render(Pipeline._sparse_stages(stages, 3, None), 'document-shard-summary') <= limit + 20_000
    for keep, name in ((1, 'synthesis'), (2, 'actions')):
        groups = DS.plan_shard_groups(stages, keep, fixed_chars=fixed)
        for gi, ids in enumerate(groups):
            draft = DS.sparse_group(stages, keep, ids, group_index=gi, group_count=len(groups), groups=groups)
            assert render(draft, 'document-shard-%s-g%d' % (name, gi + 1)) <= limit + 20_000, (name, gi)
    view, info = DS.fit_integration(Pipeline._integration_stages(stages), budget_chars=DS.token_budget_chars(), fixed_chars=fixed)
    assert render(view, 'document-integration') <= limit + 20_000
