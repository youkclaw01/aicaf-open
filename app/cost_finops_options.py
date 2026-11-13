"""비용 v2 저장 입력 계약. 잘못된 수치는 버리거나 0으로 바꾸지 않고 명시적으로 거절한다."""
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation
import re

ROOT_KEYS = {"dataset", "basis", "currency", "start", "end", "filters", "group_by", "tag_key", "allocation_key", "actions", "recurring_cost", "recurring_cost_evidence", "transition_cost", "transition_cost_evidence", "projection", "fx", "new", "ai_mappings", "candidate_states"}
NEW_KEYS = {"baseline_month", "monthly_requests", "input_tokens", "output_tokens", "monthly_work_hours", "hourly_cost", "automation_share", "build_cost", "support_cost", "support_cost_evidence", "price_in_ref", "price_out_ref", "work_days", "sensitivities", "label", "evidence"}
ACTION_KEYS = {"id", "name", "kind", "row_ids", "amount", "currency", "basis", "enabled", "scope_confirmed", "evidence", "evidence_snapshot", "phase", "owner", "constraint"}
MAPPING_KEYS = {"account", "deployment", "price_in_ref", "price_out_ref", "target_in_ref", "target_out_ref", "target_confirmed", "evidence"}
PHASES = {"candidate", "evaluated", "planned", "applied", "verified", "blocked", "dismissed"}


def obj(value, label, allowed):
    if not isinstance(value, dict):
        raise ValueError(label + " 입력은 객체여야 합니다.")
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(label + "에 정의되지 않은 항목: " + ", ".join(sorted(unknown)))


def text(value, label, limit=2048):
    if value is not None and (not isinstance(value, str) or len(value) > limit):
        raise ValueError(label + " 문자의 유형·길이가 유효하지 않습니다.")


def numeric(value, label, maximum=10**18):
    if value is None:
        return
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            raise ValueError(label + "은 숫자여야 합니다.")
        n = Decimal(str(value))
        if not n.is_finite() or not 0 <= n <= maximum:
            raise ValueError(label + "은 유한한 비음수여야 합니다.")
    except InvalidOperation as e:
        raise ValueError(label + " 숫자가 유효하지 않습니다.") from e


def boolean(value, label):
    if value is not None and not isinstance(value, bool):
        raise ValueError(label + "은 불리언이어야 합니다.")


def validate_new(value, allow_sensitivity=True):
    obj(value, "신규 비용", NEW_KEYS)
    nums = {"monthly_requests", "input_tokens", "output_tokens", "monthly_work_hours", "hourly_cost", "automation_share", "build_cost", "support_cost", "work_days"}
    for k, v in value.items():
        if k in nums:
            numeric(v, k, 1 if k == "automation_share" else 31 if k == "work_days" else 10**18)
        elif k != "sensitivities":
            text(v, k)
    if value.get("baseline_month") and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value["baseline_month"]):
        raise ValueError("신규 기준월 형식은 YYYY-MM입니다.")
    variants = value.get("sensitivities") or []
    if not isinstance(variants, list) or len(variants) > 5 or variants and not allow_sensitivity:
        raise ValueError("민감도는 최대 5개 독립 입력으로 구성합니다.")
    for item in variants:
        validate_new(item, False)


