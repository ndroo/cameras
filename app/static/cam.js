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
const S = { mode: "live", date: D.date, dayStart: D.dayStart, dayEnd: D.dayEnd, clips: [], events: [], dates: new Set(), month: D.date.slice(0, 6),
            t: now(), segStart: 0, segEnd: 0, playing: false, loaded: false, priv: D.privacy, privacy: [] };

// ---------------------------------------------------------------- player
const wrap = $("#camWrap");
let knownAr = 16 / 9; try { knownAr = +localStorage.getItem("ar:" + D.camId) || knownAr; } catch {}
function noteAr(v) { if (v && v.videoWidth && v.videoHeight) { const ar = v.videoWidth / v.videoHeight; if (Math.abs(ar - knownAr) > 0.01) { knownAr = ar; try { localStorage.setItem("ar:" + D.camId, ar); } catch {} layout(); } } }
function layout() {
  const side = o.rotate === 90 || o.rotate === 270;
  const ar0 = knownAr;
  const ar = side ? 1 / ar0 : ar0;                       // shape of the picture as shown
  // size the whole column to the picture: as wide as the page allows, but no taller than ~56% of the window
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
  S.t = now(); keepInView(S.t); history.replaceState(null, "", `/cam/${D.camId}`); layout(); draw(); showPtz();
}
// ---------------------------------------------------------------- privacy mode
const privOverlay = $("#privOverlay");
function showPrivacy() {
  stopLive(); live.style.display = "none"; hideHint(); poster.style.opacity = "0"; privOverlay.hidden = false;
  $("#privTxt").textContent = `Until ${fmtT(S.priv.until)} · turned on by ${S.priv.by}`;
  $("#privResume").hidden = !S.priv.canResume;
  modeBtn.classList.add("live"); modeTxt.textContent = "LIVE"; label.hidden = true; speed.hidden = true; status.textContent = "";
  S.t = now(); keepInView(S.t); history.replaceState(null, "", `/cam/${D.camId}`); layout(); draw(); showPtz();
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
function snap(t) {
  // nearest moment that has footage; "now" counts as footage on today's bar
  if (covered(t)) return t;
  const cands = [];
  for (const c of S.clips) { cands.push(c.start); cands.push(c.end - 1); }
  if (S.date === today()) cands.push(now());
  if (!cands.length) return t;
  return cands.reduce((best, x) => Math.abs(x - t) < Math.abs(best - t) ? x : best, cands[0]);
}
const nextCovered = t => { const c = S.clips.filter(c => c.end > t).sort((a, b) => a.start - b.start)[0]; return c ? Math.max(t, c.start) : null; };

function play(t) {
  if (S.date === today() && t >= now() - 30) return goLive();
  const n = covered(t) ? t : nextCovered(t);
  if (n === null) { status.textContent = "No footage after that time on this day."; S.t = t; draw(); return; }
  const c = covered(n); t = n;
  S.mode = "play"; S.t = t; S.segStart = t; S.segEnd = Math.min(c.end, t + D.maxClip); S.playing = false;
  stopLive(); live.style.display = "none"; clip.hidden = false; privOverlay.hidden = true; showPtz();
  const url = `/stream/${D.camId}/clip.mp4?start=${S.segStart}&end=${S.segEnd}&speed=${speed.value}`;
  clip.volume = vol; clip.muted = !sound; setClip(url); clip.playbackRate = +speed.value;
  clip.play().catch(() => { if (!clip.muted) { clip.muted = true; clip.play().catch(() => {}); } });   // no tap yet on this page: start muted
  speed.hidden = false;
  modeBtn.classList.remove("live"); modeTxt.innerHTML = '<span class="wideOnly">Go live</span><span class="narrowOnly">Live</span>'; label.hidden = false; label.textContent = fmtTs(t);
  poster.style.opacity = "0"; showHint("Fetching from the camera's card…"); status.textContent = "";
  history.replaceState(null, "", `/cam/${D.camId}?date=${S.date}&at=${t}`); layout(); draw();
}
clip.addEventListener("playing", () => { hideHint(); S.playing = true; });
clip.addEventListener("waiting", () => showHint("Buffering…"));
clip.addEventListener("loadedmetadata", () => noteAr(clip));
clip.addEventListener("timeupdate", () => { if (S.mode === "play" && S.playing && !drag) { S.t = S.segStart + clip.currentTime; label.textContent = fmtTs(S.t); keepInView(S.t); draw(); } });
clip.addEventListener("ended", () => { const n = nextCovered(S.segEnd + 1); if (n !== null && n < S.dayEnd) play(n); else if (S.date === today()) goLive(); else status.textContent = "End of footage for this day."; });
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
window.addEventListener("resize", layout); document.addEventListener("fullscreenchange", layout);

// ---------------------------------------------------------------- pan / tilt (cameras with a motor, live view only)
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
}

