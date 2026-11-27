import { applyToBox } from "/static/orient.js";
const CAMS = JSON.parse(document.getElementById("data").textContent);
const $ = s => document.querySelector(s);
const wall = $("#wall"), colsSel = $("#cols");
let sel = new Set(CAMS.slice(0, 6).map(c => c.id)), cols = 0, order = CAMS.map(c => c.id);
// the page may list only one property's cameras (house filter), so picks and order for
// cameras not listed here are kept as they are rather than dropped
try { const s = JSON.parse(localStorage.getItem("wall:sel")); if (Array.isArray(s)) sel = new Set(s.filter(id => Number.isInteger(id))); cols = +localStorage.getItem("wall:cols") || 0;
  const o = JSON.parse(localStorage.getItem("wall:order")); if (Array.isArray(o)) order = [...o.filter(id => Number.isInteger(id)), ...order.filter(id => !o.includes(id))]; } catch {}
// the TV app (/wall?tv=1): every camera listed (the house it asked for), filling the screen, nothing saved
const TV = wall.dataset.tv === "1";
const BLANK = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7";
if (TV) { sel = new Set(CAMS.map(c => c.id)); cols = 0; order = CAMS.map(c => c.id); }
const shownSel = () => CAMS.filter(c => sel.has(c.id)).length;
const byOrder = (a, b) => order.indexOf(a.id) - order.indexOf(b.id);
colsSel.value = String(cols);

