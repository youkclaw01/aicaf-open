"""진단·개선과제별 MAF 작성·독립 검토와 결정적 병합을 실행한다."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

from asis_model import ASSET, digest, encoded
from asis_runtime import policy

PLAN_SCHEMA = 'asis-action-item-plan/1'
REFERENCE_KEYS = ('fact_refs', 'criterion_refs', 'goal_refs', 'direction_refs', 'issue_refs')


def validate_plan(data, candidate):
    """본문 없는 계획의 참조·전수 처리·다대다 관계를 검증한다."""
    if not isinstance(candidate, dict) or set(candidate) != {'diagnoses', 'actions', 'coverage'}:
        raise ValueError('invalid_action_item_plan_shape')
    if any(not isinstance(candidate[key], list) for key in candidate):
        raise ValueError('invalid_action_item_plan_lists')
    issues = data['context']['analysis_plan']['issues']
    allowed = {key: {row['id'] for row in data[family]} for key, family in
               (('fact_refs', 'facts'), ('criterion_refs', 'criteria'), ('goal_refs', 'goals'))}
    allowed['direction_refs'] = {row['id'] for row in data['stage_data']['approved_directions']['directions']}
    allowed['issue_refs'] = {row['id'] for row in issues}
    counts = {key: len(candidate[key]) for key in ('diagnoses', 'actions')}

    def indices(value, kind, required=False):
        if (not isinstance(value, list) or (required and not value)
                or any(type(index) is not int or not 0 <= index < counts[kind] for index in value)
                or len(value) != len(set(value))):
            raise ValueError('invalid_action_item_plan_indices:' + kind)

    for family in ('diagnoses', 'actions'):
        signatures = set()
        required = {'scope', 'reason', *REFERENCE_KEYS}
        if family == 'actions': required.add('diagnosis_indices')
        for row in candidate[family]:
            if (not isinstance(row, dict) or set(row) != required
                    or any(not isinstance(row[key], str) or not row[key].strip() for key in ('scope', 'reason'))):
                raise ValueError('invalid_action_item_plan_slot:' + family)
            for key in REFERENCE_KEYS:
                values = row[key]
                if (not isinstance(values, list) or (key != 'goal_refs' and not values)
                        or any(not isinstance(ref, str) or ref not in allowed[key] for ref in values)
                        or len(values) != len(set(values))):
                    raise ValueError('unbound_action_item_plan_reference:' + key)
            if family == 'actions': indices(row['diagnosis_indices'], 'diagnoses', required=True)
            signature = digest({key: value for key, value in row.items() if key != 'reason'})
            if signature in signatures: raise ValueError('duplicate_action_item_plan_slot:' + family)
            signatures.add(signature)
    covered = set()
    for row in candidate['coverage']:
        if (not isinstance(row, dict) or set(row) != {'issue_id', 'diagnosis_indices', 'action_indices',
                'disposition', 'reason', 'required_evidence'} or row['issue_id'] not in allowed['issue_refs']
                or row['issue_id'] in covered or row['disposition'] not in ('included', 'observation_only', 'deferred')
                or not isinstance(row['reason'], str) or not row['reason'].strip()
                or not isinstance(row['required_evidence'], str)):
            raise ValueError('invalid_action_item_plan_coverage')
        indices(row['diagnosis_indices'], 'diagnoses')
        indices(row['action_indices'], 'actions')
        for family, field in (('diagnoses', 'diagnosis_indices'), ('actions', 'action_indices')):
            expected = {i for i, slot in enumerate(candidate[family]) if row['issue_id'] in slot['issue_refs']}
            if set(row[field]) != expected: raise ValueError('action_item_plan_coverage_reference_mismatch')
        if row['disposition'] == 'included' and not row['diagnosis_indices']:
            raise ValueError('included_action_item_without_diagnosis')
        if row['disposition'] != 'included' and (row['diagnosis_indices'] or row['action_indices']):
            raise ValueError('nonincluded_action_item_has_slots')
        if row['disposition'] == 'deferred' and not row['required_evidence'].strip():
            raise ValueError('deferred_action_item_without_evidence_condition')
        covered.add(row['issue_id'])
    if covered != allowed['issue_refs']: raise ValueError('action_item_plan_issue_coverage_mismatch')


def allocate_plan(data, candidate, binding):
    """계획 해석과 고객 사실을 분리하고 ID·진단 관계는 코드가 할당한다."""
    prepared = deepcopy(data)
    diagnoses = [{**deepcopy(row), 'id': 'J_ITEM_' + str(i+1).zfill(4)}
                 for i, row in enumerate(candidate['diagnoses'])]
    actions = []
    for i, row in enumerate(candidate['actions']):
        slot = {key: deepcopy(value) for key, value in row.items() if key != 'diagnosis_indices'}
        slot.update(id='A_ITEM_' + str(i+1).zfill(4),
                    diagnosis_refs=[diagnoses[index]['id'] for index in row['diagnosis_indices']])
        actions.append(slot)
    prepared['allocated_ids']['diagnoses'] = [row['id'] for row in diagnoses]
    prepared['allocated_ids']['actions'] = [row['id'] for row in actions]
    coverage = [{key: deepcopy(value) for key, value in row.items()
                 if key not in ('diagnosis_indices', 'action_indices')} |
                {'diagnosis_refs': [diagnoses[i]['id'] for i in row['diagnosis_indices']],
                 'action_refs': [actions[i]['id'] for i in row['action_indices']]}
                for row in candidate['coverage']]
    prepared['context']['action_item_plan'] = {'schema': PLAN_SCHEMA, 'input_sha256': binding,
        'candidate_sha256': digest(candidate), 'diagnoses': diagnoses, 'actions': actions,
        'coverage': coverage, 'ids_allocated_by': 'code', 'customer_fact_promoted': False}
    return prepared


# ── 진단·과제 계획의 영역별 분할 ─────────────────────────────────────────────────────────────────────────────
# 이슈마다 한 방향에만 연결되므로(운영 실측: 254개가 1개 방향, 1개만 2개) 이슈의 주도 영역으로 나누면 중복 없이 분할된다.
# 모델은 영역마다 슬롯(범위·이유·연결 ID)만 정하고, 사실·기준·목표 ID는 이슈가 이미 가진 것을 코드가 가져온다.
# 전수 처리(coverage)와 ID 할당·합본은 코드가 한다. 모델 입력은 이슈·방향의 제목·이유·ID만 담고 사실 본문은 담지 않는다.

def lead_areas(data, area_keys):
    """이슈 ID → 주도 영역. 이슈 계획의 area_refs 첫 항목이 주도 영역이다(영역별 이슈 계획이 그 규칙을 강제한다)."""
    issues = data['context']['analysis_plan']['issues']
    result = {}
    for row in issues:
        refs = row.get('area_refs') or []
        lead = row.get('lead_area') or (refs[0] if refs else None)
        if lead not in area_keys:
            raise ValueError('issue_without_lead_area:' + str(row.get('id')))
        result[row['id']] = lead
    return result


def direction_of_issue(pipe, data):
    """이슈 ID → 그 이슈를 인용한 승인 방향 ID 목록.

    승인 방향은 종합 계획 슬롯과 같은 순서이고 슬롯이 이슈를 인용한다(issue_refs). 승인 결과에는 issue_refs가 남지 않아
    ① 종합 계획 체크포인트에서 복원하고, ② 체크포인트가 없는 입력(종합 계획을 거치지 않은 정본)은 승인 방향이 이슈를 직접 인용한
    기록(directions[].issue_refs)을 쓴다. 두 출처 모두 없으면 이슈의 direction_id로 짐작하지 않고 실패한다.
    (운영 입력에서 이슈 255개의 direction_id는 승인 방향 129개와 1:1이 아니어서 짐작하면 틀린 연결이 만들어진다.)
    """
    approved_rows = data['stage_data']['approved_directions']['directions']
    approved = [row['id'] for row in approved_rows]
    links = {row['id']: [] for row in data['context']['analysis_plan']['issues']}
    records = sorted((pipe.folder/'checkpoints'/'synthesis-plan').glob('*/accepted.json'), key=lambda p: p.stat().st_mtime)
    restored = False
    for path in reversed(records):
        slots = json.loads(path.read_text(encoding='utf-8')).get('candidate', {}).get('directions', [])
        if len(slots) == len(approved):
            for direction_id, slot in zip(approved, slots):
                for issue_id in slot['issue_refs']:
                    if issue_id in links and direction_id not in links[issue_id]:
                        links[issue_id].append(direction_id)
            restored = True
            break
    if not restored and all(isinstance(row.get('issue_refs'), list) for row in approved_rows):
        for row in approved_rows:
            for issue_id in row['issue_refs']:
                if issue_id in links and row['id'] not in links[issue_id]:
                    links[issue_id].append(row['id'])
        restored = True
    if not restored:
        raise ValueError('issue_direction_links_unavailable')
    if any(not rows for rows in links.values()):
        raise ValueError('issue_without_direction_link')
    return links


def area_plan_view(data, area_id, issue_ids, links):
    """한 영역 계획 호출에 보낼 입력. 이슈·방향의 제목·이유·ID만 담고 사실·기준 본문은 담지 않는다."""
    issues = {row['id']: row for row in data['context']['analysis_plan']['issues']}
    directions = {row['id']: row for row in data['stage_data']['approved_directions']['directions']}
    wanted_directions = []
    for issue_id in issue_ids:
        for ref in links.get(issue_id, []):
            if ref in directions and ref not in wanted_directions:
                wanted_directions.append(ref)
    return {'run': deepcopy(data['run']),
            'area': {'id': area_id},
            'issues': [{'id': i, 'scope': issues[i]['scope'], 'reason': issues[i]['reason'],
                        'direction_refs': [d for d in links.get(i, []) if d in directions]} for i in issue_ids],
            'directions': [{'id': d, 'title': directions[d]['title']} for d in wanted_directions]}


def project_area_plan(candidate):
    """근거 문자열 배열의 내용·순서를 보존해 계약의 문자열로 투영한다. 판정·참조는 변경하지 않는다."""
    result = deepcopy(candidate)
    changes = []
    if not isinstance(result, dict) or not isinstance(result.get('coverage'), list):
        return result, changes
    for index, row in enumerate(result['coverage']):
        if not isinstance(row, dict):
            continue
        value = row.get('required_evidence')
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            text = '\n'.join(value)
            changes.append({'where': '/coverage/' + str(index) + '/required_evidence',
                            'before': deepcopy(value), 'after': text, 'rule': 'string_list_to_newline_text'})
            row['required_evidence'] = text
    return result, changes


def validate_area_plan(view, candidate):
    """영역 계획 후보의 구조를 검증한다. 모델은 슬롯의 범위·이유·연결 인덱스만 정한다."""
    if not isinstance(candidate, dict) or set(candidate) != {'diagnoses', 'actions', 'coverage'}:
        raise ValueError('invalid_area_item_plan_shape')
    if any(not isinstance(candidate[key], list) for key in candidate):
        raise ValueError('invalid_area_item_plan_lists')
    issue_ids = {row['id'] for row in view['issues']}
    direction_ids = {row['id'] for row in view['directions']}
    counts = {key: len(candidate[key]) for key in ('diagnoses', 'actions')}

    def indices(value, kind, required=False):
        if (not isinstance(value, list) or (required and not value)
                or any(type(i) is not int or not 0 <= i < counts[kind] for i in value) or len(value) != len(set(value))):
            raise ValueError('invalid_area_item_plan_indices:' + kind)
    for family in ('diagnoses', 'actions'):
        required = {'scope', 'reason', 'issue_refs', 'direction_refs'} | ({'diagnosis_indices'} if family == 'actions' else set())
        seen = set()
        for row in candidate[family]:
            if (not isinstance(row, dict) or set(row) != required
                    or any(not isinstance(row[key], str) or not row[key].strip() for key in ('scope', 'reason'))):
                raise ValueError('invalid_area_item_plan_slot:' + family)
            for key, allowed in (('issue_refs', issue_ids), ('direction_refs', direction_ids)):
                values = row[key]
                if (not isinstance(values, list) or not values or any(not isinstance(v, str) or v not in allowed for v in values)
                        or len(values) != len(set(values))):
                    raise ValueError('unbound_area_item_plan_reference:' + key)
            if family == 'actions':
                indices(row['diagnosis_indices'], 'diagnoses', required=True)
            signature = digest({k: v for k, v in row.items() if k != 'reason'})
            if signature in seen:
                raise ValueError('duplicate_area_item_plan_slot:' + family)
            seen.add(signature)
    covered = set()
    for row in candidate['coverage']:
        if (not isinstance(row, dict) or set(row) != {'issue_id', 'diagnosis_indices', 'action_indices', 'disposition', 'reason', 'required_evidence'}
                or row['issue_id'] not in issue_ids or row['issue_id'] in covered
                or row['disposition'] not in ('included', 'observation_only', 'deferred')
                or not isinstance(row['reason'], str) or not row['reason'].strip() or not isinstance(row['required_evidence'], str)):
            raise ValueError('invalid_area_item_plan_coverage')
        indices(row['diagnosis_indices'], 'diagnoses'); indices(row['action_indices'], 'actions')
        for family, field in (('diagnoses', 'diagnosis_indices'), ('actions', 'action_indices')):
            expected = {i for i, slot in enumerate(candidate[family]) if row['issue_id'] in slot['issue_refs']}
            if set(row[field]) != expected:
                raise ValueError('area_item_plan_coverage_reference_mismatch')
        if row['disposition'] == 'included' and not row['diagnosis_indices']:
            raise ValueError('included_area_item_without_diagnosis')
        if row['disposition'] != 'included' and (row['diagnosis_indices'] or row['action_indices']):
            raise ValueError('nonincluded_area_item_has_slots')
        if row['disposition'] == 'deferred' and not row['required_evidence'].strip():
            raise ValueError('deferred_area_item_without_evidence_condition')
        covered.add(row['issue_id'])
    if covered != issue_ids:
        raise ValueError('area_item_plan_issue_coverage_mismatch')


def merge_area_plans(data, area_plans):
    """영역별 후보를 하나의 계획 후보로 합친다. 사실·기준·목표 ID는 이슈에서 코드가 가져온다(모델이 고르지 않는다).
    영역을 가로지르는 방향은 방향 ID로 이어지며, 전수 처리는 합본에서 다시 확인한다."""
    issues = {row['id']: row for row in data['context']['analysis_plan']['issues']}
    merged = {'diagnoses': [], 'actions': [], 'coverage': []}
    for area_id in sorted(area_plans):
        plan = area_plans[area_id]
        d_offset, a_offset = len(merged['diagnoses']), len(merged['actions'])

        def refs(slot):
            facts, criteria, goals = [], [], []
            for issue_id in slot['issue_refs']:
                for target, key in ((facts, 'fact_refs'), (criteria, 'criterion_refs'), (goals, 'goal_refs')):
                    target.extend(r for r in issues[issue_id].get(key, []) if r not in target)
            return {'fact_refs': facts, 'criterion_refs': criteria, 'goal_refs': goals}
        for slot in plan['diagnoses']:
            merged['diagnoses'].append({'scope': slot['scope'], 'reason': slot['reason'], 'issue_refs': list(slot['issue_refs']),
                                        'direction_refs': list(slot['direction_refs']), **refs(slot)})
        for slot in plan['actions']:
            merged['actions'].append({'scope': slot['scope'], 'reason': slot['reason'], 'issue_refs': list(slot['issue_refs']),
                                      'direction_refs': list(slot['direction_refs']), **refs(slot),
                                      'diagnosis_indices': [i + d_offset for i in slot['diagnosis_indices']]})
        for row in plan['coverage']:
            merged['coverage'].append({**deepcopy(row), 'diagnosis_indices': [i + d_offset for i in row['diagnosis_indices']],
                                       'action_indices': [i + a_offset for i in row['action_indices']]})
    return merged


# ── 통합 단계: 영역별 진단 주제 → 과제 통합 → 기존 슬롯 형식으로 연결 ────────────────────────────────────────
# 영역별 계획이 만든 진단·과제 후보(운영: 진단 213·과제 371)를 고객이 의사결정에 쓸 핵심 주제와 과제로 합친다.
# 모델은 후보 번호의 묶음과 제목·이유만 정하고, 참조 합집합·ID·전수 처리·상한은 코드(asis_consolidate)가 검증한다.
# 이후 단계(항목별 작성·검토·계약·Excel·PPT)는 같은 diagnoses/actions 형식을 쓰며 개수만 줄어든다.

def area_page_limits(candidates_by_area, recommended=100, fixed_pages=8):
    """영역별 진단 주제 상한(코드가 정한다): 영역 상한(기본 10, secgov 20)과 진단 후보 수 중 작은 값이다.

    장수를 맞추려고 영역 상한을 줄이지 않는다(영석님 지시). 영역이 적게 나오는 것은 모델이 합칠 내용이 적다고 판단했을 때뿐이다.
    합계가 권장 장수를 넘는지는 deck_page_budget이 기록하고, 상한(150)을 넘을 때만 생성이 실패한다."""
    import asis_consolidate as C
    return {area: min(C.area_limit(area), len(rows)) for area, rows in candidates_by_area.items()}


async def _consolidation_call(pipe, data, kind, view, instruction, validate, unit_key, root, system):
    """통합 호출 1건: 계약 오류는 직전 오류만 알려 다시 묻고, 그래도 안 되면 실패한다. 호출 입력·응답·영수증을 저장한다."""
    prior_error = None
    for attempt in range(1, pipe.rounds + 1):
        note = ('\n직전 응답의 오류만 고친다: ' + prior_error) if prior_error else ''
        raw, receipt = await pipe.runtime.ask('interpret', instruction + note + '\n입력:\n' + json.dumps(view, ensure_ascii=False),
            session_id='asis-consolidate-' + digest((data['run'], kind, unit_key, attempt))[:28], instructions=system)
        folder = root / unit_key / ('round-' + str(attempt).zfill(3))
        pipe.save(folder/'input.json', view); pipe.save(folder/'response.txt', raw.encode('utf-8')); pipe.save(folder/'receipt.json', receipt)
        try:
            return validate(pipe.validator.strict_load(raw))
        except (ValueError, KeyError, TypeError) as error:
            prior_error = str(error)
            pipe.save(folder/'contract-error.json', {'error': prior_error})
    raise RuntimeError('consolidation_failed:' + unit_key + ':' + str(prior_error))


async def consolidate_plan(pipe, data, base, root, system):
    """영역별 계획 후보(base)를 진단 주제와 과제로 통합해 기존 계획 후보 형식으로 돌려준다."""
    import asis_consolidate as C
    issues = {row['id']: row for row in data['context']['analysis_plan']['issues']}
    def slot_area(slot):
        counts = {}
        for ref in slot['issue_refs']:
            area = issues[ref].get('lead_area') or issues[ref]['area_refs'][0]
            counts[area] = counts.get(area, 0) + 1
        return sorted(counts, key=lambda a: (-counts[a], a))[0]
    by_area = {}
    for index, slot in enumerate(base['diagnoses']):
        by_area.setdefault(slot_area(slot), []).append(index)
    limits = area_page_limits({a: v for a, v in by_area.items()})
    topics_by_area = {}; topic_of = {}

    async def one_area(area):
        rows = [base['diagnoses'][i] for i in by_area[area]]
        view = C.diagnosis_grouping_view(area, rows); view['limit'] = limits[area]; view['run'] = deepcopy(data['run'])
        instruction = C.DIAGNOSIS_GROUPING_INSTRUCTION.format(limit=limits[area])
        groups = await _consolidation_call(pipe, data, 'topics', view, instruction,
            lambda response: C.validate_diagnosis_topics(len(rows), response, limits[area]), 'diagnosis-topics-' + area, root, system)
        return area, rows, C.build_topics(area, rows, groups)
    results = await asyncio.gather(*(one_area(a) for a in sorted(by_area)))
    all_topics = []
    for area, rows, topics in results:
        topics_by_area[area] = topics; all_topics.extend(topics)
        for topic in topics:
            for member in topic['member_indices']:
                topic_of[by_area[area][member]] = topic['id']
    titles = {t['id']: t['title'] for t in all_topics}
    candidates = C.link_actions_to_topics(base, slot_area, topic_of)
    view = C.action_grouping_view(candidates, titles); view['run'] = deepcopy(data['run'])
    groups = await _consolidation_call(pipe, data, 'actions', view, C.ACTION_GROUPING_INSTRUCTION.format(limit=C.ACTION_LIMIT),
        lambda response: C.validate_action_groups(len(candidates), response), 'action-groups', root, system)
    actions = C.build_actions(candidates, groups)
    total, parts, state = C.deck_page_budget(topics_by_area, len(actions))
    if state == 'over_max':
        raise RuntimeError('consolidated_deck_over_max_pages:%d' % total)
    plan = C.consolidated_plan(base, all_topics, actions)
    report = {'candidates': {'diagnoses': len(base['diagnoses']), 'actions': len(base['actions'])},
              'consolidated': {'diagnoses': len(plan['diagnoses']), 'actions': len(plan['actions'])},
              'page_budget': {'total': total, 'state': state, 'parts': parts}, 'area_limits': limits,
              'topic_members': {t['id']: t['member_indices'] for t in all_topics}, 'action_members': {a['id']: a['member_indices'] for a in actions}}
    # 통합 슬롯이 합친 통합 전 후보(제목·이유·이슈 참조). 계획 슬롯의 스키마는 바꾸지 않고 별도 추적표로 남긴다(Excel 원본 상세 시트의 근거).
    report['trace'] = {
        'diagnoses': [{'topic_id': t['id'], 'area_id': t['area_id'], 'title': t['title'],
                       'origin': [{'scope': base['diagnoses'][by_area[t['area_id']][m]]['scope'], 'reason': base['diagnoses'][by_area[t['area_id']][m]]['reason'],
                                   'issue_refs': list(base['diagnoses'][by_area[t['area_id']][m]]['issue_refs'])} for m in t['member_indices']]} for t in all_topics],
        'actions': [{'action_id': a['id'], 'title': a['title'],
                     'origin': [{'scope': base['actions'][m]['scope'], 'reason': base['actions'][m]['reason'], 'issue_refs': list(base['actions'][m]['issue_refs'])}
                                for m in a['member_indices']]} for a in actions]}
    pipe.save(root/'consolidation.json', report)
    return plan, report


async def plan_items(pipe, original):
    return await pipe.run_task(original,'action-item-plan',lambda:_plan_items(pipe,original))


async def _plan_items(pipe, original):
    """전체 본문을 생성하지 않고 가변 진단·과제 슬롯과 근거 범위만 계획한다."""
    data = deepcopy(original)
    data['context'].pop('action_item_plan', None)
    data['context'].pop('model_scope', None)
    data['allocated_ids']['diagnoses'] = []
    data['allocated_ids']['actions'] = []
    data['stage_data'] = {key: deepcopy(data['stage_data'][key])
                          for key in ('approved_areas', 'approved_directions')}
    data['stage_data'].update(mode='action_item_plan', planning_kind='action_items')
    pipe.validator.validate_input(data)
    # 주도 영역은 이슈 계획 확정(_finalize)이 area_refs 첫 항목으로 정한 값이다. 없으면 같은 규칙으로 구한다.
    area_keys = sorted({row.get('lead_area') or (row.get('area_refs') or [None])[0] for row in data['context']['analysis_plan']['issues']} - {None})
    lead = lead_areas(data, set(area_keys))
    links = direction_of_issue(pipe, data)
    instruction = AREA_PLAN_INSTRUCTION
    system = pipe.instructions((ASSET/'contract/prompts/P0-system.txt').read_text(encoding='utf-8'))
    area_binding = digest({'instruction': instruction, 'system': system, 'policy': policy(),
                           'implementation_sha256': 'area-local-view-v2', 'schema': PLAN_SCHEMA})
    # 영역별 진단·과제 후보 체크포인트는 통합 단계와 무관하게 보존한다(키가 같아 재사용). 통합 결과만 새 키로 저장한다.
    # 통합 결과의 재사용 키는 통합 지시문을 포함한다. 지시문을 고쳐도 키가 같으면 옛 지시문으로 만든 통합 결과(운영 2026-10-19: 상한 30개를 그대로 만든 계획)가 재사용되어
    # 새 지시문이 한 번도 쓰이지 않는다. 영역별 계획(area_binding)은 이 지시문과 무관하므로 그대로 재사용된다.
    import asis_consolidate as _C
    area_inputs = {area: area_plan_binding(data, area, [i for i,a in lead.items() if a == area], links,
                                         binding=area_binding) for area in area_keys}
    binding = digest({'area_binding': area_binding, 'area_inputs': area_inputs, 'consolidation': 'consolidate-v1', 'limits': [10, 19],
                      'consolidation_instruction': {'actions': _C.ACTION_GROUPING_INSTRUCTION, 'topics': _C.DIAGNOSIS_GROUPING_INSTRUCTION,
                                                     'action_limit': _C.ACTION_LIMIT, 'action_recommended': _C.ACTION_RECOMMENDED}})
    area_root = pipe.folder/'checkpoints'/'action-item-plan'/area_binding
    root = pipe.folder/'checkpoints'/'action-item-plan'/binding
    accepted = root/'accepted.json'
    if not accepted.exists():
        plan_root = pipe.folder/'checkpoints'/'action-item-plan'
        has_upstream_repair = bool(original.get('context', {}).get('same_revision_recovery') or (plan_root.is_dir() and (pipe.folder / 'analysis-reviewed.json').is_file()))
        if has_upstream_repair and plan_root.is_dir():
            for cand_file in sorted(plan_root.glob('*/accepted.json'), key=lambda p: p.stat().st_mtime, reverse=True):
                try:
                    cached = json.loads(cand_file.read_text(encoding='utf-8'))
                    if (cached.get('candidate_sha256') == digest(cached.get('candidate'))
                            and cached.get('review', {}).get('verdict') == 'pass'
                            and isinstance(cached.get('trace'), dict)):
                        validate_plan(data, cached['candidate'])
                        pipe.save(accepted, {**cached, 'input_sha256': binding})
                        break
                except (ValueError, KeyError, TypeError, OSError):
                    continue
    if accepted.exists():
        cached = json.loads(accepted.read_text(encoding='utf-8'))
        if (cached.get('input_sha256') != binding or cached.get('candidate_sha256') != digest(cached.get('candidate'))
                or cached.get('review', {}).get('verdict') != 'pass'):
            raise ValueError('action_item_plan_checkpoint_binding_failed')
        validate_plan(data, cached['candidate'])
        pipe.event('action-item-plan', 'content_review', 'reused')
        prepared = allocate_plan(original, cached['candidate'], binding)
        prepared['context']['action_item_plan']['consolidation_trace'] = cached['trace']
        return prepared
    from asis_tasks import task
    manager = pipe.task_manager(original)
    for area_id in area_keys:
        key = 'action-item-plan-area-' + area_id
        if key not in manager.ledger['tasks']:
            manager.register([task(key, area_id + ' 진단·과제 계획', executor='model', pair_key=key)])

    async def unit(area_id):
        async with pipe.item_gate:
            issue_ids = [i for i, a in lead.items() if a == area_id]
            return area_id, await _plan_area_items(pipe, original, data, area_id, issue_ids, links, instruction, system, area_binding, area_root)
    results = await asyncio.gather(*(unit(a) for a in area_keys), return_exceptions=True)
    failures = [row for row in results if isinstance(row, BaseException)]
    if failures:
        raise failures[0]
    candidate = merge_area_plans(data, dict(results))
    # 영역 호출은 자기 이슈만 처리한다. 모든 이슈가 정확히 한 번 처리됐는지는 합본에서 코드가 다시 확인한다.
    validate_plan(data, candidate)
    # 통합 전 후보(영역별 진단·과제)를 고객이 의사결정에 쓸 핵심 주제·과제로 합친다. 결과는 같은 계획 형식이다.
    candidate, consolidation = await consolidate_plan(pipe, data, candidate, root, system)
    validate_plan(data, candidate)
    trace = consolidation['trace']
    review = {'verdict': 'pass', 'content_checked': True, 'render_checked': False, 'defects': [], 'unresolved': [],
              'review_mode': 'area_plans_reviewed_individually_merged_and_validated_by_code'}
    pipe.save(accepted, {'input_sha256': binding, 'candidate_sha256': digest(candidate), 'candidate': candidate, 'review': review, 'trace': trace,
                         'execution_classification': pipe.classification(original)})
    prepared = allocate_plan(original, candidate, binding)
    prepared['context']['action_item_plan']['consolidation_trace'] = trace
    pipe.validator.validate_input(prepared)
    pipe.event('action-item-plan', 'content_review', 'completed',
               diagnosis_count=len(candidate['diagnoses']), action_count=len(candidate['actions']))
    return prepared


AREA_PLAN_INSTRUCTION = (
    '이번 호출에 배정된 영역의 이슈(제목·이유)와 그 이슈가 연결된 종합 방향(제목)만으로 진단·개선과제의 작업 범위를 계획한다. '
    '본문·사실·수치·ID를 새로 만들지 않는다. 진단과 과제의 개수는 독립·가변이며 1:1이나 이슈당 한 건으로 고정하지 않는다. '
    '한 과제는 복수 진단에 연결할 수 있다. 다른 대상·선행 조건·검증 조건은 별도 과제로 분리한다. '
    '각 진단과 과제는 scope, reason, issue_refs, direction_refs만 가진다(입력에 있는 이슈 ID·방향 ID만 쓴다). '
    '과제에는 diagnosis_indices(이 응답 diagnoses 배열의 0부터 시작하는 정수 위치)를 추가한다. '
    '입력의 모든 이슈를 coverage에 정확히 한 번씩 처리한다. coverage 항목은 issue_id, diagnosis_indices, action_indices, disposition, reason, required_evidence다. '
    'disposition=included는 실제 슬롯과 연결, observation_only는 진단·조치 불필요의 근거, deferred는 필요한 근거와 판단 조건을 명시하고 슬롯은 비운다. '
    '미확인을 결함·정상으로 승격하지 않는다. 응답은 정확히 {"diagnoses":[],"actions":[],"coverage":[]} JSON이다. '
    '수정에는 직전 후보와 위치·원문·이유·수정 방향을 사용하고 무관한 계획은 보존한다. 도구는 사용하지 않는다.')


REVIEW_ROLE_LIMIT = ('이 검토는 진단·과제 작업 범위 계획이다. 계획의 scope·reason은 이슈 plan_issues의 서술 범위를 따른 것이므로, 이슈 서술과의 일치·이슈 전수 처리·방향 연결·'
                     '미확인의 보존만 검토한다. 이슈 서술이 인용한 수집 사실의 본문 대조는 이후 항목별 작성·검토 단계가 인용 사실 본문으로 수행한다. '
                     'fact_summary에 본문이 없다는 사실만으로 blocked로 판정하지 않는다.')


def fact_summary(data, issue_rows):
    """이슈가 연결한 사실의 요약(ID·출처·경로·수집 상태·건수). 본문은 보내지 않는다. 검토자가 서술의 근거 출처를 확인할 수 있게 한다."""
    wanted = {ref for row in issue_rows for ref in row.get('fact_refs', [])}
    out = []
    for fact in data.get('facts', []):
        if fact['id'] in wanted:
            value = fact.get('value')
            out.append({'id': fact['id'], 'source_id': fact.get('source_id'), 'field_path': fact.get('field_path'),
                        'collection_status': fact.get('collection_status'), 'scope_complete': fact.get('scope_complete'),
                        'record_count': len(value) if isinstance(value, list) else None})
    return out


def review_input(data, view):
    """영역 계획 검토에 보내는 정본 형태의 소형 입력.

    이슈·방향의 제목·이유와 이슈가 연결한 사실의 요약(본문 없음)을 담는다. 이슈 reason이 이미 'HTTP 499 관측' 같은 사실 서술을 포함하므로
    검토자가 서술의 출처를 볼 수 있어야 한다(운영 실측: 사실이 비어 있어 telemetry 영역이 '사실 검증을 완료할 수 없다'며 blocked).
    사실 본문 대조는 이후 항목별 작성·검토가 맡는다는 역할 한계를 함께 명시한다."""
    issue_rows = {row['id']: row for row in data['context']['analysis_plan']['issues']}
    mine = [issue_rows[row['id']] for row in view['issues']]
    shell = {key: deepcopy(data[key]) for key in ('run', 'goals') if key in data}
    shell.update(facts=[], criteria=[], judgments=[], measurements=[], allocated_ids={'diagnoses': [], 'actions': []})
    shell['context'] = {'model_scope': {'projection_schema': 'asis-model-scope/1', 'fact_refs': [], 'criterion_refs': [], 'goal_refs': [row['id'] for row in data.get('goals', [])],
                                        'inline_fact_refs': []},
                        'analysis_plan': {'issues': [{'id': row['id'], 'scope': row['scope'], 'reason': row['reason']} for row in view['issues']]}}
    shell['stage_data'] = {'mode': 'action_item_plan', 'planning_kind': 'action_items', 'plan_view': deepcopy(view),
                           'fact_summary': fact_summary(data, mine), 'review_role_limit': REVIEW_ROLE_LIMIT}
    return shell


def area_plan_binding(data, area_id, issue_ids, links, *, binding):
    """실제로 읽는 영역 뷰와 지시·모델·스키마만 결합한다. 다른 영역 본문은 키에 넣지 않는다."""
    return digest({'binding': binding, 'area': area_id, 'view': area_plan_view(data, area_id, issue_ids, links)})


def saved_area_plan_round(folder, view, validator):
    """같은 입력의 마지막 작성 원본·지적·소진 횟수를 읽는다. 원본 파일은 변경하지 않는다."""
    rounds = sorted((int(p.name[6:]), p) for p in folder.glob('round-*')
                    if p.is_dir() and p.name[6:].isdigit())
    if not rounds:
        return None
    attempt, source = rounds[-1]
    paths = {name: source/name for name in ('input.json', 'response.txt', 'receipt.json')}
    if not all(p.is_file() for p in paths.values()):
        raise ValueError('area_item_plan_checkpoint_incomplete:' + str(source))
    sent = json.loads(paths['input.json'].read_text(encoding='utf-8'))
    if (not isinstance(sent, dict) or set(sent) - set(view) - {'prior_output', 'accumulated_defects'}
            or digest({key: sent.get(key) for key in view}) != digest(view)):
        raise ValueError('area_item_plan_checkpoint_input_mismatch:' + str(source))
    raw = paths['response.txt'].read_text(encoding='utf-8')
    review_path = source/'review.json'
    review = json.loads(review_path.read_text(encoding='utf-8')) if review_path.is_file() else None
    candidate = None; source_hash = None; changes = []; error = None
    try:
        candidate = validator.strict_load(raw)
        source_hash = digest(candidate)
        candidate, changes = project_area_plan(candidate)
        validate_area_plan(view, candidate)
    except (ValueError, TypeError, KeyError) as exc:
        error = str(exc)
    return {'attempt': attempt, 'source': source, 'candidate': candidate, 'review': review,
            'prior': candidate if candidate is not None else {'raw_response': raw},
            'defects': deepcopy(sent.get('accumulated_defects', [])), 'error': error,
            'proof': {'source_round': source.name, 'source_files_sha256': {
                name: sha256(path.read_bytes()).hexdigest() for name, path in paths.items()},
                'source_candidate_sha256': source_hash, 'projected_candidate_sha256': digest(candidate),
                'changes': changes}}


async def _plan_area_items(pipe, original, data, area_id, issue_ids, links, instruction, system, binding, root):
    """한 영역의 진단·과제 계획을 작성하고 독립 검토한다. 완료 저장 단위다."""
    done = root/'areas'/area_id/'accepted.json'
    unit_key = 'action-item-plan-area-' + area_id
    view = area_plan_view(data, area_id, issue_ids, links)
    unit_binding = area_plan_binding(data, area_id, issue_ids, links, binding=binding)
    if done.exists():
        record = json.loads(done.read_text(encoding='utf-8'))
        if record.get('input_sha256') == unit_binding and record.get('candidate_sha256') == digest(record.get('candidate')):
            validate_area_plan(view, record['candidate'])
            pipe.event(unit_key, 'interpret', 'reused')
            return record['candidate']
    review_view = review_input(data, view)
    prior = None; defects = []
    recovery = {}
    async def work():
        nonlocal prior
        remaining = max(pipe.rounds, 6); attempt = 0
        saved = saved_area_plan_round(root/'areas'/area_id, view, pipe.validator)
        if saved:
            attempt = saved['attempt']; remaining = max(0, remaining - attempt)
            prior = saved['prior']; defects.extend(saved['defects'])
            old_review = saved['review']
            if saved['error']:
                defects.append({'where': '/', 'quote': json.dumps(prior, ensure_ascii=False)[:500],
                                'why': saved['error'], 'fix': '지적된 계약을 수정하고 마지막 후보의 나머지 내용은 보존한다.', 'evidence_refs': []})
            elif not old_review or old_review.get('verdict') == 'pass' or saved['proof']['changes']:
                recovered = saved['source']/'recoveries'/uuid4().hex
                proof = {**saved['proof'], 'input_sha256': unit_binding, 'review_input_sha256': digest(review_view)}
                pipe.save(recovered/'proof.json', proof)
                pipe.event(unit_key, 'interpret', 'resumed', round=attempt, author_requests=0)
                review = await pipe.independent_review(review_view, saved['candidate'], unit_key + '-review')
                pipe.save(recovered/'review.json', review)
                if review['verdict'] == 'pass':
                    recovery.update(source_round=saved['source'].name, proof_path=str(recovered/'proof.json'))
                    return saved['candidate'], review
                old_review = review
            if old_review:
                if old_review['verdict'] == 'blocked':
                    raise RuntimeError('action_item_plan_area_review_blocked:' + area_id)
                defects.extend(old_review.get('defects', []))
        while remaining > 0:
            attempt += 1; remaining -= 1
            sent = {**view, 'prior_output': prior, 'accumulated_defects': deepcopy(defects)}
            pipe.event(unit_key, 'interpret', 'running', round=attempt)
            raw, receipt = await pipe.runtime.ask('interpret', instruction + '\n입력:\n' + json.dumps(sent, ensure_ascii=False),
                session_id='asis-item-plan-' + digest((data['run'], unit_binding, attempt))[:32], instructions=system)
            folder = root/'areas'/area_id/('round-' + str(attempt).zfill(3))
            pipe.save(folder/'input.json', sent); pipe.save(folder/'response.txt', raw.encode('utf-8')); pipe.save(folder/'receipt.json', receipt)
            candidate = None
            try:
                candidate = pipe.validator.strict_load(raw)
                source_candidate_hash = digest(candidate)
                candidate, projection = project_area_plan(candidate)
                if projection:
                    pipe.save(folder/'projection.json', {'source_candidate_sha256': source_candidate_hash,
                              'projected_candidate_sha256': digest(candidate), 'changes': projection})
                validate_area_plan(view, candidate)
            except (ValueError, TypeError, KeyError) as exc:
                prior = candidate if candidate is not None else {'raw_response': raw}
                defects.append({'where': '/', 'quote': raw[:500], 'why': str(exc),
                                'fix': 'Use the declared metadata-only plan schema, only the issue and direction IDs given, and process every issue once.', 'evidence_refs': []})
                continue
            review = await pipe.independent_review(review_view, candidate, unit_key + '-review')
            pipe.save(folder/'review.json', review)
            if review['verdict'] == 'pass':
                return candidate, review
            if review['verdict'] == 'blocked':
                raise RuntimeError('action_item_plan_area_review_blocked:' + area_id)
            prior = candidate; defects.extend(review['defects'])
        raise RuntimeError('action_item_plan_area_budget_exhausted:' + area_id)
    candidate, review = await pipe.run_task(view, unit_key, work)
    record = {'input_sha256': unit_binding, 'candidate_sha256': digest(candidate), 'candidate': candidate, 'review': review}
    if recovery:
        record['recovery'] = recovery
    pipe.save(done, record)
    pipe.event(unit_key, 'interpret', 'completed', diagnoses=len(candidate['diagnoses']), actions=len(candidate['actions']))
    return candidate

def item_input(data, kind, slot, diagnoses=None):
    """항목의 원천 근거와 공통 고객 맥락을 보존하고 다른 항목의 본문 작성은 배정하지 않는다."""
    scoped = deepcopy(data)
    # 통합 추적표(통합 전 진단·과제 수백 건의 제목·이유)는 Excel 원본 연결용이다. 항목 작성·검토 모델은 쓰지 않으므로 입력에서 뺀다(정본 data에는 그대로 있다).
    # 운영 2026-10-16: 이 추적표가 모든 항목 입력에 38만 자씩 실려 한도(90만)를 넘어 진단 항목 수십 건이 모델 호출 전에 실패했다.
    plan_context = scoped.get('context', {}).get('action_item_plan')
    if isinstance(plan_context, dict):
        plan_context.pop('consolidation_trace', None)
    # 항목 1개를 쓰는 모델은 통합 전 계획 전체(진단·과제 슬롯 수백 건)도, 연결하지 않은 이슈 전체도 쓰지 않는다. 정본(data)은 그대로 두고 전송용 사본에서만 뺀다.
    # 방향 입력(direction_input)이 이미 같은 규칙으로 이슈를 줄인다. 운영 2026-10-16: 항목 입력 100건 중 77건이 90만 자를 넘어 모델 호출 전에 실패했다.
    scoped.get('context', {}).pop('action_item_plan', None)
    plan = scoped.get('context', {}).get('analysis_plan')
    if isinstance(plan, dict) and isinstance(plan.get('issues'), list):
        wanted = set(slot.get('issue_refs', []))
        kept = [row for row in plan['issues'] if row.get('id') in wanted]
        scoped['context']['analysis_plan'] = {**plan, 'issues': kept,
            'model_scope_note': {'item_id': slot['id'], 'kept_issues': len(kept), 'total_issues': len(plan['issues']),
                                 'rule': 'issues linked to this item only'}}
    directions = data['stage_data']['approved_directions']
    selected = [deepcopy(row) for row in directions['directions'] if row['id'] in slot['direction_refs']]
    scoped['stage_data'] = {'mode': 'actions',
        'item_assignment': {'kind': kind, 'item_id': slot['id'], 'slot': deepcopy(slot)},
        'approved_areas': deepcopy(data['stage_data']['approved_areas']),
        'approved_directions': {**deepcopy(directions), 'directions': selected,
            'edges': [deepcopy(row) for row in directions['edges'] if row['to_id'] in slot['direction_refs']]}}
    inline = set(slot['fact_refs']) | {ref for row in selected for ref in row['fact_refs']}
    if kind == 'action':
        linked = [deepcopy(diagnoses[ref]) for ref in slot['diagnosis_refs']]
        scoped['stage_data']['approved_diagnoses'] = {'stage': 'actions', 'run': deepcopy(data['run']),
            'diagnoses': linked, 'actions': [], 'unresolved': [],
            'edges': [{'from_id': ref, 'to_id': row['id'], 'relation': 'diagnoses'}
                      for row in linked for ref in row['direction_refs']]}
        inline.update(ref for row in linked for ref in row['fact_refs'])
    scoped['context']['model_scope'] = {'item_id': slot['id'], 'kind': kind,
        'inline_fact_refs': sorted(inline), 'scope_complete_not_implied': True}
    family, sibling = ('diagnoses', 'actions') if kind == 'diagnosis' else ('actions', 'diagnoses')
    field, relation = ('direction_refs', 'diagnoses') if kind == 'diagnosis' else ('diagnosis_refs', 'addresses')
    schema = json.loads((ASSET/'contract/output.schema.json').read_text(encoding='utf-8'))
    branch = deepcopy(next(row for row in schema['oneOf'] if row['properties']['stage']['const'] == 'actions'))
    branch['properties'][family].update(minItems=1, maxItems=1)
    branch['properties'][sibling].update(maxItems=0)
    scoped['stage_data']['item_contract'] = {
        'schema':'asis-single-item-contract/1', 'item_id':slot['id'], 'kind':kind,
        'output_family':family, 'empty_sibling':sibling, 'external_nodes_are_reference_only':True,
        'graph_authority':'code_from_declared_semantic_references',
        'expected_edges':[{'from_id':ref,'to_id':slot['id'],'relation':relation} for ref in slot[field]],
        'author_output_schema':encoded(branch).decode('utf-8'),
        'scope_rule':'author_output_schema는 실제 작성 출력 스키마의 JSON 문자열이다. 할당된 단일 항목만 출력한다. 승인된 선행 노드는 외부 참조이며 후보에 추가하거나 재작성하지 않는다. '
            '관계선은 코드가 의미 참조에서 생성한다. 작성·검토·구조 검사는 동일 계약을 적용하며 정상 관계의 반전·삭제를 지시하지 않는다.'}
    return scoped


def project_item_graph(candidate, assignment):
    """본문·의미 참조·할당 범위는 보존하고 중복 표현인 관계선만 결정적으로 생성한다."""
    value = deepcopy(candidate)
    if not isinstance(value, dict):
        return value
    family, sibling = ('diagnoses', 'actions') if assignment['kind'] == 'diagnosis' else ('actions', 'diagnoses')
    rows = value.get(family)
    if (value.get('stage') != 'actions' or value.get(sibling) != []
            or not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
            or rows[0].get('id') != assignment['item_id']):
        # 잘못된 항목·범위·참조를 투영으로 숨기지 않는다. 기존 검사기가 그대로 거부한다.
        return value
    field, relation = ('direction_refs', 'diagnoses') if family == 'diagnoses' else ('diagnosis_refs', 'addresses')
    refs = rows[0].get(field)
    if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
        return value
    value['edges'] = [{'from_id': ref, 'to_id': rows[0]['id'], 'relation': relation} for ref in refs]
    return value


def validate_item(pipe, scoped, candidate):
    """다른 항목의 출력·필수 참조 누락·관계 변경을 구조 검증으로 거부한다."""
    assignment = scoped['stage_data']['item_assignment']
    kind, slot = assignment['kind'], assignment['slot']
    family, sibling = ('diagnoses', 'actions') if kind == 'diagnosis' else ('actions', 'diagnoses')
    if candidate.get('stage') != 'actions' or candidate.get(sibling) != []:
        raise ValueError('action_item_output_scope_mismatch')
    rows = candidate.get(family)
    if not isinstance(rows, list) or len(rows) != 1 or rows[0].get('id') != assignment['item_id']:
        raise ValueError('action_item_output_coverage_mismatch')
    row = rows[0]
    if kind == 'action':
        # 장표 한 장에 담기는 분량을 작성 단계에서 확인한다(넘으면 작성자에게 다시 쓰게 한다). 글자를 줄여 그리지 않는다.
        import asis_consolidated_deck as deck
        deck.action_overview_blocks(row)
    for key in ('direction_refs', 'diagnosis_refs') if kind == 'action' else ('direction_refs',):
        if set(row[key]) != set(slot[key]): raise ValueError('action_item_relation_binding_failed:' + key)
    for key in ('fact_refs', 'criterion_refs', 'goal_refs'):
        if set(slot[key]) - set(row[key]): raise ValueError('action_item_required_reference_missing:' + key)


def validate_item_review(scoped, candidate, review):
    """정상인 코드 소유 구조를 바꾸라는 검토 지시를 작성자 결함으로 전가하지 않는다."""
    assignment = scoped['stage_data']['item_assignment']
    try:
        validate_item(None, scoped, candidate)
    except (ValueError, KeyError, TypeError):
        return
    if candidate.get('run') != scoped['run']:
        return
    projected = project_item_graph(candidate, assignment)
    edges = projected.get('edges', [])
    tuples = [(row['from_id'], row['to_id'], row['relation']) for row in edges]
    graph_valid = candidate.get('edges') == edges and len(tuples) == len(set(tuples))
    sibling = 'actions' if assignment['kind'] == 'diagnosis' else 'diagnoses'
    for defect in review.get('defects', []):
        where = defect.get('where', '')
        if graph_valid and (where == '/edges' or where.startswith('/edges/')):
            raise ValueError('item_review_code_owned_graph_conflict')
        if where == '/'+sibling and candidate.get(sibling) == []:
            raise ValueError('item_review_output_scope_conflict')
        family = 'diagnoses' if assignment['kind'] == 'diagnosis' else 'actions'
        fixed = ('id', 'direction_refs', 'diagnosis_refs') if family == 'actions' else ('id', 'direction_refs')
        if any(where == '/'+family+'/0/'+field or where.startswith('/'+family+'/0/'+field+'/') for field in fixed):
            raise ValueError('item_review_allocated_reference_conflict')


def collect_item_defects(pipe, scoped, candidate):
    """파싱된 항목 후보의 스키마·ID·근거·관계를 전수 확인하고 literal 지적을 모은다."""
    from jsonschema import Draft202012Validator
    defects = []

    def add(path, value, why, fix='등록 원장·할당 ID와 해당 항목의 이전 원문을 보존하여 수정한다.'):
        quote = value if isinstance(value, str) and value else json.dumps(value, ensure_ascii=False)
        row = {'where': path or '/', 'quote': quote, 'why': why, 'fix': fix, 'evidence_refs': []}
        if row not in defects:
            defects.append(row)

    def nodes(value, path=''):
        if isinstance(value, dict):
            yield path, value
            for key, child in value.items():
                yield from nodes(child, path+'/'+str(key).replace('~', '~0').replace('/', '~1'))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                yield from nodes(child, path+'/'+str(index))

    branch = next(row for row in pipe.validator.output_schema()['oneOf']
                  if row['properties']['stage']['const'] == 'actions')
    for error in Draft202012Validator(branch).iter_errors(candidate):
        path = '/'.join(str(key).replace('~', '~0').replace('/', '~1') for key in error.absolute_path)
        add('/'+path, error.instance, 'output_schema:'+error.validator)
    if not isinstance(candidate, dict):
        return {'defects': defects, 'content_review_possible': False, 'checks': ['schema']}
    if candidate.get('run') != scoped['run'] or candidate.get('stage') != 'actions':
        add('/', candidate, 'action_item_run_or_stage_binding_failed')
        return {'defects': defects, 'content_review_possible': False, 'checks': ['schema', 'isolation']}
    allowed = {key: {row['id'] for row in scoped[family]} for key, family in
        (('fact_refs', 'facts'), ('criterion_refs', 'criteria'), ('goal_refs', 'goals'), ('judgment_refs', 'judgments'))}
    allowed['evidence_refs'] = set().union(*allowed.values())
    allocated = {identifier for family in scoped['allocated_ids'].values() for identifier in family}
    prior_ids = {row['id'] for _, row in nodes(scoped['stage_data']) if isinstance(row.get('id'), str)}
    ids = []
    for path, row in nodes(candidate):
        for key, refs in allowed.items():
            if isinstance(row.get(key), list) and any(not isinstance(ref, str) or ref not in refs for ref in row[key]):
                add(path+'/'+key, row[key], 'invalid_reference:'+key)
        if 'id' in row:
            if not isinstance(row['id'], str) or row['id'] not in allocated:
                add(path+'/id', row['id'], 'unallocated_output_id')
            elif row['id'] in ids:
                add(path+'/id', row['id'], 'duplicate_output_id')
            ids.append(row['id'])
    all_ids = prior_ids | {identifier for identifier in ids if isinstance(identifier, str)} | set().union(*allowed.values())
    for path, row in nodes(candidate):
        for key in ('direction_refs', 'diagnosis_refs', 'action_refs', 'area_implication_refs'):
            if isinstance(row.get(key), list) and any(not isinstance(ref, str) or ref not in all_ids for ref in row[key]):
                add(path+'/'+key, row[key], 'invalid_relation_reference:'+key)
    assignment = scoped['stage_data']['item_assignment']
    slot = assignment['slot']
    family, sibling = ('diagnoses', 'actions') if assignment['kind'] == 'diagnosis' else ('actions', 'diagnoses')
    rows = candidate.get(family)
    scope_valid = (candidate.get(sibling) == [] and isinstance(rows, list) and len(rows) == 1
                   and isinstance(rows[0], dict) and rows[0].get('id') == assignment['item_id'])
    if not scope_valid:
        add('/', candidate, 'action_item_output_scope_or_coverage_mismatch')
    else:
        row = rows[0]
        for key in ('direction_refs', 'diagnosis_refs') if family == 'actions' else ('direction_refs',):
            if isinstance(row.get(key), list) and set(row[key]) != set(slot[key]):
                add('/'+family+'/0/'+key, row[key], 'action_item_relation_binding_failed:'+key)
        for key in ('fact_refs', 'criterion_refs', 'goal_refs'):
            if isinstance(row.get(key), list) and set(slot[key]) - set(row[key]):
                add('/'+family+'/0/'+key, row[key], 'action_item_required_reference_missing:'+key)
    expected = set()
    for key, field, relation in (('diagnoses', 'direction_refs', 'diagnoses'), ('actions', 'diagnosis_refs', 'addresses')):
        for row in candidate.get(key, []) if isinstance(candidate.get(key), list) else []:
            if isinstance(row, dict) and isinstance(row.get('id'), str) and isinstance(row.get(field), list):
                expected.update((ref, row['id'], relation) for ref in row[field] if isinstance(ref, str))
    edges = candidate.get('edges')
    if isinstance(edges, list) and all(isinstance(row, dict) and all(isinstance(row.get(k), str)
            for k in ('from_id', 'to_id', 'relation')) for row in edges):
        actual = [(row['from_id'], row['to_id'], row['relation']) for row in edges]
        if len(actual) != len(set(actual)) or set(actual) != expected:
            add('/edges', edges, 'edge_reference_mismatch')
        for index, row in enumerate(edges):
            if row['from_id'] not in all_ids or row['to_id'] not in all_ids:
                add('/edges/'+str(index), row, 'dangling_edge')
    return {'defects': defects, 'content_review_possible': scope_valid,
            'checks': ['schema', 'isolation', 'ids', 'evidence', 'relations', 'item_binding', 'edges']}


def budget_report(pipe, data, plan, kind, prepare_one, diagnoses):
    """항목별 전송 입력 크기·토큰 범위·단가 구간(272K)을 모델 호출 전에 점검해 이벤트 'input-budget'으로 남긴다. 작업은 막지 않는다."""
    try:
        import asis_input_budget as budget
        family = 'diagnoses' if kind == 'diagnosis' else 'actions'
        rows = [budget.measure_item(pipe, prepare_one(kind, slot, diagnoses)) for slot in plan[family]]
        summary = budget.summarize(rows)
        summary['over_budget'] = [{k: r[k] for k in ('item_id', 'tokens_low', 'tokens_high', 'direct_fact_chars', 'over_by_low')} for r in summary['over_budget']][:20]
        summary['uncertain'] = [{k: r[k] for k in ('item_id', 'tokens_low', 'tokens_high', 'direct_fact_chars')} for r in summary['uncertain']][:20]
        summary['max_bytes'] = max((r['bytes'] for r in rows), default=0)
        pipe.event('input-budget', kind, 'completed', **summary)
    except Exception as exc:
        pipe.event('input-budget', kind, 'completed', error=(type(exc).__name__ + ':' + str(exc))[:160], items=0, default_tier=0, over_budget=[], uncertain=[])


def split_inputs(pipe, data):
    """과제 분할에 필요한 정본 관계: 방향 -> 영역 키, 방향 -> 그 방향이 거느린 이슈. 승인 결과·계획 체크포인트에서 복원하며 추측하지 않는다."""
    links = direction_of_issue(pipe, data)
    issues_of_direction = {}
    for issue_id, refs in links.items():
        for ref in refs:
            issues_of_direction.setdefault(ref, []).append(issue_id)
    area_of = {}
    for row in data['stage_data']['approved_directions']['directions']:
        refs = row.get('area_implication_refs') or ['?']
        area_of[row['id']] = refs[0]
    return area_of, issues_of_direction


SPLIT_FILE = ('actions-merge', 'action-split.json')


def saved_splits(pipe):
    path = pipe.folder.joinpath('checkpoints', *SPLIT_FILE)
    return json.loads(path.read_text(encoding='utf-8')).get('splits', []) if path.is_file() else []


def replay_splits(plan, records, area_of, issues_of_direction):
    """계획 전후 해시가 일치하는 분할만 재생한다. 이미 적용된 계획에는 다시 분할하지 않는다."""
    import asis_action_split as split
    if not records:return plan
    current = digest(plan)
    if current == records[-1].get('plan_after_sha256'):return deepcopy(plan)
    start = next((i for i,r in enumerate(records) if r.get('plan_before_sha256') == current), None)
    if start is None:raise ValueError('split_plan_binding_mismatch')
    for record in records[start:]:
        if record.get('plan_before_sha256') != digest(plan):raise ValueError('split_plan_binding_chain_mismatch')
        plan, actual = split.split_action_slot(plan, record['original_id'], direction_area=area_of, issues_of_direction=issues_of_direction)
        if actual['plan_after_sha256'] != record.get('plan_after_sha256') or actual['new_id'] != record.get('new_id'):
            raise ValueError('split_plan_binding_replay_mismatch')
    return plan


def split_if_needed(pipe, data, plan):
    """① 저장된 분할을 되살리고, ② 승인 전·길이 실패 3회·방향 2개 이상인 과제를 새로 나눈다(계획·승인 결과는 건드리지 않는다).

    영석님 지시(2026-10-18): 과제는 권장 20개, 최대 30개. 계획 체크포인트는 해시에 상한이 들어 있어 바꿀 수 없으므로 이 개정에서는 한도를 넘는 과제만 나누고
    새 슬롯을 마지막 번호 뒤에 붙인다. 원 슬롯은 같은 ID 로 앞 절반을 유지하고, 다른 슬롯의 ID·순서·내용·승인 체크포인트는 그대로다.
    나눈 슬롯은 입력이 바뀌었으므로 길이 실패 기록을 보존용으로 옮기고 0부터 다시 센다(원문부터 다시 시도한다).
    반환: (새 plan, 전체 분할 기록(되살린 것 + 새로 한 것), 새로 한 분할 수)."""
    import asis_action_split as split
    import asis_length_failures as length_failures
    records = saved_splits(pipe)
    if records and digest(plan) not in {r.get(k) for r in records for k in ('plan_before_sha256','plan_after_sha256')}:
        path = pipe.folder.joinpath('checkpoints', *SPLIT_FILE)
        journal = json.loads(path.read_text(encoding='utf-8'))
        archive_hash = digest(journal)
        pipe.save(path.parent/'superseded-splits'/(archive_hash+'.json'), journal)
        pipe.save(path, {'schema':'asis-action-split-set/2','base_plan_sha256':digest(plan),
                         'splits':[], 'superseded_journal_sha256':archive_hash})
        # 계획이 바뀐 경우 이전 길이 실패도 새 슬롯의 실패 횟수로 쓰지 않는다. 원 기록은 별도 보존한다.
        for slot in plan['actions']:
            old = pipe.folder/'checkpoints'/('action-'+slot['id'])/length_failures.FILE
            if old.is_file():
                failures = json.loads(old.read_text(encoding='utf-8'))
                pipe.save(old.parent/'before-plan-change'/(digest(failures)+'.json'), failures)
                pipe.save(old, {'schema':'asis-length-failures/1','count':0,'failures':[],
                                'superseded_plan_journal_sha256':archive_hash})
        pipe.event('actions','author','action_split_superseded',reason='source_plan_changed_or_unbound',
                   current_plan_sha256=digest(plan),superseded_journal_sha256=archive_hash)
        records = []
    area_of = issues_of_direction = None
    if records or any((pipe.folder/'checkpoints'/('action-'+slot['id'])/length_failures.FILE).is_file() for slot in plan['actions']):
        area_of, issues_of_direction = split_inputs(pipe, data)
    if records:
        plan = replay_splits(plan, records, area_of, issues_of_direction)
    fresh = 0
    while True:
        target = None
        for slot in plan['actions']:
            root = pipe.folder/'checkpoints'/('action-'+slot['id'])
            if (root/'accepted.json').exists() or not (root/length_failures.FILE).is_file():
                continue
            if json.loads((root/length_failures.FILE).read_text(encoding='utf-8'))['count'] >= 3 and len(slot['direction_refs']) >= 2:
                target = slot['id']; break
        if target is None:
            return plan, records, fresh
        plan, record = split.split_action_slot(plan, target, direction_area=area_of, issues_of_direction=issues_of_direction)
        records = records + [record]; fresh += 1
        root = pipe.folder/'checkpoints'/('action-'+target)
        old = root/length_failures.FILE
        n = len(list(root.glob('length-failures-before-split-*.json'))) + 1
        old.replace(root/('length-failures-before-split-%03d.json' % n))   # 원 기록은 지우지 않고 보존, 새 입력은 0부터 센다


async def reviewed_actions(pipe, original):
    """필요한 진단만 기다리는 항목 워커를 유한 병렬로 실행하고 전수 대조한 뒤 병합한다."""
    data = await plan_items(pipe, original)
    plan = data['context']['action_item_plan']
    # ① 저장된 분할을 되살리고 ② 길이로 3번 실패한 과제를 둘로 나눈다(권장 20개·최대 30개). 승인된 계획·체크포인트는 건드리지 않는다.
    plan, split_records, fresh_splits = split_if_needed(pipe, data, plan)
    if split_records:
        data['context']['action_item_plan'] = plan
        # 검증은 코드가 할당한 ID 목록(allocated_ids)과 출력 ID 를 대조한다(unallocated_output_id). 새 슬롯의 ID 도 같은 규칙으로 할당한다.
        data['allocated_ids']['actions'] = [row['id'] for row in plan['actions']]
        if fresh_splits:
            pipe.save(pipe.folder.joinpath('checkpoints', *SPLIT_FILE), {'schema': 'asis-action-split-set/2',
                      'base_plan_sha256':split_records[0]['plan_before_sha256'], 'splits': split_records})
            pipe.event('actions', 'author', 'action_split', original_ids=[r['original_id'] for r in split_records[-fresh_splits:]],
                       new_ids=[r['new_id'] for r in split_records[-fresh_splits:]], action_count=len(plan['actions']))
    from asis_tasks import task
    manager = pipe.task_manager(data)
    specs = []
    if 'actual_render' not in manager.ledger['tasks']:
        # 단독 항목 회귀도 렌더 미완료 선행 gate를 가진다. 없는 렌더를 완료로 만들지 않는다.
        specs.append(task('actual_render','PPT 전장 실제 렌더',scheduling='exclusive',resource='office',group='native'))
    for kind, family in (('diagnosis','diagnoses'),('action','actions')):
        for slot in plan[family]:
            unit = kind+'-'+slot['id']
            dependencies = ['action-item-plan']+(['diagnosis-'+ref for ref in slot['diagnosis_refs']] if kind=='action' else [])
            if unit not in manager.ledger['tasks']:
                specs.append(task(unit,slot['scope']+' 작성·검토',executor='model',depends_on=dependencies,pair_key=unit))
            native_unit = 'native-'+unit
            if native_unit not in manager.ledger['tasks']:
                specs.append(task(native_unit,slot['scope']+' 최종 장표 검증',depends_on=[unit,'actual_render'],group='visual',pair_key=unit))
    if specs:
        manager.register(specs)
    for kind, family in (('diagnosis','diagnoses'),('action','actions')):
        for slot in plan[family]:
            unit = kind+'-'+slot['id']
            pipe.pair_task(data,unit)
            manager.bind_pair('native-'+unit,
                author_session='asis-author-'+digest({'run':data['run'],'key':unit})[:24],
                reviewer_session='asis-review-'+digest({'run':data['run'],'key':unit})[:24],scope='native')
    previous = original['stage_data'].get('prior_output')
    repairs = {}
    if original['stage_data'].get('accumulated_defects'):
        if not isinstance(previous, dict) or previous.get('run') != data['run']:
            raise ValueError('action_item_repair_previous_candidate_required')
        for defect in original['stage_data']['accumulated_defects']:
            match = __import__('re').match(r'^/(diagnoses|actions)/([0-9]+)(/.*)?$', defect['where'])
            if not match:
                raise ValueError('action_item_repair_target_unbound')
            family, index, tail = match.group(1), int(match.group(2)), match.group(3) or ''
            try:
                identifier = previous[family][index]['id']
                literal = pipe.validator.pointer(previous, defect['where'])
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise ValueError('action_item_repair_target_unbound') from exc
            if defect['quote'] not in (literal if isinstance(literal, str) else json.dumps(literal, ensure_ascii=False)):
                raise ValueError('action_item_repair_quote_unbound')
            if identifier not in {slot['id'] for slot in plan[family]}:
                raise ValueError('action_item_repair_id_not_in_plan')
            repairs.setdefault(identifier, []).append({**deepcopy(defect),
                'source_where': defect.get('source_where', defect['where']), 'where': '/'+family+'/0'+tail})
    tasks = {}

    for kind, family in (('diagnosis', 'diagnoses'), ('action', 'actions')):
        for slot in plan[family]:
            pipe.event(kind+'-'+slot['id'], 'author', 'queued', item_kind=kind,
                       item_id=slot['id'], item_title=slot['scope'])
    pipe.event('actions', 'author', 'running', diagnosis_count=len(plan['diagnoses']), action_count=len(plan['actions']))

    def prepare_one(kind, slot, diagnoses):
            scoped = item_input(data, kind, slot, diagnoses)
            if slot['id'] in repairs:
                family = 'diagnoses' if kind == 'diagnosis' else 'actions'
                row = next(row for row in previous[family] if row['id'] == slot['id'])
                prior = {'stage': 'actions', 'run': deepcopy(data['run']), 'diagnoses': [],
                         'actions': [], 'edges': [], 'unresolved': []}
                prior[family] = [deepcopy(row)]
                relation, field = ('diagnoses', 'direction_refs') if kind == 'diagnosis' else ('addresses', 'diagnosis_refs')
                prior['edges'] = [{'from_id': ref, 'to_id': row['id'], 'relation': relation} for ref in row[field]]
                scoped['stage_data'].update(prior_output=prior, accumulated_defects=deepcopy(repairs[slot['id']]))
            return scoped

    # 모델 호출 전 입력 예산 점검: 진단 항목의 실제 전송 크기·단가 구간을 기록한다. 관측일 뿐이라 실패하거나 초과해도 작업을 막지 않는다.
    # 과제 항목은 진단 결과가 있어야 입력이 정해지므로 진단 완료 뒤에 점검한다.
    budget_report(pipe, data, plan, 'diagnosis', prepare_one, {})

    async def run_one(kind, slot):
        unit = kind+'-'+slot['id']
        try:
            diagnoses = {}
            if kind == 'action':
                for ref in slot['diagnosis_refs']:
                    output = await tasks[ref]
                    diagnoses[ref] = output['diagnoses'][0]
            async with pipe.item_gate:
                # 슬롯 승인 전에는 큰 원천 입력을 복제하거나 SDK 요청을 준비하지 않는다.
                scoped = prepare_one(kind, slot, diagnoses)
                output = await pipe.reviewed_stage(scoped, 3, unit,
                    candidate_check=lambda candidate: validate_item(pipe, scoped, candidate))
            return output
        except Exception as exc:
            pipe.event(unit, 'content_review', 'failed', item_kind=kind, item_id=slot['id'],
                       item_title=slot['scope'], error_type=type(exc).__name__)
            raise
    # 보존 후보의 대용량 동기 재검증과 새 SDK 준비를 같은 루프에서 겹치지 않는다.
    # 이미 완료된 선행 항목만 순차 검증한다. 새 항목의 의존성·자원 기반 병렬은 유지한다.
    preserved = getattr(getattr(pipe, 'resume', None), 'preserved', set())
    for kind, family in (('diagnosis', 'diagnoses'), ('action', 'actions')):
        for slot in plan[family]:
            unit = kind+'-'+slot['id']
            dependency_ready = kind == 'diagnosis' or all(
                ref in tasks and tasks[ref].done() and tasks[ref].exception() is None
                for ref in slot['diagnosis_refs'])
            if unit in preserved and dependency_ready:
                tasks[slot['id']] = asyncio.create_task(run_one(kind, slot))
                await asyncio.gather(tasks[slot['id']], return_exceptions=True)
    for slot in plan['diagnoses']:
        if slot['id'] not in tasks:
            tasks[slot['id']] = asyncio.create_task(run_one('diagnosis', slot))
    for slot in plan['actions']:
        if slot['id'] not in tasks:
            tasks[slot['id']] = asyncio.create_task(run_one('action', slot))
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    failures = [identifier for identifier, result in zip(tasks, results) if isinstance(result, BaseException)]
    if failures:
        first_exc = next(result for result in results if isinstance(result, BaseException))
        pipe.save(pipe.folder/'checkpoints'/'actions-merge'/'failed-items.json',
                  {'plan_sha256': digest(plan), 'failed_item_ids': failures,
                   'successful_item_ids': [identifier for identifier in tasks if identifier not in failures]})
        pipe.event('actions', 'content_review', 'failed', failed_item_ids=failures)
        raise RuntimeError('action_item_generation_failed:' + ','.join(failures)) from first_exc
    outputs = dict(zip(tasks, results))
    diagnoses = {slot['id']: outputs[slot['id']]['diagnoses'][0] for slot in plan['diagnoses']}
    merged = {'stage': 'actions', 'run': deepcopy(data['run']), 'diagnoses': [], 'actions': [], 'edges': [], 'unresolved': []}
    for kind, family in (('diagnosis', 'diagnoses'), ('action', 'actions')):
        for slot in plan[family]:
            output = outputs[slot['id']]
            # 완료된 단위의 전체 입력 복사본을 상주시켜 병렬 한도를 무력화하지 않는다.
            scoped = prepare_one(kind, slot, diagnoses)
            pipe.validator.validate_output(scoped, output)
            validate_item(pipe, scoped, output)
            del scoped
            merged[family].extend(deepcopy(output[family]))
            for row in output['unresolved']:
                if row not in merged['unresolved']: merged['unresolved'].append(deepcopy(row))
    for row in plan['coverage']:
        if row['disposition'] == 'deferred':
            issue = next(issue for issue in data['context']['analysis_plan']['issues'] if issue['id'] == row['issue_id'])
            merged['unresolved'].append({'topic': issue['scope'], 'reason': row['reason'],
                                        'required_evidence': row['required_evidence'], 'fact_refs': deepcopy(issue['fact_refs'])})
    merged['edges'] = [{'from_id': ref, 'to_id': row['id'], 'relation': relation}
        for family, key, relation in (('diagnoses', 'direction_refs', 'diagnoses'), ('actions', 'diagnosis_refs', 'addresses'))
        for row in merged[family] for ref in row[key]]
    pipe.validator.validate_output(data, merged)
    pipe.save(pipe.folder/'checkpoints'/'actions-merge'/digest(plan)/'accepted.json',
        {'schema': 'asis-action-item-merge/1', 'plan_sha256': digest(plan), 'candidate_sha256': digest(merged),
         'candidate': merged, 'review_mode': 'per_item_content_review_and_deterministic_merge',
         'reviewed_item_ids': list(tasks), 'whole_document_review_required': True,
         'execution_classification': pipe.classification(data)})
    pipe.event('actions', 'consistency', 'completed', diagnosis_count=len(merged['diagnoses']), action_count=len(merged['actions']))
    pipe.event('actions', 'content_review', 'completed', diagnosis_count=len(merged['diagnoses']), action_count=len(merged['actions']),
               review_mode='per_item_content_review_and_deterministic_merge')
    return data, merged


# ── 장표 시각 명세 단계: 문서 검토를 통과한 결과의 통합 진단 주제마다 장표 형식과 핵심 글을 정한다 ────────────────────────────────
# 영역별 호출 1회(주제 최대 20개). 입력은 진단 요약과 인용 사실의 ID·종류·개수뿐이며 사실 본문은 보내지 않는다.
# 계약 위반(형식·글자 수·인용 범위·누락)은 직전 오류만 알려 다시 묻고, 그래도 안 되면 실패한다(글 장표로 몰래 대체하지 않는다).

async def visual_specs(pipe, result):
    """통합 진단 주제 전체의 시각 명세를 영역 단위로 만든다. 반환: {topic_id: 검증된 시각 명세}. 완료된 영역은 체크포인트로 재사용한다."""
    import asis_consolidated_deck as deck
    import asis_visual_forms as forms
    data = result['input']
    by_area = deck.topics_by_area(result)
    diagnoses = {row['id']: row for row in result['stages'][2]['diagnoses']}
    facts = {row['id']: row for row in data['facts']}
    system = pipe.instructions((ASSET/'contract/prompts/P0-system.txt').read_text(encoding='utf-8'))
    root = pipe.folder/'checkpoints'/'visual-specs'
    preserved_by_topic = {}
    for accepted_path in sorted(root.glob('*/accepted.json')):
        try:
            rec = json.loads(accepted_path.read_text(encoding='utf-8'))
            vis = rec.get('visuals')
            if isinstance(vis, dict) and rec.get('visuals_sha256') == digest(vis):
                for tid, v in vis.items():
                    if tid in diagnoses:
                        preserved_by_topic[tid] = forms.validate_visual(v, set(diagnoses[tid]['fact_refs']))
        except (ValueError, KeyError, TypeError, OSError):
            continue

    async def one(area_id, topics):
        view = forms.visual_view(area_id, topics, diagnoses, facts); view['run'] = deepcopy(data['run'])
        binding = digest({'view': view, 'instruction': forms.VISUAL_INSTRUCTION, 'system': system, 'policy': policy(), 'schema': 'visual-specs/1'})
        done = root/area_id/'accepted.json'
        topic_ids = [t['id'] for t in topics]
        cited = {t['id']: set(diagnoses[t['id']]['fact_refs']) for t in topics}
        if done.exists():
            record = json.loads(done.read_text(encoding='utf-8'))
            visuals = record.get('visuals')
            if record.get('visuals_sha256') == digest(visuals):
                if record.get('input_sha256') == binding:
                    return {tid: forms.reconcile_visual(visuals[tid], diagnoses[tid], facts) for tid in topic_ids}
                if record.get('view_sha256') in (None, digest(view)) and isinstance(visuals, dict):
                    try:
                        validated = forms.validate_visuals(topic_ids, cited, {'visuals': visuals})
                        return {tid: forms.reconcile_visual(validated[tid], diagnoses[tid], facts) for tid in topic_ids}
                    except (ValueError, KeyError, TypeError):
                        pass
        if all(tid in preserved_by_topic for tid in topic_ids):
            visuals = {tid: forms.reconcile_visual(preserved_by_topic[tid], diagnoses[tid], facts) for tid in topic_ids}
            pipe.save(done, {'input_sha256': binding, 'view_sha256': digest(view), 'visuals_sha256': digest(visuals), 'visuals': visuals})
            return visuals
        async with pipe.item_gate:
            visuals = await _consolidation_call(pipe, data, 'visuals', view, forms.VISUAL_INSTRUCTION,
                lambda response: {tid: forms.reconcile_visual(v, diagnoses[tid], facts)
                                  for tid, v in forms.validate_visuals(topic_ids, cited, response).items()}, 'visual-specs-' + area_id, root, system)
        pipe.save(done, {'input_sha256': binding, 'view_sha256': digest(view), 'visuals_sha256': digest(visuals), 'visuals': visuals})
        return visuals
    parts = await asyncio.gather(*(one(a, t) for a, t in sorted(by_area.items())))
    merged = {}
    for part in parts:
        merged.update(part)
    if (data.get('context') or {}).get('report_kind') == 'integrated':
        result['maturity_spider'] = await spider_opinions(pipe, result, root, system)
        result['goal_policy_analysis'] = await goal_policy_opinions(pipe, result, root, system)
    return merged


async def goal_policy_opinions(pipe, result, root=None, system=None):
    """인터뷰 목표·페인포인트·정책 현황을 분석하여 고정된 『목표 및 정책 분석』 템플릿 장표의 각 칸 내용을 생성·검증한다."""
    import asis_goal_policy as gp
    data = result['input']
    if root is None:
        root = pipe.folder/'checkpoints'/'visual-specs'
    if system is None:
        system = pipe.instructions((ASSET/'contract/prompts/P0-system.txt').read_text(encoding='utf-8'))
    view = gp.extract_goal_policy_evidence(data); view['run'] = deepcopy(data['run'])
    binding = digest({'view': view, 'instruction': gp.GOAL_POLICY_INSTRUCTION, 'system': system, 'policy': policy(), 'schema': 'goal-policy-analysis/1'})
    done = root/'goal-policy-analysis'/'accepted.json'
    if done.exists():
        try:
            record = json.loads(done.read_text(encoding='utf-8'))
            spec = record.get('goal_policy_analysis')
            if isinstance(spec, dict) and record.get('goal_policy_sha256') == digest(spec):
                if record.get('input_sha256') == binding or record.get('view_sha256') in (None, digest(view)):
                    return gp.validate_goal_policy_spec(spec)
        except (ValueError, KeyError, TypeError, OSError):
            pass
    async with pipe.item_gate:
        spec = await _consolidation_call(
            pipe, data, 'goal_policy_analysis', view, gp.GOAL_POLICY_INSTRUCTION,
            lambda response: gp.validate_goal_policy_spec(response),
            'goal-policy-analysis', root, system,
        )
    pipe.save(done, {'input_sha256': binding, 'view_sha256': digest(view), 'goal_policy_sha256': digest(spec), 'goal_policy_analysis': spec})
    return spec


async def spider_opinions(pipe, result, root=None, system=None):
    """인터뷰 평가 점수(scoring.compute)와 9대 영역 분석 결과를 바탕으로 성숙도 스파이더 차트 장표의 AI 평가 의견을 생성·검증한다."""
    import asis_maturity_spider as spider
    data = result['input']
    if root is None:
        root = pipe.folder/'checkpoints'/'visual-specs'
    if system is None:
        system = pipe.instructions((ASSET/'contract/prompts/P0-system.txt').read_text(encoding='utf-8'))
    view = spider.spider_opinion_view(result); view['run'] = deepcopy(data['run'])
    area_ids = [r['area_id'] for r in view['areas']]
    binding = digest({'view': view, 'instruction': spider.SPIDER_OPINION_INSTRUCTION, 'system': system, 'policy': policy(), 'schema': 'maturity-spider/1'})
    done = root/'maturity-spider'/'accepted.json'
    if done.exists():
        try:
            record = json.loads(done.read_text(encoding='utf-8'))
            spec = record.get('maturity_spider')
            if isinstance(spec, dict) and record.get('maturity_spider_sha256') == digest(spec):
                if record.get('input_sha256') == binding or record.get('view_sha256') in (None, digest(view)):
                    return spider.validate_spider_opinions(area_ids, spec)
        except (ValueError, KeyError, TypeError, OSError):
            pass
    async with pipe.item_gate:
        spec = await _consolidation_call(
            pipe, data, 'maturity_spider', view, spider.SPIDER_OPINION_INSTRUCTION,
            lambda response: spider.validate_spider_opinions(area_ids, response),
            'maturity-spider', root, system,
        )
    pipe.save(done, {'input_sha256': binding, 'view_sha256': digest(view), 'maturity_spider_sha256': digest(spec), 'maturity_spider': spec})
    return spec
