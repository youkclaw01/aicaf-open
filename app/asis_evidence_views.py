"""수집 전수 원장을 분류하고 비용·메트릭의 의미를 보존한 분석용 뷰를 만든다."""
from __future__ import annotations
from collections import Counter
from copy import deepcopy
from decimal import Decimal
import hashlib
import json

from asis_model import digest, encoded

AREA_WORDS = {
    'network': ('network', 'subnet', 'dns', 'route', 'nat', 'gateway', 'firewall', 'endpoint', 'waf'),
    'compute': ('compute', 'virtualmachine', 'vmss', 'container', 'recovery', 'backup', 'autoscale', 'reliability'),
    'data': ('storage', 'data', 'vault', 'mysql', 'sql'),
    'ai': ('foundry', 'ai', 'machinelearning', 'aml', 'quota'),
    'secgov': ('rbac', 'policy', 'defender', 'security', 'identity', 'tenant', 'managementgroup', 'compliance'),
    'telemetry': ('metric', 'monitor', 'diagnostic', 'alert', 'insights'),
    'operating': ('shutdown', 'advisor', 'tags'),
    'cost': ('cost', 'budget', 'price'),
}


def areas_for(key):
    lower=key.casefold()
    found=[area for area,words in AREA_WORDS.items() if any(word in lower for word in words)]
    return found or ['operating']


def datasets(data):
    """원천 배열마다 고유 경로·상태·행수·해시를 등록한다. 별도 필드를 버리지 않는다."""
    docs=data['context'].get('source_documents',{})
    by_pointer={(f['source_id'],f['field_path']):f for f in data['facts']}
    result=[]
    def add(source,pointer,value,key,metadata=None):
        fact=by_pointer.get((source,pointer))
        result.append({'id':'DS_'+digest((source,pointer))[:16], 'source_id':source,'pointer':pointer,
            'area_refs':areas_for(key), 'observed_records':len(value) if isinstance(value,list) else None,
            'source_status':(metadata or {}).get('status','collected'),
            'collection_status':fact['collection_status'] if fact else 'collected',
            'scope_complete':fact['scope_complete'] if fact else False,
            'value_sha256':digest(value), 'fact_id':fact['id'] if fact else None,
            'metadata':deepcopy(metadata or {})})
    topo=docs.get('topology',{})
    for key in ('resources','relationships'):
        if key in topo:add('topology.json','/'+key,topo[key],key)
    for key,value in topo.get('configurations',{}).items():
        add('topology.json','/configurations/'+key,value,key,topo.get('coverage',{}).get('detailDatasets',{}).get(key,{}))
    for key,value in docs.get('evidence',{}).get('sections',{}).items():
        if isinstance(value,dict):
            # 상태·기간·오류는 등록 원장과 원문에 각각 보존한다.
            add('aicaf-evidence.json','/sections/'+key+'/records',value.get('records'),key,
                {k:deepcopy(v) for k,v in value.items() if k!='records'})
    return result


def _fact(data,key,value,pointer,refs,complete=False):
    identifier='F_D_'+key
    if any(x['id']==identifier for x in data['facts']):return identifier
    data['facts'].append({'id':identifier,'source_id':'derived_from_source','field_path':pointer,
        'run':deepcopy(data['run']),'value':value,'collection_status':'collected','scope_complete':complete})
    data['context'].setdefault('derived_fact_register',[]).append({'fact_id':identifier,'source_fact_refs':refs,
        'calculation':'decimal_sum_or_source_count','value_sha256':digest(value),'source_scope_complete':complete})
    return identifier


