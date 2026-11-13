"""Cost Optimization 탭 API. server.py 는 register() 한 줄로 이 모듈을 붙인다(다른 탭 작업과 충돌 최소화).

- 계산: cost_calc.compute() 한 곳. 화면 JSON·엑셀·PPT 가 같은 결과를 쓴다.
- 가정값: data/cost/{aid}.json (디스크). 편집 즉시 저장·재계산.
- 보고서 생성: data/cost/{aid}/job.json 상태 파일 기반. 서버가 재시작되어도 상태가 남고,
  '진행 중'인데 심장 박동이 끊긴 작업은 실패로 바꿔 다시 생성할 수 있게 한다.
"""
import asyncio
import json
import os
import threading
import time
import urllib.parse
from pathlib import Path

from aiohttp import web

import cost_calc
import cost_report

S = {}  # register() 가 채우는 server.py 의 도구들


def _dir():
    d = Path(S["ROOT"]) / "cost"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _atomic(path, data):
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_bytes(data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))
    for i in range(5):
        try:
            tmp.replace(path)
            return
        except PermissionError:  # Windows: 남이 읽는 중이면 잠깐 뒤 다시
            time.sleep(0.2 * (i + 1))
    tmp.replace(path)


def _load_assumptions(aid):
    p = _dir() / f"{aid}.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _evidence(a):
    """인프라 분석 케이스의 aicaf-evidence.json. 없으면 None(화면은 '수집 안 됨' 표시)."""
    case_id = a.get("infra_case_id")
    if not case_id:
        return None, None
    p = S["infra_case_dir"](case_id) / "Source" / "aicaf-evidence.json"
    if not p.is_file():
        return None, None
    try:
        with open(p, encoding="utf-8-sig") as fh:
            return json.load(fh), p.stat().st_mtime
    except (OSError, ValueError):
        return None, None


_CACHE = {}


def _result(aid):
    a = S["assessment"](aid)
    ev, mtime = _evidence(a)
    track = a.get("track") or "new"
    saved = _load_assumptions(aid)
    with S["db"]() as c:
        ans = {r["qid"]: dict(r) for r in c.execute("select qid, score, summary, transcript from answers where assessment_id=?", (aid,))}
    qs = S['project_questions'](aid) if S.get('project_questions') else S['questions'](track)
    res = cost_calc.compute(ev, track, saved, qs, ans)
    res["customer"] = a["customer"]
    res["evidence_mtime"] = mtime
    return a, res


async def api_cost(req):
    aid = int(req.match_info["aid"])
    S["need_project"](req, aid)
    a, res = await asyncio.to_thread(_result, aid)
    res["job"] = _job_state(aid)
    return web.json_response(res, dumps=lambda o: json.dumps(o, ensure_ascii=False, default=str))


async def api_cost_assumptions(req):
    aid = int(req.match_info["aid"])
    S["need_project"](req, aid, "interviewer")
    body = await req.json()
    a = S["assessment"](aid)
    clean = cost_calc.merge_assumptions(body, a.get("track") or "new")  # 모르는 키·잘못된 값은 버린다
    _atomic(_dir() / f"{aid}.json", clean)
    S["audit"](req["user"]["upn"], aid, None, "cost-assumptions", json.dumps(body, ensure_ascii=False)[:500])
    return await api_cost(req)


async def api_cost_reset(req):
    aid = int(req.match_info["aid"])
    S["need_project"](req, aid, "interviewer")
    (_dir() / f"{aid}.json").unlink(missing_ok=True)
    S["audit"](req["user"]["upn"], aid, None, "cost-assumptions-reset", "")
    return await api_cost(req)


# ---------------- 보고서 생성 작업(디스크 상태) ----------------
STALE = 120  # 심장 박동이 이 시간(초) 이상 끊기면 중단된 작업으로 본다


