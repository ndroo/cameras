"""
cameras.andrewmcgrath.info - a small, self-hosted viewer for the Tapo cameras.

People log in with their phone number (SMS code).  Admins create houses, import
cameras from the Tapo account, and put people in groups; a group belongs to a house
and grants its members some or all of that house's cameras plus the text alerts it
has turned on (doorbell, tamper, offline/online).  Live video comes through the Tapo
cloud relay into go2rtc (one exec pipe per camera) and is proxied to the browser.
"""
import asyncio, hashlib, json, logging, os, random, re, secrets, shutil, socket, subprocess, sys, tempfile, threading, time, uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx, websockets
from fastapi import FastAPI, Request, Form, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import audio, db, garage, rtsp, sms, tapo

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("app")
HERE = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv(path):
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv(os.path.join(HERE, "..", ".env"))
if not os.environ.get("TAPO_ACCESS_KEY"):
    logging.getLogger("app").warning("TAPO_ACCESS_KEY / TAPO_SECRET_KEY not set: Tapo cloud login will fail")

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(HERE, "..", "data"))
os.makedirs(os.path.join(DATA_DIR, "thumbs"), exist_ok=True)
GO2RTC = os.environ.get("GO2RTC_URL", "http://127.0.0.1:1984")
INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET") or secrets.token_hex(16)
os.environ["INTERNAL_SECRET"] = INTERNAL_SECRET
PIPE_CMD = os.environ.get("PIPE_CMD", "python3 -m app.pipe")
# "rtsp": go2rtc pulls each camera over RTSP from this process (app/rtsp.py), keeping
# the cameras' G.711 sound as-is (go2rtc turns it into FLAC for browsers; no ffmpeg);
# "http": go2rtc pulls MPEG-TS from /internal/live, with an ffmpeg per camera for AAC;
# "exec": the old way, one python pipe process per camera (app/pipe.py)
STREAM_SOURCE = os.environ.get("STREAM_SOURCE", "rtsp")
RTSP_PORT = int(os.environ.get("RTSP_PORT", "8554"))
# live streams block a thread each for as long as someone watches; keep them off the
# default executor (5 threads on this 1-CPU machine) so playback and PTZ never queue
from concurrent.futures import ThreadPoolExecutor
_live_pool = ThreadPoolExecutor(max_workers=48, thread_name_prefix="live")
HEALTH_INTERVAL = int(os.environ.get("HEALTH_INTERVAL", "600"))
OFFLINE_AFTER = int(os.environ.get("OFFLINE_AFTER_FAILS", "2"))
# Shown as offline after OFFLINE_AFTER failed checks, but only texted once it has been down this long
# (Tapo cameras drop off for a check or two now and then and come straight back).
OFFLINE_ALERT_SECS = int(os.environ.get("OFFLINE_ALERT_MINUTES", "30")) * 60
TZ = ZoneInfo(os.environ.get("TZ_NAME", "America/Toronto"))
SECURE_COOKIE = os.environ.get("BASE_URL", "").startswith("https")

cloud = tapo.TapoCloud(os.environ.get("TPLINK_EMAIL", ""), os.environ.get("TPLINK_PASSWORD", ""),
                       os.environ.get("TPLINK_TERMINAL_UUID") or str(uuid.uuid4()).upper(),
                       cache_path=os.path.join(DATA_DIR, "tapo_token.json"))
mq: tapo.MqttSession = None

app = FastAPI(docs_url=None, redoc_url=None)
class _Static(StaticFiles):
    """Browsers revalidate every static file (a cheap 304 when unchanged). video-rtc.js
    is imported by video-stream.js without a ?v= tag, so heuristic caching kept an old
    player running after deploys."""
    async def get_response(self, path, scope):
        r = await super().get_response(path, scope)
        r.headers["Cache-Control"] = "no-cache"
        return r


app.mount("/static", _Static(directory=os.path.join(HERE, "static")), name="static")
tpl = Jinja2Templates(directory=os.path.join(HERE, "templates"))


def fmt_ago(ts):
    if not ts:
        return "never"
    d = int(time.time() - ts)
    if d < 60: return "just now"
    if d < 3600: return f"{d // 60} min ago"
    if d < 86400: return f"{d // 3600} h ago"
    return datetime.fromtimestamp(ts, TZ).strftime("%b %-d, %-I:%M %p")


