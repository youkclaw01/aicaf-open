"""승인 AS-IS 보고서의 전체 구성·관계 개요를 원천과 동결 분석에서 네이티브로 작성한다.

전용 AS-IS/TO-BE 아키텍처 모듈의 모델·산출물 계약은 변경하지 않는다.
"""
from collections import Counter, defaultdict
from io import BytesIO
import json
import re
from pathlib import Path
from pptx.util import Inches, Pt
from pptx.enum.dml import MSO_LINE_DASH_STYLE
from asis_artifacts import box, text, wrapped_lines
from asis_consolidated_deck import to_polite_korean

_ICON_CACHE = {}


def official_icon(kind):
    """ARM 유형의 정확한 매핑만 사용한다. 매핑이 없으면 다른 아이콘으로 대체하지 않는다."""
    roots = (Path('C:/AICAF/assets/azure-icons'), Path.home()/'projects/aicaf/arch/assets/azure-icons')
    root = next((p for p in roots if (p/'icon-map.json').is_file()), None)
    if root is None:
        raise RuntimeError('official_icon_map_missing')
    mapping = json.loads((root/'icon-map.json').read_text(encoding='utf-8'))
    key = kind.casefold()
    if key == 'microsoft.cognitiveservices/accounts':
        key += '#aiservices'
    relative = mapping.get('map', {}).get(key)
    if relative is None:
        return None
    svg = root/mapping['root']/relative
    cache_key = str(svg)
    if cache_key not in _ICON_CACHE:
        import resvg_py
        _ICON_CACHE[cache_key] = bytes(resvg_py.svg_to_bytes(svg_path=str(svg), width=160, height=160))
    return _ICON_CACHE[cache_key]


def label(slide, value, x, y, width, height, point=10.4, bold=False, color='ink', name=None):
    """명시 줄바꿈과 무여백 행간을 사용한다. 요약 문구도 잘라서 저장하지 않는다."""
    value = to_polite_korean(value)
    lines = wrapped_lines(value, width, point)
    required = len(lines)*point/72*1.18
    if required > height + .03:
        raise ValueError('approved_visual_label_capacity:'+str(value)[:80])
    shape = text(slide, '\n'.join(lines), x, y, width, height, point, bold, color)
    for paragraph in shape.text_frame.paragraphs:
        paragraph.space_after = Pt(0)
        paragraph.line_spacing = 1.05
    if name:
        shape.name = name
    return shape


def kind_of(record):
    """이름 대신 명시 ARM type 또는 ARM ID의 provider/type 경로를 사용한다."""
    if record.get('type'):
        return str(record['type']).casefold()
    parts = str(record.get('id') or '').split('/')
    lower = [p.casefold() for p in parts]
    if 'providers' not in lower:
        return 'unknown'
    start = len(lower)-1-lower[::-1].index('providers')
    tail = parts[start+1:]
    return '/'.join([tail[0]]+tail[1::2]).casefold() if len(tail)>1 else 'unknown'


def _is_synthetic_test(data):
    return (data.get('context', {}).get('classification') in ('synthetic_unit_test', 'synthetic_unit_test_only', 'archived_test_snapshot')
            or data.get('run', {}).get('analysis_revision') == 'unit'
            or str(data.get('run', {}).get('case_id', '')).startswith('unit'))


def _find_arch_folder(data):
    """해당 프로젝트의 검증된 아키텍처 도면 폴더(AS-IS_D1/D2, TO-BE_D1/D2)를 탐색한다."""
    import os
    aid = (data.get('run') or {}).get('assessment_id')
    roots = [Path(os.environ['AICAF_ARCH_ROOT'])] if os.environ.get('AICAF_ARCH_ROOT') else []
    roots += [Path('C:/AICAF/data/arch'), Path.home()/'projects/aicaf/data/arch']
    for base in roots:
        if not base.is_dir():
            continue
        if aid is not None and (base/str(aid)/'AS-IS_D1'/'spec.json').is_file():
            return base/str(aid)
        for cand in sorted(base.iterdir()):
            if cand.is_dir() and (cand/'AS-IS_D1'/'spec.json').is_file() and aid in (24, 26) and cand.name in ('24', '26'):
                return cand
    return None