def _jobdir(aid):
    d = _dir() / str(aid)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _job_state(aid):
    p = _jobdir(aid) / "job.json"
    try:
        j = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "none"}
    if j.get("status") == "running" and time.time() - j.get("beat", 0) > STALE:
        j.update(status="failed", error="서버 재시작 등으로 작업이 중단되었습니다. 다시 생성하세요.")
        _atomic(p, j)
    files = []
    for k in ("xlsx", "pptx"):
        f = _jobdir(aid) / f"cost.{k}"
        if j.get("status") == "done" and f.is_file():
            files.append({"kind": k, "bytes": f.stat().st_size})
    j["files"] = files
    return j


def _run_job(aid, actor):
    jd = _jobdir(aid)
    jp = jd / "job.json"
    st = {"status": "running", "started": time.time(), "beat": time.time(), "actor": actor,
          "steps": [{"name": "calc", "label": "계산", "status": "running"},
                    {"name": "xlsx", "label": "엑셀 작성", "status": "대기"},
                    {"name": "pptx", "label": "PPT 작성", "status": "대기"}]}
    _atomic(jp, st)

    def step(i, state, **kw):
        st["steps"][i]["status"] = state
        st["beat"] = time.time()
        st.update(kw)
        _atomic(jp, st)
    try:
        a, res = _result(aid)
        _atomic(jd / "result.json", json.dumps(res, ensure_ascii=False, default=str).encode("utf-8"))
        step(0, "ok")
        step(1, "running")
        _atomic(jd / "cost.xlsx", cost_report.build_xlsx(res, a["customer"]))
        step(1, "ok")
        step(2, "running")
        _atomic(jd / "cost.pptx", cost_report.build_pptx(res, a["customer"]))
        step(2, "ok", status="done", finished=time.time())
    except Exception as e:  # 실패는 화면에 사유를 남긴다
        for s_ in st["steps"]:
            if s_["status"] == "running":
                s_["status"] = "failed"
        st.update(status="failed", error=f"{type(e).__name__}: {e}"[:300], beat=time.time())
        _atomic(jp, st)


async def api_cost_build(req):
    aid = int(req.match_info["aid"])
    S["need_project"](req, aid, "interviewer")
    j = _job_state(aid)
    if j.get("status") == "running":
        raise web.HTTPConflict(text="이미 생성 중입니다.")
    threading.Thread(target=_run_job, args=(aid, req["user"]["upn"]), daemon=True).start()
    S["audit"](req["user"]["upn"], aid, None, "cost-build", "")
    await asyncio.sleep(0.2)
    return web.json_response(_job_state(aid))


async def api_cost_job(req):
    aid = int(req.match_info["aid"])
    S["need_project"](req, aid)
    return web.json_response(_job_state(aid))


async def api_cost_file(req):
    aid = int(req.match_info["aid"])
    S["need_project"](req, aid)
    kind = req.match_info["kind"]
    if kind not in ("xlsx", "pptx"):
        raise web.HTTPNotFound()
    p = _jobdir(aid) / f"cost.{kind}"
    if _job_state(aid).get("status") != "done" or not p.is_file():
        raise web.HTTPNotFound()
    a = S["assessment"](aid)
    name = f"Cost_Optimization_{a['customer']}.{kind}"
    S["audit"](req["user"]["upn"], aid, None, "cost-download", kind)
    return web.FileResponse(p, headers={"Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(name)}"})


def register(app, **deps):
    """deps: ROOT, db, need_project, assessment, infra_case_dir, audit, questions"""
    S.update(deps)
    base = "/api/aicaf/assessments/{aid}/cost"
    app.add_routes([
        web.get(base, api_cost),
        web.put(base + "/assumptions", api_cost_assumptions),
        web.delete(base + "/assumptions", api_cost_reset),
        web.post(base + "/build", api_cost_build),
        web.get(base + "/job", api_cost_job),
        web.get(base + "/file/{kind}", api_cost_file),
    ])
