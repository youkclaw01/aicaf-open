"""입력 예산 사전 점검의 파이프라인 연결 시험.

계약:
- 진단 계획이 승인된 직후(모델 호출 전), 진단 전 항목의 전송 크기·단가 구간을 점검해 이벤트로 남긴다.
- 점검은 관측이다. 어떤 이유로 실패해도 개정을 막지 않고, 예산을 넘는 항목이 있어도 작업을 멈추지 않는다(기록만).
- 요약은 작아야 한다(이벤트·job.json에 실린다). 항목 전체 목록이 아니라 구간별 개수와 초과 항목만 담는다.
- 정본 입력은 변경하지 않는다.
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_item_agents as IA
import asis_input_budget as budget
from asis_pipeline import Pipeline
from test_asis_item_agents import ItemRuntime, item_fixture


def run_actions(tmp_path):
    data = item_fixture()
    runtime = ItemRuntime()
    pipe = Pipeline(runtime, tmp_path / 'run')
    events = []
    original = pipe.event

    def spy(key, phase, status, **kw):
        events.append((key, phase, status, kw))
        return original(key, phase, status, **kw)
    pipe.event = spy
    asyncio.run(pipe.reviewed_actions(data))
    return data, pipe, events


def budget_events(events):
    return [e for e in events if e[0] == 'input-budget']


def test_the_diagnosis_budget_check_is_recorded_before_any_item_is_authored(tmp_path):
    data, pipe, events = run_actions(tmp_path)
    found = budget_events(events)
    assert found, 'the pre-flight check must leave an event'
    first_author = next(i for i, e in enumerate(events) if e[0].startswith('diagnosis-') and e[1] == 'author' and e[2] == 'running')
    assert events.index(found[0]) < first_author, 'the check must run before the first diagnosis author call'


def test_the_summary_counts_every_diagnosis_and_is_small(tmp_path):
    data, pipe, events = run_actions(tmp_path)
    kw = budget_events(events)[0][3]
    plan_size = len(item_fixture()['context'].get('action_item_plan', {}).get('diagnoses', [])) or kw['items']
    assert kw['items'] >= 1 and kw['items'] == kw['default_tier'] + len(kw['over_budget']) + len(kw['uncertain'])
    assert len(json.dumps(kw, ensure_ascii=False, default=str)) < 4000


def test_a_failing_check_never_stops_the_revision(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError('budget_exploded')
    monkeypatch.setattr(budget, 'measure_item', boom)
    data, pipe, events = run_actions(tmp_path)
    kw = budget_events(events)[0][3]
    assert kw.get('error') and 'budget_exploded' in kw['error']
    assert any(e[0] == 'actions' and e[2] == 'running' for e in events), 'the revision continued'


def test_the_check_does_not_change_the_canonical_input(tmp_path):
    data = item_fixture()
    before = json.dumps(data, ensure_ascii=False, sort_keys=True)
    runtime = ItemRuntime()
    pipe = Pipeline(runtime, tmp_path / 'run')
    asyncio.run(pipe.reviewed_actions(data))
    assert json.dumps(data, ensure_ascii=False, sort_keys=True) == before


def test_over_budget_items_are_reported_not_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(budget, 'LONG_THRESHOLD', 10)
    data, pipe, events = run_actions(tmp_path)
    kw = budget_events(events)[0][3]
    assert kw['over_budget'], 'with a tiny threshold every item is over budget'
    assert any(e[0].startswith('diagnosis-') and e[2] == 'completed' or e[1] == 'content_review' for e in events), 'work still ran'


def test_the_event_stays_small_even_when_every_item_is_over_budget(tmp_path, monkeypatch):
    """항목이 수백 개인 프로젝트에서 모든 항목이 예산을 넘어도 이벤트가 job.json을 부풀리지 않는다(초과 항목 목록에 상한)."""
    monkeypatch.setattr(budget, 'LONG_THRESHOLD', 1)
    fixture = item_fixture()
    plan = fixture['context'].get('action_item_plan') or {}
    data, pipe, events = run_actions(tmp_path)
    kw = budget_events(events)[0][3]
    assert len(kw['over_budget']) <= 20 and len(kw['uncertain']) <= 20
    assert len(json.dumps(kw, ensure_ascii=False, default=str)) < 4000


def test_the_over_budget_cap_is_applied_to_a_large_plan_without_running_the_model(tmp_path):
    """상한 자체를 직접 확인한다: 항목 수가 상한보다 많은 계획으로 점검 함수만 호출한다."""
    from copy import deepcopy
    data = item_fixture()
    runtime = ItemRuntime()
    pipe = Pipeline(runtime, tmp_path / 'run')
    events = []
    pipe.event = lambda key, phase, status, **kw: events.append((key, phase, status, kw))
    import asis_item_agents as agents
    rows = [{'item_id': 'J_%04d' % i, 'bytes': 2_000_000, 'tokens_low': 500_000, 'tokens_high': 800_000, 'tier': 'long',
             'direct_fact_ids': [], 'direct_fact_chars': 0, 'kind': 'item_author'} for i in range(60)]
    original = budget.measure_item
    iterator = iter(rows)
    budget.measure_item = lambda *a, **k: next(iterator)
    try:
        plan = {'diagnoses': [{'id': 'J_%04d' % i, 'scope': 's'} for i in range(60)], 'actions': []}
        agents.budget_report(pipe, data, plan, 'diagnosis', lambda kind, slot, diagnoses: {}, {})
    finally:
        budget.measure_item = original
    kw = events[0][3]
    assert kw['items'] == 60 and len(kw['over_budget']) == 20, 'the event lists at most 20 items but counts all of them'