// ---------------------------------------------------------------- timeline (one zoomable day bar)
const KIND = { motion: ["#ff9f0a", "motion"], person: ["#34c759", "person"], ring: ["#bf5af2", "doorbell"], "missed doorbell ring": ["#bf5af2", "doorbell"],
  "doorbell ring answered": ["#bf5af2", "doorbell"], vehicle: ["#5ac8fa", "vehicle"], pet: ["#ffd60a", "pet"], tamper: ["#ff453a", "tamper"], "package delivered": ["#ff375f", "package"], "package picked up": ["#ff375f", "package"] };
const kindOf = e => { for (const l of e.labels) if (KIND[l]) return KIND[l]; return ["#8b93a3", e.labels[0] || "event"]; };
const track = $("#track24");
let view = null;   // [a, b] visible window; null = whole day
const vis = () => view || [S.dayStart, S.dayEnd];
function setView(a, b) {
  const span = Math.max(600, Math.min(86400, b - a));
  a = Math.max(S.dayStart, Math.min(a, S.dayEnd - span)); b = a + span;
  view = span >= 86400 ? null : [a, b];
  $("#zoomReset").hidden = !view; draw();
}
function zoomAt(factor, t) {
  const [a, b] = vis(), span = b - a, ns = span / factor, f = (t - a) / span;
  setView(t - f * ns, t - f * ns + ns);
}
function keepInView(t) {
  if (!view) return; const [a, b] = view, span = b - a;
  if (t < a || t > b) setView(t - span / 2, t + span / 2);
  else if (t > b - span * 0.08) setView(t - span * 0.9, t + span * 0.1);
}
function tickPlan(span) {
  if (span > 12 * 3600) return [3600, 3 * 3600]; if (span > 4 * 3600) return [900, 3600]; if (span > 3600) return [300, 900]; if (span > 1200) return [60, 300]; return [30, 60];
}
function draw() {
  const nowT = now(), [a, b] = vis(), span = b - a, X = t => ((t - a) / span * 100) + "%";
  const segs = track.querySelector(".segs"); segs.innerHTML = "";
  for (const c of S.clips) { const s = Math.max(a, c.start), e = Math.min(b, c.end); if (e <= s) continue;
    const d = document.createElement("div"); d.className = "seg"; d.style.left = X(s); d.style.width = ((e - s) / span * 100) + "%"; segs.appendChild(d); }
  for (const [ps, pe0] of S.privacy) { const pe = S.priv && pe0 >= nowT - 120 ? Math.max(pe0, nowT) : pe0, s = Math.max(a, ps), e = Math.min(b, pe); if (e <= s) continue;
    const d = document.createElement("div"); d.className = "seg priv"; d.title = `privacy mode ${fmtT(ps)} – ${fmtT(pe)}`; d.style.left = X(s); d.style.width = ((e - s) / span * 100) + "%"; segs.appendChild(d); }
  const marks = track.querySelector(".marks"); marks.innerHTML = "";
  for (const e of S.events) { if (e.start < a || e.start > b) continue;
    const m = document.createElement("div"); m.className = "mark"; const [col, name] = kindOf(e);
    m.style.left = X(e.start); m.style.background = col; m.title = `${name} · ${fmtT(e.start)}`; marks.appendChild(m); }
  const ticks = track.querySelector(".ticks"); ticks.innerHTML = "";
  const [minor, major] = tickPlan(span), narrow = track.clientWidth < 560;
  for (let t = S.dayStart + Math.ceil((a - S.dayStart) / minor) * minor; t <= b; t += minor) {
    const isMajor = (t - S.dayStart) % major === 0; if (!isMajor && span / minor > 60) continue;
    const k = document.createElement("div"); k.className = "tick" + (isMajor ? " major" : ""); k.style.left = X(t);
    if (isMajor && t < b && !(narrow && (Math.round((t - S.dayStart) / major) % 2))) k.dataset.l = shortT(t);
    ticks.appendChild(k);
  }
  const selEl = track.querySelector(".sel");
  if (cut.on && cut.a && cut.b) { const s1 = Math.max(a, Math.min(cut.a, cut.b)), e1 = Math.min(b, Math.max(cut.a, cut.b)); selEl.style.display = e1 > s1 ? "" : "none"; selEl.style.left = X(s1); selEl.style.width = ((e1 - s1) / span * 100) + "%"; }
  else selEl.style.display = "none";
  const nowEl = track.querySelector(".now"); const showNow = nowT >= a && nowT <= b; nowEl.style.display = showNow ? "" : "none"; if (showNow) nowEl.style.left = X(nowT);
  const future = track.querySelector(".future") || Object.assign(track.appendChild(document.createElement("div")), { className: "future" });
  if (nowT < b) { future.style.display = ""; future.style.left = X(Math.max(a, nowT)); } else future.style.display = "none";
  const head = track.querySelector(".head"); const inside = S.t >= a && S.t <= b; head.style.display = inside ? "" : "none"; head.style.left = X(S.t);
  head.classList.toggle("isLive", S.mode === "live");
  $("#tlTime").textContent = (S.mode === "live" ? "Live · " : "") + fmtTs(S.mode === "live" ? nowT : S.t);
  $("#dayTitle").textContent = fmtD(S.date); $("#daySub").textContent = S.date === today() ? "today" : "";
  $("#dayNext").disabled = S.date >= today();
  const kinds = new Map(); for (const e of S.events) { const [c, n] = kindOf(e); kinds.set(n, c); }
  $("#legend").innerHTML = `<span><i style="background:rgba(79,140,255,.45)"></i>footage</span>` + [...kinds].map(([n, c]) => `<span><i style="background:${c}"></i>${n}</span>`).join("") + (S.privacy.length ? `<span><i class="privSw"></i>privacy mode (hidden)</span>` : "") + (S.loaded && !S.clips.length ? `<span class="muted">${S.nocard ? "no SD card" : "no footage on this day"}</span>` : "");
}
let drag = null; const ptrs = new Map(); let pinch = null;
const tAt = x => { const r = track.getBoundingClientRect(); const [a, b] = vis(); const f = Math.min(1, Math.max(0, (x - r.left) / r.width)); return Math.round(a + f * (b - a)); };
track.addEventListener("pointerdown", ev => {
  track.setPointerCapture(ev.pointerId); ptrs.set(ev.pointerId, ev.clientX); ev.preventDefault();
  if (ptrs.size === 2) { const [x1, x2] = [...ptrs.values()]; pinch = { d: Math.abs(x2 - x1), mid: tAt((x1 + x2) / 2), view: vis().slice() }; drag = null; return; }
  if (cut.on) { cut.dragging = true; cut.a = tAt(ev.clientX); cut.b = cut.a; drawCut(); return; }
  drag = true; S.t = snap(tAt(ev.clientX)); draw();
});
track.addEventListener("pointermove", ev => {
  if (!ptrs.has(ev.pointerId)) return; ptrs.set(ev.pointerId, ev.clientX);
  if (pinch && ptrs.size === 2) { const [x1, x2] = [...ptrs.values()]; const d = Math.abs(x2 - x1) || 1;
    const span = (pinch.view[1] - pinch.view[0]) * pinch.d / d; const r = track.getBoundingClientRect(); const f = ((x1 + x2) / 2 - r.left) / r.width;
    setView(pinch.mid - f * span, pinch.mid + (1 - f) * span); return; }
  if (cut.dragging) { cut.b = tAt(ev.clientX); drawCut(); return; }
  if (drag) { S.t = snap(tAt(ev.clientX)); draw(); }
});
const endPtr = ev => { ptrs.delete(ev.pointerId); if (ptrs.size < 2) pinch = null; if (cut.dragging) { cut.dragging = false; cut.b = tAt(ev.clientX); drawCut(); return; } if (drag && ptrs.size === 0) { drag = null; const t = snap(tAt(ev.clientX)); const r = track.getBoundingClientRect();
  if (S.date === today() && (t >= now() - 30 || ev.clientX >= r.right - 6)) goLive(); else play(t); } };
