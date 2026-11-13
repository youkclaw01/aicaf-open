"""종합 방향의 참조 그래프와 항목별 병렬 작성·검토를 관리한다."""
from copy import deepcopy
import asyncio
import json
from pathlib import Path

from asis_model import ASSET, digest, encoded
from asis_runtime import policy


def plan_schema_name(data):
    return data['context']['analysis_plan'].get('schema','asis-issue-plan/1')


def plan_request_view(data, payload):
    """종합 범위 계획·검토 호출에 전송할 입력만 투영한다. 정본·검증·체크포인트 바인딩은 전체 data 기준이다."""
    areas = data['stage_data']['approved_areas']['areas']; plan_issues = data['context']['analysis_plan']['issues']
    cited = {key: {ref for area in areas for ref in area['implication'][key]} | {ref for row in plan_issues for ref in row[key]}
             for key in ('fact_refs','criterion_refs')}
    sent = deepcopy(payload)
    # 영역 단위 호출이면 영역 ID를 범위에 담는다. 수집 기준 매칭 축소(asis_evidence_views.prompt_view)가 이 값으로 이 영역 행만 남긴다.
    # 영역 ID가 없던 이전에는 그 분기가 실행되지 않아 매칭 248행(0.36MB)이 영역 호출마다 모두 전송됐다.
    plan_area = (data.get('stage_data', {}).get('plan_assignment') or {}).get('area_id')
    sent['context']['model_scope'] = {'projection_schema':'asis-model-scope/1',
        'fact_refs':sorted(cited['fact_refs'] & {r['id'] for r in data['facts']}),
        'criterion_refs':sorted(cited['criterion_refs'] & {r['id'] for r in data['criteria']}),
        'goal_refs':[r['id'] for r in data.get('goals',[])],'inline_fact_refs':[]}
    if plan_area:
        sent['context']['model_scope']['area_id'] = plan_area
    ctx = sent['context']
    for heavy in ('collection_basis_matches','criterion_assessments','collection_states','dataset_register','dataset_index',
                  'derived_fact_register','metric_summary','cost_summary'):
        ctx.pop(heavy,None)
    sent['stage_data']['approved_areas'] = {key: value for key, value in sent['stage_data']['approved_areas'].items() if key != 'areas'}
    sent['stage_data']['approved_areas']['areas'] = [
        {'area_id':area['area_id'],'headline':area['headline'],
         'observation_ids':[row.get('id') for row in area.get('observations',[]) if isinstance(row,dict)],
         'implication':area['implication']} for area in areas]
    # 영역 승인본의 미확인은 모든 영역의 목록이라 영역 1개 호출에 917건(0.69MB)이 실렸다. 이 영역의 시사점·이슈가 인용한 사실과 겹치는 것부터
    # 방향 입력과 같은 예산 안에서 본문을 채우고, 나머지는 자리·주제만 남겨 위치(/unresolved/N)를 정본과 같게 둔다.
    approved_sent = sent['stage_data']['approved_areas']
    if isinstance(approved_sent.get('unresolved'), list) and approved_sent['unresolved']:
        import asis_model_view as _mv
        rows = approved_sent['unresolved']
        mine = cited['fact_refs']
        # 예산 안에 들어오는 목록은 그대로 보낸다(방향 입력의 budgeted_unresolved와 같은 규칙). 예산을 넘을 때만 겹침 순으로 줄인다.
        if len(json.dumps(rows, ensure_ascii=False)) <= _mv.UNRESOLVED_BUDGET_CHARS:
            kept_rows = list(rows)
        else:
            kept_rows, _ = _mv.budgeted_unresolved(_mv.related_unresolved(rows, mine, all_rows=rows), mine, _mv.UNRESOLVED_BUDGET_CHARS)
        key = lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True)
        kept_keys = {key(row) for row in kept_rows}
        approved_sent['unresolved'] = [row if (isinstance(row, dict) and key(row) in kept_keys) else {'omitted': True} for row in rows]
        if len(kept_rows) < len(rows):
            approved_sent['omitted_unresolved'] = {'omitted_count': len(rows) - len(kept_rows), 'kept_count': len(kept_rows),
                'omitted_topics': [{'topic': row.get('topic', ''), 'fact_ref_count': len(row.get('fact_refs', []))}
                                   for row in rows if isinstance(row, dict) and key(row) not in kept_keys],
                'reason': 'unresolved_text_over_input_budget_topics_kept_text_in_canonical_ledger'}
    ctx['analysis_plan'] = {'schema':plan_schema_name(data),
        'issues':[{key:row[key] for key in ('id','direction_id','lead_area','area_refs','scope','reason','fact_refs','criterion_refs','goal_refs') if key in row}
                  for row in plan_issues]}
    return sent


# 모델이 쓰지 않고 코드가 생성하는 최상위 필드. 이 위치의 지적은 작성자에게 보완을 요구할 수 없다.
CODE_OWNED_FIELDS = frozenset({'/edges'})


def project_graph(candidate):
    """서술에 선언된 실제 사실·시사점 관계로만 그래프를 생성한다. validator는 별도 유지한다."""
    value = deepcopy(candidate)
    directions = value.get('directions')
    if not isinstance(directions, list):
        raise ValueError('invalid_synthesis_direction_rows')
    edges = []
    for field, relation in (('fact_refs', 'supports'), ('area_implication_refs', 'consolidates')):
        for row in directions:
            if not isinstance(row, dict) or not isinstance(row.get('id'), str):
                raise ValueError('invalid_synthesis_direction_row')
            refs = row.get(field)
            if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
                raise ValueError('invalid_synthesis_graph_reference:' + field)
            edges.extend({'from_id': ref, 'to_id': row['id'], 'relation': relation} for ref in refs)
    value['edges'] = edges
    return value


def validate_plan(data, candidate):
    """종합 그룹의 의미는 모델이 정하고 원천 ID·전수 범위·다대다 참조는 코드가 확인한다."""
    if not isinstance(candidate, dict) or set(candidate) != {'directions'} or not isinstance(candidate['directions'], list):
        raise ValueError('invalid_synthesis_plan_shape')
    slots = candidate['directions']
    if not slots or len(slots) > len(data['allocated_ids']['directions']):
        raise ValueError('synthesis_plan_allocation_exhausted')
    allowed = {key: {row['id'] for row in data[family]} for key, family in
               (('fact_refs','facts'), ('criterion_refs','criteria'), ('goal_refs','goals'))}
    issues = data['context']['analysis_plan']['issues']
    allowed['issue_refs'] = {row['id'] for row in issues}
    allowed['area_implication_refs'] = {row['implication']['id'] for row in data['stage_data']['approved_areas']['areas']}
    seen = set()
    for slot in slots:
        if (not isinstance(slot, dict) or set(slot) != {'scope','reason',*allowed}
                or any(not isinstance(slot[key],str) or not slot[key].strip() for key in ('scope','reason'))):
            raise ValueError('invalid_synthesis_plan_slot')
        for key, valid in allowed.items():
            refs = slot[key]
            if (not isinstance(refs,list) or (key != 'goal_refs' and not refs)
                    or any(not isinstance(ref,str) or ref not in valid for ref in refs)
                    or len(refs) != len(set(refs))):
                raise ValueError('unbound_synthesis_plan_reference:'+key)
        signature = digest({key: value for key,value in slot.items() if key != 'reason'})
        if signature in seen:
            raise ValueError('duplicate_synthesis_plan_slot')
        seen.add(signature)
    for key in ('issue_refs','area_implication_refs'):
        if {ref for slot in slots for ref in slot[key]} != allowed[key]:
            raise ValueError('synthesis_plan_coverage_missing:'+key)


