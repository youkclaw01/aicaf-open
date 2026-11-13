"""보고서 v2 검증. 수치가 있는 입력은 실제 고객과 무관한 가상 시험 원장이다."""
import importlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from lxml import etree
import openpyxl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def fixture_result(amount=20, actions=False, track="existing", new_options=None):
    """실제 계산 코어로 작은 가상 원장을 만든다."""
    from cost_finops import build_analysis, digest
    rid = "/subscriptions/test-report/resourceGroups/rg-test/providers/Microsoft.Compute/virtualMachines/vm-test"
    ev = {
        "subscriptionId": "test-report",
        "completedAtUtc": "2026-10-08T00:00:00Z",
        "costPeriod": {"fromUtc": "2026-09-01T00:00:00Z", "toUtc": "2026-09-30T23:59:59Z"},
        "sections": {
            "costByResource": {"status": "collected", "records": [
                {"Cost": 100, "Currency": "USD", "ResourceId": rid, "ServiceName": "Virtual Machines", "Meter": "시험 미터 A"},
                {"Cost": amount, "Currency": "USD", "ResourceId": rid + "-b", "ServiceName": "Storage", "Meter": "시험 미터 B"},
            ]},
            "resourceTags": {"status": "collected", "records": [{"id": rid, "tags": {"workload": "가상 업무"}}]},
        },
    }
    opts = {"dataset": "resource", "currency": "USD", "allocation_key": "workload", "projection": {"horizon_months": 2}}
    if actions:
        opts.update(actions=[{
            "id": "test-action", "name": "가상 시험 조치", "kind": "confirmed_amount", "row_ids": ["resource_0"],
            "amount": 25, "currency": "USD", "basis": "ActualCost", "enabled": True,
            "scope_confirmed": True, "evidence": "가상 시험 가정", "evidence_snapshot": digest(ev), "phase": "planned",
        }], recurring_cost=5, recurring_cost_evidence="가상 시험 추가 운영비", transition_cost=10,
            transition_cost_evidence="가상 시험 전환비", projection={"enabled": True, "evidence": "가상 시험 동일 일평균", "horizon_months": 2})
    if new_options is not None:
        opts["new"] = new_options
    result = build_analysis(ev, track, opts)
    result["data_classification"] = "synthetic_test_fixture"
    return result


def report_module():
    assert importlib.util.find_spec("cost_finops_report") is not None, "동일 계산 원장의 실제 보고서 생성기가 필요하다"
    return importlib.import_module("cost_finops_report")


def ledger_cells(workbook):
    ws = workbook["계산원장"]
    return {ws.cell(r, 1).value: ws.cell(r, 3) for r in range(5, ws.max_row + 1) if ws.cell(r, 1).value}


def test_xlsx_formulas_recalculate_from_real_ir_without_null_to_zero():
    result = fixture_result(amount=None)
    data = report_module().build_xlsx(result, "가상 시험 사례")
    assert isinstance(data, bytes) and data[:2] == b"PK"
    formulas = openpyxl.load_workbook(io.BytesIO(data), data_only=False)
    cached = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    refs, values = ledger_cells(formulas), ledger_cells(cached)
    for node in result["calculation"]:
        if node["kind"] == "formula":
            assert refs[node["id"]].data_type == "f", node["id"]
        expected, actual = node["value"], values[node["id"]].value
        if expected is None:
            assert actual in (None, ""), (node["id"], actual)
        else:
            assert actual == pytest.approx(expected, rel=1e-10, abs=1e-8), node["id"]
    assert cached["요약"]["B5"].value in (None, "")
    assert cached["요약"]["B6"].value == 100


