"""Agent4 전용 라우팅의 사전 차단 시험. 고객 입력·네트워크·비밀을 사용하지 않는다."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
spec = importlib.util.find_spec("arch_astra")
A = __import__("arch_astra") if spec else None
URL = "https://<FOUNDRY_ACCOUNT>.cognitiveservices.azure.com/openai/v1/responses"
MODEL = "gpt-6-astra"


def sha(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def fixture(stage="spec"):
    sk = {"diagram": "D1", "state": "TO-BE", "track": "existing", "canvas": {"title": "TO-BE synthetic", "ratio": "16:9", "legend": ["IaaS", "PaaS", "SaaS"]},
          "regions": [{"id": "r1", "name": "koreacentral", "order": 1}], "external": [],
          "containers": [{"id": "g1", "level": "group", "label": "synthetic", "parent": "r1", "row": 3}],
          "nodes": [{"id": "n1", "label": "APP", "service": "Virtual Machine", "parent": "g1", "row": 3, "count": 1, "members": ["synthetic/app"], "evidence": ["synthetic/app"], "reason": "합성 증거의 앱"}],
          "edges": [], "findings": [], "tasks": [], "lockedTruth": {"nodeCount": 1, "edgeCount": 0, "regions": ["koreacentral"]}}
    ledger = {"tasks": []}
    return {"schema_version": 1, "stage": stage, "requested_model": MODEL, "endpoint": URL, "skeleton": sk, "task_ledger": ledger,
            "bindings": {"skeleton_sha256": sha(sk), "task_ledger_sha256": sha(ledger), "image_sha256": None}}


class RouteTests(unittest.TestCase):
    def test_spec_reuses_existing_call_and_records_provenance(self):
        self.assertIsNotNone(A)
        assert A is not None
        import arch_job as job
        from unittest.mock import patch
        sent = []
        payload = {"summary": "합성 앱을 표시한다.", "reasons": {"n1": "앱 행에 배치한다."}, "image_brief": "A single fixed application node inside the supplied region."}

        def transport(url, data, ctype, timeout):
            body = json.loads(data.decode("utf-8"))
            sent.append({"url": url, "body": body, "ctype": ctype})
            return {"id": "resp_unit_spec", "model": MODEL, "status": "completed", "output_text": json.dumps(payload, ensure_ascii=False), "usage": {"input_tokens": 1, "output_tokens": 1}}

        with patch.object(job, "_post", transport):
            result = A.execute(fixture(), job)
            self.assertIs(job._post, transport, "transport must be restored")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["provenance"]["response_id"], "resp_unit_spec")
        self.assertEqual(result["provenance"]["actual_model"], MODEL)
        self.assertEqual(result["output_json"], payload)
        self.assertFalse(result["fallback_used"])
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["url"], URL)
        self.assertEqual(sent[0]["body"]["model"], MODEL)
        self.assertEqual(sent[0]["body"]["input"][0]["content"][0]["text"], job.spec_prompt(fixture()["skeleton"]))

    def test_invalid_response_envelope_fails_closed(self):
        assert A is not None
        import arch_job as job
        from unittest.mock import patch
        cases = [({"id": "resp_unit_bad", "model": "gpt-6.1-sol", "status": "completed"}, "response_model_guard"),
                 ({"id": "resp_unit_bad", "model": MODEL, "status": "incomplete"}, "response_incomplete"),
                 ({"model": MODEL, "status": "completed"}, "response_id_guard"),
                 ({"id": "resp_unit_bad", "status": "completed"}, "response_model_guard")]
        for envelope, code in cases:
            with self.subTest(code=code, envelope=envelope):
                sent = []
                def transport(*args):
                    sent.append(args)
                    return dict(envelope, output_text='{}')
                with patch.object(job, "_post", transport):
                    result = A.execute(fixture(), job)
                    self.assertIs(job._post, transport)
                self.assertFalse(result["ok"], result)
                self.assertEqual(result["error"]["code"], code)
                self.assertEqual(len(sent), 1)
                self.assertFalse(result["fallback_used"])
                self.assertNotIn("output_json", result)

    def test_design_and_analysis_dispatch_to_dedicated_prompts(self):
        assert A is not None
        import arch_job as job
        from unittest.mock import patch
        for stage, rows in [("design", "task_assessments"), ("analysis", "observations")]:
            with self.subTest(stage=stage):
                payload = {"summary": "합성 입력의 근거만 사용한다.", rows: [], "limitations": ["운영 트래픽 증거 없음"]}
                request = fixture(stage)
                sent = []
                def transport(url, data, ctype, timeout):
                    sent.append(json.loads(data.decode("utf-8")))
                    return {"id": "resp_unit_" + stage, "model": MODEL, "status": "completed", "output_text": json.dumps(payload, ensure_ascii=False)}
                with patch.object(job, "_post", transport):
                    result = A.execute(request, job)
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["output_json"], payload)
                prompt = sent[0]["input"][0]["content"][0]["text"]
                self.assertTrue(prompt.startswith("ARCHITECTURE_STAGE: " + stage), prompt)
                self.assertIn(rows, prompt)
                self.assertIn("FIXED_SKELETON:\n" + json.dumps(request["skeleton"], ensure_ascii=False), prompt)
                self.assertEqual(result["provenance"]["stage"], stage)

    def test_read_reuses_existing_prompt_and_binds_image(self):
        assert A is not None
        import base64
        import io
        import arch_job as job
        from PIL import Image
        from unittest.mock import patch
        image = io.BytesIO()
        Image.new("RGB", (64, 48), "white").save(image, format="PNG")
        image_bytes = image.getvalue()
        request = fixture("read")
        request["image_base64"] = base64.b64encode(image_bytes).decode("ascii")
        request["bindings"]["image_sha256"] = hashlib.sha256(image_bytes).hexdigest()
        before = sha(request)
        payload = {"nodes": [{"id": "n1", "found": True, "bbox": [0.1, 0.1, 0.2, 0.2], "seen_label": "APP"}], "extras": []}
        sent = []
        def transport(url, data, ctype, timeout):
            sent.append(json.loads(data.decode("utf-8")))
            return {"id": "resp_unit_read", "model": MODEL, "status": "completed", "output_text": json.dumps(payload)}
        with patch.object(job, "_post", transport):
            result = A.execute(request, job)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["output_json"], payload)
        content = sent[0]["input"][0]["content"]
        self.assertEqual(content[0]["text"], job.read_prompt(request["skeleton"]))
        self.assertEqual(content[1]["type"], "input_image")
        self.assertEqual(base64.b64decode(content[1]["image_url"].split(",", 1)[1]), image_bytes)
        self.assertEqual(result["provenance"]["bindings"]["image_sha256"], request["bindings"]["image_sha256"])
        self.assertEqual(sha(request), before, "model must not mutate frozen input")

    def test_locked_truth_type_rejected_before_transport(self):
        """잘못된 고정 골격 타입을 네트워크 없이 JSON 실패로 반환하는 단위시험."""
        assert A is not None
        import arch_job as job
        from unittest.mock import Mock, patch
        for lock in [["unit_invalid_type"], "unit_invalid_type"]:
            with self.subTest(lock_type=type(lock).__name__):
                request = fixture()
                request["skeleton"]["lockedTruth"] = lock
                request["bindings"]["skeleton_sha256"] = sha(request["skeleton"])
                runner = Mock(side_effect=AssertionError("unit unexpected SSH"))
                with patch.object(job, "_post") as post, patch.object(job, "_token") as token:
                    for invoke, kwargs in [(A.execute, {"job": job}), (A.call_remote, {"runner": runner})]:
                        with self.subTest(boundary=invoke.__name__):
                            result = json.loads(json.dumps(invoke(request, **kwargs)))
                            self.assertFalse(result["ok"])
                            self.assertEqual(result["error"]["code"], "input_schema")
                            self.assertFalse(result["fallback_used"])
                            self.assertNotIn("output_json", result)
                    post.assert_not_called()
                    token.assert_not_called()
                runner.assert_not_called()

    def test_invalid_input_bindings_stop_before_transport(self):
        assert A is not None
        import copy
        import arch_job as job
        from unittest.mock import patch
        cases = []
        req = fixture()
        req["requested_model"] = "gpt-6.1-sol"
        cases.append((req, "route_guard"))
        req = fixture()
        req["endpoint"] = "https://api.githubcopilot.com/responses"
        cases.append((req, "route_guard"))
        req = fixture()
        req["bindings"]["skeleton_sha256"] = "0" * 64
        cases.append((req, "input_binding"))
        req = fixture()
        req["task_ledger"]["tasks"] = [{"id": "T-UNAPPROVED"}]
        req["bindings"]["task_ledger_sha256"] = sha(req["task_ledger"])
        cases.append((req, "ledger_binding"))
        req = fixture()
        req["skeleton"]["lockedTruth"]["nodeCount"] = 2
        req["bindings"]["skeleton_sha256"] = sha(req["skeleton"])
        cases.append((req, "frozen_truth_guard"))
        req = fixture()
        req["stage"] = "general_chat"
        cases.append((req, "stage_guard"))
        req = fixture("read")
        req["image_base64"] = "iVBORw0KGgo="
        req["bindings"]["image_sha256"] = "0" * 64
        cases.append((req, "image_binding"))
        req = fixture()
        req["api_key"] = "unit-nonsecret-marker"
        cases.append((req, "secret_guard"))
        req = fixture()
        req["schema_version"] = 99
        cases.append((req, "input_schema"))
        for request, code in cases:
            with self.subTest(code=code):
                sent = []
                def transport(*args):
                    sent.append(args)
                    return {"id": "resp_unit_input", "model": MODEL, "status": "completed", "output_text": '{}'}
                with patch.object(job, "_post", transport):
                    result = A.execute(copy.deepcopy(request), job)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], code)
                self.assertEqual(sent, [])
                self.assertFalse(result["fallback_used"])

    def test_invalid_model_payloads_are_not_accepted_as_facts(self):
        assert A is not None
        import base64
        import io
        import arch_job as job
        from PIL import Image
        from unittest.mock import patch
        image = io.BytesIO()
        Image.new("RGB", (32, 32), "white").save(image, format="PNG")
        image_bytes = image.getvalue()
        cases = [
            ("spec", {"summary": "x", "reasons": {"n99": "invented"}, "image_brief": "x"}, "output_fact_guard"),
            ("spec", {"summary": "x", "reasons": {"n1": "x"}, "image_brief": "x", "nodes": []}, "output_fact_guard"),
            ("spec", {"summary": None, "reasons": {"n1": "x"}, "image_brief": "x"}, "output_schema"),
            ("design", {"summary": "x", "limitations": [], "task_assessments": [{"task_id": "T-UNAPPROVED", "node_ids": ["n1"], "rationale": "x", "verification": "x"}]}, "ledger_reference_guard"),
            ("analysis", {"summary": "x", "limitations": [], "observations": [{"finding_id": "F99", "node_ids": ["n1"], "observation": "x", "implication": "x", "verification": "x"}]}, "finding_reference_guard"),
            ("read", {"nodes": [{"id": "n1", "found": True, "bbox": [0.9, 0.9, 0.5, 0.5], "seen_label": "APP"}], "extras": []}, "layout_guard"),
            ("read", {"nodes": [], "extras": []}, "layout_guard"),
            ("read", {"nodes": [{"id": "n1", "found": True, "bbox": [0.1, 0.1, 0.2, 0.2], "seen_label": "APP"}], "extras": [{"label": "UNAPPROVED", "kind": "service"}]}, "layout_extra_guard"),
            ("spec", "this is not JSON", "invalid_json"),
            ("spec", {"api_key": "unit-nonsecret"}, "secret_guard"),
        ]
        for stage, payload, code in cases:
            with self.subTest(stage=stage, code=code):
                request = fixture(stage)
                if stage == "read":
                    request["image_base64"] = base64.b64encode(image_bytes).decode("ascii")
                    request["bindings"]["image_sha256"] = hashlib.sha256(image_bytes).hexdigest()
                before = sha(request)
                raw = payload if isinstance(payload, str) else json.dumps(payload)
                sent = []
                def transport(*args):
                    sent.append(args)
                    return {"id": "resp_unit_payload", "model": MODEL, "status": "completed", "output_text": raw}
                with patch.object(job, "_post", transport):
                    result = A.execute(request, job)
                self.assertFalse(result["ok"], result)
                self.assertEqual(result["error"]["code"], code)
                self.assertEqual(result["provenance"]["response_id"], "resp_unit_payload")
                self.assertNotIn("output_json", result)
                self.assertEqual(len(sent), 1)
                self.assertEqual(sha(request), before)
                self.assertFalse(result["fallback_used"])

    def test_request_builder_drift_is_blocked_at_transport_boundary(self):
        assert A is not None
        import arch_job as job
        from unittest.mock import patch
        for url, model in [("https://api.githubcopilot.com/responses", MODEL), (URL, "gpt-6.1-sol")]:
            with self.subTest(url=url, model=model):
                sent = []
                def transport(*args):
                    sent.append(args)
                    return {"id": "resp_unit_drift", "model": MODEL, "status": "completed", "output_text": json.dumps({"summary": "x", "reasons": {"n1": "x"}, "image_brief": "x"})}
                def drifted_builder(prompt, image):
                    return json.loads(job._out_text(job._post(url, json.dumps({"model": model}).encode("utf-8"), "application/json", 30)))
                with patch.object(job, "_post", transport), patch.object(job, "ask_json", drifted_builder):
                    result = A.execute(fixture(), job)
                self.assertFalse(result["ok"], result)
                self.assertEqual(result["error"]["code"], "route_guard")
                self.assertEqual(sent, [])

    def test_foundry_errors_never_fallback_or_expose_error_body(self):
        assert A is not None
        import arch_job as job
        from unittest.mock import patch
        for error in [RuntimeError("HTTP 401: unit-only sensitive marker"), RuntimeError("HTTP 503: unit-only sensitive marker"), TimeoutError("unit-only sensitive marker")]:
            with self.subTest(error=type(error).__name__):
                sent = []
                def transport(*args):
                    sent.append(args)
                    raise error
                with patch.object(job, "_post", transport):
                    result = A.execute(fixture(), job)
                    self.assertIs(job._post, transport)
                self.assertFalse(result["ok"])
                self.assertEqual(len(sent), 1)
                self.assertFalse(result["fallback_used"])
                self.assertNotIn("unit-only sensitive marker", json.dumps(result))

    def test_vm_identity_claims_are_verified_without_logging_tokens(self):
        assert A is not None
        import base64
        import arch_job as job
        from unittest.mock import patch
        vm = A.EXPECTED_VM
        for mirid in [vm, vm.replace("<vm_name>", "vm-unapproved")]:
            with self.subTest(mirid=mirid):
                claims = {"aud": "https://cognitiveservices.azure.com", "xms_mirid": mirid, "oid": "unit-principal", "tid": "unit-tenant"}
                token = "unit." + base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8")).decode("ascii").rstrip("=") + ".unit"
                sent = []
                def transport(*args):
                    job._token()
                    sent.append(args)
                    return {"id": "resp_unit_identity", "model": MODEL, "status": "completed", "output_text": json.dumps({"summary": "x", "reasons": {"n1": "x"}, "image_brief": "x"})}
                with patch.object(job, "_post", transport), patch.object(job, "_token", lambda: token):
                    result = A.execute(fixture(), job)
                self.assertNotIn(token, json.dumps(result))
                if mirid == vm:
                    self.assertTrue(result["ok"], result)
                    self.assertIn("identity", result["provenance"])
                    self.assertEqual(result["provenance"]["identity"]["xms_mirid"].lower(), vm)
                else:
                    self.assertFalse(result["ok"], result)
                    self.assertEqual(result["error"]["code"], "identity_guard")
                    self.assertEqual(sent, [])

    def test_ssh_client_binds_reply_to_input_and_tool(self):
        assert A is not None
        self.assertTrue(callable(getattr(A, "call_remote", None)), "SSH client is not implemented")
        request = fixture()
        tool_sha = hashlib.sha256(Path(A.__file__).read_bytes()).hexdigest()
        payload = {"summary": "x", "reasons": {"n1": "x"}, "image_brief": "x"}
        for request_sha in [sha(request), "0" * 64]:
            with self.subTest(input_sha256=request_sha):
                sent = []
                reply = {"ok": True, "output_json": payload, "fallback_used": False,
                         "provenance": {"actual_model": MODEL, "requested_model": MODEL, "posted_model": MODEL, "endpoint": URL, "response_id": "resp_unit_ssh", "response_status": "completed",
                                        "input_sha256": request_sha, "bindings": request["bindings"], "worker_sha256": tool_sha, "identity_verified": True,
                                        "identity": {"xms_mirid": A.EXPECTED_VM}}}
                def runner(argv, **kwargs):
                    sent.append((argv, kwargs))
                    return types.SimpleNamespace(returncode=0, stdout=json.dumps(reply), stderr="")
                result = A.call_remote(request, runner=runner)
                self.assertEqual(len(sent), 1)
                argv, kwargs = sent[0]
                self.assertIn("app-vm", argv)
                self.assertIn(r"C:\AICAF\.venv\Scripts\python.exe", argv[-1])
                self.assertEqual(kwargs["encoding"], "utf-8")
                self.assertEqual(json.loads(kwargs["input"]), request)
                if request_sha == sha(request):
                    self.assertTrue(result["ok"], result)
                else:
                    self.assertFalse(result["ok"], result)
                    self.assertEqual(result["error"]["code"], "ssh_reply_binding")

    def test_remote_envelope_types_fail_closed(self):
        """모의 SSH 응답의 내부 타입과 민감 표식 차단을 검증하는 단위시험."""
        assert A is not None
        from unittest.mock import Mock
        request = fixture()
        tool_sha = hashlib.sha256(Path(A.__file__).read_bytes()).hexdigest()
        marker = "unit_sensitive_envelope_marker"
        for field in ("error", "provenance", "identity", "xms_mirid"):
            values = [[marker], 1, True, None, []]
            values.append({"value": marker} if field == "xms_mirid" else marker)
            for value in values:
                with self.subTest(field=field, value_type=type(value).__name__):
                    reply = {"ok": True, "output_json": {"summary": "x", "reasons": {"n1": "x"}, "image_brief": "x"}, "fallback_used": False,
                             "provenance": {"actual_model": MODEL, "requested_model": MODEL, "posted_model": MODEL, "endpoint": URL,
                                            "response_id": "resp_unit_envelope", "response_status": "completed", "input_sha256": sha(request),
                                            "bindings": request["bindings"], "worker_sha256": tool_sha, "identity_verified": True,
                                            "identity": {"xms_mirid": A.EXPECTED_VM}}}
                    if field == "error":
                        reply["ok"] = False
                        reply["error"] = value
                    elif field == "provenance":
                        reply["provenance"] = value
                    elif field == "identity":
                        reply["provenance"]["identity"] = value
                    else:
                        reply["provenance"]["identity"]["xms_mirid"] = value
                    runner = Mock(return_value=types.SimpleNamespace(returncode=0, stdout=json.dumps(reply), stderr=marker))
                    result = json.loads(json.dumps(A.call_remote(request, runner=runner)))
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["error"]["code"], "ssh_reply_binding")
                    self.assertEqual(result["provenance"], {})
                    self.assertFalse(result["fallback_used"])
                    self.assertNotIn("output_json", result)
                    self.assertNotIn(marker, json.dumps(result))
                    runner.assert_called_once()

    def test_worker_refuses_unbound_tool_before_loading_service(self):
        assert A is not None
        self.assertTrue(callable(getattr(A, "worker", None)), "remote worker is not implemented")
        result = A.worker(fixture(), "0" * 64)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "tool_binding")
        self.assertFalse(result["fallback_used"])

    def test_prepare_syncs_only_dedicated_tool_and_checks_hash(self):
        assert A is not None
        self.assertTrue(callable(getattr(A, "prepare_remote", None)), "dedicated tool preparation is not implemented")
        expected = hashlib.sha256(Path(A.__file__).read_bytes()).hexdigest()
        for remote_sha in [expected, "0" * 64]:
            with self.subTest(remote_sha=remote_sha):
                sent = []
                def runner(argv, **kwargs):
                    sent.append(argv)
                    return types.SimpleNamespace(returncode=0, stdout=json.dumps({"sha256": remote_sha}) if len(sent) == 3 else "", stderr="")
                result = A.prepare_remote(runner=runner)
                self.assertEqual(result["ok"], remote_sha == expected)
                self.assertEqual(len(sent), 3)
                self.assertEqual(sent[1][0], "scp")
                self.assertIn("C:/AICAF/tools/agent4-foundry-astra/arch_astra.py", sent[1][-1])
                commands = json.dumps(sent)
                self.assertNotIn("C:/AICAF/app/", commands)
                self.assertNotIn("schtasks", commands)
                self.assertNotIn("server.py", commands)
                self.assertNotIn("az ", commands)

    def test_cli_output_io_fails_with_accepted_provenance(self):
        """모의 호출 수락 후 출력 I/O 장애의 JSON·출처·기존 파일 보존 단위시험."""
        assert A is not None
        import contextlib
        import io
        import tempfile
        from unittest.mock import mock_open, patch
        marker = "unit_sensitive_output_marker"
        provenance = {"response_id": "resp_unit_io", "actual_model": MODEL, "requested_model": MODEL, "response_status": "completed"}
        accepted = {"ok": True, "output_json": {"summary": "x", "reasons": {"n1": "x"}, "image_brief": "x"},
                    "provenance": provenance, "fallback_used": False}
        faults = [(operation, error) for operation in ("mkdir", "open", "write", "close") for error in (PermissionError, OSError)]
        faults.append(("exists", FileExistsError))
        original_open = open
        with tempfile.TemporaryDirectory() as root:
            request_path = Path(root) / "input.json"
            request_path.write_text(json.dumps(fixture()), encoding="utf-8")
            preserved_path = Path(root) / "existing.json"
            preserved_path.write_text("preserved", encoding="utf-8")
            for operation, error_type in faults:
                with self.subTest(operation=operation, error_type=error_type.__name__):
                    output_path = Path(root) / (operation + "_" + error_type.__name__ + ".json")
                    error = error_type(marker)
                    output_file = mock_open()()
                    if operation == "write":
                        output_file.write.side_effect = error
                    elif operation == "close":
                        output_file.__exit__.side_effect = error

                    def open_file(path, mode="r", *args, **kwargs):
                        if mode == "x":
                            self.assertEqual(path, output_path)
                            if operation == "exists":
                                output_path.write_text("preserved", encoding="utf-8")
                                raise error
                            if operation == "open":
                                raise error
                            return output_file
                        return original_open(path, mode, *args, **kwargs)

                    capture = io.StringIO()
                    with patch.object(A, "call_remote", return_value=accepted) as dispatch, patch("builtins.open", side_effect=open_file) as opener, \
                            patch.object(Path, "mkdir", side_effect=error if operation == "mkdir" else None), contextlib.redirect_stdout(capture):
                        rc = A.main(["call", "--request", str(request_path), "--output", str(output_path)])
                    result = json.loads(capture.getvalue())
                    self.assertEqual(rc, 3 if operation == "exists" else 2)
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["error"]["code"], "output_exists" if operation == "exists" else "output_io")
                    self.assertEqual(result["provenance"], provenance)
                    self.assertFalse(result["fallback_used"])
                    self.assertNotIn("output_json", result)
                    self.assertNotIn(marker, capture.getvalue())
                    self.assertEqual(preserved_path.read_text(encoding="utf-8"), "preserved")
                    dispatch.assert_called_once()
                    output_calls = [call for call in opener.call_args_list if len(call.args) > 1 and call.args[1] == "x"]
                    self.assertEqual(len(output_calls), 0 if operation == "mkdir" else 1)
                    if operation == "exists":
                        self.assertEqual(output_path.read_text(encoding="utf-8"), "preserved")

    def test_cli_preserves_existing_output_without_calling_foundry(self):
        assert A is not None
        self.assertTrue(callable(getattr(A, "main", None)), "CLI is not implemented")
        import contextlib
        import io
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as root:
            request_path = Path(root) / "input.json"
            request_path.write_text(json.dumps(fixture(), ensure_ascii=False), encoding="utf-8")
            output_path = Path(root) / "result.json"
            output_path.write_text("preserved", encoding="utf-8")
            sent = []
            capture = io.StringIO()
            with patch.object(A, "call_remote", lambda *args: sent.append(args)), contextlib.redirect_stdout(capture):
                rc = A.main(["call", "--request", str(request_path), "--output", str(output_path)])
            self.assertEqual(rc, 3)
            self.assertEqual(sent, [])
            self.assertEqual(output_path.read_text(encoding="utf-8"), "preserved")
            self.assertEqual(json.loads(capture.getvalue())["error"]["code"], "output_exists")

    def test_config_drift_rejected_before_transport(self):
        self.assertIsNotNone(A, "Agent4 dedicated route is not implemented")
        sent = []
        job = types.SimpleNamespace(TEXT_URL=URL, TEXT_MODEL="gpt-6.1-sol", ask_json=lambda *args: sent.append(args))
        result = A.execute(fixture(), job)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "route_guard")
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
