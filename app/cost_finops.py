"""실제 비용 v2 원장. 집계 grain·청구 기준·통화·누락 상태를 혼합하지 않는다.

화면과 문서가 쓰는 수치·수식은 이 결정적 계산 원장에서 함께 나온다.
이전 cost_calc.compute 계약은 변경하지 않으며 compute_finops로 연결한다.
"""
from decimal import Decimal
from collections import defaultdict
from datetime import date
import calendar
import hashlib
import json

CORE_VERSION = "2.0.0"
from pathlib import Path
CORE_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
SCHEMA = "aicaf-finops/v2"


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        n = Decimal(str(value))
        return n if n.is_finite() else None
    except Exception:
        return None


def section(ev, key):
    s = (ev or {}).get("sections", {}).get(key)
    return s if isinstance(s, dict) else {"status": "missing", "records": [], "reason": "수집 안 됨"}


class Ledger:
    """연산과 입력 참조를 원장에 남겨 같은 참조로 Excel 수식을 생성한다."""
    def __init__(self):
        self.nodes = []
        self.values = {}

    def store(self, node):
        # 아직 후속 수식이 연결되기 전의 미확인 입력을 실제 식으로 교체할 때 ID와 위상 순서를 보존한다.
        self.nodes = [n for n in self.nodes if n["id"] != node["id"]]
        self.nodes.append(node)

    def input(self, key, label, value, unit, source=""):
        val = number(value)
        self.values[key] = val
        self.store({"id": key, "label": label, "kind": "input", "value": float(val) if val is not None else None,
                           "unit": unit, "source": source, "status": "known" if val is not None else "missing", "expression": None})
        return key

    def formula(self, key, label, op, args, unit, source="계산 원장"):
        values = [self.values[a] for a in args]
        val = None
        if all(v is not None for v in values):
            if op == "sum":
                val = sum(values, Decimal(0))
            elif op == "sub":
                val = values[0] - values[1]
            elif op == "mul":
                val = values[0] * values[1]
            elif op == "div":
                val = values[0] / values[1] if values[1] != 0 else None
            elif op == "min":
                val = min(values)
            elif op == "max":
                val = max(values)
            else:
                raise ValueError(f"지원하지 않는 계산 연산: {op}")
        self.values[key] = val
        self.store({"id": key, "label": label, "kind": "formula", "value": float(val) if val is not None else None,
                           "unit": unit, "source": source, "status": "known" if val is not None else "missing", "expression": {"op": op, "args": args}})
        return key

    def metric(self, key, status=None):
        node = next(n for n in self.nodes if n["id"] == key)
        return {"value": node["value"], "node": key, "status": status or node["status"], "unit": node["unit"], "source": node["source"]}


DATASETS = {
    "monthly": {"section": "costMonthlyTotals", "label": "월별 전체 비용", "basis": ["ActualCost", "AmortizedCost"],
                "dimensions": ["month", "subscription"], "grain": "월 × 청구 기준 × 통화"},
    "resource": {"section": "costByResource", "label": "자원·미터 상세", "basis": ["ActualCost"],
                 "dimensions": ["subscription", "resource_group", "service", "resource", "region", "sku", "meter", "tag"],
                 "grain": "수집 전체기간 × 자원 × 서비스 × 미터 × 통화"},
    "service": {"section": "costByService", "label": "서비스별 비용", "basis": ["ActualCost"], "dimensions": ["month", "subscription", "service"], "grain": "월 × 서비스 × 통화"},
    "resource_group": {"section": "costByResourceGroup", "label": "리소스 그룹별 비용", "basis": ["ActualCost"], "dimensions": ["month", "subscription", "resource_group"], "grain": "월 × 리소스 그룹 × 통화"},
    "tag": {"section": "costByTag", "label": "청구 태그별 비용", "basis": ["ActualCost"], "dimensions": ["subscription", "tag_value"], "grain": "수집 전체기간 × 태그키 하나 × 태그값 × 통화"},
    "pricing": {"section": "costByPricingModel", "label": "과금유형별 비용", "basis": ["AmortizedCost"], "dimensions": ["month", "subscription", "pricing_model", "charge_type"], "grain": "월 × 과금유형 × 청구유형 × 통화"},
    "ai_daily": {"section": "aiCostDaily", "label": "AI 일별·자원·미터", "basis": ["ActualCost"], "dimensions": ["date", "subscription", "resource_group", "resource", "region", "sku", "meter", "tag"], "grain": "일 × AI 계정 × 미터 × 통화"},
}


def arm_parts(rid):
    parts = str(rid or "").lower().split("/")
    return {k: parts[parts.index(p) + 1] if p in parts and len(parts) > parts.index(p) + 1 else None
            for k, p in (("subscription", "subscriptions"), ("resource_group", "resourcegroups"))}


def usage_date(value):
    if value is None:
        return None
    s = str(value)
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s[:10]


def normalize(ev, topology, dataset):
    config = DATASETS[dataset]
    src = section(ev, config["section"])
    # 실패한 수집에 남은 오래된 행은 실제 비용으로 승격하지 않는다.
    if src.get("status") not in ("collected", "partial") or src.get("ok") is False:
        return []
    inventory = {str(r.get("id", "")).lower(): r for r in (topology or {}).get("resources", [])}
    tag_records = section(ev, "resourceTags")
    tags = {str(r.get("id", "")).lower(): r.get("tags", {}) for r in tag_records.get("records", [])} if tag_records.get("status") in ("collected", "partial") and tag_records.get("ok") is not False else {}
    rows = []
    for i, source in enumerate(src.get("records", [])):
        rid = str(source.get("ResourceId") or "").lower()
        inv = inventory.get(rid, {})
        value = number(source.get("Cost"))
        row = {"id": f"{dataset}_{i}", "amount": float(value) if value is not None else None,
               "currency": source.get("Currency"), "basis": source.get("costType") or (config["basis"][0] if len(config["basis"]) == 1 else None),
               "source": f"{config['section']}.records[{i}]", "source_index": i, "dataset": dataset,
               "month": source.get("month"), "subscription": (ev or {}).get("subscriptionId"),
               "resource": rid or None, "resource_name": inv.get("name") or (rid.split("/")[-1] if rid else None),
               "resource_group": str(source.get("ResourceGroupName") or "").lower() or None, "service": source.get("ServiceName"), "meter": source.get("Meter"),
               "tag_key": source.get("tagKey"), "tag_value": source.get("TagValue"),
               "pricing_model": source.get("PricingModel"), "charge_type": source.get("ChargeType"),
               "date": usage_date(source.get("UsageDate")),
               "region": inv.get("location"), "sku": (inv.get("sku") or {}).get("name"), "tags": tags.get(rid, {}),
               "dimension_sources": {"region": "topology.resources.location", "sku": "topology.resources.sku", "tags": "resourceTags.records.tags"},
               "period": (ev or {}).get("costPeriod", {}), "raw": source}
        if rid:
            row.update({k: v for k, v in arm_parts(rid).items() if v is not None})
        rows.append(row)
    return rows


