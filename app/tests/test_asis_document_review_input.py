"""문서 검토 입력: 후보(draft.stages)와 그것이 인용한 근거만 보내고, 배경 원장과 미확인 본문은 보내지 않는다.

운영 실측(2026-10-16): 요약이 통과한 뒤 문서 검토 15개 호출이 모두 모델 입력 한도(92.2만 토큰)를 넘어 서비스가 400으로 거절했고 개정이 중지됐다.
후보는 9천 자인데 배경 context 113만 자와 사실 50만 자가 실렸고, 영역 간 통합 검토 후보에는 미확인 본문 84만 자가 들어 있었다.
"""
from copy import deepcopy
from pathlib import Path
import json, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
import asis_analysis
import asis_model_view as mv
from asis_pipeline import Pipeline
from test_asis_summary_input import summary_data


def stages_of(data):
    sd = data['stage_data']
    return [sd['approved_areas'], sd['approved_directions'], sd['approved_actions'],
            {'stage': 'summary', 'run': data['run'], 'summary': {'headline': 'h', 'message': 'm', 'key_points': [], 'direction_refs': [], 'action_refs': [], 'decision_points': []}, 'unresolved': []}]


def review_input(data, draft, key):
    review = deepcopy(data)
    review['stage_data'] = {'mode': key, 'draft': draft}
    return review


def engine():
    pipe = Pipeline.__new__(Pipeline)
    pipe.validator = asis_analysis.contract()
    return pipe


def test_the_document_review_calls_are_recognised_by_their_task_key(tmp_path):
    data = summary_data(tmp_path)
    pipe = engine()
    stages = stages_of(data)
    for key, draft in (('document-shard-area-network', pipe._sparse_stages(stages, 0, 0)), ('document-shard-shared', pipe._sparse_stages(stages, 0, -1)),
                       ('document-shard-synthesis', pipe._sparse_stages(stages, 1, None)), ('document-shard-actions', pipe._sparse_stages(stages, 2, None)),
                       ('document-shard-summary', pipe._sparse_stages(stages, 3, None)), ('document-integration', pipe._integration_stages(stages))):
        assert mv.kind_of(5, review_input(data, draft, key)) == 'document_review', key
    assert mv.kind_of(5, review_input(data, pipe._sparse_stages(stages, 0, 0), 'visual-pages')) is None


def test_every_document_review_input_is_under_the_limit_while_the_raw_input_is_not(tmp_path):
    """운영과 같은 경로: 값을 대조하는 구간(종합·과제)은 항목 구간으로 나뉘고 통합은 단계적으로 줄어든다. 단일 샤드가 한도를 넘는 것은 구간 분할이 해결한다(test_asis_document_shards)."""
    import asis_document_shards as DS
    data = summary_data(tmp_path, unresolved=900, directions=93, fact_chars=60_000)
    pipe = engine()
    stages = stages_of(data)
    assert len(json.dumps(data, ensure_ascii=False)) > mv.LIMITS['document_review']
    fixed = DS.fixed_overhead_chars(data, stages)
    drafts = [('document-shard-area-' + area['area_id'], pipe._sparse_stages(stages, 0, index)) for index, area in enumerate(stages[0]['areas'])]
    drafts += [('document-shard-shared', pipe._sparse_stages(stages, 0, -1)), ('document-shard-summary', pipe._sparse_stages(stages, 3, None))]
    for keep, name in ((1, 'synthesis'), (2, 'actions')):
        groups = DS.plan_shard_groups(stages, keep, fixed_chars=fixed)
        drafts += [('document-shard-%s-g%d' % (name, i + 1), DS.sparse_group(stages, keep, g, group_index=i, group_count=len(groups), groups=groups)) for i, g in enumerate(groups)]
    view, info = DS.fit_integration(pipe._integration_stages(stages), budget_chars=DS.token_budget_chars(), fixed_chars=fixed)
    drafts.append(('document-integration', view))
    for key, draft in drafts:
        system, prompt = pipe.render_prompt(5, review_input(data, draft, key))
        assert len(prompt) < mv.LIMITS['document_review'] + 20_000, (key, len(prompt))
        assert 'collection_basis_matches' not in prompt and 'consolidation_trace' not in prompt, key


def test_the_integration_review_sees_unresolved_topics_but_not_their_text(tmp_path):
    data = summary_data(tmp_path, unresolved=500)
    brief = engine()._integration_stages(stages_of(data))['stages']
    for stage in brief[:3]:
        rows = stage['unresolved']
        assert len(rows) == 500 and all(set(row) == {'topic', 'fact_ref_count'} for row in rows)
        assert stage['unresolved_note']['total'] == 500


