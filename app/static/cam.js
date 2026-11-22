import { applyToBox } from "/static/orient.js";
const D = JSON.parse(document.getElementById("data").textContent);
const $ = s => document.querySelector(s);
const TZ = D.tz;
const live = $("#live"), clip = $("#clip"), stage = $("#stage"), poster = stage.querySelector(".poster"), hint = $("#hint");
const modeBtn = $("#modeBtn"), modeTxt = $("#modeTxt"), label = $("#label"), status = $("#status");
const fmtT = t => new Date(t * 1000).toLocaleTimeString("en-CA", { timeZone: TZ, hour: "numeric", minute: "2-digit" });
const fmtTs = t => new Date(t * 1000).toLocaleTimeString("en-CA", { timeZone: TZ, hour: "numeric", minute: "2-digit", second: "2-digit" });
const ymd = d => d.toLocaleDateString("en-CA", { timeZone: TZ }).replace(/-/g, "");
const fmtD = d => new Date(+d.slice(0, 4), +d.slice(4, 6) - 1, +d.slice(6, 8)).toLocaleDateString("en-CA", { weekday: "long", month: "long", day: "numeric" });
const shortT = t => { const p = new Intl.DateTimeFormat("en-CA", { timeZone: TZ, hour: "numeric", minute: "2-digit", hour12: true }).formatToParts(new Date(t * 1000));
  const h = p.find(x => x.type === "hour").value, m = p.find(x => x.type === "minute").value, ap = (p.find(x => x.type === "dayPeriod") || {}).value || "";
  return m === "00" ? `${h} ${ap}` : `${h}:${m}`; };
const now = () => Math.floor(Date.now() / 1000);
const today = () => ymd(new Date());

