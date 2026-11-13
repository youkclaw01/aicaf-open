"""아키텍처 작도 작업 1건(상태×도면) — 분리 프로세스로 돈다(포털 재시작에도 계속, 상태는 디스크 job.json).

단계(표준 §0): 명세(gpt-6-astra) → 이미지(gpt-image-2.5-sunburst, 샘플 2장 참조) → 판독(gpt-6-astra vision → layout.json)
→ PPT(코드: 네이티브 도형·공식 아이콘·연결선) → 검사(A1~A9). 이미지 쪽 실패(A4·A5·A9)는 직전 이미지+지적을 넣어 최대 3회 다시 그린다.
노드·개수·이름·연결은 골격(arch_facts.skeleton)이 정하고 모델은 바꾸지 못한다(모델이 낸 값은 문구·순서에만 쓰인다).

사용: python arch_job.py <job_dir>   (job_dir/input.json 에 case_dir·track·state·diagram)
"""
import base64
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import arch_facts as F  # noqa: E402
import arch_render as R  # noqa: E402

TEXT_URL = os.environ.get("AICAF_ARCH_TEXT_URL", "https://<FOUNDRY_ACCOUNT>.cognitiveservices.azure.com/openai/v1/responses")
TEXT_MODEL = os.environ.get("AICAF_ARCH_TEXT_MODEL", "gpt-6-astra")
IMG_URL = os.environ.get("AICAF_ARCH_IMG_URL", "https://<FOUNDRY_ACCOUNT>.openai.azure.com/openai/deployments/gpt-image-2.5-sunburst/images/edits?api-version=2025-04-01-preview")
IMG_MODEL = "gpt-image-2.5-sunburst"
SAMPLES = [Path(__file__).resolve().parent / "arch_samples" / n for n in ("sample-full-infra.jpg", "sample-network.jpg")]
MAX_ROUNDS = 3
STEPS = [("spec", "명세"), ("image", "이미지"), ("read", "판독"), ("pptx", "PPT"), ("check", "검사")]


def _atomic(path, obj):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    data = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8")
    tmp.write_bytes(data)
    for i in range(6):
        try:
            tmp.replace(path)
            return
        except PermissionError:  # Windows: 화면이 읽는 중이면 잠깐 뒤 다시
            time.sleep(0.2 * (i + 1))
    tmp.replace(path)


def _token():
    r = urllib.request.Request("http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01"
                               "&resource=https%3A%2F%2Fcognitiveservices.azure.com", headers={"Metadata": "true"})
    return json.loads(urllib.request.urlopen(r, timeout=15).read())["access_token"]


def _post(url, data, ctype, timeout=900, on_wait=None):
    """429(분당 호출 한도)는 'retry after N seconds' 를 지켜 최대 10회 기다린다 — 도면 4장이 동시에 이미지 배포를 쓰면 생긴다."""
    import random
    import re as _re
    for attempt in range(10):
        try:
            req = urllib.request.Request(url, data=data, headers={"Authorization": "Bearer " + _token(), "Content-Type": ctype})
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        except urllib.error.HTTPError as e:
            body = e.read()[:400].decode("utf-8", "replace")
            if e.code in (429, 500, 502, 503, 504) and attempt < 9:
                m = _re.search(r"retry after (\d+) second", body)
                wait = (int(m.group(1)) if m else 20 * (attempt + 1)) + random.uniform(3, 25)
                if on_wait:
                    on_wait(f"한도 대기 {int(wait)}초({attempt + 1})")
                time.sleep(wait)
                continue
            raise RuntimeError(f"HTTP {e.code}: {body}")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < 2:
                time.sleep(15)
                continue
            raise RuntimeError(f"network: {e}")


def _out_text(d):
    if d.get("output_text"):
        return d["output_text"]
    return "".join(c.get("text", "") for o in d.get("output") or [] if o.get("type") == "message" for c in o.get("content") or [])


