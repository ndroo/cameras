"""
cameras.andrewmcgrath.info - a small, self-hosted viewer for the Tapo cameras.

People log in with their phone number (SMS code).  Admins create houses, import
cameras from the Tapo account, and put people in groups; a group belongs to a house
and grants its members some or all of that house's cameras plus the text alerts it
has turned on (doorbell, tamper, offline/online).  Live video comes through the Tapo
cloud relay into go2rtc (one exec pipe per camera) and is proxied to the browser.
"""
import asyncio, json, logging, os, random, secrets, subprocess, sys, tempfile, threading, time, uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx, websockets
from fastapi import FastAPI, Request, Form, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, sms, tapo

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

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(HERE, "..", "data"))
os.makedirs(os.path.join(DATA_DIR, "thumbs"), exist_ok=True)
GO2RTC = os.environ.get("GO2RTC_URL", "http://127.0.0.1:1984")
INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET") or secrets.token_hex(16)
os.environ["INTERNAL_SECRET"] = INTERNAL_SECRET
PIPE_CMD = os.environ.get("PIPE_CMD", "python3 -m app.pipe")
HEALTH_INTERVAL = int(os.environ.get("HEALTH_INTERVAL", "600"))
OFFLINE_AFTER = int(os.environ.get("OFFLINE_AFTER_FAILS", "2"))
TZ = ZoneInfo(os.environ.get("TZ_NAME", "America/Toronto"))
SECURE_COOKIE = os.environ.get("BASE_URL", "").startswith("https")

cloud = tapo.TapoCloud(os.environ.get("TPLINK_EMAIL", ""), os.environ.get("TPLINK_PASSWORD", ""),
                       os.environ.get("TPLINK_TERMINAL_UUID") or str(uuid.uuid4()).upper(),
                       cache_path=os.path.join(DATA_DIR, "tapo_token.json"))
mq: tapo.MqttSession = None

app = FastAPI(docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
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
tpl.env.filters["ago"] = fmt_ago
tpl.env.filters["dt"] = fmt_dt
tpl.env.filters["ymd"] = lambda ts: datetime.fromtimestamp(ts, TZ).strftime("%Y%m%d")


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


@app.get("/account", response_class=HTMLResponse)
def account(request: Request, msg: str = "", err: str = ""):
    me = require(request)
    sessions = db.one("SELECT COUNT(*) n FROM sessions WHERE person_id=?", me["id"])["n"]
    return render(request, "account.html", me=me, msg=msg, err=err, sessions=sessions)


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


@app.get("/logout")
def logout(request: Request):
    tok = request.cookies.get("s")
    if tok:
        db.run("DELETE FROM sessions WHERE token=?", tok)
    r = RedirectResponse("/login", status_code=303)
    r.delete_cookie("s")
    return r


# ------------------------------------------------------------------ pages
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    me = require(request)
    cams = db.person_cameras(me)
    houses = []
    for c in cams:
        if not houses or houses[-1]["name"] != (c["house_name"] or "Unassigned"):
            houses.append({"name": c["house_name"] or "Unassigned", "cams": []})
        houses[-1]["cams"].append(c)
    return render(request, "home.html", houses=houses, me=me)


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
            "isAdmin": bool(me["is_admin"]), "tz": str(TZ)}
    return render(request, "cam.html", cam=cam, sd=sd, me=me, data=json.dumps(data))


@app.get("/wall", response_class=HTMLResponse)
def wall(request: Request):
    me = require(request)
    cams = [{"id": c["id"], "name": c["name"], "house": c["house_name"], "online": c["online"], "rotate": c["rotate"], "flipH": c["flip_h"],
             "flipV": c["flip_v"], "thumbAt": int(c["thumb_at"] or 0)} for c in db.person_cameras(me)]
    return render(request, "wall.html", me=me, data=json.dumps(cams))


@app.get("/events", response_class=HTMLResponse)
def events_page(request: Request):
    me = require(request)
    ids = [c["id"] for c in db.person_cameras(me)]
    rows = []
    if ids:
        rows = db.q(f"SELECT e.*, c.name AS cam_name, h.name AS house_name FROM events e JOIN cameras c ON c.id=e.camera_id "
                    f"LEFT JOIN houses h ON h.id=c.house_id WHERE e.camera_id IN ({','.join('?' * len(ids))}) "
                    f"ORDER BY e.at DESC LIMIT 200", *ids)
    return render(request, "events.html", events=rows, me=me)