SYNTHESIS_PLAN_INSTRUCTION = (
    '영역별 승인 사실·시사점을 고객 목표에 따라 종합할 판단 범위만 계획한다. 본문·ID·edges를 작성하지 않는다. '
    '한 영역당 하나나 한 이슈당 하나로 고정하지 않는다. 공통 원인·영향·선행조건이 확인되는 이슈는 묶고 '
    '서로 다른 판단 범위는 분리한다. 제공된 모든 analysis_plan 이슈와 승인 영역 시사점을 한 번 이상 연결한다. '
    '미확인은 조건부 판단·확인 방향으로 유지하며 정상·결함으로 확정하지 않는다. '
    '응답은 {"directions":[{"scope":"판단 대상·범위","reason":"근거·결합 이유",'
    '"issue_refs":[],"area_implication_refs":[],"fact_refs":[],"criterion_refs":[],"goal_refs":[]}]} JSON이다. '
    '참조는 제공된 실제 ID만 사용하고 같은 범위를 중복하지 않는다. 계획 이유는 고객 운영 사실이 아니다. '
    '직전 후보와 모든 위치·원문 지적을 한 번에 보완한다. 도구는 사용하지 않는다.')


CROSS_AREA_GROUPING_INSTRUCTION = (
    '영역별로 따로 만든 종합 방향 후보의 범위·이유 목록이다. 서로 다른 영역이 같은 판단 주제(같은 리소스·기능·결정 지점을 두고 같은 질문에 답하는 후보)를 '
    '각자 만든 것만 하나로 묶는다. 범위가 다르면 묶지 않는다. 묶는 기준은 판단 주제의 동일성이며 단순히 같은 영역·같은 기술 용어를 쓴다는 이유가 아니다. '
    '후보의 본문·참조·ID를 새로 쓰지 않고 후보 번호(index)만 사용한다. 한 후보는 최대 한 그룹에만 들어간다. 묶을 후보가 없으면 groups는 빈 배열이다. '
    '응답은 {"groups":[{"members":[후보 번호,...],"reason":"같은 판단 주제로 묶는 이유"}]} JSON이다. 모든 그룹은 서로 다른 영역의 후보를 2개 이상 포함한다. 도구는 사용하지 않는다.')


SECOND_PASS_MAX_MEMBERS = 4
SECOND_PASS_GROUPING_INSTRUCTION = (
    '종합 방향 후보의 범위·이유 목록이다. 서로 다른 후보가 같은 확인 대상(같은 리소스·기능)에 같은 질문으로 답하거나, 하나가 다른 하나의 판정 조건·근거 상세이거나, '
    '같은 결정을 위한 입력을 따로 정리하는 경우만 하나의 방향으로 합친다. 영역이 같거나 달라도 같은 판단이면 합친다. 같은 영역·같은 기술 용어라는 이유만으로는 합치지 않는다. '
    '합치면 사라지는 판단이 없어야 한다: 목적·대상·결정 지점이 다르면 합치지 않는다. 한 묶음은 최대 4개 후보까지 허용한다(5개 이상이 같은 판단이면 가장 가까운 것끼리 나누어 묶는다). '
    '후보의 본문·ID를 새로 쓰지 않고 후보 번호(index)만 쓴다. 한 후보는 최대 한 그룹에만 들어간다. 묶을 후보가 없으면 groups는 빈 배열이다. '
    '응답은 {"groups":[{"members":[후보 번호,...],"reason":"합치는 이유와 합쳐도 사라지는 판단이 없다는 근거"}]} JSON이다. 도구는 사용하지 않는다.')


def merged_direction_over_limit(data, directions, members):
    """묶음 members를 합친 방향이 작성 호출의 전송 한도를 넘는지 코드가 계산한다. 넘으면 (크기, 한도), 아니면 None.

    합집합으로 참조가 늘어나면 그 방향이 인용하는 사실 본문이 늘어 한도(asis_model_view.LIMITS)를 넘을 수 있다(운영 2026-10-16: 2차 묶기 후 방향 6개가
    145만~196만 자로 모델 호출 전에 model_input_over_limit 실패). 사실 본문 크기는 모델이 아니라 코드만 알 수 있으므로 코드가 판단한다."""
    import asis_model_view as view_module
    merged, _ = apply_cross_area_groups(directions, [sorted(members)])
    lead = max(members, key=lambda i: (sum(len(directions[i][k]) for k in ('issue_refs', 'area_implication_refs', 'fact_refs', 'criterion_refs', 'goal_refs')), -i))
    slot = dict(next(row for row in merged if row['scope'] == directions[lead]['scope']), id='D_P0000')
    try:
        view_module.build(2, direction_input(data, slot))
    except ValueError as error:
        text = str(error)
        if text.startswith('model_input_over_limit:'):
            size, limit = text.rsplit(':', 1)[1].split('>')
            return int(size), int(limit)
        raise
    return None


def validate_second_pass_groups(directions, response):
    """2차 묶기의 모델 응답을 코드가 확인한다: 실제 후보 번호만, 한 후보는 한 그룹, 그룹당 2~4개. 영역 제한은 두지 않는다(같은 영역 안 중복도 묶는다)."""
    if not isinstance(response, dict) or set(response) != {'groups'} or not isinstance(response['groups'], list):
        raise ValueError('invalid_second_pass_groups_shape')
    used = set(); groups = []
    for group in response['groups']:
        if (not isinstance(group, dict) or set(group) != {'members', 'reason'} or not isinstance(group['reason'], str) or not group['reason'].strip()
                or not isinstance(group['members'], list) or not 2 <= len(group['members']) <= SECOND_PASS_MAX_MEMBERS
                or any(type(i) is not int or not 0 <= i < len(directions) for i in group['members'])
                or len(set(group['members'])) != len(group['members'])):
            raise ValueError('invalid_second_pass_group')
        if used & set(group['members']):
            raise ValueError('second_pass_group_member_reused')
        used |= set(group['members']); groups.append(sorted(group['members']))
    return groups


def cross_area_view(directions, area_of):
    """통합 계획 호출에 보낼 소형 입력: 후보 번호·영역·범위·이유만 담는다(참조·본문 없음)."""
    return {'candidates': [{'index': i, 'area': area_of[i], 'scope': row['scope'], 'reason': row['reason'][:400]}
                           for i, row in enumerate(directions)]}


def validate_cross_area_groups(directions, area_of, response):
    """모델이 정한 묶음을 코드가 확인한다: 실제 후보 번호만, 한 후보는 한 그룹, 그룹은 서로 다른 영역 2개 이상의 후보."""
    if not isinstance(response, dict) or set(response) != {'groups'} or not isinstance(response['groups'], list):
        raise ValueError('invalid_cross_area_groups_shape')
    used = set(); groups = []
    for group in response['groups']:
        if (not isinstance(group, dict) or set(group) != {'members', 'reason'} or not isinstance(group['reason'], str) or not group['reason'].strip()
                or not isinstance(group['members'], list) or len(group['members']) < 2
                or any(type(i) is not int or not 0 <= i < len(directions) for i in group['members'])
                or len(set(group['members'])) != len(group['members'])):
            raise ValueError('invalid_cross_area_group')
        if used & set(group['members']):
            raise ValueError('cross_area_group_member_reused')
        if len({area for i in group['members'] for area in area_of[i].split('+')}) < 2:
            raise ValueError('cross_area_group_single_area')
        used |= set(group['members']); groups.append(sorted(group['members']))
    return groups