track.addEventListener("pointerup", endPtr); track.addEventListener("pointercancel", endPtr);
track.addEventListener("wheel", ev => { ev.preventDefault(); if (Math.abs(ev.deltaX) > Math.abs(ev.deltaY) && view) { const [a, b] = view, s = b - a; setView(a + ev.deltaX / track.clientWidth * s, b + ev.deltaX / track.clientWidth * s); }
  else zoomAt(ev.deltaY < 0 ? 1.25 : 0.8, tAt(ev.clientX)); }, { passive: false });
track.addEventListener("dblclick", ev => zoomAt(view ? 1 / 100 : 6, tAt(ev.clientX)));
$("#zoomReset").addEventListener("click", () => setView(S.dayStart, S.dayEnd));
setInterval(() => { if (!drag && !pinch) { if (S.mode === "live" && S.date === today()) { S.t = now(); keepInView(S.t); } draw(); } }, 15000);

// ---------------------------------------------------------------- download a section
const cut = { on: false, a: 0, b: 0, dragging: false };
const cutPanel = $("#cutPanel");
const BYTES_PER_SEC = 160000;   // observed ~19 MB per 2 min of 1080p from the card
function drawCut() {
  const lo = Math.min(cut.a, cut.b), hi = Math.max(cut.a, cut.b);
  $("#cutStartT").textContent = cut.a ? fmtT(lo) : "–"; $("#cutEndT").textContent = cut.b && hi > lo ? fmtT(hi) : "–";
  const go = $("#cutGo"), note = $("#cutNote"), len = hi - lo, ok = cut.a && cut.b && len >= 5;
  go.hidden = !ok; note.textContent = "";
  if (ok) {
    const h = Math.floor(len / 3600), m = Math.round((len % 3600) / 60), dur = (h ? `${h} h ` : "") + `${m} min`;
    const mb = len * BYTES_PER_SEC / 1e6, size = mb >= 1000 ? `${(mb / 1000).toFixed(1)} GB` : `${Math.round(mb)} MB`;
    if (len > D.maxDownload) { go.hidden = true; note.textContent = `${dur} selected. The limit is ${Math.round(D.maxDownload / 3600)} hours per file; shorten the selection.`; }
    else if (S.privacy.some(([ps, pe]) => ps < hi && pe > lo)) { go.hidden = true; note.textContent = "That selection includes privacy-mode time, which can't be downloaded. Pick a stretch that doesn't cross the black band."; }
    else { go.textContent = `Download selection · ${dur} · ~${size}`; go.href = `/stream/${D.camId}/clip.mp4?start=${lo}&end=${hi}&download=1`; }
  }
  draw();
}
function setCut(on) { cut.on = on; cut.a = cut.b = 0; cutPanel.hidden = !on; $("#cutBtn").classList.toggle("active", on); $("#cutBtn").querySelector("span").textContent = on ? "Done" : "Section"; track.classList.toggle("cutting", on); drawCut(); }
$("#cutStart").addEventListener("click", () => { cut.a = Math.round(S.mode === "live" ? now() : S.t); if (!cut.b || cut.b < cut.a) cut.b = 0; drawCut(); });
$("#cutEnd").addEventListener("click", () => { cut.b = Math.round(S.mode === "live" ? now() : S.t); drawCut(); });
$("#cutCancel").addEventListener("click", () => setCut(false));
$("#cutBtn").addEventListener("click", () => setCut(!cut.on));
$("#cutGo").addEventListener("click", () => { status.textContent = "Preparing your download… it arrives as the card sends it."; });

