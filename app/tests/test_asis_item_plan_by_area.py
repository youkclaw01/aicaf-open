"""진단·과제 계획을 영역별로 나눈다: 이슈의 주도 영역마다 작은 호출 1개씩, 전수 처리와 합본·ID 연결은 코드가 확인한다.
운영 실측: 한 번의 호출에 전체를 실어 프롬프트 954,441토큰이 한도 922,000을 넘어 400으로 거절되고 개정이 멈췄다."""
from copy import deepcopy
from pathlib import Path
import asyncio, json, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
import asis_item_agents as items
from asis_item_agents import (lead_areas, area_plan_view, validate_area_plan, merge_area_plans, validate_plan, allocate_plan)
from asis_pipeline import Pipeline

AREAS = ('ai', 'network')


def make_data():
    run = {'id': 'run-1'}
    issues = []
    for n, (area, scope) in enumerate([('ai', '모델 배포와 호출 대응'), ('ai', '비용 계측'), ('network', '사설 접근 경계'), ('network', '공개 진입 보호')], 1):
        issues.append({'id': 'PLAN_P%04d' % n, 'area_refs': [area], 'lead_area': area, 'scope': scope, 'reason': scope + '의 이유',
                       'fact_refs': ['F_%d' % n], 'criterion_refs': ['C_%d' % n], 'goal_refs': ['G_1'], 'direction_id': 'D_P%04d' % n})
    directions = [{'id': 'D_P0001', 'title': '호출 경계'}, {'id': 'D_P0002', 'title': '비용 통제'}, {'id': 'D_P0003', 'title': '네트워크 경계'}]
    return {'run': run, 'facts': [{'id': 'F_%d' % n} for n in range(1, 5)], 'criteria': [{'id': 'C_%d' % n} for n in range(1, 5)], 'goals': [{'id': 'G_1'}],
            'context': {'analysis_plan': {'issues': issues}}, 'stage_data': {'approved_directions': {'directions': directions}}}


LINKS = {'PLAN_P0001': ['D_P0001'], 'PLAN_P0002': ['D_P0002'], 'PLAN_P0003': ['D_P0003'], 'PLAN_P0004': ['D_P0003']}


def good_area_plan(view):
    """입력(view)이 담은 이슈와 방향 연결만으로 만든 올바른 영역 계획. 모델 대역이 입력 밖의 지식을 쓰지 않게 한다."""
    issues = view['issues']
    return {'diagnoses': [{'scope': '진단 ' + row['id'], 'reason': '이유', 'issue_refs': [row['id']], 'direction_refs': list(row['direction_refs'])} for row in issues],
            'actions': [{'scope': '과제 ' + row['id'], 'reason': '이유', 'issue_refs': [row['id']], 'direction_refs': list(row['direction_refs']), 'diagnosis_indices': [k]}
                        for k, row in enumerate(issues)],
            'coverage': [{'issue_id': row['id'], 'diagnosis_indices': [k], 'action_indices': [k], 'disposition': 'included', 'reason': '포함', 'required_evidence': ''}
                         for k, row in enumerate(issues)]}


def test_lead_areas_assigns_every_issue_to_exactly_one_area():
    data = make_data()
    lead = lead_areas(data, set(AREAS))
    assert lead == {'PLAN_P0001': 'ai', 'PLAN_P0002': 'ai', 'PLAN_P0003': 'network', 'PLAN_P0004': 'network'}
    data['context']['analysis_plan']['issues'][0]['lead_area'] = 'unknown'
    with pytest.raises(ValueError, match='issue_without_lead_area'):
        lead_areas(data, set(AREAS))


def test_the_area_view_has_titles_and_ids_only_and_never_fact_bodies():
    data = make_data()
    view = area_plan_view(data, 'ai', ['PLAN_P0001', 'PLAN_P0002'], LINKS)
    assert [row['id'] for row in view['issues']] == ['PLAN_P0001', 'PLAN_P0002']
    assert [row['id'] for row in view['directions']] == ['D_P0001', 'D_P0002'], 'only the directions that these issues belong to'
    text = json.dumps(view, ensure_ascii=False)
    assert 'fact_refs' not in text and 'criterion_refs' not in text and 'F_1' not in text, 'the planner never sees fact or criterion references or bodies'
    assert len(text) < 1500


