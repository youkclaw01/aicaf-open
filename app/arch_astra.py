"""Agent4 아키텍처용 Foundry 전용 경로. 일반 Copilot 설정과 운영 포털은 변경하지 않는다."""
import argparse
import base64
import datetime
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time

EXPECTED_URL = os.environ.get("AICAF_ARCH_TEXT_URL", "https://<FOUNDRY_ACCOUNT>.cognitiveservices.azure.com/openai/v1/responses")
EXPECTED_MODEL = "gpt-6-astra"
EXPECTED_VM = os.environ.get("AICAF_EXPECTED_VM", "/subscriptions/<SUBSCRIPTION_ID>/resourcegroups/<RESOURCE_GROUP>/providers/microsoft.compute/virtualmachines/<VM_NAME>").lower()
STAGES = ("design", "analysis", "spec", "read")
SSH_ALIAS = os.environ.get("AICAF_APP_VM_ALIAS", "app-vm")
REMOTE_PYTHON = r"C:\AICAF\.venv\Scripts\python.exe"
REMOTE_DIR = "C:/AICAF/tools/agent4-foundry-astra"
REMOTE_TOOL = REMOTE_DIR + "/arch_astra.py"


class BoundaryError(RuntimeError):
    """사용 가능한 결과로 취급하면 안 되는 응답·입력 경계."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def failure(code, detail, provenance=None):
    """원격 오류 본문·토큰은 전파하지 않고 비밀 없는 실패경계를 반환한다."""
    return {"ok": False, "error": {"code": code, "detail": detail}, "provenance": provenance or {}, "fallback_used": False}


def canonical_sha(obj):
    """전달 과정과 무관하게 같은 JSON 입력·원장에 같은 지문을 부여한다."""
    data = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def has_secret_field(obj):
    """자격증명 필드는 프롬프트·결과 파일로 전달하지 않는다."""
    if isinstance(obj, dict):
        return any(str(k).lower() in {"api_key", "access_token", "authorization", "client_secret", "password", "connection_string"}
                   or has_secret_field(v) for k, v in obj.items())
    if isinstance(obj, list):
        return any(has_secret_field(v) for v in obj)
    return False


def validate_input(request):
    """전송 전에 라우팅·고정 골격·채택 과제 원장·판독 이미지의 결합을 확인한다."""
    if not isinstance(request, dict) or type(request.get("schema_version")) is not int or request["schema_version"] != 1:
        raise BoundaryError("input_schema")
    if has_secret_field(request):
        raise BoundaryError("secret_guard")
    if request.get("requested_model") != EXPECTED_MODEL or request.get("endpoint") != EXPECTED_URL:
        raise BoundaryError("route_guard")
    if request.get("stage") not in STAGES:
        raise BoundaryError("stage_guard")
    sk, ledger, bindings = request.get("skeleton"), request.get("task_ledger"), request.get("bindings")
    if not isinstance(sk, dict) or not isinstance(ledger, dict) or not isinstance(bindings, dict):
        raise BoundaryError("input_schema")
    if bindings.get("skeleton_sha256") != canonical_sha(sk) or bindings.get("task_ledger_sha256") != canonical_sha(ledger):
        raise BoundaryError("input_binding")
    if not isinstance(sk.get("nodes"), list) or not isinstance(sk.get("edges"), list) or not isinstance(sk.get("regions"), list):
        raise BoundaryError("input_schema")
    lock = sk.get("lockedTruth")
    if not isinstance(lock, dict):
        raise BoundaryError("input_schema")
    if lock.get("nodeCount") != len(sk["nodes"]) or lock.get("edgeCount") != len(sk["edges"]) or lock.get("regions") != [r["name"] for r in sk["regions"]]:
        raise BoundaryError("frozen_truth_guard")
    node_ids = [n["id"] for n in sk["nodes"]]
    if len(node_ids) != len(set(node_ids)) or any(type(n.get("count")) is not int or n["count"] < 1 for n in sk["nodes"]):
        raise BoundaryError("frozen_truth_guard")
    if not isinstance(ledger.get("tasks"), list) or canonical_sha(ledger["tasks"]) != canonical_sha(sk.get("tasks") or []):
        raise BoundaryError("ledger_binding")
    tids = [t["id"] for t in ledger["tasks"]]
    if len(tids) != len(set(tids)):
        raise BoundaryError("ledger_binding")
    image = None
    if request["stage"] == "read":
        try:
            image = base64.b64decode(request.get("image_base64") or "", validate=True)
        except (ValueError, TypeError):
            raise BoundaryError("image_binding") from None
        if not image or bindings.get("image_sha256") != hashlib.sha256(image).hexdigest():
            raise BoundaryError("image_binding")
        if len(image) > 8 * 1024 * 1024 or not image.startswith(b"\x89PNG\r\n\x1a\n"):
            raise BoundaryError("image_binding")
    elif request.get("image_base64") is not None or bindings.get("image_sha256") is not None:
        raise BoundaryError("image_binding")
    return image


def architecture_prompt(stage, sk):
    """설계 판단·분석은 모델로, 리소스·개수·관계·채택 과제는 전달된 코드 골격으로 고정한다."""
    if stage == "design":
        schema = ("task_assessments: an array with exactly one entry per supplied adopted task. Each entry has task_id, "
                  "node_ids (only supplied node IDs affected by that task), rationale, verification. No tasks means an empty array. "
                  "Assess the supplied TO-BE design; do not create or approve another task, resource, region or connection.")
    else:
        schema = ("observations: an array with exactly one entry per supplied finding. Each entry has finding_id, "
                  "node_ids (only supplied nodes with that finding's evidence), observation, implication, verification. "
                  "No findings means an empty array. Distinguish configuration evidence from observed runtime traffic.")
    return (f"ARCHITECTURE_STAGE: {stage}\n"
            "You are an Azure architecture consultant. Write compact professional Korean prose grounded ONLY in the supplied fixed skeleton. "
            "Counts, containment, resource names, regions, evidence and relationships are fixed by code. You may not change them. "
            "Planned TO-BE components are not deployed customer resources; configuration does not prove observed runtime traffic. "
            "Do not invent prices, metrics, workload telemetry or new services. Return only a JSON object with summary (a string), "
            "limitations (an array of short strings), and " + schema + "\nFIXED_SKELETON:\n" + json.dumps(sk, ensure_ascii=False))


def prompt_for(request, job):
    """서비스의 기존 명세·판독 문법은 그대로 재사용한다."""
    stage, sk = request["stage"], request["skeleton"]
    if stage == "spec":
        return job.spec_prompt(sk)
    if stage == "read":
        return job.read_prompt(sk)
    return architecture_prompt(stage, sk)


def validate_output(request, payload):
    """Astra 문구·판독 결과를 사실 원장과 분리하고, 임의 구조·과제·노드 참조는 차단한다."""
    if not isinstance(payload, dict):
        raise BoundaryError("output_schema")
    if has_secret_field(payload):
        raise BoundaryError("secret_guard")
    stage, sk = request["stage"], request["skeleton"]
    node_ids = {n["id"] for n in sk["nodes"]}
    required = {"spec": {"summary", "reasons", "image_brief"}, "read": {"nodes", "extras"},
                "design": {"summary", "limitations", "task_assessments"}, "analysis": {"summary", "limitations", "observations"}}[stage]
    if stage != "read" and set(payload) & {"nodes", "containers", "edges", "regions", "lockedTruth", "tasks"}:
        raise BoundaryError("output_fact_guard")
    if set(payload) != required:
        raise BoundaryError("output_schema")
    if stage != "read" and (not isinstance(payload["summary"], str) or not payload["summary"].strip()):
        raise BoundaryError("output_schema")
    if stage == "spec":
        reasons = payload["reasons"]
        if not isinstance(reasons, dict) or set(reasons) != node_ids:
            raise BoundaryError("output_fact_guard")
        if not all(isinstance(x, str) and x.strip() for x in reasons.values()) or not isinstance(payload["image_brief"], str) or not payload["image_brief"].strip():
            raise BoundaryError("output_schema")
    elif stage == "read":
        nodes, extras = payload["nodes"], payload["extras"]
        if not isinstance(nodes, list) or any(not isinstance(n, dict) for n in nodes) or [n.get("id") for n in nodes].count(None):
            raise BoundaryError("layout_guard")
        if len(nodes) != len(node_ids) or {n["id"] for n in nodes} != node_ids:
            raise BoundaryError("layout_guard")
        for n in nodes:
            if set(n) != {"id", "found", "bbox", "seen_label"} or type(n["found"]) is not bool or not isinstance(n["seen_label"], str):
                raise BoundaryError("layout_guard")
            bb = n["bbox"]
            if n["found"]:
                if not isinstance(bb, list) or len(bb) != 4 or not all(type(x) in (int, float) and math.isfinite(x) and 0 <= x <= 1 for x in bb):
                    raise BoundaryError("layout_guard")
                if bb[2] <= 0 or bb[3] <= 0 or bb[0] + bb[2] > 1.000001 or bb[1] + bb[3] > 1.000001:
                    raise BoundaryError("layout_guard")
            elif bb is not None:
                raise BoundaryError("layout_guard")
        if not isinstance(extras, list) or any(not isinstance(e, dict) or set(e) != {"label", "kind"} or not isinstance(e["label"], str)
                                              or e["kind"] not in {"service", "container", "annotation"} for e in extras):
            raise BoundaryError("layout_guard")
        if any(e["kind"] == "service" for e in extras):
            raise BoundaryError("layout_extra_guard")
    else:
        if not isinstance(payload["limitations"], list) or not all(isinstance(x, str) for x in payload["limitations"]):
            raise BoundaryError("output_schema")
        design = stage == "design"
        key, ref = ("task_assessments", "task_id") if design else ("observations", "finding_id")
        code = "ledger_reference_guard" if design else "finding_reference_guard"
        rows = payload[key]
        expected = {x["id"] for x in (request["task_ledger"]["tasks"] if design else sk.get("findings") or [])}
        if not isinstance(rows, list) or any(not isinstance(x, dict) for x in rows) or len(rows) != len(expected) or {x.get(ref) for x in rows} != expected:
            raise BoundaryError(code)
        fields = {ref, "node_ids", "rationale", "verification"} if design else {ref, "node_ids", "observation", "implication", "verification"}
        for row in rows:
            if set(row) != fields or not isinstance(row["node_ids"], list) or any(x not in node_ids for x in row["node_ids"]):
                raise BoundaryError(code)
            if not all(isinstance(row[k], str) and row[k].strip() for k in fields - {ref, "node_ids"}):
                raise BoundaryError("output_schema")


def execute(request, job):
    """기존 ask_json·spec_prompt·관리 ID 전송을 전용 프로세스에서 사용한다. 원본 파일은 변경하지 않는다."""
    if job.TEXT_URL != EXPECTED_URL or job.TEXT_MODEL != EXPECTED_MODEL:
        return failure("route_guard", "approved Foundry route mismatch")
    try:
        image = validate_input(request)
    except BoundaryError as e:
        return failure(e.code, "input boundary rejected before network")
    except (KeyError, TypeError, ValueError):
        return failure("input_schema", "invalid input shape")
    prompt = prompt_for(request, job)
    source = Path(job.__file__)
    provenance = {"stage": request["stage"], "provider": "azure-foundry", "requested_model": EXPECTED_MODEL, "endpoint": EXPECTED_URL,
                  "call_module": str(source), "call_module_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "python": sys.executable,
                  "input_sha256": canonical_sha(request), "bindings": dict(request["bindings"]), "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                  "auth_mechanism": "existing_arch_job_IMDS"}
    original, original_token = job._post, job._token

    def capture_token():
        token = original_token()
        try:
            part = token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
            if claims.get("aud", "").rstrip("/") != "https://cognitiveservices.azure.com" or claims.get("xms_mirid", "").lower() != EXPECTED_VM:
                raise BoundaryError("identity_guard")
        except (IndexError, ValueError, TypeError, AttributeError):
            raise BoundaryError("identity_guard") from None
        # JWT 자체와 Authorization 헤더는 출력·파일·실행 인자로 보내지 않는다.
        provenance["identity"] = {k: claims.get(k) for k in ("aud", "xms_mirid", "oid", "tid")}
        provenance["identity_verified"] = True
        provenance["token_acquisitions"] = provenance.get("token_acquisitions", 0) + 1
        return token

    def capture(url, data, ctype, timeout):
        if url != EXPECTED_URL or ctype != "application/json":
            raise BoundaryError("route_guard")
        body = json.loads(data.decode("utf-8"))
        if body.get("model") != EXPECTED_MODEL:
            raise BoundaryError("route_guard")
        provenance["request_body_sha256"] = hashlib.sha256(data).hexdigest()
        provenance["posted_model"] = body["model"]
        response = original(url, data, ctype, timeout)
        provenance.update(response_id=response.get("id"), actual_model=response.get("model"), response_status=response.get("status"), usage=response.get("usage"))
        if response.get("model") != EXPECTED_MODEL:
            raise BoundaryError("response_model_guard")
        if response.get("status") != "completed":
            raise BoundaryError("response_incomplete")
        if not isinstance(response.get("id"), str) or not re.fullmatch(r"resp_[A-Za-z0-9_-]+", response["id"]):
            raise BoundaryError("response_id_guard")
        return response

    start = time.monotonic()
    job._post = capture
    job._token = capture_token
    try:
        payload = job.ask_json(prompt, image)
        validate_output(request, payload)
        provenance["output_sha256"] = canonical_sha(payload)
    except BoundaryError as e:
        return failure(e.code, "Foundry response boundary rejected", provenance)
    except json.JSONDecodeError:
        return failure("invalid_json", "Foundry response is not valid JSON", provenance)
    except Exception as e:
        return failure("foundry_error", type(e).__name__, provenance)
    finally:
        job._post = original
        job._token = original_token
        provenance["seconds"] = round(time.monotonic() - start, 3)
    return {"ok": True, "output_json": payload, "provenance": provenance, "fallback_used": False}


def call_remote(request, runner=None):
    """Mac의 일반 오케스트레이터는 키 없이 SSH로 전용 VM 프로세스에 JSON만 전달한다."""
    try:
        validate_input(request)
    except BoundaryError as e:
        return failure(e.code, "input boundary rejected before SSH")
    except (KeyError, TypeError, ValueError):
        return failure("input_schema", "invalid input shape")
    tool_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    command = f'"{REMOTE_PYTHON}" -B "{REMOTE_TOOL}" worker --expected-tool-sha {tool_sha}'
    argv = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", SSH_ALIAS, command]
    try:
        r = (runner or subprocess.run)(argv, input=json.dumps(request, ensure_ascii=False, allow_nan=False),
                                        capture_output=True, text=True, encoding="utf-8", errors="strict", timeout=660)
    except subprocess.TimeoutExpired:
        return failure("ssh_timeout", "remote acceptance unknown; no automatic retry", {"input_sha256": canonical_sha(request), "acceptance_unknown": True})
    except (OSError, UnicodeError):
        return failure("ssh_transport", "SSH transport failed; no fallback")
    try:
        result = json.loads(r.stdout)
    except (json.JSONDecodeError, TypeError):
        return failure("ssh_reply_json", "remote worker did not return JSON")
    if not isinstance(result, dict) or has_secret_field(result) or result.get("fallback_used") is not False:
        return failure("ssh_reply_binding", "unsafe remote reply rejected")
    p = result.get("provenance", {})
    if not isinstance(result.get("error", {}), dict) or not isinstance(p, dict):
        return failure("ssh_reply_binding", "invalid remote reply envelope")
    identity = p.get("identity", {})
    if not isinstance(identity, dict) or not isinstance(identity.get("xms_mirid", ""), str):
        return failure("ssh_reply_binding", "invalid remote reply envelope")
    if not result.get("ok"):
        code = (result.get("error") or {}).get("code", "remote_worker_error")
        if not isinstance(code, str) or not re.fullmatch(r"[a-z_]+", code):
            code = "remote_worker_error"
        return failure(code, "remote worker failed without fallback", p)
    if (r.returncode != 0 or p.get("input_sha256") != canonical_sha(request) or p.get("bindings") != request["bindings"]
            or p.get("worker_sha256") != tool_sha or p.get("actual_model") != EXPECTED_MODEL
            or p.get("requested_model") != EXPECTED_MODEL or p.get("posted_model") != EXPECTED_MODEL or p.get("endpoint") != EXPECTED_URL
            or p.get("response_status") != "completed" or not isinstance(p.get("response_id"), str)
            or not re.fullmatch(r"resp_[A-Za-z0-9_-]+", p["response_id"]) or p.get("identity_verified") is not True
            or (p.get("identity") or {}).get("xms_mirid", "").lower() != EXPECTED_VM):
        return failure("ssh_reply_binding", "remote response does not bind to approved input, tool and identity", p)
    try:
        validate_output(request, result.get("output_json"))
    except (BoundaryError, KeyError, TypeError, ValueError):
        return failure("ssh_reply_binding", "remote output failed local revalidation", p)
    p["transport"] = "mac_ssh_aicaf_vm_managed_identity"
    p["local_tool_sha256"] = tool_sha
    return result


def worker(request, expected_tool_sha):
    """별도 VM 프로세스에서 기존 운영 호출 모듈을 읽어 사용한다. 서비스 파일·데이터는 쓰지 않는다."""
    tool_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if expected_tool_sha != tool_sha:
        return failure("tool_binding", "remote dedicated tool digest mismatch")
    if os.name != "nt":
        return failure("vm_required", "Foundry worker requires the approved Windows VM; no local fallback")
    try:
        validate_input(request)
        address = socket.gethostbyname("<FOUNDRY_ACCOUNT>.cognitiveservices.azure.com")
        if not ipaddress.ip_address(address).is_private:
            return failure("private_network_guard", "Foundry DNS did not resolve to a private address")
        sys.dont_write_bytecode = True
        sys.path.insert(0, "C:/AICAF/app")
        import arch_job as job
        if Path(job.__file__).resolve().as_posix().lower() != "c:/aicaf/app/arch_job.py":
            return failure("source_guard", "unexpected service call module")
        result = execute(request, job)
    except BoundaryError as e:
        return failure(e.code, "worker input boundary rejected")
    except (KeyError, TypeError, ValueError):
        return failure("input_schema", "worker input schema invalid")
    except (ImportError, OSError):
        return failure("worker_environment", "existing VM environment unavailable; no install or fallback")
    p = result.setdefault("provenance", {})
    p.update(worker_path=str(Path(__file__).resolve()), worker_sha256=tool_sha, resolved_ip=address,
             observed_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
    if result["ok"] and p.get("identity_verified") is not True:
        return failure("identity_guard", "worker result has no verified managed identity", p)
    return result


def prepare_remote(runner=None):
    """전용 도구 파일 하나만 별도 tools 디렉터리에 복사하고 원격 해시를 대조한다. 포털 배포는 수행하지 않는다."""
    run = runner or subprocess.run
    local = Path(__file__).resolve()
    expected = hashlib.sha256(local.read_bytes()).hexdigest()
    windows_dir = REMOTE_DIR.replace("/", chr(92))
    mkdir = f'if not exist "{windows_dir}" mkdir "{windows_dir}"'
    hash_code = "import hashlib,pathlib,json; p=pathlib.Path('" + REMOTE_TOOL + "'); print(json.dumps(dict(sha256=hashlib.sha256(p.read_bytes()).hexdigest())))"
    commands = [["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", SSH_ALIAS, mkdir],
                ["scp", "-q", "-o", "BatchMode=yes", str(local), SSH_ALIAS + ":" + REMOTE_TOOL],
                ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", SSH_ALIAS, f'"{REMOTE_PYTHON}" -B -c "{hash_code}"']]
    r = None
    try:
        for command in commands:
            r = run(command, capture_output=True, text=True, encoding="utf-8", errors="strict", timeout=120)
            if r.returncode:
                return failure("prepare_transport", "dedicated tool preparation failed; no fallback")
        assert r is not None
        remote = json.loads(r.stdout)
        if remote.get("sha256") != expected:
            return failure("tool_binding", "uploaded dedicated tool hash mismatch")
    except (OSError, UnicodeError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return failure("prepare_transport", "dedicated tool preparation unavailable; no fallback")
    return {"ok": True, "worker_path": REMOTE_TOOL, "sha256": expected, "uploaded_files": [REMOTE_TOOL], "fallback_used": False}


def main(argv=None):
    """총괄과 Agent4가 같은 CLI를 직접 재실행할 수 있게 한다. 성공·실패 모두 JSON이며 기존 산출물은 보존한다."""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Agent4 private Foundry Astra only; no Copilot fallback")
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare", help="copy only the dedicated worker and verify its hash")
    prep.add_argument("--output")
    call = commands.add_parser("call", help="send one bound architecture request over approved SSH")
    call.add_argument("--request", required=True)
    call.add_argument("--output", required=True)
    remote = commands.add_parser("worker", help="approved VM only; reads a UTF-8 JSON request from stdin")
    remote.add_argument("--expected-tool-sha", required=True)
    args = parser.parse_args(argv)
    target = Path(args.output) if getattr(args, "output", None) else None
    if target is not None and target.exists():
        print(json.dumps(failure("output_exists", "existing output preserved; use a fresh path")))
        return 3
    if args.command == "prepare":
        result = prepare_remote()
    else:
        try:
            if args.command == "worker":
                reconfigure = getattr(sys.stdin, "reconfigure", None)
                if callable(reconfigure):
                    reconfigure(encoding="utf-8")
                request = json.loads(sys.stdin.read(16 * 1024 * 1024))
            else:
                with open(args.request, encoding="utf-8") as f:
                    request = json.load(f)
        except (OSError, UnicodeError, json.JSONDecodeError):
            result = failure("input_json", "request is not readable UTF-8 JSON")
        else:
            result = worker(request, args.expected_tool_sha) if args.command == "worker" else call_remote(request)
    data = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    if target is not None:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "x", encoding="utf-8") as f:
                f.write(data + "\n")
        except FileExistsError:
            print(json.dumps(failure("output_exists", "existing output preserved", result.get("provenance"))))
            return 3
        except OSError:
            result = failure("output_io", "output write failed; no automatic retry", result.get("provenance"))
            data = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    print(data)
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