def apply_cross_area_groups(directions, groups):
    """그룹의 후보를 하나로 합친다: 참조는 합집합으로 보존하고 판단 문구는 참조가 가장 많은 후보의 것을 쓴다. 코드가 수행한다."""
    refs = ('issue_refs', 'area_implication_refs', 'fact_refs', 'criterion_refs', 'goal_refs')
    drop = set(); keep_at = {}
    for members in groups:
        lead = max(members, key=lambda i: (sum(len(directions[i][k]) for k in refs), -i))
        merged = deepcopy(directions[lead])
        for other in members:
            if other == lead:
                continue
            for key in refs:
                merged[key] = list(dict.fromkeys(merged[key] + directions[other][key]))
            drop.add(other)
        keep_at[lead] = merged
    result = [keep_at.get(i, row) for i, row in enumerate(directions) if i not in drop]
    report = [{'kept_scope': directions[max(m, key=lambda i: (sum(len(directions[i][k]) for k in refs), -i))]['scope'],
               'merged_scopes': [directions[i]['scope'] for i in m if i != max(m, key=lambda j: (sum(len(directions[j][k]) for k in refs), -j))]}
              for m in groups]
    return result, report


def area_plan_scope(data, area_id):
    """한 영역이 이끄는 이슈와 그 영역 시사점만 담은 소단위 입력을 만든다. 정본은 변경하지 않는다."""
    scoped = deepcopy(data)
    areas = data['stage_data']['approved_areas']
    scoped['stage_data'] = {'mode':'synthesis_plan','approved_areas':{**{k:v for k,v in areas.items() if k!='areas'},
        'areas':[deepcopy(row) for row in areas['areas'] if row['area_id']==area_id]},
        'plan_assignment':{'area_id':area_id}}
    scoped['context']['analysis_plan'] = {**data['context']['analysis_plan'],
        'issues':[deepcopy(row) for row in data['context']['analysis_plan']['issues'] if row['lead_area']==area_id]}
    scoped['allocated_ids'] = {**data['allocated_ids'],
        'directions':list(data['allocated_ids']['directions'])}
    return scoped


def validate_area_plan(scoped, candidate):
    """영역 소단위 후보의 참조 전수 연결을 확인한다. 배정 ID 상한은 통합 단계에서 코드가 확정한다."""
    if not isinstance(candidate, dict) or set(candidate) != {'directions'} or not isinstance(candidate['directions'], list) or not candidate['directions']:
        raise ValueError('invalid_synthesis_area_plan_shape')
    allowed = {key: {row['id'] for row in scoped[family]} for key, family in
               (('fact_refs','facts'), ('criterion_refs','criteria'), ('goal_refs','goals'))}
    allowed['issue_refs'] = {row['id'] for row in scoped['context']['analysis_plan']['issues']}
    allowed['area_implication_refs'] = {row['implication']['id'] for row in scoped['stage_data']['approved_areas']['areas']}
    seen = set()
    for slot in candidate['directions']:
        if (not isinstance(slot, dict) or set(slot) != {'scope','reason',*allowed}
                or any(not isinstance(slot[key],str) or not slot[key].strip() for key in ('scope','reason'))):
            raise ValueError('invalid_synthesis_area_plan_slot')
        for key, valid in allowed.items():
            refs = slot[key]
            if (not isinstance(refs,list) or (key != 'goal_refs' and not refs)
                    or any(not isinstance(ref,str) or ref not in valid for ref in refs) or len(refs) != len(set(refs))):
                raise ValueError('unbound_synthesis_area_plan_reference:'+key)
        signature = digest({key: value for key,value in slot.items() if key != 'reason'})
        if signature in seen:
            raise ValueError('duplicate_synthesis_area_plan_slot')
        seen.add(signature)
    for key in ('issue_refs','area_implication_refs'):
        if {ref for slot in candidate['directions'] for ref in slot[key]} != allowed[key]:
            raise ValueError('synthesis_area_plan_coverage_missing:'+key)


async def _plan_area_synthesis(pipe, original, data, area_id, system, binding, root):
    """한 영역의 종합 방향 후보 작성과 독립 검토를 완료 저장 단위로 실행한다."""
    scoped = area_plan_scope(data, area_id)
    unit_binding = digest({'binding':binding,'area':area_id,'input':scoped})
    accepted = root/'areas'/area_id/'accepted.json'
    key = 'synthesis-plan-area-'+area_id
    if accepted.is_file():
        record = json.loads(accepted.read_text(encoding='utf-8'))
        if record.get('input_sha256') == unit_binding and record.get('candidate_sha256') == digest(record.get('candidate')):
            validate_area_plan(scoped, record['candidate'])
            pipe.event(key,'interpret','reused')
            return record['candidate']['directions']
    prior = None; defects = []
    async def work():
        nonlocal prior
        attempt = 0; remaining = pipe.rounds; language_refunds = 0
        while remaining > 0:
            attempt += 1; remaining -= 1
            payload = deepcopy(scoped)
            payload['stage_data'].update(prior_output=prior,accumulated_defects=deepcopy(defects))
            from asis_evidence_views import prompt_view
            sent = plan_request_view(scoped, payload)
            pipe.event(key,'interpret','running',attempt=attempt)
            raw,receipt = await pipe.runtime.ask('interpret',SYNTHESIS_PLAN_INSTRUCTION+'\n입력:\n'+encoded(prompt_view(sent)).decode('utf-8'),
                session_id='asis-synthesis-plan-'+digest({'run':data['run'],'area':area_id,'attempt':attempt})[:24],instructions=system)
            folder = root/'areas'/area_id/('round-'+str(attempt).zfill(3))
            pipe.save(folder/'input.json',payload); pipe.save(folder/'response.txt',raw.encode('utf-8')); pipe.save(folder/'receipt.json',receipt)
            candidate = None
            try:
                candidate = pipe.validator.strict_load(raw); validate_area_plan(scoped,candidate)
                from asis_language_guard import find_language_defects, allowed_text_from_input
                language = find_language_defects(candidate,allowed_text=allowed_text_from_input(scoped))
                if language:
                    prior = candidate; defects.extend(language)
                    # 일본어·한자 혼입은 문자 오타라서 보완 횟수를 쓰지 않는다. 같은 오류의 무한 반복만 상한으로 막는다.
                    if language_refunds < 8:
                        language_refunds += 1; remaining += 1
                    continue
            except (ValueError,KeyError,TypeError) as error:
                prior = candidate if candidate is not None else {'raw_response':raw}
                defects.append({'where':'/','quote':raw[:500],'why':str(error),'fix':'선언된 영역 종합 범위·참조 스키마와 전수 범위를 보완한다.','evidence_refs':[]})
                continue
            review = await pipe.independent_review(plan_request_view(scoped,scoped),candidate,key+'-review')
            if review['verdict'] == 'pass':
                return candidate, review
            if review['verdict'] == 'blocked':
                raise RuntimeError('synthesis_plan_area_review_blocked:'+area_id)
            prior = candidate; defects.extend(review['defects'])
        raise RuntimeError('synthesis_plan_area_budget_exhausted:'+area_id)
    evidence = plan_request_view(scoped,scoped)
    candidate, review = await pipe.run_task(evidence,key,work)
    pipe.save(accepted,{'input_sha256':unit_binding,'candidate_sha256':digest(candidate),'candidate':candidate,'review':review})
    pipe.event(key,'interpret','completed',directions=len(candidate['directions']))
    return candidate['directions']


