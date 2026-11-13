"""인터뷰 목표·비저닝·페인포인트 및 정책 분석 장표 생성 모듈.

영석님이 확정해 주신 『1_Goal-and-Policy-Analysis_youk.pptx』(`asis_assets/goal-policy-template.pptx`)의
표·카드·제목·폰트(맑은 고딕)·글자 크기·박스 좌표를 1pt도 변경하지 않고 그대로 템플릿으로 고정하여 복제하며,
AI(MAF)는 해당 표와 칸의 용량 계약에 맞춰 의견과 내용을 작성하는 역할을 수행한다.
"""
from copy import deepcopy
import json
from lxml import etree
from pptx import Presentation
from pptx.oxml import parse_xml
from asis_model import ASSET
from asis_artifacts import wrapped_lines

TEMPLATE_PATH = ASSET / 'goal-policy-template.pptx'
FONT_NAME = '맑은 고딕'
DRAWING_NS = 'http://schemas.openxmlformats.org/drawingml/2006/main'

VISION_DOMAINS = (
    '업무 처리 효율화·성과',
    '응답 품질·지식 최신성',
    '현업 확산·대외 연계',
    '업무 자동화·운영 체계',
)

POLICY_DOMAINS = (
    'ID · 인증 통제',
    '모델 · 호출 관문',
    '데이터 · 문서 권한',
    '용량 · 가용성 · 관측',
    '비용 · 투자 성과',
)

FACT_GROUP_TITLES = (
    '목표 및 비저닝 현황',
    '현업 페인포인트 및 확장 제약',
    '운영 · 보안 정책 및 실측 검증 현황',
)

LIMITS = {
    'subtitle': 140,
    'vision_domain': 16,
    'vision_goal': 20,
    'vision_pain_point': 34,
    'vision_blocker': 16,
    'policy_domain': 14,
    'policy_standard': 16,
    'policy_current': 18,
    'policy_verified': 14,
    'policy_gap': 30,
    'fact_title': 22,
    'fact_bullet': 65,
    'implication_box': 130,
}

GOAL_POLICY_INSTRUCTION = (
    '인터뷰 및 실측 사실에서 추출된 고객 목표·비저닝·현업 페인포인트와 AI 운영·보안·거버넌스 정책 현황을 교차 분석하여, '
    '고정된 『목표 및 정책 분석』 템플릿 장표의 각 표·카드·Fact Finding·시사점 칸에 들어갈 내용을 한국어 경어체(~합니다)와 개조식 명사구로 작성한다. '
    '합성 인터뷰의 가상 시나리오 고유명(예: 상담 요약, 영업 제안서 초안 작성 등)은 표면에 직접 노출하지 않고 고객 전달용 컨설팅 표현으로 일반화하되, '
    '입력에 있는 수치·실측 설정(50% 대 30%, 일 1회 복제, 2명, 로컬 인증 차단, APIM 0건, 정책 4개 면제, 경보 0건, HTTP 429 118건, 종량제·미태그 등)은 왜곡 없이 보존한다. '
    '응답은 반드시 {"goal_policy_analysis": {'
    '"subtitle": "상단 요약 설명 1문장(~합니다, 130자 이하)", '
    '"vision_rows": [{"domain": "업무 처리 효율화·성과", "goal": "목표 명사구(16자 이하)", "pain_point": "페인포인트·현황 명사구(30자 이하)", "blocker": "핵심 과제 명사구(13자 이하)"} (정확히 4개)], '
    '"policy_rows": [{"domain": "ID · 인증 통제", "standard": "목표 정책 기준(14자 이하)", "current": "인터뷰 응답 현황(15자 이하)", "verified": "실측·통제 상태(11자 이하)", "gap": "핵심 정책 격차·위험(24자 이하)"} (정확히 5개)], '
    '"fact_groups": [{"title": "소제목", "bullets": ["완결된 경어체 문장1(~합니다, 58자 이하)", "완결된 경어체 문장2(~합니다, 58자 이하)"]} (정확히 3개 그룹, 각 2개 문장)], '
    '"implication_box": "우측 하단 시사점 종합 1~2문장(~합니다, 120자 이하)"'
    '}} JSON만 반환한다. 도구는 사용하지 않는다.'
)


