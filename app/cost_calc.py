"""Cost Optimization 계산 — 화면·엑셀·PPT 가 모두 이 모듈의 compute() 결과 하나만 쓴다.

근거
- 산출식: 06-C 비용 분석 백데이터(변수표 → 식 → 예시), collector/03-cost-requirements.md
- 데이터: 인프라 분석 케이스 Source/aicaf-evidence.json 의 cost*/ai*/prices* 섹션, 인터뷰 D영역 답변
- 원칙: 수집되지 않은 값은 0 으로 채우지 않는다. 각 값은 {v, f(식), src(출처)} 로 근거를 함께 싣는다.
"""
import json
import re
from collections import defaultdict

HOURS_PER_MONTH = 730          # Azure 가격표 월 환산 기준 시간
WORK_HOURS_PER_MONTH = 209     # 환산 인원(FTE) 기준 월 근무시간
STATUS_KO = {"collected": "수집됨", "not_present": "해당 없음", "permission_denied": "권한 없음",
             "collection_failed": "수집 실패", "partial": "일부 수집", "missing": "수집 안 됨"}
AI_SERVICES = {"Foundry Models", "Foundry Tools", "Cognitive Services", "Azure OpenAI"}

# ---------------- 기본 가정값 (화면에서 편집, 저장) ----------------
DEFAULTS = {
    "common": {"fx_krw": 1400.0, "horizon_months": 24},
    "existing": {
        # 개선 항목별 적용 비율(06-C 계산 예시 값). 모두 '가정' — 인터뷰·담당자 확인 대상
        "pp_shift": 0.5,       # 우선 처리(PP) 요청 중 표준 처리로 옮길 비율
        "batch_share": 0.25,   # β 즉시 응답이 필요 없는 일괄 처리 가능 비율
        "cache_share": 0.375,  # κ 캐시 가능한 입력 토큰 비율(공통 앞부분 1,024토큰 이상)
        "route_share": 0.4,    # ρ 경량 모델로 옮길 요청 비율
        "light_model": "",     # 경량 모델(빈 값이면 가격표에서 자동 선택)
        "impl_cost": 20000.0,  # 최적화 적용 일회성 비용(USD, 가정)
    },
    "new": {
        "tasks": [{"name": "문서 검토", "N": 2000, "t": 0.5, "w": 30.0, "E": 0.0, "a": 0.6}],
        "tin": 6000, "tout": 800,          # 요청당 입력·출력 토큰
        "model": "",                       # 모델(빈 값이면 가격표에서 자동 선택)
        "region": "koreacentral",
        "support": {"search": "Basic", "apim": "Basic v2", "log_gb": 10.0},
        "K": 80000.0,                      # 초기 구축비(USD)
        "sens": 0.3,                       # 민감도 ±30%
    },
}