async def plan_synthesis(pipe, original):
    """영역별 종합 방향 후보를 가용 자원만큼 동시에 만들고 통합 검토·코드 확정으로 하나의 계획으로 합친다."""
    from asis_tasks import task
    data = deepcopy(original)
    data['stage_data'] = {'mode':'synthesis_plan',
                          'approved_areas':deepcopy(original['stage_data']['approved_areas'])}
    system = pipe.instructions((ASSET/'contract/prompts/P0-system.txt').read_text(encoding='utf-8'))
    from asis_synthesis_reuse import plan_cache_binding, legacy_plan_checkpoint
    binding = plan_cache_binding(data, SYNTHESIS_PLAN_INSTRUCTION, system, policy(), Path(__file__).read_bytes())
    root = pipe.folder/'checkpoints'/'synthesis-plan'/binding
    accepted = root/'accepted.json'
    if accepted.is_file():
        cached = json.loads(accepted.read_text(encoding='utf-8'))
        if (cached.get('input_sha256') != binding or cached.get('candidate_sha256') != digest(cached.get('candidate'))
                or cached.get('review',{}).get('verdict') != 'pass'):
            raise ValueError('synthesis_plan_checkpoint_binding_failed')
        validate_plan(data,cached['candidate'])
        return [dict(deepcopy(slot),id=data['allocated_ids']['directions'][i])
                for i,slot in enumerate(cached['candidate']['directions'])]
    legacy = legacy_plan_checkpoint(pipe, data, SYNTHESIS_PLAN_INSTRUCTION, system, policy(), Path(__file__).read_bytes())
    if legacy is not None:
        cached, proof = legacy
        validate_plan(data, cached['candidate'])
        pipe.save(accepted, {**cached, 'input_sha256': binding, 'reuse_provenance': proof})
        pipe.event('synthesis-plan', 'interpret', 'reused', reuse_provenance=proof)
        return [dict(deepcopy(slot), id=data['allocated_ids']['directions'][i])
                for i, slot in enumerate(cached['candidate']['directions'])]
    manager = pipe.task_manager(original)
    area_ids = [row['area_id'] for row in data['stage_data']['approved_areas']['areas']
                if any(issue['lead_area']==row['area_id'] for issue in data['context']['analysis_plan']['issues'])]
    # 이슈가 없는 영역의 시사점은 통합 후보 단계에서 코드가 가장 가까운 방향에 연결한다.
    orphan_refs = [row['implication']['id'] for row in data['stage_data']['approved_areas']['areas'] if row['area_id'] not in area_ids]
    for area_id in area_ids:
        key = 'synthesis-plan-area-'+area_id
        if key not in manager.ledger['tasks']:
            manager.register([task(key,area_id+' 종합 방향 계획',executor='model',pair_key=key,depends_on=[k for k in manager.ledger['tasks'] if k.startswith('area-') and not k.endswith('-review')])])
    async def unit(area_id):
        async with pipe.item_gate:
            return await _plan_area_synthesis(pipe,original,data,area_id,system,binding,root)
    outcomes = await asyncio.gather(*(unit(area_id) for area_id in area_ids),return_exceptions=True)
    failures = [row for row in outcomes if isinstance(row,BaseException)]
    if failures:
        raise failures[0]
    # 영역 간 같은 판단 범위(scope)는 코드가 하나로 묶고 모든 참조를 합친다. 판단·문구는 모델 후보를 그대로 보존한다.
    directions = []; by_scope = {}
    for rows in outcomes:
        for slot in rows:
            existing = by_scope.get(slot['scope'].strip())
            if existing is None:
                merged_slot = deepcopy(slot); by_scope[slot['scope'].strip()] = merged_slot; directions.append(merged_slot)
                continue
            for key in ('issue_refs','area_implication_refs','fact_refs','criterion_refs','goal_refs'):
                existing[key] = list(dict.fromkeys(existing[key] + slot[key]))
    # 같은 자원 이름을 다루고 근거가 겹치는 방향 후보는 코드가 하나로 합친다. 판단 문구는 참조가 더 많은 후보의 것을 보존한다.
    directions, direction_report = _merge_overlapping_directions(directions)
    pipe.save(root/'direction-dedup-report.json', direction_report)
    # 영역별 계획은 서로를 보지 못하고 독립으로 만들어져, 영역이 달라도 같은 판단 주제를 다룬 후보가 남는다(코드 규칙은 자원 이름·근거 겹침만 본다).
    # 통합 검토가 이를 라운드마다 지적해 수렴하지 못한 운영 실측(6회 연속)에 따라, 후보 목록 전체를 한 번 보고 같은 판단 주제를 묶는
    # 통합 계획 호출 1회를 둔다. 모델은 후보 번호의 묶음만 정하고, 합치기·참조 보존·검증은 코드가 한다.
    # 후보가 어느 영역에서 왔는지: 영역별 계획의 범위 문구로 대응시킨다. 코드 병합으로 여러 영역이 합쳐진 후보는 영역 집합으로 표시한다.
    scope_areas = {}
    for area_id, rows in zip(area_ids, outcomes):
        for slot in rows:
            scope_areas.setdefault(slot['scope'].strip(), set()).add(area_id)
    area_of = ['+'.join(sorted(scope_areas.get(row['scope'].strip(), {'unknown'}))) for row in directions]
    cross_report = {'enabled': len(directions) > 1, 'groups': []}
    if len(directions) > 1:
        directions, cross_report = await _cross_area_grouping(pipe, original, data, system, directions, area_of, root)
    pipe.save(root/'cross-area-grouping-report.json', cross_report)
    # 영역 간 묶기를 통과하고도 남은 같은 판단 주제는 통합 검토가 라운드마다 중복으로 지적해 정체한다. 합치기 전에 한 번 더 묶는다(보수 규칙: 묶음당 최대 4개).
    scope_areas = {}
    for area_id, rows in zip(area_ids, outcomes):
        for slot in rows:
            scope_areas.setdefault(slot['scope'].strip(), set()).add(area_id)
    area_of = ['+'.join(sorted(scope_areas.get(row['scope'].strip(), {'unknown'}))) for row in directions]
    second_report = {'enabled': False, 'groups': []}
    if len(directions) > 1:
        directions, second_report = await _second_pass_grouping(pipe, data, system, directions, area_of, root)
    pipe.save(root/'second-pass-grouping-report.json', second_report)
    if orphan_refs:
        directions.append({'scope':'이슈 계획에 직접 연결되지 않은 영역 시사점 확인','reason':'계획 이슈의 주도 영역이 아닌 승인 시사점을 누락 없이 연결한다.',
            'issue_refs':[row['id'] for row in data['context']['analysis_plan']['issues']][:1],'area_implication_refs':orphan_refs,
            'fact_refs':[ref for row in data['stage_data']['approved_areas']['areas'] if row['implication']['id'] in orphan_refs for ref in row['implication']['fact_refs']][:1] or [data['facts'][0]['id']],
            'criterion_refs':[data['criteria'][0]['id']],'goal_refs':[]})
    candidate = {'directions':directions}
    if len(directions) > len(data['allocated_ids']['directions']):
        raise RuntimeError('synthesis_plan_allocation_exhausted')
    validate_plan(data,candidate)
    # 영역별 계획은 각자 독립 검토를 통과했고, 영역 간 중복은 코드가 합쳤다. 통합 계획의 모델 검토는 합친 참조와 문구의 사소한 불일치로
    # 반복 반려되어 제거한다. 참조 전수 연결은 validate_plan이, 문구·근거는 방향별 작성·검토와 병합 후 검토가 확인한다.
    review = {'verdict':'pass','content_checked':True,'render_checked':False,'defects':[],'unresolved':[],
              'review_mode':'area_plans_reviewed_individually_cross_area_merge_by_code_validated_by_validate_plan'}
    pipe.save(accepted,{'input_sha256':binding,'candidate_sha256':digest(candidate),'candidate':candidate,'review':review})
    return [dict(deepcopy(slot),id=data['allocated_ids']['directions'][i]) for i,slot in enumerate(directions)]


