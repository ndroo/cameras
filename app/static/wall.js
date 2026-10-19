import { applyToBox } from "/static/orient.js";
const CAMS = JSON.parse(document.getElementById("data").textContent);
const $ = s => document.querySelector(s);
const wall = $("#wall"), colsSel = $("#cols");
let sel = new Set(CAMS.slice(0, 6).map(c => c.id)), cols = 0;
try { const s = JSON.parse(localStorage.getItem("wall:sel")); if (Array.isArray(s)) sel = new Set(s.filter(id => CAMS.some(c => c.id === id))); cols = +localStorage.getItem("wall:cols") || 0; } catch {}
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
function save() { try { localStorage.setItem("wall:sel", JSON.stringify([...sel])); localStorage.setItem("wall:cols", String(cols)); } catch {} }
const tiles = new Map();
function drawWall() {
  const chosen = CAMS.filter(c => sel.has(c.id));
  $("#wallEmpty").hidden = chosen.length > 0;
  const n = chosen.length, w = wall.clientWidth || innerWidth;
  const auto = w < 640 ? (n > 1 ? 2 : 1) : n <= 1 ? 1 : n <= 4 ? 2 : n <= 9 ? 3 : 4;
  wall.style.gridTemplateColumns = `repeat(${cols || auto}, minmax(0, 1fr))`;
  for (const [id, t] of tiles) if (!sel.has(id)) { t.vs.src = ""; t.el.remove(); tiles.delete(id); }
  for (const c of chosen) {
    if (tiles.has(c.id)) continue;
    const el = document.createElement("a"); el.className = "wallTile"; el.href = `/cam/${c.id}`;
    el.innerHTML = `<div class="stage"><img class="poster" src="/stream/${c.id}/thumb.jpg?t=${c.thumbAt}" alt=""><div class="spinner">Connecting…</div>
      <div class="tag"><span class="dot ${c.online === 1 ? "ok" : c.online === 0 ? "bad" : "unk"}"></span><b>${c.name}</b><span>${c.house || ""}</span></div></div>`;
    const stage = el.querySelector(".stage"), vs = document.createElement("video-stream"); vs.setAttribute("mode", "mse,hls,mp4,mjpeg"); vs.setAttribute("background", "true");
    stage.prepend(vs);
    wall.appendChild(el);
    const poster = stage.querySelector(".poster"), hint = stage.querySelector(".spinner");
    const lay = () => { for (const x of [vs, poster]) applyToBox(stage, x, c.rotate, c.flipH, c.flipV); };
    const shown = () => { poster.style.opacity = "0"; hint.style.display = "none"; };
    const v = vs.video;
    if (v) { v.muted = true; v.controls = false; v.setAttribute("playsinline", "");
      v.addEventListener("playing", shown); v.addEventListener("timeupdate", () => { if (v.currentTime > 0) shown(); });
      v.addEventListener("waiting", () => { hint.style.display = ""; hint.textContent = "Reconnecting…"; }); }
    // belt and braces: some modes never fire "playing" on the inner element
    const poll = setInterval(() => { const vv = vs.video; if (vv && vv.readyState >= 3 && !vv.paused && vv.currentTime > 0) { shown(); } if (!tiles.has(c.id)) clearInterval(poll); }, 500);
    vs.src = `/stream/${c.id}/ws`;
    tiles.set(c.id, { el, vs, lay }); lay();
    new ResizeObserver(lay).observe(stage);
  }
}
colsSel.addEventListener("change", () => { cols = +colsSel.value; save(); drawWall(); });
$("#fullBtn").addEventListener("click", () => { (wall.requestFullscreen || wall.webkitRequestFullscreen).call(wall); });
window.addEventListener("resize", () => { if (!cols) drawWall(); });
drawPick(); drawWall();