let o = { rotate: D.rotate, flip_h: D.flipH, flip_v: D.flipV };
if (!D.isAdmin) { try { const s = JSON.parse(localStorage.getItem("orient:" + D.camId)); if (s) o = s; } catch {} }
const RANGES = { "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400 };
let range0 = new URLSearchParams(location.search).get("range") || (D.at ? "day" : null);
if (!range0) { try { range0 = localStorage.getItem("range") || "day"; } catch { range0 = "day"; } }
if (range0 !== "day" && !RANGES[range0]) range0 = "day";
const S = { mode: "live", range: range0, date: D.date, from: D.dayStart, to: D.dayEnd, clips: [], events: [], dates: new Set(), month: D.date.slice(0, 6),
            t: now(), segStart: 0, segEnd: 0, playing: false, loaded: false, priv: D.privacy, privacy: [] };

// ---------------------------------------------------------------- player
const wrap = $("#camWrap");
let knownAr = 16 / 9; try { knownAr = +localStorage.getItem("ar:" + D.camId) || knownAr; } catch {}
function noteAr(v) { if (v && v.videoWidth && v.videoHeight) { const ar = v.videoWidth / v.videoHeight; if (Math.abs(ar - knownAr) > 0.01) { knownAr = ar; try { localStorage.setItem("ar:" + D.camId, ar); } catch {} layout(); } } }
const isFull = () => (document.fullscreenElement || document.webkitFullscreenElement) === $("#player");
function layout() {
  const side = o.rotate === 90 || o.rotate === 270;
  const ar0 = knownAr;
  const ar = side ? 1 / ar0 : ar0;                       // shape of the picture as shown
  // size the whole column to the picture: as wide as the page allows, but no taller than ~56% of the window
  // fullscreen: the stage fills whatever the controls bar leaves (CSS flex), no fixed height
  if (isFull()) { stage.style.aspectRatio = "auto"; stage.style.height = ""; for (const el of [live, clip, poster]) applyToBox(stage, el, o.rotate, o.flip_h, o.flip_v); return; }
  const maxW = wrap.parentElement.clientWidth, maxH = window.innerHeight * 0.56;
  const w = Math.min(maxW, Math.max(320, maxH * ar)), h = w / ar;
  wrap.style.maxWidth = (window.innerWidth >= 1000 ? Math.round(w) + "px" : "");
  stage.style.aspectRatio = "auto"; stage.style.height = Math.round(window.innerWidth >= 1000 ? h : stage.clientWidth / ar) + "px";
  for (const el of [live, clip, poster]) applyToBox(stage, el, o.rotate, o.flip_h, o.flip_v);
}
async function saveOrient() {
  if (D.isAdmin) { const fd = new FormData(); fd.set("rotate", o.rotate); fd.set("flip_h", o.flip_h); fd.set("flip_v", o.flip_v); fetch(`/cam/${D.camId}/orientation`, { method: "POST", body: fd }); }
  else { try { localStorage.setItem("orient:" + D.camId, JSON.stringify(o)); } catch {} }
}
// ---------------------------------------------------------------- sound (on by default, remembered; browsers may force a muted start)
// the button toggles sound; turning it back on returns to the last volume. The slider sets
// the level (dragging to zero mutes without forgetting the level).
const soundBtn = $("#soundBtn"), volEl = $("#vol");
let sound = true, vol = 0.8;
try { sound = localStorage.getItem("sound") !== "0"; const x = parseFloat(localStorage.getItem("vol")); if (x > 0 && x <= 1) vol = x; } catch {}
const activeVideo = () => S.mode === "live" ? live.video : clip;
function saveSound() { try { localStorage.setItem("sound", sound ? "1" : "0"); localStorage.setItem("vol", String(vol)); } catch {} }
function applySound() { for (const v of [live.video, clip]) if (v) { v.volume = vol; v.muted = !sound; } }
function drawSound() {
  const v = activeVideo(), on = !!v && !v.muted, blocked = sound && !on;
  soundBtn.textContent = !on ? "🔇" : vol < 0.34 ? "🔈" : vol < 0.67 ? "🔉" : "🔊";
  soundBtn.classList.toggle("on", on); soundBtn.classList.toggle("attn", blocked);
  soundBtn.title = on ? "Mute" : blocked ? "Tap for sound (the browser started it muted)" : "Turn sound on";
  if (document.activeElement !== volEl) volEl.value = sound ? Math.round(vol * 100) : 0;   // forced-muted start still shows the level
  volEl.classList.toggle("off", !on); volEl.style.setProperty("--p", volEl.value + "%");
}
function setSound(on) {
  sound = on; saveSound(); applySound();
  const v = activeVideo(); if (on && v) v.play().catch(() => {});
  drawSound();
}
volEl.addEventListener("input", () => {
  const x = +volEl.value / 100;
  if (x > 0) vol = x;
  sound = x > 0; saveSound(); applySound();
  const v = activeVideo(); if (sound && v) v.play().catch(() => {});
  drawSound();
});
volEl.addEventListener("change", drawSound);
for (const v of [live.video, clip]) if (v) { v.volume = vol; v.addEventListener("volumechange", drawSound); }

function showHint(txt) { hint.innerHTML = '<i class="spin"></i>' + txt; hint.style.display = ""; }
function hideHint() { hint.style.display = "none"; }

// Blanking live.src only forgets the address: the websocket stays open and the stream
// keeps playing (audible now there's sound) and holding a camera session while the card
// plays back, which the cameras can't always serve at once. Disconnect for real.
function stopLive() {
  live.src = "";
  live.ondisconnect();
  if (live.video) live.video.pause();
}

function goLive() {
  S.mode = "live"; S.playing = false;
  clip.pause(); clearClip(); clip.hidden = true;
  if (S.priv) return showPrivacy();
  if (live.video) { live.video.volume = vol; live.video.muted = !sound; }
  live.style.display = ""; live.src = `/stream/${D.camId}/ws`;
  modeBtn.classList.add("live"); modeTxt.textContent = "LIVE"; label.hidden = true; speed.hidden = true;
  poster.style.opacity = ".6"; showHint("Connecting to camera…"); status.textContent = "";
  S.t = now(); keepInView(S.t); setURL(); layout(); draw(); showPtz(); showSeek();
}
// ---------------------------------------------------------------- privacy mode
const privOverlay = $("#privOverlay");
function showPrivacy() {
  stopLive(); live.style.display = "none"; hideHint(); poster.style.opacity = "0"; privOverlay.hidden = false;
  $("#privTxt").textContent = `Until ${fmtT(S.priv.until)} · turned on by ${S.priv.by}`;
  $("#privResume").hidden = !S.priv.canResume;
  modeBtn.classList.add("live"); modeTxt.textContent = "LIVE"; label.hidden = true; speed.hidden = true; status.textContent = "";
  S.t = now(); keepInView(S.t); setURL(); layout(); draw(); showPtz(); showSeek();
}
async function privacyPost(fd) {
  const r = await fetch(`/cam/${D.camId}/privacy`, { method: "POST", body: fd });
  if (!r.ok) { let m = "Something went wrong."; try { const j = await r.json(); m = j.error || j.detail || m; } catch {} throw new Error(m); }
  location.replace(`/cam/${D.camId}`);
}
if (S.priv) setTimeout(() => location.replace(`/cam/${D.camId}`), Math.max(1, S.priv.until - now() + 3) * 1000);   // comes back on by itself
$("#privResume").addEventListener("click", ev => { ev.target.disabled = true; const fd = new FormData(); fd.set("resume", "1");
  privacyPost(fd).catch(e => { ev.target.disabled = false; $("#privTxt").textContent = e.message; }); });
if (D.canPause) {
  const btn = $("#privBtn"), modal = $("#privModal"), err = $("#privErr");
  btn.hidden = false; if (S.priv) btn.classList.add("active");
  btn.addEventListener("click", () => { err.hidden = true; $("#privGo").textContent = S.priv ? "Change privacy time" : "Turn on privacy"; modal.hidden = false; });
  $("#privClose").addEventListener("click", () => { modal.hidden = true; });
  modal.addEventListener("click", ev => { if (ev.target === modal) modal.hidden = true; });
  $("#privGo").addEventListener("click", ev => { ev.target.disabled = true; const fd = new FormData(); fd.set("hours", $("#privHours").value);
    privacyPost(fd).catch(e => { ev.target.disabled = false; err.textContent = e.message; err.hidden = false; }); });
}

// ---------------------------------------------------------------- card footage source
// iPhones only play a plain <video src> from servers that answer byte-range requests,
// and card footage is made on the fly (no length), so there it never played. Where the
// browser has Apple's ManagedMediaSource (iPhone, Mac Safari) - or ?mse=1 to test - the
// page fetches the fragmented MP4 itself and feeds it in, like live view does.
const useMSE = "ManagedMediaSource" in window || new URLSearchParams(location.search).has("mse");
let clipMSE = null;          // {abort} for the feed in progress
function report(kind, detail) {
  try { navigator.sendBeacon("/client-log", JSON.stringify(Object.assign({ kind, cam: D.camId, ua: navigator.userAgent, mse: useMSE }, detail))); } catch {}
}
function clearClip() {
  if (clipMSE) { clipMSE.abort(); clipMSE = null; }
  clip.removeAttribute("src"); clip.srcObject = null; clip.load();
}
function findBox(buf, type) {          // offset of a 4-char box type in the bytes, or -1
  const t = [...type].map(c => c.charCodeAt(0));
  for (let i = 4; i + 4 <= buf.length; i++) if (buf[i] === t[0] && buf[i + 1] === t[1] && buf[i + 2] === t[2] && buf[i + 3] === t[3]) return i;
  return -1;
}
function codecsOf(head) {              // from the init segment: the real H.264 profile/level, plus AAC if present
  if (findBox(head, "moof") < 0) return null;      // wait until the whole moov has arrived
  const a = findBox(head, "avcC"); if (a < 0) return null;
  const hex = n => n.toString(16).padStart(2, "0");
  return `avc1.${hex(head[a + 5])}${hex(head[a + 6])}${hex(head[a + 7])}` + (findBox(head, "mp4a") >= 0 ? ",mp4a.40.2" : "");
}
function setClip(url) {
  clearClip();
  if (!useMSE) { clip.src = url; return; }
  const MS = window.ManagedMediaSource || window.MediaSource, ms = new MS(), ac = new AbortController();
  clipMSE = ac;
  if (window.ManagedMediaSource) { clip.disableRemotePlayback = true; clip.srcObject = ms; } else clip.src = URL.createObjectURL(ms);
  ms.addEventListener("sourceopen", async () => {
    let sb = null, head = new Uint8Array(0), done = false;
    const queue = [];
    const feed = () => {
      if (!sb || sb.updating) return;
      const b = sb.buffered;                     // keep memory bounded on long clips
      if (b.length && clip.currentTime - b.start(0) > 60) { try { sb.remove(b.start(0), clip.currentTime - 20); return; } catch {} }
      if (queue.length) { try { sb.appendBuffer(queue[0]); queue.shift(); } catch (e) { if (e.name !== "QuotaExceededError") throw e; setTimeout(feed, 1000); } }
      else if (done && ms.readyState === "open") { try { ms.endOfStream(); } catch {} }
    };
    try {
      const r = await fetch(url, { signal: ac.signal });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const rd = r.body.getReader();
      for (;;) {
        const { done: end, value } = await rd.read();
        if (end) break;
        if (!sb) {
          const h = new Uint8Array(head.length + value.length); h.set(head); h.set(value, head.length); head = h;
          const codecs = codecsOf(head); if (!codecs) continue;
          sb = ms.addSourceBuffer(`video/mp4; codecs="${codecs}"`);
          sb.addEventListener("updateend", feed);
          queue.push(head);
        } else queue.push(value);
        feed();
      }
      done = true; feed();
    } catch (e) {
      if (e.name === "AbortError" || ac.signal.aborted) return;
      report("clip-mse", { message: String(e) });
      clip.dispatchEvent(new Event("error"));
    }
  }, { once: true });
}

const covered = t => S.clips.find(c => c.start <= t && t < c.end);
const hasNow = () => S.to >= now() - 60;
function snap(t) {
  // nearest moment that has footage; "now" counts as footage when the range reaches today
  if (covered(t)) return t;
  const cands = [];
  for (const c of S.clips) { cands.push(c.start); cands.push(c.end - 1); }
  if (hasNow()) cands.push(now());
  if (!cands.length) return t;
  return cands.reduce((best, x) => Math.abs(x - t) < Math.abs(best - t) ? x : best, cands[0]);
}
const nextCovered = t => { const c = S.clips.filter(c => c.end > t).sort((a, b) => a.start - b.start)[0]; return c ? Math.max(t, c.start) : null; };
function setURL() {
  const q = new URLSearchParams();
  if (S.range === "day") q.set("date", S.date); else q.set("range", S.range);
  if (S.mode === "play") q.set("at", Math.round(S.t));
  history.replaceState(null, "", `/cam/${D.camId}?${q}`);
}
function myJobOn(cam) { return DL.jobs.find(j => j.cam_id === cam && j.state === "saving"); }

function play(t) {
  if (hasNow() && t >= now() - 30) return goLive();
  const n = covered(t) ? t : nextCovered(t);
  if (n === null) { status.textContent = "No footage after that time in this range."; S.t = t; draw(); return; }
  const c = covered(n); t = n;
  const job = myJobOn(D.camId);
  if (job) { status.textContent = `The camera is busy saving your download (${job.pct}%). Playback works again when it's done.`; S.t = t; draw(); return; }
  S.mode = "play"; S.t = t; S.segStart = t; S.segEnd = Math.min(c.end, t + D.maxClip); S.playing = false;
  stopLive(); live.style.display = "none"; clip.hidden = false; privOverlay.hidden = true; showPtz(); showSeek();
  const url = `/stream/${D.camId}/clip.mp4?start=${S.segStart}&end=${S.segEnd}&speed=${speed.value}`;
  clip.volume = vol; clip.muted = !sound; setClip(url); clip.playbackRate = +speed.value;
  clip.play().catch(() => { if (!clip.muted) { clip.muted = true; clip.play().catch(() => {}); } });   // no tap yet on this page: start muted
  speed.hidden = false;
  modeBtn.classList.remove("live"); modeTxt.innerHTML = '<span class="wideOnly">Go live</span><span class="narrowOnly">Live</span>'; label.hidden = false; label.textContent = fmtTs(t);
  poster.style.opacity = "0"; showHint("Fetching from the camera's card…"); status.textContent = "";
  keepInView(t); setURL(); layout(); draw();
}
// skip within what's already loaded when we can; otherwise ask the card again from there
function seekTo(t) {
  if (S.mode === "play" && t >= S.segStart && t < S.segEnd) {
    const off = t - S.segStart, b = clip.buffered;
    for (let i = 0; i < b.length; i++) if (off >= b.start(i) && off <= b.end(i)) { clip.currentTime = off; S.t = t; draw(); return; }
  }
  play(Math.round(t));
}
const visibleEvents = () => S.events.filter(e => !hiddenKinds.has(kindOf(e)[1]));
function jumpEvent(dir) {
  const ev = visibleEvents().slice().sort((a, b) => a.start - b.start);
  const cur = S.mode === "live" ? now() : S.t;
  const e = dir > 0 ? ev.find(e => e.start > cur + 1) : ev.filter(e => e.start < cur - 4).pop();
  if (e) play(Math.max(S.from, e.start - 3)); else status.textContent = dir > 0 ? "No later detections in this range." : "No earlier detections in this range.";
}
const seekBtns = $("#seekBtns");
function showSeek() { seekBtns.classList.toggle("playing", S.mode === "play"); }
seekBtns.addEventListener("click", ev => {
  const b = ev.target.closest("button[data-seek]"); if (!b) return;
  const k = b.dataset.seek, cur = S.mode === "live" ? now() : S.t;
  if (k === "prev") jumpEvent(-1); else if (k === "next") jumpEvent(1); else seekTo(cur + +k);
});
clip.addEventListener("playing", () => { hideHint(); S.playing = true; });
clip.addEventListener("waiting", () => showHint("Buffering…"));
clip.addEventListener("loadedmetadata", () => noteAr(clip));
clip.addEventListener("timeupdate", () => { if (S.mode === "play" && S.playing && !drag) { S.t = S.segStart + clip.currentTime; label.textContent = fmtTs(S.t); keepInView(S.t); draw(); } });
clip.addEventListener("ended", () => { const n = nextCovered(S.segEnd + 1); if (n !== null && n < S.to) play(n); else if (hasNow()) goLive(); else status.textContent = "End of footage in this range."; });
// the camera serves one playback at a time and can be briefly busy (another viewer, or
// one just abandoned): retry a couple of times before giving up
let clipRetries = 0;
clip.addEventListener("error", () => {
  if (S.mode !== "play" || !(clip.getAttribute("src") || clip.srcObject || clipMSE)) return;
  report("clip-error", { message: clip.error ? clip.error.message : "" });
  if (clipRetries < 2) { clipRetries++; showHint("Camera busy, retrying…"); setTimeout(() => { if (S.mode === "play") play(Math.round(S.t)); }, 2500); }
  else showHint("Couldn't load that part. The camera may be busy; try again in a moment.");
});
clip.addEventListener("playing", () => { clipRetries = 0; });
if (live.video) {
  const v = live.video; v.setAttribute("playsinline", ""); v.muted = !sound; v.controls = false;
  v.addEventListener("playing", () => { if (S.mode === "live") { hideHint(); poster.style.opacity = "0"; layout(); } });
  v.addEventListener("loadedmetadata", () => noteAr(v));
  v.addEventListener("waiting", () => { if (S.mode === "live") showHint("Reconnecting…"); });
}
modeBtn.addEventListener("click", () => { if (S.mode !== "live") goLive(); });
$("#moreBtn").addEventListener("click", ev => {     // phones: rotate/mirror/flip/still/privacy live in a second row
  const c = $("#player .controls"), on = c.classList.toggle("more"); ev.currentTarget.setAttribute("aria-expanded", on);
});
$("#player .controls").addEventListener("click", ev => {
  const b = ev.target.closest("button[data-act]"); if (!b) return;
  const a = b.dataset.act;
  if (a === "sound") { const v = activeVideo(); setSound(!(v && !v.muted)); }
  if (a === "rotate") { o.rotate = (o.rotate + 90) % 360; layout(); saveOrient(); }
  if (a === "flip_h") { o.flip_h = o.flip_h ? 0 : 1; layout(); saveOrient(); }
  if (a === "flip_v") { o.flip_v = o.flip_v ? 0 : 1; layout(); saveOrient(); }
  if (a === "full") { const p = $("#player"); (p.requestFullscreen || p.webkitRequestFullscreen).call(p); }
  if (a === "snap") {
    const v = S.mode === "live" ? live.video : clip; if (!v || !v.videoWidth) return;
    const c = document.createElement("canvas"); c.width = v.videoWidth; c.height = v.videoHeight; c.getContext("2d").drawImage(v, 0, 0);
    const a2 = document.createElement("a"); a2.download = `${D.name}-${new Date().toISOString().replace(/[:.]/g, "-")}.jpg`.replace(/\s+/g, "_"); a2.href = c.toDataURL("image/jpeg", 0.92); a2.click();
  }
});
window.addEventListener("resize", layout); document.addEventListener("fullscreenchange", layout); document.addEventListener("webkitfullscreenchange", layout);

// ---------------------------------------------------------------- pan / tilt (cameras with a motor, live view only)
// ---------------------------------------------------------------- garage door (admin-only, one momentary press = toggle)
const garageBtn = $("#garageBtn");
if (garageBtn && D.opener) {
  garageBtn.hidden = false;
  const txt = $("#garageTxt");
  const base = D.opener.name || "Garage";
  txt.textContent = base;
  let busy = false;
  garageBtn.addEventListener("click", async () => {
    if (busy) return;
    if (!confirm(`Open / close "${base}"? The door will move.`)) return;
    busy = true; garageBtn.classList.add("busy"); txt.textContent = "Working…";
    try {
      const r = await fetch(`/garage/${D.opener.id}/press`, { method: "POST", credentials: "same-origin" });
      const j = await r.json().catch(() => ({}));
      if (!r.ok || !j.ok) throw new Error(j.error || `error ${r.status}`);
      garageBtn.classList.remove("busy"); garageBtn.classList.add("ok"); txt.textContent = "Sent ✓";
      setTimeout(() => { garageBtn.classList.remove("ok"); txt.textContent = base; busy = false; }, 2500);
    } catch (e) {
      garageBtn.classList.remove("busy"); garageBtn.classList.add("bad"); txt.textContent = "Failed";
      garageBtn.title = String(e.message || e);
      setTimeout(() => { garageBtn.classList.remove("bad"); txt.textContent = base; busy = false; }, 4000);
    }
  });
}

const ptz = $("#ptz"), PTZ_STEP = 10;
function camVector(dir) {
  // screen direction -> camera pan/tilt, undoing the display rotation and flips so "up" moves the picture up
  const [sx, sy] = { up: [0, -1], down: [0, 1], left: [-1, 0], right: [1, 0] }[dir];   // screen y grows downward
  const a = -o.rotate * Math.PI / 180, c = Math.round(Math.cos(a)), s = Math.round(Math.sin(a));
  let x = sx * c - sy * s, y = sx * s + sy * c;
  if (o.flip_h) x = -x;
  if (o.flip_v) y = -y;
  return [x * PTZ_STEP, -y * PTZ_STEP];                  // camera: pan right positive, tilt up positive
}
let ptzBusy = false, ptzHold = null;
async function ptzMove(dir, btn) {
  if (ptzBusy) return;
  ptzBusy = true; btn.classList.add("busy"); ptz.classList.remove("err");
  const [x, y] = camVector(dir), fd = new FormData(); fd.set("x", x); fd.set("y", y);
  try { const r = await fetch(`/cam/${D.camId}/ptz`, { method: "POST", body: fd }); if (!r.ok) throw 0; }
  catch { ptz.classList.add("err"); setTimeout(() => ptz.classList.remove("err"), 1500); }
  finally { ptzBusy = false; btn.classList.remove("busy"); }
}
function showPtz() { ptz.hidden = !(D.ptz && S.mode === "live" && !S.priv); }
if (D.ptz) {
  stage.classList.add("hasPtz");
  const stop = () => { if (ptzHold) { clearInterval(ptzHold); ptzHold = null; } };
  ptz.addEventListener("pointerdown", ev => {
    const b = ev.target.closest("button[data-dir]"); if (!b) return;
    ev.preventDefault(); stop();
    const go = () => ptzMove(b.dataset.dir, b);
    go(); ptzHold = setInterval(go, 350);                 // hold to keep turning
  });
  for (const t of ["pointerup", "pointercancel", "pointerleave"]) ptz.addEventListener(t, stop);
  window.addEventListener("blur", stop);
  ptz.addEventListener("contextmenu", ev => ev.preventDefault());
  $("#caliBtn").addEventListener("click", async () => {
    if (ptz.classList.contains("calibrating")) return;
    if (!confirm("Calibrate this camera? It will sweep through its whole pan and tilt range (about 30 seconds) to re-find its position, then come back.")) return;
    ptz.classList.add("calibrating"); status.textContent = "Calibrating: the camera is sweeping its range…";
    try {
      const r = await fetch(`/cam/${D.camId}/calibrate`, { method: "POST" }), j = await r.json().catch(() => ({}));
      if (!r.ok || !j.ok) throw new Error(j.error || "Calibration failed.");
      setTimeout(() => { ptz.classList.remove("calibrating"); status.textContent = "Calibrated. Use the arrows to aim it if needed."; }, 30000);
    } catch (e) { ptz.classList.remove("calibrating"); status.textContent = e.message; }
  });
}

// ---------------------------------------------------------------- timeline (a zoomable bar over a day, 24 h, 7 or 30 days)
const KIND = { motion: ["#ff9f0a", "motion"], person: ["#34c759", "person"], ring: ["#bf5af2", "doorbell"], "missed doorbell ring": ["#bf5af2", "doorbell"],
  "doorbell ring answered": ["#bf5af2", "doorbell"], vehicle: ["#5ac8fa", "vehicle"], "line crossing": ["#ff6b9d", "line crossing"], intrusion: ["#ff6b9d", "intrusion"], "baby cry": ["#ffd60a", "sound"], sound: ["#ffd60a", "sound"], pet: ["#ffd60a", "pet"], tamper: ["#ff453a", "tamper"], "package delivered": ["#ff375f", "package"], "package picked up": ["#ff375f", "package"] };
const kindOf = e => { for (const l of e.labels) if (KIND[l]) return KIND[l]; return ["#8b93a3", e.labels[0] || "event"]; };
const hiddenKinds = new Set();
const track = $("#track24"), bubble = track.querySelector(".bubble"), mini = $("#mini");
const multiDay = () => S.to - S.from > 26 * 3600;
const fmtDay = t => new Date(t * 1000).toLocaleDateString("en-CA", { timeZone: TZ, weekday: "short", day: "numeric" });
const fmtWhen = (t, secs) => (multiDay() ? new Date(t * 1000).toLocaleDateString("en-CA", { timeZone: TZ, weekday: "short", month: "short", day: "numeric" }) + ", " : "") + (secs ? fmtTs(t) : fmtT(t));
// local midnight before t (in the cameras' time zone)
function midnight(t) {
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-CA", { timeZone: TZ, hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" }).formatToParts(new Date(t * 1000)).map(x => [x.type, x.value]));
  return t - ((+p.hour % 24) * 3600 + +p.minute * 60 + +p.second);
}
let view = null;   // [a, b] visible window; null = whole range
const vis = () => view || [S.from, S.to];
function setView(a, b) {
  const full = S.to - S.from, span = Math.max(60, Math.min(full, b - a));
  a = Math.max(S.from, Math.min(a, S.to - span)); b = a + span;
  view = span >= full ? null : [a, b];
  draw();
}
function zoomAt(factor, t) {
  const [a, b] = vis(), span = b - a, ns = span / factor, f = (t - a) / span;
  setView(t - f * ns, t - f * ns + ns);
}
function zoomTo(span) {
  if (span === "all") return setView(S.from, S.to);
  const [a, b] = vis(), c = (S.mode === "live" ? Math.min(now(), S.to) : S.t);
  const mid = c >= a && c <= b ? c : (a + b) / 2;
  setView(mid - span * (S.mode === "live" ? 0.85 : 0.5), mid + span * (S.mode === "live" ? 0.15 : 0.5));
}
function keepInView(t) {
  if (!view) return; const [a, b] = view, span = b - a;
  if (t < a || t > b) setView(t - span / 2, t + span / 2);
  else if (t > b - span * 0.08) setView(t - span * 0.9, t + span * 0.1);
}
// [minor step, major step] in seconds for a visible span; major ticks carry a label
const STEPS = [[10, 60], [30, 120], [60, 300], [300, 900], [600, 1800], [900, 3600], [1800, 3 * 3600], [3600, 3 * 3600], [3600, 6 * 3600], [3 * 3600, 12 * 3600], [6 * 3600, 86400], [86400, 2 * 86400], [86400, 7 * 86400]];
function tickPlan(span, px) {
  const want = Math.max(4, Math.min(12, px / 85));            // about one label per 85 px
  for (const st of STEPS) if (span / st[1] <= want) return st;
  return STEPS[STEPS.length - 1];
}
function draw() {
  const nowT = now(), [a, b] = vis(), span = b - a, X = t => ((t - a) / span * 100) + "%", W = (s, e) => ((e - s) / span * 100) + "%";
  // alternate day bands on multi-day views, so you can tell the days apart at a glance
  const days = track.querySelector(".days"); days.innerHTML = "";
  if (span > 30 * 3600) for (let m = midnight(a); m < b; m = midnight(m + 30 * 3600)) {
    const n = midnight(m + 30 * 3600);
    if (Math.round((m - S.from) / 86400) % 2) { const d = document.createElement("div"); d.className = "dayBand"; d.style.left = X(Math.max(a, m)); d.style.width = W(Math.max(a, m), Math.min(b, n)); days.appendChild(d); }
  }
  const segs = track.querySelector(".segs"); segs.innerHTML = "";
  const px = span / Math.max(1, track.clientWidth);              // seconds per pixel: merge slivers closer than that
  let run = null;
  const flush = () => { if (!run) return; const d = document.createElement("div"); d.className = "seg"; d.style.left = X(run[0]); d.style.width = `max(1px, ${W(run[0], run[1])})`; segs.appendChild(d); run = null; };
  for (const c of S.clips) { const s = Math.max(a, c.start), e = Math.min(b, c.end); if (e <= s) continue;
    if (run && s - run[1] <= px * 2) run[1] = Math.max(run[1], e); else { flush(); run = [s, e]; } }
  flush();
  for (const [ps, pe0] of S.privacy) { const pe = S.priv && pe0 >= nowT - 120 ? Math.max(pe0, nowT) : pe0, s = Math.max(a, ps), e = Math.min(b, pe); if (e <= s) continue;
    const d = document.createElement("div"); d.className = "seg priv"; d.title = `privacy mode ${fmtT(ps)} – ${fmtT(pe)}`; d.style.left = X(s); d.style.width = W(s, e); segs.appendChild(d); }
  // detections on their own lane above the footage; one marker per kind per couple of pixels
  const marks = track.querySelector(".marks"); marks.innerHTML = "";
  const seen = new Set();
  for (const e of visibleEvents()) { if (e.start < a || e.start > b) continue;
    const [col, name] = kindOf(e), key = name + ":" + Math.round((e.start - a) / (px * 3)); if (seen.has(key)) continue; seen.add(key);
    const m = document.createElement("div"); m.className = "mark"; m.style.left = X(e.start); m.style.background = col; marks.appendChild(m); }
  const ticks = track.querySelector(".ticks"); ticks.innerHTML = "";
  const [minor, major] = tickPlan(span, track.clientWidth);
  const base = midnight(a);
  const lbl = t => (major >= 86400 || (t - midnight(t)) < 60) && span > 20 * 3600 ? fmtDay(t) : shortT(t);
  if (minor >= 86400) {
    for (let t = midnight(a + 3600 * 23); t <= b; t = midnight(t + 30 * 3600)) {
      if (t < a) continue;
      const k = document.createElement("div"), isMajor = Math.round((t - base) / 86400) % (major / 86400) === 0;
      k.className = "tick" + (isMajor ? " major" : ""); k.style.left = X(t); if (isMajor) k.dataset.l = fmtDay(t); ticks.appendChild(k);
    }
  } else {
    for (let t = base + Math.ceil((a - base) / minor) * minor; t <= b; t += minor) {
      const isMajor = (t - base) % major === 0, isMid = span > 20 * 3600 && (t - midnight(t + 1)) < 60;
      const k = document.createElement("div"); k.className = "tick" + (isMajor || isMid ? " major" : "") + (isMid ? " midnight" : ""); k.style.left = X(t);
      if ((isMajor || isMid) && t < b) k.dataset.l = lbl(t);
      ticks.appendChild(k);
    }
  }
  const selEl = track.querySelector(".sel");
  if (cut.on && cut.a && cut.b) { const s1 = Math.max(a, Math.min(cut.a, cut.b)), e1 = Math.min(b, Math.max(cut.a, cut.b)); selEl.style.display = e1 > s1 ? "" : "none"; selEl.style.left = X(s1); selEl.style.width = W(s1, e1); }
  else selEl.style.display = "none";
  const nowEl = track.querySelector(".now"); const showNow = nowT >= a && nowT <= b; nowEl.style.display = showNow ? "" : "none"; if (showNow) nowEl.style.left = X(nowT);
  const future = track.querySelector(".future") || Object.assign(track.appendChild(document.createElement("div")), { className: "future" });
  if (nowT < b) { future.style.display = ""; future.style.left = X(Math.max(a, nowT)); } else future.style.display = "none";
  const head = track.querySelector(".head"); const ht = S.mode === "live" ? Math.min(nowT, S.to) : S.t, inside = ht >= a && ht <= b; head.style.display = inside ? "" : "none"; head.style.left = X(ht);
  head.classList.toggle("isLive", S.mode === "live");
  $("#tlTime").textContent = (S.mode === "live" ? "Live · " : "") + fmtWhen(S.mode === "live" ? nowT : S.t, true);
  // zoom buttons: highlight the current zoom, hide ones wider than the range
  for (const zb of $("#zoomBtns").children) { const z = zb.dataset.z; zb.hidden = z !== "all" && +z >= S.to - S.from; zb.classList.toggle("active", z === "all" ? !view : !!view && Math.abs(span - +z) < 2); }
  $("#panL").disabled = !view || a <= S.from; $("#panR").disabled = !view || b >= S.to;
  drawMini();
  $("#dayTitle").textContent = fmtD(S.date); $("#daySub").textContent = S.date === today() ? "today" : "";
  $("#dayNext").disabled = S.date >= today();
  drawLegend();
}
function drawLegend() {
  const kinds = new Map(); for (const e of S.events) { const [c, n] = kindOf(e); const k = kinds.get(n) || { c, n: 0 }; k.n++; kinds.set(n, k); }
  const L = $("#legend"); L.innerHTML = `<span><i style="background:rgba(79,140,255,.55)"></i>footage</span>`;
  for (const [n, k] of kinds) {
    const b = document.createElement("button"); b.type = "button"; b.className = "lk" + (hiddenKinds.has(n) ? " off" : ""); b.title = hiddenKinds.has(n) ? `Show ${n}` : `Hide ${n}`;
    b.innerHTML = `<i style="background:${k.c}"></i>${n} <span class="muted">${k.n}</span>`;
    b.addEventListener("click", () => { hiddenKinds.has(n) ? hiddenKinds.delete(n) : hiddenKinds.add(n); draw(); drawEvents(); });
    L.appendChild(b);
  }
  if (S.privacy.length) L.insertAdjacentHTML("beforeend", `<span><i class="privSw"></i>privacy mode (hidden)</span>`);
  if (S.loaded && !S.clips.length) L.insertAdjacentHTML("beforeend", `<span class="muted">${S.nocard ? "no SD card" : "no footage in this range"}</span>`);
}
function drawMini() {
  mini.hidden = !view; if (!view) return;
  const full = S.to - S.from, X = t => ((t - S.from) / full * 100) + "%";
  const segs = mini.querySelector(".segs"); segs.innerHTML = "";
  for (const c of S.clips) { const d = document.createElement("div"); d.className = "seg"; d.style.left = X(c.start); d.style.width = `max(1px, ${((c.end - c.start) / full * 100)}%)`; segs.appendChild(d); }
  const w = mini.querySelector(".win"); w.style.left = X(view[0]); w.style.width = ((view[1] - view[0]) / full * 100) + "%";
}
{ let on = false; const go = ev => { const r = mini.getBoundingClientRect(), t = S.from + (S.to - S.from) * Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width)), sp = view[1] - view[0]; setView(t - sp / 2, t + sp / 2); };
  mini.addEventListener("pointerdown", ev => { if (!view) return; on = true; mini.setPointerCapture(ev.pointerId); go(ev); });
  mini.addEventListener("pointermove", ev => { if (on && view) go(ev); });
  mini.addEventListener("pointerup", () => { on = false; }); mini.addEventListener("pointercancel", () => { on = false; }); }