// ---------------------------------------------------------------- speed
const speed = $("#speed");
speed.addEventListener("change", () => {
  clip.playbackRate = +speed.value; try { localStorage.setItem("speed", speed.value); } catch {}
  // 1x footage arrives in real time; faster speeds need the camera's fast mode, so re-ask
  if (S.mode === "play") play(Math.round(S.t));
});
try { speed.value = localStorage.getItem("speed") || "1"; } catch {}

// ---------------------------------------------------------------- events list
function drawEvents() {
  const ul = $("#evList"); ul.innerHTML = "";
  $("#evTitle").textContent = S.events.length ? `${S.events.length} detection${S.events.length > 1 ? "s" : ""} on this day` : "No detections on this day";
  for (const e of [...S.events].reverse()) {
    const [col, name] = kindOf(e); const li = document.createElement("li");
    li.innerHTML = `<i class="sw" style="background:${col}"></i><b>${fmtT(e.start)}</b> <span>${e.labels.join(", ")}</span><span class="muted small"> · ${Math.max(1, Math.round((e.end - e.start)))}s</span>`;
    li.addEventListener("click", () => play(e.start)); ul.appendChild(li);
  }
}

// ---------------------------------------------------------------- day + calendar
const shift = (key, n) => { const d = new Date(+key.slice(0, 4), +key.slice(4, 6) - 1, +key.slice(6, 8) + n); return `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}${String(d.getDate()).padStart(2, "0")}`; };
let sdLoaded = false;
function setBusy(on, txt) { const l = $("#tlLoading"); l.hidden = !on; if (txt) l.querySelector("span").textContent = txt; $("#tl").classList.toggle("loading", on); $("#dayPrev").disabled = on; $("#dayNext").disabled = on || S.date >= today(); }
async function loadDay(key, at, attempt = 0) {
  status.textContent = ""; setBusy(true, attempt ? `Camera busy, retrying… (${attempt})` : `Reading the card for ${fmtD(key)}…`);
  try {
    const r = await fetch(`/cam/${D.camId}/recordings.json?date=${key}${sdLoaded ? "" : "&sd=1"}`);
    if (!r.ok) { const msg = (await r.json()).detail || String(r.status);
      if (/busy|71103/.test(msg) && attempt < 4) { await new Promise(res => setTimeout(res, 4000)); return loadDay(key, at, attempt + 1); }
      throw new Error(msg); }
    const j = await r.json();
    if (j.nocard) { S.nocard = true; $("#cardInfo").textContent = "This camera has no SD card, so there's no recorded footage. Live view still works."; }
    S.date = key; S.clips = j.clips; S.events = j.events || []; S.privacy = j.privacy || []; S.dayStart = j.dayStart; S.dayEnd = j.dayEnd; S.loaded = true; view = null; $("#zoomReset").hidden = true;
    if (j.sd) { sdLoaded = true; const sd = j.sd; const size = sd.total_space === "0B" ? sd.video_total_space : sd.total_space;
      $("#cardInfo").textContent = `Card: ${size}, ${sd.free_space} free${sd.loop_record_status === "1" ? ", oldest footage is overwritten as it fills" : ""}. Footage comes straight from the card, so playback takes a few seconds to start.`; }
    setBusy(false);
    if (at) play(at); else if (S.mode !== "live") { S.t = S.dayStart; draw(); }
    if (key.slice(0, 6) !== S.month) await loadMonth(key.slice(0, 6)); else drawCal();
    draw(); drawEvents();
  } catch (e) { setBusy(false); status.textContent = "Couldn't read the card: " + e.message; }
}
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
    b.addEventListener("click", () => { $("#calModal").hidden = true; S.date = key; draw(); loadDay(key); });
    g.appendChild(b);
  }
  $("#nextM").disabled = S.month >= td.slice(0, 6);
}
$("#dayBtn").addEventListener("click", () => { $("#calModal").hidden = false; if (!S.dates.size) loadMonth(S.month); });
$("#calClose").addEventListener("click", () => { $("#calModal").hidden = true; });
$("#calModal").addEventListener("click", ev => { if (ev.target.id === "calModal") $("#calModal").hidden = true; });
document.addEventListener("keydown", ev => { if (ev.key === "Escape") { $("#calModal").hidden = true; } });
$("#dayPrev").addEventListener("click", () => loadDay(shift(S.date, -1)));
$("#dayNext").addEventListener("click", () => { if (S.date < today()) loadDay(shift(S.date, 1)); });
$("#prevM").addEventListener("click", () => { const y = +S.month.slice(0, 4), m = +S.month.slice(4, 6); const d = new Date(y, m - 2, 1); loadMonth(`${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}`); });
$("#nextM").addEventListener("click", () => { const y = +S.month.slice(0, 4), m = +S.month.slice(4, 6); const d = new Date(y, m, 1); loadMonth(`${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}`); });

// ---------------------------------------------------------------- start
layout(); draw(); drawEvents(); drawSound();
if (D.at) { S.mode = "play"; loadDay(D.date, D.at); } else { goLive(); loadDay(S.date); }
loadMonth(S.month);
