"""병합 검토가 합쳐진 /unresolved/N 항목을 지적해도 개정이 실패하지 않는다.
운영에서 synthesis_repair_target_unbound로 종합이 멈춘 결함(방향 작성자가 쓴 미확인 문장을 병합 검토가 지적)의 회귀 시험이다."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from asis_synthesis import unresolved_owners
import asyncio, json
from test_asis_parallel_synthesis import synthesis_fixture, DirectionRuntime
from asis_pipeline import Pipeline

OUTPUTS = {
    'D_P0006': {'directions': [], 'unresolved': [
        {'topic': '업무 귀속 확인', 'reason': '이번 approved_areas에는 I_network만 제공되어 있으며, I_data의 승인된 시사점 본문은 제공되지 않았다.', 'required_evidence': 'x'},
        {'topic': '다른 항목', 'reason': '전혀 다른 문장이다.', 'required_evidence': 'y'}]},
    'D_P0007': {'directions': [], 'unresolved': [
        {'topic': '업무 귀속 확인', 'reason': '누적 지적에서 요구한 공통 확인·결정 방향은 D_P0007로 반영했으나 I_data는 제공되지 않았다.', 'required_evidence': 'z'}]},
}


def defect(where, quote):
    return {'where': where, 'quote': quote, 'why': '시험', 'fix': '시험', 'evidence_refs': []}


def test_a_merged_unresolved_defect_is_traced_to_the_one_direction_that_wrote_it():
    owners = unresolved_owners(OUTPUTS, defect('/unresolved/20/reason', 'I_network만 제공되어 있으며, I_data의 승인된 시사점 본문은 제공되지 않았다'))
    assert owners == [('D_P0006', '/unresolved/0/reason')], 'the position must be local to that direction own unresolved list'


def test_the_same_sentence_in_two_directions_is_ambiguous_and_is_not_guessed():
    outputs = {k: {'directions': [], 'unresolved': [{'topic': 't', 'reason': '두 방향이 같은 문장을 쓴다.', 'required_evidence': 'e'}]} for k in ('D_P0001', 'D_P0002')}
    owners = unresolved_owners(outputs, defect('/unresolved/3/reason', '두 방향이 같은 문장을 쓴다'))
    assert len(owners) == 2, 'ambiguous ownership must be reported so the caller refuses to guess'


def test_a_quote_that_no_direction_wrote_finds_no_owner():
    assert unresolved_owners(OUTPUTS, defect('/unresolved/5/reason', '어느 방향도 쓰지 않은 문장')) == []


def test_non_unresolved_locations_and_bad_input_return_nothing():
    assert unresolved_owners(OUTPUTS, defect('/directions/3/title', 'x')) == []
    assert unresolved_owners(OUTPUTS, defect('/unresolved/1/reason', '')) == []
    assert unresolved_owners(None, defect('/unresolved/1/reason', 'x')) == []
    assert unresolved_owners({'D': 'not a dict'}, defect('/unresolved/1/reason', 'x')) == []


def test_whole_row_location_matches_by_serialized_row():
    owners = unresolved_owners(OUTPUTS, defect('/unresolved/9', '업무 귀속 확인'))
    assert {o[0] for o in owners} == {'D_P0006', 'D_P0007'}


def test_whole_synthesis_review_pointing_at_a_merged_unresolved_item_repairs_only_its_author_direction(tmp_path):
    """병합 검토가 /unresolved/N을 지적하면(운영 D_P0006 사례) 개정이 실패하지 않고, 그 문장을 쓴 방향 하나만 다시 쓴다."""
    import asyncio, json
    from collections import Counter
    from copy import deepcopy
    from asis_pipeline import Pipeline
    from test_asis_parallel_synthesis import DirectionRuntime, synthesis_fixture
    data, plan = synthesis_fixture(tmp_path)
    SENTENCE = '이번 승인 영역에는 일부만 제공되어 대조할 수 없다는 부정확한 설명이다.'
    FIRST = data['stage_data']['direction_slots'][0]

    class UnresolvedDefect(DirectionRuntime):
        def __init__(self, plan):
            super().__init__(plan)
            self.whole = 0
            self.repaired_inputs = []

        async def ask(self, role, message, **kwargs):
            payload = json.loads(message.rsplit('\n입력:\n', 1)[-1]); stage = payload['stage_data']
            if role == 'content_review' and stage.get('mode') == 'synthesis' and not stage.get('direction_assignment'):
                self.whole += 1
                self.calls.append({'role': role, 'input': deepcopy(payload), **kwargs})
                if self.whole == 1:
                    rows = stage['draft']['unresolved']
                    position = next(i for i, r in enumerate(rows) if SENTENCE in r['reason'])
                    value = {'stage': 'review', 'run': payload['run'], 'verdict': 'revise', 'content_checked': True, 'render_checked': False,
                             'defects': [{'where': '/unresolved/' + str(position) + '/reason', 'quote': SENTENCE, 'why': '입력에는 제공되어 있다.',
                                          'fix': '미제공 단정을 제거한다.', 'evidence_refs': []}], 'unresolved': []}
                    return json.dumps(value, ensure_ascii=False), {'test_only': True, 'role': role}
            raw, receipt = await super().ask(role, message, **kwargs)
            if role in ('author', 'direction_author') and stage.get('direction_assignment'):
                candidate = json.loads(raw)
                if stage.get('prior_output'):
                    self.repaired_inputs.append(deepcopy(stage))
                    candidate['unresolved'] = [u for u in candidate['unresolved'] if SENTENCE not in u['reason']]
                elif stage['direction_assignment']['slot']['id'] == FIRST:
                    candidate['unresolved'] = candidate['unresolved'] + [{'topic': '시험 미확인', 'reason': SENTENCE, 'required_evidence': '시험', 'fact_refs': []}]
                raw = json.dumps(candidate, ensure_ascii=False)
            return raw, receipt

    runtime = UnresolvedDefect(plan)
    pipe = Pipeline(runtime, tmp_path / 'run')
    result = asyncio.run(pipe.reviewed_synthesis(data))
    authors = [c for c in runtime.calls if c['role'] in ('author', 'direction_author')]
    counts = Counter(c['input']['stage_data']['direction_assignment']['slot']['id'] for c in authors)
    assert sorted(counts.values()) == [1, 2], 'only the direction that wrote the flagged sentence is rewritten: ' + str(dict(counts))
    assert runtime.whole == 2
    repaired = runtime.repaired_inputs[0]
    assert repaired['accumulated_defects'][0]['where'] == '/unresolved/' + '0' + '/reason' or repaired['accumulated_defects'][0]['where'].startswith('/unresolved/')
    assert SENTENCE in repaired['accumulated_defects'][0]['quote']
    assert not any(SENTENCE in u['reason'] for u in result['unresolved']), 'the flagged sentence must be gone from the final merged result'


def test_a_review_pointing_at_the_code_generated_edges_is_skipped_and_recorded_instead_of_failing(tmp_path):
    """운영 실측: 통합 검토가 /edges(코드가 생성하는 필드)를 지적해 synthesis_repair_target_unbound로 개정이 중단됐다.
    targets()가 /edges 지적을 건너뛰고 기록하며, 같은 목록의 방향 지적은 정상 귀속하고, 다른 위치는 기존처럼 엄격히 실패하는지 본다."""
    import inspect, re, textwrap
    import asis_synthesis
    source = inspect.getsource(asis_synthesis.reviewed_synthesis)
    start = source.index('        ignored_code_owned = []')
    end = source.index('        targeted = targets(prior')
    block = textwrap.dedent(source[start:end])

    saved = []

    class Pipe:
        folder = tmp_path

        def save(self, path, value):
            saved.append((str(path), value))
    namespace = {'__import__': __import__, 'deepcopy': __import__('copy').deepcopy, 'json': json, 'pipe': Pipe(), 'CODE_OWNED_FIELDS': asis_synthesis.CODE_OWNED_FIELDS,
                 'outputs': {}, 'budgets': {'D_P0001': {'remaining': 3}}, 'unresolved_owners': asis_synthesis.unresolved_owners}

    class Validator:
        @staticmethod
        def pointer(candidate, where):
            return candidate['directions'][0]['title']
    namespace['pipe'].validator = Validator()
    exec(block, namespace)
    targets = namespace['targets']
    candidate = {'directions': [{'id': 'D_P0001', 'title': '제목'}]}
    defects = [{'where': '/edges', 'quote': '[]', 'why': '연결이 없다.', 'fix': '추가', 'evidence_refs': []},
               {'where': '/directions/0/title', 'quote': '제목', 'why': '중복', 'fix': '구분', 'evidence_refs': []}]
    result = targets(candidate, defects)
    assert list(result) == ['D_P0001'] and len(result['D_P0001']) == 1, 'the direction defect is still attributed to its direction'
    assert namespace['ignored_code_owned'][0]['where'] == '/edges'
    assert saved and saved[-1][1]['ignored'][0]['where'] == '/edges' and saved[-1][0].endswith('ignored-code-owned-defects.json')
    import pytest
    with pytest.raises(ValueError, match='synthesis_repair_target_unbound'):
        targets(candidate, [{'where': '/somewhere_else', 'quote': 'x', 'why': 'w', 'fix': 'f', 'evidence_refs': []}])


def test_the_summary_view_sent_to_the_integration_review_keeps_the_real_edges():
    from asis_synthesis import _brief_directions, project_graph
    merged = project_graph({'stage': 'synthesis', 'run': {}, 'directions': [
        {'id': 'D_1', 'title': 't', 'text': 'x', 'fact_refs': ['F_1', 'F_2'], 'area_implication_refs': ['I_a'], 'criterion_refs': [], 'goal_refs': [],
         'customer_impact': '', 'change_direction': ''}], 'unresolved': []})
    brief = _brief_directions(merged)
    assert [(e['from_id'], e['to_id']) for e in brief['edges']] == [('F_1', 'D_1'), ('F_2', 'D_1'), ('I_a', 'D_1')]


def test_other_non_direction_locations_still_fail_strictly():
    from asis_synthesis import CODE_OWNED_FIELDS
    assert CODE_OWNED_FIELDS == frozenset({'/edges'}), 'only the code-generated edges field is exempt; nothing else may be silently ignored'