def test_shard_reviews_keep_the_unresolved_text_the_integration_review_leaves_out(tmp_path):
    """미확인 본문은 사라지지 않는다: 영역·종합·과제 샤드 검토가 그대로 본다."""
    data = summary_data(tmp_path, unresolved=50)
    draft = engine()._sparse_stages(stages_of(data), 1, None)
    rows = draft['stages'][1]['unresolved']
    assert rows and all('reason' in row for row in rows)


def test_a_fact_the_candidate_cites_is_sent_and_an_uncited_one_is_not(tmp_path):
    data = summary_data(tmp_path)
    pipe = engine()
    draft = pipe._sparse_stages(stages_of(data), 1, None)
    view = mv.document_review_view(review_input(data, draft, 'document-shard-synthesis'))
    cited = mv.refs_in(draft, 'fact_refs')
    ids = {row['id'] for row in view['facts']}
    assert cited and cited <= ids and ids <= cited


def test_the_canonical_input_and_stages_are_not_modified(tmp_path):
    data = summary_data(tmp_path)
    stages = stages_of(data)
    before = json.dumps([data, stages], sort_keys=True, ensure_ascii=False)
    pipe = engine()
    pipe._integration_stages(stages)
    mv.document_review_view(review_input(data, pipe._sparse_stages(stages, 1, None), 'document-shard-synthesis'))
    assert json.dumps([data, stages], sort_keys=True, ensure_ascii=False) == before


# ── 운영 2026-10-19(프로젝트 25 12단계): 문서 검토 14개 호출이 모두 197만~257만 자였다. stage_data 의 승인 블록 3개(195만 자)가 그대로 실렸다 ──────────

def test_the_document_review_view_drops_the_approved_blocks_that_only_duplicate_the_draft(tmp_path):
    data = summary_data(tmp_path, unresolved=300, directions=40, fact_chars=20_000)
    pipe = engine()
    stages = stages_of(data)
    draft = pipe._sparse_stages(stages, 0, 0)
    review = review_input(data, draft, 'document-shard-area-network')
    review['stage_data'].update({k: v for k, v in data['stage_data'].items() if k.startswith('approved_')})
    assert all(k in review['stage_data'] for k in ('approved_areas', 'approved_directions', 'approved_actions')), '시험 준비: 운영 입력과 같은 모양'
    view = mv.document_review_view(review)
    assert not any(k.startswith('approved_') for k in view['stage_data']), sorted(view['stage_data'])
    assert view['stage_data']['draft'] == draft, '검토 대상은 그대로다'
    assert view['stage_data']['mode'] == 'document-shard-area-network'


def test_the_view_size_no_longer_includes_the_approved_blocks(tmp_path):
    data = summary_data(tmp_path, unresolved=900, directions=93, fact_chars=60_000)
    pipe = engine()
    stages = stages_of(data)
    draft = pipe._sparse_stages(stages, 0, 0)
    review = review_input(data, draft, 'document-shard-area-network')
    review['stage_data'].update({k: v for k, v in data['stage_data'].items() if k.startswith('approved_')})
    approved = sum(len(json.dumps(review['stage_data'][k], ensure_ascii=False)) for k in review['stage_data'] if k.startswith('approved_'))
    view = mv.document_review_view(review)
    assert len(json.dumps(view['stage_data'], ensure_ascii=False)) < approved / 4


def test_the_canonical_input_is_not_changed_by_building_the_view(tmp_path):
    data = summary_data(tmp_path, unresolved=50, directions=10)
    pipe = engine()
    stages = stages_of(data)
    review = review_input(data, pipe._sparse_stages(stages, 0, 0), 'document-shard-area-network')
    review['stage_data'].update({k: v for k, v in data['stage_data'].items() if k.startswith('approved_')})
    before = json.dumps(review, sort_keys=True, ensure_ascii=False)
    mv.document_review_view(review)
    assert json.dumps(review, sort_keys=True, ensure_ascii=False) == before


# ── 운영 2026-10-19(프로젝트 25 12단계): 영역 검토 9개가 모두 blocked(지적 0·미확인 26건): '사실 값이 not_inlined 로 제공되어 대조할 수 없다' ──────────