def fmt_dt(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%a %b %-d, %-I:%M %p") if ts else ""


STATIC_V = str(int(time.time()))          # changes on every start, so browsers refetch scripts after a deploy
tpl.env.globals["v"] = STATIC_V
tpl.env.globals["tz"] = str(TZ)
tpl.env.filters["ago"] = fmt_ago
tpl.env.filters["dt"] = fmt_dt
tpl.env.filters["hm"] = lambda ts: datetime.fromtimestamp(ts, TZ).strftime("%-I:%M %p").lower()
tpl.env.filters["ymd"] = lambda ts: datetime.fromtimestamp(ts, TZ).strftime("%Y%m%d")


def sd_info(cam):
    """What the SD card holds, for the camera lists: None until the first check, else
    {state: card|none|problem, short, long}. short fits a tile, long is the hover/admin text."""
    try:
        sd = json.loads(cam["sd"]) if cam["sd"] else None
    except Exception:
        sd = None
    if not sd:
        return None
    if sd["state"] == "none":
        return {"state": "none", "short": "No SD card", "long": "No SD card, so nothing is recorded. Live view still works."}
    if sd["state"] == "problem":
        return {"state": "problem", "short": "SD card problem", "long": f"SD card status: {sd.get('status') or 'unknown'}"}
    size = re.sub(r"(\d)([KMGT]?B)$", r"\1 \2", sd.get("total") or "?")    # "111.3GB" -> "111.3 GB"
    oldest = sd.get("oldest") or 0
    if not oldest:
        return {"state": "card", "short": "SD card, no footage yet", "long": f"{size} SD card, no footage yet"}
    secs = max(0, time.time() - oldest)
    span = f"{int(secs // 86400)} day{'s' if secs >= 2 * 86400 else ''}" if secs >= 86400 else f"{max(1, int(secs // 3600))} h"
    since = datetime.fromtimestamp(oldest, TZ).strftime("%b %-d")
    return {"state": "card", "short": f"SD {span} back", "span": span, "size": size, "since": since,
            "long": f"{size} SD card, footage back to {since} ({span})"
                    + (", oldest footage is overwritten as it fills" if sd.get("loop") else "")}


tpl.env.filters["sd"] = sd_info


# ------------------------------------------------------------------ auth
def current_person(request: Request):
    tok = request.cookies.get("s")
    if not tok:
        return None
    s = db.one("SELECT p.*, s.token FROM sessions s JOIN people p ON p.id=s.person_id WHERE s.token=?", tok)
    if s and time.time() - (s["last_seen"] or 0) > 300:
        db.run("UPDATE sessions SET last_seen=? WHERE token=?", time.time(), tok)
        db.run("UPDATE people SET last_seen=? WHERE id=?", time.time(), s["id"])
    return s


def require(request: Request, admin=False):
    p = current_person(request)
    if not p:
        raise HTTPException(status_code=303, headers={"Location": "/login?next=" + request.url.path})
    if admin and not p["is_admin"]:
        raise HTTPException(status_code=403, detail="Admins only")
    return p


def render(request, name, **ctx):
    ctx.setdefault("me", current_person(request))
    return tpl.TemplateResponse(request, name, ctx)


@app.exception_handler(HTTPException)
async def _redirects(request, exc):
    if exc.status_code == 303:
        return RedirectResponse(exc.headers["Location"], status_code=303)
    if exc.status_code in (401, 403, 404) and "text/html" in request.headers.get("accept", ""):
        return render(request, "error.html", code=exc.status_code, detail=exc.detail)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


def _find_person(ident):
    """Look a person up by mobile number or email. Returns (person, channel, address)."""
    ph = sms.normalize_phone(ident)
    if ph:
        p = db.one("SELECT * FROM people WHERE phone=?", ph)
        admin_phone = sms.normalize_phone(os.environ.get("ADMIN_PHONE", ""))
        if not p and admin_phone and ph == admin_phone:
            db.run("INSERT INTO people(name,phone,is_admin,created) VALUES(?,?,1,?)",
                   os.environ.get("ADMIN_NAME", "Admin"), ph, time.time())
            p = db.one("SELECT * FROM people WHERE phone=?", ph)
        return p, "sms", ph
    em = sms.normalize_email(ident)
    if em:
        return db.one("SELECT * FROM people WHERE email=?", em), "email", em
    return None, None, None


def _locked(ident):
    r = db.one("SELECT * FROM login_fails WHERE ident=?", ident)
    return bool(r and r["until"] and r["until"] > time.time())


def _fail(ident):
    r = db.one("SELECT * FROM login_fails WHERE ident=?", ident)
    n = (r["count"] if r else 0) + 1
    until = time.time() + 900 if n >= 8 else None
    db.run("INSERT INTO login_fails(ident,count,until) VALUES(?,?,?) ON CONFLICT(ident) DO UPDATE SET count=excluded.count, until=excluded.until", ident, 0 if until else n, until)


def _start_session(request, person, next):
    db.run("DELETE FROM login_fails WHERE ident IN (?,?)", person["phone"], person["email"] or "")
    tok = secrets.token_urlsafe(32)
    db.run("INSERT INTO sessions(token,person_id,created,last_seen,ua) VALUES(?,?,?,?,?)",
           tok, person["id"], time.time(), time.time(), request.headers.get("user-agent", "")[:200])
    resp = RedirectResponse(next if next.startswith("/") else "/", status_code=303)
    resp.set_cookie("s", tok, max_age=365 * 86400, httponly=True, secure=SECURE_COOKIE, samesite="lax")
    return resp


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/", err: str = ""):
    if current_person(request):
        return RedirectResponse(next or "/")
    return render(request, "login.html", next=next, err=err, ident="")


@app.post("/login", response_class=HTMLResponse)
def login_send(request: Request, ident: str = Form(...), next: str = Form("/"), passcode: str = Form("")):
    person, channel, addr = _find_person(ident)
    if not channel:
        return render(request, "login.html", next=next, err="Enter a mobile number or an email address.", ident=ident)
    if not person:
        return render(request, "login.html", next=next, ident=ident,
                      err="That number or email isn't set up for camera access yet. Ask Andrew to add you.")
    if _locked(addr):
        return render(request, "login.html", next=next, ident=ident, err="Too many attempts. Try again in 15 minutes.")
    if passcode:
        if sms.check_passcode(passcode, person["passcode_hash"]):
            return _start_session(request, person, next)
        _fail(addr)
        return render(request, "login.html", next=next, ident=ident, err="Wrong passcode.")
    recent = db.one("SELECT COUNT(*) n FROM otps WHERE phone=? AND created>?", addr, time.time() - 600)["n"]
    if recent >= 4:
        return render(request, "login.html", next=next, ident=ident, err="Too many codes sent. Try again in 10 minutes.")
    code = f"{random.SystemRandom().randrange(0, 1000000):06d}"
    db.run("DELETE FROM otps WHERE phone=? OR expires<?", addr, time.time())
    db.run("INSERT INTO otps(phone,code,expires,created) VALUES(?,?,?,?)", addr, code, time.time() + 600, time.time())
    msg = f"Your camera login code is {code}. It expires in 10 minutes."
    if channel == "sms":
        sms.send(addr, msg, kind="otp", person_id=person["id"])
    else:
        sms.send_email(addr, f"Camera login code {code}", msg, kind="otp", person_id=person["id"])
    return render(request, "code.html", next=next, ident=addr, channel=channel, err="",
                  has_passcode=bool(person["passcode_hash"]))


@app.post("/login/code", response_class=HTMLResponse)
def login_code(request: Request, ident: str = Form(...), code: str = Form(""), passcode: str = Form(""), next: str = Form("/")):
    person, channel, addr = _find_person(ident)
    if not person:
        return RedirectResponse("/login", status_code=303)
    ctx = dict(next=next, ident=addr, channel=channel, has_passcode=bool(person["passcode_hash"]))
    if _locked(addr):
        return render(request, "code.html", err="Too many attempts. Try again in 15 minutes.", **ctx)
    if passcode:
        if sms.check_passcode(passcode, person["passcode_hash"]):
            db.run("DELETE FROM otps WHERE phone=?", addr)
            return _start_session(request, person, next)
        _fail(addr)
        return render(request, "code.html", err="Wrong passcode.", **ctx)
    row = db.one("SELECT rowid, * FROM otps WHERE phone=? ORDER BY created DESC LIMIT 1", addr)
    code = "".join(ch for ch in code if ch.isdigit())
    if not row or row["expires"] < time.time() or row["attempts"] >= 5 or row["code"] != code:
        if row:
            db.run("UPDATE otps SET attempts=attempts+1 WHERE rowid=?", row["rowid"])
        _fail(addr)
        return render(request, "code.html", err="Wrong or expired code.", **ctx)
    db.run("DELETE FROM otps WHERE phone=?", addr)
    return _start_session(request, person, next)


ALERT_LABELS = {"doorbell": "Doorbell pressed", "tamper": "Camera tampered with",
                "offline": "Camera offline / back online", "motion": "Motion / person"}
ALERT_HELP = {"doorbell": "Someone presses a doorbell at this property.",
              "tamper": "A camera is covered, knocked or moved.",
              "offline": f"A camera has been offline for {OFFLINE_ALERT_SECS // 60} minutes, and again when it's back.",
              "motion": "Every motion or person detection. Noisy, best left off."}


def _my_alert_groups(me):
    """Groups this person is in that send at least one kind of text, with what they get."""
    out = []
    for g in db.q("SELECT g.*, h.name AS house_name, gm.muted FROM groups g JOIN houses h ON h.id=g.house_id "
                  "JOIN group_members gm ON gm.group_id=g.id AND gm.person_id=? ORDER BY h.sort, h.name, g.name", me["id"]):
        muted = db.muted_set(g["muted"])
        kinds = [{"kind": k, "label": ALERT_LABELS[k], "on": k not in muted} for k in db.ALERT_KINDS if g[f"notify_{k}"]]
        if kinds:
            out.append({"id": g["id"], "name": g["name"], "house": g["house_name"], "kinds": kinds})
    return out


@app.get("/account", response_class=HTMLResponse)
def account(request: Request, msg: str = "", err: str = ""):
    me = require(request)
    sessions = db.one("SELECT COUNT(*) n FROM sessions WHERE person_id=?", me["id"])["n"]
    groups = _my_alert_groups(me)
    # one row per kind of text, one column per group: None where that group doesn't send it
    rows = []
    for k in db.ALERT_KINDS:
        cells = [next((x for x in g["kinds"] if x["kind"] == k), None) for g in groups]
        if any(cells):
            rows.append({"kind": k, "label": ALERT_LABELS[k], "help": ALERT_HELP[k], "cells": cells})
    custom = [dict(r) | {"labels": ", ".join(ALERT_KIND_LABELS[k][0] for k in (r["kinds"] or "").split(",") if k in ALERT_KIND_LABELS) or "nothing"}
              for r in db.q("SELECT ca.camera_id, ca.kinds, c.name FROM camera_alerts ca JOIN cameras c ON c.id=ca.camera_id WHERE ca.person_id=? ORDER BY c.name", me["id"])
              if db.can_view(me, r["camera_id"])]
    return render(request, "account.html", me=me, msg=msg, err=err, sessions=sessions, alert_groups=groups, alert_rows=rows, custom_alerts=custom)


@app.post("/account/alerts")
async def account_alerts(request: Request):
    """Each person chooses which of their groups' texts they get."""
    me = require(request)
    f = await request.form()
    for g in _my_alert_groups(me):
        offered = [k["kind"] for k in g["kinds"]]
        got = set(f.getlist(f"get_{g['id']}"))
        row = db.one("SELECT muted FROM group_members WHERE group_id=? AND person_id=?", g["id"], me["id"])
        # kinds the group doesn't send right now keep their old setting
        muted = (db.muted_set(row["muted"]) - set(offered)) | {k for k in offered if k not in got}
        db.run("UPDATE group_members SET muted=? WHERE group_id=? AND person_id=?",
               ",".join(k for k in db.ALERT_KINDS if k in muted), g["id"], me["id"])
    return RedirectResponse("/account?msg=Text+alerts+saved", status_code=303)


@app.post("/account/passcode")
def account_passcode(request: Request, passcode: str = Form(""), passcode2: str = Form(""), clear: str = Form("")):
    me = require(request)
    if clear:
        db.run("UPDATE people SET passcode_hash=NULL WHERE id=?", me["id"])
        return RedirectResponse("/account?msg=Passcode+removed", status_code=303)
    if len(passcode) < 6:
        return RedirectResponse("/account?err=Use+at+least+6+characters", status_code=303)
    if passcode != passcode2:
        return RedirectResponse("/account?err=The+two+entries+don't+match", status_code=303)
    db.run("UPDATE people SET passcode_hash=? WHERE id=?", sms.hash_passcode(passcode), me["id"])
    return RedirectResponse("/account?msg=Passcode+saved", status_code=303)


@app.post("/account/signout-everywhere")
def signout_everywhere(request: Request):
    me = require(request)
    db.run("DELETE FROM sessions WHERE person_id=?", me["id"])
    r = RedirectResponse("/login", status_code=303); r.delete_cookie("s")
    return r


@app.get("/dev/login")
def dev_login(request: Request, phone: str):
    """Local development only (DEV_LOGIN=1): sign in without an SMS."""
    if os.environ.get("DEV_LOGIN") != "1" or request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(404)
    person = db.one("SELECT * FROM people WHERE phone=?", sms.normalize_phone(phone))
    if not person:
        raise HTTPException(404)
    tok = secrets.token_urlsafe(32)
    db.run("INSERT INTO sessions(token,person_id,created,last_seen,ua) VALUES(?,?,?,?,?)", tok, person["id"], time.time(), time.time(), "dev")
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("s", tok, max_age=86400, httponly=True, samesite="lax")
    return resp


def _link_key(tok):
    return "link:" + hashlib.sha256(tok.encode()).hexdigest()


@app.get("/l/{tok}", response_class=HTMLResponse)
def login_link_page(request: Request, tok: str):
    """One-time sign-in link an admin made. GET only shows a button, so link previews
    in Messages/WhatsApp don't use the link up; the POST signs in."""
    j = json.loads(db.kv_get(_link_key(tok)) or "null")
    person = db.one("SELECT * FROM people WHERE id=?", j["person_id"]) if j and j["expires"] > time.time() else None
    return render(request, "login_link.html", tok=tok, person=person)


@app.post("/l/{tok}")
def login_link_use(request: Request, tok: str):
    key = _link_key(tok)
    j = json.loads(db.kv_get(key) or "null")
    db.run("DELETE FROM kv WHERE k=?", key)
    person = db.one("SELECT * FROM people WHERE id=?", j["person_id"]) if j and j["expires"] > time.time() else None
    if not person:
        return RedirectResponse("/login?err=That+sign-in+link+has+expired+or+was+already+used", status_code=303)
    return _start_session(request, person, "/")


@app.get("/logout")
def logout(request: Request):
    tok = request.cookies.get("s")
    if tok:
        db.run("DELETE FROM sessions WHERE token=?", tok)
    r = RedirectResponse("/login", status_code=303)
    r.delete_cookie("s")
    return r


# ------------------------------------------------------------------ pages
def _house_filter(request, cams):
    """The house picked in the filter bar (?house=<id>|all, remembered in a cookie), the houses
    this person can see, and the cameras narrowed to the pick. Returns (pick, houses, cams, set_cookie)."""
    houses, seen = [], set()
    for c in cams:
        if c["house_id"] and c["house_id"] not in seen:
            seen.add(c["house_id"]); houses.append({"id": c["house_id"], "name": c["house_name"]})
    q = request.query_params.get("house")
    raw = q if q is not None else request.cookies.get("house", "all")
    pick = int(raw) if raw.isdigit() and int(raw) in seen else None
    if len(houses) < 2:
        pick = None
    if pick:
        cams = [c for c in cams if c["house_id"] == pick]
    return pick, houses, cams, q is not None


def _remember_house(resp, pick):
    resp.set_cookie("house", str(pick or "all"), max_age=365 * 86400, httponly=True, samesite="lax", secure=SECURE_COOKIE)
    return resp


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    me = require(request)
    pick, house_list, cams, setc = _house_filter(request, db.person_cameras(me))
    houses = []
    for c in cams:
        if not houses or houses[-1]["name"] != (c["house_name"] or "Unassigned"):
            houses.append({"name": c["house_name"] or "Unassigned", "cams": []})
        houses[-1]["cams"].append(c)
    private = {r["camera_id"]: r["until"] for r in db.q("SELECT camera_id, until FROM privacy WHERE ended_at IS NULL AND until>?", time.time())}
    r = render(request, "home.html", houses=houses, me=me, private=private, house_pick=pick, house_list=house_list)
    return _remember_house(r, pick) if setc else r


def _opener_for_cam(cam_id):
    return db.one("SELECT * FROM garage_openers WHERE camera_id=?", cam_id)


def _opener_view(opener, me):
    """What the camera page/wall needs about an assigned opener (admin-only control for now)."""
    if not opener or not me["is_admin"]:
        return None
    return {"id": opener["id"], "name": opener["name"], "state": garage.door_state(opener)}


@app.get("/cam/{cam_id}", response_class=HTMLResponse)
async def cam_page(request: Request, cam_id: int, date: str = "", at: str = ""):
    me = require(request)
    if not db.can_view(me, cam_id):
        raise HTTPException(404, "No such camera")
    cam = db.one("SELECT c.*, h.name AS house_name FROM cameras c LEFT JOIN houses h ON h.id=c.house_id WHERE c.id=?", cam_id)
    if not (date.isdigit() and len(date) == 8):
        date = datetime.now(TZ).strftime("%Y%m%d")
    day_start, day_end = _day_bounds(date)
    sd = None            # card status is fetched by the page after it renders (see recordings.json)
    data = {"camId": cam_id, "name": cam["name"], "date": date, "dayStart": day_start, "dayEnd": day_end, "at": int(at) if at.isdigit() else 0,
            "maxClip": MAX_CLIP_SECONDS, "maxDownload": MAX_DOWNLOAD_SECONDS, "rotate": cam["rotate"], "flipH": cam["flip_h"], "flipV": cam["flip_v"],
            "isAdmin": bool(me["is_admin"]), "tz": str(TZ), "ptz": bool(cam["ptz"]) and bool(me["is_admin"]),
            "canPause": bool(me["is_admin"]), "privacy": _privacy_info(cam_id, me),
            "opener": _opener_view(_opener_for_cam(cam_id), me)}
    return render(request, "cam.html", cam=cam, sd=sd, me=me, data=json.dumps(data))


@app.get("/wall", response_class=HTMLResponse)
def wall(request: Request):
    me = require(request)
    pick, house_list, rows, setc = _house_filter(request, db.person_cameras(me))
    openers = {o["camera_id"]: o for o in db.q("SELECT * FROM garage_openers WHERE camera_id IS NOT NULL")} if me["is_admin"] else {}
    cams = [{"id": c["id"], "name": c["name"], "house": c["house_name"], "online": c["online"], "rotate": c["rotate"], "flipH": c["flip_h"],
             "flipV": c["flip_v"], "thumbAt": int(c["thumb_at"] or 0), "private": bool(db.privacy_active(c["id"])),
             "sd": sd_info(c),
             "opener": ({"id": openers[c["id"]]["id"], "name": openers[c["id"]]["name"]} if c["id"] in openers else None)} for c in rows]
    r = render(request, "wall.html", me=me, data=json.dumps(cams), house_pick=pick, house_list=house_list)
    return _remember_house(r, pick) if setc else r


@app.get("/events", response_class=HTMLResponse)
def events_page(request: Request):
    me = require(request)
    pick, house_list, cams, setc = _house_filter(request, db.person_cameras(me))
    ids = [c["id"] for c in cams]
    rows = []
    if ids:
        rows = db.q(f"SELECT e.*, c.name AS cam_name, h.name AS house_name FROM events e JOIN cameras c ON c.id=e.camera_id "
                    f"LEFT JOIN houses h ON h.id=c.house_id WHERE e.camera_id IN ({','.join('?' * len(ids))}) "
                    f"ORDER BY e.at DESC LIMIT 200", *ids)
    r = render(request, "events.html", events=rows, me=me, house_pick=pick, house_list=house_list)
    return _remember_house(r, pick) if setc else r


ALERT_KIND_LABELS = {"doorbell": ("Doorbell pressed", ""), "person": ("People", "a person is detected"),
                     "vehicle": ("Vehicles", "a car or truck is detected"), "motion": ("Any motion", "every detection, noisy"),
                     "tamper": ("Tampering", "covered, knocked or moved"), "offline": ("Offline / back online", f"after {OFFLINE_ALERT_SECS // 60} min down")}


def _alert_kinds_for(cam):
    """Choices that make sense for this camera (doorbell only on doorbells)."""
    is_bell = (cam["model"] or "").upper().startswith("D")
    return [k for k in db.CAMERA_ALERT_KINDS if k != "doorbell" or is_bell]


def _effective_alerts(person, cam_id):
    """(kinds this person gets from this camera, whether they set them themselves)."""
    row = db.one("SELECT kinds FROM camera_alerts WHERE person_id=? AND camera_id=?", person["id"], cam_id)
    if row:
        return {k for k in (row["kinds"] or "").split(",") if k}, True
    on = {k for k in db.ALERT_KINDS if any(p["id"] == person["id"] for p in db._group_recipients(cam_id, k))}
    if "motion" in on:
        on |= {"person", "vehicle"}
    return on, False


def _alerts_target(request, me, cam_id, person_id):
    """Whose settings a request is about: your own, or (admins only) anyone who can see the camera."""
    if not person_id or int(person_id) == me["id"]:
        return me
    if not me["is_admin"]:
        raise HTTPException(403, "You can only change your own alerts.")
    p = db.one("SELECT * FROM people WHERE id=?", int(person_id))
    if not p or not db.can_view(p, cam_id):
        raise HTTPException(404, "That person can't see this camera.")
    return p


@app.get("/cam/{cam_id}/alerts.json")
def cam_alerts_get(request: Request, cam_id: int):
    me = require(request)
    if not db.can_view(me, cam_id):
        raise HTTPException(404)
    cam = db.one("SELECT * FROM cameras WHERE id=?", cam_id)
    kinds = _alert_kinds_for(cam)
    on, custom = _effective_alerts(me, cam_id)
    out = {"custom": custom, "via": me["alert_via"] or "text", "hasEmail": bool(me["email"]), "hasPhone": bool(me["phone"]),
           "kinds": [{"kind": k, "label": ALERT_KIND_LABELS[k][0], "help": ALERT_KIND_LABELS[k][1], "on": k in on} for k in kinds]}
    if me["is_admin"]:      # who gets what from this camera
        who = []
        for p in db.q("SELECT * FROM people ORDER BY name"):
            if not db.can_view(p, cam_id):
                continue
            pon, pc = _effective_alerts(p, cam_id)
            who.append({"id": p["id"], "name": p["name"], "me": p["id"] == me["id"], "custom": pc, "via": p["alert_via"] or "text",
                        "on": [k for k in kinds if k in pon]})
        out["who"] = who
    return out


@app.post("/cam/{cam_id}/alerts")
async def cam_alerts_set(request: Request, cam_id: int):
    me = require(request)
    if not db.can_view(me, cam_id):
        raise HTTPException(404)
    form = await request.form()
    who = _alerts_target(request, me, cam_id, form.get("person_id"))
    if form.get("reset"):
        db.run("DELETE FROM camera_alerts WHERE person_id=? AND camera_id=?", who["id"], cam_id)
    else:
        kinds = [k for k in db.CAMERA_ALERT_KINDS if k in form.getlist("kind")]
        db.run("INSERT INTO camera_alerts(person_id,camera_id,kinds) VALUES(?,?,?) ON CONFLICT(person_id,camera_id) DO UPDATE SET kinds=excluded.kinds",
               who["id"], cam_id, ",".join(kinds))
    if who["id"] != me["id"]:
        log.info("alerts for %s on cam%s set by %s", who["name"], cam_id, me["name"])
    return {"ok": True}


@app.post("/account/alert-via")
async def account_alert_via(request: Request, via: str = Form("text")):
    me = require(request)
    if via not in ("text", "email", "both"):
        raise HTTPException(400)
    db.run("UPDATE people SET alert_via=? WHERE id=?", via, me["id"])
    return RedirectResponse("/account#alerts", status_code=303)


@app.get("/cam/{cam_id}/settings.json")
async def cam_settings_get(request: Request, cam_id: int):
    """Detection, spotlight, card and saved positions, read live from the camera (admins only)."""
    require(request, admin=True)
    cam = db.one("SELECT * FROM cameras WHERE id=?", cam_id)
    if not cam:
        raise HTTPException(404)
    try:
        return await asyncio.get_event_loop().run_in_executor(None, lambda: tapo.camera_settings(cloud, cam["device_id"], ptz=bool(cam["ptz"])))
    except Exception as e:
        log.info("settings %s: %s", cam["name"], e)
        raise HTTPException(502, "The camera didn't answer. Try again.")


CAM_ACTIONS = {"detect", "reboot", "format", "light", "siren", "preset_go", "preset_save", "preset_delete"}


@app.post("/cam/{cam_id}/action")
async def cam_action(request: Request, cam_id: int, action: str = Form(...), kind: str = Form(""), on: int = Form(0),
                     sens: str = Form(""), id: str = Form(""), name: str = Form("")):
    me = require(request, admin=True)
    cam = db.one("SELECT * FROM cameras WHERE id=?", cam_id)
    if not cam:
        raise HTTPException(404)
    if action not in CAM_ACTIONS or (action == "detect" and kind not in tapo.DETECTORS):
        raise HTTPException(400, "Unknown action")
    if action.startswith("preset") and not cam["ptz"]:
        raise HTTPException(400, "This camera can't pan or tilt.")
    kw = {"kind": kind, "on": bool(on), "sens": max(1, min(100, int(sens))) if sens.isdigit() else None, "id": id, "name": name.strip()[:30]}
    if action == "preset_save" and not kw["name"]:
        raise HTTPException(400, "Give the position a name.")
    lock = _ptz_locks.setdefault(cam_id, threading.Lock())

    def go():
        with lock:
            return tapo.camera_action(cloud, cam["device_id"], action, **kw)
    try:
        await asyncio.get_event_loop().run_in_executor(None, go)
    except Exception as e:
        log.warning("action %s on %s: %s", action, cam["name"], e)
        return JSONResponse({"ok": False, "error": f"The camera didn't do that ({e})."}, status_code=502)
    log.info("action %s on %s by %s: %s", action, cam["name"], me["name"], {k: v for k, v in kw.items() if v})
    if action == "format":
        db.run("UPDATE cameras SET sd_at=0 WHERE id=?", cam_id)       # re-check the card on the next health pass
    return {"ok": True}


@app.get("/cam/{cam_id}/image.json")
async def cam_image_get(request: Request, cam_id: int):
    """Current day/night setting, read live from the camera (admins only)."""
    require(request, admin=True)
    cam = db.one("SELECT * FROM cameras WHERE id=?", cam_id)
    if not cam:
        raise HTTPException(404)
    try:
        return await asyncio.get_event_loop().run_in_executor(None, tapo.image_mode, cloud, cam["device_id"])
    except Exception as e:
        log.info("image mode %s: %s", cam["name"], e)
        raise HTTPException(502, "The camera didn't answer. Try again.")


@app.post("/cam/{cam_id}/image")
async def cam_image_set(request: Request, cam_id: int, mode: str = Form(""), night: str = Form("")):
    me = require(request, admin=True)
    cam = db.one("SELECT * FROM cameras WHERE id=?", cam_id)
    if not cam:
        raise HTTPException(404)
    if mode and mode not in ("auto", "on", "off"):
        raise HTTPException(400, "Unknown mode")
    if night and night not in tapo.NIGHT_VISION_MODES:
        raise HTTPException(400, "Unknown night vision mode")
    try:
        await asyncio.get_event_loop().run_in_executor(None, lambda: tapo.set_image_mode(cloud, cam["device_id"], mode or None, night or None))
    except Exception as e:
        log.warning("set image mode %s: %s", cam["name"], e)
        return JSONResponse({"ok": False, "error": "The camera didn't take the change. Try again."}, status_code=502)
    log.info("image mode %s: mode=%s night=%s by %s", cam["name"], mode, night, me["name"])
    return {"ok": True}


@app.post("/cam/{cam_id}/orientation")
def cam_orientation(request: Request, cam_id: int, rotate: int = Form(0), flip_h: int = Form(0), flip_v: int = Form(0)):
    require(request, admin=True)
    db.run("UPDATE cameras SET rotate=?, flip_h=?, flip_v=? WHERE id=?", rotate % 360, 1 if flip_h else 0, 1 if flip_v else 0, cam_id)
    return JSONResponse({"ok": True})


_ptz_locks = {}


@app.post("/cam/{cam_id}/ptz")
async def cam_ptz(request: Request, cam_id: int, x: int = Form(0), y: int = Form(0)):
    """Pan/tilt by a few degrees. Admins only: everyone else can watch but not move it."""
    require(request, admin=True)
    cam = db.one("SELECT * FROM cameras WHERE id=?", cam_id)
    if not cam:
        raise HTTPException(404, "No such camera")
    if not cam["ptz"]:
        return JSONResponse({"ok": False, "error": "This camera cannot pan or tilt."}, status_code=400)
    x, y = max(-45, min(45, x)), max(-45, min(45, y))
    if not x and not y:
        return JSONResponse({"ok": True})
    lock = _ptz_locks.setdefault(cam_id, threading.Lock())

    def go():
        with lock:                      # one motor command per camera at a time
            tapo.ptz_move(cloud, cam["device_id"], x, y)
    try:
        await asyncio.get_event_loop().run_in_executor(None, go)
    except Exception as e:
        log.warning("ptz %s: %s", cam["name"], e)
        return JSONResponse({"ok": False, "error": "The camera did not move. Try again."}, status_code=502)
    return JSONResponse({"ok": True})


@app.post("/cam/{cam_id}/calibrate")
async def cam_calibrate(request: Request, cam_id: int):
    """Re-home the pan/tilt motor (admins only)."""
    me = require(request, admin=True)
    cam = db.one("SELECT * FROM cameras WHERE id=?", cam_id)
    if not cam:
        raise HTTPException(404, "No such camera")
    if not cam["ptz"]:
        return JSONResponse({"ok": False, "error": "This camera cannot pan or tilt."}, status_code=400)
    lock = _ptz_locks.setdefault(cam_id, threading.Lock())

    def go():
        with lock:
            tapo.ptz_calibrate(cloud, cam["device_id"])
    try:
        await asyncio.get_event_loop().run_in_executor(None, go)
    except Exception as e:
        log.warning("calibrate %s: %s", cam["name"], e)
        return JSONResponse({"ok": False, "error": "The camera did not start calibrating. Try again."}, status_code=502)
    log.info("calibrate %s by %s", cam["name"], me["name"])
    return JSONResponse({"ok": True})


@app.post("/garage/{opener_id}/press")
async def garage_press(request: Request, opener_id: int):
    """One momentary press (open/close). Admin-only, like pan/tilt and privacy."""
    me = require(request, admin=True)
    op = db.one("SELECT * FROM garage_openers WHERE id=?", opener_id)
    if not op:
        raise HTTPException(404)
    try:
        await asyncio.get_event_loop().run_in_executor(None, garage.press, op)
    except Exception as e:
        log.warning("garage press %s: %s", op["name"], e)
        return JSONResponse({"ok": False, "error": str(e)[:160]}, status_code=502)
    cam = db.one("SELECT * FROM cameras WHERE id=?", op["camera_id"]) if op["camera_id"] else None
    if cam:
        record_event(cam, "garage", f"{me['name']} pressed {op['name']}", notify=False)
    return {"ok": True}


@app.post("/admin/garage")
def admin_garage(request: Request, id: int = Form(0), name: str = Form(""), house_id: int = Form(0),
                 camera_id: int = Form(0), host: str = Form(""), auth: str = Form(""), relay: int = Form(0),
                 pulse: float = Form(0.5), state_input: str = Form(""), state_invert: int = Form(0),
                 delete: str = Form("")):
    require(request, admin=True)
    if id and delete:
        db.run("DELETE FROM garage_openers WHERE id=?", id)
        return RedirectResponse("/admin#garage", status_code=303)
    si = int(state_input) if state_input.strip().isdigit() else None
    vals = (name.strip(), house_id or None, camera_id or None, host.strip(), auth.strip(),
            relay, max(0.1, min(5.0, pulse)), si, state_invert)
    if id:
        db.run("UPDATE garage_openers SET name=?, house_id=?, camera_id=?, host=?, auth=?, relay=?, pulse=?, state_input=?, state_invert=? WHERE id=?",
               *vals, id)
    else:
        db.run("INSERT INTO garage_openers(name,house_id,camera_id,host,auth,relay,pulse,state_input,state_invert,backend,created) VALUES(?,?,?,?,?,?,?,?,?, 'shelly', ?)",
               *vals, time.time())
    return RedirectResponse("/admin#garage", status_code=303)


@app.post("/admin/garage/{opener_id}/test")
async def admin_garage_test(request: Request, opener_id: int):
    """Admin 'Test connection' — checks the relay answers, without pressing."""
    require(request, admin=True)
    op = db.one("SELECT * FROM garage_openers WHERE id=?", opener_id)
    if not op:
        raise HTTPException(404)
    ok, err = await asyncio.get_event_loop().run_in_executor(None, garage.reachable, op)
    return {"ok": ok, "error": err}


# ------------------------------------------------------------------ privacy mode
PRIVACY_MAX_HOURS = 24
_live_ws = {}          # cam_id -> set of open live websockets, closed when privacy starts


def _fmt_hours(h):
    return f"{h:g} hour" + ("" if h == 1 else "s")


@app.post("/cam/{cam_id}/privacy")
async def cam_privacy(request: Request, cam_id: int, hours: float = Form(0), resume: str = Form("")):
    """Pause a camera for 1-24 hours, or resume it early. Admins only. The camera keeps
    recording to its card; the site hides live view and that stretch of footage from everyone."""
    me = require(request, admin=True)
    cam = db.one("SELECT * FROM cameras WHERE id=?", cam_id)
    if not cam:
        raise HTTPException(404, "No such camera")
    cur = db.privacy_active(cam_id)
    if resume:
        if not cur:
            return JSONResponse({"ok": True})
        db.run("UPDATE privacy SET ended_at=?, ended_by=? WHERE id=?", time.time(), me["id"], cur["id"])
        record_event(cam, "privacy_off", f"Resumed early by {me['name']}")
        return JSONResponse({"ok": True})
    if not 1 <= hours <= PRIVACY_MAX_HOURS:
        return JSONResponse({"ok": False, "error": f"Pick between 1 and {PRIVACY_MAX_HOURS} hours."}, status_code=400)
    now = time.time()
    if cur:                             # already paused: extend or shorten from now
        db.run("UPDATE privacy SET ended_at=?, ended_by=? WHERE id=?", now, me["id"], cur["id"])
    db.run("INSERT INTO privacy(camera_id,start,until,by_person) VALUES(?,?,?,?)", cam_id, now, now + hours * 3600, me["id"])
    until = datetime.fromtimestamp(now + hours * 3600, TZ).strftime("%-I:%M %p").lower()
    record_event(cam, "privacy_on", f"{me['name']} turned on privacy for {_fmt_hours(hours)} (until {until})")
    for ws in list(_live_ws.get(cam_id, ())):
        try: await ws.close(code=4423)
        except Exception: pass
    return JSONResponse({"ok": True})


def _privacy_info(cam_id, me):
    cur = db.privacy_active(cam_id)
    if not cur:
        return None
    return {"until": int(cur["until"]), "by": cur["by_name"] or "someone",
            "canResume": bool(me["is_admin"])}


def privacy_expire_once():
    """Windows that ran out: close them and log the automatic resume."""
    for pr in db.q("SELECT * FROM privacy WHERE ended_at IS NULL AND until<=?", time.time()):
        db.run("UPDATE privacy SET ended_at=until WHERE id=?", pr["id"])
        cam = db.one("SELECT * FROM cameras WHERE id=?", pr["camera_id"])
        if cam:
            record_event(cam, "privacy_off", "Resumed automatically", at=pr["until"])


def privacy_loop():
    while True:
        try:
            privacy_expire_once()
        except Exception as e:
            log.warning("privacy expiry: %s", e)
        time.sleep(30)


def _minus_windows(clips, wins):
    """Cut privacy windows out of the card's clip list."""
    out = []
    for c in clips:
        pieces = [(c["start"], c["end"])]
        for ws_, we in wins:
            pieces = [p for s, e in pieces for p in ((s, min(e, ws_)), (max(s, we), e)) if p[1] > p[0]]
        out += [{**c, "start": int(s), "end": int(e)} for s, e in pieces]
    return out


# ------------------------------------------------------------------ streams
def _stream_name(cam):
    return f"cam{cam['id']}"


GO2RTC_CONFIG = os.environ.get("GO2RTC_CONFIG", os.path.join(DATA_DIR, "go2rtc.yaml"))


def _go2rtc_yaml():
    """go2rtc refuses exec sources added over its API ("insecure producer"), so the
    streams live in its config file, regenerated from the camera table."""
    lines = ["api:", '  listen: "127.0.0.1:1984"', "rtsp:", '  listen: ""', "webrtc:", '  listen: ""',
             "log:", "  level: info", "  exec: info", "streams:"]
    cams = db.q("SELECT * FROM cameras WHERE enabled=1 ORDER BY id")
    if not cams:
        lines.append("  {}")
    port = os.environ.get("PORT", "8000")
    for cam in cams:
        if STREAM_SOURCE == "exec":
            lines.append(f'  {_stream_name(cam)}: "exec:{PIPE_CMD} {cam["device_id"]}#killsignal=15#killtimeout=5"')
        elif STREAM_SOURCE == "rtsp":
            lines.append(f'  {_stream_name(cam)}: "rtsp://127.0.0.1:{RTSP_PORT}/{INTERNAL_SECRET}/{cam["id"]}"')
        else:
            lines.append(f'  {_stream_name(cam)}: "http://127.0.0.1:{port}/internal/live/{cam["id"]}#header=X-Internal:{INTERNAL_SECRET}"')
    return "\n".join(lines) + "\n"


async def register_all_streams():
    text = _go2rtc_yaml()
    try:
        cur = open(GO2RTC_CONFIG).read()
    except FileNotFoundError:
        cur = None
    if cur == text:
        return
    open(GO2RTC_CONFIG, "w").write(text)
    async with httpx.AsyncClient(timeout=5) as c:
        try:
            await c.post(f"{GO2RTC}/api/restart")
        except Exception as e:
            log.info("go2rtc restart: %s", e)
    log.info("go2rtc config rewritten with %d streams", text.count("exec:"))


_rtsp = None   # the in-process RTSP server (STREAM_SOURCE=rtsp)


def _true_h264_level(msg, cam_id):
    """go2rtc announces every H.264 stream as level 4.1 at most ("avc1.640029"). The D130
    doorbells send 2560x1920 at level 5.0; iPhone Safari sets its decoder up from the
    announced level and then can't play them (it kept reconnecting). Put the camera's
    real profile/level, read from its SPS, into the codec string."""
    lvl = _rtsp.levels.get(cam_id) if _rtsp else None
    if lvl and "avc1." in msg:
        return re.sub(r"avc1\.[0-9A-Fa-f]{6}", "avc1." + lvl, msg)
    return msg


@app.websocket("/stream/{cam_id}/ws")
async def stream_ws(ws: WebSocket, cam_id: int):
    tok = ws.cookies.get("s")
    s = db.one("SELECT p.* FROM sessions s JOIN people p ON p.id=s.person_id WHERE s.token=?", tok) if tok else None
    if not s or not db.can_view(s, cam_id):
        await ws.close(code=4403); return
    if db.privacy_active(cam_id):
        await ws.close(code=4423); return
    cam = db.one("SELECT * FROM cameras WHERE id=?", cam_id)
    await ws.accept()
    _live_ws.setdefault(cam_id, set()).add(ws)
    url = GO2RTC.replace("http", "ws", 1) + f"/api/ws?src={_stream_name(cam)}"
    try:
        async with websockets.connect(url, max_size=None) as up:
            async def c2s():
                while True:
                    m = await ws.receive()
                    if m.get("type") == "websocket.disconnect":
                        raise WebSocketDisconnect()
                    if m.get("text") is not None:
                        await up.send(m["text"])
                    elif m.get("bytes") is not None:
                        await up.send(m["bytes"])

            async def s2c():
                async for m in up:
                    if isinstance(m, (bytes, bytearray)):
                        await ws.send_bytes(m)
                    else:
                        await ws.send_text(_true_h264_level(m, cam_id))
            done, pending = await asyncio.wait([asyncio.create_task(c2s()), asyncio.create_task(s2c())],
                                               return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            # collect both tasks' outcomes (a viewer leaving ends them with disconnect errors)
            await asyncio.gather(*done, *pending, return_exceptions=True)
    except (WebSocketDisconnect, websockets.ConnectionClosed):
        pass
    except Exception as e:
        log.info("ws bridge cam%s: %s", cam_id, e)
    finally:
        _live_ws.get(cam_id, set()).discard(ws)
        try: await ws.close()
        except Exception: pass


@app.get("/stream/{cam_id}/thumb.jpg")
def thumb(request: Request, cam_id: int):
    me = require(request)
    if not db.can_view(me, cam_id):
        raise HTTPException(404)
    p = os.path.join(DATA_DIR, "thumbs", f"{cam_id}.jpg")
    if db.privacy_active(cam_id):
        return FileResponse(os.path.join(HERE, "static", "privacy.svg"), media_type="image/svg+xml", headers={"Cache-Control": "no-cache"})
    if not os.path.exists(p):
        return FileResponse(os.path.join(HERE, "static", "nothumb.svg"), media_type="image/svg+xml")
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})