@app.post("/cam/{cam_id}/orientation")
def cam_orientation(request: Request, cam_id: int, rotate: int = Form(0), flip_h: int = Form(0), flip_v: int = Form(0)):
    require(request, admin=True)
    db.run("UPDATE cameras SET rotate=?, flip_h=?, flip_v=? WHERE id=?", rotate % 360, 1 if flip_h else 0, 1 if flip_v else 0, cam_id)
    return JSONResponse({"ok": True})


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
    for cam in cams:
        lines.append(f'  {_stream_name(cam)}: "exec:{PIPE_CMD} {cam["device_id"]}#killsignal=15#killtimeout=5"')
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


@app.websocket("/stream/{cam_id}/ws")
async def stream_ws(ws: WebSocket, cam_id: int):
    tok = ws.cookies.get("s")
    s = db.one("SELECT p.* FROM sessions s JOIN people p ON p.id=s.person_id WHERE s.token=?", tok) if tok else None
    if not s or not db.can_view(s, cam_id):
        await ws.close(code=4403); return
    cam = db.one("SELECT * FROM cameras WHERE id=?", cam_id)
    await ws.accept()
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
                        await ws.send_text(m)
            done, pending = await asyncio.wait([asyncio.create_task(c2s()), asyncio.create_task(s2c())],
                                               return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
    except (WebSocketDisconnect, websockets.ConnectionClosed):
        pass
    except Exception as e:
        log.info("ws bridge cam%s: %s", cam_id, e)
    finally:
        try: await ws.close()
        except Exception: pass


@app.get("/stream/{cam_id}/thumb.jpg")
def thumb(request: Request, cam_id: int):
    me = require(request)
    if not db.can_view(me, cam_id):
        raise HTTPException(404)
    p = os.path.join(DATA_DIR, "thumbs", f"{cam_id}.jpg")
    if not os.path.exists(p):
        return FileResponse(os.path.join(HERE, "static", "nothumb.svg"), media_type="image/svg+xml")
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})


@app.get("/stream/{cam_id}/frame.jpg")
async def frame(request: Request, cam_id: int):
    me = require(request)
    if not db.can_view(me, cam_id):
        raise HTTPException(404)
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


# ------------------------------------------------------------------ recordings (SD card)
MAX_CLIP_SECONDS = int(os.environ.get("MAX_CLIP_SECONDS", "900"))
MAX_DOWNLOAD_SECONDS = int(os.environ.get("MAX_DOWNLOAD_SECONDS", "10800"))
_playback_locks = {}


def _plock(cam_id):
    return _playback_locks.setdefault(cam_id, threading.Lock())


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
        clips = await loop.run_in_executor(None, _clips_for_day, cam, date, ds)
        try:
            events = await loop.run_in_executor(None, tapo.detections_of_day, cloud, cam["device_id"], ds, de)
        except Exception as e:
            log.info("detections cam%s: %s", cam_id, e); events = []
        out = {"date": date, "clips": clips, "events": events, "dayStart": ds, "dayEnd": de}
        if request.query_params.get("sd"):
            try:
                out["sd"] = await loop.run_in_executor(None, tapo.sd_status, cloud, cam["device_id"])
            except Exception as e:
                log.info("sd status cam%s: %s", cam_id, e)
        return out
    except Exception as e:
        raise HTTPException(503, str(e)[:200])


