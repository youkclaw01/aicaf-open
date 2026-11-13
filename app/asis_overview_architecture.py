"""AS-IS 현황 오버뷰 아키텍처: 리소스가 아니라 서비스 계층 단위의 개념 구성을 만든다.

한 장의 노드는 5~8개의 계층이고 노드에는 개수만 표시한다. 개별 리소스 이름은 쓰지 않는다. 연결선은 수집된 명시 관계가 있는 계층 사이에만 그린다.
이 모듈은 수집 데이터 -> 계층 노드 -> 계층 간 연결 계산(순수 함수)만 한다. 그리기는 asis_consolidated_deck이 한다.
"""
from collections import Counter, defaultdict

# 서비스 계층(표시 순서 = 위에서 아래). 리소스 유형 접두가 일치하는 첫 계층에 속한다. 어디에도 속하지 않으면 '기타'.
LAYERS = [
    ('edge', '진입·보호', ('microsoft.network/applicationgateways', 'microsoft.network/frontdoors', 'microsoft.cdn/', 'microsoft.network/azurefirewalls',
                         'microsoft.network/firewallpolicies', 'microsoft.network/applicationgatewaywebapplicationfirewallpolicies', 'microsoft.network/publicipaddresses',
                         'microsoft.network/dnszones', 'microsoft.domainregistration/', 'microsoft.certificateregistration/')),
    ('network', '네트워크 경계', ('microsoft.network/virtualnetworks', 'microsoft.network/networksecuritygroups', 'microsoft.network/natgateways',
                              'microsoft.network/privateendpoints', 'microsoft.network/privatednszones', 'microsoft.network/networkinterfaces',
                              'microsoft.network/routetables', 'microsoft.network/networkwatchers')),
    ('compute', '컴퓨트', ('microsoft.compute/virtualmachines', 'microsoft.compute/disks', 'microsoft.compute/virtualmachinescalesets', 'microsoft.web/',
                         'microsoft.containerservice/', 'microsoft.app/')),
    ('ai', 'AI 서비스', ('microsoft.cognitiveservices/', 'microsoft.machinelearningservices/', 'microsoft.search/')),
    ('data', '데이터', ('microsoft.storage/', 'microsoft.sql/', 'microsoft.documentdb/', 'microsoft.dbfor', 'microsoft.keyvault/')),
    ('observability', '관측·운영', ('microsoft.insights/', 'microsoft.operationalinsights/', 'microsoft.alertsmanagement/', 'microsoft.recoveryservices/')),
    ('identity', '아이덴티티·거버넌스', ('microsoft.managedidentity/', 'microsoft.authorization/', 'microsoft.policyinsights/')),
]
OTHER = ('other', '기타')
SERVICE_NAMES = {'networkinterfaces': '네트워크 인터페이스', 'networksecuritygroups': 'NSG', 'privateendpoints': '프라이빗 엔드포인트', 'privatednszones': '프라이빗 DNS',
                 'virtualnetworks': '가상 네트워크', 'natgateways': 'NAT 게이트웨이', 'networkwatchers': 'Network Watcher', 'publicipaddresses': '공용 IP',
                 'applicationgateways': 'Application Gateway', 'virtualmachines': '가상 머신', 'disks': '디스크', 'accounts': 'AI 서비스 계정',
                 'storageaccounts': '스토리지 계정', 'vaults': 'Key Vault', 'actiongroups': '경고 작업 그룹', 'userassignedidentities': '관리 ID',
                 'dnszones': 'DNS 영역', 'domains': '도메인', 'certificateorders': '인증서'}
# 계층 대표 아이콘(ARM 유형). 매핑이 없으면 아이콘 없이 라벨만 둔다(다른 아이콘으로 대체하지 않는다).
LAYER_ICONS = {'edge': 'microsoft.network/applicationgateways', 'network': 'microsoft.network/virtualnetworks', 'compute': 'microsoft.compute/virtualmachines',
               'ai': 'microsoft.cognitiveservices/accounts', 'data': 'microsoft.storage/storageaccounts', 'observability': 'microsoft.insights/actiongroups',
               'identity': 'microsoft.managedidentity/userassignedidentities'}
NODE_MAX = 8        # 한 장에 그리는 계층 노드 최대 수(오버뷰 수준)


def layer_of(resource_type):
    kind = str(resource_type or '').casefold()
    for key, label, prefixes in LAYERS:
        if any(kind.startswith(prefix) for prefix in prefixes):
            return key
    return OTHER[0]


