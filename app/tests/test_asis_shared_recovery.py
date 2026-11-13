"""공통 미확인 지적을 실제 작성·독립 검토 경로로 복구하는 회귀 시험."""
from collections import Counter
from copy import deepcopy
from pathlib import Path
import asyncio
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_asis_connection import SyntheticRuntime, synthetic_input
from asis_pipeline import Pipeline
import pytest


def test_shared_repair_rejects_a_field_absent_from_the_canonical_item(tmp_path):
    engine = Pipeline(SyntheticRuntime(), tmp_path / "run")
    stage = {"areas": [], "unresolved": [{"topic": "합성 항목", "reason": "합성 사유",
             "required_evidence": "합성 확인 결과", "fact_refs": []}]}
    defect = {"where": "/stages/0/unresolved/0/nonexistent_field", "quote": "합성 값",
              "why": "합성 지적", "fix": "합성 보완", "evidence_refs": []}
    with pytest.raises(RuntimeError, match="area_repair_target_unbound"):
        engine.repair_plan([stage], [defect])


def test_shared_unresolved_repair_preserves_approved_areas_and_replaces_old_wording(tmp_path):
    data = synthetic_input(tmp_path)
    events = []
    old_text = "합성 회귀: 고객용 항목에 내부 생성 지시가 남아 있다."
    new_text = "합성 회귀: 관측값과 보고서의 표시 결과가 일치하는지 확인한다."
    shared = {"topic": "합성 표시 결과 확인", "reason": "합성 시험의 확인 결과가 필요하다.",
              "required_evidence": old_text, "fact_refs": [data["facts"][0]["id"]]}

    class SharedRuntime(SyntheticRuntime):
        async def ask(self, role, message, **kwargs):
            payload = json.loads(message.rsplit("\n입력:\n", 1)[-1])
            if 'stage_data' not in payload:
                return await super().ask(role, message, **kwargs)
            scoped = payload["stage_data"]
            if role in ('author', 'direction_author') and scoped.get("mode") == "area" and scoped.get("area_slots") == []:
                self.calls.append({"role": role, "mode": "shared_unresolved", "input": deepcopy(payload)})
                previous = scoped["prior_output"]
                assert previous["areas"] == []
                assert previous["unresolved"][target_index] == shared
                assert scoped["approved_areas"]["areas"] == seed["stages"][0]["areas"]
                defect = scoped["accumulated_defects"][0]
                assert defect["source_where"] == f"/stages/0/unresolved/{target_index}/required_evidence"
                assert defect["where"] == f"/unresolved/{target_index}/required_evidence"
                assert defect["quote"] == old_text
                output = deepcopy(previous)
                output["unresolved"][target_index]["required_evidence"] = new_text
                return json.dumps(output, ensure_ascii=False), {"test_only": True, "role": role}
            return await super().ask(role, message, **kwargs)

    runtime = SharedRuntime()
    engine = Pipeline(runtime, tmp_path / "run", progress=events.append)
    with asyncio.Runner() as runner:
        seed = runner.run(engine.analyze(data))
        seed["stages"][0]["unresolved"].append(deepcopy(shared))
        target_index = len(seed["stages"][0]["unresolved"]) - 1
        prior_areas = deepcopy(seed["stages"][0]["areas"])
        calls_before = len(runtime.calls)
        defect = {"where": f"/stages/0/unresolved/{target_index}/required_evidence", "quote": old_text,
                  "why": "합성 시험: 공통 미확인 항목을 고객 확인 결과로 표현해야 한다.",
                  "fix": "관측값을 보존하고 고객 확인 결과를 설명한다.", "evidence_refs": [data["facts"][0]["id"]]}
        repaired = runner.run(engine.analyze(data, repair_seed=seed, repair_defects=[defect]))
    assert repaired["stages"][0]["areas"] == prior_areas
    assert repaired["stages"][0]["unresolved"][target_index]["required_evidence"] == new_text
    assert old_text not in [item["required_evidence"] for item in repaired["unresolved"]]
    authors = Counter(call["mode"] for call in runtime.calls[calls_before:] if call["role"] in ('author', 'direction_author'))
    assert authors['shared_unresolved'] == authors['summary'] == 1
    synthesis_authors = [call for call in runtime.calls[calls_before:] if call['role'] in ('author', 'direction_author') and call['mode']=='synthesis']
    assert all(call['input']['stage_data'].get('direction_assignment') for call in synthesis_authors)
    assert authors['synthesis'] == len(repaired['stages'][1]['directions'])
    assert authors['actions'] == len(repaired['stages'][2]['diagnoses']) + len(repaired['stages'][2]['actions'])
    assert any(call["role"] == "content_review" and call["input"]["stage_data"].get("draft", {}).get("areas") == []
               for call in runtime.calls[calls_before:])
    assert repaired["quality_status"] == "content_review_passed"