def test_a_valid_area_plan_passes_and_each_defect_is_rejected():
    view = area_plan_view(make_data(), 'ai', ['PLAN_P0001', 'PLAN_P0002'], LINKS)
    validate_area_plan(view, good_area_plan(view))
    mutations = {
        'missing issue in coverage': lambda p: p['coverage'].pop(),
        'issue of another area': lambda p: p['diagnoses'][0].update(issue_refs=['PLAN_P0003']),
        'unknown direction': lambda p: p['diagnoses'][0].update(direction_refs=['D_X']),
        'extra key': lambda p: p['diagnoses'][0].update(fact_refs=['F_1']),
        'duplicate slot': lambda p: p['diagnoses'].append(deepcopy(p['diagnoses'][0])),
        'bad action index': lambda p: p['actions'][0].update(diagnosis_indices=[9]),
        'included without a diagnosis': lambda p: (p['diagnoses'].clear(), p['actions'].clear()),
    }
    for name, mutate in mutations.items():
        plan = good_area_plan(view); mutate(plan)
        with pytest.raises(ValueError):
            validate_area_plan(view, plan)


def test_merging_takes_fact_criterion_and_goal_ids_from_the_issues_not_from_the_model():
    data = make_data()
    plans = {a: good_area_plan(area_plan_view(data, a, [i for i, x in lead_areas(data, set(AREAS)).items() if x == a], LINKS)) for a in AREAS}
    merged = merge_area_plans(data, plans)
    first = merged['diagnoses'][0]
    assert first['fact_refs'] == ['F_1'] and first['criterion_refs'] == ['C_1'] and first['goal_refs'] == ['G_1'], 'IDs come from the issue, the model never chose them'
    assert len(merged['diagnoses']) == 4 and len(merged['actions']) == 4
    assert merged['actions'][2]['diagnosis_indices'] == [2], 'area offsets are applied so indices stay valid in the merged plan'
    assert merged['coverage'][3]['action_indices'] == [3]


def test_the_merged_plan_passes_the_existing_whole_plan_validation_and_covers_every_issue():
    data = make_data()
    plans = {a: good_area_plan(area_plan_view(data, a, [i for i, x in lead_areas(data, set(AREAS)).items() if x == a], LINKS)) for a in AREAS}
    merged = merge_area_plans(data, plans)
    validate_plan(data, merged)
    broken = deepcopy(merged); broken['coverage'].pop()
    with pytest.raises(ValueError, match='action_item_plan_issue_coverage_mismatch'):
        validate_plan(data, broken)


def test_a_direction_that_spans_two_areas_stays_linked_through_its_id():
    data = make_data()
    links = dict(LINKS); links['PLAN_P0001'] = ['D_P0001', 'D_P0003']
    a = area_plan_view(data, 'ai', ['PLAN_P0001', 'PLAN_P0002'], links)
    n = area_plan_view(data, 'network', ['PLAN_P0003', 'PLAN_P0004'], links)
    assert 'D_P0003' in {d['id'] for d in a['directions']} and 'D_P0003' in {d['id'] for d in n['directions']}


def test_direction_links_are_restored_from_the_saved_synthesis_plan_and_never_guessed(tmp_path):
    data = make_data()
    pipe = Pipeline(object(), tmp_path / 'run')
    with pytest.raises(ValueError, match='issue_direction_links_unavailable'):
        items.direction_of_issue(pipe, data)
    folder = pipe.folder / 'checkpoints' / 'synthesis-plan' / 'abc'
    folder.mkdir(parents=True)
    slots = [{'issue_refs': ['PLAN_P0001']}, {'issue_refs': ['PLAN_P0002']}, {'issue_refs': ['PLAN_P0003', 'PLAN_P0004']}]
    (folder / 'accepted.json').write_text(json.dumps({'candidate': {'directions': slots}}), encoding='utf-8')
    assert items.direction_of_issue(pipe, data) == LINKS
    slots[2]['issue_refs'] = ['PLAN_P0003']
    (folder / 'accepted.json').write_text(json.dumps({'candidate': {'directions': slots}}), encoding='utf-8')
    with pytest.raises(ValueError, match='issue_without_direction_link'):
        items.direction_of_issue(pipe, data)


