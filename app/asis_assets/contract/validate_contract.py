"""설계 문서의 프롬프트·스키마·참조 검증 패키지를 생성하고 시험한다."""
from __future__ import annotations
import copy
import hashlib
import json
import re
import sqlite3
import unittest
from pathlib import Path
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent
PACK = ROOT
RUN = {
    "type": "object", "additionalProperties": False,
    "required": ["assessment_id", "case_id", "track", "source_revision", "analysis_revision", "source_bundle_sha256"],
    "properties": {
        "assessment_id": {"type": "integer", "minimum": 1}, "case_id": {"type": "string", "minLength": 1},
        "track": {"enum": ["new", "existing"]}, "source_revision": {"type": "string", "minLength": 1},
        "analysis_revision": {"type": "string", "minLength": 1},
        "source_bundle_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    },
}
STR = {"type": "string", "minLength": 1}
REF = {"type": "array", "items": STR, "uniqueItems": True}


def obj(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "additionalProperties": False, "properties": properties,
            "required": required if required is not None else list(properties)}


def array(item: dict) -> dict:
    return {"type": "array", "items": item}


def refs(minimum: int = 0) -> dict:
    return {**REF, "minItems": minimum}


UNRESOLVED = obj({"topic": STR, "reason": STR, "required_evidence": STR, "fact_refs": refs()})
BASE_REFS = {"fact_refs": refs(1), "criterion_refs": refs(), "goal_refs": refs()}
OBS = obj({"id": STR, "text": STR, **BASE_REFS, "judgment_refs": refs()})
IMPLICATION = obj({"id": STR, "text": STR, "impact": STR, "change_direction": STR,
                   **BASE_REFS, "criterion_refs": refs(1), "judgment_refs": refs()})
DIRECTION = obj({"id": STR, "title": STR, "text": STR, "customer_impact": STR, "change_direction": STR,
                 "area_implication_refs": refs(1), **BASE_REFS, "criterion_refs": refs(1)})
DIAGNOSIS = obj({"id": STR, "text": STR, "impact": STR, "direction_refs": refs(1),
                 **BASE_REFS, "criterion_refs": refs(1)})
ACTION = obj({"id": STR, "title": STR, "target": STR, "change_type": {"enum": ["modify", "add", "retain", "verify"]},
              "rationale": STR, "diagnosis_refs": refs(1), "direction_refs": refs(1), **BASE_REFS,
              "criterion_refs": refs(1), "prerequisites": refs(), "deliverable": STR,
              "verification_condition": STR, "priority_level": {"enum": ["first", "next", "later", "pending"]},
              "priority_basis": STR, "improvement_points": refs(),
              "detailed_implementation_ref": {"type": ["string", "null"]}})
EDGE = obj({"from_id": STR, "to_id": STR, "relation": {"enum": ["supports", "consolidates", "diagnoses", "addresses"]}})


def output_schema() -> dict:
    common = {"run": RUN, "unresolved": array(UNRESOLVED)}
    variants = []
    def variant(stage: str, fields: dict) -> None:
        variants.append(obj({"stage": {"const": stage}, **common, **fields}))
    variant("area", {"areas": array(obj({"area_id": STR, "headline": STR, "observations": array(OBS), "implication": IMPLICATION}))})
    variant("synthesis", {"directions": array(DIRECTION), "edges": array(EDGE)})
    variant("actions", {"diagnoses": array(DIAGNOSIS), "actions": array(ACTION), "edges": array(EDGE)})
    variant("summary", {"summary": obj({"headline": STR, "message": STR,
                                         "key_points": array(obj({"id": STR, "text": STR, **BASE_REFS})),
                                         "direction_refs": refs(), "action_refs": refs(), "decision_points": refs()})})
    variant("review", {"verdict": {"enum": ["pass", "revise", "blocked"]}, "content_checked": {"type": "boolean"},
                         "render_checked": {"type": "boolean"},
                         "defects": array(obj({"where": {"type": "string", "pattern": "^/"}, "quote": STR,
                                               "why": STR, "fix": STR, "evidence_refs": refs()}))})
    variant("architecture", {"handoff": obj({"node_refs": refs(), "edge_refs": refs(), "layout_requirements": refs(),
                                              "unknown_links": array(UNRESOLVED),
                                              "detailed_image_ref": {"type": ["string", "null"]},
                                              "native_ppt_ref": {"type": ["string", "null"]},
                                              "actual_ppt_preview_ref": {"type": ["string", "null"]}})})
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "urn:ai-assessment:asis:output:r2",
            "description": "신규 작성 계약 설계안. 현행 운영 API 또는 실행 결과를 뜻하지 않는다.", "oneOf": variants}


