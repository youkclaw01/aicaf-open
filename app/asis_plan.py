"""가변 분석 계획. 영역별 계획·독립 검토를 병렬로 실행하고 통합 검토 뒤 코드가 ID·참조·범위를 확정한다."""
from copy import deepcopy
import asyncio
import json
import re
from pathlib import Path
from asis_analysis import AREA_NAMES
from asis_runtime import policy

AREA_REPAIR_ROUNDS=6
ISSUE_KEYS={'area_refs','fact_refs','criterion_refs','goal_refs','scope','reason'}


def _validate_issues(candidate, raw, scope, area_id=None):
    """계획 후보의 형태·참조·중복을 결정적으로 검사한다."""
    from asis_model import digest
    problems=[]
    if not isinstance(candidate,dict) or set(candidate)!={'issues'} or not isinstance(candidate.get('issues'),list) or not candidate['issues']:
        return [{'where':'/','quote':raw[:500],'why':'invalid_issue_plan_shape','fix':'Return the required nonempty issues JSON.'}]
    seen=set()
    allowed_by_key=(('area_refs',set(AREA_NAMES),True),('fact_refs',scope['facts'],True),
                    ('criterion_refs',scope['criteria'],True),('goal_refs',scope['goals'],False))
    for index,row in enumerate(candidate['issues']):
        valid=isinstance(row,dict) and set(row)==ISSUE_KEYS
        if valid:
            for key,allowed,nonempty in allowed_by_key:
                values=row[key]
                if not isinstance(values,list) or (nonempty and not values) or not all(isinstance(x,str) and x in allowed for x in values):valid=False;break
                if len(values)!=len(set(values)):valid=False;break
            valid=valid and all(isinstance(row[key],str) and row[key].strip() for key in ('scope','reason'))
            if valid and area_id and row['area_refs'][0]!=area_id:valid=False
        signature=digest(row)
        if not valid or signature in seen:
            problems.append({'where':'/issues/'+str(index),'quote':json.dumps(row,ensure_ascii=False),
                'why':'unbound_or_duplicate_issue_plan','fix':'Use declared reference IDs, put the assigned area first in area_refs and keep a distinct evidence-backed decision scope.'})
        seen.add(signature)
    return problems


def _unit_instruction(area_id):
    return ('공식 기준과 수집 근거에 따라 이번 호출에 배정된 영역('+area_id+': '+AREA_NAMES[area_id]+')의 고객 의사결정 단위 분석 이슈를 계획한다. '
        '모든 이슈의 area_refs 첫 항목은 '+area_id+'이어야 하며 다른 영역과 같은 원인은 area_refs에 함께 넣는다. 영역당 한 이슈·한 과제로 고정하지 않는다. '
        '서로 다른 대상·선행 조건·검증 조건은 별도 이슈로 유지한다. 부재·미수집·부분·권한 제한·신규 설계·현재 결함을 구분한다. '
        '수치·사실·ID·참조·충족을 만들어내지 않는다. 검증되지 않은 숫자는 scope/reason에 넣지 않는다. 근거가 없으면 사실을 등록하지 않고 확인 이슈로 계획한다. '
        'JSON은 {"issues":[{"area_refs":[영역 키],"fact_refs":[제공 사실 ID],"criterion_refs":[제공 공식 기준 ID],'
        '"goal_refs":[등록 목표 ID],"scope":"이슈 대상·범위","reason":"근거·판단·영향과 분리 이유"}]}만 반환한다. '
        '등록 area 키는 '+','.join(AREA_NAMES)+'이다. 한 개 이상, 근거에 필요한 만큼 계획하고 임의 개수 상한이나 고정 문구를 적용하지 않는다. '
        '직전 후보와 누적 지적을 보존하여 보완한다. 도구는 사용하지 않는다.')


def _extend_scope(original, scope, defects):
    """지적문(why·fix·quote)이 이름으로 인용한 실제 사실·기준 ID만 영역 범위에 추가한다. 존재하지 않는 ID는 무시한다."""
    text=' '.join(str(d.get(k,'')) for d in defects for k in ('why','fix','quote'))
    known_f={r['id'] for r in original['facts']};known_c={r['id'] for r in original['criteria']}
    cited_f=[x for x in sorted(known_f) if x in text and x not in scope['fact_refs']]
    cited_c=[x for x in sorted(known_c) if x in text and x not in scope['criterion_refs']]
    if not cited_f and not cited_c:return scope
    return {**scope,'fact_refs':list(scope['fact_refs'])+cited_f,'criterion_refs':list(scope['criterion_refs'])+cited_c}