let drag = null; const ptrs = new Map(); let pinch = null;
const tAt = x => { const r = track.getBoundingClientRect(); const [a, b] = vis(); const f = Math.min(1, Math.max(0, (x - r.left) / r.width)); return Math.round(a + f * (b - a)); };
function showBubble(x, t) {
  const r = track.getBoundingClientRect(), [a, b] = vis();
  bubble.hidden = false; bubble.textContent = fmtWhen(t, (b - a) < 3 * 3600);
  bubble.style.left = Math.min(r.width - bubble.offsetWidth / 2, Math.max(bubble.offsetWidth / 2, x - r.left)) + "px";
}
track.addEventListener("pointerdown", ev => {
  track.setPointerCapture(ev.pointerId); ptrs.set(ev.pointerId, ev.clientX); ev.preventDefault();
  if (ptrs.size === 2) { const [x1, x2] = [...ptrs.values()]; pinch = { d: Math.abs(x2 - x1), mid: tAt((x1 + x2) / 2), view: vis().slice() }; drag = null; return; }
  if (cut.on) { cut.dragging = true; cut.a = tAt(ev.clientX); cut.b = cut.a; drawCut(); showBubble(ev.clientX, cut.a); return; }
  drag = true; S.t = snap(tAt(ev.clientX)); showBubble(ev.clientX, S.t); draw();
});
track.addEventListener("pointermove", ev => {
  if (!ptrs.has(ev.pointerId)) { if (ev.pointerType === "mouse") showBubble(ev.clientX, tAt(ev.clientX)); return; }
  ptrs.set(ev.pointerId, ev.clientX);
  if (pinch && ptrs.size === 2) { const [x1, x2] = [...ptrs.values()]; const d = Math.abs(x2 - x1) || 1;
    const span = (pinch.view[1] - pinch.view[0]) * pinch.d / d; const r = track.getBoundingClientRect(); const f = ((x1 + x2) / 2 - r.left) / r.width;
    setView(pinch.mid - f * span, pinch.mid + (1 - f) * span); return; }
  if (cut.dragging) { cut.b = tAt(ev.clientX); showBubble(ev.clientX, cut.b); drawCut(); return; }
  if (drag) { S.t = snap(tAt(ev.clientX)); showBubble(ev.clientX, S.t); draw(); }
});
track.addEventListener("pointerleave", ev => { if (!ptrs.size) bubble.hidden = true; });
const endPtr = ev => { ptrs.delete(ev.pointerId); if (ptrs.size < 2) pinch = null; if (ev.pointerType !== "mouse") bubble.hidden = true;
  if (cut.dragging) { cut.dragging = false; cut.b = tAt(ev.clientX); drawCut(); return; }
  if (drag && ptrs.size === 0) { drag = null; const t = snap(tAt(ev.clientX)); const r = track.getBoundingClientRect();
    if (hasNow() && (t >= now() - 30 || (!view && ev.clientX >= r.right - 6))) goLive(); else play(t); } };