def row_period(row, start, end):
    if row.get("date"):
        return row["date"], row["date"]
    if row.get("month"):
        month = row["month"][:7]
        y, m = map(int, month.split("-"))
        last = f"{month}-{calendar.monthrange(y, m)[1]:02d}"
        observed_end = str(row.get("period", {}).get("toUtc") or last)[:10]
        last = min(last, observed_end) if "MTD" in row["month"] else last
        return month + "-01", last
    return start, end


def group_rows(calc, rows, group_by, currency):
    buckets = defaultdict(list)
    for r in rows:
        key = r.get(group_by)
        buckets[str(key) if key is not None else "미확인/미배분"].append(r["id"])
    groups = []
    for i, (label, keys) in enumerate(sorted(buckets.items())):
        nid = calc.formula(f"group_{i}", label, "sum", keys, currency)
        groups.append({"label": label, "key": label, "dimension": group_by, **calc.metric(nid), "count": len(keys)})
    return sorted(groups, key=lambda g: -(g["value"] if g["value"] is not None else float("-inf")))


def compare_scenario(calc, rows, opts, currency, basis, total_status, evidence_hash):
    """버킷별 잔액에 순차 적용한다. 기본 절감률은 없으며 확인되지 않은 조치는 계산에서 제외한다."""
    remaining = {r["id"]: r["id"] for r in rows if r["amount"] is not None and r["amount"] > 0}
    actions, savings = [], []
    zero = calc.input("zero", "선택한 조치 없음의 합계 기준", 0, currency, "공집합의 합계(미수집값 대체 아님)")
    overall = calc.formula("remaining_total", "전체 비용 잔액", "max", ["total", zero], currency)
    for i, action in enumerate(opts.get("actions") or []):
        a = dict(action)
        a.update({"source": action.get("evidence") or "근거 미입력", "status": "needs_confirmation", "applied": {"value": None, "status": "not_estimated", "unit": currency}})
        if total_status != "known":
            a["status"] = "baseline_incomplete"
            actions.append(a)
            continue
        if not (action.get("enabled") is True and action.get("scope_confirmed") is True and str(action.get("evidence") or "").strip()):
            actions.append(a)
            continue
        if action.get("evidence_snapshot") != evidence_hash:
            a["status"] = "stale_source" if action.get("evidence_snapshot") else "scope_binding_required"
            actions.append(a)
            continue
        if action.get("currency") != currency or action.get("basis") != basis:
            a["status"] = "incompatible_basis"
            actions.append(a)
            continue
        scoped = sorted(set(action.get("row_ids") or []))
        if not scoped or any(k not in remaining for k in scoped):
            a["status"] = "scope_unavailable"
            actions.append(a)
            continue
        amount = number(action.get("amount"))
        if action.get("kind") != "confirmed_amount" or amount is None or amount < 0:
            a["status"] = "amount_unavailable"
            actions.append(a)
            continue
        stem = f"action_{i}"
        requested = calc.input(stem + "_requested", action.get("name") or "조치 요청액", amount, currency, action["evidence"])
        scope_budget = calc.formula(stem + "_budget", "조치 범위의 남은 비용", "sum", [remaining[k] for k in scoped], currency)
        applied = calc.formula(stem + "_applied", "중복 제거 후 적용 절감액", "min", [requested, scope_budget, overall], currency)
        removed = calc.formula(stem + "_removed", "겹침·잔액상한으로 제외한 금액", "sub", [requested, applied], currency)
        for j, key in enumerate(scoped):
            fraction = calc.formula(stem + f"_weight_{j}", "범위 내 잔액 비중", "div", [remaining[key], scope_budget], "비율")
            if calc.values[scope_budget] == 0:
                fraction = zero
            allocated = calc.formula(stem + f"_allocated_{j}", "버킷 적용 절감", "mul", [applied, fraction], currency)
            remaining[key] = calc.formula(stem + f"_remaining_{j}", "버킷 남은 비용", "sub", [remaining[key], allocated], currency)
        overall = calc.formula(stem + "_overall", "전체 비용 남은 잔액", "sub", [overall, applied], currency)
        a.update(status="estimated", requested=calc.metric(requested), applied=calc.metric(applied), overlap_removed=calc.metric(removed), confidence="사용자 확인 가정(실현 절감 아님)")
        actions.append(a)
        savings.append(applied)
    if not savings:
        return actions, {"as_is": calc.metric("total", total_status), "to_be": calc.metric("total", "no_confirmed_actions"),
                         "savings": {"value": None, "status": "not_estimated", "unit": currency, "source": "확인된 조치 없음"}, "savings_rate": {"value": None, "status": "not_estimated"}}
    calc.formula("gross_savings", "확인된 조치의 겹침 제거 합계", "sum", savings, currency)
    calc.formula("after_known", "확인된 효과 적용 후 비용", "sub", ["total", "gross_savings"], currency)
    recurring = opts.get("recurring_cost") if str(opts.get("recurring_cost_evidence") or "").strip() else None
    calc.input("recurring_cost", "확인된 추가 운영비(동일 기간)", recurring, currency, opts.get("recurring_cost_evidence") or "추가 운영비 미확인")
    calc.formula("to_be", "추가 운영비 포함 TO-BE 예상", "sum", ["after_known", "recurring_cost"], currency)
    calc.formula("savings", "운영 추가비 포함 순절감", "sub", ["total", "to_be"], currency)
    calc.formula("savings_rate", "동일 기준 AS-IS 대비 순절감률", "div", ["savings", "total"], "비율")
    return actions, {"as_is": calc.metric("total", total_status), "to_be": calc.metric("to_be"), "known_to_be": calc.metric("after_known"),
                     "savings": calc.metric("savings", "scenario_estimate"), "gross_savings": calc.metric("gross_savings"), "savings_rate": calc.metric("savings_rate"), "recurring_cost": calc.metric("recurring_cost")}


