"""항목 완성까지 같은 MAF 작성 객체·대화를 유지하는 제품 계약 시험."""
import asyncio
from pathlib import Path
import sys
import types

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from asis_runtime import Runtime


def test_one_item_keeps_its_author_object_and_conversation_through_native_correction():
    """모델 전송만 합성이며 실제 Runtime의 객체 수명·역할 격리를 확인한다."""
    created = []
    seen = []

    class Client:
        async def start(self):
            pass

        async def stop(self):
            pass

    class Agent:
        def __init__(self, **kwargs):
            self.turns = []
            self.name = kwargs['name']
            created.append(self)

        async def run(self, message, *, session, options=None):
            self.turns.append(message)
            if session.service_session_id is None:
                session.service_session_id = 'offline-' + session.session_id
            seen.append((self, session, len(self.turns)))
            return types.SimpleNamespace(text='{"synthetic_only":true}',
                response_id='offline-' + str(len(seen)),
                additional_properties={'model': 'gpt-6.1-sol'}, usage_details={})

    async def exercise():
        async with Runtime(client=Client(), agent_factory=Agent) as runtime:
            await runtime.ask('author', 'first item candidate', session_id='item-a-author')
            first = seen[-1]
            await runtime.ask('content_review', 'all item findings', session_id='item-a-review')
            await runtime.ask('author', 'apply every finding together', session_id='item-a-author')
            # JSON 검토 완료는 작성자 수명의 끝이 아니다. 실제 장표 단계 이후도 유지한다.
            await asyncio.to_thread(lambda: None)
            await runtime.ask('author', 'correct the rendered item text', session_id='item-a-author')
            author_turns = [row for row in seen if row[1].session_id == 'item-a-author']
            assert all(row[0] is first[0] for row in author_turns), 'item author object was recreated'
            assert all(row[1] is first[1] for row in author_turns), 'item conversation was replaced'
            assert [row[2] for row in author_turns] == [1, 2, 3], 'item author history was discarded'
            assert seen[1][0] is not first[0] and seen[1][1] is not first[1]
        assert len(created) == 2, 'one writer and one reviewer must suffice for this item'

    asyncio.run(exercise())


def test_item_reviewer_keeps_its_session_when_candidate_changes(tmp_path):
    """후보 해시는 검토 근거이며 동일 항목 검토자의 대화 교체 사유가 아니다."""
    from copy import deepcopy
    import json
    from asis_item_agents import allocate_plan, item_input, validate_item
    from asis_pipeline import Pipeline
    from test_asis_item_agents import ItemRuntime, item_fixture, metadata_plan

    class RejectOnce(ItemRuntime):
        def __init__(self):
            super().__init__()
            self.reviews = 0

        async def ask(self, role, message, **kwargs):
            raw, receipt = await super().ask(role, message, **kwargs)
            data = json.loads(message.rsplit('\n입력:\n', 1)[-1])
            if role == 'content_review':
                self.reviews += 1
                if self.reviews == 1:
                    row = data['stage_data']['draft']['diagnoses'][0]
                    defects = [{'where': '/diagnoses/0/' + key, 'quote': row[key],
                                'why': '합성 시험의 항목 범위를 명시한다.',
                                'fix': '확인 범위를 명시한다.', 'evidence_refs': []}
                               for key in ('text', 'impact')]
                    raw = json.dumps({'stage': 'review', 'run': data['run'], 'verdict': 'revise',
                        'content_checked': True, 'render_checked': False,
                        'defects': defects, 'unresolved': []}, ensure_ascii=False)
            elif role in ('author', 'direction_author') and data['stage_data'].get('prior_output'):
                assert len(data['stage_data']['accumulated_defects']) == 2
                candidate = deepcopy(data['stage_data']['prior_output'])
                candidate['diagnoses'][0]['text'] = '확인 범위를 구분한 합성 진단'
                candidate['diagnoses'][0]['impact'] = '확인 범위를 구분한 합성 영향'
                raw = json.dumps(candidate, ensure_ascii=False)
            return raw, receipt

    runtime = RejectOnce()
    pipe = Pipeline(runtime, tmp_path/'run')
    data = allocate_plan(item_fixture(), metadata_plan(), 'offline-plan')
    scoped = item_input(data, 'diagnosis', data['context']['action_item_plan']['diagnoses'][0])
    candidate = asyncio.run(pipe.reviewed_stage(scoped, 3, 'diagnosis-J_ITEM_0001',
        candidate_check=lambda value: validate_item(pipe, scoped, value)))
    assert candidate['diagnoses'][0]['text'] == '확인 범위를 구분한 합성 진단'
    author_ids = {call['session_id'] for call in runtime.calls if call['role'] in ('author', 'direction_author')}
    review_ids = {call['session_id'] for call in runtime.calls if call['role'] == 'content_review'}
    assert len(author_ids) == 1
    assert len(review_ids) == 1, 'item reviewer conversation changed with candidate hash'
    assert not author_ids & review_ids