track.addEventListener("pointerup", endPtr); track.addEventListener("pointercancel", endPtr);
track.addEventListener("wheel", ev => { ev.preventDefault(); if (Math.abs(ev.deltaX) > Math.abs(ev.deltaY) && view) { const [a, b] = view, s = b - a; setView(a + ev.deltaX / track.clientWidth * s, b + ev.deltaX / track.clientWidth * s); }
  else zoomAt(ev.deltaY < 0 ? 1.25 : 0.8, tAt(ev.clientX)); }, { passive: false });
track.addEventListener("dblclick", ev => zoomAt(4, tAt(ev.clientX)));
$("#zoomBtns").addEventListener("click", ev => { const b = ev.target.closest("button[data-z]"); if (b) zoomTo(b.dataset.z === "all" ? "all" : +b.dataset.z); });
$("#panL").addEventListener("click", () => { if (view) { const s = view[1] - view[0]; setView(view[0] - s * 0.8, view[1] - s * 0.8); } });
$("#panR").addEventListener("click", () => { if (view) { const s = view[1] - view[0]; setView(view[0] + s * 0.8, view[1] + s * 0.8); } });
setInterval(() => { if (!drag && !pinch) { if (RANGES[S.range]) { const to = now() + 60; S.from += to - S.to; S.to = to; } if (S.mode === "live" && hasNow()) { S.t = now(); keepInView(S.t); } draw(); } }, 15000);