def enrich(original):
    """동일 입력의 원문을 보존하고 결정적 집계만 파생 사실로 추가한다."""
    data=deepcopy(original)
    ctx=data['context']
    if ctx.get('evidence_view_revision') == '1':
        return data
    ctx['evidence_view_revision'] = '1'
    docs=ctx.get('source_documents',{})
    topo=docs.get('topology',{});evidence=docs.get('evidence',{})
    ctx['dataset_register']=datasets(data)
    resources=topo.get('resources',[])
    counts=[{'type':key,'count':count} for key,count in sorted(Counter(r.get('type','unknown') for r in resources if isinstance(r,dict)).items())]
    ctx['inventory_summary']=counts
    if counts:
        fid=_fact(data,'inventory_by_type',counts,'/inventory_summary',['F_T_resources'],
            topo.get('verification',{}).get('resourceCount',{}).get('complete') is True)
        for i,row in enumerate(counts):
            data['measurements'].append({'id':'M_D_inventory_'+str(i+1),'value':row['count'],'unit':'resources:'+row['type'],'fact_refs':[fid]})
    costs=[];groups={}
    for row in evidence.get('sections',{}).get('costMonthlyTotals',{}).get('records',[]):
        if not isinstance(row,dict) or not isinstance(row.get('Cost'),(int,float)) or isinstance(row.get('Cost'),bool):continue
        key=(str(row.get('month','')),str(row.get('costType','unknown')),str(row.get('Currency','unknown')))
        groups.setdefault(key,[]).append(Decimal(str(row['Cost'])))
    for (period,basis,currency),values in sorted(groups.items()):
        costs.append({'period':period,'basis':basis,'currency':currency,'amount':float(sum(values,Decimal(0))),
                      'source_rows':len(values),'scope':'subscription','amount_class':'observed_cost_query',
                      'from_utc':evidence.get('costPeriod',{}).get('fromUtc'),'to_utc':evidence.get('costPeriod',{}).get('toUtc')})
    ctx['cost_summary']=costs
    if costs:
        fid=_fact(data,'cost_monthly',costs,'/cost_summary',['F_E_costMonthlyTotals'])
        for i,row in enumerate(costs):
            data['measurements'].append({'id':'M_D_cost_'+str(i+1),'value':row['amount'],
                'unit':row['currency']+':'+row['basis']+':'+row['period'],'fact_refs':[fid]})
    metrics={}
    for section in ('aiMetricsTokensDaily','aiMetricsRequestsByStatus','aiMetricsLatency','aiMetricsPtuUtilization'):
        for row in evidence.get('sections',{}).get(section,{}).get('records',[]):
            if not isinstance(row,dict) or not isinstance(row.get('total'),(int,float)) or isinstance(row.get('total'),bool):continue
            key=(str(row.get('resourceId','')).casefold(),str(row.get('metric','')),encoded(row.get('dimensions') or {}).decode('utf-8'))
            entry=metrics.setdefault(key,{'resource_id':row.get('resourceId',''),'metric':row.get('metric',''),
                'dimensions':deepcopy(row.get('dimensions') or {}),'times':[],'values':[],'source_section':section})
            entry['times'].append(str(row.get('time') or ''));entry['values'].append(Decimal(str(row['total'])))
    metric_rows=[]
    for key,value in sorted(metrics.items()):
        total=float(sum(value['values'],Decimal(0)))
        metric_rows.append({k:v for k,v in value.items() if k not in ('times','values')}|
            {'points':len(value['values']),'total':int(total) if total.is_integer() else total,
             'observed_from':min(value['times']),'observed_to':max(value['times']),
             'aggregation':'sum_of_non_null_returned_total','business_success_verified':False})
    ctx['metric_summary']=metric_rows
    if metric_rows:
        source_refs=list(dict.fromkeys('F_E_'+r['source_section'] for r in metric_rows))
        fid=_fact(data,'metrics_by_dimension',metric_rows,'/metric_summary',source_refs)
        for i,row in enumerate(metric_rows):
            data['measurements'].append({'id':'M_D_metric_'+str(i+1),'value':row['total'],
                'unit':row['metric']+':observed_points_only','fact_refs':[fid]})
    ctx['analysis_limits']=[
        '인벤토리 총수 일치는 모든 기능·실효 경로·통신·업무 품질의 검증이 아니다.',
        'ActualCost와 AmortizedCost는 별도 기준이며 합산하지 않는다. 기간 중간 값은 완료 월과 구분한다.',
        '메트릭은 반환된 비어 있지 않은 관측점만 집계하며 관측 공백은 0으로 변환하지 않는다. 다른 메트릭을 합산하지 않는다.',
        'HTTP 200은 플랫폼 응답이며 유효 업무 완료·품질·경제적 효과의 증거가 아니다.',
        'Azure 외 서비스 비용·인력·도입비·채택률·시간 가치·현금 절감은 미확인이다.',
    ]
    return data