def ask_json(prompt, image_png=None):
    content = [{"type": "input_text", "text": prompt}]
    if image_png:
        content.append({"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(image_png).decode()})
    body = {"model": TEXT_MODEL, "input": [{"role": "user", "content": content}], "text": {"format": {"type": "json_object"}}}
    return json.loads(_out_text(_post(TEXT_URL, json.dumps(body).encode(), "application/json", 600)))


def draw_image(prompt, refs, on_wait=None):
    """images/edits: 참조 이미지(샘플 2장, 재시도면 직전 그림까지)를 image[] 로 넣는다."""
    b = uuid.uuid4().hex
    out = b""
    for k, v in {"model": IMG_MODEL, "prompt": prompt, "size": "1536x1024", "n": "1", "quality": "medium"}.items():
        out += f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    for name, data, ct in refs:
        out += f'--{b}\r\nContent-Disposition: form-data; name="image[]"; filename="{name}"\r\nContent-Type: {ct}\r\n\r\n'.encode() + data + b"\r\n"
    out += f"--{b}--\r\n".encode()
    d = _post(IMG_URL, out, "multipart/form-data; boundary=" + b, 900, on_wait)
    return base64.b64decode(d["data"][0]["b64_json"])


# ---------------- 프롬프트(영문, 결과 문구는 한국어) ----------------
def spec_prompt(sk):
    brief = {"diagram": sk["diagram"], "state": sk["state"], "regions": sk["regions"],
             "nodes": [{k: n.get(k) for k in ("id", "label", "service", "count", "row", "parent", "finding", "task", "changed", "reason")} for n in sk["nodes"]],
             "containers": [{k: c.get(k) for k in ("id", "level", "label", "sub", "parent", "row", "style")} for c in sk["containers"]],
             "edges": sk["edges"], "findings": sk["findings"], "tasks": sk["tasks"]}
    return ("You are writing the drawing specification for an Azure architecture diagram. The skeleton below is FIXED: you may not add, "
            "remove, rename or recount any node, container or edge. Write in Korean. Return JSON with keys: "
            "'summary' (3 sentences: what this diagram shows and the main observation), "
            "'reasons' (object: node id -> one short Korean sentence why it sits in that row/container, based only on its 'reason' and evidence), "
            "'image_brief' (an English paragraph, <=180 words, describing the layout for an illustrator: region frame, rows top-to-bottom, "
            "which container holds which labels, management block position, arrow colours: inbound blue, outbound red, bidirectional black).\n"
            "SKELETON:\n" + json.dumps(brief, ensure_ascii=False))


def image_prompt(sk, brief, fix=None):
    labels = [n["label"] for n in sk["nodes"]]
    conts = [c["label"] for c in sk["containers"] if c.get("label")]
    bp = (sk.get("canvas") or {}).get("blueprint") or {}
    ten = bp.get("tenant_name") or sk["regions"][0]["name"]
    p = ("Draw an enterprise-grade Microsoft Azure architecture blueprint in the exact visual grammar of 'WARA AKS Standard' "
         "(white canvas with subtle engineering grid, top global entry bar with User/Public DNS/Entra ID rounded cards, "
         "orange-dashed Azure Region boundary with Subscription pill badge, blue-dashed VNet container with [RG-...] owner pill badges, "
         "green/teal dashed subnet containers with CIDR subtitles, right-hand purple-dashed 'Regional PaaS / Operations' and "
         "'Durable data / security' containers, horizontal resource cards with official flat Azure icon top-left, bold resource title, "
         "and 2-3 lines of concise technical metadata below, orthogonal elbow connectors with pill labels, and a 3-column bottom bar "
         "for CONNECTOR legend, Confirmed Baseline, and Verification Gate). Copy ONLY the visual grammar and layout structure — "
         "do NOT copy any sample resource names or unrelated text. "
         f"16:9 landscape. Header title: '{ten} — {sk['canvas']['title']}'.\n"
         f"Layout: {brief}\n"
         "Draw EXACTLY these objects, each once, with religious fidelity to this text label (no other services, no invented names): "
         + "; ".join(labels) + ".\nContainers (frames) with exactly these titles: " + "; ".join(conts) + ".")
    if sk["diagram"] == "D2":
        p += " Add a legend top-left with three arrows: red Outbound Traffic, blue Inbound Traffic, purple/black Private Link & Bidirectional traffic; a blue 'Private Network' band at the bottom of the VNet; a Management panel on the far right with a dark 'MS Network' bottom band."
    if fix:
        p += "\nREVISION of the previous drawing (the last reference image). Keep everything else, but fix: " + fix
    return p


def read_prompt(sk):
    want = [{"id": n["id"], "label": n["label"]} for n in sk["nodes"]]
    return ("Read this architecture diagram. For each expected object below, decide if it is drawn (an icon or box with that label or an "
            "obvious equivalent of the same label). Return JSON {\"nodes\":[{\"id\":...,\"found\":true|false,\"bbox\":[x,y,w,h] normalised 0..1 or null,"
            "\"seen_label\":\"text you read\"}], \"extras\":[{\"label\":\"text\",\"kind\":\"service|container|annotation\"}]}. "
            "'extras' = every additional SERVICE icon or box whose label is not in the expected list (ignore titles, legends, arrow labels, CIDR "
            "text, region names, row names). Expected objects: " + json.dumps(want, ensure_ascii=False))


def _load_project_reports(data_root, aid):
    """해당 프로젝트의 최신 현황분석(current-state) 및 종합(asis) 검토 완료 보고서를 읽는다."""
    out = {"current_state": None, "integrated": None}
    if not data_root or not aid:
        return out
    root = Path(data_root)
    for key, sub in (("current_state", "current-state"), ("integrated", "asis")):
        st_path = root / sub / str(aid) / "state.json"
        if not st_path.is_file():
            continue
        try:
            st = json.loads(st_path.read_text(encoding="utf-8"))
            rev = st.get("candidate_revision_id") or st.get("latest_revision_id")
            if not rev:
                continue
            rev_dir = root / sub / str(aid) / "revisions" / rev
            for fn in ("analysis-reviewed.json", "approved-report.json", "analysis.json"):
                fp = rev_dir / fn
                if fp.is_file():
                    out[key] = json.loads(fp.read_text(encoding="utf-8"))
                    break
        except Exception:
            continue
    return out


# ---------------- 메인 ----------------
def run(job_dir):
    jd = Path(job_dir)
    inp = json.loads((jd / "input.json").read_text(encoding="utf-8"))
    st = {"status": "running", "pid": os.getpid(), "started": time.time(), "beat": time.time(), "round": 0, "error": "",
          "steps": [{"name": k, "label": v, "status": "대기", "detail": ""} for k, v in STEPS], "checks": {}, "history": []}

    def save():
        st["beat"] = time.time()
        _atomic(jd / "job.json", st)

    def step(name, status, detail=""):
        for s in st["steps"]:
            if s["name"] == name:
                s["status"], s["detail"], s["at"] = status, detail, time.strftime("%H:%M:%S")
        save()

    save()
    try:
        topo, ev, topo_path = F.load(inp["case_dir"])
        facts = F.Facts(topo, ev)
        base_fnd = F.findings(facts)
        base_tks = F.tasks(facts, inp["track"], base_fnd)
        tp = jd.parent / "tobe-tasks.json"  # 06-D 과제 원장이 있으면 그것으로(없으면 현황분석·종합 보고서 또는 판정 기반 과제)
        aid = inp.get("assessment_id") or (int(jd.parent.name) if jd.parent.name.isdigit() else None)
        data_root = jd.parents[2] if len(jd.parents) >= 3 and jd.parent.parent.name == "arch" else None
        reps = _load_project_reports(data_root, aid)
        fnd_enriched, tks_enriched = F.enrich_from_reports(base_fnd, base_tks, reps["current_state"], reps["integrated"])
        override = None
        if tp.is_file():
            override = json.loads(tp.read_text(encoding="utf-8")).get("tasks")
        elif reps["integrated"] or reps["current_state"]:
            _atomic(tp, {"source": "AS-IS 현황분석 및 종합 시사점·개선과제 보고서 연동", "findings": fnd_enriched, "tasks": tks_enriched})
            override = tks_enriched
        sk = F.skeleton(topo, ev, inp["track"], inp["state"], inp["diagram"], override if inp["state"] == "TO-BE" else None, fnd_enriched)
        st["source"] = {"topology": topo_path, "task_source": "tobe-tasks.json (현황·종합보고서 연동)" if override else "현황 판정(06-D 블록 트리거)"}
        # 1) 명세
        step("spec", "running", TEXT_MODEL)
        sp = ask_json(spec_prompt(sk))
        for n in sk["nodes"]:
            r = (sp.get("reasons") or {}).get(n["id"])
            if isinstance(r, str) and r.strip():
                n["reason_text"] = r.strip()[:200]
        sk["summary"] = str(sp.get("summary") or "")[:800]
        brief = str(sp.get("image_brief") or "")[:1500]
        sk["md"] = spec_md(sk)
        _atomic(jd / "spec.json", sk)
        _atomic(jd / "spec.md", sk["md"].encode("utf-8"))
        step("spec", "ok", f"노드 {len(sk['nodes'])} · 선 {len(sk['edges'])}")
        refs = [(p.name, p.read_bytes(), "image/jpeg") for p in SAMPLES]
        fix, prev_img, final = None, None, None
        for rnd in range(1, MAX_ROUNDS + 1):
            st["round"] = rnd
            # 2) 이미지
            step("image", "running", f"{rnd}회차")
            img = draw_image(image_prompt(sk, brief, fix), refs + ([("previous.png", prev_img, "image/png")] if prev_img else []),
                             on_wait=lambda m: step("image", "running", f"{rnd}회차 · {m}"))
            _atomic(jd / f"image_r{rnd}.png", img)
            step("image", "ok", f"{rnd}회차")
            # 3) 판독
            step("read", "running", f"{rnd}회차")
            lay = ask_json(read_prompt(sk), img)
            _atomic(jd / f"layout_r{rnd}.json", lay)
            found = sum(1 for n in lay.get("nodes") or [] if n.get("found"))
            step("read", "ok", f"{found}/{len(sk['nodes'])}")
            # 4) PPT(아이콘·도형·선은 전부 코드)
            step("pptx", "running", f"{rnd}회차")
            boxes = (R.layout_d1 if sk["diagram"] == "D1" else R.layout_d2)(sk, lay)
            warn = []
            data, used = R.build_pptx(sk, boxes, facts.res, warn)
            _atomic(jd / f"diagram_r{rnd}.pptx", data)
            step("pptx", "ok", f"아이콘 {len(used)}종")
            # 5) 검사
            step("check", "running", f"{rnd}회차")
            res, icons = R.check(sk, boxes, data, lay, used, warn)
            st["checks"], st["icons"] = res, icons
            fails = [k for k, v in res.items() if not v["pass"]]
            st["history"].append({"round": rnd, "fails": fails, "detail": {k: res[k]["detail"] for k in res}})
            step("check", "ok" if not fails else "failed", "A1~A9 통과" if not fails else "미통과 " + ",".join(fails))
            final = (img, data, lay)
            img_fail = [k for k in fails if k in ("A4", "A5", "A9")]
            if not fails or not img_fail or rnd == MAX_ROUNDS:
                break  # 코드 쪽 실패(A1~A3·A6~A8)는 다시 그려도 같다 → 멈추고 사유를 남긴다
            fix = " ".join(res[k]["fix"] for k in img_fail)
            prev_img = img
            for s_ in st["steps"][1:]:
                s_["status"], s_["detail"] = "대기", ""
            save()
        img, data, lay = final
        _atomic(jd / "image.png", img)
        _atomic(jd / "diagram.pptx", data)
        _atomic(jd / "layout.json", lay)
        _atomic(jd / "preview.png", R.preview_png(sk, (R.layout_d1 if sk["diagram"] == "D1" else R.layout_d2)(sk, lay), facts.res))
        st["status"] = "done" if all(v["pass"] for v in st["checks"].values()) else "failed"
        if st["status"] == "failed":
            st["error"] = "검사 미통과: " + ", ".join(f"{k} {v['detail']}" for k, v in st["checks"].items() if not v["pass"])
        st["finished"] = time.time()
        save()
    except Exception as e:  # 실패 사유는 화면에 그대로
        for s_ in st["steps"]:
            if s_["status"] == "running":
                s_["status"] = "failed"
        st.update(status="failed", error=f"{type(e).__name__}: {e}"[:500], trace=traceback.format_exc()[-2000:])
        save()


def spec_md(sk):
    L = [f"# {sk['canvas']['title']}", "", f"- 도면: {sk['diagram']} · 상태: {sk['state']} · 고객 유형: {sk['track']}",
         f"- 리전: {', '.join(r['name'] for r in sk['regions'])}", f"- 노드 {len(sk['nodes'])} · 연결 {len(sk['edges'])}", ""]
    if sk.get("summary"):
        L += ["## 요약", sk["summary"], ""]
    if sk.get("findings"):
        has_obs = any(x.get("observation_refs") for x in sk["findings"])
        if has_obs:
            L += ["## 현황 판정(주황 테두리)", "| ID | 내용 | 원칙 | 현황분석 근거 | 대상 수 |", "|---|---|---|---|---|"]
            L += [f"| {x['id']} | {x['title']} | {x['principle']} | {', '.join(x.get('observation_refs') or [])} | {x['count']} |" for x in sk["findings"]]
        else:
            L += ["## 현황 판정(주황 테두리)", "| ID | 내용 | 원칙 | 대상 수 |", "|---|---|---|---|"]
            L += [f"| {x['id']} | {x['title']} | {x['principle']} | {x['count']} |" for x in sk["findings"]]
        L.append("")
    if sk.get("tasks"):
        L += ["## TO-BE 과제(초록)", "| 과제 | 이름 | 근거 | 원칙 |", "|---|---|---|---|"]
        L += [f"| {t['id']} | {t['name']} | {t['from']} | {t['principle']} |" for t in sk["tasks"]]
        L.append("")
    L += ["## 노드(배치 근거)", "| 노드 | 서비스 | 개수 | 행 | 근거 |", "|---|---|---|---|---|"]
    for n in sk["nodes"]:
        L.append(f"| {n['label']} | {n['service']} | {n.get('count', 1)} | R{n.get('row')} | {(n.get('reason_text') or n.get('reason') or '').replace('|', '/')} |")
    L += ["", "## 연결", "| 출발 | 도착 | 방향 | 라벨 | 근거 |", "|---|---|---|---|---|"]
    lab = {n["id"]: n["label"] for n in sk["nodes"]}
    for e in sk["edges"]:
        L.append(f"| {lab.get(e['from'], e['from'])} | {lab.get(e['to'], e['to'])} | {e['dir']} | {e.get('label', '')} | {e.get('evidence', '')} |")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    run(sys.argv[1])