// ---------------------------------------------------------------- downloads (saved on the server, then fetched)
const cut = { on: false, a: 0, b: 0, dragging: false };
const cutPanel = $("#cutPanel");
const BYTES_PER_SEC = 150000;   // measured ~45 MB per 5 min of 1080p from the card
const DL = { jobs: [], capacity: null, timer: null };
const fmtDur = s => { const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = Math.round(s % 60); return h ? `${h} h ${m} min` : m ? `${m} min${x && m < 10 ? ` ${x} s` : ""}` : `${x} s`; };
const fmtMB = b => b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : `${Math.max(1, Math.round(b / 1e6))} MB`;
const footageIn = (lo, hi) => S.clips.reduce((n, c) => n + Math.max(0, Math.min(hi, c.end) - Math.max(lo, c.start)), 0);
function drawCut() {
  const lo = Math.min(cut.a, cut.b), hi = Math.max(cut.a, cut.b);
  $("#cutStartT").textContent = cut.a ? fmtWhen(lo, true) : "–"; $("#cutEndT").textContent = cut.b && hi > lo ? fmtWhen(hi, true) : "–";
  const go = $("#cutGo"), note = $("#cutNote"), len = hi - lo, ok = cut.a && cut.b && len >= 5;
  go.hidden = !ok; $("#cutZoom").hidden = !ok; note.textContent = "";
  if (ok) {
    const secs = footageIn(lo, hi), size = secs * BYTES_PER_SEC;
    const capNote = DL.capacity != null && secs > DL.capacity ? ` Right now the server has room for about ${fmtDur(DL.capacity)}.` : "";
    if (!secs) { go.hidden = true; note.textContent = "There's no footage on the card in that stretch."; }
    else if (len > D.maxDownload) { go.hidden = true; note.textContent = `${fmtDur(len)} selected. The limit is ${Math.round(D.maxDownload / 3600)} hours per file.`; }
    else if (S.privacy.some(([ps, pe]) => ps < hi && pe > lo)) { go.hidden = true; note.textContent = "That stretch includes privacy-mode time, which can't be downloaded."; }
    else { go.textContent = `Save download · ${fmtDur(secs)} · ~${fmtMB(size)}`; note.textContent = "The server saves it from the camera (about as long as the footage takes to play), then you download the file. It's deleted once downloaded, or after an hour." + capNote; }
  }
  draw();
}
function setCut(on) { cut.on = on; cut.a = cut.b = 0; cutPanel.hidden = !on; $("#cutBtn").classList.toggle("active", on); $("#cutBtn").querySelector("span").textContent = on ? "Done" : "Download"; track.classList.toggle("cutting", on); if (on) pollJobs(); drawCut(); }
const playhead = () => Math.round(S.mode === "live" ? now() : S.t);
$("#cutStart").addEventListener("click", () => { cut.a = playhead(); if (!cut.b || cut.b < cut.a) cut.b = 0; drawCut(); });
$("#cutEnd").addEventListener("click", () => { cut.b = playhead(); if (!cut.a) cut.a = Math.max(S.from, cut.b - 60); drawCut(); });
cutPanel.addEventListener("click", ev => { const b = ev.target.closest("button[data-n]"); if (!b) return; const [k, d] = b.dataset.n.split(",");
  if (!cut.a || !cut.b) { const p = playhead(), lv = S.mode === "live"; cut.a = lv ? p - 60 : p; cut.b = lv ? p : Math.min(now(), p + 60); }
  const lo = Math.min(cut.a, cut.b), hi = Math.max(cut.a, cut.b); cut.a = lo; cut.b = hi;
  cut[k] = Math.min(now(), Math.max(S.from, cut[k] + +d)); drawCut(); });