def prompt_view(original):
    """원문 바이트는 원장에 보존하고 중복 원문·대량 가격행은 모델 뷰에서 분리한다."""
    source_context=original.get('context',{})
    scope=source_context.get('model_scope',{})
    # 제외할 원문과 관련 없는 사실은 복제하기 전에 분리한다.
    prepared={key:value for key,value in original.items() if key!='context'}
    prepared['context']={key:value for key,value in source_context.items()
                         if key not in ('source_documents','collection_basis_workbook','collection_basis',
                                        'interview_snapshot','prior_current_state_analysis')}
    if scope.get('projection_schema')=='asis-model-scope/1':
        for family,field in (('facts','fact_refs'),('criteria','criterion_refs'),('goals','goal_refs')):
            refs=scope.get(field)
            allowed={row['id'] for row in original.get(family,[])}
            if not isinstance(refs,list) or not all(isinstance(ref,str) and ref in allowed for ref in refs):
                raise ValueError('invalid_model_scope_reference:'+field)
            selected=set(refs)
            prepared[family]=[row for row in original.get(family,[]) if row['id'] in selected]
        fact_ids=set(scope['fact_refs']);criterion_ids=set(scope['criterion_refs']);goal_ids=set(scope['goal_refs'])
        for family in ('measurements','judgments'):
            if family in original:
                prepared[family]=[row for row in original[family]
                    if set(row.get('fact_refs',[]))<=fact_ids and set(row.get('criterion_refs',[]))<=criterion_ids]
        ctx=prepared['context']
        for name in ('dataset_register','dataset_index','derived_fact_register'):
            if isinstance(source_context.get(name),list):
                ctx[name]=[row for row in source_context[name] if row.get('fact_id') in fact_ids]
        if isinstance(source_context.get('criterion_assessments'),list):
            ctx['criterion_assessments']=[row for row in source_context['criterion_assessments']
                                         if row.get('criterion_id') in criterion_ids]
        if isinstance(source_context.get('goal_interpretations'),list):
            ctx['goal_interpretations']=[row for row in source_context['goal_interpretations'] if row.get('goal_id') in goal_ids]
    data=deepcopy(prepared);context=data.get('context',{})
    basis=source_context.get('collection_basis')
    if isinstance(basis,dict):
        # 대상 연결은 그대로 전달하지만 기초 원장·Excel 전송값은 모델에 중복 삽입하지 않는다.
        matches=basis.get('criterionMatches',[])
        # 영역 단위 호출은 그 영역의 대상 연결과 해당 영역이 인용한 기준의 연결만 전달한다. 정본 원장은 변경하지 않는다.
        if scope.get('projection_schema')=='asis-model-scope/1' and scope.get('area_id'):
            area_id=scope['area_id'];allowed_criteria=set(scope.get('criterion_refs',[]))
            all_criteria={row['id'] for row in original.get('criteria',[])}
            own=[row for row in matches if row.get('area')==area_id]
            cited=[row for row in matches if row.get('area')!=area_id and row.get('criterion_id') in allowed_criteria]
            # 범위 기준이 전체 기준과 같다면(영역 자체 기준이 없는 경우) 다른 영역 행은 제외해 영역 단위 호출을 작게 유지한다.
            matches=own if (all_criteria and allowed_criteria>=all_criteria) else own+cited
            context['collection_basis_match_scope']={'area_id':area_id,'selected':len(matches),'total':len(basis.get('criterionMatches',[])),
                'rule':'rows of this area or of criteria cited in this unit; other areas are handled by their own units'}
        # 영역 호출은 대상 연결의 판정 상태와 건수를 쓴다. 자원 ID 전체 목록은 정본 원장·Excel에 그대로 있고 호출마다 반복하지 않는다.
        if scope.get('projection_schema')=='asis-model-scope/1' and scope.get('area_id'):
            matches=[{**{k:v for k,v in row.items() if k!='related_resource_ids'},
                      'related_resource_count':len(row.get('related_resource_ids') or []),
                      'related_resource_ids_sample':list((row.get('related_resource_ids') or [])[:3])} for row in matches]
            context['collection_basis_match_scope']['related_resource_ids']='counted per row with a 3-id sample; full lists remain in the canonical ledger and Excel'
        context['collection_basis_matches']=deepcopy(matches)
        context['collection_basis_source_artifacts']=deepcopy(basis.get('source_artifacts',{}))
        context['collection_basis_display_policy']='Script matching is target discovery only, not criterion compliance. Full inventory and collector workbook remain unchanged in the canonical source.'
    # 공식 원문은 criteria, 수집 상태는 facts에 이미 있다. 동일 내용을 각 기준마다 중복하지 않는다.
    duplicate_keys={'quote','source_url','source_snapshot_sha256','evidence_states','checked_at','modality'}
    if isinstance(context.get('criterion_assessments'),list):
        context['criterion_assessments']=[{k:deepcopy(v) for k,v in row.items() if k not in duplicate_keys}
            for row in context['criterion_assessments']]
    for name in ('dataset_register','dataset_index'):
        if isinstance(context.get(name),list):
            context[name]=[{k:deepcopy(v) for k,v in row.items() if k!='metadata'} for row in context[name]]
    context['evidence_display_policy']='Raw source documents are stored unchanged in the canonical ledger; declared bulk fact values are not inlined. IDs, source paths, status, hashes and derived exact aggregates are supplied. Never infer omitted raw properties.'
    bulk=[]
    for fact in data.get('facts',[]):
        relevant=context.get('model_scope',{}).get('inline_fact_refs',[])
        if fact['id'].startswith(('F_E_prices','F_T_relationships')) or (len(encoded(fact.get('value')))>45000 and fact['id'] not in relevant):
            bulk.append({'id':fact['id'],'source_id':fact['source_id'],'pointer':fact['field_path'],
                         'value_sha256':digest(fact['value']),'record_count':len(fact['value']) if isinstance(fact['value'],list) else None})
            # 모델 뷰 전용 선언과 정본 입력의 실제 사실값은 명시적으로 구분한다.
            fact['value']={'not_inlined':True,'canonical_value_sha256':digest(fact['value']),
                           'observed_record_count':len(fact['value']) if isinstance(fact['value'],list) else None}
    context['not_inlined_fact_register']=bulk
    return data


