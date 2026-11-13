"""인터뷰 평가 점수(scoring.compute) 기반 현황 분석 9대 영역 성숙도 스파이더 차트 계산·AI 의견 검증·PPT 렌더링."""
from __future__ import annotations
from copy import deepcopy
import math
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.oxml import parse_xml
from pptx.util import Inches, Pt
import scoring
from asis_analysis import AREA_NAMES
from asis_artifacts import COLOR
from asis_consolidated_deck import concise_headline, to_polite_korean

AREA_ORDER = ('network', 'compute', 'data', 'ai', 'secgov', 'telemetry', 'quality', 'operating', 'cost')

# 방안 1: 현황분석 9대 영역 ↔ 인터뷰 주제(신규구축 A1~D5 / 기구축 XA1~XD5) 매핑
AREA_INTERVIEW_MAP = {
    'network':   {'existing': ('XB3',),                   'new': ('B3',),                   'fallback': 'B', 'short': '네트워크'},
    'compute':   {'existing': ('XB1', 'XB4'),             'new': ('B1', 'B4'),              'fallback': 'B', 'short': '컴퓨트'},
    'data':      {'existing': ('XA4', 'XC2'),             'new': ('A4', 'C2'),              'fallback': 'A', 'short': '스토리지·데이터'},
    'ai':        {'existing': ('XB1', 'XB2', 'XB4'),      'new': ('A3', 'B1', 'B4'),        'fallback': 'B', 'short': 'AI 서비스'},
    'secgov':    {'existing': ('XC1', 'XC4', 'XC5'),      'new': ('C1', 'C4', 'C5'),        'fallback': 'C', 'short': '보안·거버넌스'},
    'telemetry': {'existing': ('XB5',),                   'new': ('B5',),                   'fallback': 'B', 'short': '관측·추적'},
    'quality':   {'existing': ('XA2', 'XC3', 'XB5'),      'new': ('A2', 'A3', 'C3'),        'fallback': 'B', 'short': '품질·평가'},
    'operating': {'existing': ('XA1', 'XA3', 'XA5'),      'new': ('A1', 'A5', 'B2'),        'fallback': 'A', 'short': '운영 모델'},
    'cost':      {'existing': ('XD1', 'XD2', 'XD3', 'XD4', 'XD5'), 'new': ('D1', 'D2', 'D3', 'D4', 'D5'), 'fallback': 'D', 'short': '비용·경제성'},
}

STAGE_INFO = [
    (1, 'Exploring',    '탐색',    '0~29점',   '미준비 · 필요성 인식'),
    (2, 'Planning',     '계획',    '30~49점',  '개선 대상 · 방향 수립'),
    (3, 'Implementing', '실증',    '50~69점',  '기준선(50점) · 일부 실행'),
    (4, 'Scaling',      '확산',    '70~84점',  '표준 절차 · 전사 확산'),
    (5, 'Realizing',    '가치 실현', '85~100점', '체계 운영 · 지속 최적화'),
]

SPIDER_OPINION_LIMITS = {'headline': 56, 'area_opinion': 68, 'verdict_banner': 132}

SPIDER_OPINION_INSTRUCTION = """인터뷰 평가 점수(scoring.compute, 0~100점 척도, 실증 진입 기준선 GATE=50점, 확산 목표=70점)와 현황 분석 9대 영역 결과를 바탕으로 성숙도 스파이더 차트 장표의 평가 의견을 작성한다.
반환은 JSON 객체 하나뿐이며 키는 다음 3개만 둔다:
{"headline": "슬라이드 상단 핵심 결론 제목(56자 이내, ~합니다/~입니다 경어체)", "verdict_banner": "하단 종합 판정 1줄 배너(132자 이내, ~합니다/~입니다 경어체)", "area_opinions": {"area_id": "해당 영역의 인터뷰 성숙도 평가 의견(68자 이내, ~합니다/~입니다 경어체)"}}
규칙:
- area_opinions에는 입력 areas의 모든 area_id 키를 빠짐없이 포함한다.
- 수치(점수·문항 수·단계)는 입력에 주어진 값만 근거로 쓰며 임의로 바꾸거나 꾸며내지 않는다.
- 각 영역의 area_opinions에는 인터뷰 응답에서 확인된 상태(강점·미달 사유·구두 미확인 여부)와 실측 현황의 핵심 격차를 명료한 컨설팅 경어체(~합니다/~입니다) 또는 개조식 명사구로 작성한다.
- 반말(~한다/~이다)은 절대 쓰지 않는다."""