@app.get("/stream/{cam_id}/frame.jpg")
async def frame(request: Request, cam_id: int):
    me = require(request)
    if not db.can_view(me, cam_id):
        raise HTTPException(404)
    if db.privacy_active(cam_id):
        raise HTTPException(423, "Privacy mode is on")
    cam = db.one("SELECT * FROM cameras WHERE id=?", cam_id)
    async with httpx.AsyncClient(timeout=40) as c:
        r = await c.get(f"{GO2RTC}/api/frame.jpeg", params={"src": _stream_name(cam)})
    if r.status_code != 200:
        raise HTTPException(502, "camera did not answer")
    _save_thumb(cam_id, r.content)
    return Response(r.content, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


def _save_thumb(cam_id, jpeg):
    p = os.path.join(DATA_DIR, "thumbs", f"{cam_id}.jpg")
    tmp = p + ".tmp"
    open(tmp, "wb").write(jpeg); os.replace(tmp, p)
    db.run("UPDATE cameras SET thumb_at=? WHERE id=?", time.time(), cam_id)


@app.get("/internal/relay/{device_id}")
def internal_relay(request: Request, device_id: str):
    if request.client.host not in ("127.0.0.1", "::1") or request.headers.get("X-Internal") != INTERNAL_SECRET:
        raise HTTPException(403)
    try:
        url, tok = mq.request_relay(device_id)
    except Exception as e:
        raise HTTPException(503, str(e))
    return {"relayUrl": url, "relayToken": tok, "term": cloud.term}


@app.get("/internal/live/{cam_id}")
async def internal_live(request: Request, cam_id: int):
    """The camera's live MPEG-TS (with AAC sound) for go2rtc, which asks for it while
    anyone is watching and hangs up when the last viewer leaves. Replaces a python
    process per camera: the relay, the TS split and the ffmpeg feed run here."""
    if request.client.host not in ("127.0.0.1", "::1") or request.headers.get("X-Internal") != INTERNAL_SECRET:
        raise HTTPException(403)
    cam = db.one("SELECT * FROM cameras WHERE id=?", cam_id)
    if not cam:
        raise HTTPException(404)
    loop = asyncio.get_event_loop()

    def open_stream():
        url, tok = mq.request_relay(cam["device_id"])
        sock = tapo.connect_relay(url, tok, cloud.term)
        tapo.start_preview(sock)
        if os.environ.get("STREAM_AUDIO", "1") != "1":
            return sock, None, tapo.iter_ts(sock)
        sp = audio.Splitter()
        pre, rest = audio.prime(tapo.iter_ts(sock), sp)
        log.info("live %s: audio %s", cam["name"], f"{sp.codec} {sp.rate} Hz" if sp.rate else "none")
        proc, afd = audio.start_ffmpeg(sp, ["-flush_packets", "1", "-muxdelay", "0", "-muxpreload", "0", "-f", "mpegts", "pipe:1"])

        def pump():
            try:
                audio.pump(rest, proc, afd, sp, pre)
            except Exception as e:
                log.info("live %s ended: %s", cam["name"], e)
        threading.Thread(target=pump, name=f"live-{cam_id}", daemon=True).start()
        return sock, proc, None

    try:
        sock, proc, raw = await loop.run_in_executor(_live_pool, open_stream)
    except Exception as e:
        log.info("live %s: %s", cam["name"], e)
        raise HTTPException(503, str(e)[:200])

    async def body():
        # async, so a hang-up runs the finally straight away (a blocked sync generator would leak the relay)
        try:
            while True:
                data = await loop.run_in_executor(_live_pool, (proc.stdout.read1 if proc else lambda n: next(raw, b"")), 65536)
                if not data:
                    break
                yield data
        finally:
            if proc:
                try: proc.kill()
                except Exception: pass
            try: sock.shutdown(socket.SHUT_RDWR)    # wakes the pump thread blocked reading the relay
            except Exception: pass
            try: sock.close()
            except Exception: pass

    return StreamingResponse(body(), media_type="video/mp2t", headers={"Cache-Control": "no-store"})


def _rtsp_open(cam_id):
    """Live TS from the camera for the RTSP server: (chunks, close)."""
    cam = db.one("SELECT * FROM cameras WHERE id=?", cam_id)
    if not cam or db.privacy_active(cam_id):
        raise RuntimeError("no such camera, or privacy mode is on")
    url, tok = mq.request_relay(cam["device_id"])
    sock = tapo.connect_relay(url, tok, cloud.term)
    tapo.start_preview(sock)
    log.info("live %s: rtsp", cam["name"])

    def close():
        try: sock.shutdown(socket.SHUT_RDWR)
        except Exception: pass
        try: sock.close()
        except Exception: pass
    return tapo.iter_ts(sock), close


# ------------------------------------------------------------------ recordings (SD card)
MAX_CLIP_SECONDS = int(os.environ.get("MAX_CLIP_SECONDS", "900"))
CLIP_CACHE_SECS = int(os.environ.get("CLIP_CACHE_SECS", str(6 * 3600)))
MAX_DOWNLOAD_SECONDS = int(os.environ.get("MAX_DOWNLOAD_SECONDS", "10800"))
# One card playback per camera at a time. A new request from the same person (scrubbing
# makes the browser drop the old request) or after 20 s without data takes over: the old
# lock-based version leaked when the browser hung up before the response body started,
# leaving the camera refusing playback (409) until a restart.
_playback = {}               # cam_id -> {"person", "ff", "at"}
_playback_guard = threading.Lock()


def playback_reaper():
    """End card playbacks that stopped sending (the browser gave up before reading
    anything): left alone they keep the relay open, and the camera then refuses every
    other playback, the Tapo app's included (error -52405)."""
    while True:
        time.sleep(10)
        with _playback_guard:
            stale = [(cid, p) for cid, p in _playback.items() if not p.get("job") and time.time() - p["at"] > 30]
        for cid, p in stale:
            log.info("ending idle playback on cam%s", cid)
            try: p["ff"] and p["ff"].kill()
            except Exception: pass
            with _playback_guard:
                if _playback.get(cid) is p:
                    del _playback[cid]


def _day_bounds(date):
    """Epoch seconds for local midnight at the start and end of YYYYMMDD."""
    d = datetime.strptime(date, "%Y%m%d").replace(tzinfo=TZ)
    return int(d.timestamp()), int((d + __import__("datetime").timedelta(days=1)).timestamp())


def _month_dates(cam, ym):
    """Days with footage in month YYYYMM (plus one day either side, for cross-midnight clips)."""
    first = datetime.strptime(ym + "01", "%Y%m%d")
    nxt = (first.replace(day=28) + __import__("datetime").timedelta(days=4)).replace(day=1)
    return tapo.recording_dates(cloud, cam["device_id"], start=first.strftime("%Y%m%d"),
                                end=(nxt - __import__("datetime").timedelta(days=1)).strftime("%Y%m%d"))


@app.get("/cam/{cam_id}/recordings")
def recordings_redirect(cam_id: int, date: str = "", at: str = ""):
    q = "&".join(x for x in [f"date={date}" if date else "", f"at={at}" if at else ""] if x)
    return RedirectResponse(f"/cam/{cam_id}" + ("?" + q if q else ""), status_code=302)


def _clips_for_day(cam, date, day_start):
    """The camera files a clip under the day it started, so include any clip from the
    day before that runs past midnight."""
    clips = tapo.recordings_of_day(cloud, cam["device_id"], date)
    prev = (datetime.strptime(date, "%Y%m%d") - __import__("datetime").timedelta(days=1)).strftime("%Y%m%d")
    try:
        for c in tapo.recordings_of_day(cloud, cam["device_id"], prev):
            if c["end"] > day_start and not any(x["start"] == c["start"] for x in clips):
                clips.append(c)
    except Exception as e:
        log.info("previous-day clips for %s: %s", cam["name"], e)
    clips.sort(key=lambda c: c["start"])
    return clips


@app.get("/cam/{cam_id}/recordings.json")
async def recordings_json(request: Request, cam_id: int, date: str = "", month: str = ""):
    me = require(request)
    if not db.can_view(me, cam_id):
        raise HTTPException(404)
    cam = db.one("SELECT * FROM cameras WHERE id=?", cam_id)
    loop = asyncio.get_event_loop()
    try:
        if month:
            return {"dates": await loop.run_in_executor(None, _month_dates, cam, month)}
        ds, de = _day_bounds(date)
        cached = db.one("SELECT * FROM clip_cache WHERE camera_id=? AND date=?", cam_id, date)
        stale = False
        # a finished day only changes when the card overwrites its oldest footage: reuse it for a while
        if cached and de < cached["at"] and time.time() - cached["at"] < CLIP_CACHE_SECS and not request.query_params.get("fresh"):
            clips, events = json.loads(cached["clips"]), json.loads(cached["events"])
        else:
            try:
                clips = await loop.run_in_executor(None, _clips_for_day, cam, date, ds)
            except tapo.NoCard:
                return {"date": date, "clips": [], "events": [], "dayStart": ds, "dayEnd": de, "privacy": [], "nocard": True}
            except Exception as e:
                if not cached:
                    raise
                # card busy (a download, someone else's playback): show what we knew rather than nothing
                log.info("recordings cam%s %s: %s; serving cached list", cam_id, date, e)
                clips, events, stale = json.loads(cached["clips"]), json.loads(cached["events"]), cached["at"]
            if not stale:
                try:
                    events = await loop.run_in_executor(None, tapo.detections_of_day, cloud, cam["device_id"], ds, de)
                except Exception as e:
                    log.info("detections cam%s: %s", cam_id, e)
                    events = json.loads(cached["events"]) if cached else []
                db.run("INSERT INTO clip_cache(camera_id,date,clips,events,at) VALUES(?,?,?,?,?) ON CONFLICT(camera_id,date) DO UPDATE SET "
                       "clips=excluded.clips, events=excluded.events, at=excluded.at", cam_id, date, json.dumps(clips), json.dumps(events), time.time())
        wins = db.privacy_windows(cam_id, ds, de)
        clips = _minus_windows(clips, wins)
        events = [e for e in events if not db.in_privacy(wins, e["start"])]
        out = {"date": date, "clips": clips, "events": events, "dayStart": ds, "dayEnd": de,
               "privacy": [[int(a), int(min(b, time.time()))] for a, b in wins], "stale": stale}
        if request.query_params.get("sd"):
            try:
                out["sd"] = await loop.run_in_executor(None, tapo.sd_status, cloud, cam["device_id"])
            except Exception as e:
                log.info("sd status cam%s: %s", cam_id, e)
        return out
    except Exception as e:
        raise HTTPException(503, str(e)[:200])


@app.get("/stream/{cam_id}/clip.mp4")
async def clip_mp4(request: Request, cam_id: int, start: int, end: int, download: int = 0, speed: float = 1):
    """SD-card recording between two epoch times, remuxed on the fly to a
    fragmented MP4 the browser can play as it arrives."""
    me = require(request)
    if not db.can_view(me, cam_id):
        raise HTTPException(404)
    if end <= start:
        raise HTTPException(400, "end must be after start")
    end = min(end, start + (MAX_DOWNLOAD_SECONDS if download else MAX_CLIP_SECONDS))
    for ws_, we in db.privacy_windows(cam_id, start, end):     # privacy footage never leaves the server
        if ws_ <= start:
            raise HTTPException(403, "That time was in privacy mode.")
        end = min(end, int(ws_))
    cam = db.one("SELECT * FROM cameras WHERE id=?", cam_id)
    mine = {"person": me["id"], "ff": None, "at": time.time(), "done": threading.Event()}
    with _playback_guard:
        cur = _playback.get(cam_id)
        if cur and cur.get("job"):
            j = cur["job"]
            raise HTTPException(409, f"This camera is busy saving a download ({j['pct']}% done). Playback works again when it finishes.")
        if cur and cur["person"] != me["id"] and time.time() - cur["at"] < 20:
            raise HTTPException(409, "Someone is already playing back from this camera. Try again in a moment.")
        _playback[cam_id] = mine
    if cur:
        if cur["ff"]:
            try: cur["ff"].kill()        # stops the old pump, which sends the camera its stop
            except Exception: pass
        # the camera serves one playback at a time: wait until the old one has really ended
        await asyncio.get_event_loop().run_in_executor(None, cur["done"].wait, 8)

    def release():
        with _playback_guard:
            if _playback.get(cam_id) is mine:
                del _playback[cam_id]
    # read from the card until we know whether it has sound, then start ffmpeg: video is
    # copied, the camera's G.711 audio becomes AAC (see audio.py); fragmented MP4 so the
    # browser can play it as it arrives
    def open_rec():
        # Watching uses real-time "playback" mode, like the Tapo app: it stops the moment we
        # say so. "download" (3-4x) is for the download button and fast playback speeds only:
        # the camera keeps uploading the whole range after we hang up, and answered "busy"
        # to everything else meanwhile. -52405 = still busy: retry briefly.
        fast = "download" if download or speed > 1 else "playback"
        modes = (fast,) * 5 + ("playback",)
        for attempt, mode in enumerate(modes):
            sp = audio.Splitter()
            rec = tapo.iter_recording(mq, cam["device_id"], cloud.term, start, end, mode=mode, cloud=cloud)
            try:
                return (sp, rec) + audio.prime(rec, sp)
            except Exception as e:
                rec.close()
                if "-52405" not in str(e) or attempt == len(modes) - 1:
                    raise
                log.info("clip cam%s: camera busy (%s), retrying", cam_id, mode)
                time.sleep(2)

    try:
        sp, rec, pre, rest = await asyncio.get_event_loop().run_in_executor(None, open_rec)
    except Exception as e:
        mine["done"].set(); release(); log.info("clip start cam%s: %s", cam_id, e)
        raise HTTPException(502, "The camera did not send that footage. Try again.")
    ff, afd = audio.start_ffmpeg(sp, ["-flush_packets", "1", "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1"])
    mine["ff"] = ff

    def feed():
        try:
            audio.pump(rest, ff, afd, sp, pre)    # closes rec at the end, which stops the camera's session
        except Exception as e:
            log.info("clip feed cam%s: %s", cam_id, e)
        finally:
            mine["done"].set()

    t = threading.Thread(target=feed, daemon=True); t.start()

    async def body():
        loop = asyncio.get_event_loop()
        try:
            while True:
                data = await loop.run_in_executor(None, ff.stdout.read, 65536)
                if not data:
                    break
                mine["at"] = time.time()
                yield data
        finally:
            try: ff.kill()
            except Exception: pass
            release()

    name = f"{cam['name']}-{datetime.fromtimestamp(start, TZ).strftime('%Y%m%d-%H%M%S')}.mp4".replace(" ", "_")
    headers = {"Cache-Control": "no-store"}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{name}"'
    return StreamingResponse(body(), media_type="video/mp4", headers=headers)


# ------------------------------------------------------------------ downloads (saved on the server, then fetched)
# Streaming a long stretch straight to the browser tied the card up for the whole range
# (the camera keeps uploading in "download" mode even after you cancel), gave no progress,
# and a dropped connection meant starting over. Now the server saves the footage in short
# pieces (each one stopped properly, retried if the card is busy), and the person
# downloads a finished file that resumes. Files go as soon as they've been fetched in full
# (after a short grace) or an hour after they were ready; a job only starts if it fits in
# the disk's free space with room to spare.
DL_DIR = os.path.join(DATA_DIR, "downloads")
DL_PIECE = int(os.environ.get("DOWNLOAD_PIECE_SECONDS", "300"))
DL_KEEP = int(os.environ.get("DOWNLOAD_KEEP_SECONDS", "3600"))
DL_GRACE = 300                      # after the last byte went out, in case the browser retries
DL_RESERVE = int(os.environ.get("DOWNLOAD_RESERVE_MB", "250")) * 1_000_000
DL_BYTES_PER_SEC = 170_000           # ~19 MB per 2 min of 2K from the card, plus a margin
DL_MAX_ACTIVE = 2
_jobs = {}                           # id -> job dict
_jobs_lock = threading.Lock()


def _dl_free():
    return shutil.disk_usage(DATA_DIR).free


def _dl_reserved():
    """Bytes promised to jobs that are still saving (what they may yet write)."""
    with _jobs_lock:
        return sum(max(0, j["estimate"] - j["bytes"]) for j in _jobs.values() if j["state"] in ("queued", "saving"))


def _dl_capacity():
    """Seconds of footage that could be saved right now."""
    return max(0, int((_dl_free() - DL_RESERVE - _dl_reserved()) / DL_BYTES_PER_SEC))


def _job_view(j):
    return {k: j[k] for k in ("id", "cam_id", "cam_name", "start", "end", "state", "pct", "bytes", "error", "created", "ready_at", "gaps")} | {
        "size": j.get("size"), "expires": (j["ready_at"] + DL_KEEP) if j.get("ready_at") else None, "url": f"/downloads/{j['id']}/file" if j["state"] == "ready" else None,
        "note": j.get("note", ""), "started": j.get("started_at"),
        # sending = the browser is fetching the file right now; fetched = it has had all of it
        "sending": j.get("sending", 0) > 0, "fetched": bool(j.get("fetched_at"))}


def _job_pieces(j):
    """TS bytes for the whole range, piece by piece; each piece retried while the card is busy.
    Updates progress as it goes. Raises if a piece can't be had at all."""
    cam = db.one("SELECT * FROM cameras WHERE id=?", j["cam_id"])
    t = j["start"]
    while t < j["end"]:
        if j["cancel"].is_set():
            return
        a, b = t, min(j["end"], t + DL_PIECE)
        # only ask for stretches that have footage; jump over gaps
        clips = [c for c in j["clips"] if c["end"] > a and c["start"] < b]
        if not clips:
            t = b; j["done_secs"] += b - a; continue
        a = max(a, min(c["start"] for c in clips)); b = min(b, max(c["end"] for c in clips))
        got = False
        for attempt in range(8):
            if j["cancel"].is_set():
                return
            mode = "download" if attempt < 6 else "playback"
            sent = 0
            try:
                rec = tapo.iter_recording(mq, cam["device_id"], cloud.term, a, b, mode=mode, cloud=cloud)
                try:
                    for chunk in rec:
                        if j["cancel"].is_set():
                            return
                        sent += len(chunk)
                        yield chunk
                        j["at"] = time.time()
                finally:
                    rec.close()
                got = True
                break
            except Exception as e:
                if sent:                  # failed part-way: keep what we have, note the hole, move on
                    log.info("download %s piece %s-%s cut short: %s", j["id"], a, b, e)
                    j["gaps"] += 1; got = True; break
                log.info("download %s piece %s-%s attempt %d: %s", j["id"], a, b, attempt + 1, e)
                j["note"] = "Camera busy, retrying…"
                time.sleep(min(15, 2 + attempt * 2))
        if not got:
            raise RuntimeError("The camera kept refusing that footage.")
        j["note"] = ""
        j["done_secs"] += min(j["end"], t + DL_PIECE) - t
        t = min(j["end"], t + DL_PIECE)


def _run_job(j):
    j["state"] = "saving"; j["at"] = j["started_at"] = time.time()
    me = {"person": j["person"], "ff": None, "at": time.time(), "done": threading.Event(), "job": j}
    # take the card: wait for (and end) any playback in progress on this camera
    with _playback_guard:
        cur = _playback.get(j["cam_id"])
        _playback[j["cam_id"]] = me
    if cur and not cur.get("job"):
        try: cur["ff"] and cur["ff"].kill()
        except Exception: pass
        cur["done"].wait(8)
    path = os.path.join(DL_DIR, f"{j['id']}.mp4")
    ff = None
    try:
        total = max(1, j["end"] - j["start"])

        def progress():
            while j["state"] == "saving":
                try: j["bytes"] = os.path.getsize(path)
                except OSError: pass
                j["pct"] = min(99, int(100 * max(j["done_secs"] / total, j["bytes"] / max(1, j["estimate"]))))
                me["at"] = time.time()
                if _dl_free() < DL_RESERVE // 2:
                    j["error"] = "The server ran low on disk space, so the download was stopped."; j["cancel"].set()
                time.sleep(1)
        threading.Thread(target=progress, daemon=True).start()
        sp = audio.Splitter()
        pieces = _job_pieces(j)
        pre, rest = audio.prime(pieces, sp)
        if not pre:
            raise RuntimeError("No footage came back for that stretch.")
        ff, afd = audio.start_ffmpeg(sp, ["-movflags", "+faststart", "-f", "mp4", "-y", path])
        audio.pump(rest, ff, afd, sp, pre)
        ff.wait(timeout=600)
        if j["cancel"].is_set():
            raise RuntimeError(j["error"] or "Cancelled.")
        if ff.returncode != 0 or not os.path.exists(path):
            raise RuntimeError("Couldn't put the video file together.")
        j["size"] = j["bytes"] = os.path.getsize(path); j["pct"] = 100
        j["ready_at"] = time.time(); j["state"] = "ready"
        log.info("download %s ready: cam%s %s-%s, %d bytes, %d gaps", j["id"], j["cam_id"], j["start"], j["end"], j["size"], j["gaps"])
    except Exception as e:
        j["state"] = "cancelled" if (j["cancel"].is_set() and not j["error"]) else "failed"
        j["error"] = j["error"] or str(e)[:200]; j["ended_at"] = time.time()
        log.info("download %s %s: %s", j["id"], j["state"], e)
        try: ff and ff.kill()
        except Exception: pass
        try: os.remove(path)
        except OSError: pass
    finally:
        me["done"].set()
        with _playback_guard:
            if _playback.get(j["cam_id"]) is me:
                del _playback[j["cam_id"]]
        _dl_kick()


def _dl_kick():
    """Start queued jobs: at most DL_MAX_ACTIVE at once and one per camera."""
    with _jobs_lock:
        active = [j for j in _jobs.values() if j["state"] == "saving"]
        busy = {j["cam_id"] for j in active}
        for j in sorted(_jobs.values(), key=lambda x: x["created"]):
            if len(active) >= DL_MAX_ACTIVE:
                break
            if j["state"] == "queued" and j["cam_id"] not in busy:
                j["state"] = "saving"; active.append(j); busy.add(j["cam_id"])
                threading.Thread(target=_run_job, args=(j,), name=f"dl-{j['id']}", daemon=True).start()


def download_reaper():
    os.makedirs(DL_DIR, exist_ok=True)
    for f in os.listdir(DL_DIR):           # jobs don't survive a restart
        try: os.remove(os.path.join(DL_DIR, f))
        except OSError: pass
    while True:
        time.sleep(30)
        now_ = time.time()
        with _jobs_lock:
            items = list(_jobs.values())
        for j in items:
            gone = False
            if j["state"] == "ready" and ((j.get("fetched_at") and now_ - j["fetched_at"] > DL_GRACE) or now_ - j["ready_at"] > DL_KEEP):
                try: os.remove(os.path.join(DL_DIR, f"{j['id']}.mp4"))
                except OSError: pass
                j["state"] = "expired"; j["ended_at"] = now_
            if j["state"] in ("failed", "cancelled", "expired") and now_ - j.get("ended_at", now_) > DL_KEEP:
                gone = True
            if gone:
                with _jobs_lock:
                    _jobs.pop(j["id"], None)


@app.get("/downloads.json")
def downloads_json(request: Request):
    me = require(request)
    with _jobs_lock:
        mine = [_job_view(j) for j in _jobs.values() if j["person"] == me["id"] and j["state"] != "expired" and not j.get("dismissed")]
    mine.sort(key=lambda x: -x["created"])
    return {"jobs": mine, "capacity": _dl_capacity(), "keep": DL_KEEP}


@app.post("/cam/{cam_id}/downloads")
async def download_create(request: Request, cam_id: int, start: int = Form(...), end: int = Form(...)):
    me = require(request)
    if not db.can_view(me, cam_id):
        raise HTTPException(404)
    cam = db.one("SELECT * FROM cameras WHERE id=?", cam_id)
    start, end = int(start), int(min(end, time.time()))
    if end - start < 5:
        return JSONResponse({"ok": False, "error": "Pick at least a few seconds."}, status_code=400)
    if end - start > MAX_DOWNLOAD_SECONDS:
        return JSONResponse({"ok": False, "error": f"That's longer than the {MAX_DOWNLOAD_SECONDS // 3600} hour limit."}, status_code=400)
    wins = db.privacy_windows(cam_id, start, end)
    if wins:
        return JSONResponse({"ok": False, "error": "That stretch includes privacy-mode time, which can't be downloaded."}, status_code=400)
    with _jobs_lock:
        if sum(1 for j in _jobs.values() if j["person"] == me["id"] and j["state"] in ("queued", "saving")) >= 3:
            return JSONResponse({"ok": False, "error": "You already have 3 downloads saving. Wait for one to finish."}, status_code=429)
    # which parts of the range have footage (from the cached day lists, else ask the card)
    loop = asyncio.get_event_loop()
    clips = []
    d = datetime.fromtimestamp(start, TZ).date()
    while d <= datetime.fromtimestamp(end, TZ).date():
        key = d.strftime("%Y%m%d")
        row = db.one("SELECT clips, at FROM clip_cache WHERE camera_id=? AND date=?", cam_id, key)
        ds, de = _day_bounds(key)
        if row and (de < row["at"] or time.time() - row["at"] < 60):
            clips += json.loads(row["clips"])
        else:
            try:
                clips += await loop.run_in_executor(None, _clips_for_day, cam, key, ds)
            except tapo.NoCard:
                return JSONResponse({"ok": False, "error": "This camera has no SD card."}, status_code=400)
            except Exception:
                clips.append({"start": max(start, ds), "end": min(end, de)})     # unknown: just ask for all of it
        d += __import__("datetime").timedelta(days=1)
    clips = [c for c in clips if c["end"] > start and c["start"] < end]
    if not clips:
        return JSONResponse({"ok": False, "error": "There's no footage on the card in that stretch."}, status_code=400)
    secs = sum(min(end, c["end"]) - max(start, c["start"]) for c in clips)
    estimate = int(secs * DL_BYTES_PER_SEC)
    room = _dl_free() - DL_RESERVE - _dl_reserved()
    if estimate > room:
        mins = max(0, int(room / DL_BYTES_PER_SEC / 60))
        return JSONResponse({"ok": False, "error": f"Not enough room on the server for that much footage right now. Up to about {mins} minutes fits; pick a shorter stretch or wait for other downloads to clear.",
                             "capacity": max(0, int(room / DL_BYTES_PER_SEC))}, status_code=507)
    jid = secrets.token_urlsafe(9)
    j = {"id": jid, "cam_id": cam_id, "cam_name": cam["name"], "person": me["id"], "start": start, "end": end, "clips": clips,
         "state": "queued", "pct": 0, "bytes": 0, "estimate": estimate, "error": "", "note": "", "created": time.time(),
         "ready_at": None, "done_secs": 0, "gaps": 0, "cancel": threading.Event(), "at": time.time()}
    with _jobs_lock:
        _jobs[jid] = j
    _dl_kick()
    return {"ok": True, "job": _job_view(j)}


@app.post("/downloads/{jid}/cancel")
def download_cancel(request: Request, jid: str):
    me = require(request)
    j = _jobs.get(jid)
    if not j or j["person"] != me["id"]:
        raise HTTPException(404)
    j["dismissed"] = True                  # whatever state it is in, it leaves the person's list
    if j["state"] in ("queued", "saving"):
        j["cancel"].set()
        if j["state"] == "queued":
            j["state"] = "cancelled"; j["ended_at"] = time.time()
    elif j["state"] == "ready":
        try: os.remove(os.path.join(DL_DIR, f"{jid}.mp4"))
        except OSError: pass
        j["state"] = "expired"; j["ended_at"] = time.time()
    return {"ok": True}


@app.get("/downloads/{jid}/file")
def download_file(request: Request, jid: str):
    """The finished file, with byte ranges so an interrupted download resumes. Once the
    last byte has gone out the file is marked fetched and removed a few minutes later."""
    me = require(request)
    j = _jobs.get(jid)
    if not j or j["person"] != me["id"]:
        raise HTTPException(404)
    path = os.path.join(DL_DIR, f"{jid}.mp4")
    if j["state"] != "ready" or not os.path.exists(path):
        raise HTTPException(410, "That download has expired. Make it again from the timeline.")
    size = os.path.getsize(path)
    a, b = 0, size - 1
    rng = request.headers.get("range", "")
    m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
    status_code = 200
    if m and (m.group(1) or m.group(2)):
        if m.group(1):
            a = int(m.group(1)); b = min(size - 1, int(m.group(2))) if m.group(2) else size - 1
        else:
            a = max(0, size - int(m.group(2)))
        if a > b or a >= size:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        status_code = 206
    name = f"{j['cam_name']}-{datetime.fromtimestamp(j['start'], TZ).strftime('%Y%m%d-%H%M%S')}.mp4".replace(" ", "_")

    def body():
        j["sending"] = j.get("sending", 0) + 1
        try:
            with open(path, "rb") as f:
                f.seek(a); left = b - a + 1
                while left > 0:
                    data = f.read(min(65536, left))
                    if not data:
                        break
                    left -= len(data)
                    yield data
            if left == 0 and b == size - 1:
                j["fetched_at"] = time.time()
        finally:
            j["sending"] -= 1
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(b - a + 1), "Cache-Control": "no-store",
               "Content-Disposition": f'attachment; filename="{name}"'}
    if status_code == 206:
        headers["Content-Range"] = f"bytes {a}-{b}/{size}"
    return StreamingResponse(body(), status_code=status_code, media_type="video/mp4", headers=headers)