class Runtime:
    def __init__(self):
        self.calls = []

    async def ask(self, role, message, **kwargs):
        view = json.loads(message.rsplit('\n입력:\n', 1)[-1])
        self.calls.append((role, view, kwargs))
        if role == 'interpret' and 'candidates' in view and 'limit' in view:
            from area_item_plan_stub import consolidation_answer
            body = consolidation_answer(view)
        elif role == 'interpret':
            body = good_area_plan(view)
        else:
            body = {'stage': 'review', 'run': view['run'], 'verdict': 'pass', 'content_checked': True, 'render_checked': False, 'defects': [], 'unresolved': []}
        return json.dumps(body, ensure_ascii=False), {'test_only': True, 'role': role}


def test_the_plan_runs_one_small_call_per_area_and_never_one_call_with_everything(tmp_path):
    """실제 종합 정본 입력(영역 3개·이슈 3개)으로 _plan_items를 끝까지 실행해 영역마다 한 번의 작은 호출만 일어나는지 본다."""
    from test_asis_parallel_synthesis import synthesis_fixture
    data, plan = synthesis_fixture(tmp_path)
    issues = data['context']['analysis_plan']['issues']
    runtime = Runtime()
    pipe = Pipeline(runtime, tmp_path / 'run')
    folder = pipe.folder / 'checkpoints' / 'synthesis-plan' / 'abc'; folder.mkdir(parents=True)
    (folder / 'accepted.json').write_text(json.dumps({'candidate': {'directions': plan['directions']}}), encoding='utf-8')
    approved = [{'id': i, 'title': '합성 방향 ' + i} for i in data['allocated_ids']['directions']]
    original = deepcopy(data)
    original['stage_data'] = {'mode': 'actions', 'approved_areas': data['stage_data']['approved_areas'],
                              'approved_directions': {'stage': 'synthesis', 'run': deepcopy(data['run']), 'directions': approved, 'edges': [], 'unresolved': []}}
    # 승인 방향의 순서와 종합 계획 슬롯의 순서가 같아야 한다: 슬롯 2개 -> 방향 2개만 승인 방향으로 둔다.
    original['stage_data']['approved_directions']['directions'] = approved[:len(plan['directions'])]
    prepared = asyncio.run(items._plan_items(pipe, original))
    calls = [c for c in runtime.calls if c[0] == 'interpret' and 'issues' in c[1]]
    consolidation = [c for c in runtime.calls if c[0] == 'interpret' and 'candidates' in c[1]]
    leads = sorted({row['lead_area'] for row in issues})
    assert len(calls) == len(leads), 'one planning call per area: %d areas' % len(leads)
    assert len(consolidation) == len(leads) + 1, 'one topic-grouping call per area and one action-grouping call across areas'
    sent_ids = [row['id'] for _, view, _ in calls for row in view['issues']]
    assert sorted(sent_ids) == sorted(row['id'] for row in issues) and len(sent_ids) == len(set(sent_ids)), 'every issue goes to exactly one area call'
    assert max(len(json.dumps(view, ensure_ascii=False)) for _, view, _ in calls) < 4000
    plan_out = prepared['context']['action_item_plan']
    assert plan_out['diagnoses'] and plan_out['actions'] and plan_out['ids_allocated_by'] == 'code'
    covered = {row['issue_id'] for row in plan_out['coverage']}
    assert covered == {row['id'] for row in issues}, 'the merged plan processes every issue once'
    areas_done = sorted(p.parent.name for p in (pipe.folder / 'checkpoints' / 'action-item-plan').rglob('areas/*/accepted.json'))
    assert areas_done == leads, 'each area saved its own completed unit'
    again = len(runtime.calls)
    asyncio.run(items._plan_items(pipe, original))
    assert len(runtime.calls) == again, 'a finished plan is reused without any model call'


