"""보고서 복구 경계의 합성 회귀시험. 고객·운영 상태는 읽거나 변경하지 않는다."""
import importlib
import io
from pathlib import Path
import sys

import openpyxl
import pytest
from pptx import Presentation

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
from test_cost_finops_report import fixture_new_ready, fixture_result, ledger_cells, ppt_text, recalculate_copy, report_module


def editable_cells(wb):
    ws = wb["가정값"]
    return {ws.cell(r, 6).value: ws.cell(r, 2) for r in range(5, ws.max_row + 1) if ws.cell(r, 6).value}


def formula_errors(wb):
    return [(ws.title, cell.coordinate, cell.value) for ws in wb for row in ws for cell in row if cell.data_type == "e"]


@pytest.mark.parametrize("invalid", [False, "0", " 35 ", 10**19])
def test_additional_invalid_paste_propagates_missing_not_savings(invalid, tmp_path):
    wb = openpyxl.load_workbook(io.BytesIO(report_module().build_xlsx(fixture_result(actions=True), "가상 입력 경계 시험")))
    editable_cells(wb)["action_0_requested"].value = invalid
    actual = recalculate_copy(wb, tmp_path)
    assert ledger_cells(actual)["action_0_requested"].value in (None, "")
    assert actual["요약"]["B9"].value in (None, "")
    assert not formula_errors(actual)
    row = next(r for r in range(5, actual["검증"].max_row + 1) if actual["검증"].cell(r, 1).value == "action_0_requested")
    assert actual["검증"].cell(row, 6).value == "변경/불일치"


@pytest.mark.parametrize("value", [0, 35, 25.125, 10**18])
def test_valid_numeric_paste_keeps_value_and_recalculates_same_operations(value, tmp_path):
    wb = openpyxl.load_workbook(io.BytesIO(report_module().build_xlsx(fixture_result(actions=True), "가상 숫자 대조 시험")))
    editable_cells(wb)["action_0_requested"].value = value
    actual = recalculate_copy(wb, tmp_path)
    assert ledger_cells(actual)["action_0_requested"].value == pytest.approx(value)
    assert actual["조치원장"]["E5"].value == pytest.approx(min(value, 100))
    assert actual["요약"]["B9"].value == pytest.approx(min(value, 100) - 5)
    assert not formula_errors(actual)


@pytest.mark.parametrize("key,invalid", [("new_automation_share", True), ("new_automation_share", 1.01), ("new_work_days", True), ("new_work_days", 32)])
def test_ratio_and_workday_paste_use_the_same_type_and_range_contract(key, invalid, tmp_path):
    result = fixture_new_ready({"work_days": 20})
    wb = openpyxl.load_workbook(io.BytesIO(report_module().build_xlsx(result, "가상 신규 입력 경계 시험")))
    editable_cells(wb)[key].value = invalid
    actual = recalculate_copy(wb, tmp_path)
    assert ledger_cells(actual)[key].value in (None, "")
    if key == "new_automation_share":
        assert ledger_cells(actual)["new_net_benefit"].value in (None, "")
    assert not formula_errors(actual)


def test_ready_fixture_resolves_stable_prices_and_has_numeric_effects():
    result = fixture_new_ready()
    new = result["new"]
    assert new["status"] == "scenario_estimate"
    assert all(new["prices"][direction] for direction in ("input", "output"))
    assert new["net_benefit"]["value"] == pytest.approx(1228.4)
    assert new["annual_net_benefit"]["value"] == pytest.approx(new["net_benefit"]["value"] * 12)
    assert all(variant["net_benefit"]["value"] is not None for variant in new["sensitivities"])


@pytest.mark.parametrize("overrides", [{"price_in_ref": "pricesModels:0", "price_out_ref": "pricesModels:1"}, {"support_cost_evidence": None}])
def test_incomplete_new_ir_remains_missing_in_ppt_and_real_xlsx(overrides):
    result = fixture_new_ready(overrides)
    assert result["new"]["status"] == "needs_inputs"
    for key in ("adoption_cost", "net_benefit", "annual_net_benefit", "roi_months"):
        assert result["new"][key]["value"] is None
    module = report_module()
    prs = Presentation(io.BytesIO(module.build_pptx(result, "가상 미산정 경계 시험")))
    assert ppt_text(prs.slides[0]).count("미산정") >= 2
    wb = openpyxl.load_workbook(io.BytesIO(module.build_xlsx(result, "가상 미산정 경계 시험")), data_only=True)
    cells = ledger_cells(wb)
    for key in ("new_adoption_cost", "new_net_benefit", "new_annual_net_benefit", "new_roi_months"):
        assert cells[key].value in (None, "")
    assert not formula_errors(wb)


