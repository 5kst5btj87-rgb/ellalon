// ---------------------------------------------------------------- maps
// Floor plans with cameras, doors, inputs, outputs and links to other maps. Icons follow the live
// status of what they stand for (camera online/offline, door locked/unlocked/open/forced, input
// alarm/armed/disarmed, output on/off, open alarms). Positions are stored as fractions of the plan.
let maps = [], mapCur = null, mapEditing = false, mapSel = null, mapZoom = 1, mapHist = [];
let mapDevs = [], mapIntTypes = null, mapIntSystems = [], mapAlarms = [], mapSaveT = null, mapRefreshT = null;
const MAP_W0 = 1600, MAP_H0 = 1000;          // canvas size of a map without a picture

const MI = {
  camera: '<path d="M4 8h10a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2H4z"/><path d="m16 11 4-2.5v7L16 13"/>',
  door_locked: '<rect x="6" y="3" width="12" height="18" rx="1"/><rect x="9.5" y="11" width="5" height="4" rx="1"/><path d="M10.5 11V9.5a1.5 1.5 0 0 1 3 0V11"/>',
  door_unlocked: '<rect x="6" y="3" width="12" height="18" rx="1"/><rect x="9.5" y="11" width="5" height="4" rx="1"/><path d="M10.5 11V8.5a1.5 1.5 0 0 1 3 0"/>',
  door_open: '<path d="M5 21V3h9v18"/><path d="M14 3l5 2v16l-5-1"/><circle cx="16" cy="12" r=".8"/>',
  door_alarm: '<rect x="6" y="3" width="12" height="18" rx="1"/><path d="M12 8v5M12 16v.5"/>',
  input: '<circle cx="12" cy="12" r="3"/><path d="M6.5 6.5a8 8 0 0 0 0 11M17.5 6.5a8 8 0 0 1 0 11"/>',
  output: '<path d="M12 3v8"/><path d="M7 6.5a7 7 0 1 0 10 0"/>',
  device: '<rect x="5" y="5" width="14" height="14" rx="3"/><circle cx="12" cy="12" r="2.5"/>',
  link: '<path d="M3 6.5 9 4l6 2.5L21 4v13.5L15 20l-6-2.5L3 20z"/><path d="M9 4v13.5M15 6.5V20"/>',
  label: '<path d="M5 6h14M12 6v13"/>',
};
const MAP_STATES = {          // status -> [colour, legend text]
  ok: ["#34c77b", "Normal / locked / online"],
  alarm: ["#ff4d4f", "Alarm (blinks)"],
  warn: ["#ffb340", "Open / held / active"],
  unlocked: ["#3fa7ff", "Unlocked / output on"],
  disarmed: ["#9b8cff", "Disarmed / bypassed / mode changed"],
  offline: ["#7d8796", "Offline / fault / unknown"],
};

// -------------------------------------------------------------- data
async function loadMaps() {
  const admin = me.role === "admin";
  $("#mapAdmin").classList.toggle("hidden", !admin);
  $("#mapNew").classList.toggle("hidden", !admin);
  $("#mapLegend").innerHTML = Object.entries(MAP_STATES).map(([k, [c, t]]) => `<div><span class="mdot" style="--c:${c}"></span>${t}</div>`).join("");
  try { maps = await api("/api/maps"); } catch (err) { maps = []; $("#mapList").innerHTML = `<p class="err small">${esc(errText(err))}</p>`; }
  await mapRefreshStatus(false);
  const want = mapCur?.id || (() => { try { return localStorage.getItem("ellalon_map"); } catch { return null; } })();
  mapOpen(maps.find((x) => x.id === want) || maps[0] || null, false);
}
async function mapRefreshStatus(draw = true) {
  const jobs = [api("/api/cameras").then((c) => (cameras = c)).catch(() => {})];
  jobs.push(mod("integrations", "/devices").then((d) => (mapDevs = d)).catch(() => (mapDevs = [])));
  if (!mapIntTypes) jobs.push(mod("integrations", "/types").then((t) => (mapIntTypes = t)).catch(() => {}));
  jobs.push(mod("integrations", "/systems").then((s) => (mapIntSystems = s)).catch(() => {}));
  jobs.push(mod("c2", "/incidents").then((r) => (mapAlarms = r.filter((i) => i.status !== "closed"))).catch(() => (mapAlarms = [])));
  await Promise.all(jobs);
  if (draw && mapCur) { mapDrawItems(); mapRenderList(); if (!$("#mapPop").classList.contains("hidden") && mapPopItem) mapPopup(mapPopItem, true); }
}
function mapOnEvent(ev) {
  if ($("#tab-maps").classList.contains("hidden")) return;
  if (ev.type.startsWith("camera.") || ev.type.startsWith("incident.") || ev.source === "integrations") {
    clearTimeout(mapRefreshT); mapRefreshT = setTimeout(() => mapRefreshStatus(), 700);
  }
}
setInterval(() => { if (me && !$("#tab-maps").classList.contains("hidden") && !mapEditing) mapRefreshStatus(); }, 10000);