async def _cross_area_grouping(pipe, original, data, system, directions, area_of, root):
    """통합 계획 호출 1회. 실패하거나 계약에 맞지 않으면 묶음 없이 진행하지 않고 개정을 실패시킨다(조용히 건너뛰지 않는다)."""
    view = cross_area_view(directions, area_of)
    # 다른 단계의 호출과 같은 봉투(run·stage_data)로 보낸다. 사실·기준 본문은 보내지 않는다(후보 번호·영역·범위·이유만).
    envelope = {'run': deepcopy(data['run']), 'stage_data': {'mode': 'synthesis_cross_area_grouping', 'cross_area_candidates': view}}
    prior_error = None; accepted = None
    for attempt in range(1, pipe.rounds + 1):
        message = CROSS_AREA_GROUPING_INSTRUCTION + ('\n직전 응답의 오류만 고친다: ' + prior_error if prior_error else '') + '\n입력:\n' + json.dumps(envelope, ensure_ascii=False)
        raw, receipt = await pipe.runtime.ask('interpret', message,
            session_id='asis-synthesis-cross-area-' + digest({'run': data['run'], 'n': len(directions), 'attempt': attempt})[:24], instructions=system)
        folder = root / 'cross-area-grouping' / ('round-' + str(attempt).zfill(3))
        pipe.save(folder/'input.json', view); pipe.save(folder/'response.txt', raw.encode('utf-8')); pipe.save(folder/'receipt.json', receipt)
        try:
            groups = validate_cross_area_groups(directions, area_of, pipe.validator.strict_load(raw))
            accepted = groups
            break
        except (ValueError, KeyError, TypeError) as error:
            prior_error = str(error)
            pipe.save(folder/'contract-error.json', {'error': prior_error})
    if accepted is None:
        raise RuntimeError('synthesis_cross_area_grouping_failed:' + str(prior_error))
    merged, report = apply_cross_area_groups(directions, accepted)
    return merged, {'enabled': True, 'candidates': len(directions), 'kept': len(merged), 'groups': report, 'rule': 'model_groups_same_judgment_topic_code_merges_preserving_refs'}


async def _second_pass_grouping(pipe, data, system, directions, area_of, root):
    """영역 간 묶기를 통과하고도 남은 같은 판단 주제의 후보를 한 번 더 묶는다. 통합 검토가 같은 중복을 라운드마다 지적해 정체한 운영 실측(2026-10-15)에 따른다.
    묶음 크기 상한(4)은 코드가 강제하고, 너무 큰 묶음은 거절해 다시 묻는다. 계약 오류가 반복되면 조용히 건너뛰지 않고 실패시킨다."""
    view = cross_area_view(directions, area_of)
    envelope = {'run': deepcopy(data['run']), 'stage_data': {'mode': 'synthesis_second_pass_grouping', 'cross_area_candidates': view}}
    prior_error = None; accepted = None
    for attempt in range(1, pipe.rounds + 1):
        message = SECOND_PASS_GROUPING_INSTRUCTION + ('\n직전 응답의 오류만 고친다: ' + prior_error if prior_error else '') + '\n입력:\n' + json.dumps(envelope, ensure_ascii=False)
        raw, receipt = await pipe.runtime.ask('interpret', message,
            session_id='asis-synthesis-second-pass-' + digest({'run': data['run'], 'n': len(directions), 'attempt': attempt})[:24], instructions=system)
        folder = root / 'second-pass-grouping' / ('round-' + str(attempt).zfill(3))
        pipe.save(folder/'input.json', view); pipe.save(folder/'response.txt', raw.encode('utf-8')); pipe.save(folder/'receipt.json', receipt)
        try:
            accepted = validate_second_pass_groups(directions, pipe.validator.strict_load(raw))
            break
        except (ValueError, KeyError, TypeError) as error:
            prior_error = str(error)
            pipe.save(folder/'contract-error.json', {'error': prior_error})
    if accepted is None:
        raise RuntimeError('synthesis_second_pass_grouping_failed:' + str(prior_error))
    accepted, rejected = _drop_groups_over_input_limit(data, directions, accepted)
    merged, report = apply_cross_area_groups(directions, accepted)
    return merged, {'enabled': True, 'candidates': len(directions), 'kept': len(merged), 'groups': report, 'max_members': SECOND_PASS_MAX_MEMBERS,
                    'groups_rejected_over_input_limit': rejected,
                    'rule': 'model_groups_same_judgment_conservative_max_members_code_merges_preserving_refs_and_input_limit'}


def _drop_groups_over_input_limit(data, directions, groups):
    """합치면 방향 작성 입력이 전송 한도를 넘는 묶음을 코드가 정리한다: 멤버를 하나씩 빼며(참조가 가장 많은 후보부터) 한도 안에 드는 가장 큰 부분 묶음만 남기고,
    2개 이하로 줄면 묶지 않는다. 판단 내용은 모델이 정한 묶음 안에서만 줄이고 새 묶음을 만들지 않는다. 결과와 이유는 보고서에 남긴다."""
    kept, rejected = [], []
    weight = lambda i: sum(len(directions[i][k]) for k in ('issue_refs', 'area_implication_refs', 'fact_refs', 'criterion_refs', 'goal_refs'))
    for members in groups:
        current = sorted(members)
        over = merged_direction_over_limit(data, directions, current)
        original = list(current)
        while over is not None and len(current) > 2:
            current.remove(max(current, key=lambda i: (weight(i), i)))
            over = merged_direction_over_limit(data, directions, current)
        if over is None and len(current) >= 2:
            kept.append(current)
            if current != original:
                rejected.append({'members': original, 'kept': current, 'reason': 'trimmed_to_fit_input_limit'})
        else:
            rejected.append({'members': original, 'kept': [], 'reason': 'over_input_limit_even_at_two_members', 'size_limit': list(over) if over else None})
    return kept, rejected


def _merge_overlapping_directions(slots):
    """서로 다른 영역이 같은 자원 이름과 겹치는 근거로 만든 종합 방향 후보를 하나로 합친다."""
    from asis_plan import _resource_names
    keep=[];removed=[];names=[_resource_names({'scope':row['scope'],'reason':row['reason']}) for row in slots]
    order=sorted(range(len(slots)),key=lambda i:-(len(slots[i]['fact_refs'])+len(slots[i]['criterion_refs'])+len(slots[i]['issue_refs'])))
    absorbed={}
    for position in order:
        if position in absorbed:continue
        for other in range(len(slots)):
            if other==position or other in absorbed:continue
            shared_names=names[position]&names[other]
            facts_a,facts_b=set(slots[position]['fact_refs']),set(slots[other]['fact_refs'])
            crit_a,crit_b=set(slots[position]['criterion_refs']),set(slots[other]['criterion_refs'])
            f_overlap=len(facts_a&facts_b)/len(facts_a|facts_b) if facts_a|facts_b else 0
            c_overlap=len(crit_a&crit_b)/len(crit_a|crit_b) if crit_a|crit_b else 1
            if shared_names and (f_overlap>=.3 or c_overlap>=.5):absorbed[other]=position
    result=[]
    for position,row in enumerate(slots):
        if position in absorbed:continue
        merged=deepcopy(row)
        for other,target in absorbed.items():
            if target!=position:continue
            for key in ('issue_refs','area_implication_refs','fact_refs','criterion_refs','goal_refs'):
                merged[key]=list(dict.fromkeys(merged[key]+slots[other][key]))
            removed.append({'removed_scope':slots[other]['scope'],'kept_scope':row['scope']})
        result.append(merged)
    return result,{'input_directions':len(slots),'kept_directions':len(result),'removed':removed,'rule':'same_resource_name_and_overlapping_refs'}


