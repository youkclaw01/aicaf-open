"""승인된 현황분석을 핵심 PPT와 전체 Excel 원장으로 분리하는 편집 계약."""
from copy import deepcopy
import json
import re
from typing import Any

from asis_model import digest

SCHEMA='assessment-current-state-editorial/1'
MAX_SLIDES=150


def source_analysis(result):
    """표현·렌더 결과와 무관한 입력·분석 정본에만 편집 결과를 결합한다."""
    return {key:deepcopy(result[key]) for key in ('schema','input','stages')}


def editorial_input(result,area_id,*,slide_budget):
    """원천 인벤토리 전체를 중복 전송하지 않고 담당 영역의 승인 원문 전부를 보낸다."""
    if type(slide_budget) is not int or slide_budget<1 or slide_budget>MAX_SLIDES:
        raise ValueError('current_editorial_invalid_slide_budget')
    areas=[a for a in result['stages'][0]['areas'] if a['area_id']==area_id]
    if len(areas)!=1:raise ValueError('current_editorial_unknown_area')
    return {'task':'current_state_editorial','schema':SCHEMA,
        'source_analysis_sha256':digest(source_analysis(result)),
        'area_source':deepcopy(areas[0]),'slide_budget':slide_budget,
        'revision_context':deepcopy(result.get('editorial_revision_context',{}).get(area_id,[])),
        'detail_policy':'all_original_observations_and_unresolved_remain_in_excel',
        'source_unresolved_count':len(result['stages'][0].get('unresolved',[]))}


def group_blocks(group):
    """본문에 표시할 핵심 현황·기술적 의미·해당 판단 한계를 순서대로 반환한다."""
    return [(title,group[key]) for title,key in (('핵심 현황','current_state'),
        ('기술적 의미','technical_meaning'),('판단 한계','limitation')) if group.get(key)]


def _display_text(value,name,*,empty=False):
    if not isinstance(value,str) or (not empty and not value.strip()):
        raise ValueError('current_editorial_missing_text:'+name)
    if re.search(r'[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]',value):
        raise ValueError('current_editorial_forbidden_language:'+name)
    return value.strip()