def test_xlsx_tracks_sources_and_only_confirmed_assumptions_are_editable():
    result = fixture_result(actions=True)
    data = report_module().build_xlsx(result, "가상 시험 사례")
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=False)
    required = {"실행계약", "원본비용", "정규화비용", "현재태그배분", "단가표", "사용량", "가정값", "입력계약", "조치원장", "출처원장", "누락상태", "검증", "기간환산", "예산", "후보과제"}
    assert required <= set(wb.sheetnames), "원본부터 근거·상태까지 추적 시트가 필요하다"
    assert wb["원본비용"]["C5"].value == 100
    assert wb["정규화비용"]["B5"].data_type == "f"
    source_text = "\n".join(str(c.value) for row in wb["실행계약"] for c in row if c.value is not None)
    assert result["inputs"]["evidence_sha256"] in source_text
    assert result["analysis_id"] in source_text
    unlocked = [(ws.title, c.coordinate) for ws in wb for row in ws for c in row if not c.protection.locked]
    assert unlocked and all(name == "가정값" for name, _ in unlocked)
    assert all(ws.protection.sheet for ws in wb)
    assumptions = wb["가정값"]
    inputs = {assumptions.cell(r, 6).value: assumptions.cell(r, 2) for r in range(5, assumptions.max_row + 1)}
    assert inputs["action_0_requested"].value == 25
    assert not inputs["action_0_requested"].protection.locked
    assert inputs["recurring_cost"].value == 5
    assert wb["조치원장"]["E5"].data_type == "f"
    cached = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    assert cached["요약"]["B9"].value == 20
    assert all(cached["검증"].cell(r, 6).value == "일치" for r in range(5, 5 + len(result["calculation"])))


def test_large_ir_sum_stays_within_excel_formula_capacity():
    from cost_finops import Ledger
    result = fixture_result()
    ledger = Ledger()
    keys = [ledger.input(f"many_{i}", "가상 대량 입력", 0.01, "USD", "가상 시험") for i in range(600)]
    ledger.formula("many_total", "가상 대량 합계", "sum", keys, "USD")
    result["calculation"].extend(ledger.nodes)
    data = report_module().build_xlsx(result, "가상 시험 사례")
    formulas = ledger_cells(openpyxl.load_workbook(io.BytesIO(data), data_only=False))
    assert len(formulas["many_total"].value) <= 8192, "Excel 함수 인수·수식 길이 한도를 지켜야 한다"
    assert "$C$" in formulas["many_total"].value and ":" in formulas["many_total"].value
    cached = ledger_cells(openpyxl.load_workbook(io.BytesIO(data), data_only=True))
    assert cached["many_total"].value == pytest.approx(ledger.metric("many_total")["value"])


def test_stale_metric_value_is_rejected_instead_of_disagreeing_with_ir():
    result = fixture_result()
    result["summary"]["total"]["value"] = 999
    with pytest.raises(ValueError, match="metric.*mismatch"):
        report_module().build_xlsx(result, "가상 시험 사례")