def test_main_pipeline_calls_each_planned_item_writer_without_bulk_fallback(tmp_path):
    """정의된 항목 함수가 아니라 실제 전체 분석 소비자의 항목별 호출을 검증한다."""
    from copy import deepcopy
    import json
    from asis_pipeline import Pipeline
    from test_asis_connection import SyntheticRuntime, synthetic_input

    class ItemCapableRuntime(SyntheticRuntime):
        async def ask(self, role, message, **kwargs):
            data = json.loads(message.rsplit('\n입력:\n', 1)[-1])
            from area_item_plan_stub import new_stage_answer
            _common = new_stage_answer(data)
            if _common is not None:
                return json.dumps(_common, ensure_ascii=False), {'test_only': True, 'role': role, 'session_id': kwargs.get('session_id')}
            if 'stage_data' not in data:
                return await super().ask(role, message, **kwargs)
            sd = data['stage_data']
            if role == 'interpret' and sd.get('mode') == 'action_item_plan':
                refs = ('fact_refs', 'criterion_refs', 'goal_refs')
                directions = sd['approved_directions']['directions']
                issues = data['context']['analysis_plan']['issues']
                slots = []
                for direction in directions:
                    linked = [issue['id'] for issue in issues if issue['direction_id'] == direction['id']]
                    slots.append({'scope': direction['title'], 'reason': '합성 계획의 독립 검증 범위',
                        'direction_refs': [direction['id']], 'issue_refs': linked,
                        **{key: deepcopy(direction[key]) for key in refs}})
                value = {'diagnoses': slots, 'actions': [dict(deepcopy(slot), diagnosis_indices=[i])
                    for i, slot in enumerate(slots)], 'coverage': [
                    {'issue_id': issue['id'], 'diagnosis_indices': [i], 'action_indices': [i],
                     'disposition': 'included', 'reason': '합성 항목 배정', 'required_evidence': ''}
                    for i, issue in enumerate(issues)]}
            elif role in ('author', 'direction_author') and sd.get('item_assignment'):
                a = sd['item_assignment']; slot = a['slot']
                refs = {key: deepcopy(slot[key]) for key in ('direction_refs', 'fact_refs', 'criterion_refs', 'goal_refs')}
                if a['kind'] == 'diagnosis':
                    row = {'id': a['item_id'], 'text': slot['scope'], 'impact': '합성 항목 영향', **refs}
                    diagnoses, actions = [row], []
                else:
                    row = {'id': a['item_id'], 'title': slot['scope'], 'target': '합성 항목 대상',
                        'change_type': 'verify', 'rationale': slot['reason'], 'diagnosis_refs': slot['diagnosis_refs'],
                        'prerequisites': [], 'deliverable': '합성 확인 결과', 'verification_condition': '합성 조건 충족',
                        'priority_level': 'pending', 'priority_basis': '합성 우선순위 유보',
                        'improvement_points': ['합성 개선 포인트'], 'detailed_implementation_ref': None, **refs}
                    diagnoses, actions = [], [row]
                value = {'stage': 'actions', 'run': data['run'], 'diagnoses': diagnoses, 'actions': actions,
                    'edges': [{'from_id': ref, 'to_id': row['id'], 'relation': 'diagnoses' if diagnoses else 'addresses'}
                        for ref in row['direction_refs' if diagnoses else 'diagnosis_refs']], 'unresolved': []}
            else:
                raw, receipt = await super().ask(role, message, **kwargs)
                if role in ('author', 'direction_author') and sd.get('mode') == 'summary':
                    value = json.loads(raw)
                    actions = sd['approved_actions']['actions']
                    value['summary']['action_refs'] = [row['id'] for row in actions]
                    raw = json.dumps(value, ensure_ascii=False)
                return raw, receipt
            self.calls.append({'role': role, 'mode': sd.get('mode'), 'input': deepcopy(data), **kwargs})
            return json.dumps(value, ensure_ascii=False), {'test_only': True, 'role': role,
                'session_id': kwargs.get('session_id'), 'actual_model': 'synthetic_not_a_provider'}

    runtime = ItemCapableRuntime()
    result = asyncio.run(Pipeline(runtime, tmp_path/'run').analyze(synthetic_input(tmp_path)))
    actions = result['stages'][2]
    calls = [call for call in runtime.calls if call['role'] in ('author', 'direction_author') and call['mode'] == 'actions']
    expected = {row['id'] for family in ('diagnoses', 'actions') for row in actions[family]}
    assert len(calls) == len(expected), 'main consumer still uses one bulk actions writer'
    assert all(call['input']['stage_data'].get('item_assignment') for call in calls)
    assert {call['input']['stage_data']['item_assignment']['item_id'] for call in calls} == expected
    assert len({call['session_id'] for call in calls}) == len(expected)


