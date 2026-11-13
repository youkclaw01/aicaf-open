"""Cost Optimization 엑셀·PPT 생성. 수치는 cost_calc.compute() 결과만 쓴다.

- 엑셀: 계산 시트는 엑셀 수식으로 남겨 가정값을 바꾸면 다시 계산된다(xlsxwriter).
- PPT: 템플릿 AI_Cost_Governance 디자인 체계(ai-ref-deck: 흰 바탕·326B91·키커·결론형 제목·푸터)를
  python-pptx 로 재현한다. 차트는 네이티브 PPT 차트(편집 가능).
"""
import io
import re
import time

import xlsxwriter
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION, XL_LABEL_POSITION
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Inches, Pt

# ---------------- 공통 ----------------
TRACK_KO = {"new": "신규 도입", "existing": "기존 운영"}


def money(v, cur="USD"):
    if v is None:
        return "-"
    return f"{cur} {v:,.0f}" if abs(v) >= 100 else f"{cur} {v:,.2f}"


# =====================================================================
# 엑셀
# =====================================================================
def build_xlsx(res, customer):
    bio = io.BytesIO()
    wb = xlsxwriter.Workbook(bio, {"in_memory": True})
    F = "Malgun Gothic"
    f_title = wb.add_format({"font_name": F, "bold": True, "font_size": 14, "font_color": "#1F3864"})
    f_sub = wb.add_format({"font_name": F, "font_size": 9, "font_color": "#6E6E73"})
    f_h = wb.add_format({"font_name": F, "bold": True, "font_color": "#FFFFFF", "bg_color": "#326B91", "border": 1,
                         "border_color": "#BFBFBF", "text_wrap": True, "valign": "vcenter"})
    f_t = wb.add_format({"font_name": F, "border": 1, "border_color": "#D9D9D9", "valign": "top", "text_wrap": True})
    f_n = wb.add_format({"font_name": F, "border": 1, "border_color": "#D9D9D9", "num_format": "#,##0.00"})
    f_n0 = wb.add_format({"font_name": F, "border": 1, "border_color": "#D9D9D9", "num_format": "#,##0"})
    f_p = wb.add_format({"font_name": F, "border": 1, "border_color": "#D9D9D9", "num_format": "0.0%"})
    f_in = wb.add_format({"font_name": F, "border": 1, "border_color": "#C55A11", "bg_color": "#FFF2CC", "num_format": "#,##0.00##"})
    f_inp = wb.add_format({"font_name": F, "border": 1, "border_color": "#C55A11", "bg_color": "#FFF2CC", "num_format": "0.0%"})
    f_k = wb.add_format({"font_name": F, "bold": True, "border": 1, "border_color": "#D9D9D9", "bg_color": "#EDF3F8", "num_format": "#,##0.00"})
    f_na = wb.add_format({"font_name": F, "border": 1, "border_color": "#D9D9D9", "font_color": "#C00000", "italic": True})
    cur = res["currency"]
    A = res["assumptions"]
    act = res["actual"]

    def sheet(name, title, heads, widths):
        ws = wb.add_worksheet(name)
        ws.hide_gridlines(2)
        ws.write(0, 0, title, f_title)
        ws.write(1, 0, f"{customer} · AI Assessment 시스템 · 통화 {cur} · 생성 {time.strftime('%Y-%m-%d %H:%M')}", f_sub)
        for i, (h, w) in enumerate(zip(heads, widths)):
            ws.write(3, i, h, f_h)
            ws.set_column(i, i, w)
        ws.set_row(3, 30)
        ws.freeze_panes(4, 0)
        return ws

    def status_row(ws, r, secname):
        if secname == "__none__":
            return False
        s = act["sections"].get(secname) or {}
        if s.get("status") != "collected" or not s.get("count"):
            ws.write(r, 0, f"{s.get('status_ko', '수집 안 됨')} — {secname} {s.get('reason') or ''}", f_na)
            return True
        return False

    # 0 요약 (나중에 채움)
    ws0 = wb.add_worksheet("요약")
    ws0.hide_gridlines(2)
    ws0.set_column(0, 0, 34)
    ws0.set_column(1, 1, 18)
    ws0.set_column(2, 2, 60)

    # 1 가정값
    wa = sheet("가정값", "가정값 — 노란 칸을 바꾸면 시나리오·ROI 시트가 다시 계산됩니다", ["항목", "값", "설명"], [34, 16, 70])
    names = {}

    def put(r, key, label, v, desc, pct=False):
        wa.write(r, 0, label, f_t)
        wa.write_number(r, 1, float(v or 0), f_inp if pct else f_in)
        wa.write(r, 2, desc, f_t)
        nm = "a_" + key
        wb.define_name(nm, f"='가정값'!$B${r + 1}")
        names[key] = nm
    r = 4
    put(r, "horizon", "비교 기간(개월)", A["horizon_months"], "누적 그래프 기간"); r += 1
    if res["track"] == "existing":
        put(r, "pp_shift", "우선 처리 → 표준 이전 비율", A["pp_shift"], "우선 처리(PP) 요청 중 표준 처리로 옮길 비율", True); r += 1
        put(r, "batch", "일괄 처리 가능 비율 β", A["batch_share"], "즉시 응답이 필요 없는 요청 비율(인터뷰)", True); r += 1
        put(r, "cache", "캐시 가능 입력 비율 κ", A["cache_share"], "공통 앞부분 1,024토큰 이상 반복 비율", True); r += 1
        put(r, "route", "경량 모델 이관 비율 ρ", A["route_share"], "품질 기준을 만족하는 요청 비율", True); r += 1
        put(r, "impl", "최적화 적용 비용", A["impl_cost"], f"일회성 비용({cur})"); r += 1
    else:
        put(r, "tin", "요청당 입력 토큰 Tin", A["tin"], "문서 분량·대화 길이로 추정"); r += 1
        put(r, "tout", "요청당 출력 토큰 Tout", A["tout"], "답변 길이로 추정"); r += 1
        put(r, "K", "초기 구축비 K", A["K"], f"일회성({cur})"); r += 1
        put(r, "sens", "민감도", A["sens"], "자동화율·토큰 ±", True); r += 1
        wa.write(r + 1, 0, "업무별 입력", f_title)
        for i, h in enumerate(["업무", "월 건수 N", "건당 시간 t", "시간당 비용 w", "기타 비용 E", "자동화율 a"]):
            wa.write(r + 2, i, h, f_h)
        t0 = r + 3
        for i, t in enumerate(A["tasks"]):
            wa.write(t0 + i, 0, t["name"], f_t)
            for j, k in enumerate(("N", "t", "w", "E")):
                wa.write_number(t0 + i, 1 + j, t[k], f_in)
            wa.write_number(t0 + i, 5, t["a"], f_inp)
        wa.set_column(3, 5, 14)
        names["tasks"] = (t0 + 1, t0 + len(A["tasks"]))

    # 2 월별
    ws = sheet("월별", "월별 비용 추이 (Cost Management)", ["월", "실비용", "상각 비용", "AI 서비스 비용"], [16, 16, 16, 18])
    if not status_row(ws, 4, "costMonthlyTotals"):
        aim = {x["month"]: x["cost"] for x in act["ai_monthly"]}
        for i, m in enumerate(act["monthly"]):
            ws.write(4 + i, 0, m["month"], f_t)
            ws.write(4 + i, 1, m["actual"], f_n) if m["actual"] is not None else ws.write(4 + i, 1, "-", f_na)
            ws.write(4 + i, 2, m["amortized"], f_n) if m["amortized"] is not None else ws.write(4 + i, 2, "-", f_na)
            ws.write(4 + i, 3, aim.get(m["month"]), f_n) if m["month"] in aim else ws.write(4 + i, 3, "-", f_na)
        n = len(act["monthly"])
        ch = wb.add_chart({"type": "column"})
        ch.add_series({"name": "실비용", "categories": ["월별", 4, 0, 3 + n, 0], "values": ["월별", 4, 1, 3 + n, 1], "fill": {"color": "#326B91"}})
        ch.add_series({"name": "AI 서비스", "categories": ["월별", 4, 0, 3 + n, 0], "values": ["월별", 4, 3, 3 + n, 3], "fill": {"color": "#9DC3E6"}})
        ch.set_title({"name": "월별 비용"})
        ws.insert_chart("F4", ch)

    def table(name, title, heads, widths, rows, secname, nums=()):
        ws = sheet(name, title, heads, widths)
        if status_row(ws, 4, secname):
            return ws
        for i, row in enumerate(rows):
            for j, v in enumerate(row):
                if j in nums and isinstance(v, (int, float)):
                    ws.write_number(4 + i, j, v, f_p if heads[j].endswith("비중") else f_n)
                else:
                    ws.write(4 + i, j, "" if v is None else v, f_t)
        ws.autofilter(3, 0, 3 + max(1, len(rows)), len(heads) - 1)
        return ws
    tot = sum(x["cost"] for x in act["by_service"]) or None
    table("서비스별", "서비스별 비용(기간 합)", ["서비스", "비용", "비중"], [36, 16, 10],
          [[x["name"], x["cost"], (x["cost"] / tot) if tot else None] for x in act["by_service"]], "costByService", (1, 2))
    table("리소스별", "리소스별 비용(기간 합, 상위)", ["리소스", "리소스 그룹", "서비스", "비용"], [40, 30, 24, 16],
          [[x["name"], x["rg"], x["service"], x["cost"]] for x in act["by_resource"]], "costByResource", (3,))
    table("태그별", "태그별 비용(기간 합)", ["태그 키", "태그 값", "비용"], [20, 40, 16],
          [[x["key"], x["value"] if x["value"] is not None else "(태그 없음)", x["cost"]] for x in act["by_tag"]], "costByTag", (2,))
    table("과금유형별", "과금 유형별 비용(상각 기준)", ["과금 모델", "청구 유형", "비용"], [20, 20, 16],
          [[x["model"], x["charge"], x["cost"]] for x in act["by_pricing"]], "costByPricingModel", (2,))
    table("예산", "예산 설정", ["예산", "금액", "주기", "현재 사용", "임계값(%)"], [30, 14, 12, 14, 20],
          [[b["name"], b["amount"], b["grain"], b["spend"], ", ".join(str(t) for t in b["thresholds"])] for b in act["budgets"]], "budgets", (1, 3))

    # AI 모델별
    if res["track"] == "existing":
        E = res["existing"]
        table("AI 모델별", "AI 모델별 월 비용(구성비 환산)", ["모델 계열", "월 비용", "입력", "출력", "캐시", "우선 처리분", "입력 단가/1M", "출력 단가/1M"],
              [30, 14, 14, 14, 14, 14, 14, 14],
              [[m["family"], m["monthly"], m["in"], m["out"], m["cached"], m["pp"], m["price"].get("in"), m["price"].get("out")] for m in E["models"]],
              "costByResource", (1, 2, 3, 4, 5, 6, 7))
    if res["track"] == "existing":
        tk = res["existing"].get("tokens") or {}
        table("토큰 사용량", "배포별 토큰 사용량(Azure Monitor) × 공식 단가",
              ["계정", "배포", "모델", "관측 일수", "요청", "입력 토큰", "출력 토큰", "요청당 입력", "요청당 출력", "단가 기준 비용"],
              [20, 26, 24, 10, 10, 14, 14, 12, 12, 14],
              [[t["account"], t["deployment"], t["model"], t["days"], t["requests"], t["in"], t["out"], t["in_per_req"], t["out_per_req"], t["list_cost"]]
               for t in tk.get("rows", [])], "aiMetricsTokensDaily", (3, 4, 5, 6, 7, 8, 9))
    # 단가표
    rows = [[k, v.get("product"), v.get("in"), v.get("out"), v.get("cached"), v.get("batch_in"), v.get("in_pp")]
            for k, v in sorted(res["model_prices"].items())]
    table("단가표", "모델 단가(Azure Retail Prices, Global 종량제, USD/100만 토큰)",
          ["모델 계열", "제품", "입력", "출력", "캐시 입력", "Batch 입력", "우선 처리 입력"], [30, 26, 12, 12, 12, 12, 14],
          rows, "pricesModels", (2, 3, 4, 5, 6))

    # 3 시나리오 / ROI — 엑셀 수식
    summary = []
    if res["track"] == "existing":
        E = res["existing"]
        ws = sheet("시나리오", "토큰 최적화 절감 시나리오 — 수식(가정값 시트 연동)", ["항목", "값", "식·근거"], [34, 18, 80])
        L = {l["id"]: l for l in E["levers"]}
        bs = E["base_split"]
        rows = [
            ("기준: 입력 월비용", bs["in"], "costByResource 입력 미터 구성비 × AI 월 비용"),
            ("기준: 출력 월비용", bs["out"], "출력 미터"),
            ("기준: 캐시·기타 월비용", bs["cached"] + bs["other"], "캐시·기타 미터"),
            ("우선 처리 월비용", L["pp"]["inputs"].get("pp_cost") or 0, "PP 미터"),
            ("표준/우선 처리 단가비", L["pp"]["inputs"].get("ratio") if L["pp"]["inputs"].get("ratio") is not None else 1, "pricesModels"),
            ("Batch/표준 단가비", L["batch"]["inputs"].get("ratio") or 0.5, "pricesModels 또는 공식 50%"),
            ("캐시/입력 단가비", L["cache"]["inputs"].get("ratio") if L["cache"]["inputs"].get("ratio") is not None else 1, "pricesModels"),
            ("경량/현재 입력 단가비", L["route"]["inputs"].get("ratio_in") if L["route"]["inputs"].get("ratio_in") is not None else 1, f"경량 모델 {E['light_model']}"),
            ("경량/현재 출력 단가비", L["route"]["inputs"].get("ratio_out") if L["route"]["inputs"].get("ratio_out") is not None else 1, ""),
        ]
        for i, (k, v, s) in enumerate(rows):
            ws.write(4 + i, 0, k, f_t)
            ws.write_number(4 + i, 1, float(v), f_n)
            ws.write(4 + i, 2, s, f_t)
        # B5 in, B6 out, B7 cached, B8 pp, B9 rpp, B10 rb, B11 rc, B12 rin, B13 rout
        f = [
            ("월 기준 AI 비용", "=B5+B6+B7", "입력 + 출력 + 캐시·기타"),
            ("S1 우선 처리 → 표준", f"=B8*{names['pp_shift']}*(1-B9)", "PP 월비용 × 이전 비율 × (1 − 단가비)"),
            ("보정 계수", "=IF(B5+B6=0,0,B15/(B5+B6))", "S1 을 입력·출력에 비례 배분"),
            ("S2 일괄 처리", f"={names['batch']}*(B5+B6)*(1-B16)*(1-B10)", "β × 남은(입력+출력) × (1 − Batch 단가비)"),
            ("S3 프롬프트 캐시", f"={names['cache']}*B5*(1-B16)*(1-{names['batch']})*(1-B11)", "κ × 남은 입력 × (1 − 캐시 단가비)"),
            ("S4 모델 라우팅", f"={names['route']}*((B5*(1-B16)*(1-{names['batch']})-B18)*(1-B12)+B6*(1-B16)*(1-{names['batch']})*(1-B13))",
             "ρ × (남은 입력 × (1 − 입력 단가비) + 남은 출력 × (1 − 출력 단가비))"),
            ("월 절감 합", "=B15+B17+B18+B19", "S1 + S2 + S3 + S4"),
            ("개선 후 월 AI 비용", "=B14-B20", ""),
            ("절감률", "=IF(B14=0,0,B20/B14)", ""),
            ("회수기간(개월)", f"=IF(B20<=0,\"회수 불가\",{names['impl']}/B20)", "적용 비용 ÷ 월 절감"),
        ]
        for i, (k, fx, s) in enumerate(f):
            rr = 13 + i
            ws.write(rr, 0, k, f_t)
            ws.write_formula(rr, 1, fx, f_p if k == "절감률" else f_k)
            ws.write(rr, 2, s, f_t)
        summary = [("월 기준 AI 비용", "='시나리오'!B14"), ("월 절감 합", "='시나리오'!B20"), ("개선 후 월 AI 비용", "='시나리오'!B21"),
                   ("절감률", "='시나리오'!B22"), ("회수기간(개월)", "='시나리오'!B23")]
        ws.write(26, 0, "개선 전", f_t)
        ws.write_formula(26, 1, "=B14", f_k)
        ws.write(27, 0, "개선 후", f_t)
        ws.write_formula(27, 1, "=B21", f_k)
        ch = wb.add_chart({"type": "column"})
        ch.add_series({"name": "월 AI 비용", "categories": ["시나리오", 26, 0, 27, 0], "values": ["시나리오", 26, 1, 27, 1],
                       "fill": {"color": "#326B91"}})
        ch.set_title({"name": "개선 전/후"})
        ch.set_legend({"none": True})
        ws.insert_chart("E4", ch)
        wr = sheet("ROI", "누적 순효과 — 월 절감 × 개월 − 적용 비용", ["개월", "누적 순효과"], [10, 18])
        for m in range(0, int(A["horizon_months"]) + 1):
            wr.write_number(4 + m, 0, m, f_n0)
            wr.write_formula(4 + m, 1, f"='시나리오'!$B$20*A{5 + m}-{names['impl']}", f_n)
    else:
        N = res["new"]
        ws = sheet("ROI", "신규 도입 ROI — 수식(가정값 시트 연동)", ["항목", "값", "식·근거"], [34, 18, 80])
        a, b = names["tasks"]
        T = f"'가정값'!$B${a}:$B${b}"
        t_ = f"'가정값'!$C${a}:$C${b}"
        w_ = f"'가정값'!$D${a}:$D${b}"
        e_ = f"'가정값'!$E${a}:$E${b}"
        a_ = f"'가정값'!$F${a}:$F${b}"
        p = N["price"] or {}
        rows = [
            ("현재 인프라 월 비용 I₀", N["I0"]["v"], N["I0"]["src"]),
            (f"입력 단가 Pin ({N['model']})", p.get("in"), f"pricesModels {N['price_region']}"),
            ("출력 단가 Pout", p.get("out"), "pricesModels"),
            ("부속 서비스 X", N["X"]["v"], "pricesSupporting: " + ", ".join(f"{s['service']} {s['sku']}" for s in N["support"])),
        ]
        for i, (k, v, s) in enumerate(rows):
            ws.write(4 + i, 0, k, f_t)
            if v is None:
                ws.write(4 + i, 1, "수집 안 됨", f_na)
            else:
                ws.write_number(4 + i, 1, float(v), f_n)
            ws.write(4 + i, 2, s, f_t)
        I0 = "N(B5)"
        f = [
            ("유지 비용 B", f"={I0}+SUMPRODUCT({T},{t_},{w_})+SUM({e_})", "I₀ + Σ(N × t × w + E)"),
            ("월 요청 R", f"=SUM({T})", "Σ N"),
            ("토큰 비용", f"=B10*({names['tin']}*B6+{names['tout']}*B7)/1000000", "R × (Tin × Pin + Tout × Pout) ÷ 10⁶"),
            ("AI 운영비 A", "=B11+B8", "토큰 + 부속 서비스"),
            ("남는 인건비 W′", f"=SUMPRODUCT({T},{t_},1-{a_},{w_})", "Σ N × t × (1 − a) × w"),
            ("도입 비용 B′", f"={I0}+B12+B13", "I₀ + A + W′"),
            ("월 순효과 Δ", "=B9-B14", "B − B′"),
            ("절감 시간 H", f"=SUMPRODUCT({T},{t_},{a_})", "Σ N × t × a"),
            ("환산 인원", "=B16/209", "H ÷ 209시간"),
            ("회수기간(개월)", f"=IF(B15<=0,\"회수 불가\",{names['K']}/B15)", "K ÷ Δ"),
        ]
        for i, (k, fx, s) in enumerate(f):
            ws.write(8 + i, 0, k, f_t)
            ws.write_formula(8 + i, 1, fx, f_k)
            ws.write(8 + i, 2, s, f_t)
        summary = [("유지 비용 B", "=ROI!B9"), ("도입 비용 B′", "=ROI!B14"), ("월 순효과 Δ", "=ROI!B15"),
                   ("환산 인원", "=ROI!B17"), ("회수기간(개월)", "=ROI!B18")]
        wr = sheet("시나리오", "민감도 — 자동화율·토큰 ±", ["경우", "자동화율 배수", "토큰 배수", "월 순효과", "회수기간"], [12, 14, 12, 16, 14])
        for i, (nm, am, tm) in enumerate((("보수", f"1-{names['sens']}", f"1+{names['sens']}"), ("기준", "1", "1"),
                                          ("낙관", f"1+{names['sens']}", f"1-{names['sens']}"))):
            r_ = 4 + i
            wr.write(r_, 0, nm, f_t)
            wr.write_formula(r_, 1, "=" + am, f_n)
            wr.write_formula(r_, 2, "=" + tm, f_n)
            wr.write_formula(r_, 3, f"=ROI!B9-(N(ROI!B5)+(ROI!B10*({names['tin']}*C{r_ + 1}*ROI!B6+{names['tout']}*C{r_ + 1}*ROI!B7)/1000000+ROI!B8)"
                                    f"+SUMPRODUCT({T},{t_},1-IF({a_}*B{r_ + 1}>1,1,{a_}*B{r_ + 1}),{w_}))", f_k)
            wr.write_formula(r_, 4, f"=IF(D{r_ + 1}<=0,\"회수 불가\",{names['K']}/D{r_ + 1})", f_n)
        wn = sheet("누적", "누적 순효과 — Δ × 개월 − K", ["개월", "누적 순효과"], [10, 18])
        for m in range(0, int(A["horizon_months"]) + 1):
            wn.write_number(4 + m, 0, m, f_n0)
            wn.write_formula(4 + m, 1, f"=ROI!$B$15*A{5 + m}-{names['K']}", f_n)

    # 인터뷰 D영역
    table("인터뷰", "인터뷰 비용 영역 답변", ["문항", "주제", "질문", "점수", "요약"], [8, 18, 60, 8, 60],
          [[q["id"], q["topic"], q["question"], q["score"], q["summary"]] for q in res["interview"]], "costMonthlyTotals" if False else "__none__")
    # 수집 상태
    ws = sheet("수집 상태", "수집 항목별 상태", ["항목", "상태", "레코드", "사유", "출처 API"], [26, 12, 10, 30, 60])
    for i, s in enumerate(act["sections"].values()):
        ws.write_row(4 + i, 0, [s["name"], s["status_ko"], s["count"], s["reason"], s["api"]], f_t)

    # 요약
    ws0.write(0, 0, f"Cost Optimization 요약 — {customer} ({TRACK_KO[res['track']]})", f_title)
    ws0.write(1, 0, f"AI Assessment 시스템 · 통화 {cur} · 비용 수집 {res.get('collected_at') or '-'} · 노란 칸(가정값 시트)을 바꾸면 다시 계산됩니다", f_sub)
    ws0.write_row(3, 0, ["지표", "값", "근거"], f_h)
    ws0.write(4, 0, "현재 월평균 비용(완료월)", f_t)
    v = act["avg_monthly"]["v"]
    ws0.write_number(4, 1, v, f_n) if v is not None else ws0.write(4, 1, "수집 안 됨", f_na)
    ws0.write(4, 2, act["avg_monthly"]["f"] + " / " + act["avg_monthly"]["src"], f_t)
    for i, (k, fx) in enumerate(summary):
        ws0.write(5 + i, 0, k, f_t)
        ws0.write_formula(5 + i, 1, fx, f_p if k == "절감률" else f_k)
        ws0.write(5 + i, 2, "계산 시트 수식", f_t)
    wb.close()
    return bio.getvalue()


