"""AI CAF Assessment 포털 — 인터뷰 Assessment + Infra Assessment(계승 예정).

구성
- 인증: 로컬 계정(기본) 또는 Entra ID(AICAF_AUTH=entra).
- 통합: 한 Assessment = 인터뷰 + 인프라 분석. 인프라 분석은 별도 포털 없이 이 서버가 업로드·실행·진행·
  다운로드를 직접 하고, 문서 생성은 C:\INFRA 의 엔진(WARA 계승)을 부른다.
- 받아쓰기: 브라우저 마이크 PCM → 서버 WebSocket → Foundry MAI-Transcribe-2-Streaming(Realtime API).
- 점수: 받아쓰기 결과를 Foundry 모델로 요약·점수 확정. 사람이 수정하면 이력이 남는다.
- 계산: 주제→영역→종합→단계는 코드가 계산한다(모델 숫자는 쓰지 않는다).
"""
import asyncio
import contextlib
import re
import base64
import hashlib
import json
from html import escape as html_escape
import os
import secrets
import sqlite3
import time
import urllib.parse
from pathlib import Path

import aiohttp
from aiohttp import web

from questions import FRAMEWORKS, TRACK_META, questions_for
import scoring
import interview_report
import collector_upload
import cost_view
import asis_view
import arch_view

ROOT = Path(os.environ.get("AICAF_DATA", r"C:\AICAF\data"))
ROOT.mkdir(parents=True, exist_ok=True)
DB = ROOT / "aicaf.sqlite3"
TENANT = os.environ.get("AICAF_TENANT", "")
CLIENT_ID = os.environ.get("AICAF_CLIENT_ID", "")
BASE_URL = os.environ.get("AICAF_BASE_URL", "https://portal.example.com")
INFRA_HOST = os.environ.get("AICAF_INFRA_HOST", "infra.portal.example.com")
INFRA_UPSTREAM = os.environ.get("AICAF_INFRA_UPSTREAM", "http://127.0.0.1:8081")
PUBLIC_HOST = urllib.parse.urlparse(BASE_URL).hostname
HOSTS = {PUBLIC_HOST, INFRA_HOST}


def base_of(req):
    """요청 Host가 허용 목록에 있으면 그 호스트 기준 주소를, 아니면 기본 주소를 쓴다."""
    h = (req.headers.get("X-Original-Host") or req.host or "").split(":")[0].lower()
    return f"https://{h}" if h in HOSTS else BASE_URL
FD_ENDPOINT = os.environ["AICAF_FD_ENDPOINT"].rstrip("/")
FD_RESOURCE_ID = os.environ["AICAF_FD_RESOURCE_ID"]
FD_REGION = os.environ.get("AICAF_FD_REGION", "koreacentral")
MODEL = os.environ.get("AICAF_MODEL", "gpt-5.4-mini")
PORT = int(os.environ.get("AICAF_PORT", "8080"))
SECRET = (ROOT / "session-secret")
if not SECRET.exists():
    SECRET.write_text(secrets.token_hex(32), encoding="utf-8")
SESSION_KEY = SECRET.read_text(encoding="utf-8").strip()
AUTHORITY = f"https://login.microsoftonline.com/{TENANT}"
COOKIE = "__Host-aicaf_session"

# 문항은 프로젝트 유형(track)별로 다르다. 전역 목록을 두지 않고 항상 track 으로 고른다.
TRACKS = tuple(FRAMEWORKS)
_QCACHE = {t: questions_for(t) for t in TRACKS}
_QMAPS = {t: {q["id"]: q for q in qs} for t, qs in _QCACHE.items()}
_WORKFLOW = None


def workflow_state(aid):
    """실제 수집·파일·검토 증거에서 공통 업무 순서를 조회한다."""
    if _WORKFLOW is None:
        raise web.HTTPServiceUnavailable(text="공통 보고서 흐름이 아직 등록되지 않았습니다.")
    return _WORKFLOW.state(aid)


def track_of(aid):
    """프로젝트 유형. 없는 프로젝트면 404."""
    with db() as c:
        r = c.execute("select track from assessments where id=?", (aid,)).fetchone()
    if not r:
        raise web.HTTPNotFound()
    return r["track"]


def qmap_for(aid):
    if _WORKFLOW is not None:
        return {q['id']: q for q in _WORKFLOW.questions(aid, required=True)}
    return _QMAPS[track_of(aid)]


def question_of(aid, qid):
    """이 프로젝트 유형의 문항. 다른 유형 문항 ID면 404."""
    q = qmap_for(aid).get(qid)
    if not q:
        raise web.HTTPNotFound(text="이 프로젝트 유형에 없는 문항입니다.")
    return q


# ---------------- 저장소 ----------------
def db():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with db() as c:
        c.executescript("""
        create table if not exists sessions(id text primary key, sub text, name text, upn text,
            roles text, created real, expires real);
        create table if not exists assessments(id integer primary key autoincrement, customer text not null,
            title text, interviewer text, created real, created_by text);
        create table if not exists consents(assessment_id integer primary key, consent integer not null,
            confirmed_by text, confirmed_at real, note text);
        create table if not exists answers(assessment_id integer, qid text, transcript text default '',
            summary text default '', score text, ai_score text, ai_reason text, edited_by text, edited_at real,
            updated real, primary key(assessment_id, qid));
        create table if not exists recordings(id integer primary key autoincrement, assessment_id integer, qid text,
            blob text, bytes integer, created real, created_by text);
        create table if not exists local_accounts(username text primary key, pw_hash text not null, salt text not null,
            display_name text default '', roles text not null, must_change integer not null default 1,
            failures integer not null default 0, locked_until real not null default 0, created real, changed real);
        create table if not exists project_members(assessment_id integer not null, username text not null,
            role text not null, added real, added_by text, primary key(assessment_id, username));
        create table if not exists voiceprints(username text primary key, emb text not null, samples integer not null,
            seconds real not null, consistency real, created real);
        create table if not exists audit(id integer primary key autoincrement, at real, actor text,
            assessment_id integer, qid text, action text, detail text);
        """)
        # track: new(신규구축) | existing(기구축). 기존 프로젝트는 모두 new(현행 100문항) — 답변·점수 보존
        for col in ("infra_case_id text", "contact_name text", "contact_role text", "contact_email text",
                    "track text not null default 'new'", "deleted_at real", "deleted_by text"):
            if col.split()[0] not in {r[1] for r in c.execute("pragma table_info(assessments)")}:
                c.execute(f"alter table assessments add column {col}")
        if "ai_detail" not in {r[1] for r in c.execute("pragma table_info(answers)")}:
            c.execute("alter table answers add column ai_detail text")  # 평가 근거(JSON: 잘된 점·부족한 점·다음 단계)
        if "prev_score" not in {r[1] for r in c.execute("pragma table_info(answers)")}:
            c.execute("alter table answers add column prev_score text")  # 보완 인터뷰 전 AI 점수
        if "cleared" not in {r[1] for r in c.execute("pragma table_info(recordings)")}:
            c.execute("alter table recordings add column cleared real")  # 문항 초기화 시 숨김(원본은 보존)
        for col in ("status text not null default 'active'", "email text default ''", "requested_note text default ''",
                    "approved_by text", "approved_at real"):
            if col.split()[0] not in {r[1] for r in c.execute("pragma table_info(local_accounts)")}:
                c.execute(f"alter table local_accounts add column {col}")
        # 기존 프로젝트: 만든 사람을 프로젝트 관리자로 등록(최초 1회)
        for a in c.execute("select id, created_by from assessments").fetchall():
            if a["created_by"] and not c.execute("select 1 from project_members where assessment_id=?", (a["id"],)).fetchone():
                c.execute("insert into project_members values(?,?,?,?,?)", (a["id"], a["created_by"], "manager", time.time(), "migration"))



def audit(actor, aid, qid, action, detail=""):
    with db() as c:
        c.execute("insert into audit(at,actor,assessment_id,qid,action,detail) values(?,?,?,?,?,?)",
                  (time.time(), actor, aid, qid, action, detail))


# ---------------- 관리 ID 토큰 ----------------
_tok_cache = {}


async def mi_token(resource):
    """VM 관리 ID(IMDS)로 토큰을 받는다."""
    hit = _tok_cache.get(resource)
    if hit and hit[1] - 300 > time.time():
        return hit[0]
    url = ("http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource="
           + urllib.parse.quote(resource, safe=""))
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers={"Metadata": "true"}, timeout=aiohttp.ClientTimeout(total=10)) as r:
            r.raise_for_status()
            d = await r.json()
    _tok_cache[resource] = (d["access_token"], int(d["expires_on"]))
    return d["access_token"]


# ---------------- 로컬 계정 인증 ----------------
# Entra 관리자 동의를 받을 수 없는 환경이라 AICAF_AUTH=local 이 기본이다.
AUTH_MODE = os.environ.get("AICAF_AUTH", "local")
PW_MIN = 8
PBKDF2_ROUNDS = 310000
MAX_FAILURES = 10
LOCK_SECONDS = 900


def pw_hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ROUNDS).hex()


def pw_policy(password):
    """복잡도: 8자 이상."""
    if len(password) < PW_MIN:
        return f"비밀번호는 {PW_MIN}자 이상이어야 합니다."
    return None


def set_password(username, password, *, must_change):
    salt = secrets.token_hex(16)
    with db() as c:
        c.execute("update local_accounts set pw_hash=?, salt=?, must_change=?, failures=0, locked_until=0, changed=? where username=?",
                  (pw_hash(password, salt), salt, 1 if must_change else 0, time.time(), username))


def create_local_account(username, password, roles, display_name="", must_change=True):
    salt = secrets.token_hex(16)
    with db() as c:
        c.execute("insert into local_accounts(username,pw_hash,salt,display_name,roles,must_change,created,changed) values(?,?,?,?,?,?,?,?)",
                  (username, pw_hash(password, salt), salt, display_name or username, json.dumps(roles),
                   1 if must_change else 0, time.time(), time.time()))


# ---------------- 권한 ----------------
# 시스템 역할(계정): admin = 최고 관리자(가입 승인·권한 부여·모든 프로젝트), pm = 프로젝트 생성 가능, user = 배정된 프로젝트만.
# 프로젝트 역할(멤버): manager = 고객 정보·멤버 관리 + 전부, interviewer = 인터뷰·인프라 분석 실행, viewer = 읽기만.
SYS_ROLES = {"aicaf.admin": "관리자", "aicaf.pm": "프로젝트 관리자", "aicaf.user": "일반 사용자"}
PROJ_ROLES = {"manager": "프로젝트 관리자", "interviewer": "인터뷰어", "viewer": "조회만"}
PROJ_RANK = {"viewer": 1, "interviewer": 2, "manager": 3}


def user_roles(user):
    return json.loads(user["roles"]) if isinstance(user.get("roles"), str) else (user.get("roles") or [])


def is_admin(user):
    return "aicaf.admin" in user_roles(user)


def can_create_project(user):
    r = user_roles(user)
    return "aicaf.admin" in r or "aicaf.pm" in r


def project_role(user, aid):
    if is_admin(user):
        return "manager"
    with db() as c:
        r = c.execute("select role from project_members where assessment_id=? and username=?", (aid, user["upn"])).fetchone()
    return r["role"] if r else None


def is_deleted(aid):
    with db() as c:
        r = c.execute("select deleted_at from assessments where id=?", (aid,)).fetchone()
    return not r or r["deleted_at"] is not None


def need_project(req, aid, at_least="viewer"):
    """프로젝트 접근 확인. 멤버가 아니거나 삭제(보관)된 프로젝트면 404(존재도 숨김), 권한이 낮으면 403."""
    role = project_role(req["user"], aid)
    if not role or is_deleted(aid):
        raise web.HTTPNotFound()
    if PROJ_RANK[role] < PROJ_RANK[at_least]:
        raise web.HTTPForbidden(text="이 작업 권한이 없습니다.")
    return role