def test_rendered_item_correction_returns_to_original_writer_and_keeps_siblings(tmp_path):
    """장표 검토 지적도 전체 actions 재작성 대신 원래 해당 항목으로 돌린다."""
    from copy import deepcopy
    from asis_pipeline import Pipeline
    from test_asis_item_agents import ItemRuntime, item_fixture

    runtime = ItemRuntime()
    pipe = Pipeline(runtime, tmp_path/'run')

    async def exercise():
        prepared, first = await pipe.reviewed_actions(item_fixture())
        row = first['actions'][0]
        defect = {'where': '/actions/0/title', 'quote': row['title'],
                  'why': '합성 렌더에서 범위 설명을 보완한다.', 'fix': '대상을 명시한다.', 'evidence_refs': []}
        prior_calls = list(runtime.calls)
        prepared['stage_data'].update(prior_output=deepcopy(first), accumulated_defects=[defect])
        _, second = await pipe.reviewed_actions(prepared)
        followups = [call for call in runtime.calls[len(prior_calls):] if call['role'] in ('author', 'direction_author')]
        assert len(followups) == 1, 'render correction reauthored accepted sibling items'
        call = followups[0]
        assignment = call['input']['stage_data']['item_assignment']
        assert assignment['item_id'] == row['id']
        original = next(c for c in prior_calls if c['role'] in ('author', 'direction_author') and
                        c['input']['stage_data']['item_assignment']['item_id'] == row['id'])
        assert call['session_id'] == original['session_id']
        assert call['input']['stage_data']['prior_output']['actions'] == [row]
        carried = call['input']['stage_data']['accumulated_defects']
        assert len(carried) == 1 and all(carried[0][key] == value for key, value in defect.items())
        assert carried[0]['source_where'] == defect['where']
        assert second['diagnoses'] == first['diagnoses'] and second['actions'][1:] == first['actions'][1:]

    asyncio.run(exercise())


def test_item_review_collects_all_parseable_candidate_findings_before_correction(tmp_path):
    """한 항목의 독립 검사들이 완료되기 전에 첫 지적만으로 재작성하지 않는다."""
    import json
    from asis_pipeline import Pipeline
    from test_asis_item_agents import ItemRuntime, item_fixture

    class TwoBadReferences(ItemRuntime):
        def __init__(self):
            super().__init__()
            self.target_authors = 0

        async def ask(self, role, message, **kwargs):
            raw, receipt = await super().ask(role, message, **kwargs)
            data = json.loads(message.rsplit('\n입력:\n', 1)[-1])
            assignment = data.get('stage_data', {}).get('item_assignment', {})
            if role in ('author', 'direction_author') and assignment.get('item_id') == 'A_ITEM_0001':
                self.target_authors += 1
                if self.target_authors == 1:
                    candidate = json.loads(raw)
                    candidate['actions'][0]['fact_refs'] = ['UNREGISTERED_FACT']
                    candidate['actions'][0]['criterion_refs'] = ['UNREGISTERED_CRITERION']
                    raw = json.dumps(candidate, ensure_ascii=False)
            return raw, receipt

    runtime = TwoBadReferences()
    asyncio.run(Pipeline(runtime, tmp_path/'run').reviewed_actions(item_fixture()))
    target = [call for call in runtime.calls if call['input'].get('stage_data', {}).get('item_assignment', {}).get('item_id') == 'A_ITEM_0001']
    authors = [call for call in target if call['role'] in ('author', 'direction_author')]
    assert len(authors) == 2
    findings = authors[1]['input']['stage_data']['accumulated_defects']
    assert {'/actions/0/fact_refs', '/actions/0/criterion_refs'} <= {row['where'] for row in findings}, 'only the first structural error was collected'
    first_review = next(call for call in target if call['role'] == 'content_review')
    assert first_review['input']['stage_data']['draft']['actions'][0]['fact_refs'] == ['UNREGISTERED_FACT']
    assert target.index(first_review) < target.index(authors[1]), 'corrective writer ran before the whole item review'
