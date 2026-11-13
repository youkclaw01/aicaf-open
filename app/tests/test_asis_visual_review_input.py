"""16단계 장표(이미지) 검토의 입력: 실제 렌더 이미지와 그 장표의 텍스트·항목 연결만 보내고, 승인된 전체 stages 와 같은 본문을 되풀이하지 않는다.

운영 사전 측정(2026-10-19, 프로젝트 25): 16단계 장표 4장 묶음 검토 1회의 입력이 594만 자(246만 토큰)였다. 모델 한도 27.2만 토큰의 9배다.
후보 draft 에 장표 4장(0.8만 자)과 전체 stages(196만 자)를 통째로 싣고, stage_data 의 승인 블록 3개(195만 자)가 같은 본문을 또 실었다. 장표 수/4회 호출이 모두 같은 구조로 실패한다.
이미지 검토는 잘림·겹침·라벨·가독성을 보며 사실 값을 대조하지 않는다. 지시문·통과 조건은 바꾸지 않는다(입력의 중복 전송만 뺀다).
"""
from copy import deepcopy
from pathlib import Path
import json, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
import asis_model_view as mv
from test_asis_summary_input import summary_data
from test_asis_document_review_input import stages_of, engine, _fact_of


def _pages(count=4):
    return [{'page': i + 1, 'texts': ['장표 텍스트 %d' % i] * 12, 'png_sha256': 'x' * 64, 'item_refs': ['J_ITEM_0001'], 'width': 1280, 'height': 720} for i in range(count)]


def _visual_input(tmp_path, key='visual-r1-001', with_stages=True, name='a'):
    folder = tmp_path / name
    folder.mkdir(parents=True, exist_ok=True)
    data = summary_data(folder, unresolved=300, directions=60, fact_chars=60_000)
    stages = stages_of(data)
    review = deepcopy(data)
    draft = {'pages': _pages()}
    if with_stages:
        draft['stages'] = stages
    review['stage_data'] = {'mode': key, 'draft': draft, 'candidate_sha256': 'x', 'deterministic_check_result': {'passed': True, 'candidate_sha256': 'x', 'actual_render': {}},
                            'approved_areas': stages[0], 'approved_directions': stages[1], 'approved_actions': stages[2]}
    return data, stages, review


def test_a_visual_batch_review_is_recognised_by_its_key_and_the_page_draft(tmp_path):
    data, stages, review = _visual_input(tmp_path)
    assert mv.kind_of(5, review) == 'visual_review'
    # 17단계(항목별) 호출은 항목 배정이 있어 별도 종류다. 키 이름만으로 장표 묶음 검토와 혼동하지 않는다.
    item = _visual_input(tmp_path, key='item-visual-r1-diagnosis-J_ITEM_0001-001', name='b')[2]
    item['stage_data']['item_assignment'] = {'kind': 'diagnosis', 'item_id': 'J_ITEM_0001', 'slot': {}}
    assert mv.kind_of(5, item) != 'visual_review'


def test_the_visual_review_view_sends_the_pages_but_not_the_full_stages_or_the_approved_blocks(tmp_path):
    data, stages, review = _visual_input(tmp_path)
    view = mv.visual_review_view(review)
    sd = view['stage_data']
    assert sd['draft']['pages'] == review['stage_data']['draft']['pages'], '장표 4장은 그대로 보낸다'
    assert not any(k.startswith('approved_') for k in sd), '승인 블록은 draft 와 중복이다'
    assert 'stages' not in sd['draft'] or len(json.dumps(sd['draft']['stages'], ensure_ascii=False)) < 0.1 * len(json.dumps(stages, ensure_ascii=False)), '전체 stages 를 되풀이하지 않는다'


def test_the_visual_review_view_keeps_the_text_of_the_items_the_pages_belong_to(tmp_path):
    """장표의 텍스트가 항목의 서술과 맞는지 보려면 장표가 속한 항목(item_refs)의 서술은 필요하다."""
    data, stages, review = _visual_input(tmp_path)
    stages[2]['diagnoses'][0]['id'] = 'J_ITEM_0001'
    review['stage_data']['draft']['stages'] = stages
    view = mv.visual_review_view(review)
    kept = view['stage_data']['draft'].get('stages')
    ids = {row['id'] for stage in (kept or []) for row in (stage.get('diagnoses') or []) + (stage.get('actions') or []) if not row.get('omitted')}
    assert 'J_ITEM_0001' in ids