# ------------------------------------------------------------------ events + alerts
COOLDOWN = {"doorbell": 90, "tamper": 600, "motion": 900, "offline": 1800, "online": 1800}
_last_sms = {}
BATCH_KINDS = {"offline", "online"}
BATCH_WINDOW = int(os.environ.get("ALERT_BATCH_SECONDS", "90"))
_batch = {}            # person_id -> {kind: {house: [camera names]}}
_batch_lock = threading.Lock()
_batch_timer = None


def _flush_batch():
    """One text per person summarising everything that changed in the window, grouped by house."""
    global _batch_timer
    with _batch_lock:
        items = {pid: kinds for pid, kinds in list(_batch.items())}
        _batch.clear(); _batch_timer = None
    base = os.environ.get("BASE_URL", "").rstrip("/")
    for pid, kinds in items.items():
        p = db.one("SELECT * FROM people WHERE id=?", pid)
        if not p:
            continue
        parts = []
        for kind in ("offline", "online"):
            for house, names in kinds.get(kind, {}).items():
                names = sorted(set(names))
                what = f"offline (down {OFFLINE_ALERT_SECS // 60}+ min)" if kind == "offline" else "back online"
                if len(names) == 1:
                    parts.append(f"{names[0]} at {house} is {what}.")
                else:
                    parts.append(f"{len(names)} cameras at {house} are {what}: {', '.join(names)}.")
        if parts:
            text = " ".join(parts)
            if base and any("online" in k for k in kinds):
                text += f" {base}/"
            deliver(p, text, kind="status", subject="Camera status")