const drawer = $("#drawer"), back = $("#drawerBack"), list = $("#pickList"), search = $("#pickSearch");
function openDrawer(on) { drawer.hidden = !on; back.hidden = !on; if (on) { drawPick(); if (CAMS.length > 8) search.focus(); } }
function drawPick() {
  const q = search.value.trim().toLowerCase(); list.innerHTML = "";
  const houses = [...new Set(CAMS.map(c => c.house || "Unassigned"))];
  for (const h of houses) {
    const cams = CAMS.filter(c => (c.house || "Unassigned") === h && (!q || c.name.toLowerCase().includes(q) || h.toLowerCase().includes(q)));
    if (!cams.length) continue;
    const allOn = cams.every(c => sel.has(c.id));
    const hd = document.createElement("div"); hd.className = "pickHouse";
    hd.innerHTML = `<b>${h}</b><button type="button" class="link small">${allOn ? "none" : "all"}</button>`;
    hd.querySelector("button").addEventListener("click", () => { for (const c of cams) allOn ? sel.delete(c.id) : sel.add(c.id); apply(); });
    list.appendChild(hd);
    for (const c of cams) {
      const row = document.createElement("label"); row.className = "pickRow";
      row.innerHTML = `<input type="checkbox" ${sel.has(c.id) ? "checked" : ""}><span class="sw"></span><span class="dot ${c.online === 1 ? "ok" : c.online === 0 ? "bad" : "unk"}"></span><span class="nm">${c.name}</span>${c.sd ? `<span class="sdTag ${c.sd.state}" title="${c.sd.long}">${c.sd.short}</span>` : ""}`;
      row.querySelector("input").addEventListener("change", ev => { ev.target.checked ? sel.add(c.id) : sel.delete(c.id); apply(); });
      list.appendChild(row);
    }
  }
  const n = shownSel();
  $("#pickCount").textContent = `${n} of ${CAMS.length}`;
  $("#pickTxt").textContent = n ? `${n} camera${n > 1 ? "s" : ""}` : "Choose cameras";
}
function apply() { save(); drawPick(); drawWall(); }
search.addEventListener("input", drawPick);
$("#pickBtn").addEventListener("click", () => openDrawer(true));
$("#drawerClose").addEventListener("click", () => openDrawer(false));
$("#drawerDone").addEventListener("click", () => openDrawer(false));
back.addEventListener("click", () => openDrawer(false));
$("#pickNone").addEventListener("click", () => { for (const c of CAMS) sel.delete(c.id); apply(); });
document.addEventListener("keydown", ev => { if (ev.key === "Escape") openDrawer(false); });
search.value = "";
function save() { if (TV) return; try { localStorage.setItem("wall:sel", JSON.stringify([...sel])); localStorage.setItem("wall:cols", String(cols)); localStorage.setItem("wall:order", JSON.stringify(order)); } catch {} }
function bestCols(n, W, H) {
  // the column count that wastes the least screen for n 16:9 tiles
  let best = 1, bestArea = 0;
  for (let c = 1; c <= Math.min(n, 8); c++) { const r = Math.ceil(n / c); const tw = Math.min(W / c, (H / r) * 16 / 9); const area = tw * tw * 9 / 16 * n; if (area > bestArea) { bestArea = area; best = c; } }
  return best;
}
// a big window: the column count and tile width that show all n tiles without scrolling
function fitWall(n, W, H, gap) {
  let best = { c: 1, tw: 0 };
  for (let c = 1; c <= Math.min(n, 8); c++) { const r = Math.ceil(n / c); const tw = Math.min((W - gap * (c - 1)) / c, (H - gap * (r - 1)) / r * 16 / 9); if (tw > best.tw) best = { c, tw }; }
  return best;
}
const tiles = new Map();
function drawWall() {
  const chosen = CAMS.filter(c => sel.has(c.id)).sort(byOrder);
  $("#wallEmpty").hidden = chosen.length > 0;
  const n = chosen.length, fs = !!document.fullscreenElement || TV;
  wall.style.maxWidth = "";
  const w = wall.clientWidth || innerWidth;
  let auto = fs ? bestCols(n, innerWidth, innerHeight) : w < 640 ? (n > 1 ? 2 : 1) : n <= 1 ? 1 : n <= 4 ? 2 : n <= 9 ? 3 : 4;
  // big window with columns on auto: size the tiles so the whole wall shows without scrolling
  if (!fs && !cols && n && w >= 1100) {
    const gap = parseFloat(getComputedStyle(wall).columnGap) || 10, room = innerHeight - (wall.getBoundingClientRect().top + scrollY) - 32;
    const f = fitWall(n, w, room, gap);
    if (f.tw >= 280) { auto = f.c; wall.style.maxWidth = Math.floor(f.c * f.tw + gap * (f.c - 1)) + "px"; }
    else auto = Math.min(n, Math.max(auto, Math.floor((w + gap) / (340 + gap))));   // too many to fit at a useful size: fill the width and scroll
  }
  wall.style.gridTemplateColumns = `repeat(${cols || auto}, minmax(0, 1fr))`;
  for (const [id, t] of tiles) if (!sel.has(id)) { t.vs.src = ""; t.el.remove(); tiles.delete(id); }
  // keep DOM order in sync with the saved order
  for (const c of chosen) { const t = tiles.get(c.id); if (t && t.el.parentNode === wall) wall.appendChild(t.el); }
  for (const c of chosen) {
    if (tiles.has(c.id)) continue;
    const el = document.createElement("div"); el.className = "wallTile"; el.title = "Click for sound";
    el.dataset.id = c.id;
    el.innerHTML = `<div class="stage"><img class="poster" src="/stream/${c.id}/thumb.jpg?t=${c.thumbAt}" alt=""><div class="spinner"><i class="spin"></i>Connecting…</div>
      <div class="tag"><span class="dot ${c.online === 1 ? "ok" : c.online === 0 ? "bad" : "unk"}"></span><b>${c.name}</b><span>${c.house || ""}${c.sd ? ` · ${c.sd.short}` : ""}</span><i class="snd" hidden>🔊</i><a class="open" href="/cam/${c.id}" title="Open this camera">↗</a>${c.opener ? `<button type="button" class="garageTile" title="Open / close ${c.opener.name}">🚪</button>` : ""}<i class="grip" title="Drag to rearrange">⠿</i></div></div>`;
    const sideways = c.rotate === 90 || c.rotate === 270; if (sideways) el.classList.add("sideways");
    const stage = el.querySelector(".stage"), vs = document.createElement("video-stream"); vs.setAttribute("mode", "mse,hls,mp4,mjpeg"); vs.setAttribute("background", "true");
    stage.prepend(vs); wall.appendChild(el);
    const poster = stage.querySelector(".poster"), hint = stage.querySelector(".spinner");
    const amb = document.createElement("canvas"); amb.className = "amb"; amb.width = 48; amb.height = 27; stage.prepend(amb);
    const actx = amb.getContext("2d", { alpha: false }); let lastAmb = 0;
    const paintAmb = now => { const vv = vs.video; if (vv && vv.readyState >= 2 && now - lastAmb > 250) { lastAmb = now; try { actx.drawImage(vv, 0, 0, amb.width, amb.height); amb.style.opacity = "1"; } catch {} } if (tiles.has(c.id)) requestAnimationFrame(paintAmb); };
    requestAnimationFrame(paintAmb);
    const lay = () => { for (const x of [vs, poster]) applyToBox(stage, x, c.rotate, c.flipH, c.flipV); };
    const shown = () => { poster.style.opacity = "0"; hint.style.display = "none"; const t = tiles.get(c.id); if (t && TV && t.live && !t.playing) { t.playing = true; tvDraw(t); } };
    const v = vs.video;
    if (v) { v.muted = true; v.controls = false; v.setAttribute("playsinline", ""); if (TV) v.poster = BLANK;   // no big grey play icon on Android TV
      v.addEventListener("playing", shown); v.addEventListener("timeupdate", () => { if (v.currentTime > 0) shown(); });
      v.addEventListener("waiting", () => { hint.style.display = ""; hint.innerHTML = '<i class="spin"></i>Reconnecting…'; }); }
    // belt and braces: some modes never fire "playing" on the inner element
    const poll = setInterval(() => { const vv = vs.video; if (vv && vv.readyState >= 3 && !vv.paused && vv.currentTime > 0) { shown(); } if (!tiles.has(c.id)) clearInterval(poll); }, 500);
    if (c.private) { hint.textContent = "🔒 Privacy mode"; poster.style.opacity = "1"; }
    else if (!TV) vs.src = `/stream/${c.id}/ws`;   // TV: tvPlan() decides which tiles play
    // click a tile to hear it (each tile toggles on its own; all start muted)
    const snd = el.querySelector(".snd");
    const drawSnd = () => { const vv = vs.video, on = !!vv && !vv.muted; snd.hidden = !on; el.classList.toggle("sound", on); el.title = on ? "Click to mute" : "Click for sound"; };
    if (v) v.addEventListener("volumechange", drawSnd);
    el.addEventListener("click", ev => {
      if (el.dataset.dragged || ev.target.closest(".open,.grip,.garageTile")) return;
      const vv = vs.video; if (!vv) return;
      let lvl = 0.8; try { const x = parseFloat(localStorage.getItem("vol")); if (x > 0 && x <= 1) lvl = x; } catch {}   // the camera page's volume
      vv.volume = lvl; vv.muted = !vv.muted; if (!vv.muted) vv.play().catch(() => {});
      drawSnd();
    });
    // garage door (admin-only; present only when the camera has an opener assigned)
    const gbtn = el.querySelector(".garageTile");
    if (gbtn && c.opener) {
      let gbusy = false;
      gbtn.addEventListener("click", async ev => {
        ev.stopPropagation();
        if (gbusy) return;
        if (!confirm(`Open / close "${c.opener.name}"? The door will move.`)) return;
        gbusy = true; gbtn.classList.add("busy"); gbtn.textContent = "…";
        try {
          const r = await fetch(`/garage/${c.opener.id}/press`, { method: "POST", credentials: "same-origin" });
          const j = await r.json().catch(() => ({}));
          if (!r.ok || !j.ok) throw new Error(j.error || `error ${r.status}`);
          gbtn.classList.remove("busy"); gbtn.classList.add("ok"); gbtn.textContent = "✓";
        } catch (e) { gbtn.classList.remove("busy"); gbtn.classList.add("bad"); gbtn.textContent = "✕"; gbtn.title = String(e.message || e); }
        setTimeout(() => { gbtn.classList.remove("ok", "bad"); gbtn.textContent = "🚪"; gbusy = false; }, 2500);
      });
    }
    tiles.set(c.id, { el, vs, lay, c, poster, stillAt: (c.thumbAt || 0) * 1000, live: false, playing: false }); lay(); enableDrag(el);
    if (TV) tvTile(tiles.get(c.id));
    new ResizeObserver(lay).observe(stage);
  }
}
colsSel.addEventListener("change", () => { cols = +colsSel.value; save(); drawWall(); });
$("#fullBtn").addEventListener("click", () => { (wall.requestFullscreen || wall.webkitRequestFullscreen).call(wall); });
window.addEventListener("resize", () => { if (!cols) drawWall(); });
drawPick(); drawWall();

