// Admin page: a section menu (one section on screen at a time) and Save buttons that save in
// place, so several rows can be saved without the page reloading.

// ---------------------------------------------------------------- section menu
const nav = document.getElementById("adminNav");
const sections = [...document.querySelectorAll(".adminBody > section[id]")];
const ids = sections.map(s => s.id);
function show(id) {
  if (!ids.includes(id)) id = "cameras";
  for (const s of sections) s.hidden = s.id !== id;
  for (const a of nav.querySelectorAll("a")) a.classList.toggle("on", a.getAttribute("href") === "#" + id);
  try { sessionStorage.setItem("admin:tab", id); } catch {}
}
let first = location.hash.slice(1);
if (!first) { try { first = sessionStorage.getItem("admin:tab") || ""; } catch {} }
show(first);
// the #section in the address would otherwise make the browser jump down to it
window.addEventListener("load", () => { if (location.hash) window.scrollTo(0, 0); });
nav.addEventListener("click", ev => {
  const a = ev.target.closest("a"); if (!a) return;
  ev.preventDefault(); const id = a.getAttribute("href").slice(1);
  history.replaceState(null, "", "#" + id); show(id); window.scrollTo({ top: 0 });
});
window.addEventListener("hashchange", () => show(location.hash.slice(1)));

// ---------------------------------------------------------------- save in place
// Only the plain Save button is handled here; Delete and other buttons (Link) submit normally.
for (const form of document.querySelectorAll("form[data-ajax]")) {
  form.addEventListener("submit", async ev => {
    const btn = ev.submitter;
    if (!btn || btn.name === "delete" || btn.hasAttribute("formaction")) return;
    ev.preventDefault();
    const label = btn.textContent;
    btn.disabled = true; btn.textContent = "Saving…";
    const done = (txt, cls) => {
      btn.textContent = txt; btn.classList.add(cls);
      setTimeout(() => { btn.textContent = label; btn.classList.remove(cls); btn.disabled = false; }, cls === "saved" ? 1500 : 4000);
    };
    try {
      const r = await fetch(form.action, { method: "POST", body: new FormData(form, btn), credentials: "same-origin" });
      const u = new URL(r.url);
      if (u.pathname.startsWith("/login")) { location.reload(); return; }      // signed out meanwhile
      const msg = u.searchParams.get("msg");
      if (!r.ok || msg) { done("Not saved", "failed"); btn.title = msg || `Error ${r.status}`; return; }
      // a passcode typed into a person row has been set: clear it so it isn't sent again
      const pc = form.querySelector("input[name=passcode]");
      if (pc && pc.value) { pc.value = ""; pc.placeholder = "set ✓"; }
      btn.title = ""; done("Saved ✓", "saved");
    } catch (e) {
      done("Not saved", "failed"); btn.title = String(e);
    }
  });
}

// links inside a section that point at another section (e.g. System -> Recent texts)
for (const a of document.querySelectorAll("a[data-tab]")) a.addEventListener("click", ev => { ev.preventDefault(); history.replaceState(null, "", "#" + a.dataset.tab); show(a.dataset.tab); window.scrollTo({ top: 0 }); });

// ---------------------------------------------------------------- groups: card <-> editor
function closeEditor(card) {
  const form = card.querySelector("form"); if (form) { form.reset(); syncGroupForm(form); }
  if (card.classList.contains("gnew")) card.hidden = true;
  else { card.querySelector(".gsum").hidden = false; card.querySelector(".gedit").hidden = true; }
  card.classList.remove("editing");
}
for (const b of document.querySelectorAll("[data-edit]")) b.addEventListener("click", () => {
  const card = document.getElementById(b.dataset.edit);
  for (const c of document.querySelectorAll(".gcard.editing")) if (c !== card) closeEditor(c);   // one editor at a time
  card.hidden = false; card.classList.add("editing");
  const sum = card.querySelector(".gsum"); if (sum) sum.hidden = true;
  card.querySelector(".gedit").hidden = false;
  card.scrollIntoView({ block: "nearest", behavior: "smooth" });
  const name = card.querySelector("input[name=name]"); if (card.classList.contains("gnew")) name.focus();
});
for (const b of document.querySelectorAll("[data-cancel]")) b.addEventListener("click", () => closeEditor(b.closest(".gcard")));

// only the chosen property's cameras are offered; "every camera" greys the list out
function syncGroupForm(form) {
  const houseEl = form.querySelector("[data-house]"); if (!houseEl) return;   // not a group form (e.g. garage)
  const house = houseEl.value;
  const all = form.querySelector("input[name=all_cameras][value='1']").checked;
  const pick = form.querySelector(".camPick"); let n = 0;
  for (const chip of pick.querySelectorAll(".pchip")) {
    const mine = chip.dataset.house === house; chip.hidden = !mine; if (mine) n++;
    const box = chip.querySelector("input"); if (!mine) box.checked = false;
  }
  pick.classList.toggle("off", all);
  pick.querySelector(".camNone").hidden = n > 0;
}
for (const form of document.querySelectorAll("form.gform")) {
  syncGroupForm(form);
  form.addEventListener("change", ev => { if (ev.target.matches("[data-house], input[name=all_cameras]")) syncGroupForm(form); });
  const camPick = form.querySelector(".camPick"); if (!camPick) continue;   // garage form has no camera picker
  // ticking a camera means "only some"
  camPick.addEventListener("click", ev => {
    if (!ev.target.closest(".pchip")) return;
    const some = form.querySelector("input[name=all_cameras][value='0']");
    if (!some.checked) { some.checked = true; syncGroupForm(form); }
  });
}


// ---------------------------------------------------------------- garage: test the relay answers
for (const b of document.querySelectorAll("[data-test]")) b.addEventListener("click", async () => {
  const out = document.getElementById("optest-" + b.dataset.test);
  b.disabled = true; if (out) { out.textContent = "checking…"; out.className = "small muted"; }
  try {
    const r = await fetch(`/admin/garage/${b.dataset.test}/test`, { method: "POST", credentials: "same-origin" });
    const j = await r.json();
    if (out) { out.textContent = j.ok ? "✓ reachable" : "✗ " + (j.error || "no answer"); out.className = "small " + (j.ok ? "ok-txt" : "err"); }
  } catch (e) { if (out) { out.textContent = "✗ " + e; out.className = "small err"; } }
  b.disabled = false;
});