// -------------------------------------------------------------- status of an item
const devOf = (it) => mapDevs.find((d) => d.id === it.ref);
const camOf = (it) => cameras.find((c) => String(c.id) === String(it.ref));
function alarmsFor(it) {
  return mapAlarms.filter((i) => {
    const p = i.event?.payload || {};
    if (it.type === "camera") return String(i.camera_id) === String(it.ref);
    return p.device_id && p.device_id === it.ref;
  });
}
function mapStatus(it, depth = 0) {
  if (it.type === "label") return { s: "ok", text: "" };
  if (it.type === "link") {
    const m = maps.find((x) => x.id === it.ref);
    if (!m) return { s: "offline", text: "Map deleted" };
    if (depth > 3) return { s: "ok", text: "" };
    const sts = (m.items || []).map((x) => mapStatus(x, depth + 1).s);
    const n = sts.filter((x) => x === "alarm").length;
    return n ? { s: "alarm", text: `${n} in alarm` } : sts.includes("offline") ? { s: "warn", text: "Something offline" } : { s: "ok", text: "All normal" };
  }
  const alarms = alarmsFor(it);
  if (it.type === "camera") {
    const c = camOf(it);
    if (!c) return { s: "offline", text: "Camera removed" };
    if (alarms.length) return { s: "alarm", text: `${alarms.length} open alarm(s)` };
    if (!c.enabled) return { s: "offline", text: "Disabled" };
    const st = c.status?.state || "unknown";
    return st === "online" ? { s: "ok", text: "Online" + (c.record ? " · recording" : "") } : { s: "offline", text: st.charAt(0).toUpperCase() + st.slice(1) };
  }
  const d = devOf(it);
  if (!d) return { s: "offline", text: "Not found - integrations not running or device removed" };
  const sys = mapIntSystems.find((x) => x.id === d.system_id);
  if (sys && sys.status && !["connected", "manual"].includes(sys.status)) return { s: "offline", text: `${sys.name}: ${sys.status}` };
  const st = d.state || {}, raw = String(st.state ?? st.last_event ?? ""), low = raw.toLowerCase();
  const nice = raw.replace(/([a-z])([A-Z])/g, "$1 $2").replace(/_/g, " ");
  if (alarms.length || st.alarmed || st.triggered && d.kind === "partition" || /forced|alarm|tamper|duress/.test(low)) return { s: "alarm", text: nice || "Alarm", door: "alarm" };
  if (/offline|fault|trouble|lost|unknown|disconnect/.test(low)) return { s: "offline", text: nice };
  if (d.kind === "door") {
    if (/held|open/.test(low) && !/close/.test(low)) return { s: "warn", text: nice, door: "open" };
    if (/unlock/.test(low)) return { s: "unlocked", text: nice, door: "unlocked" };
    if (/card|pin|facility|cypher/.test(low)) return { s: "disarmed", text: nice, door: "locked" };
    return { s: "ok", text: nice || "Locked", door: "locked" };
  }
  if (d.kind === "output") return /\bon\b|active|energ|pulse/.test(low) ? { s: "unlocked", text: nice } : { s: "ok", text: nice || "Off" };
  if (st.bypassed || /disarm|bypass|mask/.test(low)) return { s: "disarmed", text: nice || "Bypassed" };
  if (d.kind === "zone" && st.triggered || /active|open|trigger/.test(low)) return { s: "warn", text: nice || "Open" };
  if (d.kind === "partition") return st.armed || st.partially_armed ? { s: "ok", text: st.armed ? "Armed" : "Stay armed" } : { s: "disarmed", text: "Disarmed" };
  return { s: "ok", text: nice || "Normal" };
}
function itemName(it) {
  if (it.label) return it.label;
  if (it.type === "camera") return camOf(it)?.name || "Camera";
  if (it.type === "link") return maps.find((x) => x.id === it.ref)?.name || "Map";
  if (it.type === "label") return "Text";
  return devOf(it)?.name || "Device";
}
function itemIcon(it, st) {
  if (it.type === "door" || it.type === "device" && devOf(it)?.kind === "door") return MI["door_" + (st.door || "locked")];
  return MI[it.type] || MI.device;
}
function mapStat() {
  if (!maps.length) return "";
  const n = maps.reduce((a, m) => a + (m.items || []).filter((i) => i.type !== "link" && mapStatus(i).s === "alarm").length, 0);
  return n ? `${n} in alarm` : `${maps.length} map(s)`;
}