def _queue_alert(person, cam, kind):
    """Collect offline/online changes for a short window so a whole house reconnecting is one text."""
    global _batch_timer
    house = db.one("SELECT name FROM houses WHERE id=?", cam["house_id"]) if cam["house_id"] else None
    hname = house["name"] if house else "Unassigned"
    with _batch_lock:
        _batch.setdefault(person["id"], {}).setdefault(kind, {}).setdefault(hname, []).append(cam["name"])
        if _batch_timer is None:
            _batch_timer = threading.Timer(BATCH_WINDOW, _flush_batch); _batch_timer.daemon = True; _batch_timer.start()


def record_event(cam, kind, detail="", at=None, notify=True):
    if kind == "motion" and db.privacy_active(cam["id"]):
        return None
    eid = db.run("INSERT INTO events(camera_id,kind,at,detail) VALUES(?,?,?,?)", cam["id"], kind, at or time.time(), detail[:500])
    if notify:
        notify_event(cam, kind, detail)
    return eid


def notify_event(cam, kind, detail=""):
    house = db.one("SELECT name FROM houses WHERE id=?", cam["house_id"]) if cam["house_id"] else None
    where = f"{cam['name']}" + (f" at {house['name']}" if house else "")
    what = detail.split(":", 2)[2] if detail.startswith("det:") else ""
    text = {"doorbell": f"Doorbell: {where}." + (f" ({what})" if what else ""),
            "tamper": f"Tamper alert: {where} was moved, covered or tampered with.",
            "motion": f"{(what or 'motion').capitalize()}: {where}.",
            "offline": f"Camera offline: {where} stopped responding.",
            "online": f"Camera back online: {where}."}.get(kind, f"{kind}: {where}")
    base = os.environ.get("BASE_URL", "").rstrip("/")
    if base and kind != "offline":
        text += f" {base}/cam/{cam['id']}"
    labels = [l.strip() for l in what.split(",")] if what else []
    for p in db.recipients_for(cam["id"], kind, labels):
        key = (p["id"], cam["id"], kind)
        if time.time() - _last_sms.get(key, 0) < COOLDOWN.get(kind, 600):
            continue
        _last_sms[key] = time.time()
        if kind in BATCH_KINDS:
            _queue_alert(p, cam, kind)        # one text per house, sent after the window closes
        else:
            deliver(p, text, kind=kind, subject=f"{kind.capitalize()}: {where}")


