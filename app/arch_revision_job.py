"""불변 아키텍처 개정 작업. 기존 모델·관리 ID·이미지/네이티브 렌더 자산을 재사용한다."""
import base64
import copy
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import arch_astra as A
import arch_job as J
import arch_facts as F
import arch_render as R
from arch_store import Store
from arch_dual_artifacts import publish_dual_artifacts


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def full_contract(inp):
    """필수 정본 두 본문을 절단 없이 결합하고 실제 사용 해시를 검증한다."""
    p = inp["prompt"]
    common, pattern = p["common"], p["patterns"][inp["diagram"]]
    if not isinstance(common, str) or not common.strip() or not isinstance(pattern, str) or not pattern.strip():
        raise ValueError("canonical_prompt_body_missing")
    return common + "\n\n" + pattern


def previous_inputs(inp):
    """서버가 고정한 직전 개정 경로와 해시만 읽는다. 사용자가 파일 경로를 선택하지 않는다."""
    prev = inp.get("previous") or {}
    directory = prev.get("source_directory")
    if not directory:
        return {}, []
    root = Path(directory)
    texts, images = {}, []
    for name, key in (("spec.json", "spec_sha256"), ("layout.json", "layout_sha256"), ("job.json", "checks_sha256")):
        path = root / name
        if path.is_file():
            raw = path.read_bytes()
            expected = prev.get(key)
            if expected and sha(raw) != expected:
                raise ValueError("previous_revision_hash_changed")
            texts[name] = json.loads(raw.decode("utf-8"))
    path = root / "image.png"
    if path.is_file():
        raw = path.read_bytes()
        expected = prev.get("image_sha256")
        if expected and sha(raw) != expected:
            raise ValueError("previous_image_hash_changed")
        images.append(raw)
    return texts, images