def validate_area(view,draft):
    """관찰의 전수 귀속·원문 인용·코드 소유 근거·실제 배치 용량을 검사한다."""
    from asis_artifacts import paragraphs_pages,wrapped_lines
    area=view['area_source'];observations={o['id']:o for o in area['observations']}
    if not isinstance(draft,dict) or draft.get('area_id')!=area['area_id']:
        raise ValueError('current_editorial_area_binding_failed')
    if set(draft)!={'area_id','headline','groups','appendix_only'}:
        raise ValueError('current_editorial_area_shape_invalid')
    out={'area_id':area['area_id'],'headline':_display_text(draft.get('headline'),'headline'),
         'groups':[],'appendix_only':[]}
    if len(wrapped_lines(out['headline'],11.96,13.5))>2:
        raise ValueError('current_editorial_headline_over_capacity')
    groups=draft.get('groups');appendix=draft.get('appendix_only')
    if not isinstance(groups,list) or not isinstance(appendix,list):
        raise ValueError('current_editorial_coverage_not_lists')
    if len(groups)>view['slide_budget'] or (observations and not groups):
        raise ValueError('current_editorial_group_budget_exceeded')
    covered=[]
    for ordinal,group in enumerate(groups):
        if not isinstance(group,dict) or set(group)!={'title','current_state','technical_meaning','limitation','observation_refs','source_quotes'}:
            raise ValueError('current_editorial_group_shape_invalid')
        kept:dict[str,Any]={name:_display_text(group.get(name),name,empty=name=='limitation')
              for name in ('title','current_state','technical_meaning','limitation')}
        if len(wrapped_lines(kept['title'],11.96,27))>1:
            raise ValueError('current_editorial_title_over_capacity')
        refs=group.get('observation_refs')
        if not isinstance(refs,list) or not refs or any(not isinstance(r,str) or r not in observations for r in refs):
            raise ValueError('current_editorial_unknown_observation')
        if len(refs)!=len(set(refs)):raise ValueError('current_editorial_duplicate_observation')
        quotes=group.get('source_quotes');quoted=set()
        if not isinstance(quotes,list) or not quotes:raise ValueError('current_editorial_missing_source_quotes')
        for quote in quotes:
            if not isinstance(quote,dict) or set(quote)!={'observation_ref','quote'}:
                raise ValueError('current_editorial_source_quote_shape_invalid')
            reference=quote['observation_ref'];text=quote['quote']
            if reference not in refs or not isinstance(text,str) or not text.strip() or text not in observations[reference]['text']:
                raise ValueError('current_editorial_source_quote_not_found')
            quoted.add(reference)
        if quoted!=set(refs):raise ValueError('current_editorial_unquoted_observation')
        source_text='\n'.join(observations[r]['text'] for r in refs)+'\n'+json.dumps(area['implication'],ensure_ascii=False)+'\n'+area['headline']
        numbers=lambda s:set(re.findall(r'\d+(?:\.\d+)?',s.replace(',','')))
        if not numbers('\n'.join(kept.values()))<=numbers(source_text):
            raise ValueError('current_editorial_new_numeric_claim')
        if len(paragraphs_pages(group_blocks(kept),width=11.96,pt=13.5,max_lines=17))!=1:
            raise ValueError('current_editorial_group_over_layout_capacity')
        kept.update(observation_refs=deepcopy(refs),source_quotes=deepcopy(quotes),
            fact_refs=sorted({f for r in refs for f in observations[r]['fact_refs']}),
            criterion_refs=sorted({c for r in refs for c in observations[r]['criterion_refs']}))
        out['groups'].append(kept);covered.extend(refs)
    for item in appendix:
        if not isinstance(item,dict) or set(item)!={'observation_ref','reason'} or item['observation_ref'] not in observations:
            raise ValueError('current_editorial_invalid_appendix_reference')
        out['appendix_only'].append({'observation_ref':item['observation_ref'],
            'reason':_display_text(item.get('reason'),'appendix_reason')})
        covered.append(item['observation_ref'])
    if len(covered)!=len(set(covered)):raise ValueError('current_editorial_duplicate_coverage')
    if set(covered)!=set(observations):raise ValueError('current_editorial_incomplete_coverage')
    return out


def model_draft(candidate):
    """코드가 합친 근거 ID를 제외한 실제 모델 작성 계약만 복원한다."""
    value=deepcopy(candidate)
    for group in value['groups']:
        group.pop('fact_refs',None);group.pop('criterion_refs',None)
    return value


def bind_projection(result,areas,reviews):
    """전 영역 검토 통과 후에만 동일 분석 원문의 보고서 표현을 확정한다."""
    expected=[a['area_id'] for a in result['stages'][0]['areas']]
    if [a.get('area_id') for a in areas]!=expected or set(reviews)!=set(expected):
        raise ValueError('current_editorial_area_coverage_failed')
    for area in areas:
        review=reviews[area['area_id']]
        if (review.get('verdict')!='pass' or review.get('content_checked') is not True
                or review.get('render_checked') is not False or review.get('defects')
                or review.get('candidate_sha256')!=digest(area)):
            raise ValueError('current_editorial_review_not_passed')
    return {'schema':SCHEMA,'source_analysis_sha256':digest(source_analysis(result)),
        'maximum_slides':MAX_SLIDES,'areas':deepcopy(areas),'reviews':deepcopy(reviews),
        'detail_policy':'complete_excel_register','selection_authored_by_model':True}


