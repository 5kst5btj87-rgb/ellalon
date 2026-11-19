// ---------------------------------------------------------------- maps
// Floor plans with cameras, doors, inputs, outputs and links to other maps. Icons follow the live
// status of what they stand for (camera online/offline, door locked/unlocked/open/forced, input
// alarm/armed/disarmed, output on/off, open alarms). Positions are stored as fractions of the plan.
let maps = [], mapCur = null, mapEditing = false, mapSel = null, mapZoom = 1, mapHist = [];
const mapSelSet = new Set();             // selected objects in edit mode (mapSel = the one clicked last)
const selItems = () => (mapCur?.items || []).filter((i) => mapSelSet.has(i.id));
function mapSelect(ids, add = false) {
  if (!add) mapSelSet.clear();
  ids.forEach((id) => mapSelSet.add(id));
  mapSel = ids.length ? ids[ids.length - 1] : mapSelSet.size ? [...mapSelSet].pop() : null;
  $("#mapItems").querySelectorAll(".mitem").forEach((e) => e.classList.toggle("sel", mapSelSet.has(e.dataset.id)));
  mapProps();
}
function mapUnselect() { mapSelSet.clear(); mapSel = null; }
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
  api("/api/maps/icons").then((l) => { mapIcons = l; if (mapCur) mapDrawItems(); }).catch(() => {});
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
let mapIcons = [];                       // imported icons: [{id, name, category, url}]
function iconHtml(it, st) {               // -> [inner html, is a picture]
  if (it.icon?.startsWith("c:")) { const ic = mapIcons.find((x) => x.id === it.icon.slice(2)); if (ic) return [`<img src="${ic.url}" alt="" draggable="false">`, true]; }
  if (it.icon?.startsWith("b:") && ICON_BY_ID[it.icon.slice(2)]) return [MAPI(ICON_BY_ID[it.icon.slice(2)].svg), false];
  return [MAPI(itemIcon(it, st)), false];
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
  mapCur = m; mapUnselect(); mapClosePop(); mapProps();
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
    if (it.type === "label") return `<div class="mitem mlabel ${mapSelSet.has(it.id) ? "sel" : ""}" data-id="${it.id}" style="left:${it.x * 100}%;top:${it.y * 100}%;--s:${it.size || 1}">${esc(name)}</div>`;
    return `<div class="mitem ${st.s} ${mapSelSet.has(it.id) ? "sel" : ""} t-${it.type}" data-id="${it.id}" title="${esc(name)}${st.text ? " - " + esc(st.text) : ""}"
        style="left:${it.x * 100}%;top:${it.y * 100}%;--c:${colour};--s:${it.size || 1}">
      ${(() => { const [h, pic] = iconHtml(it, st); return `<span class="mbadge ${pic ? "pic" : ""}">${h}</span>`; })()}${it.show_label !== false ? `<span class="mname">${esc(name)}</span>` : ""}</div>`;
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
  if (ev.ctrlKey || ev.metaKey || ev.shiftKey) {          // add to / remove from the selection
    if (mapSelSet.has(id)) { mapSelSet.delete(id); mapSelect([], true); } else mapSelect([id], true);
    return;
  }
  if (!mapSelSet.has(id)) mapSelect([id]); else { mapSel = id; mapProps(); }
  const st = $("#mapStage").getBoundingClientRect(), x0 = ev.clientX, y0 = ev.clientY;
  const group = selItems().map((i) => ({ i, x: i.x, y: i.y, el: $(`#mapItems .mitem[data-id="${i.id}"]`) }));
  let moved = false;
  try { el.setPointerCapture(ev.pointerId); } catch {}
  el.onpointermove = (e) => {
    if (!moved && Math.hypot(e.clientX - x0, e.clientY - y0) < 3) return;
    moved = true;
    const dx = (e.clientX - x0) / st.width, dy = (e.clientY - y0) / st.height;
    for (const g of group) {                               // the whole selection moves together
      g.i.x = Math.min(1, Math.max(0, g.x + dx)); g.i.y = Math.min(1, Math.max(0, g.y + dy));
      if (g.el) { g.el.style.left = g.i.x * 100 + "%"; g.el.style.top = g.i.y * 100 + "%"; }
    }
    if (group.some((g) => g.i.type === "camera")) mapDrawCones();
  };
  el.onpointerup = () => { el.onpointermove = el.onpointerup = null; if (moved) { mapSave(); mapProps(); } };
}
function mapDrawCones() {
  const [w, h] = mapNat();
  $("#mapCones").innerHTML = mapCur.items.filter((i) => i.type === "camera").map((i) => cone(i, w, h, (MAP_STATES[mapStatus(i).s] || MAP_STATES.ok)[0])).join("");
}
// pan by dragging the background, zoom with the wheel
(() => {
  const v = $("#mapView");
  v.addEventListener("wheel", (e) => { if (!mapCur) return; e.preventDefault(); mapZoomBy(e.deltaY < 0 ? 1.15 : 1 / 1.15, e.clientX, e.clientY); }, { passive: false });
  v.addEventListener("pointerdown", (e) => {
    if (e.target.closest(".mitem") || e.button !== 0) return;
    if (mapEditing && (e.shiftKey || mapBoxMode)) { mapBoxSelect(e); return; }
    const x0 = e.clientX, y0 = e.clientY, sl = v.scrollLeft, stp = v.scrollTop;
    let moved = false;
    const mv = (ev) => { if (Math.abs(ev.clientX - x0) + Math.abs(ev.clientY - y0) > 3) moved = true; v.scrollLeft = sl - (ev.clientX - x0); v.scrollTop = stp - (ev.clientY - y0); };
    const up = () => {
      window.removeEventListener("pointermove", mv); window.removeEventListener("pointerup", up); v.classList.remove("panning");
      if (!moved) { mapClosePop(); if (mapEditing && mapSelSet.size) { mapUnselect(); mapProps(); mapDrawItems(); } }
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
    mapCur.items = [...(mapCur.items || []), it]; mapSelSet.clear(); mapSelSet.add(it.id); mapSel = it.id;
    mapDrawItems(); mapProps(); mapSave();
  });
  window.addEventListener("resize", () => { if (mapCur && !$("#tab-maps").classList.contains("hidden")) mapSize(); });
  document.addEventListener("keydown", (e) => {
    if ($("#tab-maps").classList.contains("hidden") || /INPUT|SELECT|TEXTAREA/.test(document.activeElement?.tagName)) return;
    if (mapEditing && mapSelSet.size && (e.key === "Delete" || e.key === "Backspace")) { mapRemoveSel(); e.preventDefault(); }
    if (mapEditing && mapCur && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a") { mapSelect(mapCur.items.map((i) => i.id)); e.preventDefault(); }
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
let mapBoxMode = false;
function mapBoxSelect(e) {                 // drag a box: everything inside it is selected
  const v = $("#mapView"), wrap = $(".mapwrap").getBoundingClientRect(), box = document.createElement("div");
  box.className = "selbox"; $(".mapwrap").appendChild(box);
  const x0 = e.clientX, y0 = e.clientY, add = e.ctrlKey || e.metaKey || e.shiftKey && mapBoxMode;
  const mv = (ev) => {
    Object.assign(box.style, { left: Math.min(x0, ev.clientX) - wrap.left + "px", top: Math.min(y0, ev.clientY) - wrap.top + "px",
      width: Math.abs(ev.clientX - x0) + "px", height: Math.abs(ev.clientY - y0) + "px" });
  };
  const up = (ev) => {
    window.removeEventListener("pointermove", mv); window.removeEventListener("pointerup", up); box.remove();
    const l = Math.min(x0, ev.clientX), r = Math.max(x0, ev.clientX), t = Math.min(y0, ev.clientY), b = Math.max(y0, ev.clientY);
    if (r - l < 4 && b - t < 4) { mapUnselect(); mapProps(); mapDrawItems(); return; }
    const ids = [...$("#mapItems").querySelectorAll(".mitem")].filter((el) => {
      const q = el.getBoundingClientRect(), cx = (q.left + q.right) / 2, cy = (q.top + q.bottom) / 2;
      return cx >= l && cx <= r && cy >= t && cy <= b;
    }).map((el) => el.dataset.id);
    mapSelect(ids, add);
  };
  window.addEventListener("pointermove", mv); window.addEventListener("pointerup", up);
}
const TYPE_ONE = { camera: "camera", door: "door", input: "input", output: "output", device: "device", link: "map link", label: "text" };
// a slider with its number next to it; unit shown after the number; `scale` turns stored value into shown value
function prow(label, k, min, max, step, val, unit = "", scale = 1, mixed = false) {
  const shown = mixed ? "" : +(val * scale).toFixed(step < 1 ? (String(step).split(".")[1] || "").length : 0);
  return `<div class="prow"><span class="pl">${label}</span>
    <input type="range" data-k="${k}" data-scale="${scale}" min="${min}" max="${max}" step="${step}" value="${mixed ? (min + max) / 2 : val}" class="${mixed ? "mixed" : ""}">
    <span class="pn"><input type="number" data-n="${k}" data-scale="${scale}" min="${+(min * scale).toFixed(3)}" max="${+(max * scale).toFixed(3)}" step="${+(step * scale).toFixed(3)}" value="${shown}" placeholder="${mixed ? "mixed" : ""}">${unit}</span></div>`;
}
function mapProps() {
  const box = $("#mapProps"), sel = mapEditing ? selItems() : [];
  $(".mapwrap").classList.toggle("withprops", !!sel.length);
  if (!sel.length) { box.classList.add("hidden"); return; }
  const one = sel.length === 1 ? sel[0] : null, cams = sel.filter((i) => i.type === "camera"), icons = sel.filter((i) => i.type !== "label");
  const same = (k, list = sel, d = undefined) => { const v = list.map((i) => i[k] ?? d); return v.every((x) => x === v[0]) ? [v[0], false] : [v[0], true]; };
  const [size, sizeMix] = same("size", sel, 1), [ang, angMix] = same("angle", cams, 0), [fov, fovMix] = same("fov", cams, 70), [rng, rngMix] = same("range", cams, 0.12);
  const kinds = Object.entries(sel.reduce((a, i) => ((a[i.type] = (a[i.type] || 0) + 1), a), {})).map(([t, n]) => `${n} ${TYPE_ONE[t]}${n > 1 ? "s" : ""}`).join(", ");
  const shownCount = icons.filter((i) => i.show_label !== false).length;
  const st = one ? mapStatus(one) : null;
  box.innerHTML = `<div class="pophead"><b>${one ? esc(itemName(one)) : `${sel.length} objects selected`}</b><button class="tclose" title="Unselect" onclick="mapUnselect();mapProps();mapDrawItems()">×</button></div>
    ${one ? `<div class="small muted">${esc(TYPE_ONE[one.type])}${st?.text ? " · " + esc(st.text) : ""}</div>` : `<div class="small muted">${esc(kinds)}</div>`}
    ${one ? `<label class="pfield"><span class="pl">${one.type === "label" ? "Text" : "Name on the map"}</span><input data-k="label" value="${esc(one.label || "")}" maxlength="120" placeholder="${one.type === "label" ? "" : esc(itemName({ ...one, label: null }))}"></label>` : ""}
    ${icons.length ? `<div class="psec">Icon</div>
      <div class="iconrow">${one && one.type !== "label" ? `<span class="mbadge ${iconHtml(one, st)[1] ? "pic" : ""}" style="--c:${(MAP_STATES[st.s] || MAP_STATES.ok)[0]}">${iconHtml(one, st)[0]}</span>` : ""}
        <span class="grow small">${one ? esc(one.icon ? (one.icon.startsWith("b:") ? ICON_BY_ID[one.icon.slice(2)]?.label : mapIcons.find((x) => "c:" + x.id === one.icon)?.name) || "Icon" : "Default icon") : `for ${icons.length} object(s)`}</span>
        <button type="button" class="small" onclick="iconPick()">Choose icon…</button></div>` : ""}
    ${prow("Size", "size", 0.4, 3, 0.1, size, "×", 1, sizeMix)}
    ${icons.length ? `<label class="inline small pchk"><input type="checkbox" data-k="show_label" ${shownCount === icons.length ? "checked" : ""}> Show the name${shownCount && shownCount < icons.length ? ` <span class="muted">(${shownCount} of ${icons.length} now)</span>` : ""}</label>` : ""}
    ${cams.length ? `<div class="psec">Camera view${!one && cams.length < sel.length ? ` <span class="muted">(${cams.length} camera${cams.length > 1 ? "s" : ""})</span>` : ""}</div>
      ${prow("Direction", "angle", 0, 359, 1, (ang + 360) % 360, "°", 1, angMix)}
      ${prow("View angle", "fov", 10, 360, 1, fov, "°", 1, fovMix)}
      ${prow("Distance", "range", 0, 0.6, 0.005, rng, "%", 100, rngMix)}` : ""}
    ${!one ? `<div class="psec">Arrange</div>
      <div class="pbtns2"><button type="button" class="ghost small" onclick="mapArrange('row')" title="Same height">⇿ In a row</button>
        <button type="button" class="ghost small" onclick="mapArrange('col')" title="Same left-right position">⇕ In a column</button>
        <button type="button" class="ghost small" onclick="mapArrange('spread')" title="Equal gaps, in the direction they are spread">↔ Space evenly</button></div>` : ""}
    <div class="psec">Select</div>
    <div class="pbtns2">${[...new Set((mapCur.items || []).map((i) => i.type))].map((t) => `<button type="button" class="ghost small" onclick="mapSelectType('${t}', event)">All ${TYPE_ONE[t]}s</button>`).join("")}
      <button type="button" class="ghost small" onclick="mapSelect(mapCur.items.map((i) => i.id))">Everything</button></div>
    <p class="muted small">Ctrl / Shift + click adds to the selection; Shift + drag on the plan (or ⬚ Select) draws a selection box; Delete removes.</p>
    <button class="danger small" onclick="mapRemoveSel()">Remove ${one ? "from map" : `${sel.length} objects from map`}</button>`;
  box.classList.remove("hidden");
  const apply = (k, v) => {
    const list = ["angle", "fov", "range"].includes(k) ? cams : k === "show_label" ? icons : sel;
    list.forEach((i) => { i[k] = v; });
    mapDrawItems(); mapSave();
  };
  box.querySelectorAll("input[data-k]").forEach((inp) => (inp.oninput = () => {
    const k = inp.dataset.k;
    if (inp.type === "checkbox") return apply(k, inp.checked);
    if (inp.type === "range") {
      inp.classList.remove("mixed");
      const n = box.querySelector(`input[data-n="${k}"]`), sc = +inp.dataset.scale;
      n.value = +(+inp.value * sc).toFixed(sc === 100 ? 1 : k === "size" ? 1 : 0); n.placeholder = "";
      return apply(k, +inp.value);
    }
    one[k] = inp.value.trim() || null; mapDrawItems(); mapSave();
  }));
  box.querySelectorAll("input[data-n]").forEach((inp) => (inp.onchange = () => {
    const k = inp.dataset.n, sc = +inp.dataset.scale, r = box.querySelector(`input[data-k="${k}"]`);
    if (inp.value === "") return;
    let v = +inp.value / sc;
    v = Math.min(+r.max, Math.max(+r.min, v));
    r.value = v; r.classList.remove("mixed"); inp.value = +(v * sc).toFixed(sc === 100 ? 1 : k === "size" ? 1 : 0);
    apply(k, v);
  }));
}
window.mapProps = mapProps;
window.mapSelect = mapSelect;
window.mapUnselect = mapUnselect;
window.mapSelectType = (t, ev) => mapSelect(mapCur.items.filter((i) => i.type === t).map((i) => i.id), ev?.ctrlKey || ev?.shiftKey);
window.mapArrange = (how) => {
  const sel = selItems(); if (sel.length < 2) return;
  if (how === "row") { const y = sel.reduce((a, i) => a + i.y, 0) / sel.length; sel.forEach((i) => (i.y = y)); }
  if (how === "col") { const x = sel.reduce((a, i) => a + i.x, 0) / sel.length; sel.forEach((i) => (i.x = x)); }
  if (how === "spread") {
    const xs = sel.map((i) => i.x), ys = sel.map((i) => i.y), k = Math.max(...xs) - Math.min(...xs) >= Math.max(...ys) - Math.min(...ys) ? "x" : "y";
    const sorted = [...sel].sort((a, b) => a[k] - b[k]), lo = sorted[0][k], hi = sorted[sorted.length - 1][k];
    sorted.forEach((i, n) => (i[k] = lo + (hi - lo) * n / (sorted.length - 1)));
  }
  mapDrawItems(); mapSave(); mapProps();
};
window.mapRemoveSel = async () => {
  const sel = selItems(); if (!mapCur || !sel.length) return;
  if (sel.length > 1 && !(await ask(`Remove ${sel.length} objects from this map? (The cameras and devices themselves are not affected.)`, { ok: "Remove", danger: true }))) return;
  mapCur.items = mapCur.items.filter((i) => !mapSelSet.has(i.id)); mapUnselect();
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
  mapUnselect(); mapClosePop(); mapProps(); mapDrawItems();
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

// ---- 3D view: tilt the plan, icons stand up; turn it with the arrows (view only - editing is flat)
let map3d = (() => { try { return localStorage.getItem("ellalon_map3d") === "1"; } catch { return false; } })(), mapRz = 0;
function map3dUi() {
  const on = map3d && !mapEditing;
  $("#mapView").classList.toggle("tilt", on); $("#map3d").classList.toggle("on", on);
  $("#mapTurnL").classList.toggle("hidden", !on); $("#mapTurnR").classList.toggle("hidden", !on);
  $("#mapStage").style.setProperty("--rz", mapRz + "deg"); $("#mapItems").style.setProperty("--rz", mapRz + "deg");
  mapClosePop();
}
$("#map3d").onclick = () => { map3d = !map3d; try { localStorage.setItem("ellalon_map3d", map3d ? "1" : "0"); } catch {} if (map3d && mapEditing) $("#mapEdit").onclick(); map3dUi(); };
$("#mapTurnL").onclick = () => { mapRz -= 30; map3dUi(); };
$("#mapTurnR").onclick = () => { mapRz += 30; map3dUi(); };
$("#mapEdit").addEventListener("click", () => setTimeout(map3dUi, 0));
map3dUi();


// ---- icon picker and import
let ipCat = null;
const TYPE_NAMES = { camera: "cameras", door: "doors", input: "inputs", output: "outputs", device: "devices", link: "map links" };
window.iconPick = () => {
  const sel = selItems().filter((i) => i.type !== "label"), it = sel.find((i) => i.id === mapSel) || sel[0]; if (!it) return;
  const types = [...new Set(sel.map((i) => i.type))];
  $("#ipAll").parentElement.querySelector("span").textContent = types.length === 1 ? `Use this icon for all ${TYPE_NAMES[it.type] || "objects of this kind"} on this map` : `Use this icon for all ${types.map((t) => TYPE_NAMES[t] || t).join(" and ")} on this map`;
  $("#ipAll").checked = false; $("#ipSearch").value = ""; $("#ipMsg").textContent = "";
  $("#ipImport").classList.toggle("hidden", me.role !== "admin");
  ipCat = ipCat || Object.keys(ICON_LIB)[0];
  ipRender(); $("#iconDialog").showModal();
};
function ipCats() {
  const custom = [...new Set(mapIcons.map((x) => x.category))];
  return [...Object.keys(ICON_LIB).map((c) => ({ c, n: ICON_LIB[c].length, custom: false })), ...custom.map((c) => ({ c, n: mapIcons.filter((x) => x.category === c).length, custom: true }))];
}
function ipRender() {
  const q = $("#ipSearch").value.trim().toLowerCase(), it = mapCur?.items.find((i) => i.id === mapSel) || selItems()[0];
  const cats = ipCats();
  if (!cats.some((x) => x.c === ipCat)) ipCat = cats[0].c;
  $("#ipCats").innerHTML = cats.map((x) => `<button type="button" class="mapli ${!q && x.c === ipCat ? "active" : ""}" data-c="${esc(x.c)}"><span>${x.custom ? "⬆ " : ""}${esc(x.c)}</span><small class="muted">${x.n}</small></button>`).join("");
  $("#ipCats").querySelectorAll("button").forEach((b) => (b.onclick = () => { ipCat = b.dataset.c; $("#ipSearch").value = ""; ipRender(); }));
  let list = [];
  const builtin = (cat) => ICON_LIB[cat].map(([id, label, svg]) => ({ key: "b:" + id, label, html: MAPI(svg), pic: false }));
  const custom = (cat) => mapIcons.filter((x) => x.category === cat).map((x) => ({ key: "c:" + x.id, label: x.name, html: `<img src="${x.url}" alt="" loading="lazy">`, pic: true, id: x.id }));
  if (q) list = [...Object.keys(ICON_LIB).flatMap(builtin), ...[...new Set(mapIcons.map((x) => x.category))].flatMap(custom)].filter((x) => x.label.toLowerCase().includes(q));
  else list = ICON_LIB[ipCat] ? builtin(ipCat) : custom(ipCat);
  const cur = selItems().every((i) => (i.icon || "") === (it?.icon || "")) ? it?.icon || "" : "-";
  $("#ipGrid").innerHTML = `<button type="button" class="ipitem ${!cur ? "on" : ""}" data-k=""><span class="mbadge">${MAPI(it ? itemIcon(it, mapStatus(it)) : MI.device)}</span><span>Default</span></button>`
    + list.map((x) => `<button type="button" class="ipitem ${cur === x.key ? "on" : ""}" data-k="${esc(x.key)}" title="${esc(x.label)}"><span class="mbadge ${x.pic ? "pic" : ""}">${x.html}</span><span>${esc(x.label)}</span>
      ${x.id && me.role === "admin" ? `<i class="ipdel" data-id="${x.id}" title="Delete this imported icon">×</i>` : ""}</button>`).join("")
    + (!list.length ? `<p class="muted small">No icons ${q ? "match" : "in this category"}.</p>` : "");
  $("#ipGrid").querySelectorAll(".ipitem").forEach((b) => (b.onclick = (e) => {
    if (e.target.classList.contains("ipdel")) { ipDelete(e.target.dataset.id); return; }
    ipApply(b.dataset.k || null);
  }));
  if (ICON_LIB[ipCat] || q) $("#ipDelCat").classList.add("hidden");
  else $("#ipDelCat").classList.toggle("hidden", me.role !== "admin");
}
function ipApply(icon) {
  const sel = selItems().filter((i) => i.type !== "label"); if (!sel.length) return;
  const types = new Set(sel.map((i) => i.type));
  const targets = $("#ipAll").checked ? mapCur.items.filter((i) => types.has(i.type)) : sel;
  targets.forEach((t) => { t.icon = icon; });
  $("#iconDialog").close(); mapDrawItems(); mapProps(); mapSave();
}
async function ipDelete(id) {
  const used = maps.reduce((a, m) => a + (m.items || []).filter((i) => i.icon === "c:" + id).length, 0);
  if (!(await ask(`Delete this imported icon?${used ? ` It is used ${used} time(s) on maps - those objects go back to the default icon.` : ""}`, { ok: "Delete", danger: true }))) return;
  await api(`/api/maps/icons/${id}`, { method: "DELETE" }).catch((err) => notify(errText(err)));
  mapIcons = mapIcons.filter((x) => x.id !== id); ipRender(); mapDrawItems();
}
$("#ipSearch").oninput = () => ipRender();
$("#ipDelCat").onclick = async () => {
  if (!(await ask(`Delete all imported icons in “${ipCat}”?`, { ok: "Delete all", danger: true }))) return;
  await api(`/api/maps/icon-categories/${encodeURIComponent(ipCat)}`, { method: "DELETE" }).catch((err) => notify(errText(err)));
  mapIcons = mapIcons.filter((x) => x.category !== ipCat); ipCat = null; ipRender(); mapDrawItems();
};
$("#ipImport").onclick = () => {
  $("#ipImportBox").classList.toggle("hidden");
  $("#ipCatName").value = ICON_LIB[ipCat] ? "My icons" : ipCat;
  $("#ipCatList").innerHTML = [...new Set(mapIcons.map((x) => x.category))].map((c) => `<option value="${esc(c)}">`).join("");
};
$("#ipGo").onclick = async () => {
  const files = [...$("#ipFiles").files], msg = $("#ipMsg");
  if (!files.length) { msg.className = "small err"; msg.textContent = "Choose icon files (SVG, PNG, JPG) or a ZIP first."; return; }
  const fd = new FormData(); files.forEach((f) => fd.append("files", f)); fd.append("category", $("#ipCatName").value.trim() || "My icons");
  msg.className = "small muted"; msg.textContent = "Importing…";
  try {
    const r = await fetch("/api/maps/icons", { method: "POST", body: fd, headers: { Authorization: "Bearer " + token } });
    if (!r.ok) { let d = `HTTP ${r.status}`; try { d = (await r.json()).detail || d; } catch {} throw new Error(d); }
    const res = await r.json();
    mapIcons = await api("/api/maps/icons");
    msg.className = "small ok"; msg.textContent = `Imported ${res.imported} icon(s).${res.skipped.length ? " Skipped: " + res.skipped.join(", ") : ""}`;
    ipCat = $("#ipCatName").value.trim() || "My icons";
    if (!mapIcons.some((x) => x.category === ipCat)) ipCat = mapIcons[mapIcons.length - 1]?.category || ipCat;   // folders in a ZIP
    $("#ipFiles").value = ""; $("#ipImportBox").classList.add("hidden"); ipRender();
  } catch (err) { msg.className = "small err"; msg.textContent = err.message; }
};

$("#mapBoxBtn").onclick = () => { mapBoxMode = !mapBoxMode; $("#mapBoxBtn").classList.toggle("on", mapBoxMode); $("#mapView").classList.toggle("boxmode", mapBoxMode); };
$("#mapEdit").addEventListener("click", () => { if (!mapEditing && mapBoxMode) $("#mapBoxBtn").onclick(); });
