"""
lcd_recal.py - where the boiler LCD is in the camera frame, and finding it again after the
camera is moved. No model.

The geometry lives in <out>/lcd_geom.json: the LCD glass corners in the full frame, the anchor
frame those corners were measured on (<out>/lcd_anchor.jpg), a flat reference for registration
taken at that position (<out>/lcd_ref.png), and the digit segments' contrast history there
(lcd_pixels.learn_seg_thr turns it into cut-offs). Before the first adoption it starts from the
anchor shipped in docs (lcd_calib.json "anchor").

Finding the LCD again (find): SIFT features of the boiler's faceplate around the LCD on the
anchor frame are matched in the new frame; a RANSAC similarity (shift, scale, roll) gives the
rough position, and ECC alignment of the anchor's LCD patch (glass frame and all) refines it to
a full perspective transform. The anchor's corners go through that transform. Replayed
2026-10-21 over 194 stills: same-position frames give back the anchor corners to the pixel
(ECC 0.99), and every frame after that day's move (camera closer, centred, rolled ~5 deg) was
found (ECC 0.94, 66-88 inlier matches) and read.

Recalibration runs when the LCD stops registering against the reference (monitor), or when an
admin asks for it (<out>/lcd_recalibrate exists).
"""
import json, os, shutil, time
import numpy as np
from PIL import Image

import lcd_pixels

GEOM = "lcd_geom.json"
ANCHOR = "lcd_anchor.jpg"
REF = "lcd_ref.png"
REQUEST = "lcd_recalibrate"     # touched by the admin "recalibrate" button
HIST_MAX = 300                  # segment values kept per segment
ANCHOR_REFRESH_S = 6 * 3600     # re-take the anchor this often from a good read, so lighting stays current
MIN_INLIERS = 15
MIN_ECC = 0.8


def _now(): return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def current(out):
    """The geometry in use (with "ref" loaded as an array and "seg_thr" learned), bootstrapped
    from the docs anchor if this output directory has never adopted one."""
    try:
        g = json.load(open(os.path.join(out, GEOM)))
    except Exception:
        a = lcd_pixels._calib()["anchor"]
        shutil.copy(os.path.join(lcd_pixels.HERE, "docs", a["image"]), os.path.join(out, ANCHOR))
        g = {"corners": a["corners"], "ref": None, "since": _now(), "how": "docs anchor", "anchor_at": 0,
             "seg_hist": {}, "history": []}
        save(out, g)
    g["seg_thr"] = lcd_pixels.learn_seg_thr(g.get("seg_hist"))
    return g


def load_ref(out, g):
    """The flat reference array for register(): this position's own, or the docs one."""
    if g.get("ref") and os.path.exists(os.path.join(out, g["ref"])):
        return np.asarray(Image.open(os.path.join(out, g["ref"])).convert("L"), dtype=np.float32)
    return None


def save(out, g):
    keep = {k: v for k, v in g.items() if k not in ("seg_thr", "_ref")}
    keep["history"] = keep.get("history", [])[-20:]
    tmp = os.path.join(out, GEOM + ".tmp")
    json.dump(keep, open(tmp, "w"))
    os.replace(tmp, os.path.join(out, GEOM))


def requested(out): return os.path.exists(os.path.join(out, REQUEST))


def clear_request(out):
    try:
        os.remove(os.path.join(out, REQUEST))
    except FileNotFoundError:
        pass


def find(out, path, g):
    """Locate the LCD corners in frame `path`. Returns (corners, stats) or (None, stats) with
    stats["error"] saying why the result can't be trusted.
    Measured from the docs anchor first: its corners were checked by eye, while every adopted
    position carries a small error from the frame it was measured on (replay: chaining a second
    move off a half-way frame put the digits off their boxes). The latest anchor is the fallback,
    for when the docs one no longer matches (new lighting, boiler cover changed)."""
    a = lcd_pixels._calib()["anchor"]
    corners, stats = _find_from(os.path.join(lcd_pixels.HERE, "docs", a["image"]), a["corners"], path)
    stats["anchor"] = "docs"
    if corners is None and g.get("how") != "docs anchor":
        c2, s2 = _find_from(os.path.join(out, ANCHOR), g["corners"], path)
        if c2 is not None:
            return c2, {**s2, "anchor": "latest", "docs_anchor": stats.get("error")}
    return corners, stats


