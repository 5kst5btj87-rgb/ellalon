// ---------------------------------------------------------------- plan import: PDF / JPG -> clean (3D) map picture
// Everything happens in the browser on a canvas:
//   improve  = levels (paper -> white, ink -> black), clean background, sharpen, colour style
//   walls    = morphological opening of the dark "ink": only lines thicker than the chosen thickness survive,
//              so text, door arcs and furniture are not walls
//   3D       = walls raised in place (floor positions unchanged, so objects on an existing map stay right);
//              thin strokes (all names and labels) are drawn back on top exactly as they were
let piSrc = null, piRot = 0, piCropR = null, piCropping = false, piOut = null, piT = null, piFileName = "", piBusy = false;
const PI_MAX = 2600;                      // longest side we work at

async function loadPlanImport() {
  let list = [];
  try { list = await api("/api/maps"); } catch {}
  $("#piMap").innerHTML = list.map((m) => `<option value="${m.id}">${esc(m.name)}</option>`).join("") || `<option value="">No maps yet</option>`;
  document.querySelector('input[name="piTo"][value="old"]').disabled = !list.length;
  piMapsCache = list; piNameCheck();
  document.querySelectorAll("#tab-planimport .pistep").forEach((e) => e.classList.toggle("off", !piSrc));
  if (me.role !== "admin") $("#piMsg").innerHTML = `<span class="err">Only administrators can save maps.</span>`;
}
let piMapsCache = [];