def extract_goal_policy_evidence(data):
    """분석 원장(input)에서 인터뷰 목표·페인포인트·정책 관련 사실과 핵심 실측 지표를 추출한다."""
    ctx = (data.get('context') or {}) if isinstance(data, dict) else {}
    goals = [
        {
            'id': g.get('id'),
            'Quote': str(g.get('quote') or '')[:140],
            'type': g.get('classification'),
        }
        for g in (ctx.get('goal_interpretations') or [])[:15]
        if isinstance(g, dict)
    ]
    iv_topics = {}
    for f in (data.get('facts') or []):
        if not isinstance(f, dict):
            continue
        fid = str(f.get('id') or '')
        if fid.startswith('F_IV_') and fid.endswith('_summary'):
            parts = fid.split('_')
            topic = parts[3] if len(parts) > 3 else fid
            val = f.get('value')
            if isinstance(val, dict):
                iv_topics.setdefault(topic, []).append({
                    'question': str(val.get('question') or '')[:80],
                    'summary': str(val.get('summary') or '')[:160],
                    'score': val.get('score'),
                })
    selected_topics = {
        k: iv_topics[k][:3]
        for k in ('XA1', 'XA2', 'XA3', 'XA4', 'XA5', 'XB1', 'XB2', 'XB4', 'XB5', 'XC1', 'XC2', 'XC3', 'XC4', 'XD1', 'XD2', 'XD4', 'XD5',
                  'A1', 'A2', 'A3', 'A4', 'A5', 'B1', 'B2', 'B4', 'B5', 'C1', 'C2', 'C3', 'C4', 'D1', 'D2', 'D4', 'D5')
        if k in iv_topics
    }
    return {
        'track': ctx.get('track') or 'existing',
        'goal_interpretations': goals,
        'interview_topics': selected_topics,
    }


def _clean_text(val, limit, field):
    import asis_consolidated_deck as consolidated
    s = consolidated.clean_surface_text(str(val or '')).replace('\n', ' ').strip()
    if not s:
        raise ValueError(f'goal_policy_empty:{field}')
    if len(s) > limit:
        raise ValueError(f'goal_policy_too_long:{field}:{len(s)}>{limit}:{s}')
    return s


def validate_goal_policy_spec(raw):
    """AI가 생성한 목표 및 정책 분석 명세가 고정 템플릿 칸 용량 계약을 준수하는지 검증한다."""
    import asis_consolidated_deck as consolidated
    if not isinstance(raw, dict):
        raise ValueError('goal_policy_not_dict')
    spec = raw.get('goal_policy_analysis') if 'goal_policy_analysis' in raw else raw
    if not isinstance(spec, dict):
        raise ValueError('goal_policy_spec_not_dict')

    subtitle = consolidated.to_polite_korean(_clean_text(spec.get('subtitle'), LIMITS['subtitle'], 'subtitle'))

    v_in = spec.get('vision_rows')
    if not isinstance(v_in, list) or len(v_in) != 4:
        raise ValueError('goal_policy_vision_rows_count')
    vision_rows = []
    for idx, r in enumerate(v_in):
        if not isinstance(r, dict):
            raise ValueError(f'goal_policy_vision_row_type:{idx}')
        domain = _clean_text(r.get('domain') or VISION_DOMAINS[idx], LIMITS['vision_domain'], f'vision_domain_{idx}')
        goal = _clean_text(r.get('goal'), LIMITS['vision_goal'], f'vision_goal_{idx}')
        if goal.startswith('목표'):
            goal = goal.lstrip('목표').lstrip(' ·:').strip()
        pain = _clean_text(r.get('pain_point'), LIMITS['vision_pain_point'], f'vision_pain_{idx}')
        blocker = _clean_text(r.get('blocker'), LIMITS['vision_blocker'], f'vision_blocker_{idx}')
        if blocker.startswith('과제'):
            blocker = blocker.lstrip('과제').lstrip(' ·:').strip()
        vision_rows.append({'domain': domain, 'goal': goal, 'pain_point': pain, 'blocker': blocker})

    p_in = spec.get('policy_rows')
    if not isinstance(p_in, list) or len(p_in) != 5:
        raise ValueError('goal_policy_policy_rows_count')
    policy_rows = []
    for idx, r in enumerate(p_in):
        if not isinstance(r, dict):
            raise ValueError(f'goal_policy_policy_row_type:{idx}')
        domain = _clean_text(r.get('domain') or POLICY_DOMAINS[idx], LIMITS['policy_domain'], f'policy_domain_{idx}')
        standard = _clean_text(r.get('standard'), LIMITS['policy_standard'], f'policy_standard_{idx}')
        current = _clean_text(r.get('current'), LIMITS['policy_current'], f'policy_current_{idx}')
        verified = _clean_text(r.get('verified'), LIMITS['policy_verified'], f'policy_verified_{idx}')
        gap = _clean_text(r.get('gap'), LIMITS['policy_gap'], f'policy_gap_{idx}')
        policy_rows.append({
            'domain': domain,
            'standard': standard,
            'current': current,
            'verified': verified,
            'gap': gap,
        })

    fg_in = spec.get('fact_groups')
    if not isinstance(fg_in, list) or len(fg_in) != 3:
        raise ValueError('goal_policy_fact_groups_count')
    fact_groups = []
    for idx, g in enumerate(fg_in):
        if not isinstance(g, dict):
            raise ValueError(f'goal_policy_fact_group_type:{idx}')
        title = _clean_text(g.get('title') or FACT_GROUP_TITLES[idx], LIMITS['fact_title'], f'fact_title_{idx}')
        bullets_in = g.get('bullets')
        if not isinstance(bullets_in, list) or len(bullets_in) != 2:
            raise ValueError(f'goal_policy_fact_bullets_count:{idx}')
        bullets = [
            consolidated.to_polite_korean(_clean_text(b.lstrip('•·- '), LIMITS['fact_bullet'], f'fact_bullet_{idx}_{b_i}'))
            for b_i, b in enumerate(bullets_in)
        ]
        fact_groups.append({'title': title, 'bullets': bullets})

    imp_box = consolidated.to_polite_korean(_clean_text(spec.get('implication_box'), LIMITS['implication_box'], 'implication_box'))

    return {
        'subtitle': subtitle,
        'vision_rows': vision_rows,
        'policy_rows': policy_rows,
        'fact_groups': fact_groups,
        'implication_box': imp_box,
    }


