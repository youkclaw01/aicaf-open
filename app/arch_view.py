"""아키텍처 API. 원본·정상 개정 보존, 프로젝트 권한, 정본 스냅샷과 이중 보기 경계를 제공한다."""
import asyncio
import hashlib
import html
import json
import os
import re
import subprocess
import sys

import urllib.parse
import uuid
from pathlib import Path

from aiohttp import web

S = {}
STATES = ("AS-IS", "TO-BE")
DIAGRAMS = ("D1", "D2")
FILES = {"spec.md": "text/markdown", "spec.json": "application/json", "image.png": "image/png",
         "preview.png": "image/png", "ppt-preview.png": "image/png", "ppt-render.pdf": "application/pdf",
         "diagram.pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
         "layout.json": "application/json", "artifact-meta.json": "application/json", "provenance.json": "application/json"}
APP = Path(__file__).resolve().parent


def _store():
    from arch_store import Store
    return Store(Path(S["ROOT"]) / "arch")


def _scope(req, role="viewer"):
    aid = int(req.match_info["aid"])
    S["need_project"](req, aid, role)
    state = req.match_info.get("state", "").upper()
    diagram = req.match_info.get("d", "").upper()
    if state and (state not in STATES or diagram not in DIAGRAMS):
        raise web.HTTPNotFound()
    return aid, state, diagram


def _legacy(aid, state, diagram):
    return Path(S["ROOT"]) / "arch" / str(aid) / f"{state}_{diagram}"


def _read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _folder(aid, state, diagram, revision=None):
    if not revision or revision == "legacy":
        return _legacy(aid, state, diagram)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", revision):
        raise web.HTTPNotFound()
    row = _store().get(aid, state, diagram, revision)
    if not row:
        raise web.HTTPNotFound()
    path = Path(row["path"])
    root = _legacy(aid, state, diagram).resolve()
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise web.HTTPNotFound()
    return path


def _job(aid, state, diagram):
    legacy = _legacy(aid, state, diagram)
    row, normal = None, None
    try:
        store = _store()
        rows = store.list(aid, state, diagram)
        row = rows[-1] if rows else None
        normal = store.current_success(aid, state, diagram)
    except ImportError:
        pass
    folder = Path(row["path"]) if row else legacy
    j = _read(folder / "job.json", {}) or {}
    j.setdefault("status", row["status"] if row else "none")
    j.update(state=state, diagram=diagram, revision_id=row["revision_id"] if row else "legacy")
    j.pop("trace", None)
    if row:
        j["requirements"] = row["input"].get("requirements", "")
        j["parent_revision_id"] = row.get("parent_revision_id")
        p = row["input"]["prompt"]
        j["prompt"] = {k: p.get(k) for k in ("id", "version", "sha256")}
    selected = Path(normal["path"]) if normal else (legacy if (legacy / "image.png").is_file() else folder)
    selected_rev = normal["revision_id"] if normal else ("legacy" if selected == legacy else j["revision_id"])
    j["current_success_revision_id"] = normal["revision_id"] if normal else ("legacy" if selected == legacy and (legacy / "image.png").is_file() else None)
    j["view_revision_id"] = selected_rev
    meta = _read(selected / "artifact-meta.json")
    j["artifacts"] = meta
    j["files"] = [{"name": n, "bytes": (selected / n).stat().st_size,
                   "sha256": hashlib.sha256((selected / n).read_bytes()).hexdigest(), "revision_id": selected_rev}
                  for n in FILES if (selected / n).is_file()]
    return j


def _case_dir(aid):
    a = S["assessment"](aid)
    cid = a.get("infra_case_id")
    if not cid:
        return None, a
    cd = S["infra_case_dir"](cid)
    return (cd if (cd / "Source").is_dir() and any((cd / "Source").glob("topology*.json")) else None), a


async def api_arch(req):
    aid, _, _ = _scope(req)
    cd, assessment = await asyncio.to_thread(_case_dir, aid)
    jobs = {f"{state}_{d}": await asyncio.to_thread(_job, aid, state, d) for state in STATES for d in DIAGRAMS}
    try:
        prompt = await asyncio.to_thread(_store().current_prompt)
    except ImportError:
        prompt = None
    return web.json_response({"ready": bool(cd), "track": assessment.get("track"), "jobs": jobs,
                              "prompt_ready": bool(prompt), "prompt": {k: prompt.get(k) for k in ("id", "version", "sha256")} if prompt else None,
                              "tobe_source": "프로젝트 과제 원장 또는 근거 기반 현황 판정"})


