"""공식 기준·관측 구성·적용성의 추적 원장. 공식 점수나 전체 통제 인증을 생성하지 않는다."""
from copy import deepcopy
import json
from pathlib import Path
from asis_model import ASSET
from asis_evidence_views import areas_for, digest

SOURCE_AREA={'NETWORK':'network','NETWORKING':'network','COMPUTE':'compute','DATA':'data','SECID':'secgov','IDENTITY':'secgov',
 'GOVERNANCE':'secgov','SECURITY':'secgov','COST':'cost','MONITORING':'telemetry','TELEMETRY':'telemetry','QUALITY':'quality',
 'OPERATING':'operating','RELIABILITY':'compute','RESOURCE ORGANIZATION':'operating'}


def evaluate(original):
    """단일 구성 속성과 전체 원칙 충족을 구분해 모든 기준의 적용·근거 공백을 기록한다."""
    data=deepcopy(original)
    metadata={x['id']:x for x in json.loads((ASSET/'criteria-metadata.json').read_text(encoding='utf-8'))} if (ASSET/'criteria-metadata.json').is_file() else {}
    facts={f['id']:f for f in data['facts']}
    datasets=data['context'].get('dataset_register',[])
    rows=[];judgments=[]
    for criterion in data['criteria']:
        cid=criterion['id'];meta=metadata.get(cid,{})
        area=SOURCE_AREA.get(str(meta.get('source_area') or '').upper())
        if not area:
            area='secgov' if any(x in cid for x in ('SE','GOV','NS','IM','PA','DP','AM')) else 'cost' if '-CO' in cid else 'telemetry' if '-OE2' in cid else 'quality' if ('RAI' in cid or '-OE3' in cid) else 'compute' if '-RE' in cid else 'operating'
        fids=[r['fact_id'] for r in datasets if r.get('fact_id') and area in r['area_refs']]
        status='unknown';applicable=False
        condition='업무·데이터·호출 방식·운영 범위에 맞는 적용 조건 확인 필요'
        conclusion='관련 구성 자료는 확보 여부를 기록하되 고객의 업무 요구와 실제 통제 효과가 확인되지 않아 전체 원칙 충족을 확정하지 않는다.'
        if cid=='AILZ-I-R6':
            fids=['F_T_foundryAccountConfiguration'] if 'F_T_foundryAccountConfiguration' in facts else fids
            values=facts.get('F_T_foundryAccountConfiguration',{}).get('value')
            if isinstance(values,list) and values:
                applicable=True;status='partial';condition='수집된 AI 모델 Endpoint 계정의 키 인증 설정'
                known=[v for v in values if v.get('localAuthenticationDisabled') is not None]
                disabled=[v for v in known if v['localAuthenticationDisabled'] is True]
                conclusion=f'관측 계정 {len(values)}개 중 키 인증 비활성 필드 확인 {len(known)}개, 명시 비활성 {len(disabled)}개. 구성 경계는 확인되나 실제 호출 주체별 Microsoft Entra ID 인증·권한 거부는 별도 시험 대상이다.'
        elif cid=='AILZ-N-R5':
            values=facts.get('F_E_wafPolicies',{}).get('value')
            if isinstance(values,list) and values:
                fids=['F_E_wafPolicies'];applicable=True;status='partial';condition='공개 웹 진입에 연결된 WAF 정책 구성'
                detection=[v for v in values if str(v.get('mode','')).casefold()=='detection']
                conclusion=f'관측 WAF 정책 {len(values)}개 중 Detection 모드 {len(detection)}개. 탐지는 요청 차단과 다르므로 공개 진입의 방어 요구·오탐 영향·허용 예외를 검토한 뒤 Prevention 전환과 검증 범위를 결정한다. 현장에서 차단이 수행됐다는 증거는 없다.'
        elif cid in ('WAF-AI-CO1','AILZ-CO-R1'):
            values=data['context'].get('cost_summary')
            if values:
                fids=['F_D_cost_monthly'] if 'F_D_cost_monthly' in facts else ['F_E_costMonthlyTotals']
                applicable=True;status='partial';condition='Azure 수집 범위의 비용 기준과 전체 업무 비용·가치 비교'
                conclusion='기간·통화·ActualCost/AmortizedCost별 조회값은 확보됐다. Azure 외 구독·인력·도입/전환 비용, 업무 귀속·유효 완료량·품질·현금효과가 없어 전체비용이나 경제적 타당성은 확정하지 않는다.'
        elif cid.startswith('AILZ-CO-'):
            condition='업무 수요·기간·허용 중단·배포 유형·단가·실제 사용에 따른 조건부 비용 최적화'
            conclusion='PTU, 전역 배포, 자동종료는 동일 업무 품질·보안·서비스 수준과 실제 수요에서 비교해야 한다. 비설치 사실만으로 미흡을 확정하거나 절감률을 부여하지 않는다.'
        fids=list(dict.fromkeys(f for f in fids if f in facts))
        evidence_states={f: {'status':facts[f]['collection_status'],'complete':facts[f]['scope_complete']} for f in fids}
        jid='CJ_'+digest(cid)[:16]
        judgments.append({'id':jid,'status':status,'fact_refs':fids,'criterion_refs':[cid],'applicability_confirmed':applicable})
        rows.append({'id':jid,'criterion_id':cid,'original_id':meta.get('original_id',cid),'area_refs':[area],
            'source_url':criterion['source_url'],'source_snapshot_sha256':criterion['source_snapshot_sha256'],
            'quote':criterion['quote'],'modality':criterion['modality'],'application_condition':condition,
            'status':status,'applicability_confirmed':applicable,'fact_refs':fids,'evidence_states':evidence_states,
            'conclusion':conclusion,'required_evidence':'같은 업무·대상·기간의 구성·호출/거부·품질·운영/재무 근거. 관련 원칙의 적용 조건과 미수집 원인을 구분한다.',
            'recommendation_quote':meta.get('recommendation_quote'),'official_score':None,
            'classification':'consulting_interpretation_of_observed_scope_not_certification'})
    data['judgments']=judgments;data['context']['criterion_assessments']=rows
    return data