def fallback_goal_policy_spec(result):
    """기존 동결 리비전(analysis.json에 goal_policy_analysis가 없는 경우)에서도 결정론적으로 동작하는 고객 전달용 기본 명세."""
    raw = (result or {}).get('goal_policy_analysis')
    if isinstance(raw, dict):
        try:
            return validate_goal_policy_spec(raw)
        except ValueError:
            pass
    default_spec = {
        'subtitle': (
            '사내 업무 지원 AI를 운영하며 현업 부서 확산과 대외 채널 연계를 추진 중이나, 목표 대비 체감 성과 격차 및 '
            '문서 권한·보안·관측 정책 공백이 존재하여 전사 확산을 위한 기술 통제 및 거버넌스 기준 확립이 필요한 상태입니다.'
        ),
        'vision_rows': [
            {
                'domain': '업무 처리 효율화·성과',
                'goal': '처리시간 50% 단축·성과 확보',
                'pain_point': '체감 단축 약 30% 수준 및 정량적 효과 측정 기준 부재',
                'blocker': '성과 측정 · ROI 기준',
            },
            {
                'domain': '응답 품질·지식 최신성',
                'goal': '양식 준수·최신 지식 즉시 반영',
                'pain_point': '답변 장황·표 양식 깨짐 및 야간 복제로 개정 규정 지연',
                'blocker': '출력 구조화 · 색인 개선',
            },
            {
                'domain': '현업 확산·대외 연계',
                'goal': '타 부서 확산·고객 채널 연계',
                'pain_point': '문서 권한 미분리 일괄 복제로 비인가 문서 노출 우려',
                'blocker': '문서 권한 분리 · 관문',
            },
            {
                'domain': '업무 자동화·운영 체계',
                'goal': '다단계 업무 자동화·안정 운영',
                'pain_point': '보안 우려로 자동화 보류 및 소수 인력 수동 관리 부담',
                'blocker': '자동화 보안 · 운영 표준',
            },
        ],
        'policy_rows': [
            {
                'domain': 'ID · 인증 통제',
                'standard': '관리 ID · 최소 권한',
                'current': '환경변수 키 · 퇴사시 교체',
                'verified': '로컬 인증 차단됨',
                'gap': '정적 키 관리 개선 및 정기 교체 주기 수립 필요',
            },
            {
                'domain': '모델 · 호출 관문',
                'standard': '허용 모델 · 중앙 관문',
                'current': '앱 직접 연결 · 차단 없음',
                'verified': 'APIM 0건·정책면제',
                'gap': '상위 AI 정책 면제 해제 및 중앙 호출 관문 부재',
            },
            {
                'domain': '데이터 · 문서 권한',
                'standard': '문서 권한 연동·보관정책',
                'current': '일괄 복제 · 보관기간 미정',
                'verified': '권한 필터링 미적용',
                'gap': '검색 문서 열람 권한 필터링 및 보관 기준 부재',
            },
            {
                'domain': '용량 · 가용성 · 관측',
                'standard': '사전 한도 감시·통합 경보',
                'current': '오류 후 인지 · 개별 로그',
                'verified': '경보 0건·429 발생',
                'gap': '사전 한도 감시·자동 전환 및 통합 경보 미구축',
            },
            {
                'domain': '비용 · 투자 성과',
                'standard': '부서별 정산 · 투자 기준',
                'current': '구독 총액 확인·기준 부재',
                'verified': '종량제 · 미태그 편중',
                'gap': '미태그 편중으로 부서별 비용 정산·ROI 산출 불가',
            },
        ],
        'fact_groups': [
            {
                'title': '목표 및 비저닝 현황',
                'bullets': [
                    '업무 시간 50% 단축을 목표로 사내 AI를 운영 중이나 체감효과는 약 30% 수준에 머물러 있습니다.',
                    '현업 부서 확산과 대외 채널 연계를 검토 중이나 보안 우려로 다단계 자동 처리는 보류되었습니다.',
                ],
            },
            {
                'title': '현업 페인포인트 및 확장 제약',
                'bullets': [
                    '답변 장황함과 표 양식 깨짐으로 재작업 부담이 크고 야간 일 1회 복제로 규정 반영이 지연됩니다.',
                    '문서 권한 미정비 일괄 복제로 비인가 문서 노출 우려가 크며 소수 인력 수동 관리 부담이 높습니다.',
                ],
            },
            {
                'title': '운영 · 보안 정책 및 실측 검증 현황',
                'bullets': [
                    '실측된 사설 연결·로컬 인증 차단 설정이 인터뷰 응답과 상이하며 상위 AI 정책 4개는 면제 상태입니다.',
                    '중앙 관문·경보(0건) 부재 속에 한도 초과(429)가 발생했고 미태그 편중으로 부서별 정산이 어렵습니다.',
                ],
            },
        ],
        'implication_box': (
            '현업 부서 확산 및 대외 서비스 연계를 위해 원본 문서 열람 권한 연동과 출력 품질 개선이 선행되어야 하며, '
            '중앙 AI 관문·통합 경보·정책 강제 및 부서별 비용 배부 기준을 표준화하여 안정적인 확장 기반을 마련해야 합니다.'
        ),
    }
    return validate_goal_policy_spec(default_spec)