def layer_label(key):
    return next((label for k, label, _ in LAYERS if k == key), OTHER[1])


def overview_nodes(resources):
    """리소스 목록에서 계층 노드를 만든다. 노드 = 계층, 값 = 리소스 수와 대표 유형(이름이 아니라 유형 이름·개수)이다.

    하위 구성 유형(virtualmachines/extensions 등)은 부모 리소스의 일부라 개수에서 제외한다(오버뷰에 부풀려 보이지 않게)."""
    counts = Counter(); types = defaultdict(Counter)
    for row in resources:
        kind = str(row.get('type', '')).casefold()
        if kind.count('/') > 1:
            continue
        key = layer_of(kind)
        counts[key] += 1
        types[key][SERVICE_NAMES.get(kind.rsplit('/', 1)[-1], kind.rsplit('/', 1)[-1])] += 1
    order = [k for k, _, _ in LAYERS] + [OTHER[0]]
    nodes = [{'layer': key, 'label': layer_label(key), 'count': counts[key], 'types': [name for name, _ in types[key].most_common(3)]}
             for key in order if counts[key]]
    return nodes


def overview_edges(resources, relationships):
    """수집된 명시 관계에서 계층 간 연결을 만든다. 추정(inferred)·관측 트래픽 단정은 제외하고 연결 수를 함께 둔다."""
    layer_by_id = {str(row.get('id', '')).casefold(): layer_of(row.get('type')) for row in resources}
    edges = Counter()
    for rel in relationships:
        if rel.get('inferred') or rel.get('observedTraffic'):
            continue
        a = layer_by_id.get(str(rel.get('sourceId', '')).casefold())
        b = layer_by_id.get(str(rel.get('targetId', '')).casefold())
        if a and b and a != b:
            edges[tuple(sorted((a, b)))] += 1
    return [{'from': a, 'to': b, 'count': n} for (a, b), n in sorted(edges.items())]


def focus_layers(fact_refs, fact_layers):
    """진단이 인용한 사실이 가리키는 계층. fact_layers는 사실 ID -> 계층 목록의 고정 지도이다(코드가 정한다, 모델이 고르지 않는다)."""
    layers = []
    for ref in fact_refs:
        for layer in fact_layers.get(ref, ()):
            if layer not in layers:
                layers.append(layer)
    return layers


# 사실 ID가 어떤 계층의 구성인지(수집 코드의 사실 이름 규칙). 새 사실 이름이 생기면 여기에 한 줄을 더한다.
FACT_LAYERS = {
    'F_T_subnets': ('network',), 'F_T_virtualNetworks': ('network',), 'F_T_networkInterfaces': ('network',), 'F_T_natGateways': ('network',),
    'F_T_privateEndpoints': ('network',), 'F_T_publicIps': ('edge',), 'F_T_applicationGateways': ('edge',), 'F_T_applicationGatewayConfiguration': ('edge',),
    'F_E_virtualMachineOs': ('compute',), 'F_T_computeReliability': ('compute',), 'F_E_vmssInstanceNics': ('compute',), 'F_T_autoscaleSettings': ('compute',),
    'F_E_aiDeployments': ('ai',), 'F_T_foundryAccountConfiguration': ('ai',), 'F_T_containerApps': ('compute',),
    'F_T_machineLearningComputeConfiguration': ('ai',), 'F_E_aiMetricsTokensDaily': ('ai', 'observability'),
    'F_T_dataServiceConfiguration': ('data',), 'F_T_storageReliability': ('data',), 'F_T_recoveryBackup': ('data',), 'F_T_recoveryVaultConfiguration': ('data',),
    'F_E_diagnosticSettingsExtended': ('observability',), 'F_E_alertRules': ('observability',), 'F_T_networkReliability': ('network',),
    'F_T_networkInterfaceReliability': ('network',), 'F_T_rbacRoleAssignments': ('identity',), 'F_T_policyAssignments': ('identity',),
    'F_E_managementGroupChain': ('identity',), 'F_E_costByResource': ('compute', 'ai', 'data'), 'F_D_cost_monthly': ('ai',),
}