def validate_options(options, datasets):
    value = deepcopy({} if options is None else options)
    obj(value, "비용 분석", ROOT_KEYS)
    dataset = value.get("dataset", "monthly")
    if dataset not in datasets:
        raise ValueError("수집 계약에 없는 비용 원장입니다.")
    value["dataset"] = dataset
    cfg = datasets[dataset]
    value.setdefault("basis", cfg["basis"][0])
    if value["basis"] not in cfg["basis"]:
        raise ValueError("선택 원장에 해당 청구 기준이 수집되지 않았습니다.")
    if value.get("currency") is not None and not re.fullmatch(r"[A-Z]{3}", value["currency"]):
        raise ValueError("통화는 수집 원본의 3자리 통화코드여야 합니다.")
    for k in ("start", "end"):
        if value.get(k):
            text(value[k], k, 10)
            date.fromisoformat(value[k])
    if value.get("start") and value.get("end") and value["start"] > value["end"]:
        raise ValueError("조회 시작일이 종료일보다 늦습니다.")
    filters = value.get("filters") or {}
    obj(filters, "필터 차원", set(cfg["dimensions"]))
    for k, v in filters.items():
        if k == "tag" and isinstance(v, dict):
            obj(v, "태그 필터", {"key", "value"})
            for field in v.values():
                text(field, "태그 값", 512)
        else:
            text(v, "필터 " + k, 512)
    if value.get("group_by") and value["group_by"] not in set(cfg["dimensions"]) - {"tag"}:
        raise ValueError("선택 집계에 없는 그룹 차원입니다.")
    for k in ("tag_key", "allocation_key", "recurring_cost_evidence", "transition_cost_evidence"):
        text(value.get(k), k)
    for k in ("recurring_cost", "transition_cost"):
        numeric(value.get(k), k)
    projection = value.get("projection") or {}
    obj(projection, "기간 환산", {"enabled", "evidence", "year", "horizon_months"})
    boolean(projection.get("enabled"), "기간 환산 확인")
    text(projection.get("evidence"), "기간 환산 근거")
    for k, minimum, maximum in (("horizon_months", 1, 120), ("year", 1900, 2200)):
        if k in projection:
            x = projection[k]
            if isinstance(x, bool) or not isinstance(x, int) or not minimum <= x <= maximum:
                raise ValueError("기간 환산 " + k + " 범위가 유효하지 않습니다.")
    fx = value.get("fx") or {}
    obj(fx, "환율", {"source_currency", "target_currency", "rate", "date", "source"})
    numeric(fx.get("rate"), "환율")
    for k in ("source_currency", "target_currency", "date", "source"):
        text(fx.get(k), "환율 " + k)
    validate_new(value.get("new") or {})
    actions = value.get("actions") or []
    if not isinstance(actions, list) or len(actions) > 50:
        raise ValueError("개선 조치는 최대 50개입니다.")
    ids = set()
    for action in actions:
        obj(action, "조치", ACTION_KEYS)
        for k in ("id", "name", "kind", "currency", "basis", "evidence", "evidence_snapshot", "phase", "owner", "constraint"):
            text(action.get(k), "조치 " + k)
        if action.get("id") in ids:
            raise ValueError("조치 ID가 중복되었습니다.")
        ids.add(action.get("id"))
        if action.get("phase", "candidate") not in PHASES:
            raise ValueError("조치 진행 상태가 유효하지 않습니다.")
        numeric(action.get("amount"), "조치 확인 금액")
        boolean(action.get("enabled"), "조치 사용")
        boolean(action.get("scope_confirmed"), "조치 범위 확인")
        row_ids = action.get("row_ids") or []
        if not isinstance(row_ids, list) or len(row_ids) > 1000 or any(not isinstance(r, str) or len(r) > 80 for r in row_ids):
            raise ValueError("조치 대상 원장 행이 유효하지 않습니다.")
    mappings = value.get("ai_mappings") or []
    if not isinstance(mappings, list) or len(mappings) > 50:
        raise ValueError("토큰 단가 연결은 최대 50개입니다.")
    scopes = set()
    for mapping in mappings:
        obj(mapping, "토큰 단가 연결", MAPPING_KEYS)
        for k, v in mapping.items():
            boolean(v, k) if k == "target_confirmed" else text(v, k)
        scope = (str(mapping.get("account") or "").lower(), mapping.get("deployment"))
        if scope in scopes:
            raise ValueError("같은 배포의 단가 연결이 중복되었습니다.")
        scopes.add(scope)
    states = value.get("candidate_states") or {}
    if not isinstance(states, dict) or len(states) > 200:
        raise ValueError("후보 진행 상태가 유효하지 않습니다.")
    for candidate_id, phase in states.items():
        text(candidate_id, "후보 ID", 512)
        if phase not in PHASES:
            raise ValueError("후보 진행 상태가 유효하지 않습니다.")
    return value