def test_packaged_template_remains_relative_after_module_relocation(tmp_path):
    import importlib.util
    import shutil
    module = report_module()
    package = tmp_path / "portable"
    (package / "templates").mkdir(parents=True)
    source = Path(module.__file__).parent
    shutil.copyfile(source / "cost_finops_report.py", package / "cost_finops_report.py")
    shutil.copyfile(source / "templates" / "cost_finops_v18.pptx", package / "templates" / "cost_finops_v18.pptx")
    spec = importlib.util.spec_from_file_location("portable_report", package / "cost_finops_report.py")
    relocated = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(relocated)
    assert relocated.TEMPLATE_PATH == package / "templates" / "cost_finops_v18.pptx"
    data = relocated.build_pptx(fixture_result(), "가상 이식 경계 시험")
    assert len(Presentation(io.BytesIO(data)).slides) >= 8


def test_explicit_template_setting_is_used_and_retains_hash_contract(tmp_path, monkeypatch):
    import shutil
    module = report_module()
    configured = tmp_path / "configured.pptx"
    shutil.copyfile(Path(module.__file__).parent / "templates" / "cost_finops_v18.pptx", configured)
    monkeypatch.setenv("AICAF_COST_TEMPLATE", str(configured))
    monkeypatch.setattr(module, "TEMPLATE_PATH", tmp_path / "missing-default.pptx")
    assert module.build_pptx(fixture_result(), "가상 설정 경계 시험")[:2] == b"PK"


def test_explicit_template_wrong_hash_is_rejected(tmp_path, monkeypatch):
    module = report_module()
    configured = tmp_path / "wrong-template.pptx"
    configured.write_bytes(b"synthetic invalid template hash")
    monkeypatch.setenv("AICAF_COST_TEMPLATE", str(configured))
    with pytest.raises(ValueError, match="template hash mismatch"):
        module.build_pptx(fixture_result(), "가상 템플릿 검증 시험")


def test_unavailable_explicit_soffice_does_not_fall_back_or_return_zero_caches(tmp_path, monkeypatch):
    module = report_module()
    monkeypatch.setenv("AICAF_SOFFICE", str(tmp_path / "unavailable-soffice"))
    with pytest.raises(RuntimeError, match="configured LibreOffice executable unavailable"):
        module.build_xlsx(fixture_result(), "가상 의존성 경계 시험")


def test_missing_renderer_still_fails_instead_of_filling_caches(monkeypatch):
    module = report_module()
    monkeypatch.delenv("AICAF_SOFFICE", raising=False)
    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    monkeypatch.setattr(module.Path, "is_file", lambda path: False)
    expected="native_office_worker_interpreter_unavailable" if module.os.name=='nt' else "LibreOffice required"
    with pytest.raises(RuntimeError, match=expected):
        module.build_xlsx(fixture_result(), "가상 누락 의존성 시험")


@pytest.mark.parametrize("variable", ["ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"])
def test_windows_install_directory_discovery_is_bound_to_existing_file(variable, tmp_path, monkeypatch):
    module = report_module()
    monkeypatch.delenv("AICAF_SOFFICE", raising=False)
    for name in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        monkeypatch.delenv(name, raising=False)
    executable = tmp_path / "Windows installation boundary" / "LibreOffice" / "program" / "soffice.exe"
    executable.parent.mkdir(parents=True)
    executable.write_text("synthetic discovery boundary; not executed", encoding="utf-8")
    executable.chmod(0o700)
    monkeypatch.setenv(variable, str(executable.parents[2]))
    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    discover = getattr(module, "_soffice_executable", None)
    assert callable(discover), "환경·설치 디렉터리 기반 실제 실행 파일 발견 경계가 필요하다"
    assert Path(discover()) == executable