def _form_page(title, body, error=""):
    err = f'<p class="err">{error}</p>' if error else ""
    html = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · AI CAF Assessment</title><style>
body{{margin:0;font-family:"맑은 고딕","Malgun Gothic","Apple SD Gothic Neo",sans-serif;background:#F4F6FA;color:#262626}}
.box{{max-width:380px;margin:12vh auto;background:#fff;border:1px solid #C9D1DA;border-radius:8px;padding:28px}}
h1{{color:#273443;font-size:20px;margin:0 0 18px}}label{{display:block;font-size:13px;color:#5B6A77;margin:12px 0 4px}}
input{{width:100%;box-sizing:border-box;padding:9px;border:1px solid #C9D1DA;border-radius:5px;font:inherit}}
button{{margin-top:18px;width:100%;background:#1F706E;color:#fff;border:0;border-radius:5px;padding:10px;font-size:15px;cursor:pointer}}
.err{{color:#B03A2E;font-size:13px}}.hint{{color:#5B6A77;font-size:12px}}</style></head>
<body><div class="box"><h1>{title}</h1>{err}{body}</div></body></html>"""
    return web.Response(text=html, content_type="text/html")


def _start_session(resp, user):
    sid = secrets.token_urlsafe(32)
    with db() as con:
        con.execute("insert into sessions values(?,?,?,?,?,?,?)",
                    (sid, "local:" + user["username"], user["display_name"], user["username"],
                     user["roles"], time.time(), time.time() + 12 * 3600))
    resp.set_cookie(COOKIE, sid, secure=True, httponly=True, samesite="Lax", path="/", max_age=12 * 3600)


LOGIN_FORM = """<form method="post" action="/login"><input type="hidden" name="next" value="{nxt}">
<label>아이디</label><input name="username" autocomplete="username" required autofocus>
<label>비밀번호</label><input name="password" type="password" autocomplete="current-password" required>
<button type="submit">로그인</button></form><p class="hint" style="text-align:center;margin-top:14px">계정이 없나요? <a href="/signup">회원가입</a></p>"""

SIGNUP_FORM = """<form method="post" action="/signup">
<label>아이디 (영문·숫자 3~32자)</label><input name="username" pattern="[A-Za-z0-9._-]{{3,32}}" required autofocus value="{username}">
<label>이름</label><input name="display_name" required value="{display_name}">
<label>이메일</label><input name="email" type="email" required value="{email}">
<label>소속·가입 사유</label><input name="note" value="{note}">
<label>비밀번호 (8자 이상)</label><input name="new1" type="password" autocomplete="new-password" minlength="8" required>
<label>비밀번호 확인</label><input name="new2" type="password" autocomplete="new-password" minlength="8" required>
<p class="hint">가입 신청 후 관리자가 승인해야 로그인할 수 있습니다.</p><button type="submit">가입 신청</button></form>
<p class="hint" style="text-align:center;margin-top:14px"><a href="/login">로그인으로 돌아가기</a></p>"""


async def signup_get(req):
    return _form_page("회원가입", SIGNUP_FORM.format(username="", display_name="", email="", note=""))


async def signup_post(req):
    f = await req.post()
    v = {k: str(f.get(k, "")).strip() for k in ("username", "display_name", "email", "note")}
    n1, n2 = str(f.get("new1", "")), str(f.get("new2", ""))
    esc_v = {k: html_escape(x) for k, x in v.items()}
    err = None
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,32}", v["username"]):
        err = "아이디는 영문·숫자·. _ - 로 3~32자입니다."
    elif not v["display_name"]:
        err = "이름을 입력하세요."
    elif not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v["email"]):
        err = "이메일 형식을 확인하세요."
    elif n1 != n2:
        err = "비밀번호가 서로 다릅니다."
    else:
        err = pw_policy(n1)
    if not err:
        with db() as c:
            if c.execute("select 1 from local_accounts where lower(username)=lower(?)", (v["username"],)).fetchone():
                err = "이미 쓰이는 아이디입니다."
    if err:
        return _form_page("회원가입", SIGNUP_FORM.format(**esc_v), err)
    salt = secrets.token_hex(16)
    with db() as c:
        c.execute("""insert into local_accounts(username,pw_hash,salt,display_name,roles,must_change,created,changed,status,email,requested_note)
                     values(?,?,?,?,?,?,?,?,?,?,?)""",
                  (v["username"], pw_hash(n1, salt), salt, v["display_name"], json.dumps(["aicaf.user"]), 0,
                   time.time(), time.time(), "pending", v["email"], v["note"][:300]))
    audit(v["username"], None, None, "signup-requested", v["email"])
    return _form_page("가입 신청 완료", '<p>관리자가 승인하면 로그인할 수 있습니다.</p><p class="hint"><a href="/login">로그인 화면으로</a></p>')


async def local_login_get(req):
    nxt = urllib.parse.quote(req.query.get("next", "/"), safe="/?=&")
    return _form_page("로그인", LOGIN_FORM.format(nxt=nxt))


async def local_login_post(req):
    f = await req.post()
    username = str(f.get("username", "")).strip()
    password = str(f.get("password", ""))
    nxt = str(f.get("next", "/"))
    nxt = nxt if nxt.startswith("/") and not nxt.startswith("//") else "/"
    with db() as c:
        u = c.execute("select * from local_accounts where username=?", (username,)).fetchone()
    form = LOGIN_FORM.format(nxt=urllib.parse.quote(nxt, safe="/?=&"))
    if u and u["locked_until"] > time.time():
        return _form_page("로그인", form, "로그인 실패가 많아 잠시 잠겼습니다. 15분 뒤 다시 시도하세요.")
    if not u or not secrets.compare_digest(pw_hash(password, u["salt"]), u["pw_hash"]):
        if u:
            fails = u["failures"] + 1
            with db() as c:
                c.execute("update local_accounts set failures=?, locked_until=? where username=?",
                          (fails, time.time() + LOCK_SECONDS if fails >= MAX_FAILURES else 0, username))
        audit(username or "-", None, None, "login-failed")
        return _form_page("로그인", form, "아이디 또는 비밀번호가 올바르지 않습니다.")
    status = u["status"] if "status" in u.keys() else "active"
    if status != "active":
        audit(username, None, None, "login-blocked", status)
        msg = {"pending": "가입 승인 대기 중입니다. 관리자 승인 후 로그인할 수 있습니다.",
               "rejected": "가입이 승인되지 않았습니다. 관리자에게 문의하세요."}.get(status, "사용이 중지된 계정입니다. 관리자에게 문의하세요.")
        return _form_page("로그인", form, msg)
    with db() as c:
        c.execute("update local_accounts set failures=0, locked_until=0 where username=?", (username,))
    audit(username, None, None, "login")
    resp = web.HTTPFound("/password?first=1" if u["must_change"] else nxt)
    _start_session(resp, dict(u))
    raise resp


PW_FORM = """<form method="post" action="/password">
<label>현재 비밀번호</label><input name="current" type="password" autocomplete="current-password" required>
<label>새 비밀번호</label><input name="new1" type="password" autocomplete="new-password" minlength="8" required>
<label>새 비밀번호 확인</label><input name="new2" type="password" autocomplete="new-password" minlength="8" required>
<p class="hint">8자 이상. 현재 비밀번호와 달라야 합니다.</p><button type="submit">비밀번호 변경</button></form>"""


async def password_get(req):
    title = "처음 로그인 — 비밀번호를 바꿔 주세요" if req.query.get("first") else "비밀번호 변경"
    return _form_page(title, PW_FORM)


async def password_post(req):
    f = await req.post()
    username = req["user"]["upn"]
    with db() as c:
        u = c.execute("select * from local_accounts where username=?", (username,)).fetchone()
    if not u:
        raise web.HTTPForbidden(text="로컬 계정이 아닙니다.")
    cur, n1, n2 = str(f.get("current", "")), str(f.get("new1", "")), str(f.get("new2", ""))
    err = None
    if not secrets.compare_digest(pw_hash(cur, u["salt"]), u["pw_hash"]):
        err = "현재 비밀번호가 올바르지 않습니다."
    elif n1 != n2:
        err = "새 비밀번호가 서로 다릅니다."
    elif n1 == cur:
        err = "현재 비밀번호와 다른 비밀번호를 쓰세요."
    else:
        err = pw_policy(n1)
    if err:
        return _form_page("비밀번호 변경", PW_FORM, err)
    set_password(username, n1, must_change=False)
    with db() as c:  # 다른 세션은 모두 끊는다
        c.execute("delete from sessions where upn=? and id<>?", (username, req.cookies.get(COOKIE, "")))
    audit(username, None, None, "password-changed")
    raise web.HTTPFound("/")


# ---------------- 인증 ----------------
def b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def jwt_payload(tok):
    p = tok.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))


_pending = {}


async def login(req):
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    _pending[state] = (nonce, verifier, time.time(), req.query.get("next", "/"))
    q = dict(client_id=CLIENT_ID, response_type="code", redirect_uri=base_of(req) + "/auth/callback",
             response_mode="query", scope="openid profile email", state=state, nonce=nonce,
             code_challenge=b64url(hashlib.sha256(verifier.encode()).digest()), code_challenge_method="S256")
    raise web.HTTPFound(f"{AUTHORITY}/oauth2/v2.0/authorize?" + urllib.parse.urlencode(q))


async def callback(req):
    state = req.query.get("state", "")
    item = _pending.pop(state, None)
    if not item or time.time() - item[2] > 600:
        raise web.HTTPBadRequest(text="login state expired")
    if "error" in req.query:
        raise web.HTTPForbidden(text=req.query.get("error_description", "login failed"))
    nonce, verifier, _, nxt = item
    assertion = await mi_token("api://AzureADTokenExchange")
    form = dict(client_id=CLIENT_ID, grant_type="authorization_code", code=req.query["code"],
                redirect_uri=base_of(req) + "/auth/callback", code_verifier=verifier,
                client_assertion_type="urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                client_assertion=assertion, scope="openid profile email")
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{AUTHORITY}/oauth2/v2.0/token", data=form) as r:
            d = await r.json()
    if "id_token" not in d:
        raise web.HTTPForbidden(text="token exchange failed: " + str(d.get("error_description", ""))[:300])
    # id_token은 TLS로 토큰 엔드포인트에서 직접 받았으므로 발급자·대상·nonce만 확인한다.
    c = jwt_payload(d["id_token"])
    if c.get("aud") != CLIENT_ID or c.get("nonce") != nonce or TENANT not in c.get("iss", ""):
        raise web.HTTPForbidden(text="invalid id_token")
    roles = c.get("roles", [])
    if not roles:
        raise web.HTTPForbidden(text="이 앱에 대한 역할이 없습니다. 관리자에게 요청하세요.")
    sid = secrets.token_urlsafe(32)
    with db() as con:
        con.execute("insert into sessions values(?,?,?,?,?,?,?)",
                    (sid, c.get("oid"), c.get("name", ""), c.get("preferred_username", ""),
                     json.dumps(roles), time.time(), time.time() + 12 * 3600))
    audit(c.get("preferred_username"), None, None, "login")
    resp = web.HTTPFound(nxt if nxt.startswith("/") else "/")
    resp.set_cookie(COOKIE, sid, secure=True, httponly=True, samesite="Lax", path="/", max_age=12 * 3600)
    raise resp


async def logout(req):
    sid = req.cookies.get(COOKIE)
    if sid:
        with db() as c:
            c.execute("delete from sessions where id=?", (sid,))
    if AUTH_MODE == "local":
        resp = web.HTTPFound("/login")
    else:
        resp = web.HTTPFound(f"{AUTHORITY}/oauth2/v2.0/logout?post_logout_redirect_uri={urllib.parse.quote(base_of(req))}")
    resp.del_cookie(COOKIE, path="/")
    raise resp


PUBLIC = {"/login", "/auth/callback", "/healthz", "/logout", "/signup"}
PW_ALLOWED = {"/password", "/logout"}


def _prefers_login_redirect(req) -> bool:
    if req.path.startswith("/ws/"):
        return False
    if not req.path.startswith("/api/"):
        return True
    if req.method != "GET":
        return False
    if req.headers.get("X-Requested-With") == "XMLHttpRequest":
        return False
    if (req.headers.get("Sec-Fetch-Mode") or "").lower() == "navigate":
        return True
    accept = (req.headers.get("Accept") or "").lower()
    if "text/html" in accept and "application/json" not in accept:
        return True
    if req.path.endswith("/view") or "/file/" in req.path or req.path.endswith((".pptx", ".xlsx", ".pdf", ".png")):
        return "application/json" not in accept
    return False


@web.middleware
async def auth_mw(req, handler):
    if (req.host or "").split(":")[0].lower() == INFRA_HOST:
        raise web.HTTPMovedPermanently(BASE_URL + req.path_qs)
    if req.path in PUBLIC or req.path.startswith("/static/"):
        return await handler(req)
    # Collector 업로드는 쿠키가 아니라 1회용 업로드 key(Bearer)로만 인증한다(WARA 와 같은 계약).
    if req.path.startswith(collector_upload.SESSIONS_PATH) and req.method == "POST":
        return await handler(req)
    if req.method not in ("GET", "HEAD", "OPTIONS"):
        origin = req.headers.get("Origin")
        if origin and origin != base_of(req):
            raise web.HTTPForbidden(text="origin")
    sid = req.cookies.get(COOKIE)
    row = None
    if sid:
        with db() as c:
            row = c.execute("select * from sessions where id=? and expires>?", (sid, time.time())).fetchone()
    if not row:
        if not _prefers_login_redirect(req):
            raise web.HTTPUnauthorized()
        raise web.HTTPFound("/login?next=" + urllib.parse.quote(req.path_qs))
    req["user"] = dict(row)
    if AUTH_MODE == "local":
        # 역할·상태는 세션에 굳히지 않고 매 요청 계정에서 읽는다(권한 변경·중지가 즉시 반영).
        with db() as c:
            acct = c.execute("select roles, status, must_change, display_name from local_accounts where username=?", (row["upn"],)).fetchone()
        if not acct or (acct["status"] or "active") != "active":
            with db() as c:
                c.execute("delete from sessions where id=?", (sid,))
            if not _prefers_login_redirect(req):
                raise web.HTTPUnauthorized()
            raise web.HTTPFound("/login?next=" + urllib.parse.quote(req.path_qs))
        req["user"]["roles"] = acct["roles"]
        req["user"]["name"] = acct["display_name"] or row["name"]
    if AUTH_MODE == "local" and req.path not in PW_ALLOWED:
        mc = acct
        if mc and mc["must_change"]:
            if req.path.startswith("/api/") or req.path.startswith("/ws/"):
                raise web.HTTPForbidden(text="password change required")
            raise web.HTTPFound("/password?first=1")
    return await handler(req)





# ---------------- Infra Assessment (WARA 엔진 계승, 네이티브) ----------------
# 별도 포털을 띄우지 않는다. 수집 결과 업로드·분석 실행·진행 조회·보고서 다운로드를
# 이 서버가 직접 하고, 분석은 C:\INFRA 의 엔진(pipeline_launcher → pipeline_runner)을 부른다.
INFRA_ROOT = Path(os.environ.get("AICAF_INFRA_ROOT", r"C:\INFRA"))
INFRA_CASES = INFRA_ROOT / "storage" / "aicaf" / "cases"
INFRA_PY = INFRA_ROOT / ".venv" / "Scripts" / "python.exe"
COLLECTOR = INFRA_ROOT / "collector" / "Collect-InfraEvidence.ps1"
UPLOAD_EXT = {".json", ".xlsx", ".pptx", ".csv"}
MAX_UPLOAD = 2 * 1024 * 1024 * 1024
STEP_LABEL = {"rules": "규칙 확인", "inputs": "입력 파일 확인", "wara-tooling": "분석 도구 실행",
              "evidence": "근거 분석", "docs": "공식 문서 수집", "cells": "장표 작성(AI)",
              "merge": "장표 병합", "assemble": "최종 보고서 조립", "excel": "엑셀 정리", "crash": "오류"}
STEP_ORDER = ["inputs", "wara-tooling", "evidence", "docs", "cells", "merge", "assemble", "excel"]
# 전체 진행률 가중치(실측 소요 비중: 장표 작성이 대부분)
STEP_WEIGHT = {"inputs": 2, "wara-tooling": 8, "evidence": 5, "docs": 10, "cells": 65, "merge": 4, "assemble": 5, "excel": 1}
DONE_STATES = {"ok", "reused", "skipped", "empty", "partial", "drift"}
# 다시 분석 시 지난 결과를 옮겨 둘 대상(삭제하지 않고 _attic 에 보존). Source(올린 파일)는 그대로 둔다.
RERUN_MOVE = ["pipeline-progress.json", "out", "Working/cell-slides", "Working/cells", "Working/ReportWorkflow-v2", "Working/wara-tooling"]


def _collector_state(aid):
    """수집 스크립트 업로드 상태: 마지막 key 발급과 마지막 업로드 완료(감사 기록 기준)."""
    with db() as c:
        k = c.execute("select at, actor, detail from audit where assessment_id=? and action='collector-key-issue' "
                      "order by id desc limit 1", (aid,)).fetchone()
        u = c.execute("select at, detail from audit where assessment_id=? and action='collector-upload' "
                      "order by id desc limit 1", (aid,)).fetchone()
    out = {}
    if u:
        try:
            files = json.loads(u["detail"]).get("files", [])
        except ValueError:
            files = []
        out["uploaded_at"], out["files"] = u["at"], files
    if k and (not u or k["at"] > u["at"]):
        # 업로드 이후 새로 발급된 key 가 있으면 '업로드 대기'. key 는 10분 유효.
        out["waiting_since"], out["waiting_by"] = k["at"], k["actor"]
        out["expires_at"] = k["at"] + 600
    return out


def _cell_progress(d):
    """장표 작성 단계: 만든 장 수 / 만들 장 수."""
    total = 0
    led = next(iter(sorted(d.glob("Working/**/SLIDE_WORK_LEDGER.json"))), None)
    if led:
        try:
            total = len(json.loads(led.read_text(encoding="utf-8")).get("items") or [])
        except (OSError, ValueError):
            pass
    made = len([p for p in (d / "Working" / "cell-slides").glob("*.pptx")]) if (d / "Working" / "cell-slides").is_dir() else 0
    return made, total


def _archive_previous_run(d):
    """지난 분석 결과를 _attic/run-시각 으로 옮긴다. 화면의 단계·결과 보고서가 처음 상태가 된다."""
    # 케이스 폴더 밖에 둔다. 엔진이 케이스 전체를 rglob 하므로 안에 두면 지난 결과를 다시 집어 쓴다.
    dest = INFRA_CASES.parent / "_attic" / d.name / time.strftime("run-%Y%m%d-%H%M%S")
    moved = []
    for rel in RERUN_MOVE:
        src = d / rel
        if src.exists():
            tgt = dest / rel
            tgt.parent.mkdir(parents=True, exist_ok=True)
            src.rename(tgt)
            moved.append(rel)
    # 분석 도구가 WARA-File 로 만들어 Source 에 둔 중간 산출물도 옮겨야 도구부터 다시 돈다.
    if any((d / "Source").glob("WARA-File*.json")):
        for pat in ("Expert-Analysis*.xlsx", "Assessment-Findings-Report*.xlsx", "Executive Summary*.pptx"):
            for p in (d / "Source").glob(pat):
                tgt = dest / "Source" / p.name
                tgt.parent.mkdir(parents=True, exist_ok=True)
                p.rename(tgt)
                moved.append("Source/" + p.name)
    (d / "out").mkdir(exist_ok=True)
    return dest, moved


CASE_ID_OK = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")  # 업로드 key 모듈(_CASE_PATTERN)과 같은 규칙


def make_case_id(customer, aid):
    # 케이스 ID 는 ASCII 만 — 한글 고객명이 그대로 들어가면 업로드 key 발급이 'Assessment 식별자' 오류로 거부된다.
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", customer).strip("-._") or "CUSTOMER"
    return f"{time.strftime('%Y-%m-%d')}-VBD-{slug[:60]}-{aid}"


def infra_case_dir(case_id):
    d = (INFRA_CASES / case_id).resolve()
    if INFRA_CASES.resolve() not in d.parents:
        raise web.HTTPBadRequest(text="invalid case")
    return d


def ensure_infra_case(a):
    """Assessment 하나에 인프라 작업 폴더 하나. 표지에 쓰는 고객명·담당자를 manifest에 둔다."""
    case_id = a.get("infra_case_id") or make_case_id(a["customer"], a["id"])
    if not CASE_ID_OK.fullmatch(case_id):
        # 예전 규칙(한글 포함)으로 만든 케이스: ASCII ID 로 옮긴다. 폴더가 있으면 통째로 이름만 바꿔 파일 보존.
        new_id = make_case_id(a["customer"], a["id"])
        old_dir, new_dir = infra_case_dir(case_id), infra_case_dir(new_id)
        if old_dir.exists() and not new_dir.exists():
            old_dir.rename(new_dir)
        with db() as c:
            c.execute("update assessments set infra_case_id=? where id=?", (new_id, a["id"]))
        a = {**a, "infra_case_id": new_id}
        case_id = new_id
        mf0 = new_dir / "case-manifest.json"
        if mf0.exists():
            m = json.loads(mf0.read_text(encoding="utf-8"))
            m["case_id"] = new_id
            mf0.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    d = infra_case_dir(case_id)
    for sub in ("Source", "Working", "out"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    mf = d / "case-manifest.json"
    doc = json.loads(mf.read_text(encoding="utf-8")) if mf.exists() else {
        "schema": "hades.wara-assessment.v1", "case_id": case_id, "status": "intake",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "uploads": [], "events": []}
    doc["display_name"] = a["customer"]
    doc.setdefault("metadata", {})["report_contact"] = {
        "name": a.get("contact_name") or "", "role": a.get("contact_role") or "", "email": a.get("contact_email") or ""}
    tmp = mf.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(mf)
    if not a.get("infra_case_id"):
        with db() as c:
            c.execute("update assessments set infra_case_id=? where id=?", (case_id, a["id"]))
    return case_id, d


def _assessment(aid):
    with db() as c:
        r = c.execute("select * from assessments where id=?", (aid,)).fetchone()
    if not r:
        raise web.HTTPNotFound()
    return dict(r)


def _files(d):
    out = []
    for p in sorted(d.glob("*")) if d.is_dir() else []:
        if p.is_file():
            out.append({"name": p.name, "bytes": p.stat().st_size, "modified": p.stat().st_mtime})
    return out


def infra_running(a):
    """인프라 분석 실행 중인지(api_infra_status 와 같은 판정: lock 파일 + progress=running)."""
    case_id = a.get("infra_case_id")
    if not case_id:
        return False
    d = infra_case_dir(case_id)
    if not (d / ".pipeline.lock").exists():
        return False
    try:
        return json.loads((d / "pipeline-progress.json").read_text(encoding="utf-8")).get("status") == "running"
    except (OSError, ValueError):
        return True  # lock 이 있는데 진행 파일을 못 읽으면 실행 중으로 본다(안전 쪽)


async def api_project_delete(req):
    """프로젝트 삭제 = 보관(soft delete). 답변·녹음·인프라 파일은 그대로 두고 목록·접근만 막는다."""
    aid = int(req.match_info["aid"])
    need_project(req, aid, "manager")  # 시스템 관리자는 project_role 에서 manager
    a = _assessment(aid)
    d = await req.json() if req.can_read_body else {}
    if str(d.get("confirm", "")).strip() != a["customer"]:
        raise web.HTTPBadRequest(text="확인용 고객명이 일치하지 않습니다.")
    if LIVE_REC.get(aid):
        raise web.HTTPConflict(text="녹음이 진행 중이라 삭제할 수 없습니다. 녹음을 마친 뒤 다시 시도하세요.")
    if infra_running(a):
        raise web.HTTPConflict(text="인프라 분석이 실행 중이라 삭제할 수 없습니다. 분석이 끝난 뒤 다시 시도하세요.")
    with db() as c:
        c.execute("update assessments set deleted_at=?, deleted_by=? where id=? and deleted_at is null",
                  (time.time(), req["user"]["upn"], aid))
    audit(req["user"]["upn"], aid, None, "project-delete", a["customer"])
    return web.json_response({"ok": True})


async def api_admin_deleted(req):
    _need_admin(req)
    with db() as c:
        rows = [dict(r) for r in c.execute("""select a.id, a.customer, a.title, a.track, a.created, a.created_by, a.deleted_at, a.deleted_by,
                 (select count(*) from answers where assessment_id=a.id) answers
                 from assessments a where a.deleted_at is not null order by a.deleted_at desc""")]
    return web.json_response(rows)


async def api_admin_restore(req):
    _need_admin(req)
    aid = int(req.match_info["aid"])
    with db() as c:
        r = c.execute("select customer from assessments where id=? and deleted_at is not null", (aid,)).fetchone()
        if not r:
            raise web.HTTPNotFound()
        c.execute("update assessments set deleted_at=null, deleted_by=null where id=?", (aid,))
    audit(req["user"]["upn"], aid, None, "project-restore", r["customer"])
    return web.json_response({"ok": True})


async def api_infra_status(req):
    """수집 원본은 읽기 전용으로 표시하고 새 AS-IS 개정만 고객 결과로 연결한다."""
    aid = int(req.match_info["aid"])
    need_project(req, aid)
    a = _assessment(aid)
    case_id = a.get("infra_case_id")
    src = _files(infra_case_dir(case_id) / "Source") if case_id else []
    current = {}
    if case_id:
        try:
            current = await asyncio.to_thread(req.app["asis_controller"].current, aid)
        except web.HTTPException as exc:
            current = {"can_generate": False, "error": exc.text}
    latest = current.get("latest") or {}
    candidate = current.get("candidate") or {}
    state = latest.get("status") or ("ready" if src else "empty")
    running = state in ("queued", "running")
    steps = [{"name": s["name"], "label": {"snapshot": "입력 동결", "analysis": "r2 근거 분석 후보", "native_pair": "PPT·XLSX 생성/재계산", "actual_render": "실제 PPT 렌더"}.get(s["name"], s["name"]), "status": s["status"], "at": ""} for s in latest.get("steps", [])]
    outputs = []
    if candidate.get("status") == "generated_candidate":
        try:
            manifest = req.app["asis_controller"].manifest(aid, candidate["revision_id"])
            for item in manifest["artifacts"]:
                kind = {"asis_report_pptx": "pptx", "asis_backdata_xlsx": "xlsx", "asis_render_pdf": "pdf"}[item["role"]]
                outputs.append({**item, "url": f'/api/aicaf/assessments/{aid}/asis/revisions/{candidate["revision_id"]}/files/{kind}'})
        except web.HTTPException as exc:
            current["error"] = exc.text
    return web.json_response({
        "case_id": case_id, "status": state, "running": running,
        "error": latest.get("error") or current.get("error") or "",
        "started_at": latest.get("created_at"), "finished_at": latest.get("finished_at"),
        "steps": steps, "source": src, "percent": round(100 * sum(s["status"] == "ok" for s in steps) / len(steps)) if steps else 0,
        "now": time.time(), "collector": _collector_state(aid),
        "has_warafile": any(f["name"].startswith("WARA-File") and f["name"].endswith(".json") for f in src),
        "has_topology": any(f["name"] == "topology.json" for f in src),
        "has_aicaf_evidence": any(f["name"] == "aicaf-evidence.json" for f in src),
        "outputs": outputs, "asis": current, "legacy_results_customer_visible": False,
    })


async def api_infra_upload(req):
    need_project(req, int(req.match_info["aid"]), "interviewer")
    a = _assessment(int(req.match_info["aid"]))
    case_id, d = ensure_infra_case(a)
    reader = await req.multipart()
    saved = []
    while True:
        part = await reader.next()
        if part is None:
            break
        if not part.filename:
            continue
        name = re.sub(r"[^A-Za-z0-9가-힣 ._()\[\]-]+", "-", Path(part.filename.replace("\\", "/")).name).strip(" .")
        if Path(name).suffix.lower() not in UPLOAD_EXT:
            raise web.HTTPBadRequest(text=f"올릴 수 없는 형식: {name}")
        tmp = d / "Source" / f".{name}.part"
        size = 0
        with open(tmp, "wb") as fh:
            while True:
                chunk = await part.read_chunk(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD:
                    fh.close()
                    tmp.unlink(missing_ok=True)
                    raise web.HTTPRequestEntityTooLarge(max_size=MAX_UPLOAD, actual_size=size)
                fh.write(chunk)
        if name.endswith(".json"):
            try:
                with open(tmp, encoding="utf-8-sig") as fh:
                    json.load(fh)
            except ValueError:
                tmp.unlink(missing_ok=True)
                raise web.HTTPBadRequest(text=f"JSON 형식이 아닙니다: {name}")
        tmp.replace(d / "Source" / name)
        saved.append({"name": name, "bytes": size})
    audit(req["user"]["upn"], a["id"], None, "infra-upload", ",".join(f["name"] for f in saved))
    return web.json_response({"saved": saved})


async def api_infra_delete_file(req):
    need_project(req, int(req.match_info["aid"]), "interviewer")
    a = _assessment(int(req.match_info["aid"]))
    case_id, d = ensure_infra_case(a)
    p = (d / "Source" / req.match_info["name"]).resolve()
    if p.parent != (d / "Source").resolve() or not p.is_file():
        raise web.HTTPNotFound()
    p.unlink()
    audit(req["user"]["upn"], a["id"], None, "infra-file-delete", p.name)
    return web.json_response({"ok": True})


async def api_infra_start(req):
    """기존 시작 경로도 새 AS-IS 개정 생성으로만 연결한다. WARA 실행·원본/이전 결과 이동 없음."""
    aid = int(req.match_info["aid"])
    need_project(req, aid, "interviewer")
    body = await req.json()
    controller = req.app["asis_controller"]
    row = await asyncio.to_thread(controller.create, aid, body)
    if row["status"] == "queued":
        task = asyncio.create_task(asyncio.to_thread(controller.launch, aid, row["revision_id"]))
        req.app["asis_tasks"].add(task)
        task.add_done_callback(req.app["asis_tasks"].discard)
    audit(req["user"]["upn"], aid, None, "asis-start", row["revision_id"])
    return web.json_response(row, status=202)


async def api_infra_download(req):
    """구 WARA/결과 엑셀 원본은 내부 보존하되 고객 결과 다운로드에서 제외한다."""
    need_project(req, int(req.match_info["aid"]))
    raise web.HTTPNotFound(text="새 AS-IS 개정 원장의 보고서·현황 데이터 경로를 사용해야 합니다.")


# 스크립트의 기본 업로드 주소·허용 주소를 이 포털로 고정해 내려준다. 원본은 WARA 정본과 같고 이 두 줄만 다르다.
_PORTAL_PIN = (b"$script:ProductionPortalOrigin = [uri]'https://legacy.portal.example.com'",
               b"[uri]$PortalBaseUri = 'https://legacy.portal.example.com'")


def collector_bytes():
    raw = COLLECTOR.read_bytes()
    for old in _PORTAL_PIN:
        if old not in raw:
            raise RuntimeError("collector script pin not found: " + old.decode())
        raw = raw.replace(old, old.replace(b"https://legacy.portal.example.com", BASE_URL.encode()))
    return raw


# 현황분석 디자인 원칙(가이드) 내려받기: 화이트리스트 키 → 파일. 경로 입력을 받지 않는다.
GUIDES_DIR = Path(__file__).parent / "guides"
GUIDES = {"design-xlsx": "AICAF-현황분석-디자인가이드-v0.2.xlsx", "design-pptx": "AICAF-현황분석-디자인가이드-v0.2.pptx"}
GUIDE_CT = {".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}


async def api_guide(req):
    fn = GUIDES.get(req.match_info["name"])
    p = GUIDES_DIR / fn if fn else None
    if not p or not p.is_file():
        raise web.HTTPNotFound()
    audit(req["user"]["upn"], None, None, "guide-download", fn)
    return web.Response(body=p.read_bytes(), content_type=GUIDE_CT[p.suffix],
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(fn)}"})


async def api_collector(req):
    if not COLLECTOR.is_file():
        raise web.HTTPNotFound(text="수집 스크립트가 서버에 없습니다.")
    return web.Response(body=collector_bytes(), content_type="application/octet-stream",
                        headers={"Content-Disposition": 'attachment; filename="Collect-InfraEvidence.ps1"'})


CU = collector_upload.CollectorUpload(
    infra_root=INFRA_ROOT, state_dir=ROOT / "collector", case_root=INFRA_CASES,
    secret=hashlib.sha256(b"aicaf-collector-key\0" + SESSION_KEY.encode()).digest())


async def api_upload_key(req):
    """이 프로젝트 인프라 케이스로만 올릴 수 있는 1회용 업로드 key(10분). 화면에 한 번만 보여 준다."""
    aid = int(req.match_info["aid"])
    need_project(req, aid, "interviewer")
    a = _assessment(aid)
    case_id, _ = ensure_infra_case(a)
    sub = str((await req.json()).get("subscriptionId", "")).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", sub):
        raise web.HTTPBadRequest(text="구독 ID 형식이 아닙니다.")
    try:
        g = CU.issue(owner=req["user"]["upn"], case_id=case_id, subscription_id=sub)
    except CU.Errors as e:
        raise web.HTTPBadRequest(text=getattr(e, "message", "발급 실패"))
    audit(req["user"]["upn"], aid, None, "collector-key-issue", f"{case_id} sub={sub} key={g['keyId'][:8]}")
    return web.json_response({"key": f"{g['keyId']}.{g['uploadKey']}", "expiresAt": g["expiresAt"], "caseId": case_id})


async def api_intake(req):
    """Collector 업로드(manual-intake). 판정은 WARA 와 같은 검증 모듈이 한다."""
    body = await req.read()
    status, payload, done_case = await asyncio.get_running_loop().run_in_executor(
        None, CU.handle, req.path, req.headers, body)
    if done_case:
        with db() as c:
            row = c.execute("select id from assessments where infra_case_id=?", (done_case,)).fetchone()
        audit("collector", row["id"] if row else None, None, "collector-upload",
              json.dumps({"case": done_case, "files": payload.get("artifacts", [])}, ensure_ascii=False))
    return web.json_response(payload, status=status)


# ---------------- Foundry 모델 ----------------
SCORE_PROMPT = """너는 AI 도입 진단 인터뷰의 평가자다. 아래 질문에 대한 고객 답변 받아쓰기를 읽고
질문 취지에 맞게 답변을 요약하고 점수를 확정한다.

점수 기준 (구두 인터뷰 기준 — 말로 확인된 수준으로 판단)
0 = 없음(관심·계획 모두 없음)
1 = 필요성만 인식(문제는 알지만 대상·방향이 아직 없음)
2 = 대상과 방향이 있음(무엇을 개선할지 말하고, 어떤 점을 고려·검토 중인지 설명함)
3 = 실제로 진행 중(담당·추진 방식이 정해졌거나 일부 실행·정리된 것이 있다고 말함)
4 = 체계적으로 운영 중(담당·정기 보고·점검처럼 정해진 방식으로 꾸준히 관리한다고 말함, 아래 4점 모습에 가까움)
- 4점 모습의 "문서로 정리·기록·측정됨" 같은 표현은, 고객이 그렇게 정해진 방식으로 운영하고 있다고 말하면 충족으로 본다. 문서 실물이나 문서화 여부를 따로 말하지 않았다는 이유로 낮추지 않는다(인터뷰는 자료 확인이 아님).
- 점수는 이 질문이 묻는 것만으로 매긴다. 질문이 묻지 않은 다른 주제(예: 현재 업무를 묻는데 AI 적용 계획·우선순위)가 없다고 점수를 낮추지 않는다. 그런 내용은 부족한 점이 아니라 다음 단계 제안에만 쓴다.

인터뷰 답변의 성격
- 고객은 미리 준비한 자료를 읽는 것이 아니라 대화 중에 즉석으로 답한다. 처리 순서·부서 구조·수치 같은 세부를 말로 다 설명하기 어렵다.
- 그러므로 세부 절차·조직도·문서를 말로 구체적으로 설명하지 못했다는 이유만으로 감점하지 않는다.
- "이런 것이 있다", "이런 부분을 고려·검토하고 있다"처럼 존재와 방향을 말하면 그 수준(대개 2점)으로 인정한다.
- 확인 포인트·4점 모습은 방향을 잡는 기준이지 모든 세부를 답해야 하는 체크리스트가 아니다.
- 부족한 점에 "처리 순서·부서 구조·세부 절차를 구체적으로 설명하지 않음" 같은 말로 설명할 세부의 부재를 쓰지 않는다. 부족한 점은 방향·담당·실행 여부처럼 다음 단계로 가는 데 실제로 빠진 것만 쓴다.
- 보완 인터뷰에서 고객이 방향·고려 사항만 보태도 그만큼 반영해 점수를 다시 매긴다. 세부가 여전히 없다는 이유로 이전 점수를 유지하지 않는다.
- 부족한 점·다음 점수에 필요한 것은 "세부 문서화" 요구보다 다음 단계로 가기 위한 실천(예: 담당 지정, 우선순위 결정, 시범 적용)을 쉬운 말로 제안한다.
미확인 = 답변으로 판단할 근거가 없음
해당없음 = 고객 상황에 맞지 않음이 답변에 분명히 드러남

받아쓰기 표기
- 각 줄 앞의 [인터뷰어]는 진행자, [고객]은 고객, [?]는 판별하지 못한 짧은 구간이다. 표시가 없는 줄은 사람이 직접 입력한 메모다.
- 목소리로 자동 판별한 표시이므로 가끔 틀릴 수 있다. 질문을 읽거나 되묻는 말투면 [고객] 표시여도 진행자 말로 본다.

요약·점수 근거로 쓰는 것
- 고객이 직접 말한 내용.
- 진행자가 고객 말을 정리·확인했고(예: "정리하면 ~라는 말씀이시죠?") 고객이 바로 동의한 경우(예: "네, 맞습니다", "그렇죠") 그 정리 내용은 고객 의견으로 본다. 요약에는 고객 의견으로 쓴다.
- 진행자가 정리했지만 고객이 고치거나 부분만 동의하면 고객이 고친 내용만 쓴다.
- 고객이 동의하지 않았거나 대답이 없는 진행자의 설명·예시·질문은 근거로 쓰지 않는다.

보완 인터뷰
- 받아쓰기에 "[보완 인터뷰]" 줄이 있으면 그 아래는 첫 답변에서 부족했던 부분을 다시 물어 받은 추가 답변이다.
- 요약은 첫 답변과 보완 답변을 하나로 합쳐 쓴다(따로 나누지 않는다). 보완 답변이 첫 답변을 고치면 보완 답변을 따른다.
- 점수·평가 근거도 첫 답변과 보완 답변 전체로 다시 판단한다. reason 에는 보완 답변으로 무엇이 새로 확인됐는지 밝힌다.

규칙
- AI를 아직 안 쓴다는 이유만으로 낮은 점수를 주지 않는다. 계획·설계·추정 자료가 있으면 그 수준으로 평가한다.
- 답변에 없는 사실을 만들지 않는다. 판단 근거가 부족하면 미확인으로 한다.
- 요약은 비IT 독자도 이해할 쉬운 한국어 2~4문장.
- 평가 근거는 확인 포인트와 4점 모습에 비춰 쓴다. 잘된 점·부족한 점은 각각 답변에서 실제 확인된 사실만 짧은 문장으로 0~3개 쓴다(없으면 빈 목록).
- 다음 점수로 올라가려면 무엇이 필요한지 한 문장으로 쓴다(4점·해당없음이면 빈 문자열).
- 출력은 JSON 한 개만: {"summary": "고객 의견 요약", "score": "0|1|2|3|4|미확인|해당없음", "reason": "점수 근거 한두 문장(진행자 정리에 고객이 동의한 내용을 썼다면 그렇다고 밝힌다)", "strengths": ["잘된 것으로 본 부분"], "gaps": ["부족하다고 본 부분"], "next": "다음 점수에 필요한 것", "opinion": "보고서 '평가 의견' 칸용 2~3문장: 확인된 현황과 그렇게 본 이유(평서문, 고객명·문항번호 쓰지 않음)", "improve": "보고서 '개선 방향' 칸용 1~2문장: 다음 단계로 가기 위한 실천 제안"}

질문: {question}
확인 포인트: {point}
4점 모습: {top}
받아쓰기:
{transcript}
"""


async def ai_score(q, transcript):
    tok = await mi_token("https://cognitiveservices.azure.com")
    body = {"model": MODEL, "input": SCORE_PROMPT.replace("{question}", q["question"]).replace("{point}", q["point"])
            .replace("{top}", q["top"]).replace("{transcript}", transcript[:12000]),
            "text": {"format": {"type": "json_object"}}}
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{FD_ENDPOINT}/openai/v1/responses", json=body,
                          headers={"Authorization": f"Bearer {tok}"},
                          timeout=aiohttp.ClientTimeout(total=120)) as r:
            d = await r.json()
            if r.status >= 400:
                raise RuntimeError(f"model {r.status}: {str(d)[:300]}")
    text = d.get("output_text") or "".join(
        c.get("text", "") for o in d.get("output", []) if o.get("type") == "message" for c in o.get("content", []))
    out = json.loads(text)
    sc = str(out.get("score", "미확인")).strip()
    if sc not in scoring.VALID:
        sc = "미확인"
    lst = lambda k: [str(x).strip() for x in (out.get(k) or []) if str(x).strip()][:3]
    detail = {"strengths": lst("strengths"), "gaps": lst("gaps"), "next": str(out.get("next") or "").strip(),
              "opinion": str(out.get("opinion") or "").strip()[:400], "improve": str(out.get("improve") or "").strip()[:300]}
    return out.get("summary", "").strip(), sc, out.get("reason", "").strip(), detail


# ---------------- 인터뷰어 목소리 ----------------
# 인터뷰어는 계정마다 다르므로(동료도 진행) 목소리 특징값(임베딩 벡터)을 계정별로 저장한다.
# 음성 원본은 저장하지 않는다. 받아쓰기 구간마다 이 벡터와 비교해 [인터뷰어]/[고객]을 붙인다.
SPK_MODEL = os.environ.get("AICAF_SPK_MODEL", r"C:\AICAF\models\spk.onnx")
SPK_THRESHOLD = float(os.environ.get("AICAF_SPK_THRESHOLD", "0.6"))  # 합성 음성 실측: 본인 0.90~0.92, 타인 0.24~0.41
SPK_MIN_SECONDS = 1.0     # 이보다 짧은 구간은 판별하지 않는다(맞장구 등)
ENROLL_MIN_SAMPLES = 3
ENROLL_MIN_SECONDS = 3.0  # 문장 하나당 최소 길이
_spk = None


def _extractor():
    global _spk
    if _spk is None:
        import sherpa_onnx
        _spk = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=SPK_MODEL, num_threads=2))
    return _spk


def spk_embed(pcm: bytes):
    """16kHz mono PCM16 → 정규화된 화자 임베딩(list)."""
    import numpy as np
    x = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768
    ex = _extractor()
    st = ex.create_stream()
    st.accept_waveform(16000, x)
    st.input_finished()
    v = np.array(ex.compute(st), dtype=np.float32)
    n = float(np.linalg.norm(v))
    return (v / n).tolist() if n else None


def _cos(a, b):
    return float(sum(x * y for x, y in zip(a, b)))


def voiceprint_of(username):
    with db() as c:
        r = c.execute("select emb,samples,seconds,consistency,created from voiceprints where username=?", (username,)).fetchone()
    return dict(r) if r else None


ENROLL_SESSIONS = {}  # username -> [embedding, ...] (등록 진행 중인 문장들, 메모리에만)


async def api_voice_status(req):
    u = req["user"]["upn"]
    vp = voiceprint_of(u)
    return web.json_response({"enrolled": bool(vp), "samples": vp["samples"] if vp else 0,
                              "created": vp["created"] if vp else None,
                              "pending": len(ENROLL_SESSIONS.get(u, [])), "need": ENROLL_MIN_SAMPLES})


async def api_voice_sample(req):
    """문장 하나를 읽은 PCM을 받아 임베딩만 모아 둔다. 원본 음성은 버린다."""
    u = req["user"]["upn"]
    pcm = await req.read()
    sec = len(pcm) / 32000
    if sec < ENROLL_MIN_SECONDS:
        return web.json_response({"ok": False, "error": f"너무 짧습니다({sec:.1f}초). 문장을 끝까지 읽어 주세요."}, status=400)
    if _rms(pcm) < SILENCE_RMS:
        return web.json_response({"ok": False, "error": "소리가 거의 들리지 않습니다. 마이크를 확인해 주세요."}, status=400)
    emb = await asyncio.get_running_loop().run_in_executor(None, spk_embed, pcm)
    if not emb:
        return web.json_response({"ok": False, "error": "목소리 특징을 뽑지 못했습니다. 다시 읽어 주세요."}, status=400)
    ENROLL_SESSIONS.setdefault(u, []).append((emb, sec))
    return web.json_response({"ok": True, "pending": len(ENROLL_SESSIONS[u]), "need": ENROLL_MIN_SAMPLES})


async def api_voice_commit(req):
    """모은 문장들이 같은 사람 목소리인지 확인한 뒤 평균 벡터를 저장한다."""
    u = req["user"]["upn"]
    items = ENROLL_SESSIONS.get(u, [])
    if len(items) < ENROLL_MIN_SAMPLES:
        return web.json_response({"ok": False, "error": f"문장 {ENROLL_MIN_SAMPLES}개 이상 필요합니다."}, status=400)
    dim = len(items[0][0])
    mean = [sum(e[i] for e, _ in items) / len(items) for i in range(dim)]
    n = sum(x * x for x in mean) ** 0.5
    mean = [x / n for x in mean]
    sims = [_cos(mean, e) for e, _ in items]
    if min(sims) < 0.7:
        ENROLL_SESSIONS.pop(u, None)
        return web.json_response({"ok": False, "error": "문장마다 목소리가 다르게 잡혔습니다(주변 소음·다른 사람 목소리). 조용한 곳에서 다시 등록해 주세요."}, status=400)
    with db() as c:
        c.execute("""insert into voiceprints(username,emb,samples,seconds,consistency,created) values(?,?,?,?,?,?)
                     on conflict(username) do update set emb=excluded.emb, samples=excluded.samples, seconds=excluded.seconds,
                     consistency=excluded.consistency, created=excluded.created""",
                  (u, json.dumps(mean), len(items), sum(s for _, s in items), min(sims), time.time()))
    ENROLL_SESSIONS.pop(u, None)
    audit(u, None, None, "voiceprint-enrolled", f"{len(items)} samples, min sim {min(sims):.2f}")
    return web.json_response({"ok": True, "samples": len(items), "consistency": round(min(sims), 3)})


async def api_voice_reset(req):
    u = req["user"]["upn"]
    ENROLL_SESSIONS.pop(u, None)
    with db() as c:
        c.execute("delete from voiceprints where username=?", (u,))
    audit(u, None, None, "voiceprint-deleted")
    return web.json_response({"ok": True})


# ---------------- 실시간 받아쓰기 ----------------
STT_ENDPOINT = os.environ.get("AICAF_STT_ENDPOINT", "https://<FOUNDRY_ACCOUNT>.services.ai.azure.com").rstrip("/")
STT_MODEL = os.environ.get("AICAF_STT_MODEL", "MAI-Transcribe-2-Streaming")
STT_LANG = os.environ.get("AICAF_STT_LANG", "ko")
SILENCE_RMS = 350        # 16-bit PCM 기준 무음 판정 문턱
SILENCE_COMMIT_S = 0.6   # 말한 뒤 이 시간 이상 조용하면 문장 확정
MAX_SEGMENT_S = 15       # 쉬지 않고 길게 말해도 이 시간마다 확정


RECORD_STORE = os.environ.get("AICAF_RECORD_STORE", "https://<STORAGE_ACCOUNT>.blob.core.windows.net/recordings").rstrip("/")
MAX_RECORD_BYTES = 32000 * 60 * 60  # 질문 하나당 최대 1시간


async def save_recording(aid, qid, pcm):
    """녹음 원본을 WAV로 Blob에 보관한다(관리 ID, Private Endpoint)."""
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm)
    name = f"{aid}/{qid}/{time.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(3)}.wav"
    tok = await mi_token("https://storage.azure.com/")
    async with aiohttp.ClientSession() as s:
        async with s.put(f"{RECORD_STORE}/{name}", data=buf.getvalue(), headers={
                "Authorization": f"Bearer {tok}", "x-ms-version": "2021-08-06",
                "x-ms-blob-type": "BlockBlob", "Content-Type": "audio/wav"}) as r:
            if r.status >= 300:
                raise RuntimeError(f"blob {r.status} {(await r.text())[:200]}")
    return name


async def api_recordings(req):
    aid = int(req.match_info["aid"])
    need_project(req, aid)
    with db() as c:
        rows = [dict(r) for r in c.execute("select id,qid,bytes,created,created_by from recordings where assessment_id=? and cleared is null order by id", (aid,))]
    return web.json_response(rows)


async def api_recording_get(req):
    rid = int(req.match_info["rid"])
    with db() as c:
        r = c.execute("select blob, assessment_id from recordings where id=?", (rid,)).fetchone()
    if not r:
        raise web.HTTPNotFound()
    need_project(req, r["assessment_id"])
    tok = await mi_token("https://storage.azure.com/")
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{RECORD_STORE}/{r['blob']}", headers={"Authorization": f"Bearer {tok}", "x-ms-version": "2021-08-06"}) as up:
            if up.status != 200:
                raise web.HTTPNotFound()
            data = await up.read()
    audit(req["user"]["upn"], None, None, "recording-download", r["blob"])
    return web.Response(body=data, content_type="audio/wav")


def _rms(b):
    import array
    a = array.array("h", b[: len(b) // 2 * 2])
    return (sum(x * x for x in a) / len(a)) ** 0.5 if a else 0.0


async def ws_transcribe(req):
    """브라우저 16kHz mono PCM16 → Foundry MAI-Transcribe-2-Streaming(Realtime API) 중계.

    서비스는 서버측 음성 감지가 없으므로, 말한 뒤 0.6초 무음이 생기면 commit 해서
    문장을 확정한다(말 중간을 자르지 않기 위함). 쉬지 않는 긴 발화는 15초마다 확정한다.
    delta(확정 조각)를 모은 것이 최종 받아쓰기이고, intermediate는 화면 표시용 임시 문장이다.
    """
    aid, qid = int(req.query["aid"]), req.query["qid"]
    need_project(req, aid, "interviewer")
    question_of(aid, qid)
    with db() as c:
        cons = c.execute("select consent from consents where assessment_id=?", (aid,)).fetchone()
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=4 * 1024 * 1024)
    await ws.prepare(req)
    if not cons or not cons["consent"]:
        await ws.send_json({"type": "error", "text": "녹음 동의가 확인되지 않았습니다."})
        await ws.close()
        return ws
    vp = voiceprint_of(req["user"]["upn"])
    if not vp:
        await ws.send_json({"type": "error", "text": "인터뷰어 목소리 등록이 필요합니다."})
        await ws.close()
        return ws
    ref = json.loads(vp["emb"])
    LIVE_REC[aid] = LIVE_REC.get(aid, 0) + 1
    try:
        return await _ws_transcribe_body(req, ws, aid, qid, ref)
    finally:
        LIVE_REC[aid] -= 1
        if LIVE_REC[aid] <= 0:
            LIVE_REC.pop(aid, None)


LIVE_REC = {}  # 프로젝트별 열린 녹음 세션 수(삭제 차단용)


async def _ws_transcribe_body(req, ws, aid, qid, ref):
    labels = []          # commit 순서대로 화자 판별 결과(대기열)
    seg_pcm = bytearray()  # 마지막 commit 이후 오디오

    async def label_segment(pcm):
        sec = len(pcm) / 32000
        if sec < SPK_MIN_SECONDS:
            return "?"
        emb = await asyncio.get_running_loop().run_in_executor(None, spk_embed, bytes(pcm))
        if not emb:
            return "?"
        return "인터뷰어" if _cos(ref, emb) >= SPK_THRESHOLD else "고객"
    tok = await mi_token("https://cognitiveservices.azure.com")
    url = "wss://" + urllib.parse.urlparse(STT_ENDPOINT).hostname + "/mai/v1/realtime?intent=transcription"
    finals = []
    raw = bytearray()
    audit(req["user"]["upn"], aid, qid, "record-start")
    s = aiohttp.ClientSession()

    async def open_up():
        """받아쓰기 서비스 연결. 중간에 끊기면 다시 열어 녹음을 이어 간다."""
        t = await mi_token("https://cognitiveservices.azure.com")
        u = await s.ws_connect(url, headers={"Authorization": "Bearer " + t}, heartbeat=20)
        await u.receive_json()  # session.created
        await u.send_json({"type": "session.update", "session": {"type": "transcription", "audio": {"input": {
            "format": {"type": "audio/pcm", "rate": 16000},
            "transcription": {"model": STT_MODEL, "language": STT_LANG},
            "turn_detection": None, "noise_reduction": None}}}})
        m = await u.receive_json()
        if m.get("type") != "session.updated":
            await u.close()
            raise RuntimeError(f"session.update: {str(m)[:200]}")
        return u

    try:
        up = await open_up()
    except Exception as e:
        await s.close()
        audit(req["user"]["upn"], aid, qid, "record-error", f"connect: {e}"[:300])
        await ws.send_json({"type": "error", "text": f"받아쓰기 연결 실패: {str(e)[:200]}"})
        await ws.close()
        return ws
    reconnects = 0
    if True:
        if True:
            stop = asyncio.Event()
            pending = 0          # 마지막 commit 이후 보낸 바이트
            seg_speech = False   # 이번 구간에 말소리가 있었는지
            silent_s = 0.0       # 말소리 뒤 연속 무음 길이

            async def downstream():
                cur = ""
                async for msg in up:
                    if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    ev = json.loads(msg.data)
                    t = ev.get("type", "")
                    if t.endswith("transcription.delta"):
                        cur += ev.get("delta", "")
                        await ws.send_json({"type": "partial", "text": cur})
                    elif t.endswith("transcription.intermediate"):
                        await ws.send_json({"type": "partial", "text": cur + ev.get("intermediate", "")})
                    elif t.endswith("transcription.completed"):
                        txt = (ev.get("transcript") or cur).strip()
                        cur = ""
                        who = (await labels.pop(0)) if labels else "?"
                        if txt:
                            line = f"[{who}] {txt}"
                            finals.append(line)
                            await ws.send_json({"type": "final", "text": line, "speaker": who})
                        if stop.is_set():
                            return
                    elif t == "error":
                        await ws.send_json({"type": "error", "text": str(ev.get("error"))[:300]})

            dtask = asyncio.create_task(downstream())
            try:
                async for m in ws:
                    if m.type == aiohttp.WSMsgType.BINARY:
                        if len(raw) < MAX_RECORD_BYTES:
                            raw.extend(m.data)
                        try:
                            if up.closed:
                                raise ConnectionResetError("upstream closed")
                            await up.send_json({"type": "input_audio_buffer.append", "audio": base64.b64encode(m.data).decode()})
                        except Exception as e:
                            # 받아쓰기 서비스 연결이 끊김: 브라우저 녹음은 유지한 채 다시 연결한다.
                            # 끊기기 전 미확정 구간은 버리고(화자 대기열도 비움) 새 연결에서 이어 쓴다.
                            reconnects += 1
                            audit(req["user"]["upn"], aid, qid, "record-reconnect", f"{type(e).__name__}: {e}"[:200])
                            if reconnects > 20:
                                raise
                            dtask.cancel()
                            for f in labels:
                                f.cancel()
                            labels.clear()
                            seg_pcm.clear()
                            pending, seg_speech, silent_s = 0, False, 0.0
                            with contextlib.suppress(Exception):
                                await up.close()
                            up = await open_up()
                            dtask = asyncio.create_task(downstream())
                            await ws.send_json({"type": "partial", "text": "(받아쓰기 연결 재시도 — 녹음은 계속됩니다)"})
                            await up.send_json({"type": "input_audio_buffer.append", "audio": base64.b64encode(m.data).decode()})
                        pending += len(m.data)
                        seg_pcm.extend(m.data)
                        dur = len(m.data) / 32000
                        if _rms(m.data) >= SILENCE_RMS:
                            seg_speech, silent_s = True, 0.0
                        elif seg_speech:
                            silent_s += dur
                        if pending and ((seg_speech and silent_s >= SILENCE_COMMIT_S) or pending / 32000 >= MAX_SEGMENT_S):
                            labels.append(asyncio.ensure_future(label_segment(bytes(seg_pcm))))
                            seg_pcm.clear()
                            await up.send_json({"type": "input_audio_buffer.commit"})
                            pending, seg_speech, silent_s = 0, False, 0.0
                    elif m.type == aiohttp.WSMsgType.TEXT and m.data == "stop":
                        break
            except Exception as e:
                audit(req["user"]["upn"], aid, qid, "record-error", f"{type(e).__name__}: {e}"[:300])
                with contextlib.suppress(Exception):
                    await ws.send_json({"type": "error", "text": f"녹음 중 오류로 중지되었습니다. 지금까지 내용은 저장됩니다. ({type(e).__name__})"})
            finally:
                stop.set()
                try:
                    if pending:
                        labels.append(asyncio.ensure_future(label_segment(bytes(seg_pcm))))
                        await up.send_json({"type": "input_audio_buffer.commit"})
                        await asyncio.wait_for(dtask, 15)
                except Exception:
                    pass
                dtask.cancel()
                with contextlib.suppress(Exception):
                    await up.close()
                await s.close()
    text = "\n".join(finals).strip()
    if raw and RECORD_STORE:
        try:
            blob = await save_recording(aid, qid, bytes(raw))
            with db() as c:
                c.execute("insert into recordings(assessment_id,qid,blob,bytes,created,created_by) values(?,?,?,?,?,?)",
                          (aid, qid, blob, len(raw), time.time(), req["user"]["upn"]))
        except Exception as e:  # 녹음 보관 실패가 받아쓰기 결과를 막지 않게 한다
            audit(req["user"]["upn"], aid, qid, "record-save-failed", str(e)[:200])
    if text:
        with db() as c:
            prev = c.execute("select transcript from answers where assessment_id=? and qid=?", (aid, qid)).fetchone()
            if req.query.get("mode") == "supp":
                text = SUPP_MARK + "\n" + text  # 보완 녹음은 구분 줄 아래에 붙인다
            full = ((prev["transcript"] + "\n") if prev and prev["transcript"] else "") + text
            c.execute("""insert into answers(assessment_id,qid,transcript,updated) values(?,?,?,?)
                         on conflict(assessment_id,qid) do update set transcript=excluded.transcript, updated=excluded.updated""",
                      (aid, qid, full, time.time()))
    audit(req["user"]["upn"], aid, qid, "record-stop", f"{len(text)} chars")
    if not ws.closed:
        await ws.send_json({"type": "done", "text": text})
        await ws.close()
    return ws


# ---------------- API ----------------
def can_edit(user):
    """레거시 호출 호환. 실제 권한은 need_project 로 프로젝트별 확인한다."""
    return True


async def api_me(req):
    u = req["user"]
    r = user_roles(u)
    return web.json_response({"name": u["name"], "upn": u["upn"], "roles": r, "is_admin": "aicaf.admin" in r,
                              "can_create": can_create_project(u), "sys_roles": SYS_ROLES, "proj_roles": PROJ_ROLES})


async def api_questions(req):
    """유형별 문항. ?track= 또는 ?aid= (aid 면 프로젝트 권한 확인 후 그 유형)."""
    if "aid" in req.query:
        aid = int(req.query["aid"])
        need_project(req, aid)
        track = track_of(aid)
        if _WORKFLOW is not None:
            return web.json_response(_WORKFLOW.questions(aid, required=True))
    else:
        track = req.query.get("track", "")
    if track not in TRACKS:
        raise web.HTTPBadRequest(text="track 은 new 또는 existing 입니다.")
    return web.json_response(_QCACHE[track])


async def api_tracks(req):
    return web.json_response({t: {k: v for k, v in m.items()} for t, m in TRACK_META.items()})


async def api_list(req):
    u = req["user"]
    with db() as c:
        if is_admin(u):
            rows = [dict(r) | {"my_role": "manager"} for r in c.execute("select * from assessments where deleted_at is null order by id desc")]
        else:
            rows = [dict(r) for r in c.execute("""select a.*, m.role as my_role from assessments a
                     join project_members m on m.assessment_id=a.id and m.username=? where a.deleted_at is null order by a.id desc""", (u["upn"],))]
        cnt = {r["assessment_id"]: r["n"] for r in c.execute("select assessment_id, count(*) n from project_members group by assessment_id")}
    for r in rows:
        r["members"] = cnt.get(r["id"], 0)
    return web.json_response(rows)


async def api_create(req):
    if not can_create_project(req["user"]):
        raise web.HTTPForbidden(text="프로젝트를 만들 권한이 없습니다. 관리자에게 '프로젝트 관리자' 권한을 요청하세요.")
    d = await req.json()
    customer = str(d.get("customer", "")).strip()
    cname, crole, cmail = (str(d.get(k, "")).strip() for k in ("contact_name", "contact_role", "contact_email"))
    if not customer:
        raise web.HTTPBadRequest(text="고객명을 입력하세요.")
    if not (cname and crole and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", cmail)):
        raise web.HTTPBadRequest(text="보고 담당자 이름·직책·이메일을 입력하세요.")
    track = str(d.get("track", "")).strip()
    if track not in TRACKS:  # 기본값 없음: 반드시 고르게 한다
        raise web.HTTPBadRequest(text="고객 유형(신규구축·기구축)을 선택하세요.")
    with db() as c:
        cur = c.execute("""insert into assessments(customer,title,interviewer,created,created_by,contact_name,contact_role,contact_email,track)
                           values(?,?,?,?,?,?,?,?,?)""",
                        (customer, d.get("title", "").strip(), d.get("interviewer", "").strip(),
                         time.time(), req["user"]["upn"], cname, crole, cmail, track))
        aid = cur.lastrowid
        c.execute("insert into project_members values(?,?,?,?,?)", (aid, req["user"]["upn"], "manager", time.time(), req["user"]["upn"]))
    try:
        case_id, _ = ensure_infra_case(_assessment(aid))
        infra_error = None
    except Exception as e:
        case_id, infra_error = None, str(e)[:300]
    audit(req["user"]["upn"], aid, None, "create", f"{customer} [{track}]")
    return web.json_response({"id": aid, "infra_case_id": case_id, "infra_error": infra_error})


async def api_get(req):
    aid = int(req.match_info["aid"])
    my_role = need_project(req, aid)
    with db() as c:
        a = c.execute("select * from assessments where id=?", (aid,)).fetchone()
        if not a:
            raise web.HTTPNotFound()
        cons = c.execute("select * from consents where assessment_id=?", (aid,)).fetchone()
        ans = {r["qid"]: dict(r) for r in c.execute("select * from answers where assessment_id=?", (aid,))}
    workflow = workflow_state(aid)
    qs = workflow.pop('_questions', _QCACHE[a['track']])
    return web.json_response({"assessment": dict(a), "consent": dict(cons) if cons else None, "my_role": my_role,
                              "answers": ans, "result": scoring.compute(qs, ans), "workflow": workflow})


async def api_consent(req):
    aid = int(req.match_info["aid"])
    need_project(req, aid, "interviewer")
    d = await req.json()
    ok = 1 if d.get("consent") else 0
    with db() as c:
        c.execute("insert or replace into consents values(?,?,?,?,?)",
                  (aid, ok, req["user"]["upn"], time.time(), d.get("note", "")[:500]))
    audit(req["user"]["upn"], aid, None, "consent", "yes" if ok else "no")
    return web.json_response({"ok": True})


async def api_score(req):
    """받아쓰기(또는 직접 입력 메모)를 AI로 요약·점수 확정."""
    aid, qid = int(req.match_info["aid"]), req.match_info["qid"]
    need_project(req, aid, "interviewer")
    q = question_of(aid, qid)
    d = await req.json() if req.can_read_body else {}
    with db() as c:
        row = c.execute("select transcript from answers where assessment_id=? and qid=?", (aid, qid)).fetchone()
    transcript = (d.get("transcript") or (row["transcript"] if row else "")).strip()
    if not transcript:
        raise web.HTTPBadRequest(text="받아쓰기 내용이 없습니다.")
    summary, sc, reason, detail = await ai_score(q, transcript)
    with db() as c:
        old = c.execute("select ai_score, prev_score from answers where assessment_id=? and qid=?", (aid, qid)).fetchone()
        # 보완 내용이 들어간 첫 재평가 때 직전 AI 점수를 '보완 전 점수'로 남긴다
        prev_score = (old["prev_score"] or old["ai_score"]) if old and SUPP_MARK in transcript else None
        c.execute("update answers set prev_score=? where assessment_id=? and qid=?", (prev_score, aid, qid))
        c.execute("""insert into answers(assessment_id,qid,transcript,summary,score,ai_score,ai_reason,ai_detail,edited_by,edited_at,updated)
                     values(?,?,?,?,?,?,?,?,null,null,?)
                     on conflict(assessment_id,qid) do update set transcript=excluded.transcript, summary=excluded.summary,
                     score=excluded.score, ai_score=excluded.ai_score, ai_reason=excluded.ai_reason, ai_detail=excluded.ai_detail,
                     edited_by=null, edited_at=null, updated=excluded.updated""",
                  (aid, qid, transcript, summary, sc, sc, reason, json.dumps(detail, ensure_ascii=False), time.time()))
    audit(req["user"]["upn"], aid, qid, "ai-score", sc)
    return web.json_response({"summary": summary, "score": sc, "reason": reason, "detail": detail, "prev_score": prev_score})


SUPP_MARK = "[보완 인터뷰]"


async def api_supplement(req):
    """보완 답변(직접 입력 메모)을 받아쓰기 끝에 구분 줄과 함께 붙인다. 재평가는 /score 가 한다."""
    aid, qid = int(req.match_info["aid"]), req.match_info["qid"]
    need_project(req, aid, "interviewer")
    question_of(aid, qid)
    text = str((await req.json()).get("text", "")).strip()
    if not text:
        raise web.HTTPBadRequest(text="보완 내용이 없습니다.")
    with db() as c:
        prev = c.execute("select transcript from answers where assessment_id=? and qid=?", (aid, qid)).fetchone()
        full = ((prev["transcript"] + "\n") if prev and prev["transcript"] else "") + SUPP_MARK + "\n" + text[:6000]
        c.execute("""insert into answers(assessment_id,qid,transcript,updated) values(?,?,?,?)
                     on conflict(assessment_id,qid) do update set transcript=excluded.transcript, updated=excluded.updated""",
                  (aid, qid, full, time.time()))
    audit(req["user"]["upn"], aid, qid, "supplement-note", f"{len(text)} chars")
    return web.json_response({"transcript": full})


async def api_reset(req):
    """문항 하나를 처음 상태로: 받아쓰기·요약·점수·근거를 지우고 녹음은 목록에서 숨긴다(원본은 DB에 보존)."""
    aid, qid = int(req.match_info["aid"]), req.match_info["qid"]
    need_project(req, aid, "interviewer")
    question_of(aid, qid)
    with db() as c:
        old = c.execute("select score from answers where assessment_id=? and qid=?", (aid, qid)).fetchone()
        c.execute("delete from answers where assessment_id=? and qid=?", (aid, qid))
        c.execute("update recordings set cleared=? where assessment_id=? and qid=? and cleared is null", (time.time(), aid, qid))
    audit(req["user"]["upn"], aid, qid, "reset", old["score"] if old and old["score"] else "")
    return web.json_response({"ok": True})


async def api_edit(req):
    """사람이 요약·점수를 수정. AI 원래 점수는 ai_score에 남는다."""
    aid, qid = int(req.match_info["aid"]), req.match_info["qid"]
    need_project(req, aid, "interviewer")
    question_of(aid, qid)
    d = await req.json()
    sc = str(d.get("score", "")).strip()
    if sc and sc not in scoring.VALID:
        raise web.HTTPBadRequest(text="invalid score")
    with db() as c:
        c.execute("""insert into answers(assessment_id,qid,summary,score,edited_by,edited_at,updated) values(?,?,?,?,?,?,?)
                     on conflict(assessment_id,qid) do update set summary=excluded.summary, score=excluded.score,
                     edited_by=excluded.edited_by, edited_at=excluded.edited_at, updated=excluded.updated""",
                  (aid, qid, d.get("summary", ""), sc or None, req["user"]["upn"], time.time(), time.time()))
    audit(req["user"]["upn"], aid, qid, "human-edit", sc)
    return web.json_response({"ok": True})


async def api_project_update(req):
    """고객·과제·보고 담당자 수정(프로젝트 관리자)."""
    aid = int(req.match_info["aid"])
    need_project(req, aid, "manager")
    d = await req.json()
    if "track" in d:
        raise web.HTTPBadRequest(text="고객 유형은 만든 뒤 바꿀 수 없습니다(답변이 문항에 묶여 있음).")
    fields = {k: str(d[k]).strip() for k in ("customer", "title", "interviewer", "contact_name", "contact_role", "contact_email") if k in d}
    if "customer" in fields and not fields["customer"]:
        raise web.HTTPBadRequest(text="고객명을 입력하세요.")
    if fields:
        with db() as c:
            c.execute("update assessments set " + ",".join(f"{k}=?" for k in fields) + " where id=?", (*fields.values(), aid))
    audit(req["user"]["upn"], aid, None, "project-update", ",".join(fields))
    return web.json_response({"ok": True})


async def api_members(req):
    aid = int(req.match_info["aid"])
    need_project(req, aid)
    with db() as c:
        rows = [dict(r) for r in c.execute("""select m.username, m.role, m.added, m.added_by, l.display_name, l.email
                 from project_members m left join local_accounts l on l.username=m.username
                 where m.assessment_id=? order by case m.role when 'manager' then 0 when 'interviewer' then 1 else 2 end, m.username""", (aid,))]
    return web.json_response(rows)


async def api_member_put(req):
    """멤버 추가·역할 변경(프로젝트 관리자). 승인된 계정만 추가할 수 있다."""
    aid = int(req.match_info["aid"])
    need_project(req, aid, "manager")
    d = await req.json()
    uname, role = str(d.get("username", "")).strip(), str(d.get("role", "")).strip()
    if role not in PROJ_ROLES:
        raise web.HTTPBadRequest(text="역할이 올바르지 않습니다.")
    with db() as c:
        acct = c.execute("select status from local_accounts where username=?", (uname,)).fetchone()
        if not acct or (acct["status"] or "active") != "active":
            raise web.HTTPBadRequest(text="승인된 사용자만 추가할 수 있습니다.")
        if role != "manager" and _last_manager(c, aid, uname):
            raise web.HTTPBadRequest(text="프로젝트 관리자가 최소 1명은 있어야 합니다.")
        c.execute("""insert into project_members values(?,?,?,?,?) on conflict(assessment_id,username)
                     do update set role=excluded.role""", (aid, uname, role, time.time(), req["user"]["upn"]))
    audit(req["user"]["upn"], aid, None, "member-set", f"{uname}={role}")
    return web.json_response({"ok": True})


def _last_manager(c, aid, uname):
    mgrs = [r["username"] for r in c.execute("select username from project_members where assessment_id=? and role='manager'", (aid,))]
    return mgrs == [uname]


async def api_member_delete(req):
    aid = int(req.match_info["aid"])
    need_project(req, aid, "manager")
    uname = req.match_info["username"]
    with db() as c:
        if _last_manager(c, aid, uname):
            raise web.HTTPBadRequest(text="마지막 프로젝트 관리자는 뺄 수 없습니다.")
        c.execute("delete from project_members where assessment_id=? and username=?", (aid, uname))
    audit(req["user"]["upn"], aid, None, "member-remove", uname)
    return web.json_response({"ok": True})


async def api_user_directory(req):
    """멤버 추가용: 승인된 사용자 목록(아이디·이름만)."""
    with db() as c:
        rows = [dict(r) for r in c.execute("select username, display_name from local_accounts where coalesce(status,'active')='active' order by display_name")]
    return web.json_response(rows)


def _need_admin(req):
    if not is_admin(req["user"]):
        raise web.HTTPForbidden(text="관리자만 할 수 있습니다.")


async def api_admin_users(req):
    _need_admin(req)
    with db() as c:
        rows = [dict(r) for r in c.execute("""select username, display_name, email, roles, coalesce(status,'active') status,
                 requested_note, created, approved_by, approved_at from local_accounts
                 order by case coalesce(status,'active') when 'pending' then 0 else 1 end, created desc""")]
        pc = {r["username"]: r["n"] for r in c.execute("select username, count(*) n from project_members group by username")}
    for r in rows:
        r["roles"] = json.loads(r["roles"])
        r["projects"] = pc.get(r["username"], 0)
    return web.json_response(rows)


async def api_admin_user_put(req):
    """가입 승인·거절·중지·재개, 시스템 권한 변경(관리자)."""
    _need_admin(req)
    uname = req.match_info["username"]
    d = await req.json()
    me = req["user"]["upn"]
    with db() as c:
        acct = c.execute("select * from local_accounts where username=?", (uname,)).fetchone()
        if not acct:
            raise web.HTTPNotFound()
        sets, vals, log = [], [], []
        if "status" in d:
            st = str(d["status"])
            if st not in ("active", "pending", "rejected", "disabled"):
                raise web.HTTPBadRequest(text="상태가 올바르지 않습니다.")
            if uname == me and st != "active":
                raise web.HTTPBadRequest(text="본인 계정은 중지할 수 없습니다.")
            sets += ["status=?"]
            vals += [st]
            if st == "active" and (acct["status"] or "active") != "active":
                sets += ["approved_by=?", "approved_at=?"]
                vals += [me, time.time()]
            log.append("status=" + st)
        if "role" in d:
            role = str(d["role"])
            if role not in SYS_ROLES:
                raise web.HTTPBadRequest(text="권한이 올바르지 않습니다.")
            if uname == me and role != "aicaf.admin":
                raise web.HTTPBadRequest(text="본인의 관리자 권한은 내릴 수 없습니다.")
            sets += ["roles=?"]
            vals += [json.dumps([role])]
            log.append("role=" + role)
        if sets:
            c.execute("update local_accounts set " + ",".join(sets) + " where username=?", (*vals, uname))
            if any(x.startswith("status=") and not x.endswith("active") for x in log):
                c.execute("delete from sessions where upn=?", (uname,))
    audit(me, None, None, "admin-user", f"{uname}: {' '.join(log)}")
    return web.json_response({"ok": True})


async def api_report(req):
    aid = int(req.match_info["aid"])
    need_project(req, aid)
    if _WORKFLOW is not None:
        value = workflow_state(aid)
        record = value['reports'][1]['record']
        if not value['reports'][1]['ready']:
            raise web.HTTPConflict(text='공식 지침 기반 실제 인터뷰와 인터뷰 보고서 검토를 먼저 완료해야 합니다.')
        raw, found = _WORKFLOW.interview.artifact(aid, record['revision_id'], 'pptx')
        return web.Response(body=raw, content_type='application/vnd.openxmlformats-officedocument.presentationml.presentation',
                            headers={'Cache-Control': 'no-store', 'X-Content-SHA256': found['sha256'],
                                     'Content-Disposition': f'attachment; filename="AI_Assessment_Interview_{aid}.pptx"'})
    with db() as c:
        a = dict(c.execute("select * from assessments where id=?", (aid,)).fetchone())
        ans = {r["qid"]: dict(r) for r in c.execute("select * from answers where assessment_id=?", (aid,))}
    qs = _QCACHE[a["track"]]
    res = scoring.compute(qs, ans)
    data = await asyncio.get_running_loop().run_in_executor(None, interview_report.build, a, qs, ans, res, TRACK_META[a["track"]])
    audit(req["user"]["upn"], aid, None, "report")
    name = urllib.parse.quote(f"AI_Interview_Assessment_{a['customer']}.pptx")
    return web.Response(body=data, content_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}"})


async def api_questionnaire(req):
    """유형별 인터뷰 질문지 엑셀(빈 양식). 로그인 사용자 누구나."""
    track = req.match_info["track"]
    if track not in TRACKS:
        raise web.HTTPNotFound()
    data = await asyncio.get_running_loop().run_in_executor(None, interview_report.questionnaire_xlsx,
                                                            _QCACHE[track], TRACK_META[track])
    name = urllib.parse.quote(f"AI_Interview_Questionnaire_{TRACK_META[track]['label']}.xlsx")
    return web.Response(body=data, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}"})


async def healthz(req):
    return web.Response(text="ok")


STATIC = Path(__file__).parent / "static"


async def page(req):
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


def make_app():
    global _WORKFLOW
    init_db()
    app = web.Application(middlewares=[auth_mw], client_max_size=MAX_UPLOAD)
    app.add_routes([
        web.get("/healthz", healthz),
        *([web.get("/login", local_login_get), web.post("/login", local_login_post)] if AUTH_MODE == "local"
          else [web.get("/login", login), web.get("/auth/callback", callback)]),
        web.get("/password", password_get), web.post("/password", password_post),
        web.get("/signup", signup_get), web.post("/signup", signup_post),
        web.get("/logout", logout),
        web.get("/api/aicaf/me", api_me), web.get("/api/aicaf/questions", api_questions),
        web.get("/api/aicaf/assessments", api_list), web.post("/api/aicaf/assessments", api_create),
        web.get("/api/aicaf/assessments/{aid}", api_get), web.post("/api/aicaf/assessments/{aid}/consent", api_consent),
        web.post("/api/aicaf/assessments/{aid}/answers/{qid}/score", api_score),
        web.put("/api/aicaf/assessments/{aid}/answers/{qid}", api_edit),
        web.delete("/api/aicaf/assessments/{aid}/answers/{qid}", api_reset),
        web.post("/api/aicaf/assessments/{aid}/answers/{qid}/supplement", api_supplement),
        web.get("/api/aicaf/assessments/{aid}/report.pptx", api_report),
        web.get("/api/aicaf/tracks", api_tracks), web.get("/api/aicaf/questionnaire/{track}.xlsx", api_questionnaire),
        web.put("/api/aicaf/assessments/{aid}", api_project_update),
        web.delete("/api/aicaf/assessments/{aid}", api_project_delete),
        web.get("/api/aicaf/admin/deleted-projects", api_admin_deleted),
        web.post("/api/aicaf/admin/deleted-projects/{aid}/restore", api_admin_restore),
        web.get("/api/aicaf/assessments/{aid}/members", api_members),
        web.put("/api/aicaf/assessments/{aid}/members", api_member_put),
        web.delete("/api/aicaf/assessments/{aid}/members/{username}", api_member_delete),
        web.get("/api/aicaf/users", api_user_directory),
        web.get("/api/aicaf/admin/users", api_admin_users),
        web.put("/api/aicaf/admin/users/{username}", api_admin_user_put),
        web.get("/ws/transcribe", ws_transcribe),
        web.get("/api/aicaf/voiceprint", api_voice_status),
        web.post("/api/aicaf/voiceprint/sample", api_voice_sample),
        web.post("/api/aicaf/voiceprint/commit", api_voice_commit),
        web.delete("/api/aicaf/voiceprint", api_voice_reset),
        web.get("/api/aicaf/assessments/{aid}/infra", api_infra_status),
        web.post("/api/aicaf/assessments/{aid}/infra/upload", api_infra_upload),
        web.delete("/api/aicaf/assessments/{aid}/infra/source/{name}", api_infra_delete_file),
        web.post("/api/aicaf/assessments/{aid}/infra/start", api_infra_start),
        web.get("/api/aicaf/assessments/{aid}/infra/file/{path:.+}", api_infra_download),
        web.get("/api/aicaf/collector", api_collector),
        web.get("/api/aicaf/guides/{name}", api_guide),
        web.post("/api/aicaf/assessments/{aid}/infra/upload-key", api_upload_key),
        web.post(collector_upload.SESSIONS_PATH, api_intake),
        web.post(collector_upload.SESSIONS_PATH + "/{tail:.+}", api_intake),
        web.get("/api/aicaf/assessments/{aid}/recordings", api_recordings),
        web.get("/api/aicaf/recordings/{rid}", api_recording_get),
        web.static("/static", STATIC),
        web.get("/", page), web.get("/interview/{aid}", page), web.get("/infra", page), web.get("/admin", page), web.get("/admin/deleted", page),
    ])
    # Cost Optimization 탭(별도 모듈)
    cost_view.register(app, ROOT=ROOT, db=db, need_project=need_project, assessment=_assessment,
                       infra_case_dir=infra_case_dir, audit=audit, questions=lambda t: _QCACHE[t],
                       project_questions=lambda aid: (_WORKFLOW.questions(aid) if _WORKFLOW is not None else None) or _QCACHE[track_of(aid)])
    # AS-IS 수집 원장·개정·실물·신규 고객 산출물 경로
    integrated = asis_view.register(app, ROOT=ROOT, db=db, need_project=need_project, assessment=_assessment,
                                   infra_case_dir=infra_case_dir, audit=audit)
    import assessment_flow_view
    _WORKFLOW = assessment_flow_view.register(app, {'ROOT': ROOT, 'db': db, 'need_project': need_project,
        'assessment': _assessment, 'infra_case_dir': infra_case_dir, 'audit': audit}, integrated)
    # 아키텍처 탭(별도 모듈)
    arch_view.register(app, ROOT=ROOT, need_project=need_project, assessment=_assessment,
                       infra_case_dir=infra_case_dir, audit=audit)
    return app


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "create-user":
        # 사용: server.py create-user <username> <role>  (비밀번호는 AICAF_NEW_PASSWORD 환경변수)
        init_db()
        pw = os.environ.pop("AICAF_NEW_PASSWORD", "")
        problem = pw_policy(pw)
        if problem:
            sys.exit(problem)
        create_local_account(sys.argv[2], pw, [sys.argv[3]], must_change=True)
        print("created", sys.argv[2], sys.argv[3], "must_change=1")
        sys.exit(0)
    web.run_app(make_app(), host="0.0.0.0", port=PORT, access_log=None)