@app.get("/stream/{cam_id}/clip.mp4")
async def clip_mp4(request: Request, cam_id: int, start: int, end: int, download: int = 0):
    """SD-card recording between two epoch times, remuxed on the fly to a
    fragmented MP4 the browser can play as it arrives."""
    me = require(request)
    if not db.can_view(me, cam_id):
        raise HTTPException(404)
    if end <= start:
        raise HTTPException(400, "end must be after start")
    end = min(end, start + (MAX_DOWNLOAD_SECONDS if download else MAX_CLIP_SECONDS))
    cam = db.one("SELECT * FROM cameras WHERE id=?", cam_id)
    lock = _plock(cam_id)
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "Someone is already playing back from this camera. Try again in a moment.")
    # small probe window + flush per packet so the first fragment leaves as soon as the
    # camera sends it (ffmpeg's default 5 MB probe would hold a whole short clip back)
    ff = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-probesize", "300000", "-analyzeduration", "500000",
                           "-fflags", "nobuffer", "-i", "pipe:0", "-map", "0:v", "-c", "copy", "-flush_packets", "1",
                           "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1"],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    state = {"err": None}

    def feed():
        try:
            for chunk in tapo.iter_recording(mq, cam["device_id"], cloud.term, start, end, cloud=cloud):
                ff.stdin.write(chunk)
        except Exception as e:
            state["err"] = str(e); log.info("clip feed cam%s: %s", cam_id, e)
        finally:
            try: ff.stdin.close()
            except Exception: pass

    t = threading.Thread(target=feed, daemon=True); t.start()

    async def body():
        loop = asyncio.get_event_loop()
        try:
            while True:
                data = await loop.run_in_executor(None, ff.stdout.read, 65536)
                if not data:
                    break
                yield data
        finally:
            try: ff.kill()
            except Exception: pass
            lock.release()

    name = f"{cam['name']}-{datetime.fromtimestamp(start, TZ).strftime('%Y%m%d-%H%M%S')}.mp4".replace(" ", "_")
    headers = {"Cache-Control": "no-store"}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{name}"'
    return StreamingResponse(body(), media_type="video/mp4", headers=headers)


# ------------------------------------------------------------------ events + alerts
COOLDOWN = {"doorbell": 90, "tamper": 600, "motion": 900, "offline": 1800, "online": 1800}
_last_sms = {}


def record_event(cam, kind, detail="", at=None):
    eid = db.run("INSERT INTO events(camera_id,kind,at,detail) VALUES(?,?,?,?)", cam["id"], kind, at or time.time(), detail[:500])
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
    for p in db.recipients_for(cam["id"], kind):
        key = (p["id"], cam["id"], kind)
        if time.time() - _last_sms.get(key, 0) < COOLDOWN.get(kind, 600):
            continue
        _last_sms[key] = time.time()
        sms.send(p["phone"], text, kind=kind, person_id=p["id"])
    return eid


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
    n = 0
    for d in dets:
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
def _snapshot_from_relay(device_id):
    url, tok = mq.request_relay(device_id, timeout=15)
    sock = tapo.connect_relay(url, tok, cloud.term)
    ts = bytearray(); t0 = time.time()
    try:
        for chunk in tapo.iter_ts(sock, timeout=15):
            ts += chunk
            if len(ts) > 1_200_000 or time.time() - t0 > 12:
                break
    finally:
        sock.close()
    if len(ts) < 50_000:
        raise RuntimeError(f"too little video ({len(ts)} bytes)")
    d = tempfile.mkdtemp(prefix="cam_")
    try:
        tsp = os.path.join(d, "s.ts"); open(tsp, "wb").write(ts)
        out = os.path.join(d, "f.jpg")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", tsp, "-frames:v", "1", "-vf", "scale=800:-2",
                        "-q:v", "4", out], check=True, capture_output=True, timeout=40)
        return open(out, "rb").read()
    finally:
        import shutil; shutil.rmtree(d, ignore_errors=True)


def check_camera(cam):
    try:
        jpeg = _snapshot_from_relay(cam["device_id"])
        _save_thumb(cam["id"], jpeg)
        was = cam["online"]
        db.run("UPDATE cameras SET online=1, last_check=?, last_error=NULL, fail_count=0, "
               "online_since=COALESCE(CASE WHEN online=1 THEN online_since END, ?) WHERE id=?", time.time(), time.time(), cam["id"])
        if was == 0:
            record_event(cam, "online")
    except Exception as e:
        fails = (cam["fail_count"] or 0) + 1
        log.warning("health %s: %s (fail %d)", cam["name"], e, fails)
        db.run("UPDATE cameras SET last_check=?, last_error=?, fail_count=? WHERE id=?", time.time(), str(e)[:300], fails, cam["id"])
        if fails >= OFFLINE_AFTER and cam["online"] != 0:
            db.run("UPDATE cameras SET online=0, online_since=? WHERE id=?", time.time(), cam["id"])
            record_event(cam, "offline", str(e)[:200])