// -------------------------------------------------------------- list of maps
function mapRenderList() {
  $("#mapList").innerHTML = maps.map((m) => {
    const bad = (m.items || []).some((i) => i.type !== "link" && mapStatus(i).s === "alarm");
    return `<button class="mapli ${mapCur?.id === m.id ? "active" : ""}" data-id="${m.id}">${MAPI(MI.link)}<span>${esc(m.name)}</span>${bad ? '<span class="mdot blink" style="--c:#ff4d4f"></span>' : ""}</button>`;
  }).join("") || `<p class="muted small">No maps yet${me.role === "admin" ? " - click “+ New map” and upload a floor plan." : "."}</p>`;
  $("#mapList").querySelectorAll(".mapli").forEach((b) => (b.onclick = () => { mapHist = []; mapOpen(maps.find((m) => m.id === b.dataset.id)); }));
}
const MAPI = (d) => `<svg viewBox="0 0 24 24" class="mi">${d}</svg>`;

// -------------------------------------------------------------- open / draw
function mapOpen(m, fit = true) {
  const changedMap = mapCur?.id !== m?.id;
  mapCur = m; mapSel = null; mapClosePop(); mapProps();
  try { if (m) localStorage.setItem("ellalon_map", m.id); } catch {}
  $("#mapTitle").textContent = m ? m.name : "No map";
  $("#mapBack").classList.toggle("hidden", !mapHist.length);
  $("#mapEditTools").classList.toggle("hidden", !mapEditing || !m);
  $("#mapEdit").disabled = !m;
  mapRenderList();
  const img = $("#mapImg"), empty = $("#mapEmpty");
  $("#mapStage").classList.toggle("hidden", !m);
  if (!m) { empty.classList.remove("hidden"); empty.innerHTML = me.role === "admin" ? "Create a map with “+ New map”, then upload a floor plan picture." : "No maps yet."; return; }
  empty.classList.add("hidden");
  const done = () => { mapSize(); if (fit || changedMap) mapFit(); mapDrawItems(); };
  if (m.image) {
    if (img.getAttribute("src") !== m.image) { img.onload = done; img.onerror = done; img.src = m.image; img.classList.remove("hidden"); return; }
    img.classList.remove("hidden");
  } else { img.removeAttribute("src"); img.classList.add("hidden"); }
  done();
}
function mapNat() {
  const img = $("#mapImg");
  return mapCur?.image && img.naturalWidth ? [img.naturalWidth, img.naturalHeight] : [MAP_W0, MAP_H0];
}
function mapSize() {
  const [w, h] = mapNat(), st = $("#mapStage");
  st.style.width = w * mapZoom + "px"; st.style.height = h * mapZoom + "px";
  st.classList.toggle("noimg", !mapCur?.image);
  const svg = $("#mapCones"); svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  $("#mapItems").style.setProperty("--z", Math.max(0.55, Math.min(1.6, Math.sqrt(mapZoom))));
}
function mapFit() {
  const v = $("#mapView"), [w, h] = mapNat();
  mapZoom = Math.max(0.05, Math.min((v.clientWidth - 24) / w, (v.clientHeight - 24) / h, 4));
  mapSize(); v.scrollLeft = 0; v.scrollTop = 0;
}
function mapZoomBy(f, cx, cy) {
  const v = $("#mapView"), r = v.getBoundingClientRect();
  cx = cx ?? r.left + v.clientWidth / 2; cy = cy ?? r.top + v.clientHeight / 2;
  const st = $("#mapStage").getBoundingClientRect(), fx = (cx - st.left) / st.width, fy = (cy - st.top) / st.height;
  mapZoom = Math.max(0.05, Math.min(8, mapZoom * f)); mapSize();
  const st2 = $("#mapStage").getBoundingClientRect();
  v.scrollLeft += st2.left + fx * st2.width - cx; v.scrollTop += st2.top + fy * st2.height - cy;
  mapClosePop();
}
function cone(it, w, h, colour) {
  const x = it.x * w, y = it.y * h, r = (it.range ?? 0.12) * w, fov = it.fov ?? 70;
  if (r <= 0) return "";
  if (fov >= 359) return `<circle cx="${x}" cy="${y}" r="${r}" fill="${colour}" fill-opacity=".16" stroke="${colour}" stroke-opacity=".5"/>`;
  const a0 = ((it.angle ?? 0) - fov / 2 - 90) * Math.PI / 180, a1 = ((it.angle ?? 0) + fov / 2 - 90) * Math.PI / 180;
  const p = (a) => `${(x + r * Math.cos(a)).toFixed(1)},${(y + r * Math.sin(a)).toFixed(1)}`;
  return `<path d="M${x},${y} L${p(a0)} A${r},${r} 0 ${fov > 180 ? 1 : 0} 1 ${p(a1)} Z" fill="url(#cg_${it.id})" stroke="${colour}" stroke-opacity=".55" stroke-width="${1.2 / Math.max(mapZoom, .2)}"/>
    <defs><radialGradient id="cg_${it.id}" cx="${x}" cy="${y}" r="${r}" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="${colour}" stop-opacity=".45"/><stop offset="1" stop-color="${colour}" stop-opacity=".05"/></radialGradient></defs>`;
}
function mapDrawItems() {
  if (!mapCur) return;
  const [w, h] = mapNat(), items = mapCur.items || [];
  let cones = "", n = { alarm: 0, offline: 0 };
  $("#mapItems").innerHTML = items.map((it) => {
    const st = mapStatus(it), [colour] = MAP_STATES[st.s] || MAP_STATES.ok;
    if (it.type !== "link" && it.type !== "label" && n[st.s] !== undefined) n[st.s]++;
    if (it.type === "camera") cones += cone(it, w, h, colour);
    const name = itemName(it);
    if (it.type === "label") return `<div class="mitem mlabel ${mapSel === it.id ? "sel" : ""}" data-id="${it.id}" style="left:${it.x * 100}%;top:${it.y * 100}%;--s:${it.size || 1}">${esc(name)}</div>`;
    return `<div class="mitem ${st.s} ${mapSel === it.id ? "sel" : ""} t-${it.type}" data-id="${it.id}" title="${esc(name)}${st.text ? " - " + esc(st.text) : ""}"
        style="left:${it.x * 100}%;top:${it.y * 100}%;--c:${colour};--s:${it.size || 1}">
      <span class="mbadge">${MAPI(itemIcon(it, st))}</span>${it.show_label !== false ? `<span class="mname">${esc(name)}</span>` : ""}</div>`;
  }).join("");
  $("#mapCones").innerHTML = cones;
  const parts = [`${items.filter((i) => !["link", "label"].includes(i.type)).length} objects`];
  if (n.alarm) parts.push(`<b class="err">${n.alarm} in alarm</b>`);
  if (n.offline) parts.push(`${n.offline} offline`);
  $("#mapSummary").innerHTML = parts.join(" · ");
  $("#mapItems").querySelectorAll(".mitem").forEach((e) => (e.onpointerdown = (ev) => mapItemDown(ev, e.dataset.id)));
  if (mapEditing) mapPalette();
}