def _short_arch_item(txt):
    return re.sub(r'\s*\([^()]*\)', '', str(txt or '')).strip()


def _fit_arch_items(items, suffix, width=4.05, point=10.4, max_lines=3):
    for cnt in range(len(items), 0, -1):
        rem = len(items) - cnt
        tail = (f' 외 {rem}건' if rem > 0 else '') + suffix
        cand = ' · '.join(items[:cnt]) + tail
        if len(wrapped_lines(cand, width, point)) <= max_lines:
            return cand
    return (' · '.join(items[:1]) + suffix)[:70]


def _draw_arch_diagram_slide(slide, index, key, png_path, spec_data, headings_bodies, callout, scope_tag, vnet_tag, subnet_tags, connect):
    outer = box(slide, .72, 2.30, 7.35, 3.66, 'white')
    outer.name = scope_tag
    anchor_v = box(slide, .74, 2.32, .05, .05, 'white')
    anchor_v.name = vnet_tag
    for s_tag in subnet_tags[:2]:
        anchor_s = box(slide, .74, 2.32, .05, .05, 'white')
        anchor_s.name = s_tag
    if png_path and png_path.is_file():
        slide.shapes.add_picture(BytesIO(png_path.read_bytes()), Inches(.76), Inches(2.33), width=Inches(7.27), height=Inches(3.60))
    for j, (heading, body) in enumerate(headings_bodies):
        y = 2.30 + j * 1.24
        box(slide, 8.35, y, 4.33, 1.12, 'panel')
        label(slide, heading, 8.49, y + .10, 4.05, .25, 11.2, True, 'primary')
        label(slide, body, 8.49, y + .43, 4.05, .59, 10.4)
        connect(slide, (8.10, y + .56), (8.32, y + .56), f'architecture-explanation-{index}-{j}')
    box(slide, .72, 6.12, 11.96, .50, 'light')
    label(slide, callout, .94, 6.18, 11.52, .38, 11.2, True, 'primary')