// ---------------------------------------------------------------- drag to rearrange: the live tile itself moves
// layout positions ignore any in-flight slide animation (offsetLeft/Top are pre-transform)
const layoutRect = x => { const w = wall.getBoundingClientRect(); return { left: w.left + x.offsetLeft, top: w.top + x.offsetTop, width: x.offsetWidth, height: x.offsetHeight }; };
const rectsOf = () => { const m = new Map(); for (const x of wall.querySelectorAll(".wallTile:not(.ph):not(.lifted)")) m.set(x, layoutRect(x)); return m; };
function flip(before) {
  for (const [x, b] of before) { const a = layoutRect(x); const dx = b.left - a.left, dy = b.top - a.top;
    if (!dx && !dy) continue;
    // continue from wherever the tile visually is right now, so a mid-slide retarget doesn't jump
    const cur = new DOMMatrixReadOnly(getComputedStyle(x).transform);
    x.style.transition = "none"; x.style.transform = `translate(${dx + cur.m41}px,${dy + cur.m42}px)`; x.getBoundingClientRect();
    x.style.transition = "transform .18s ease"; x.style.transform = ""; }
}
function slotAt(px, py, el, ph) {
  // which tile's settled box is under the pointer, and whether we are clearly past its midpoint
  for (const x of wall.querySelectorAll(".wallTile:not(.ph):not(.lifted)")) {
    const r = layoutRect(x);
    if (px < r.left || px > r.left + r.width || py < r.top || py > r.top + r.height) continue;
    const f = (px - r.left) / r.width;
    if (f > 0.35 && f < 0.65) return null;                       // dead zone around the midpoint: keep the current slot
    return { over: x, before: f <= 0.35 };
  }
  return null;
}
function enableDrag(el) {
  let start = null, lifted = false, ph = null, off = null;
  // links and images start a native drag in the browser, which cancels our pointer events
  el.draggable = false; el.addEventListener("dragstart", ev => ev.preventDefault());
  el.querySelectorAll("img").forEach(i => { i.draggable = false; });
  el.addEventListener("click", ev => { if (el.dataset.dragged) { ev.preventDefault(); delete el.dataset.dragged; } });
  el.addEventListener("pointerdown", ev => {
    if (ev.button !== 0) return;
    const onGrip = !!ev.target.closest(".grip");
    if (ev.pointerType !== "mouse" && !onGrip) return;
    start = { x: ev.clientX, y: ev.clientY, id: ev.pointerId }; try { el.setPointerCapture(ev.pointerId); } catch {} if (onGrip) ev.preventDefault();
  });
  el.addEventListener("pointermove", ev => {
    if (!start || ev.pointerId !== start.id) return;
    if (!lifted) {
      if (Math.hypot(ev.clientX - start.x, ev.clientY - start.y) < 8) return;
      lifted = true; el.dataset.dragged = "1";
      const r = el.getBoundingClientRect(); off = { x: ev.clientX - r.left, y: ev.clientY - r.top };
      ph = document.createElement("div"); ph.className = "wallTile ph"; ph.style.height = r.height + "px";
      wall.insertBefore(ph, el);
      el.classList.add("lifted"); Object.assign(el.style, { position: "fixed", left: r.left + "px", top: r.top + "px", width: r.width + "px", height: r.height + "px", zIndex: 100, transition: "none" });
      document.body.classList.add("dragging");
    }
    el.style.transform = `translate(${ev.clientX - off.x - parseFloat(el.style.left)}px, ${ev.clientY - off.y - parseFloat(el.style.top)}px) scale(1.03)`;
    const hit = slotAt(ev.clientX, ev.clientY, el, ph);
    if (hit) { let target = hit.before ? hit.over : hit.over.nextSibling; if (target === el) target = el.nextSibling;
      const already = target === ph || (target === null && ph === wall.lastElementChild) || (target && target.previousSibling === ph);
      if (!already) { const b = rectsOf(); wall.insertBefore(ph, target); flip(b); } }
  });
  const end = ev => {
    if (!start || ev.pointerId !== start.id) return; start = null;
    if (!lifted) return; lifted = false; document.body.classList.remove("dragging");
    const r = layoutRect(ph), cur = el.getBoundingClientRect();
    // glide into the slot, then snap back into the grid flow
    el.style.transition = "transform .18s ease"; el.style.transform = `translate(${r.left - parseFloat(el.style.left)}px, ${r.top - parseFloat(el.style.top)}px) scale(1)`;
    const settle = () => { el.classList.remove("lifted"); el.style.cssText = ""; wall.insertBefore(el, ph); ph.remove(); ph = null; for (const t of tiles.values()) t.lay();
      const ids = [...wall.querySelectorAll(".wallTile:not(.ph)")].map(x => +x.dataset.id); order = [...ids, ...order.filter(id => !ids.includes(id))]; save(); };
    let done = false; const once = () => { if (!done) { done = true; settle(); } };
    if (Math.abs(r.left - cur.left) + Math.abs(r.top - cur.top) < 2) once();
    else { el.addEventListener("transitionend", ev => { if (ev.target === el) once(); }, { once: true }); setTimeout(once, 220); }
  };
  el.addEventListener("pointerup", end); el.addEventListener("pointercancel", end);
}
document.addEventListener("fullscreenchange", () => { drawWall(); setTimeout(() => { for (const t of tiles.values()) t.lay(); }, 50); });

