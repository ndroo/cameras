// Apply a camera's saved orientation (rotate / mirror / flip) to its thumbnail.
export function transformFor(rot, fh, fv) {
  const t = [];
  if (rot) t.push(`rotate(${rot}deg)`);
  if (fh) t.push("scaleX(-1)");
  if (fv) t.push("scaleY(-1)");
  return t.join(" ") || "none";
}
export function applyToBox(box, el, rot, fh, fv) {
  const sideways = rot === 90 || rot === 270;
  el.style.transform = transformFor(rot, fh, fv);
  if (sideways) {
    // the element is rotated, so its width must equal the box height and vice versa
    const w = box.clientWidth, h = box.clientHeight;
    el.style.width = h + "px"; el.style.height = w + "px";
    el.style.left = (w - h) / 2 + "px"; el.style.top = (h - w) / 2 + "px";
  } else {
    el.style.width = el.style.height = "100%"; el.style.left = el.style.top = "0";
  }
}
for (const box of document.querySelectorAll(".thumb[data-rotate]")) {
  const rot = +box.dataset.rotate, fh = +box.dataset.fh, fv = +box.dataset.fv;
  const img = box.querySelector("img");
  const go = () => applyToBox(box, img, rot, fh, fv);
  go(); new ResizeObserver(go).observe(box);
}