def test_pptx_reuses_template_masters_and_native_charts_with_explicit_fonts():
    from pptx import Presentation
    module = report_module()
    assert callable(getattr(module, "build_pptx", None)), "지정 템플릿의 실제 PPT 생성기가 필요하다"
    result = fixture_result()
    data = module.build_pptx(result, "가상 시험 사례")
    assert isinstance(data, bytes) and data[:2] == b"PK"
    prs = Presentation(io.BytesIO(data))
    assert len(prs.slides) >= 8
    assert prs.core_properties.identifier == result["analysis_id"]
    all_text = "\n".join(shape.text for slide in prs.slides for shape in slide.shapes if shape.has_text_frame)
    assert "가상" in all_text and "미산정" in all_text
    assert "AI Cost Governance & Optimization" in all_text
    assert "AI Assessment 시스템" in all_text
    template = module._template_path()
    ns = {"c": "http://schemas.openxmlformats.org/drawingml/2006/chart", "a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(data)) as output, zipfile.ZipFile(template) as source:
        for part in source.namelist():
            if part.startswith(("ppt/slideMasters/", "ppt/slideLayouts/", "ppt/theme/")):
                assert output.read(part) == source.read(part), part
        charts = [part for part in output.namelist() if part.startswith("ppt/charts/chart") and part.endswith(".xml")]
        assert len(charts) >= 2
        for part in charts:
            root = etree.fromstring(output.read(part))
            axes = root.xpath(".//c:catAx|.//c:valAx", namespaces=ns)
            assert axes
            for element in axes + root.xpath(".//c:dLbls", namespaces=ns):
                for face in ("latin", "ea", "cs"):
                    values = element.xpath(f"./c:txPr/a:p/a:pPr/a:defRPr/a:{face}/@typeface", namespaces=ns)
                    assert values and set(values) == {"Malgun Gothic"}, (part, face, values)


def test_missing_summary_stat_uses_cjk_template_font():
    from pptx import Presentation
    data = report_module().build_pptx(fixture_result(), "가상 시험 사례")
    prs = Presentation(io.BytesIO(data))
    missing = [shape for shape in prs.slides[0].shapes if shape.has_text_frame and shape.text == "미산정"]
    assert missing
    assert all(run.font.name == "Malgun Gothic" for shape in missing for paragraph in shape.text_frame.paragraphs for run in paragraph.runs), "미산정은 숫자용 Segoe UI가 아니라 맑은 고딕으로 명시해야 한다"


def fixture_new_ready(overrides=None):
    """가격과 업무량까지 명시한 가상 신규 도입 원장이다. 공식 가격·고객 사실이 아니다."""
    from cost_finops import build_analysis, price_catalogue
    rates = [
        {"meterName": "시험 Input", "skuName": "가상 표준 SKU", "productName": "가상 AI 단가", "unitOfMeasure": "1M Tokens", "retailPrice": 0.4, "currencyCode": "USD", "type": "Consumption", "tierMinimumUnits": 0, "armRegionName": "test-region"},
        {"meterName": "시험 Output", "skuName": "가상 표준 SKU", "productName": "가상 AI 단가", "unitOfMeasure": "1M Tokens", "retailPrice": 1.6, "currencyCode": "USD", "type": "Consumption", "tierMinimumUnits": 0, "armRegionName": "test-region"},
    ]
    ev = {
        "subscriptionId": "test-report",
        "completedAtUtc": "2026-10-08T00:00:00Z",
        "costPeriod": {"fromUtc": "2026-09-01T00:00:00Z", "toUtc": "2026-09-30T23:59:59Z"},
        "sections": {
            "costMonthlyTotals": {"status": "collected", "records": [{"Cost": 120, "Currency": "USD", "costType": "ActualCost", "month": "2026-09"}]},
            "pricesModels": {"status": "collected", "records": rates},
        },
    }
    catalogue = price_catalogue(ev)
    opts = {"dataset": "monthly", "currency": "USD", "projection": {"horizon_months": 2}, "new": {
        "baseline_month": "2026-09", "monthly_requests": 1000, "input_tokens": 2000, "output_tokens": 500,
        "hourly_cost": 25, "monthly_work_hours": 100, "automation_share": 0.5, "build_cost": 400,
        "support_cost": 20, "support_cost_evidence": "가상 시험 지원비", "price_in_ref": catalogue[0]["ref"], "price_out_ref": catalogue[1]["ref"],
        "sensitivities": [{"label": "가상 자동화 대안", "automation_share": 0.7}],
    }}
    if overrides:
        opts["new"].update(overrides)
    result = build_analysis(ev, "new", opts)
    result["data_classification"] = "synthetic_test_fixture"
    return result


def ppt_text(slide):
    parts = []
    for shape in slide.shapes:
        if shape.has_text_frame:
            parts.append(shape.text)
        if shape.has_table:
            parts.extend(cell.text for row in shape.table.rows for cell in row.cells)
    return "\n".join(parts)


def test_new_pptx_summary_uses_new_ir_and_displays_original_price_units():
    from pptx import Presentation
    result = fixture_new_ready()
    data = report_module().build_pptx(result, "가상 신규 도입 사례")
    prs = Presentation(io.BytesIO(data))
    summary = ppt_text(prs.slides[0])
    assert "신규 월 순효과" in summary, "신규 시나리오를 기존 인프라 절감 미확정과 혼동하지 않는다"
    assert f"USD {result['new']['net_benefit']['value']:,.2f}" in summary
    text = "\n".join(ppt_text(slide) for slide in prs.slides)
    assert "시험 Input" in text and "시험 Output" in text
    assert "1M Tokens" in text and "가상 표준 SKU" in text
    assert "pricesModels:0" not in text, "단가 행 번호는 고객용 라벨이 아니다"


def test_new_xlsx_summary_and_sensitivities_use_same_ir():
    result = fixture_new_ready()
    data = report_module().build_xlsx(result, "가상 신규 도입 사례")
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    summary = {wb["요약"].cell(r, 1).value: wb["요약"].cell(r, 2).value for r in range(5, wb["요약"].max_row + 1)}
    assert summary.get("신규 월 순효과") == result["new"]["net_benefit"]["value"], "신규 도입 순효과를 기존 인프라 절감액과 분리해 요약해야 한다"
    values = ledger_cells(wb)
    for node in result["calculation"]:
        if node["value"] is None:
            assert values[node["id"]].value in (None, "")
        else:
            assert values[node["id"]].value == pytest.approx(node["value"], rel=1e-10, abs=1e-8)
    labels = "\n".join(str(c.value) for row in wb["신규도입"] for c in row if c.value is not None)
    assert "가상 자동화 대안" in labels


def test_new_pptx_annual_and_variants_remain_separate_from_billed_savings():
    from pptx import Presentation
    result = fixture_new_ready()
    data = report_module().build_pptx(result, "가상 신규 도입 사례")
    prs = Presentation(io.BytesIO(data))
    text = "\n".join(ppt_text(slide) for slide in prs.slides)
    assert f"USD {result['new']['annual_net_benefit']['value']:,.2f}" in text, "동일 신규 가정의 연간 효과도 IR에서 읽어야 한다"
    assert "동일 월 가정" in text
    assert "가상 자동화 대안" in text
    assert "현재 인프라" in text, "청구비용 비교와 신규 업무비용 시나리오를 구분해야 한다"


def recalculate_copy(workbook, tmp_path):
    """검증용 사본을 해당 플랫폼의 실제 계산 엔진으로 재계산해 읽는다."""
    source = tmp_path / "edited.xlsx"
    workbook.save(source)
    output = tmp_path / "calculated"
    output.mkdir()
    if os.name=='nt':
        from asis_office import convert_isolated
        target=convert_isolated(source,output,'xlsx',timeout=90)
        return openpyxl.load_workbook(target,data_only=True)
    run = subprocess.run([report_module()._soffice_executable(), "-env:UserInstallation=" + (tmp_path / "profile").as_uri(), "--headless", "--convert-to", "xlsx", "--outdir", str(output), str(source)], capture_output=True, text=True, encoding="utf-8", timeout=90)
    assert run.returncode == 0 and (output / source.name).is_file(), run.stdout + run.stderr
    return openpyxl.load_workbook(output / source.name, data_only=True)


@pytest.mark.parametrize("invalid", [True, "35", -1, 10**19])
def test_offline_assumption_invalid_types_and_range_do_not_become_savings(invalid, tmp_path):
    data = report_module().build_xlsx(fixture_result(actions=True), "가상 시험 사례")
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=False)
    assumptions = wb["가정값"]
    row = next(r for r in range(5, assumptions.max_row + 1) if assumptions.cell(r, 6).value == "action_0_requested")
    assumptions.cell(row, 2).value = invalid
    cached = recalculate_copy(wb, tmp_path)
    assert ledger_cells(cached)["action_0_requested"].value in (None, ""), "붙여넣기로 검사를 우회한 잘못된 가정은 미산정이어야 한다"
    assert cached["요약"]["B9"].value in (None, "")
    assert not [(ws.title, c.coordinate) for ws in cached for row in ws for c in row if c.data_type == "e"]