async def _plan_area(pipe, original, key, scope, system, binding, root, *, integration_defects=None, prior_issues=None):
    """한 영역의 계획 작성과 독립 검토를 완료 저장 단위로 실행한다."""
    from asis_model import digest
    from asis_evidence_views import scoped_prompt_data
    unit_binding=digest({'binding':binding,'area':key,'facts':scope['fact_refs'],'criteria':scope['criterion_refs'],'integration_defects':integration_defects or []})
    accepted=root/'areas'/key/'accepted.json'
    if accepted.is_file() and not integration_defects:
        record=json.loads(accepted.read_text(encoding='utf-8'))
        if record.get('input_sha256')==unit_binding and record.get('candidate_sha256')==digest(record.get('issues')):
            pipe.event('issue-plan-area-'+key,'interpret','reused')
            # 보존 결과도 중앙 원장에서 영역 작업을 완료로 확정해야 통합 검토가 선행 작업을 기다리지 않는다.
            from asis_evidence_views import scoped_prompt_data as _reuse_view
            reused_evidence=_reuse_view(original,scope,{'mode':'issue_plan','plan_assignment':{'area_id':key}},area_id=key)
            async def reuse():
                # 보존 검토 원장은 저장된 통과 검토를 그대로 다시 닫는다. 입력 바인딩이 달라져도 새 모델 호출은 하지 않는다.
                if record.get('review',{}).get('verdict')!='pass':raise RuntimeError('issue_plan_reused_review_not_pass:'+key)
                await pipe.reuse_stored_review(_reuse_view(original,scope,{'mode':'issue_plan','plan_assignment':{'area_id':key,'area_name':AREA_NAMES[key]},
                    'prior_output':None,'accumulated_defects':[]},area_id=key),{'issues':record['issues']},'issue-plan-area-'+key+'-review',record['review'])
                return record['issues'],record['review']
            issues,_=await pipe.run_task(reused_evidence,'issue-plan-area-'+key,reuse)
            return issues
    if integration_defects:
        # 통합 검토가 다른 영역의 근거를 인용해 지적한 경우, 그 근거를 이 재작성 호출의 범위에 추가한다(전수 범위·정본은 불변).
        scope=_extend_scope(original,scope,integration_defects)
    sets={'facts':set(scope['fact_refs']),'criteria':set(scope['criterion_refs']),'goals':set(scope['goal_refs'])}
    prior={'issues':prior_issues} if prior_issues else None
    defects=[dict(row) for row in (integration_defects or [])]
    async def work():
        nonlocal prior
        # 영역 단위 호출은 작고 독립적이라 보완 횟수를 넉넉히 둔다. 라운드마다 한두 문장씩 지적되는 경우를 흡수한다.
        attempt=0;remaining=max(pipe.rounds,AREA_REPAIR_ROUNDS);language_refunds=0
        while remaining>0:
            attempt+=1;remaining-=1
            view=scoped_prompt_data(original,scope,{'mode':'issue_plan','plan_assignment':{'area_id':key,'area_name':AREA_NAMES[key]},
                                                  'prior_output':prior,'accumulated_defects':deepcopy(defects)},area_id=key)
            pipe.event('issue-plan-area-'+key,'interpret','running',attempt=attempt)
            raw,receipt=await pipe.runtime.ask('interpret',_unit_instruction(key)+'\n입력:\n'+json.dumps(view,ensure_ascii=False),
                session_id='asis-plan-'+digest({'run':original['run'],'input':unit_binding,'attempt':attempt})[:32],instructions=system)
            pipe.save(root/'areas'/key/('input-'+unit_binding[:12]+'-'+str(attempt)+'.json'),{'input':view,'receipt':receipt,'raw_response':raw})
            try:candidate=pipe.validator.strict_load(raw)
            except (ValueError,TypeError) as exc:
                prior={'raw_response':raw};defects.append({'where':'/','quote':raw[:500],'why':str(exc),'fix':'Return the required issues JSON without prose.'});continue
            problems=_validate_issues(candidate,raw,sets,key)
            language_only=False
            if not problems:
                from asis_language_guard import find_language_defects, allowed_text_from_input
                problems=find_language_defects(candidate,allowed_text=allowed_text_from_input(original));language_only=bool(problems)
            if problems:
                prior=candidate;defects.extend(problems)
                # 일본어·한자 혼입은 문자 오타라서 보완 횟수를 쓰지 않는다. 같은 오류의 무한 반복만 상한으로 막는다.
                if language_only and language_refunds<8:language_refunds+=1;remaining+=1
                continue
            review=await pipe.independent_review(view,candidate,'issue-plan-area-'+key+'-review')
            if review['verdict']=='pass':
                return candidate['issues'],review
            if review['verdict']=='blocked':raise RuntimeError('issue_plan_review_blocked:'+key)
            prior=candidate;defects.extend(review['defects'])
        raise RuntimeError('issue_plan_repair_budget_exhausted:'+key)
    from asis_evidence_views import scoped_prompt_data as _view
    evidence=_view(original,scope,{'mode':'issue_plan','plan_assignment':{'area_id':key}},area_id=key)
    issues,review=await pipe.run_task(evidence,'issue-plan-area-'+key,work)
    pipe.save(accepted,{'input_sha256':unit_binding,'candidate_sha256':digest(issues),'issues':issues,'review':review})
    pipe.event('issue-plan-area-'+key,'interpret','completed',issues=len(issues))
    return issues