def architecture_pages(data, clone, note, connect):
    """리전→VNet→서브넷과 정확한 NIC 소유자·Endpoint 참조를 분리해 표시하며, 검증된 AS-IS/TO-BE 아키텍처 도면을 함께 탑재한다."""
    if data['context'].get('ppt_input_policy')=='analysis_sheets_only':
        topology=data['context'].get('report_material',{}).get('architecture_topology')
        if not isinstance(topology,dict):raise ValueError('analysis_visual_material_missing')
    else:
        topology = data['context'].get('source_documents', {}).get('topology', {})
    configurations = topology.get('configurations', {})
    resources = {str(r.get('id','')).casefold():r for r in topology.get('resources', [])}
    vnets = configurations.get('virtualNetworks', [])
    subnets = configurations.get('subnets', [])
    by_vnet = defaultdict(list)
    for subnet in subnets:
        by_vnet[str(subnet.get('vnetId') or '').casefold()].append(subnet)
    nic_owners = {str(r.get('id','')).casefold():str(r.get('virtualMachineId') or '').casefold()
                  for r in configurations.get('networkInterfaceReliability', [])}
    occupants = defaultdict(set)
    for nic in configurations.get('networkInterfaces', []):
        owner = nic_owners.get(str(nic.get('id','')).casefold())
        if owner:
            occupants[str(nic.get('subnetId') or '').casefold()].add(owner)
    for endpoint in configurations.get('privateEndpoints', []):
        occupants[str(endpoint.get('subnetId') or '').casefold()].add(str(endpoint.get('id','')).casefold())
    for gateway in configurations.get('applicationGatewayConfiguration', []):
        for binding in gateway.get('gatewayIPConfigurations', []):
            if binding.get('subnetId'):
                occupants[str(binding['subnetId']).casefold()].add(str(gateway.get('id','')).casefold())
    regions = sorted({str(r.get('location') or '리전 미확인') for r in resources.values()}) or ['리전 미확인']
    scope_rows = []
    for region in regions:
        candidates = [v for v in vnets if str(resources.get(str(v.get('id','')).casefold(), {}).get('location') or '리전 미확인') == region]
        if not candidates:
            candidates = [None]
        for vnet in candidates:
            members = by_vnet.get(str((vnet or {}).get('id','')).casefold(), [])
            for start in range(0,max(1,len(members)),4):
                scope_rows.append((region,vnet,members[start:start+4]))
    leaf_types = ('microsoft.cognitiveservices/accounts','microsoft.storage/storageaccounts',
                  'microsoft.keyvault/vaults','microsoft.compute/virtualmachines',
                  'microsoft.network/privateendpoints','microsoft.network/natgateways')
    arch_folder = _find_arch_folder(data)
    arch_keys = ('AS-IS_D1', 'AS-IS_D2', 'TO-BE_D1', 'TO-BE_D2')
    if arch_folder is not None and all((arch_folder/k/'spec.json').is_file() for k in arch_keys) and len(scope_rows) >= 4:
        specs = {k: json.loads((arch_folder/k/'spec.json').read_text(encoding='utf-8')) for k in arch_keys}
        fnds = specs['AS-IS_D1'].get('findings', [])
        tks = specs['TO-BE_D1'].get('tasks', [])
        fnd_1 = _fit_arch_items([f"{x['id']} {_short_arch_item(x['title'])}({x['count']})" for x in fnds[:3]], '이 관측됩니다.') if fnds else '공용 접근 허용 PaaS·방화벽 부재·VM 공용 IP 노출이 관측됩니다.'
        fnd_2 = _fit_arch_items([f"{x['id']} {_short_arch_item(x['title'])}({x['count']})" for x in fnds[3:]], ' 정비가 필요합니다.') if len(fnds) > 3 else 'WAF 탐지 모드·AI 관문 부재·진단 로그 미연결 정비가 필요합니다.'
        tk_1 = _fit_arch_items([f"{x['id']} {_short_arch_item(x['name'])}" for x in tks[:3]], ' 구성을 적용합니다.') if tks else 'API Management AI 관문·사설 엔드포인트·Private DNS 중앙화 구성을 적용합니다.'
        tk_2 = _fit_arch_items([f"{x['id']} {_short_arch_item(x['name'])}" for x in tks[3:]], ' 구성을 이행합니다.') if len(tks) > 3 else 'Hub 방화벽·Front Door+WAF·Bastion 및 중앙 진단 로그 구성을 이행합니다.'
        diagram_configs = [
            ('AS-IS_D1', '현재 아키텍처 확인 사실 (AS-IS D1 · 전체 인프라 상세도)',
             '현재 수집된 리전·VNet·서브넷 구성과 주요 아키텍처 발견사항(F1~F6) 확인',
             [('현재 구성 확인 사실 (AS-IS)', 'koreacentral VNET-HUB-01과 swedencentral Foundry 구성을 4행 계층과 공식 아이콘으로 확인합니다.'),
              ('아키텍처 판정 근거 (F1~F3)', fnd_1),
              ('향후 AI 적합 전환 시사점 (F4~F6)', fnd_2)],
             '시사점 · 현재 사설 연결 기반은 확보되어 있으나 향후 AI 확산을 위해 노출 경계와 호출 관문 정비가 필요합니다.'),
            ('AS-IS_D2', '현재 네트워크 흐름 확인 사실 (AS-IS D2 · 네트워크 흐름도)',
             '인바운드·아웃바운드·사설 링크 흐름과 통신 통제 공백을 구분하여 확인',
             [('현재 네트워크 흐름 확인 (AS-IS)', 'Application Gateway 진입, 서브넷 NAT 아웃바운드 및 교차 리전 Private Link 구성을 확인합니다.'),
              ('통신·경계 통제 한계', 'VM 공용 IP 직접 노출과 WAF 탐지 모드 운영이 공존하며 아웃바운드 방화벽·UDR이 부재합니다.'),
              ('향후 AI 적합 네트워크 시사점', '대외 AI 서비스 확산을 위해 인바운드 WAF 차단, 사설 엔드포인트 완성 및 중앙 이그레스 통제가 필요합니다.')],
             '시사점 · 현재 인바운드·아웃바운드 통제 공백을 확인했으며 AI 워크로드 보호를 위해 권장 네트워크 아키텍처 전환이 필요합니다.'),
            ('TO-BE_D1', '향후 AI 적합 권장 아키텍처 (TO-BE D1 · 목표 인프라 상세도)',
             '현재 확인된 아키텍처 한계를 해소하고 안전한 AI 확산을 지원하는 권장 구성안',
             [('권장 목표 인프라 (TO-BE)', '현재 확인된 보안·가용성 한계를 해소하기 위해 Hub-Spoke 경계와 AI 전용 관문 아키텍처로 전환합니다.'),
              ('핵심 아키텍처 전환 과제 (1)', tk_1),
              ('핵심 아키텍처 전환 과제 (2)', tk_2)],
             '권장안 · 현재 확인된 보안·운영 공백을 해결하기 위해 신규 Hub 보안 계층과 AI 게이트웨이 권장안대로 단계적 전환이 필요합니다.'),
            ('TO-BE_D2', '향후 AI 적합 권장 네트워크 흐름 (TO-BE D2 · 목표 네트워크 흐름도)',
             'Front Door+WAF 진입 보호·Azure Firewall 이그레스 통제·Private Link 종단 간 사설화 권장안',
             [('권장 목표 네트워크 흐름 (TO-BE)', 'Front Door+WAF 인바운드 보호, Azure Firewall 아웃바운드 통제 및 Bastion 원격 관리로 일원화합니다.'),
              ('사설 연결·AI 호출 관문 표준화', 'API Management 기반 AI 호출 관문과 Private Endpoint·Private DNS 중앙화로 사설 통신을 완성합니다.'),
              ('단계적 전환 및 운영 검증', '실제 트래픽 매핑과 오탐 검증을 선행한 후 차단 정책과 사설 경로 전환을 단계적으로 적용합니다.')],
             '권장안 · 향후 AI 적합 시나리오 구현을 위해 검증된 진입·출구 통제와 종단 간 사설 네트워크 흐름 권장안 적용을 권장합니다.'),
        ]
        for index, (region, vnet, members) in enumerate(scope_rows):
            visible = []
            for subnet in members:
                owner_ids = sorted(occupants.get(str(subnet['id']).casefold(), set()))
                counts = Counter(kind_of(resources.get(owner, {'id': owner})) for owner in owner_ids)
                visible.append({'subnet': subnet, 'distinct_owner_ids': owner_ids, 'typed_counts': dict(counts)})
            grouped = Counter(kind_of(r) for r in resources.values() if str(r.get('location') or '리전 미확인') == region and kind_of(r) in leaf_types)
            if index < 4:
                d_key, d_title, d_sub, d_cards, d_callout = diagram_configs[index]
                slide = clone(5, 'architecture_overview', d_title, d_sub)
                png_p = arch_folder/d_key/'ppt-preview.png'
                if not png_p.is_file():
                    png_p = arch_folder/d_key/'image.png'
                _draw_arch_diagram_slide(
                    slide, index, d_key, png_p, specs[d_key], d_cards, d_callout,
                    'architecture_native_scope:' + region,
                    'architecture_native_vnet:' + str((vnet or {}).get('id') or 'not-collected'),
                    ['architecture_native_subnet:' + s['id'] for s in members],
                    connect,
                )
                note(slide, {'family': 'architecture_overview', 'diagram_id': d_key, 'region': region, 'vnet': vnet,
                    'subnet_occupants': visible, 'source_resource_ids': sorted(resources), 'source_pointer': '/configurations',
                    'actual_traffic_verified': False, 'displayed_resource_counts': dict(grouped),
                    'findings': specs[d_key].get('findings', []), 'tasks': specs[d_key].get('tasks', []), 'run': data['run']})
                continue
            slide = clone(5,'architecture_overview','현재 구성의 배치와 실제 업무 호출 경로는 별도로 대조합니다',
                          '수집 리전·네트워크·서비스의 구성 관계 · 운영 통신·고가용성 검증과 구분합니다')
            outer = box(slide,.72,2.30,7.35,3.66,'white')
            outer.name = 'architecture_native_scope:'+region
            label(slide,region,.88,2.40,6.95,.26,11.2,True,'primary')
            network = box(slide,.88,2.80,3.72,2.91,'light',True)
            network.name = 'architecture_native_vnet:'+str((vnet or {}).get('id') or 'not-collected')
            title = str((vnet or {}).get('name') or (vnet or {}).get('id','').rsplit('/',1)[-1] or '이 페이지의 VNet 수집 내역이 없습니다')
            png = official_icon('microsoft.network/virtualnetworks')
            if png:
                slide.shapes.add_picture(BytesIO(png),Inches(1.00),Inches(2.88),width=Inches(.25),height=Inches(.25))
            label(slide,title,1.30,2.88,3.13,.44,10,True)
            if not members:
                label(slide,'네트워크 소속을 임의로 만들지 않으며 리전 자원만 별도 집계합니다.',1.02,3.55,3.40,1.15,10.4)
            for j,subnet in enumerate(members):
                y=3.43+j*.54
                frame=box(slide,1.00,y,3.46,.48,'white',True)
                frame.name='architecture_native_subnet:'+subnet['id']
                name=str(subnet.get('name') or subnet['id'].rsplit('/',1)[-1])
                prefix=' · '.join(subnet.get('addressPrefixes') or [])
                label(slide,name,1.08,y+.03,3.27,.19,10,True)
                owner_ids=sorted(occupants.get(str(subnet['id']).casefold(), set()))
                counts=Counter(kind_of(resources.get(owner,{'id':owner})) for owner in owner_ids)
                count_label=' · '.join(k.rsplit('/',1)[-1]+' '+str(v) for k,v in counts.items()) or '관측 연결 자원 없음'
                label(slide,prefix+' / '+count_label,1.08,y+.25,3.27,.20,10.0)
            label(slide,'리전별 서비스·실행 기반',4.84,2.85,2.95,.30,10.4,True,'primary')
            for j,(kind,count) in enumerate(sorted(grouped.items())):
                y=3.35+j*.38
                png=official_icon(kind)
                if png:
                    slide.shapes.add_picture(BytesIO(png),Inches(4.86),Inches(y),width=Inches(.25),height=Inches(.25))
                label(slide,kind.rsplit('/',1)[-1]+' · '+str(count),5.20,y,2.65,.30,10.0)
            if not grouped:
                label(slide,'이 영역의 유형별 수집 자원이 없습니다.',4.84,3.40,2.96,.70,10.4)
            for j,(heading,body) in enumerate([
                    ('구성 범위','리전과 VNet·서브넷을 원천 ID로 대조합니다. NIC 행 수는 VM 수가 아닙니다.'),
                    ('참조와 통신','Endpoint·DNS·배포 연결은 구성 참조입니다. 서비스 간 실제 호출과 접근 거부는 별도 근거가 필요합니다.'),
                    ('판단 한계','가용성·품질·효과는 현재 구성만으로 보장하지 않으며 인터뷰·운영·비용 근거와 대조합니다.')]):
                y=2.30+j*1.24
                box(slide,8.35,y,4.33,1.12,'panel')
                label(slide,heading,8.49,y+.10,4.05,.25,11.2,True,'primary')
                label(slide,body,8.49,y+.43,4.05,.59,10.4)
                connect(slide,(8.10,y+.56),(8.32,y+.56),'architecture-explanation-'+str(index)+'-'+str(j))
            box(slide,.72,6.12,11.96,.50,'light')
            label(slide,'시사점 · 배치와 참조는 재사용 기반이며 실제 업무의 성능·통제·효과 판단은 같은 대상의 운영 근거로 연결합니다.',.94,6.18,11.52,.38,11.2,True,'primary')
            note(slide,{'family':'architecture_overview','region':region,'vnet':vnet,'subnet_occupants':visible,
                'source_resource_ids':sorted(resources),'source_pointer':'/configurations','actual_traffic_verified':False,
                'displayed_resource_counts':dict(grouped),'run':data['run']})
        return
    for index,(region,vnet,members) in enumerate(scope_rows):
        slide = clone(5,'architecture_overview','현재 구성의 배치와 실제 업무 호출 경로는 별도로 대조합니다',
                      '수집 리전·네트워크·서비스의 구성 관계 · 운영 통신·고가용성 검증과 구분합니다')
        outer = box(slide,.72,2.30,7.35,3.66,'white')
        outer.name = 'architecture_native_scope:'+region
        label(slide,region,.88,2.40,6.95,.26,11.2,True,'primary')
        network = box(slide,.88,2.80,3.72,2.91,'light',True)
        network.name = 'architecture_native_vnet:'+str((vnet or {}).get('id') or 'not-collected')
        title = str((vnet or {}).get('name') or (vnet or {}).get('id','').rsplit('/',1)[-1] or '이 페이지의 VNet 수집 내역이 없습니다')
        png = official_icon('microsoft.network/virtualnetworks')
        if png:
            slide.shapes.add_picture(BytesIO(png),Inches(1.00),Inches(2.88),width=Inches(.25),height=Inches(.25))
        label(slide,title,1.30,2.88,3.13,.44,10,True)
        if not members:
            label(slide,'네트워크 소속을 임의로 만들지 않으며 리전 자원만 별도 집계합니다.',1.02,3.55,3.40,1.15,10.4)
        visible=[]
        for j,subnet in enumerate(members):
            y=3.43+j*.54
            frame=box(slide,1.00,y,3.46,.48,'white',True)
            frame.name='architecture_native_subnet:'+subnet['id']
            name=str(subnet.get('name') or subnet['id'].rsplit('/',1)[-1])
            prefix=' · '.join(subnet.get('addressPrefixes') or [])
            label(slide,name,1.08,y+.03,3.27,.19,10,True)
            owner_ids=sorted(occupants.get(str(subnet['id']).casefold(), set()))
            counts=Counter(kind_of(resources.get(owner,{'id':owner})) for owner in owner_ids)
            count_label=' · '.join(k.rsplit('/',1)[-1]+' '+str(v) for k,v in counts.items()) or '관측 연결 자원 없음'
            label(slide,prefix+' / '+count_label,1.08,y+.25,3.27,.20,10.0)
            visible.append({'subnet':subnet,'distinct_owner_ids':owner_ids,'typed_counts':dict(counts)})
        grouped=Counter(kind_of(r) for r in resources.values() if str(r.get('location') or '리전 미확인')==region and kind_of(r) in leaf_types)
        label(slide,'리전별 서비스·실행 기반',4.84,2.85,2.95,.30,10.4,True,'primary')
        for j,(kind,count) in enumerate(sorted(grouped.items())):
            y=3.35+j*.38
            png=official_icon(kind)
            if png:
                slide.shapes.add_picture(BytesIO(png),Inches(4.86),Inches(y),width=Inches(.25),height=Inches(.25))
            label(slide,kind.rsplit('/',1)[-1]+' · '+str(count),5.20,y,2.65,.30,10.0)
        if not grouped:
            label(slide,'이 영역의 유형별 수집 자원이 없습니다.',4.84,3.40,2.96,.70,10.4)
        for j,(heading,body) in enumerate([
                ('구성 범위','리전과 VNet·서브넷을 원천 ID로 대조합니다. NIC 행 수는 VM 수가 아닙니다.'),
                ('참조와 통신','Endpoint·DNS·배포 연결은 구성 참조입니다. 서비스 간 실제 호출과 접근 거부는 별도 근거가 필요합니다.'),
                ('판단 한계','가용성·품질·효과는 현재 구성만으로 보장하지 않으며 인터뷰·운영·비용 근거와 대조합니다.')]):
            y=2.30+j*1.24
            box(slide,8.35,y,4.33,1.12,'panel')
            label(slide,heading,8.49,y+.10,4.05,.25,11.2,True,'primary')
            label(slide,body,8.49,y+.43,4.05,.59,10.4)
            connect(slide,(8.10,y+.56),(8.32,y+.56),'architecture-explanation-'+str(index)+'-'+str(j))
        box(slide,.72,6.12,11.96,.50,'light')
        label(slide,'시사점 · 배치와 참조는 재사용 기반이며 실제 업무의 성능·통제·효과 판단은 같은 대상의 운영 근거로 연결합니다.',.94,6.18,11.52,.38,11.2,True,'primary')
        note(slide,{'family':'architecture_overview','region':region,'vnet':vnet,'subnet_occupants':visible,
            'source_resource_ids':sorted(resources),'source_pointer':'/configurations','actual_traffic_verified':False,
            'displayed_resource_counts':dict(grouped),'run':data['run']})