// -------------------------------------------------------------- clicking, dragging
let mapPopItem = null;
function mapItemDown(ev, id) {
  ev.stopPropagation(); ev.preventDefault();
  const it = mapCur.items.find((i) => i.id === id), el = ev.currentTarget;
  if (!mapEditing) { mapPopup(it); return; }
  mapSel = id; mapProps();
  $("#mapItems").querySelectorAll(".mitem").forEach((e) => e.classList.toggle("sel", e.dataset.id === id));
  const st = $("#mapStage").getBoundingClientRect(), x0 = ev.clientX, y0 = ev.clientY;
  let moved = false;
  el.setPointerCapture(ev.pointerId);
  el.onpointermove = (e) => {
    if (!moved && Math.hypot(e.clientX - x0, e.clientY - y0) < 3) return;
    moved = true;
    it.x = Math.min(1, Math.max(0, (e.clientX - st.left) / st.width));
    it.y = Math.min(1, Math.max(0, (e.clientY - st.top) / st.height));
    el.style.left = it.x * 100 + "%"; el.style.top = it.y * 100 + "%";
    if (it.type === "camera") { const [w, h] = mapNat(); $("#mapCones").innerHTML = mapCur.items.filter((i) => i.type === "camera").map((i) => cone(i, w, h, (MAP_STATES[mapStatus(i).s] || MAP_STATES.ok)[0])).join(""); }
  };
  el.onpointerup = () => { el.onpointermove = el.onpointerup = null; if (moved) mapSave(); };
}
// pan by dragging the background, zoom with the wheel
(() => {
  const v = $("#mapView");
  v.addEventListener("wheel", (e) => { if (!mapCur) return; e.preventDefault(); mapZoomBy(e.deltaY < 0 ? 1.15 : 1 / 1.15, e.clientX, e.clientY); }, { passive: false });
  v.addEventListener("pointerdown", (e) => {
    if (e.target.closest(".mitem") || e.button !== 0) return;
    const x0 = e.clientX, y0 = e.clientY, sl = v.scrollLeft, stp = v.scrollTop;
    let moved = false;
    const mv = (ev) => { if (Math.abs(ev.clientX - x0) + Math.abs(ev.clientY - y0) > 3) moved = true; v.scrollLeft = sl - (ev.clientX - x0); v.scrollTop = stp - (ev.clientY - y0); };
    const up = () => {
      window.removeEventListener("pointermove", mv); window.removeEventListener("pointerup", up); v.classList.remove("panning");
      if (!moved) { mapClosePop(); if (mapEditing && mapSel) { mapSel = null; mapProps(); mapDrawItems(); } }
    };
    v.classList.add("panning"); window.addEventListener("pointermove", mv); window.addEventListener("pointerup", up);
  });
  v.addEventListener("dragover", (e) => { if (mapEditing && mapCur) e.preventDefault(); });
  v.addEventListener("drop", (e) => {
    if (!mapEditing || !mapCur) return;
    e.preventDefault();
    let d; try { d = JSON.parse(e.dataTransfer.getData("text/plain")); } catch { return; }
    if (!d?.type) return;
    const st = $("#mapStage").getBoundingClientRect();
    const x = (e.clientX - st.left) / st.width, y = (e.clientY - st.top) / st.height;
    if (x < 0 || y < 0 || x > 1 || y > 1) return;
    const it = { id: Math.random().toString(36).slice(2, 12), type: d.type, ref: d.ref ?? null, x, y, angle: 0, fov: 70, range: d.type === "camera" ? 0.12 : 0, size: 1, label: d.type === "label" ? "Text" : null, show_label: true };
    mapCur.items = [...(mapCur.items || []), it]; mapSel = it.id;
    mapDrawItems(); mapProps(); mapSave();
  });
  window.addEventListener("resize", () => { if (mapCur && !$("#tab-maps").classList.contains("hidden")) mapSize(); });
  document.addEventListener("keydown", (e) => {
    if ($("#tab-maps").classList.contains("hidden") || /INPUT|SELECT|TEXTAREA/.test(document.activeElement?.tagName)) return;
    if (mapEditing && mapSel && (e.key === "Delete" || e.key === "Backspace")) { mapRemoveSel(); e.preventDefault(); }
    if (e.key === "Escape") mapClosePop();
  });
})();

