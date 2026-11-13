"""전송 전 한도 검사(model_input_over_limit)로 막히는 항목도 요약 규칙으로 복구하는 계약 시험(모델 호출 없음).

운영 2026-10-18(프로젝트 25 과제 A_ITEM_0001, 연결 방향 17개): 입력이 상한을 넘어 render_prompt 안의 asis_model_view.build 가 ValueError 를 던졌다.
요약 규칙은 render_prompt 뒤에서만 걸려 있어 이 경로에는 닿지 못했다(기록도 요약도 일어나지 않고 항목이 즉시 실패).
계약: 요약은 한도 검사보다 앞에서 걸려야 한다. 항목이 길이 실패 2회 이상이면 build 가 요약된 뷰로 크기를 재고, 요약 후에도 넘을 때만 ValueError 를 올린다.
"""
from copy import deepcopy
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pytest
import asis_model_view as MV
from test_asis_model_view import heavy_data

SENTENCE = '이 방향은 운영 체계를 정비하는 것이다. 조직이 일관된 절차를 갖추는 것이 바람직하다. 세부 항목은 별도 문서에 둔다. '


def item_data(tmp_path, repeat=400):
    """승인 방향의 서술문이 길어 전송 크기가 커진 항목 입력(item_author)."""
    data, plan = heavy_data(tmp_path, extra_facts=0)
    text = SENTENCE * repeat
    directions = [{'id': 'D_P%04d' % i, 'title': 't', 'text': text, 'change_direction': text, 'customer_impact': text,
                   'fact_refs': ['F_MINE'], 'criterion_refs': [], 'goal_refs': [], 'area_implication_refs': []} for i in range(3)]
    scoped = deepcopy(data)
    scoped['stage_data'] = {'mode': 'actions',
        'item_assignment': {'kind': 'diagnosis', 'item_id': 'J_ITEM_0001',
                            'slot': {'fact_refs': ['F_MINE'], 'criterion_refs': [], 'goal_refs': [], 'direction_refs': [d['id'] for d in directions]}},
        'approved_areas': deepcopy(data['stage_data']['approved_areas']),
        'approved_directions': {'directions': directions, 'edges': [], 'unresolved': []}}
    scoped['allocated_ids'] = {'diagnoses': ['J_ITEM_0001'], 'actions': []}
    return scoped


def test_the_over_limit_check_runs_on_the_summarized_view_when_the_level_is_set(tmp_path, monkeypatch):
    data = item_data(tmp_path)
    full = MV._chars(MV.build(3, data)[1])
    monkeypatch.setitem(MV.LIMITS, 'item_author', int(full * 0.9))
    with pytest.raises(ValueError, match='model_input_over_limit'):
        MV.build(3, data)
    kind, view = MV.build(3, data, summary_level=1)
    assert kind == 'item_author' and MV._chars(view) < full
    texts = [d['text'] for d in view['stage_data']['approved_directions']['directions']]
    assert all(len(t) < len(SENTENCE * 400) for t in texts), '승인 서술문이 줄어야 한다'
    assert all('이 방향은 운영 체계를 정비하는 것이다.' in t for t in texts), '첫 문장은 남는다'


def test_the_limit_still_fails_when_even_the_summary_is_too_long(tmp_path, monkeypatch):
    data = item_data(tmp_path)
    monkeypatch.setitem(MV.LIMITS, 'item_author', 5_000)
    with pytest.raises(ValueError, match='model_input_over_limit'):
        MV.build(3, data, summary_level=2)


def test_level_zero_and_the_default_change_nothing(tmp_path):
    data = item_data(tmp_path, repeat=5)
    a = MV.build(3, data)
    b = MV.build(3, data, summary_level=0)
    assert json.dumps(a[1], sort_keys=True, ensure_ascii=False) == json.dumps(b[1], sort_keys=True, ensure_ascii=False)


def test_the_canonical_input_is_never_modified_by_a_summarized_build(tmp_path):
    data = item_data(tmp_path)
    before = json.dumps(data, sort_keys=True, ensure_ascii=False)
    MV.build(3, data, summary_level=2)
    assert json.dumps(data, sort_keys=True, ensure_ascii=False) == before


def test_a_non_item_kind_ignores_the_summary_level(tmp_path, monkeypatch):
    data, plan = heavy_data(tmp_path, extra_facts=0)
    assert MV.kind_of(3, data) is None or not MV.kind_of(3, data).startswith('item_')
    a = MV.build(3, data)
    b = MV.build(3, data, summary_level=2)
    assert json.dumps(a[1], sort_keys=True, ensure_ascii=False) == json.dumps(b[1], sort_keys=True, ensure_ascii=False)
