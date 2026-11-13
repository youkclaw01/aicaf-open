"""인터뷰 종합 보고서 — 'WAF_Reliability-인터뷰 종합' 양식을 코드로 고정한 빌더.

원칙
- 모양(위치·크기·글꼴·색·행 간격·쪽 나눔)은 전부 이 파일의 상수가 정한다. 모델은 관여하지 않는다.
- 숫자(점수·평균·충족 여부·단계)는 scoring.compute 결과만 쓴다.
- 모델이 쓰는 것은 문항별 '평가 의견'·'개선 방향' 문장뿐이다(채점할 때 함께 생성되어 ai_detail 에 저장됨).
  보고서를 만들 때는 모델을 부르지 않는다 → 20초 안에 끝나고, 같은 데이터면 같은 파일이 나온다.
- 글이 칸을 넘치면 문장 경계에서 줄이고 '…'를 붙인다. 글자 크기는 줄이지 않는다.
템플릿 원본: WAF_Reliability-인터뷰 종합.pptx (마스터·테마만 interview_template.pptx 로 보관).
"""
import io
import json
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.dml import MSO_LINE_DASH_STYLE
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Pt

TEMPLATE = Path(__file__).parent / "interview_template.pptx"
FONT = "맑은 고딕"

# ---- 색 (템플릿 실측) ----
NAVY, NAVY_SUB, ACCENT_LINE = "1F3864", "C7D3E8", "4A7EBB"
ROW_BG, CARD_BG, CARD_LINE, GRID = "F4F6FA", "F7F9FC", "D9DFE8", "E8ECF2"
TXT, TXT_MUTED, TXT_LABEL, TXT_FOOT, TXT_DARK = "4A4A4A", "7A7A7A", "8A8A8A", "A0A0A0", "262626"
BLUE, AMBER, RED = "0072C6", "FFB900", "D73B01"            # 요약 막대·KPI
S_BLUE, S_ORANGE, S_RED = "2E5C9A", "E68A00", "C0392B"     # 문항 점수
NOTE_BG, NOTE_TXT = "FDF3E7", "8A5A00"

# ---- 점수 의미 (0~4, 구두 인터뷰 기준) ----
SCORE_LABEL = {"0": "미준비", "1": "필요성 인식", "2": "방향 수립", "3": "진행 중", "4": "체계 운영",
               "미확인": "미확인", "해당없음": "해당없음", "": "미응답"}
SCORE_DESC = [("0점", "미준비", "관심·계획 없음"), ("1점", "필요성 인식", "문제는 알지만 대상·방향 없음"),
              ("2점", "방향 수립", "개선 대상과 고려 방향을 말함"), ("3점", "진행 중", "담당·방식이 정해졌거나 일부 실행"),
              ("4점", "체계 운영", "정해진 방식으로 꾸준히 관리")]
GATE = 50  # 영역·주제 기준선(실증 Implementing 진입 점수). 충족/미달 판정에 쓴다.
AREA_SHORT = {"A": "업무 가치", "B": "인프라/AI", "C": "보안·거버넌스", "D": "토큰·비용"}  # meta 없을 때(신규구축) 기본값
FOOTER = f"0~4점 · 주제 점수 = 평균÷4×100 · 영역 = 주제 평균 · 기준 {GATE} · 성숙도 5단계(Exploring~Realizing)"


def score_color(s):
    return {"0": S_RED, "1": S_RED, "2": S_ORANGE, "3": TXT, "4": S_BLUE}.get(str(s or ""), TXT_MUTED)


def score_text(answer):
    """미입력과 실제 0점을 구분한다. 저장 점수와 구두평가 기준은 변경하지 않는다."""
    value = answer.get("score")
    return "" if value is None else str(value)


def gate_color(v):
    if v is None:
        return TXT_MUTED
    return BLUE if v >= GATE else (AMBER if v >= GATE - 20 else RED)


# ---------------- 그리기 기본 도구 ----------------
def _rgb(h):
    return RGBColor.from_string(h)


def rect(s, x, y, w, h, fill, line=None, line_w=9525):
    r = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Emu(x), Emu(y), Emu(w), Emu(h))
    r.fill.solid()
    r.fill.fore_color.rgb = _rgb(fill)
    if line:
        r.line.color.rgb = _rgb(line)
        r.line.width = Emu(line_w)
    else:
        r.line.fill.background()
    # 템플릿처럼 빈 effectLst 를 명시해 테마 그림자(effectRef idx=2)를 끈다
    sp = r._element.spPr
    for e in sp.findall(qn("a:effectLst")):
        sp.remove(e)
    ln = sp.find(qn("a:ln"))
    eff = sp.makeelement(qn("a:effectLst"), {})
    (ln.addnext(eff) if ln is not None else sp.append(eff))
    if r.has_text_frame:
        r.text_frame.text = ""
    return r