def test_the_area_plan_review_sees_a_body_free_summary_of_the_facts_its_issues_cite_and_its_role_limit():
    """운영 실측: 검토 입력의 사실이 비어 telemetry 영역 검토가 '사실 검증을 완료할 수 없다'며 blocked를 냈다."""
    data = make_data()
    data['facts'] = [{'id': 'F_1', 'source_id': 'cost.json', 'field_path': '/a', 'collection_status': 'collected', 'scope_complete': True,
                      'value': [{'blob': 'x' * 500}] * 7},
                     {'id': 'F_2', 'source_id': 'net.json', 'field_path': '/b', 'collection_status': 'collected', 'scope_complete': False, 'value': {'k': 'v'}},
                     {'id': 'F_3', 'source_id': 'other.json', 'field_path': '/c', 'collection_status': 'collected', 'scope_complete': True, 'value': 'not cited'},
                     {'id': 'F_4', 'source_id': 'o.json', 'field_path': '/d', 'collection_status': 'collected', 'scope_complete': True, 'value': 'not cited'}]
    view = area_plan_view(data, 'ai', ['PLAN_P0001', 'PLAN_P0002'], LINKS)
    shell = items.review_input(data, view)
    summary = shell['stage_data']['fact_summary']
    assert {row['id'] for row in summary} == {'F_1', 'F_2'}, 'only the facts the area issues cite'
    assert next(row for row in summary if row['id'] == 'F_1')['record_count'] == 7
    assert 'value' not in json.dumps(summary), 'the summary never carries a fact body'
    assert 'xxxx' not in json.dumps(shell), 'no fact body reaches the plan reviewer'
    assert 'blocked로 판정하지 않는다' in shell['stage_data']['review_role_limit']
    assert shell['facts'] == [] and shell['stage_data']['plan_view'] == view


def test_area_plan_preserves_evidence_text_without_six_identical_format_retries(tmp_path, monkeypatch):
    """실제 실패처럼 근거를 문자열 배열로 반환해도 내용 보존 투영 후 한 번만 검토한다."""
    data = make_data()
    view = area_plan_view(data, 'ai', ['PLAN_P0001', 'PLAN_P0002'], LINKS)
    raw_candidate = good_area_plan(view)
    raw_candidate['coverage'][0]['required_evidence'] = []
    raw_candidate['coverage'][1]['required_evidence'] = ['원본 호출 기록', '요청별 처리 결과']
    raw = json.dumps(raw_candidate, ensure_ascii=False)

    class EvidenceArrayRuntime:
        calls = 0

        async def ask(self, *args, **kwargs):
            self.calls += 1
            return raw, {'provider': 'controlled-test', 'requested_model': 'controlled-test'}

    runtime = EvidenceArrayRuntime()
    pipe = Pipeline(runtime, folder=tmp_path, rounds=1)
    reviewed = []

    async def review(review_data, candidate, key):
        reviewed.append(deepcopy(candidate))
        return {'verdict': 'pass', 'defects': []}

    monkeypatch.setattr(pipe, 'independent_review', review)
    result = asyncio.run(items._plan_area_items(
        pipe, data, data, 'ai', ['PLAN_P0001', 'PLAN_P0002'], LINKS,
        items.AREA_PLAN_INSTRUCTION, '동일 계약의 통제된 시험', 'binding', tmp_path / 'plan'))
    assert runtime.calls == 1
    assert len(reviewed) == 1 and reviewed[0] == result
    assert result['coverage'][0]['required_evidence'] == ''
    assert result['coverage'][1]['required_evidence'] == '원본 호출 기록\n요청별 처리 결과'
    assert raw_candidate['coverage'][1]['required_evidence'] == ['원본 호출 기록', '요청별 처리 결과']
    folder = tmp_path / 'plan' / 'areas' / 'ai' / 'round-001'
    assert (folder / 'response.txt').read_text(encoding='utf-8') == raw
    proof = json.loads((folder / 'projection.json').read_text(encoding='utf-8'))
    assert proof['source_candidate_sha256'] == items.digest(raw_candidate)
    assert proof['projected_candidate_sha256'] == items.digest(result)
    assert [row['where'] for row in proof['changes']] == ['/coverage/0/required_evidence', '/coverage/1/required_evidence']
    assert proof['changes'][1]['before'] == ['원본 호출 기록', '요청별 처리 결과']


