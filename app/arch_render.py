"""아키텍처 PPTX 렌더러 + 결정 검사(A1~A9) — 표준 arch/ARCH-DRAWING-STANDARD.md §2·§3·§4·§9.

- 좌표·크기·색·선·폰트·아이콘은 전부 이 파일의 코드가 정한다. 모델이 정하는 것은 같은 행 안의 순서(판독 결과)뿐이다.
- 아이콘은 icon-map.json(공식 V24, 정확한 파일명)으로만 고른다. 매핑 없는 타입은 경고로 남기고 아이콘을 그리지 않는다
  (일반 아이콘 대체 금지). 이미지 모델이 그린 아이콘은 쓰지 않는다 — PPT 는 전부 공식 SVG 를 다시 놓는다.
- 그리기 전에 기하 모델(boxes)을 먼저 만들고, 그 모델로 A1·A2·A3·A7 을 재고, 같은 모델로 PPT 와 미리보기 PNG 를 그린다.
"""
import io
import json
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.dml import MSO_LINE_DASH_STYLE
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Pt

import arch_facts as F

W, H = 13.333, 7.5
EMU = 914400
FONT = "맑은 고딕"
# §4 색 (WARA AKS Standard 엔터프라이즈 블루프린트 팔레트)
C = {"ink": "1A202C", "region": "D2691E", "region_fill": "FFFDF9", "vnet": "2E75B6", "vnet_fill": "F3F8FD",
     "subnet": "2E7D5B", "subnet_ing": "008080", "subnet_pe": "5B9BD5", "rg": "A0AEC0",
     "paas_border": "7030A0", "paas_fill": "F8F5FC", "data_border": "7030A0", "data_fill": "FBF9FF",
     "mgmt_border": "6B7280", "mgmt_fill": "F9FAFB", "common_fill": "FFFBEB", "common_border": "D97706",
     "chip": "F2F2F2", "yellow": "FFF2CC", "peach": "FCE4D6", "gray": "E2E8F0", "new": "2E7D5B", "issue": "C55A11",
     "red": "FF0000", "in": "0070C0", "out": "D9534F", "both": "7030A0", "muted": "4A5568", "white": "FFFFFF",
     "band": "2E75B6", "msband": "374151", "panel": "374151", "footer_bg": "FBFBFD", "footer_border": "CBD5E1",
     "pill_bg": "EDF2F7", "pill_border": "718096", "grid": "F1F5F9"}
EDGE_COLOR = {"in": C["in"], "out": C["out"], "both": C["both"]}
ICON_DIR_CANDIDATES = [Path(r"C:\AICAF\assets\azure-icons"), Path.home() / "projects/aicaf/arch/assets/azure-icons"]


def icon_root():
    for p in ICON_DIR_CANDIDATES:
        if (p / "icon-map.json").is_file():
            return p
    raise FileNotFoundError("azure-icons(icon-map.json) 를 찾을 수 없습니다.")


_IMAP = None


def icon_map():
    global _IMAP
    if _IMAP is None:
        r = icon_root()
        m = json.loads((r / "icon-map.json").read_text(encoding="utf-8"))
        _IMAP = (r, m)
    return _IMAP


def icon_key(node, facts_res):
    """노드 → icon-map 키. cognitiveservices 는 kind 로 나눈다(#aiservices/#openai). 그 밖에는 타입 그대로."""
    t = node.get("type") or ""
    if t == "microsoft.cognitiveservices/accounts":
        kinds = {((facts_res.get(m) or {}).get("kind") or "").lower() for m in node.get("members") or []}
        return "microsoft.cognitiveservices/accounts#openai" if kinds == {"openai"} else "microsoft.cognitiveservices/accounts#aiservices"
    return t


_PNG = {}


def icon_png(key, warnings):
    """공식 SVG → PNG(캐시). 매핑 없으면 None + 경고."""
    root, m = icon_map()
    rel = (m.get("map") or {}).get(key)
    if not rel:
        warnings.append(f"icon-map 에 없는 타입: {key} (아이콘 생략, 매핑 추가 필요)")
        return None, None
    if rel in _PNG:
        return _PNG[rel], rel
    svg = root / m["root"] / rel
    try:
        import resvg_py
        png = bytes(resvg_py.svg_to_bytes(svg_path=str(svg), width=128, height=128))
    except ImportError:
        import subprocess
        r = subprocess.run(["rsvg-convert", "-w", "128", "-h", "128", str(svg)], capture_output=True)
        if r.returncode == 0 and r.stdout:
            png = r.stdout
        else:
            b = io.BytesIO()
            Image.new("RGBA", (64, 64), (46, 117, 182, 255)).save(b, "PNG")
            png = b.getvalue()
    _PNG[rel] = png
    return png, rel


# ---------------- 기하 모델 ----------------
class Box:
    __slots__ = ("id", "kind", "x", "y", "w", "h", "parent", "data")

    def __init__(self, id, kind, x, y, w, h, parent=None, data=None):
        self.id, self.kind, self.x, self.y, self.w, self.h, self.parent, self.data = id, kind, x, y, w, h, parent, data or {}

    @property
    def r(self):
        return self.x + self.w

    @property
    def b(self):
        return self.y + self.h

    def inside(self, o, tol=0.005):
        return self.x >= o.x - tol and self.y >= o.y - tol and self.r <= o.r + tol and self.b <= o.b + tol

    def overlap(self, o, tol=0.01):
        return not (self.r <= o.x + tol or o.r <= self.x + tol or self.b <= o.y + tol or o.b <= self.y + tol)


def _text_w(s, pt):
    """글자 폭(인치) 근사: 한글 1.0em, 그 밖 0.55em."""
    em = pt / 72
    return sum(em * (1.0 if ord(c) >= 0x1100 else 0.62 if c.isupper() or c.isdigit() else 0.55) for c in s)


def _fit(s, w, pt):
    """폭 안에 들어가게 자른다(…). A7 라벨 넘침 방지."""
    if _text_w(s, pt) <= w:
        return s
    while s and _text_w(s + "…", pt) > w:
        s = s[:-1]
    return s + "…"


def _shrink(s, w, pt, min_pt=5.0):
    """이름은 자르지 않는다: 글자 크기를 min_pt 까지 줄여 맞추고, 그래도 넘치면 두 줄로 접는다.
    반환 (글자, 크기). 두 줄로도 넘칠 때만 둘째 줄을 자른다(A15 가 센다)."""
    p = pt
    while p > min_pt and _text_w(s, p) > w:
        p -= 0.5
    if _text_w(s, p) <= w:
        return s, p
    return _wrap2(s, w, p), p


def _wrap2(s, w, pt):
    """두 줄까지 줄바꿈(하이픈·밑줄·공백에서 우선), 넘치면 둘째 줄을 자른다. 이름은 자르기보다 접어서 보인다."""
    if _text_w(s, pt) <= w:
        return s
    # 두 줄이 모두 들어가는 끊는 자리 중 긴 줄이 가장 짧은 곳(공백·하이픈·밑줄 우선)
    best = None
    for i in range(1, len(s)):
        a, b = s[:i].rstrip(), s[i:].lstrip()
        if _text_w(a, pt) <= w and _text_w(b, pt) <= w:
            score = (0 if s[i - 1] in "-_ ." or s[i] == " " else 1, max(_text_w(a, pt), _text_w(b, pt)))
            if best is None or score < best[0]:
                best = (score, a + "\n" + b)
    if best:
        return best[1]
    cut = 0
    for i in range(len(s), 0, -1):
        if _text_w(s[:i], pt) <= w:
            cut = i
            break
    br = max([s.rfind(ch, 0, cut) + 1 for ch in "-_ ."] + [0])
    if br > cut * 0.5:
        cut = br
    return s[:cut] + "\n" + _fit(s[cut:], w, pt)


