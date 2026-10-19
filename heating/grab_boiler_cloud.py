"""
Capture shim for the heating monitor inside the cameras app.

The original grab_boiler_cloud.py logged in and opened its own MQTT session. Here the
web app owns the single Tapo session, so a still is taken by asking the app for a relay
(loopback, shared secret) and reading a second of video from it.
"""
import os, sys, subprocess, tempfile, time, shutil, uuid
from datetime import datetime
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))          # make `app` importable
from app import tapo                                 # noqa: E402

CAMERAS = {
    "boiler": os.environ.get("TAPO_BOILER_DEVICE", "8021B85E0B01678EECC5649C225AAA28234B5C71"),
    "zone":   os.environ.get("TAPO_ZONE_DEVICE",   "8021B086176FF38DD640693F7636ECC123D3BA54"),
}
TERM = os.environ.get("TPLINK_TERMINAL_UUID") or str(uuid.uuid4()).upper()
DEBUG = False


def load_dotenv(path=None):
    path = path or os.path.join(os.path.dirname(HERE), ".env")
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def log(*a): print(time.strftime("%H:%M:%S"), *a, flush=True)


def _sharpness(path):
    from PIL import Image, ImageFilter, ImageStat
    edges = Image.open(path).convert("L").filter(ImageFilter.FIND_EDGES)
    return sum(v * v for v in ImageStat.Stat(edges).stddev)


def ts_to_jpeg(ts, frames=8, sharpen=False, quality=95):
    from PIL import Image, ImageFilter
    d = tempfile.mkdtemp(prefix="heat_")
    try:
        tspath = os.path.join(d, "s.ts"); open(tspath, "wb").write(ts)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", tspath, "-frames:v", str(frames), "-vsync", "0",
                        os.path.join(d, "f_%03d.png")], check=True, capture_output=True, timeout=45)
        pngs = sorted(p for p in (os.path.join(d, f) for f in os.listdir(d)) if p.endswith(".png"))
        if not pngs:
            raise RuntimeError("ffmpeg produced no frames")
        img = Image.open(max(pngs, key=_sharpness))
        if sharpen:
            img = img.filter(ImageFilter.UnsharpMask(radius=1.5, percent=90, threshold=2))
        jpg = os.path.join(d, "best.jpg"); img.save(jpg, "JPEG", quality=quality)
        return open(jpg, "rb").read()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def relay_for(device_id):
    base = os.environ.get("INTERNAL_URL", "http://127.0.0.1:8000")
    r = requests.get(f"{base}/internal/relay/{device_id}", headers={"X-Internal": os.environ.get("INTERNAL_SECRET", "")}, timeout=40)
    if r.status_code != 200:
        raise RuntimeError(f"relay request failed: {r.status_code} {r.text[:120]}")
    return r.json()


def grab_once(out, device_id, name="camera", timeout=12, opts=None):
    opts = opts or {"frames": 8, "sharpen": False, "quality": 95}
    j = relay_for(device_id)
    sock = tapo.connect_relay(j["relayUrl"], j["relayToken"], j["term"])
    ts = bytearray(); t0 = time.time()
    try:
        for chunk in tapo.iter_ts(sock, timeout=timeout):
            ts += chunk
            if len(ts) > 1_500_000 or time.time() - t0 > timeout:
                break
    finally:
        sock.close()
    if len(ts) < 50_000:
        raise RuntimeError(f"not enough video received ({len(ts)} TS bytes)")
    jpeg = ts_to_jpeg(ts, frames=opts["frames"], sharpen=opts["sharpen"], quality=opts["quality"])
    os.makedirs(out, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    p = os.path.join(out, f"{name}_{stamp}.jpg")
    open(p, "wb").write(jpeg); open(os.path.join(out, f"{name}_latest.jpg"), "wb").write(jpeg)
    log(f"saved {p} ({len(jpeg)} bytes)")
    return p