# =====================================================================
# PPT — 템플릿 디자인 체계(ai-ref-deck) 재현
# =====================================================================
C = {"primary": "326B91", "ink": "232D2B", "muted": "596660", "light": "EDF3F8", "panel": "F4F7FA", "rowAlt": "F5F7F6",
     "white": "FFFFFF", "c2": "9DC3E6", "c3": "BDD7EE", "c4": "8A9690", "c5": "DEEBF7"}
FONT, FNUM = "Malgun Gothic", "Segoe UI"
FOOTER = "AI Cost Governance & Optimization"
KICK = "06  /  ASSESSMENT · 비용 분석"


def _rgb(h):
    return RGBColor.from_string(h)


class Deck:
    def __init__(self):
        self.p = Presentation()
        self.p.slide_width, self.p.slide_height = Inches(13.333), Inches(7.5)
        self.slides = []

    def text(self, s, t, x, y, w, h, size=13.5, bold=False, color="ink", align="l", valign="m", font=FONT):
        tb = s.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
        tf = tb.text_frame
        tf.word_wrap = True
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        tf.vertical_anchor = {"m": MSO_ANCHOR.MIDDLE, "t": MSO_ANCHOR.TOP, "b": MSO_ANCHOR.BOTTOM}[valign]
        for i, line in enumerate(t.split("\n") if isinstance(t, str) else t):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.alignment = {"l": PP_ALIGN.LEFT, "c": PP_ALIGN.CENTER, "r": PP_ALIGN.RIGHT}[align]
            r = p.add_run()
            r.text = line
            r.font.size, r.font.bold, r.font.name = Pt(size), bold, font
            r.font.color.rgb = _rgb(C.get(color, color))
        return tb

    def rect(self, s, x, y, w, h, fill, line=None, shape=MSO_SHAPE.RECTANGLE):
        r = s.shapes.add_shape(shape, Inches(x), Inches(y), Inches(w), Inches(h))
        r.fill.solid()
        r.fill.fore_color.rgb = _rgb(C.get(fill, fill))
        if line:
            r.line.color.rgb = _rgb(C.get(line, line))
            r.line.width = Pt(1)
        else:
            r.line.fill.background()
        r.shadow.inherit = False
        return r

    def slide(self, kicker=KICK, title="", subtitle="", notes=""):
        s = self.p.slides.add_slide(self.p.slide_layouts[6])
        self.text(s, kicker, 0.62, 0.30, 10.0, 0.25, 9, True, "primary")
        if title:
            self.text(s, title, 0.62, 0.88, 12.08, 0.58, 27 if len(title) < 34 else 24, True)
        if subtitle:
            self.text(s, subtitle, 0.64, 1.64, 12.0, 0.48, 15.3, False, "muted")
        if notes:
            s.notes_slide.notes_text_frame.text = notes
        self.slides.append(s)
        return s

    def callout(self, s, title, body="", y=5.85, h=None):
        h = h or (0.85 if body else 0.6)
        self.rect(s, 0.72, y, 11.96, h, "light")
        tb = self.text(s, title, 0.94, y + 0.06, 11.52, (h - 0.12) if not body else 0.36, 14.4, True, "primary")
        if body:
            self.text(s, body, 0.94, y + 0.42, 11.52, h - 0.48, 12.6, False, "ink")
        return tb

    def note(self, s, t, y=6.62):
        self.text(s, t, 0.72, y, 11.9, 0.27, 10.5, False, "muted")

    def table(self, s, rows, x=0.72, y=2.4, w=11.96, colw=None, rowh=0.36, size=11.7):
        n, m = len(rows), len(rows[0])
        gt = s.shapes.add_table(n, m, Inches(x), Inches(y), Inches(w), Inches(rowh * n))
        tbl = gt.table
        colw = colw or [w / m] * m
        for j, cw in enumerate(colw):
            tbl.columns[j].width = Inches(cw)
        for i, row in enumerate(rows):
            tbl.rows[i].height = Inches(rowh)
            for j, v in enumerate(row):
                c = tbl.cell(i, j)
                c.margin_left = c.margin_right = Inches(0.1)
                c.margin_top = c.margin_bottom = Inches(0.03)
                c.fill.solid()
                c.fill.fore_color.rgb = _rgb(C["primary"] if i == 0 else (C["rowAlt"] if i % 2 else C["white"]))
                tf = c.text_frame
                tf.text = "" if v is None else str(v)
                for p in tf.paragraphs:
                    for r in p.runs:
                        r.font.size, r.font.name = Pt(size), FONT
                        r.font.bold = i == 0 or j == 0
                        r.font.color.rgb = _rgb(C["white"] if i == 0 else (C["primary"] if j == 0 else C["ink"]))
                    if j > 0 and i > 0 and isinstance(v, str) and re.fullmatch(r"(USD |KRW )?-?[\d,.]+(%|개월|명|시간/월|/월)?( / [\d,.]+)?|-", v):
                        p.alignment = PP_ALIGN.RIGHT
        return tbl

    def chart(self, s, kind, cats, series, x, y, w, h, legend=True, labels=False, colors=None, fmt=None):
        if fmt is None:  # 값이 작으면(사용 초기) 소수점 둘째 자리까지 보여 0 으로 뭉개지지 않게
            mx = max([abs(v) for _, vals in series for v in vals if v is not None] or [0])
            fmt = '#,##0' if mx >= 100 else '#,##0.00'
        cd = CategoryChartData()
        cd.categories = cats
        for name, vals in series:
            cd.add_series(name, vals)
        gf = s.shapes.add_chart(kind, Inches(x), Inches(y), Inches(w), Inches(h), cd)
        ch = gf.chart
        ch.has_title = False
        ch.font.name, ch.font.size = FONT, Pt(10.5)
        ch.has_legend = legend
        if legend:
            ch.legend.position = XL_LEGEND_POSITION.BOTTOM
            ch.legend.include_in_layout = False
        cols = colors or [C["primary"], C["c2"], C["c4"], C["c3"], C["muted"], C["c5"]]
        plot = ch.plots[0]
        if kind == XL_CHART_TYPE.DOUGHNUT:
            for i, pt in enumerate(plot.series[0].points):
                pt.format.fill.solid()
                pt.format.fill.fore_color.rgb = _rgb(cols[i % len(cols)])
        else:
            for i, se in enumerate(plot.series):
                if kind in (XL_CHART_TYPE.LINE, XL_CHART_TYPE.LINE_MARKERS):
                    se.format.line.color.rgb = _rgb(cols[i % len(cols)])
                    se.format.line.width = Pt(2.25)
                else:
                    se.format.fill.solid()
                    se.format.fill.fore_color.rgb = _rgb(cols[i % len(cols)])
            try:
                ch.value_axis.tick_labels.number_format = fmt
                ch.value_axis.tick_labels.number_format_is_linked = False
                ch.value_axis.major_gridlines.format.line.color.rgb = _rgb("E6EEF5")
                ch.value_axis.format.line.fill.background()
            except (AttributeError, ValueError):
                pass
        if labels:
            plot.has_data_labels = True
            dl = plot.data_labels
            dl.number_format, dl.number_format_is_linked = fmt, False
            dl.font.size = Pt(10)
            if kind != XL_CHART_TYPE.DOUGHNUT:
                dl.position = XL_LABEL_POSITION.OUTSIDE_END
        return ch

    def stat(self, s, value, label, x, y, w=3.6):
        self.text(s, value, x, y, w, 0.72, 32, True, "primary", font=FNUM)
        self.text(s, label, x, y + 0.74, w, 0.55, 12.6, False, "muted", valign="t")

    def save(self):
        total = len(self.slides)
        for i, s in enumerate(self.slides):
            self.text(s, FOOTER, 0.64, 7.13, 5.10, 0.18, 8.1, False, "muted")
            self.text(s, f"{i + 1:02d} / {total:02d}", 11.75, 7.11, 0.96, 0.22, 9, False, "muted", "r")
        bio = io.BytesIO()
        self.p.save(bio)
        return bio.getvalue()