def dataset_info(ev, topology, currency):
    result = {}
    for key, cfg in DATASETS.items():
        src = section(ev, cfg["section"])
        rows = normalize(ev, topology, key)
        result[key] = {**cfg, "status": "collection_failed" if src.get("ok") is False else src.get("status", "missing"),
                       "reason": src.get("reason") or "", "api": src.get("api") or "", "record_count": len(src.get("records") or []),
                       "currencies": sorted({r["currency"] for r in rows if r["currency"]}),
                       "values": {dim: sorted({str(r[dim]) for r in rows if r.get(dim) is not None and r["currency"] == currency})
                                  for dim in cfg["dimensions"] if dim != "tag"},
                       "tag_keys": sorted({r["tag_key"] for r in rows if r["tag_key"]} | {k for r in rows for k in r["tags"]}),
                       "date_mode": "collection_period_only" if key in ("resource", "tag") else "monthly" if key != "ai_daily" else "daily"}
    return result


def advisor_candidates(ev, selected_rows):
    src = section(ev, "advisorRecommendations")
    if src.get("status") not in ("collected", "partial") or src.get("ok") is False:
        return []
    resource_ids = {r["resource"] for r in selected_rows if r.get("resource")}
    found = []
    for i, r in enumerate(src.get("records") or []):
        if r.get("category") != "Cost":
            continue
        rid = str(r.get("resourceId") or "").lower()
        annual = number(r.get("annualSavingsAmount"))
        matched = rid in resource_ids
        found.append({"id": r.get("id") or f"advisor_{i}", "label": r.get("problem") or "Advisor 비용 권고",
                      "resource": rid or None, "impact": r.get("impact"), "currency": r.get("savingsCurrency"),
                      "annual_estimate": float(annual) if annual is not None else None,
                      "amount_original": r.get("savingsAmount"), "amount_unit": "미수집(원문 보존)",
                      "status": "needs_review" if matched else "scope_not_resolved", "phase": "candidate",
                      "confidence": "Advisor 예상치·구매/가용성 검토 전", "included": False,
                      "overlap_group": digest(rid)[:12], "source": f"advisorRecommendations.records[{i}]",
                      "constraint": "구독 범위 대안·약정기간·대상 범위가 중복될 수 있어 자동 합산하지 않음", "raw": r})
    rank = {"High": 0, "Medium": 1, "Low": 2}
    return sorted(found, key=lambda c: (rank.get(c["impact"], 3), c["currency"] or "", -(c["annual_estimate"] or 0), c["id"]))


def ai_observations(calc, ev, start, end, currency):
    src = section(ev, "aiMetricsTokensDaily")
    deps = section(ev, "aiDeployments")
    dep_map = {(str(d.get("accountId") or "").lower(), d.get("name")): d for d in deps.get("records", [])} if deps.get("status") in ("collected", "partial") else {}
    grouped, observed = defaultdict(lambda: defaultdict(list)), defaultdict(set)
    if src.get("status") in ("collected", "partial") and src.get("ok") is not False:
        for i, r in enumerate(src.get("records") or []):
            day = str(r.get("time") or "")[:10]
            if not day or start and day < start or end and day > end:
                continue
            dims = {str(k).lower(): v for k, v in (r.get("dimensions") or {}).items()}
            key = (str(r.get("resourceId") or "").lower(), dims.get("modeldeploymentname"))
            metric = r.get("metric")
            grouped[key][metric].append((r.get("total"), f"aiMetricsTokensDaily.records[{i}]"))
            observed[key].add(day)
    tokens = []
    for i, ((account, deployment), ms) in enumerate(sorted(grouped.items(), key=lambda item: str(item[0]))):
        dep = dep_map.get((account, deployment), {})
        row = {"account": account, "account_name": account.split("/")[-1], "deployment": deployment,
               "model": dep.get("modelName"), "deployment_type": dep.get("skuName"), "observed_days": len(observed[(account, deployment)]),
               "billing_cost": {"value": None, "status": "not_collected", "reason": "배포별 청구비용 차원이 수집되지 않음"},
               "retail_estimate": {"value": None, "status": "price_mapping_required", "unit": currency, "reason": "정확한 모델·지역·배포유형·단가 행 연결 필요(청구비용과 별개)"}}
        for label, metric in (("input_tokens", "ProcessedPromptTokens"), ("output_tokens", "GeneratedTokens"), ("requests", "ModelRequests"), ("cached_tokens", "CachedTokens")):
            vals = ms.get(metric, [])
            keys = [calc.input(f"token_{i}_{label}_{j}", label, v, "요청" if label == "requests" else "토큰", source) for j, (v, source) in enumerate(vals)]
            node = f"token_{i}_{label}"
            if keys:
                calc.formula(node, label, "sum", keys, "요청" if label == "requests" else "토큰")
            else:
                calc.input(node, label, None, "요청" if label == "requests" else "토큰", metric + " 미수집")
            row[label] = calc.metric(node)
        calc.formula(f"token_{i}_input_per_request", "요청당 입력 토큰", "div", [f"token_{i}_input_tokens", f"token_{i}_requests"], "토큰/요청")
        row["input_per_request"] = calc.metric(f"token_{i}_input_per_request")
        tokens.append(row)
    return {"status": src.get("status", "missing"), "tokens": tokens, "reason": src.get("reason") or "",
            "cost_semantics": "실제 청구 AI 비용·관측 토큰·소매가격 추정은 독립 기준",
            "ptu": section(ev, "aiMetricsPtuUtilization")}