def _launch(row):
    jd = Path(row["path"])
    log = open(jd / "job.log", "a", encoding="utf-8")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    flags = (0x08000000 | 0x00000008 | 0x01000000) if os.name == "nt" else 0
    try:
        p = subprocess.Popen([sys.executable, str(APP / "arch_revision_job.py"), str(jd)], stdout=log,
                             stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=str(APP), env=env,
                             creationflags=flags, close_fds=True)
        return p.pid
    finally:
        log.close()


def _error(exc):
    code = getattr(exc, "code", "arch_request_failed")
    status = getattr(exc, "status", 400)
    return web.json_response({"error": code, "detail": str(exc)}, status=status)


async def api_arch_start(req):
    aid, state, diagram = _scope(req, "interviewer")
    try:
        body = await req.json() if req.can_read_body else {}
        if not isinstance(body, dict):
            raise ValueError("input_object_required")
        requirements = body.get("requirements", "")
        if not isinstance(requirements, str) or len(requirements) > 12000:
            raise ValueError("requirements_invalid_or_too_long")
        cd, a = await asyncio.to_thread(_case_dir, aid)
        if not cd:
            raise web.HTTPConflict(text="인프라 분석 수집 자료가 아직 없습니다.")
        store = _store()
        current = store.current_prompt()
        if not current:
            raise web.HTTPConflict(text="승인된 상세 정본이 없습니다.")
        if ((body.get("prompt_version") and body["prompt_version"] != current["version"])
                or (body.get("prompt_sha256") and body["prompt_sha256"] != current["sha256"])):
            raise web.HTTPConflict(text="정본이 변경되었습니다. 현재 정본을 다시 읽어 요청하세요.")
        row, created = await asyncio.to_thread(store.reserve, aid, state, diagram,
                                              body.get("nonce") or uuid.uuid4().hex, requirements,
                                              req["user"]["upn"], str(cd), a.get("track") or "new",
                                              body.get("parent_revision_id"))
        selected_prompt = row["input"]["prompt"]
        if ((body.get("prompt_version") and body["prompt_version"] != selected_prompt["version"])
                or (body.get("prompt_sha256") and body["prompt_sha256"] != selected_prompt["sha256"])):
            if created:
                await asyncio.to_thread(store.finish, aid, state, diagram, row["revision_id"], "failed", {}, {}, "prompt_snapshot_conflict")
            raise web.HTTPConflict(text="정본 변경 경합이 확인되었습니다. 다시 읽고 요청하세요.")
        if created:
            try:
                pid = await asyncio.to_thread(_launch, row)
            except Exception:
                await asyncio.to_thread(store.finish, aid, state, diagram, row["revision_id"], "failed", {}, {}, "worker_launch_failed")
                raise
            S["audit"](req["user"]["upn"], aid, None, "arch-revision-start", f"{state} {diagram} rev={row['revision_id']} pid={pid}")
        return web.json_response(await asyncio.to_thread(_job, aid, state, diagram), status=202 if created else 200)
    except web.HTTPException:
        raise
    except ImportError:
        return web.json_response({"error": "arch_store_unavailable"}, status=503)
    except Exception as exc:
        return _error(exc)


async def api_prompt(req):
    aid, _, _ = _scope(req)
    if req.method == "GET":
        try:
            prompt = await asyncio.to_thread(_store().current_prompt)
        except ImportError:
            prompt = None
        return web.json_response({"ready": bool(prompt), "prompt": prompt})
    roles = req["user"].get("roles") or []
    if isinstance(roles, str):
        roles = json.loads(roles)
    if "aicaf.admin" not in roles:
        raise web.HTTPForbidden()
    try:
        pack = await req.json()
        prompt = await asyncio.to_thread(_store().save_prompt, pack, req["user"]["upn"])
        S["audit"](req["user"]["upn"], aid, None, "arch-prompt-save", prompt["version"])
        return web.json_response({"ready": True, "prompt": prompt})
    except Exception as exc:
        return _error(exc)


async def api_revisions(req):
    aid, state, diagram = _scope(req)
    rows = await asyncio.to_thread(_store().list, aid, state, diagram)
    return web.json_response({"revisions": [{k: row.get(k) for k in ("revision_id", "parent_revision_id", "status", "input_sha256", "created_at")} for row in rows]})


