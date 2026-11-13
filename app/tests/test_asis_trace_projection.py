"""선언된 사실 연결을 편집 가능한 실제 관계 장표에 전달하는 합성 회귀."""
from pathlib import Path
import json
import sys

from pptx import Presentation
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_artifacts
from test_asis_configuration_delivery import configuration_case


def trace_notes(deck):
    for slide in deck.slides:
        raw = slide.notes_slide.notes_text_frame.text
        if raw.startswith("원장 참조(직접 선언 및 공통 Fact 교집합 파선 구분)\n"):
            yield slide, json.loads(raw.split("\n", 1)[1])


def test_declared_fact_direction_support_survives_without_an_observation(tmp_path):
    result, _ = configuration_case(tmp_path)
    used = {ref for area in result["stages"][0]["areas"] for observation in area["observations"] for ref in observation["fact_refs"]}
    fact = next(row["id"] for row in result["input"]["facts"] if row["id"] not in used)
    action = result["stages"][2]["actions"][0]
    direction = next(row for row in result["stages"][1]["directions"] if row["id"] in action["direction_refs"])
    direction["fact_refs"].append(fact)
    action["fact_refs"].append(fact)
    result["stages"][1]["edges"].append({"from_id": fact, "to_id": direction["id"], "relation": "supports"})
    destination = tmp_path / "direct-support.pptx"
    asis_artifacts.presentation(__import__('visual_spec_stub').with_visual_specs(result), destination)
    relevant = [note for _, note in trace_notes(Presentation(destination)) if note["action_id"] == action["id"]]
    assert relevant
    assert any([fact, direction["id"], False] in note["edges"] for note in relevant), "직접 supports 원장이 있으면 관찰이 없어도 사실 노드를 고립시키지 않는다"


def segment_hits_rectangle(start, end, rectangle):
    """경계 접촉은 제외하고 선분이 다른 노드 내부를 통과하는지 계산한다."""
    left, top, right, bottom = rectangle
    epsilon = 1000
    left += epsilon
    top += epsilon
    right -= epsilon
    bottom -= epsilon
    dx, dy = end[0] - start[0], end[1] - start[1]
    low, high = 0.0, 1.0
    for coefficient, bound in ((-dx, start[0] - left), (dx, right - start[0]),
                               (-dy, start[1] - top), (dy, bottom - start[1])):
        if coefficient == 0:
            if bound < 0:
                return False
        elif coefficient < 0:
            low = max(low, bound / coefficient)
        else:
            high = min(high, bound / coefficient)
        if low > high:
            return False
    return low < high


def test_native_trace_connectors_do_not_disappear_through_unrelated_nodes(tmp_path):
    from pptx.shapes.connector import Connector
    result, _ = configuration_case(tmp_path)
    path = tmp_path / "trace-routes.pptx"
    asis_artifacts.presentation(__import__('visual_spec_stub').with_visual_specs(result), path)
    identifiers = {row["id"] for row in result["input"]["facts"]}
    for stage in result["stages"]:
        for key in ("directions", "diagnoses", "actions"):
            identifiers.update(row["id"] for row in stage.get(key, []))
    for area in result["stages"][0]["areas"]:
        identifiers.add(area["implication"]["id"])
        identifiers.update(row["id"] for row in area["observations"])
    collisions = []
    for page, (slide, _) in enumerate(trace_notes(Presentation(path)), 1):
        nodes = [("".join(shape.text.split()), (shape.left, shape.top, shape.left + shape.width, shape.top + shape.height))
                 for shape in slide.shapes if shape.has_text_frame and "".join(shape.text.split()) in identifiers]
        for shape in slide.shapes:
            if isinstance(shape, Connector):
                for identifier, rectangle in nodes:
                    if segment_hits_rectangle((shape.begin_x, shape.begin_y), (shape.end_x, shape.end_y), rectangle):
                        collisions.append((page, shape.name, identifier))
    assert not collisions, collisions[:12]