def price_catalogue(ev):
    """Retail Prices의 원본 단위·지역·배포 단가를 보존한다. 실제 청구 할인율로 해석하지 않는다."""
    import re
    found = []
    for sec in ("pricesModels", "pricesSupporting"):
        src = section(ev, sec)
        if src.get("status") not in ("collected", "partial") or src.get("ok") is False:
            continue
        for i, raw in enumerate(src.get("records") or []):
            unit = str(raw.get("unitOfMeasure") or "")
            meter = str(raw.get("meterName") or "")
            match = re.fullmatch(r"([0-9]+)([MK]?)\s+Tokens", unit, re.I)
            denominator = (int(match[1]) * {"M": 1000000, "K": 1000, "": 1}[match[2].upper()]) if match else None
            direction = "input" if re.search(r"\b(Inp|Inpt|Input|Cached)\b", meter, re.I) else "output" if re.search(r"\b(Opt|Outp|Output|Completion)\b", meter, re.I) else None
            value = number(raw.get("retailPrice"))
            found.append({"ref": f"{sec}:{digest(raw)}", "row_identity": digest(raw), "source": f"{sec}.records[{i}]", "meter": meter,
                          "product": raw.get("productName"), "sku": raw.get("skuName"), "region": raw.get("armRegionName"),
                          "currency": raw.get("currencyCode"), "unit": unit, "price": float(value) if value is not None else None,
                          "type": raw.get("type"), "tier_minimum": raw.get("tierMinimumUnits"), "effective_date": raw.get("effectiveStartDate"), "price_observed_at": (ev or {}).get("completedAtUtc"),
                          "reservation_term": raw.get("reservationTerm"), "is_primary": raw.get("isPrimaryMeterRegion"),
                          "token_denominator": denominator, "direction": direction, "raw": raw})
    return found


def chosen_token_price(catalogue, ref, direction, currency):
    row = next((r for r in catalogue if r["ref"] == ref), None)
    if not row or row["currency"] != currency or row["direction"] != direction or row["type"] != "Consumption" or row["tier_minimum"] != 0 or not row["token_denominator"] or row["price"] is None:
        return None
    # 전체 입력 토큰에는 캐시·배치·PTU·context 제한 단가를 자동 적용하지 않는다.
    if row.get("effective_date") and row.get("price_observed_at") and str(row["effective_date"])[:10] > str(row["price_observed_at"])[:10]:
        return None
    restricted = ("cached", "batch", "btch", "ptu", "provisioned", "shtco", "lngco", "shortco", "longco")
    if any(part in str(row.get("meter") or "").lower() for part in restricted) or row["price"] < 0:
        return None
    return row


def token_rate_family(row):
    """수집 미터의 명시적 모델 접두어만 분류한다. 미확인 모델을 유사 이름으로 대체하지 않는다."""
    import re
    meter = row["meter"].lower()
    aliases = ((r"^5\.4 mini\b", "gpt-5.4-mini"), (r"^5\.4 nano\b", "gpt-5.4-nano"), (r"^6\b", "gpt-6"),
               (r"^5\.4\b", "gpt-5.4"), (r"^4\.1 mini\b", "gpt-4.1-mini"), (r"^4\.1\b", "gpt-4.1"), (r"^4o mini\b", "gpt-4o-mini"), (r"^4o\b", "gpt-4o"))
    return next((model for pattern, model in aliases if re.search(pattern, meter)), None)


def compatible_pair(selected):
    """입출력은 동일 모델·지역·배포 단가 문맥이어야 한다."""
    import re
    left, right = selected.get("input"), selected.get("output")
    if not left or not right:
        return False
    def family(row):
        known = token_rate_family(row)
        if known:
            return known
        return re.split(r"\b(?:Inp|Opt|Input|Output|Inpt|Outp)\b", row.get("meter") or "", flags=re.I)[0].strip().lower()
    if family(left) != family(right) or left.get("region") != right.get("region"):
        return False
    def zone(row):
        meter = " " + str(row.get("meter") or "").lower() + " "
        return "global" if " gl " in meter else "datazone" if " dz " in meter else "regional"
    return zone(left) == zone(right)