def _num(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def merge_assumptions(saved, track):
    """저장된 편집값을 기본값 위에 덮는다. 모르는 키는 버린다."""
    base = json.loads(json.dumps({**DEFAULTS["common"], **DEFAULTS[track]}))
    saved = saved or {}
    for k, v in saved.items():
        if k not in base:
            continue
        if isinstance(base[k], dict) and isinstance(v, dict):
            base[k].update({kk: vv for kk, vv in v.items() if kk in base[k]})
        elif k == "tasks" and isinstance(v, list):
            base[k] = [{"name": str(t.get("name", ""))[:40], **{f: _num(t.get(f), 0.0) for f in ("N", "t", "w", "E", "a")}}
                       for t in v[:5] if isinstance(t, dict)] or base[k]
        elif isinstance(base[k], (int, float)):
            n = _num(v)
            if n is not None:
                base[k] = n
        elif isinstance(base[k], str):
            base[k] = str(v)[:80]
    return base


def val(v, f="", src="", unit="USD"):
    """근거가 붙은 값. v 가 None 이면 화면은 '-' 와 상태를 보여 준다."""
    return {"v": None if v is None else round(float(v), 6), "f": f, "src": src, "unit": unit}


# ---------------- 증거 파일 ----------------
def section(ev, name):
    """섹션 상태와 레코드. 파일·섹션이 없으면 status=missing."""
    if not ev:
        return "missing", "aicaf-evidence.json 없음", []
    s = (ev.get("sections") or {}).get(name)
    if not isinstance(s, dict):
        return "missing", "수집 스크립트 버전에 이 항목이 없음", []
    return s.get("status") or "missing", s.get("reason") or "", s.get("records") or []


def sec_info(ev, name):
    st, reason, recs = section(ev, name)
    api = ((ev or {}).get("sections") or {}).get(name, {}).get("api", "") if ev else ""
    return {"name": name, "status": st, "status_ko": STATUS_KO.get(st, st), "reason": reason, "count": len(recs), "api": api}


# ---------------- 가격표 해석 ----------------
_DIR_IN = re.compile(r"\b(inp|inpt|input)\b", re.I)
_DIR_OUT = re.compile(r"\b(opt|outp|outpt|output)\b", re.I)
_GLOBAL = re.compile(r"\b(gl|glbl)\b", re.I)
_FLAGS = {
    "pp": re.compile(r"\bpp\b", re.I),
    "batch": re.compile(r"\bbatch\b", re.I),
    "cached": re.compile(r"\b(cchd|cached|cd)\b", re.I),
    "cwrite": re.compile(r"\bwr\b", re.I),
}
_EXCL = re.compile(r"\b(flex|fl|dz|dzone|data ?zone|regnl|rgnl|regional|aud|rt|tts|transcribe|tcrb|ft|img|image|media|"
                   r"training|hosting|grader|dev|computer-use|mdel|grdr)\b", re.I)
_DROP = re.compile(r"\b(inp|inpt|input|opt|outp|outpt|output|gl|glbl|std|pp|batch|cchd|cached|cd|wr|tokens?|1m|1k)\b", re.I)


def meter_parse(meter):
    """미터명 → (모델 계열, 방향 in/out, 플래그). 계열은 방향·처리방식 낱말을 지운 나머지."""
    m = (meter or "").replace("-", " ")
    d = "in" if _DIR_IN.search(m) else "out" if _DIR_OUT.search(m) else None
    flags = {k for k, rx in _FLAGS.items() if rx.search(m)}
    fam = re.sub(r"\s+", " ", _DROP.sub(" ", m)).strip().lower()
    return fam, d, flags


def price_catalog(ev, region="eastus2"):
    """Global 종량제 토큰 단가 카탈로그: 계열 → {in, out, in_pp, out_pp, cached, batch_in, batch_out} (USD/100만 토큰)."""
    st, reason, recs = section(ev, "pricesModels")
    cat = defaultdict(dict)
    regions = sorted({r.get("armRegionName") for r in recs})
    use = region if region in regions else ("eastus2" if "eastus2" in regions else (regions[0] if regions else None))
    for r in recs:
        if r.get("armRegionName") != use or r.get("type") != "Consumption":
            continue
        meter = r.get("meterName") or ""
        mm = meter.replace("-", " ")
        if not _GLOBAL.search(mm) or _EXCL.search(mm):
            continue
        unit = r.get("unitOfMeasure")
        mult = 1000.0 if unit == "1K" else 1.0 if unit == "1M" else None
        if mult is None:
            continue
        fam, d, flags = meter_parse(meter)
        if not d or not fam:
            continue
        key = d
        if "cached" in flags:
            if "batch" in flags or d != "in":
                continue
            key = "cached"
        elif "cwrite" in flags:
            continue
        elif "batch" in flags:
            key = "batch_" + d
        elif "pp" in flags:
            key = d + "_pp"
        cat[fam].setdefault(key, round(float(r.get("retailPrice") or 0) * mult, 6))
        cat[fam].setdefault("product", r.get("productName") or "")
    out = {k: v for k, v in cat.items() if "in" in v and "out" in v}
    return {"status": st, "reason": reason, "region": use, "models": out}


def pick_model(catalog, wanted, prefer=("gpt 5 mini", "5.4 mini", "gpt 4.1 mini")):
    models = catalog["models"]
    if wanted in models:
        return wanted
    for p in prefer:
        if p in models:
            return p
    return min(models, key=lambda k: models[k]["in"]) if models else ""


def support_price(ev, region, service, sku, meter_kw):
    """부속 서비스 단가 1건(USD, 단위 그대로)."""
    st, reason, recs = section(ev, "pricesSupporting")
    for r in recs:
        if r.get("armRegionName") == region and r.get("type") == "Consumption" and r.get("skuName") == sku \
                and r.get("serviceName") == service and meter_kw in (r.get("meterName") or "") and (r.get("retailPrice") or 0) > 0:
            return float(r["retailPrice"]), r.get("unitOfMeasure"), r.get("meterName")
    return None, None, None


# ---------------- 실제 비용(공통) ----------------
def actual_costs(ev):
    out: dict = {"sections": {n: sec_info(ev, n) for n in (
        "costMonthlyTotals", "costByService", "costByResourceGroup", "costByResource", "costByTag",
        "costByPricingModel", "budgets", "aiCostDaily", "aiDeployments", "aiMetricsTokensDaily",
        "aiMetricsPtuUtilization", "pricesModels", "pricesSupporting")}}
    cur = None
    st, _, recs = section(ev, "costMonthlyTotals")
    months = defaultdict(dict)
    for r in recs:
        months[r["month"]][r.get("costType")] = float(r.get("Cost") or 0)
        cur = cur or r.get("Currency")
    out["monthly"] = [{"month": m, "actual": v.get("ActualCost"), "amortized": v.get("AmortizedCost")} for m, v in sorted(months.items())]
    full = [x for x in out["monthly"] if "MTD" not in x["month"] and x["actual"] is not None]
    out["full_months"] = [x["month"] for x in full]
    out["avg_monthly"] = val(sum(x["actual"] for x in full) / len(full) if full else None,
                             f"완료월 실비용 평균 = Σ ActualCost ÷ {len(full)}개월", "costMonthlyTotals (Cost Management Query)")
    st, _, recs = section(ev, "costByService")
    svc = defaultdict(float)
    svc_month = defaultdict(lambda: defaultdict(float))
    for r in recs:
        svc[r.get("ServiceName") or "(없음)"] += float(r.get("Cost") or 0)
        svc_month[r.get("month")][r.get("ServiceName") or "(없음)"] += float(r.get("Cost") or 0)
        cur = cur or r.get("Currency")
    total = sum(svc.values())
    out["by_service"] = [{"name": k, "cost": v, "share": (v / total if total else None)} for k, v in sorted(svc.items(), key=lambda x: -x[1])]
    out["ai_monthly"] = [{"month": m, "cost": sum(v for k, v in d.items() if k in AI_SERVICES)} for m, d in sorted(svc_month.items())]
    st, _, recs = section(ev, "costByResourceGroup")
    rg = defaultdict(float)
    for r in recs:
        rg[r.get("ResourceGroupName") or "(없음)"] += float(r.get("Cost") or 0)
    out["by_rg"] = [{"name": k, "cost": v} for k, v in sorted(rg.items(), key=lambda x: -x[1])]
    st, _, recs = section(ev, "costByResource")
    res = defaultdict(float)
    meta = {}
    for r in recs:
        rid = r.get("ResourceId") or ""
        res[rid] += float(r.get("Cost") or 0)
        meta[rid] = r.get("ServiceName")
    out["by_resource"] = [{"id": k, "name": k.split("/")[-1] or "(없음)", "rg": (k.split("/resourcegroups/")[1].split("/")[0] if "/resourcegroups/" in k.lower() else ""),
                           "service": meta[k], "cost": v} for k, v in sorted(res.items(), key=lambda x: -x[1])]
    st, _, recs = section(ev, "costByTag")
    out["by_tag"] = [{"key": r.get("tagKey"), "value": r.get("TagValue"), "cost": float(r.get("Cost") or 0)} for r in recs]
    st, _, recs = section(ev, "costByPricingModel")
    pm = defaultdict(float)
    for r in recs:
        pm[(r.get("PricingModel") or "", r.get("ChargeType") or "")] += float(r.get("Cost") or 0)
    out["by_pricing"] = [{"model": k[0], "charge": k[1], "cost": v} for k, v in sorted(pm.items(), key=lambda x: -x[1])]
    st, _, recs = section(ev, "budgets")
    out["budgets"] = [{"name": r.get("name"), "amount": r.get("amount"), "grain": r.get("timeGrain"), "spend": r.get("currentSpend"),
                       "thresholds": [t.get("threshold") for t in (r.get("thresholds") or [])]} for r in recs]
    out["currency"] = cur or "USD"
    out["period"] = (ev or {}).get("costPeriod") or {}
    return out


_DZ = re.compile(r"\b(dz|dzone|data ?zone)\b", re.I)


def _date(s):
    import datetime
    return datetime.date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def token_usage(ev, cat):
    """Azure Monitor 토큰 메트릭(배포별 합). 단가표로 토큰 기준 비용을 따로 계산해 청구 비용과 교차 확인한다."""
    st, reason, recs = section(ev, "aiMetricsTokensDaily")
    st_d, _, deps = section(ev, "aiDeployments")
    model_of = {(str(d.get("accountId", "")).split("/")[-1].lower(), d.get("name")): d.get("modelName") for d in deps}
    agg = defaultdict(lambda: defaultdict(float))
    days = defaultdict(set)
    for r in recs:
        acc = (r.get("resourceId") or "").split("/")[-1]
        dep = (r.get("dimensions") or {}).get("modeldeploymentname") or ""
        agg[(acc, dep)][r.get("metric")] += float(r.get("total") or 0)
        days[(acc, dep)].add((r.get("time") or "")[:10])
    rows = []
    for (acc, dep), m in sorted(agg.items(), key=lambda x: -x[1].get("TokenTransaction", 0)):
        model = model_of.get((acc.lower(), dep)) or dep
        fam = re.sub(r"[-_]", " ", model.lower()).replace("gpt ", "")
        p = cat["models"].get(fam) or cat["models"].get(fam + " shortco") or cat["models"].get("gpt " + fam) or {}
        pin, pout = m.get("ProcessedPromptTokens", 0), m.get("GeneratedTokens", 0)
        req = m.get("ModelRequests", 0)
        rows.append({"account": acc, "deployment": dep, "model": model, "days": len(days[(acc, dep)]), "requests": req,
                     "in": pin, "out": pout, "in_per_req": (pin / req) if req else None, "out_per_req": (pout / req) if req else None,
                     "list_cost": ((pin * p["in"] + pout * p["out"]) / 1e6) if p else None, "price_key": fam if p else None})
    return {"status": st, "status_ko": STATUS_KO.get(st, st), "reason": reason, "rows": rows}


def ai_meters(ev):
    """AI 미터 비용(기간 합)을 계열·방향·처리방식으로 나눈다. 출처 = costByResource(AI 서비스)."""
    st, _, recs = section(ev, "costByResource")
    rows = []
    for r in recs:
        if (r.get("ServiceName") or "") not in AI_SERVICES:
            continue
        fam, d, flags = meter_parse(r.get("Meter"))
        rows.append({"resource": (r.get("ResourceId") or "").split("/")[-1], "sub": r.get("MeterSubCategory"), "meter": r.get("Meter"),
                     "family": fam, "dir": d, "flags": sorted(flags), "cost": float(r.get("Cost") or 0)})
    return rows


# ---------------- 기구축: 절감 시나리오 ----------------
def existing_model(ev, A, act):
    cat = price_catalog(ev)
    meters = ai_meters(ev)
    nfull = max(1, len(act["full_months"]))
    by_model = defaultdict(lambda: {"in": 0.0, "out": 0.0, "cached": 0.0, "other": 0.0, "pp": 0.0, "total": 0.0})
    for m in meters:
        b = by_model[m["family"] or "(기타)"]
        k = "cached" if ("cached" in m["flags"] or "cwrite" in m["flags"]) else (m["dir"] or "other")
        b[k] += m["cost"]
        b["total"] += m["cost"]
        if "pp" in m["flags"]:
            b["pp"] += m["cost"]
    # 월 기준액 = 서비스별 비용의 AI 서비스 완료월 평균(월별 추이 그래프와 같은 값).
    # 미터별 비용(기간 합, 이번 달 진행분 포함)은 '구성 비율'로만 쓰고 기준액에 맞춰 환산한다.
    full = set(act["full_months"])
    ai_full = [x["cost"] for x in act["ai_monthly"] if x["month"] in full]
    month_base = sum(ai_full) / len(ai_full) if ai_full else None
    base_kind = "full"
    st_d, _, daily = section(ev, "aiCostDaily")
    if not month_base and daily:
        # 완료월 AI 비용이 0(사용 시작 직후)이면 관측 일자 기준 실행률로 월 환산: Σ일별 비용 ÷ 관측 일수 × 30.4
        days = sorted({str(r.get("UsageDate")) for r in daily})
        span = (_date(days[-1]) - _date(days[0])).days + 1 if days else 0
        if span:
            month_base = sum(float(r.get("Cost") or 0) for r in daily) / span * 30.4
            base_kind = f"runrate:{days[0]}~{days[-1]}:{span}"
    meter_total = sum(b["total"] for b in by_model.values())
    k = (month_base / meter_total) if (month_base and meter_total) else 0.0
    models = []
    for fam, b in sorted(by_model.items(), key=lambda x: -x[1]["total"]):
        p = cat["models"].get(fam) or cat["models"].get(_DZ.sub("", fam).strip(), {})  # Data Zone 미터는 같은 모델 Global 단가로 비율 계산
        models.append({"family": fam, "total": b["total"], "monthly": b["total"] * k, "in": b["in"] * k, "out": b["out"] * k,
                       "cached": b["cached"] * k, "pp": b["pp"] * k, "price": p})
    base_in = sum(m["in"] for m in models)
    base_out = sum(m["out"] for m in models)
    base_cached = sum(m["cached"] for m in models)
    base_other = sum(m["monthly"] - m["in"] - m["out"] - m["cached"] for m in models)
    base = base_in + base_out + base_cached + base_other
    if base_kind == "full":
        src_base = (f"costByService AI 서비스({', '.join(sorted(AI_SERVICES))}) 완료월 {len(ai_full)}개월 평균, "
                    f"입력·출력·캐시 구성비는 costByResource 미터 비율")
    else:
        _, rng, sp = base_kind.split(":")
        src_base = (f"완료월 AI 비용 0 → aiCostDaily {rng}({sp}일) 합 ÷ {sp}일 × 30.4 (사용 시작 직후 실행률), "
                    f"구성비는 costByResource 미터 비율")
    levers = []
    top = models[0] if models else None
    tp = (top or {}).get("price") or {}

    # 단가비는 모델별로 구해 비용 가중 평균(유효 단가비)으로 합친다. 단가가 없는 모델(음성 등)은 비율 1 = 절감 없음.
    # 같은 유효 단가비를 엑셀 수식에도 쓰므로 가정값을 바꿔도 화면·엑셀 결과가 같다.
    def light_for(p):
        if A.get("light_model") in cat["models"]:
            lp_ = cat["models"][A["light_model"]]
            return A["light_model"] if lp_["in"] < p["in"] else None
        cands = [m for m, v in cat["models"].items() if v["in"] < p["in"] * 0.5 and v.get("product") == p.get("product")]
        return min(cands, key=lambda m: abs(cat["models"][m]["in"] - p["in"] * 0.2)) if cands else None

    def wavg(pairs):
        w = sum(a for a, _ in pairs)
        return (sum(a * r for a, r in pairs) / w) if w else None
    rb_pairs, rc_pairs, ri_pairs, ro_pairs, pp_pairs, routes = [], [], [], [], [], []
    for m in models:
        p = m["price"] or {}
        has = bool(p.get("in") and p.get("out"))
        rb = (p["batch_in"] / p["in"]) if (has and p.get("batch_in")) else (0.5 if has else 1.0)
        rb_pairs.append((m["in"] + m["out"], rb))
        rc_pairs.append((m["in"], (p["cached"] / p["in"]) if (has and p.get("cached")) else 1.0))
        lt = light_for(p) if has else None
        lp_ = cat["models"].get(lt, {}) if lt else {}
        ri_pairs.append((m["in"], (lp_["in"] / p["in"]) if lp_ else 1.0))
        ro_pairs.append((m["out"], (lp_["out"] / p["out"]) if lp_ else 1.0))
        if lt and (m["in"] or m["out"]):
            routes.append(f"{m['family']}→{lt}")
        pp_pairs.append((m["pp"], (p["in"] / p["in_pp"]) if (has and p.get("in_pp")) else 1.0))

    # L1 우선 처리 → 표준 처리
    pp_cost = sum(m["pp"] for m in models)
    pp_ratio = wavg(pp_pairs) if pp_cost else None
    s_pp = pp_cost * A["pp_shift"] * (1 - pp_ratio) if (pp_cost and pp_ratio is not None) else None
    levers.append({"id": "pp", "name": "우선 처리 → 표준 처리", "save": s_pp, "status": "ok" if s_pp is not None else ("해당 없음" if not pp_cost else "단가 없음"),
                   "f": "우선 처리 월비용 × 이전 비율 × (1 − 표준 단가 ÷ 우선 처리 단가)",
                   "src": f"우선 처리 미터 {pp_cost:,.2f}/월, 단가비 {pp_ratio if pp_ratio is None else round(pp_ratio, 4)} (pricesModels)",
                   "inputs": {"pp_cost": pp_cost, "ratio": pp_ratio, "share": A["pp_shift"]}})
    f_pp = (1 - A["pp_shift"] * (1 - pp_ratio)) if (pp_cost and pp_ratio is not None and (base_in + base_out)) else 1.0
    # 우선 처리 절감은 입력·출력에 비례 배분해 이후 항목의 기준에서 뺀다
    adj = (s_pp or 0) / (base_in + base_out) if (base_in + base_out) else 0
    w_in, w_out = base_in * (1 - adj), base_out * (1 - adj)

    # L2 배포 유형(Global) — 비 Global 배포가 있을 때만
    st, _, deps = section(ev, "aiDeployments")
    nonglobal = [d for d in deps if not str(d.get("skuName") or "").startswith("Global")]
    levers.append({"id": "global", "name": "Global 배포 전환", "save": None,
                   "status": ("해당 없음" if st == "collected" and not nonglobal else STATUS_KO.get(st, st) if st != "collected"
                              else f"비 Global {len(nonglobal)}개({', '.join(d.get('name', '') for d in nonglobal)}) — 데이터 상주 요건 확인 후 판단"),
                   "f": "Σ 배포 토큰 × (현재 단가 − Global 단가) — 데이터 상주 요건 없을 때",
                   "src": f"aiDeployments {len(deps)}개 중 비 Global {len(nonglobal)}개", "inputs": {}})

    # L3 일괄 처리
    b_ratio = wavg(rb_pairs)
    b_ratio = 1.0 if b_ratio is None else b_ratio
    s_batch = A["batch_share"] * (w_in + w_out) * (1 - b_ratio) if base else None
    levers.append({"id": "batch", "name": "일괄 처리(Batch)", "save": s_batch, "status": "ok" if base else "수집 안 됨",
                   "f": "β × (입력 + 출력 월비용) × (1 − Batch 단가 ÷ 표준 단가)",
                   "src": f"모델별 Batch 단가비의 비용 가중 평균 {round(b_ratio, 4)} (pricesModels, Batch 단가 없으면 공식 50% 할인, 토큰 단가 없는 모델은 제외)",
                   "inputs": {"base": w_in + w_out, "ratio": b_ratio, "share": A["batch_share"]}})
    r_in, r_out = w_in * (1 - A["batch_share"]), w_out * (1 - A["batch_share"])

    # L4 프롬프트 캐시
    c_ratio = wavg(rc_pairs)
    s_cache = A["cache_share"] * r_in * (1 - c_ratio) if (base and c_ratio is not None) else None
    levers.append({"id": "cache", "name": "프롬프트 캐시", "save": s_cache, "status": "ok" if s_cache is not None else "입력 토큰 비용 없음",
                   "f": "κ × 남은 입력 월비용 × (1 − 캐시 입력 단가 ÷ 입력 단가)",
                   "src": f"모델별 캐시/입력 단가비의 가중 평균 {c_ratio if c_ratio is None else round(c_ratio, 4)} (pricesModels), Batch 이전분 제외",
                   "inputs": {"base": r_in, "ratio": c_ratio, "share": A["cache_share"]}})
    r_in2 = r_in - (s_cache or 0)

    # L5 모델 라우팅
    # 모델마다 같은 제품군에서 입력 단가가 절반 미만인 모델 중 20%에 가장 가까운 것(또는 사용자가 고른 경량 모델)
    light = ", ".join(routes) if routes else "-"
    ri, ro = wavg(ri_pairs), wavg(ro_pairs)
    if base and ri is not None and ro is not None and routes:
        s_route = A["route_share"] * (r_in2 * (1 - ri) + r_out * (1 - ro))
    else:
        s_route = None
        ri = 1.0 if ri is None else ri
        ro = 1.0 if ro is None else ro
    levers.append({"id": "route", "name": "모델 라우팅", "save": s_route, "status": "ok" if s_route is not None else "더 저렴한 같은 계열 모델 없음",
                   "f": "ρ × (남은 입력 × (1 − 경량/현재 입력 단가) + 남은 출력 × (1 − 경량/현재 출력 단가))",
                   "src": f"{light} (pricesModels, 입력·출력 단가비는 모델별 비용 가중 평균)",
                   "inputs": {"in": r_in2, "out": r_out, "ratio_in": ri, "ratio_out": ro, "share": A["route_share"], "light": light}})

    # L6 PTU
    st, reason, recs = section(ev, "aiMetricsPtuUtilization")
    levers.append({"id": "ptu", "name": "PTU 규모 조정·예약", "save": None,
                   "status": (STATUS_KO.get(st, st) + (f" ({reason})" if reason else "")) if not recs else f"활용률 {len(recs)}건 — 검토",
                   "f": "(현재 PTU − 필요 PTU) × 시간 단가 × 730 / PTU × (시간 단가 − 예약 단가) × 730",
                   "src": "aiMetricsPtuUtilization", "inputs": {}})
    # L7 Gateway 토큰 한도 — 직접 절감 아님
    levers.append({"id": "gateway", "name": "Gateway 토큰 한도", "save": None, "status": "통제(직접 절감 아님)",
                   "f": "월 한도 초과분 차단·부서별 배분", "src": "APIM llm-token-limit 정책", "inputs": {}})
    total = sum(l["save"] for l in levers if l["save"] is not None)
    after = base - total if base else None
    monthly_save = total if base else None
    payback = (A["impl_cost"] / monthly_save) if monthly_save else None
    return {
        "catalog_region": cat["region"], "models": models,
        "base": val(base if base else None, "AI 서비스 월 비용 평균(완료월)" if base_kind == "full" else "AI 일별 비용 실행률 × 30.4일", src_base),
        "base_kind": base_kind, "tokens": token_usage(ev, cat),
        "base_split": {"in": base_in, "out": base_out, "cached": base_cached, "other": base_other},
        "levers": levers, "light_model": light,
        "total_save": val(total if base else None, "Σ 개선 항목 월 절감(적용 순서대로 차감)", "아래 개선 항목"),
        "after": val(after, "개선 전 월 AI 비용 − 월 절감 합", "계산"),
        "save_pct": val((total / base) if base else None, "월 절감 합 ÷ 개선 전 월 AI 비용", "계산", "%"),
        "payback": val(payback, "적용 비용 ÷ 월 절감 합", "가정 적용 비용 + 계산", "개월"),
        "payback_in_horizon": bool(payback is not None and payback <= A["horizon_months"]),
        "cum": [{"m": m, "v": (monthly_save * m - A["impl_cost"]) if monthly_save else None} for m in range(0, int(A["horizon_months"]) + 1)],
    }


# ---------------- 신규 도입 ROI ----------------
def new_model(ev, A, act):
    cat = price_catalog(ev, A["region"])
    model = pick_model(cat, A["model"])
    p = cat["models"].get(model)
    region = A["region"]
    sup = []
    for key, svc, sku, kw, qty in (("search", "Azure Cognitive Search", A["support"].get("search"), "Unit", HOURS_PER_MONTH),
                                   ("apim", "API Management", A["support"].get("apim"), "Unit", HOURS_PER_MONTH),
                                   ("log", "Log Analytics", "Analytics Logs", "Data Ingestion", _num(A["support"].get("log_gb"), 0))):
        if not sku:
            continue
        price, unit, meter = support_price(ev, region, svc, sku, kw)
        sup.append({"key": key, "service": svc, "sku": sku, "meter": meter, "unit": unit, "price": price, "qty": qty,
                    "monthly": (price * qty) if price is not None else None})
    X = sum(s["monthly"] for s in sup if s["monthly"] is not None) if sup and all(s["monthly"] is not None for s in sup) else None
    I0 = act["avg_monthly"]["v"]
    T = A["tasks"]

    def calc(a_mul=1.0, tok_mul=1.0):
        labor = sum(t["N"] * t["t"] * t["w"] + t["E"] for t in T)
        B = (I0 or 0) + labor
        R = sum(t["N"] for t in T)
        token = R * (A["tin"] * tok_mul * p["in"] + A["tout"] * tok_mul * p["out"]) / 1e6 if p else None
        Acost = (token + X) if (token is not None and X is not None) else None
        W = sum(t["N"] * t["t"] * (1 - min(1.0, t["a"] * a_mul)) * t["w"] for t in T)
        Bp = (I0 or 0) + Acost + W if Acost is not None else None
        D = B - Bp if Bp is not None else None
        H = sum(t["N"] * t["t"] * min(1.0, t["a"] * a_mul) for t in T)
        pay = (A["K"] / D) if (D and D > 0) else None
        return {"B": B, "token": token, "A": Acost, "W": W, "Bp": Bp, "D": D, "H": H, "pay": pay, "labor": labor, "R": R}
    base = calc()
    s = A["sens"]
    cases = [("보수", calc(1 - s, 1 + s)), ("기준", base), ("낙관", calc(1 + s, 1 - s))]
    i0src = "costMonthlyTotals 완료월 평균" if I0 is not None else "수집 안 됨 → 0 으로 계산하지 않고 인건비만 비교"
    return {
        "model": model, "price": p, "price_region": cat["region"], "support": sup,
        "I0": val(I0, "현재 인프라 월 비용(완료월 평균)", i0src),
        "B": val(base["B"], "I₀ + Σ(N × t × w + E)" if I0 is not None else "Σ(N × t × w + E)  (I₀ 수집 안 됨 — 인건비만)",
                 "수집 I₀ + 인터뷰·가정 업무량" if I0 is not None else "인터뷰·가정 업무량, I₀ 미포함"),
        "token": val(base["token"], "R × (Tin × Pin + Tout × Pout) ÷ 10⁶", f"모델 {model} 단가 (pricesModels {cat['region']})"),
        "X": val(X, "Σ 부속 서비스 단가 × 수량", f"pricesSupporting {region}"),
        "A": val(base["A"], "토큰 비용 + 부속 서비스 X", "계산"),
        "W": val(base["W"], "Σ N × t × (1 − a) × w", "가정 자동화율"),
        "Bp": val(base["Bp"], "I₀ + A + W′" if I0 is not None else "A + W′  (I₀ 미포함)", "계산"),
        "D": val(base["D"], "B − B′ (양수 = 절감)", "계산"),
        "H": val(base["H"], "Σ N × t × a", "계산", "시간/월"),
        "FTE": val(base["H"] / WORK_HOURS_PER_MONTH, f"H ÷ {WORK_HOURS_PER_MONTH}시간", "계산", "명"),
        "pay": val(base["pay"], "K ÷ Δ (Δ ≤ 0 이면 회수 불가)", "가정 구축비", "개월"),
        "cases": [{"name": n, "a_mul": m, **{k: c[k] for k in ("W", "D", "pay", "A")}} for (n, c), m in zip(cases, (1 - s, 1.0, 1 + s))],
        "cum": [{"m": m, "v": (base["D"] * m - A["K"]) if base["D"] is not None else None} for m in range(0, int(A["horizon_months"]) + 1)],
    }


# ---------------- 인터뷰 → 신규 가정값 시드 ----------------
WORK_DAYS = 22  # 월 근무일(하루 건수 → 월 건수)
_RX_DAY = re.compile(r"하루\s*([\d,]+)\s*건")
_RX_MIN = re.compile(r"(?:한\s*건|건당)[^.\d]{0,12}?(\d+(?:\.\d+)?)\s*분")


def interview_seed(questions, answers):
    """신규구축: 인터뷰 답변(받아쓰기 원문)에서 업무량을 코드로 읽는다. 못 읽으면 None(기본 가정 유지).
    읽는 것: '하루 N건'(월 = N × 22일), '한 건에/건당 M분'. 둘 다 같은 답변에서 나온 것만 쓴다."""
    for q in questions:
        a = answers.get(q["id"]) or {}
        txt = (a.get("transcript") or "") + " " + (a.get("summary") or "")
        d, m = _RX_DAY.search(txt), _RX_MIN.search(txt)
        if d and m:
            n_day = float(d.group(1).replace(",", ""))
            return {"qid": q["id"], "N": n_day * WORK_DAYS, "t": float(m.group(1)) / 60.0,
                    "quote": txt.strip()[:160], "rule": f"하루 {n_day:,.0f}건 × {WORK_DAYS}일, 건당 {m.group(1)}분"}
    return None


# ---------------- 인터뷰 D영역 ----------------
def interview_d(questions, answers):
    out = []
    for q in questions:
        if q["area"] not in ("D",) and not q["id"].startswith("XD"):
            continue
        a = answers.get(q["id"]) or {}
        out.append({"id": q["id"], "topic": q["topic_name"], "question": q["question"], "score": a.get("score") or "",
                    "summary": (a.get("summary") or "")[:200]})
    return out


def compute(ev, track, assumptions, questions=(), answers=None):
    """단일 계산 진입점. 화면 JSON·엑셀·PPT 가 모두 이 결과를 쓴다."""
    seed = interview_seed(questions, answers or {}) if track == "new" else None
    if seed and not (assumptions or {}).get("tasks"):
        # 저장된 업무 편집값이 없을 때만 인터뷰 값으로 채운다(사람이 고친 값이 우선)
        assumptions = dict(assumptions or {})
        assumptions["tasks"] = [{**DEFAULTS["new"]["tasks"][0], "name": "인터뷰 업무", "N": seed["N"], "t": seed["t"]}]
    A = merge_assumptions(assumptions, track)
    act = actual_costs(ev)
    res = {"track": track, "assumptions": A, "actual": act, "has_evidence": bool(ev), "seed": seed,
           "interview": interview_d(questions, answers or {}),
           "collected_at": (ev or {}).get("completedAtUtc"), "currency": act["currency"]}
    if track == "existing":
        res["existing"] = existing_model(ev, A, act)
    else:
        res["new"] = new_model(ev, A, act)
    cat = price_catalog(ev, A.get("region", "eastus2"))
    res["model_options"] = sorted(cat["models"])
    res["model_prices"] = cat["models"]
    return res


def interview_seed_verified(questions, answers):
    """원문 한 답변에서 건수·시간이 모두 검증된 경우만 업무량을 시드한다."""
    for q in questions:
        raw = (answers.get(q["id"]) or {}).get("transcript") or ""
        daily, minutes = _RX_DAY.search(raw), _RX_MIN.search(raw)
        if not daily or not minutes:
            continue
        left = max(0, min(daily.start(), minutes.start()) - 60)
        right = min(len(raw), max(daily.end(), minutes.end()) + 60)
        quote = raw[left:right]
        return {"daily_requests": float(daily.group(1).replace(",", "")), "hours_per_task": float(minutes.group(1))/60,
                "qid": q["id"], "quote": quote, "quote_span": [left,right], "source": "interview_transcript",
                "monthly_projection": "월 근무일 입력 전 미산정"}
    return {}


def compute_finops(ev, track="existing", options=None, topology=None, questions=(), answers=None):
    """기존 계산을 보존한 v2 단일 진입점. 실제 비용 원장과 소매가격 추정을 분리한다."""
    from cost_finops import build_analysis
    return build_analysis(ev, track, options, topology, questions, answers)