def stage_of_score(sc: float) -> tuple[int, str, str]:
    if sc >= 85.0:
        return 5, 'Realizing', '가치 실현'
    if sc >= 70.0:
        return 4, 'Scaling', '확산'
    if sc >= 50.0:
        return 3, 'Implementing', '실증'
    if sc >= 30.0:
        return 2, 'Planning', '계획'
    return 1, 'Exploring', '탐색'


def compute_spider_metrics(result: dict) -> dict:
    """인터뷰 스냅샷(scoring.compute)과 9대 영역 분석 결과를 결합하여 결정적 성숙도 수치를 계산한다."""
    data = result.get('input', {})
    ctx = data.get('context', {})
    track = (data.get('run') or {}).get('track') or 'existing'
    t_key = 'existing' if track == 'existing' else 'new'
    snap = ctx.get('interview_snapshot') or {}
    qs = (snap.get('questionnaire') or {}).get('questions') or []
    ans = snap.get('answers') or {}
    if not qs:
        from questions import questions_for
        qs = questions_for(track)
    res = scoring.compute(qs, ans)

    stage_areas = {a['area_id']: a for a in (result.get('stages') or [{}])[0].get('areas', []) if isinstance(a, dict) and 'area_id' in a}
    area_rows = []
    for aid in AREA_ORDER:
        spec = AREA_INTERVIEW_MAP[aid]
        tcodes = spec[t_key]
        t_scores = [res['topics'][t]['score'] for t in tcodes if t in res['topics'] and res['topics'][t]['score'] is not None]
        used_fallback = False
        if t_scores:
            sc_100 = round(sum(t_scores) / len(t_scores), 1)
        elif res['areas'].get(spec['fallback']) is not None:
            sc_100 = round(float(res['areas'][spec['fallback']]), 1)
            used_fallback = True
        else:
            sc_100 = 0.0
            used_fallback = True
        sc_4 = round(sc_100 / 25.0, 2)
        st_idx, st_en, st_ko = stage_of_score(sc_100)

        q_sub = [q for q in qs if q.get('topic') in tcodes]
        scored_qs = []
        unk_cnt = 0
        low_cnt = 0
        for q in q_sub:
            ar = ans.get(q['id']) or {}
            s_val = str(ar.get('score') or '').strip()
            if s_val == '미확인':
                unk_cnt += 1
            elif s_val in ('0', '1', '2', '3', '4'):
                iv = int(s_val)
                if iv <= 1:
                    low_cnt += 1
                scored_qs.append({
                    'id': q['id'],
                    'topic': q['topic'],
                    'topic_name': q.get('topic_name', ''),
                    'score': iv,
                    'must': bool(q.get('must')),
                    'summary': str(ar.get('summary') or '').strip(),
                })
        scored_qs.sort(key=lambda x: (x['score'], x['id']))
        topic_breakdown = []
        for t in tcodes:
            t_info = res['topics'].get(t, {})
            t_sc = t_info.get('score')
            topic_breakdown.append({
                'topic': t,
                'name': t_info.get('name', t),
                'score': t_sc,
            })

        area_obj = stage_areas.get(aid) or {}
        hl = str(area_obj.get('headline') or '')
        imp_text = str((area_obj.get('implication') or {}).get('text') or '')
        if scored_qs:
            w = scored_qs[0]
            first_sent = w['summary'].split('.')[0].strip() + '.' if '.' in w['summary'] else w['summary']
            default_opinion = to_polite_korean(concise_headline(f"[{w['topic']} {w['score']}점] {first_sent}", 66))
        else:
            default_opinion = to_polite_korean(concise_headline(
                f"[{'·'.join(tcodes)} 구두 미확인 {unk_cnt}문항] {spec['fallback']}영역 평균({sc_100:.1f}점) 적용 · 실측 구성 대조가 필요합니다.", 66
            ))

        area_rows.append({
            'area_id': aid,
            'name': AREA_NAMES.get(aid, spec['short']),
            'short': spec['short'],
            'topics': list(tcodes),
            'topic_breakdown': topic_breakdown,
            'score_100': sc_100,
            'score_4': sc_4,
            'gate_100': 50.0,
            'target_100': 70.0,
            'used_fallback': used_fallback,
            'stage_idx': st_idx,
            'stage_en': st_en,
            'stage_ko': st_ko,
            'q_total': len(q_sub),
            'q_scored': len(scored_qs),
            'q_unknown': unk_cnt,
            'q_low': low_cnt,
            'scored_items': scored_qs[:3],
            'area_headline': hl,
            'area_implication': imp_text,
            'opinion': default_opinion,
        })

    overall_100 = round(float(res['overall']), 1) if res.get('overall') is not None else round(sum(r['score_100'] for r in area_rows) / max(len(area_rows), 1), 1)
    ov_idx, ov_en, ov_ko = stage_of_score(overall_100)
    below_gate = [r for r in area_rows if r['score_100'] < 50.0]
    customer = str(ctx.get('customer') or (ctx.get('assessment_metadata') or {}).get('customer') or '고객')
    unknown_pct = round(float(res.get('unknown_ratio') or 0.0) * 100.0, 1)

    low_names = '·'.join(r['short'] for r in sorted(below_gate, key=lambda x: x['score_100'])[:3]) or '핵심 통제 영역'
    default_headline = to_polite_korean(concise_headline(f"{customer}의 선결 과제는 {low_names} 성숙도 확보입니다", 54))
    hold_txt = f" · 미확인 {res['unknown']}문항({unknown_pct:.0f}%) 판정 보류" if res.get('stage') is None and res.get('unknown') else ''
    default_banner = to_polite_korean(concise_headline(
        f"판정  |  인터뷰 종합 {overall_100:.1f}점({ov_idx}단계 {ov_ko} {ov_en} 구간{hold_txt}) · 9개 영역 중 {len(below_gate)}개 영역이 실증 기준선(50점) 미충족",
        130,
    ))

    return {
        'customer': customer,
        'track': track,
        'overall_100': overall_100,
        'overall_4': round(overall_100 / 25.0, 2),
        'overall_stage_idx': ov_idx,
        'overall_stage_en': ov_en,
        'overall_stage_ko': ov_ko,
        'official_stage': res.get('stage'),
        'hold_reason': res.get('reason') or '',
        'answered': res.get('answered', 0),
        'total_q': res.get('total', 0),
        'unknown_q': res.get('unknown', 0),
        'unknown_ratio_pct': unknown_pct,
        'must_low': res.get('must_low', 0),
        'below_gate_count': len(below_gate),
        'headline': default_headline,
        'verdict_banner': default_banner,
        'areas': area_rows,
    }