$("#cutCancel").addEventListener("click", () => setCut(false));
$("#cutBtn").addEventListener("click", () => setCut(!cut.on));
$("#cutZoom").addEventListener("click", () => { const lo = Math.min(cut.a, cut.b), hi = Math.max(cut.a, cut.b), pad = (hi - lo) * 0.15; setView(lo - pad, hi + pad); });
async function startDownload(lo, hi, btn) {
  if (btn) btn.disabled = true;
  try {
    const fd = new FormData(); fd.set("start", Math.round(lo)); fd.set("end", Math.round(hi));
    const r = await fetch(`/cam/${D.camId}/downloads`, { method: "POST", body: fd }), j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) throw new Error(j.error || j.detail || "Couldn't start the download.");
    status.textContent = "Saving from the camera… it'll show under Downloads when it's ready.";
    if (cut.on) setCut(false);
    if (S.mode === "play") goLive();          // the card is busy with the download now
    pollJobs();
  } catch (e) { status.textContent = e.message; $("#cutNote").textContent = e.message; }
  finally { if (btn) btn.disabled = false; }
}
$("#cutGo").addEventListener("click", ev => startDownload(Math.min(cut.a, cut.b), Math.max(cut.a, cut.b), ev.currentTarget));
async function pollJobs() {
  clearTimeout(DL.timer);
  try { const j = await (await fetch("/downloads.json")).json(); DL.jobs = j.jobs || []; DL.capacity = j.capacity; } catch {}
  drawJobs();
  const active = DL.jobs.some(j => j.state === "queued" || j.state === "saving");
  if (!active && status.textContent.startsWith("Saving from the camera")) status.textContent = "";
  DL.timer = setTimeout(pollJobs, active ? 2000 : 30000);
}
function drawJobs() {
  const list = $("#dlList"), ul = $("#dlItems"); const show = DL.jobs.filter(j => j.state !== "expired");
  list.hidden = !show.length; ul.innerHTML = "";
  $("#dlHint").textContent = show.some(j => j.state === "ready") ? "Ready files are deleted after you download them, or after an hour." : "";
  for (const j of show) {
    const li = document.createElement("li"), when = `${new Date(j.start * 1000).toLocaleDateString("en-CA", { timeZone: TZ, weekday: "short", month: "short", day: "numeric" })}, ${fmtT(j.start)}–${fmtT(j.end)}`;
    let right = "";
    if (j.state === "queued") right = `<span class="muted small">Waiting…</span> <button type="button" class="link small" data-cancel="${j.id}">Cancel</button>`;
    else if (j.state === "saving") right = `<span class="pbarS"><i style="width:${j.pct}%"></i></span><span class="small">${j.pct}%${j.note ? " · " + j.note : ""}</span> <button type="button" class="link small" data-cancel="${j.id}">Cancel</button>`;
    else if (j.state === "ready") right = `<a class="btn primary small" href="${j.url}" download>Download · ${fmtMB(j.size)}</a> <span class="muted tiny">until ${fmtT(j.expires)}</span> <button type="button" class="link small" data-cancel="${j.id}">Delete</button>`;
    else right = `<span class="err small">${j.state === "cancelled" ? "Cancelled" : (j.error || "Failed")}</span> <button type="button" class="link small" data-retry="${j.start},${j.end},${j.cam_id}">Try again</button>`;
    li.innerHTML = `<span><b>${j.cam_name}</b> <span class="muted small">${when}${j.gaps ? ` · ${j.gaps} short gap${j.gaps > 1 ? "s" : ""}` : ""}</span></span><span class="dlRight">${right}</span>`;
    ul.appendChild(li);
  }
}
$("#dlItems").addEventListener("click", async ev => {
  const c = ev.target.closest("[data-cancel]"), r = ev.target.closest("[data-retry]");
  if (c) { c.disabled = true; await fetch(`/downloads/${c.dataset.cancel}/cancel`, { method: "POST" }); pollJobs(); }
  if (r) { const [a, b, cam] = r.dataset.retry.split(",").map(Number); if (cam !== D.camId) { location.href = `/cam/${cam}`; return; } startDownload(a, b, r); }
});