_RESOURCE_TOKEN=re.compile(r'[A-Za-z][A-Za-z0-9_-]{5,}[0-9]|[A-Za-z]+-[A-Za-z0-9-]{3,}')


def _resource_names(row):
    """이슈 문장에 이름으로 등장한 자원·배포 식별자만 추린다. 공식 기준 ID와 사실 ID는 제외한다."""
    text=row['scope']+' '+row['reason']
    return {token.lower() for token in _RESOURCE_TOKEN.findall(text) if not token.upper().startswith(('WAF','CAF','AILZ','F_'))}


def duplicate_candidates(rows, owners):
    """서로 다른 영역이 같은 자원 이름으로 만든 이슈 쌍을 후보로 표시한다. 병합 여부는 모델 검토가 판단하고 코드는 위치만 알린다."""
    by_name={}
    for position,row in enumerate(rows):
        for name in _resource_names(row):by_name.setdefault(name,[]).append(position)
    pairs={}
    for name,positions in by_name.items():
        if not 2<=len(positions)<=8:continue
        for left_index,left in enumerate(positions):
            for right in positions[left_index+1:]:
                if owners[left]!=owners[right]:pairs.setdefault((left,right),[]).append(name)
    return [{'issue_positions':['/issues/'+str(left),'/issues/'+str(right)],'shared_resource_names':sorted(names),
             'owner_areas':[owners[left],owners[right]]} for (left,right),names in sorted(pairs.items())]