def validate_projection(result):
    """새 보고서·재개·다운로드 검증에서 정본·선정·검토 바인딩을 다시 검사한다."""
    value=result.get('presentation')
    if not isinstance(value,dict) or value.get('schema')!=SCHEMA or value.get('maximum_slides')!=MAX_SLIDES:
        raise ValueError('current_editorial_projection_missing')
    if value.get('source_analysis_sha256')!=digest(source_analysis(result)):
        raise ValueError('current_editorial_source_analysis_changed')
    normalized=[]
    for area in value['areas']:
        view=editorial_input(result,area['area_id'],slide_budget=MAX_SLIDES)
        checked=validate_area(view,model_draft(area))
        if checked!=area:raise ValueError('current_editorial_code_owned_references_changed')
        normalized.append(checked)
    if sum(len(a['groups']) for a in normalized)>MAX_SLIDES:
        raise ValueError('current_editorial_slide_maximum_exceeded')
    rebound=bind_projection(result,normalized,value['reviews'])
    if rebound!=value:raise ValueError('current_editorial_projection_binding_failed')
    return value


AUTHOR_INSTRUCTION='''승인된 현황분석 영역 원문 전체를 고객 제안용 현황 PPT의 핵심 내용으로 편집한다.
첫 현황보고서도 최대150장이다. 전체 문장을 장표에 옮기지 않는다. 중요한 현재 상태·근거·기술적 영향이 남도록 편집하고 일반 설명·같은 현상의 반복·인벤토리 상세를 통합한다.
정본 관찰과 미확인은 모두 Excel에 그대로 보존된다. 본문에 필요하지 않은 상세는 appendix_only에 원래 관찰 ID와 납득 가능한 선정 사유를 기록한다.
한 groups 항목은 한 장표다. slide_budget는 상한이지 채워야 할 개수가 아니다. 한 장표에는 하나의 핵심 메시지와 핵심 현황·기술적 의미·필요한 판단 한계를 둔다.
고객의 실제 사용·책임·목표를 만들지 않는다. 미확인을 없음·결함·정상으로 확정하지 않는다. 기술 원문에 없는 사실·숫자·계산·신규 개선과제를 추가하지 않는다.
모든 관찰 ID는 groups.observation_refs 또는 appendix_only 중 정확히 한 곳에 귀속한다. 묶인 각 관찰의 실제 text에서 source_quotes를 한 개 이상 정확하게 인용한다.
headline은 해당 영역의 결론형 핵심 문장이다. title은 결론형 한 문장으로 27포인트 한 줄, headline은13.5포인트 두 줄 이내다.
본문 세 필드는13.5포인트로 전체 약8줄 이내가 되게 명료하게 작성한다. 현재 상태와 의미를 분리하며 limitation은 필요한 경우만 작성하고 없으면 빈 문자열이다.
출처 원문은 source_quotes에 보존하고 본문에는 핵심 해설을 작성한다. 같은 불확실성·일반 원칙을 모든 장표에 반복하지 않는다. 중대한 근거·위험·보안·운영·비용 현황을 일반 상세라는 이유로 숨기지 않는다.
전문 컨설턴트 문체의 한글만 사용한다. 공식 영어 제품명은 유지한다. 감상·의인화·챗봇 안내를 넣지 않는다.
JSON 객체 하나만 반환한다. 키는 area_id,headline,groups,appendix_only다.
groups 항목의 키는 title,current_state,technical_meaning,limitation,observation_refs,source_quotes다.
source_quotes 항목의 키는 observation_ref,quote이며 quote는 해당 관찰 text의 정확한 부분 문자열이다.
appendix_only 항목의 키는 observation_ref,reason이다. 근거 ID·사실·기준 연결·장수 계산은 코드가 담당하므로 다른 키나 완료 선언을 추가하지 않는다.
직전 후보와 모든 지적이 있으면 원문·정상 내용을 유지하고 지적을 일괄 보완한다.'''