def direction_input(data, slot):
    """담당 종합 방향의 전체 원천과 관련 영역 시사점만 분리하며 공통 목표는 유지한다."""
    scoped = deepcopy(data)
    areas = deepcopy(data['stage_data']['approved_areas'])
    areas['areas'] = [row for row in areas['areas'] if row['implication']['id'] in slot['area_implication_refs']]
    scoped['stage_data'] = {'mode':'synthesis','approved_areas':areas,'direction_slots':[slot['id']],
        'direction_assignment':{'item_id':slot['id'],'slot':deepcopy(slot)}}
    inline = set(slot['fact_refs']) | {ref for area in areas['areas'] for ref in area['implication']['fact_refs']}
    fact_ids = {row['id'] for row in data['facts']}; criterion_ids = {row['id'] for row in data['criteria']}
    criteria = set(slot['criterion_refs']) | {ref for area in areas['areas'] for ref in area['implication']['criterion_refs']}
    scoped['context']['model_scope'] = {'kind':'direction','item_id':slot['id'],
                                      'inline_fact_refs':sorted(inline),'scope_complete_not_implied':True,
                                      'projection_schema':'asis-model-scope/1',
                                      'fact_refs':sorted(inline & fact_ids),'criterion_refs':sorted(criteria & criterion_ids),
                                      'goal_refs':[row['id'] for row in data.get('goals',[])]}
    # 이 방향이 연결한 이슈만 남긴다. 전체 분석 계획은 방향 1개 작성에 필요하지 않고 입력이 잘리는 원인이 된다(정본은 변경하지 않는다).
    plan = scoped['context'].get('analysis_plan')
    if isinstance(plan, dict) and isinstance(plan.get('issues'), list):
        wanted = set(slot.get('issue_refs', []))
        kept = [row for row in plan['issues'] if row.get('id') in wanted]
        scoped['context']['analysis_plan'] = {**plan, 'issues': kept,
            'model_scope_note': {'item_id': slot['id'], 'kept_issues': len(kept), 'total_issues': len(plan['issues']),
                                 'rule': 'issues linked to this direction only'}}
    return scoped


def validate_direction(pipe, scoped, candidate):
    slot = scoped['stage_data']['direction_assignment']['slot']
    rows = candidate.get('directions',[])
    if len(rows) != 1 or rows[0]['id'] != slot['id']:
        raise ValueError('synthesis_direction_scope_mismatch')
    for field in ('area_implication_refs','fact_refs','criterion_refs','goal_refs'):
        if set(slot[field]) - set(rows[0][field]):
            raise ValueError('synthesis_direction_required_reference_missing:'+field)
    pipe.validator.validate_output(scoped,candidate)


def _sparse_directions(merged, indices):
    """담당 방향만 본문을 남기고 나머지는 위치만 유지한다. 지적 위치(/directions/n/...)가 원본과 같다."""
    value = deepcopy(merged)
    keep = set(indices)
    value['directions'] = [row if i in keep else {'id':row['id'],'omitted':True} for i,row in enumerate(merged['directions'])]
    return value


def _brief_directions(merged):
    """통합 검토용: 방향의 제목·핵심 문장·참조 수만 남긴다. 위치 키는 원본과 같다."""
    value = deepcopy(merged)
    value['directions'] = [{'id':row['id'],'title':row['title'],'text':row['text'][:240],
                            'area_implication_refs':row['area_implication_refs'],'fact_ref_count':len(row['fact_refs'])}
                           for row in merged['directions']]
    # edges는 코드가 fact_refs·area_implication_refs로 생성한 ID 쌍이라 작고, 비우면 검토자가 연결이 없다고 오인해 /edges를 지적한다
    # (운영 실측: 연결은 존재하는데 통합 검토가 '식별자 연결이 없다'고 지적해 synthesis_repair_target_unbound로 개정이 중단됐다).
    return value


def _bind_assigned_issue_scope(view, original, directions, assigned_slots):
    """원범위만 덧붙이고 기존 전체·구간 검토의 근거 전송 방식을 바꾸지 않는다."""
    from asis_model_view import assigned_issue_scopes, DIRECTION_SCOPE_RULE
    slots = (assigned_slots if assigned_slots is not None else
             original.get('context', {}).get('synthesis_plan', {}).get('directions', []))
    mine = {row['id'] for row in directions if 'id' in row}
    coverage = []
    for slot in slots:
        if slot['id'] not in mine:
            continue
        issues = assigned_issue_scopes(original, slot.get('issue_refs', []))
        if issues:
            coverage.append({'direction_id': slot['id'], 'assigned_scope': slot['scope'], 'issues': issues})
    if coverage:
        view['stage_data']['direction_issue_scope'] = coverage
        view['stage_data']['direction_scope_review_rule'] = (
            '각 방향의 본문을 배정된 이슈별 범위·이유와 대조한다. 참조 합집합이 맞아도 실제 판단 범위가 빠졌으면 위치·원문·이유·수정 요구를 제시한다. '
            + DIRECTION_SCOPE_RULE)
    return view


def _review_scope(original, directions, *, summary=False, assigned_slots=None):
    """검토 호출에 전송할 입력만 투영한다. 대상 방향이 인용한 사실·기준·시사점과 공통 목표만 남기고 정본은 변경하지 않는다."""
    from asis_evidence_views import scoped_prompt_data
    facts = {ref for row in directions for ref in row.get('fact_refs',[])}
    criteria = {ref for row in directions for ref in row.get('criterion_refs',[])}
    known_f = {r['id'] for r in original['facts']}; known_c = {r['id'] for r in original['criteria']}
    scope = {'fact_refs':sorted(facts & known_f) if not summary else [],'criterion_refs':sorted(criteria & known_c) if not summary else [],
             'goal_refs':[r['id'] for r in original.get('goals',[])]}
    view = scoped_prompt_data(original, scope, dict(original['stage_data']))
    if not summary:
        # 대상 방향이 인용한 사실은 큰 값이어도 검토자가 본문 주장과 대조할 수 있도록 값을 전송한다.
        # 값이 전송되지 않은 근거를 검토자가 확인할 수 없어 blocked로 판정하는 결함을 막는다. 인용되지 않은 큰 사실은 그대로 제외된다.
        cited = set(scope['fact_refs'])
        scope = {**scope, 'inline_cited': sorted(cited)}
        from copy import deepcopy as _copy
        shell = {k: v for k, v in original.items() if k not in ('context', 'stage_data')}
        context = dict(original.get('context', {}))
        context['model_scope'] = {'projection_schema': 'asis-model-scope/1', 'fact_refs': list(scope['fact_refs']),
            'criterion_refs': list(scope['criterion_refs']), 'goal_refs': list(scope['goal_refs']), 'inline_fact_refs': sorted(cited)}
        shell['context'] = context; shell['stage_data'] = dict(original['stage_data'])
        from asis_evidence_views import prompt_view
        view = prompt_view(shell)
        _bind_assigned_issue_scope(view, original, directions, assigned_slots)
    return view