def input_schema() -> dict:
    fact = obj({"id": STR, "source_id": STR, "field_path": STR, "run": RUN,
                "value": {}, "collection_status": {"enum": ["collected", "not_present", "not_collected", "permission_denied", "collection_failed"]},
                "scope_complete": {"type": "boolean"}})
    criterion = obj({"id": STR, "quote": STR, "source_url": {"type": "string", "pattern": "^https://"},
                     "source_snapshot_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                     "checked_at": STR, "modality": {"enum": ["must", "should", "consider", "informational"]}})
    judgment = obj({"id": STR, "status": {"enum": ["compliant", "partial", "gap", "unknown", "not_applicable", "ready", "partially_ready", "not_ready"]},
                    "fact_refs": refs(), "criterion_refs": refs(), "applicability_confirmed": {"type": "boolean"}})
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "urn:ai-assessment:asis:input:r2",
            "description": "신규 계약의 설계안이며 배포된 운영 API 형태를 뜻하지 않는다.",
            **obj({"run": RUN, "context": {"type": "object"}, "goals": array(obj({"id": STR, "source_id": STR, "quote": STR})),
                    "facts": array(fact), "measurements": array(obj({"id": STR, "value": {"type": "number"}, "unit": STR, "fact_refs": refs(1)})),
                    "criteria": array(criterion), "judgments": array(judgment),
                    "allocated_ids": {"type": "object", "additionalProperties": refs()}, "stage_data": {"type": "object"}},
                   ["run", "context", "goals", "facts", "measurements", "criteria", "judgments", "allocated_ids", "stage_data"])}


def dump(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def walk(value: object):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def strict_load(text: str) -> object:
    def no_constant(value: str):
        raise ValueError("non_finite_json:" + value)
    return json.loads(text, parse_constant=no_constant)


def pointer(document: object, path: str) -> object:
    item = document
    for key in path.lstrip("/").split("/"):
        key = key.replace("~1", "/").replace("~0", "~")
        item = item[int(key)] if isinstance(item, list) else item[key]
    return item


def validate_input(data: dict) -> None:
    Draft202012Validator(input_schema()).validate(data)
    for fact in data["facts"]:
        if fact["run"] != data["run"]:
            raise ValueError("cross_run_fact:" + fact["id"])
        if fact["collection_status"] in ("not_collected", "permission_denied", "collection_failed") and fact["value"] is not None:
            raise ValueError("unknown_value_must_be_null:" + fact["id"])
        if fact["collection_status"] == "not_present" and not fact["scope_complete"]:
            raise ValueError("unproven_absence:" + fact["id"])
    for key in ("facts", "goals", "criteria", "judgments", "measurements"):
        ids = [item["id"] for item in data[key]]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate_input_id:" + key)
    facts = {item["id"] for item in data["facts"]}
    criteria = {item["id"] for item in data["criteria"]}
    for measurement in data["measurements"]:
        if set(measurement["fact_refs"]) - facts:
            raise ValueError("measurement_fact_missing")
        referenced = [fact for fact in data["facts"] if fact["id"] in measurement["fact_refs"]]
        if any(fact["collection_status"] not in ("collected", "not_present") for fact in referenced):
            raise ValueError("measurement_from_unknown_fact")
    for judgment in data["judgments"]:
        if set(judgment["fact_refs"]) - facts or set(judgment["criterion_refs"]) - criteria:
            raise ValueError("judgment_reference_missing")
    allocated = [item for items in data["allocated_ids"].values() for item in items]
    if len(allocated) != len(set(allocated)):
        raise ValueError("duplicate_allocated_id")
    for previous in walk(data["stage_data"]):
        if "stage" in previous and "run" in previous and previous["run"] != data["run"]:
            raise ValueError("cross_run_previous_stage")


def validate_output(data: dict, result: dict) -> None:
    validate_input(data)
    Draft202012Validator(output_schema()).validate(result)
    if result["run"] != data["run"]:
        raise ValueError("cross_run_output")
    source_sets = {"fact_refs": {x["id"] for x in data["facts"]}, "criterion_refs": {x["id"] for x in data["criteria"]},
                   "goal_refs": {x["id"] for x in data["goals"]}, "judgment_refs": {x["id"] for x in data["judgments"]}}
    source_sets["evidence_refs"] = set().union(*source_sets.values())
    allocated = {x for values in data["allocated_ids"].values() for x in values}
    previous = {node["id"] for node in walk(data["stage_data"]) if "id" in node and isinstance(node["id"], str)}
    current_ids = []
    for node in walk(result):
        for key, allowed in source_sets.items():
            if key in node and set(node[key]) - allowed:
                raise ValueError("invalid_reference:" + key)
        if "id" in node:
            if node["id"] not in allocated:
                raise ValueError("unallocated_output_id:" + node["id"])
            current_ids.append(node["id"])
    if len(current_ids) != len(set(current_ids)):
        raise ValueError("duplicate_output_id")
    if result["stage"] == "area":
        expected_areas = {slot["area_id"] for slot in data["stage_data"].get("area_slots", [])}
        actual_areas = [area["area_id"] for area in result["areas"]]
        if set(actual_areas) != expected_areas or len(actual_areas) != len(set(actual_areas)):
            raise ValueError("area_slot_coverage_mismatch")
    all_ids = previous | set(current_ids) | set.union(*source_sets.values())
    for node in walk(result):
        for key in ("direction_refs", "action_refs", "diagnosis_refs", "area_implication_refs"):
            if key in node and set(node[key]) - all_ids:
                raise ValueError("invalid_relation_reference:" + key)
    for edge in result.get("edges", []):
        if edge["from_id"] not in all_ids or edge["to_id"] not in all_ids:
            raise ValueError("dangling_edge")
    expected_edges = set()
    if result["stage"] == "synthesis":
        for direction in result["directions"]:
            expected_edges.update((fact, direction["id"], "supports") for fact in direction["fact_refs"])
            expected_edges.update((item, direction["id"], "consolidates") for item in direction["area_implication_refs"])
    elif result["stage"] == "actions":
        for diagnosis in result["diagnoses"]:
            expected_edges.update((item, diagnosis["id"], "diagnoses") for item in diagnosis["direction_refs"])
        for action in result["actions"]:
            expected_edges.update((item, action["id"], "addresses") for item in action["diagnosis_refs"])
    if result["stage"] in ("synthesis", "actions"):
        actual_edges = [(edge["from_id"], edge["to_id"], edge["relation"]) for edge in result["edges"]]
        if len(actual_edges) != len(set(actual_edges)) or set(actual_edges) != expected_edges:
            raise ValueError("edge_reference_mismatch")
    if result["stage"] == "review":
        if result["verdict"] == "pass" and (result["defects"] or result["unresolved"]):
            raise ValueError("pass_with_open_defects")
        render_record = data["stage_data"].get("deterministic_check_result", {}).get("actual_render", {})
        if result["render_checked"] and (not render_record.get("verified") or not render_record.get("artifact_sha256") or not render_record.get("preview_sha256")):
            raise ValueError("render_claim_without_binding")
        for defect in result["defects"]:
            literal = pointer(data["stage_data"]["draft"], defect["where"])
            if defect["quote"] not in (literal if isinstance(literal, str) else json.dumps(literal, ensure_ascii=False)):
                raise ValueError("fabricated_review_quote")
    if result["stage"] == "architecture":
        topology = data["stage_data"].get("topology", {})
        for key, ref_key in (("nodes", "node_refs"), ("edges", "edge_refs")):
            valid = {item["id"] for item in topology.get(key, [])}
            if set(result["handoff"][ref_key]) - valid:
                raise ValueError("fabricated_topology:" + ref_key)


def render_prompt(stage: int, data: dict) -> tuple[str, str]:
    validate_input(data)
    system = (PACK / "prompts" / "P0-system.txt").read_text(encoding="utf-8")
    template = (PACK / "prompts" / f"P{stage}-stage.txt").read_text(encoding="utf-8")
    marker = "{{stage_input_json}}"
    if template.count(marker) != 1:
        raise ValueError("invalid_template_marker")
    return system, template.replace(marker, json.dumps(data, ensure_ascii=False, allow_nan=False))


def synthetic_fixture() -> tuple[dict, dict]:
    # 가상 계약 시험 전용이다. 고객 원장·보고서나 실제 모델 반환이 아니다.
    run = {"assessment_id": 999999, "case_id": "SYNTHETIC_CONTRACT_ONLY", "track": "new", "source_revision": "test-source",
           "analysis_revision": "test-analysis", "source_bundle_sha256": "a" * 64}
    data = {"run": run, "context": {"synthetic_contract_only": True}, "goals": [{"id": "G1", "source_id": "fixture", "quote": "계약 시험"}],
            "facts": [{"id": "F1", "source_id": "fixture", "field_path": "/fixture/count", "run": copy.deepcopy(run), "value": 7, "collection_status": "collected", "scope_complete": True}],
            "measurements": [{"id": "M1", "value": 7, "unit": "count", "fact_refs": ["F1"]}],
            "criteria": [{"id": "C1", "quote": "합성 계약 시험 기준", "source_url": "https://example.invalid/fixture", "source_snapshot_sha256": "b" * 64,
                           "checked_at": "synthetic", "modality": "consider"}],
            "judgments": [{"id": "J1", "status": "unknown", "fact_refs": ["F1"], "criterion_refs": ["C1"], "applicability_confirmed": False}],
            "allocated_ids": {"observations": ["O1"], "area_implications": ["I1"]}, "stage_data": {"area_slots": [{"area_id": "fixture"}]}}
    base = {"fact_refs": ["F1"], "criterion_refs": ["C1"], "goal_refs": ["G1"], "judgment_refs": ["J1"]}
    output = {"stage": "area", "run": run, "areas": [{"area_id": "fixture", "headline": "합성 계약 시험", "observations": [{"id": "O1", "text": "계약 시험 값 7", **base}],
                                                       "implication": {"id": "I1", "text": "합성 시험 설명", "impact": "합성 시험", "change_direction": "확인", **base}}], "unresolved": []}
    return data, output


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.data, self.output = synthetic_fixture()
    def test_positive(self):
        validate_output(self.data, self.output)
    def test_schema_definitions(self):
        Draft202012Validator.check_schema(input_schema())
        Draft202012Validator.check_schema(output_schema())
    def test_all_stage_templates(self):
        for stage in range(1, 7):
            system, prompt = render_prompt(stage, self.data)
            self.assertTrue(system and prompt)
            self.assertNotIn("{{stage_input_json}}", prompt)
            self.assertIn("SYNTHETIC_CONTRACT_ONLY", prompt)
    def test_unknown_fact(self):
        self.output["areas"][0]["observations"][0]["fact_refs"] = ["missing"]
        with self.assertRaises(ValueError): validate_output(self.data, self.output)
    def test_unknown_criterion(self):
        self.output["areas"][0]["implication"]["criterion_refs"] = ["missing"]
        with self.assertRaises(ValueError): validate_output(self.data, self.output)
    def test_unknown_goal(self):
        self.output["areas"][0]["implication"]["goal_refs"] = ["missing"]
        with self.assertRaises(ValueError): validate_output(self.data, self.output)
    def test_output_run_changed(self):
        self.output = copy.deepcopy(self.output)
        self.output["run"]["assessment_id"] = 1
        with self.assertRaises(ValueError): validate_output(self.data, self.output)
    def test_fact_run_changed(self):
        self.data = copy.deepcopy(self.data)
        self.data["facts"][0]["run"]["source_revision"] = "old"
        with self.assertRaises(ValueError): validate_input(self.data)
    def test_unallocated_id(self):
        self.output["areas"][0]["observations"][0]["id"] = "UNALLOCATED"
        with self.assertRaises(ValueError): validate_output(self.data, self.output)
    def test_boolean_measurement(self):
        self.data["measurements"][0]["value"] = True
        with self.assertRaises(Exception): validate_input(self.data)
    def test_unknown_not_zero(self):
        self.data["facts"][0]["collection_status"] = "permission_denied"
        self.data["facts"][0]["value"] = 0
        with self.assertRaises(ValueError): validate_input(self.data)
    def test_unknown_null_allowed(self):
        self.data["facts"][0]["collection_status"] = "not_collected"
        self.data["facts"][0]["value"] = None
        self.data["measurements"] = []
        validate_input(self.data)
    def test_unproven_absence(self):
        self.data["facts"][0]["collection_status"] = "not_present"
        self.data["facts"][0]["scope_complete"] = False
        with self.assertRaises(ValueError): validate_input(self.data)
    def test_non_finite(self):
        with self.assertRaises(ValueError): strict_load('{"value":NaN}')
    def test_additional_field(self):
        self.output["fake_customer_fact"] = 100
        with self.assertRaises(Exception): validate_output(self.data, self.output)
    def test_duplicate_input_id(self):
        self.data["facts"].append(copy.deepcopy(self.data["facts"][0]))
        with self.assertRaises(ValueError): validate_input(self.data)
    def test_fabricated_review_quote(self):
        self.data["stage_data"]["draft"] = {"headline": "검토할 실제 문장"}
        output = {"stage": "review", "run": self.data["run"], "verdict": "revise", "content_checked": True, "render_checked": False,
                  "defects": [{"where": "/headline", "quote": "원문에 없는 문장", "why": "시험", "fix": "시험", "evidence_refs": ["F1"]}], "unresolved": []}
        with self.assertRaises(ValueError): validate_output(self.data, output)
    def test_actual_review_quote(self):
        self.data["stage_data"]["draft"] = {"headline": "검토할 실제 문장"}
        output = {"stage": "review", "run": self.data["run"], "verdict": "revise", "content_checked": True, "render_checked": False,
                  "defects": [{"where": "/headline", "quote": "실제 문장", "why": "시험", "fix": "시험", "evidence_refs": ["F1"]}], "unresolved": []}
        validate_output(self.data, output)
    def test_cross_run_previous_stage(self):
        previous = copy.deepcopy(self.output)
        previous["run"]["source_revision"] = "old"
        self.data["stage_data"]["approved_areas"] = previous
        with self.assertRaises(ValueError): validate_input(self.data)
    def test_measurement_from_unknown(self):
        self.data["facts"][0]["collection_status"] = "not_collected"
        self.data["facts"][0]["value"] = None
        with self.assertRaises(ValueError): validate_input(self.data)
    def test_synthesis_reference_edges(self):
        self.data["stage_data"]["approved_areas"] = copy.deepcopy(self.output)
        self.data["allocated_ids"]["directions"] = ["DIR1"]
        output = {"stage": "synthesis", "run": self.data["run"], "directions": [{"id": "DIR1", "title": "합성 시험",
                  "text": "합성 시험", "customer_impact": "합성 시험", "change_direction": "확인", "area_implication_refs": ["I1"],
                  "fact_refs": ["F1"], "criterion_refs": ["C1"], "goal_refs": ["G1"]}],
                  "edges": [{"from_id": "F1", "to_id": "DIR1", "relation": "supports"},
                            {"from_id": "I1", "to_id": "DIR1", "relation": "consolidates"}], "unresolved": []}
        validate_output(self.data, output)
        output["edges"].pop()
        with self.assertRaises(ValueError): validate_output(self.data, output)
    def test_fabricated_topology(self):
        self.data["stage_data"]["topology"] = {"nodes": [{"id": "NODE1"}], "edges": []}
        output = {"stage": "architecture", "run": self.data["run"], "handoff": {"node_refs": ["FAKE_FIREWALL"],
                  "edge_refs": [], "layout_requirements": ["실제 노드만"], "unknown_links": [], "detailed_image_ref": None,
                  "native_ppt_ref": None, "actual_ppt_preview_ref": None}, "unresolved": []}
        with self.assertRaises(ValueError): validate_output(self.data, output)

    def test_area_slot_not_omitted(self):
        self.output["areas"] = []
        with self.assertRaises(ValueError): validate_output(self.data, self.output)
    def test_render_claim_needs_binding(self):
        output = {"stage": "review", "run": self.data["run"], "verdict": "pass", "content_checked": True,
                  "render_checked": True, "defects": [], "unresolved": []}
        with self.assertRaises(ValueError): validate_output(self.data, output)

    def test_distinct_reference_image_roles(self):
        contract = json.loads((PACK / "contract.json").read_text(encoding="utf-8"))
        images = {item["source_id"]: item for item in contract.get("reference_images", [])}
        expected = {"img_734e5dc9e1cb.png": "fact_finding_to_synthesis", "img_ee77215db7e8.png": "diagnosis_to_action"}
        for source_id, role in expected.items():
            self.assertIn(source_id, images)
            self.assertEqual(images[source_id]["role"], role)
            self.assertTrue(images[source_id]["reference_only"])

    def test_legacy_result_excel_retirement(self):
        contract = json.loads((PACK / "contract.json").read_text(encoding="utf-8"))
        self.assertFalse(contract.get("legacy_wara_result_excel_allowed", True))
        self.assertTrue(contract["retain_collection_data"])
        self.assertTrue({"Assessment-Findings-Report", "Expert-Analysis"}.issubset(set(contract["retired_customer_result_names"])))
        self.assertEqual(set(contract["required_customer_result_roles"]), {"asis_report_pptx", "asis_backdata_xlsx"})

    def test_latest_primary_guidance_recorded(self):
        path = PACK / "guidance-source-index.json"
        self.assertTrue(path.is_file())
        rows = json.loads(path.read_text(encoding="utf-8"))["sources"]
        self.assertTrue({"U138006", "U138008", "U138078", "U138079", "U139691"}.issubset({item["source_id"] for item in rows}))

    def test_requirement_attachment_and_retirement_binding(self):
        requirements = json.loads((PACK / "requirements.json").read_text(encoding="utf-8"))
        rows = {item["id"]: item for item in requirements["requirements"]}
        self.assertTrue({"U138078", "img_734e5dc9e1cb.png"}.issubset(set(rows["R13"]["source_refs"])))
        self.assertTrue({"U138006", "U138008"}.issubset(set(rows["R26"]["source_refs"])))



if __name__ == "__main__":
    unittest.main(verbosity=2)
