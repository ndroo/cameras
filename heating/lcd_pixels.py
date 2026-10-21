"""
lcd_pixels.py - read the P-950EH boiler LCD from pixels, no model.

Pipeline (all numpy + Pillow):
  1. The caller passes the LCD geometry (lcd_recal.current): the glass corners in the full frame
     and the flat reference taken at that camera position. lcd_recal finds them again on its own
     when the camera is moved.
  2. The frame is perspective-warped to a flat 640x220 grayscale rectangle using those corners.
  3. The flat image is registered (+-6 px) against the reference so a small camera nudge does
     not move the icon boxes.
  4. Icons: score = share of the box's pixels at least 15% darker than the glass around the box
     in the same frame (calib "icon_method"). This replaced darkness against the stored all-off
     photo on 2026-10-20: lighting changes made that reference stale (unlit icons scored 9-22 and
     lit ones drifted 10-20% lower in three days); unlit icons now score 0.00.
     Digits: contrast = how much darker each segment is than the brightest glass around it (in %),
     decoded as slanted 7-segment cells. Each segment's cut-off is learned from its own history at
     the current camera position (learn_seg_thr: lit and unlit form two clusters); until a segment
     has shown both states, it is lit when it is at least half as dark as the darkest segment of
     its digit (and above DIGIT_MIN). This replaced darkness against a stored all-off photo on
     2026-10-21: that photo only fits one camera position, and the camera gets moved.

Calibration (2026-10-17, 60 frames): every icon separable with a wide margin; temperature
exact on 59/60. Icons that never lit in that period (sun, snowflake, DHW box, [i], wrench,
DHW pump) have PROVISIONAL boxes placed from the manual's legend drawing - treat a first
"lit" on one of those as something to verify by eye.
"""
import json, os
import numpy as np
from PIL import Image, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
CALIB = os.path.join(HERE, "docs", "lcd_calib.json")
DIG = {(1,1,1,1,1,1,0):0,(0,1,1,0,0,0,0):1,(1,1,0,1,1,0,1):2,(1,1,1,1,0,0,1):3,(0,1,1,0,0,1,1):4,(1,0,1,1,0,1,1):5,
       (1,0,1,1,1,1,1):6,(1,1,1,0,0,0,0):7,(1,1,1,0,0,1,0):7,(1,1,1,1,1,1,1):8,(1,1,1,1,0,1,1):9}

_cache = {}
def _calib():
    if "c" not in _cache:
        c = json.load(open(CALIB))
        c["_ref"] = np.asarray(Image.open(os.path.join(HERE, "docs", c["ref"])).convert("L"), dtype=np.float32)
        m = np.ones(c["_ref"].shape, bool)
        for x0, y0, x1, y1 in c["mask_exclude"]:
            m[y0:y1, x0:x1] = False
        c["_mask"] = m
        _cache["c"] = c
    return _cache["c"]

def _persp(src, dst):
    A, b = [], []
    for (x, y), (u, v) in zip(dst, src):
        A.append([x, y, 1, 0, 0, 0, -u * x, -u * y]); b.append(u)
        A.append([0, 0, 0, x, y, 1, -v * x, -v * y]); b.append(v)
    return np.linalg.solve(np.array(A, float), np.array(b, float))

def flatten(path, corners):
    """Flat 640x220 grayscale LCD for a frame, given the glass corners (TL, TR, BR, BL) in the full frame."""
    c = _calib(); W, H = c["out_size"]; p = c["pad"]
    g = Image.open(path).convert("L")
    dst = [(p, p), (W - p, p), (W - p, H - p), (p, H - p)]
    return g.transform((W, H), Image.PERSPECTIVE, tuple(_persp([tuple(q) for q in corners], dst)), Image.BICUBIC)

def register(a, ref=None, r=6):
    """(ncc, dx, dy): shift that best aligns frame `a` to the reference on the static regions."""
    c = _calib(); m = c["_mask"]; ref = c["_ref"] if ref is None else ref; y = ref[m] - ref[m].mean(); yy = float((y * y).sum())
    best = None
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            b = np.roll(np.roll(a, -dy, 0), -dx, 1); x = b[m] - b[m].mean()
            s = float((x * y).sum() / np.sqrt((x * x).sum() * yy + 1e-6))
            if best is None or s > best[0]:
                best = (s, dx, dy)
    return best

