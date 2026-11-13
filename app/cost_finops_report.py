"""동일 계산 원장을 Excel 수식·고객용 문서로 변환하는 비용 보고서 생성기.

비용 계산은 하지 않는다. 입력은 계산 코어가 확정한 값·연산·참조이며,
Excel은 실제 LibreOffice 재계산값을 대조한 뒤 검증된 캐시만 이식한다.
"""
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
import math
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
import zipfile

from lxml import etree
import openpyxl
import xlsxwriter

FONT = "Malgun Gothic"
PRIMARY = "326B91"
NS_X = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
SUMMARY_METRICS = [
    ("선택 범위 비용 합계", "summary.total"),
    ("금액 확인된 행의 부분합", "summary.known_subtotal"),
    ("AS-IS 기준 비용", "comparison.as_is"),
    ("TO-BE 예상 / 확정 조치 전 기준선", "comparison.to_be"),
    ("추가 운영비 포함 순절감", "comparison.savings"),
    ("동일 기준 순절감률", "comparison.savings_rate"),
    ("중복 제거 총절감", "comparison.gross_savings"),
    ("확인된 추가 운영비", "comparison.recurring_cost"),
    ("같은 일평균 가정의 월 환산 순절감", "projection.monthly_savings"),
    ("같은 일평균 가정의 연간 환산 순절감", "projection.annual_savings"),
    ("전환비 회수기간", "projection.payback_months"),
    ("출처가 있는 표시 환산 비용", "display_fx"),
    ("신규 현재 유지 월비용", "new.maintain_cost"),
    ("신규 AI 도입 월비용", "new.adoption_cost"),
    ("신규 월 순효과", "new.net_benefit"),
    ("동일 신규 가정의 연간 순효과", "new.annual_net_benefit"),
    ("신규 도입 회수기간", "new.roi_months"),
]


def _get(result, path):
    value = result
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value or {}


def _validate_ir(result):
    """연산 계약을 검사하되 수치를 다시 계산하지 않는다."""
    nodes = result.get("calculation")
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("calculation ledger required")
    found = {}
    for node in nodes:
        key = node.get("id")
        if not isinstance(key, str) or not key or key in found:
            raise ValueError("duplicate or invalid calculation id")
        value = node.get("value")
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
            raise ValueError("non-finite calculation value: " + key)
        if node.get("kind") == "formula":
            expr = node.get("expression") or {}
            op, args = expr.get("op"), expr.get("args")
            if op not in {"sum", "sub", "mul", "div", "min", "max"} or not isinstance(args, list):
                raise ValueError("unsupported calculation expression: " + key)
            if any(arg not in found for arg in args):
                raise ValueError("unresolved or forward calculation reference: " + key)
            if op in {"sub", "mul", "div"} and len(args) != 2 or op in {"min", "max"} and not args:
                raise ValueError("invalid calculation arity: " + key)
        elif node.get("kind") != "input" or node.get("expression") is not None:
            raise ValueError("invalid calculation kind: " + key)
        found[key] = node

    def validate_metrics(value):
        if isinstance(value, dict):
            if "node" in value and "value" in value:
                key = value["node"]
                if key not in found:
                    raise ValueError("unknown metric calculation reference: " + str(key))
                actual, expected = value["value"], found[key]["value"]
                same = actual is None if expected is None else isinstance(actual, (int, float)) and not isinstance(actual, bool) and math.isfinite(actual) and math.isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-8)
                if not same or value.get("unit") and value["unit"] != found[key].get("unit"):
                    raise ValueError("metric value or unit mismatch: " + key)
            for key, child in value.items():
                if key not in {"raw", "calculation", "options", "inputs", "input_files", "datasets", "ptu", "selected_prices"}:
                    validate_metrics(child)
        elif isinstance(value, list):
            for item in value:
                validate_metrics(item)

    validate_metrics(result)
    for row in result.get("rows") or []:
        if row.get("id") not in found or row.get("amount") != found[row["id"]]["value"]:
            raise ValueError("normalized row and IR input mismatch")
    return found


def _compact_refs(cells):
    """동일 열의 연속 참조만 범위로 줄이며 인수 순서·중복은 보존한다."""
    result, i = [], 0
    while i < len(cells):
        match = re.fullmatch(r"(.*!\$([A-Z]+)\$)(\d+)", cells[i])
        j = i + 1
        if match:
            prefix, col, start = match.group(1), match.group(2), int(match.group(3))
            while j < len(cells) and cells[j] == prefix + str(start + j - i):
                j += 1
        if match and j > i + 1:
            result.append(cells[i] + f":${col}${start + j - i - 1}")
        else:
            result.append(cells[i])
        i = j
    return result


def _aggregate(function, refs):
    """Excel 함수의 255인수 제한을 피하는 동치의 참조 묶음이다."""
    if len(refs) <= 80:
        return function + "(" + ",".join(refs) + ")"
    groups = [_aggregate(function, refs[i:i + 80]) for i in range(0, len(refs), 80)]
    return _aggregate("SUM" if function == "COUNT" else function, groups)


def _ir_formula(expression, refs):
    """IR의 기본 연산과 결측 전파를 셀 참조 수식으로 직역한다."""
    op, args = expression["op"], expression["args"]
    if not args:
        return "=0"  # 계산 원장에 명시된 공집합 합계만 0이다.
    cells = [refs[arg] for arg in args]
    compact = _compact_refs(cells)
    operation = {"sum": "SUM", "min": "MIN", "max": "MAX"}.get(op)
    if operation:
        body = _aggregate(operation, compact)
    else:
        body = cells[0] + {"sub": "-", "mul": "*", "div": "/"}[op] + cells[1]
    condition = f"{_aggregate('COUNT', compact)}={len(cells)}"
    if op == "div":
        condition = f"AND({condition},{cells[1]}<>0)"
    formula = f'=IF({condition},{body},"")'
    if len(formula) > 8192:
        raise ValueError("calculation expression exceeds Excel formula capacity")
    return formula


def _link_formula(ref):
    # 직접 =빈셀 참조는 Excel에서 0이 되므로 수집 여부를 먼저 확인한다.
    return f'=IF(COUNT({ref})=1,{ref},"")'


def _sheet_paths(archive):
    """시트 순서가 아니라 workbook 관계와 이름으로 ZIP 파트를 결합한다."""
    wb = etree.fromstring(archive.read("xl/workbook.xml"))
    rels = etree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {rel.get("Id"): rel.get("Target") for rel in rels}
    output = {}
    for sheet in wb.findall(f"{{{NS_X}}}sheets/{{{NS_X}}}sheet"):
        target = targets[sheet.get(f"{{{NS_R}}}id")]
        output[sheet.get("name")] = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
    return output


def _graft_formula_caches(original, calculated):
    """템플릿 스타일·수식·보호는 보존하고 실제 계산된 값만 이식한다."""
    with zipfile.ZipFile(io.BytesIO(original)) as src, zipfile.ZipFile(io.BytesIO(calculated)) as donor:
        src_paths, donor_paths = _sheet_paths(src), _sheet_paths(donor)
        strings = []
        if "xl/sharedStrings.xml" in donor.namelist():
            root = etree.fromstring(donor.read("xl/sharedStrings.xml"))
            strings = ["".join(si.itertext()) for si in root]
        changed = {}
        for name, part in src_paths.items():
            target = etree.fromstring(src.read(part))
            donor_root = etree.fromstring(donor.read(donor_paths[name]))
            donor_cells = {c.get("r"): c for c in donor_root.iter(f"{{{NS_X}}}c")}
            for cell in target.iter(f"{{{NS_X}}}c"):
                if cell.find(f"{{{NS_X}}}f") is None:
                    continue
                actual = donor_cells.get(cell.get("r"))
                if actual is None or actual.get("t") == "e":
                    raise RuntimeError("formula recalculation error: " + name + "!" + cell.get("r"))
                value = actual.find(f"{{{NS_X}}}v")
                if value is None:
                    raise RuntimeError("missing formula cache: " + name + "!" + cell.get("r"))
                value = deepcopy(value)
                kind = actual.get("t")
                if kind == "s":
                    value.text, kind = strings[int(value.text)], "str"
                for old in list(cell):
                    if old.tag in {f"{{{NS_X}}}v", f"{{{NS_X}}}is"}:
                        cell.remove(old)
                cell.attrib.pop("t", None)
                if kind:
                    cell.set("t", kind)
                cell.append(value)
            changed[part] = etree.tostring(target, encoding="UTF-8", xml_declaration=True, standalone=True)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
            for info in src.infolist():
                out.writestr(info, changed.get(info.filename, src.read(info.filename)))
        return buf.getvalue()


def _verify_cached(data, nodes):
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    ws = wb["계산원장"]
    for row, node in enumerate(nodes, 5):
        actual, expected = ws.cell(row, 3).value, node["value"]
        if expected is None:
            if actual not in (None, ""):
                raise RuntimeError("null calculation cache mismatch: " + node["id"])
        elif not isinstance(actual, (int, float)) or isinstance(actual, bool) or not math.isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-8):
            raise RuntimeError("calculation cache mismatch: " + node["id"])
    for sheet in wb:
        for row in sheet:
            for cell in row:
                if cell.data_type == "e":
                    raise RuntimeError("formula error: " + sheet.title + "!" + cell.coordinate)
    wb.close()