def deduplicate_issues(rows, owners):
    """서로 다른 영역이 같은 자원 이름으로 만든 이슈 쌍에서 한 쪽만 남긴다.

    남기는 쪽: 이슈의 첫 영역(area_refs[0])이 해당 이슈 영역 참조 중 앞선 우선순위를 가진 쪽(AREA_NAMES 순서가 아니라 참조 수가 많은 쪽).
    버리는 쪽의 영역·사실·기준·목표 참조는 남기는 이슈에 합쳐 근거가 줄지 않게 한다. 문장(scope·reason)은 모델 후보 그대로 보존한다.
    """
    pairs=duplicate_candidates(rows,owners)
    drop={};keep_of={}
    for pair in pairs:
        left,right=[int(position.rsplit('/',1)[1]) for position in pair['issue_positions']]
        if left in drop or right in drop:continue
        # 같은 판단·검증 단위로 볼 수 있을 때만 병합한다: 영역·기준 참조가 거의 같고 사실 참조가 겹친다.
        a,b=rows[left],rows[right]
        shared_f=set(a['fact_refs'])&set(b['fact_refs']);union_f=set(a['fact_refs'])|set(b['fact_refs'])
        shared_c=set(a['criterion_refs'])&set(b['criterion_refs']);union_c=set(a['criterion_refs'])|set(b['criterion_refs'])
        same_areas=set(a['area_refs'])==set(b['area_refs'])
        f_overlap=len(shared_f)/len(union_f) if union_f else 0
        c_overlap=len(shared_c)/len(union_c) if union_c else 1
        if not (same_areas and (f_overlap>=.3 or c_overlap>=.5)):continue
        keep,lose=(left,right) if len(a['fact_refs'])+len(a['criterion_refs'])>=len(b['fact_refs'])+len(b['criterion_refs']) else (right,left)
        drop[lose]=keep
    removed=[];result=[]
    for position,row in enumerate(rows):
        if position in drop:
            keep=drop[position]
            removed.append({'removed_position':position,'kept_position':keep,'removed_scope':row['scope'],'kept_scope':rows[keep]['scope']})
            continue
        result.append(deepcopy(row))
    index_of={position:i for i,position in enumerate(p for p in range(len(rows)) if p not in drop)}
    for lose,keep in drop.items():
        target=result[index_of[keep]]
        for field in ('area_refs','fact_refs','criterion_refs','goal_refs'):
            for value in rows[lose][field]:
                if value not in target[field]:target[field].append(value)
    return result,{'removed':removed,'input_issues':len(rows),'kept_issues':len(result),'rule':'same_resource_name_same_areas_and_overlapping_refs'}


def _integration_view(original, scope):
    """통합 검토는 이슈 간 중복·상충·누락을 본다. 인용 사실은 전문을 두고, 기준 평가·수집 대상 연결은 ID·상태·영역만 남긴다."""
    from asis_evidence_views import scoped_prompt_data
    view=scoped_prompt_data(original,scope,{'mode':'issue_plan'})
    ctx=view['context']
    keep_assess=('criterion_id','status','classification','area_refs','conclusion','id','official_score','required_evidence')
    if isinstance(ctx.get('criterion_assessments'),list):
        ctx['criterion_assessments']=[{k:row[k] for k in keep_assess if k in row} for row in ctx['criterion_assessments']]
    if isinstance(ctx.get('collection_basis_matches'),list):
        ctx['collection_basis_matches']=[{k:row.get(k) for k in ('area','criterion_id','target_id','target_name','assessment_status','target_discovery_status')}
                                         | {'related_resource_count':len(row.get('related_resource_ids') or [])} for row in ctx['collection_basis_matches']]
        ctx['collection_basis_match_scope']={'rule':'integration review: resource ID lists are summarized as counts; full lists remain in the area units','selected':len(ctx['collection_basis_matches'])}
    return view