def hline(s, x, y, w, color, width=9525, dash=False):
    c = s.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Emu(x), Emu(y), Emu(x + w), Emu(y))
    c.line.color.rgb = _rgb(color)
    c.line.width = Emu(width)
    if dash:
        c.line.dash_style = MSO_LINE_DASH_STYLE.DASH
    return c


def text(s, x, y, w, h, lines, size, color, bold=False, align="l", autofit=True):
    """템플릿 텍스트상자와 같은 속성: wrap=square, anchor=top, spAutoFit, 단락 뒤 간격 0, 맑은 고딕."""
    tb = s.shapes.add_textbox(Emu(x), Emu(y), Emu(w), Emu(h))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.TOP
    body = tf._txBody.bodyPr
    for child in list(body):
        body.remove(child)
    if autofit:
        body.append(body.makeelement(qn("a:spAutoFit"), {}))
    for i, ln in enumerate(lines if isinstance(lines, list) else [lines]):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = {"l": PP_ALIGN.LEFT, "c": PP_ALIGN.CENTER, "r": PP_ALIGN.RIGHT}[align]
        p.space_after = Pt(0)
        r = p.add_run()
        r.text = ln
        f = r.font
        f.size, f.bold, f.name = Pt(size), bold, FONT
        f.color.rgb = _rgb(color)
        rpr = r._r.get_or_add_rPr()
        rpr.append(rpr.makeelement(qn("a:ea"), {"typeface": FONT}))
    return tb


# ---------------- 칸 맞춤 (글자 크기는 고정, 넘치면 문장 단위로 줄임) ----------------
MARGIN_PT = 14.4  # 좌우 내부 여백 합(0.1in × 2)


def _w(ch):
    o = ord(ch)
    if o >= 0x1100:
        return 1.08   # 맑은 고딕 한글은 em 보다 조금 넓게 그려진다(렌더 실측 보정)
    if ch in "ilI.,:;|!'`·()[] ":
        return 0.32
    if ch.isupper() or ch.isdigit():
        return 0.62
    return 0.55


def line_count(t, width_emu, size_pt):
    cap = (width_emu / 12700 - MARGIN_PT) / size_pt
    n = 0
    for para in t.split("\n"):
        cur, n = 0.0, n + 1
        for word in para.split(" "):
            ww = sum(_w(c) for c in word) + 0.32
            if cur + ww > cap and cur > 0:
                n, cur = n + 1, 0.0
            while ww > cap:            # 공백 없는 긴 낱말은 글자 단위로 넘어간다
                n, ww = n + 1, ww - cap
            cur += ww
    return n


def fit(t, width_emu, size_pt, max_lines):
    t = " ".join((t or "").split())
    if line_count(t, width_emu, size_pt) <= max_lines:
        return t
    cut = [i + 1 for i, c in enumerate(t) if c in ".。!?" or (c == "," and i > 20)]
    for i in reversed(cut):
        cand = t[:i].rstrip(",") + (" …" if t[:i].endswith(",") else "")
        if line_count(cand, width_emu, size_pt) <= max_lines:
            return cand
    while t and line_count(t + "…", width_emu, size_pt) > max_lines:
        t = t[:-1]
    return t.rstrip() + "…"


# ---------------- 공통 틀 ----------------
def header(s, title, sub):
    rect(s, 0, 0, 12191695, 713232, NAVY)
    text(s, 411480, 100584, 8229600, 310896, title, 19, "FFFFFF", bold=True)
    text(s, 411480, 420624, 10972800, 237744, sub, 10, NAVY_SUB)


def footer(s, page):
    text(s, 411480, 6455664, 10058400, 237744, FOOTER, 8, TXT_FOOT)
    text(s, 11338560, 6455664, 457200, 237744, str(page), 8, TXT_FOOT, align="r")


def new(prs):
    return prs.slides.add_slide(prs.slide_layouts[6])  # Blank


# ---------------- 1. 표지 ----------------
def cover(prs, a, meta):
    s = new(prs)
    rect(s, 0, 0, 12191695, 6858000, NAVY)
    rect(s, 0, 2743200, 12191695, 36576, ACCENT_LINE)
    # 제목은 템플릿 38pt 한 줄. 고객명이 길면 2줄로 내리고 위로 올려 구분선(2743200) 위에 둔다.
    title = fit(f"{a['customer']} {meta['report']} – 인터뷰 종합", 10515600, 38, 2)
    two = line_count(title, 10515600, 38) > 1
    text(s, 914400, 1874519 - (579120 if two else 0), 10515600, 677108 * (2 if two else 1), title, 38, "FFFFFF", bold=True)
    text(s, 914400, 2971800, 10515600, 365760, f"{meta['label']} — {meta['desc']} · 평가 결과 상세 · 문항별 평가 의견 수록", 15, NAVY_SUB)