// ---------------------------------------------------------------- speed
const speed = $("#speed");
speed.addEventListener("change", () => {
  clip.playbackRate = +speed.value; try { localStorage.setItem("speed", speed.value); } catch {}
  // 1x footage arrives in real time; faster speeds need the camera's fast mode, so re-ask
  if (S.mode === "play") play(Math.round(S.t));
});
try { speed.value = localStorage.getItem("speed") || "1"; } catch {}

// ---------------------------------------------------------------- detections list
function drawEvents() {
  const ul = $("#evList"); ul.innerHTML = "";
  const ev = visibleEvents();
  $("#evTitle").textContent = ev.length ? `${ev.length} detection${ev.length > 1 ? "s" : ""} in this range` + (hiddenKinds.size ? " (some kinds hidden)" : "") : "No detections in this range";
  let lastDay = "";
  for (const e of [...ev].reverse().slice(0, 500)) {
    const day = multiDay() ? new Date(e.start * 1000).toLocaleDateString("en-CA", { timeZone: TZ, weekday: "long", month: "short", day: "numeric" }) : "";
    if (day && day !== lastDay) { lastDay = day; const h = document.createElement("li"); h.className = "evDay"; h.textContent = day; ul.appendChild(h); }
    const [col, name] = kindOf(e); const li = document.createElement("li");
    li.innerHTML = `<i class="sw" style="background:${col}"></i><b>${fmtT(e.start)}</b> <span>${e.labels.join(", ")}</span><span class="muted small"> · ${Math.max(1, Math.round((e.end - e.start)))}s</span><span class="grow"></span><button type="button" class="evDl" title="Download this (with 15 s either side)">⬇</button>`;
    li.addEventListener("click", ev2 => {
      if (ev2.target.closest(".evDl")) { ev2.stopPropagation(); startDownload(Math.max(S.from, e.start - 15), Math.min(now(), Math.max(e.end, e.start + 5) + 15), ev2.target); return; }
      play(Math.max(S.from, e.start - 3));
    });
    ul.appendChild(li);
  }
}