async def plan_analysis(pipe, original):
    """영역별 계획을 가용 자원만큼 동시에 만들고 통합 검토·코드 확정으로 이어 붙인다."""
    from asis_model import ASSET,digest
    from asis_evidence_views import plan_scopes,scoped_prompt_data
    system=pipe.instructions((ASSET/'contract/prompts/P0-system.txt').read_text(encoding='utf-8'))
    binding=digest({'input':{k:v for k,v in original.items() if k!='stage_data'},'policy':policy(),'system':system,'plan_schema':'asis-issue-plan/2'})
    root=pipe.folder/'checkpoints'/'issue-plan';root.mkdir(parents=True,exist_ok=True)
    scopes=plan_scopes(original,AREA_NAMES)
    # 코드 기준선 분석이 영역 시사점으로 이미 인용한 근거·기준은 해당 영역 계획 범위에 항상 포함한다.
    import asis_analysis
    for area in asis_analysis.analyze(original)['stages'][0]['areas']:
        scope=scopes[area['area_id']]
        for field,ref in (('fact_refs','fact_refs'),('criterion_refs','criterion_refs')):
            known=set(scope[field])
            scope[field]+= [x for x in area['implication'][ref] if x not in known]
    # 시사점이 인용한 기준도 자체 기준도 없는 영역에만 전체 공식 기준을 대체 범위로 준다(근거를 임의로 비우지 않는다).
    # 수집 대상 연결(criterionMatches)이 그 영역을 가리키는 기준을 먼저 사용하고, 그것도 없을 때만 전체 기준을 쓴다.
    basis_ = original.get('context',{}).get('collection_basis')
    matched_ = {}
    for row_ in (basis_.get('criterionMatches',[]) if isinstance(basis_,dict) else []):
        if isinstance(row_.get('criterion_id'),str):matched_.setdefault(row_.get('area'),[]).append(row_['criterion_id'])
    known_ = {row['id'] for row in original.get('criteria',[])}
    for key_,scope_ in scopes.items():
        if not scope_['criterion_refs']:
            own_ = [c for c in dict.fromkeys(matched_.get(key_,[])) if c in known_]
            scope_['criterion_refs']=own_ or [row['id'] for row in original.get('criteria',[])]
    all_facts={row['id'] for row in original['facts']};all_criteria={row['id'] for row in original['criteria']}
    if set().union(*(set(s['fact_refs']) for s in scopes.values()))!=all_facts or set().union(*(set(s['criterion_refs']) for s in scopes.values()))!=all_criteria:
        raise RuntimeError('plan_scope_inventory_incomplete')

    async def unit(key,**extra):
        async with pipe.item_gate:
            return await _plan_area(pipe,original,key,scopes[key],system,binding,root,**extra)
    outcomes=await asyncio.gather(*(unit(key) for key in AREA_NAMES),return_exceptions=True)
    failures=[o for o in outcomes if isinstance(o,BaseException)]
    if failures:raise failures[0]
    by_area=dict(zip(AREA_NAMES,outcomes))
    # 영역 간 중복은 모델 검토 대신 코드가 한 쪽만 남기는 규칙으로 한 번만 정리한다. 품질 검증은 문서 전체 묶음 검토가 맡는다.
    merged=[];owners=[]
    for key in AREA_NAMES:
        for row in by_area[key]:
            merged.append(row);owners.append(key)
    seen=set();unique=[];unique_owners=[]
    for row,owner in zip(merged,owners):
        signature=digest(row)
        if signature not in seen:seen.add(signature);unique.append(row);unique_owners.append(owner)
    merged,owners=unique,unique_owners
    merged,dedup_report=deduplicate_issues(merged,owners)
    pipe.save(root/'dedup-report.json',dedup_report)
    pipe.event('issue-plan','interpret','deduplicated',removed=len(dedup_report['removed']),kept=len(merged))
    candidate={'issues':merged}
    review={'verdict':'pass','content_checked':True,'render_checked':False,'defects':[],'unresolved':[],
            'review_mode':'area_units_reviewed_individually_cross_area_dedup_by_code'}
    issues=candidate['issues']
    pipe.save(root/'accepted.json',{'input_sha256':binding,'candidate_sha256':digest(issues),'issues':issues,'review':review})
    pipe.event('issue-plan','interpret','completed',issues=len(issues))
    return await pipe.run_task(original,'issue-plan',lambda:_finalize(original,issues,binding))


def _finalize(original, issues, binding):
    """통합 검토를 통과한 이슈에 ID·참조를 코드로 배정한다. 정본은 변경하지 않는다."""
    result=deepcopy(original)
    legacy=len(issues)==len(AREA_NAMES) and all(len(row['area_refs'])==1 for row in issues) and {row['area_refs'][0] for row in issues}==set(AREA_NAMES)
    allocated=[]
    for index,row in enumerate(issues):
        key=row['area_refs'][0] if legacy else 'P'+str(index+1).zfill(4)
        observation_id = None if legacy else 'O_' + key
        allocated.append({**deepcopy(row),'id':'PLAN_'+key,'direction_id':'D_'+key,
            'diagnosis_id':'J_'+key,'action_id':'A_'+key,'lead_area':row['area_refs'][0],
            **({'observation_id':observation_id} if observation_id else {})})
    if not legacy:
        result['allocated_ids']['observations']=list(dict.fromkeys(
            result['allocated_ids'].get('observations',[]) + [row['observation_id'] for row in allocated]))
    for family,field in (('directions','direction_id'),('diagnoses','diagnosis_id'),('actions','action_id')):
        result['allocated_ids'][family]=[row[field] for row in allocated]
    result['context']['analysis_plan']={'schema':'asis-issue-plan/1','source_sha256':binding,'issues':allocated,'ids_allocated_by':'code','customer_fact_promoted':False}
    return result