def test_the_visual_review_view_is_under_the_model_limit_on_a_large_input(tmp_path):
    data, stages, review = _visual_input(tmp_path)
    assert len(json.dumps(review, ensure_ascii=False)) > mv.LIMITS['document_review']
    pipe = engine()
    system, prompt = pipe.render_prompt(5, review)
    assert len(prompt) < mv.LIMITS['document_review'] + 20_000, len(prompt)


def test_the_canonical_input_is_not_changed_by_building_the_visual_view(tmp_path):
    data, stages, review = _visual_input(tmp_path)
    before = json.dumps(review, ensure_ascii=False, sort_keys=True)
    mv.visual_review_view(review)
    assert json.dumps(review, ensure_ascii=False, sort_keys=True) == before


def test_a_document_review_call_is_not_taken_for_a_visual_review(tmp_path):
    data, stages, review = _visual_input(tmp_path, key='document-shard-arch-area', name='c')
    review['stage_data']['draft'] = {'stages': stages}
    assert mv.kind_of(5, review) == 'document_review'


def test_a_fact_that_was_not_collected_keeps_a_null_value_so_the_input_contract_still_holds(tmp_path):
    """계약(validate_input): 수집되지 않은·거부된·실패한 사실의 value 는 반드시 null 이다. 크기 축약이 null 을 not_inlined 사전으로 바꾸면 모델 호출 전에 실패한다.
    (운영 사전 측정 2026-10-19: F_E_aiMetricsLatency unknown_value_must_be_null)"""
    data, stages, review = _visual_input(tmp_path, name='n')
    review['facts'].append({'id': 'F_NC', 'run': review['run'], 'source_id': 'S', 'field_path': '/x', 'collection_status': 'not_collected', 'scope_complete': False, 'value': None})
    diagnoses = review['stage_data']['draft']['stages'][2]['diagnoses']
    assert diagnoses, '시험 데이터에 진단이 있어야 한다'
    diagnoses[0]['fact_refs'] = ['F_NC']
    review['stage_data']['draft']['pages'][0]['item_refs'] = [diagnoses[0]['id']]
    view = mv.visual_review_view(review)
    row = next(f for f in view['facts'] if f['id'] == 'F_NC')
    assert row['value'] is None


def test_a_not_collected_fact_stays_null_in_the_document_review_view_too(tmp_path):
    from test_asis_document_review_input import review_input
    data = summary_data((tmp_path / 'd'), unresolved=5, directions=3) if (tmp_path / 'd').mkdir() is None else None
    data['facts'].append({'id': 'F_NC', 'run': data['run'], 'source_id': 'S', 'field_path': '/x', 'collection_status': 'not_collected', 'scope_complete': False, 'value': None})
    stages = stages_of(data)
    stages[1]['directions'][0]['fact_refs'] = ['F_NC']
    pipe = engine()
    for key in ('document-shard-arch-direction', 'document-integration'):
        view = mv.document_review_view(review_input(data, pipe._sparse_stages(stages, 1, None), key))
        rows = [f for f in view['facts'] if f['id'] == 'F_NC']
        assert all(f['value'] is None for f in rows), key


# ── 17단계: 항목별 장표(이미지) 검토. 작성·내용 검토(item_review)와 같은 경로를 공유하지만 후보에 pages 가 있다 ─────────────────────────────

def _item_visual_input(tmp_path, name='i'):
    data, stages, review = _visual_input(tmp_path, key='item-visual-r1-diagnosis-J_ITEM_0001-001', name=name)
    slot = {'id': 'J_ITEM_0001', 'fact_refs': [], 'criterion_refs': [], 'goal_refs': [], 'direction_refs': [], 'issue_refs': [], 'diagnosis_refs': [], 'scope': '시험'}
    review['stage_data'] = {'mode': 'actions', 'item_assignment': {'kind': 'diagnosis', 'item_id': 'J_ITEM_0001', 'slot': slot},
                            'draft': {'stage': 'actions', 'run': review['run'], 'diagnoses': [{'id': 'J_ITEM_0001', 'text': 't', 'impact': 'i', 'direction_refs': [], 'fact_refs': []}], 'actions': [], 'edges': [], 'unresolved': [],
                                      'pages': _pages(2)},
                            'candidate_sha256': 'x', 'deterministic_check_result': {'passed': True, 'candidate_sha256': 'x', 'actual_render': {}},
                            'approved_areas': stages[0], 'approved_directions': stages[1]}
    review['allocated_ids'] = {key: (['J_ITEM_0001'] if key == 'diagnoses' else []) for key in review.get('allocated_ids', {})}
    return data, stages, review


