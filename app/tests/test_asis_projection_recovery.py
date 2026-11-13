"""운영에서 수집한 구조의 합성 원문을 실제 네이티브 표현에 전달한다."""
from copy import deepcopy
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_analysis
import asis_artifacts
import asis_model
from test_asis_configuration_delivery import configuration_case


def nested_case(tmp_path):
    previous, originals = configuration_case(tmp_path)
    topology = originals["topology.json"]
    account = topology["configurations"]["foundryAccountConfiguration"][0]["id"]
    for resource in topology["resources"]:
        if resource["id"] == account:
            resource["location"] = "synthetic-observed-region"
    record = originals["aicaf-evidence.json"]["sections"]["aiDeployments"]["records"][0]
    record.pop("modelName")
    record.pop("skuName")
    record["model"] = {"name": "SYNTHETIC-OBSERVED-MODEL", "version": "SYNTHETIC-VERSION"}
    record["sku"] = {"name": "SYNTHETIC-OBSERVED-SKU"}
    endpoint = topology["configurations"]["privateEndpoints"][0]
    endpoint["privateLinkServiceConnections"] = [{"privateLinkServiceId": account, "status": "Approved", "groupIds": ["account"]}]
    source = tmp_path / "synthetic-configuration-source"
    for name, value in originals.items():
        (source / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    assessment = {"id": previous["input"]["run"]["assessment_id"], "customer": "SYNTHETIC UNIT TEST - NOT A CUSTOMER",
                  "title": "Projection regression only", "infra_case_id": previous["input"]["run"]["case_id"], "track": "existing"}
    return asis_analysis.analyze(asis_model.normalize(source, assessment, [], "synthetic-projection-r1",
                                                     classification="synthetic_unit_test_not_customer"))


def test_observed_nested_deployment_and_exact_id_region_reach_native_rows(tmp_path):
    result = nested_case(tmp_path)
    original = deepcopy(result["input"])
    area = next(area for area in result["stages"][0]["areas"] if area["area_id"] == "ai")
    rows = asis_artifacts.native_diagram_rows(area, result["input"])
    account = next(row for row in rows if row[0] == "account-unit")
    deployment = next(row for row in rows if row[0] == "deployment-unit")
    assert "synthetic-observed-region" in account[1]
    assert "SYNTHETIC-OBSERVED-MODEL" in deployment[1]
    assert "SYNTHETIC-VERSION" in deployment[1]
    assert "SYNTHETIC-OBSERVED-SKU" in deployment[1]
    assert "리전 미확인" not in account[1]
    assert "모델 미확인" not in deployment[1]
    assert "SKU 미확인" not in deployment[1]
    graph = asis_artifacts.configuration_relationships(result["input"], "ai")
    node = next(node for node in graph["nodes"] if node["id"].endswith("/deployments/deployment-unit"))
    assert "SYNTHETIC-OBSERVED-MODEL" in node["detail"]
    assert result["input"] == original


def test_declared_private_link_target_and_observed_state_reach_network_graph(tmp_path):
    result = nested_case(tmp_path)
    graph = asis_artifacts.configuration_relationships(result["input"], "network")
    endpoint = next(node for node in graph["nodes"] if node["kind"] == "endpoint")
    account_id = next(fact for fact in result["input"]["facts"] if fact["id"] == "F_T_foundryAccountConfiguration")["value"][0]["id"]
    links = [edge for edge in graph["edges"] if edge["from_id"] == endpoint["id"] and edge["to_id"] == account_id]
    assert len(links) == 1
    assert "Approved" in endpoint["detail"]
    assert "미확인" not in endpoint["detail"]
    target = next(node for node in graph["nodes"] if node["id"] == account_id)
    assert target["collected"] is True
    assert "synthetic-observed-region" in target["detail"]
    assert links[0]["provenance"][0]["field"].endswith("privateLinkServiceConnections/0/privateLinkServiceId")
    assert graph["runtime_verified"] is False


def test_area_network_overview_contains_all_declared_native_references(tmp_path):
    from pptx import Presentation
    from pptx.shapes.connector import Connector
    result = nested_case(tmp_path)
    graph = asis_artifacts.configuration_relationships(result["input"], "network")
    expected = {edge["id"] for edge in graph["edges"]}
    assert expected
    path = tmp_path / "network-overview.pptx"
    asis_artifacts.presentation(__import__('visual_spec_stub').with_visual_specs(result), path)
    found = {shape.name.split(":", 1)[1] for slide in Presentation(path).slides for shape in slide.shapes
             if isinstance(shape, Connector) and shape.name.startswith("area_configuration_edge:")}
    assert found == expected, "영역 첫 구성도 자체에도 원문에서 선언된 참조를 모두 표시해야 한다"


def test_official_criterion_quote_url_and_modality_are_separate_from_interpretation(tmp_path):
    from pptx import Presentation
    result = nested_case(tmp_path)
    area = next(row for row in result["stages"][0]["areas"] if row["area_id"] == "data")
    criteria = {row["id"]: row for row in result["input"]["criteria"]}
    expected = [criteria[key] for key in area["implication"]["criterion_refs"]]
    assert expected
    path = tmp_path / "official-authority.pptx"
    asis_artifacts.presentation(__import__('visual_spec_stub').with_visual_specs(result), path)
    pages = [slide for slide in Presentation(path).slides if any(shape.has_text_frame and
        "스토리지·데이터 · 시사점과 적용 판단" in getattr(shape, "text", "") for shape in slide.shapes)]
    actual = "".join("".join(getattr(shape, "text", "").split()) for slide in pages for shape in slide.shapes if shape.has_text_frame)
    for criterion in expected:
        for field in ("quote", "source_url", "modality"):
            assert "".join(criterion[field].split()) in actual, (criterion["id"], field)
    assert "분석상적용해설" in actual