def apply_token_prices(calc, ai, catalogue, opts, currency):
    mappings = {(str(m.get("account") or "").lower(), m.get("deployment")): m for m in opts.get("ai_mappings") or []}
    known, target_known = [], []
    for i, row in enumerate(ai["tokens"]):
        mapping = mappings.get((row["account"], row["deployment"]), {})
        row["selected_prices"] = {}
        effects = {}
        for scope, in_key, out_key in (("current", "price_in_ref", "price_out_ref"), ("target", "target_in_ref", "target_out_ref")):
            confirmed = scope == "current" or mapping.get("target_confirmed") is True and bool(str(mapping.get("evidence") or "").strip())
            selected = {d: chosen_token_price(catalogue, mapping.get(k), d, currency) if confirmed else None for d, k in (("input", in_key), ("output", out_key))}
            families = {token_rate_family(p) for p in selected.values() if p}
            if not compatible_pair(selected) or len(families) > 1 or scope == "current" and families and row.get("model") not in families:
                selected = {"input": None, "output": None}
            if scope == "current":
                sku = str(row.get("sku") or "").lower()
                for chosen in selected.values():
                    if not chosen:
                        continue
                    meter = " " + str(chosen.get("meter") or "").lower() + " "
                    if "provisioned" in sku or "batch" in sku or "datazone" in sku and " dz " not in meter or "global" in sku and " gl " not in meter:
                        selected = {"input": None, "output": None}
                        break
            parts = []
            for direction, price in selected.items():
                stem = f"token_{i}_{scope}_{direction}"
                calc.input(stem + "_rate", "소매가격 " + direction, price["price"] if price else None, currency + "/단위", price["source"] if price else "모델·단위·방향·통화 단가 연결 미확인")
                calc.input(stem + "_denominator", "단가 단위 토큰", price["token_denominator"] if price else None, "토큰/단위", price["source"] if price else "단위 미확인")
                calc.formula(stem + "_units", "관측 토큰 / 단위", "div", [f"token_{i}_{direction}_tokens", stem + "_denominator"], "단위")
                parts.append(calc.formula(stem + "_cost", "선택 소매단가 기준 비용", "mul", [stem + "_units", stem + "_rate"], currency))
            node = calc.formula(f"token_{i}_{scope}_cost", "관측 사용량의 선택 소매가격 추정", "sum", parts, currency)
            effects[scope] = node
            row["selected_prices"][scope] = selected
        row["retail_estimate"] = {**calc.metric(effects["current"]), "classification": "selected_retail_rate_estimate", "confidence": "단가 연결은 사용자 선택. 지역·배포유형·효력·컨텍스트 확인 필요; 청구비용 아님"}
        row["retail_to_be"] = {**calc.metric(effects["target"]), "classification": "what_if_same_observed_tokens", "source_evidence": mapping.get("evidence")}
        node = calc.formula(f"token_{i}_retail_savings", "같은 관측 사용량의 소매가격 차이", "sub", [effects["current"], effects["target"]], currency)
        row["retail_savings"] = calc.metric(node)
        if row["retail_estimate"]["value"] is not None:
            known.append(effects["current"])
        if row["retail_to_be"]["value"] is not None:
            target_known.append(effects["target"])
    if known and len(known) == len(ai["tokens"]):
        calc.formula("token_retail_total", "단가 연결 완료 배포의 소매가격 추정 합계", "sum", known, currency)
    else:
        calc.input("token_retail_total", "소매가격 추정 합계", None, currency, "전체 배포 단가 연결 미완료")
    ai["retail_estimate"] = calc.metric("token_retail_total")
    ai["pricing_basis"] = "소매 단가·같은 관측 토큰을 사용한 시나리오; Actual/Amortized 청구비용과 합산하지 않음"


def new_estimate(calc, ev, opts, currency, questions, answers, catalogue, prefix="new"):
    from cost_calc import interview_seed_verified
    seed = interview_seed_verified(questions, answers or {})
    no = opts.get("new") or {}
    key = lambda name: f"{prefix}_{name}"
    metric = lambda name: calc.metric(key(name))
    user_source = "사용자 입력 가정(실측으로 승격하지 않음)"
    for name, label, unit in (("hourly_cost", "시간당 비용", currency + "/시간"), ("monthly_requests", "월 요청량", "요청/월"),
                              ("input_tokens", "요청당 입력 토큰", "토큰/요청"), ("output_tokens", "요청당 출력 토큰", "토큰/요청"),
                              ("automation_share", "자동화 적용 비율", "비율"), ("build_cost", "초기 구축비", currency),
                              ("monthly_work_hours", "기존 월 투입 시간", "시간/월"), ("work_days", "월 근무일", "일/월")):
        calc.input(key(name), label, no.get(name), unit, user_source)
    automation = number(no.get("automation_share"))
    if automation is not None and not 0 <= automation <= 1:
        raise ValueError("자동화 적용 비율은 0~1 사이의 확인된 가정이어야 합니다.")
    if seed:
        calc.input(key("daily_requests"), "인터뷰 일 처리량", seed["daily_requests"], "건/일", seed["qid"] + " · " + seed["quote"])
        calc.input(key("hours_per_task"), "인터뷰 건당 소요 시간", seed["hours_per_task"], "시간/건", seed["qid"] + " · " + seed["quote"])
        if no.get("monthly_requests") is None:
            calc.formula(key("monthly_requests"), "월 처리량 = 일 처리량 × 확인 근무일", "mul", [key("daily_requests"), key("work_days")], "건/월")
        if no.get("monthly_work_hours") is None:
            calc.formula(key("monthly_work_hours"), "월 작업시간 = 월 처리량 × 건당 시간", "mul", [key("monthly_requests"), key("hours_per_task")], "시간/월")
    baseline_month = no.get("baseline_month")
    monthly_rows = normalize(ev, None, "monthly")
    baseline = [r for r in monthly_rows if r["month"] == baseline_month and r["currency"] == currency and r["basis"] == "ActualCost"] if baseline_month else []
    if len(baseline) == 1:
        calc.input(key("baseline_cost"), "선택한 완료월의 실제 인프라 비용", baseline[0]["amount"], currency + "/월", baseline[0]["source"])
    else:
        calc.input(key("baseline_cost"), "신규 시나리오 기준 월비용", None, currency + "/월", "완료월 Actual 비용 선택 필요(미수집·MTD 대체 없음)")
    calc.input(key("one"), "잔여 업무비용 계산 단위", 1, "상수", "비율 차감의 수학적 단위")
    calc.input(key("twelve"), "동일 월 시나리오 12개월 반복", 12, "개월", "연간 예측 환산 가정(실적 아님)")
    calc.formula(key("labor_cost"), "기존 월 업무비용", "mul", [key("monthly_work_hours"), key("hourly_cost")], currency + "/월")
    calc.formula(key("remain_share"), "자동화되지 않은 업무 비율", "sub", [key("one"), key("automation_share")], "비율")
    calc.formula(key("remaining_labor"), "도입 후 잔여 업무비용", "mul", [key("labor_cost"), key("remain_share")], currency + "/월")
    selected = {direction: chosen_token_price(catalogue, no.get("price_in_ref" if direction == "input" else "price_out_ref"), direction, currency) for direction in ("input", "output")}
    if not compatible_pair(selected):
        selected = {"input": None, "output": None}
    for direction in ("input", "output"):
        row = selected[direction]
        calc.input(key(direction + "_price"), "선택 소매 단가 " + direction, row["price"] if row else None, currency + "/단위", row["source"] if row else "정확한 통화·방향·단위의 공식 단가 선택 필요")
        calc.input(key(direction + "_denominator"), "소매가격 단위 토큰 수", row["token_denominator"] if row else None, "토큰/단위", row["source"] if row else "가격 단위 미확인")
        calc.formula(key(direction + "_quantity"), "월 요청량 × 요청당 토큰", "mul", [key("monthly_requests"), key(direction + "_tokens")], "토큰/월")
        calc.formula(key(direction + "_units"), "단가 단위로 환산한 월 토큰", "div", [key(direction + "_quantity"), key(direction + "_denominator")], "단위/월")
        calc.formula(key(direction + "_cost"), "방향별 월 AI 소매가격 추정", "mul", [key(direction + "_units"), key(direction + "_price")], currency + "/월")
    calc.formula(key("ai_cost"), "입력·출력 월 AI 추정 합계", "sum", [key("input_cost"), key("output_cost")], currency + "/월")
    support = no.get("support_cost") if no.get("support_cost_evidence") else None
    calc.input(key("support_cost"), "별도 부속 서비스·운영비", support, currency + "/월", no.get("support_cost_evidence") or "인프라 증가·운영비 미확인(0 대체 없음)")
    calc.formula(key("maintain_cost"), "유지 시 월 총비용", "sum", [key("baseline_cost"), key("labor_cost")], currency + "/월")
    calc.formula(key("adoption_cost"), "도입 시 월 총비용", "sum", [key("baseline_cost"), key("remaining_labor"), key("ai_cost"), key("support_cost")], currency + "/월")
    calc.formula(key("net_benefit"), "월 순효과", "sub", [key("maintain_cost"), key("adoption_cost")], currency + "/월")
    calc.formula(key("annual_net_benefit"), "동일 가정 유지 시 연간 순효과 예측", "mul", [key("net_benefit"), key("twelve")], currency + "/년")
    calc.formula(key("roi_months"), "초기 구축비 / 월 순효과", "div", [key("build_cost"), key("net_benefit")], "개월")
    if calc.values[key("net_benefit")] is not None and calc.values[key("net_benefit")] <= 0:
        calc.input(key("roi_months"), "현재 가정에서 회수되지 않음", None, "개월", "월 순효과가 양수가 아님")
    result = {name: metric(name) for name in ("hourly_cost", "monthly_requests", "input_tokens", "output_tokens", "automation_share", "monthly_work_hours", "work_days", "baseline_cost", "maintain_cost", "adoption_cost", "ai_cost", "support_cost", "net_benefit", "annual_net_benefit", "roi_months", "build_cost")}
    result.update(seed=seed, baseline_month=baseline_month, prices=selected, classification="신규 도입 시나리오·소매가격 추정(현재 청구비용과 분리)",
                  status="needs_inputs" if result["adoption_cost"]["value"] is None else "scenario_estimate",
                  limitations=["기존 인프라 비용을 유지한다고 가정하며 증가 비용은 별도 입력", "선택 단가의 모델·지역·배포유형·사용 가능성 확인 필요", "연간 효과는 같은 입력을 12개월 반복하는 예측으로 실적이 아님"])
    result["sensitivities"] = []
    if prefix == "new":
        for i, sc in enumerate(no.get("sensitivities") or []):
            variant = {**no, **sc, "sensitivities": []}
            data = new_estimate(calc, ev, {**opts, "new": variant}, currency, questions, answers, catalogue, f"new_sensitivity_{i}")
            result["sensitivities"].append({"label": sc.get("label") or "사용자 입력 대안", "inputs": sc, "adoption_cost": data["adoption_cost"], "net_benefit": data["net_benefit"], "roi_months": data["roi_months"]})
    return result