// ---------------------------------------------------------------- loading: a day, or the last 24 h / 7 / 30 days
const shift = (key, n) => { const d = new Date(+key.slice(0, 4), +key.slice(4, 6) - 1, +key.slice(6, 8) + n); return `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}${String(d.getDate()).padStart(2, "0")}`; };
const keyOf = t => ymd(new Date(t * 1000));
let sdLoaded = false, loadSeq = 0;
function setBusy(on, txt) { const l = $("#tlLoading"); l.hidden = !on; if (txt) l.querySelector("span").textContent = txt; $("#tl").classList.toggle("loading", on); $("#dayPrev").disabled = on; $("#dayNext").disabled = on || S.date >= today(); }
async function fetchDay(key, seq) {
  for (let attempt = 0; ; attempt++) {
    const r = await fetch(`/cam/${D.camId}/recordings.json?date=${key}${sdLoaded ? "" : "&sd=1"}`);
    if (seq !== loadSeq) return null;
    if (r.ok) return r.json();
    const msg = (await r.json().catch(() => ({}))).detail || String(r.status);
    if (/busy|71103|52405/.test(msg) && attempt < 4) { setBusy(true, `Camera busy, retrying… (${attempt + 1})`); await new Promise(res => setTimeout(res, 4000)); continue; }
    throw new Error(msg);
  }
}
function absorb(key, j) {
  if (S.range === "day" && j.dayStart) { S.from = j.dayStart; S.to = j.dayEnd; }
  S.days.set(key, j);
  if (j.nocard) { S.nocard = true; $("#cardInfo").textContent = "This camera has no SD card, so there's no recorded footage. Live view still works."; }
  if (j.sd) { sdLoaded = true; const sd = j.sd; const size = sd.total_space === "0B" ? sd.video_total_space : sd.total_space;
    $("#cardInfo").textContent = `Card: ${size}, ${sd.free_space} free${sd.loop_record_status === "1" ? ", oldest footage is overwritten as it fills" : ""}. Footage comes straight from the card, so playback takes a few seconds to start.`; }
  const inR = x => x.end > S.from && x.start < S.to;
  const all = [...S.days.values()];
  const seen = new Set();
  S.clips = all.flatMap(d => d.clips || []).filter(c => inR(c) && !seen.has(c.start) && seen.add(c.start)).sort((a, b) => a.start - b.start);
  const seenE = new Set();
  S.events = all.flatMap(d => d.events || []).filter(e => e.start >= S.from && e.start < S.to && !seenE.has(e.start + ":" + e.labels) && seenE.add(e.start + ":" + e.labels)).sort((a, b) => a.start - b.start);
  S.privacy = all.flatMap(d => d.privacy || []);
}
async function load(at) {
  const seq = ++loadSeq; status.textContent = ""; S.days = new Map(); S.loaded = false; S.nocard = false; view = null;
  let keys;
  if (S.range === "day") { const ds = D.date === S.date ? D.dayStart : null; keys = [S.date];
    S.from = ds || Math.floor(new Date(+S.date.slice(0, 4), +S.date.slice(4, 6) - 1, +S.date.slice(6, 8)).getTime() / 1000); S.to = S.from + 86400; }
  else { S.to = now() + 60; S.from = S.to - RANGES[S.range]; keys = []; for (let t = S.to; t > S.from - 86400; t -= 86400) { const k = keyOf(Math.max(S.from, t)); if (!keys.includes(k)) keys.push(k); } }
  $("#dayNav").hidden = S.range !== "day"; $("#rangeTitle").hidden = S.range === "day";
  for (const b of $("#rangeSel").children) b.classList.toggle("active", b.dataset.range === S.range);
  S.clips = []; S.events = []; S.privacy = []; draw(); drawEvents();
  // multi-day: only ask about days the card says have footage
  if (keys.length > 1) {
    const months = [...new Set(keys.map(k => k.slice(0, 6)))];
    const have = new Set();
    try { for (const m of months) { const j = await (await fetch(`/cam/${D.camId}/recordings.json?month=${m}`)).json(); (j.dates || []).forEach(d => have.add(d)); } } catch {}
    if (seq !== loadSeq) return;
    if (have.size) keys = keys.filter(k => have.has(k) || k === today());
  }
  let stale = 0, failed = 0;
  for (let i = 0; i < keys.length; i++) {
    setBusy(true, keys.length > 1 ? `Reading the card: ${fmtD(keys[i])} (${i + 1} of ${keys.length})…` : `Reading the card for ${fmtD(keys[i])}…`);
    try { const j = await fetchDay(keys[i], seq); if (!j) return; if (j.stale) stale = Math.max(stale, j.stale); absorb(keys[i], j); }
    catch (e) { if (seq !== loadSeq) return; failed++; if (keys.length === 1) { setBusy(false); status.textContent = "Couldn't read the card: " + e.message; S.loaded = true; draw(); return; } }
    if (seq !== loadSeq) return;
    if (i === 0 && at) { setBusy(false); play(at); }
    draw(); drawEvents();
  }
  S.loaded = true; setBusy(false);
  const days = new Set(S.clips.map(c => keyOf(c.start))).size;
  $("#rangeTitle").innerHTML = RANGES[S.range] ? `<b>Last ${S.range === "24h" ? "24 hours" : S.range === "7d" ? "7 days" : "30 days"}</b> <span class="muted small">${S.clips.length ? `footage on ${days} day${days > 1 ? "s" : ""}, back to ${new Date(S.clips[0].start * 1000).toLocaleDateString("en-CA", { timeZone: TZ, weekday: "short", month: "short", day: "numeric" })}, ${fmtT(S.clips[0].start)}` : ""}</span>` : "";
  if (stale) status.textContent = `The card is busy, so this shows the footage list from ${fmtT(stale)}.`;
  else if (failed) status.textContent = `Couldn't read ${failed} day${failed > 1 ? "s" : ""} from the card; try again in a moment.`;
  if (!at && S.mode !== "live") { S.t = S.from; }
  if (S.date.slice(0, 6) !== S.month) await loadMonth(S.date.slice(0, 6)); else drawCal();
  draw(); drawEvents();
}
function setRange(r) {
  S.range = r; try { localStorage.setItem("range", r); } catch {}
  if (r === "day" && S.mode === "live") S.date = today();
  setURL(); load(S.mode === "play" ? Math.round(S.t) : 0);
}
$("#rangeSel").addEventListener("click", ev => { const b = ev.target.closest("button[data-range]"); if (b && b.dataset.range !== S.range) setRange(b.dataset.range); });
async function loadMonth(m) {
  S.month = m; $("#calGrid").classList.add("loading");
  try { const r = await fetch(`/cam/${D.camId}/recordings.json?month=${m}`); const j = await r.json(); S.dates = new Set(j.dates || []); } catch { S.dates = new Set(); }
  $("#calGrid").classList.remove("loading"); drawCal();
}
function drawCal() {
  const y = +S.month.slice(0, 4), mo = +S.month.slice(4, 6);
  $("#monthTitle").textContent = new Date(y, mo - 1, 1).toLocaleDateString("en-CA", { month: "long", year: "numeric" });
  const g = $("#calGrid"); g.innerHTML = "";
  for (const w of ["S", "M", "T", "W", "T", "F", "S"]) { const h = document.createElement("div"); h.className = "dow"; h.textContent = w; g.appendChild(h); }
  const first = new Date(y, mo - 1, 1).getDay(), days = new Date(y, mo, 0).getDate(), td = today();
  for (let i = 0; i < first; i++) g.appendChild(document.createElement("div"));
  for (let d = 1; d <= days; d++) {
    const key = `${y}${String(mo).padStart(2, "0")}${String(d).padStart(2, "0")}`;
    const b = document.createElement("button"); b.type = "button"; b.textContent = d;
    b.className = "day" + (S.dates.has(key) ? " has" : "") + (key === S.date ? " sel" : "") + (key === td ? " today" : "");
    if (key > td) b.disabled = true;
    b.addEventListener("click", () => { $("#calModal").hidden = true; S.date = key; S.range = "day"; try { localStorage.setItem("range", "day"); } catch {} setURL(); load(); });
    g.appendChild(b);
  }
  $("#nextM").disabled = S.month >= td.slice(0, 6);
}
$("#dayBtn").addEventListener("click", () => { $("#calModal").hidden = false; if (!S.dates.size) loadMonth(S.month); });
$("#calClose").addEventListener("click", () => { $("#calModal").hidden = true; });
$("#calModal").addEventListener("click", ev => { if (ev.target.id === "calModal") $("#calModal").hidden = true; });
document.addEventListener("keydown", ev => { if (ev.key === "Escape") { $("#calModal").hidden = true; } });
$("#dayPrev").addEventListener("click", () => { S.date = shift(S.date, -1); setURL(); load(); });
$("#dayNext").addEventListener("click", () => { if (S.date < today()) { S.date = shift(S.date, 1); setURL(); load(); } });
$("#prevM").addEventListener("click", () => { const y = +S.month.slice(0, 4), m = +S.month.slice(4, 6); const d = new Date(y, m - 2, 1); loadMonth(`${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}`); });
$("#nextM").addEventListener("click", () => { const y = +S.month.slice(0, 4), m = +S.month.slice(4, 6); const d = new Date(y, m, 1); loadMonth(`${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}`); });

// ---------------------------------------------------------------- start
S.days = new Map();
layout(); draw(); drawEvents(); drawSound(); showSeek(); pollJobs();
if (D.at) { S.mode = "play"; S.t = D.at; load(D.at); } else { goLive(); load(); }
loadMonth(S.month);