def _soffice_executable():
    """명시 설정을 우선하고 PATH·Windows 설치 경로·macOS에서 실행 파일을 찾는다."""
    def available(path):
        return path.is_file() and (os.name == "nt" or os.access(path, os.X_OK))

    configured = os.environ.get("AICAF_SOFFICE", "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if not available(candidate):
            raise RuntimeError("configured LibreOffice executable unavailable: " + configured)
        return str(candidate)
    executable = shutil.which("soffice")
    if executable:
        return executable
    candidates = [Path(os.environ[name]) / "LibreOffice" / "program" / "soffice.exe"
                  for name in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)") if os.environ.get(name)]
    candidates.extend([Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"),
                       Path("C:/Program Files/LibreOffice/program/soffice.exe"),
                       Path("C:/Program Files (x86)/LibreOffice/program/soffice.exe")])
    for candidate in candidates:
        if available(candidate):
            return str(candidate)
    raise RuntimeError("LibreOffice required for verified XLSX generation; set AICAF_SOFFICE or install the approved runtime")


def _recalculate(data, nodes):
    with tempfile.TemporaryDirectory(prefix="cost-report-") as directory:
        root = Path(directory)
        source = root / "source.xlsx"
        source.write_bytes(data)
        output = root / "calculated"
        output.mkdir()
        if os.name == "nt" and not os.environ.get("AICAF_SOFFICE", "").strip():
            from asis_office import convert_isolated
            target = convert_isolated(source, output, "xlsx", timeout=180)
        else:
            exe = _soffice_executable()
            cmd = [exe, "-env:UserInstallation=" + (root / "profile").as_uri(), "--headless", "--convert-to", "xlsx", "--outdir", str(output), str(source)]
            try:
                run = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("LibreOffice recalculation timeout") from exc
            except OSError as exc:
                raise RuntimeError("LibreOffice executable failed to start") from exc
            target = output / source.name
            if run.returncode or not target.is_file():
                raise RuntimeError("LibreOffice recalculation failed: " + (run.stderr or run.stdout).strip())
        donor = target.read_bytes()
        _verify_cached(donor, nodes)
        result = _graft_formula_caches(data, donor)
        _verify_cached(result, nodes)
        return result


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _flatten(value, path=""):
    """긴 옵션 JSON을 원문 손실 없이 개별 입력 경로로 기록한다."""
    if isinstance(value, dict) and value:
        for key, child in value.items():
            yield from _flatten(child, path + "." + key if path else key)
    elif isinstance(value, list) and value:
        for i, child in enumerate(value):
            yield from _flatten(child, f"{path}[{i}]")
    else:
        yield path, _json(value) if isinstance(value, (dict, list)) else value


def _assumption_nodes(result, nodes):
    """관측값·단가·상수는 잠그고 승인된 수치 가정만 편집 대상으로 지정한다."""
    selected = {}
    opts = result.get("options") or {}
    for i, action in enumerate(result.get("actions") or []):
        key = (action.get("requested") or {}).get("node")
        if key in nodes:
            selected[key] = (f"actions[{i}].amount", action.get("status") == "estimated")
    for key in ("recurring_cost", "transition_cost"):
        if key in nodes:
            confirmed = bool(str(opts.get(key + "_evidence") or "").strip())
            selected[key] = (key, confirmed)
    if "fx_rate" in nodes:
        selected["fx_rate"] = ("fx.rate", True)
    new_keys = {"hourly_cost", "monthly_requests", "input_tokens", "output_tokens", "automation_share", "build_cost", "monthly_work_hours", "work_days", "support_cost"}
    for key, node in nodes.items():
        if node["kind"] != "input" or not key.startswith("new_"):
            continue
        field = next((field for field in new_keys if key.endswith("_" + field)), None)
        if field:
            supported = field != "support_cost" or "미확인" not in str(node.get("source", ""))
            selected[key] = ("new." + key.removeprefix("new_"), supported)
    return selected


def _assumption_maximum(node):
    """저장 입력 계약과 동일한 상한을 수식·입력 UI에 함께 적용한다."""
    return 1 if node.get("unit") == "비율" else 31 if node["id"].endswith("_work_days") else 10**18


def _input_formula(ref, node):
    """타입 확인 후에만 범위를 평가하며 붙여넣기 오류는 미산정으로 전파한다."""
    bounds = f"AND({ref}>=0,{ref}<={_assumption_maximum(node)})"
    return f'=IF(ISNUMBER({ref}),IF(ISLOGICAL({ref}),"",IF({bounds},{ref},"")),"")'


def _add_trace_sheets(wb, result, customer, sheet, text, number, refs, nodes):
    """금액을 추가 계산하지 않고 원본·가정·상태·출처를 원장 참조에 연결한다."""
    percent = wb.add_format({"font_name": FONT, "font_size": 10, "num_format": "0.0%", "valign": "top"})
    # Calc는 숫자 형식 셀의 boolean을 1/0으로 바꾼다. 편집 셀은 논리 타입을 보존한다.
    # 금액·비율의 표시 형식은 연결된 계산원장·요약 셀에서 유지한다.
    edit_number = wb.add_format({"font_name": FONT, "font_size": 10, "font_color": "0000FF", "bg_color": "FFF2CC", "locked": False, "num_format": "General"})
    edit_percent = edit_number
    input_refs = {}

    def scalar(ws, row, col, value, fmt=None):
        if isinstance(value, (dict, list)):
            value = _json(value)
        if isinstance(value, str) and len(value) > 32767:
            raise ValueError("source text exceeds Excel cell capacity")
        ws.write(row, col, value, fmt or (number if isinstance(value, (int, float)) and not isinstance(value, bool) else text))

    def record(ws, row, values):
        for col, value in enumerate(values):
            scalar(ws, row, col, value)

    def metric(ws, row, col, value):
        value = value or {}
        key = value.get("node")
        fmt = percent if value.get("unit") == "비율" else number
        if key:
            if key not in nodes:
                raise ValueError("unknown metric calculation reference: " + key)
            ws.write_formula(row, col, _link_formula(refs[key]), fmt, value.get("value") if value.get("value") is not None else "")
        else:
            ws.write_blank(row, col, None, fmt)

    ctx = result.get("context") or {}
    period = ctx.get("period") or {}
    inputs = result.get("inputs") or {}
    contract = sheet("실행계약", "기간·통화·청구 기준과 입력 출처를 고정한 분석 실행", ["항목", "값", "적용 기준"])
    contract.set_column(0, 0, 32)
    contract.set_column(1, 1, 80)
    contract.set_column(2, 2, 70)
    meta = [
        ("고객", customer, "AI Assessment 시스템"),
        ("분석 실행 ID", result.get("analysis_id"), "같은 입력·옵션·계산 버전의 스냅샷"),
        ("계산 버전", result.get("core_version"), "동일 산정 기준"),
        ("계산 소스 SHA-256", result.get("core_source_sha256"), "산출 수식의 근거 버전"),
        ("입력 구분", result.get("data_classification") or "입력 원장 기반", "가상 시험 입력과 실제 수집을 구분"),
        ("현재/신규", "신규 도입" if result.get("track") == "new" else "기존 운영", "신규 도입은 소매가격·업무량 시나리오"),
        ("선택 원장", (result.get("datasets", {}).get(ctx.get("dataset"), {}) or {}).get("label", ctx.get("dataset")), ctx.get("grain")),
        ("청구 기준", ctx.get("basis"), "ActualCost와 AmortizedCost는 서로 합산하지 않음"),
        ("통화", ctx.get("currency"), "소매가격·다른 통화와 합산하지 않음"),
        ("기간 시작", period.get("start"), "선택한 집계 grain의 관측기간"),
        ("기간 종료", period.get("end"), "MTD·부분기간의 관측 한계 유지"),
        ("필터", (result.get("options") or {}).get("filters") or {}, "지원되는 원장 차원만 적용"),
        ("세금·credit·청구 할인", "별도 내역 미수집", "포함 여부·청구서 정산 일치 여부를 주장하지 않음"),
        ("현시점 태그", "수집시점 현재 태그", "과거 청구시점 태그·공유비용 배분 정책 아님"),
        ("가정 편집", "가정값 시트의 노란 입력만 편집", "원본·단가·기간·단위·수식은 보호됨. 새 근거/범위는 시스템에서 재생성"),
        ("오프라인 변경", "기존 분석 실행의 탐색 시나리오", "검증 시트의 변경/불일치는 원래 분석 원장과의 차이. PPT/시스템 값은 자동 변경되지 않음"),
        ("재계산", "실제 재계산값과 원장 값 대조", "미산정은 빈값으로 유지. 수식 오류가 있으면 생성 실패"),
    ]
    meta.extend(("입력 " + key, value, "정규화 입력의 해시·수집정보") for key, value in inputs.items())
    input_file_names = {"aicaf-evidence.json": "증거 입력", "topology.json": "구성 입력"}
    for path, value in _flatten(result.get("input_files") or {}):
        file_name, _, field = path.rpartition(".")
        label = input_file_names.get(file_name, "수집 입력")
        meta.append((label + " " + field, value, "수집 원파일의 SHA-256·메타데이터"))
    for row, values in enumerate(meta, 4):
        record(contract, row, values)

    raw = sheet("원본비용", "선택 범위 원문 비용을 변경 없이 보존한다", ["원장행 ID", "원본 근거", "원문 Cost", "원문 Currency", "원문 ResourceId", "원문 JSON", "확정 숫자 입력"])
    raw.set_column(1, 1, 40)
    raw.set_column(4, 5, 66)
    normalized = sheet("정규화비용", "금액은 단일 원장에 연결하고 현재 인벤토리 차원의 출처를 표시한다", ["원장행 ID", "금액", "통화", "청구 기준", "기간 시작", "기간 종료", "구독", "리소스 그룹", "서비스", "자원", "지역", "SKU", "미터", "날짜", "월", "태그 키", "청구 태그 값", "현재 태그", "차원 출처", "원문 근거", "원문 행번호", "원문 JSON"])
    normalized.set_column(9, 9, 60)
    normalized.set_column(17, 21, 50)
    for row, item in enumerate(result.get("rows") or [], 4):
        key, original = item["id"], item.get("raw") or {}
        record(raw, row, [key, item.get("source"), original.get("Cost"), original.get("Currency"), original.get("ResourceId"), original, nodes[key]["value"]])
        input_refs[key] = _link_formula(f"'원본비용'!$G${row + 1}")
        record(normalized, row, [key, None, item.get("currency"), item.get("basis"), period.get("start"), period.get("end"), item.get("subscription"), item.get("resource_group"), item.get("service"), item.get("resource"), item.get("region"), item.get("sku"), item.get("meter"), item.get("date"), item.get("month"), item.get("tag_key"), item.get("tag_value"), item.get("tags"), item.get("dimension_sources"), item.get("source"), item.get("source_index"), original])
        metric(normalized, row, 1, {"node": key, "value": nodes[key]["value"], "unit": nodes[key].get("unit")})

    catalogue = sheet("단가표", "소매 단가·원문 단위·적용 조건은 실제 청구비용과 별도로 추적한다", ["단가 참조", "미터", "SKU", "제품", "소매 단가", "통화", "원문 단위", "지역", "과금 유형", "최저 구간", "효력 시작", "단위 토큰 수", "방향", "예약 기간", "원문 근거", "원문 JSON"])
    catalogue.set_column(1, 3, 40)
    catalogue.set_column(14, 15, 64)
    price_sources = {}
    for row, item in enumerate(result.get("prices") or [], 4):
        amount = item.get("price", item.get("amount"))
        denominator = item.get("token_denominator", item.get("denominator"))
        record(catalogue, row, [item.get("ref"), item.get("meter"), item.get("sku"), item.get("product"), amount, item.get("currency"), item.get("unit"), item.get("region"), item.get("type"), item.get("tier_minimum"), item.get("effective_date"), denominator, item.get("direction"), item.get("reservation_term"), item.get("source"), item.get("raw")])
        price_sources[item.get("source")] = (row, amount, denominator)
    for key, node in nodes.items():
        p = price_sources.get(node.get("source"))
        if p and node["kind"] == "input" and (key.endswith("_rate") or key.endswith("_price") or key.endswith("_denominator")):
            col, value = ("L", p[2]) if key.endswith("_denominator") else ("E", p[1])
            if node["value"] != value:
                raise ValueError("selected price and IR input mismatch: " + key)
            input_refs[key] = _link_formula(f"'단가표'!${col}${p[0] + 1}")

    assumptions = sheet("가정값", "노란 숫자만 변경할 수 있으며 원문·근거·범위의 승인은 대체하지 않는다", ["가정 항목", "값", "단위", "입력 근거", "분석시점 값", "계산 참조", "옵션 경로", "편집 조건"])
    assumptions.set_column(0, 0, 36)
    assumptions.set_column(3, 3, 64)
    for row, (key, (path, enabled)) in enumerate(_assumption_nodes(result, nodes).items(), 4):
        node = nodes[key]
        record(assumptions, row, [node.get("label"), None, node.get("unit"), node.get("source"), node["value"], key, path, "오프라인 가정·비음수 유한 숫자" if enabled else "근거 미확인·편집 잠금"])
        fmt = edit_percent if node.get("unit") == "비율" else edit_number
        scalar(assumptions, row, 1, node["value"], fmt if enabled else number)
        if enabled:
            maximum = _assumption_maximum(node)
            assumptions.data_validation(row, 1, row, 1, {"validate": "decimal", "criteria": "between", "minimum": 0, "maximum": maximum, "ignore_blank": True, "error_type": "stop", "error_title": "가정 입력 오류", "error_message": "비음수 숫자와 항목별 허용 범위를 확인하십시오."})
            if node.get("unit") == "비율":
                assumptions.write_comment(row, 1, "비율은 0~1 숫자로 입력합니다. 표기 예: 0.5는 50%이며, 계산원장·요약에는 백분율로 표시됩니다.")
            input_refs[key] = _input_formula(f"'가정값'!$B${row + 1}", node)
    if not _assumption_nodes(result, nodes):
        record(assumptions, 4, ["편집 가능한 확인 가정 없음", None, None, "실측 원본을 가정값으로 변경하지 않습니다."])
    options = sheet("입력계약", "분석 필터·근거·조치 범위는 스냅샷으로 보존한다", ["입력 경로", "원본 값", "적용 기준"])
    options.set_column(0, 0, 48)
    options.set_column(1, 1, 86)
    for row, (path, value) in enumerate(_flatten(result.get("options") or {}), 4):
        record(options, row, [path, value, "기간·근거·단가 연결·조치 추가는 시스템에서 새 분석으로 생성"])

    composition = sheet("비용구성", "선택한 차원의 금액은 동일 계산원장에서 가져온다", ["그룹", "금액", "단위", "상태", "차원", "행 수", "계산 참조"])
    for row, group in enumerate(result.get("groups") or [], 4):
        record(composition, row, [group.get("label"), None, group.get("unit"), group.get("status"), group.get("dimension"), group.get("count"), group.get("node")])
        metric(composition, row, 1, group)
    allocation = result.get("allocation") or {}
    allocated = sheet("현재태그배분", "현재 태그값 배분은 역사적 청구 태그·공유비용 배분 정책이 아니다", ["배분 대상", "금액 / 비율", "단위", "상태", "태그 키", "의미·제약", "계산 참조"])
    allocation_rows = [("태그값 확인된 비용", allocation.get("allocated")), ("미배분·태그 확인 불가", allocation.get("unallocated")), ("동일 원장 총비용", allocation.get("total")), ("현재 태그 금액 커버리지", allocation.get("coverage"))]
    allocation_rows.extend((g.get("label"), g) for g in allocation.get("groups") or [])
    for row, (label, value) in enumerate(allocation_rows, 4):
        value = value or {}
        record(allocated, row, [label, None, value.get("unit"), value.get("status", allocation.get("status")), allocation.get("key"), allocation.get("reason"), value.get("node")])
        metric(allocated, row, 1, value)

    usage = sheet("사용량", "관측 사용량·배포별 청구비용·소매가격 추정은 독립 기준이다", ["계정", "배포", "모델", "배포 유형", "관측 일수", "입력 토큰", "출력 토큰", "요청 수", "캐시 토큰", "요청당 입력", "현재 소매 추정", "대안 소매 추정", "소매 차액", "배포별 청구비용", "단가 연결 원문", "관측 상태"])
    for row, token in enumerate((result.get("ai") or {}).get("tokens") or [], 4):
        record(usage, row, [token.get("account"), token.get("deployment"), token.get("model"), token.get("deployment_type", token.get("sku")), token.get("observed_days"), None, None, None, None, None, None, None, None, None, token.get("selected_prices"), "미수집 메트릭은 빈셀"])
        for col, field in enumerate(("input_tokens", "output_tokens", "requests", "cached_tokens", "input_per_request", "retail_estimate", "retail_to_be", "retail_savings", "billing_cost"), 5):
            metric(usage, row, col, token.get(field))

    actions = sheet("조치원장", "범위별 잔액 상한·중복 제거·추가 비용을 계산 원장에 연결한다", ["조치 ID", "조치", "대상 원장행", "확인 요청액", "적용 절감", "중복·상한 제외", "진행 단계", "계산 상태", "통화", "청구 기준", "근거", "담당", "제약", "입력 스냅샷", "활성", "범위 확인"])
    for row, action in enumerate(result.get("actions") or [], 4):
        record(actions, row, [action.get("id"), action.get("name"), action.get("row_ids"), action.get("amount"), None, None, action.get("phase"), action.get("status"), action.get("currency"), action.get("basis"), action.get("evidence"), action.get("owner"), action.get("constraint"), action.get("evidence_snapshot"), action.get("enabled"), action.get("scope_confirmed")])
        if action.get("requested"):
            metric(actions, row, 3, action.get("requested"))
        metric(actions, row, 4, action.get("applied"))
        metric(actions, row, 5, action.get("overlap_removed"))
    if not result.get("actions"):
        record(actions, 4, ["확인된 조치 없음", "절감액은 미산정", None, None, None, None, "실현 절감 증거 없음"])

    budgets = sheet("예산", "예산은 원문 범위 안에서만 비교하며 전체 조회 비용과 혼합하지 않는다", ["예산", "설정 금액", "원문 현재 사용", "자체 사용 비율", "통화", "기간", "범위", "원문 근거", "제약", "원문 JSON"])
    for row, item in enumerate((result.get("budgets") or {}).get("rows") or [], 4):
        record(budgets, row, [item.get("name"), None, None, None, item.get("currency") or "통화 미수집·원문 단위", item.get("period"), item.get("scope"), item.get("source"), item.get("constraint"), item.get("raw")])
        for col, field in enumerate(("amount", "current_spend", "ratio"), 1):
            metric(budgets, row, col, item.get(field))
    candidates = sheet("후보과제", "Advisor 공급자 예상치는 확인 조치가 아니며 자동 합산하지 않는다", ["권고", "우선순위", "공급자 연간 예상", "통화", "원문 금액", "원문 단위", "진행 단계", "검토 상태", "범위", "제약", "근거", "TO-BE 포함", "원문 JSON"])
    for row, item in enumerate(result.get("candidates") or [], 4):
        phase = ((result.get("options") or {}).get("candidate_states") or {}).get(item.get("id"), item.get("phase"))
        record(candidates, row, [item.get("label"), item.get("impact"), item.get("annual_estimate"), item.get("currency"), item.get("amount_original"), item.get("amount_unit"), phase, item.get("status"), item.get("resource"), item.get("constraint"), item.get("source"), False, item.get("raw")])

    projection = result.get("projection") or {}
    timeline = sheet("기간환산", "동일 일평균 사용량을 반복한 가정이며 공식 예측·실적이 아니다", ["항목 / 경과월", "값", "단위", "상태", "근거·가정", "계산 참조"])
    for row, (label, key) in enumerate((("월 환산 순절감", "monthly_savings"), ("연간 환산 순절감", "annual_savings"), ("회수기간", "payback_months"), ("일회성 전환비", "transition_cost")), 4):
        value = projection.get(key) or {}
        record(timeline, row, [label, None, value.get("unit"), value.get("status"), projection.get("assumptions"), value.get("node")])
        metric(timeline, row, 1, value)
    for row, point in enumerate(projection.get("cumulative") or [], 9):
        record(timeline, row, [point.get("month"), None, point.get("unit"), point.get("status"), projection.get("evidence"), point.get("node")])
        metric(timeline, row, 1, point)
    new = result.get("new") or {}
    adoption = sheet("신규도입", "신규 도입은 선택 단가·업무량·인건비·운영비의 가정 시나리오이다", ["항목", "값", "단위", "상태", "계산 참조", "가정·분류"])
    row = 4
    for key, value in new.items():
        if isinstance(value, dict) and "value" in value:
            record(adoption, row, [nodes.get(value.get("node"), {}).get("label", key), None, value.get("unit"), value.get("status"), value.get("node"), new.get("classification")])
            metric(adoption, row, 1, value)
            row += 1
    for variant in new.get("sensitivities") or []:
        for key in ("adoption_cost", "net_benefit", "roi_months"):
            value = variant.get(key) or {}
            record(adoption, row, [(variant.get("label") or "입력 대안") + " · " + key, None, value.get("unit"), value.get("status"), value.get("node"), variant.get("inputs")])
            metric(adoption, row, 1, value)
            row += 1

    sources = sheet("출처원장", "입력 해시·행 포인터·계산 셀·단가 원문을 함께 추적한다", ["자료 구분", "참조", "원문 값 / 해시", "원본 근거", "수집 시각", "기간", "계산 셀", "상태"])
    row = 4
    for key, value in inputs.items():
        record(sources, row, ["입력", key, value, "입력 스냅샷", inputs.get("collected_at"), inputs.get("source_period"), None, "보존"])
        row += 1
    for key, node in nodes.items():
        record(sources, row, ["계산 입력" if node["kind"] == "input" else "계산식", key, node.get("value"), node.get("source"), inputs.get("collected_at"), period, refs[key], node.get("status")])
        row += 1
    missing = sheet("누락상태", "미수집·권한 부족·미산정과 정상 조회의 0을 구분한다", ["대상", "상태", "사유·필요 조건", "집계 기준", "근거"])
    row = 4
    for key, item in (result.get("datasets") or {}).items():
        record(missing, row, [item.get("label", key), item.get("status"), item.get("reason"), item.get("grain"), item.get("api")])
        row += 1
    for key in ("forecast", "anomalies", "ai", "budgets", "allocation"):
        item = result.get(key) or {}
        record(missing, row, [{"forecast": "공식 예측", "anomalies": "이상 지출", "ai": "AI 사용량", "budgets": "예산", "allocation": "현재 태그 배분"}[key], item.get("status"), item.get("reason"), item.get("cost_semantics", "별도 원장"), None])
        row += 1
    for key, node in nodes.items():
        if node["value"] is None:
            record(missing, row, [node.get("label"), node.get("status"), node.get("source"), node.get("unit"), key])
            row += 1
    validation = sheet("검증", "실제 재계산값을 분석시점 원장과 대조하고 오프라인 가정 변경을 식별한다", ["계산 참조", "항목", "분석시점 원장 값", "실제 재계산값", "차이", "판정", "단위", "근거"])
    for row, (key, node) in enumerate(nodes.items(), 4):
        record(validation, row, [key, node.get("label"), node["value"], None, None, None, node.get("unit"), "허용오차=max(1e-8, abs(기준값)×1e-10); 미산정은 빈값 일치"])
        metric(validation, row, 3, {"node": key, "value": node["value"], "unit": node.get("unit")})
        nr = row + 1
        validation.write_formula(row, 4, f'=IF(COUNT(C{nr},D{nr})=2,D{nr}-C{nr},"")', number, "" if node["value"] is None else 0)
        # AND는 차감을 단락 평가하지 않는다. 숫자 쌍이 확인된 IF 분기에서만 차감한다.
        validation.write_formula(row, 5, f'=IF(AND(C{nr}="",D{nr}=""),"일치",IF(COUNT(C{nr},D{nr})=2,IF(ABS(D{nr}-C{nr})<=MAX(1E-8,ABS(C{nr})*1E-10),"일치","변경/불일치"),"변경/불일치"))', text, "일치")
    for row, check in enumerate(result.get("checks") or [], 5 + len(nodes)):
        record(validation, row, [check.get("code"), "계산 코어 검증", None, None, None, "통과" if check.get("passed") else "확인 필요", None, check])
    return input_refs, percent


def build_xlsx(result, customer):
    """같은 계산 원장을 실제 재계산·검증한 Excel bytes로 제공한다."""
    nodes = _validate_ir(result)
    bio = io.BytesIO()
    wb = xlsxwriter.Workbook(bio, {"in_memory": True, "strings_to_formulas": False, "strings_to_urls": False})
    wb.set_properties({"title": str(customer) + " 비용 분석", "author": "AI Assessment 시스템", "created": datetime(2000, 1, 1, tzinfo=timezone.utc)})
    wb.set_custom_property("analysis_id", result.get("analysis_id", ""))
    wb.set_calc_mode("auto")
    text = wb.add_format({"font_name": FONT, "font_size": 10, "text_wrap": True, "valign": "top"})
    number = wb.add_format({"font_name": FONT, "font_size": 10, "num_format": "#,##0.00########", "valign": "top"})
    header = wb.add_format({"font_name": FONT, "font_size": 11, "bold": True, "font_color": "FFFFFF", "bg_color": PRIMARY, "text_wrap": True})
    title = wb.add_format({"font_name": FONT, "font_size": 16, "bold": True, "font_color": PRIMARY})

    def sheet(name, caption, heads):
        ws = wb.add_worksheet(name)
        ws.hide_gridlines(2)
        last = len(heads) - 1
        ws.merge_range(0, 0, 0, last, caption, title)
        ws.merge_range(1, 0, 1, last, str(customer) + " · AI Assessment 시스템 · 빈셀은 미수집·미산정이며 0이 아닙니다", text)
        ws.set_row(0, 26)
        ws.set_row(1, 32)
        ws.write_row(3, 0, heads, header)
        ws.set_column(0, last, 24, text)
        ws.set_column(0, 0, 36)
        ws.freeze_panes(4, 0)
        ws.set_row(3, 32)
        ws.set_default_row(26)
        ws.set_landscape()
        ws.fit_to_pages(1, 0)
        ws.repeat_rows(0, 3)
        ws.set_footer("&LAI Cost Governance & Optimization&R&P / &N")
        ws.protect(options={"autofilter": True, "sort": True, "select_unlocked_cells": True, "select_locked_cells": True})
        return ws

    summary = sheet("요약", "동일 실행의 비용·절감 지표", ["지표", "값", "단위", "상태", "근거", "계산 참조"])
    ledger = sheet("계산원장", "입력·연산·참조의 단일 수식 원장", ["계산 참조", "항목", "값 / 수식", "단위", "상태", "원본 근거", "연산", "인수"])
    ledger.set_column(5, 5, 64)
    ledger.set_column(7, 7, 80)
    refs = {node["id"]: f"'계산원장'!$C${row}" for row, node in enumerate(result["calculation"], 5)}
    input_refs, percent = _add_trace_sheets(wb, result, customer, sheet, text, number, refs, nodes)
    for row, node in enumerate(result["calculation"], 4):
        ledger.write(row, 0, node["id"], text)
        ledger.write(row, 1, node.get("label", ""), text)
        fmt = percent if node.get("unit") == "비율" else number
        if node["kind"] == "formula":
            ledger.write_formula(row, 2, _ir_formula(node["expression"], refs), fmt, node["value"] if node["value"] is not None else "")
        elif node["id"] in input_refs:
            ledger.write_formula(row, 2, input_refs[node["id"]], fmt, node["value"] if node["value"] is not None else "")
        elif node["value"] is not None:
            ledger.write_number(row, 2, node["value"], fmt)
        else:
            ledger.write_blank(row, 2, None, fmt)
        ledger.write_row(row, 3, [node.get("unit", ""), node.get("status", ""), node.get("source", ""), (node.get("expression") or {}).get("op", "input"), _json((node.get("expression") or {}).get("args", []))], text)
    for row, (label, path) in enumerate(SUMMARY_METRICS, 4):
        metric = _get(result, path)
        summary.write(row, 0, label, text)
        fmt = percent if metric.get("unit") == "비율" or path.endswith("savings_rate") else number
        if metric.get("node"):
            summary.write_formula(row, 1, _link_formula(refs[metric["node"]]), fmt, metric.get("value") if metric.get("value") is not None else "")
        else:
            summary.write_blank(row, 1, None, fmt)
        summary.write_row(row, 2, [metric.get("unit", ""), metric.get("status", "not_estimated"), metric.get("source", ""), metric.get("node", "")], text)
    summary.set_column(4, 4, 64)
    for ws in wb.worksheets():
        ws.autofilter(3, 0, max(4, ws.dim_rowmax), ws.dim_colmax)
    wb.close()
    return _recalculate(bio.getvalue(), list(nodes.values()))


# ---------------- 지정 템플릿 기반 PowerPoint ----------------
TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "cost_finops_v18.pptx"
TEMPLATE_SHA256 = "802f9b80e0dfa3e9b8f51363d129b791d50793513d7fb7dd6ffd3a21a798a539"


def _template_path():
    """동일한 정본 해시를 요구하며 패키지 기본 경로 또는 명시 설정을 해석한다."""
    configured = os.environ.get("AICAF_COST_TEMPLATE", "").strip()
    if not configured:
        return TEMPLATE_PATH
    candidate = Path(configured).expanduser()
    return candidate if candidate.is_absolute() else Path(__file__).resolve().parent / candidate


def _faces(properties, font=FONT):
    from pptx.oxml.ns import qn
    from pptx.oxml.xmlchemy import OxmlElement
    for face in ("latin", "ea", "cs"):
        tag = "a:" + face
        child = properties.find(qn(tag))
        if child is None:
            child = OxmlElement(tag)
            properties.append(child)
        child.set("typeface", font)


def _metric_value(metric, nodes):
    metric = metric or {}
    key = metric.get("node")
    if key:
        return nodes[key]["value"]
    if metric.get("value") is not None:
        raise ValueError("numeric metric requires calculation reference")
    return None


def _metric_text(metric, nodes, currency=None, ratio=False):
    value = _metric_value(metric, nodes)
    if value is None:
        return "미산정"
    if ratio or (metric or {}).get("unit") == "비율":
        return f"{value:.1%}"
    numeric = f"{value:,.8f}".rstrip("0").rstrip(".") if 0 < abs(value) < 0.01 else f"{value:,.2f}"
    return str(currency) + " " + numeric if currency else numeric


def _short(value, limit=32):
    text = str(value) if value not in (None, "") else "미확인"
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _check_package(data):
    """ZIP 관계·content type의 실제 대상 존재를 확인한다."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = set(archive.namelist())
        root = etree.fromstring(archive.read("[Content_Types].xml"))
        for item in root:
            if item.tag.endswith("Override") and item.get("PartName", "").lstrip("/") not in names:
                raise RuntimeError("missing content-type part")
        for part in names:
            if not part.endswith(".rels"):
                continue
            folder = posixpath.dirname(part)
            base = folder[:-6] if folder.endswith("/_rels") else "" if folder == "_rels" else folder
            for relation in etree.fromstring(archive.read(part)):
                if relation.get("TargetMode") == "External":
                    continue
                target = relation.get("Target", "")
                resolved = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(base, target))
                if resolved not in names:
                    raise RuntimeError("missing relationship target: " + resolved)


def _template_deck(result):
    """원본 프레젠테이션의 마스터·레이아웃·테마를 그대로 사용하는 Deck 확장이다."""
    import hashlib
    from cost_report import Deck, C
    from pptx import Presentation
    from pptx.enum.chart import XL_LABEL_POSITION
    from pptx.util import Pt

    source = _template_path().read_bytes()
    if hashlib.sha256(source).hexdigest() != TEMPLATE_SHA256:
        raise ValueError("design template hash mismatch")

    class TemplateDeck(Deck):
        def __init__(self):
            self.p = Presentation(io.BytesIO(source))
            for slide_id in list(self.p.slides._sldIdLst):
                self.p.part.drop_rel(slide_id.rId)
                self.p.slides._sldIdLst.remove(slide_id)
            self.slides = []
            self.p.core_properties.title = "AI Assessment 시스템 · 비용 분석"
            self.p.core_properties.subject = "동일 입력·기간·통화의 비용 분석"
            self.p.core_properties.author = "AI Assessment 시스템"
            self.p.core_properties.last_modified_by = "AI Assessment 시스템"
            self.p.core_properties.identifier = result.get("analysis_id", "")
            self.p.core_properties.created = datetime(2000, 1, 1, tzinfo=timezone.utc)
            self.p.core_properties.modified = datetime(2000, 1, 1, tzinfo=timezone.utc)

        def text(self, *args, **kwargs):
            shape = super().text(*args, **kwargs)
            for properties in shape._element.xpath(".//a:rPr|.//a:defRPr"):
                _faces(properties, kwargs.get("font", FONT))
            return shape

        def slide(self, kicker="", title="", subtitle="", notes=""):
            s = self.p.slides.add_slide(self.p.slide_layouts[0])
            self.text(s, kicker, 0.62, 0.30, 10.0, 0.25, 9, True, "primary")
            self.text(s, title, 0.62, 0.88, 12.08, 0.58, 27, True)
            self.text(s, subtitle, 0.64, 1.64, 12.0, 0.48, 15.3, False, "muted")
            if notes:
                s.notes_slide.notes_text_frame.text = notes
            self.slides.append(s)
            return s

        def table(self, *args, **kwargs):
            table = super().table(*args, **kwargs)
            for properties in table._tbl.xpath(".//a:rPr|.//a:defRPr"):
                _faces(properties)
            return table

        def chart(self, s, *args, **kwargs):
            chart = super().chart(s, *args, **kwargs)
            chart.font.name = FONT
            for name in ("category_axis", "value_axis"):
                try:
                    axis = getattr(chart, name)
                    axis.tick_labels.font.name = FONT
                    axis.tick_labels.font.size = Pt(10)
                    axis.tick_labels.font.color.rgb = __import__("pptx").dml.color.RGBColor.from_string(C["muted"])
                except (AttributeError, ValueError):
                    continue
            if chart.has_legend:
                chart.legend.font.name = FONT
                chart.legend.font.size = Pt(10.5)
            for plot in chart.plots:
                if plot.has_data_labels:
                    plot.data_labels.font.name = FONT
                    plot.data_labels.font.size = Pt(10)
                    # 스택형에 outEnd를 쓰면 PowerPoint가 차트를 폐기한다.
                    if "STACKED" in str(chart.chart_type):
                        plot.data_labels.position = XL_LABEL_POSITION.CENTER
            for properties in chart._chartSpace.xpath(".//a:rPr|.//a:defRPr"):
                _faces(properties)
            return chart

        def stat(self, s, value, label, x, y, w=3.6):
            # 템플릿의 Segoe UI 숫자 서체는 유지하되 한글 상태에는 맑은 고딕을 지정한다.
            font = FONT if any("가" <= char <= "힣" for char in value) else "Segoe UI"
            self.text(s, value, x, y, w, 0.72, 32, True, "primary", font=font)
            self.text(s, label, x, y + 0.74, w, 0.55, 12.6, color="muted", valign="t")

        def note(self, s, t, y=6.20):
            return super().note(s, t, y=y)

        def save(self):
            data = super().save()
            # 직렬화 과정의 부수 변경도 방지해 원본 마스터·테마 bytes를 보존한다.
            with zipfile.ZipFile(io.BytesIO(source)) as donor, zipfile.ZipFile(io.BytesIO(data)) as generated:
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as output:
                    for info in generated.infolist():
                        part = info.filename
                        preserve = part.startswith(("ppt/slideMasters/", "ppt/slideLayouts/", "ppt/theme/")) and part in donor.namelist()
                        output.writestr(info, donor.read(part) if preserve else generated.read(part))
            data = buf.getvalue()
            _check_package(data)
            return data

    return TemplateDeck()


def build_pptx(result, customer):
    """계산 IR 값·참조와 지정 마스터를 사용하는 편집 가능한 PPT bytes다."""
    from pptx.enum.chart import XL_CHART_TYPE
    nodes = _validate_ir(result)
    d = _template_deck(result)
    context = result.get("context") or {}
    currency = context.get("currency") or "통화 미확인"
    period = context.get("period") or {}
    classification = str(result.get("data_classification") or "")
    synthetic = any(word in classification.lower() for word in ("synthetic", "fixture", "example", "가상"))
    common = f"{period.get('start', '기간 미확인')} ~ {period.get('end', '기간 미확인')} · {context.get('basis', '기준 미확인')} · {currency}"
    if synthetic:
        common = "가상 시험·실제 고객 결과 아님 | " + common
    summary = result.get("summary") or {}
    comparison = result.get("comparison") or {}
    total = summary.get("total") or {}
    savings = comparison.get("savings") or {}
    projection = result.get("projection") or {}

    def slide(section, title, bound=()):
        notes = _json({"분석 실행 ID": result.get("analysis_id"), "고객": customer, "기준": context, "입력": result.get("inputs"), "원파일": result.get("input_files"), "입력 구분": classification or "입력 원장 기반", "계산 참조": list(bound), "계산 값": {key: nodes[key]["value"] for key in bound if key in nodes}})
        return d.slide(kicker=f"{len(d.slides) + 1:02d}  /  {section}", title=title, subtitle=common, notes=notes)

    def value(metric, ratio=False):
        return _metric_text(metric, nodes, None if ratio else currency, ratio=ratio)

    def chart(s, kind, categories, metric_series, x, y, w, h, unit=None):
        if not categories or not any(_metric_value(m, nodes) is not None for _, ms in metric_series for m in ms):
            return None
        series = [(name, [_metric_value(m, nodes) for m in ms]) for name, ms in metric_series]
        monetary = bool(unit and str(unit).startswith(currency))
        nonzero = [abs(v) for _, values in series for v in values if v is not None and v != 0]
        axis_format = "#,##0.000000" if monetary and nonzero and max(nonzero) < 0.01 else "#,##0.00" if monetary else "#,##0.00####"
        chart = d.chart(s, kind, categories, series, x, y, w, h, legend=len(series) > 1, labels=True, colors=[PRIMARY, "596660"], fmt=axis_format)
        if monetary:
            from pptx.chart.datalabel import DataLabels
            from pptx.oxml.ns import qn
            from pptx.oxml.xmlchemy import OxmlElement
            from pptx.util import Pt
            for plot in chart.plots:
                plot.data_labels.number_format = "#,##0.00"
                for native_series, (_, values) in zip(plot.series, series):
                    for index, amount in enumerate(values):
                        if amount is None or not 0 < abs(amount) < 0.01:
                            continue
                        # 표시 형식만 바꾸며 차트 캐시·내장 Excel의 원장 값은 반올림하지 않는다.
                        label = native_series.points[index].data_label
                        element = label._get_or_add_dLbl()
                        formatting = element.find(qn("c:numFmt"))
                        if formatting is None:
                            formatting = OxmlElement("c:numFmt")
                            element.insert_element_before(formatting, "c:spPr", "c:txPr", "c:dLblPos", "c:showLegendKey", "c:showVal")
                        formatting.set("formatCode", "#,##0.000000")
                        formatting.set("sourceLinked", "0")
                        label.font.name, label.font.size = FONT, Pt(10)
                        series_labels = DataLabels(native_series._element.get_or_add_dLbls())
                        series_labels.number_format = "#,##0.00"
                        series_labels.show_value = True
                        series_labels.font.name, series_labels.font.size = FONT, Pt(10)
            for properties in chart._chartSpace.xpath(".//a:rPr|.//a:defRPr"):
                _faces(properties)
        s.shapes[-1].name = "IR_CHART|" + "|".join((m or {}).get("node") or "missing" for _, ms in metric_series for m in ms)
        if unit:
            d.text(s, unit, x + 0.05, y - 0.22, w, 0.2, 10.5, color="muted")
        return chart

    has_savings = _metric_value(savings, nodes) is not None
    if result.get("track") == "new":
        new_summary = result.get("new") or {}
        s = slide("신규 도입 요약", "신규 도입의 월 비용과 순효과는 가정 기준이다", [(new_summary.get(k) or {}).get("node") for k in ("maintain_cost", "adoption_cost", "net_benefit")])
        d.text(s, _short(customer, 50) + " · AI Assessment 시스템", 0.72, 2.3, 11.96, 0.3, 14.4, True, "primary")
        for x, metric, label in ((0.72, new_summary.get("maintain_cost"), "현재 유지 월비용"), (4.65, new_summary.get("adoption_cost"), "AI 도입 월비용"), (8.55, new_summary.get("net_benefit"), "신규 월 순효과")):
            d.stat(s, value(metric), label, x, 2.90, w=3.7)
        d.table(s, [["판단 기준", "분석 결과"], ["비교 기준", "선택 완료월 인프라·동일 업무량·사용자 확인 인건비·자동화 가정"], ["단가·운영비", "선택한 소매 단가와 별도 지원비로 산정. 실제 청구 절감액이 아님"], ["미확인 입력", "업무량·시간·단가·지원비가 없으면 관련 총비용과 순효과는 미산정"]], y=4.40, rowh=0.42, colw=[2.4, 9.56])
        d.note(s, "월 순효과는 신규 도입 가정 시나리오다. 기존 인프라 비용의 절감 미확정과 구분하고 실현 절감으로 해석하지 않는다.")
    else:
        title = "확인 조치의 순효과는 추가 운영비를 포함한다" if has_savings else "현재 비용은 확인됐으나 절감액은 미확정이다" if _metric_value(total, nodes) is not None else "현재 비용과 절감 효과는 추가 수집 후 산정한다"
        s = slide("경영 요약", title, [total.get("node"), savings.get("node")])
        d.text(s, _short(customer, 50) + " · AI Assessment 시스템", 0.72, 2.3, 11.96, 0.3, 14.4, True, "primary")
        for x, metric, label in ((0.72, total, "선택 범위 비용 합계"), (4.65, comparison.get("to_be"), "TO-BE / 확정 조치 전 기준선"), (8.55, savings, "추가 운영비 포함 순절감")):
            d.stat(s, value(metric), label, x, 2.90, w=3.7)
        d.table(s, [["판단 기준", "분석 결과"], ["실측과 가정", "청구 실제 비용·관측 사용량·소매가격 추정을 각각 분리"], ["조치 상태", "확인 조치의 시나리오이며 실제 절감 실현 증거와 구분" if has_savings else "확정 조치 없음 또는 추가 비용 미확인·절감액 미산정"], ["기간·통화", context.get("grain") or "선택 원장 집계 기준"]], y=4.40, rowh=0.42, colw=[2.4, 9.56])
        d.note(s, "세금·credit·청구 할인 별도 내역 미수집. 공급자 권고 예상치는 실제 절감액에 자동 합산하지 않는다.")

    all_groups = result.get("groups") or []
    groups = all_groups[:6]
    complete_groups = bool(all_groups) and _metric_value(total, nodes) is not None and all(_metric_value(g, nodes) is not None for g in all_groups)
    composition_title = "선택 원장의 비용 구성은 상위 항목에서 확인된다" if complete_groups else "확인된 비용 구성은 부분합이며 미확인 항목이 남아 있다" if any(_metric_value(g, nodes) is not None for g in all_groups) else "현재 비용 구성은 추가 수집 후 산정한다"
    s = slide("현재 비용 구조", composition_title, [g.get("node") for g in groups])
    if groups:
        chart(s, XL_CHART_TYPE.BAR_CLUSTERED, [_short(g.get("label"), 27) for g in groups], [("선택 범위 비용", groups)], 0.72, 2.55, 6.4, 3.35, currency)
        d.table(s, [["항목", "금액"]] + [[_short(g.get("label"), 31), value(g)] for g in groups], x=7.45, y=2.40, w=5.23, colw=[3.10, 2.13], rowh=0.43, size=11.0)
    else:
        d.callout(s, "비용 구성 미산정", "선택 원장의 실제 비용 행과 지원되는 차원 수집이 필요하다.", y=2.60, h=0.9)
    d.note(s, f"표시 {len(groups)}개 / 전체 {len(result.get('groups') or [])}개. 상위 표시액은 전체 비용과 구분하며 나머지를 임의 합산하지 않는다.")

    detail = sorted(result.get("rows") or [], key=lambda row: (_metric_value({"node": row["id"]}, nodes) is None, -(_metric_value({"node": row["id"]}, nodes) or 0)))[:6]
    s = slide("세부 비용 원인", "비용 원인은 원문 자원·미터와 함께 추적한다", [row["id"] for row in detail])
    if detail:
        rows = [["자원 / 기간", "서비스 / 분류", "미터 / 과금 유형", "금액", "관측 차원"]]
        for row in detail:
            rows.append([_short(row.get("resource_name") or row.get("month") or row.get("date") or row.get("resource_group"), 25), _short(row.get("service") or row.get("tag_value") or row.get("pricing_model"), 24), _short(row.get("meter") or row.get("charge_type"), 28), value({"node": row["id"]}), _short(row.get("region") or row.get("tag_key") or "해당 원문 기준", 22)])
        d.table(s, rows, y=2.35, rowh=0.48, colw=[2.45, 2.55, 2.90, 2.05, 2.01], size=11.0)
    else:
        d.callout(s, "원문 비용 미수집 또는 선택 행 없음", "권한·기간·통화·원문 상태는 Excel의 누락상태 시트에서 구분한다.", y=2.60, h=0.9)
    d.note(s, "표시는 상위 6행 이내다. 자원 지역·SKU·현재 태그는 수집시점 인벤토리 조인이며 청구시점 정보와 다를 수 있다.")

    allocation = result.get("allocation") or {}
    has_allocation = _metric_value(allocation.get("allocated"), nodes) is not None and _metric_value(allocation.get("unallocated"), nodes) is not None
    allocation_title = "현시점 태그의 미배분 비용을 별도로 식별한다" if has_allocation else "현시점 태그의 배분 비용은 추가 수집 후 산정한다"
    s = slide("현재 태그 배분", allocation_title, [(allocation.get(k) or {}).get("node") for k in ("allocated", "unallocated", "coverage")])
    if has_allocation:
        chart(s, XL_CHART_TYPE.COLUMN_CLUSTERED, ["태그값 확인", "미배분·태그 확인 불가"], [("현재 태그 기준", [allocation["allocated"], allocation["unallocated"]])], 0.72, 2.65, 6.3, 3.10, currency)
        d.table(s, [["항목", "값"], ["배분 태그 키", allocation.get("key")], ["태그값 확인 비용", value(allocation.get("allocated"))], ["미배분 비용", value(allocation.get("unallocated"))], ["금액 커버리지", value(allocation.get("coverage"), ratio=True)]], x=7.4, y=2.4, w=5.28, colw=[2.7, 2.58], rowh=0.48, size=11.7)
    else:
        d.callout(s, "현재 태그 배분은 미산정이다", _short(allocation.get("reason"), 105), y=2.60, h=0.9)
    d.note(s, "수집시점의 현재 태그만 사용한다. 역사적 청구 태그·원가 확정·공유비용 배분 정책을 의미하지 않는다.")

    all_tokens = ((result.get("ai") or {}).get("tokens") or [])
    tokens = all_tokens[:4]
    s = slide("AI 비용·사용량", "관측 토큰과 소매가격은 청구비용과 분리한다", [(t.get(k) or {}).get("node") for t in tokens for k in ("input_tokens", "output_tokens", "retail_estimate")])
    if tokens:
        chart(s, XL_CHART_TYPE.COLUMN_CLUSTERED, [_short(t.get("deployment"), 23) for t in tokens], [("입력 토큰", [t.get("input_tokens") for t in tokens]), ("출력 토큰", [t.get("output_tokens") for t in tokens])], 0.72, 2.65, 6.2, 3.20, "관측 토큰")
        d.table(s, [["배포", "캐시 토큰", "소매 추정"]] + [[_short(t.get("deployment"), 25), _metric_text(t.get("cached_tokens"), nodes), value(t.get("retail_estimate"))] for t in tokens], x=7.15, y=2.4, w=5.53, colw=[2.45, 1.35, 1.73], rowh=0.58, size=10.8)
        d.text(s, "정확한 모델·지역·배포 유형·원문 단위의 단가 연결을 확인한다.", 7.15, 5.43, 5.5, 0.47, 12.6, color="muted")
    else:
        d.callout(s, "배포별 사용량 관측이 없다", "입력·출력·요청·캐시 메트릭과 배포 목록을 같은 기간으로 수집해야 한다.", y=2.60, h=0.9)
    d.note(s, f"표시 {len(tokens)}개 / 전체 {len(all_tokens)}개 · 계정·배포 순서의 첫 {len(tokens)}개. 전체는 Excel 사용량에서 추적한다. 청구비용과 구분하며 단가·메트릭 미확인은 미산정(0 아님).")

    selected_prices = []
    for direction, price in ((result.get("new") or {}).get("prices") or {}).items():
        if price:
            selected_prices.append(("신규 입력" if direction == "input" else "신규 출력", price))
    for token in (result.get("ai") or {}).get("tokens") or []:
        for scope, prices in (token.get("selected_prices") or {}).items():
            for direction, price in (prices or {}).items():
                if price:
                    label = _short(token.get("deployment"), 18) + " · " + ("현재" if scope == "current" else "대안") + (" 입력" if direction == "input" else " 출력")
                    selected_prices.append((label, price))
    if selected_prices:
        bound_prices = [key for key, node in nodes.items() if node["kind"] == "input" and any(node.get("source") == price.get("source") for _, price in selected_prices) and (key.endswith("_rate") or key.endswith("_price"))]
        s = slide("선택 소매 단가", "소매 단가는 원문 단위·SKU·지역을 함께 확인한다", bound_prices)
        rows = [["적용 대상", "원문 미터", "SKU / 지역", "소매 단가", "원문 단위"]]
        for label, price in selected_prices[:6]:
            amount_node = next((node for node in nodes.values() if node["id"] in bound_prices and node.get("source") == price.get("source")), None)
            amount = amount_node["value"] if amount_node else None
            display = "미산정" if amount is None else f"{price.get('currency') or '통화 미확인'} {amount:,.6f}".rstrip("0").rstrip(".")
            rows.append([_short(label, 33), _short(price.get("meter"), 41), _short(price.get("sku"), 34) + " / " + _short(price.get("region"), 22), display, price.get("unit") or "단위 미수집"])
        d.table(s, rows, y=2.35, rowh=0.50, colw=[2.10, 3.25, 2.70, 1.65, 2.26], size=10.8)
        d.note(s, f"선택 단가 {min(6, len(selected_prices))}개 / 전체 {len(selected_prices)}개. 소매 추정이며 실제 할인·세금·credit·지역/배포 전환 비용을 자동 가정하지 않는다.")

    candidates = (result.get("candidates") or [])[:5]
    s = slide("개선 후보", "검토 후보는 확정 조치 전까지 합산하지 않는다")
    if candidates:
        priority = {"High": "높음", "Medium": "중간", "Low": "낮음"}
        rows = [["권고 원문 요약", "공급자 연간 예상", "우선순위", "검토 상태"]]
        for candidate in candidates:
            annual = candidate.get("annual_estimate")
            estimate = "미수집" if annual is None else f"{candidate.get('currency') or '통화 미수집'} {annual:,.2f}"
            rows.append([_short(candidate.get("label"), 73), estimate, priority.get(candidate.get("impact"), "미확인"), "대상 확인 필요" if candidate.get("status") == "scope_not_resolved" else "조건 검토 필요"])
        d.table(s, rows, y=2.4, rowh=0.57, colw=[6.20, 2.4, 1.35, 2.01], size=10.8)
    else:
        d.callout(s, "자동 반영할 확인 조치가 없다", "권고 후보와 사용자 확인 조치는 별개이며 검증된 범위·근거·기간·통화가 필요하다.", y=2.60, h=0.9)
    d.note(s, "공급자 예상치·약정 대안은 중복될 수 있다. 원문 전체·제약·진행 단계는 Excel 후보과제·조치원장에서 추적한다.")

    s = slide("AS-IS / TO-BE", "확인 조치의 순효과는 추가 운영비를 포함한다" if has_savings else "확정 조치가 없으면 절감 효과는 미산정이다", [(comparison.get(k) or {}).get("node") for k in ("as_is", "to_be", "savings", "gross_savings", "recurring_cost")])
    chart(s, XL_CHART_TYPE.COLUMN_CLUSTERED, ["AS-IS", "TO-BE" if has_savings else "조치 전 기준선"], [("동일 기간 비용", [comparison.get("as_is"), comparison.get("to_be")])], 0.72, 2.65, 6.4, 3.15, currency)
    d.table(s, [["지표", "값"], ["중복 제거 총절감", value(comparison.get("gross_savings"))], ["추가 운영비", value(comparison.get("recurring_cost"))], ["순절감", value(savings)], ["순절감률", value(comparison.get("savings_rate"), ratio=True)]], x=7.5, y=2.4, w=5.18, colw=[2.8, 2.38], rowh=0.51, size=11.7)
    if result.get("track") == "new":
        d.note(s, "이 비교는 현재 인프라 비용의 최적화 원장이다. 신규 도입의 업무비용·소매가격 가정 시나리오와 합산하지 않는다.")
    else:
        d.note(s, "확정 조치가 없으면 TO-BE는 현재 비용의 기준선이며 절감액은 미산정이다. 조치가 있어도 추가 비용 미확인은 유지한다.")

    actions = (result.get("actions") or [])[:6]
    if actions:
        s = slide("조치별 기여", "조치 효과는 범위별 잔액과 중복 상한을 적용한다", [(a.get(k) or {}).get("node") for a in actions for k in ("requested", "applied", "overlap_removed")])
        chart(s, XL_CHART_TYPE.BAR_CLUSTERED, [_short(a.get("name"), 27) for a in actions], [("중복 제거 후 적용", [a.get("applied") for a in actions])], 0.72, 2.65, 6.4, 3.15, currency)
        d.table(s, [["조치", "요청 / 적용", "겹침 제외"]] + [[_short(a.get("name"), 25), value(a.get("requested")) + " / " + value(a.get("applied")), value(a.get("overlap_removed"))] for a in actions], x=7.35, y=2.4, w=5.33, colw=[1.95, 2.0, 1.38], rowh=0.49, size=10.5)
        d.note(s, "표시는 상위 6개 이내다. 진행 단계는 사용자 기록이며 실제 절감 실현 증거가 아니다. 전체 조치는 Excel에 보존한다.")

    if result.get("track") == "new":
        new = result.get("new") or {}
        s = slide("신규 도입 시나리오", "신규 도입 효과는 확인 가정과 선택 단가로 산정한다", [(new.get(k) or {}).get("node") for k in ("maintain_cost", "adoption_cost", "net_benefit", "roi_months")])
        chart(s, XL_CHART_TYPE.COLUMN_CLUSTERED, ["현재 유지", "AI 도입"], [("월 비용 시나리오", [new.get("maintain_cost"), new.get("adoption_cost")])], 0.72, 2.65, 6.4, 3.15, currency + "/월 · 소매가격 시나리오")
        d.table(s, [["가정 지표", "값"], ["월 요청량", _metric_text(new.get("monthly_requests"), nodes)], ["자동화 비율", value(new.get("automation_share"), ratio=True)], ["월 순효과", value(new.get("net_benefit"))], ["회수기간(개월)", _metric_text(new.get("roi_months"), nodes)]], x=7.45, y=2.4, w=5.23, colw=[2.7, 2.53], rowh=0.49)
        d.note(s, "기준 완료월·인건비·사용량·정확한 단가·별도 운영비가 없으면 관련 합계는 미산정이다. 현재 청구비용과 혼합하지 않는다.")

        variants = new.get("sensitivities") or []
        if variants:
            s = slide("신규 가정 대안", "가정 변경의 효과는 독립 시나리오로 비교한다", [(v.get(k) or {}).get("node") for v in variants for k in ("adoption_cost", "net_benefit", "roi_months")])
            chart(s, XL_CHART_TYPE.BAR_CLUSTERED, ["기준 가정"] + [_short(v.get("label"), 27) for v in variants], [("도입 월비용", [new.get("adoption_cost")] + [v.get("adoption_cost") for v in variants])], 0.72, 2.65, 6.4, 3.15, currency + "/월 · 소매가격 시나리오")
            d.table(s, [["대안", "월 순효과", "회수 개월"]] + [[_short(v.get("label"), 26), value(v.get("net_benefit")), _metric_text(v.get("roi_months"), nodes)] for v in variants], x=7.45, y=2.4, w=5.23, colw=[2.35, 1.85, 1.03], rowh=0.55, size=10.8)
            d.note(s, "각 대안은 코어의 독립 입력·수식 원장으로 계산한다. 실제 성과나 통계적 신뢰구간을 의미하지 않는다.")
        s = slide("신규 연간 효과·회수", "신규 연간 효과는 동일 월 가정의 반복이다", [(new.get(k) or {}).get("node") for k in ("net_benefit", "annual_net_benefit", "build_cost", "roi_months")])
        d.stat(s, value(new.get("annual_net_benefit")), "동일 월 가정의 연간 순효과", 0.72, 2.75, w=5.8)
        d.stat(s, _metric_text(new.get("roi_months"), nodes), "확인 구축비의 회수기간(개월)", 7.0, 2.75, w=5.4)
        d.table(s, [["환산 기준", "적용 조건"], ["동일 월 가정", "같은 업무량·인건비·자동화·소매 단가·별도 운영비를 반복한 가정"], ["월 순효과", value(new.get("net_benefit"))], ["초기 구축비", value(new.get("build_cost"))], ["예측 한계", "공식 예측·연간 실적이 아니며 월 순효과 비양수·구축비 미확인 시 회수기간 미산정"]], y=4.3, rowh=0.37, colw=[2.5, 9.46], size=11.7)
        d.note(s, "연간 환산과 회수기간도 동일 계산원장을 사용한다. 현재 청구비용 절감의 기간 환산 원장과 구분한다.")
    else:
        s = slide("기간 환산·누적", "월·연간 효과는 승인된 반복 가정으로만 환산한다", [(projection.get(k) or {}).get("node") for k in ("monthly_savings", "annual_savings", "payback_months")])
        cumulative = projection.get("cumulative") or []
        if projection.get("enabled") and any(_metric_value(p, nodes) is not None for p in cumulative):
            chart(s, XL_CHART_TYPE.LINE_MARKERS, [str(p.get("month")) for p in cumulative], [("전환비 차감 누적 순효과", cumulative)], 0.72, 2.65, 7.1, 3.1, currency + " · 경과월")
            d.table(s, [["환산 지표", "값"], ["월 순절감", value(projection.get("monthly_savings"))], ["연간 순절감", value(projection.get("annual_savings"))], ["회수기간(개월)", _metric_text(projection.get("payback_months"), nodes)]], x=8.05, y=2.4, w=4.63, colw=[2.3, 2.33], rowh=0.55, size=11.0)
        else:
            d.callout(s, "월·연간 효과와 회수기간은 미산정이다", "같은 일평균 사용량의 반복 가정·기간 순절감·전환 비용의 확인이 필요하다.", y=2.60, h=0.9)
            d.table(s, [["환산 조건", "적용 기준"], ["기간", "끝일−시작일+1의 포함 달력일. 미완료·짧은 관측기간 한계 유지"], ["연간화", "대상 연도 달력일을 반복한 가정. Microsoft 공식 예측·연간 실적 아님"], ["회수기간", "확인 전환비 / 월 환산 순절감. 순절감 미확인·비양수는 미산정"]], y=3.95, rowh=0.49, colw=[2.3, 9.66])
        d.note(s, "동일 사용량·단가·조치 효과가 반복되는 시나리오이며 수요 성장·환율 변동·계약 변동을 자동 가정하지 않는다.")

    s = slide("실행 조건·수집 한계", "추가 수집은 배분·예측·이상 판정의 전제이다")
    rows = [["영역", "현재 상태 / 실행 조건"], ["공식 예측", _short((result.get("forecast") or {}).get("reason"), 108)], ["이상 지출", _short((result.get("anomalies") or {}).get("reason"), 108)], ["현재 태그 배분", _short(allocation.get("reason"), 108)], ["예산", "원문 기간·범위·통화 확인 전 선택 범위 비용과 직접 비교하지 않음"], ["조치 확정", "근거 스냅샷·대상 행·기간·통화·추가 운영비·전환비를 확인 후 새 분석 생성"]]
    d.table(s, rows, y=2.35, rowh=0.58, colw=[2.25, 9.71], size=11.7)
    d.note(s, "현재 보고서는 불변 분석 스냅샷이다. Excel에서 가정을 바꿔도 시스템·기존 PPT는 자동 변경되지 않는다.")

    s = slide("가정·근거 추적", "모든 결과는 동일 입력·기간·기준으로 추적된다")
    inputs = result.get("inputs") or {}
    rows = [["입력 / 버전", "SHA-256 / 근거"], ["증거 입력", inputs.get("evidence_sha256") or "미수집"], ["구성 입력", inputs.get("topology_sha256") or "미수집"], ["인터뷰 입력", inputs.get("answers_sha256") or "미수집"], ["계산 버전", str(result.get("core_version") or "미확인") + " · " + str(result.get("core_source_sha256") or "미확인")], ["디자인 정본", TEMPLATE_SHA256]]
    d.table(s, rows, y=2.35, rowh=0.49, colw=[2.25, 9.71], size=10.8)
    d.text(s, "분석 실행 ID  " + str(result.get("analysis_id") or "미확인"), 0.72, 5.43, 11.96, 0.27, 10.5, color="muted")
    d.text(s, "Cost Management: 청구 기준별 비용 | Azure Monitor: 관측 사용량 | Azure Retail Prices: 소매 단가", 0.72, 5.82, 11.96, 0.27, 11.7, color="muted")
    d.note(s, "가정·원문 단위·조치별 범위·단가·수식·누락·원파일 해시는 동일 실행의 Excel에서 전체 추적한다.")
    return d.save()