def _replace_shape_text_preserving_style(shape, lines):
    """템플릿 도형의 위치·크기·폰트('맑은 고딕')·글자 크기·색상·정렬·줄간격·불릿 서식을 그대로 보존하면서 텍스트만 교체한다."""
    if isinstance(lines, str):
        lines = lines.split('\n')
    lines = [str(x) for x in lines if str(x)] or ['']
    tf = shape.text_frame
    txBody = tf._txBody

    # Ensure lstStyle defRPr also has latin/ea/cs = '맑은 고딕' so PowerPoint/LibreOffice never fall back to serif
    for def_rpr in txBody.findall(f'.//{{{DRAWING_NS}}}defRPr'):
        for script in ('latin', 'ea', 'cs'):
            sc = def_rpr.find(f'{{{DRAWING_NS}}}{script}')
            if sc is None:
                sc = etree.SubElement(def_rpr, f'{{{DRAWING_NS}}}{script}')
            sc.set('typeface', FONT_NAME)

    existing_ps = txBody.findall(f'{{{DRAWING_NS}}}p')
    proto_p = deepcopy(existing_ps[0]) if existing_ps else etree.Element(f'{{{DRAWING_NS}}}p')
    proto_pPr = proto_p.find(f'{{{DRAWING_NS}}}pPr')
    proto_runs = proto_p.findall(f'{{{DRAWING_NS}}}r')
    proto_rPr = deepcopy(proto_runs[0].find(f'{{{DRAWING_NS}}}rPr')) if (proto_runs and proto_runs[0].find(f'{{{DRAWING_NS}}}rPr') is not None) else etree.Element(f'{{{DRAWING_NS}}}rPr')

    for script in ('latin', 'ea', 'cs'):
        sc = proto_rPr.find(f'{{{DRAWING_NS}}}{script}')
        if sc is None:
            sc = etree.SubElement(proto_rPr, f'{{{DRAWING_NS}}}{script}')
        sc.set('typeface', FONT_NAME)
    for attr in ('err', 'dirty', 'smtClean'):
        proto_rPr.attrib.pop(attr, None)

    for old_p in existing_ps:
        txBody.remove(old_p)

    for line_str in lines:
        new_p = etree.SubElement(txBody, f'{{{DRAWING_NS}}}p')
        if proto_pPr is not None:
            new_p.append(deepcopy(proto_pPr))
        new_r = etree.SubElement(new_p, f'{{{DRAWING_NS}}}r')
        new_r.append(deepcopy(proto_rPr))
        new_t = etree.SubElement(new_r, f'{{{DRAWING_NS}}}t')
        new_t.text = line_str