def deliver(person, text, kind="misc", subject="Camera alert"):
    """Send an alert the way the person asked for: text, email or both."""
    via = (person["alert_via"] if "alert_via" in person.keys() else None) or "text"
    if via in ("text", "both") and person["phone"]:
        sms.send(person["phone"], text, kind=kind, person_id=person["id"])
    if via in ("email", "both") and person["email"]:
        sms.send_email(person["email"], subject, text, kind=kind, person_id=person["id"])
    if via == "email" and not person["email"] and person["phone"]:      # asked for email but has none: don't drop it
        sms.send(person["phone"], text, kind=kind, person_id=person["id"])


def on_mqtt_event(topic, payload):
    """Device messages that are not relay replies. Logged in full so new event
    types can be mapped; recognised ones become events."""
    try:
        db.run("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
               f"mqtt:{topic}", json.dumps(payload)[:4000] if not isinstance(payload, bytes) else repr(payload[:500]))
    except Exception:
        pass
    log.info("mqtt event %s %s", topic, (json.dumps(payload) if not isinstance(payload, bytes) else payload)[:300])
    from .events import classify   # local import: events.py may be edited without restarting the import graph
    cls = classify(topic, payload)
    if not cls:
        return
    device_id, kind, detail = cls
    cam = db.one("SELECT * FROM cameras WHERE device_id=?", device_id)
    if cam:
        record_event(cam, kind, detail)


# ------------------------------------------------------------------ detection sync (safety net for push events)
DET_INTERVAL = int(os.environ.get("DET_INTERVAL", "60"))
DET_KIND = {"ring": "doorbell", "missed doorbell ring": "doorbell", "doorbell ring answered": "doorbell", "tamper": "tamper", "anti-theft": "tamper"}


def _det_kind(labels):
    for l in labels:
        if l in DET_KIND:
            return DET_KIND[l]
    return "motion"


def sync_detections(cam, lookback=600, notify=True):
    """Pull the camera's own detection list for the last `lookback` seconds and record
    anything new. Doorbell rings arrive here within a minute even if the cloud push
    never reaches us. Returns the number of new events."""
    now = time.time()
    dets = tapo.detections_of_day(cloud, cam["device_id"], int(now - lookback), int(now) + 60)
    wins = db.privacy_windows(cam["id"], now - lookback, now + 60)
    n = 0
    for d in dets:
        if db.in_privacy(wins, d["start"]):
            continue
        tag = f"det:{d['start']}:"
        if db.one("SELECT 1 FROM events WHERE camera_id=? AND detail LIKE ?", cam["id"], tag + "%"):
            continue
        kind = _det_kind(d["labels"]); labels = ", ".join(d["labels"])
        if notify and now - d["start"] < 180:
            record_event(cam, kind, tag + labels, at=d["start"])
        else:   # old: keep the record, skip the text
            db.run("INSERT INTO events(camera_id,kind,at,detail) VALUES(?,?,?,?)", cam["id"], kind, d["start"], tag + labels)
        n += 1
    return n


def detection_loop():
    time.sleep(40)
    first = True
    while True:
        for cam in db.q("SELECT * FROM cameras WHERE enabled=1 AND online=1"):
            try:
                sync_detections(cam, lookback=86400 if first else 600, notify=not first)
            except Exception as e:
                if not any(code in str(e) for code in ("71114", "71105")):   # no card / detection off: quietly skip
                    log.info("detections %s: %s", cam["name"], e)
            time.sleep(1)
        first = False
        time.sleep(DET_INTERVAL)


# ------------------------------------------------------------------ health checks
def _snapshot_from_relay(device_id, timeout=12, resolution="VGA"):
    """One keyframe, then hang up. Pulls ~100-300 KB instead of seconds of video, which matters on
    slow uplinks where several cameras share the line."""
    url, tok = mq.request_relay(device_id, timeout=15, resolution=resolution)
    sock = tapo.connect_relay(url, tok, cloud.term)
    tapo.start_preview(sock, resolution)
    demux = rtsp.TsDemux(); params = {}; idr = None; got = 0; t0 = time.time()
    try:
        for chunk in tapo.iter_ts(sock, timeout=15):
            got += len(chunk)
            for kind, _pts, data in demux.feed(chunk):   # complete PES packets only
                if kind != "video":
                    continue
                nals = rtsp.nal_units(data)
                for n in nals:
                    if n[0] & 0x1F in (7, 8):
                        params[n[0] & 0x1F] = n
                if any(n[0] & 0x1F == 5 for n in nals):
                    idr = nals
                    break
            if (idr and 7 in params and 8 in params) or time.time() - t0 > timeout:
                break
    finally:
        sock.close()
    if not idr or 7 not in params or 8 not in params:
        raise RuntimeError(f"no keyframe ({got} bytes in {time.time() - t0:.0f}s)")
    es = b"".join(b"\x00\x00\x00\x01" + n for n in [params[7], params[8]] + [n for n in idr if n[0] & 0x1F not in (7, 8)])
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "h264", "-i", "pipe:0", "-frames:v", "1",
                        "-vf", "scale=800:-2", "-q:v", "4", "-f", "image2", "-c:v", "mjpeg", "pipe:1"],
                       input=es, capture_output=True, timeout=40)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError(f"keyframe decode failed: {r.stderr[:200]!r}")
    return r.stdout


def _ping_camera(device_id):
    """Tiny control call through the cloud. Proves the camera is up without pulling video."""
    tapo.device_call(cloud, device_id, [{"method": "getDeviceInfo", "params": {"device_info": {"name": ["basic_info"]}}}],
                     timeout=20)