# ── 복잡한 개념 아키텍처의 그림 생성(Foundry gpt-image) 계약 ────────────────────────────────────────────────
# 계층 7개 이하의 단순한 개념도는 네이티브 도형으로 그리고(편집 가능), 노드가 많거나 연결이 복잡한 경우만 그림으로 만든다.
# 그림 모델은 정해진 계층 이름과 개수만 그린다(새 이름·새 수치 금지). 그린 결과는 판독 모델이 읽어 계층별 존재를 코드가 검사한다.
IMAGE_MODEL = 'gpt-image-2.5-sunburst'      # Foundry 배포(영석님 환경에 이미 연결됨, arch_job.IMG_MODEL과 동일)
IMAGE_NODE_THRESHOLD = 7                    # 계층 노드가 이 수를 넘거나 연결이 NATIVE_EDGE_MAX를 넘으면 그림으로 만든다
NATIVE_EDGE_MAX = 6
IMAGE_MIN_WIDTH_IN = 6.5                    # 이미지 속 글자가 읽히려면 장표에서 이 폭 이상으로 둔다(10pt 규칙의 이미지판)
IMAGE_LABEL_MAX = 22


def needs_image(nodes, edges):
    """단순한 개념도는 네이티브 도형, 복잡한 것만 그림. 판단은 코드가 한다."""
    return len(nodes) > IMAGE_NODE_THRESHOLD or len(edges) > NATIVE_EDGE_MAX


def image_skeleton(nodes, edges, focus, gaps, title):
    """그림 모델에 보낼 고정 골격. 노드·연결·강조·갭의 이름과 개수가 이미 정해져 있어 모델은 더하거나 빼지 못한다."""
    labels = {n['layer']: n['label'] for n in nodes}
    return {'title': title,
            'nodes': [{'id': n['layer'], 'label': n['label'], 'count': n['count'], 'focus': n['layer'] in focus, 'gap': n['layer'] in gaps} for n in nodes],
            'edges': [{'from': labels[e['from']], 'to': labels[e['to']], 'count': e['count']} for e in edges if e['from'] in labels and e['to'] in labels]}


def image_prompt(skeleton, fix=None):
    """개념 아키텍처 그림 프롬프트(영문 지시, 라벨은 한국어). 상세 리소스가 아니라 큰 범주의 개념도임을 명시한다."""
    labels = '; '.join('%s (%d)' % (n['label'], n['count']) for n in skeleton['nodes'])
    emphasis = '; '.join(n['label'] for n in skeleton['nodes'] if n['focus'])
    gaps = '; '.join(n['label'] for n in skeleton['nodes'] if n['gap'])
    links = '; '.join('%s - %s' % (e['from'], e['to']) for e in skeleton['edges'])
    p = ('Draw a CONCEPTUAL, high-level Microsoft Azure architecture overview diagram: large service-category blocks only, no individual resources, '
         'no resource names, no CIDR, no regions. White background, thin steel-blue (#326B91) outlines, flat style, one official-style Azure icon per block, '
         'the block label below or inside in Korean, a small count badge on each block. 16:9 landscape, generous margins, every label at least 14px tall. '
         'Draw EXACTLY these blocks, each once, with exactly this Korean label and count: ' + labels + '. '
         'Draw straight orthogonal connectors ONLY between these pairs: ' + (links or 'none') + '. '
         'Highlight these blocks with a thick blue outline: ' + (emphasis or 'none') + '. '
         'Mark these blocks with an orange dashed outline and the Korean word 확인 필요 as the gap: ' + (gaps or 'none') + '. '
         'Do not add any other service, label, legend text or decoration. Title: ' + skeleton['title'] + '.')
    if fix:
        p += ' REVISION of the previous drawing: keep everything else, fix only: ' + fix
    return p


def check_image_reading(skeleton, reading):
    """판독 모델의 결과(JSON)로 그림을 검사한다: 정해진 블록이 모두 있고, 없는 블록이 추가되지 않았는지. 위반 항목과 수정 지시를 돌려준다."""
    found = {row.get('id') for row in reading.get('nodes', []) if row.get('found')}
    missing = [n['label'] for n in skeleton['nodes'] if n['id'] not in found]
    extras = [row.get('label') for row in reading.get('extras', []) if row.get('label')]
    fails = []
    if missing:
        fails.append({'code': 'I1', 'detail': 'missing blocks: ' + ', '.join(missing), 'fix': 'Draw the missing blocks exactly as specified: ' + ', '.join(missing) + '.'})
    if extras:
        fails.append({'code': 'I2', 'detail': 'extra blocks: ' + ', '.join(extras), 'fix': 'Remove these blocks that were not requested: ' + ', '.join(extras) + '.'})
    return fails