// -------------------------------------------------------------- pop-up of an object (view mode)
function mapClosePop() { $("#mapPop").classList.add("hidden"); $("#mapPop").innerHTML = ""; mapPopItem = null; }
function mapPopup(it, refresh = false) {
  const pop = $("#mapPop"), st = mapStatus(it), name = itemName(it), [colour] = MAP_STATES[st.s] || MAP_STATES.ok;
  if (it.type === "link") { const m = maps.find((x) => x.id === it.ref); if (m) { mapHist.push(mapCur.id); mapOpen(m); } return; }
  if (it.type === "label") return;
  const keepVideo = refresh && mapPopItem?.id === it.id && pop.querySelector("iframe");
  mapPopItem = it;
  const alarms = alarmsFor(it);
  let body = "", cam = null;
  if (it.type === "camera") cam = camOf(it);
  else {
    const d = devOf(it);
    if (d) {
      const type = mapIntSystems.find((s) => s.id === d.system_id)?.type, acts = mapIntTypes?.[type]?.actions?.[d.kind] || [];
      const id = encodeURIComponent(d.id);
      body += `<div class="popacts">${acts.filter((a) => !a.startsWith("mode_")).map((a) => `<button class="small ${a === "unlock" || a === "disarm" ? "" : "ghost"}" onclick="mapAct('${id}','${a}')">${ACTION_LABEL[a] || a}</button>`).join(" ")}
        ${acts.some((a) => a.startsWith("mode_")) ? `<select class="modesel" onchange="if(this.value){mapAct('${id}',this.value);this.value=''}"><option value="">Door mode…</option>${acts.filter((a) => a.startsWith("mode_")).map((a) => `<option value="${a}">${ACTION_LABEL[a].replace("Mode: ", "")}</option>`).join("")}</select>` : ""}</div>`;
      cam = cameras.find((c) => d.cameras?.includes(c.id));
    }
  }
  const video = cam ? `<div class="popvid">${keepVideo ? "" : `<iframe src="${tileSrc(cam, 4, false)}" allow="autoplay; fullscreen"></iframe>`}</div>
      <div class="popacts"><button class="ghost small" onclick="addToFreeTile(${cam.id});show('wall')">Show on video wall</button>
      <button class="ghost small" onclick="show('playback');setTimeout(()=>{$('#pbCam').value='${cam.id}';$('#pbCam').dispatchEvent(new Event('change'))},50)">Playback</button></div>` : "";
  const html = `<div class="pophead"><span class="mdot ${st.s === "alarm" ? "blink" : ""}" style="--c:${colour}"></span><b>${esc(name)}</b><button class="tclose" onclick="mapClosePop()">×</button></div>
    <div class="small muted">${esc(st.text || "")}${cam && it.type !== "camera" ? ` · camera: ${esc(cam.name)}` : ""}</div>
    ${alarms.length ? `<div class="popalarm">${alarms.slice(0, 3).map((a) => `<div>⚠ ${esc(a.title)}</div>`).join("")}<button class="small danger" onclick="show('incidents')">Open Alarm manager</button></div>` : ""}
    ${body}`;
  if (keepVideo) { pop.querySelector(".popinfo").innerHTML = html; return; }
  pop.innerHTML = `<div class="popinfo">${html}</div>${video}`;
  pop.classList.remove("hidden");
  // place next to the icon, inside the view
  const v = $(".mapwrap").getBoundingClientRect(), el = $(`#mapItems .mitem[data-id="${it.id}"]`)?.getBoundingClientRect();
  if (el) {
    const pw = pop.offsetWidth, ph = pop.offsetHeight;
    let x = el.right + 10 - v.left, y = el.top - v.top - 10;
    if (x + pw > v.width - 8) x = el.left - v.left - pw - 10;
    pop.style.left = Math.max(8, x) + "px"; pop.style.top = Math.max(8, Math.min(y, v.height - ph - 8)) + "px";
  }
}
window.mapAct = async (id, action) => {
  if (["disarm", "unlock", "bypass"].includes(action) && !(await ask(`${ACTION_LABEL[action]} - are you sure?`, { ok: ACTION_LABEL[action] }))) return;
  try { await mod("integrations", `/devices/${id}/action`, { method: "POST", body: { action } }); setTimeout(() => mapRefreshStatus(), 800); }
  catch (err) { notify(errText(err)); }
};
window.mapClosePop = mapClosePop;