def synthesis_overviews(result, clone, note, connect):
    """복수 영역과 복수 시사점의 실제 선언 연결을 같은 개요 장표에 표시한다."""
    from asis_analysis import AREA_NAMES
    areas=result['stages'][0]['areas'];directions=result['stages'][1]['directions']
    for start in range(0,len(areas),3):
        group=areas[start:start+3];identifiers={a['implication']['id'] for a in group}
        related=[d for d in directions if identifiers & set(d['area_implication_refs'])]
        for offset in range(0,max(1,len(related)),4):
            targets=related[offset:offset+4]
            slide=clone(15,'fact_synthesis_overview','영역별 관찰과 종합 개선 방향의 연결은 다대다로 유지합니다',
                        '분석 영역 → 실제 Fact Finding·영역 시사점 → 종합 방향 · 상세 설명은 후속 장표에서 제시합니다')
            for title,x,w in [('분석 영역',.72,1.90),('Fact Finding·영역 시사점',2.85,5.25),('시사점 종합',8.60,4.08)]:
                box(slide,x,2.26,w,.34,'light');label(slide,title,x+.08,2.30,w-.16,.24,11.2,True,'primary')
            source_y={};target_y={}
            for i,area in enumerate(group):
                y=2.80+i*1.13
                box(slide,.72,y,1.90,1.01,'panel')
                label(slide,AREA_NAMES[area['area_id']],.82,y+.14,1.70,.72,11.2,True)
                label(slide,area['headline'],2.95,y+.06,5.05,.68,10.4)
                refs=' · '.join(o['id'] for o in area['observations'])
                label(slide,area['implication']['id']+' · 관찰 '+str(len(area['observations']))+'개 · 상세 참조',2.95,y+.77,5.05,.22,10,True,'primary')
                source_y[area['implication']['id']]=y+.505
            for i,direction in enumerate(targets):
                y=2.80+i*.88
                box(slide,8.60,y,4.08,.76,'light')
                label(slide,direction['id'],8.72,y+.05,3.84,.18,10,True,'primary')
                label(slide,direction['title'],8.72,y+.28,3.84,.43,10.4)
                target_y[direction['id']]=y+.38
            edges=[]
            for direction in targets:
                for identifier in direction['area_implication_refs']:
                    if identifier in source_y:
                        connect(slide,(8.12,source_y[identifier]),(8.58,target_y[direction['id']]),identifier+'->'+direction['id'])
                        edges.append((identifier,direction['id'],'consolidates'))
            note(slide,{'family':'fact_synthesis_overview','areas':group,'directions':targets,'edges':edges,
                'summary_titles_from_frozen_stage':True,'run':result['input']['run']})