def chart_sources(data):
    import posixpath
    import zipfile
    from lxml import etree
    namespace = {"c": "http://schemas.openxmlformats.org/drawingml/2006/chart"}
    output = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for part in sorted(archive.namelist()):
            if not part.startswith("ppt/charts/chart") or not part.endswith(".xml"):
                continue
            root = etree.fromstring(archive.read(part))
            relationship_part = posixpath.dirname(part) + "/_rels/" + posixpath.basename(part) + ".rels"
            rels = etree.fromstring(archive.read(relationship_part))
            targets = {node.get("Id"): node.get("Target") for node in rels}
            external = root.find("c:externalData", namespace)
            reference = external.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
            target = posixpath.normpath(posixpath.join(posixpath.dirname(part), targets[reference]))
            embedded = openpyxl.load_workbook(io.BytesIO(archive.read(target)), data_only=True)
            numbers = [cell.value for ws in embedded for row in ws for cell in row if isinstance(cell.value, (int, float)) and not isinstance(cell.value, bool)]
            output.append((part, root, numbers))
    return output, namespace


def test_currency_display_rounds_labels_only_and_preserves_native_price_precision():
    amount = 4378.574171
    result = fixture_result(amount=amount)
    data = report_module().build_pptx(result, "가상 통화 정밀도 시험")
    sources, namespace = chart_sources(data)
    assert any(amount in numbers for _, _, numbers in sources)
    assert any(amount == float(value) for _, root, _ in sources for value in root.xpath(".//c:numCache/c:pt/c:v/text()", namespaces=namespace))
    for part, root, _ in sources:
        formats = root.xpath(".//c:dLbls/c:numFmt/@formatCode|.//c:valAx/c:numFmt/@formatCode", namespaces=namespace)
        assert formats and set(formats) == {"#,##0.00"}, (part, formats)


@pytest.mark.parametrize("amount", [0.004378574171, -0.004378574171])
def test_tiny_currency_label_uses_six_places_without_rounding_embedded_values(amount):
    data = report_module().build_pptx(fixture_result(amount=amount), "가상 미세 통화 시험")
    sources, namespace = chart_sources(data)
    assert any(amount in numbers for _, _, numbers in sources)
    matched = 0
    for part, root, _ in sources:
        for series in root.xpath(".//c:ser", namespaces=namespace):
            for point in series.xpath("./c:val/c:numRef/c:numCache/c:pt", namespaces=namespace):
                if float(point.find("c:v", namespace).text) != amount:
                    continue
                matched += 1
                index = point.get("idx")
                formats = series.xpath(f'./c:dLbls/c:dLbl[c:idx/@val="{index}"]/c:numFmt/@formatCode', namespaces=namespace)
                assert formats == ["#,##0.000000"], (part, index, formats)
    assert matched


def test_incomplete_costs_do_not_claim_confirmed_composition_or_allocation():
    result = fixture_result(amount=None)
    assert result["summary"]["total"]["value"] is None
    data = report_module().build_pptx(result, "가상 불완전 원장 시험")
    text = "\n".join(ppt_text(slide) for slide in Presentation(io.BytesIO(data)).slides)
    assert "선택 원장의 비용 구성은 상위 항목에서 확인된다" not in text
    assert "현시점 태그의 미배분 비용을 별도로 식별한다" not in text
    assert "부분합" in text and "추가 수집 후 산정" in text


def test_complete_costs_retain_original_confirmed_conclusion_titles():
    data = report_module().build_pptx(fixture_result(), "가상 완전 원장 대조 시험")
    text = "\n".join(ppt_text(slide) for slide in Presentation(io.BytesIO(data)).slides)
    assert "선택 원장의 비용 구성은 상위 항목에서 확인된다" in text
    assert "현시점 태그의 미배분 비용을 별도로 식별한다" in text


def test_mixed_currency_series_keeps_regular_labels_when_tiny_label_is_customized():
    data = report_module().build_pptx(fixture_result(amount=0.004378574171), "가상 혼합 라벨 회귀 시험")
    sources, namespace = chart_sources(data)
    matched = 0
    for part, root, _ in sources:
        for series in root.xpath(".//c:ser", namespaces=namespace):
            amounts = [float(value) for value in series.xpath("./c:val/c:numRef/c:numCache/c:pt/c:v/text()", namespaces=namespace)]
            if 100 not in amounts or 0.004378574171 not in amounts:
                continue
            matched += 1
            assert series.xpath("./c:dLbls/c:showVal/@val", namespaces=namespace) == ["1"], part
    assert matched
