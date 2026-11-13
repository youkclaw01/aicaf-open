"""r2 근거 연결 분석 어댑터. 실측 구성과 확인 과제를 분리한 후보이며 독립 품질 승인을 만들지 않는다."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path

from asis_model import ASSET


AREA_NAMES = {'network': '네트워크', 'compute': '컴퓨트', 'data': '스토리지·데이터', 'ai': 'AI 서비스',
              'secgov': '보안·거버넌스', 'telemetry': '관측·추적', 'quality': '품질·평가', 'operating': '운영 모델',
              'cost': '비용·경제성'}
RESERVED = {'azurefirewallsubnet', 'gatewaysubnet', 'routeserversubnet', 'azurefirewallmanagementsubnet', 'azurebastionsubnet'}


def contract():
    path = ASSET / 'contract' / 'validate_contract.py'
    spec = importlib.util.spec_from_file_location('asis_r2_contract', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def criteria():
    items = json.loads((ASSET/'criteria.json').read_text(encoding='utf-8'))
    index = json.loads((ASSET/'sources-index.json').read_text(encoding='utf-8'))
    for item in items:
        row = next(r for r in index if r['url'] == item['source_url'])
        raw = (ASSET/row['text_path']).read_bytes()
        if hashlib.sha256(raw).hexdigest() != item['source_snapshot_sha256'] or item['quote'] not in raw.decode('utf-8'):
            raise ValueError('criterion_source_binding_failed:' + item['id'])
    return items


class Probes:
    def __init__(self, data):
        self.data = data
        self.facts = {f['id']: f for f in data['facts']}

    def fact(self, fid):
        if fid not in self.facts:
            source = 'aicaf-evidence.json' if fid.startswith('F_E_') else 'topology.json'
            key = fid[4:]
            pointer = '/sections/'+key+'/records' if fid.startswith('F_E_') else '/configurations/'+key
            row = {'id': fid, 'source_id': source, 'field_path': pointer, 'run': deepcopy(self.data['run']),
                   'value': None, 'collection_status': 'not_collected', 'scope_complete': False}
            self.data['facts'].append(row); self.facts[fid] = row
        return self.facts[fid]

    def rows(self, fid):
        row = self.fact(fid)
        return row['value'] if row['collection_status'] in ('collected', 'not_present') and isinstance(row['value'], list) else None

    def count(self, fid, label):
        row = self.fact(fid); values = self.rows(fid)
        if values is None:
            return label+'은 '+{'not_collected':'미수집','permission_denied':'권한 제한으로 미확인','collection_failed':'수집 실패로 미확인'}.get(row['collection_status'],'미확인')+' 상태입니다.'
        if not row['scope_complete']:
            return f'부분 수집 범위에서 {label} {len(values)}개 행이 관측되었습니다. 전체 범위의 부재·총수는 판정하지 않습니다.'
        return f'성공적으로 조회한 범위에서 {label} {len(values)}개 행이 확인되었습니다.'

    def property(self, fid, key, expected):
        rows = self.rows(fid)
        if rows is None:
            return None
        relevant = [r for r in rows if isinstance(r, dict)]
        known = [r for r in relevant if key in r and r[key] is not None and r[key] != '']
        matching = [r for r in known if r[key] == expected]
        return len(relevant), len(known), len(matching)

    def property_text(self, fid, key, expected, label):
        counts = self.property(fid, key, expected)
        if counts is None:
            return label+'은 원본 필드 미확인으로 판정을 보류합니다.'
        total, known, match = counts
        return f'관측된 대상 {total}개 중 {key} 필드가 확인된 대상은 {known}개이며, {label} 설정은 {match}개입니다. 미설정·미수집 값은 반대 설정으로 계산하지 않습니다.'


def requirement_plan(data, revision_context):
    """명시된 분석 영역만 제약으로 적용하고, 임의 해석·고객 사실 승격은 보류한다."""
    raw = data['context'].get('requirements', '')
    if not isinstance(raw, str) or len(raw) > 6000:
        raise ValueError('invalid_analysis_requirements')
    context = deepcopy(revision_context) if revision_context is not None else {'requirements': raw, 'prior_output': None}
    if not isinstance(context, dict) or context.get('requirements', '') != raw:
        raise ValueError('revision_requirement_binding_mismatch')
    prior = context.get('prior_output')
    if prior is not None:
        previous = prior.get('input', {}).get('run', {}) if isinstance(prior, dict) else {}
        if any(previous.get(key) != data['run'][key] for key in ('assessment_id', 'case_id', 'track')):
            raise ValueError('cross_project_prior_output')
    aliases = {'network': ('네트워크', '사설 DNS', 'Private Endpoint'), 'compute': ('컴퓨트', '복구 목표'),
               'data': ('스토리지', '데이터 접근', '데이터 보존'), 'ai': ('AI 서비스', '모델 배포', '콘텐츠 안전'),
               'secgov': ('보안', '거버넌스', '권한'), 'telemetry': ('모니터링', '관측', '로그 보존'),
               'quality': ('품질', '평가'), 'operating': ('운영 책임', '변경 승인', '롤백'),
               'cost': ('비용', '경제성', '절감', '단가', '가치')}
    items = []
    for quote in (line.strip() for line in raw.splitlines() if line.strip()):
        areas = [key for key, names in aliases.items() if any(name.casefold() in quote.casefold() for name in names)]
        # 결론 조작 요청은 영역명이 포함되어도 적용하지 않는다.
        if any(term in quote for term in ('전부 정상', '무시하', '무시해', '근거 없이', '점수를 올')):
            areas = []
        items.append({'id': 'REQ_'+str(len(items)+1), 'quote': quote, 'area_refs': areas,
                      'status': 'applied_as_analysis_constraint' if areas else 'requires_interpretation',
                      'action_refs': ['A_'+key for key in areas],
                      'interpretation': '명시 영역의 검토 제약으로 적용하며 충족·사실·우선순위 확정으로 해석하지 않습니다.' if areas else '명시된 분석 영역·검증 조건을 확인한 후 적용해야 합니다.'})
    return context, {'schema': 'asis-requirements-application/1', 'source_sha256': hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':')).encode('utf-8')).hexdigest(),
                     'customer_fact_promoted': False, 'items': items,
                     'prior_output_sha256': hashlib.sha256(json.dumps(prior, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':')).encode('utf-8')).hexdigest() if prior is not None else None}


def analyze(original, *, revision_context=None):
    """현재 구성의 의미와 확인 과제를 r2 JSON으로 작성한다. 업무별 최종 판단은 독립 검토 대상이다."""
    data = deepcopy(original)
    revision_context, requirements_application = requirement_plan(data, revision_context)
    data['criteria'] = criteria()
    from asis_evidence_views import enrich
    from asis_criteria_assessment import evaluate
    if 'dataset_register' not in data['context']:
        data = enrich(data)
    data = evaluate(data)
    p = Probes(data)
    areas = []; directions = []; diagnoses = []; actions = []; unresolved = []
    criterion_ids = {c['id'] for c in data['criteria']}

    def area(key, headline, observation_specs, implication, impact, direction, criterion_refs,
             action_title, target, change_type, verification, priority='next', prerequisite=None):
        refs = list(dict.fromkeys(fid for _, fids in observation_specs for fid in fids))
        for fid in refs:
            p.fact(fid)
        for cid in criterion_refs:
            if cid not in criterion_ids:
                raise ValueError('unbound_area_criterion:'+cid)
        observations = [{'id': f'O_{key}_{i+1}', 'text': text, 'fact_refs': fids,
                         'criterion_refs': [], 'goal_refs': [], 'judgment_refs': []}
                        for i, (text, fids) in enumerate(observation_specs)]
        implication_id, direction_id, diagnosis_id, action_id = 'I_'+key, 'D_'+key, 'J_'+key, 'A_'+key
        args = {'fact_refs': refs, 'criterion_refs': criterion_refs, 'goal_refs': []}
        areas.append({'area_id': key, 'headline': headline, 'observations': observations,
                      'implication': {'id': implication_id, 'text': implication, 'impact': impact,
                                      'change_direction': direction, **args, 'judgment_refs': []}})
        directions.append({'id': direction_id, 'title': headline, 'text': implication,
                           'customer_impact': impact, 'change_direction': direction,
                           'area_implication_refs': [implication_id], **args})
        diagnoses.append({'id': diagnosis_id, 'text': implication, 'impact': impact,
                          'direction_refs': [direction_id], **args})
        actions.append({'id': action_id, 'title': action_title, 'target': target,
                        'change_type': change_type, 'rationale': direction,
                        'diagnosis_refs': [diagnosis_id], 'direction_refs': [direction_id], **args,
                        'prerequisites': prerequisite or [], 'deliverable': target+'의 범위·근거·판단·승인 기록',
                        'verification_condition': verification, 'priority_level': priority,
                        'priority_basis': '현재 확인된 구성과 검증 공백의 선후 의존에 따른 제안이며, 업무 중요도·일정은 미확인입니다.',
                        'improvement_points': [(AREA_NAMES[key] + ' 구성의 범위·근거 확인')[:15]], 'detailed_implementation_ref': None})
        unresolved.append({'topic': AREA_NAMES[key]+' 적용성·운영 확인',
                           'reason': '구성 조회를 실제 동작·고객 목표의 충족과 동일시하지 않습니다.',
                           'required_evidence': verification, 'fact_refs': refs})

    sn = p.rows('F_T_subnets')
    if sn is None:
        subnet_text = p.count('F_T_subnets', '서브넷')
    else:
        wl = [s for s in sn if str(s.get('name') or '').lower() not in RESERVED]
        subnet_text = f'예약 이름을 제외한 관측 서브넷 {len(wl)}개 중 NSG 참조는 {sum(bool(s.get("nsgId")) for s in wl)}개, UDR 참조는 {sum(bool(s.get("routeTableId")) for s in wl)}개, NAT Gateway 참조는 {sum(bool(s.get("natGatewayId")) for s in wl)}개입니다. UDR와 Firewall은 별도 구성 사실입니다.'
    fa = p.rows('F_T_foundryAccountConfiguration')
    access = p.property('F_T_foundryAccountConfiguration', 'publicNetworkAccess', 'Disabled')
    if access is None or access[1] != access[0]:
        boundary_note = 'AI 계정의 공용 접근 경계는 일부 또는 전체 필드가 미확인입니다. 차단 여부를 추정하지 않고 원본 속성과 호출 범위를 우선 확인해야 합니다. '
    elif access[0] == 0:
        boundary_note = '조회 범위의 AI 계정 행은 비어 있습니다. 신규 도입의 준비 상태를 AI 자원 부재만으로 낮게 판단하지 않고, 목표 업무의 연결 요구부터 확인해야 합니다. '
    elif access[2] == access[0]:
        boundary_note = '관측 AI 계정의 공용 접근 차단 설정이 일관되므로 이 경계는 유지·활용 대상입니다. 연결 오류를 해소하려고 공용 접근을 열기보다 사설 경로와 권한을 분리해 검증해야 합니다. '
    else:
        boundary_note = '관측 AI 계정에는 명시 공용 차단이 적용되지 않은 대상이 포함됩니다. 실제 공개 호출 허용·데이터 민감도·예외 승인 여부를 확인한 뒤 격리 변경의 필요성과 영향 범위를 결정해야 합니다. '
    net_ai = p.property_text('F_T_foundryAccountConfiguration', 'publicNetworkAccess', 'Disabled', 'AI 계정 공용 접근 차단')
    pe = p.rows('F_T_privateEndpoints'); zg = p.rows('F_E_privateEndpointDnsZoneGroups'); links = p.rows('F_T_privateDnsLinks')
    dns_text = p.count('F_T_privateEndpoints', 'Private Endpoint')+' '+p.count('F_E_privateEndpointDnsZoneGroups', 'DNS zone group')
    if pe is not None and zg is not None and links is not None:
        groups = {str(z.get('privateEndpointId') or '').lower(): z for z in zg}
        bound = [x for x in pe if str(x.get('id') or '').lower() in groups]
        dns_text += f'동일 Endpoint ID로 zone group이 연결된 관측 대상은 {len(bound)}개입니다. VNet별 실제 DNS 응답·사설 호출 성공은 수집하지 않았습니다.'
    area('network', '사설 연결 구성을 실제 호출 경로 검증과 분리합니다',
         [(subnet_text, ['F_T_subnets']), (net_ai, ['F_T_foundryAccountConfiguration']),
          (dns_text, ['F_T_privateEndpoints','F_E_privateEndpointDnsZoneGroups','F_T_privateDnsLinks']),
          (p.count('F_T_azureFirewalls','Azure Firewall')+' '+p.count('F_T_routeTables','경로 테이블'), ['F_T_azureFirewalls','F_T_routeTables'])],
         boundary_note+'공용 차단·Endpoint·DNS 연결은 사설 경로의 구성 근거입니다. NSG·UDR·NAT의 존재만으로 실제 허용·거부 경로나 외부 반출 통제가 검증되지는 않습니다. 호출 주체부터 DNS·라우팅·대상 권한을 순서대로 검증해야 합니다.',
         '사설 경로를 이용하는 업무는 연결 오류와 정책 거부를 구분하지 못하면 불필요한 공개 전환이나 반복 장애로 이어질 수 있습니다. 현재 입력에는 그런 사고 발생 증거가 없습니다.',
         '현재 격리 설정을 유지하고, 승인된 호출 주체별 DNS 응답·유효 경로·허용 및 거부 결과를 묶어 확인합니다. Firewall 도입 여부는 반출 요구와 기존 통제를 비교한 뒤 결정합니다.',
         ['WAF-AI-SE4'], '호출 주체별 사설 경로와 반출 통제 확인', '호출 주체·서브넷·Endpoint·DNS·유효 경로', 'verify',
         '승인 주체의 사설 호출 성공, 비승인 주체의 거부, 실제 이름 해석·유효 NSG/경로 증거가 동일 범위에 연결될 것', 'first')

    vm = p.rows('F_E_virtualMachineOs')
    vm_text = p.count('F_E_virtualMachineOs','VM 운영체제 조회')
    if vm is not None:
        zone = sum(bool([z for z in (r.get('zones') or []) if z]) for r in vm)
        vm_text += f'비어 있지 않은 zones 값이 있는 VM은 {zone}개입니다. 단일 VM의 영역 지정은 업무 전체의 다중 영역 이중화를 증명하지 않습니다.'
    area('compute', '실행 기반을 유지하되 업무 복구 목표와 의존성을 확인합니다',
         [(vm_text,['F_E_virtualMachineOs']), (p.count('F_T_computeReliability','컴퓨트 구성'),['F_T_computeReliability']),
          (p.count('F_T_recoveryBackup','백업 보호 조회'),['F_T_recoveryBackup'])],
         'VM·관리형 플랫폼의 선택은 모델 호출 방식, 지연 요구, 배포·운영 역량과 함께 판단해야 합니다. VM만 있다는 이유로 잘못된 플랫폼으로 분류하지 않습니다. 수집 구성에는 업무별 허용 중단시간·복구시점과 재기동·복원 시험이 없으므로, 인스턴스 수나 지역 수를 업무 가용성 보장으로 읽지 않아야 합니다.',
         '복구 목표와 앱·모델·데이터 의존이 연결되지 않으면 기반 리소스를 추가해도 업무의 실제 복구 조건을 충족하는지 판단할 수 없습니다.',
         '업무의 RTO/RPO·유지보수 허용 범위를 먼저 확인하고 기존 실행 기반의 재시작·복원·종속 서비스 복구 경로를 검증합니다. 관리형 플랫폼 전환은 그 결과에 따른 선택지로 비교합니다.',
         ['WAF-AI-RE1','WAF-AI-OE1'], '업무 복구 목표와 실행 플랫폼 선택 검증', '업무별 앱·모델·데이터 의존', 'verify',
         '업무 RTO/RPO와 승인된 복구 시험 결과, 플랫폼별 운영·제약 비교가 같은 업무 경로에 연결될 것', 'next', ['업무 중요도와 복구 목표 확인'])

    ds = p.rows('F_T_dataServiceConfiguration')
    storage = [r for r in (ds or []) if r.get('type') == 'microsoft.storage/storageaccounts'] if ds is not None else None
    dat_text = p.count('F_T_dataServiceConfiguration','데이터 서비스 구성')
    if storage is not None:
        known = [r for r in storage if r.get('publicNetworkAccess') not in (None,'')]
        closed = sum(r['publicNetworkAccess']=='Disabled' for r in known)
        tls = [r for r in storage if r.get('minimumTlsVersion') not in (None,'')]
        dat_text += f'관측 스토리지 {len(storage)}개 중 공용 접근 필드 확인 {len(known)}개, 명시 차단 {closed}개이며, minimumTlsVersion 값이 확인된 대상은 {len(tls)}개입니다. 빈 값은 암호화 해제나 실제 공개 열람의 증거가 아닙니다.'
    area('data','데이터 접근 경계를 저장소 설정과 업무 권한으로 확인합니다',
         [(dat_text,['F_T_dataServiceConfiguration']), (p.count('F_T_privateEndpoints','데이터/AI Endpoint 구성'),['F_T_privateEndpoints'])],
         '저장소의 공용 네트워크 설정과 데이터의 익명 공개·업무별 권한·민감도는 서로 다른 통제입니다. 공용 네트워크가 허용된 설정만으로 데이터가 공개되었다고 단정하지 않습니다. 검색·벡터 저장소의 부재도 RAG가 고객의 필수 사용 방식으로 확인되지 않은 한 결함이 아닙니다.',
         '모델 입력에 사용할 데이터의 민감도와 허용 주체가 확인되지 않으면 필요한 사설 경로·권한·삭제/보존 정책을 확정할 수 없습니다. 현재 사고나 데이터 유출은 확인되지 않았습니다.',
         '실제 데이터 분류와 업무별 읽기·쓰기·보존 요구를 확인한 뒤 기존 저장소의 네트워크·인증·세부 권한을 결합해 시험합니다. 검색·벡터 구성은 데이터 검색 요구가 확인된 범위에만 검토합니다.',
         ['WAF-AI-SE2'], '데이터 분류·업무 권한과 저장소 접근 검증', '업무 데이터·저장소·호출 주체', 'verify',
         '민감도·소유자·읽기/쓰기 범위가 확인되고 승인/비승인 접근·보존/삭제 조건의 결과가 기록될 것')

    dep = p.rows('F_E_aiDeployments'); rai = p.rows('F_E_aiRaiPolicies')
    model_text = p.count('F_E_aiDeployments','모델 배포')
    if dep is not None:
        models = sorted({str(r.get('modelName')) for r in dep if r.get('modelName')})
        model_text += ' 모델명은 '+(', '.join(models) if models else '필드 미확인')+'입니다. 배포 존재를 특정 고객 업무의 사용 증거로 확대하지 않습니다.'
    auth_text = p.property_text('F_T_foundryAccountConfiguration','localAuthenticationDisabled',True,'키 인증 비활성')
    filter_text = p.count('F_E_aiRaiPolicies','콘텐츠 안전 정책')
    if dep is not None and rai is not None:
        policy={(str(r.get('accountId') or '').lower(),str(r.get('name') or '').lower()) for r in rai}
        bound=sum((str(d.get('accountId') or '').lower(),str(d.get('raiPolicyName') or '').lower()) in policy for d in dep)
        filter_text += f'배포의 accountId·raiPolicyName과 정책 원장이 일치하는 관측 배포는 {bound}개입니다. 기본 정책도 정책이며 사용자 지정이 없다는 이유로 콘텐츠 안전 부재로 판정하지 않습니다.'
    area('ai','모델 배포·인증·콘텐츠 정책의 연결과 효과를 구분합니다',
         [(model_text,['F_E_aiDeployments']), (auth_text,['F_T_foundryAccountConfiguration']),
          (filter_text,['F_E_aiDeployments','F_E_aiRaiPolicies']),
          (p.count('F_E_aiCapabilityHosts','AI capability host 조회'),['F_E_aiCapabilityHosts'])],
         '계정의 키 인증 비활성은 자격증명 노출 경로를 줄이는 구성 근거이며 실제 호출 주체의 관리 ID 권한을 증명하지는 않습니다. 모델·버전·배포 SKU·안전 정책의 연결은 동일 계정/배포 ID로 확인해야 하고, 기본 필터와 사용자 지정 필터의 차이는 업무 위험·지원 기능에 따라 비교해야 합니다. 수집 오류가 포함된 capability host는 부재로 판단하지 않습니다.',
         '업무별 모델·인증·정책 효과가 연결되지 않으면 배포가 있어도 품질·보호·변경 영향의 책임 범위를 확정할 수 없습니다.',
         '배포별 업무 소유자·인증 주체·안전 정책·버전 변경 조건을 확인하고 허용/거부 및 안전 테스트 결과를 연결합니다. 신규 유형도 수집된 AI 자원의 실제 용도를 확인한 뒤 준비 범위를 확정합니다.',
         ['WAF-AI-SE1','WAF-AI-SE3','WAF-AI-APP1'], '배포별 업무·인증·안전 정책 적용 확인', 'AI 계정·모델 배포·콘텐츠 정책', 'verify',
         '동일 배포 ID에 업무·호출 주체·정책·버전이 매핑되고 승인된 안전/권한 시험과 변경 조건이 기록될 것', 'first')

    ra=p.rows('F_T_rbacRoleAssignments'); rd=p.rows('F_T_rbacRoleDefinitions')
    role_text=p.count('F_T_rbacRoleAssignments','역할 할당')
    if ra is not None and rd is not None:
        names={str(r.get('id') or '').lower():r.get('roleName') for r in rd}
        owners=[r for r in ra if str(r.get('scopeId') or '').count('/')==2 and names.get(str(r.get('roleDefinitionId') or '').lower())=='Owner']
        role_text+=f'관측 구독 범위 Owner 할당은 {len(owners)}개이며, 이 중 ServicePrincipal은 {sum(r.get("principalType")=="ServicePrincipal" for r in owners)}개입니다. 정당한 자동화 필요와 위임 범위는 추가 확인 대상입니다.'
    area('secgov','권한·정책 구성과 실제 강제·승인을 분리합니다',
         [(role_text,['F_T_rbacRoleAssignments','F_T_rbacRoleDefinitions']),
          (p.count('F_T_policyAssignments','조회 범위의 직접 정책 할당'),['F_T_policyAssignments']),
          (p.count('F_E_managementGroupChain','관리그룹 상위 체인'),['F_E_managementGroupChain'])],
         '직접 정책 할당의 조회 결과가 비어도 상위 상속 정책과 리소스별 실제 강제 상태의 부재를 뜻하지 않습니다. Owner 역할 개수만으로 모든 할당이 과도하다고 판정하지 않으며 서비스 주체의 실제 업무·권한 필요·승인 범위를 확인해야 합니다. MFA·PIM·조건부 접근은 수집 범위 밖의 통제 효과로 남습니다.',
         '권한 주체·업무·승인 사유가 연결되지 않으면 자동화 변경의 영향과 책임을 추적하기 어렵습니다. 광범위한 역할이 실제 악용되었다는 증거는 없습니다.',
         '기존 할당을 변경하기 전에 주체별 목적·상속·세부 권한·승인 기한을 확인하고, 필요 범위 축소안과 변경 후 업무 검증 조건을 마련합니다. 정책은 할당과 실제 차단/평가 결과를 함께 확인합니다.',
         ['WAF-AI-SE3','CAF-AI-GOV1'], '상속 정책·업무별 권한과 승인 근거 확인', '구독/리소스 역할·상위 정책·승인 기록', 'verify',
         '역할 목적·승인자·필요 범위와 정책 상속/강제 결과가 확인되고 권한 축소 시 필요한 업무의 성공/불필요 접근 거부가 검증될 것', 'first')

    diag=p.rows('F_E_diagnosticSettingsExtended'); diag_fact=p.fact('F_E_diagnosticSettingsExtended')
    scope_fact=p.fact('F_E_diagnosticSettingsExtended_parentCount'); parents=scope_fact['value']
    diag_text=p.count('F_E_diagnosticSettingsExtended','확장 진단 설정')
    if isinstance(parents,int):
        diag_text+=f'이 조회가 확인한 부모 리소스 수는 {parents}개입니다. 대상 목록·모든 범주·업무 로그의 전체 보존 상태는 별도 확인합니다.'
    gap=diag is not None and not diag and diag_fact['scope_complete']
    area('telemetry','구성 조회 공백과 업무 관측 경로를 연결해 보완합니다',
         [(diag_text,['F_E_diagnosticSettingsExtended','F_E_diagnosticSettingsExtended_parentCount']),
          (p.count('F_E_alertRules','경고 규칙'),['F_E_alertRules']),
          (p.count('F_E_aiMetricsTokensDaily','기간별 AI 사용량 메트릭'),['F_E_aiMetricsTokensDaily'])],
         '플랫폼 메트릭이 관측되어도 요청별 로그·보안 감사·업무 결과 추적이 같은 수준으로 확보되었다고 볼 수 없습니다. 진단 설정 조회 결과는 확인한 부모 범위의 구성 사실이며 앱 자체 로그 전체의 부재가 아닙니다. 경고 규칙의 존재도 실제 알림 수신·담당 대응·재발 방지의 증거와 분리해야 합니다.',
         '요청·주체·업무·배포의 연결 기록이 없으면 오류의 원인과 비용 귀속, 모델 변경 영향을 같은 업무에서 검증하기 어렵습니다. 보관 범위·민감도·전송비용과의 균형이 필요합니다.',
         '확인한 조회 공백부터 필요 로그 범주·목적지·보존 기간·민감정보 제외를 설계하고, 승인 범위에서 수집/조회/알림 전달/대응까지 시험합니다. 메트릭·요청 로그·업무 지표는 서로 대체하지 않습니다.',
         ['WAF-AI-OE2'], '범위별 관측 경로·알림·보존 조건 수립', '확인한 부모 리소스·앱 요청·로그 목적지·운영 담당', 'add' if gap else 'verify',
         '대상/범주·목적지·보존/민감정보 정책이 확인되고 시험 요청의 추적·알림 수신·담당 대응이 동일 범위에서 증명될 것', 'first')

    area('quality','인프라 배포와 업무 응답 품질을 별도 검증합니다',
         [(p.count('F_E_aiDeployments','평가 대상 배포 후보'),['F_E_aiDeployments'])],
         '현재 수집본은 업무별 평가 데이터·정답/금지응답 기준·오류 비용·모델/검색 버전별 비교 결과를 포함하지 않습니다. 배포 개수나 인프라 메트릭을 답변 정확도·안전성·품질 성숙도 점수로 치환하지 않습니다. 이 영역은 미확인이며, 평가가 없다고 확정하지 않습니다.',
         '업무 성공 기준이 합의되지 않으면 모델 변경이 개선인지 저하인지와 허용 가능한 실패 범위를 판단할 수 없습니다.',
         '대상 업무의 성공·실패·위험 기준과 승인된 평가 자료를 먼저 확인하고 버전/입력/결과를 고정한 비교를 수행합니다. 신규 도입은 검증 계획, 기도입은 실제 반복 평가 결과로 구분합니다.',
         ['WAF-AI-OE3'], '업무별 품질·안전 평가 기준과 자료 확인', '업무·평가 자료·모델/검색 버전·결과', 'verify',
         '업무 목표·평가 범위·허용 실패·위험 기준과 버전별 실제 결과/승인이 확인될 것', 'first')

    area('operating','운영 책임·변경 승인·복구 증거를 인터뷰로 확인합니다',
         [(p.count('F_E_managementGroupChain','수집된 관리 범위'),['F_E_managementGroupChain']),
          (p.count('F_E_aiDeployments','운영 책임 매핑 대상 배포'),['F_E_aiDeployments'])],
         '조직 책임·운영 절차·변경 승인·고객 목표는 관리그룹이나 배포 구성만으로 확인되지 않습니다. 질문은행의 설명과 계산용 입력 메타데이터를 고객의 실제 답변으로 취급하지 않습니다. 현재 검증된 목표가 없으므로 우선순위는 확정 사업계획이 아니라 구성 의존에 따른 검토 제안입니다.',
         '소유자·변경 범위·롤백 조건이 불명확하면 기술 통제가 준비되어도 장애 대응과 모델 변경의 책임 경계를 확정하기 어렵습니다.',
         '업무 목표와 운영 책임을 검증된 인터뷰 인용으로 확인하고 배포·데이터·권한·모니터링의 변경 승인 및 롤백 조건에 연결합니다. 일정·효과·책임자는 고객 근거가 확보된 뒤 확정합니다.',
         ['CAF-AI-MNG1'], '업무 목표·운영 책임과 변경 승인 체계 확인', '업무 목표·담당 역할·변경/롤백·운영 기록', 'verify',
         '검증된 고객 목표 인용, 담당 역할·변경 승인/롤백 조건·최근 운영 검토 기록이 해당 업무 경로에 연결될 것', 'first')

    cost_rows=data['context'].get('cost_summary',[])
    metric_rows=data['context'].get('metric_summary',[])
    cost_text='기간·통화·청구 기준이 연결된 Azure 비용 조회 원장이 미확인이다.'
    if cost_rows:
        cost_text=' / '.join(f"{r['period']} {r['basis']} {r['amount']:.2f} {r['currency']}" for r in cost_rows)
        cost_text+='; ActualCost와 AmortizedCost는 별도 기준이며 합산하지 않는다. MTD와 완료 월은 구분한다.'
    metric_text='모델·상태·기간별 사용 메트릭의 관측 범위가 미확인이다.'
    if metric_rows:
        metric_text=f'계정·메트릭·차원별 집계 {len(metric_rows)}행은 반환된 비어 있지 않은 관측점만 합산했다. 토큰 종류·모델 요청·HTTP 상태는 서로 합산하지 않으며 업무 완료와 품질을 증명하지 않는다.'
    area('cost','Azure 비용과 업무의 전체비용·효과를 구분해 경제성을 판단한다',
         [(cost_text,['F_D_cost_monthly'] if cost_rows else ['F_E_costMonthlyTotals']),
          (metric_text,['F_D_metrics_by_dimension'] if metric_rows else ['F_E_aiMetricsTokensDaily']),
          (p.count('F_E_budgets','예산 구성')+' '+p.count('F_E_costByResource','리소스별 비용 조회'),['F_E_budgets','F_E_costByResource'])],
         'Azure 청구액·상각 비용과 업무의 전체비용은 범위가 다르다. 사용량과 비용은 동일 계정·모델·기간·측정 기준으로 연결하고 초기 도입·전환 비용, 반복 운영 비용, Azure 외 서비스·인력 비용은 별도 원장으로 보완한다. 모델 단가 변경이나 요청 감소만으로 동일 품질의 최적화가 입증되지는 않는다.',
         '전체비용과 유효 업무 완료량·품질·채택률이 연결되지 않으면 도입 타당성, 현재 운영의 단위경제성 및 확산 효과를 확정할 수 없다. 시간 확보를 현금 절감으로 계산하지 않으며 처리능력·품질·매출 기여의 중복 합산을 금지한다.',
         '비AI 포함 대안을 동일 업무 범위·처리량·품질·보안·서비스 수준에서 비교하고 비용·가치·민감도 원장을 연결한다. 기사용은 메트릭 관측 범위와 실제 청구의 배부 차이를 확인한 뒤 PTU·PAYGO·캐시·재시도·검토율 변화의 조건부 대안을 비교한다.',
         ['WAF-AI-CO1','AILZ-CO-R1','AILZ-CO-R2'], '업무별 전체비용·유효 완료당 비용과 경제성 기준 수립',
         '동일 업무·계정·모델·자료·기간별 비용 및 효과 원장','verify',
         '기간·통화·리전·가격일·할인·배부·포함/제외·초기/반복 비용, 유효 업무 완료·품질·수요·채택·재시도·검토율·민감도·보류/중단 조건이 동일 비교 범위에 연결될 것','first')

    for item in requirements_application['items']:
        if not item['area_refs']:
            unresolved.append({'topic': '추가 요구 적용 확인 '+item['id'], 'reason': item['interpretation'],
                               'required_evidence': '요구 원문: '+item['quote']+' / 대상 영역·원문 근거·완료 조건 확인', 'fact_refs': []})
            continue
        constraint = '추가 검토 제약(충족 미확인): '+item['quote']
        for key in item['area_refs']:
            target_area = next(row for row in areas if row['area_id'] == key)
            target_area['implication']['text'] += '\n'+constraint+' 현재 구성만으로 요구 충족을 판정하지 않고 아래 검증 조건을 적용합니다.'
            target_area['implication']['change_direction'] += '\n'+constraint
            direction = next(row for row in directions if row['id'] == 'D_'+key)
            direction['text'] = target_area['implication']['text']
            direction['change_direction'] = target_area['implication']['change_direction']
            diagnosis = next(row for row in diagnoses if row['id'] == 'J_'+key)
            diagnosis['text'] = target_area['implication']['text']
            action = next(row for row in actions if row['id'] == 'A_'+key)
            action['rationale'] = direction['change_direction']
            action['verification_condition'] += '\n'+constraint+' / 해당 제약을 유지한 시험 범위·성공/거부 결과·승인 근거가 확인될 것'
            action['improvement_points'].append(constraint[:15])
            action['prerequisites'].append('추가 제약의 업무 적용 범위 확인: '+item['quote'])
    # 합성 경로의 과제 필드를 장표 분량 계약(asis_consolidated_deck.ACTION_LIMITS)에 맞춘다. 운영 경로는 모델이 지시문의 분량으로 작성한다.
    # 요구사항이 덧붙인 문구(constraint)는 고객 요구의 원문이므로 자르지 않는다: 원 조건·포인트를 먼저 줄이고, 요구사항 문구는 그대로 남긴다.
    from asis_consolidated_deck import ACTION_LIMITS, ITEMS_PER_COLUMN_CHARS
    def clip(value, limit):
        value = ' '.join(str(value).split())
        return value if len(value) <= limit else value[:limit - 1] + '…'
    for action in actions:
        action['target'] = clip(action['target'], ACTION_LIMITS['target'])
        action['rationale'] = clip(action['rationale'], ACTION_LIMITS['rationale'])
        action['deliverable'] = clip(action['deliverable'], ACTION_LIMITS['deliverable'])
        if '추가 검토 제약' not in action['verification_condition']:
            action['verification_condition'] = clip(action['verification_condition'], ACTION_LIMITS['condition'])
        action['prerequisites'] = [clip(row, ITEMS_PER_COLUMN_CHARS[min(3, max(1, len(action['prerequisites'])))]) for row in action['prerequisites']][:3]
        action['improvement_points'] = [clip(row, ITEMS_PER_COLUMN_CHARS[min(3, max(1, len(action['improvement_points'])))]) for row in action['improvement_points']][:3]
    allocated={'observations':[o['id'] for a in areas for o in a['observations']],
               'implications':[a['implication']['id'] for a in areas], 'directions':[r['id'] for r in directions],
               'diagnoses':[r['id'] for r in diagnoses], 'actions':[r['id'] for r in actions], 'summary_points':['S_network','S_ai','S_telemetry']}
    data['allocated_ids']=allocated
    data['stage_data']={'area_slots':[{'area_id':k} for k in AREA_NAMES]}
    run=data['run']
    stages=[{'stage':'area','run':deepcopy(run),'unresolved':deepcopy(unresolved),'areas':areas}]
    edge1=[{'from_id':f,'to_id':d['id'],'relation':'supports'} for d in directions for f in d['fact_refs']]
    edge1 += [{'from_id':f,'to_id':d['id'],'relation':'consolidates'} for d in directions for f in d['area_implication_refs']]
    stages.append({'stage':'synthesis','run':deepcopy(run),'unresolved':deepcopy(unresolved),'directions':directions,'edges':edge1})
    edge2=[{'from_id':f,'to_id':d['id'],'relation':'diagnoses'} for d in diagnoses for f in d['direction_refs']]
    edge2 += [{'from_id':f,'to_id':a['id'],'relation':'addresses'} for a in actions for f in a['diagnosis_refs']]
    stages.append({'stage':'actions','run':deepcopy(run),'unresolved':deepcopy(unresolved),'diagnoses':diagnoses,'actions':actions,'edges':edge2})
    keypoints=[{'id':'S_'+k,'text':next(a['implication']['change_direction'] for a in areas if a['area_id']==k),
                'fact_refs':next(a['implication']['fact_refs'] for a in areas if a['area_id']==k),
                'criterion_refs':next(a['implication']['criterion_refs'] for a in areas if a['area_id']==k),'goal_refs':[]} for k in ('network','ai','telemetry')]
    stages.append({'stage':'summary','run':deepcopy(run),'unresolved':deepcopy(unresolved),
                   'summary':{'headline':'확인된 구성과 업무·운영 검증 공백을 분리합니다',
                              'message':'기존 구성의 활용 가능성과 실제 통제 효과를 구분한 현황 검토 후보입니다. 고객 목표·운영 효과·수집 이후 변경은 확인되지 않았으며 미확인을 결함·정상·점수로 바꾸지 않았습니다.',
                              'key_points':keypoints,'direction_refs':[r['id'] for r in directions],
                              'action_refs':[r['id'] for r in actions],
                              'decision_points':['고객 목표·업무 중요도와 우선순위 확인','현재 수집 범위·시점 이후 변경 확인','독립 내용·템플릿·실물 검토 후 고객 결과 승인']}})
    validator=contract(); validator.validate_input(data)
    for stage in stages:
        validator.validate_output(data,stage)
        data['stage_data']['previous_'+stage['stage']]=stage
    return {'schema':'ai-assessment-asis-candidate/r2','input':data,'stages':stages,
            'quality_status':'not_independently_reviewed','model_call_performed':False,
            'analysis_mode':'bounded_configuration_and_verification_candidate',
            'unresolved':unresolved, 'revision_context':revision_context,
            'requirements_application':requirements_application}