def plan_scopes(original, area_keys):
    """영역별 계획 단위가 받을 사실·기준 범위를 코드로 확정한다. 정본은 변경하지 않는다.

    등록 원장이 영역을 지정한 항목은 해당 영역에만, 지정이 없는 항목은 모든 영역에 공통으로 전달한다.
    모든 항목은 최소 한 영역에 배정되므로 전수 범위는 영역 단위의 합집합과 같다.
    """
    keys=list(area_keys)
    context=original.get('context',{})
    fact_areas={};criterion_areas={}
    for name in ('dataset_register','dataset_index','derived_fact_register'):
        for row in context.get(name,[]) if isinstance(context.get(name),list) else []:
            fid=row.get('fact_id')
            if isinstance(fid,str):
                fact_areas.setdefault(fid,set()).update(a for a in row.get('area_refs',[]) if a in keys)
    for row in context.get('criterion_assessments',[]) if isinstance(context.get('criterion_assessments'),list) else []:
        cid=row.get('criterion_id')
        if isinstance(cid,str):
            criterion_areas.setdefault(cid,set()).update(a for a in row.get('area_refs',[]) if a in keys)
    # 수집 대상 연결은 기준을 영역 하나에 연결한다. 기준 평가에 영역이 없는 기준의 배정 근거로 쓴다.
    basis=context.get('collection_basis')
    for row in (basis.get('criterionMatches',[]) if isinstance(basis,dict) else []):
        cid=row.get('criterion_id');area=row.get('area')
        if isinstance(cid,str) and area in keys and not criterion_areas.get(cid):
            criterion_areas.setdefault(cid,set()).add(area)
    goal_ids=[row['id'] for row in original.get('goals',[])]
    scopes={}
    for key in keys:
        scopes[key]={
            'fact_refs':[row['id'] for row in original.get('facts',[])
                         if not fact_areas.get(row['id']) or key in fact_areas[row['id']]],
            'criterion_refs':[row['id'] for row in original.get('criteria',[])
                              if not criterion_areas.get(row['id']) or key in criterion_areas[row['id']]],
            'goal_refs':list(goal_ids)}
    # 자체 기준이 없는 영역의 대체 범위는 호출자가 정한다. 전체 기준을 일괄 전달하면 입력과 응답이 비대해진다.
    return scopes


def scoped_prompt_data(original, scope, stage_data, *, area_id=None):
    """정본 전체를 복제하지 않고 명시 범위의 모델 뷰만 만든다."""
    shell={key:value for key,value in original.items() if key not in ('context','stage_data')}
    context=dict(original.get('context',{}))
    context['model_scope']={'projection_schema':'asis-model-scope/1',**({'area_id':area_id} if area_id else {}),
                            'fact_refs':list(scope['fact_refs']),'criterion_refs':list(scope['criterion_refs']),
                            'goal_refs':list(scope['goal_refs']),'inline_fact_refs':[]}
    shell['context']=context
    shell['stage_data']=dict(stage_data)
    return prompt_view(shell)