def allocation_result(calc, rows, dataset, tag_key, currency):
    if dataset != "resource":
        return {"status": "unsupported_grain", "reason": "원장과 동일한 자원 상세 grain에서 현재 태그 기준 배분을 확인합니다.", "groups": []}
    if not tag_key:
        return {"status": "key_required", "reason": "워크로드·제품·조직·환경의 배분 태그키를 명시적으로 선택하십시오.", "groups": []}
    buckets = defaultdict(list)
    for r in rows:
        value = r["tags"].get(tag_key)
        buckets[str(value) if value not in (None, "") else "미배분/태그 확인 불가"].append(r["id"])
    groups = []
    allocated, unallocated = [], []
    for i, (name, ids) in enumerate(sorted(buckets.items())):
        node = calc.formula(f"allocation_{i}", "배분 " + name, "sum", ids, currency)
        groups.append({"label": name, **calc.metric(node), "count": len(ids)})
        (unallocated if name == "미배분/태그 확인 불가" else allocated).extend(ids)
    calc.formula("allocation_allocated", "태그값 확인된 비용", "sum", allocated, currency)
    calc.formula("allocation_unallocated", "태그 부재·확인 불가 비용", "sum", unallocated, currency)
    calc.formula("allocation_coverage", "현재 태그값 확인 금액 비율", "div", ["allocation_allocated", "total"], "비율")
    return {"status": "known", "key": tag_key, "groups": groups, "total": calc.metric("total"),
            "allocated": calc.metric("allocation_allocated"), "unallocated": calc.metric("allocation_unallocated"), "coverage": calc.metric("allocation_coverage"),
            "semantics": "current_inventory_tags_not_historical_billing_tags", "reason": "수집시점의 현재 태그입니다. 원가 확정·과거 청구시점 태그·공유비용 배분정책으로 해석하지 않습니다."}