// ---- loading
$("#piFile").onchange = (e) => { const f = e.target.files[0]; e.target.value = ""; if (f) piLoadFile(f); };
const piDropEl = $("#piDrop");
piDropEl.addEventListener("dragover", (e) => { e.preventDefault(); piDropEl.classList.add("over"); });
piDropEl.addEventListener("dragleave", () => piDropEl.classList.remove("over"));
piDropEl.addEventListener("drop", (e) => { e.preventDefault(); piDropEl.classList.remove("over"); const f = e.dataTransfer.files[0]; if (f) piLoadFile(f); });
async function piLoadFile(f) {
  piFileName = f.name.replace(/\.[^.]+$/, "");
  $("#piPages").innerHTML = ""; $("#piState").textContent = "Reading the file…";
  if (!$("#piName").value || $("#piName").dataset.auto === "1") { $("#piName").value = piFileName.slice(0, 80); $("#piName").dataset.auto = "1"; }
  piNameCheck();
  if (f.type === "application/pdf" || /\.pdf$/i.test(f.name)) {
    const fd = new FormData(); fd.append("file", f);
    try {
      const r = await fetch("/api/maps/pdf-pages", { method: "POST", body: fd, headers: { Authorization: "Bearer " + token } });
      if (!r.ok) { let d = `HTTP ${r.status}`; try { d = (await r.json()).detail || d; } catch {} throw new Error(d); }
      const { pages } = await r.json();
      $("#piPages").innerHTML = pages.length > 1 ? `<div class="small muted">Choose the page with the plan:</div>` + pages.map((p, i) => `<img src="${p}" data-i="${i}" title="Page ${i + 1}">`).join("") : "";
      $("#piPages").querySelectorAll("img").forEach((im) => (im.onclick = () => { $("#piPages").querySelectorAll("img").forEach((x) => x.classList.toggle("on", x === im)); piSetImage(im.src); }));
      $("#piPages").querySelector("img")?.classList.add("on");
      piSetImage(pages[0]);
    } catch (err) { $("#piState").innerHTML = `<span class="err">${esc(err.message)}</span>`; }
  } else if (/^image\//.test(f.type)) piSetImage(URL.createObjectURL(f));
  else $("#piState").innerHTML = `<span class="err">Use a PDF, JPG or PNG file.</span>`;
}
function piSetImage(url) {
  const im = new Image();
  im.onload = () => { piSrc = im; piRot = 0; piCropR = null; document.querySelectorAll("#tab-planimport .pistep").forEach((e) => e.classList.remove("off")); $("#piEmpty").classList.add("hidden"); piRender(); };
  im.onerror = () => ($("#piState").innerHTML = `<span class="err">Could not open this picture.</span>`);
  im.src = url;
}

// ---- controls
["piStyle", "piStr", "piClean", "piSharp", "pi3d", "piH", "piT", "piShowW"].forEach((id) => ($("#" + id).oninput = () => piSoon()));
$("#piRotL").onclick = () => { piRot = (piRot + 270) % 360; piCropR = null; piRender(); };
$("#piRotR").onclick = () => { piRot = (piRot + 90) % 360; piCropR = null; piRender(); };
$("#piCropReset").onclick = () => { piCropR = null; piCropping = false; $("#piCrop").classList.remove("on"); piRender(); };
$("#piCrop").onclick = () => { piCropping = !piCropping; $("#piCrop").classList.toggle("on", piCropping); if (piCropping && piCropR) { piCropR = null; piRender(); } };
function piSoon() { clearTimeout(piT); piT = setTimeout(piRender, 180); $("#piStrV").textContent = $("#piStr").value + "%"; }

// crop by dragging on the picture (in fractions of the uncropped, rotated picture)
(() => {
  const v = $("#piView"), box = $("#piCropBox");
  v.addEventListener("pointerdown", (e) => {
    if (!piCropping || !piSrc) return;
    const c = $("#piCanvas").getBoundingClientRect(), x0 = e.clientX, y0 = e.clientY;
    const mv = (ev) => {
      const l = Math.max(c.left, Math.min(x0, ev.clientX)), r = Math.min(c.right, Math.max(x0, ev.clientX));
      const t = Math.max(c.top, Math.min(y0, ev.clientY)), b = Math.min(c.bottom, Math.max(y0, ev.clientY));
      const vr = v.getBoundingClientRect();
      Object.assign(box.style, { left: l - vr.left + v.scrollLeft + "px", top: t - vr.top + v.scrollTop + "px", width: r - l + "px", height: b - t + "px" });
      box.classList.remove("hidden");
      box.dataset.r = JSON.stringify([(l - c.left) / c.width, (t - c.top) / c.height, (r - c.left) / c.width, (b - c.top) / c.height]);
    };
    const up = () => {
      window.removeEventListener("pointermove", mv); window.removeEventListener("pointerup", up);
      box.classList.add("hidden");
      const r = box.dataset.r ? JSON.parse(box.dataset.r) : null; box.dataset.r = "";
      if (r && r[2] - r[0] > 0.03 && r[3] - r[1] > 0.03) {
        const cur = piCropR || [0, 0, 1, 1], w = cur[2] - cur[0], h = cur[3] - cur[1];   // crop again inside the current crop
        piCropR = [cur[0] + r[0] * w, cur[1] + r[1] * h, cur[0] + r[2] * w, cur[1] + r[3] * h];
        piCropping = false; $("#piCrop").classList.remove("on"); piRender();
      }
    };
    window.addEventListener("pointermove", mv); window.addEventListener("pointerup", up);
  });
  const hold = (on) => { if (piOut && piSrc) { piShowCanvas(on ? piBase() : piOut); } };
  $("#piBefore").addEventListener("pointerdown", () => hold(true));
  ["pointerup", "pointerleave"].forEach((t) => $("#piBefore").addEventListener(t, () => hold(false)));
})();

// ---- the processing
function piBase() {                       // rotated + cropped original, at most PI_MAX px
  const s = piSrc, rot = piRot % 180 !== 0;
  const W0 = rot ? s.naturalHeight : s.naturalWidth, H0 = rot ? s.naturalWidth : s.naturalHeight;
  const cr = piCropR || [0, 0, 1, 1];
  const cw = W0 * (cr[2] - cr[0]), ch = H0 * (cr[3] - cr[1]), k = Math.min(1, PI_MAX / Math.max(cw, ch));
  const out = document.createElement("canvas"); out.width = Math.round(cw * k); out.height = Math.round(ch * k);
  const g = out.getContext("2d");
  g.fillStyle = "#fff"; g.fillRect(0, 0, out.width, out.height);
  g.scale(k, k); g.translate(-cr[0] * W0, -cr[1] * H0);
  g.translate(W0 / 2, H0 / 2); g.rotate(piRot * Math.PI / 180); g.drawImage(s, -s.naturalWidth / 2, -s.naturalHeight / 2);
  return out;
}
function boxSum(src, w, h, r, horiz) {     // running window sum along rows or columns
  const out = new Uint16Array(w * h);
  if (horiz) for (let y = 0; y < h; y++) { let acc = 0; const o = y * w;
    for (let x = -r; x < w; x++) { if (x + r < w) acc += src[o + x + r]; if (x - r - 1 >= 0) acc -= src[o + x - r - 1]; if (x >= 0) out[o + x] = acc; } }
  else for (let x = 0; x < w; x++) { let acc = 0;
    for (let y = -r; y < h; y++) { if (y + r < h) acc += src[(y + r) * w + x]; if (y - r - 1 >= 0) acc -= src[(y - r - 1) * w + x]; if (y >= 0) out[y * w + x] = acc; } }
  return out;
}
function erode(m, w, h, r) {               // pixel stays only if the whole (2r+1)^2 square is ink
  const n = 2 * r + 1, a = boxSum(m, w, h, r, true), t = new Uint8Array(w * h);
  for (let i = 0; i < t.length; i++) t[i] = a[i] === n ? 1 : 0;
  const b = boxSum(t, w, h, r, false), o = new Uint8Array(w * h);
  for (let i = 0; i < o.length; i++) o[i] = b[i] === n ? 1 : 0;
  return o;
}
function dilate(m, w, h, r) {
  const a = boxSum(m, w, h, r, true), t = new Uint8Array(w * h);
  for (let i = 0; i < t.length; i++) t[i] = a[i] > 0 ? 1 : 0;
  const b = boxSum(t, w, h, r, false), o = new Uint8Array(w * h);
  for (let i = 0; i < o.length; i++) o[i] = b[i] > 0 ? 1 : 0;
  return o;
}
function keepLong(m, w, h, minLen) {     // walls are long and connected; letters, symbols and furniture are not
  const lab = new Int32Array(w * h), out = new Uint8Array(w * h), stack = new Int32Array(w * h);
  let id = 0;
  for (let s0 = 0; s0 < m.length; s0++) {
    if (!m[s0] || lab[s0]) continue;
    id++; let sp = 0, x1 = w, y1 = h, x2 = 0, y2 = 0, cnt = 0; stack[sp++] = s0; lab[s0] = id;
    const pix = [];
    while (sp) {
      const i = stack[--sp], x = i % w, y = (i / w) | 0; pix.push(i); cnt++;
      if (x < x1) x1 = x; if (x > x2) x2 = x; if (y < y1) y1 = y; if (y > y2) y2 = y;
      if (x > 0 && m[i - 1] && !lab[i - 1]) { lab[i - 1] = id; stack[sp++] = i - 1; }
      if (x < w - 1 && m[i + 1] && !lab[i + 1]) { lab[i + 1] = id; stack[sp++] = i + 1; }
      if (y > 0 && m[i - w] && !lab[i - w]) { lab[i - w] = id; stack[sp++] = i - w; }
      if (y < h - 1 && m[i + w] && !lab[i + w]) { lab[i + w] = id; stack[sp++] = i + w; }
    }
    if (Math.max(x2 - x1, y2 - y1) >= minLen) for (const i of pix) out[i] = 1;
  }
  return out;
}
function piRender() {
  if (!piSrc || piBusy) { if (piSrc) piSoon(); return; }
  piBusy = true; $("#piState").textContent = "Working…";
  setTimeout(() => {
    try { piOut = piProcess(); piShowCanvas(piOut); $("#piState").textContent = `${piOut.width} × ${piOut.height} px`; }
    catch (err) { $("#piState").innerHTML = `<span class="err">${esc(err.message)}</span>`; }
    piBusy = false;
  }, 20);
}
function piProcess() {
  const base = piBase(), w = base.width, h = base.height, g0 = base.getContext("2d");
  const img = g0.getImageData(0, 0, w, h), d = img.data, n = w * h;
  const style = $("#piStyle").value, str = +$("#piStr").value / 100, clean = $("#piClean").checked, sharp = $("#piSharp").checked;
  // luminance + levels from the histogram (paper = 85th percentile, ink = 2nd percentile)
  const L = new Float32Array(n), hist = new Uint32Array(256);
  for (let i = 0, j = 0; i < n; i++, j += 4) { const v = 0.299 * d[j] + 0.587 * d[j + 1] + 0.114 * d[j + 2]; L[i] = v; hist[v | 0]++; }
  const pct = (p) => { let acc = 0; for (let v = 0; v < 256; v++) { acc += hist[v]; if (acc >= n * p) return v; } return 255; };
  const lo = pct(0.02), hi = Math.max(lo + 20, pct(0.85));
  const lev = new Float32Array(n);
  for (let i = 0; i < n; i++) { const s = Math.min(1, Math.max(0, (L[i] - lo) / (hi - lo))); lev[i] = (1 - str) * (L[i] / 255) + str * s; }
  if (sharp) {                             // unsharp mask on the levelled luminance
    const bl = new Float32Array(n);
    for (let y = 1; y < h - 1; y++) for (let x = 1; x < w - 1; x++) { const i = y * w + x;
      bl[i] = (lev[i] * 4 + lev[i - 1] + lev[i + 1] + lev[i - w] + lev[i + w]) / 8; }
    for (let y = 1; y < h - 1; y++) for (let x = 1; x < w - 1; x++) { const i = y * w + x; lev[i] = Math.min(1, Math.max(0, lev[i] + 0.8 * (lev[i] - bl[i]))); }
  }
  if (clean) for (let i = 0; i < n; i++) if (lev[i] > 0.86) lev[i] = 1;      // paper, stains, noise -> white
  // ink and walls
  const ink = new Uint8Array(n);
  for (let i = 0; i < n; i++) ink[i] = lev[i] < 0.55 ? 1 : 0;
  if (clean) for (let y = 1; y < h - 1; y++) for (let x = 1; x < w - 1; x++) {   // lone specks from the scanner
    const i = y * w + x;
    if (ink[i] && ink[i - 1] + ink[i + 1] + ink[i - w] + ink[i + w] + ink[i - w - 1] + ink[i - w + 1] + ink[i + w - 1] + ink[i + w + 1] < 2) { ink[i] = 0; lev[i] = 1; }
  }
  const r = Math.max(1, Math.round(+$("#piT").value * Math.max(w, h) / 2600));
  const walls = keepLong(dilate(erode(ink, w, h, r), w, h, r), w, h, Math.max(14 * r, Math.round(0.035 * Math.max(w, h))));
  // colour the floor plan in the chosen style
  const pal = { clean: [[255, 255, 255], [20, 24, 30]], soft: [[236, 240, 245], [40, 46, 56]], blueprint: [[14, 42, 74], [200, 230, 255]] }[style];
  const out = g0.createImageData(w, h), o = out.data;
  for (let i = 0, j = 0; i < n; i++, j += 4) {
    const t = lev[i];
    if (style === "original") {            // keep colours, apply the levels to each channel
      const f = L[i] > 1 ? (t * 255) / L[i] : 0;
      o[j] = Math.min(255, d[j] * f); o[j + 1] = Math.min(255, d[j + 1] * f); o[j + 2] = Math.min(255, d[j + 2] * f);
    } else for (let c = 0; c < 3; c++) o[j + c] = pal[1][c] + (pal[0][c] - pal[1][c]) * t;
    o[j + 3] = 255;
  }
  const flat = document.createElement("canvas"); flat.width = w; flat.height = h; flat.getContext("2d").putImageData(out, 0, 0);
  const show = $("#piShowW").checked, threeD = $("#pi3d").checked;
  if (!threeD && !show) return flat;
  const res = document.createElement("canvas"); res.width = w; res.height = h;
  const g = res.getContext("2d");
  const maskCanvas = (rgb, alpha = 255) => {
    const c = document.createElement("canvas"); c.width = w; c.height = h;
    const cg = c.getContext("2d"), id = cg.createImageData(w, h), p = id.data;
    for (let i = 0, j = 0; i < n; i++, j += 4) if (walls[i]) { p[j] = rgb[0]; p[j + 1] = rgb[1]; p[j + 2] = rgb[2]; p[j + 3] = alpha; }
    cg.putImageData(id, 0, 0); return c;
  };
  if (!threeD) { g.drawImage(flat, 0, 0); g.globalAlpha = 0.55; g.drawImage(maskCanvas([255, 40, 40]), 0, 0); g.globalAlpha = 1; return res; }
  // floor: the plan without the walls
  const floorCol = style === "blueprint" ? pal[0] : style === "soft" ? pal[0] : [246, 247, 249];
  const floor = g0.createImageData(w, h), f = floor.data;
  for (let i = 0, j = 0; i < n; i++, j += 4) { if (walls[i]) { f[j] = floorCol[0]; f[j + 1] = floorCol[1]; f[j + 2] = floorCol[2]; } else { f[j] = o[j]; f[j + 1] = o[j + 1]; f[j + 2] = o[j + 2]; } f[j + 3] = 255; }
  const fc = document.createElement("canvas"); fc.width = w; fc.height = h; fc.getContext("2d").putImageData(floor, 0, 0);
  g.drawImage(fc, 0, 0);
  // walls: soft shadow, then the raised wall (sides, then the lit top)
  const H = Math.round(+$("#piH").value * Math.max(w, h) / 2600), dx = -0.45, dy = -1;
  const dark = style === "blueprint", side = maskCanvas(dark ? [64, 120, 170] : [120, 128, 140]), top = maskCanvas(dark ? [190, 225, 255] : [58, 64, 74]);
  const black = maskCanvas([0, 0, 0]);
  g.save(); g.filter = `blur(${Math.max(2, H / 2.5)}px)`; g.globalAlpha = dark ? 0.55 : 0.32;
  g.drawImage(black, H * 0.55, H * 0.7); g.restore();
  for (let i = 1; i <= H; i++) {
    g.globalAlpha = 1; g.drawImage(side, dx * i, dy * i);
    if (i % 3 === 0) { g.globalAlpha = 0.06; g.drawImage(black, dx * i, dy * i); }   // shading towards the top
  }
  g.globalAlpha = 1; g.drawImage(top, dx * H, dy * H);
  // edge highlight on the wall tops
  g.globalAlpha = 0.5; g.drawImage(maskCanvas(dark ? [255, 255, 255] : [150, 158, 170]), dx * H - 1, dy * H - 1); g.drawImage(top, dx * H, dy * H); g.globalAlpha = 1;
  // names, labels, doors, furniture (everything thin) on top, exactly where they were
  const thin = g0.createImageData(w, h), tp = thin.data, near = dilate(walls, w, h, 1);
  for (let i = 0, j = 0; i < n; i++, j += 4) if (ink[i] && !near[i]) { tp[j] = o[j]; tp[j + 1] = o[j + 1]; tp[j + 2] = o[j + 2]; tp[j + 3] = 255; }
  const tc = document.createElement("canvas"); tc.width = w; tc.height = h; tc.getContext("2d").putImageData(thin, 0, 0);
  g.drawImage(tc, 0, 0);
  if (show) { g.globalAlpha = 0.5; g.drawImage(maskCanvas([255, 40, 40]), 0, 0); g.globalAlpha = 1; }
  return res;
}
function piShowCanvas(c) {
  const cv = $("#piCanvas"); cv.width = c.width; cv.height = c.height; cv.getContext("2d").drawImage(c, 0, 0);
  $("#piCanvas").classList.toggle("cropping", piCropping);
}

// ---- saving
function piNameCheck() {
  const name = $("#piName").value.trim(), clash = piMapsCache.find((m) => m.name.toLowerCase() === name.toLowerCase());
  const warn = $("#piNameWarn");
  warn.classList.toggle("hidden", !clash || document.querySelector('input[name="piTo"]:checked').value !== "new");
  if (clash) warn.innerHTML = `A map called “${esc(clash.name)}” already exists. Choose another name, or <a class="link" onclick="piUseExisting('${clash.id}')">replace its picture</a> (its name and objects stay).`;
}
window.piUseExisting = (id) => { document.querySelector('input[name="piTo"][value="old"]').checked = true; $("#piMap").value = id; piNameCheck(); };
$("#piName").oninput = () => { $("#piName").dataset.auto = "0"; piNameCheck(); };
document.querySelectorAll('input[name="piTo"]').forEach((r) => (r.onchange = piNameCheck));
$("#piMap").onchange = () => { document.querySelector('input[name="piTo"][value="old"]').checked = true; piNameCheck(); };
$("#piSave").onclick = async () => {
  const msg = $("#piMsg");
  if (!piOut) { msg.className = "small err"; msg.textContent = "Choose a floor plan first."; return; }
  const toNew = document.querySelector('input[name="piTo"]:checked').value === "new", name = $("#piName").value.trim();
  if (toNew && !name) { msg.className = "small err"; msg.textContent = "Give the map a name."; return; }
  if (toNew && piMapsCache.some((m) => m.name.toLowerCase() === name.toLowerCase())) { msg.className = "small err"; msg.textContent = "That name is taken - choose another, or replace the picture of the existing map."; return; }
  msg.className = "small muted"; msg.textContent = "Saving…";
  try {
    let blob = await new Promise((ok) => piOut.toBlob(ok, "image/png"));
    if (blob.size > 14 * 1024 * 1024) blob = await new Promise((ok) => piOut.toBlob(ok, "image/jpeg", 0.92));
    let id;
    if (toNew) id = (await api("/api/maps", { method: "POST", body: { name } })).id;
    else id = $("#piMap").value;
    const fd = new FormData(); fd.append("file", new File([blob], blob.type === "image/png" ? "plan.png" : "plan.jpg", { type: blob.type }));
    const r = await fetch(`/api/maps/${id}/image`, { method: "PUT", body: fd, headers: { Authorization: "Bearer " + token } });
    if (!r.ok) { let d = `HTTP ${r.status}`; try { d = (await r.json()).detail || d; } catch {} throw new Error(d); }
    const saved = await r.json();
    msg.className = "small ok"; msg.innerHTML = `Saved to “${esc(saved.name)}”. <a class="link" onclick="piOpenMap('${id}')">Open in Maps →</a>${toNew ? " Then click “Edit map” to place cameras and doors." : ""}`;
    loadPlanImport();
  } catch (err) { msg.className = "small err"; msg.textContent = errText(err); }
};
window.piOpenMap = (id) => { try { localStorage.setItem("ellalon_map", id); } catch {} if (typeof maps !== "undefined") { maps = []; mapCur = null; } show("maps"); };