def spider_opinion_view(result: dict) -> dict:
    """AI 모델이 성숙도 스파이더 차트 장표의 평가 의견(headline, area_opinions, verdict_banner)을 작성하기 위한 입력 뷰."""
    mat = compute_spider_metrics(result)
    return {
        'customer': mat['customer'],
        'track': mat['track'],
        'overall_100': mat['overall_100'],
        'overall_stage': f"{mat['overall_stage_idx']}단계 {mat['overall_stage_ko']} ({mat['overall_stage_en']})",
        'official_stage': mat['official_stage'],
        'hold_reason': mat['hold_reason'],
        'unknown_q': mat['unknown_q'],
        'unknown_ratio_pct': mat['unknown_ratio_pct'],
        'must_low': mat['must_low'],
        'below_gate_count': mat['below_gate_count'],
        'areas': [
            {
                'area_id': r['area_id'],
                'name': r['name'],
                'topics': r['topics'],
                'topic_scores': r['topic_breakdown'],
                'score_100': r['score_100'],
                'score_4': r['score_4'],
                'stage': f"{r['stage_idx']}단계 {r['stage_ko']}",
                'used_fallback': r['used_fallback'],
                'q_total': r['q_total'],
                'q_scored': r['q_scored'],
                'q_unknown': r['q_unknown'],
                'scored_items': r['scored_items'],
                'area_headline': r['area_headline'],
                'area_implication': r['area_implication'],
            }
            for r in mat['areas']
        ],
    }


