import { applyToBox } from "/static/orient.js";
const player = document.getElementById("player"), stage = player.querySelector(".stage");
const vs = document.getElementById("video"), poster = stage.querySelector(".poster"), camId = player.dataset.cam;
const isAdmin = !!document.getElementById("saved");
const key = "orient:" + camId;
let o = { rotate: +player.dataset.rotate, flip_h: +player.dataset.fh, flip_v: +player.dataset.fv };
if (!isAdmin) { try { const s = JSON.parse(localStorage.getItem(key)); if (s) o = s; } catch {} }

function layout() {
  const sideways = o.rotate === 90 || o.rotate === 270;
  const v = vs.video;
  let ar = (v && v.videoWidth && v.videoHeight) ? v.videoWidth / v.videoHeight : 16 / 9;
  // keep the stage the right shape for the rotated picture, bounded by the viewport height
  const w = stage.clientWidth;
  let h = sideways ? w * ar : w / ar;
  const maxH = window.innerHeight - 160;
  if (h > maxH) { h = maxH; }
  stage.style.aspectRatio = "auto"; stage.style.height = h + "px";
  applyToBox(stage, vs, o.rotate, o.flip_h, o.flip_v);
  applyToBox(stage, poster, o.rotate, o.flip_h, o.flip_v);
}
async function save() {
  if (isAdmin) {
    const fd = new FormData(); fd.set("rotate", o.rotate); fd.set("flip_h", o.flip_h); fd.set("flip_v", o.flip_v);
    await fetch(`/cam/${camId}/orientation`, { method: "POST", body: fd });
    const s = document.getElementById("saved"); s.textContent = "saved for everyone"; setTimeout(() => s.textContent = "", 2000);
  } else { try { localStorage.setItem(key, JSON.stringify(o)); } catch {} }
}
player.querySelector(".controls").addEventListener("click", async ev => {
  const b = ev.target.closest("button"); if (!b) return;
  const act = b.dataset.act;
  if (act === "rotate") { o.rotate = (o.rotate + 90) % 360; layout(); save(); }
  if (act === "flip_h") { o.flip_h = o.flip_h ? 0 : 1; layout(); save(); }
  if (act === "flip_v") { o.flip_v = o.flip_v ? 0 : 1; layout(); save(); }
  if (act === "full") { (player.requestFullscreen || player.webkitRequestFullscreen).call(player); }
  if (act === "snap") {
    const v = vs.video; if (!v || !v.videoWidth) return;
    const c = document.createElement("canvas"); c.width = v.videoWidth; c.height = v.videoHeight;
    c.getContext("2d").drawImage(v, 0, 0);
    const a = document.createElement("a"); a.download = `camera-${camId}-${new Date().toISOString().replace(/[:.]/g, "-")}.jpg`;
    a.href = c.toDataURL("image/jpeg", 0.92); a.click();
  }
});
// show the live picture once frames arrive
const v = vs.video;
if (v) {
  v.setAttribute("playsinline", ""); v.muted = true; v.controls = false;
  v.addEventListener("playing", () => { stage.classList.add("live"); layout(); });
  v.addEventListener("loadedmetadata", layout);
  v.addEventListener("waiting", () => stage.classList.remove("live"));
}
vs.src = `/stream/${camId}/ws`;
layout(); window.addEventListener("resize", layout);
document.addEventListener("fullscreenchange", layout);