# ---------------- 2. 평가 결과 요약 ----------------
def summary(prs, questions, answers, res, page, meta):
    s = new(prs)
    header(s, "평가 결과 요약", f"{meta['report']} · 영역별 점수와 주제별 분포")
    areas = res["areas"]
    met = sum(1 for k in "ABCD" if areas.get(k) is not None and areas[k] >= GATE)
    below = sum(1 for k in "ABCD" if areas.get(k) is not None and areas[k] < GATE)
    unscored = sum(1 for k in "ABCD" if areas.get(k) is None)
    topic_labels = {q["topic"]: q.get("display_topic_name", q["topic_name"]) for q in questions}
    low = sum(1 for q in questions if score_text(answers.get(q["id"]) or {}) in ("0", "1"))
    text(s, 411480, 896112, 5486400, 256032, "영역 점수", 12, NAVY, bold=True)
    kpis = [("평가 문항", str(res["total"]), "개", NAVY), ("응답 완료", str(res["answered"]), f"/{res['total']}", BLUE),
            ("기준 충족 영역", str(met), "/4", BLUE if met == 4 else AMBER), ("1점 이하", str(low), "개", RED)]
    for i, (label, val, unit, col) in enumerate(kpis):
        x = 589334 + i * 1298448
        rect(s, x, 1325227, 1243584, 676656, CARD_BG, CARD_LINE)
        text(s, x, 1380091, 1243584, 182880, label, 8, TXT_LABEL, align="c")
        uw = 201168 if len(unit) <= 2 else 384048          # 템플릿 단위칸 폭, 긴 단위('/100')만 넓힘
        text(s, x, 1544683, 1243584 - uw - 18288, 292608, val, 19, col, bold=True, align="r")
        text(s, x + 1243584 - uw - 18288, 1654411, uw, 182880, unit, 8, TXT_LABEL)
    # 막대 그래프 (0~100, 기준선)
    from scoring import STAGES
    stage = next((label for name, label, _, _ in STAGES if name == res.get("stage")), "보류")
    text(s, 589334, 2111611, 2926080, 219456, f"영역별 점수 · 기준 {GATE} 대비", 10, NAVY, bold=True)
    text(s, 3231950, 2111611, 2606040, 201168, f"{stage} · 미달 {below} · 미산정 {unscored}", 8, TXT_LABEL, bold=True, align="r")
    y0, y100 = 3702667, 2404219
    for v, y in ((0, y0), (50, (y0 + y100) // 2), (100, y100)):
        hline(s, 854510, y, 4160520, GRID)
        text(s, 397310, y - 73152, 402336, 164592, str(v), 7, TXT_LABEL, align="r")
    for i, k in enumerate("ABCD"):
        v = areas.get(k)
        bx = 1091111 + i * 1040130
        col = gate_color(v)
        if v is not None:
            h = int((y0 - y100) * v / 100)
            if h > 0:
                rect(s, bx, y0 - h, 566928, h, col)
            text(s, bx - 146304, y0 - h - 210312, 859536, 182880, f"{v:.1f}", 9, col, bold=True, align="c")
            text(s, bx - 164592, 3885547, 896112, 164592, f"{v - GATE:+.1f}", 7, col, bold=True, align="c")
        text(s, bx - 146304, 3730099, 859536, 164592, f"{k} {meta['short'][k]}", 9, NAVY, bold=True, align="c")
    gy = y0 - int((y0 - y100) * GATE / 100)
    hline(s, 854510, gy, 4160520, RED, 15875, dash=True)
    text(s, 5042462, gy - 91440, 685800, 182880, f"기준 {GATE}", 7, RED, bold=True)
    # 하위 6 주제
    topics = res["topics"]
    bad = sorted([(v["score"], k, v) for k, v in topics.items() if v["score"] is not None and v["score"] < GATE])[:6]
    text(s, 488750, 4187952, 2926080, 219456, f"기준 미달 주제 (하위 {len(bad)})", 10, NAVY, bold=True)
    rect(s, 589334, 4451893, 5230368, 237744, NAVY)
    for x, w, t, al in ((699062, 2377440, "주제", "l"), (3140510, 640080, "영역", "c"),
                        (3872030, 731520, "점수", "r"), (4694990, 1005840, "기준 대비", "r")):
        text(s, x, 4488469, w, 182880, t, 8, "FFFFFF", bold=True, align=al)
    for i, (v, k, t) in enumerate(bad):
        y = 4689637 + i * 196596
        if i % 2 == 0:
            rect(s, 589334, y, 5230368, 196596, CARD_BG)
        col = gate_color(v)
        text(s, 699062, y + 18288, 2377440, 173736, fit(f"{k} {topic_labels.get(k, t['name'])}", 2377440, 8, 1), 8, TXT_DARK)
        text(s, 3140510, y + 18288, 640080, 173736, t["area"], 8, TXT_LABEL, align="c")
        text(s, 3872030, y + 9144, 731520, 182880, f"{v:.1f}", 9, col, bold=True, align="r")
        text(s, 4694990, y + 18288, 1005840, 173736, f"{v - GATE:+.1f}", 8, col, bold=True, align="r")
    if not bad:
        message = "산정된 주제에서는 기준 미달이 확인되지 않았습니다." if any(t["score"] is not None for t in topics.values()) else "주제 점수가 아직 산정되지 않아 기준 미달 여부를 판단할 수 없습니다."
        text(s, 699062, 4707925, 5000000, 173736, message, 8, TXT_MUTED)
    # 오른쪽: 주제 점수 목록
    text(s, 6355080, 896112, 5486400, 256032, f"주제 점수 ({len(topics)}개)", 12, NAVY, bold=True)
    pitch = min(301752, (6380000 - 1207008) // max(len(topics), 1))
    for i, (k, t) in enumerate(sorted(topics.items())):
        y = 1207008 + i * pitch
        v = t["score"]
        col = S_BLUE if v is not None and v >= GATE else (S_ORANGE if v is not None else TXT_MUTED)
        if i % 2 == 0:
            rect(s, 6355080, y, 5394960, pitch, ROW_BG)
        rect(s, 6355080, y, 45720, pitch, col)
        dy = (pitch - 219456) // 2
        text(s, 6492240, y + dy, 914400, 219456, k, 9, NAVY, bold=True)
        text(s, 7452360, y + dy, 2834640, 219456, fit(topic_labels.get(k, t["name"]), 2834640, 9, 1), 9, TXT)
        text(s, 10332720, y + dy - 18288, 685800, 246888, "-" if v is None else f"{v:.1f}", 10, col, bold=True, align="r")
        text(s, 11064240, y + dy, 685800, 219456, "-" if v is None else ("충족" if v >= GATE else "미달"), 8, col, bold=True, align="c")
    footer(s, page)


# ---------------- 3. 문항 표 (문항별 결과 / 개선 필요) ----------------
ROWS_PER_PAGE, ROW_Y0, ROW_H = 9, 1115568, 589788
DETAIL_COLS = dict(id=(502920, 1051560), grp=(1600200, 1691640), q=(3383280, 3931920),
                   sc=(7406640, 502920), vd=(8001000, 914400), op=(9006840, 2743200))
IMPROVE_COLS = dict(id=(502920, 1051560), grp=(1600200, 1828800), q=(3520440, 4114800),
                    sc=(7726679, 502920), op=(8366760, 3383280))


def opinion_of(a, improve=False):
    """모델이 채점 때 써 둔 문장. 없으면(이전 채점분) 기존 근거로 대신한다."""
    d = {}
    try:
        d = json.loads(a.get("ai_detail") or "{}")
    except ValueError:
        pass
    if improve:
        return d.get("improve") or d.get("next") or a.get("ai_reason") or ""
    return d.get("opinion") or a.get("ai_reason") or a.get("summary") or ""


def question_table(prs, rows, title, sub, cols, page, improve=False):
    s = new(prs)
    header(s, title, sub)
    rect(s, 365760, 822960, 11430000, 292608, NAVY)
    heads = [("id", "문항 ID", "l"), ("grp", "주제", "l"), ("q", "평가 질문", "l"), ("sc", "점수", "c")]
    heads += [("op", "평가 의견 · 개선 방향", "l")] if improve else [("vd", "판정", "l"), ("op", "평가 의견", "l")]
    for key, label, al in heads:
        x, w = cols[key]
        text(s, x, 868680, w, 201168, label, 9, "FFFFFF", bold=True, align=al)
    for i, (q, a) in enumerate(rows):
        y = ROW_Y0 + i * ROW_H
        sc = score_text(a)
        col = score_color(sc)
        if i % 2 == 0:
            rect(s, 365760, y, 11430000, ROW_H, ROW_BG)
        rect(s, 365760, y, 41148, ROW_H, col)
        x, w = cols["id"]
        text(s, x, y + 54864, w, 219456, q["id"], 8, NAVY, bold=True)
        text(s, x, y + 265176, w, 201168, ("필수 · " if q["must"] else "") + q["area"], 8, TXT_MUTED)
        x, w = cols["grp"]
        text(s, x, y + 54864, w, 457200, fit(q.get("display_topic_name", q["topic_name"]), w, 8, 2), 8, TXT)
        x, w = cols["q"]
        text(s, x, y + 45720, w, 502920, fit(q["question"], w, 8, 3), 8, TXT)  # 질문은 원문, 3줄 초과 시만 줄임
        x, w = cols["sc"]
        text(s, x, y + 146304, w, 274320, sc if sc in "01234" and sc else "-", 15 if sc in ("0", "1", "2", "3", "4") else 9,
             col, bold=True, align="c")
        if not improve:
            x, w = cols["vd"]
            text(s, x, y + 173736, w + 45720, 219456, SCORE_LABEL.get(sc, sc), 8, col, bold=True)
        x, w = cols["op"]
        op = opinion_of(a, improve) if a else ""
        text(s, x, y + 36576, w, 530352, fit(op, w, 7, 4) if op else ("" if improve else "아직 응답이 없습니다."),
             7, TXT_MUTED)
    footer(s, page)


# ---------------- 4. 평가 기준 및 범위 ----------------
def criteria(prs, res, page):
    s = new(prs)
    header(s, "평가 기준 및 범위", "점수 정의 · 산정 방식 · 성숙도 판정")
    text(s, 411480, 960120, 5486400, 274320, "점수 정의", 13, NAVY, bold=True)
    rect(s, 411480, 1325880, 5486400, 310896, NAVY)
    for x, w, t in ((548640, 731520, "점수"), (1371600, 1828800, "상태"), (3291840, 2468880, "인터뷰에서 확인되는 모습")):
        text(s, x, 1380744, w, 219456, t, 9, "FFFFFF", bold=True)
    for i, (sc, st, ev) in enumerate(SCORE_DESC + [("미확인", "판단 근거 없음", "집계 제외 · 20% 초과 시 판정 보류")]):
        y = 1636776 + i * 329184
        if i % 2 == 0:
            rect(s, 411480, y, 5486400, 329184, ROW_BG)
        text(s, 548640, y + 54864, 731520, 237744, sc, 10, score_color(sc[0]) if sc[0].isdigit() else TXT_MUTED, bold=True)
        text(s, 1371600, y + 54864, 1828800, 237744, st, 10, TXT)
        text(s, 3291840, y + 54864, 2468880, 237744, fit(ev, 2468880, 9, 1), 9, TXT_MUTED)
    text(s, 6355080, 960120, 5486400, 274320, "산정 방식", 13, NAVY, bold=True)
    rect(s, 6355080, 1325880, 5394960, 1371600, ROW_BG)
    text(s, 6583680, 1508760, 4937760, 1097280, [
        "주제 점수            문항 평균 ÷ 4 × 100",
        "영역 점수            주제 점수 평균 (A·B·C·D)",
        f"기준선               {GATE}점 (실증 단계 진입)",
        "성숙도 판정         점수 구간 + 영역 조건 + 필수 문항"], 11, TXT, autofit=False)
    text(s, 6355080, 2926080, 5486400, 274320, "성숙도 단계", 13, NAVY, bold=True)
    rect(s, 6355080, 3291840, 5394960, 1188720, ROW_BG)
    text(s, 6583680, 3474720, 4937760, 914400, [
        "Exploring 0~29  ·  Planning 30~49",
        "Implementing 50~69  ·  Scaling 70~84",
        "Realizing 85 이상 (전 영역 70 이상)",
        fit(f"이번 결과: {res.get('stage') or '판정 전'} · {res.get('reason') or ''}", 4937760, 11, 1)], 11, TXT, autofit=False)
    rect(s, 6355080, 4709160, 5394960, 1143000, NOTE_BG)
    text(s, 6583680, 4892040, 4937760, 868680, [
        "본 평가는 Microsoft Cloud Adoption Framework·Well-Architected",
        "AI 지침과 AI Strategy Roadmap 5단계를 참고해 구성한 내부",
        "인터뷰 진단 모델이며, Microsoft 공식 점수나 인증이 아닙니다.",
        "인터뷰는 구두 답변 기준으로, 세부 자료 확인은 별도입니다."], 9, NOTE_TXT, autofit=False)
    footer(s, page)


def split_even(rows):
    """한 쪽 최대 9행. 쪽 수는 최소로, 행은 쪽마다 고르게(마지막 쪽에 1~2행만 남지 않게). 문항 순서는 유지."""
    if not rows:
        return []
    n = -(-len(rows) // ROWS_PER_PAGE)
    base, extra = divmod(len(rows), n)
    out, i = [], 0
    for k in range(n):
        m = base + (1 if k < extra else 0)
        out.append(rows[i:i + m])
        i += m
    return out


# ---------------- 조립 ----------------
DEFAULT_META = dict(label="신규구축", desc="AI를 아직 쓰지 않는 고객", report="신규구축 AI 도입 준비도", short=AREA_SHORT)


def build(assessment, questions, answers, res, meta=None):
    """meta = questions.TRACK_META[track] (표지·요약의 유형 표기와 영역 짧은 이름)."""
    meta = meta or DEFAULT_META
    prs = Presentation(str(TEMPLATE))
    cover(prs, assessment, meta)
    page = 2
    summary(prs, questions, answers, res, page, meta)
    rows = [(q, answers.get(q["id"]) or {}) for q in questions]
    pages = split_even(rows)
    for n, chunk in enumerate(pages, 1):
        page += 1
        question_table(prs, chunk, f"문항별 평가 결과 ({n}/{len(pages)})",
                       f"{chunk[0][0]['id']} ~ {chunk[-1][0]['id']}", DETAIL_COLS, page)
    order = {"0": 0, "1": 1, "미확인": 2}
    weak = [(q, a) for q, a in rows if score_text(a) in ("0", "1")
            or (q["must"] and score_text(a) == "미확인")]
    weak.sort(key=lambda r: (order.get(str(r[1].get("score")), 9), r[0]["id"]))
    ip = split_even(weak)
    for n, chunk in enumerate(ip, 1):
        page += 1
        question_table(prs, chunk, f"개선 필요 문항 ({n}/{len(ip)})",
                       f"1점 이하·필수 미확인 {len(weak)}문항 · 점수 낮은 순", IMPROVE_COLS, page, improve=True)
    page += 1
    criteria(prs, res, page)
    problems = verify(prs)
    if problems:
        raise ValueError("interview report layout contract: " + "; ".join(problems[:10]))
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


# ---------------- 양식 계약 검사 (저장 전에 강제) ----------------
ALLOWED_COLORS = {NAVY, NAVY_SUB, ACCENT_LINE, ROW_BG, CARD_BG, CARD_LINE, GRID, TXT, TXT_MUTED, TXT_LABEL, TXT_FOOT,
                  TXT_DARK, BLUE, AMBER, RED, S_BLUE, S_ORANGE, S_RED, NOTE_BG, NOTE_TXT, "FFFFFF"}
ALLOWED_SIZES = {7, 8, 9, 10, 11, 12, 13, 15, 19, 38}


def verify(prs):
    """템플릿 계약 위반 목록. 비어 있어야 저장된다."""
    out = []
    W, H = prs.slide_width, prs.slide_height
    for n, s in enumerate(prs.slides, 1):
        for sh in s.shapes:
            if sh.left < 0 or sh.top < 0 or sh.left + sh.width > W + 12700 or sh.top + sh.height > H + 12700:
                out.append(f"S{n} {sh.name} off-slide")
            if not sh.has_text_frame:
                continue
            for p in sh.text_frame.paragraphs:
                for r in p.runs:
                    f = r.font
                    if f.name != FONT:
                        out.append(f"S{n} font {f.name}")
                    if f.size is None or int(f.size.pt) not in ALLOWED_SIZES:
                        out.append(f"S{n} size {f.size}")
                    if str(f.color.rgb) not in ALLOWED_COLORS:
                        out.append(f"S{n} color {f.color.rgb}")
            # 고정 높이 칸(본문 문장)은 계산한 줄 수가 칸 높이를 넘으면 안 된다
            t = sh.text_frame.text
            if t and sh.text_frame.paragraphs[0].runs:
                sz = sh.text_frame.paragraphs[0].runs[0].font.size.pt
                lines = sum(line_count(x, sh.width, sz) for x in t.split("\n"))
                cap = max(1, int((sh.height / 12700 + 2) // (sz * 1.2)))
                if lines > cap and sh.height > 300000 and n > 1 or (n == 1 and lines > 2 and sz >= 38):
                    out.append(f"S{n} overflow {sh.name} {lines}>{cap} '{t[:20]}'")
    return out


# ---------------- 유형별 인터뷰 질문지 엑셀 ----------------
def questionnaire_xlsx(questions, meta):
    """기본 열과 점수 위치는 유지하고, 질문은행의 진행자 정보와 용어를 함께 내려준다."""
    from openpyxl import Workbook
    from openpyxl.comments import Comment
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.worksheet.datavalidation import DataValidation
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "질문지"
    ws["A1"] = f"{meta['report']} 인터뷰 질문지 ({meta['label']} — {meta['desc']})"
    ws["A1"].font = Font(name=FONT, bold=True, size=14, color=NAVY)
    ws["A2"] = "점수 0~4 · 미확인 · 해당없음 | 구두 답변 기준(존재·방향을 말하면 2점) | 주제 점수 = 평균÷4×100"
    ws["A2"].font = Font(name=FONT, size=9, color=TXT_MUTED)
    head = ["번호", "영역", "주제", "질문", "확인 포인트", "4점 모습", "필수", "확인 자료 · 근거", "점수", "메모",
            "용어 설명", "진행자 메모", "선택 추가 질문"]
    widths = [8, 18, 20, 52, 30, 34, 6, 34, 8, 40, 34, 46, 56]
    thin = Side(style="thin", color=CARD_LINE)
    for i, (h, w) in enumerate(zip(head, widths), 1):
        c = ws.cell(row=4, column=i, value=h)
        c.font, c.fill = Font(name=FONT, bold=True, color="FFFFFF"), PatternFill("solid", fgColor=NAVY)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[c.column_letter].width = w
    for r, q in enumerate(questions, 5):
        terms = "\n".join(f"{t['term']}: {t['explanation']}" for t in q.get("terms", []))
        note = q.get("interviewer_note", "")
        followups = "\n".join(q.get("followups", []))
        vals = [q["id"], f"{q['area']} {q.get('display_area_name', q['area_name'])}",
                f"{q['topic']} {q.get('display_topic_name', q['topic_name'])}", q["question"], q["point"],
                q["top"], "필수" if q["must"] else "", q["evidence"], "", "", terms, note, followups]
        for i, v in enumerate(vals, 1):
            c = ws.cell(row=r, column=i, value=v)
            c.font = Font(name=FONT, size=10, color=TXT)
            c.alignment = Alignment(wrap_text=True, vertical="top")
            c.border = Border(top=thin, bottom=thin, left=thin, right=thin)
            if r % 2:
                c.fill = PatternFill("solid", fgColor=ROW_BG)
            if i in (9, 10):
                c.fill = PatternFill("solid", fgColor="FFF2CC")
        ws.cell(r, 4).comment = Comment("\n".join(x for x in (terms, note, followups) if x), "AI Assessment 시스템")
    last = 4 + len(questions)
    dv = DataValidation(type="list", formula1='"0,1,2,3,4,미확인,해당없음"', allow_blank=True)
    ws.add_data_validation(dv)
    dv.add(f"I5:I{last}")
    ws.freeze_panes = "E5"
    ws.auto_filter.ref = f"A4:M{last}"
    guide = wb.create_sheet("작성 안내")
    guide.append(["AI Assessment 시스템 — 인터뷰 질문지 작성 기준"])
    guide.append(["기록 위치", "질문지의 노란색 점수·메모 칸에 현장 답변을 기록한다. 용어·진행자 메모·추가 질문은 오른쪽 열과 질문 셀 주석에서 확인한다."])
    guide.append(["평가 기준", "0점 없음 · 1점 필요성만 인식 · 2점 대상과 방향 있음 · 3점 담당·방법이 정해졌거나 일부 실행 · 4점 체계적 운영·정기 확인."])
    guide.append(["구두 답변", "즉석 답변으로 판단한다. 세부 수치, 자료, 기술 용어를 모른다는 이유만으로 감점하지 않는다. 4점 모습의 자료는 현장 제출 조건이 아니다."])
    guide.append(["미확인", "평가 근거가 부족하면 미확인으로 기록한다. 해당없음은 고객 상황에 적용되지 않는 것이 확인된 경우이며, 숫자를 모르는 경우와 다르다."])
    guide.append(["비용 입력", "계산할 수치를 모르면 미산정으로 남긴다. 관행적인 근무일, 자동화율, 평균 인건비, 0을 대신 넣지 않는다."])
    guide.append(["금액 구분", "통화·기간·대상 업무·포함 비용을 함께 기록하고 예상과 실제, 초기 비용과 월 운영비, 시간 여유와 실제 지출 감소를 구분한다."])
    guide.append(["개인정보", "개인 연봉, 비밀번호, 전체 계약서, 원문 대화나 민감 자료를 요구하지 않는다. 승인된 합산 수치와 확인 경로만 사용한다."])
    guide.append(["기록 예시(가상)", "메모: 방향을 검토 중이며 담당 지정은 다음 단계. 점수: 2. 이 예시는 실제 고객 답변이 아니다."])
    guide.append(["비용 기록 예시(가상)", "2026-09 / 같은 대상 업무 전체 / 하루 100건 / 건당 6분 / 실제 업무일수는 후속 확인. 근무일 미확인 동안 월 환산은 미산정."])
    guide.column_dimensions["A"].width = 25
    guide.column_dimensions["B"].width = 110
    guide.freeze_panes = "B2"
    for row in guide:
        for c in row:
            c.font = Font(name=FONT, size=10, color=TXT)
            c.alignment = Alignment(wrap_text=True, vertical="top")
            c.border = Border(top=thin, bottom=thin, left=thin, right=thin)
    guide["A1"].font = Font(name=FONT, size=14, bold=True, color=NAVY)
    guide.merge_cells("A1:B1")
    guide.row_dimensions[1].height = 28
    for r in range(2, guide.max_row + 1):
        guide.row_dimensions[r].height = 44
    cost = wb.create_sheet("비용 확인")
    cost.append(["AI Assessment 시스템 — 비용 산정용 선택 확인"])
    cost.append(["구두 평가와 분리", "확인한 숫자와 단위·기간·범위를 기록한다. 빈칸은 미산정이며 성숙도 0점이나 실제 비용 0을 뜻하지 않는다."])
    cost_head = ["관련 문항", "산정에 쓸 정보", "단위", "기준 기간", "대상 범위", "누락 시 처리", "확인 경로", "확인한 값", "근거 메모"]
    cost.append([])
    cost.append(cost_head)
    routes = {
        "collected_evidence_confirmed_by_finance": "비용 담당 확인과 같은 월의 청구 내역",
        "finance_confirmation": "비용·재무 담당자가 확인한 통화와 집계",
        "raw_transcript_seed_with_scope_confirmation": "고객 원문 답변과 업무 범위의 후속 확인",
        "manual_verified_input": "업무·비용 담당자가 확인한 값",
        "manual_verified_input_or_confirmed_seed": "업무 담당 집계 또는 같은 원문에 있는 확인값",
        "finance_confirmed_manual_derivation": "같은 기간·범위의 합산 업무비용과 총시간 확인",
        "explicit_assumption_confirmation": "시험이나 승인된 예상의 근거 확인",
        "manual_verified_input_with_evidence": "항목·기간·범위를 확인한 견적 또는 비용 집계",
        "technical_confirmation": "기술 담당자의 시험·사용 기록 확인",
        "collected_price_row_selected_by_technical_owner": "기술·계약 담당자의 공식 가격 조건 확인",
        "collected_price_unit": "선택한 공식 가격의 원문 단위 확인",
        "collected_cost_evidence": "같은 기간·범위·청구 기준의 비용 내역",
        "collected_usage_metrics": "기술 담당자의 사용량 측정 결과",
        "technical_confirmation_of_price_refs": "기술 담당자의 모델·지역·요금 방식과 가격 확인",
        "technical_and_contract_confirmation": "기술 담당자의 사용률과 계약 담당자의 예약 조건",
        "finance_and_technical_confirmation": "비용 담당자의 예산과 기술 담당자의 알림 확인",
        "finance_confirmation_of_allocation_key": "비용 담당자의 부서 구분과 배분 기준",
        "confirmed_action_bound_to_source_snapshot": "동일 비용 원본에 연결한 조치별 확인 근거",
        "explicit_projection_confirmation": "기간 환산에 쓰는 반복 가정의 확인",
        "finance_and_business_owner_confirmation": "재무·업무 담당자의 같은 범위 전후 비교",
        "explicit_estimate_followup": "새 과제의 견적·예상량과 후속 산정 확인",
    }
    bindings, related = {}, {}
    for q in questions:
        for b in q.get("cost_inputs", []):
            bindings.setdefault(b["key"], b)
            related.setdefault(b["key"], []).append(q["id"])
    for key, b in bindings.items():
        cost.append([", ".join(related[key]), b["label"], b["unit"], b["period"], b["scope"], b["missing"],
                     routes.get(b["source_route"], "업무·비용·기술 담당자의 후속 확인"), "", ""])
    cost_widths = [18, 32, 30, 36, 42, 60, 42, 22, 46]
    for i, width in enumerate(cost_widths, 1):
        cost.column_dimensions[cost.cell(4, i).column_letter].width = width
    cost.merge_cells("A1:I1")
    cost.row_dimensions[1].height = 28
    cost.row_dimensions[2].height = 40
    cost.freeze_panes = "C5"
    cost.auto_filter.ref = f"A4:I{max(4, cost.max_row)}"
    for row in cost:
        for c in row:
            c.font = Font(name=FONT, size=10, color=TXT)
            c.alignment = Alignment(wrap_text=True, vertical="top")
            c.border = Border(top=thin, bottom=thin, left=thin, right=thin)
            if c.row == 4:
                c.font = Font(name=FONT, bold=True, color="FFFFFF")
                c.fill = PatternFill("solid", fgColor=NAVY)
            elif c.row >= 5 and c.column in (8, 9):
                c.fill = PatternFill("solid", fgColor="FFF2CC")
    cost["A1"].font = Font(name=FONT, size=14, bold=True, color=NAVY)
    # 넓은 글자와 명시적 줄바꿈을 고려해 기본 행 높이로 감춰지는 보조 문구를 줄인다.
    from math import ceil
    for sheet, first, dimensions in ((ws, 5, widths), (cost, 5, cost_widths)):
        for r in range(first, sheet.max_row + 1):
            lines = 1
            for c, width in zip(sheet[r], dimensions):
                count = sum(max(1, ceil(sum(1.8 if ord(ch) > 0x2e80 else 1 for ch in line) / max(1, width - 2)))
                            for line in str(c.value or "").split("\n"))
                lines = max(lines, count)
            sheet.row_dimensions[r].height = max(60, lines * 14 + 12)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
