import { applyToBox } from "/static/orient.js";
const CAMS = JSON.parse(document.getElementById("data").textContent);
const $ = s => document.querySelector(s);
const wall = $("#wall"), colsSel = $("#cols");
let sel = new Set(CAMS.slice(0, 6).map(c => c.id)), cols = 0, order = CAMS.map(c => c.id);
try { const s = JSON.parse(localStorage.getItem("wall:sel")); if (Array.isArray(s)) sel = new Set(s.filter(id => CAMS.some(c => c.id === id))); cols = +localStorage.getItem("wall:cols") || 0;
  const o = JSON.parse(localStorage.getItem("wall:order")); if (Array.isArray(o)) order = [...o.filter(id => CAMS.some(c => c.id === id)), ...order.filter(id => !o.includes(id))]; } catch {}
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
      row.innerHTML = `<input type="checkbox" ${sel.has(c.id) ? "checked" : ""}><span class="sw"></span><span class="dot ${c.online === 1 ? "ok" : c.online === 0 ? "bad" : "unk"}"></span><span class="nm">${c.name}</span>`;
      row.querySelector("input").addEventListener("change", ev => { ev.target.checked ? sel.add(c.id) : sel.delete(c.id); apply(); });
      list.appendChild(row);
    }
  }
  $("#pickCount").textContent = `${sel.size} of ${CAMS.length}`;
  $("#pickTxt").textContent = sel.size ? `${sel.size} camera${sel.size > 1 ? "s" : ""}` : "Choose cameras";
}
function apply() { save(); drawPick(); drawWall(); }
search.addEventListener("input", drawPick);
$("#pickBtn").addEventListener("click", () => openDrawer(true));
$("#drawerClose").addEventListener("click", () => openDrawer(false));
$("#drawerDone").addEventListener("click", () => openDrawer(false));
back.addEventListener("click", () => openDrawer(false));
$("#pickNone").addEventListener("click", () => { sel.clear(); apply(); });
document.addEventListener("keydown", ev => { if (ev.key === "Escape") openDrawer(false); });
search.value = "";
function save() { try { localStorage.setItem("wall:sel", JSON.stringify([...sel])); localStorage.setItem("wall:cols", String(cols)); localStorage.setItem("wall:order", JSON.stringify(order)); } catch {} }
function bestCols(n, W, H) {
  // the column count that wastes the least screen for n 16:9 tiles
  let best = 1, bestArea = 0;
  for (let c = 1; c <= Math.min(n, 8); c++) { const r = Math.ceil(n / c); const tw = Math.min(W / c, (H / r) * 16 / 9); const area = tw * tw * 9 / 16 * n; if (area > bestArea) { bestArea = area; best = c; } }
  return best;
}
const tiles = new Map();
function drawWall() {
  const chosen = CAMS.filter(c => sel.has(c.id)).sort(byOrder);
  $("#wallEmpty").hidden = chosen.length > 0;
  const n = chosen.length, w = wall.clientWidth || innerWidth, fs = !!document.fullscreenElement;
  const auto = fs ? bestCols(n, innerWidth, innerHeight) : w < 640 ? (n > 1 ? 2 : 1) : n <= 1 ? 1 : n <= 4 ? 2 : n <= 9 ? 3 : 4;
  wall.style.gridTemplateColumns = `repeat(${cols || auto}, minmax(0, 1fr))`;
  for (const [id, t] of tiles) if (!sel.has(id)) { t.vs.src = ""; t.el.remove(); tiles.delete(id); }
  // keep DOM order in sync with the saved order
  for (const c of chosen) { const t = tiles.get(c.id); if (t && t.el.parentNode === wall) wall.appendChild(t.el); }
  for (const c of chosen) {
    if (tiles.has(c.id)) continue;
    const el = document.createElement("a"); el.className = "wallTile"; el.href = `/cam/${c.id}`;
    el.dataset.id = c.id;
    el.innerHTML = `<div class="stage"><img class="poster" src="/stream/${c.id}/thumb.jpg?t=${c.thumbAt}" alt=""><div class="spinner">Connecting…</div>
      <div class="tag"><span class="dot ${c.online === 1 ? "ok" : c.online === 0 ? "bad" : "unk"}"></span><b>${c.name}</b><span>${c.house || ""}</span><i class="grip" title="Drag to rearrange">⠿</i></div></div>`;
    const sideways = c.rotate === 90 || c.rotate === 270; if (sideways) el.classList.add("sideways");
    const stage = el.querySelector(".stage"), vs = document.createElement("video-stream"); vs.setAttribute("mode", "mse,hls,mp4,mjpeg"); vs.setAttribute("background", "true");
    stage.prepend(vs); wall.appendChild(el);
    const poster = stage.querySelector(".poster"), hint = stage.querySelector(".spinner");
    const amb = document.createElement("canvas"); amb.className = "amb"; amb.width = 48; amb.height = 27; stage.prepend(amb);
    const actx = amb.getContext("2d", { alpha: false }); let lastAmb = 0;
    const paintAmb = now => { const vv = vs.video; if (vv && vv.readyState >= 2 && now - lastAmb > 250) { lastAmb = now; try { actx.drawImage(vv, 0, 0, amb.width, amb.height); amb.style.opacity = "1"; } catch {} } if (tiles.has(c.id)) requestAnimationFrame(paintAmb); };
    requestAnimationFrame(paintAmb);
    const lay = () => { for (const x of [vs, poster]) applyToBox(stage, x, c.rotate, c.flipH, c.flipV); };
    const shown = () => { poster.style.opacity = "0"; hint.style.display = "none"; };
    const v = vs.video;
    if (v) { v.muted = true; v.controls = false; v.setAttribute("playsinline", "");
      v.addEventListener("playing", shown); v.addEventListener("timeupdate", () => { if (v.currentTime > 0) shown(); });
      v.addEventListener("waiting", () => { hint.style.display = ""; hint.textContent = "Reconnecting…"; }); }
    // belt and braces: some modes never fire "playing" on the inner element
    const poll = setInterval(() => { const vv = vs.video; if (vv && vv.readyState >= 3 && !vv.paused && vv.currentTime > 0) { shown(); } if (!tiles.has(c.id)) clearInterval(poll); }, 500);
    vs.src = `/stream/${c.id}/ws`;
    tiles.set(c.id, { el, vs, lay }); lay(); enableDrag(el);
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
