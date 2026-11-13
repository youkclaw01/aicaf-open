"""12단계 문서 전체 검토 = 아키텍처 관점의 정합성 검토(영석님 정의 2026-10-19): 세부 사실은 4·7·10단계에서 이미 검증됐으므로 사실 본문을 보내지 않는다.

운영 2026-10-19(프로젝트 25 11·12차 재개): 문서 검토가 17개 호출로 영역·종합·과제의 사실 값·수집 상태를 다시 대조해 입력 한도 초과(208만 자), 영역 9개 blocked, 영역 5개 허위 반려가 났다.
영석님: "12단계는 세부 데이터가 있냐 없냐가 아니라 전체적인 아키텍처 관점에서 서로 충돌나거나 방향이 맞지 않는 게 있는지를 검토한다. 세부 상세 내용은 앞에서 검증이 끝났다."

계약:
- 검토 단위는 4개다: 영역 간(arch-area) · 방향 간(arch-direction) · 진단·과제 간(arch-action) · 통합(integration).
- 모든 단위의 검토 입력에는 사실 본문·수집 상태·판정·측정을 싣지 않는다(ID 참조만).
- 지시문은 사실 값 대조를 하지 말고 입력에 사실이 없다는 이유로 blocked·지적하지 않으며, 충돌·중복·상위 어긋남·참조 끊김·요약 불일치·우선순위 불일치만 지적하라고 한다.
- 통과 조건은 그대로다(하나라도 pass 가 아니면 다음 단계로 못 간다).
"""
from copy import deepcopy
from pathlib import Path
import json, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
import asis_analysis
import asis_model_view as mv
import asis_document_shards as DS
from asis_pipeline import Pipeline
from test_asis_summary_input import summary_data
from test_asis_document_review_input import stages_of, review_input, engine, _fact_of, _with_states


def _draft(pipe, data, stages, key):
    """운영과 같은 경로: 통합은 fit_integration, 나머지는 architecture_draft."""
    if key == 'integration':
        view, info = DS.fit_integration(pipe._integration_stages(stages), budget_chars=DS.token_budget_chars(), fixed_chars=DS.fixed_overhead_chars(data, stages))
        return view
    return DS.architecture_draft(stages, key)


def test_the_architecture_review_units_are_four_and_named(tmp_path):
    names = DS.architecture_units()
    assert [row['key'] for row in names] == ['arch-area', 'arch-direction', 'arch-action', 'integration']
    assert all(row['note'] for row in names)


def test_the_architecture_instruction_forbids_value_comparison_and_blocking_on_missing_facts():
    text = Pipeline.ARCHITECTURE_REVIEW_INSTRUCTION
    for phrase in ('사실 값', '수집 상태', 'blocked', '충돌', '중복', '참조', '요약', '우선순위', 'content_checked=true', 'render_checked=false'):
        assert phrase in text, phrase
    assert '근거를 대조한다' not in text


def test_the_architecture_views_carry_no_fact_bodies_collection_states_or_judgments(tmp_path):
    data = _with_states(summary_data(tmp_path, unresolved=5, directions=3))
    data['facts'].append(_fact_of(40_000))
    pipe = engine()
    stages = stages_of(data)
    stages[1]['directions'][0]['fact_refs'] = ['F_BIG']
    for unit in DS.architecture_units():
        draft = _draft(pipe, data, stages, unit['key'])
        view = mv.document_review_view(review_input(data, draft, 'document-shard-' + unit['key'] if unit['key'] != 'integration' else 'document-integration'))
        assert view['facts'] == [], unit['key']
        assert view['measurements'] == [] and view['judgments'] == [] and view['criteria'] == [], unit['key']
        for name in ('collection_states', 'dataset_register', 'dataset_index', 'criterion_assessments'):
            assert name not in view['context'], (unit['key'], name)


def test_the_architecture_drafts_keep_the_original_json_positions_and_omit_other_layers(tmp_path):
    data = summary_data(tmp_path, unresolved=5, directions=3)
    stages = stages_of(data)
    area = DS.architecture_draft(stages, 'arch-area')['stages']
    assert area[0]['areas'] and area[1].get('omitted') and area[2].get('omitted') and area[3].get('omitted')
    direction = DS.architecture_draft(stages, 'arch-direction')['stages']
    assert direction[0].get('omitted') and direction[1]['directions'] and direction[2].get('omitted')
    action = DS.architecture_draft(stages, 'arch-action')['stages']
    assert action[2]['actions'] and action[1].get('omitted') and action[0].get('omitted')
    assert [row['id'] for row in direction[1]['directions']] == [row['id'] for row in stages[1]['directions']]