def validate_spider_opinions(area_ids: list[str] | tuple[str, ...], payload: dict) -> dict:
    """모델이 반환한 스파이더 차트 평가 의견 JSON의 형식·키·길이·경어체를 검증한다."""
    if not isinstance(payload, dict) or set(payload) != {'headline', 'verdict_banner', 'area_opinions'}:
        raise ValueError('spider_opinion_shape_invalid')
    hl = str(payload.get('headline') or '').strip()
    if not hl:
        raise ValueError('spider_opinion_headline_empty')
    hl = to_polite_korean(hl)
    if len(hl) > SPIDER_OPINION_LIMITS['headline']:
        raise ValueError(f"spider_opinion_headline_too_long:{len(hl)}>{SPIDER_OPINION_LIMITS['headline']}")

    vb = str(payload.get('verdict_banner') or '').strip()
    if not vb:
        raise ValueError('spider_opinion_banner_empty')
    vb = to_polite_korean(vb)
    if not vb.startswith('판정'):
        vb = '판정  |  ' + vb
    if len(vb) > SPIDER_OPINION_LIMITS['verdict_banner']:
        raise ValueError(f"spider_opinion_banner_too_long:{len(vb)}>{SPIDER_OPINION_LIMITS['verdict_banner']}")

    opinions = payload.get('area_opinions')
    if not isinstance(opinions, dict) or set(opinions) != set(area_ids):
        raise ValueError('spider_opinion_areas_mismatch')
    cleaned_opinions = {}
    for aid in area_ids:
        val = str(opinions.get(aid) or '').strip()
        if not val:
            raise ValueError('spider_opinion_area_empty:' + aid)
        val = to_polite_korean(val)
        if len(val) > SPIDER_OPINION_LIMITS['area_opinion']:
            raise ValueError(f"spider_opinion_area_too_long:{aid}:{len(val)}>{SPIDER_OPINION_LIMITS['area_opinion']}")
        cleaned_opinions[aid] = val
    return {'headline': hl, 'verdict_banner': vb, 'area_opinions': cleaned_opinions}


def resolve_spider_spec(result: dict) -> dict:
    """결정적 점수 계산 결과에 AI가 작성·검증한 평가 의견(maturity_spider)을 결합한다."""
    mat = compute_spider_metrics(result)
    ai_spec = result.get('maturity_spider') or (result.get('report_material') or {}).get('maturity_spider')
    if isinstance(ai_spec, dict):
        if ai_spec.get('headline'):
            mat['headline'] = to_polite_korean(str(ai_spec['headline']))
        if ai_spec.get('verdict_banner'):
            vb = to_polite_korean(str(ai_spec['verdict_banner']))
            mat['verdict_banner'] = vb if vb.startswith('판정') else ('판정  |  ' + vb)
        area_ops = ai_spec.get('area_opinions') or {}
        if isinstance(area_ops, dict):
            for r in mat['areas']:
                if area_ops.get(r['area_id']):
                    r['opinion'] = to_polite_korean(str(area_ops[r['area_id']]))
    return mat


def _draw_polygon(slide, pts, fill_hex, line_hex, alpha_pct=None, line_pt=1.5):
    fb = slide.shapes.build_freeform(Inches(pts[0][0]), Inches(pts[0][1]))
    fb.add_line_segments([(Inches(x), Inches(y)) for x, y in pts[1:]], close=True)
    sh = fb.convert_to_shape()
    if fill_hex:
        sh.fill.solid()
        sh.fill.fore_color.rgb = RGBColor.from_string(fill_hex)
        if alpha_pct is not None:
            srgb = sh._element.xpath('.//a:solidFill/a:srgbClr')
            if srgb:
                srgb[0].append(parse_xml(f'<a:alpha xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" val="{int(alpha_pct * 1000)}"/>'))
    else:
        sh.fill.background()
    if line_hex:
        sh.line.color.rgb = RGBColor.from_string(line_hex)
        sh.line.width = Pt(line_pt)
    else:
        sh.line.fill.background()
    return sh