REVIEW_INSTRUCTION='''담당 영역의 승인 원문 전체와 핵심 편집 후보 전체를 대조해 독립 검토한다.
원문에서 중요한 현재 상태·근거·영향이 누락되거나 미확인이 확정 사실로 바뀌었는지, 일반·중복 통합이 타당한지, appendix_only 사유가 중요한 관찰을 숨기는지 검사한다.
첫 현황 PPT는 최대150장이고 모든 문장을 담지 않는 편집이 사용자 승인 사항이다. 전체 원문은 Excel에 보존된다. 상세가 본문에 없다는 사실만으로 결함으로 판정하지 않는다.
수치·설정·대상·범위·판단 한계가 원문과 맞고 고객이 핵심 상황을 이해할 수 있어야 한다. 신규 개선과제·실제 인터뷰·새 사실을 만들어서는 안 된다.
한 영역에 대한 모든 지적을 한 번에 반환한다. 단순 취향·근거 없는 가능성·제공하지 않은 고객 사실을 요구하는 지적을 만들지 않는다.
JSON 객체 하나만 반환한다. 키는 verdict,content_checked,render_checked,defects다.
verdict는 pass/revise/blocked, content_checked는 true, render_checked는 false다. 통과는 defects=[]일 때만 허용한다.
각 결함은 where,quote,why,fix다. where는 candidate 안의 실제 문자열 필드 JSON pointer, quote는 그 필드의 정확한 부분 문자열이다.
원문과 판정을 구분하고 한글 전문 문체를 사용한다.'''


def _message(instruction,payload):
    return instruction+'\n입력:\n'+json.dumps(payload,ensure_ascii=False)


def input_measurement(system,message):
    """문자·바이트를 실측한다. 토큰 값은 가능할 때만 추정치로 명시한다."""
    from asis_model_view import MODEL_TOKEN_LIMIT
    text=system+'\n'+message
    measured={'characters':len(text),'utf8_bytes':len(text.encode('utf-8')),
        'model_token_limit':MODEL_TOKEN_LIMIT,'provider_token_count':None,
        'token_count_classification':'provider_not_observed_before_request'}
    try:
        from importlib import import_module
        tokenizer=import_module('tiktoken')
        measured['o200k_estimated_tokens']=len(tokenizer.get_encoding('o200k_base').encode(text,disallowed_special=()))
    except ImportError:
        measured['o200k_estimated_tokens']=None
    if len(text)>900_000:raise ValueError('current_editorial_input_character_limit')
    estimated=measured.get('o200k_estimated_tokens')
    if isinstance(estimated,int) and estimated>MODEL_TOKEN_LIMIT:
        raise ValueError('current_editorial_estimated_token_limit')
    return measured


def editorial_system(pipe):
    """기존 실제 적용 스킬·문체에 첫 보고서의 최신 편집 승인을 명시한다."""
    from asis_artifacts import ASSET
    return pipe.instructions((ASSET/'contract/prompts/P0-system.txt').read_text(encoding='utf-8'))+(
        '\n[첫 현황보고서의 최신 사용자 지시]\n최대150장, 핵심 현황·중요 근거·영향 중심, 일반·중복 내용 통합. '
        '전체 원장은 Excel에 보존한다. 이는 본문 선택·재편집 승인으로, 모든 원문을 PPT에 펼치라는 일반 지침의 예외다. '
        '이 작업은 이미 승인된 현황의 표현 편집이며 인터뷰·최종 종합·새 개선과제 작성이 아니다.')


