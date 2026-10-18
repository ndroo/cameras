import { applyToBox } from "/static/orient.js";
const D = JSON.parse(document.getElementById("data").textContent);
const $ = s => document.querySelector(s);
const video = $("#clip"), stage = $("#player .stage"), hint = $("#hint"), label = $("#label"), save = $("#save"), status = $("#status");
const TZ = "America/Toronto";
const fmtT = t => new Date(t * 1000).toLocaleTimeString("en-CA", { timeZone: TZ, hour: "numeric", minute: "2-digit" });
const fmtTs = t => new Date(t * 1000).toLocaleTimeString("en-CA", { timeZone: TZ, hour: "numeric", minute: "2-digit", second: "2-digit" });
const fmtD = d => new Date(+d.slice(0, 4), +d.slice(4, 6) - 1, +d.slice(6, 8)).toLocaleDateString("en-CA", { weekday: "long", month: "long", day: "numeric" });

let state = { date: D.date, clips: D.clips, dayStart: D.dayStart, dayEnd: D.dayEnd, dates: new Set(D.dates), month: D.date.slice(0, 6),
              t: D.at || 0, segStart: 0, segEnd: 0, playing: false };

// ---------------------------------------------------------------- player orientation
function layout() {
  const rot = D.rotate, side = rot === 90 || rot === 270;
  const ar = (video.videoWidth && video.videoHeight) ? video.videoWidth / video.videoHeight : 16 / 9;
  const w = stage.clientWidth; let h = side ? w * ar : w / ar; h = Math.min(h, window.innerHeight - 200);
  stage.style.aspectRatio = "auto"; stage.style.height = h + "px"; applyToBox(stage, video, rot, D.flipH, D.flipV);
}
video.addEventListener("loadedmetadata", layout); window.addEventListener("resize", layout); layout();

// ---------------------------------------------------------------- footage coverage
const covered = t => state.clips.find(c => c.start <= t && t < c.end);
function nextCovered(t) { const c = state.clips.filter(c => c.end > t).sort((a, b) => a.start - b.start)[0]; return c ? Math.max(t, c.start) : null; }

function play(t) {
  const c = covered(t) || (nextCovered(t) !== null ? covered(nextCovered(t)) : null);
  if (!c) { status.textContent = "No footage after that time on this day."; return; }
  t = Math.max(t, c.start);
  state.t = t; state.segStart = t; state.segEnd = Math.min(c.end, t + D.maxClip);
  const url = `/stream/${D.camId}/clip.mp4?start=${state.segStart}&end=${state.segEnd}`;
  video.src = url; video.play().catch(() => {});
  save.href = url + "&download=1"; save.style.display = "";
  label.textContent = `${fmtT(state.segStart)} – ${fmtT(state.segEnd)}`;
  hint.textContent = "Fetching from the camera's card…"; hint.style.display = ""; status.textContent = "";
  history.replaceState(null, "", `/cam/${D.camId}/recordings?date=${state.date}&at=${t}`);
  draw();
}
video.addEventListener("playing", () => { hint.style.display = "none"; state.playing = true; });
video.addEventListener("waiting", () => { hint.textContent = "Buffering…"; hint.style.display = ""; });
video.addEventListener("timeupdate", () => { if (state.playing && !drag) { state.t = state.segStart + video.currentTime; draw(); } });
video.addEventListener("ended", () => { const n = nextCovered(state.segEnd + 1); if (n !== null && n < state.dayEnd) play(n); else status.textContent = "End of footage for this day."; });
video.addEventListener("error", () => { hint.textContent = "Couldn't load that part. Try another time."; hint.style.display = ""; });