async def api_arch_file(req):
    aid, state, diagram = _scope(req)
    name = req.match_info["name"]
    if name not in FILES:
        raise web.HTTPNotFound()
    folder = await asyncio.to_thread(_folder, aid, state, diagram, req.query.get("rev"))
    p = folder / name
    if p.is_symlink() or not p.is_file():
        raise web.HTTPNotFound()
    sha = hashlib.sha256(p.read_bytes()).hexdigest()
    if req.query.get("sha256") and req.query["sha256"] != sha:
        raise web.HTTPConflict(text="산출물 해시가 일치하지 않습니다.")
    if name == "ppt-preview.png":
        receipt = _read(folder / "ppt-preview.binding.json")
        pptx = folder / "diagram.pptx"
        if not receipt or not pptx.is_file() or receipt.get("source_pptx_sha256") != hashlib.sha256(pptx.read_bytes()).hexdigest() or receipt.get("sha256") != sha:
            raise web.HTTPConflict(text="현재 PPT의 실제 렌더가 아닙니다.")
    a = S["assessment"](aid)
    nice = f"Architecture_{state}_{diagram}_{a['customer']}_{req.query.get('rev','legacy')}{p.suffix}"
    inline = name.endswith(".png") and req.query.get("dl") != "1"
    disp = "inline" if inline else f"attachment; filename*=UTF-8''{urllib.parse.quote(nice)}"
    return web.FileResponse(p, headers={"Content-Disposition": disp, "Content-Type": FILES[name], "Cache-Control": "no-store", "X-Content-SHA256": sha})


async def api_arch_view(req):
    aid, state, diagram = _scope(req)
    rev = req.query.get("rev") or "legacy"
    folder = await asyncio.to_thread(_folder, aid, state, diagram, rev)
    if not (folder / "image.png").is_file():
        raise web.HTTPNotFound()
    base = f"/api/aicaf/assessments/{aid}/arch/{state}/{diagram}/file/"
    def url(name, download=False):
        p = folder / name
        q = {"rev": rev, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
        if download:
            q["dl"] = "1"
        return base + name + "?" + urllib.parse.urlencode(q)
    receipt = _read(folder / "ppt-preview.binding.json")
    ppt = folder / "diagram.pptx"
    native = bool(receipt and ppt.is_file() and (folder / "ppt-preview.png").is_file()
                  and receipt.get("source_pptx_sha256") == hashlib.sha256(ppt.read_bytes()).hexdigest()
                  and receipt.get("sha256") == hashlib.sha256((folder / "ppt-preview.png").read_bytes()).hexdigest())
    original = html.escape(url("image.png"), quote=True)

    downloads = f'<a href="{html.escape(url("image.png", True), quote=True)}">이미지 원본 다운로드</a>'
    if ppt.is_file():
        downloads += f'<a href="{html.escape(url("diagram.pptx", True), quote=True)}">편집 가능 PPT 다운로드</a>'
    body = f'''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(state)} {diagram} 아키텍처</title><style>body{{font-family:"Malgun Gothic",sans-serif;margin:0;background:#f4f6fa;color:#262626}}header{{padding:16px;display:flex;gap:16px;align-items:center;background:white}}button,a{{padding:8px 12px}}.viewport{{height:calc(100vh - 110px);overflow:auto;background:white}}img{{max-width:none;width:100%;transform-origin:top left}}.disabled{{color:#5b6a77}}</style>
<header><button onclick="window.close();setTimeout(()=>document.getElementById('return').focus(),200)">닫기</button><a id="return" href="/interview/{aid}?tab=arch&amp;arch_state={state}" onclick="if(window.opener&&!window.opener.closed){{window.opener.focus();window.close();return false}}">원래 화면으로 안전하게 복귀</a>
<button onclick="choose('original')">이미지 원본</button><button {'disabled' if not native else ''} onclick="choose('ppt')">PPT 변환본</button>{downloads}<label>확대 <input type="range" min="50" max="250" value="100" oninput="document.getElementById('pic').style.width=this.value+'%'" /></label></header>
<p>개정 {html.escape(rev)} · {'실제 PPT 렌더 결합 확인' if native else '실제 PPT 렌더 미확인: 모사 이미지를 대체 표시하지 않음'}</p><div class="viewport"><img id="pic" src="{original}" alt="이미지 원본" /></div>
<script>const original={json.dumps(url('image.png'))},ppt={json.dumps(url('ppt-preview.png') if native else '')};function choose(kind){{const img=document.getElementById('pic');img.src=kind==='ppt'?ppt:original;img.alt=kind==='ppt'?'PPT 변환본 실제 렌더':'이미지 원본';}}</script></html>'''
    return web.Response(text=body, content_type="text/html", headers={"Cache-Control": "no-store"})


def register(app, **deps):
    """기존 공유 server 등록 인자는 유지하며 전용 라우트를 확장한다."""
    S.update(deps)
    base = "/api/aicaf/assessments/{aid}/arch"
    app.add_routes([web.get(base, api_arch), web.get(base + "/prompt", api_prompt), web.put(base + "/prompt", api_prompt),
                    web.get(base + "/{state}/{d}/revisions", api_revisions), web.post(base + "/{state}/{d}/start", api_arch_start),
                    web.get(base + "/{state}/{d}/view", api_arch_view), web.get(base + "/{state}/{d}/file/{name}", api_arch_file)])