def budget_observations(calc, ev):
    src = section(ev, "budgets")
    rows = []
    if src.get("status") in ("collected", "partial") and src.get("ok") is not False:
        for i, raw in enumerate(src.get("records") or []):
            amount = calc.input(f"budget_{i}_amount", "예산 설정 금액", raw.get("amount"), raw.get("currency") or "통화 미수집", f"budgets.records[{i}].amount")
            spend = calc.input(f"budget_{i}_spend", "예산 항목 현재 사용 금액", raw.get("currentSpend"), raw.get("currency") or "통화 미수집", f"budgets.records[{i}].currentSpend")
            ratio = calc.formula(f"budget_{i}_ratio", "원문 예산 항목 내부 사용비율", "div", [spend, amount], "비율")
            rows.append({"name": raw.get("name"), "amount": calc.metric(amount), "current_spend": calc.metric(spend), "ratio": calc.metric(ratio),
                         "currency": raw.get("currency"), "period": raw.get("timePeriod"), "scope": raw.get("filter"), "source": f"budgets.records[{i}]", "raw": raw,
                         "constraint": "예산 범위·기간·통화가 조회 비용과 동일한지 확인 전 대시보드 총비용과 직접 비교하지 않음"})
    return {"status": src.get("status", "missing"), "rows": rows, "reason": src.get("reason") or "예산 알림은 실제 사용 한도·자동 비용 차단이 아님"}


def projection_result(calc, opts, start, end, comparison, currency):
    cfg = opts.get("projection") or {}
    enabled = cfg.get("enabled") is True and bool(str(cfg.get("evidence") or "").strip())
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1 if start and end else None
    year = int(cfg.get("year") or end[:4]) if end else None
    year_days = 366 if year and calendar.isleap(year) else 365
    calc.input("projection_days", "선택기간 포함 달력일 수", days if enabled else None, "일", "끝일−시작일+1; 미완료일 포함은 관측 한계")
    calc.input("projection_year_days", "환산 대상 연도 달력일", year_days if enabled else None, "일/년", str(year) + "년 달력 기준")
    calc.input("projection_months", "연간 개월 수", 12, "개월/년", "달력 단위")
    if comparison["savings"].get("node") is None:
        calc.input("savings", "순절감 미산정", None, currency, "확인된 조치·추가 운영비 확인 필요")
    calc.formula("projection_daily_savings", "기간 순절감 / 포함 달력일", "div", ["savings", "projection_days"], currency + "/일")
    calc.formula("projection_annual_savings", "일평균 순절감 × 연도 달력일", "mul", ["projection_daily_savings", "projection_year_days"], currency + "/년")
    calc.formula("projection_monthly_savings", "연간 환산 순절감 / 12", "div", ["projection_annual_savings", "projection_months"], currency + "/월")
    transition = opts.get("transition_cost") if opts.get("transition_cost_evidence") else None
    calc.input("transition_cost", "확인된 전환 일회성 비용", transition, currency, opts.get("transition_cost_evidence") or "전환 비용 미확인")
    calc.formula("projection_payback", "전환비 / 월 환산 순절감", "div", ["transition_cost", "projection_monthly_savings"], "개월")
    if calc.values["projection_monthly_savings"] is not None and calc.values["projection_monthly_savings"] <= 0:
        calc.input("projection_payback", "현재 가정에서 회수되지 않음", None, "개월", "월 순절감이 양수가 아님")
    cumulative = []
    for month in range(int(cfg.get("horizon_months", 24)) + 1):
        calc.input(f"projection_month_{month}", "누적 비교 월", month, "개월", "사용자 선택 비교기간의 달력 단위")
        calc.formula(f"projection_benefit_{month}", "월 순절감 × 개월", "mul", ["projection_monthly_savings", f"projection_month_{month}"], currency)
        node = calc.formula(f"projection_cash_{month}", "전환비 차감 누적 효과", "sub", [f"projection_benefit_{month}", "transition_cost"], currency)
        cumulative.append({"month": month, **calc.metric(node)})
    return {"enabled": enabled, "status": "scenario_estimate" if enabled else "confirmation_required", "year": year,
            "annual_savings": calc.metric("projection_annual_savings"), "monthly_savings": calc.metric("projection_monthly_savings"),
            "payback_months": calc.metric("projection_payback"), "transition_cost": calc.metric("transition_cost"), "cumulative": cumulative,
            "classification": "same_daily_usage_scenario_not_forecast_actual", "evidence": cfg.get("evidence"),
            "assumptions": ["같은 일평균 사용량·단가·조치 효과가 반복된다고 가정", "선택기간 포함 달력일을 사용하며 미완료일·짧은 관측기간의 한계 유지", "Microsoft 공식 예측 결과나 연간 실적이 아님"]}


def matches_filters(row, filters):
    for key, expected in filters.items():
        if expected in (None, ""):
            continue
        if key == "tag" and isinstance(expected,dict):
            if (row.get("tags") or {}).get(expected["key"]) != expected["value"]:
                return False
        elif expected == "__missing__":
            if row.get(key) not in (None, ""):
                return False
        elif str(row.get(key) or "") != str(expected):
            return False
    return True