def _seg_boxes(x, y, w, h, t, sk):
    m = y + h // 2
    X = lambda yy: x + sk * (y + h - yy) / h
    segs = [(X(y)+t, y, X(y)+w-t, y+t), (X(y+t)+w-t, y+t, X(m)+w, m-t//2), (X(m)+w-t, m+t//2, X(y+h)+w, y+h-t),
            (X(y+h)+t, y+h-t, X(y+h)+w-t, y+h), (X(m), m+t//2, X(y+h)+t, y+h-t), (X(y+t), y+t, X(m)+t, m-t//2),
            (X(m)+t, m-t//2, X(m)+w-t, m+t//2)]
    return [tuple(int(round(v)) for v in s) for s in segs]

DIGIT_MIN = 20.0    # % darker than the glass: never lit below this (unlit segments measured 2-28, lit 21-63)
DIGIT_FRAC = 0.5    # no learned cut-off yet: lit if at least this share of the digit's darkest segment
                    # (replay 2026-10-21: 171/184 frames exact and none wrong from a cold start; 0.55 lost the new view)

def contrast(a, k=31):
    """Per pixel, % darker than the brightest glass within k px (the LCD background in this frame)."""
    im = Image.fromarray(a.clip(0, 255).astype("uint8")).filter(ImageFilter.GaussianBlur(1))
    bg = np.asarray(im.filter(ImageFilter.MaxFilter(k)).filter(ImageFilter.BoxBlur(k // 2)), dtype=np.float32)
    return 100 * (bg - a) / np.maximum(bg, 1)

def seg_values(con):
    """Contrast of all 21 segments, keyed "0a".."2g" (cell 0 = hundreds)."""
    out = {}
    for i, cc in enumerate(_calib()["digits"]["cells"]):
        for j, (x0, y0, x1, y1) in enumerate(_seg_boxes(cc["x"], cc["y"], cc["w"], cc["h"], cc["t"], cc["sk"])):
            out[f"{i}{'abcdefg'[j]}"] = float(con[y0:y1, x0:x1].mean())
    return out

def learn_seg_thr(hist):
    """{seg: cut-off} for the segments whose history splits cleanly into an unlit and a lit cluster
    (between-cluster distance >= 3x the clusters' spread, and a gap of >= 4 between them)."""
    out = {}
    for k, vals in (hist or {}).items():
        v = np.sort(np.asarray(vals[-300:], dtype=float)); best = None
        if len(v) < 8:
            continue
        for i in range(3, len(v) - 2):
            lo, hi = v[:i], v[i:]
            s = (hi.mean() - lo.mean()) / (lo.std() + hi.std() + 1)
            if best is None or s > best[0]:
                best = (s, (lo[-1] + hi[0]) / 2, hi[0] - lo[-1])
        if best and best[0] >= 3 and best[2] >= 4:
            out[k] = round(float(best[1]), 1)
    return out

def _cell(i, vals, seg_thr):
    v = [vals[f"{i}{s}"] for s in "abcdefg"]
    fallback = max(DIGIT_MIN, DIGIT_FRAC * max(v))
    segs = tuple(1 if x > seg_thr.get(f"{i}{s}", fallback) else 0 for x, s in zip(v, "abcdefg"))
    return ("blank" if segs == (0,) * 7 else DIG.get(segs, "?")), segs, [round(x) for x in v]

METHOD = "glass_share-v1+seg_contrast-v1"   # bump when scores change meaning: monitor resets trust and learned icons


def icon_score(a, box, m):
    """Share of the box's pixels at least m["darker"] darker than the surrounding glass."""
    x0, y0, x1, y1 = box; p = m["pad"]
    glass = float(np.percentile(a[max(0, y0 - p):y1 + p, max(0, x0 - p):x1 + p], m["glass_pct"]))
    return float((a[y0:y1, x0:x1] < (1 - m["darker"]) * glass).mean())


def read(path, geom, learned=None):
    """Full pixel read. Returns a dict; 'ok' False with 'error' if the frame can't be trusted.
    geom: {"corners": glass corners in the frame, "ref": flat reference (array, None = docs),
    "seg_thr": learned digit cut-offs}. learned: {icon_name: {"thr": float}} promotions confirmed
    in production (overrides the provisional calibration for that icon)."""
    c = _calib(); learned = learned or {}
    flat = np.asarray(flatten(path, geom["corners"]), dtype=np.float32)
    ncc, dx, dy = register(flat, geom.get("ref"))
    out = {"ok": True, "register": {"ncc": round(ncc, 3), "dx": dx, "dy": dy}, "scores": {}, "provisional": []}
    if ncc < c["min_ncc"]:
        out.update(ok=False, error=f"LCD does not match the reference (ncc {ncc:.2f}) - camera moved, dark, or screen off")
    a = np.roll(np.roll(flat, -dy, 0), -dx, 1)
    for name, ic in c["icons"].items():
        v = icon_score(a, ic["box"], c["icon_method"])
        thr = learned.get(name, {}).get("thr", ic["thr"])
        out["scores"][name] = round(v, 3); out[name] = bool(v > thr)
        if ic.get("provisional") and name not in learned:
            out["provisional"].append(name)
    out["seg_values"] = {k: round(v, 1) for k, v in seg_values(contrast(a)).items()}
    cells = [_cell(i, out["seg_values"], geom.get("seg_thr") or {}) for i in range(len(c["digits"]["cells"]))]
    out["digit_cells"] = [{"digit": d, "segs": s, "values": v} for d, s, v in cells]
    digs = [d for d, _, _ in cells]
    if "?" in digs or digs[2] == "blank" or (digs[1] == "blank" and digs[0] != "blank"):
        out["temperature_f"] = None            # not a plain number: blank screen, or an error code like E04
    else:
        n = [0 if d == "blank" else d for d in digs]; out["temperature_f"] = n[0] * 100 + n[1] * 10 + n[2]
    out["mode"] = "dhw" if out.get("dhw_box") else ("heating" if out.get("radiator") else "idle")
    return out