def _order(nodes, layout):
    """같은 그룹 안의 순서: 판독(layout.json)의 x→y 순. 판독에 없으면 골격 순서."""
    pos = {}
    for n in (layout or {}).get("nodes") or []:
        if n.get("id") and isinstance(n.get("bbox"), list) and len(n["bbox"]) == 4:
            pos[n["id"]] = (n["bbox"][1] // 0.12, n["bbox"][0])
    return sorted(nodes, key=lambda n: pos.get(n["id"], (99, nodes.index(n))))


def _grid(kids, x, y, w, h, parent, out, minw=0.62, maxw=1.6, maxh=1.0):
    """카드 격자 배치: 폭이 minw 이상이 되도록 열 수를 정하고 남으면 줄을 늘린다(A7 겹침·과소 방지).
    카드는 maxw×maxh 를 넘지 않게 하고 남는 공간은 가운데 정렬(카드가 상자를 꽉 채워 늘어나지 않게)."""
    n = len(kids)
    if not n:
        return
    cols = max(1, min(n, int(w // minw) or 1))
    rows = (n + cols - 1) // cols
    cw, ch = min(w / cols, maxw), min(h / rows, maxh)
    x += (w - cw * cols) / 2
    y += 0 if rows > 1 else 0
    for i, k in enumerate(kids):
        out.append(Box(k["id"], "card", x + (i % cols) * cw, y + (i // cols) * ch, cw - 0.04, ch - 0.04, parent, k))


CARD_W, CARD_H = 1.12, 0.66  # 카드 기본 크기(인치) — 아이콘 0.35 + 라벨 2줄 + 칩 1줄


def _need(n_cards, max_cols):
    """카드 n 개를 담는 데 필요한 (폭, 높이). 서브넷·묶음 상자를 내용에 맞춰 줄이는 데 쓴다(빈 공간 방지)."""
    cols = max(1, min(n_cards, max_cols))
    rows = (n_cards + cols - 1) // cols
    return cols * CARD_W + 0.14, rows * CARD_H + 0.34


def _xorder(items, key_nodes, layout):
    """판독(layout.json)에서 본 가로 순서로 열을 정렬한다 — 이미지 모델의 구도를 PPT 에 반영하는 지점."""
    pos = {}
    for n in (layout or {}).get("nodes") or []:
        if n.get("found") and isinstance(n.get("bbox"), list) and len(n["bbox"]) == 4:
            pos[n["id"]] = n["bbox"][0]
    def k(it):
        xs = [pos[i] for i in key_nodes(it) if i in pos]
        return (0, sum(xs) / len(xs)) if xs else (1, items.index(it))
    return sorted(items, key=k)


def _subw(s, kids_of):
    """서브넷 폭 = max(카드가 차지하는 폭, 이름이 6.5pt 로 한 줄에 들어가는 폭 + 배지 자리) — 이름이 잘리지 않게(A15)."""
    nb = len(s.get("badges") or [])
    return max(_need(len(kids_of.get(s["id"], [])) or 1, 3)[0], _text_w(s.get("label", ""), 6.8) + 0.22 + 0.2 * nb, 1.38)


def layout_d1(spec, layout=None):
    """D1(§2): 제목·범례 → 프레임 밖 Internet → 리전 프레임 안 왼쪽 열(공통 네트워크 위·관리 아래),
    가운데 VNet 열(서브넷을 역할 행 R1 진입·R2 관문·R3 앱·R4 데이터에 배치), 오른쪽 'PaaS · VNet 밖' 한 묶음(행별),
    바닥 가용 영역 띠. 상자 크기는 담긴 카드 수로 정한다(빈 상자·큰 빈 공간 금지 — A13)."""
    boxes = []
    nodes = spec["nodes"]
    regs = spec["regions"]
    prim, sec = regs[0], regs[1:]
    kids_of = {}
    for n in nodes:
        kids_of.setdefault(n["parent"], []).append(n)
    m = 0.34
    sec_w = 1.45 if sec else 0
    rx, ry = m, 1.02
    rw = W - 2 * m - (sec_w + 0.12 if sec else 0)
    rh = H - ry - 0.62
    boxes.append(Box(prim["id"], "region", rx, ry, rw, rh, None, {"label": f"Azure Region — {prim['name']}", "region_name": prim["name"]}))
    zone_h = 0.22
    inner_top, inner_bot = ry + 0.28, ry + rh - zone_h - 0.05
    # 왼쪽 열: 공통 네트워크(위) + 관리(아래)
    lw = 1.92
    common = kids_of.get("c:common", [])
    mg = kids_of.get("m:mgmt", [])
    cw_, ch_ = _need(len(common), 1)
    ch_ = min(ch_ if common else 0, (inner_bot - inner_top) * 0.44)
    if common:
        cb = Box("c:common", "group", rx + 0.08, inner_top, lw, max(0.92, (len(common) + 1) // 2 * CARD_H + 0.3), prim["id"],
                 {"label": "Common Network / DNS", "style": "yellow"})
        boxes.append(cb)
        _grid(common, cb.x + 0.04, cb.y + 0.24, cb.w - 0.08, cb.h - 0.28, "c:common", boxes, minw=0.88)
        my = cb.b + 0.08
    else:
        my = inner_top
    mb = Box("m:mgmt", "mgmt", rx + 0.08, my, lw, inner_bot - my, prim["id"], {"label": "Management & Governance"})
    boxes.append(mb)
    _grid(mg, mb.x + 0.04, mb.y + 0.32, mb.w - 0.08, min(mb.h - 0.36, ((len(mg) + 1) // 2) * 0.76), "m:mgmt", boxes, minw=0.88)
    # 가운데: VNet 열들(+ TO-BE 신규 VNet). 오른쪽: PaaS 묶음
    vnets = [c for c in spec["containers"] if c["level"] == "vnet" and c["parent"] == prim["id"]]
    vnets = _xorder(vnets, lambda v: [n["id"] for s in spec["containers"] if s.get("parent") == v["id"] for n in kids_of.get(s["id"], [])], layout)
    subs_of = {v["id"]: [s for s in spec["containers"] if s.get("parent") == v["id"]] for v in vnets}
    loose = [s for s in spec["containers"] if s["level"] == "subnet" and s["parent"] == prim["id"]]
    if loose:  # Hub 없이 생긴 TO-BE 서브넷 → 가상의 '신규 관문' 열
        vnets.insert(0, {"id": "v:loose", "label": "신규 관문 서브넷", "sub": "", "style": "new", "level": "vnet"})
        subs_of["v:loose"] = loose
    groups = [c for c in spec["containers"] if c["level"] == "group" and c["parent"] == prim["id"] and c["id"] != "c:common"
              and kids_of.get(c["id"])]
    # 행 높이 = 행마다 필요한 최대 높이(서브넷·묶음 기준), 남는 높이는 비율로 나눈다
    rows = (1, 2, 3, 4)
    need_h = {r: 0.0 for r in rows}
    need_w = {}
    for v in vnets:
        tot = 0
        for r in rows:
            rs = [s for s in subs_of[v["id"]] if s.get("row") == r]
            if not rs:
                continue
            ws = [_subw(s, kids_of) for s in rs]
            hs = [_need(len(kids_of.get(s["id"], [])) or 1, 3)[1] for s in rs]
            need_h[r] = max(need_h[r], max(hs))
            tot = max(tot, sum(ws) + 0.08 * (len(rs) - 1))
        need_w[v["id"]] = max(tot + 0.16, 1.3)
    paas_w = 0
    for g in groups:
        k = len(kids_of[g["id"]])
        w_, h_ = _need(k, 1)  # PaaS 묶음은 1열 세로 — 가운데 VNet 에 폭을 준다
        h_ = min(h_, 1.0 + 0.0 * k)
        need_h[g.get("row", 3)] = max(need_h[g.get("row", 3)], min(h_, CARD_H * 1.6) + 0.05)
        paas_w = max(paas_w, w_ + 0.2)
    used_rows = [r for r in rows if need_h[r] > 0]
    head = 0.42  # VNet 라벨 2줄
    avail_h = inner_bot - inner_top - head - 0.06 * len(used_rows)
    tot_h = sum(need_h[r] for r in used_rows) or 1
    scale = min(1.35, avail_h / tot_h)
    if scale < 1:  # 넘치면 카드 높이까지 줄여 맞춘다(A7 과소 검사가 하한을 지킨다)
        pass
    row_y, y = {}, inner_top + head
    for r in used_rows:
        row_y[r] = (y, need_h[r] * scale)
        y += need_h[r] * scale + 0.06
    cx0 = rx + 0.1 + lw + 0.14
    cx1 = rx + rw - 0.1 - (paas_w + 0.1 if groups else 0)
    tw = sum(need_w.values()) or 1
    wscale = min(1.5, (cx1 - cx0 - 0.1 * max(0, len(vnets) - 1)) / tw)
    x = cx0
    for v in vnets:
        w = need_w[v["id"]] * wscale
        vb = Box(v["id"], "vnet", x, inner_top, w, inner_bot - inner_top, prim["id"], v)
        boxes.append(vb)
        for r in used_rows:
            rs = [s for s in subs_of[v["id"]] if s.get("row") == r]
            if not rs:
                continue
            by_, bh_ = row_y[r]
            ws = [_subw(s, kids_of) * wscale for s in rs]
            k = min(1.0, (vb.w - 0.16 - 0.08 * (len(rs) - 1)) / sum(ws))
            sx = vb.x + 0.08
            for s_, sw in zip(rs, ws):
                kids = _order(kids_of.get(s_["id"], []), layout)
                nh = _need(len(kids) or 1, 3)[1] * scale
                sb = Box(s_["id"], "subnet", sx, by_, sw * k, min(bh_, nh), v["id"], s_)
                boxes.append(sb)
                _grid(kids, sb.x + 0.03, sb.y + 0.27, sb.w - 0.06, sb.h - 0.3, s_["id"], boxes, minw=0.85)
                sx += sw * k + 0.08
        x += w + 0.1
    if groups:
        px = rx + rw - 0.08 - paas_w
        pb = Box("g:paas", "group", px, inner_top, paas_w, inner_bot - inner_top, prim["id"], {"label": "Regional PaaS & Durable Data", "style": "purple"})
        boxes.append(pb)
        for g in sorted(groups, key=lambda g: g.get("row", 3)):
            r = g.get("row", 3)
            by_, bh_ = row_y.get(r, (inner_top + head, 1))
            kids = _order(kids_of[g["id"]], layout)
            nh = _need(len(kids), 1)[1] * scale
            label = {1: "External Edge", 2: "Gateway Plane", 3: "Regional PaaS / Operations", 4: "Durable data / security"}.get(r, "")
            gb = Box(g["id"], "rowgroup", px + 0.06, by_, paas_w - 0.12, min(bh_, nh), "g:paas", dict(g, label=label, style="purple"))
            boxes.append(gb)
            _grid(kids, gb.x + 0.03, gb.y + 0.25, gb.w - 0.06, gb.h - 0.28, g["id"], boxes, minw=0.85)
    # 행 이름표(프레임 안 오른쪽 끝 세로 대신 VNet 왼쪽 여백에 작은 글씨) — 3-Tier 가 보이게
    for r in used_rows:
        by_, bh_ = row_y[r]
        boxes.append(Box(f"rowlabel:{r}", "rowlabel", cx0 - 0.13, by_, 0.12, bh_, prim["id"],
                         {"label": {1: "진입", 2: "관문", 3: "앱", 4: "데이터"}[r]}))
    # 가용 영역 띠(바닥)
    boxes.append(Box("zoneband", "zoneband", rx + 0.1, inner_bot + 0.04, rw - 0.2, zone_h - 0.04, prim["id"], {"zones": spec.get("zoneFacts")}))
    # 보조 리전
    if sec:
        sx = rx + rw + 0.12
        sh = (rh - 0.1 * (len(sec) - 1)) / len(sec)
        for i, rg in enumerate(sec):
            y0 = ry + i * (sh + 0.1)
            kids = [n for g in spec["containers"] if g["parent"] == rg["id"] for n in kids_of.get(g["id"], [])]
            hh = min(sh, 0.4 + len(kids) * 0.82)
            boxes.append(Box(rg["id"], "region", sx, y0, sec_w, hh, None, {"label": f"{rg['name']} Region", "small": True}))
            for j, n in enumerate(kids):
                boxes.append(Box(n["id"], "card", sx + 0.08, y0 + 0.34 + j * 0.82, sec_w - 0.16, 0.76, rg["id"], n))
    # 외부: Internet 은 프레임 위(진입 카드 위쪽)에 — 선이 프레임 위 경계를 뚫고 들어간다(§2.1)
    ing = [e["to"] for e in spec["edges"] if e["from"] == "x:internet"]
    bx_ = {b.id: b for b in boxes}
    tx = [bx_[t].x + bx_[t].w / 2 for t in ing if t in bx_ and bx_[t].kind in ("card", "subnet")]
    ix = (sum(tx) / len(tx)) if tx else W - m - 1.6
    ix = min(max(ix - 0.6, 5.2), W - m - 1.5 - (1.45 if len(spec["external"]) > 1 else 0))
    for i, e in enumerate(spec["external"]):
        boxes.append(Box(e["id"], "external", ix + i * 1.45, 0.42, 1.38, 0.54, None, e))
    boxes.append(Box("x:out-of-scope", "oos", W - m - 1.25, H - 0.27, 1.2, 0.24, None, {"label": "범위 밖"}))
    return boxes


def layout_d2(spec, layout=None):
    """D2(§3.2): 범례 좌상단, VNet 경계 안 A 인바운드 축(R1 진입→R2 관문→R3 애플리케이션 띠), C 데이터 열(오른쪽),
    D 관리 패널 최우측, E/B 우상단 외부. 공통 네트워크 서비스는 흐름 축 밖 상단 띠. 선은 띠(lane) 단위."""
    boxes = []
    nodes = spec["nodes"]
    prim = spec["regions"][0]
    boxes.append(Box("d2:legend", "legend", 0.4, 0.62, 3.3, 0.7, None, {}))
    for nt in spec.get("notes") or []:
        boxes.append(Box("note:" + nt["anchor"], "note", 3.85, 0.62, 3.2, 0.7, None, nt))
        break
    for i, e in enumerate(spec["external"]):
        boxes.append(Box(e["id"], "external", 8.85 - i * 1.4, 0.55, 1.3, 0.75, None, e))
    boxes.append(Box("x:out-of-scope", "oos", 7.25, 0.62, 1.4, 0.3, None, {"label": "범위 밖"}))
    rx, ry, rw, rh = 0.3, 1.45, 10.0, 5.85
    boxes.append(Box(prim["id"], "region", rx, ry, rw, rh, None, {"label": f"{prim['name']} Region"}))
    common = [n for n in nodes if n["parent"] == "c:common"]
    cy = ry + 0.08
    if common:
        g = Box("c:common", "group", rx + 0.1, cy, rw - 0.2, 0.78, prim["id"], {"label": "Common Network Service", "style": "yellow"})
        boxes.append(g)
        _grid(common, g.x + 1.9, g.y + 0.06, g.w - 1.95, g.h - 0.1, "c:common", boxes, minw=0.9)
        cy += 0.88
    px, pw = 10.45, 2.5
    boxes.append(Box("m:mgmt", "panel", px, 0.55, pw, 6.75, None, {"label": "Management"}))
    boxes.append(Box("d2:msnet", "msband", px, 7.0, pw, 0.3, "m:mgmt", {"label": "MS Network"}))
    _grid([n for n in nodes if n["parent"] == "m:mgmt"], px + 0.08, 0.95, pw - 0.16, 5.95, "m:mgmt", boxes, minw=1.1)
    # 카드 분류: VNet 안(서브넷 소속) / VNet 밖(PaaS·공용 진입)
    inv = lambda n: n["parent"].startswith("s:")
    rows = {1: [], 2: [], 3: [], 4: []}
    for n in nodes:
        if n["parent"] in ("m:mgmt", "c:common"):
            continue
        if not inv(n) and n.get("row", 3) >= 3:
            rows[4].append(n)  # VNet 밖 PaaS 는 오른쪽 열(데이터·PaaS)
        elif inv(n) and n.get("row") == 4:
            rows[3].append(n)  # VNet 안 PE 는 애플리케이션 띠(사설 연결 지점)
        else:
            rows[min(3, max(1, n.get("row", 3)))].append(n)
    lx, lw = rx + 0.1, 6.95
    y = cy
    if rows[1]:
        lane = Box("lane:1", "lane", lx, y, lw, 0.95, prim["id"], {"label": "진입"})
        boxes.append(lane)
        _grid(_order(rows[1], layout), lane.x + 0.55, lane.y + 0.04, lane.w - 0.6, lane.h - 0.08, "lane:1", boxes, minw=0.9)
        y += 1.03
    vx, vy, vw, vh = lx, y, lw, ry + rh - y - 0.08
    boxes.append(Box("d2:vnet", "vnetline", vx, vy, vw, vh, prim["id"], {"label": "Virtual Networks · " + ", ".join(
        f"{c['label']} ({c.get('sub','').split(' · ')[0]})" for c in spec["containers"] if c["level"] == "vnet")}))
    boxes.append(Box("d2:privnet", "band", vx, vy + vh - 0.3, vw, 0.3, "d2:vnet", {"label": "Private Network"}))
    top, avail = vy + 0.3, vh - 0.66
    h2 = min(1.25, avail * 0.45) if rows[2] else 0
    yy = top
    for r, h in ((2, h2), (3, avail - h2 - (0.06 if h2 else 0))):
        if not rows[r]:
            continue
        lane = Box(f"lane:{r}", "lane", vx + 0.08, yy, vw - 0.16, h, "d2:vnet", {"label": {2: "관문", 3: "애플리케이션"}[r]})
        boxes.append(lane)
        _grid(_order(rows[r], layout), lane.x + 0.6, lane.y + 0.04, lane.w - 0.64, lane.h - 0.08, f"lane:{r}", boxes, minw=0.85, maxh=1.15)
        yy += h + 0.06
    if rows[4]:
        dx = vx + vw + 0.12
        lane = Box("lane:4", "lane", dx, cy, rx + rw - 0.1 - dx, ry + rh - 0.08 - cy, prim["id"], {"label": "PaaS · 데이터 (VNet 밖)"})
        boxes.append(lane)
        _grid(_order(rows[4], layout), lane.x + 0.04, lane.y + 0.26, lane.w - 0.08, lane.h - 0.3, "lane:4", boxes, minw=1.2)
    return boxes


def resolve(by, boxes, nid):
    """선 끝점 → 그림 상자. D2 처럼 서브넷 상자를 그리지 않는 도면에서는 그 서브넷에 속한 첫 카드로 푼다."""
    if nid in by:
        return by[nid]
    for b in boxes:
        if b.kind == "card" and (b.data or {}).get("parent") == nid:
            return b
    return None


# ---------------- PPT ----------------
def _rgb(h):
    return RGBColor.from_string(h)


def _noshadow(shp):
    sp = shp._element.spPr
    for e in sp.findall(qn("a:effectLst")):
        sp.remove(e)
    sp.append(sp.makeelement(qn("a:effectLst"), {}))


def _rect(s, b, fill, line, lw=0.75, dash=None, shape=MSO_SHAPE.RECTANGLE, name=None):
    r = s.shapes.add_shape(shape, Emu(int(b.x * EMU)), Emu(int(b.y * EMU)), Emu(int(b.w * EMU)), Emu(int(b.h * EMU)))
    if fill:
        r.fill.solid()
        r.fill.fore_color.rgb = _rgb(fill)
    else:
        r.fill.background()
    if line:
        r.line.color.rgb = _rgb(line)
        r.line.width = Pt(lw)
        if dash:
            r.line.dash_style = dash
    else:
        r.line.fill.background()
    if shape == MSO_SHAPE.ROUNDED_RECTANGLE:
        r.adjustments[0] = 0.04
    _noshadow(r)
    r.name = name or b.id[:60]
    return r


def _text(s, x, y, w, h, t, pt, color="000000", bold=False, align="l", name=None, underline=False, wrap=True):
    tb = s.shapes.add_textbox(Emu(int(x * EMU)), Emu(int(y * EMU)), Emu(int(w * EMU)), Emu(int(h * EMU)))
    tf = tb.text_frame
    tf.word_wrap = wrap
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = MSO_ANCHOR.TOP
    for i, line in enumerate(t.split("\n")):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = {"l": PP_ALIGN.LEFT, "c": PP_ALIGN.CENTER, "r": PP_ALIGN.RIGHT}[align]
        r = p.add_run()
        r.text = line
        r.font.size = Pt(pt)
        r.font.bold = bold
        r.font.underline = underline
        r.font.name = FONT
        r.font.color.rgb = _rgb(color)
        rpr = r._r.get_or_add_rPr()
        rpr.append(rpr.makeelement(qn("a:ea"), {"typeface": FONT}))
    if name:
        tb.name = name
    return tb


def _hatch(shp, color):
    """방화벽 서브넷 빨간 빗금(§2.2): 선 패턴 대신 패턴 채움(wdUpDiag) + 연노랑 배경."""
    sp = shp._element.spPr
    for tag in ("a:solidFill", "a:noFill", "a:pattFill", "a:gradFill"):
        for e in sp.findall(qn(tag)):
            sp.remove(e)
    pf = sp.makeelement(qn("a:pattFill"), {"prst": "wdUpDiag"})
    fg = pf.makeelement(qn("a:fgClr"), {})
    fg.append(fg.makeelement(qn("a:srgbClr"), {"val": "F4B6B6"}))
    bg = pf.makeelement(qn("a:bgClr"), {})
    bg.append(bg.makeelement(qn("a:srgbClr"), {"val": C["yellow"]}))
    pf.append(fg)
    pf.append(bg)
    ln = sp.find(qn("a:ln"))
    (ln.addprevious(pf) if ln is not None else sp.append(pf))


def _connector(s, a, b, color, dashed=False, label="", both=False):
    """직각 꺾은선(§3.4). a·b 는 Box. 화살촉 채움, 1.5pt."""
    ax, ay = a.x + a.w / 2, a.y + a.h / 2
    bx, by = b.x + b.w / 2, b.y + b.h / 2
    if abs(ay - by) > abs(ax - bx) or a.kind == "external" or b.kind == "external":  # 외부는 늘 위아래로(프레임 위 경계 통과)
        p1 = (ax, a.b if by > ay else a.y)
        p2 = (bx, b.y if by > ay else b.b)
    else:
        p1 = (a.r if bx > ax else a.x, ay)
        p2 = (b.x if bx > ax else b.r, by)
    c = s.shapes.add_connector(MSO_CONNECTOR.ELBOW, Emu(int(p1[0] * EMU)), Emu(int(p1[1] * EMU)),
                               Emu(int(p2[0] * EMU)), Emu(int(p2[1] * EMU)))
    c.line.color.rgb = _rgb(color)
    c.line.width = Pt(1.5)
    if dashed:
        c.line.dash_style = MSO_LINE_DASH_STYLE.DASH
    ln = c.line._get_or_add_ln()
    ln.append(ln.makeelement(qn("a:tailEnd"), {"type": "triangle"}))
    if both:
        ln.append(ln.makeelement(qn("a:headEnd"), {"type": "triangle"}))
    c.name = f"edge:{color}"
    if label and abs(p1[0] - p2[0]) + abs(p1[1] - p2[1]) > 0.5:
        # 라벨은 출발점 바로 옆(출발 상자 가장자리 밖) — 꺾은선 중간은 카드 위에 떨어지기 쉽다
        if abs(p1[1] - a.b) < 0.01:      # 아래로 출발
            return (p1[0] + 0.04, p1[1] + 0.02, label, color)
        if abs(p1[1] - a.y) < 0.01:      # 위로 출발
            return (p1[0] + 0.04, p1[1] - 0.17, label, color)
        if abs(p1[0] - a.r) < 0.01:      # 오른쪽으로 출발
            return (p1[0] + 0.04, p1[1] - 0.17, label, color)
        return (p1[0] - 0.04 - _text_w(label, 6.5) - 0.06, p1[1] - 0.17, label, color)
    return None


def _pill(s, x, y, w, h, text, pt=6.5, fill="EDF2F7", border="718096", color="1A202C", bold=True, name="pill"):
    b = Box(name, "pill", x, y, w, h)
    r = _rect(s, b, fill, border, 0.75, shape=MSO_SHAPE.ROUNDED_RECTANGLE, name=name)
    r.adjustments[0] = 0.25
    _text(s, x + 0.02, y + 0.015, w - 0.04, h - 0.02, _fit(text, w - 0.04, pt), pt, color, bold=bold, align="c", name=name + ":txt", wrap=False)
    return r


def build_pptx(spec, boxes, facts_res, warnings):
    """boxes(기하 모델) → PPTX 바이트 + 사용한 아이콘 경로 목록 (WARA AKS Standard 엔터프라이즈 블루프린트 규격)."""
    prs = Presentation()
    prs.slide_width, prs.slide_height = Emu(int(W * EMU)), Emu(int(H * EMU))
    s = prs.slides.add_slide(prs.slide_layouts[6])
    used_icons = []
    by = {b.id: b for b in boxes}
    d2 = spec["diagram"] == "D2"
    bp = (spec.get("canvas") or {}).get("blueprint") or {}
    # 0) 은은한 설계 그리드 배경 (슬라이드 내부)
    for gx in (2.5, 5.0, 7.5, 10.0, 12.5):
        ln_g = s.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Emu(int(gx * EMU)), Emu(int(0.05 * EMU)), Emu(int(gx * EMU)), Emu(int(7.45 * EMU)))
        ln_g.line.color.rgb = _rgb(C["grid"]); ln_g.line.width = Pt(0.25); ln_g.line.dash_style = MSO_LINE_DASH_STYLE.SQUARE_DOT; ln_g.name = "grid:v"
    for gy in (1.5, 3.0, 4.5, 6.0):
        ln_g = s.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Emu(int(0.1 * EMU)), Emu(int(gy * EMU)), Emu(int(13.2 * EMU)), Emu(int(gy * EMU)))
        ln_g.line.color.rgb = _rgb(C["grid"]); ln_g.line.width = Pt(0.25); ln_g.line.dash_style = MSO_LINE_DASH_STYLE.SQUARE_DOT; ln_g.name = "grid:h"
    # 1) 상단 타이틀 헤더 & 우측 상태 배지(§Z1)
    title = spec["canvas"]["title"]
    ten_name = bp.get("tenant_name") or spec["regions"][0]["name"]
    sub_short = bp.get("subscription_short") or "azure-sub"
    header_title = f"{ten_name} — {title}" if not d2 else f"{ten_name} — {title} ({'목표 설계' if spec['state'] == 'TO-BE' else '수집 실측'})"
    _text(s, 0.34, 0.08, 7.8, 0.34, _fit(header_title, 7.7, 16), 16, C["ink"], bold=True, name="title", wrap=False)
    sub_line = f"{sub_short} · {spec['regions'][0]['name']} · {'Target Architecture' if spec['state'] == 'TO-BE' else 'Observed Baseline'} · 노드 {len(spec['nodes'])}개 / 연결 {len(spec['edges'])}개"
    _text(s, 0.34, 0.38, 6.5, 0.2, _fit(sub_line, 6.4, 8.5), 8.5, C["muted"], name="subtitle", wrap=False)
    if not d2:
        badges_top = bp.get("status_badges") or (["AS-IS BASELINE", "EVIDENCE VERIFIED"] if spec["state"] == "AS-IS" else ["TARGET DESIGN", "TO-BE ARCHITECTURE"])
        _pill(s, 10.55, 0.09, 1.18, 0.22, badges_top[0], 6.5, "EBF8FF", C["in"], C["in"], name="badge:state1")
        _pill(s, 11.80, 0.09, 1.18, 0.22, badges_top[1], 6.5, "F0FFF4" if spec["state"] == "TO-BE" else "FFFAF0",
              C["new"] if spec["state"] == "TO-BE" else C["issue"], C["new"] if spec["state"] == "TO-BE" else C["issue"], name="badge:state2")
        _pill(s, 11.55, 0.34, 1.43, 0.19, bp.get("status_note") or ("실제 배포 전 검토본" if spec["state"] == "TO-BE" else "수집 증거 기반 실측본"),
              6.0, "F7FAFC", C["rg"], C["muted"], bold=False, name="badge:note")
        # 상단 좌측 외부 진입 보조 카드 (사용자/Reviewer & Entra ID 요약) — D1 상단 바 구성
        u_card = Box("top:user", "topcard", 0.34, 0.58, 2.15, 0.38)
        _rect(s, u_card, C["white"], C["mgmt_border"], 0.85, shape=MSO_SHAPE.ROUNDED_RECTANGLE, name="topcard:user")
        u_png, u_rel = icon_png("ext/users", warnings)
        if u_png and u_rel:
            s.shapes.add_picture(io.BytesIO(u_png), Emu(int(0.40 * EMU)), Emu(int(0.63 * EMU)), Emu(int(0.26 * EMU)), Emu(int(0.26 * EMU))).name = "icon:" + str(u_rel)
            used_icons.append(u_rel)
        _text(s, 0.72, 0.60, 1.72, 0.16, "사용자 / Reviewer", 7.5, C["ink"], bold=True, name="topcard:user:title", wrap=False)
        _text(s, 0.72, 0.76, 1.72, 0.16, _fit(f"HTTPS 443 · {bp.get('public_dns') or bp.get('entry_gateway') or '포털/관리 진입'}", 1.7, 6.0), 6.0, C["muted"], name="topcard:user:sub", wrap=False)
        e_card = Box("top:entra", "topcard", 2.62, 0.58, 2.25, 0.38)
        _rect(s, e_card, C["white"], "A64D79", 0.85, shape=MSO_SHAPE.ROUNDED_RECTANGLE, name="topcard:entra")
        e_png, e_rel = icon_png("mgmt/entra", warnings)
        if e_png and e_rel:
            s.shapes.add_picture(io.BytesIO(e_png), Emu(int(2.68 * EMU)), Emu(int(0.63 * EMU)), Emu(int(0.26 * EMU)), Emu(int(0.26 * EMU))).name = "icon:" + str(e_rel)
            used_icons.append(e_rel)
        _text(s, 3.00, 0.60, 1.82, 0.16, "Microsoft Entra ID", 7.5, C["ink"], bold=True, name="topcard:entra:title", wrap=False)
        _text(s, 3.00, 0.76, 1.82, 0.16, _fit(f"{ten_name} · Azure RBAC / OIDC", 1.8, 6.0), 6.0, C["muted"], name="topcard:entra:sub", wrap=False)
    # 2) 컨테이너(부모 먼저)
    depth = {"region": 0, "vnet": 1, "vnetline": 1, "group": 1, "mgmt": 1, "panel": 1, "subnet": 2, "rowgroup": 2, "lane": 2, "band": 3, "msband": 3,
             "rowlabel": 3, "zoneband": 3}
    for b in sorted([b for b in boxes if b.kind in depth], key=lambda b: depth[b.kind]):
        d = b.data
        new = d.get("style") == "new"
        if b.kind == "region":
            _rect(s, b, C["region_fill"], C["region"], 1.5, MSO_LINE_DASH_STYLE.DASH, name="region:" + b.id)
            t_, p_ = _shrink(d["label"], b.w - 0.16, 10.0 if not d.get("small") else 8.5, 6.5)
            _text(s, b.x + 0.08, b.y + 0.03, b.w - 0.16, 0.22, _fit(t_.replace("\n", " "), b.w - 0.16, p_) if "\n" in t_ else t_, p_,
                  "B45F06", bold=True, name="label:" + b.id, wrap=False)
            if not d.get("small") and not d2:
                _pill(s, b.r - 1.95, b.y + 0.04, 1.85, 0.19, f"Subscription: {sub_short}", 6.0, "FFFBEB", C["region"], "B45F06", name="pill:sub:" + b.id)
        elif b.kind == "vnet":
            _rect(s, b, C["vnet_fill"], C["new"] if new else C["vnet"], 1.75 if new else 1.25, MSO_LINE_DASH_STYLE.DASH, name="vnet:" + b.id)
            lab, lp_ = _shrink(d.get("label", ""), b.w - 0.14, 8.5, 5.5)
            _text(s, b.x + 0.08, b.y + 0.03, b.w - 0.14, 0.24, lab, lp_, C["new"] if new else C["vnet"], bold=True, name="label:" + b.id)
            sub = (d.get("sub") or "") + (f" · {d['task']}" if d.get("task") else "")
            if sub:
                sp_ = _shrink(sub, b.w - 0.14, 6.5, 5)[1]
                _text(s, b.x + 0.08, b.y + (0.27 if "\n" in lab else 0.17), b.w - 0.14, 0.13, _fit(sub, b.w - 0.14, sp_), sp_, C["muted"])
            if d.get("rg") and b.w >= 2.2:
                _pill(s, b.r - 1.18, b.y + 0.04, 1.10, 0.17, d["rg"], 5.8, C["white"], C["vnet"], C["vnet"], name="pill:vnetrg:" + b.id[:30])
        elif b.kind == "vnetline":
            _rect(s, b, None, C["vnet"], 1.25, MSO_LINE_DASH_STYLE.DASH, name="vnet:d2")
            vp_ = _shrink("<·> " + d["label"], b.w - 0.2, 8, 6)[1]
            _text(s, b.x + 0.08, b.y + 0.05, b.w - 0.2, 0.2, _fit("<·> " + d["label"], b.w - 0.2, vp_), vp_, C["vnet"], bold=True, wrap=False)
        elif b.kind in ("group", "rowgroup"):
            st_style = d.get("style")
            fill = {"yellow": C["common_fill"], "peach": C["mgmt_fill"], "purple": C["paas_fill"]}.get(st_style, C["white"])
            line_col = {"yellow": C["common_border"], "peach": C["mgmt_border"], "purple": C["paas_border"]}.get(st_style, C["rg"])
            lbl_col = C["paas_border"] if st_style == "purple" else ("B45309" if st_style == "yellow" else C["ink"])
            _rect(s, b, fill, line_col, 1.0 if st_style == "purple" else 0.75, MSO_LINE_DASH_STYLE.DASH, name="group:" + b.id)
            if b.kind == "group" or d.get("label"):
                _text(s, b.x + 0.06, b.y + 0.03, b.w - 0.12, 0.18, _fit(d.get("label", ""), b.w - 0.12, 7.5), 7.5, lbl_col, bold=True)
            if d.get("rg") and b.w >= 1.6 and b.kind == "rowgroup":
                _pill(s, b.r - 0.95, b.y + 0.03, 0.90, 0.15, d["rg"], 5.5, C["white"], line_col, lbl_col, name="pill:grprg:" + b.id[:30])
        elif b.kind == "mgmt":
            _rect(s, b, C["mgmt_fill"], C["mgmt_border"], 1.0, MSO_LINE_DASH_STYLE.DASH, name="mgmt")
            _text(s, b.x + 0.08, b.y + 0.04, b.w - 0.14, 0.20, d.get("label") or "Management & Governance", 8.0, C["ink"], bold=True)
            _text(s, b.x + 0.08, b.y + 0.19, b.w - 0.14, 0.12, "Application data path 분리 · 거버넌스/보안 통제", 5.8, C["muted"], wrap=False)
        elif b.kind == "panel":
            _rect(s, b, C["mgmt_fill"], C["panel"], 1.5, MSO_LINE_DASH_STYLE.DASH, name="mgmt")
            _text(s, b.x + 0.1, b.y + 0.08, b.w - 0.2, 0.25, "Management & Governance", 10, C["ink"], bold=True, underline=True)
        elif b.kind == "subnet":
            hatched = d.get("style") == "hatched-red"
            rz = d.get("role_zone") or ""
            sub_col = C["new"] if new else (C["red"] if hatched else (C["subnet_ing"] if rz == "ingress_egress" else (C["subnet_pe"] if rz == "private_pe" else C["subnet"])))
            r = _rect(s, b, C["white"], sub_col, 1.5 if (new or hatched) else 1.0,
                      None if hatched else MSO_LINE_DASH_STYLE.DASH, MSO_SHAPE.ROUNDED_RECTANGLE, name="subnet:" + b.id)
            if hatched:
                _hatch(r, C["red"])
            nb = len(d.get("badges") or [])
            lw_ = b.w - 0.08 - 0.18 * nb
            sl_ = d.get("label", "")
            badge_y = b.y + 0.02
            if _text_w(sl_, 5.5) > lw_:  # 이름이 배지와 겹치면 배지를 CIDR 줄 오른쪽으로 내린다
                lw_ = b.w - 0.08
                badge_y = b.y + 0.13
            p_ = _shrink(sl_, lw_, 6.8, 5)[1]
            _text(s, b.x + 0.04, b.y + 0.02, lw_, 0.13, _fit(sl_, lw_, p_), p_, sub_col, bold=True, name="label:" + b.id)
            if d.get("sub"):
                sub_txt = d["sub"] + (f" · {d['rg']}" if d.get("rg") and len(d["sub"]) < 16 else "")
                _text(s, b.x + 0.04, b.y + 0.13, b.w - 0.08, 0.11, _fit(sub_txt, b.w - 0.08, 5.8), 5.8, C["muted"])
            bx = b.r - 0.18
            for bd in d.get("badges") or []:
                key = {"nsg": "microsoft.network/networksecuritygroups", "pe": "microsoft.network/privateendpoints",
                       "nat": "microsoft.network/natgateways"}[bd]
                png, rel = icon_png(key, warnings)
                if png:
                    s.shapes.add_picture(io.BytesIO(png), Emu(int(bx * EMU)), Emu(int(badge_y * EMU)), Emu(int(0.14 * EMU)), Emu(int(0.14 * EMU))).name = "icon:" + rel
                    used_icons.append(rel)
                bx -= 0.18
        elif b.kind == "rowlabel":
            _text(s, b.x - 0.02, b.y + b.h / 2 - 0.25, 0.16, 0.5, "\n".join(d["label"]), 6.5, C["muted"], bold=True, align="c", name="rowlabel")
        elif b.kind == "zoneband":
            _rect(s, b, "F1F5F9", C["rg"], 0.5, name="zoneband")
            zs = spec.get("zoneFacts") or {}
            for i, z in enumerate(("1", "2", "3")):
                zx = b.x + (i + 0.5) * b.w / 3
                cnt = zs.get(z, 0)
                _text(s, zx - 0.9, b.y + 0.02, 1.8, b.h, f"Zone {z}" + (f" · 워크로드 {cnt}개" if cnt else " · 미지정"), 7.0, C["muted"], bold=True, align="c", name="zone")
            nz = zs.get("none", 0)
            if nz:
                _text(s, b.r - 2.3, b.y + 0.02, 2.25, b.h, f"가용영역 미지정(Regional/Single) {nz}개", 6.5, C["issue"], align="r", name="zone-none")
        elif b.kind == "lane":
            _rect(s, b, "F7F9FC" if d["label"] != "데이터" else "F8F5FC", C["paas_border"] if "PaaS" in d["label"] else C["rg"], 0.75, MSO_LINE_DASH_STYLE.DASH, name="lane:" + b.id)
            _text(s, b.x + 0.06, b.y + 0.04, (b.w - 0.12) if len(d["label"]) > 6 else 0.5, 0.26, d["label"], 8, C["paas_border"] if "PaaS" in d["label"] else C["muted"], bold=True)
        elif b.kind == "band":
            _rect(s, b, C["band"], None, name="privnet")
            _text(s, b.x, b.y + 0.05, b.w, b.h, "Private Network Boundary (VNet Injection & Private Link)", 9.5, "FFFFFF", bold=True, align="c")
        elif b.kind == "msband":
            _rect(s, b, C["msband"], None, name="msnet")
            _text(s, b.x, b.y + 0.05, b.w, b.h, "MS Network (Control & Security Plane)", 9.0, "FFFFFF", bold=True, align="c")
    # 3) 최하단 3열 아키텍처 범례·확정 기준·검증 바(§Z6, D1 전용)
    if not d2:
        fy, fh = H - 0.56, 0.50
        f_box = Box("footer:bar", "footer", 0.34, fy, W - 0.68, fh)
        _rect(s, f_box, C["footer_bg"], C["footer_border"], 0.75, shape=MSO_SHAPE.ROUNDED_RECTANGLE, name="footer:bar")
        # 1열: CONNECTOR & 범례 (A8/기존 테스트 호환 이름 유지)
        _text(s, 0.44, fy + 0.03, 1.5, 0.14, "CONNECTOR & LEGEND", 6.5, C["ink"], bold=True, name="footer:col1:hdr", wrap=False)
        for i, t in enumerate(spec["canvas"]["legend"]):
            bb = Box("lg" + t, "x", 1.85 + i * 0.48, fy + 0.03, 0.44, 0.14)
            _rect(s, bb, C["yellow"], C["rg"], 0.5, name="legendchip:" + t)
            _text(s, bb.x, bb.y + 0.01, bb.w, bb.h, t, 6.0, align="c", wrap=False)
        for i, (k, t) in enumerate((("in", "HTTPS / request"), ("out", "Egress / NAT"), ("both", "Private Link / Data"))):
            lx_ = 0.44 + i * 1.38
            ly_ = fy + 0.22
            c_ = s.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Emu(int(lx_ * EMU)), Emu(int((ly_ + 0.05) * EMU)),
                                        Emu(int((lx_ + 0.32) * EMU)), Emu(int((ly_ + 0.05) * EMU)))
            c_.line.color.rgb = _rgb(EDGE_COLOR[k]); c_.line.width = Pt(1.5)
            c_.line._get_or_add_ln().append(c_.line._get_or_add_ln().makeelement(qn("a:tailEnd"), {"type": "triangle"}))
            c_.name = f"legend:{k}"
            _text(s, lx_ + 0.36, ly_ - 0.01, 1.0, 0.13, t, 6.0, C["muted"], name="legendtext", wrap=False)
        if spec["state"] == "AS-IS":
            bb = Box("lgiss", "x", 0.44, fy + 0.35, 0.22, 0.11)
            _rect(s, bb, None, C["issue"], 1.5, shape=MSO_SHAPE.ROUNDED_RECTANGLE, name="legend:issue")
            _text(s, 0.70, fy + 0.34, 3.6, 0.12, "주황 테두리 = 현황 판정 미흡 리소스 (우상단 [F#] 표시)", 6.0, C["issue"], bold=True, wrap=False)
        else:
            bb = Box("lgnew", "x", 0.44, fy + 0.35, 0.22, 0.11)
            _rect(s, bb, None, C["new"], 1.75, shape=MSO_SHAPE.ROUNDED_RECTANGLE, name="legend:new")
            _text(s, 0.70, fy + 0.34, 3.6, 0.12, "초록 테두리 = TO-BE 개선과제 신규·변경 리소스 (우상단 [T-#] 표시)", 6.0, C["new"], bold=True, wrap=False)
        # 2열: 확정된 기준 (Confirmed Criteria)
        _text(s, 4.70, fy + 0.03, 3.8, 0.14, "확정된 아키텍처 기준 (Confirmed Baseline)", 6.5, C["new"], bold=True, name="footer:col2:hdr", wrap=False)
        for idx, cl in enumerate((bp.get("confirmed_criteria") or [])[:2]):
            _text(s, 4.70, fy + 0.18 + idx * 0.14, 4.0, 0.13, _fit("• " + cl, 3.95, 6.0), 6.0, C["ink"], name=f"footer:col2:l{idx}", wrap=False)
        # 3열: 중점 검증 / 개선과제 (Verification & Action Gate)
        _text(s, 8.85, fy + 0.03, 4.0, 0.14, "핵심 판정 및 구축 검증 항목 (Verification Gate)" if spec["state"] == "AS-IS" else "TO-BE 핵심 이행 과제 (Target Action Gate)",
              6.5, C["issue"], bold=True, name="footer:col3:hdr", wrap=False)
        for idx, vl in enumerate((bp.get("verification_items") or [])[:2]):
            _text(s, 8.85, fy + 0.18 + idx * 0.14, 4.05, 0.13, _fit("• " + vl, 4.0, 6.0), 6.0, C["ink"], name=f"footer:col3:l{idx}", wrap=False)
    # 4) 범례(D2 §3.1)·주석
    for b in boxes:
        if b.kind == "legend":
            _rect(s, b, C["footer_bg"], C["footer_border"], 0.75, shape=MSO_SHAPE.ROUNDED_RECTANGLE, name="d2:legendbox")
            for i, (k, t) in enumerate((("out", "Outbound / Egress Traffic"), ("in", "Inbound / Request Traffic"), ("both", "Private Link / Bidirectional"))):
                y = b.y + 0.05 + i * 0.21
                c = s.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Emu(int((b.x + 0.08) * EMU)), Emu(int((y + 0.09) * EMU)),
                                           Emu(int((b.x + 0.52) * EMU)), Emu(int((y + 0.09) * EMU)))
                c.line.color.rgb = _rgb(EDGE_COLOR[k])
                c.line.width = Pt(1.5)
                ln = c.line._get_or_add_ln()
                ln.append(ln.makeelement(qn("a:tailEnd"), {"type": "triangle"}))
                c.name = f"legend:{k}"
                _text(s, b.x + 0.60, y, 2.6, 0.18, ("적색 : " if k == "out" else "청색 : " if k == "in" else "보라 : ") + t, 8.0, C["ink"], bold=True)
        elif b.kind == "note":
            _rect(s, b, C["common_fill"], C["common_border"], 0.75, shape=MSO_SHAPE.ROUNDED_RECTANGLE, name="note")
            _text(s, b.x + 0.06, b.y + 0.05, b.w - 0.12, b.h - 0.1, b.data["text"].replace(" / ", "\n"), 7.5, C["ink"])
    # 5) 외부 진입 카드 (Internet / Public DNS / DDoS)
    for b in boxes:
        if b.kind == "external":
            if b.data["kind"] == "internet":
                r = _rect(s, b, C["white"], C["in"], 1.1, shape=MSO_SHAPE.ROUNDED_RECTANGLE, name="external:internet")
                dns_png, dns_rel = icon_png("microsoft.network/dnszones", warnings)
                if dns_png:
                    s.shapes.add_picture(io.BytesIO(dns_png), Emu(int((b.x + 0.06) * EMU)), Emu(int((b.y + 0.07) * EMU)), Emu(int(0.24 * EMU)), Emu(int(0.24 * EMU))).name = "icon:" + dns_rel
                    used_icons.append(dns_rel)
                ext_title = bp.get("public_dns") or "Internet / Public DNS"
                _text(s, b.x + 0.33, b.y + 0.05, b.w - 0.36, 0.18, _fit(ext_title, b.w - 0.36, 7.5), 7.5, C["in"], bold=True, align="l", wrap=False)
                _text(s, b.x + 0.33, b.y + 0.22, b.w - 0.36, 0.14, "Public Ingress · HTTPS 443", 5.8, C["muted"], align="l", wrap=False)
                if b.h >= 0.48:
                    _text(s, b.x + 0.06, b.y + 0.36, b.w - 0.12, 0.13, _fit(f"Target: {bp.get('entry_gateway') or 'Public Endpoint'}", b.w - 0.12, 5.5), 5.5, C["muted"], align="l", wrap=False)
            else:
                _rect(s, b, C["white"], C["vnet"], 0.9, shape=MSO_SHAPE.ROUNDED_RECTANGLE, name="external:ddos")
                png, rel = icon_png("microsoft.network/ddosprotectionplans", warnings)
                if png:
                    s.shapes.add_picture(io.BytesIO(png), Emu(int((b.x + 0.06) * EMU)), Emu(int((b.y + 0.08) * EMU)), Emu(int(0.26 * EMU)), Emu(int(0.26 * EMU))).name = "icon:" + rel
                    used_icons.append(rel)
                _text(s, b.x + 0.35, b.y + 0.08, b.w - 0.38, 0.2, _fit(b.data["label"], b.w - 0.38, 7.5), 7.5, C["ink"], bold=True, align="l")
                _text(s, b.x + 0.35, b.y + 0.26, b.w - 0.38, 0.14, "L3/L4 Edge Protection", 5.8, C["muted"], align="l", wrap=False)
        elif b.kind == "oos":
            if any(e["to"] == "x:out-of-scope" for e in spec["edges"]):
                _rect(s, b, C["gray"], C["muted"], 0.75, MSO_LINE_DASH_STYLE.DASH, MSO_SHAPE.ROUNDED_RECTANGLE, name="oos")
                _text(s, b.x, b.y + 0.04, b.w, b.h, "범위 밖(미수집)", 7.5, C["muted"], bold=True, align="c")
    # 6) 연결선 — 카드보다 먼저 그려 카드가 선 위에 오게 한다(선이 카드 글자를 가로지르지 않게)
    pending_labels = []
    seen_pairs, seen_labels = set(), set()
    for e in spec["edges"]:
        a, b = resolve(by, boxes, e["from"]), resolve(by, boxes, e["to"])
        if not a or not b or a is b or (a.id, b.id, e["dir"]) in seen_pairs:
            continue
        seen_pairs.add((a.id, b.id, e["dir"]))
        lt = e.get("label", "") + (" (추정)" if e.get("style") == "dashed-assumed" else "")
        if lt == "Private Link" or (a.id, lt) in seen_labels:
            lt = ""  # 범례가 설명하거나 같은 출발점에서 이미 붙인 라벨
        seen_labels.add((a.id, lt))
        lab = _connector(s, a, b, EDGE_COLOR.get(e["dir"], C["both"]), dashed=e.get("style") == "dashed-assumed",
                         label=lt, both=e["dir"] == "both")
        if lab:
            pending_labels.append(lab)
    # 7) 엔터프라이즈 리소스 카드(§2): 둥근 카드 테두리 + 아이콘 + 굵은 라벨 + 구조화된 3줄 기술 칩 + 판정/과제 배지
    for b in boxes:
        if b.kind != "card":
            continue
        n = b.data
        issue, new, changed = bool(n.get("finding")) and spec["state"] == "AS-IS", bool(n.get("new")), bool(n.get("changed"))
        pk = by.get(b.parent)
        # 기본 카드 배경 + 얇은 테두리 (화살표 가림 및 카드 경계 명확화)
        card_border = C["paas_border"] if (pk and (pk.data or {}).get("style") == "purple") else (C["mgmt_border"] if n.get("mgmt") else C["rg"])
        _rect(s, b, C["white"], card_border, 0.75,
              MSO_LINE_DASH_STYLE.DASH if new else MSO_LINE_DASH_STYLE.SOLID,
              MSO_SHAPE.ROUNDED_RECTANGLE, name="cardbg:" + n["id"][:50])
        if issue or new or changed:
            _rect(s, b, None, C["issue"] if issue else C["new"], 1.85 if new else 1.65,
                  MSO_LINE_DASH_STYLE.SOLID, MSO_SHAPE.ROUNDED_RECTANGLE, name=("issue:" if issue else "new:" if new else "chg:") + n["id"][:50])
        horiz = b.w >= 1.18 and b.h >= 0.56 and not n.get("mgmt")
        key = icon_key(n, facts_res)
        png, rel = icon_png(key, warnings)
        lab = n["label"]
        if n.get("count", 1) > 1 and not n.get("mgmt"):
            lab = n["service"] + f" : {n['count']}개"
        chips = list(n.get("chips") or [])
        if n.get("changed"):
            chips = [f"{t}: {c}" for t in (n.get("tasks") or [""])[:1] for c in n["changed"][:1]] + chips
        if horiz:
            isz = min(0.28, b.h * 0.36)
            if png:
                s.shapes.add_picture(io.BytesIO(png), Emu(int((b.x + 0.05) * EMU)), Emu(int((b.y + 0.05) * EMU)),
                                     Emu(int(isz * EMU)), Emu(int(isz * EMU))).name = "icon:" + str(rel)
                used_icons.append(rel)
            tx_x = b.x + isz + 0.08
            right_pad = 0.42 if (issue or new or changed) else 0.06
            tx_w = max(0.45, b.w - isz - 0.08 - right_pad)
            lab2, lp2 = _shrink(lab, tx_w, 7.0, 5.2)
            if "\n" in lab2:
                for p_ in (7.0, 6.5, 6.0, 5.5, 5.0, 4.5):
                    ws_ = lab.split(" ")
                    fits = [(" ".join(ws_[:i]), " ".join(ws_[i:])) for i in range(1, len(ws_))
                            if _text_w(" ".join(ws_[:i]), p_) <= tx_w and _text_w(" ".join(ws_[i:]), p_) <= tx_w]
                    if fits:
                        a_, b2_ = min(fits, key=lambda t: max(_text_w(t[0], p_), _text_w(t[1], p_)))
                        lab2, lp2 = a_ + "\n" + b2_, p_
                        break
            _text(s, tx_x, b.y + 0.04, tx_w, 0.25, lab2, lp2, C["ink"], bold=True, align="l", name="cardlabel:" + n["id"][:50])
            cy_start = b.y + max(isz + 0.08, 0.34 if "\n" in lab2 else 0.24)
            room = int(max(0, (b.b - cy_start - 0.02) // 0.12))
            for k, ch in enumerate(chips[:room]):
                cp_ = _shrink(ch, b.w - 0.10, 5.8, 4.8)[1]
                _text(s, b.x + 0.05, cy_start + k * 0.12, b.w - 0.10, 0.12, _fit(ch, b.w - 0.10, cp_), cp_, C["muted"], align="l", wrap=False)
        else:
            isz = min(0.30, b.h * 0.38, b.w * 0.42)
            if png:
                reps = min(3, n.get("count", 1)) if n.get("count", 1) > 1 and not n.get("mgmt") else 1
                gap = isz * 0.52
                x0 = b.x + b.w / 2 - (isz + gap * (reps - 1)) / 2
                for k in range(reps):
                    s.shapes.add_picture(io.BytesIO(png), Emu(int((x0 + k * gap) * EMU)), Emu(int((b.y + 0.03) * EMU)),
                                         Emu(int(isz * EMU)), Emu(int(isz * EMU))).name = "icon:" + rel
                    used_icons.append(rel)
            ly = b.y + 0.04 + isz
            lab2, lp2 = _shrink(lab, b.w - 0.04, 6.8, 5.2)
            if "\n" in lab2:
                for p_ in (6.8, 6.5, 6, 5.5, 5, 4.5):
                    ws_ = lab.split(" ")
                    fits = [(" ".join(ws_[:i]), " ".join(ws_[i:])) for i in range(1, len(ws_))
                            if _text_w(" ".join(ws_[:i]), p_) <= b.w - 0.04 and _text_w(" ".join(ws_[i:]), p_) <= b.w - 0.04]
                    if fits:
                        a_, b2_ = min(fits, key=lambda t: max(_text_w(t[0], p_), _text_w(t[1], p_)))
                        lab2, lp2 = a_ + "\n" + b2_, p_
                        break
            _text(s, b.x + 0.02, ly, b.w - 0.04, 0.24, lab2, lp2, C["ink"], bold=True, align="c", name="cardlabel:" + n["id"][:50])
            if "\n" in lab2:
                ly += 0.10
            room = int(max(0, (b.b - ly - 0.15) // 0.12))
            for k, ch in enumerate(chips[:room]):
                cp_ = _shrink(ch, b.w - 0.08, 5.8, 4.8)[1]
                _text(s, b.x + 0.03, ly + 0.15 + k * 0.12, b.w - 0.06, 0.12, _fit(ch, b.w - 0.08, cp_), cp_, C["muted"], align="c", wrap=False)
        if issue and b.w >= 0.85:
            _text(s, b.r - 0.42, b.y + 0.01, 0.40, 0.13, ",".join(n["finding"])[:12], 6.2, C["issue"], bold=True, align="r", name="findingid")
        if (new or changed) and b.w >= 0.85 and not any(tid_ in " ".join(chips) for tid_ in (n.get("tasks") or [n.get("task") or "-"])):
            tid = n.get("task") or ",".join(n.get("tasks") or [])
            _text(s, b.r - 0.62, b.y + 0.01, 0.60, 0.13, tid[:14], 6.2, C["new"], bold=True, align="r", name="taskid")
    # 8) 연결선 캡슐 라벨 배치
    placed = []
    for (lx, ly, label, color) in pending_labels:
        lw_ = _text_w(label, 6.2) + 0.12
        for _ in range(6):  # 다른 선 라벨과 겹치면 아래로 한 줄씩 비킨다
            if not any(not (lx + lw_ <= px or px + pw <= lx or ly + 0.16 <= py or py + 0.16 <= ly) for px, py, pw in placed):
                break
            ly += 0.16
        obst = [c for c in boxes if c.kind in ("card", "external", "note")]
        obst += [Box("hdr", "x", c.x, c.y, c.w, 0.26) for c in boxes if c.kind in ("vnetline", "vnet", "subnet", "lane", "rowgroup", "group")]
        if any(not (lx + lw_ <= c.x or c.r <= lx or ly + 0.16 <= c.y or c.b <= ly) for c in obst):
            continue
        placed.append((lx, ly, lw_))
        tb = _text(s, lx, ly, lw_, 0.16, label, 6.2, color, bold=True, align="c", name="edgelabel", wrap=False)
        tb.fill.solid()
        tb.fill.fore_color.rgb = _rgb("FFFFFF")
        tb.line.color.rgb = _rgb(color)
        tb.line.width = Pt(0.5)
    bio = io.BytesIO()
    prs.save(bio)
    return bio.getvalue(), sorted(set(used_icons))


# ---------------- 검사 A1~A9 ----------------
def check(spec, boxes, pptx_bytes, layout, used_icons, warnings):
    out = {}
    by = {b.id: b for b in boxes}
    regs = [by[r["id"]] for r in spec["regions"] if r["id"] in by]
    # A1: 리전이 가장 바깥 — 모든 컨테이너·카드가 어떤 리전 안(외부·범례·제목 제외)
    out_of = [b.id for b in boxes if b.kind not in ("region", "external", "oos", "legend", "note", "panel", "msband", "rowlabel")
              and b.parent != "m:mgmt" and not any(b.inside(r) for r in regs)]
    if spec["diagram"] == "D2":
        out_of = [i for i in out_of if not i.startswith("m:")]
    out["A1"] = {"pass": not out_of, "detail": f"리전 밖 개체 {len(out_of)}", "items": out_of[:10],
                 "fix": "Region frame must enclose every other container."}
    # A2: 행 순서(같은 리전·같은 열 기준, 외부/관문 < 앱 < 데이터 — y 중심)
    bad = []
    cards = [b for b in boxes if b.kind == "card" and not b.data.get("mgmt") and b.parent != "c:common"]  # 공통 서비스는 흐름 축 밖
    d2 = spec["diagram"] == "D2"
    for r in regs:
        inr = [b for b in cards if b.inside(r)]
        if d2:  # §3.2: 띠 순서 진입<관문<애플리케이션(위→아래), PaaS·데이터 열은 축 오른쪽 — 실제 배치된 띠로 잰다
            ln = lambda b: int(b.parent.split(":")[1]) if (b.parent or "").startswith("lane:") else 0
            for a in [b for b in inr if ln(b) in (1, 2, 3)]:
                for b in [b for b in inr if ln(b) == 4]:
                    if b.x < a.r:
                        bad.append(f"{b.data['label']}(PaaS·데이터 열) 가 {a.data['label']} 왼쪽")
                for b in [b for b in inr if ln(b) in (1, 2, 3) and ln(b) > ln(a)]:
                    if a.y + a.h / 2 >= b.y + b.h / 2:
                        bad.append(f"{a.data['label']}(띠{ln(a)}) ≥ {b.data['label']}(띠{ln(b)})")
            continue
        for lo, hi in ((1, 3), (2, 3), (3, 4), (1, 4), (2, 4)):
            A = [b for b in inr if b.data.get("row") == lo]
            B = [b for b in inr if b.data.get("row") == hi]
            for a in A:
                for b in B:
                    if a.y + a.h / 2 >= b.y + b.h / 2:
                        bad.append(f"{a.data['label']}(R{lo}) ≥ {b.data['label']}(R{hi})")
    out["A2"] = {"pass": not bad, "detail": f"행 역전 {len(bad)}", "items": bad[:10],
                 "fix": "Move each object into its specified row: external/gateway row above application row above data row."}
    # A3: 관리 서비스는 관리 블록 안, 관리 블록 밖에 관리 타입 없음
    mg = by.get("m:mgmt")
    mis = [b.data["label"] for b in boxes if b.kind == "card" and b.data.get("mgmt") and not (mg and b.inside(mg))]
    leak = [b.data["label"] for b in boxes if b.kind == "card" and not b.data.get("mgmt")
            and (b.data.get("type") in F.MGMT_TYPES)]
    out["A3"] = {"pass": bool(mg) and not mis and not leak, "detail": f"관리 블록 {'있음' if mg else '없음'} · 밖 {len(mis)} · 흐름 축 안 관리 {len(leak)}",
                 "items": mis + leak, "fix": "Move management services into the Management block."}
    # A4: 판독 노드 수·이름 = spec
    want = {n["id"]: n["label"] for n in spec["nodes"]}
    got = {}
    for n in (layout or {}).get("nodes") or []:
        if n.get("id") in want and n.get("found", True):
            got[n["id"]] = n
    missing = [want[i] for i in want if i not in got]
    out["A4"] = {"pass": not missing and len(got) == len(want), "detail": f"판독 {len(got)}/{len(want)} · 일치율 {round(100*len(got)/max(1,len(want)))}%",
                 "items": missing[:15], "fix": "These objects are missing from the drawing; draw each exactly once with its exact label: " + "; ".join(missing[:25])}
    # A5: 증거에 없는 서비스 0 — PPT 노드는 증거/과제 근거, 판독 추가물(서비스) 0
    rids = set(spec.get("resourceIds") or [])
    unb = [n["label"] for n in spec["nodes"] if not n.get("mgmt") and not n.get("task")
           and not set(n.get("members") or []) <= rids]
    extras = [x.get("label", "") for x in (layout or {}).get("extras") or [] if (x.get("kind") or "") == "service"]
    out["A5"] = {"pass": not unb and not extras, "detail": f"근거 없는 노드 {len(unb)} · 그림에 추가된 서비스 {len(extras)}",
                 "items": (unb + extras)[:10], "fix": "Remove these objects; they are not in the specification: " + "; ".join(extras[:20])}
    # A6: 선 색 = 방향
    prs = Presentation(io.BytesIO(pptx_bytes))
    sl = prs.slides[0]
    edge_shapes = [sh for sh in sl.shapes if sh.name.startswith("edge:")]
    want_cols, sp_ = [], set()
    for e in spec["edges"]:
        a_, b_ = resolve(by, boxes, e["from"]), resolve(by, boxes, e["to"])
        if a_ and b_ and a_ is not b_ and (a_.id, b_.id, e["dir"]) not in sp_:
            sp_.add((a_.id, b_.id, e["dir"])); want_cols.append(EDGE_COLOR.get(e["dir"]))
    got_cols = [sh.name.split(":")[1] for sh in edge_shapes]
    wrong = [i for i, (a, b) in enumerate(zip(want_cols, got_cols)) if a != b]
    out["A6"] = {"pass": len(want_cols) == len(got_cols) and not wrong, "detail": f"선 {len(got_cols)}개 · 색 불일치 {len(wrong)}",
                 "items": wrong[:10], "fix": "inbound=blue, outbound=red, bidirectional=black"}
    # A7: 겹침 0(형제 카드·형제 컨테이너), 라벨 프레임 안(_fit 으로 폭 보장, 높이 측정)
    ov = []
    sib = {}
    for b in boxes:
        if b.kind in ("card", "subnet", "vnet", "group", "rowgroup", "region", "mgmt", "panel", "external", "lane"):
            sib.setdefault((b.parent, b.kind == "card"), []).append(b)
    for k, lst in sib.items():
        for i in range(len(lst)):
            for j in range(i + 1, len(lst)):
                if lst[i].overlap(lst[j]):
                    ov.append(f"{lst[i].id[-30:]} ∩ {lst[j].id[-30:]}")
    W_, H_ = W, H
    off = [sh.name for sh in sl.shapes if sh.left is not None and (sh.left < 0 or sh.top < 0 or
           (sh.left + sh.width) / EMU > W_ + 0.01 or (sh.top + sh.height) / EMU > H_ + 0.01)]
    small = [b.id[-30:] for b in boxes if b.kind == "card" and (b.w < 0.45 or b.h < 0.42)]
    out["A7"] = {"pass": not ov and not off and not small, "detail": f"겹침 {len(ov)} · 슬라이드 밖 {len(off)} · 너무 작은 카드 {len(small)}",
                 "items": (ov + off + small)[:10], "fix": "code relayout"}
    # A8: D2 범례 3·Private Network 띠·관리 패널
    if spec["diagram"] == "D2":
        names = [sh.name for sh in sl.shapes]
        lg = len([n for n in names if n.startswith("legend:")])
        ok = lg == 3 and "privnet" in names and "mgmt" in names
        out["A8"] = {"pass": ok, "detail": f"범례 {lg}/3 · Private Network {'있음' if 'privnet' in names else '없음'} · 관리 패널 {'있음' if 'mgmt' in names else '없음'}",
                     "items": [], "fix": "Add the 3-colour legend, the Private Network band and the Management panel."}
    else:
        out["A8"] = {"pass": True, "detail": "D1 해당 없음(D2 전용 검사)", "items": [], "fix": ""}
    # A9: 샘플 고유 명칭 0(PPT 글자 + 판독 글자 + spec.md)
    texts = [sh.text_frame.text for sh in sl.shapes if sh.has_text_frame]
    texts += [x.get("label", "") for x in (layout or {}).get("extras") or []] + [spec.get("md", "")]
    hits = F.a9_hits(texts)
    out["A9"] = {"pass": not hits, "detail": f"샘플 고유 명칭 {len(hits)}건", "items": hits[:10], "fix": "Remove sample-only names: " + "; ".join(hits[:10])}
    d1 = spec["diagram"] == "D1"
    names = [sh.name for sh in sl.shapes]
    texts_all = [sh.text_frame.text for sh in sl.shapes if sh.has_text_frame]
    # A10: 외부 진입 — Internet 이 프레임 밖 위, inbound 선이 Internet 에서 출발해 리전 안 개체로 들어간다
    inet = by.get("x:internet")
    inb = [e for e in spec["edges"] if e["from"] == "x:internet" and e["dir"] == "in" and e["to"] in by]
    in_frame = [e for e in inb if any(by[e["to"]].inside(r) for r in regs)]
    above = bool(inet) and bool(regs) and inet.b <= regs[0].y + 0.01
    want_ing = any(a_[0] == "x:internet" for a_ in spec.get("drawableRelations") or [])
    ok10 = (not want_ing) or (above and len(in_frame) > 0)
    out["A10"] = {"pass": ok10, "detail": f"Internet {'프레임 위' if above else '위치 이상'} · inbound 선 {len(in_frame)}"
                  + ("" if want_ing else " (수집된 공개 진입점 없음)"), "items": [], "fix": "code: place Internet above the region frame and draw inbound edges"}
    # A11: 수집 관계 중 그릴 대상 쌍의 80% 이상이 선으로(카드 단위로 접은 뒤)
    want = {tuple(x) for x in spec.get("drawableRelations") or []}
    drawn_pairs = {(e["from"], e["to"]) for e in spec["edges"] if resolve(by, boxes, e["from"]) and resolve(by, boxes, e["to"])}
    drawn_pairs |= {tuple(x) for x in spec.get("replacedRelations") or []}
    hit = len(want & drawn_pairs)
    ratio = hit / len(want) if want else 1.0
    out["A11"] = {"pass": ratio >= 0.8, "detail": f"그릴 관계 {len(want)} · 그린 선 {hit} · {round(ratio*100)}%",
                  "items": [f"{a_}→{b_}" for a_, b_ in sorted(want - drawn_pairs)][:10], "fix": "code: draw collected relations"}
    # A12: '×' 뒤 숫자 필수, ARM 타입명 원형 노출 0
    bad12 = [t for t in texts_all if re.search(r"×(?!\s*\d)", t)]
    bad12 += [t for t in texts_all if re.search(r"(?<![\w/])(?:[a-z]+(?:gateways|accounts|vaults|workspaces|clusters|zones|profiles|sites|registries)\b)", t)]
    out["A12"] = {"pass": not bad12, "detail": f"개수·표시명 결함 {len(bad12)}", "items": bad12[:10], "fix": "code: counts and display names"}
    # A13: 빈 상자 0 · 채움률(서브넷·묶음 상자에서 카드가 차지하는 비율) 하한 25%
    conts = [b_ for b_ in boxes if b_.kind in ("subnet", "rowgroup", "group") and b_.id not in ("g:paas",)]
    empty = [b_.id[-30:] for b_ in conts if not any(c.parent == b_.id for c in boxes if c.kind == "card")]
    low = []
    for b_ in conts:
        cs = [c for c in boxes if c.kind == "card" and c.parent == b_.id]
        if cs:
            fill = sum(min(c.w, 1.15) * min(c.h, 0.7) for c in cs) / max(0.01, b_.w * b_.h)
            if fill < 0.25:
                low.append(f"{(b_.data or {}).get('label', b_.id)[:24]} {round(fill*100)}%")
    out["A13"] = {"pass": not empty and not low, "detail": f"빈 상자 {len(empty)} · 채움률 25% 미만 {len(low)}",
                  "items": (empty + low)[:10], "fix": "code: shrink containers to content"}
    # A14: 주황(문제) 테두리 = 판정 근거 리소스와 정확히 일치, 판정 번호 표기
    if spec["state"] == "AS-IS":
        ev_res = {r for x in spec.get("findings") or [] for r in x["resources"]}
        marked = [n for n in spec["nodes"] if n.get("finding")]
        wrong = [n["label"] for n in marked if not set(n.get("members") or []) & ev_res]
        missing = [n["label"] for n in spec["nodes"] if set(n.get("members") or []) & ev_res and not n.get("finding") and n["id"] in by]
        issue_shapes = len([x for x in names if x.startswith("issue:")])
        ids_shown = len([x for x in names if x == "findingid"])
        ok14 = not wrong and not missing and issue_shapes == len([n for n in marked if n["id"] in by])
        out["A14"] = {"pass": ok14, "detail": f"주황 {issue_shapes} · 근거 노드 {len(marked)} · 번호 {ids_shown} · 불일치 {len(wrong) + len(missing)}",
                      "items": (wrong + missing)[:10], "fix": "code: orange only on finding evidence"}
    else:
        newn = [n for n in spec["nodes"] if n.get("new") or n.get("changed")]
        new_shapes = len([x for x in names if x.startswith(("new:", "chg:"))])
        out["A14"] = {"pass": new_shapes == len([n for n in newn if n["id"] in by]), "detail": f"초록(신규·변경) {new_shapes} · 과제 노드 {len(newn)}",
                      "items": [], "fix": "code: green only on task objects"}
    # A15: 이름(리전·VNet·서브넷·카드 라벨)은 잘리지 않는다 — '…' 0. 칩(부가 설명)은 대상 아님
    cut15 = [sh.text_frame.text for sh in sl.shapes if sh.has_text_frame and sh.name.startswith(("label:", "cardlabel:"))
             and "…" in sh.text_frame.text]
    out["A15"] = {"pass": not cut15, "detail": f"잘린 이름 {len(cut15)}", "items": cut15[:10],
                  "fix": "code: shrink font or wrap instead of truncating names"}
    # 아이콘 출처(표준 §2.5): PPT 의 모든 그림 = icon-map 경로
    root, m = icon_map()
    allowed = set((m.get("map") or {}).values())
    pics = [sh.name[5:] for sh in sl.shapes if sh.shape_type == 13]
    bad_icons = [p for p in pics if p not in allowed]
    icons = {"pictures": len(pics), "distinct": sorted(set(pics)), "not_in_icon_map": bad_icons, "warnings": sorted(set(warnings))}
    return out, icons


# ---------------- 미리보기 PNG(같은 기하 모델) ----------------
def preview_png(spec, boxes, facts_res, scale=96):
    img = Image.new("RGB", (int(W * scale), int(H * scale)), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(r"C:\Windows\Fonts\malgun.ttf", 10)
    except OSError:
        font = ImageFont.load_default()
    col = {"region": "#5B9BD5", "vnet": "#2E75B6", "vnetline": "#5B9BD5", "subnet": "#8FAADC", "group": "#BFBFBF", "rowgroup": "#BFBFBF",
           "mgmt": "#BFBFBF", "panel": "#404040", "band": "#2E75B6", "msband": "#404040", "external": "#595959", "card": None}
    fill = {"vnet": "#F3F8FD", "mgmt": "#FCE4D6", "band": "#2E75B6", "msband": "#404040"}
    for b in boxes:
        if b.kind not in col:
            continue
        xy = [b.x * scale, b.y * scale, b.r * scale, b.b * scale]
        if b.kind == "card":
            key = icon_key(b.data, facts_res)
            png, _ = icon_png(key, [])
            if png:
                ic = Image.open(io.BytesIO(png)).convert("RGBA").resize((int(0.33 * scale), int(0.33 * scale)))
                img.paste(ic, (int((b.x + b.w / 2 - 0.165) * scale), int((b.y + 0.03) * scale)), ic)
            d.text(((b.x + 0.02) * scale, (b.y + 0.4) * scale), b.data["label"][:18], fill="black", font=font)
            if b.data.get("finding") and spec["state"] == "AS-IS":
                d.rectangle(xy, outline="#C55A11", width=2)
            if b.data.get("new") or b.data.get("changed"):
                d.rectangle(xy, outline="#2E7D5B", width=2)
        else:
            d.rectangle(xy, outline=col[b.kind], fill=fill.get(b.kind) if b.kind in ("vnet", "mgmt", "band", "msband") else None,
                        width=2 if b.kind == "region" else 1)
            lab = (b.data or {}).get("label") or ""
            if lab:
                d.text((xy[0] + 4, xy[1] + 2), lab[:40], fill="white" if b.kind in ("band", "msband") else "black", font=font)
    bio = io.BytesIO()
    img.save(bio, "PNG")
    return bio.getvalue()