def build_analysis(ev, track="existing", options=None, topology=None, questions=(), answers=None):
    from cost_finops_options import validate_options
    opts = validate_options(options, DATASETS)
    dataset = opts["dataset"]
    basis = opts.get("basis", "ActualCost")
    currency = opts.get("currency", "USD")
    calc = Ledger()
    source_period = (ev or {}).get("costPeriod") or {}
    start = opts.get("start") or str(source_period.get("fromUtc") or "")[:10]
    end = opts.get("end") or str(source_period.get("toUtc") or "")[:10]
    if dataset in ("resource", "tag") and (start != str(source_period.get("fromUtc") or "")[:10] or end != str(source_period.get("toUtc") or "")[:10]):
        raise ValueError("자원·태그 비용은 수집 전체기간 합계입니다. 부분기간 조회에는 일별 상세 비용 수집이 필요합니다.")
    src = section(ev, DATASETS[dataset]["section"])
    rows = normalize(ev, topology, dataset)
    if dataset == "tag":
        tag_keys = sorted({r["tag_key"] for r in rows if r["tag_key"] is not None})
        tag_key = opts.get("tag_key") or (tag_keys[0] if tag_keys else None)
        rows = [r for r in rows if r["tag_key"] == tag_key]
    else:
        tag_key = opts.get("tag_key")
    required_date = "month" if dataset in ("monthly", "service", "resource_group", "pricing") else "date" if dataset == "ai_daily" else None
    uncertain = [r for r in rows if not r["currency"] or not r["basis"] or required_date and not r.get(required_date)]
    rows = [r for r in rows if r not in uncertain]
    filters = opts.get("filters") or {}
    unsupported = set(filters) - set(DATASETS[dataset]["dimensions"])
    if unsupported:
        raise ValueError(f"{dataset} 집계에 없는 교차 차원: {', '.join(sorted(unsupported))}")
    period_rows = []
    for row in rows:
        rs, re = row_period(row, start, end)
        if start and end and (re < start or rs > end):
            continue
        if row.get("month") and start and end and (rs < start or re > end):
            raise ValueError("월별 집계는 월 중간 기간으로 나누지 않습니다. 일별 비용 수집이 필요합니다.")
        period_rows.append(row)
    rows = period_rows
    rows = [r for r in rows if r["basis"] == basis and r["currency"] == currency
            and matches_filters(r, filters)]
    keys = [calc.input(r["id"], r["resource_name"] or r["month"] or "비용 행", r["amount"], currency, r["source"]) for r in rows]
    source_status = "collection_failed" if src.get("ok") is False else src.get("status", "missing")
    known_keys = [r["id"] for r in rows if r["amount"] is not None]
    if known_keys:
        calc.formula("known_subtotal", "금액 확인된 행의 부분합", "sum", known_keys, currency)
    else:
        calc.input("known_subtotal", "금액 확인된 행 없음", None, currency, source_status)
    total_status = source_status
    if source_status in ("collected", "partial"):
        total_status = "partial" if source_status == "partial" or uncertain or len(known_keys) != len(keys) else ("known" if keys else "no_records")
    if keys and total_status == "known":
        calc.formula("total", "선택 범위 비용 합계", "sum", keys, currency)
    else:
        calc.input("total", "선택 범위 비용 합계", None, currency, src.get("reason", "비용 행 없음"))
    default_group = {"monthly": "month", "service": "service", "resource": "service", "resource_group": "resource_group", "tag": "tag_value", "pricing": "pricing_model", "ai_daily": "meter"}
    group_by = opts.get("group_by") or default_group[dataset]
    groups = group_rows(calc, rows, group_by, currency)
    actions, comparison = compare_scenario(calc, rows, opts, currency, basis, total_status, digest(ev))
    context = {"dataset": dataset, "basis": basis, "currency": currency, "period": {"start": start, "end": end}, "grain": DATASETS[dataset]["grain"], "group_by": group_by, "tag_key": tag_key}
    fx = opts.get("fx") or {}
    fx_value = number(fx.get("rate"))
    display_fx = {"value": None, "status": "missing_fx", "unit": fx.get("target_currency"), "source": "환율·기준일·출처 미확인"}
    if fx_value is not None and fx_value > 0 and fx.get("source") and fx.get("date") and fx.get("target_currency"):
        date.fromisoformat(fx["date"])
        if fx.get("source_currency", currency) != currency:
            raise ValueError("환율의 기준 통화가 선택한 청구 통화와 다릅니다.")
        calc.input("fx_rate", "사용자 지정 표시 환율", fx_value, f"{fx['target_currency']}/{currency}", str(fx["source"]) + " · " + fx["date"])
        calc.formula("display_fx", "표시용 환산 비용(청구 금액 아님)", "mul", ["total", "fx_rate"], fx["target_currency"])
        display_fx = {**calc.metric("display_fx"), "date": fx["date"], "rate": float(fx_value), "rate_source": fx["source"], "classification": "user_supplied_fx"}
    ai = ai_observations(calc, ev, start, end, currency)
    catalogue = price_catalogue(ev)
    apply_token_prices(calc, ai, catalogue, opts, currency)
    new_result = new_estimate(calc, ev, opts, currency, questions, answers, catalogue) if track == "new" else None
    projection = projection_result(calc, opts, start, end, comparison, currency)
    allocation = allocation_result(calc, rows, dataset, opts.get("allocation_key"), currency)
    budgets = budget_observations(calc, ev)
    inputs = {"evidence_sha256": digest(ev), "topology_sha256": digest(topology), "answers_sha256": digest(answers),
              "collector_version": (ev or {}).get("collectorVersion"), "collected_at": (ev or {}).get("completedAtUtc"), "source_period": (ev or {}).get("costPeriod", {})}
    checks = [{"code": "single_currency", "passed": len({r['currency'] for r in rows}) <= 1},
              {"code": "single_basis", "passed": len({r['basis'] for r in rows}) <= 1},
              {"code": "groups_reconcile", "passed": all(g["value"] is not None for g in groups) and calc.values["total"] is not None
               and abs(sum(Decimal(str(g["value"])) for g in groups) - calc.values["total"]) < Decimal("0.0000001") if groups else True}]
    return {"schema": SCHEMA, "core_version": CORE_VERSION, "track": track, "analysis_id": digest([CORE_VERSION, CORE_SOURCE_SHA256, ev, opts, topology, answers, list(questions), calc.nodes]), "core_source_sha256": CORE_SOURCE_SHA256,
            "quality": {"uncertain_rows": uncertain, "uncertain_count": len(uncertain), "complete_total": total_status == "known"}, "inputs": inputs, "ai": ai, "new": new_result, "prices": catalogue, "projection": projection, "allocation": allocation, "budgets": budgets, "datasets": dataset_info(ev, topology, currency), "candidates": advisor_candidates(ev, rows), "checks": checks, "display_fx": display_fx,
            "forecast": {"status": "missing", "reason": "Cost Management Forecast 결과가 수집되지 않았습니다. 같은 범위·통화의 예측 결과 수집이 필요합니다."},
            "anomalies": {"status": "missing", "reason": "공식 이상 지출 결과·전체 비용 일별 이력이 미수집입니다. 월간 증감과 이상 판정은 구분합니다."},
            "context": context, "options": opts, "summary": {"total": calc.metric("total", total_status), "known_subtotal": calc.metric("known_subtotal"), "row_count": len(rows), "unknown_amount_count": len(rows) - len(known_keys)}, "rows": rows, "groups": groups, "actions": actions, "comparison": comparison, "calculation": calc.nodes}