def draw_maturity_spider_slide(clone, note, text_fn, box_fn, wrapped_lines, line_fn, result: dict):
    """승인 통합 보고서(Summary Report)의 인터뷰 평가 점수 기반 9대 영역 성숙도 스파이더 차트 장표를 그린다."""
    mat = resolve_spider_spec(result)
    s = clone(
        6,
        'maturity_spider',
        mat['headline'],
        '현황 분석 9대 영역 기준 인터뷰 평가 점수(0~100점, 기준선 50점 · 확산 목표 70점) 및 성숙도 5단계(Exploring~Realizing) 진단 결과',
    )

    def fitted(val, x, y, w, h, pt=10.0, bold=False, color='ink', align=None, max_l=None):
        val = to_polite_korean(str(val))
        ls = wrapped_lines(val, w, pt)
        if max_l is not None:
            ls = ls[:max_l]
        kwargs = {} if align is None else {'align': align}
        sh = text_fn(s, '\n'.join(ls), x, y, w, h, pt, bold, color, **kwargs)
        for p in sh.text_frame.paragraphs:
            p.space_after = Pt(0)
            p.line_spacing = 1.04
        return sh

    # 좌측: 9각 스파이더 차트 패널
    chart_panel = box_fn(s, .72, 2.24, 5.60, 3.84, 'white')
    chart_panel.name = 'approved_spider_chart:panel'
    fitted('영역별 현재 수준 vs 기준선(50점) · 목표(70점)', .88, 2.30, 5.20, .26, 11.0, True, 'primary', max_l=1)

    cx, cy, radius = 3.52, 4.18, 1.12
    axes = mat['areas']
    n_axes = len(axes)
    angles = [-math.pi / 2 + i * (2 * math.pi / n_axes) for i in range(n_axes)]

    for ring_val in (25, 50, 75, 100):
        r = radius * (ring_val / 100.0)
        r_pts = [(cx + r * math.cos(a), cy + r * math.sin(a)) for a in angles]
        for idx_r, (p1, p2) in enumerate(zip(r_pts, r_pts[1:] + r_pts[:1])):
            line_fn(s, p1, p2, f'spider-ring-{ring_val}-{idx_r}')
        fitted(str(ring_val), cx - .18, cy - r - .09, .36, .16, 7.5, ring_val == 50, 'primary' if ring_val == 50 else 'muted', PP_ALIGN.CENTER, max_l=1)
    fitted('0', cx - .14, cy - .08, .28, .16, 7.5, False, 'muted', PP_ALIGN.CENTER, max_l=1)

    for idx_a, a in enumerate(angles):
        line_fn(s, (cx, cy), (cx + radius * math.cos(a), cy + radius * math.sin(a)), f'spider-axis-{idx_a}')

    target_pts = [(cx + radius * 0.70 * math.cos(a), cy + radius * 0.70 * math.sin(a)) for a in angles]
    gate_pts = [(cx + radius * 0.50 * math.cos(a), cy + radius * 0.50 * math.sin(a)) for a in angles]
    curr_pts = [(cx + radius * (max(3.0, ax['score_100']) / 100.0) * math.cos(a), cy + radius * (max(3.0, ax['score_100']) / 100.0) * math.sin(a)) for ax, a in zip(axes, angles)]

    _draw_polygon(s, target_pts, 'EBF2F8', '8FAEC9', alpha_pct=45, line_pt=1.0).name = 'approved_spider_polygon:target'
    _draw_polygon(s, gate_pts, 'D6E6F4', '4A7EBB', alpha_pct=50, line_pt=1.3).name = 'approved_spider_polygon:gate'
    _draw_polygon(s, curr_pts, '2B6288', '1B4665', alpha_pct=78, line_pt=1.8).name = 'approved_spider_polygon:current'

    for ax, a in zip(axes, angles):
        lx = cx + (radius + .16) * math.cos(a)
        ly = cy + (radius + .15) * math.sin(a)
        cos_a = math.cos(a)
        if abs(cos_a) < 0.22:
            box_x, box_w, al = lx - .95, 1.90, PP_ALIGN.CENTER
            box_y = ly - (.20 if math.sin(a) < 0 else -.02)
        elif cos_a > 0:
            box_x, box_w, al = lx - .02, 1.48, PP_ALIGN.LEFT
            box_y = ly - .11
        else:
            box_x, box_w, al = lx - 1.46, 1.48, PP_ALIGN.RIGHT
            box_y = ly - .11
        lbl = f"{ax['short']} ({ax['score_100']:.1f})"
        fitted(lbl, box_x, box_y, box_w, .22, 8.2, True, 'primary' if ax['score_100'] < 50 else 'ink', al, max_l=1)

    box_fn(s, 1.10, 5.80, .14, .14, 'panel')
    fitted('확산 목표 (70점)', 1.28, 5.77, 1.30, .20, 8.5, False, 'muted', max_l=1)
    box_fn(s, 2.70, 5.80, .14, .14, 'light')
    fitted('기준선 (GATE 50점)', 2.88, 5.77, 1.45, .20, 8.5, True, 'primary', max_l=1)
    box_fn(s, 4.45, 5.80, .14, .14, 'primary')
    fitted('현재 인터뷰 점수', 4.63, 5.77, 1.40, .20, 8.5, True, 'primary', max_l=1)

    # 우측 상단: 성숙도 위치 (scoring.STAGES 5단계)
    fitted(f"성숙도 위치 (인터뷰 종합 {mat['overall_100']:.1f}점 · {mat['overall_4']:.2f} / 4.0점)", 6.52, 2.24, 6.16, .24, 10.8, True, 'primary', max_l=1)
    for idx, (st_num, st_en, st_ko, st_range, _) in enumerate(STAGE_INFO):
        sx = 6.52 + idx * 1.24
        sw = 1.18
        is_cur = (st_num == mat['overall_stage_idx'])
        is_gate = (st_num == 3)
        st_box = box_fn(s, sx, 2.52, sw, .66, 'primary' if is_cur else ('light' if is_gate else 'white'))
        st_box.name = f'approved_maturity_stage:{st_en}'
        fitted(f"{st_num}. {st_ko}", sx + .04, 2.56, sw - .08, .24, 10.0, True, 'white' if is_cur else 'primary', PP_ALIGN.CENTER, max_l=1)
        fitted(f"{st_en} ({st_range})", sx + .02, 2.84, sw - .04, .26, 7.8, False, 'white' if is_cur else 'muted', PP_ALIGN.CENTER, max_l=1)
        if is_cur:
            fitted(f"▲ 현재 ({mat['overall_100']:.1f}점)", sx - .04, 3.21, sw + .08, .20, 8.6, True, 'primary', PP_ALIGN.CENTER, max_l=1)
        elif st_num == 3:
            fitted('▲ 기준선 (50점)', sx - .04, 3.21, sw + .08, .20, 8.6, True, 'primary', PP_ALIGN.CENTER, max_l=1)
        elif st_num == 4:
            fitted('▲ 목표 (70점)', sx - .04, 3.21, sw + .08, .20, 8.6, True, 'primary', PP_ALIGN.CENTER, max_l=1)

    # 우측 하단: 주요 Gap 및 AI 평가 의견 표 (점수 낮은 순 6개 영역)
    gap_areas = sorted(axes, key=lambda x: (x['score_100'] >= 50.0, x['score_100']))[:6]
    gap_hdr = f"주요 Gap 영역 (기준선 50점 미달 {mat['below_gate_count']}개 영역 · 현재 → 기준 50 / 목표 70점)" if mat['below_gate_count'] else "영역별 성숙도 평가 요약 (현재 → 기준 50 / 목표 70점)"
    fitted(gap_hdr, 6.52, 3.46, 6.16, .24, 10.5, True, 'primary', max_l=1)
    box_fn(s, 6.52, 3.74, 6.16, .32, 'primary')
    fitted('현황 분석 영역 (연결 주제)', 6.60, 3.79, 1.85, .22, 9.0, True, 'white', max_l=1)
    fitted('현재 → 기준(목표)', 8.48, 3.79, 1.18, .22, 9.0, True, 'white', PP_ALIGN.CENTER, max_l=1)
    fitted('AI 성숙도 평가 의견 (인터뷰·실측 종합)', 9.72, 3.79, 2.90, .22, 9.0, True, 'white', max_l=1)

    for r_i, ax in enumerate(gap_areas):
        ry = 4.09 + r_i * .33
        r_box = box_fn(s, 6.52, ry, 6.16, .31, 'panel' if r_i % 2 == 1 else 'white')
        r_box.name = f"approved_spider_gap_row:{ax['area_id']}"
        t_str = '·'.join(ax['topics'][:3])
        fitted(f"{ax['short']} ({t_str})", 6.60, ry + .05, 1.85, .22, 8.8, True, 'primary', max_l=1)
        fitted(f"{ax['score_100']:.1f} → 50(70)", 8.48, ry + .05, 1.18, .22, 8.8, True, 'ink', PP_ALIGN.CENTER, max_l=1)
        fitted(ax['opinion'], 9.72, ry + .05, 2.90, .22, 8.3, False, 'ink', max_l=1)

    # 하단 판정 배너
    banner_box = box_fn(s, .72, 6.16, 11.96, .48, 'light')
    banner_box.name = 'approved_spider_verdict:banner'
    fitted(mat['verdict_banner'], .90, 6.25, 11.60, .30, 10.0, True, 'primary', max_l=1)

    note(s, {
        'family': 'maturity_spider',
        'run': result['input']['run'],
        'overall_100': mat['overall_100'],
        'overall_stage': mat['overall_stage_en'],
        'official_stage': mat['official_stage'],
        'hold_reason': mat['hold_reason'],
        'areas': [
            {
                'area_id': r['area_id'],
                'topics': r['topics'],
                'score_100': r['score_100'],
                'stage': r['stage_en'],
                'opinion': r['opinion'],
            }
            for r in axes
        ],
    })
    return s