def _fact_of(size):
    return {'id': 'F_BIG', 'source_id': 'S', 'field_path': '/x', 'value': ['행 %d ' % i + 'x' * 40 for i in range(max(1, size // 50))]}


def test_the_document_review_view_sends_the_cited_fact_value_even_when_it_is_large(tmp_path):
    """검토자가 작성자의 구체 값(배포 SKU·정책 참조·메트릭 차원)을 대조할 수 있어야 한다. 본문 값은 후보가 인용한 사실에 한한다(8단계 _review_scope 와 같은 원칙)."""
    data = summary_data(tmp_path, unresolved=5, directions=3)
    data['facts'].append(_fact_of(40_000))
    pipe = engine()
    stages = stages_of(data)
    draft = pipe._sparse_stages(stages, 0, 0)
    draft['stages'][0]['areas'][0]['observations'] = [{'id': 'O_X', 'text': 't', 'fact_refs': ['F_BIG']}]
    review = review_input(data, draft, 'document-shard-area-network')
    view = mv.document_review_view(review)
    big = next(f for f in view['facts'] if f['id'] == 'F_BIG')
    assert not (isinstance(big['value'], dict) and big['value'].get('not_inlined')), '인용된 큰 사실도 값을 보낸다'
    assert 'F_BIG' in view['context']['model_scope']['inline_fact_refs']


def test_a_fact_nobody_cites_is_still_left_out_of_the_document_review_view(tmp_path):
    data = summary_data(tmp_path, unresolved=5, directions=3)
    data['facts'].append(_fact_of(40_000))
    pipe = engine()
    stages = stages_of(data)
    review = review_input(data, pipe._sparse_stages(stages, 0, 0), 'document-shard-area-network')
    view = mv.document_review_view(review)
    assert 'F_BIG' not in {f['id'] for f in view['facts']}


def test_the_integration_and_the_synthesis_groups_keep_large_facts_out_because_they_check_structure_not_values(tmp_path):
    """통합 검토는 영역 간 일관성만 본다(사실 값 대조는 영역·종합·과제 샤드의 몫). 통합 입력에는 큰 사실 본문을 싣지 않는다."""
    data = summary_data(tmp_path, unresolved=5, directions=3)
    data['facts'].append(_fact_of(40_000))
    pipe = engine()
    stages = stages_of(data)
    stages[0]['areas'][0]['implication']['fact_refs'] = ['F_BIG']
    review = review_input(data, pipe._integration_stages(stages), 'document-integration')
    view = mv.document_review_view(review)
    for f in view['facts']:
        if f['id'] == 'F_BIG':
            assert isinstance(f['value'], dict) and f['value'].get('not_inlined')


# ── 운영 2026-10-19(12차 재개): 값 전송을 종합·과제 구간까지 넓혀 구간 입력이 92.8만 자 > 90만으로 실패했다. 값 대조는 영역 검토만 한다 ───────────────────

def test_synthesis_and_actions_group_reviews_keep_large_facts_as_ids_and_sizes(tmp_path):
    """종합·과제 구간은 방향·진단의 근거 관계를 본다. 사실 값 대조는 영역 검토의 몫이므로 큰 사실은 ID·크기만 보낸다(11차에서 한도 안으로 모델에 도달한 상태)."""
    data = summary_data(tmp_path, unresolved=5, directions=3)
    data['facts'].append(_fact_of(40_000))
    pipe = engine()
    stages = stages_of(data)
    stages[1]['directions'][0]['fact_refs'] = ['F_BIG']
    stages[2]['diagnoses'][0]['fact_refs'] = ['F_BIG']
    for mode, keep in (('document-shard-synthesis-g1', 1), ('document-shard-actions-g2', 2), ('document-shard-synthesis', 1), ('document-shard-actions', 2)):
        review = review_input(data, pipe._sparse_stages(stages, keep, None), mode)
        view = mv.document_review_view(review)
        big = [f for f in view['facts'] if f['id'] == 'F_BIG']
        assert big and isinstance(big[0]['value'], dict) and big[0]['value'].get('not_inlined'), mode


def test_only_the_area_shards_receive_the_value_of_a_large_cited_fact(tmp_path):
    data = summary_data(tmp_path, unresolved=5, directions=3)
    data['facts'].append(_fact_of(40_000))
    pipe = engine()
    stages = stages_of(data)
    draft = pipe._sparse_stages(stages, 0, 0)
    draft['stages'][0]['areas'][0]['observations'] = [{'id': 'O_X', 'text': 't', 'fact_refs': ['F_BIG']}]
    for mode in ('document-shard-area-network', 'document-shard-area-secgov'):
        big = next(f for f in mv.document_review_view(review_input(data, draft, mode))['facts'] if f['id'] == 'F_BIG')
        assert not (isinstance(big['value'], dict) and big['value'].get('not_inlined')), mode


# ── 운영 2026-10-19(11·12차 재개): 12단계 영역 검토가 4단계에서 통과한 관찰 8건을 '수집 사유가 제시되지 않았다'며 반려했다. ──────────────────────────────
# 원인: document_review_view 가 요약 뷰 규칙(SUMMARY_DROP_CONTEXT)을 그대로 써서 collection_states·dataset_register·dataset_index·criterion_assessments 를 지웠다.
# 수집 사유(no-provisioned-deployments)와 수집 대상(AzureOpenAITimeToResponse P1D)은 사실 값이 아니라 collection_states 에만 있다. 4단계 검토 입력에는 있었다.

def _with_states(data):
    data['context']['collection_states'] = {
        'F_BIG': {'status': 'collected', 'reason': 'no-provisioned-deployments', 'api': 'Monitor metrics AzureOpenAIProvisionedManagedUtilizationV2 PT1H 30일', 'complete': False},
        'F_OTHER': {'status': 'collected', 'reason': 'other-reason-unrelated', 'api': 'unrelated api', 'complete': True}}
    data['context']['dataset_register'] = [{'fact_id': 'F_BIG', 'metadata': {'reason': 'no-provisioned-deployments'}}, {'fact_id': 'F_OTHER', 'metadata': {}}]
    data['context']['dataset_index'] = [{'fact_id': 'F_BIG', 'metadata': {}}, {'fact_id': 'F_OTHER', 'metadata': {}}]
    return data


def test_the_area_review_view_keeps_the_collection_state_of_the_facts_the_candidate_cites(tmp_path):
    """검토자가 수집 사유·수집 대상을 근거로 볼 수 있어야 한다. 4단계 검토 입력에는 있었다."""
    data = _with_states(summary_data(tmp_path, unresolved=5, directions=3))
    data['facts'].append(_fact_of(2_000))
    pipe = engine()
    stages = stages_of(data)
    draft = pipe._sparse_stages(stages, 0, 0)
    draft['stages'][0]['areas'][0]['observations'] = [{'id': 'O_X', 'text': 't', 'fact_refs': ['F_BIG']}]
    view = mv.document_review_view(review_input(data, draft, 'document-shard-area-ai'))
    states = view['context'].get('collection_states')
    assert isinstance(states, dict) and 'F_BIG' in states, '인용한 사실의 수집 상태가 검토 입력에 있다'
    assert states['F_BIG']['reason'] == 'no-provisioned-deployments'
    assert 'F_OTHER' not in states, '인용하지 않은 사실의 수집 상태는 싣지 않는다'


def test_the_area_review_view_keeps_the_dataset_rows_of_the_cited_facts_only(tmp_path):
    data = _with_states(summary_data(tmp_path, unresolved=5, directions=3))
    data['facts'].append(_fact_of(2_000))
    pipe = engine()
    stages = stages_of(data)
    draft = pipe._sparse_stages(stages, 0, 0)
    draft['stages'][0]['areas'][0]['observations'] = [{'id': 'O_X', 'text': 't', 'fact_refs': ['F_BIG']}]
    view = mv.document_review_view(review_input(data, draft, 'document-shard-area-ai'))
    for name in ('dataset_register', 'dataset_index'):
        rows = view['context'].get(name)
        assert isinstance(rows, list) and {row['fact_id'] for row in rows} == {'F_BIG'}, name


def test_the_other_review_modes_do_not_receive_the_collection_state(tmp_path):
    """종합·과제 구간·통합·요약은 수집 사유를 근거로 판단하지 않는다. 입력을 늘리지 않는다."""
    data = _with_states(summary_data(tmp_path, unresolved=5, directions=3))
    data['facts'].append(_fact_of(2_000))
    pipe = engine()
    stages = stages_of(data)
    stages[1]['directions'][0]['fact_refs'] = ['F_BIG']
    for mode, keep in (('document-shard-synthesis-g1', 1), ('document-shard-actions-g1', 2), ('document-shard-summary', 3)):
        view = mv.document_review_view(review_input(data, pipe._sparse_stages(stages, keep, None), mode))
        assert 'collection_states' not in view['context'], mode