def test_the_item_visual_review_is_told_apart_from_the_item_content_review_by_the_pages_in_its_draft(tmp_path):
    data, stages, review = _item_visual_input(tmp_path)
    assert mv.kind_of(5, review) == 'item_visual_review'
    del review['stage_data']['draft']['pages']
    assert mv.kind_of(5, review) == 'item_review', '페이지가 없는 항목 검토는 작성·내용 검토 그대로다'


def test_the_item_visual_review_view_drops_bodies_and_duplicate_approved_blocks_but_keeps_the_item(tmp_path):
    data, stages, review = _item_visual_input(tmp_path, name='j')
    view = mv.item_visual_review_view(review)
    sd = view['stage_data']
    assert sd['draft']['diagnoses'][0]['id'] == 'J_ITEM_0001' and sd['draft']['pages'] == review['stage_data']['draft']['pages']
    assert sd['item_assignment']['item_id'] == 'J_ITEM_0001'
    assert 'approved_areas' not in sd and 'approved_directions' not in sd
    assert all(not (isinstance(f['value'], dict) is False and f['value'] is not None) for f in view['facts'])
    for name in ('collection_states', 'dataset_register', 'dataset_index', 'criterion_assessments'):
        assert name not in view['context']


def test_the_item_visual_review_input_is_under_the_model_limit_on_a_large_input(tmp_path):
    data, stages, review = _item_visual_input(tmp_path, name='k')
    pipe = engine()
    system, prompt = pipe.render_prompt(5, review)
    assert len(prompt) < mv.LIMITS['item_visual_review'] + 20_000, len(prompt)


# ── 변이 보강: 담당 외 항목의 본문과 큰 사실 본문이 실제로 빠지는지 직접 묻는다 ──────────────────────────────────────────────────────

def test_the_visual_review_view_omits_the_text_of_items_the_pages_do_not_belong_to(tmp_path):
    data, stages, review = _visual_input(tmp_path, name='o')
    diagnoses = review['stage_data']['draft']['stages'][2]['diagnoses']
    assert len(diagnoses) >= 2, '시험 데이터에 진단이 둘 이상 있어야 한다'
    review['stage_data']['draft']['pages'][0]['item_refs'] = [diagnoses[0]['id']]
    view = mv.visual_review_view(review)
    kept = view['stage_data']['draft']['stages'][2]['diagnoses']
    assert 'text' in kept[0] and kept[0]['id'] == diagnoses[0]['id'], '담당 항목은 서술을 유지한다'
    assert all(row.get('omitted') and 'text' not in row for row in kept[1:]), '담당 외 항목은 id 와 omitted 표시만 남긴다'


def test_a_large_cited_fact_value_is_replaced_by_its_size_in_the_visual_review_views(tmp_path):
    data, stages, review = _visual_input(tmp_path, name='p')
    review['facts'].append(_fact_of(40_000))
    diagnoses = review['stage_data']['draft']['stages'][2]['diagnoses']
    diagnoses[0]['fact_refs'] = ['F_BIG']
    review['stage_data']['draft']['pages'][0]['item_refs'] = [diagnoses[0]['id']]
    big = next(f for f in mv.visual_review_view(review)['facts'] if f['id'] == 'F_BIG')
    assert isinstance(big['value'], dict) and big['value'].get('not_inlined') and big['value']['observed_chars'] > 30_000


def test_a_large_fact_is_replaced_by_its_size_in_the_item_visual_review_view(tmp_path):
    data, stages, review = _item_visual_input(tmp_path, name='q')
    review['facts'].append(_fact_of(40_000))
    review['stage_data']['draft']['diagnoses'][0]['fact_refs'] = ['F_BIG']
    review['stage_data']['item_assignment']['slot']['fact_refs'] = ['F_BIG']
    big = next(f for f in mv.item_visual_review_view(review)['facts'] if f['id'] == 'F_BIG')
    assert isinstance(big['value'], dict) and big['value'].get('not_inlined')