def health_loop():
    time.sleep(20)
    while True:
        for cam in db.q("SELECT * FROM cameras WHERE enabled=1"):
            check_camera(cam)
            time.sleep(2)
        time.sleep(HEALTH_INTERVAL)


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


def _prune_stills(keep_days=2):
    """Timestamped stills pile up fast; keep two days of them (the *_latest.jpg files stay)."""
    cutoff = time.time() - keep_days * 86400
    try:
        for f in os.listdir(HEATING_OUT):
            if f.endswith(".jpg") and "_latest" not in f:
                p = os.path.join(HEATING_OUT, f)
                if os.path.getmtime(p) < cutoff:
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


@app.get("/heating")
@app.get("/heating/")
@app.get("/heating/{page}")
def heating_page(page: str = ""):
    """The heating dashboard. Public on purpose: it shows mechanical equipment only."""
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


@app.post("/admin/heating/run")
def admin_heating_run(request: Request):
    require(request, admin=True)
    threading.Thread(target=heating_cycle, daemon=True).start()
    return RedirectResponse("/admin?msg=Heating+cycle+started", status_code=303)


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
    members = {}
    for r in db.q("SELECT gm.group_id, p.name FROM group_members gm JOIN people p ON p.id=gm.person_id ORDER BY p.name"):
        members.setdefault(r["group_id"], []).append(r["name"])
    gcams = {}
    for r in db.q("SELECT gc.group_id, c.name FROM group_cameras gc JOIN cameras c ON c.id=gc.camera_id ORDER BY c.name"):
        gcams.setdefault(r["group_id"], []).append(r["name"])
    recent_sms = db.q("SELECT * FROM sms_log ORDER BY at DESC LIMIT 15")
    watch = []
    for r in db.q("SELECT k, v FROM kv WHERE k LIKE 'watch:%'"):
        j = json.loads(r["v"]); j["url"] = r["k"][6:]; watch.append(j)
    backup = json.loads(db.kv_get("backup:last") or "null")
    return render(request, "admin.html", houses=houses, cams=cams, people=people, groups=groups, members=members,
                  gcams=gcams, msg=msg, me=me, mqtt_ok=mq.connected if mq else False, recent_sms=recent_sms,
                  heating=_heating_state, watch=watch, backup=backup)


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


@app.post("/admin/group")
async def admin_group(request: Request):
    require(request, admin=True)
    f = await request.form()
    gid = int(f.get("id") or 0)
    if gid and f.get("delete"):
        db.run("DELETE FROM groups WHERE id=?", gid)
        return RedirectResponse("/admin", status_code=303)
    vals = (f.get("name", "").strip(), int(f.get("house_id") or 0), 1 if f.get("all_cameras") else 0,
            1 if f.get("notify_doorbell") else 0, 1 if f.get("notify_tamper") else 0,
            1 if f.get("notify_offline") else 0, 1 if f.get("notify_motion") else 0)
    if gid:
        db.run("UPDATE groups SET name=?, house_id=?, all_cameras=?, notify_doorbell=?, notify_tamper=?, notify_offline=?, notify_motion=? WHERE id=?", *vals, gid)
    else:
        gid = db.run("INSERT INTO groups(name,house_id,all_cameras,notify_doorbell,notify_tamper,notify_offline,notify_motion) VALUES(?,?,?,?,?,?,?)", *vals)
    db.run("DELETE FROM group_members WHERE group_id=?", gid)
    for pid in f.getlist("members"):
        db.run("INSERT OR IGNORE INTO group_members(group_id,person_id) VALUES(?,?)", gid, int(pid))
    db.run("DELETE FROM group_cameras WHERE group_id=?", gid)
    for cid in f.getlist("cameras"):
        db.run("INSERT OR IGNORE INTO group_cameras(group_id,camera_id) VALUES(?,?)", gid, int(cid))
    return RedirectResponse("/admin", status_code=303)


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