def _find_from(anchor_path, anchor_corners, path):
    import cv2
    a = cv2.imread(anchor_path, cv2.IMREAD_GRAYSCALE)
    b = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    ac = np.array(anchor_corners, np.float32)
    x0, y0 = ac.min(0); x1, y1 = ac.max(0); w, h = x1 - x0, y1 - y0
    # The faceplate (buttons, knob, model badge, bezel) is in the LCD's plane and has the texture
    # the glass lacks: match features in a window around the LCD only, so pipes and walls don't vote.
    # Features at half size: SIFT doubles its input internally, and a full 1080p frame peaked at
    # ~600 MB (the VM has 1 GB, shared with the live streams). ECC below runs at full size.
    mask = np.zeros_like(a)
    mask[int(max(0, y0 - h)):int(y1 + 2 * h), int(max(0, x0 - 0.6 * w)):int(x1 + 0.6 * w)] = 255
    half = lambda im: cv2.resize(im, (im.shape[1] // 2, im.shape[0] // 2), interpolation=cv2.INTER_AREA)
    sift = cv2.SIFT_create(4000, contrastThreshold=0.02)
    ka, da = sift.detectAndCompute(half(a), half(mask))
    kb, db = sift.detectAndCompute(half(b), None)
    stats = {"inliers": 0, "ecc": None}
    if da is None or db is None or len(ka) < MIN_INLIERS:
        return None, {**stats, "error": "too few features"}
    good = [p[0] for p in cv2.BFMatcher().knnMatch(da, db, k=2) if len(p) == 2 and p[0].distance < 0.8 * p[1].distance]
    if len(good) < MIN_INLIERS:
        return None, {**stats, "error": f"only {len(good)} feature matches"}
    src = np.float32([ka[m.queryIdx].pt for m in good]) * 2; dst = np.float32([kb[m.trainIdx].pt for m in good]) * 2
    # A full homography from these matches came out skewed (the faceplate is low-texture); a
    # similarity is stable, and ECC below supplies the perspective.
    A, inl = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=6.0, maxIters=5000)
    stats["inliers"] = int(inl.sum()) if inl is not None else 0
    if A is None or stats["inliers"] < MIN_INLIERS:
        return None, {**stats, "error": f"only {stats['inliers']} consistent matches"}
    px, py = int(0.12 * w), int(0.25 * h)
    tx0, ty0 = int(x0) - px, int(y0) - py
    tmpl = a[max(0, ty0):int(y1) + py, max(0, tx0):int(x1) + px].astype(np.float32)
    T = np.array([[1, 0, max(0, tx0)], [0, 1, max(0, ty0)], [0, 0, 1]], np.float32)   # patch -> anchor coords
    warp = (np.vstack([A, [0, 0, 1]]).astype(np.float32) @ T); warp /= warp[2, 2]
    try:
        cc, warp = cv2.findTransformECC(cv2.GaussianBlur(tmpl, (0, 0), 1.5), cv2.GaussianBlur(b.astype(np.float32), (0, 0), 1.5),
                                        warp, cv2.MOTION_HOMOGRAPHY,
                                        (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 200, 1e-6), None, 5)
    except cv2.error as e:
        return None, {**stats, "error": f"ECC did not converge ({str(e).strip().splitlines()[-1][:80]})"}
    stats["ecc"] = round(float(cc), 3)
    corners = cv2.perspectiveTransform(ac[None], warp @ np.linalg.inv(T))[0]
    if cc < MIN_ECC:
        return None, {**stats, "error": f"LCD patch matches poorly (ECC {cc:.2f})"}
    area = lambda q: 0.5 * abs(np.dot(q[:, 0], np.roll(q[:, 1], 1)) - np.dot(q[:, 1], np.roll(q[:, 0], 1)))
    ratio = area(corners) / max(area(ac), 1)
    inside = (corners[:, 0] >= 0).all() and (corners[:, 1] >= 0).all() and (corners[:, 0] < b.shape[1]).all() and (corners[:, 1] < b.shape[0]).all()
    if not (0.3 < ratio < 3.5) or not inside:
        return None, {**stats, "error": f"implausible LCD outline (area x{ratio:.2f}, inside frame: {inside})"}
    return [[round(float(x), 1), round(float(y), 1)] for x, y in corners], stats


def adopt(out, path, corners, stats, why):
    """Make `path` the anchor at `corners`: new flat reference, fresh digit history."""
    old = current(out)
    shutil.copy(path, os.path.join(out, ANCHOR))
    flat = lcd_pixels.flatten(path, corners)
    flat.save(os.path.join(out, REF))
    hist = old.get("history", []) + [{"until": _now(), "since": old.get("since"), "how": old.get("how"), "corners": old.get("corners")}]
    g = {"corners": corners, "ref": REF, "since": _now(), "how": why, "stats": stats, "anchor_at": time.time(),
         "seg_hist": {}, "history": hist}
    save(out, g)
    g["seg_thr"] = {}
    return g


def record(out, g, path, px):
    """After a good read at the current position: add the digit segments to their history, and
    re-take the anchor now and then so the feature match sees today's lighting."""
    h = g.setdefault("seg_hist", {})
    for k, v in (px.get("seg_values") or {}).items():
        h.setdefault(k, []).append(v)
        del h[k][:-HIST_MAX]
    if px.get("temperature_f") is not None and time.time() - (g.get("anchor_at") or 0) > ANCHOR_REFRESH_S:
        shutil.copy(path, os.path.join(out, ANCHOR)); g["anchor_at"] = time.time()
    save(out, g)