// in fullscreen, hover affordances only while the mouse is moving
let idleT = null;
document.addEventListener("mousemove", () => { if (!document.fullscreenElement) return; wall.classList.add("active"); clearTimeout(idleT); idleT = setTimeout(() => wall.classList.remove("active"), 2500); });
document.addEventListener("fullscreenchange", () => { wall.classList.remove("active"); });

// ---------------------------------------------------------------- TV: only so many live videos at once
// A TV decodes a fixed number of videos at a time (the living room TCL: 2; a third fails, and the player then
// retries forever). So every camera shows its latest picture, TV_LIVE of them play live and they take turns;
// a tile leaving its turn keeps its last frame, so no picture is old for long. The remote's arrows pick a tile
// (it goes live and stays live); OK fills the screen with it; Back (the app calls tvBack()) returns to the grid.
// ?live=N for a TV that can decode more. Only 2 relays run on the server instead of one per camera.
let tvPicked = null, tvSolo = null, tvTurn = 0;
const TV_LIVE = Math.max(1, Math.min(4, +new URLSearchParams(location.search).get("live") || 2));
const TV_TURN_MS = 20000, TV_START_MS = 15000, TV_FREE_MS = 1500, TV_SIT_OUT_MS = 30000;
let tvFreeAt = 0, tvStartT = 0, tvFocusT = 0;
const tvLive = () => [...tiles.values()].filter(t => t.live).length;
const tvList = () => CAMS.filter(c => sel.has(c.id) && !c.private).sort(byOrder).map(c => tiles.get(c.id)).filter(Boolean);
function tvTile(t) {
  t.badge = document.createElement("span"); t.badge.className = "tvBadge"; t.el.querySelector(".stage").appendChild(t.badge);
  t.el.tabIndex = 0;
  t.el.addEventListener("focus", () => { tvPicked = t; clearTimeout(tvFocusT); tvFocusT = setTimeout(tvPlan, 500); });   // arrowing across tiles doesn't start each one
  t.vs.RECONNECT_TIMEOUT = 864e5;   // the player's own retries would fight the turns for decoders; tvPlan restarts tiles
  t.el.addEventListener("keydown", ev => { if (ev.key === "Enter" && !tvSolo) { ev.preventDefault(); tvSoloOn(t); } });
  tvDraw(t);
}
function tvAgo(ms) { const s = Math.max(0, (Date.now() - ms) / 1000); return !ms ? "" : s < 60 ? `${Math.round(s)} s ago` : s < 3600 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`; }
function tvDraw(t) {
  if (!t.badge) return;
  t.badge.classList.toggle("live", t.live && t.playing);
  t.badge.textContent = t.c.private ? "Privacy mode" : t.live ? (t.playing ? "Live" : "Connecting…") : tvAgo(t.stillAt);
}
function tvStart(t) {
  if (t.live || tvLive() >= TV_LIVE) return;
  t.live = true; t.playing = false; t.startedAt = Date.now();
  t.vs.src = `/stream/${t.c.id}/ws`;
  tvDraw(t);
}
function tvStop(t) {
  if (!t.live) return;
  const v = t.vs.video;
  if (t.playing && v && v.videoWidth) {        // keep its last frame as the still
    try { const cv = document.createElement("canvas"), k = Math.min(1, 960 / v.videoWidth);
      cv.width = Math.round(v.videoWidth * k); cv.height = Math.round(v.videoHeight * k);
      cv.getContext("2d").drawImage(v, 0, 0, cv.width, cv.height);
      t.poster.src = cv.toDataURL("image/jpeg", 0.85); t.stillAt = Date.now(); } catch {}
  }
  t.poster.style.opacity = "1";
  t.vs.ondisconnect();                          // closes the socket
  const vv = t.vs.video;                         // and makes the browser let go of the decoder now, not when it gets round to it
  if (vv) { try { vv.pause(); vv.removeAttribute("src"); vv.srcObject = null; vv.load(); } catch {} }
  tvFreeAt = Date.now() + TV_FREE_MS;
  t.live = false; t.playing = false;
  tvDraw(t);
}
// Which tiles should play now: the solo tile; else the picked one plus the tiles whose turn it is.
function tvPlan() {
  const all = tvList(); if (!all.length) return;
  let want;
  if (tvSolo) want = [tvSolo];
  else {
    want = tvPicked && all.includes(tvPicked) ? [tvPicked] : [];
    const now = Date.now();
    for (let i = 0; want.length < Math.min(TV_LIVE, all.length) && i < all.length; i++) {
      const t = all[(tvTurn + i) % all.length];
      if (!want.includes(t) && !(t.failedAt && now - t.failedAt < TV_SIT_OUT_MS)) want.push(t);   // just failed: sit a turn out
    }
  }
  all.filter(t => t.live && !want.includes(t)).forEach(tvStop);
  // a decoder takes a moment to come free; starting before then is refused on the TV
  clearTimeout(tvStartT);
  tvStartT = setTimeout(() => want.forEach(tvStart), Math.max(0, tvFreeAt - Date.now()));
}
function tvSoloOn(t) {
  tvSolo = t; wall.classList.add("solo"); t.el.classList.add("soloTile");
  tvPlan(); setTimeout(() => { const v = t.vs.video; if (v) { v.muted = false; v.play().catch(() => {}); } t.lay(); }, 800);
}
window.tvBack = () => {           // the TV app asks first on Back: true = handled here
  if (!tvSolo) return false;
  const t = tvSolo; tvSolo = null; const v = t.vs.video; if (v) v.muted = true;
  wall.classList.remove("solo"); t.el.classList.remove("soloTile"); t.el.focus(); tvPlan();
  setTimeout(() => { for (const x of tiles.values()) x.lay(); }, 50);
  return true;
};
if (TV) {
  tvPlan();
  setInterval(() => {               // turns, and a tile that hasn't started in time waits for its next one
    const now = Date.now();
    let freed = false;
    for (const t of tiles.values()) {
      const vs = t.vs, gone = !vs.ws && !vs.pc;   // the player gave up on its connection
      if (t.live && ((!t.playing && now - t.startedAt > TV_START_MS) || (t.playing && gone))) {
        t.failedAt = now; tvStop(t); freed = true;   // keeps its last frame; another tile gets the slot
      }
      tvDraw(t);
    }
    if (freed) tvPlan();
  }, 1000);
  setInterval(() => { if (tvSolo) return; tvTurn += Math.max(1, TV_LIVE - (tvPicked ? 1 : 0)); tvPlan(); }, TV_TURN_MS);
}