// ---------------------------------------------------------------- timelines
const tracks = [
  { el: $("#track24"), range: () => [state.dayStart, state.dayEnd], ticks: 24, tickEvery: 1, labelEvery: 6 },
  { el: $("#track1"), range: () => { const a = Math.max(state.dayStart, Math.min(state.t - 1800, state.dayEnd - 3600)); return [a, a + 3600]; }, ticks: 60, tickEvery: 5, labelEvery: 15 },
];
function draw() {
  for (const tr of tracks) {
    const [a, b] = tr.range(), span = b - a;
    const segs = tr.el.querySelector(".segs"); segs.innerHTML = "";
    for (const c of state.clips) {
      const s = Math.max(a, c.start), e = Math.min(b, c.end); if (e <= s) continue;
      const d = document.createElement("div"); d.className = "seg" + (c.type === 2 ? " motion" : "");
      d.style.left = ((s - a) / span * 100) + "%"; d.style.width = ((e - s) / span * 100) + "%"; segs.appendChild(d);
    }
    const ticks = tr.el.querySelector(".ticks"); ticks.innerHTML = "";
    for (let i = 0; i <= tr.ticks; i += tr.tickEvery) {
      const k = document.createElement("div"); k.className = "tick" + (i % tr.labelEvery === 0 ? " major" : "");
      k.style.left = (i / tr.ticks * 100) + "%";
      if (i % tr.labelEvery === 0 && i < tr.ticks) k.dataset.l = fmtT(a + i / tr.ticks * span).replace(":00", "");
      ticks.appendChild(k);
    }
    const head = tr.el.querySelector(".head");
    const inside = state.t >= a && state.t <= b;
    head.style.display = inside ? "" : "none";
    head.style.left = ((state.t - a) / span * 100) + "%";
  }
  $("#tlTime").textContent = state.t ? fmtTs(state.t) : "";
  $("#dayTitle").textContent = fmtD(state.date);
}
let drag = null;
for (const tr of tracks) {
  const el = tr.el;
  const tAt = ev => { const r = el.getBoundingClientRect(); const [a, b] = tr.range(); const f = Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width)); return Math.round(a + f * (b - a)); };
  el.addEventListener("pointerdown", ev => { drag = tr; el.setPointerCapture(ev.pointerId); state.t = tAt(ev); draw(); ev.preventDefault(); });
  el.addEventListener("pointermove", ev => { if (drag !== tr) return; state.t = tAt(ev); draw(); });
  el.addEventListener("pointerup", ev => { if (drag !== tr) return; drag = null; play(tAt(ev)); });
  el.addEventListener("pointercancel", () => { drag = null; });
}

// ---------------------------------------------------------------- calendar
async function loadMonth(m) {
  state.month = m;
  try { const r = await fetch(`/cam/${D.camId}/recordings.json?month=${m}`); const j = await r.json(); state.dates = new Set(j.dates || []); } catch { state.dates = new Set(); }
  drawCal();
}
function drawCal() {
  const y = +state.month.slice(0, 4), mo = +state.month.slice(4, 6);
  $("#monthTitle").textContent = new Date(y, mo - 1, 1).toLocaleDateString("en-CA", { month: "long", year: "numeric" });
  const g = $("#calGrid"); g.innerHTML = "";
  for (const w of ["S", "M", "T", "W", "T", "F", "S"]) { const h = document.createElement("div"); h.className = "dow"; h.textContent = w; g.appendChild(h); }
  const first = new Date(y, mo - 1, 1).getDay(), days = new Date(y, mo, 0).getDate();
  for (let i = 0; i < first; i++) g.appendChild(document.createElement("div"));
  const today = new Date().toLocaleDateString("en-CA", { timeZone: TZ }).replace(/-/g, "");
  for (let d = 1; d <= days; d++) {
    const key = `${y}${String(mo).padStart(2, "0")}${String(d).padStart(2, "0")}`;
    const b = document.createElement("button"); b.type = "button"; b.textContent = d;
    b.className = "day" + (state.dates.has(key) ? " has" : "") + (key === state.date ? " sel" : "") + (key === today ? " today" : "");
    if (key > today) b.disabled = true;
    b.addEventListener("click", () => loadDay(key));
    g.appendChild(b);
  }
}
async function loadDay(key) {
  status.textContent = "Loading day…";
  try {
    const r = await fetch(`/cam/${D.camId}/recordings.json?date=${key}`); if (!r.ok) throw new Error((await r.json()).detail || r.status);
    const j = await r.json();
    state.date = key; state.clips = j.clips; state.dayStart = j.dayStart; state.dayEnd = j.dayEnd; state.t = j.dayStart; state.playing = false;
    video.pause(); video.removeAttribute("src"); video.load(); save.style.display = "none"; label.textContent = "–";
    hint.textContent = state.clips.length ? "Drag the timeline to a time" : "No footage on this day"; hint.style.display = "";
    status.textContent = ""; history.replaceState(null, "", `/cam/${D.camId}/recordings?date=${key}`);
    if (key.slice(0, 6) !== state.month) await loadMonth(key.slice(0, 6)); else drawCal();
    draw();
  } catch (e) { status.textContent = "Couldn't load that day: " + e.message; }
}
$("#prevM").addEventListener("click", () => { const y = +state.month.slice(0, 4), m = +state.month.slice(4, 6); const d = new Date(y, m - 2, 1); loadMonth(`${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}`); });
$("#nextM").addEventListener("click", () => { const y = +state.month.slice(0, 4), m = +state.month.slice(4, 6); const d = new Date(y, m, 1); loadMonth(`${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}`); });

// ---------------------------------------------------------------- start
drawCal(); 
if (!state.t) { const last = state.clips.length ? state.clips[state.clips.length - 1] : null; state.t = last ? Math.max(last.start, last.end - 60) : state.dayStart; }
draw();
if (D.at) play(D.at);