# PPT 는 고객 문서라 내부 데이터 항목명을 쓰지 않는다
FRIENDLY = [("costMonthlyTotals", "월별 비용"), ("costByService", "서비스별 비용"), ("costByResourceGroup", "리소스 그룹별 비용"),
            ("costByResource", "리소스별 비용"), ("costByTag", "태그별 비용"), ("costByPricingModel", "과금 유형별 비용"),
            ("aiCostDaily", "AI 일별 비용"), ("aiDeployments", "AI 배포 목록"), ("aiMetricsTokensDaily", "토큰 사용량 메트릭"),
            ("aiMetricsPtuUtilization", "PTU 활용률 메트릭"), ("pricesModels", "공식 모델 단가"), ("pricesSupporting", "공식 부속 서비스 단가"),
            ("budgets", "예산"), ("no-provisioned-deployments", "PTU 배포 없음")]


def fr(t):
    t = str(t or "")
    for a, b in FRIENDLY:
        t = t.replace(a, b)
    return t


def pay_txt(v, horizon):
    if v is None:
        return "-"
    return f"{v:.1f}개월" if v <= horizon else f"{horizon:.0f}개월 내 회수 안 됨"


def _na(sec):
    return fr(f"{sec.get('status_ko', '수집 안 됨')}" + (f" ({sec['reason']})" if sec.get("reason") else ""))