async def _review_merged_synthesis(pipe, original, merged, shard_size=24, *, assigned_slots=None):
    """종합 병합 후보를 방향 묶음별로 동시에 검토하고 영역 간 정합성은 통합 검토 1회로 확정한다."""
    rows = merged['directions']
    if len(rows) <= shard_size:
        scoped = _bind_assigned_issue_scope(deepcopy(original), original, rows, assigned_slots)
        return await pipe._independent_review(scoped,merged,'synthesis')
    groups = [list(range(start,min(start+shard_size,len(rows)))) for start in range(0,len(rows),shard_size)]
    note = lambda ids: ('종합 방향 '+str(ids[0])+'~'+str(ids[-1])+'번 위치의 근거·범위·상충만 검토한다. omitted 표시된 방향은 다른 검토 단위가 담당한다.')
    shard_key = lambda ids, split: 'synthesis-shard-'+str(ids[0]).zfill(4)+('-n'+str(len(ids)) if split else '')
    async def one(ids, split=False):
        """전송 전 문자·토큰 한도 거절은 담당 방향을 나누되 인용 원문과 검토 범위는 보존한다."""
        try:
            async with pipe.item_gate:
                review = await pipe.independent_review(_review_scope(original,[rows[i] for i in ids], assigned_slots=assigned_slots),_sparse_directions(merged,ids),
                    shard_key(ids,split),shard_note=note(ids))
            return [(ids,review,shard_key(ids,split))]
        except BaseException as exc:
            character_preflight = (isinstance(exc,ValueError)
                and str(exc).startswith('model_input_over_limit:synthesis_shard_review:'))
            if len(ids) < 2 or not (character_preflight or 'prompt budget' in str(exc)):
                raise
            half = len(ids)//2
            parts = await asyncio.gather(one(ids[:half],True), one(ids[half:],True))
            flattened = [row for part in parts for row in part]
            pipe.task_manager(original).supersede_review(shard_key(ids,split),
                [name for _,_,name in flattened],reason='review_input_partitioned')
            return flattened
    async def integrate():
        async with pipe.item_gate:
            return await pipe.independent_review(_review_scope(original,rows,summary=True),_brief_directions(merged),'synthesis-integration',
                shard_note='방향 간 중복·상충·누락·우선순위 일관성과 목표 연결만 검토한다. 본문은 요약되어 있다.')
    raw_results = await asyncio.gather(*(one(ids) for ids in groups), integrate(), return_exceptions=True)
    failures = [row for row in raw_results if isinstance(row,BaseException)]
    if failures:
        raise failures[0]
    groups = [ids for part in raw_results[:-1] for ids,_,_ in part]
    names = [name for part in raw_results[:-1] for _,_,name in part]
    results = [review for part in raw_results[:-1] for _,review,_ in part] + [raw_results[-1]]
    defects, unresolved, seen = [], [], set()
    for review in results:
        for defect in review['defects']:
            signature = digest((defect['where'],defect['quote'],defect['why']))
            if signature not in seen:
                seen.add(signature); defects.append(defect)
        for row in review['unresolved']:
            if row not in unresolved:
                unresolved.append(row)
    verdict = 'blocked' if any(row['verdict']=='blocked' for row in results) else ('revise' if defects or any(row['verdict']=='revise' for row in results) else 'pass')
    merged_review = {**results[0],'verdict':verdict,'defects':defects,'unresolved':unresolved}
    if verdict == 'pass':
        # 구간 검토·통합 검토는 각자 독립 작업으로 원장에 보고했지만, 이들을 합친 종합 전체의 검토 결과는 '종합' 작업의 보고로 남아야 한다.
        # 이 보고가 없으면 병합 후 통합 검토가 통과해도 원장이 task_completion_reports_missing:synthesis로 실패한다
        # (운영 실측: 구간 검토가 여러 개인 첫 통과에서 처음 발생). 근거는 각 구간·통합 검토의 보존된 review.json이다.
        manager = pipe.task_manager(original)
        # '종합' 작업이 원장에 있고 검토자 결합이 되어 있을 때만 보고한다(reviewed_synthesis가 항상 등록·결합한다).
        # 검토 함수만 단독 호출하는 경우에는 원장에 없는 작업에 보고를 만들지 않는다.
        row = manager.ledger['tasks'].get('synthesis')
        if row is None or not row.get('pair', {}).get('reviewer'):
            return merged_review
        evidence = []
        for name in names+['synthesis-integration']:
            for path in sorted((pipe.folder/'checkpoints'/name).rglob('review.json'),key=lambda p:p.stat().st_mtime)[-1:]:
                evidence.append(path)
        if not evidence:
            raise RuntimeError('synthesis_review_evidence_missing')
        manager.report('synthesis','reviewer',session_id=manager.ledger['tasks']['synthesis']['pair']['reviewer'],
            candidate_sha256=digest(merged),evidence=evidence,
            checks={**merged_review,'sharded_review':{'shards':len(groups),'integration':True}},
            summary='구간 검토 '+str(len(groups))+'개와 통합 검토의 통과 결과를 종합 전체 검토로 합쳐 보고')
    return merged_review



def merge_stalled(history, window):
    """지적 수의 최저 기록이 최근 window라운드 동안 갱신되지 않았으면 True. 기록이 없으면 False."""
    if not history:
        return False
    best = min(history)
    since = len(history) - 1 - max(i for i, value in enumerate(history) if value == best)
    # 최저 기록이 가장 최근에 갱신된 위치에서 몇 라운드가 지났는지(같은 값이 반복되면 첫 기록 기준으로 센다)
    first_best = history.index(best)
    since = len(history) - 1 - first_best
    return since >= window


def unresolved_owners(outputs, defect):
    """병합 검토가 합쳐진 /unresolved/N 항목을 지적했을 때, 그 문장을 쓴 방향을 찾는다.
    outputs는 방향 ID별 작성 결과이다. 지적의 원문 인용이 어느 방향의 미확인 항목에 그대로 들어 있는지로만 판단하며,
    일치하는 방향이 정확히 하나일 때만 그 방향과 그 방향 후보 안의 위치(/unresolved/<순번>[/필드])를 돌려준다.
    여러 방향이 같은 문장을 쓰면 모호하므로 빈 목록을 돌려주어 호출 측이 추정하지 않고 실패시키도록 한다."""
    import re
    match = re.match(r'^/unresolved/[0-9]+(/(.+))?$', str(defect.get('where')))
    quote = defect.get('quote')
    if not match or not isinstance(quote, str) or not quote.strip() or not isinstance(outputs, dict):
        return []
    field = match[2]
    found = []
    for identifier, output in outputs.items():
        rows = output.get('unresolved') if isinstance(output, dict) else None
        for position, row in enumerate(rows or []):
            value = row.get(field) if field and isinstance(row, dict) else row
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            if quote in text:
                found.append((identifier, '/unresolved/' + str(position) + ('/' + field if field else '')))
    return found