def test_each_architecture_unit_input_is_under_the_model_limit_on_a_large_input(tmp_path):
    data = summary_data(tmp_path, unresolved=900, directions=93, fact_chars=60_000)
    pipe = engine()
    stages = stages_of(data)
    for unit in DS.architecture_units():
        key = 'document-shard-' + unit['key'] if unit['key'] != 'integration' else 'document-integration'
        draft = _draft(pipe, data, stages, unit['key'])
        system, prompt = pipe.render_prompt(5, review_input(data, draft, key))
        assert len(prompt) < mv.LIMITS['document_review'] + 20_000, (unit['key'], len(prompt))


def test_the_document_review_makes_four_calls_and_not_seventeen(tmp_path, monkeypatch):
    """_document_review 가 만드는 검토 호출 수. 영역 9개·공통·종합 구간·과제 구간·요약 샤드가 더 이상 별도 호출이 아니다."""
    import asyncio
    data = summary_data(tmp_path, unresolved=5, directions=3)
    stages = stages_of(data)
    pipe = engine()
    calls = []

    class Gate:
        async def __aenter__(self): return None
        async def __aexit__(self, *a): return False
    pipe.item_gate = Gate()

    async def fake_review(data_, draft, key, **kw):
        calls.append((key, kw.get('shard_note', '')))
        return {'stage': 'review', 'run': data_['run'], 'verdict': 'pass', 'content_checked': True, 'render_checked': False, 'defects': [], 'unresolved': []}
    pipe.independent_review = fake_review
    pipe.task_manager = lambda d: type('M', (), {'report': lambda *a, **k: None, 'ledger': {'tasks': {'document': {'pair': {'reviewer': 'x'}}}}})()
    pipe.folder = tmp_path / 'f'
    pipe.save = lambda *a, **k: None
    pipe.event = lambda *a, **k: None
    asyncio.run(pipe._document_review(data, stages, {'stages': stages}))
    keys = [key for key, _ in calls]
    assert keys == ['document-shard-arch-area', 'document-shard-arch-direction', 'document-shard-arch-action', 'document-integration'] or sorted(keys) == sorted(['document-shard-arch-area', 'document-shard-arch-direction', 'document-shard-arch-action', 'document-integration']), keys
    assert all('아키텍처' in note or '충돌' in note or '정합' in note for key, note in calls)


# ── 게이트는 완화하지 않는다: 4단위 중 어느 하나가 pass 가 아니면 12단계는 통과하지 못한다 ──────────────────────────────────────────

def _run_with_verdicts(tmp_path, verdicts, defects=None):
    import asyncio
    data = summary_data(tmp_path, unresolved=5, directions=3)
    stages = stages_of(data)
    pipe = engine()

    class Gate:
        async def __aenter__(self): return None
        async def __aexit__(self, *a): return False
    pipe.item_gate = Gate()

    async def fake_review(data_, draft, key, **kw):
        unit = 'integration' if key == 'document-integration' else key.replace('document-shard-', '')
        verdict = verdicts.get(unit, 'pass')
        rows = (defects or {}).get(unit, [])
        return {'stage': 'review', 'run': data_['run'], 'verdict': verdict, 'content_checked': True, 'render_checked': False, 'defects': rows, 'unresolved': []}
    pipe.independent_review = fake_review
    pipe.task_manager = lambda d: type('M', (), {'report': lambda *a, **k: None, 'ledger': {'tasks': {'document': {'pair': {'reviewer': 'x'}}}}})()
    pipe.folder = tmp_path / 'f'
    pipe.save = lambda *a, **k: None
    pipe.event = lambda *a, **k: None
    return asyncio.run(pipe._document_review(data, stages, {'stages': stages}))


@pytest.mark.parametrize('unit', ['arch-area', 'arch-direction', 'arch-action', 'integration'])
def test_a_revise_from_any_single_unit_keeps_the_document_review_from_passing(tmp_path, unit):
    defect = {'where': '/stages/2/actions/0/title', 'quote': 'q', 'why': '범위 중복', 'fix': '구분', 'evidence_refs': []}
    review = _run_with_verdicts(tmp_path, {unit: 'revise'}, {unit: [defect]})
    assert review['verdict'] == 'revise' and review['defects'] == [defect]
    assert review['shards'][unit] == 'revise'


@pytest.mark.parametrize('unit', ['arch-area', 'arch-direction', 'arch-action', 'integration'])
def test_a_blocked_from_any_single_unit_blocks_the_document_review(tmp_path, unit):
    assert _run_with_verdicts(tmp_path, {unit: 'blocked'})['verdict'] == 'blocked'


def test_the_document_review_passes_only_when_all_four_units_pass(tmp_path):
    assert _run_with_verdicts(tmp_path, {})['verdict'] == 'pass'