def build_pptx(res, customer):
    d = Deck()
    cur = res["currency"]
    act = res["actual"]
    A = res["assumptions"]
    tr = res["track"]
    S = act["sections"]
    # 1 표지
    s = d.slide(kicker="AI ASSESSMENT  ·  COST OPTIMIZATION")
    d.text(s, f"{customer}\nAI 비용 분석 결과", 0.66, 1.05, 11.6, 2.3, 40, True, valign="b")
    d.text(s, f"{TRACK_KO[tr]} 고객 · 수집 기간 {(act['period'] or {}).get('fromUtc', '')[:10]} ~ {(act['period'] or {}).get('toUtc', '')[:10]}",
           0.70, 3.55, 11.9, 0.5, 20, False, "muted")
    d.text(s, "실제 비용·공식 단가·인터뷰 가정으로 계산한 결과", 0.70, 4.4, 11.6, 0.9, 20, True, "primary")
    d.text(s, f"AI Assessment 시스템 · {time.strftime('%Y-%m-%d')}", 0.70, 6.1, 8, 0.35, 13.5, False, "muted")

    avg = act["avg_monthly"]["v"]
    # 2 요약
    if tr == "existing":
        E = res["existing"]
        base, save, pct, pay = E["base"]["v"], E["total_save"]["v"], E["save_pct"]["v"], E["payback"]["v"]
        if not save:
            title = "AI 비용 절감액을 계산할 데이터가 부족합니다"
        elif E.get("payback_in_horizon"):
            title = f"AI 비용은 월 {money(save, cur)}({pct:.0%}) 줄일 수 있습니다"
        else:
            title = f"AI 사용량이 아직 작아 절감 여지는 월 {money(save, cur)}({pct:.0%})입니다"
        sub = "실제 비용(Cost Management)과 공식 단가로 개선 항목별 절감액을 계산했습니다."
        if E.get("base_kind", "full") != "full":
            sub = "완료월 AI 비용이 없어 최근 일별 비용을 월로 환산했습니다(사용 시작 직후)."
        s = d.slide(title=title, subtitle=sub)
        d.stat(s, money(avg, cur), "현재 월평균 전체 비용(완료월)", 0.72, 2.45)
        d.stat(s, money(base, cur), "현재 월 AI 비용", 4.62, 2.45)
        d.stat(s, money(save, cur), f"월 절감 가능액 ({'-' if pct is None else f'{pct:.0%}'})", 8.52, 2.45)
        rows = [["개선 항목", "월 절감", "상태·근거"]] + [[l["name"], money(l["save"], cur) if l["save"] is not None else "-",
                                                         fr(l["status"] if l["save"] is None else l["src"])[:60]] for l in E["levers"]]
        d.table(s, rows, y=3.95, colw=[3.2, 2.2, 6.56], rowh=0.3, size=10.8)
        d.note(s, f"회수기간 {pay_txt(pay, A['horizon_months'])} = 적용 비용 {money(A['impl_cost'], cur)} ÷ 월 절감 · 비율은 가정값(엑셀 가정값 시트에서 변경)")
    else:
        N = res["new"]
        D_, pay = N["D"]["v"], N["pay"]["v"]
        title = (f"AI 도입 시 월 {money(D_, cur)} 절감, {pay:.1f}개월에 회수됩니다" if (D_ and pay) else "현재 가정으로는 도입 효과가 비용보다 작습니다")
        s = d.slide(title=title, subtitle="같은 업무량에서 현재 유지 비용과 AI 도입 비용을 비교했습니다.")
        d.stat(s, money(N["B"]["v"], cur), "현재 유지 월 비용 B", 0.72, 2.45)
        d.stat(s, money(N["Bp"]["v"], cur), "AI 도입 월 비용 B′", 4.62, 2.45)
        d.stat(s, money(D_, cur), "월 순효과 Δ", 8.52, 2.45)
        rows = [["지표", "값", "식"], ["AI 운영비 A", money(N["A"]["v"], cur), N["A"]["f"]], ["토큰 비용", money(N["token"]["v"], cur), N["token"]["f"]],
                ["부속 서비스 X", money(N["X"]["v"], cur), N["X"]["f"]], ["절감 시간", f"{N['H']['v']:,.0f}시간/월", N["H"]["f"]],
                ["환산 인원", f"{N['FTE']['v']:.1f}명", N["FTE"]["f"]], ["회수기간", "-" if pay is None else f"{pay:.1f}개월", N["pay"]["f"]]]
        d.table(s, rows, y=3.95, colw=[3.0, 2.6, 6.36], rowh=0.3, size=10.8)
        sd = res.get("seed")
        d.note(s, f"모델 {N['model']} ({N['price_region']} 공식 단가) · 업무량 {('인터뷰 답변: ' + sd['rule']) if sd else '가정값'} · 인건비·자동화율은 가정값")
        d.note(s, "단가: Azure Retail Prices · 현재 인프라 비용: Cost Management 완료월 평균", y=6.85)

    # 3 월별 추이
    s = d.slide(title="월별 비용 추이", subtitle="Cost Management 실비용(ActualCost)과 그중 AI 서비스 비용")
    if act["monthly"]:
        aim = {x["month"]: x["cost"] for x in act["ai_monthly"]}
        cats = [m["month"] for m in act["monthly"]]
        d.chart(s, XL_CHART_TYPE.COLUMN_CLUSTERED, cats, [("실비용", [m["actual"] or 0 for m in act["monthly"]]),
                                                          ("AI 서비스", [aim.get(c, 0) for c in cats])], 0.72, 2.3, 7.6, 3.7, labels=True)
        d.text(s, ["근거", "Cost Management 월별·서비스별 비용", f"완료월 평균 {money(avg, cur)}", "MTD = 이번 달 진행분"],
               8.7, 2.4, 4.0, 1.6, 13.5, False, "ink", valign="t")
    else:
        d.callout(s, "월별 비용을 수집하지 못했습니다", _na(S["costMonthlyTotals"]), y=2.5)
    d.callout(s, "비용 추이 해석", "MTD 월은 완료되지 않아 평균에서 제외합니다.", y=6.25, h=0.7)

    # 4 서비스별 비중
    s = d.slide(title="서비스별 비용 비중", subtitle="수집 기간 합계 기준 상위 서비스")
    if act["by_service"]:
        top = act["by_service"][:6]
        rest = sum(x["cost"] for x in act["by_service"][6:])
        cats = [x["name"] for x in top] + (["기타"] if rest else [])
        vals = [x["cost"] for x in top] + ([rest] if rest else [])
        d.chart(s, XL_CHART_TYPE.DOUGHNUT, cats, [("비용", vals)], 0.72, 2.25, 6.0, 4.3, labels=False)
        tot = sum(vals)
        d.table(s, [["서비스", "비용", "비중"]] + [[c, money(v, cur), f"{v / tot:.1%}"] for c, v in zip(cats, vals)],
                x=7.0, y=2.4, w=5.68, colw=[2.9, 1.7, 1.08], rowh=0.34, size=11)
    else:
        d.callout(s, "서비스별 비용을 수집하지 못했습니다", _na(S["costByService"]), y=2.5)

    # 5 AI 모델별 (기존) / 비용 구성 (신규)
    if tr == "existing":
        E = res["existing"]
        s = d.slide(title="AI 비용은 소수 모델에 집중됩니다", subtitle="모델 계열별 월 비용(입력·출력·캐시 구성)")
        ms = [m for m in E["models"] if m["monthly"] > 0][:6]
        if ms:
            d.chart(s, XL_CHART_TYPE.BAR_STACKED, [m["family"] for m in ms],
                    [("입력", [m["in"] for m in ms]), ("출력", [m["out"] for m in ms]), ("캐시", [m["cached"] for m in ms])],
                    0.72, 2.3, 7.6, 3.9)
            d.text(s, ["근거", "리소스별 비용의 미터 구성비", "× AI 서비스 월평균", f"기준 {money(E['base']['v'], cur)}/월"],
                   8.7, 2.4, 4.0, 1.6, 13.5, False, "ink", valign="t")
        else:
            d.callout(s, "AI 비용 데이터가 없습니다", _na(S["costByResource"]), y=2.5)
        # 토큰 사용량(Azure Monitor)
        tk = E.get("tokens") or {}
        if tk.get("rows"):
            s = d.slide(title="배포별 토큰 사용량과 단가 기준 비용", subtitle="Azure Monitor 토큰 메트릭(수집 기간 합) × 공식 단가 — 청구 비용과 교차 확인용")
            rows = [["계정 · 배포", "요청", "입력 토큰", "출력 토큰", "요청당 입력/출력", "단가 기준 비용"]]
            for t in tk["rows"][:8]:
                rows.append([f"{t['account']} · {t['deployment']}", f"{t['requests']:,.0f}", f"{t['in']:,.0f}", f"{t['out']:,.0f}",
                             "-" if t["in_per_req"] is None else f"{t['in_per_req']:,.0f} / {t['out_per_req']:,.0f}",
                             money(t["list_cost"], cur) if t["list_cost"] is not None else "단가 없음"])
            d.table(s, rows, y=2.35, colw=[4.0, 1.2, 1.6, 1.6, 1.9, 1.66], rowh=0.38, size=10.8)
            d.note(s, f"관측 일수: 배포별 {', '.join(sorted({str(t['days']) for t in tk['rows']}))}일 · 요청당 토큰은 캐시·Batch 대상 판단에 씁니다")
        elif S.get("aiMetricsTokensDaily"):
            pass
        # 6 절감 전/후
        s = d.slide(title="개선 항목을 순서대로 적용하면 월 AI 비용이 줄어듭니다",
                    subtitle="적용 순서: 우선 처리 → 일괄 처리 → 캐시 → 모델 라우팅 (앞 항목 적용 후 남은 비용에 다음 식)")
        ok = [l for l in E["levers"] if l["save"]]
        if E["base"]["v"]:
            # 폭포형: 보이지 않는 받침 + 절감 막대
            cats = ["개선 전"] + [l["name"] for l in ok] + ["개선 후"]
            lvl, bases, bars = E["base"]["v"], [0], [E["base"]["v"]]
            for l in ok:
                lvl -= l["save"]
                bases.append(lvl)
                bars.append(l["save"])
            bases.append(0)
            bars.append(E["after"]["v"])
            ch = d.chart(s, XL_CHART_TYPE.COLUMN_STACKED, cats, [("받침", bases), ("월 AI 비용·절감", bars)], 0.72, 2.3, 7.8, 3.9,
                         legend=False, colors=["FFFFFF", C["primary"]])
            se = ch.plots[0].series
            se[0].format.fill.background()
            se[1].data_labels.show_value = True
            se[1].data_labels.number_format = "#,##0" if E["base"]["v"] >= 100 else "#,##0.00"
            se[1].data_labels.number_format_is_linked = False
            se[1].data_labels.font.size = Pt(10)
            for i in range(1, len(cats) - 1):
                pt = se[1].points[i]
                pt.format.fill.solid()
                pt.format.fill.fore_color.rgb = _rgb(C["c2"])
            d.text(s, ["가정(엑셀에서 변경)", f"우선 처리 이전 {A['pp_shift']:.0%}", f"일괄 처리 β {A['batch_share']:.0%}",
                       f"캐시 κ {A['cache_share']:.0%}", f"라우팅 ρ {A['route_share']:.0%} → {E['light_model']}"],
                   8.8, 2.4, 3.9, 2.4, 13.5, False, "ink", valign="t")
        else:
            d.callout(s, "기준 AI 비용이 없어 절감 시나리오를 계산하지 않았습니다", _na(S["costByResource"]), y=2.5)
        # 7 ROI
        s = d.slide(title=("최적화 적용 비용은 절감액으로 회수됩니다" if E.get("payback_in_horizon")
                           else "지금 사용량에서는 적용 비용을 기간 안에 회수하지 못합니다"),
                    subtitle="누적 순효과 = 월 절감 × 개월 − 적용 비용 · 사용량이 늘면 절감액도 같은 비율로 커집니다")
        cum = [c for c in E["cum"] if c["v"] is not None]
        if cum:
            d.chart(s, XL_CHART_TYPE.LINE_MARKERS, [str(c["m"]) for c in cum], [("누적 순효과", [c["v"] for c in cum])], 0.72, 2.3, 8.0, 3.9, legend=False)
            pv = E["payback"]["v"]
            if pv is not None and pv <= A["horizon_months"]:
                d.stat(s, f"{pv:.1f}개월", "회수기간", 9.1, 2.6, w=3.7)
            else:
                d.stat(s, "회수 안 됨", f"{A['horizon_months']:.0f}개월 기준 · 월 절감 {money(E['total_save']['v'], cur)}", 9.1, 2.6, w=3.7)
        else:
            d.callout(s, "절감액이 없어 회수기간을 계산하지 않았습니다", "", y=2.5)
    else:
        N = res["new"]
        s = d.slide(title="도입 비용의 대부분은 남는 인건비이고 AI 운영비는 작습니다", subtitle="현재 유지 vs AI 도입 월 비용 구성")
        I0 = N["I0"]["v"] or 0
        d.chart(s, XL_CHART_TYPE.COLUMN_STACKED, ["현재 유지", "AI 도입"],
                [("인프라 I₀", [I0, I0]), ("인건비", [N["B"]["v"] - I0, N["W"]["v"]]), ("AI 운영비", [0, N["A"]["v"] or 0])],
                0.72, 2.3, 7.4, 3.9)
        d.table(s, [["부속 서비스", "SKU", "월 비용"]] + [[x["service"], x["sku"], money(x["monthly"], cur)] for x in N["support"]],
                x=8.4, y=2.4, w=4.28, colw=[1.9, 1.2, 1.18], rowh=0.34, size=10.5)
        s = d.slide(title="회수기간은 자동화율 가정에 따라 달라집니다", subtitle=f"자동화율·토큰 ±{A['sens']:.0%} 세 가지 경우와 누적 순효과")
        d.table(s, [["경우", "월 순효과", "회수기간"]] + [[c["name"], money(c["D"], cur), "-" if not c["pay"] else f"{c['pay']:.1f}개월"] for c in N["cases"]],
                x=8.4, y=2.4, w=4.28, colw=[1.2, 1.7, 1.38], rowh=0.4)
        cum = [c for c in N["cum"] if c["v"] is not None]
        if cum:
            d.chart(s, XL_CHART_TYPE.LINE_MARKERS, [str(c["m"]) for c in cum], [("누적 순효과", [c["v"] for c in cum])], 0.72, 2.3, 7.4, 3.9, legend=False)

    # 권고
    s = d.slide(title="권고 사항", subtitle="계산 결과와 수집 상태에서 나온 다음 조치")
    rec = []
    if tr == "existing":
        for l in sorted([l for l in res["existing"]["levers"] if l["save"]], key=lambda x: -x["save"]):
            rec.append([l["name"], money(l["save"], cur) + "/월", l["f"][:70]])
    else:
        rec.append(["시범 적용", money(res["new"]["D"]["v"], cur) + "/월", "대상 업무 1개로 자동화율 실측 후 가정값 갱신"])
    if not act["budgets"]:
        rec.append(["예산·알림 설정", "통제", "예산이 없음 — Cost Management 예산과 임계값 알림"])
    # 신규 도입 고객에게는 운영 메트릭 미수집이 조치 대상이 아니다
    for nm in (("aiMetricsTokensDaily", "aiMetricsPtuUtilization") if tr == "existing" else ()):
        if S[nm]["status"] != "collected" and S[nm].get("reason") != "no-provisioned-deployments":
            rec.append(["사용량 메트릭 확인", "데이터", f"{fr(nm)}: {_na(S[nm])}"])
    d.table(s, [["조치", "효과", "근거"]] + rec[:8], y=2.4, colw=[3.0, 2.2, 6.76], rowh=0.42)

    # 산출식 근거
    s = d.slide(title="산출식과 데이터 근거", subtitle="모든 수치는 같은 계산 함수에서 나오며, 엑셀에 식이 그대로 들어 있습니다")
    if tr == "existing":
        E = res["existing"]
        rows = [["항목", "식", "데이터"]] + [[l["name"], l["f"], fr(l["src"])[:48]] for l in E["levers"][:6]]
    else:
        N = res["new"]
        lab = {"B": "유지 비용 B", "token": "토큰 비용", "X": "부속 서비스 X", "A": "AI 운영비 A", "W": "남는 인건비 W′", "D": "월 순효과 Δ", "pay": "회수기간"}
        rows = [["항목", "식", "데이터"]] + [[lab[k], N[k]["f"], fr(N[k]["src"])[:48]] for k in ("B", "token", "X", "A", "W", "D", "pay")]
    d.table(s, rows, y=2.35, colw=[2.6, 5.4, 3.96], rowh=0.42, size=10.5)
    d.note(s, "단가: Azure Retail Prices API · 비용: Cost Management Query · 근거: Azure AI Landing Zones 비용 항목, Well-Architected 비용 최적화", y=6.55)
    return d.save()