def draw_goal_policy_slide(deck, donors, manifest, result):
    """영석님이 지정하신 `goal-policy-template.pptx` 슬라이드의 모든 도형·표·폰트·크기를 그대로 복제하고 AI 텍스트만 주입한다."""
    from pptx.util import Inches
    spec = fallback_goal_policy_spec(result)
    tmpl_deck = Presentation(str(TEMPLATE_PATH))
    donor = tmpl_deck.slides[0]

    # Use donor 6 layout from content-asis-e.pptx so slideMaster/slideLayout match exactly across fragments
    target_layout = donors[6].slide_layout
    slide = deck.slides.add_slide(target_layout)
    for sh in list(slide.shapes):
        slide.shapes._spTree.remove(sh._element)

    mapping = {}
    for rel in donor.part.rels.values():
        if rel.reltype.endswith('/notesSlide') or rel.reltype.endswith('/slideLayout'):
            continue
        mapping[rel.rId] = slide.part.relate_to(
            rel.target_ref if rel.is_external else rel.target_part,
            rel.reltype,
            is_external=rel.is_external,
        )
    for sh in donor.shapes:
        if sh.name == 'TextBox 211':
            # Page number textbox at the end is added by asis_approved_presentation's global page numbering loop
            continue
        node = parse_xml(sh._element.xml)
        for child in node.iter():
            for key, val in list(child.attrib.items()):
                if key.startswith('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}') and val in mapping:
                    child.set(key, mapping[val])
        slide.shapes._spTree.insert_element_before(node, 'p:extLst')

    by_name = {sh.name: sh for sh in slide.shapes}
    if 'Text 0' in by_name:
        by_name['Text 0'].name = 'approved_kicker:goal_policy_analysis'
    marker = slide.shapes.add_textbox(Inches(.62), Inches(.28), Inches(.01), Inches(.01))
    marker.name = 'approved_family:goal_policy_analysis'

    # 1. 상단 타이틀·서브타이틀
    if 'Text 1' in by_name:
        _replace_shape_text_preserving_style(by_name['Text 1'], ['목표 및 정책 분석'])
    if 'Text 2' in by_name:
        _replace_shape_text_preserving_style(by_name['Text 2'], [spec['subtitle']])

    # 2. 좌측 상단: 4대 비저닝·페인포인트 카드 (TextBox 102~131)
    vision_shape_Wait = (
        ('TextBox 102', 'TextBox 103', 'TextBox 105', 'TextBox 107'),
        ('TextBox 110', 'TextBox 111', 'TextBox 113', 'TextBox 115'),
        ('TextBox 118', 'TextBox 119', 'TextBox 121', 'TextBox 123'),
        ('TextBox 126', 'TextBox 127', 'TextBox 129', 'TextBox 131'),
    )
    for row, (dom_n, goal_n, pain_n, blk_n) in zip(spec['vision_rows'], vision_shape_Wait):
        _replace_shape_text_preserving_style(by_name[dom_n], [row['domain']])
        goal_lines = wrapped_lines('목표 · ' + row['goal'], 1.70, 9.0)[:2]
        _replace_shape_text_preserving_style(by_name[goal_n], goal_lines)
        pain_lines = wrapped_lines(row['pain_point'], 1.66, 9.0)[:3]
        _replace_shape_text_preserving_style(by_name[pain_n], pain_lines)
        _replace_shape_text_preserving_style(by_name[blk_n], [row['blocker']])

    # 3. 좌측 하단: 5행 정책 비교 표 (TextBox 145~193)
    policy_shape_names = (
        ('TextBox 145', 'TextBox 147', 'TextBox 149', 'TextBox 151', 'TextBox 153'),
        ('TextBox 155', 'TextBox 157', 'TextBox 159', 'TextBox 161', 'TextBox 163'),
        ('TextBox 165', 'TextBox 167', 'TextBox 169', 'TextBox 171', 'TextBox 173'),
        ('TextBox 175', 'TextBox 177', 'TextBox 179', 'TextBox 181', 'TextBox 183'),
        ('TextBox 185', 'TextBox 187', 'TextBox 189', 'TextBox 191', 'TextBox 193'),
    )
    for row, (dom_n, std_n, cur_n, ver_n, gap_n) in zip(spec['policy_rows'], policy_shape_names):
        _replace_shape_text_preserving_style(by_name[dom_n], wrapped_lines(row['domain'], 1.16, 9.0)[:2])
        _replace_shape_text_preserving_style(by_name[std_n], wrapped_lines(row['standard'], 1.34, 9.0)[:2])
        _replace_shape_text_preserving_style(by_name[cur_n], wrapped_lines(row['current'], 1.48, 9.0)[:2])
        _replace_shape_text_preserving_style(by_name[ver_n], wrapped_lines(row['verified'], 1.22, 9.0)[:2])
        _replace_shape_text_preserving_style(by_name[gap_n], wrapped_lines(row['gap'], 1.90, 9.0)[:2])

    # 4. 우측 상단: Fact Finding 3개 그룹 × 2개 불릿 (TextBox 197~205)
    fact_shape_names = (
        ('TextBox 197', ('TextBox 198', 'TextBox 199')),
        ('TextBox 200', ('TextBox 201', 'TextBox 202')),
        ('TextBox 203', ('TextBox 204', 'TextBox 205')),
    )
    for grp, (title_n, (b1_n, b2_n)) in zip(spec['fact_groups'], fact_shape_names):
        _replace_shape_text_preserving_style(by_name[title_n], [grp['title']])
        # TextBox 205 in template had height 0.1618in so 2nd line was clipped; ensure height=0.34in like TextBox 198~204
        for bn, b_text in ((b1_n, grp['bullets'][0]), (b2_n, grp['bullets'][1])):
            sh_b = by_name[bn]
            if sh_b.height < int(0.33 * 914400):
                sh_b.height = int(0.34 * 914400)
            _replace_shape_text_preserving_style(sh_b, [b_text])

    # 5. 우측 하단: 시사점 박스 (TextBox 210, 11.0pt Bold 맑은 고딕)
    imp_lines = wrapped_lines(spec['implication_box'], 3.96, 11.0)[:4]
    _replace_shape_text_preserving_style(by_name['TextBox 210'], imp_lines)

    payload = {'family': 'goal_policy_analysis', 'source_page': 7, 'goal_policy_analysis': spec}
    frame = slide.notes_slide.notes_text_frame
    if frame is None:
        from xml.sax.saxutils import escape
        escaped = escape(json.dumps(payload, ensure_ascii=False))
        node = parse_xml(
            '<p:sp xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            '<p:nvSpPr><p:cNvPr id="2" name="Notes body"/><p:cNvSpPr/><p:nvPr><p:ph type="body" idx="1"/></p:nvPr></p:nvSpPr>'
            '<p:spPr/><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>' + escaped + '</a:t></a:r></a:p></p:txBody></p:sp>'
        )
        slide.notes_slide.shapes._spTree.insert_element_before(node, 'p:extLst')
        frame = slide.notes_slide.notes_text_frame
    if frame is not None:
        frame.text = json.dumps(payload, ensure_ascii=False)
    manifest.append({
        'family': 'goal_policy_analysis',
        'donor_page': 7,
        'title': '목표 및 정책 분석',
        'slide': slide,
    })
    return slide
