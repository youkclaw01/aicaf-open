"""정본 프롬프트와 프로젝트별 불변 개정을 SQLite로 지속 보관한다."""
from contextlib import contextmanager
import hashlib
import json
import math
import os
import re
from pathlib import Path
import sqlite3
import uuid

SOURCE_ROOT = Path(__file__).resolve().parent.parent / "arch"
SAMPLE_NAMES = ("sample-full-infra.jpg", "sample-network.jpg")


class StoreError(Exception):
    """API가 구분할 수 있는 ASCII 오류 코드와 HTTP 상태를 보존한다."""

    def __init__(self, code, status):
        self.code, self.status = code, status
        super().__init__(code)


def _text(value, code, *, empty=False):
    if not isinstance(value, str) or "\x00" in value or (not empty and not value.strip()):
        raise StoreError(code, 400)
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise StoreError(code, 400) from None
    return value


def _identifier(value, code):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", value):
        raise StoreError(code, 400)
    return value


def _json_value(value, depth=0):
    if depth > 64:
        raise StoreError("invalid_json_value", 400)
    if isinstance(value, dict):
        for key, item in value.items():
            _text(key, "invalid_json_key", empty=True)
            _json_value(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _json_value(item, depth + 1)
    elif isinstance(value, str):
        _text(value, "invalid_json_text", empty=True)
    elif value is None or type(value) in (bool, int):
        pass
    elif type(value) is float and math.isfinite(value):
        pass
    else:
        raise StoreError("invalid_json_value", 400)


def _file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _source(path, expected):
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise StoreError("invalid_source_sha256", 400)
    try:
        actual = _file_sha(path)
    except OSError:
        raise StoreError("source_unavailable", 400) from None
    if actual != expected:
        raise StoreError("source_sha256_mismatch", 400)


def _prompt(pack, actor):
    _text(actor, "invalid_actor")
    if not isinstance(pack, dict):
        raise StoreError("invalid_prompt", 400)
    _json_value(pack)
    _identifier(pack.get("id"), "invalid_prompt_id")
    _text(pack.get("version"), "invalid_prompt_version")
    _text(pack.get("common"), "invalid_prompt_common")
    patterns = pack.get("patterns")
    if not isinstance(patterns, dict):
        raise StoreError("invalid_prompt_patterns", 400)
    for name in ("D1", "D2"):
        _text(patterns.get(name), "invalid_prompt_pattern")
    if pack.get("status") != "approved" or pack.get("fallback_used", False) is not False:
        raise StoreError("prompt_not_approved", 400)
    analysis = pack.get("analysis")
    if not isinstance(analysis, list) or not analysis:
        raise StoreError("invalid_prompt_analysis", 400)
    for row in analysis:
        if not isinstance(row, dict) or row.get("actual_model") != "gpt-6-astra" or row.get("fallback_used", False) is not False:
            raise StoreError("invalid_analysis_model", 400)
        response_id = row.get("response_id")
        if not isinstance(response_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", response_id):
            raise StoreError("invalid_analysis_response_id", 400)
    sources = pack.get("sources")
    if not isinstance(sources, dict):
        raise StoreError("invalid_prompt_sources", 400)
    _source(SOURCE_ROOT / "ARCH-DRAWING-STANDARD.md", sources.get("guide_sha256"))
    samples = sources.get("samples")
    if not isinstance(samples, list) or len(samples) != 2:
        raise StoreError("invalid_prompt_samples", 400)
    names = []
    for sample in samples:
        if not isinstance(sample, dict) or sample.get("name") not in SAMPLE_NAMES:
            raise StoreError("invalid_prompt_sample", 400)
        names.append(sample["name"])
        _source(SOURCE_ROOT / "samples" / sample["name"], sample.get("sha256"))
    if len(set(names)) != 2:
        raise StoreError("duplicate_prompt_sample", 400)
    return json.loads(_json(pack))


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _sha(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _context(aid, state, diagram):
    if type(aid) is not int or not 0 < aid <= 9223372036854775807:
        raise StoreError("invalid_assessment_id", 400)
    if not isinstance(state, str) or state not in ("AS-IS", "TO-BE"):
        raise StoreError("invalid_state", 400)
    if not isinstance(diagram, str) or diagram not in ("D1", "D2"):
        raise StoreError("invalid_diagram", 400)
    return aid, state, diagram


def _path_text(value, code):
    if not isinstance(value, (str, os.PathLike)):
        raise StoreError(code, 400)
    return _text(os.fspath(value), code)


def _body(row):
    return json.loads(row["body"]) if row else None


class Store:
    """조회는 디렉터리를 만들지 않으며 쓰기 작업만 잠금 안에서 수행한다."""

    def __init__(self, root):
        self.root = Path(_path_text(root, "invalid_root")).resolve()
        self.db_path = self.root / "arch-store.sqlite3"

    @contextmanager
    def _read(self):
        self._safe(self.db_path)
        if not self.db_path.is_file():
            yield None
            return
        conn = sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _write(self):
        self._safe(self.db_path)
        self.root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("CREATE TABLE IF NOT EXISTS prompts (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                         "id TEXT NOT NULL, version TEXT NOT NULL, sha256 TEXT NOT NULL, "
                         "snapshot TEXT NOT NULL, UNIQUE(id, version))")
            conn.execute("CREATE TABLE IF NOT EXISTS revisions (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                         "revision_id TEXT NOT NULL UNIQUE, aid INTEGER NOT NULL, state TEXT NOT NULL, "
                         "diagram TEXT NOT NULL, nonce TEXT NOT NULL, request_sha256 TEXT NOT NULL, "
                         "status TEXT NOT NULL, body TEXT NOT NULL, UNIQUE(aid,state,diagram,nonce))")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_active_revision "
                         "ON revisions(aid,state,diagram) WHERE status='running'")
            conn.execute("CREATE TABLE IF NOT EXISTS successes (aid INTEGER NOT NULL, state TEXT NOT NULL, "
                         "diagram TEXT NOT NULL, revision_id TEXT NOT NULL, PRIMARY KEY(aid,state,diagram))")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def save_prompt(self, pack, actor):
        """승인된 정본의 본문 전체와 저장 주체를 독립 스냅샷으로 고정한다."""
        pack = _prompt(pack, actor)
        digest = _sha(pack)
        snapshot = dict(pack, actor=actor, sha256=digest)
        with self._write() as conn:
            prior = conn.execute("SELECT sha256,snapshot FROM prompts WHERE id=? AND version=?",
                                 (pack["id"], pack["version"])).fetchone()
            if prior:
                if prior["sha256"] != digest:
                    raise StoreError("prompt_version_conflict", 409)
                return json.loads(prior["snapshot"])
            conn.execute("INSERT INTO prompts(id,version,sha256,snapshot) VALUES (?,?,?,?)",
                         (pack["id"], pack["version"], digest, _json(snapshot)))
        return json.loads(_json(snapshot))

    def current_prompt(self):
        """최근 승인된 정본을 반환하고 저장된 본문을 임의 생성하지 않는다."""
        with self._read() as conn:
            row = conn.execute("SELECT snapshot FROM prompts ORDER BY seq DESC LIMIT 1").fetchone() if conn else None
            return json.loads(row["snapshot"]) if row else None

    def _safe(self, path):
        """프로젝트 경로의 심볼릭 링크로 쓰기 경계를 우회하지 못하게 한다."""
        path = Path(path)
        if not path.resolve().is_relative_to(self.root):
            raise StoreError("unsafe_store_path", 409)
        for part in (path, *path.parents):
            if part == self.root:
                break
            if part.is_symlink():
                raise StoreError("unsafe_store_path", 409)
        return path

    def _previous(self, directory):
        """실제로 존재하는 직전 파일만 해시하며 레거시를 이동하지 않는다."""
        previous: dict[str, str | None] = {"source_directory": None, "spec_sha256": None, "layout_sha256": None,
                                         "image_sha256": None, "checks_sha256": None}
        if directory is None:
            return previous
        directory = self._safe(directory)
        for stem, filename in (("spec", "spec.json"), ("layout", "layout.json"),
                               ("image", "image.png"), ("checks", "checks.json")):
            path = self._safe(directory / filename)
            if stem == "checks" and not path.is_file():
                path = self._safe(directory / "job.json")
            if path.is_file():
                previous[stem + "_sha256"] = _file_sha(path)
                previous["source_directory"] = str(directory)
        return previous

    @staticmethod
    def _get(conn, aid, state, diagram, rev):
        return _body(conn.execute("SELECT body FROM revisions WHERE aid=? AND state=? AND diagram=? AND revision_id=?",
                                  (aid, state, diagram, rev)).fetchone()) if conn else None

    @staticmethod
    def _success(conn, aid, state, diagram):
        if conn is None:
            return None
        return _body(conn.execute("SELECT r.body FROM successes s JOIN revisions r ON r.revision_id=s.revision_id "
                                  "WHERE s.aid=? AND s.state=? AND s.diagram=? AND r.status='done'",
                                  (aid, state, diagram)).fetchone())

    def get(self, aid, state, diagram, rev):
        """동일 프로젝트·상태·도면 안에서만 개정을 조회한다."""
        _context(aid, state, diagram)
        _identifier(rev, "invalid_revision_id")
        with self._read() as conn:
            return self._get(conn, aid, state, diagram, rev)

    def list(self, aid, state, diagram):
        """실패한 개정도 포함해 예약 순서대로 독립 행을 반환한다."""
        _context(aid, state, diagram)
        with self._read() as conn:
            if conn is None:
                return []
            return [_body(row) for row in conn.execute("SELECT body FROM revisions WHERE aid=? AND state=? AND diagram=? ORDER BY seq",
                                                       (aid, state, diagram)).fetchall()]

    def current_success(self, aid, state, diagram):
        """원자적으로 승격된 마지막 정상 개정을 반환한다."""
        _context(aid, state, diagram)
        with self._read() as conn:
            return self._success(conn, aid, state, diagram)

    def reserve(self, aid, state, diagram, nonce, requirements, actor, case_dir, track,
                parent_revision_id=None):
        """동일 nonce를 멱등 처리하고 도면당 실행 중인 개정을 하나로 제한한다."""
        _context(aid, state, diagram)
        _identifier(nonce, "invalid_nonce")
        _text(requirements, "invalid_requirements", empty=True)
        _text(actor, "invalid_actor")
        case_dir = _path_text(case_dir, "invalid_case_dir")
        _text(track, "invalid_track")
        if parent_revision_id is not None:
            _identifier(parent_revision_id, "invalid_parent_revision_id")
        request = {"assessment_id": aid, "state": state, "diagram": diagram, "nonce": nonce,
                   "requirements": requirements, "actor": actor, "case_dir": case_dir, "track": track,
                   "parent_revision_id": parent_revision_id}
        request_sha = _sha(request)
        if self.current_prompt() is None:
            raise StoreError("prompt_unavailable", 409)
        with self._write() as conn:
            prior = conn.execute("SELECT request_sha256,body FROM revisions WHERE aid=? AND state=? AND diagram=? AND nonce=?",
                                 (aid, state, diagram, nonce)).fetchone()
            if prior:
                if prior["request_sha256"] != request_sha:
                    raise StoreError("nonce_conflict", 409)
                return _body(prior), False
            active = conn.execute("SELECT revision_id FROM revisions WHERE aid=? AND state=? AND diagram=? AND status='running'",
                                  (aid, state, diagram)).fetchone()
            if active:
                raise StoreError("active_revision", 409)
            parent = (self._get(conn, aid, state, diagram, parent_revision_id) if parent_revision_id
                      else self._success(conn, aid, state, diagram))
            if parent_revision_id and parent is None:
                raise StoreError("parent_not_found", 404)
            revision_id = uuid.uuid4().hex
            parent_id = parent["revision_id"] if parent else None
            legacy = self._safe(self.root / str(aid) / f"{state}_{diagram}")
            path = self._safe(legacy / "revisions" / revision_id)
            prompt = json.loads(conn.execute("SELECT snapshot FROM prompts ORDER BY seq DESC LIMIT 1").fetchone()["snapshot"])
            inp = {"schema_version": 2, "revision_id": revision_id, "parent_revision_id": parent_id,
                   "assessment_id": aid, "state": state, "diagram": diagram, "case_dir": case_dir,
                   "track": track, "actor": actor, "requirements": requirements, "prompt": prompt,
                   "previous": self._previous(parent["path"] if parent else legacy)}
            row = {"revision_id": revision_id, "parent_revision_id": parent_id, "assessment_id": aid,
                   "state": state, "diagram": diagram, "status": "running", "nonce": nonce,
                   "input_sha256": _sha(inp), "path": str(path), "input": inp,
                   "checks": {}, "artifacts": {}, "error": ""}
            path.mkdir(parents=True, exist_ok=False)
            try:
                with (path / "input.json").open("x", encoding="utf-8", newline="\n") as output:
                    output.write(_json(inp))
                    output.flush()
                    os.fsync(output.fileno())
                conn.execute("INSERT INTO revisions(revision_id,aid,state,diagram,nonce,request_sha256,status,body) VALUES (?,?,?,?,?,?,?,?)",
                             (revision_id, aid, state, diagram, nonce, request_sha, "running", _json(row)))
            except BaseException:
                (path / "input.json").unlink(missing_ok=True)
                path.rmdir()
                raise
            return json.loads(_json(row)), True

    def _completion_error(self, row, checks, artifacts):
        """실제 파일·렌더 영수증·동일 개정 결합을 검증하고 차단 사유를 반환한다."""
        if (not all(f"A{i}" in checks for i in range(1, 10))
                or not all(value is True or (isinstance(value, dict) and value.get("pass") is True)
                           for value in checks.values())):
            return "checks_failed"
        try:
            directory = self._safe(row["path"])
            if _file_sha(self._safe(directory / "input.json")) != row["input_sha256"]:
                return "input_sha256_mismatch"
            if (artifacts.get("schema") != "aicaf.arch.dual-artifacts.v1"
                    or artifacts.get("status") != "done" or artifacts.get("render_status") != "ready"):
                return "artifacts_not_ready"
            context = {key: row[key] for key in ("assessment_id", "state", "diagram", "revision_id")}
            if any(artifacts.get(key) != value for key, value in context.items()):
                return "artifact_context_mismatch"
            manifest = json.loads(self._safe(directory / "artifact-meta.json").read_text(encoding="utf-8"))
            if manifest != artifacts:
                return "artifact_manifest_mismatch"
            records = artifacts["artifacts"]
            if not isinstance(records, dict):
                return "invalid_artifact_records"
            for kind, name in (("original_image", "image.png"), ("native_pptx", "diagram.pptx"),
                               ("ppt_preview", "ppt-preview.png")):
                record = records[kind]
                if (not isinstance(record, dict) or record.get("id") != kind or record.get("name") != name
                        or record.get("revision_id") != row["revision_id"]):
                    return "artifact_revision_mismatch"
                data = self._safe(directory / name).read_bytes()
                if (not data or record.get("sha256") != hashlib.sha256(data).hexdigest()
                        or type(record.get("bytes")) is not int or record["bytes"] != len(data)):
                    return "artifact_sha256_mismatch"
            original, native, preview = (records[key] for key in ("original_image", "native_pptx", "ppt_preview"))
            if original.get("source_model") not in ("gpt-image-2.5", "gpt-image-2.5-sunburst"):
                return "invalid_original_image_model"
            binding = dict(context, original_image_sha256=original["sha256"], native_pptx_sha256=native["sha256"])
            binding_sha = _sha(binding)
            if (artifacts.get("binding") != binding or artifacts.get("binding_sha256") != binding_sha
                    or any(artifacts.get(key) != value for key, value in binding.items())
                    or any(record.get("binding_sha256") != binding_sha for record in (original, native, preview))):
                return "artifact_binding_mismatch"
            if (native.get("source_image_sha256") != original["sha256"]
                    or preview.get("source_image_sha256") != original["sha256"]
                    or preview.get("source_pptx_sha256") != native["sha256"]):
                return "artifact_source_mismatch"
            renderer = preview.get("renderer")
            if (not isinstance(renderer, dict) or renderer.get("name") != "LibreOffice"
                    or renderer.get("method") != "pptx_to_pdf_to_png"
                    or not isinstance(renderer.get("version"), str) or not renderer["version"].strip()
                    or renderer.get("source_snapshot_sha256") != native["sha256"]):
                return "true_render_required"
            receipt = json.loads(self._safe(directory / "ppt-preview.binding.json").read_text(encoding="utf-8"))
            for key, value in preview.items():
                if key == "binding_sha256":
                    continue
                if key == "renderer":
                    if any(receipt.get("renderer", {}).get(k) != v for k, v in value.items()):
                        return "render_receipt_mismatch"
                elif receipt.get(key) != value:
                    return "render_receipt_mismatch"
            receipt_renderer = receipt["renderer"]
            if type(receipt_renderer.get("exit_code")) is not int or receipt_renderer["exit_code"] != 0:
                return "render_not_completed"
            for key, value in (("directory", str(directory)), ("path", str(directory / "ppt-preview.png")),
                               ("pdf_path", str(directory / "ppt-render.pdf")),
                               ("binding_path", str(directory / "ppt-preview.binding.json"))):
                if receipt.get(key) != value:
                    return "render_path_mismatch"
            if _file_sha(self._safe(directory / "ppt-render.pdf")) != preview.get("source_pdf_sha256"):
                return "render_pdf_sha256_mismatch"
            return ""
        except StoreError as error:
            return error.code
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return "artifact_binding_invalid"

    def _persist_checks(self, directory, checks):
        """기존 검사 파일은 덮어쓰지 않고 새 검사 파일만 UTF-8로 고정한다."""
        try:
            path = self._safe(Path(directory) / "checks.json")
            if path.exists():
                if json.loads(path.read_text(encoding="utf-8")) != checks:
                    return "checks_content_conflict"
                return ""
            with path.open("x", encoding="utf-8", newline="\n") as output:
                output.write(_json(checks))
                output.flush()
                os.fsync(output.fileno())
            return ""
        except StoreError as error:
            return error.code
        except (OSError, ValueError):
            return "checks_persist_failed"

    def finish(self, aid, state, diagram, rev, status, checks, artifacts, error=""):
        """종료 결과를 불변 저장하고 모든 검증을 통과한 개정만 원자 승격한다.

        완료 요청의 결합 검증 실패도 failed 행으로 보존하며 실행 잠금을 해제한다.
        """
        _context(aid, state, diagram)
        _identifier(rev, "invalid_revision_id")
        if not isinstance(status, str) or status not in ("done", "failed"):
            raise StoreError("invalid_finish_status", 400)
        if not isinstance(checks, dict) or not isinstance(artifacts, dict):
            raise StoreError("invalid_finish_payload", 400)
        _text(error, "invalid_error", empty=True)
        _json_value(checks)
        _json_value(artifacts)
        requested_sha = _sha({"status": status, "checks": checks, "artifacts": artifacts, "error": error})
        checks, artifacts = json.loads(_json(checks)), json.loads(_json(artifacts))
        if self.get(aid, state, diagram, rev) is None:
            raise StoreError("revision_not_found", 404)
        with self._write() as conn:
            row = self._get(conn, aid, state, diagram, rev)
            if row is None:
                raise StoreError("revision_not_found", 404)
            if row["status"] != "running":
                if row.get("finish_sha256") == requested_sha:
                    return row
                raise StoreError("immutable_result", 409)
            reason = self._completion_error(row, checks, artifacts) if status == "done" else ""
            persist_error = self._persist_checks(row["path"], checks)
            reason = reason or persist_error
            final_status = "failed" if reason else status
            row.update(status=final_status, checks=checks, artifacts=artifacts,
                       error=error or reason, finish_sha256=requested_sha)
            conn.execute("UPDATE revisions SET status=?,body=? WHERE revision_id=?",
                         (final_status, _json(row), rev))
            if final_status == "done":
                conn.execute("INSERT INTO successes(aid,state,diagram,revision_id) VALUES (?,?,?,?) "
                             "ON CONFLICT(aid,state,diagram) DO UPDATE SET revision_id=excluded.revision_id",
                             (aid, state, diagram, rev))
            return json.loads(_json(row))