// -------------------------------------------------------------- edit mode
function mapPalette() {
  const q = ($("#mapSearch").value || "").toLowerCase(), on = new Set((mapCur?.items || []).map((i) => i.type + ":" + i.ref));
  const kindType = (k) => (k === "door" ? "door" : k === "output" ? "output" : ["input", "zone"].includes(k) ? "input" : "device");
  const sysName = (id) => mapIntSystems.find((s) => s.id === id)?.name || "";
  const groups = [
    ["Cameras", cameras.map((c) => ({ type: "camera", ref: String(c.id), name: c.name, sub: c.location || "" }))],
    ["Doors", mapDevs.filter((d) => d.kind === "door").map((d) => ({ type: "door", ref: d.id, name: d.name, sub: sysName(d.system_id) }))],
    ["Inputs / zones", mapDevs.filter((d) => ["input", "zone"].includes(d.kind)).map((d) => ({ type: "input", ref: d.id, name: d.name, sub: sysName(d.system_id) }))],
    ["Outputs / relays", mapDevs.filter((d) => d.kind === "output").map((d) => ({ type: "output", ref: d.id, name: d.name, sub: sysName(d.system_id) }))],
    ["Other devices", mapDevs.filter((d) => !["door", "input", "zone", "output"].includes(d.kind)).map((d) => ({ type: kindType(d.kind), ref: d.id, name: d.name, sub: `${d.kind} · ${sysName(d.system_id)}` }))],
    ["Links to other maps", maps.filter((m) => m.id !== mapCur?.id).map((m) => ({ type: "link", ref: m.id, name: m.name, sub: "" }))],
    ["Text", [{ type: "label", ref: null, name: "Text label", sub: "" }]],
  ];
  $("#mapPalList").innerHTML = groups.map(([title, list]) => {
    list = list.filter((x) => !q || (x.name + " " + x.sub).toLowerCase().includes(q));
    if (!list.length) return "";
    return `<div class="palgroup"><div class="small muted">${title}</div>${list.map((x) => `<div class="palitem ${x.type !== "label" && on.has(x.type + ":" + x.ref) ? "placed" : ""}" draggable="true" data-d='${esc(JSON.stringify({ type: x.type, ref: x.ref }))}'>
      ${MAPI(x.type === "door" ? MI.door_locked : MI[x.type] || MI.device)}<span>${esc(x.name)}<small>${esc(x.sub)}</small></span>${x.type !== "label" && on.has(x.type + ":" + x.ref) ? '<span class="small muted">on map</span>' : ""}</div>`).join("")}</div>`;
  }).join("") || `<p class="muted small">Nothing found.</p>`;
  $("#mapPalList").querySelectorAll(".palitem").forEach((p) => (p.ondragstart = (e) => { e.dataTransfer.setData("text/plain", p.dataset.d); e.dataTransfer.effectAllowed = "copy"; }));
}
$("#mapSearch").oninput = mapPalette;
function mapProps() {
  const box = $("#mapProps"), it = mapEditing && mapCur?.items.find((i) => i.id === mapSel);
  if (!it) { box.classList.add("hidden"); return; }
  const cam = it.type === "camera";
  box.innerHTML = `<div class="pophead"><b>${esc(itemName(it))}</b><button class="tclose" onclick="mapSel=null;mapProps();mapDrawItems()">×</button></div>
    <label>${it.type === "label" ? "Text" : "Name on the map (empty = its own name)"}<input data-k="label" value="${esc(it.label || "")}" maxlength="120"></label>
    ${cam ? `<label>Direction <span class="muted" id="mpA">${Math.round(it.angle)}°</span><input type="range" data-k="angle" min="0" max="359" value="${(it.angle + 360) % 360}"></label>
    <label>View angle <span class="muted" id="mpF">${Math.round(it.fov)}°</span><input type="range" data-k="fov" min="10" max="360" value="${it.fov}"></label>
    <label>View distance<input type="range" data-k="range" min="0" max="0.6" step="0.005" value="${it.range}"></label>` : ""}
    <label>Icon size<input type="range" data-k="size" min="0.4" max="3" step="0.1" value="${it.size || 1}"></label>
    ${it.type !== "label" ? `<label class="inline"><input type="checkbox" data-k="show_label" ${it.show_label !== false ? "checked" : ""}> Show the name</label>` : ""}
    <button class="danger small" onclick="mapRemoveSel()">Remove from map</button>`;
  box.classList.remove("hidden");
  box.querySelectorAll("[data-k]").forEach((inp) => (inp.oninput = () => {
    const k = inp.dataset.k;
    it[k] = inp.type === "checkbox" ? inp.checked : inp.type === "range" ? +inp.value : inp.value.trim() || null;
    if (k === "angle") $("#mpA").textContent = Math.round(it.angle) + "°";
    if (k === "fov") $("#mpF").textContent = Math.round(it.fov) + "°";
    mapDrawItems(); mapSave();
  }));
}
window.mapProps = mapProps;
window.mapRemoveSel = () => {
  if (!mapCur || !mapSel) return;
  mapCur.items = mapCur.items.filter((i) => i.id !== mapSel); mapSel = null;
  mapProps(); mapDrawItems(); mapSave();
};
function mapSave() {
  clearTimeout(mapSaveT);
  const m = mapCur;
  mapSaveT = setTimeout(async () => {
    try {
      const r = await api(`/api/maps/${m.id}`, { method: "PUT", body: { name: m.name, items: m.items } });
      const i = maps.findIndex((x) => x.id === r.id); if (i >= 0) maps[i] = { ...maps[i], ...r, items: m.items };
      $("#mapSummary").insertAdjacentHTML("beforeend", ' · <span class="ok">saved</span>');
    } catch (err) { notify("The map could not be saved: " + errText(err)); }
  }, 500);
}
$("#mapEdit").onclick = () => {
  mapEditing = !mapEditing;
  $("#mapEdit").textContent = mapEditing ? "✓ Done" : "✎ Edit map";
  $("#mapEditTools").classList.toggle("hidden", !mapEditing);
  $("#mapPalette").classList.toggle("hidden", !mapEditing);
  $("#mapView").classList.toggle("editing", mapEditing);
  mapSel = null; mapClosePop(); mapProps(); mapDrawItems();
  if (mapEditing) mapPalette();
};
$("#mapNew").onclick = async () => {
  const name = ((await askText("Name of the new map (e.g. Building A - floor 1):", "", { ok: "Create" })) || "").trim();
  if (!name) return;
  try {
    const m = await api("/api/maps", { method: "POST", body: { name } });
    maps.push(m); mapHist = []; mapOpen(m);
    if (!mapEditing) $("#mapEdit").onclick();
    notify("Map created. Now click “Upload floor plan…” and pick a picture of the plan (PNG or JPG), then drag cameras and devices onto it.");
  } catch (err) { notify(errText(err)); }
};
$("#mapRename").onclick = async () => {
  const name = ((await askText("New name:", mapCur.name, { ok: "Rename" })) || "").trim();
  if (!name) return;
  mapCur.name = name; $("#mapTitle").textContent = name; mapRenderList(); mapSave();
};
$("#mapDelete").onclick = async () => {
  if (!(await ask(`Delete the map “${mapCur.name}”? The cameras and devices themselves are not affected.`, { ok: "Delete", danger: true }))) return;
  try { await api(`/api/maps/${mapCur.id}`, { method: "DELETE" }); } catch (err) { return notify(errText(err)); }
  maps = maps.filter((m) => m.id !== mapCur.id); mapCur = null; mapHist = []; mapOpen(maps[0] || null);
};
$("#mapUpload").onclick = () => $("#mapFile").click();
$("#mapFile").onchange = async (e) => {
  const f = e.target.files[0]; e.target.value = "";
  if (!f || !mapCur) return;
  const fd = new FormData(); fd.append("file", f);
  try {
    const r = await fetch(`/api/maps/${mapCur.id}/image`, { method: "PUT", body: fd, headers: { Authorization: "Bearer " + token } });
    if (!r.ok) { let d = `HTTP ${r.status}`; try { d = (await r.json()).detail || d; } catch {} throw new Error(d); }
    const m = await r.json(), i = maps.findIndex((x) => x.id === m.id);
    maps[i] = { ...maps[i], image: m.image }; mapCur = maps[i]; mapOpen(mapCur, true);
  } catch (err) { notify("Upload failed: " + err.message); }
};
$("#mapBack").onclick = () => { const id = mapHist.pop(); mapOpen(maps.find((m) => m.id === id) || maps[0]); };
$("#mapZoomIn").onclick = () => mapZoomBy(1.25);
$("#mapZoomOut").onclick = () => mapZoomBy(0.8);
$("#mapFit").onclick = () => { mapFit(); mapClosePop(); };
