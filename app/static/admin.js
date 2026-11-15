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