def ask(prompt, images, stage, calls):
    """실제 Azure 응답 봉투를 보존한다. 조용한 다른 모델·제공자 대체는 없다."""
    if J.TEXT_URL != A.EXPECTED_URL or J.TEXT_MODEL != A.EXPECTED_MODEL:
        raise ValueError("architecture_route_drift")
    content = [{"type": "input_text", "text": prompt}]
    for raw in images:
        content.append({"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(raw).decode("ascii")})
    body = {"model": A.EXPECTED_MODEL, "input": [{"role": "user", "content": content}],
            "text": {"format": {"type": "json_object"}}, "store": False}
    response = J._post(A.EXPECTED_URL, canonical(body), "application/json", 600)
    if not isinstance(response, dict):
        raise ValueError("architecture_response_not_object")
    model, ident, status = response.get("model"), response.get("id"), response.get("status")
    calls.append({"stage": stage, "requested_model": A.EXPECTED_MODEL, "actual_model": model,
                  "response_id": ident, "response_status": status, "prompt_sha256": sha(prompt.encode("utf-8")),
                  "image_sha256": [sha(raw) for raw in images], "request_sha256": sha(canonical(body)),
                  "route": "VM existing arch_job IMDS private Responses", "fallback_used": False})
    if model != A.EXPECTED_MODEL or status != "completed" or not isinstance(ident, str) or not ident.startswith("resp_"):
        raise ValueError("architecture_response_envelope_rejected")
    value = json.loads(J._out_text(response))
    if not isinstance(value, dict) or A.has_secret_field(value):
        raise ValueError("architecture_output_rejected")
    return value


def run(job_dir):
    jd = Path(job_dir)
    inp = json.loads((jd / "input.json").read_text(encoding="utf-8"))
    aid, state, diagram, rev = inp["assessment_id"], inp["state"], inp["diagram"], inp["revision_id"]
    project = jd.parents[2]
    store = Store(project.parent)
    calls, checks, metadata = [], {}, None
    status = {"status": "running", "revision_id": rev, "pid": os.getpid(), "beat": time.time(), "steps": []}
    original_token = J._token
    identity = {}

    def guarded_token():
        token = original_token()
        parts = token.split(".")
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        if claims.get("aud", "").rstrip("/") != "https://cognitiveservices.azure.com" or claims.get("xms_mirid", "").lower() != A.EXPECTED_VM:
            raise ValueError("managed_identity_guard")
        identity.update({key: claims.get(key) for key in ("aud", "xms_mirid", "oid", "tid")})
        return token

    def step(name, value, detail=""):
        row = next((x for x in status["steps"] if x["name"] == name), None)
        if row is None:
            row = {"name": name, "label": name}
            status["steps"].append(row)
        row.update(status=value, detail=detail)
        status["beat"] = time.time()
        J._atomic(jd / "job.json", status)

    J._token = guarded_token
    try:
        contract = full_contract(inp)
        previous, prior_images = previous_inputs(inp)
        topo, evidence, path = F.load(inp["case_dir"])
        facts_obj = F.Facts(topo, evidence)
        base_fnd = F.findings(facts_obj)
        base_tks = F.tasks(facts_obj, inp["track"], base_fnd)
        data_root = project.parent.parent if len(project.parents) >= 2 and project.parent.name == "arch" else None
        reps = J._load_project_reports(data_root, aid)
        fnd_enriched, tks_enriched = F.enrich_from_reports(base_fnd, base_tks, reps["current_state"], reps["integrated"])
        task_path = project / "tobe-tasks.json"
        tasks = None
        if task_path.is_file():
            tasks = json.loads(task_path.read_text(encoding="utf-8")).get("tasks")
        elif reps["integrated"] or reps["current_state"]:
            J._atomic(task_path, {"source": "AS-IS 현황분석 및 종합 시사점·개선과제 보고서 연동", "findings": fnd_enriched, "tasks": tks_enriched})
            tasks = tks_enriched
        sk = F.skeleton(topo, evidence, inp["track"], state, diagram, tasks if state == "TO-BE" else None, fnd_enriched)
        frozen = copy.deepcopy(sk)
        facts_sha = sha(canonical(frozen))
        status["frozen_facts_sha256"] = facts_sha
        J._atomic(jd / "frozen-facts.json", frozen)
        step("spec", "running")
        prompt = (contract + "\n\nADDITIONAL REQUIREMENTS (scoped to this revision, not global):\n" + inp.get("requirements", "")
                  + "\nPREVIOUS RESULT JSON (preserve valid content):\n" + json.dumps(previous, ensure_ascii=False)
                  + "\nFROZEN CODE SKELETON:\n" + json.dumps(frozen, ensure_ascii=False)
                  + "\nReturn JSON summary, reasons (each exact node id), image_brief (complete detailed drawing specification, no word cap), requirements_assessment {kind,accepted,rationale}. "
                    "Do not add/remove/rename/recount facts or adopt tasks. AS-IS facts cannot change. If requested facts conflict or need unapproved planned resources, accepted=false. "
                    "Explain observation, evidence, customer impact, tradeoffs and limitations professionally in Korean. image_brief must cover every frame, label, CIDR, count, relationship, direction, annotation, legend, table/border/font detail; never summarize required objects away.")
        J._atomic(jd / "effective-spec.prompt.txt", prompt.encode("utf-8"))
        result = ask(prompt, prior_images, "spec", calls)
        A.validate_output({"stage": "spec", "skeleton": frozen}, {key: result[key] for key in ("summary", "reasons", "image_brief")})
        assessment = result.get("requirements_assessment") or {}
        if inp.get("requirements") and assessment.get("accepted") is not True:
            J._atomic(jd / "requirements-assessment.json", assessment)
            raise ValueError("requirements_need_confirmation_or_conflict_with_facts")
        sk["summary"] = result["summary"]
        for n in sk["nodes"]:
            n["reason_text"] = result["reasons"][n["id"]]
        sk["md"] = J.spec_md(sk) + "\n## 상세 작도 명세\n" + result["image_brief"] + "\n"
        J._atomic(jd / "spec.json", sk)
        J._atomic(jd / "spec.md", sk["md"].encode("utf-8"))
        step("spec", "ok")
        image_prompt = contract + "\n" + sk["md"] + "\nScoped requirements:\n" + inp.get("requirements", "")
        J._atomic(jd / "effective-image.prompt.txt", image_prompt.encode("utf-8"))
        refs = [(p.name, p.read_bytes(), "image/jpeg") for p in J.SAMPLES]
        refs += [("previous.png", raw, "image/png") for raw in prior_images]
        step("image", "running")
        image = J.draw_image(image_prompt, refs, on_wait=lambda detail: step("image", "running", detail))
        J._atomic(jd / "image.png", image)
        calls.append({"stage": "image", "requested_model": J.IMG_MODEL, "actual_model": None,
                      "prompt_sha256": sha(image_prompt.encode("utf-8")), "returned_image_sha256": sha(image),
                      "stored_image_sha256": sha((jd / "image.png").read_bytes()), "fallback_used": False})
        step("image", "ok")
        step("read", "running")
        read_text = contract + "\n" + J.read_prompt(frozen)
        J._atomic(jd / "effective-read.prompt.txt", read_text.encode("utf-8"))
        layout = ask(read_text, [image], "read", calls)
        A.validate_output({"stage": "read", "skeleton": frozen}, layout)
        J._atomic(jd / "layout.json", layout)
        step("read", "ok")
        step("pptx", "running")
        facts = F.Facts(topo, evidence)
        boxes = (R.layout_d1 if diagram == "D1" else R.layout_d2)(sk, layout)
        warnings = []
        pptx, icons_used = R.build_pptx(sk, boxes, facts.res, warnings)
        checks, icons = R.check(sk, boxes, pptx, layout, icons_used, warnings)
        J._atomic(jd / "diagram.pptx", pptx)
        step("pptx", "ok")
        step("render", "running")
        metadata = publish_dual_artifacts(jd.parent, image, pptx, revision_id=rev,
                                         pptx_revision_id=rev, source_image_sha256=sha(image), source_model=J.IMG_MODEL,
                                         assessment_id=aid, state=state, diagram=diagram, allowed_root=project.parent,
                                         soffice=os.environ.get("AICAF_SOFFICE_PATH") or None)
        step("render", "ok" if metadata["render_status"] == "ready" else "failed")
        status.update(checks=checks, icons=icons)
        passed = bool(checks) and all(x["pass"] for x in checks.values()) and metadata["render_status"] == "ready"
        status.update(status="done" if passed else "failed", error="" if passed else "checks_or_actual_render_failed", finished=time.time())
        store.finish(aid, state, diagram, rev, status["status"], checks, metadata, status["error"])
    except Exception as exc:
        status.update(status="failed", error=type(exc).__name__ + ": " + (str(exc) if isinstance(exc, ValueError) else "revision_generation_failed"))
        store.finish(aid, state, diagram, rev, "failed", checks, metadata or {}, status["error"])
    finally:
        J._token = original_token
        J._atomic(jd / "provenance.json", {"revision_id": rev, "assessment_id": aid,
                  "prompt": {k: inp["prompt"].get(k) for k in ("id", "version", "sha256")},
                  "input_sha256": sha((jd / "input.json").read_bytes()), "identity": identity, "calls": calls,
                  "model_original_unchanged": (jd / "image.png").is_file(), "no_fallback": True})
        status["beat"] = time.time()
        J._atomic(jd / "job.json", status)


if __name__ == "__main__":
    run(sys.argv[1])