def check_camera(cam):
    try:
        try:
            jpeg = _snapshot_from_relay(cam["device_id"])
        except Exception as e:
            # On a slow uplink the 12 s video grab fails now and then (relay 404 / too little video)
            # while the camera is fine. Only call it down if it doesn't answer a control call either.
            try:
                _ping_camera(cam["device_id"])
            except Exception:
                raise e
            log.info("health %s: no snapshot (%s) but camera answers; counting as online", cam["name"], e)
            jpeg = None
        if jpeg and not db.privacy_active(cam["id"]):
            _save_thumb(cam["id"], jpeg)
        was = cam["online"]
        db.run("UPDATE cameras SET online=1, last_check=?, last_error=NULL, fail_count=0, down_since=NULL, offline_alerted=0, "
               "online_since=COALESCE(CASE WHEN online=1 THEN online_since END, ?) WHERE id=?", time.time(), time.time(), cam["id"])
        if was == 0:
            # "back online" is only texted to people who were told it went offline
            record_event(cam, "online", notify=bool(cam["offline_alerted"]))
        if time.time() - (cam["sd_at"] or 0) > SD_EVERY:
            refresh_sd(cam)
        if cam["ptz"] is None:          # until a conclusive answer: find out whether it has a motor
            try:
                has = tapo.ptz_supported(cloud, cam["device_id"])
                if has is not None:
                    db.run("UPDATE cameras SET ptz=? WHERE id=?", int(has), cam["id"])
            except Exception as e:
                log.warning("ptz probe %s: %s", cam["name"], e)
    except Exception as e:
        fails = (cam["fail_count"] or 0) + 1
        now = time.time()
        down_since = cam["down_since"] or now
        log.warning("health %s: %s (fail %d)", cam["name"], e, fails)
        db.run("UPDATE cameras SET last_check=?, last_error=?, fail_count=?, down_since=? WHERE id=?", now, str(e)[:300], fails, down_since, cam["id"])
        if fails >= OFFLINE_AFTER and cam["online"] != 0:
            db.run("UPDATE cameras SET online=0, online_since=? WHERE id=?", down_since, cam["id"])
            record_event(cam, "offline", str(e)[:200], notify=False)
        if fails >= OFFLINE_AFTER and not cam["offline_alerted"] and now - down_since >= OFFLINE_ALERT_SECS:
            db.run("UPDATE cameras SET offline_alerted=1 WHERE id=?", cam["id"])
            notify_event(cam, "offline")


SD_EVERY = int(os.environ.get("SD_EVERY", "3600"))    # how often each camera's SD card is looked at


def refresh_sd(cam):
    """Ask the camera about its card (one control call) and keep a summary for the camera lists.
    record_start_time is the oldest footage still on the card."""
    try:
        try:
            info = tapo.sd_status(cloud, cam["device_id"])
        except Exception as e:
            if "71114" not in str(e):
                raise
            info = None
        status = (info or {}).get("status", "")
        if not info or status in ("offline", "") or info.get("detect_status") == "offline":
            sd = {"state": "none"}
        elif info.get("detect_status") == "failed" or info.get("rw_attr") == "r" or (status == "insufficient" and info.get("total_space") == "0B" and info.get("video_total_space") == "0B"):
            sd = {"state": "problem", "status": "unreadable" if info.get("detect_status") == "failed" else ("read-only" if info.get("rw_attr") == "r" else status)}
        elif status in ("normal", "full", "insufficient"):
            total = info.get("video_total_space") if info.get("total_space") == "0B" else info.get("total_space")
            sd = {"state": "card", "status": status, "total": total, "free": info.get("free_space"),
                  "oldest": int(info.get("record_start_time") or 0), "loop": info.get("loop_record_status") == "1"}
        else:
            sd = {"state": "problem", "status": status}
        db.run("UPDATE cameras SET sd=?, sd_at=? WHERE id=?", json.dumps(sd), time.time(), cam["id"])
        # text admins once when a card goes bad (and once when it's fine again)
        key, was = f"sdalert:{cam['id']}", db.kv_get(f"sdalert:{cam['id']}")
        if sd["state"] == "problem" and was != "problem":
            db.kv_set(key, "problem")
            for p in _admins():
                if p["phone"]:
                    sms.send(p["phone"], f"{cam['name']}: the SD card is {sd.get('status') or 'not working'}, so it isn't recording. It likely needs replacing.", kind="sdcard", person_id=p["id"])
        elif sd["state"] == "card" and was == "problem":
            db.kv_set(key, "ok")
            for p in _admins():
                if p["phone"]:
                    sms.send(p["phone"], f"{cam['name']}: the SD card is working again.", kind="sdcard", person_id=p["id"])
    except Exception as e:
        log.info("sd check %s: %s", cam["name"], e)


def _interleave_by_house(cams):
    """Round-robin across houses so cameras sharing one internet line aren't checked back to back."""
    by = {}
    for c in cams:
        by.setdefault(c["house_id"], []).append(c)
    queues = list(by.values()); out = []
    while any(queues):
        for q in queues:
            if q:
                out.append(q.pop(0))
    return out


def health_loop():
    time.sleep(20)
    while True:
        t0 = time.time()
        cams = _interleave_by_house(db.q("SELECT * FROM cameras WHERE enabled=1 ORDER BY house_id, id"))
        gap = HEALTH_INTERVAL / max(len(cams), 1)      # spread the checks over the whole interval
        for i, cam in enumerate(cams):
            check_camera(cam)
            time.sleep(max(2, t0 + (i + 1) * gap - time.time()))


@app.post("/admin/check/{cam_id}")
def admin_check(request: Request, cam_id: int):
    require(request, admin=True)
    cam = db.one("SELECT * FROM cameras WHERE id=?", cam_id)
    threading.Thread(target=check_camera, args=(cam,), daemon=True).start()
    return RedirectResponse(f"/cam/{cam_id}", status_code=303)


# ------------------------------------------------------------------ heating monitor (boiler, zone panel, ecobee, weather)
HEATING_DIR = os.path.join(HERE, "..", "heating")
HEATING_OUT = os.path.join(DATA_DIR, "heating", "stills")
HEATING_EVERY = int(os.environ.get("HEATING_EVERY", "900"))
HEATING_MODEL = os.environ.get("HEATING_MODEL", "claude-sonnet-5-5")
_heating_state = {"last": 0, "ok": None, "error": ""}


def _heating_env():
    env = dict(os.environ)
    env.update({"HEATING_OUT": HEATING_OUT, "MONITOR_OUT": HEATING_OUT, "INTERNAL_URL": "http://127.0.0.1:8000",
                "ECOBEE_TOKEN_FILE": os.path.join(DATA_DIR, "heating", "ecobee_token.enc"), "PYTHONUNBUFFERED": "1"})
    return env


def _heating_run(args, timeout=600):
    r = subprocess.run([sys.executable] + args, cwd=HEATING_DIR, env=_heating_env(), capture_output=True, text=True, timeout=timeout)
    for line in (r.stdout + r.stderr).strip().splitlines()[-12:]:
        log.info("heating %s: %s", args[0], line)
    return r.returncode == 0


def heating_cycle():
    os.makedirs(HEATING_OUT, exist_ok=True)
    tok = os.path.join(DATA_DIR, "heating", "ecobee_token.enc")
    if not os.path.exists(tok) and os.path.exists(os.path.join(HEATING_DIR, "ecobee_token.enc")):
        import shutil; shutil.copy(os.path.join(HEATING_DIR, "ecobee_token.enc"), tok)
    ok = True
    for args in (["ecobee_monitor.py", "--once"], ["weather_monitor.py", "--once"],
                 ["monitor.py", "--once", "--model", HEATING_MODEL, "--out", HEATING_OUT]):
        try:
            ok = _heating_run(args) and ok
        except Exception as e:
            ok = False; log.warning("heating %s: %s", args[0], e)
    _heating_state.update(last=time.time(), ok=ok)
    _prune_stills()
    return ok


def _alert_stills():
    """Still filenames the alert history points to: newer alerts name them, older ones are
    matched to the reading taken at the same moment."""
    keep, want = set(), set()
    try:
        for line in open(os.path.join(HEATING_OUT, "alerts.jsonl")):
            try:
                a = json.loads(line)
            except Exception:
                continue
            if a.get("image"):
                keep.add(a["image"])
            elif a.get("timestamp"):
                want.add(a["timestamp"])
        if want:
            for line in open(os.path.join(HEATING_OUT, "readings.jsonl")):
                m = re.search(r'"timestamp": "([^"]+)"', line[:200])
                if m and m.group(1) in want:
                    img = (json.loads(line).get("boiler") or {}).get("image")
                    if img:
                        keep.add(img)
    except FileNotFoundError:
        pass
    except Exception as e:
        log.info("alert stills: %s", e)
    return keep


def _prune_stills(keep_days=2, keep_disputed_days=30, keep_alert_days=90):
    """Timestamped stills pile up fast; keep two days of them (the *_latest.jpg files stay).
    Frames where the arbiter overruled the pixels (disputed_*) are kept a month for review, and the
    boiler still behind each alert is kept 90 days so the dashboard can show what the screen said."""
    now = time.time()
    alert_stills = _alert_stills()
    try:
        # which alert stills exist, so the dashboard only links pictures it can show
        have = sorted(f for f in alert_stills if os.path.exists(os.path.join(HEATING_OUT, f)))
        with open(os.path.join(HEATING_OUT, "alert_stills.json"), "w") as fh:
            json.dump(have, fh)
    except Exception as e:
        log.info("alert stills list: %s", e)
    try:
        for f in os.listdir(HEATING_OUT):
            if f.endswith(".jpg") and "_latest" not in f:
                p = os.path.join(HEATING_OUT, f)
                days = keep_alert_days if f in alert_stills else keep_disputed_days if f.startswith("disputed_") else keep_days
                if os.path.getmtime(p) < now - days * 86400:
                    os.remove(p)
    except Exception:
        pass


def heating_loop():
    time.sleep(90)
    while True:
        if os.environ.get("ANTHROPIC_API_KEY"):
            try:
                heating_cycle()
            except Exception as e:
                log.warning("heating cycle: %s", e)
        else:
            log.info("heating: ANTHROPIC_API_KEY not set, skipping")
        time.sleep(HEATING_EVERY)


HEATING_PAGES = {"": "index.html", "index.html": "index.html", "legend.html": "legend.html", "history.html": "history.html"}


HEATING_SITE = os.environ.get("HEATING_SITE", "https://180beatrice.andrewmcgrath.info/heating")


@app.get("/heating")
@app.get("/heating/")
def heating_root():
    return RedirectResponse(HEATING_SITE, status_code=302)   # the canonical page lives on the 180 Beatrice site


@app.get("/heating/{page}")
def heating_page(page: str = ""):
    """Legend and history pages, plus the data the site's dashboard reads. Public on purpose."""
    if page == "dashboard.js":       # the one copy of the dashboard script; the 180 Beatrice page loads it too
        return FileResponse(os.path.join(HEATING_DIR, "dashboard.js"), media_type="text/javascript", headers={"Cache-Control": "no-cache"})
    name = HEATING_PAGES.get(page)
    if not name:
        raise HTTPException(404)
    return FileResponse(os.path.join(HEATING_DIR, name), media_type="text/html", headers={"Cache-Control": "no-cache"})


@app.get("/heating/stills/{name}")
def heating_still(name: str):
    if "/" in name or ".." in name or not name.endswith((".jpg", ".json", ".jsonl", ".png")):
        raise HTTPException(404)
    p = os.path.join(HEATING_OUT, name)
    if not os.path.exists(p):
        raise HTTPException(404)
    return FileResponse(p, headers={"Cache-Control": "no-cache"})


@app.get("/heating/docs/{name}")
def heating_doc(name: str):
    if "/" in name or ".." in name:
        raise HTTPException(404)
    p = os.path.join(HEATING_DIR, "docs", name)
    if not os.path.exists(p):
        raise HTTPException(404)
    return FileResponse(p)


# Boiler LCD recalibration has no button or route: it re-finds the LCD by itself when the camera
# moves. To force it, touch $DATA_DIR/heating/stills/lcd_recalibrate on the volume, e.g.
#   fly ssh console -C "touch /data/heating/stills/lcd_recalibrate"
# and the next heating cycle (within HEATING_EVERY) recalibrates.


# ------------------------------------------------------------------ watchdog for the other sites
WATCH_URLS = [u.strip() for u in os.environ.get("WATCH_URLS", "https://180beatrice.andrewmcgrath.info/,https://andrewmcgrath.info/").split(",") if u.strip()]
WATCH_EVERY = int(os.environ.get("WATCH_EVERY", "300"))
_watch = {}   # url -> {"fails": n, "down": bool}


def _admins():
    return db.q("SELECT * FROM people WHERE is_admin=1")


def watch_once():
    import requests as rq
    for url in WATCH_URLS:
        st = _watch.setdefault(url, {"fails": 0, "down": False})
        try:
            r = rq.get(url, timeout=20, headers={"User-Agent": "cameras-watchdog"})
            ok = r.status_code < 500
            err = "" if ok else f"HTTP {r.status_code}"
        except Exception as e:
            ok, err = False, str(e)[:120]
        if ok:
            if st["down"]:
                st["down"] = False
                for p in _admins():
                    sms.send(p["phone"], f"Site back up: {url}", kind="watchdog", person_id=p["id"])
            st["fails"] = 0
        else:
            st["fails"] += 1
            if st["fails"] >= 2 and not st["down"]:
                st["down"] = True
                log.warning("watchdog: %s down (%s)", url, err)
                for p in _admins():
                    sms.send(p["phone"], f"Site down: {url} ({err}). Checked twice over {WATCH_EVERY * 2 // 60} min.", kind="watchdog", person_id=p["id"])
        db.kv_set(f"watch:{url}", json.dumps({"ok": ok, "err": err, "at": time.time(), "down": st["down"]}))


def watch_loop():
    time.sleep(120)
    while True:
        try:
            watch_once()
        except Exception as e:
            log.warning("watchdog: %s", e)
        time.sleep(WATCH_EVERY)


# ------------------------------------------------------------------ nightly backup
BACKUP_DIR = os.path.join(DATA_DIR, "backups")


def backup_once():
    """Consistent SQLite copy + the heating logs and state, kept locally for 14 days and
    pushed to the bucket when one is configured (Fly Tigris: BUCKET_NAME + AWS_* secrets)."""
    import sqlite3, tarfile, glob as _glob
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now(TZ).strftime("%Y%m%d")
    dbp = os.path.join(BACKUP_DIR, f"cameras-{stamp}.db")
    src = sqlite3.connect(db.DB_PATH); dst = sqlite3.connect(dbp)
    with dst: src.backup(dst)
    src.close(); dst.close()
    tarp = os.path.join(BACKUP_DIR, f"heating-{stamp}.tar.gz")
    with tarfile.open(tarp, "w:gz") as tf:
        for f in _glob.glob(os.path.join(HEATING_OUT, "*.json")) + _glob.glob(os.path.join(HEATING_OUT, "*.jsonl")) + \
                 _glob.glob(os.path.join(HEATING_OUT, "*_latest.jpg")) + [os.path.join(DATA_DIR, "heating", "ecobee_token.enc")]:
            if os.path.exists(f):
                tf.add(f, arcname=os.path.basename(f))
    for f in sorted(_glob.glob(os.path.join(BACKUP_DIR, "*")))[:-28]:
        os.remove(f)
    bucket = os.environ.get("BUCKET_NAME")
    if bucket:
        import boto3
        s3 = boto3.client("s3", endpoint_url=os.environ.get("AWS_ENDPOINT_URL_S3", "https://fly.storage.tigris.dev"))
        for f in (dbp, tarp):
            s3.upload_file(f, bucket, "backups/" + os.path.basename(f))
        log.info("backup uploaded to %s", bucket)
    db.kv_set("backup:last", json.dumps({"at": time.time(), "db": os.path.getsize(dbp), "heating": os.path.getsize(tarp), "bucket": bool(bucket)}))


