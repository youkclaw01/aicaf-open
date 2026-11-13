"""입력 예산 사전 점검(asis_input_budget) 시험.

계약: 모델을 부르기 전에, 정본 입력과 계획 슬롯으로 각 항목의 실제 전송 프롬프트를 렌더해 크기·예상 토큰·단가 구간(272K)·예상 크레딧을 계산한다.
- 모델 호출·쓰기 없음. 운영 전송 경로(Pipeline.render_prompt)를 그대로 쓴다(별도 추정식 금지).
- 토큰은 단계별 바이트→토큰 비(영수증 실측)로 환산하되 환산 불확실성은 낮음/높음 범위로 함께 낸다.
- 예산을 넘는 항목은 이름과 초과량을 돌려주고, 그 항목이 '줄일 수 없는 직접 인용'인지 근거를 함께 낸다.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_input_budget as budget
from asis_pipeline import Pipeline
from test_asis_model_view import item_scoped
from test_asis_parallel_synthesis import DirectionRuntime


def pipe(tmp_path):
    return Pipeline(DirectionRuntime({'directions': []}), tmp_path / 'run')


def test_an_item_is_measured_with_the_real_transport_render(tmp_path):
    data, scoped = item_scoped(tmp_path)
    p = pipe(tmp_path)
    row = budget.measure_item(p, scoped)
    assert row['bytes'] == len(p.render_prompt(3, scoped)[1].encode('utf-8')), 'must measure the same prompt the model receives'
    assert row['kind'] == 'item_author' and row['item_id'] == scoped['stage_data']['item_assignment']['item_id']


def test_the_estimate_gives_a_range_not_one_number(tmp_path):
    data, scoped = item_scoped(tmp_path)
    row = budget.measure_item(pipe(tmp_path), scoped, ratio_low=0.25, ratio_high=0.5)
    assert row['tokens_low'] == round(row['bytes'] * 0.25) and row['tokens_high'] == round(row['bytes'] * 0.5)
    assert row['tokens_low'] < row['tokens_high']


def test_the_tier_is_long_only_when_the_whole_range_is_over_the_threshold(tmp_path):
    ok = budget.tier(tokens_low=100_000, tokens_high=200_000)
    assert ok == 'default'
    unsure = budget.tier(tokens_low=250_000, tokens_high=300_000)
    assert unsure == 'uncertain', 'a range that straddles 272K must be reported as uncertain, not silently default'
    long_ = budget.tier(tokens_low=280_000, tokens_high=400_000)
    assert long_ == 'long'


def test_the_cost_uses_the_published_unit_prices_for_the_tier():
    row = budget.cost_usd(input_tokens=100_000, output_tokens=5_000, tier='default')
    assert row == pytest.approx(100_000 * 2.0 / 1e6 + 5_000 * 10.0 / 1e6)
    row = budget.cost_usd(input_tokens=300_000, output_tokens=5_000, tier='long')
    assert row == pytest.approx(300_000 * 4.0 / 1e6 + 5_000 * 15.0 / 1e6)


def test_a_summary_lists_only_the_items_over_budget_with_their_overage(tmp_path):
    rows = [{'item_id': 'J_1', 'bytes': 500_000, 'tokens_low': 100_000, 'tokens_high': 150_000, 'tier': 'default'},
            {'item_id': 'A_1', 'bytes': 1_000_000, 'tokens_low': 270_000, 'tokens_high': 400_000, 'tier': 'uncertain'},
            {'item_id': 'A_2', 'bytes': 1_200_000, 'tokens_low': 330_000, 'tokens_high': 450_000, 'tier': 'long'}]
    out = budget.summarize(rows)
    assert [r['item_id'] for r in out['over_budget']] == ['A_2'] and [r['item_id'] for r in out['uncertain']] == ['A_1']
    assert out['items'] == 3 and out['default_tier'] == 1
    assert out['over_budget'][0]['over_by_low'] == 330_000 - 272_000


def test_an_item_cannot_be_reduced_when_its_overage_comes_from_a_direct_citation(tmp_path):
    """슬롯이 직접 인용한 사실은 줄이면 근거 인용이 끊긴다. 초과 원인이 거기라면 '줄일 수 없음'으로 보고한다."""
    data, scoped = item_scoped(tmp_path)
    direct = set(scoped['stage_data']['item_assignment']['slot']['fact_refs'])
    row = budget.measure_item(pipe(tmp_path), scoped)
    assert 'direct_fact_chars' in row and row['direct_fact_chars'] >= 0
    assert set(row['direct_fact_ids']) <= direct


def test_measuring_does_not_modify_the_canonical_input(tmp_path):
    data, scoped = item_scoped(tmp_path)
    before = json.dumps(scoped, ensure_ascii=False, sort_keys=True)
    budget.measure_item(pipe(tmp_path), scoped)
    assert json.dumps(scoped, ensure_ascii=False, sort_keys=True) == before
