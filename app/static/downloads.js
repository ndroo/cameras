// Downloads tray, on every page (bottom right). A download is two steps: the server copies
// the footage off the camera's card (minutes), then the browser fetches the finished file.
// The tray shows both, and fetches the file by itself the moment it's ready, in the browser
// that asked for it, so nobody has to sit and watch or come back and press a second button.
(function () {
  const tray = document.getElementById("dlTray"); if (!tray) return;
  const ul = tray.querySelector("ul"), head = tray.querySelector(".dlTrayHead"), sum = tray.querySelector(".sum"), foot = tray.querySelector(".dlFoot");
  const TZ = tray.dataset.tz || undefined, baseTitle = document.title;
  const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const fmtT = t => new Date(t * 1000).toLocaleTimeString("en-CA", { timeZone: TZ, hour: "numeric", minute: "2-digit" });
  const fmtDay = t => new Date(t * 1000).toLocaleDateString("en-CA", { timeZone: TZ, weekday: "short", month: "short", day: "numeric" });
  const fmtMB = b => b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : `${Math.max(1, Math.round(b / 1e6))} MB`;
  const fmtLeft = s => s < 50 ? "under a minute left" : `about ${Math.max(1, Math.round(s / 60))} min left`;
  const listeners = [];
  const DL = window.Downloads = { jobs: [], capacity: null, refresh, started, on: fn => listeners.push(fn) };

  // which downloads this browser asked for, so only it fetches them (not a phone signed in
  // as the same person), and only once however many tabs are open
  const KEY = "dlMine";
  const mine = () => { try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch { return {}; } };
  const saveMine = m => { try { localStorage.setItem(KEY, JSON.stringify(m)); } catch {} };
  function started(job) {
    const m = mine(); m[job.id] = { at: Date.now(), got: 0 }; saveMine(m);
    setOpen(true); flash.add(job.id);
    if (!DL.jobs.some(j => j.id === job.id)) DL.jobs.unshift(job);
    draw(); refresh();
  }
  function fetchFile(job) {
    const go = () => {
      const m = mine(), e = m[job.id]; if (!e || e.got) return;
      e.got = Date.now(); saveMine(m);
      const a = document.createElement("a"); a.href = job.url; a.download = ""; a.hidden = true;
      document.body.appendChild(a); a.click(); a.remove();
    };
    if (navigator.locks) navigator.locks.request("dl-fetch", go); else go();
  }

  let open = true; try { open = sessionStorage.getItem("dlTrayShut") !== "1"; } catch {}
  function setOpen(on) { open = on; try { sessionStorage.setItem("dlTrayShut", on ? "" : "1"); } catch {} tray.classList.toggle("shut", !on); head.setAttribute("aria-expanded", on); room(); }
  head.addEventListener("click", () => setOpen(!open));
  // on phones the tray sits across the bottom: leave room so it never covers the end of the page
  const room = () => document.documentElement.style.setProperty("--tray", tray.hidden ? "0px" : tray.offsetHeight + 12 + "px");

  const flash = new Set(), rate = new Map(), seen = new Map();   // rows to flash; id -> [time, pct] first seen copying; id -> last state
  function leftText(j) {
    if (j.note) return j.note;
    if (j.pct >= 99) return "finishing up…";
    const r = rate.get(j.id), t = Date.now() / 1000;
    if (!r) { rate.set(j.id, [t, j.pct]); return ""; }
    if (t - r[0] < 6 || j.pct - r[1] < 2) return "";
    return fmtLeft((100 - j.pct) * (t - r[0]) / (j.pct - r[1]));
  }
  // after this browser tried to fetch a ready file by itself: "auto" while we wait to see it
  // arrive, "held" if it never did (browsers start one download on their own, then hold back
  // more until you click or allow automatic downloads for the site)
  function phase(j, m) {
    const e = m[j.id]; if (!e || !e.got || j.state !== "ready" || j.fetched || j.sending) return "";
    return Date.now() - e.got < 8000 ? "auto" : "held";
  }
  function rowHTML(j, ph) {
    const when = `${fmtDay(j.start)}, ${fmtT(j.start)} – ${fmtT(j.end)}`;
    const top = (x, tip) => `<div class="dlTop"><span class="what"><b>${esc(j.cam_name)}</b> <span class="muted">${when}</span></span><button type="button" class="dlX" data-cancel="${j.id}" title="${tip}" aria-label="${tip}">×</button></div>${x}`;
    const gaps = j.gaps ? ` · ${j.gaps} short gap${j.gaps > 1 ? "s" : ""}` : "";
    if (j.state === "queued") return top(`<div class="dlBar"><i></i></div><div class="dlLine">Waiting for another download to finish first</div>`, "Cancel");
    if (j.state === "saving") return top(`<div class="dlBar"><i></i></div><div class="dlLine"><span>Step 1 of 2 · copying from the camera</span><span class="grow"></span><span class="dlPct"></span></div><div class="dlLine dlEta" hidden></div>`, "Cancel");
    if (j.state === "ready" && j.fetched) return top(`<div class="dlLine"><span class="okTxt">✓ Downloaded</span><span>${fmtMB(j.size)}${gaps}</span><span class="grow"></span><a href="${j.url}" download>Download again</a></div>`, "Remove from this list");
    if (j.state === "ready" && j.sending) return top(`<div class="dlBar busy"><i></i></div><div class="dlLine"><span>Step 2 of 2 · downloading to this device</span><span class="grow"></span><span>${fmtMB(j.size)}</span></div>`, "Delete");
    if (j.state === "ready") return top(`<div class="dlLine"><span class="okTxt">Ready</span><span>${fmtMB(j.size)}${gaps}</span><span class="grow"></span><a class="btn go" href="${j.url}" download>Download</a></div><div class="dlLine tiny${ph === "held" ? " held" : ""}">${
      ph === "auto" ? "Starting the download…" : ph === "held" ? `Your browser didn't let it start by itself. Press Download to save it (kept until ${fmtT(j.expires)}).` : `Kept until ${fmtT(j.expires)}.`}</div>`, "Delete");
    return top(`<div class="dlLine"><span class="err">${j.state === "cancelled" ? "Cancelled" : esc(j.error || "Failed")}</span><span class="grow"></span><button type="button" class="link" data-retry="${j.id}">Try again</button></div>`, "Remove from this list");
  }
  function draw() {
    const m = mine(), jobs = DL.jobs;
    tray.hidden = !jobs.length;
    const keep = new Set(jobs.map(j => j.id));
    for (const li of [...ul.children]) if (!keep.has(li.dataset.id)) li.remove();
    jobs.forEach((j, i) => {
      let li = ul.querySelector(`li[data-id="${j.id}"]`);
      if (!li) { li = document.createElement("li"); li.dataset.id = j.id; }
      if (ul.children[i] !== li) ul.insertBefore(li, ul.children[i] || null);
      const ph = phase(j, m), sig = [j.state, j.fetched, j.sending, j.error, j.gaps, ph].join("|");
      if (li.dataset.sig !== sig) { li.dataset.sig = sig; li.className = "dlJob is-" + j.state; li.innerHTML = rowHTML(j, ph); }
      if (j.state === "saving") {
        li.querySelector(".dlBar i").style.width = j.pct + "%"; li.querySelector(".dlPct").textContent = j.pct + "%";
        const eta = li.querySelector(".dlEta"), txt = leftText(j); eta.hidden = !txt; eta.textContent = txt;
      }
      if (flash.delete(j.id)) { li.classList.remove("flash"); void li.offsetWidth; li.classList.add("flash"); }
    });
    const copying = jobs.filter(j => j.state === "saving"), waiting = jobs.filter(j => j.state === "queued"), ready = jobs.filter(j => j.state === "ready" && !j.fetched);
    sum.textContent = copying.length ? `copying ${copying.length > 1 ? copying.length + " · " : ""}${Math.min(...copying.map(j => j.pct))}%` : waiting.length ? "waiting" : ready.length ? `${ready.length} ready` : "";
    foot.textContent = copying.length || waiting.length ? "You can leave this page. Keep the site open in a tab and the file downloads by itself when it's ready." : ready.length || jobs.some(j => j.fetched) ? "Files are removed a few minutes after you download them, or an hour after they're ready." : "";
    foot.hidden = !foot.textContent;
    document.title = (copying.length ? `⬇ ${Math.min(...copying.map(j => j.pct))}% · ` : ready.length ? "✓ Download ready · " : "") + baseTitle;
    room();
  }

  let timer = null, busy = false, again = false;
  async function refresh() {
    clearTimeout(timer);
    if (busy) { again = true; return; }       // the answer on its way may predate what just changed: ask once more after it
    busy = true;
    try {
      const r = await fetch("/downloads.json"); if (r.ok) { const j = await r.json(); DL.jobs = j.jobs || []; DL.capacity = j.capacity; }
    } catch {} finally { busy = false; }
    if (again) { again = false; return refresh(); }
    const m = mine();
    for (const j of DL.jobs) {
      const was = seen.get(j.id); seen.set(j.id, j.state);
      if (j.state === "ready" && was && was !== "ready") { flash.add(j.id); setOpen(true); }     // finished while we watched
      if (j.state === "ready" && !j.fetched && !j.sending && m[j.id] && !m[j.id].got) fetchFile(j);
    }
    // forget downloads the server has let go of
    let pruned = false; for (const id of Object.keys(m)) if (Date.now() - m[id].at > 3 * 3600e3) { delete m[id]; pruned = true; } if (pruned) saveMine(m);
    draw();
    for (const fn of listeners) { try { fn(DL); } catch {} }
    // keep a close eye while something is moving; with nothing listed, don't poll at all
    // (starting one, another tab starting one, or coming back to the tab all ask again)
    const m2 = mine(), moving = DL.jobs.some(j => j.state === "queued" || j.state === "saving" || j.sending
      || (j.state === "ready" && !j.fetched && m2[j.id] && Date.now() - (m2[j.id].got || Date.now()) < 60000));
    if (DL.jobs.length) timer = setTimeout(refresh, moving ? 2000 : 15000);
  }

  ul.addEventListener("click", async ev => {
    if (ev.target.closest("a[download]")) { setTimeout(refresh, 1500); return; }      // show "downloading" / "downloaded" promptly
    const c = ev.target.closest("[data-cancel]"), r = ev.target.closest("[data-retry]");
    if (c) {
      const j = DL.jobs.find(x => x.id === c.dataset.cancel);
      if (j && j.state === "saving" && !confirm("Stop this download? What's been copied so far is thrown away.")) return;
      c.disabled = true; await fetch(`/downloads/${c.dataset.cancel}/cancel`, { method: "POST" }).catch(() => {}); refresh();
    }
    if (r) {
      const j = DL.jobs.find(x => x.id === r.dataset.retry); if (!j) return;
      r.disabled = true;
      const fd = new FormData(); fd.set("start", j.start); fd.set("end", j.end);
      try {
        const res = await fetch(`/cam/${j.cam_id}/downloads`, { method: "POST", body: fd }), out = await res.json().catch(() => ({}));
        if (!res.ok || !out.ok) throw new Error(out.error || out.detail || "Couldn't start the download.");
        await fetch(`/downloads/${j.id}/cancel`, { method: "POST" }).catch(() => {});
        started(out.job);
      } catch (e) { r.disabled = false; alert(e.message); }
    }
  });
  window.addEventListener("storage", ev => { if (ev.key === KEY) refresh(); });          // another tab started one
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  tray.classList.toggle("shut", !open); head.setAttribute("aria-expanded", open);
  refresh();
})();