def test_windows_recalculation_selects_native_worker_and_keeps_cache_verification(monkeypatch):
    """호출 계약 시험이며 실제 Excel의 계산 결과를 모사한 품질 증거로 쓰지 않는다."""
    from types import SimpleNamespace
    import asis_office
    module=report_module();calls=[];checks=[]
    monkeypatch.setattr(module,'os',SimpleNamespace(name='nt',environ={}))
    def forbidden_libreoffice():raise AssertionError('Windows native calculation must not require LibreOffice')
    monkeypatch.setattr(module,'_soffice_executable',forbidden_libreoffice)
    def native(source,output,extension,*,timeout):
        assert source.read_bytes()==b'SYNTHETIC_UNIT_INPUT' and extension=='xlsx' and timeout==180
        target=output/source.name;target.write_bytes(b'SYNTHETIC_UNIT_NATIVE_DONOR');calls.append('native');return target
    monkeypatch.setattr(asis_office,'convert_isolated',native,raising=False)
    monkeypatch.setattr(module,'_verify_cached',lambda data,nodes:checks.append(data))
    def graft(original,donor):
        assert original==b'SYNTHETIC_UNIT_INPUT' and donor==b'SYNTHETIC_UNIT_NATIVE_DONOR'
        return b'SYNTHETIC_UNIT_GRAFTED_CACHE'
    monkeypatch.setattr(module,'_graft_formula_caches',graft)
    assert module._recalculate(b'SYNTHETIC_UNIT_INPUT',[])==b'SYNTHETIC_UNIT_GRAFTED_CACHE'
    assert calls==['native'] and checks==[b'SYNTHETIC_UNIT_NATIVE_DONOR',b'SYNTHETIC_UNIT_GRAFTED_CACHE']