@pytest.mark.parametrize('bad', [None, 1, ['확인 자료', 1], [{'field': '자료'}]])
def test_area_plan_projection_never_coerces_unknown_evidence_types(bad):
    """문자열 배열 이외의 값은 임의 해석하지 않고 기존 계약에서 차단한다."""
    data = make_data()
    view = area_plan_view(data, 'ai', ['PLAN_P0001', 'PLAN_P0002'], LINKS)
    candidate = good_area_plan(view)
    candidate['coverage'][0]['required_evidence'] = bad
    projected, changes = items.project_area_plan(candidate)
    assert not changes and projected == candidate
    with pytest.raises(ValueError, match='invalid_area_item_plan_coverage'):
        validate_area_plan(view, projected)


def test_area_plan_projection_does_not_approve_a_deferral_without_evidence():
    """빈 배열의 형식만 바꿔도 보류에 필요한 근거와 판단 조건은 여전히 필수다."""
    data = make_data()
    view = area_plan_view(data, 'ai', ['PLAN_P0001', 'PLAN_P0002'], LINKS)
    candidate = good_area_plan(view)
    candidate['coverage'][0].update(disposition='deferred', diagnosis_indices=[], action_indices=[], required_evidence=[])
    projected, changes = items.project_area_plan(candidate)
    assert changes and projected['coverage'][0]['required_evidence'] == ''
    with pytest.raises(ValueError):
        validate_area_plan(view, projected)


def test_area_plan_resumes_the_last_saved_candidate_without_replacing_six_rounds(tmp_path, monkeypatch):
    """실패 원본 여섯 회를 보존하고 마지막 후보의 형식 복구 뒤 새 작성 없이 독립 검토한다."""
    data = make_data()
    view = area_plan_view(data, 'ai', ['PLAN_P0001', 'PLAN_P0002'], LINKS)
    root = tmp_path / 'plan'
    for number in range(1, 7):
        candidate = good_area_plan(view)
        candidate['diagnoses'][0]['scope'] = '보존 후보 ' + str(number)
        candidate['coverage'][0]['required_evidence'] = []
        folder = root / 'areas' / 'ai' / ('round-' + str(number).zfill(3))
        folder.mkdir(parents=True)
        (folder / 'input.json').write_text(json.dumps({**view, 'prior_output': None, 'accumulated_defects': []}), encoding='utf-8')
        (folder / 'response.txt').write_text(json.dumps(candidate, ensure_ascii=False), encoding='utf-8')
        (folder / 'receipt.json').write_text(json.dumps({'provider': 'controlled-test', 'request_number': number}), encoding='utf-8')
    originals = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}

    class NoRegenerationRuntime:
        async def ask(self, *args, **kwargs):
            raise AssertionError('saved_candidate_must_not_be_regenerated')

    pipe = Pipeline(NoRegenerationRuntime(), folder=tmp_path, rounds=1)
    checked = []

    async def review(review_data, candidate, key):
        checked.append(deepcopy(candidate))
        return {'verdict': 'pass', 'defects': []}

    monkeypatch.setattr(pipe, 'independent_review', review)
    result = asyncio.run(items._plan_area_items(pipe, data, data, 'ai', ['PLAN_P0001', 'PLAN_P0002'], LINKS,
                        items.AREA_PLAN_INSTRUCTION, '같은 입력·모델 정책의 복구', 'binding', root))
    assert len(checked) == 1 and result['diagnoses'][0]['scope'] == '보존 후보 6'
    assert result['coverage'][0]['required_evidence'] == ''
    assert all(p.read_bytes() == content for p, content in originals.items())
    saved = json.loads((root / 'areas' / 'ai' / 'accepted.json').read_text(encoding='utf-8'))
    assert saved['candidate_sha256'] == items.digest(result)