def allocate_budgets(result,skeleton_pages):
    """실제 부속 장표·영역 수·승인 관찰의 원문 크기에 비례해 가변 상한을 배분한다."""
    from asis_model import encoded
    areas=result['stages'][0]['areas']
    available=MAX_SLIDES-skeleton_pages-len(areas)
    if available<len(areas):raise ValueError('current_editorial_no_content_capacity')
    weights={a['area_id']:max(1,len(encoded(a['observations']))) for a in areas}
    total=sum(weights.values());extra=available-len(areas)
    allocation={key:1+extra*weight//total for key,weight in weights.items()}
    remainder=available-sum(allocation.values())
    ranked=sorted(weights,key=lambda key:(-(extra*weights[key]%total),key))
    for key in ranked[:remainder]:allocation[key]+=1
    return allocation


def _review_contract(candidate,review):
    """검토 통과 선언보다 실제 원문 지적과 후보 결합을 우선 검증한다."""
    if not isinstance(review,dict) or set(review)!={'verdict','content_checked','render_checked','defects'}:
        raise ValueError('current_editorial_review_shape_invalid')
    if review['verdict'] not in ('pass','revise','blocked') or review['content_checked'] is not True or review['render_checked'] is not False:
        raise ValueError('current_editorial_review_incomplete')
    if not isinstance(review['defects'],list) or (review['verdict']=='pass' and review['defects']) or (review['verdict']!='pass' and not review['defects']):
        raise ValueError('current_editorial_review_verdict_conflict')
    for defect in review['defects']:
        if not isinstance(defect,dict) or set(defect)!={'where','quote','why','fix'}:
            raise ValueError('current_editorial_defect_shape_invalid')
        if any(not isinstance(defect[k],str) or not defect[k].strip() for k in defect):
            raise ValueError('current_editorial_empty_defect')
        pointer=defect['where']
        if not pointer.startswith('/'):raise ValueError('current_editorial_invalid_defect_pointer')
        value=candidate
        try:
            for part in pointer[1:].split('/'):
                key=part.replace('~1','/').replace('~0','~')
                value=value[int(key)] if isinstance(value,list) else value[key]
        except (KeyError,ValueError,TypeError,IndexError) as exc:
            raise ValueError('current_editorial_defect_pointer_not_found') from exc
        if not isinstance(value,str) or defect['quote'] not in value:
            raise ValueError('current_editorial_defect_quote_not_found')
    return {**deepcopy(review),'candidate_sha256':digest(candidate)}


def editorial_task_specs(result):
    """본문 편집은 실제 영역 수에 맞는 고정 작성·검토 쌍 작업으로 등록한다."""
    from asis_tasks import task
    return [task('current-editorial-'+a['area_id'],'현황 핵심 편집·독립 검토',executor='model',
        depends_on=['area-'+a['area_id']],pair_key='current-editorial-'+a['area_id']) for a in result['stages'][0]['areas']]


def editorial_defects_by_area(result,defects):
    """전수 시각 검토의 실제 문자열 지적을 해당 편집 쌍에만 귀속한다."""
    mapped={};areas=result['presentation']['areas']
    for defect in defects:
        pointer=defect.get('where','')
        if not pointer.startswith('/presentation/areas/'):continue
        parts=pointer[1:].split('/')
        try:
            index=int(parts[2]);area=areas[index]
            if index<0:raise IndexError('negative_index')
            text=result
            for part in parts:
                key=part.replace('~1','/').replace('~0','~')
                text=text[int(key)] if isinstance(text,list) else text[key]
        except (KeyError,ValueError,TypeError,IndexError) as exc:
            raise ValueError('current_editorial_visual_defect_pointer_not_found') from exc
        literal={key:deepcopy(defect[key]) for key in ('where','quote','why','fix')}
        if not isinstance(text,str) or not isinstance(literal['quote'],str) or not literal['quote'] or literal['quote'] not in text:
            raise ValueError('current_editorial_visual_defect_quote_not_found')
        mapped.setdefault(area['area_id'],[]).append(literal)
    return mapped


async def prepare_editorial(pipe,result):
    """Excel 원문 동결 후 영역별 고정 작성·검토 쌍, 저장·재사용·국소 보완을 수행한다."""
    import asyncio

    from uuid import uuid4
    from asis_model import strict_json
    from asis_runtime import policy
    from assessment_flow_artifacts import freeze_current_state,consume_current_state,presentation_current_state
    base=deepcopy(result);base.pop('presentation',None)
    source_root=pipe.folder/'editorial-sources'/digest(base)
    workbook=source_root/'source-analysis.xlsx';freeze_receipt=source_root/'freeze-receipt.json'
    if not workbook.is_file() or not freeze_receipt.is_file():
        pipe.save(freeze_receipt,freeze_current_state(base,workbook))
    frozen=consume_current_state(workbook,strict_json(freeze_receipt.read_bytes()))
    if frozen!=base:raise ValueError('current_editorial_frozen_source_changed')
    skeleton=await asyncio.to_thread(presentation_current_state,frozen,None,_measure_only=True)
    budgets=allocate_budgets(frozen,skeleton);data=frozen['input'];system=editorial_system(pipe)
    manager=pipe.task_manager(data);root=pipe.folder/'checkpoints'/'current-editorial'
    specs=editorial_task_specs(frozen)
    manager.register([s for s in specs if s['id'] not in manager.ledger['tasks']])
    views={a['area_id']:editorial_input(frozen,a['area_id'],slide_budget=budgets[a['area_id']]) for a in frozen['stages'][0]['areas']}
    measurements={key:input_measurement(system,_message(AUTHOR_INSTRUCTION,view)) for key,view in views.items()}
    pipe.save(root/'input-preflight.json',{'source_analysis_sha256':digest(source_analysis(frozen)),
        'skeleton_pages':skeleton,'area_budgets':budgets,'areas':measurements,'model_requests_performed_by_preflight':0})

    async def one(area_id):
        view=views[area_id];key='current-editorial-'+area_id
        pair=pipe.pair_task(data,key);pair_record=pair.ledger['tasks'][key]['pair']
        binding=digest({'view':view,'author_instruction':AUTHOR_INSTRUCTION,'review_instruction':REVIEW_INSTRUCTION,
                        'system':system,'policy':policy(),'contract':'current-editorial-v1'})
        folder=root/area_id;accepted=folder/'accepted.json'
        async def operation():
            prior_accepted=None
            if accepted.is_file():
                saved=strict_json(accepted.read_bytes())
                if saved.get('candidate_sha256')!=digest(saved['candidate']):raise ValueError('current_editorial_cached_candidate_changed')
                prior_accepted=model_draft(saved['candidate'])
                if saved.get('input_sha256')==binding:
                    candidate=validate_area(view,model_draft(saved['candidate']));review=saved['review']
                    if saved.get('candidate_sha256')!=digest(candidate) or review.get('candidate_sha256')!=digest(candidate) or review.get('verdict')!='pass':
                        raise ValueError('current_editorial_cached_binding_failed')
                    for role in ('author','reviewer'):
                        pipe.report_pair(data,key,role,candidate,[accepted],review if role=='reviewer' else {'deterministic_passed':True},'동일 원문·편집·독립 검토를 재검증해 재사용')
                    return candidate,review
            state_file=folder/'pending.json';state={}
            if state_file.is_file():
                saved=strict_json(state_file.read_bytes())
                if saved.get('input_sha256')==binding:state=saved
            prior=state.get('prior_candidate',prior_accepted)
            defects=state.get('defects',deepcopy(view['revision_context']))
            for attempt in range(int(state.get('author_responses',0))+1,pipe.rounds+1):
                pair.begin_pair_candidate(key)
                current=folder/'inputs'/binding/uuid4().hex/('round-'+str(attempt).zfill(3))
                payload={**view,'prior_candidate':deepcopy(prior),'accumulated_defects':deepcopy(defects)}
                message=_message(AUTHOR_INSTRUCTION,payload)
                pipe.save(current/'author-input.json',payload);pipe.save(current/'author-measurement.json',input_measurement(system,message))
                pipe.event(key,'author','running',round=attempt)
                async with pipe.item_gate:
                    raw,receipt=await pipe.runtime.ask('author',message,session_id=pair_record['author'],instructions=system)
                pipe.save(current/'author-response.txt',raw.encode('utf-8'));pipe.save(current/'author-receipt.json',receipt)
                try:
                    proposed=strict_json(raw.encode('utf-8'));candidate=validate_area(view,proposed)
                except (ValueError,TypeError,KeyError) as exc:
                    prior={'raw_response':raw};defects=[{'origin':'deterministic_contract','error':str(exc)}]
                    pipe.save(state_file,{'input_sha256':binding,'author_responses':attempt,'prior_candidate':prior,'defects':defects})
                    continue
                pipe.report_pair(data,key,'author',candidate,[current/'author-response.txt',current/'author-receipt.json'],
                    {'deterministic_passed':True},'동결된 영역 원문 전부에서 핵심을 선택·통합하고 모든 관찰 귀속을 검사')
                review_payload={'task':'current_state_editorial_review','source':view,'candidate':candidate}
                review=None;contract_error=None;last_review=None;review_attempt=0;review_receipt=None
                for review_attempt in range(1,pipe.rounds+1):
                    sent={**review_payload,'prior_review':last_review,'review_contract_error':contract_error}
                    review_message=_message(REVIEW_INSTRUCTION,sent)
                    pipe.save(current/f'review-input-{review_attempt}.json',sent)
                    pipe.save(current/f'review-measurement-{review_attempt}.json',input_measurement(system,review_message))
                    async with pipe.item_gate:
                        review_raw,review_receipt=await pipe.runtime.ask('content_review',review_message,session_id=pair_record['reviewer'],instructions=system)
                    pipe.save(current/f'review-response-{review_attempt}.txt',review_raw.encode('utf-8'))
                    pipe.save(current/f'review-receipt-{review_attempt}.json',review_receipt)
                    try:
                        last_review=strict_json(review_raw.encode('utf-8'));review=_review_contract(candidate,last_review);break
                    except (ValueError,TypeError,KeyError) as exc:
                        last_review={'raw_response':review_raw};contract_error=str(exc)
                if review is None:raise RuntimeError('current_editorial_review_contract_exhausted:'+area_id+':'+str(contract_error))
                if review_receipt is None:raise RuntimeError('current_editorial_review_receipt_missing:'+area_id)
                pipe.save(current/'review.json',review)
                pipe.report_pair(data,key,'reviewer',candidate,[current/'review.json',current/f'review-receipt-{review_attempt}.json'],review,
                    '선택·통합된 모든 본문과 상세 원장 전용 항목을 동일한 영역 승인 원문 전부와 독립 대조')
                if review['verdict']=='pass':
                    if accepted.is_file():pipe.save(folder/'history'/digest(strict_json(accepted.read_bytes()))/'accepted.json',strict_json(accepted.read_bytes()))
                    pipe.save(accepted,{'input_sha256':binding,'candidate_sha256':digest(candidate),'candidate':candidate,'review':review,
                        'author_receipt':receipt,'review_receipt':review_receipt,'source_workbook_sha256':strict_json(freeze_receipt.read_bytes())['xlsx_sha256']})
                    return candidate,review
                prior=model_draft(candidate);defects.extend(review['defects'])
                pipe.save(state_file,{'input_sha256':binding,'author_responses':attempt,'prior_candidate':prior,'defects':defects})
            raise RuntimeError('current_editorial_author_budget_exhausted:'+area_id)
        return await pair.perform(key,operation)
    parts=await asyncio.gather(*(one(a['area_id']) for a in frozen['stages'][0]['areas']))
    projection=bind_projection(frozen,[candidate for candidate,_ in parts],{candidate['area_id']:review for candidate,review in parts})
    output={**frozen,'presentation':projection}
    pipe.save(root/'accepted-projection.json',projection)
    return output