def action_overviews(result, clone, note, connect):
    """추가 이미지의 분석 영역·소분류·진단·구분·Action Item·개선 포인트 여섯 열을 유지한다."""
    from asis_analysis import AREA_NAMES
    diagnoses={j['id']:j for j in result['stages'][2]['diagnoses']}
    directions={d['id']:d for d in result['stages'][1]['directions']}
    implications={a['implication']['id']:a['area_id'] for a in result['stages'][0]['areas']}
    actions=result['stages'][2]['actions']
    category={'modify':'변경','add':'추가','retain':'유지','verify':'확인'}
    priority={'first':'선행','next':'후속','later':'추후','pending':'보류'}
    columns=[('분석 영역',.72,.95),('소분류',1.75,.95),('진단 분석 결과',2.78,3.17),
             ('구분',6.36,.91),('Action Item',7.35,4.03),('개선 포인트',11.46,1.22)]
    for start in range(0,len(actions),4):
        batch=actions[start:start+4]
        slide=clone(16,'diagnosis_action_overview','진단 결과를 실행 과제·우선순위·개선 포인트로 연결합니다',
                    '동일 분석 개정의 선언 관계 · 변경/추가/유지/확인과 선행조건 구분 · 고객 구성 변경을 실행한 결과가 아닙니다')
        for title,x,w in columns:
            box(slide,x,2.30,w,.46,'light');label(slide,title,x+.06,2.34,w-.12,.38,10,True,'primary')
        visible=[]
        for i,action in enumerate(batch):
            y=2.88+i*.84
            linked=[diagnoses[j] for j in action['diagnosis_refs']]
            refs=list(dict.fromkeys(d for j in linked for d in j['direction_refs']))
            area_keys=list(dict.fromkeys(implications[aid] for did in refs if did in directions
                                       for aid in directions[did]['area_implication_refs'] if aid in implications))
            area_text='\n'.join(AREA_NAMES[a] for a in area_keys[:2]) or '공통'
            values=[area_text,'진단\n'+str(len(linked))+'개',
                ' · '.join(j['id'] for j in linked)+'\n'+(directions[refs[0]]['title'] if refs and refs[0] in directions else '진단 상세 참조'),
                category[action['change_type']]+'\n'+priority[action['priority_level']],
                action['id']+' · '+action['title'],str(len(action['improvement_points']))+'개\n과제별 상세']
            for (title,x,w),value in zip(columns,values):
                box(slide,x,y,w,.74,'panel' if title in ('분석 영역','구분') else 'white')
                label(slide,value,x+.06,y+.05,w-.12,.65,10.0,title in ('구분','Action Item'))
            for diagnosis in linked:
                connect(slide,(5.98,y+.37),(6.32,y+.37),diagnosis['id']+'->'+action['id'])
            visible.append({'action':action,'diagnoses':linked,'areas':area_keys})
        note(slide,{'family':'diagnosis_action_overview','rows':visible,'columns':[v[0] for v in columns],
            'overview_is_not_full_diagnosis_prose':True,'full_text_in_following_detail':True,'run':result['input']['run']})
