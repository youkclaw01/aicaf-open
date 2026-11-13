"""Collector 업로드 — WARA 포털과 같은 계약(1회용 업로드 key + manual-intake 세션).

WARA 정본(C:\\WARA\\collector\\collector_routes.py)과 같은 검증 모듈
(C:\\INFRA\\collector\\assessment_api: CollectorUploadKeyAuthority·FilesystemManualIntakeStore)을 그대로 쓴다.
다른 점은 업로드가 들어갈 곳뿐이다: WARA 는 계정별 케이스, AICAF 는 key 를 발급한 '프로젝트'의
인프라 케이스(C:\\INFRA\\storage\\aicaf\\cases\\<case>\\Source) 로 간다.

    POST /api/aicaf/assessments/{aid}/infra/upload-key        (로그인·인터뷰어 이상) key 발급, 10분·1회용
    POST /api/v1/manual-intake/sessions                       (Bearer key) 세션 생성
    POST /api/v1/manual-intake/sessions/{id}/artifacts/{role} (Bearer key) 1MB 청크
    POST /api/v1/manual-intake/sessions/{id}/complete         (Bearer key) 두 파일(+선택 aicaf-evidence.json) 확정 → Source 게시

세 번째 role 'aicaf-evidence-v1'(aicaf-evidence.json)은 AICAF 전용이다. 공용 모듈(manual_intake.py·
manual_intake_store.py)의 역할표는 바꾸지 않고, 이 파일에서만 역할표를 넓히고 청크 저장을 직접 처리한다.
구 스크립트(두 파일)는 그대로 통과한다.

Collector 스크립트(Collect-InfraEvidence.ps1)는 WARA 와 같은 경로·헤더·본문을 쓴다.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

_SESSION_ID = re.compile(r"\Aupl_[0-9a-f]{32}\Z")
_RANGE = re.compile(r"\Abytes (\d+)-(\d+)/(\d+)\Z")
_DENIED = {"error": "COLLECTOR_UPLOAD_DENIED", "message": "업로드가 거부되었습니다."}
EVIDENCE_ROLE = "aicaf-evidence-v1"
EVIDENCE_FILENAME = re.compile(r"\Aaicaf-evidence\.json\Z")
EVIDENCE_SCHEMA = "aicaf-evidence/v1"
SESSIONS_PATH = "/api/v1/manual-intake/sessions"


class CollectorUpload:
    def __init__(self, *, infra_root: Path, state_dir: Path, case_root: Path, secret: bytes):
        sys.path.insert(0, str(infra_root / "collector"))
        from assessment_api.collector_upload_key import CollectorUploadKeyAuthority, CollectorUploadKeyError
        from assessment_api.collector_upload_key_postgres import SqliteCollectorUploadKeyStore, CollectorUploadKeyStoreError
        from assessment_api.manual_intake import ManualIntakeError, ROLE_FILENAMES
        from assessment_api.manual_intake_store import CHUNK_SIZE_BYTES, FilesystemManualIntakeStore
        self.Errors = (CollectorUploadKeyError, CollectorUploadKeyStoreError, ManualIntakeError)
        self.KeyError_ = CollectorUploadKeyError
        # 기존 두 role 은 필수, 세 번째는 선택
        self.REQUIRED = frozenset(ROLE_FILENAMES)
        self.ROLES, self.CHUNK = {**ROLE_FILENAMES, EVIDENCE_ROLE: EVIDENCE_FILENAME}, CHUNK_SIZE_BYTES
        state_dir.mkdir(parents=True, exist_ok=True)
        self.keys = SqliteCollectorUploadKeyStore(state_dir / "collector-keys.sqlite3")
        self.authority = CollectorUploadKeyAuthority(secret=secret, store=self.keys)
        self.intake = FilesystemManualIntakeStore(state_dir / "intake-sessions", case_root=case_root)
        self.case_root = case_root

    # ---- 발급 (로그인한 사용자, 프로젝트 단위) ----
    def issue(self, *, owner: str, case_id: str, subscription_id: str) -> dict:
        return dict(self.authority.issue(owner_subject=owner, case_id=case_id,
                                         subscription_id=subscription_id.strip().lower()))

    # ---- 업로드 (Bearer key) ----
    def handle(self, route: str, headers, body: bytes):
        """(status, json, done_case_id|None)"""
        try:
            if route == SESSIONS_PATH:
                return (*self._create(headers, body), None)
            tail = route[len(SESSIONS_PATH) + 1:]
            if "/artifacts/" in tail:
                sid, role = tail.split("/artifacts/", 1)
                return (*self._chunk(headers, body, sid, role), None)
            if tail.endswith("/complete"):
                return self._complete(headers, body, tail[:-len("/complete")])
        except self.Errors:
            return 401, dict(_DENIED), None
        return 404, dict(_DENIED), None

    def _bearer(self, headers):
        auth = str(headers.get("Authorization", ""))
        token = auth[7:].strip() if auth.startswith("Bearer ") else ""
        kid, sep, plain = token.partition(".")
        if not sep or not kid or not plain:
            raise self.KeyError_(status=401, code="COLLECTOR_KEY_INVALID", message="업로드 key가 유효하지 않습니다.")
        return kid, plain

    def _idem(self, headers):
        v = str(headers.get("X-Idempotency-Key", ""))
        if re.fullmatch(r"[0-9a-f]{64}", v) is None:
            raise self.KeyError_(status=401, code="COLLECTOR_KEY_INVALID", message="업로드 key가 유효하지 않습니다.")
        return v

    def _verify(self, kid, plain):
        rec = self.keys.stored_record(kid)
        if rec is None:
            raise self.KeyError_(status=401, code="COLLECTOR_KEY_INVALID", message="업로드 key가 유효하지 않습니다.")
        self.authority.verify(key_id=kid, presented_key=plain, owner_subject=rec.owner_subject,
                              case_id=rec.case_id, subscription_id=rec.subscription_id, allow_consumed=False)
        return rec

    def _create(self, headers, body):
        kid, plain = self._bearer(headers)
        idem = self._idem(headers)
        try:
            request = json.loads(body)
        except (UnicodeDecodeError, ValueError):
            return 401, dict(_DENIED)
        rec = self._verify(kid, plain)
        # 스크립트가 보낸 구독이 key 를 발급받은 구독과 다르면 거부(다른 고객 구독을 이 프로젝트에 올리는 것 방지)
        sb = (request.get("sourceBinding") or {}) if isinstance(request, dict) else {}
        if sb.get("subscriptionId") and str(sb["subscriptionId"]).lower() != rec.subscription_id:
            return 401, {"error": "COLLECTOR_SUBSCRIPTION_MISMATCH",
                         "message": "업로드 key를 발급받은 구독과 수집한 구독이 다릅니다."}
        sid = "upl_" + hashlib.sha256(f"{rec.owner_subject}\0{idem}".encode()).hexdigest()[:32]
        req_sha = hashlib.sha256(json.dumps(request, ensure_ascii=False, separators=(",", ":"),
                                            sort_keys=True).encode()).hexdigest()
        existing = self.intake.load_session(rec.owner_subject, idem)
        if existing is not None:
            if existing.get("requestSha256") != req_sha:
                return 401, dict(_DENIED)
            return 200, dict(existing["response"])
        resp = {"schema": "wara.manual-intake-session.v1", "sessionId": sid,
                "chunkSizeBytes": self.CHUNK, "status": "created"}
        self.intake.save_session(rec.owner_subject, idem, {"response": resp, "requestSha256": req_sha,
                                                           "caseId": rec.case_id, "keyId": kid})
        self.keys.bind_session(key_id=kid, session_id=sid, idempotency_key=idem)
        return 201, resp

    def _session(self, headers, sid):
        if _SESSION_ID.fullmatch(sid) is None:
            return None, None, None
        kid, plain = self._bearer(headers)
        idem = self._idem(headers)
        rec = self._verify(kid, plain)
        s = self.intake.load_session(rec.owner_subject, idem)
        if s is None or s["response"]["sessionId"] != sid:
            return None, None, None
        return kid, idem, rec

    def _chunk(self, headers, body, sid, role):
        if role not in self.ROLES:
            return 401, dict(_DENIED)
        kid, idem, rec = self._session(headers, sid)
        if rec is None:
            return 401, dict(_DENIED)
        m = _RANGE.fullmatch(str(headers.get("Content-Range", "")))
        if m is None:
            return 401, dict(_DENIED)
        start, end, total = (int(v) for v in m.groups())
        if end - start + 1 != len(body) or total > rec.max_upload_bytes:
            return 401, dict(_DENIED)
        if hashlib.sha256(body).hexdigest() != str(headers.get("X-Chunk-Sha256", "")).lower():
            return 401, dict(_DENIED)
        if role == EVIDENCE_ROLE:
            nxt = self._append_evidence(rec.owner_subject, idem, start, body)
        else:
            nxt = self.intake.append_chunk(rec.owner_subject, idem, role, offset=start, body=body)
        return 200, {"schema": "wara.manual-intake-chunk.v1", "role": role, "nextOffset": nxt}

    def _append_evidence(self, owner, idem, offset, body):
        """세 번째 role 청크 저장. 공용 저장소의 append_chunk 와 같은 규칙(순차 오프셋, 같은 바이트 재전송만 허용)."""
        import os
        part = self.intake._part_path(owner, idem, EVIDENCE_ROLE)
        part.parent.mkdir(parents=True, exist_ok=True)
        current = part.stat().st_size if part.is_file() else 0
        if offset == current:
            with open(part, "ab") as h:
                h.write(body)
                h.flush()
                os.fsync(h.fileno())
            return current + len(body)
        if offset < current and offset + len(body) <= current:
            with open(part, "rb") as h:
                h.seek(offset)
                if h.read(len(body)) == body:
                    return current
        raise self.KeyError_(status=409, code="CHUNK_OFFSET_CONFLICT", message="업로드 오프셋이 서버 상태와 일치하지 않습니다.")

    def _evidence_ok(self, data: bytes, sub: str) -> bool:
        """JSON 파싱·schema·구독 결속만 본다(내용 검증은 분석 엔진 단계)."""
        try:
            doc = json.loads(data.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError):
            return False
        return (isinstance(doc, dict) and doc.get("schema") == EVIDENCE_SCHEMA
                and str(doc.get("subscriptionId", "")).lower() == sub)

    def _complete(self, headers, body, sid):
        kid, idem, rec = self._session(headers, sid)
        if rec is None:
            return 401, dict(_DENIED), None
        try:
            request = json.loads(body)
        except (UnicodeDecodeError, ValueError):
            return 401, dict(_DENIED), None
        arts = request.get("artifacts") if isinstance(request, dict) else None
        if not isinstance(arts, list) or len(arts) not in (2, 3):
            return 401, dict(_DENIED), None
        roles = [(a or {}).get("role") if isinstance(a, dict) else None for a in arts]
        if len(set(roles)) != len(roles) or not self.REQUIRED <= set(roles):
            return 401, dict(_DENIED), None
        names = {}
        for a in arts:
            role, fn = (a or {}).get("role"), (a or {}).get("filename")
            pat = self.ROLES.get(role) if isinstance(role, str) else None
            if pat is None or not isinstance(fn, str) or pat.fullmatch(fn) is None:
                return 401, dict(_DENIED), None
            data = self.intake.artifact_bytes(rec.owner_subject, idem, role)
            if hashlib.sha256(data).hexdigest() != a.get("sha256"):
                return 401, dict(_DENIED), None
            if role == EVIDENCE_ROLE and not self._evidence_ok(data, rec.subscription_id):
                return 401, {"error": "AICAF_EVIDENCE_INVALID", "message": "aicaf-evidence.json 형식 또는 구독이 맞지 않습니다."}, None
            names[role] = fn
        # 다시 수집해 올리면 같은 파일명(topology.json)이 이미 있다. 이전 원본은 Source/_superseded 로 옮겨 보존한다.
        src = self.case_root / rec.case_id / "Source"
        for fn in names.values():
            old = src / fn
            if old.exists():
                keep = src / "_superseded" / f"{old.stem}.{int(old.stat().st_mtime)}{old.suffix}"
                keep.parent.mkdir(parents=True, exist_ok=True)
                old.replace(keep)
        stale = src / "aicaf-evidence.json"
        if EVIDENCE_ROLE not in names and stale.exists():
            keep = src / "_superseded" / f"aicaf-evidence.{int(stale.stat().st_mtime)}.json"
            keep.parent.mkdir(parents=True, exist_ok=True)
            stale.replace(keep)
        for old in src.glob("WARA-File*.json") if src.is_dir() else []:
            if old.name not in names.values():
                keep = src / "_superseded" / old.name
                keep.parent.mkdir(parents=True, exist_ok=True)
                old.replace(keep)
        written = self.intake.commit(rec.owner_subject, idem, case_id=rec.case_id, filenames=names)
        digests = {r: hashlib.sha256(Path(p).read_bytes()).hexdigest() for r, p in written.items()}
        # 게시 원장 기준 파일은 기존과 같게(두 필수 role 중 정렬 첫째) — 세 번째 role 이 끼어도 바뀌지 않게
        first = sorted(r for r in written if r in self.REQUIRED)[0]
        # 게시 원장은 (케이스, 이름) create-only 다. WARA 는 업로드마다 새 케이스라 문제가 없지만
        # AICAF 는 프로젝트당 케이스 하나에 다시 수집해 올리므로, 원장 이름에 key id 를 붙여 1회 게시를 기록한다.
        self.keys.reserve_publication_and_consume(
            key_id=kid, owner_subject=rec.owner_subject, case_id=rec.case_id,
            artifact_name=f"{kid[:16]}-{names[first]}"[:128], sha256=digests[first],
            size_bytes=Path(written[first]).stat().st_size)
        self.intake.discard(rec.owner_subject, idem)
        material = "\0".join(f"{n}:{digests[r]}" for r, n in sorted(names.items()))
        receipt_sha = hashlib.sha256(f"{rec.case_id}\0{material}".encode()).hexdigest()
        # 스크립트는 StrictMode 에서 receipt.revisionId·sessionId 를 읽는다. 없으면 업로드가 끝난 뒤
        # 마지막 결과 출력에서 'property cannot be found' 로 실패로 보인다(WARA 서버도 같은 결함).
        return 200, {"schema": "wara.manual-intake-registration-receipt.v1", "assessmentId": rec.case_id,
                     "revisionId": receipt_sha[:16], "sessionId": sid, "receiptSha256": receipt_sha,
                     "created": True, "artifacts": sorted(names.values())}, rec.case_id
