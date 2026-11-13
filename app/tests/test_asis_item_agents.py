"""항목별 생산 경로의 계약 시험. 모델 경계만 명시적 합성 전송이다."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_analysis
from asis_pipeline import Pipeline


def item_fixture():
    data, area = asis_analysis.contract().synthetic_fixture()
    data['allocated_ids'].update(directions=['D1', 'D2'], diagnoses=[], actions=[])
    refs = {'fact_refs': ['F1'], 'criterion_refs': ['C1'], 'goal_refs': ['G1']}
    issues = [{'id': 'PLAN1', 'area_refs': ['network'], 'scope': '합성 접근 경계',
               'reason': '합성 참조 시험', **deepcopy(refs)},
              {'id': 'PLAN2', 'area_refs': ['network', 'ai'], 'scope': '합성 호출 품질',
               'reason': '합성 참조 시험', **deepcopy(refs)}]
    data['context']['analysis_plan'] = {'schema': 'asis-issue-plan/1', 'issues': issues}
    directions = [{'id': 'D'+str(i+1), 'title': issue['scope'], 'text': '합성 방향',
                   'customer_impact': '합성 영향', 'change_direction': '합성 확인',
                   'area_implication_refs': ['I1'], 'issue_refs': [issue['id']], **deepcopy(refs)} for i, issue in enumerate(issues)]
    data['stage_data'] = {'mode': 'actions', 'approved_areas': area,
        'approved_directions': {'stage': 'synthesis', 'run': deepcopy(data['run']),
            'directions': directions, 'unresolved': [],
            'edges': [{'from_id': r, 'to_id': d['id'], 'relation': rel}
                      for d in directions for key, rel in (('fact_refs', 'supports'),
                          ('area_implication_refs', 'consolidates')) for r in d[key]]}}
    return data


def metadata_plan():
    refs = {'fact_refs': ['F1'], 'criterion_refs': ['C1'], 'goal_refs': ['G1']}
    diagnoses = [{'scope': '합성 진단 '+str(i+1), 'reason': '검증할 결정 범위',
                  'issue_refs': ['PLAN'+str(i+1)], 'direction_refs': ['D'+str(i+1)],
                  **deepcopy(refs)} for i in range(2)]
    actions = [{'scope': '합성 개선 '+str(i+1), 'reason': '별도 검증 조건',
                'diagnosis_indices': [0 if i < 2 else 1],
                'issue_refs': ['PLAN1' if i < 2 else 'PLAN2'],
                'direction_refs': ['D1' if i < 2 else 'D2'], **deepcopy(refs)} for i in range(3)]
    coverage = [{'issue_id': 'PLAN1', 'diagnosis_indices': [0], 'action_indices': [0, 1],
                 'disposition': 'included', 'reason': '별도 조치 두 건', 'required_evidence': ''},
                {'issue_id': 'PLAN2', 'diagnosis_indices': [1], 'action_indices': [2],
                 'disposition': 'included', 'reason': '별도 조치 한 건', 'required_evidence': ''}]
    return {'diagnoses': diagnoses, 'actions': actions, 'coverage': coverage}


def area_slice_of_metadata_plan(view):
    """고정 계획(metadata_plan)에서 이 영역이 담당하는 이슈의 진단·과제·coverage만 뽑아 영역 계획 응답으로 돌려준다."""
    plan = metadata_plan()
    mine = {row['id'] for row in view['issues']}
    d_keep = [i for i, row in enumerate(plan['diagnoses']) if set(row['issue_refs']) <= mine and row['issue_refs']]
    a_keep = [i for i, row in enumerate(plan['actions']) if set(row['issue_refs']) <= mine and row['issue_refs']]
    d_map = {old: new for new, old in enumerate(d_keep)}; a_map = {old: new for new, old in enumerate(a_keep)}
    direction_ids = {row['id'] for row in view['directions']}
    def trim(row):
        return {'scope': row['scope'], 'reason': row['reason'], 'issue_refs': list(row['issue_refs']), 'direction_refs': [d for d in row['direction_refs'] if d in direction_ids]}
    return {'diagnoses': [trim(plan['diagnoses'][i]) for i in d_keep],
            'actions': [{**trim(plan['actions'][i]), 'diagnosis_indices': [d_map[j] for j in plan['actions'][i]['diagnosis_indices'] if j in d_map]} for i in a_keep],
            'coverage': [{**row, 'diagnosis_indices': [d_map[j] for j in row['diagnosis_indices'] if j in d_map], 'action_indices': [a_map[j] for j in row['action_indices'] if j in a_map]}
                         for row in plan['coverage'] if row['issue_id'] in mine]}


class ItemRuntime:
    """외부 모델 호출 없이 실제 조정기·검증기·원장을 실행하는 시험 경계."""
    def __init__(self):
        self.calls = []
        self.active = 0
        self.max_active = 0

    async def ask(self, role, message, **kwargs):
        data = json.loads(message.rsplit('\n입력:\n', 1)[-1])
        if 'stage_data' not in data and 'area' in data and 'issues' in data:
            # 영역별 진단·과제 계획 호출: 이 시험의 고정 계획(metadata_plan)을 이 영역의 이슈에 해당하는 부분만 돌려준다.
            self.calls.append({'role': role, 'input': deepcopy(data), **kwargs})
            return json.dumps(area_slice_of_metadata_plan(data), ensure_ascii=False), {'test_only': True, 'role': role, 'session_id': kwargs['session_id']}
        from area_item_plan_stub import new_stage_answer
        _common = new_stage_answer(data)
        if _common is not None:
            return json.dumps(_common, ensure_ascii=False), {'test_only': True, 'role': role, 'session_id': kwargs.get('session_id')}
        stage = data['stage_data']
        self.calls.append({'role': role, 'input': deepcopy(data), **kwargs})
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(.003)
        try:
            if role == 'interpret':
                value = metadata_plan()
            elif role in ('author', 'direction_author'):
                assignment = stage['item_assignment']
                kind, identifier = assignment['kind'], assignment['item_id']
                slot = assignment['slot']
                refs = {key: deepcopy(slot[key]) for key in ('fact_refs', 'criterion_refs', 'goal_refs', 'direction_refs')}
                if kind == 'diagnosis':
                    row = {'id': identifier, 'text': slot['scope'], 'impact': '합성 영향', **refs}
                    diagnoses, actions = [row], []
                else:
                    row = {'id': identifier, 'title': slot['scope'], 'target': '합성 대상',
                        'change_type': 'verify', 'rationale': slot['reason'],
                        'diagnosis_refs': deepcopy(slot['diagnosis_refs']), **refs,
                        'prerequisites': [], 'deliverable': '합성 검증 기록',
                        'verification_condition': '합성 조건 확인', 'priority_level': 'pending',
                        'priority_basis': '합성 판단 유보', 'improvement_points': ['합성 개선 포인트'],
                        'detailed_implementation_ref': None}
                    diagnoses, actions = [], [row]
                edges = [{'from_id': source, 'to_id': row['id'], 'relation': relation}
                    for key, relation in (('direction_refs', 'diagnoses'),) if diagnoses for source in row[key]]
                edges += [{'from_id': source, 'to_id': row['id'], 'relation': 'addresses'}
                    for source in row.get('diagnosis_refs', [])] if actions else []
                value = {'stage': 'actions', 'run': data['run'], 'diagnoses': diagnoses,
                    'actions': actions, 'edges': edges, 'unresolved': []}
            else:
                value = {'stage': 'review', 'run': data['run'], 'verdict': 'pass',
                    'content_checked': True, 'render_checked': False, 'defects': [], 'unresolved': []}
            return json.dumps(value, ensure_ascii=False), {'test_only': True, 'role': role,
                'session_id': kwargs.get('session_id'), 'actual_model': 'synthetic_not_a_provider'}
        finally:
            self.active -= 1


def test_each_diagnosis_and_action_has_its_own_author_review_and_exact_merge(tmp_path):
    data = item_fixture()
    original = deepcopy(data)
    runtime = ItemRuntime()
    events = []
    pipe = Pipeline(runtime, tmp_path/'run', progress=events.append)
    assert hasattr(pipe, 'reviewed_actions'), 'per-item actions entrypoint is missing'
    prepared, merged = asyncio.run(pipe.reviewed_actions(data))
    assert data == original, 'source evidence must not be mutated'
    assert len(merged['diagnoses']) == 2 and len(merged['actions']) == 3
    authors = [call for call in runtime.calls if call['role'] in ('author', 'direction_author')]
    assert len(authors) == len(merged['diagnoses']) + len(merged['actions'])
    assert len({call['session_id'] for call in authors}) == len(authors)
    for call in authors:
        item = call['input']['stage_data']['item_assignment']
        assert item['item_id'] in prepared['allocated_ids']['diagnoses' if item['kind'] == 'diagnosis' else 'actions']
        if item['kind'] == 'action':
            approved = call['input']['stage_data']['approved_diagnoses']
            assert {row['id'] for row in approved['diagnoses']} == set(item['slot']['diagnosis_refs'])
    item_reviews = [call for call in runtime.calls if call['role'] == 'content_review'
                    and call['input']['stage_data'].get('item_assignment')]
    assert len(item_reviews) == len(authors)
    assert not ({call['session_id'] for call in authors} & {call['session_id'] for call in item_reviews})
    # 합성 Runtime은 실제 자원 admission을 별도로 시험한다. 이 시험은 준비된 항목의 숨은 2건 장벽을 금지한다.
    assert 2 < runtime.max_active <= len(authors), 'ready independent items must reach resource admission'
    asis_analysis.contract().validate_output(prepared, merged)
    assert any(e['unit'] == 'actions' and e['phase'] == 'content_review' and e['status'] == 'completed' for e in events)
    assert prepared['facts'] == original['facts'] and prepared['judgments'] == original['judgments']