def backup_loop():
    while True:
        now = datetime.now(TZ)
        nxt = now.replace(hour=3, minute=30, second=0, microsecond=0)
        if nxt <= now:
            nxt += __import__("datetime").timedelta(days=1)
        time.sleep((nxt - now).total_seconds())
        try:
            backup_once()
        except Exception as e:
            log.warning("backup: %s", e)


@app.post("/admin/backup")
def admin_backup(request: Request):
    require(request, admin=True)
    threading.Thread(target=backup_once, daemon=True).start()
    return RedirectResponse("/admin?msg=Backup+started", status_code=303)


# ------------------------------------------------------------------ admin
@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request, msg: str = ""):
    me = require(request, admin=True)
    houses = db.q("SELECT * FROM houses ORDER BY sort, name")
    cams = db.q("SELECT c.*, h.name AS house_name FROM cameras c LEFT JOIN houses h ON h.id=c.house_id ORDER BY h.sort, h.name, c.sort, c.name")
    people = db.q("SELECT * FROM people ORDER BY is_admin DESC, name")
    groups = db.q("SELECT g.*, h.name AS house_name FROM groups g JOIN houses h ON h.id=g.house_id ORDER BY h.sort, h.name, g.name")
    members, roster = {}, {}
    for r in db.q("SELECT gm.group_id, gm.muted, p.id, p.name FROM group_members gm JOIN people p ON p.id=gm.person_id ORDER BY p.name"):
        members.setdefault(r["group_id"], []).append(r["name"])
        roster.setdefault(r["group_id"], []).append({"id": r["id"], "name": r["name"], "muted": db.muted_set(r["muted"])})
    gcams, member_ids, gcam_ids = {}, {}, {}
    for r in db.q("SELECT gc.group_id, c.id, c.name FROM group_cameras gc JOIN cameras c ON c.id=gc.camera_id ORDER BY c.sort, c.name"):
        gcams.setdefault(r["group_id"], []).append(r["name"])
        gcam_ids.setdefault(r["group_id"], set()).add(r["id"])
    for gid, rows in roster.items():
        member_ids[gid] = {m["id"] for m in rows}
    recent_sms = db.q("SELECT * FROM sms_log ORDER BY at DESC LIMIT 15")
    watch = []
    for r in db.q("SELECT k, v FROM kv WHERE k LIKE 'watch:%'"):
        j = json.loads(r["v"]); j["url"] = r["k"][6:]; watch.append(j)
    backup = json.loads(db.kv_get("backup:last") or "null")
    openers = db.q("SELECT o.*, h.name AS house_name, c.name AS cam_name FROM garage_openers o "
                   "LEFT JOIN houses h ON h.id=o.house_id LEFT JOIN cameras c ON c.id=o.camera_id ORDER BY h.sort, h.name, o.name")
    try:
        g = json.load(open(os.path.join(HEATING_OUT, "lcd_geom.json")))
        lcd = {"since": g.get("since"), "how": g.get("how"),
               "pending": os.path.exists(os.path.join(HEATING_OUT, "lcd_recalibrate"))}
    except Exception:
        lcd = None
    return render(request, "admin.html", houses=houses, cams=cams, people=people, groups=groups, members=members,
                  roster=roster, alert_kinds=ALERT_LABELS, alert_help=ALERT_HELP, gcams=gcams, member_ids=member_ids, gcam_ids=gcam_ids, msg=msg, me=me, mqtt_ok=mq.connected if mq else False, recent_sms=recent_sms,
                  heating=_heating_state, heating_every=HEATING_EVERY // 60, watch=watch, backup=backup, lcd=lcd, openers=openers)


@app.post("/admin/house")
def admin_house(request: Request, name: str = Form(...), sort: int = Form(0), id: int = Form(0), delete: str = Form("")):
    require(request, admin=True)
    if id and delete:
        db.run("DELETE FROM houses WHERE id=?", id)
    elif id:
        db.run("UPDATE houses SET name=?, sort=? WHERE id=?", name.strip(), sort, id)
    else:
        slug = "".join(ch if ch.isalnum() else "-" for ch in name.strip().lower()).strip("-")
        db.run("INSERT INTO houses(name,slug,sort) VALUES(?,?,?)", name.strip(), slug, sort)
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/import")
async def admin_import(request: Request):
    require(request, admin=True)
    try:
        devs = await asyncio.get_event_loop().run_in_executor(None, cloud.device_list)
    except Exception as e:
        return RedirectResponse(f"/admin?msg=Tapo+error:+{e}", status_code=303)
    n = 0
    import base64 as b64
    for d in devs:
        if d.get("deviceType") not in ("SMART.IPCAMERA", "SMART.TAPODOORBELL"):
            continue
        try:
            alias = b64.b64decode(d.get("alias", "")).decode()
        except Exception:
            alias = d.get("alias") or d.get("deviceName") or "Camera"
        if not db.one("SELECT id FROM cameras WHERE device_id=?", d["deviceId"]):
            db.run("INSERT INTO cameras(device_id,name,model,device_type,enabled) VALUES(?,?,?,?,1)",
                   d["deviceId"], alias, d.get("deviceModel"), d.get("deviceType"))
            n += 1
    if n and mq:
        mq.reconnect(f"{n} new camera(s) imported")
    await register_all_streams()
    subscribe_device_topics()
    return RedirectResponse(f"/admin?msg=Imported+{n}+new+camera(s)", status_code=303)


@app.post("/admin/camera")
async def admin_camera(request: Request, id: int = Form(...), name: str = Form(...), house_id: int = Form(0),
                       sort: int = Form(0), rotate: int = Form(0), flip_h: int = Form(0), flip_v: int = Form(0),
                       enabled: int = Form(0), delete: str = Form("")):
    require(request, admin=True)
    if delete:
        db.run("DELETE FROM cameras WHERE id=?", id)
    else:
        db.run("UPDATE cameras SET name=?, house_id=?, sort=?, rotate=?, flip_h=?, flip_v=?, enabled=? WHERE id=?",
               name.strip(), house_id or None, sort, rotate % 360, flip_h, flip_v, enabled, id)
        await register_all_streams()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/person")
def admin_person(request: Request, name: str = Form(...), phone: str = Form(...), email: str = Form(""),
                 passcode: str = Form(""), is_admin: int = Form(0), id: int = Form(0), delete: str = Form("")):
    me = require(request, admin=True)
    ph = sms.normalize_phone(phone)
    em = sms.normalize_email(email) if email.strip() else None
    if id and delete:
        if id != me["id"]:
            db.run("DELETE FROM people WHERE id=?", id)
        return RedirectResponse("/admin", status_code=303)
    if not ph:
        return RedirectResponse("/admin?msg=Bad+phone+number", status_code=303)
    if email.strip() and not em:
        return RedirectResponse("/admin?msg=Bad+email+address", status_code=303)
    try:
        if id:
            db.run("UPDATE people SET name=?, phone=?, email=?, is_admin=? WHERE id=?", name.strip(), ph, em, is_admin, id)
        else:
            id = db.run("INSERT INTO people(name,phone,email,is_admin,created) VALUES(?,?,?,?,?)", name.strip(), ph, em, is_admin, time.time())
    except Exception:
        return RedirectResponse("/admin?msg=That+phone+number+or+email+is+already+used", status_code=303)
    if passcode.strip():
        if len(passcode.strip()) < 6:
            return RedirectResponse("/admin?msg=Passcode+must+be+at+least+6+characters", status_code=303)
        db.run("UPDATE people SET passcode_hash=? WHERE id=?", sms.hash_passcode(passcode.strip()), id)
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/person/{person_id}/link")
def admin_person_link(request: Request, person_id: int):
    """Make a one-time sign-in link (24 hours) to send to someone who's stuck."""
    require(request, admin=True)
    person = db.one("SELECT * FROM people WHERE id=?", person_id)
    if not person:
        raise HTTPException(404)
    db.run("DELETE FROM kv WHERE k LIKE 'link:%' AND json_extract(v,'$.expires')<?", time.time())
    tok = secrets.token_urlsafe(24)
    db.kv_set(_link_key(tok), json.dumps({"person_id": person_id, "expires": time.time() + 86400}))
    base = os.environ.get("BASE_URL", "").rstrip("/") or str(request.base_url).rstrip("/")
    return render(request, "login_link_made.html", person=person, url=f"{base}/l/{tok}")


@app.post("/admin/group")
async def admin_group(request: Request):
    require(request, admin=True)
    f = await request.form()
    gid = int(f.get("id") or 0)
    if gid and f.get("delete"):
        db.run("DELETE FROM groups WHERE id=?", gid)
        return RedirectResponse("/admin#groups", status_code=303)
    house_id = int(f.get("house_id") or 0)
    all_cams = 1 if f.get("all_cameras") == "1" else 0
    vals = (f.get("name", "").strip(), house_id, all_cams,
            1 if f.get("notify_doorbell") else 0, 1 if f.get("notify_tamper") else 0,
            1 if f.get("notify_offline") else 0, 1 if f.get("notify_motion") else 0)
    if gid:
        db.run("UPDATE groups SET name=?, house_id=?, all_cameras=?, notify_doorbell=?, notify_tamper=?, notify_offline=?, notify_motion=? WHERE id=?", *vals, gid)
    else:
        gid = db.run("INSERT INTO groups(name,house_id,all_cameras,notify_doorbell,notify_tamper,notify_offline,notify_motion) VALUES(?,?,?,?,?,?,?)", *vals)
    # Per-member opt-outs: the grid lists members as they were before this save (rcv_<pid> marks a row);
    # anyone without a row (just added) keeps what they had, or gets everything if new.
    before = {r["person_id"]: r["muted"] or "" for r in db.q("SELECT person_id, muted FROM group_members WHERE group_id=?", gid)}
    db.run("DELETE FROM group_members WHERE group_id=?", gid)
    for pid in f.getlist("members"):
        pid = int(pid)
        if f.get(f"rcv_{pid}"):
            got = set(f.getlist(f"get_{pid}"))
            muted = ",".join(k for k in db.ALERT_KINDS if k not in got)
        else:
            muted = before.get(pid, "")
        db.run("INSERT OR IGNORE INTO group_members(group_id,person_id,muted) VALUES(?,?,?)", gid, pid, muted)
    db.run("DELETE FROM group_cameras WHERE group_id=?", gid)
    if not all_cams:
        own = {r["id"] for r in db.q("SELECT id FROM cameras WHERE house_id=?", house_id)}
        for cid in f.getlist("cameras"):
            if int(cid) in own:
                db.run("INSERT OR IGNORE INTO group_cameras(group_id,camera_id) VALUES(?,?)", gid, int(cid))
    return RedirectResponse("/admin#groups", status_code=303)


@app.post("/admin/test-sms")
def admin_test_sms(request: Request):
    me = require(request, admin=True)
    sid, err = sms.send(me["phone"], "Test message from the camera system.", kind="test", person_id=me["id"])
    return RedirectResponse(f"/admin?msg={'Sent' if sid else 'Failed: ' + str(err)}", status_code=303)


@app.get("/admin/mqtt-log")
def admin_mqtt_log(request: Request):
    require(request, admin=True)
    rows = db.q("SELECT k, v FROM kv WHERE k LIKE 'mqtt:%' ORDER BY k")
    return JSONResponse({r["k"]: (json.loads(r["v"]) if r["v"].startswith("{") else r["v"]) for r in rows})


@app.post("/client-log")
async def client_log(request: Request):
    """Playback failures reported by browsers (see report() in video-rtc.js)."""
    if not current_person(request):
        raise HTTPException(403)
    body = (await request.body())[:2000].decode("utf-8", "replace")
    log.warning("client-log %s", body)
    return Response(status_code=204)


@app.get("/healthz")
def healthz():
    return {"ok": True, "mqtt": mq.connected if mq else False}


@app.get("/manifest.webmanifest")
def manifest():
    return JSONResponse({"name": "Cameras", "short_name": "Cameras", "start_url": "/", "display": "standalone",
                         "background_color": "#0f1115", "theme_color": "#0f1115",
                         "icons": [{"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"},
                                   {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"}]},
                        media_type="application/manifest+json")


# ------------------------------------------------------------------ startup
@app.on_event("startup")
async def startup():
    global mq
    db.conn()
    mq = tapo.MqttSession(cloud, on_event=on_mqtt_event)
    if cloud.email:
        mq.start()
        threading.Thread(target=health_loop, name="health", daemon=True).start()
        threading.Thread(target=detection_loop, name="detections", daemon=True).start()
        threading.Thread(target=heating_loop, name="heating", daemon=True).start()
    else:
        log.warning("TPLINK_EMAIL not set: streams and health checks disabled")
    threading.Thread(target=watch_loop, name="watchdog", daemon=True).start()
    threading.Thread(target=backup_loop, name="backup", daemon=True).start()
    threading.Thread(target=privacy_loop, name="privacy", daemon=True).start()
    threading.Thread(target=playback_reaper, name="playback-reaper", daemon=True).start()
    threading.Thread(target=download_reaper, name="download-reaper", daemon=True).start()
    if STREAM_SOURCE == "rtsp":
        global _rtsp
        from . import rtsp
        _rtsp = rtsp.Server(_rtsp_open, INTERNAL_SECRET, RTSP_PORT)
        _rtsp.start()
    asyncio.create_task(_register_later())


def subscribe_device_topics():
    """Device events (doorbell ring, detections, tamper) and shadow updates (online
    state) arrive on these per-device topics - found in the Tapo app's MQTT client."""
    if not mq:
        return
    for cam in db.q("SELECT device_id FROM cameras WHERE enabled=1"):
        for t in ("events/accepted", "shadow/update/accepted"):
            mq.subscribe(f"$tpiot/things/{cam['device_id']}/{t}")


async def _register_later():
    subscribe_device_topics()
    for _ in range(10):
        await asyncio.sleep(2)
        try:
            await register_all_streams(); return
        except Exception:
            pass