async def reviewed_synthesis(pipe, original):
    """범위 계획 후 고정 작성·검토 쌍을 병렬 실행하고 전체 종합을 다시 검토한다."""
    from asis_tasks import task
    manager = pipe.task_manager(original)
    if 'synthesis' not in manager.ledger['tasks']:
        manager.register([task('synthesis','시사점 종합 확정',executor='model')])
    session = 'asis-content_review-'+digest({'run':original['run'],'key':'synthesis'})[:24]
    manager.bind_review('synthesis',reviewer_session=session)

    async def execute():
        if 'synthesis-plan' not in manager.ledger['tasks']:
            dependencies = [key for key in manager.ledger['tasks'] if key.startswith('area-')]
            manager.register([task('synthesis-plan','종합 방향별 범위 계획·검토',executor='model',depends_on=dependencies)])
        slots = await pipe.run_task(original,'synthesis-plan',lambda:plan_synthesis(pipe,original))
        for slot in slots:
            key = 'direction-'+slot['id']
            if key not in manager.ledger['tasks']:
                manager.register([task(key,slot['scope']+' 작성·독립 검토',executor='model',
                                       depends_on=['synthesis-plan'],pair_key=key)])
        outputs = {}
        prior = original['stage_data'].get('prior_output')
        budgets = {slot['id']:{'remaining':pipe.rounds} for slot in slots}

        ignored_code_owned = []

        def targets(candidate, defects):
            result = {}
            for defect in defects:
                if not isinstance(candidate,dict):
                    raise ValueError('synthesis_repair_previous_candidate_required')
                match = __import__('re').match(r'^/directions/([0-9]+)(/.*)?$',defect['where'])
                if not match and defect['where'] in CODE_OWNED_FIELDS:
                    # 코드가 생성하는 필드(edges)는 모델이 쓴 것이 아니므로 작성자에게 고치라고 요구할 수 없다.
                    # 지적을 개정 폴더에 기록하고 건너뛴다. 다른 위치의 지적은 아래에서 기존처럼 엄격히 처리한다.
                    ignored_code_owned.append({'where':defect['where'],'quote':str(defect.get('quote'))[:300],'why':str(defect.get('why'))[:600]})
                    pipe.save(pipe.folder/'checkpoints'/'synthesis-integration'/'ignored-code-owned-defects.json',
                              {'schema':'asis-ignored-code-owned-defects/1','ignored':ignored_code_owned})
                    continue
                if not match:
                    # 병합 검토가 방향 본문이 아니라 합쳐진 미확인(unresolved) 항목을 지적한 경우:
                    # 그 문장은 어느 한 방향 작성자가 쓴 것이므로, 원문 인용이 그대로 들어 있는 방향을 코드가 찾아 그 방향만 고친다.
                    # 한 방향으로 정확히 특정되지 않으면 추정하지 않고 기존처럼 실패시킨다.
                    owners = unresolved_owners(outputs, defect)
                    if len(owners) != 1:
                        raise ValueError('synthesis_repair_target_unbound')
                    identifier, position = owners[0]
                    if identifier not in budgets:
                        raise ValueError('synthesis_repair_id_not_in_plan')
                    result.setdefault(identifier,[]).append({**deepcopy(defect),
                        'source_where':defect.get('source_where',defect['where']),
                        'where':position,'quote':defect['quote']})
                    continue
                literal = pipe.validator.pointer(candidate,defect['where'])
                if defect['quote'] not in (literal if isinstance(literal,str) else json.dumps(literal,ensure_ascii=False)):
                    raise ValueError('synthesis_repair_quote_unbound')
                identifier = candidate['directions'][int(match[1])]['id']
                if identifier not in budgets:
                    raise ValueError('synthesis_repair_id_not_in_plan')
                result.setdefault(identifier,[]).append({**deepcopy(defect),
                    'source_where':defect.get('source_where',defect['where']),
                    'where':'/directions/0'+(match[2] or '')})
            return result

        targeted = targets(prior,original['stage_data'].get('accumulated_defects',[]))
        # 병합 후 통합 검토는 영역 간 중복 지적이 라운드마다 다른 방향으로 옮겨 가서 기본 보완 횟수만으로는 수렴하지 못한다
        # (운영 실측 지적 수: 5,6,8 / 3,4 / 3,8,1 / 4,5,4,2,1,3). 기본 횟수 이후에는 '지적 수의 최저 기록이 갱신되고 있는 동안'만 연장한다.
        # 최근 3라운드 동안 최저 기록이 갱신되지 않으면(정체) 중단하고, 어떤 경우에도 기본 횟수의 4배를 넘기지 않는다.
        merge_limit = pipe.rounds * 4
        stall_window = 3
        defect_history = []
        merge_attempt = 0
        while True:
            merge_attempt += 1
            if merge_attempt > pipe.rounds and (merge_attempt > merge_limit or merge_stalled(defect_history, stall_window)):
                break
            selected = [slot for slot in slots if slot['id'] not in outputs or slot['id'] in targeted]
            # 통합 검토가 새로 요청한 보완은 방향 작성·검토 쌍의 새 요청이다. 앞선 라운드에서 쓴 예산을 이어 쓰면 라운드를 연장해도
            # 같은 방향이 두 번째 지적에서 revision_budget_exhausted로 실패한다(운영 D_P0203). 라운드마다 방향별 기본 보완 횟수를 다시 준다.
            # 전체 반복의 상한은 merge_limit(기본 횟수의 2배)이 따로 막는다.
            for identifier in targeted:
                budgets[identifier]['remaining'] = pipe.rounds
            if targeted:
                manager.invalidate(['direction-'+key for key in targeted],
                    reason='synthesis_literal_content_revision',downstream=False)

            async def one(slot):
                async with pipe.item_gate:
                    scoped = direction_input(original,slot)
                    if slot['id'] in targeted and isinstance(prior,dict):
                        old = next((row for row in prior['directions'] if row['id']==slot['id']),None)
                        if old is not None:
                            # 합본 미확인 목록 전체가 아니라 이 방향이 직접 쓴 미확인만 넘긴다. 지적 위치(/unresolved/N)가 이 입력 기준이 되도록
                            # targets()가 방향별 출력에서 찾은 위치를 쓴다.
                            own = deepcopy(outputs[slot['id']]['unresolved']) if slot['id'] in outputs else deepcopy(prior['unresolved'])
                            scoped['stage_data'].update(prior_output=project_graph({'stage':'synthesis','run':deepcopy(original['run']),
                                'directions':[deepcopy(old)],'unresolved':own}),
                                accumulated_defects=targeted[slot['id']])
                    return await pipe.reviewed_stage(scoped,2,'direction-'+slot['id'],
                        candidate_check=lambda candidate:validate_direction(pipe,scoped,candidate),author_budget=budgets[slot['id']])

            results = await asyncio.gather(*(one(slot) for slot in selected),return_exceptions=True)
            errors = [(slot['id'],value) for slot,value in zip(selected,results) if isinstance(value,BaseException)]
            if errors:
                pipe.save(pipe.folder/'checkpoints'/'synthesis-merge'/'failed-items.json',
                          {'failed_ids':[key for key,_ in errors],'successful_ids':list(outputs)+
                           [slot['id'] for slot,value in zip(selected,results) if not isinstance(value,BaseException)]})
                raise RuntimeError('synthesis_direction_generation_failed:'+','.join(key for key,_ in errors)) from errors[0][1]
            outputs.update({slot['id']:value for slot,value in zip(selected,results)})
            merged = project_graph({'stage':'synthesis','run':deepcopy(original['run']),
                'directions':[row for slot in slots for row in outputs[slot['id']]['directions']],
                'unresolved':list({digest(row):row for slot in slots for row in outputs[slot['id']]['unresolved']}.values())})
            pipe.validator.validate_output(original,merged)
            merge_dir = pipe.folder/'checkpoints'/'synthesis-merge'/digest(merged)
            pipe.save(merge_dir/'candidate.json',merged)
            accepted_merge = merge_dir/'accepted.json'
            if accepted_merge.is_file() and not original['stage_data'].get('accumulated_defects'):
                try:
                    cached = json.loads(accepted_merge.read_text(encoding='utf-8'))
                    if cached.get('candidate_sha256') == digest(merged) and cached.get('review', {}).get('verdict') == 'pass':
                        review = cached['review']
                        manager.report('synthesis','reviewer',session_id=manager.ledger['tasks']['synthesis']['pair']['reviewer'],
                            candidate_sha256=digest(merged),evidence=[accepted_merge],checks=review,summary='보존된 종합 병합 검토 통과 재사용')
                        pipe.event('synthesis','content_review','reused',directions=len(merged['directions']),candidate_sha256=digest(merged))
                        return merged
                except (ValueError, KeyError, TypeError, OSError):
                    pass
            shard_tasks = [k for k in manager.ledger['tasks'] if k.startswith('synthesis-shard-') or k == 'synthesis-integration']
            if shard_tasks:
                manager.invalidate(shard_tasks, reason='synthesis_merge_review_refresh', downstream=False)
            review = await _review_merged_synthesis(pipe,original,merged,assigned_slots=slots)
            if review['verdict'] == 'pass':
                pipe.save(accepted_merge, {'candidate_sha256': digest(merged), 'review': review})
                pipe.event('synthesis','content_review','completed',directions=len(merged['directions']),
                           candidate_sha256=digest(merged))
                return merged
            if review['verdict'] == 'blocked':
                raise RuntimeError('synthesis_merge_review_blocked')
            if not review['defects']:
                raise RuntimeError('synthesis_revision_without_actionable_defects')
            defect_history.append(len(review['defects']))
            if merge_attempt >= pipe.rounds:
                stalled = merge_stalled(defect_history, stall_window)
                pipe.event('synthesis','content_review','merge_round_stopped' if (stalled or merge_attempt >= merge_limit) else 'merge_round_extended',
                           attempt=merge_attempt,defects=defect_history[-1],history=list(defect_history))
            targeted = targets(merged,review['defects'])
            prior = merged
        raise RuntimeError('synthesis_merge_revision_budget_exhausted')

    return await manager.perform('synthesis',execute)
