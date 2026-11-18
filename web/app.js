// Ellalon web client - plain JS, no build step. Uses only the public core API + module routes.
const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
let token = localStorage.getItem("ellalon_token");
let me = null, cameras = [], ws = null;
let wallTiles = JSON.parse(localStorage.getItem("ellalon_wall") || "[]");

async function api(path, opts = {}) {
  const r = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", ...(token ? { Authorization: "Bearer " + token } : {}), ...(opts.headers || {}) },
    body: opts.body && typeof opts.body !== "string" ? JSON.stringify(opts.body) : opts.body,
  });
  if (r.status === 401) { logout(); throw new Error("unauthorized"); }
  if (!r.ok) throw new Error((await r.text()) || r.statusText);
  return r.json();
}
const mod = (name, path, opts) => api(`/modules/${name}${path}`, opts);

// ---------------------------------------------------------------- auth
$("#loginForm").onsubmit = async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    const r = await api("/api/auth/login", { method: "POST", body: { username: f.get("username"), password: f.get("password") } });
    token = r.token; localStorage.setItem("ellalon_token", token); start();
  } catch { $("#loginErr").textContent = "Invalid credentials"; }
};
function logout() {
  token = null; localStorage.removeItem("ellalon_token");
  fetch("/api/auth/logout", { method: "POST" });
  ws && ws.close();
  $("#shell").classList.add("hidden"); $("#login").classList.remove("hidden");
}
$("#logout").onclick = logout;

async function start() {
  try { me = await api("/api/auth/me"); } catch { return logout(); }
  $("#login").classList.add("hidden"); $("#shell").classList.remove("hidden");
  $("#who").textContent = `${me.sub} (${me.role})`;
  fetch("/api/health").then((r) => r.json()).then((h) => {
    $("#brand").innerHTML = `ELLALON <span class="build">${esc(h.build || h.version)}</span>`; $("#brand").title = `Version ${h.version}, build ${h.build}`;
  }).catch(() => {});
  cameras = await api("/api/cameras");
  connectWs(); renderWall(); renderWallSide(); loadLayouts(); refreshAlarmBadge(); show("wall");
}

// ---------------------------------------------------------------- tabs
document.querySelectorAll("nav a").forEach((a) => (a.onclick = () => show(a.dataset.tab)));
function show(tab) {
  document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("active", a.dataset.tab === tab));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("hidden", t.id !== "tab-" + tab));
  ({ incidents: loadIncidents, events: loadEvents, cameras: loadCameras, modules: loadModules, integrations: loadIntegrations, storage: loadStorageTab, playback: loadPlayback, fastpb: loadFastPb }[tab] || (() => {}))();
}

// ---------------------------------------------------------------- live events
function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/api/ws?token=${encodeURIComponent(token)}`);
  ws.onopen = () => ($("#wsDot").className = "dot ok");
  ws.onclose = () => { $("#wsDot").className = "dot bad"; if (token) setTimeout(connectWs, 3000); };
  ws.onmessage = (m) => {
    const { kind, data } = JSON.parse(m.data);
    if (kind !== "event") return;
    $("#events").insertAdjacentHTML("afterbegin", eventRow(data));
    if (data.type === "wall.updated" && data.payload?.layout_data) applyWall(data.payload.layout_data, data.payload.layout_data.name);
    if (data.type.startsWith("camera.") && !$("#tab-wall").classList.contains("hidden")) refreshWallCams();
    if (["high", "critical"].includes(data.severity)) flashCamera(data.camera_id);
    if (data.type.startsWith("camera.") && !$("#tab-cameras").classList.contains("hidden")) loadCameras();
    if (data.source === "integrations" && !$("#tab-integrations").classList.contains("hidden")) refreshIntegrationsSoon();
    if (data.type.startsWith("incident.")) {
      if (!$("#tab-incidents").classList.contains("hidden")) loadIncidents(); else refreshAlarmBadge();
      if (data.type === "incident.opened") { alarmToast(data); alarmBeep(); }
    }
  };
}
const eventRow = (e) => `<tr><td>${new Date(e.ts).toLocaleTimeString()}</td><td>${esc(e.source)}</td><td>${esc(e.type)}</td>
  <td><span class="sev ${esc(e.severity)}">${esc(e.severity)}</span></td><td>${esc(camName(e.camera_id))}</td><td><code>${esc(JSON.stringify(e.payload)).slice(0, 160)}</code></td></tr>`;
const camName = (id) => (id ? cameras.find((c) => c.id === id)?.name || "#" + id : "");
async function loadEvents() { $("#events").innerHTML = (await api("/api/events?limit=200")).map(eventRow).join(""); }

// ---------------------------------------------------------------- video wall
// Tiles are updated in place: only a tile whose camera changed reloads its stream, so adding or
// moving one camera never restarts the others.
let wallLayout = localStorage.getItem("ellalon_wall_layout") || "", wallLayouts = [];
$("#grid").value = localStorage.getItem("ellalon_wall_grid") || "2";
$("#grid").onchange = () => { wallFocus = null; wallChanged(); };
const wallN = () => +$("#grid").value;
// split screen = substream (light), single / full screen = main stream (full quality)
const tileSrc = (cam, n, main) => `/modules/streaming/stream.html?src=cam_${cam.id}${cam.substream_url && n > 1 && !main ? "_sub" : ""}&mode=webrtc,mse`;
let wallFocus = null;      // index of the tile shown full screen, or null
const wallNow = () => Array.from({ length: wallN() ** 2 }, (_, i) => (cameras.some((c) => c.id === wallTiles[i]) ? wallTiles[i] : null));

function renderWall() {
  const n = wallN(), wall = $("#wall");
  if (wallFocus !== null && !cameras.some((c) => c.id === wallTiles[wallFocus])) wallFocus = null;
  const f = wallFocus !== null;
  wall.classList.toggle("focus", f);
  wall.style.gridTemplateColumns = f ? "1fr" : `repeat(${n},1fr)`; wall.style.gridTemplateRows = f ? "1fr" : `repeat(${n},1fr)`;
  while (wall.children.length > n * n) wall.lastElementChild.remove();
  while (wall.children.length < n * n) wall.appendChild(makeTile());
  [...wall.children].forEach((t, i) => updateTile(t, i, n));
}
function makeTile() {
  const t = document.createElement("div");
  t.className = "tile empty"; t.draggable = true;
  t.innerHTML = `<div class="tvid"></div><span class="label hidden"></span><button type="button" class="tclose hidden" title="Remove from wall">×</button>
    <span class="thint">+ camera<br><small>drag from the list or click</small></span>`;
  let clickTimer;
  t.onclick = () => {           // wait a moment: a double-click must not open the camera list
    clearTimeout(clickTimer);
    clickTimer = setTimeout(() => { if (wallFocus === null) pickCamera(+t.dataset.i); }, 260);
  };
  t.ondblclick = (e) => {
    e.preventDefault(); clearTimeout(clickTimer);
    if (wallFocus === null && t.dataset.cam) focusTile(+t.dataset.i); else unfocusWall();
  };
  t.querySelector(".tclose").onclick = (e) => { e.stopPropagation(); setTile(+t.dataset.i, null); };
  t.ondragstart = (e) => {
    if (!t.dataset.cam) return e.preventDefault();
    e.dataTransfer.setData("text/plain", JSON.stringify({ from: +t.dataset.i })); e.dataTransfer.effectAllowed = "move";
  };
  t.ondragover = (e) => { e.preventDefault(); t.classList.add("drop"); };
  t.ondragleave = () => t.classList.remove("drop");
  t.ondrop = (e) => {
    e.preventDefault(); t.classList.remove("drop");
    let d; try { d = JSON.parse(e.dataTransfer.getData("text/plain")); } catch { return; }
    const to = +t.dataset.i;
    if (d.cam) setTile(to, d.cam);
    else if (Number.isInteger(d.from) && d.from !== to) {
      const x = wallTiles[d.from] ?? null; wallTiles[d.from] = wallTiles[to] ?? null; wallTiles[to] = x; wallChanged();
    }
  };
  return t;
}
function focusTile(i) {
  wallFocus = i; renderWall();
  const w = $("#tab-wall");                       // fill the monitor
  if (!document.fullscreenElement && w.requestFullscreen) w.requestFullscreen().catch(() => {});
}
function unfocusWall() {
  wallFocus = null; renderWall();
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
}
document.addEventListener("fullscreenchange", () => { if (!document.fullscreenElement && wallFocus !== null) unfocusWall(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && wallFocus !== null) unfocusWall(); });
function updateTile(t, i, n) {
  const cam = cameras.find((c) => c.id === wallTiles[i]), src = cam ? tileSrc(cam, n, wallFocus === i || n === 1) : "";
  t.classList.toggle("hidden", wallFocus !== null && wallFocus !== i);   // others keep playing, hidden
  t.dataset.i = i; t.dataset.cam = cam ? cam.id : "";
  t.classList.toggle("empty", !cam);
  if ((t.dataset.src || "") !== src) {          // only this tile reconnects
    t.dataset.src = src;
    t.querySelector(".tvid").innerHTML = src ? `<iframe src="${src}" allow="autoplay; fullscreen"></iframe>` : "";
  }
  const lbl = t.querySelector(".label"); lbl.textContent = cam ? cam.name : ""; lbl.classList.toggle("hidden", !cam);
  t.querySelector(".tclose").classList.toggle("hidden", !cam);
  t.querySelector(".thint").classList.toggle("hidden", !!cam);
}
function wallChanged() {
  localStorage.setItem("ellalon_wall", JSON.stringify(wallTiles)); localStorage.setItem("ellalon_wall_grid", $("#grid").value);
  renderWall(); renderWallSide(); layoutStatus();
}
function setTile(i, cid) { wallTiles[i] = cid; wallChanged(); }
function addToFreeTile(cid) {
  const now = wallNow(), i = now.indexOf(null);
  if (i < 0) return layoutMsg("The wall is full - drag the camera onto a tile to replace it, or choose a bigger grid.", true);
  setTile(i, cid);
}
function pickCamera(i) {
  $("#pickList").innerHTML = cameras.map((c) => `<button value="${c.id}">${esc(c.name)} <span class="muted">${esc(c.location || "")}</span></button>`).join("");
  const d = $("#picker"); d.showModal();
  d.onclose = () => { if (d.returnValue !== "cancel") setTile(i, d.returnValue === "clear" ? null : +d.returnValue); };
}
function applyWall(layout, name) {
  wallFocus = null;
  $("#grid").value = String(Math.min(6, Math.max(1, layout.cols || 2)));
  wallTiles = [...(layout.tiles || [])];
  if (name !== undefined) { wallLayout = name; localStorage.setItem("ellalon_wall_layout", name); }
  wallChanged(); fillLayoutSel();
}
$("#wallClear").onclick = () => { if (wallNow().some(Boolean) && confirm("Remove all cameras from the wall?")) { wallTiles = []; wallChanged(); } };

// ---- camera list (left side)
const sideHidden = () => localStorage.getItem("ellalon_wall_side") === "hidden";
$("#wallSide").classList.toggle("hidden", sideHidden());
$("#wallSideBtn").onclick = () => {
  localStorage.setItem("ellalon_wall_side", sideHidden() ? "shown" : "hidden");
  $("#wallSide").classList.toggle("hidden", sideHidden());
};
$("#wallSearch").oninput = renderWallSide;
function renderWallSide() {
  const q = $("#wallSearch").value.trim().toLowerCase(), on = new Set(wallNow().filter(Boolean));
  const list = cameras.filter((c) => !q || `${c.name} ${c.location || ""} ${c.settings?.ip || ""}`.toLowerCase().includes(q));
  const groups = {}; list.forEach((c) => (groups[c.location || "No location"] ||= []).push(c));
  const names = Object.keys(groups).sort(), heads = !(names.length === 1 && names[0] === "No location");
  $("#wallCams").innerHTML = names.map((g) => (heads ? `<div class="wgroup">${esc(g)}</div>` : "") + groups[g].map((c) => {
    const [cls, text] = CAM_STATE[c.status?.state] || CAM_STATE.checking;
    return `<div class="wcam${on.has(c.id) ? " on" : ""}" draggable="true" data-id="${c.id}" title="${esc(text)} - drag to a tile or click to add">
      <span class="dot ${cls}"></span><span class="wname">${esc(c.name)}</span>${on.has(c.id) ? '<span class="small">on wall</span>' : ""}</div>`;
  }).join("")).join("") || `<p class="muted small">${cameras.length ? "No camera matches" : "No cameras yet - add them in the Cameras tab"}</p>`;
  $("#wallCams").querySelectorAll(".wcam").forEach((e) => {
    e.ondragstart = (ev) => { ev.dataTransfer.setData("text/plain", JSON.stringify({ cam: +e.dataset.id })); ev.dataTransfer.effectAllowed = "copy"; };
    e.onclick = () => addToFreeTile(+e.dataset.id);
  });
}
async function refreshWallCams() {
  try { cameras = await api("/api/cameras"); } catch { return; }
  renderWall(); renderWallSide();   // statuses update; streams are not restarted
}
setInterval(() => { if (token && me && !$("#tab-wall").classList.contains("hidden")) refreshWallCams(); }, 20000);

// ---- saved layouts (stored by the video wall service, shared by all users)
let msgTimer;
function layoutMsg(text, bad) {
  const m = $("#layoutMsg"); m.textContent = text; m.className = "small " + (bad ? "err" : "muted");
  clearTimeout(msgTimer); if (text) msgTimer = setTimeout(layoutStatus, 5000);
}
function fillLayoutSel() {
  $("#layoutSel").innerHTML = `<option value="">— not saved —</option>` +
    wallLayouts.map((l) => `<option ${l.name === wallLayout ? "selected" : ""}>${esc(l.name)}</option>`).join("");
  $("#layoutSave").disabled = $("#layoutDel").disabled = !wallLayouts.some((l) => l.name === wallLayout);
}
function layoutStatus() {
  const l = wallLayouts.find((x) => x.name === wallLayout), m = $("#layoutMsg"); m.className = "small muted";
  if (!l) return (m.textContent = "");
  const n = wallN(), saved = Array.from({ length: n * n }, (_, i) => l.tiles?.[i] ?? null);
  m.textContent = l.cols !== n || JSON.stringify(saved) !== JSON.stringify(wallNow()) ? "● unsaved changes" : "";
}
async function loadLayouts() {
  try { wallLayouts = await mod("videowall", "/layouts"); }
  catch { wallLayouts = []; return layoutMsg("Saved layouts are unavailable - is the Video Wall module running? (System tab)", true); }
  if (wallLayout && !wallLayouts.some((l) => l.name === wallLayout)) wallLayout = "";
  fillLayoutSel(); layoutStatus();
}
$("#layoutSel").onchange = () => {
  const l = wallLayouts.find((x) => x.name === $("#layoutSel").value);
  if (!l) { wallLayout = ""; localStorage.setItem("ellalon_wall_layout", ""); return fillLayoutSel(), layoutStatus(); }
  applyWall(l, l.name); layoutMsg(`Loaded "${l.name}"`);
};
async function saveLayout(name) {
  const n = wallN();
  try {
    await mod("videowall", `/layouts/${encodeURIComponent(name)}`, { method: "PUT", body: { name, rows: n, cols: n, tiles: wallNow() } });
    wallLayout = name; localStorage.setItem("ellalon_wall_layout", name);
    await loadLayouts(); layoutMsg(`Saved "${name}"`);
  } catch (err) { layoutMsg("Could not save: " + errText(err), true); }
}
$("#layoutSave").onclick = () => wallLayout && saveLayout(wallLayout);
$("#layoutSaveAs").onclick = () => {
  const name = (prompt("Name for this layout:", wallLayout || "") || "").trim();
  if (!name) return;
  if (/[\/\\?#%]/.test(name) || name.length > 60) return layoutMsg("Use a name up to 60 characters without / \\ ? # %", true);
  if (wallLayouts.some((l) => l.name === name) && name !== wallLayout && !confirm(`A layout called "${name}" exists. Replace it?`)) return;
  saveLayout(name);
};
$("#layoutDel").onclick = async () => {
  if (!wallLayout || !confirm(`Delete the saved layout "${wallLayout}"? The wall itself stays as it is.`)) return;
  try { await mod("videowall", `/layouts/${encodeURIComponent(wallLayout)}`, { method: "DELETE" }); wallLayout = ""; localStorage.setItem("ellalon_wall_layout", ""); await loadLayouts(); layoutMsg("Layout deleted"); }
  catch (err) { layoutMsg(errText(err), true); }
};
function flashCamera(id) {
  document.querySelectorAll(`.tile[data-cam="${id}"]`).forEach((t) => { t.classList.add("alert"); setTimeout(() => t.classList.remove("alert"), 8000); });
}

// ---------------------------------------------------------------- alarm manager (c2 module)
let incRows = [];
const incSelected = new Set();
const SEV_NAME = { info: "Info", low: "Low", warning: "Warning", high: "High", critical: "Critical" };
$("#incFilter").onchange = loadIncidents;
$("#alarmSound").checked = localStorage.getItem("ellalon_alarm_sound") === "on";
$("#alarmSound").onchange = () => localStorage.setItem("ellalon_alarm_sound", $("#alarmSound").checked ? "on" : "off");
async function loadIncidents() {
  const f = $("#incFilter").value;
  try {
    const rows = await mod("c2", "/incidents" + (f && f !== "active" ? "?status=" + f : ""));
    incRows = f === "active" ? rows.filter((i) => i.status !== "closed") : rows;
  } catch {
    incRows = []; $("#incidents").innerHTML = `<tr><td colspan=8 class="muted">The C&C module is not running (System tab)</td></tr>`; return;
  }
  for (const id of [...incSelected]) if (!incRows.some((i) => i.id === id)) incSelected.delete(id);
  const t = (x) => new Date(x).toLocaleString();
  $("#incidents").innerHTML = incRows.map((i) => {
    const p = i.event?.payload || {}, extra = [p.target, p.rule_type ? null : p.rule, p.region && "region " + p.region, p.detail, p.confidence && Math.round(p.confidence * 100) + "% sure"].filter(Boolean).join(" · ");
    return `<tr class="inc ${esc(i.status)}">
      <td>${i.status !== "closed" ? `<input type="checkbox" class="incSel" value="${esc(i.id)}" ${incSelected.has(i.id) ? "checked" : ""}>` : ""}</td>
      <td>${t(i.created)}${i.count > 1 ? `<div class="muted small">last ${t(i.last)}</div>` : ""}</td>
      <td><span class="sev ${esc(i.severity)}">${esc(SEV_NAME[i.severity] || i.severity)}</span></td>
      <td>${p.snapshot ? `<a href="${esc(p.snapshot)}" target="_blank" title="Open the snapshot"><img class="incsnap" src="${esc(p.snapshot)}" loading="lazy" alt=""></a>` : ""}${esc(i.title)}${extra ? `<div class="muted small">${esc(extra)}</div>` : ""}</td>
      <td>${esc(camName(i.camera_id))}</td><td>${i.count > 1 ? `<b>${i.count}×</b>` : "1"}</td><td>${esc(i.status)}</td>
      <td class="nowrap">${i.camera_id ? `<button class="ghost" onclick="incLive('${esc(i.id)}')">Live</button> ` : ""}
        ${i.status === "open" ? `<button onclick="incSet('${esc(i.id)}','acknowledged')">Ack</button> ` : ""}
        ${i.status !== "closed" ? `<button class="ghost" onclick="incSet('${esc(i.id)}','closed')">Close</button>` : ""}</td></tr>`;
  }).join("") || `<tr><td colspan=8 class="muted">No alarms</td></tr>`;
  $("#incidents").querySelectorAll(".incSel").forEach((cb) => (cb.onchange = () => { cb.checked ? incSelected.add(cb.value) : incSelected.delete(cb.value); incSelUi(); }));
  const open = incRows.filter((i) => i.status === "open").length;
  $("#incSummary").textContent = f === "active" ? `${open} open · ${incRows.length - open} acknowledged` : `${incRows.length} alarm(s)`;
  incSelUi(); refreshAlarmBadge();
}
function incSelUi() {
  const open = incRows.filter((i) => i.status !== "closed");
  $("#incCloseSel").disabled = !incSelected.size;
  $("#incCloseSel").textContent = incSelected.size ? `Close selected (${incSelected.size})` : "Close selected";
  $("#incSelAll").checked = !!open.length && incSelected.size === open.length;
}
$("#incSelAll").onchange = () => { incSelected.clear(); if ($("#incSelAll").checked) incRows.filter((i) => i.status !== "closed").forEach((i) => incSelected.add(i.id)); loadIncidents(); };
async function incBulk(body) {
  try { await mod("c2", "/incidents/bulk", { method: "POST", body }); } catch (err) { alert(errText(err)); }
  incSelected.clear(); loadIncidents();
}
$("#incAckAll").onclick = () => incBulk({ ids: [], status: "acknowledged" });
$("#incCloseSel").onclick = () => { const note = prompt(`Closing note for ${incSelected.size} alarm(s) (optional):`); if (note !== null) incBulk({ ids: [...incSelected], status: "closed", note }); };
window.incSet = async (id, status) => {
  const note = status === "closed" ? prompt("Closing note (optional):") : "";
  if (note === null) return;
  await mod("c2", `/incidents/${id}`, { method: "PATCH", body: { status, note } }); loadIncidents();
};
window.incLive = (id) => {
  const i = incRows.find((x) => x.id === id); if (!i) return;
  const cam = cameras.find((c) => c.id === i.camera_id);
  $("#liveTitle").textContent = `${i.title}`;
  $("#liveInfo").textContent = `${SEV_NAME[i.severity] || i.severity} · ${i.status} · first ${new Date(i.created).toLocaleString()}${i.count > 1 ? ` · ${i.count} times` : ""}`;
  $("#liveFrame").src = cam ? `/modules/streaming/stream.html?src=cam_${cam.id}&mode=webrtc,mse` : "about:blank";
  $("#liveFrame").classList.remove("hidden"); $("#liveClip").classList.add("hidden");
  $("#liveReplay").classList.toggle("hidden", !cam?.record);
  $("#liveReplay").textContent = "⟲ 60 s before the alarm";
  $("#liveReplay").onclick = async () => {
    const b = $("#liveReplay"); b.disabled = true; b.textContent = "Cutting the clip…";
    try {
      await loadStorages().catch(() => {});
      const end = Math.floor(new Date(i.created).getTime() / 1000) + 10;   // 50 s before + 10 s after
      const url = await fetchClip(cam, 60, end);
      $("#liveFrame").classList.add("hidden"); $("#liveFrame").src = "about:blank";
      const v = $("#liveClip"); if (v.src.startsWith("blob:")) URL.revokeObjectURL(v.src);
      v.src = url; v.classList.remove("hidden"); v.play().catch(() => {});
      b.textContent = "⟲ Replaying 60 s around the alarm";
    } catch (err) { b.textContent = err.message; }
    b.disabled = false;
  };
  $("#liveAck").classList.toggle("hidden", i.status !== "open"); $("#liveClose").classList.toggle("hidden", i.status === "closed");
  $("#liveAck").onclick = async () => { await incSet(id, "acknowledged"); $("#liveDialog").close(); };
  $("#liveClose").onclick = async () => { await incSet(id, "closed"); $("#liveDialog").close(); };
  $("#liveDialog").showModal();
};
$("#liveDialog").onclose = () => {
  $("#liveFrame").src = "about:blank"; const v = $("#liveClip"); v.pause();
  if (v.src.startsWith("blob:")) URL.revokeObjectURL(v.src); v.removeAttribute("src");
};
async function refreshAlarmBadge() {
  try {
    const n = (await mod("c2", "/incidents?status=open")).length, b = $("#alarmBadge");
    b.textContent = n > 99 ? "99+" : n; b.classList.toggle("hidden", !n);
  } catch {}
}
let audioCtx;
function alarmBeep() {
  if (!$("#alarmSound").checked) return;
  try {
    audioCtx ||= new AudioContext();
    [0, 0.25].forEach((d) => { const o = audioCtx.createOscillator(), g = audioCtx.createGain();
      o.frequency.value = 880; g.gain.value = 0.15; o.connect(g); g.connect(audioCtx.destination);
      o.start(audioCtx.currentTime + d); o.stop(audioCtx.currentTime + d + 0.15); });
  } catch {}
}
function alarmToast(e) {
  const t = document.createElement("div");
  t.className = "toast " + esc(e.severity);
  t.innerHTML = `<b>New alarm</b> ${esc(e.payload?.title || "")}<div class="small">${esc(camName(e.camera_id))}</div>`;
  t.onclick = () => { t.remove(); show("incidents"); };
  $("#toasts").prepend(t); setTimeout(() => t.remove(), 8000);
  while ($("#toasts").children.length > 4) $("#toasts").lastElementChild.remove();
}

// ---------------------------------------------------------------- fast playback (last N seconds)
const fpClips = {};                 // camera id -> {url, label}
function clipPath(cam, secs, endTs) {
  const sid = cam.settings?.storage_id, q = `seconds=${secs}${endTs ? `&end=${endTs}` : ""}`;
  return storages.some((s) => s.id === sid && s.type === "recorder")
    ? `/api/recorders/${encodeURIComponent(sid)}/clip/${cam.id}?${q}` : `/modules/vms-recorder/cameras/${cam.id}/clip?${q}`;
}
async function fetchClip(cam, secs, endTs) {
  const r = await fetch(clipPath(cam, secs, endTs), { headers: { Authorization: "Bearer " + token } });
  if (!r.ok) {
    let d = `HTTP ${r.status}`; try { d = (await r.json()).detail || d; } catch {}
    throw new Error(r.status === 404 && d.startsWith("HTTP") ? "Recorder module not running" : d);
  }
  return URL.createObjectURL(await r.blob());
}
const fpSecs = () => +$("#fpSecs").value;
function fpEndTs() {
  if ($("#fpEndMode").value !== "at" || !$("#fpEnd").value) return null;
  return Math.floor(new Date($("#fpEnd").value).getTime() / 1000);
}
$("#fpEndMode").onchange = () => {
  const at = $("#fpEndMode").value === "at"; $("#fpEnd").classList.toggle("hidden", !at);
  if (at && !$("#fpEnd").value) { const d = new Date(Date.now() - 5 * 60000 - new Date().getTimezoneOffset() * 60000); $("#fpEnd").value = d.toISOString().slice(0, 19); }
  fpButtons();
};
$("#fpSecs").onchange = fpButtons;
$("#fpSearch").oninput = renderFastPb;
const fpLabel = () => `▶ ${fpSecs() >= 120 ? fpSecs() / 60 + " min" : fpSecs() + " s"}${fpEndTs() ? " before " + new Date(fpEndTs() * 1000).toLocaleTimeString() : ""}`;
function fpButtons() { document.querySelectorAll(".fpLoad").forEach((b) => (b.textContent = fpLabel())); $("#fpAll").textContent = fpLabel().replace("▶", "▶ Load all:"); }
async function loadFastPb() {
  await loadStorages().catch(() => {});
  try { cameras = await api("/api/cameras"); } catch {}
  renderFastPb();
}
function renderFastPb() {
  const q = $("#fpSearch").value.trim().toLowerCase();
  const list = cameras.filter((c) => c.record && (!q || `${c.name} ${c.location || ""}`.toLowerCase().includes(q)));
  const grid = $("#fpGrid");
  grid.innerHTML = list.map((c) => `<div class="fptile" data-id="${c.id}">
      <div class="fphead"><b>${esc(c.name)}</b> <span class="muted small">${esc(c.location || "")}</span></div>
      <div class="fpvid"><video playsinline controls muted loop></video><span class="fpmsg muted small">Not loaded</span></div>
      <div class="fpbar"><button class="fpLoad ghost"></button> <a class="fpDl link hidden" download>⬇ Download</a> <span class="fpwhen muted small"></span></div>
    </div>`).join("") || `<p class="muted">No cameras are recording. Turn on “Record” for a camera in the Cameras tab.</p>`;
  grid.querySelectorAll(".fptile").forEach((t) => {
    const id = +t.dataset.id;
    t.querySelector(".fpLoad").onclick = () => fpLoad(id);
    if (fpClips[id]) fpShow(t, fpClips[id]);
  });
  $("#fpInfo").textContent = `${list.length} recording camera(s)`;
  fpButtons();
}
function fpShow(t, clip) {
  const v = t.querySelector("video"), dl = t.querySelector(".fpDl");
  if (v.src !== clip.url) v.src = clip.url;
  t.querySelector(".fpmsg").classList.add("hidden");
  dl.href = clip.url; dl.download = clip.file; dl.classList.remove("hidden");
  t.querySelector(".fpwhen").textContent = clip.label;
}
async function fpLoad(id) {
  const t = $(`.fptile[data-id="${id}"]`), cam = cameras.find((c) => c.id === id); if (!t || !cam) return;
  const secs = fpSecs(), endTs = fpEndTs(), msg = t.querySelector(".fpmsg"), btn = t.querySelector(".fpLoad");
  btn.disabled = true; msg.className = "fpmsg muted small"; msg.textContent = "Cutting the clip…";
  try {
    const url = await fetchClip(cam, secs, endTs), end = endTs ? new Date(endTs * 1000) : new Date();
    if (fpClips[id]) URL.revokeObjectURL(fpClips[id].url);
    const from = new Date(end - secs * 1000), stamp = from.toISOString().slice(0, 19).replace(/[:T]/g, "-");
    fpClips[id] = { url, file: `${cam.name.replace(/[^\w.-]+/g, "_")}_${stamp}.mp4`, label: `${from.toLocaleTimeString()} – ${end.toLocaleTimeString()}` };
    fpShow(t, fpClips[id]);
    t.querySelector("video").play().catch(() => {});
  } catch (err) { msg.className = "fpmsg err small"; msg.textContent = err.message; }
  btn.disabled = false;
}
$("#fpAll").onclick = async () => {
  const ids = [...document.querySelectorAll(".fptile")].map((t) => +t.dataset.id);
  $("#fpAll").disabled = true; let next = 0, done = 0;
  const worker = async () => { while (next < ids.length) { await fpLoad(ids[next++]); $("#fpInfo").textContent = `Loaded ${++done} of ${ids.length}`; } };
  await Promise.all([worker(), worker(), worker()]);       // 3 at a time
  $("#fpAll").disabled = false;
};
$("#fpPlay").onclick = () => {
  const vids = [...document.querySelectorAll("#fpGrid video")].filter((v) => v.src);
  const anyPlaying = vids.some((v) => !v.paused);
  vids.forEach((v) => (anyPlaying ? v.pause() : v.play().catch(() => {})));
};
$("#fpRestart").onclick = () => document.querySelectorAll("#fpGrid video").forEach((v) => { if (v.src) { v.currentTime = 0; v.play().catch(() => {}); } });

// ---------------------------------------------------------------- playback (vms-recorder module)
function loadPlayback() {
  $("#pbCam").innerHTML = cameras.filter((c) => c.record).map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
  if (!$("#pbDay").value) $("#pbDay").value = new Date().toISOString().slice(0, 10);
}
$("#pbLoad").onclick = async () => {
  const day = $("#pbDay").value.replaceAll("-", "");
  try {
    const cid = +$("#pbCam").value, cam = cameras.find((c) => c.id === cid);
    let segs = await mod("vms-recorder", `/cameras/${cid}/recordings?day=${day}`).catch(() => []);
    segs = segs.map((s) => ({ ...s, url: "/modules/vms-recorder/" + s.url }));
    if (!storages.length) await loadStorages().catch(() => {});
    const sid = cam?.settings?.storage_id, rec = storages.find((s) => s.id === sid && s.type === "recorder");
    if (rec) segs = segs.concat(await api(`/api/recorders/${sid}/recordings?camera=${cid}&day=${day}`).catch(() => []));
    segs.sort((a, b) => a.start.localeCompare(b.start));
    $("#pbList").innerHTML = segs.map((s) => `<li data-url="${esc(s.url)}">${new Date(s.start).toLocaleTimeString()} <span class="muted">${(s.bytes / 1e6).toFixed(0)} MB</span></li>`).join("") || `<li class="muted">No recordings</li>`;
    $("#pbList").querySelectorAll("li[data-url]").forEach((li) => (li.onclick = () => { $("#pbVideo").src = li.dataset.url; $("#pbVideo").play(); }));
  } catch { $("#pbList").innerHTML = `<li class="muted">Recorder module not running</li>`; }
};

// ---------------------------------------------------------------- cameras
let brands = [], editing = null;
const COPY_FIELDS = [["brand", "Brand & ports"], ["credentials", "Username & password"], ["substream", "Substream on/off"],
  ["recording", "Recording"], ["analytics", "Analytics"], ["face_recognition", "Face recognition"], ["location", "Location"], ["enabled", "Enabled"], ["storage", "Recording storage"], ["events", "Analytics events"]];
const F_STR = ["name", "location", "brand", "ip", "username", "password", "main_path", "sub_path", "storage_id", "alarm_severity"];
const F_NUM = ["port", "onvif_port", "channel", "start_number"];
const F_BOOL = ["use_substream", "record", "analytics", "face_recognition", "enabled", "events_enabled", "events_alarm"];
const el = (f, k) => f.elements.namedItem(k);
const errText = (e) => { let t = e.message; try { t = JSON.parse(t).detail || t; } catch {} return typeof t === "string" ? t : JSON.stringify(t); };
const hideCreds = (u) => (u || "").replace(/\/\/[^@/]*@/, "//***@");

async function loadBrands() {
  if (!brands.length) brands = await api("/api/camera-brands");
  document.querySelectorAll(".brandSel").forEach((s) => {
    if (!s.options.length) s.innerHTML = brands.map((b) => `<option value="${b.id}">${esc(b.label)}</option>`).join("");
    s.onchange = () => brandUi(s.form);
  });
  await loadStorages().catch(() => {});
  document.querySelectorAll(".storageSel").forEach((sel) => storageOptions(sel, sel.value));
}
function brandUi(form) {
  const b = brands.find((x) => x.id === el(form, "brand").value) || {};
  form.querySelectorAll(".rtspOnly,.chOnly").forEach((e) => e.classList.toggle("hidden", !!b.onvif));
  form.querySelectorAll(".onvifOnly").forEach((e) => e.classList.toggle("hidden", !b.onvif));
  form.querySelectorAll(".customOnly").forEach((e) => e.classList.toggle("hidden", !b.custom));
  if (form.id === "batchForm") batchCount();
  if (form.id === "camEdit") evUi();
}
// ---- analytics events from the camera
const EV_METHOD = { hikvision: "Uses the Hikvision event stream (ISAPI) on the HTTP port.", dahua: "Uses the Dahua event stream on the HTTP port." };
function evUi() {
  const f = $("#camEdit"), on = el(f, "events_enabled").checked;
  f.querySelector(".evOnly").classList.toggle("hidden", !on);
  el(f, "alarm_severity").disabled = !el(f, "events_alarm").checked;
  $("#evMethod").textContent = EV_METHOD[el(f, "brand").value] || "Uses ONVIF events on the HTTP/ONVIF port (Axis, Provision, FLIR and other ONVIF cameras). ONVIF must be enabled on the camera.";
  const n = $("#evTypes").querySelectorAll("input").length;
  $("#evHint").textContent = n ? "Tick the events you want. All or none ticked = every event the camera sends." : "Every event the camera sends is received. Click “Find event types” to choose specific ones.";
}
function renderEvTypes(types, chosen, saved = false) {
  $("#evTypes").dataset.saved = saved ? "1" : "";
  $("#evTypes").innerHTML = types.map((t) => `<label title="${esc(t.id)}"><input type="checkbox" value="${esc(t.id)}" ${!chosen.length || chosen.includes(t.id) ? "checked" : ""}> ${esc(t.label)}</label>`).join("");
  evUi();
}
el($("#camEdit"), "events_enabled").addEventListener("change", evUi);
el($("#camEdit"), "events_alarm").addEventListener("change", evUi);
$("#evFind").onclick = async () => {
  const msg = $("#evMsg"), f = $("#camEdit"); msg.className = "small muted"; msg.textContent = "Asking the camera…"; $("#evFind").disabled = true;
  const chosen = [...$("#evTypes").querySelectorAll("input")].filter((b) => b.checked).map((b) => b.value);
  const prev = $("#evTypes").querySelectorAll("input").length;
  const wasAll = !prev || (!$("#evTypes").dataset.saved && chosen.length === prev);   // saved list = an explicit choice
  try {
    const r = await api("/api/cameras/events/probe", { method: "POST", body: formData(f) });
    renderEvTypes(r.types, wasAll ? [] : chosen);
    msg.className = "small ok"; msg.textContent = `${r.types.length} event type(s) found · ${r.method}`;
  } catch (err) { msg.className = "small err"; msg.textContent = errText(err); }
  $("#evFind").disabled = false;
};
const EV_STATE = { connected: ["ok", "Listening"], connecting: ["wait", "Connecting…"], error: ["bad", "Error"] };
const evStateHtml = (e) => {
  if (!e) return "–";
  const [cls, text] = EV_STATE[e.state] || EV_STATE.connecting;
  const last = e.last ? ` · last: ${e.last_event} at ${new Date(e.last).toLocaleString()}` : " · no events yet";
  return `<span class="cstat" title="${esc((e.method || "") + " - " + (e.detail || "") + last)}"><span class="dot ${cls}"></span> ${text}</span>`;
};
function formData(form) {
  const o = {};
  for (const k of F_STR) { const e = el(form, k); if (e) o[k] = e.value.trim(); }
  for (const k of F_NUM) { const e = el(form, k); if (e && e.value !== "") o[k] = +e.value; }
  for (const k of F_BOOL) { const e = el(form, k); if (e) o[k] = e.checked; }
  if (o.location === "") o.location = null;
  if (o.storage_id === "") o.storage_id = null;
  if (form.id === "camEdit") {          // all boxes or none ticked = all event types
    const boxes = [...$("#evTypes").querySelectorAll("input")], on = boxes.filter((b) => b.checked).map((b) => b.value);
    o.event_types = on.length === boxes.length ? [] : on;
  }
  return o;
}
function fillForm(form, s) {
  for (const k of [...F_STR, ...F_NUM]) { const e = el(form, k); if (e && s[k] != null) e.value = s[k]; }
  for (const k of F_BOOL) { const e = el(form, k); if (e && s[k] != null) e.checked = !!s[k]; }
  brandUi(form);
}

$("#camAdd").onclick = async () => {
  await loadBrands(); editing = null;
  const f = $("#camEdit"); f.reset(); brandUi(f);
  $("#camTitle").textContent = "Add camera"; $("#camUrls").textContent = ""; $("#camMsg").textContent = "";
  renderEvTypes([], []); $("#evMsg").textContent = "";
  $("#copyBox").classList.add("hidden"); $("#camDialog").showModal();
};
window.editCam = async (id) => {
  await loadBrands(); editing = cameras.find((c) => c.id === id);
  const f = $("#camEdit"); f.reset(); $("#evMsg").textContent = "";
  renderEvTypes((editing.settings.event_types || []).map((id) => ({ id, label: id })), editing.settings.event_types || [], true);
  fillForm(f, { ...editing.settings, name: editing.name });
  if (editing.events) { const [, t] = EV_STATE[editing.events.state] || []; $("#evMsg").className = "small " + (editing.events.state === "error" ? "err" : "muted");
    $("#evMsg").textContent = `${t || ""}: ${editing.events.detail || ""}`; }
  $("#camTitle").textContent = "Edit camera: " + editing.name; $("#camMsg").textContent = "";
  $("#camUrls").textContent = "Main: " + hideCreds(editing.url) + (editing.substream_url ? "   ·   Sub: " + hideCreds(editing.substream_url) : "   ·   no substream");
  $("#copyFields").innerHTML = COPY_FIELDS.map(([k, l]) => `<label><input type="checkbox" value="${k}"> ${l}</label>`).join("");
  const others = cameras.filter((c) => c.id !== id);
  $("#copyTargets").innerHTML = others.map((c) => `<label><input type="checkbox" value="${c.id}"> ${esc(c.name)}</label>`).join("") || `<span class="muted">No other cameras yet</span>`;
  $("#copyBox").classList.remove("hidden"); $("#camDialog").showModal();
};
async function saveCam() {
  const body = formData($("#camEdit"));
  const r = editing ? await api(`/api/cameras/${editing.id}`, { method: "PUT", body }) : await api("/api/cameras", { method: "POST", body });
  if (!editing) editing = r;
  return r;
}
$("#camEdit").onsubmit = async (e) => {
  e.preventDefault(); const msg = $("#camMsg"); msg.className = "muted"; msg.textContent = "Saving…";
  try { await saveCam(); $("#camDialog").close(); loadCameras(); } catch (err) { msg.className = "err"; msg.textContent = errText(err); }
};
$("#copyAll").onclick = () => $("#copyTargets").querySelectorAll("input").forEach((i) => (i.checked = true));
$("#copyApply").onclick = async () => {
  const fields = [...$("#copyFields").querySelectorAll("input:checked")].map((i) => i.value);
  const target_ids = [...$("#copyTargets").querySelectorAll("input:checked")].map((i) => +i.value);
  const msg = $("#camMsg"); msg.className = "err";
  if (!fields.length || !target_ids.length) { msg.textContent = "Choose at least one setting and one camera"; return; }
  msg.className = "muted"; msg.textContent = "Saving and copying…";
  try {
    await saveCam();
    const r = await api(`/api/cameras/${editing.id}/copy`, { method: "POST", body: { fields, target_ids } });
    msg.className = r.failed.length ? "err" : "ok";
    msg.textContent = `Updated ${r.updated.length} camera(s)` + (r.failed.length ? ` · failed: ${r.failed.map((x) => `#${x.id} ${x.error}`).join("; ")}` : "");
    loadCameras();
  } catch (err) { msg.className = "err"; msg.textContent = errText(err); }
};
document.querySelectorAll("dialog [data-close]").forEach((b) => (b.onclick = () => b.closest("dialog").close()));

// batch add
function parseIps(text) {
  const out = [];
  for (const raw of text.split(/[\s,;]+/)) {
    if (!raw) continue;
    const m = raw.match(/^(\d+\.\d+\.\d+\.)(\d+)-(?:\d+\.\d+\.\d+\.)?(\d+)$/);
    if (m) { for (let i = +m[2]; i <= +m[3] && i <= 255; i++) out.push(m[1] + i); } else out.push(raw);
  }
  return out;
}
function parseRange(text) {
  const out = [];
  for (const part of String(text).split(",")) {
    const m = part.trim().match(/^(\d+)(?:-(\d+))?$/);
    if (m) for (let i = +m[1]; i <= +(m[2] || m[1]); i++) out.push(i);
  }
  return out.length ? out : [1];
}
function batchCount() {
  const f = $("#batchForm"), b = brands.find((x) => x.id === el(f, "brand").value) || {};
  const n = parseIps(el(f, "ips").value).length * (b.onvif ? 1 : parseRange(el(f, "channels").value).length);
  $("#batchCount").textContent = n ? `${n} camera(s) will be added` : "";
}
$("#camBatch").onclick = async () => {
  await loadBrands(); const f = $("#batchForm"); f.reset(); brandUi(f);
  $("#batchMsg").textContent = ""; $("#batchDialog").showModal();
};
$("#batchForm").oninput = batchCount;
$("#batchForm").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target, b = brands.find((x) => x.id === el(f, "brand").value) || {};
  const body = { ...formData(f), name_prefix: el(f, "name_prefix").value.trim() || "Camera",
    ips: parseIps(el(f, "ips").value), channels: b.onvif ? [1] : parseRange(el(f, "channels").value) };
  const msg = $("#batchMsg"); msg.className = "muted"; msg.textContent = "Adding cameras…";
  try {
    const r = await api("/api/cameras/batch", { method: "POST", body });
    msg.className = r.failed.length ? "err" : "ok";
    msg.textContent = `Added ${r.created.length} camera(s)` + (r.failed.length ? ` · ${r.failed.length} failed: ` + r.failed.map((x) => `${x.ip} ch${x.channel}: ${x.error}`).join("; ") : "");
    loadCameras(); if (!r.failed.length) setTimeout(() => $("#batchDialog").close(), 1200);
  } catch (err) { msg.className = "err"; msg.textContent = errText(err); }
};

const CAM_STATE = { online: ["ok", "Online"], offline: ["bad", "Offline"], auth: ["bad", "Wrong user/password"],
  path: ["bad", "Wrong stream path"], error: ["bad", "Stream error"], checking: ["wait", "Checking…"], disabled: ["", "Disabled"] };
const camStateHtml = (st) => {
  const [cls, text] = CAM_STATE[st?.state] || CAM_STATE.checking;
  const since = st?.since ? ` · since ${new Date(st.since).toLocaleString()}` : "";
  return `<span class="cstat" title="${esc((st?.detail || "") + since)}"><span class="dot ${cls}"></span> ${text}</span>`;
};
$("#camTest").onclick = async () => {
  const msg = $("#camMsg"); msg.className = "muted"; msg.textContent = "Testing connection…";
  try {
    const r = await api("/api/cameras/probe", { method: "POST", body: formData($("#camEdit")) });
    const line = (n, x) => `${n}: ${CAM_STATE[x.state]?.[1] || x.state} - ${x.detail}`;
    msg.className = r.main.state === "online" && (!r.sub || r.sub.state === "online") ? "ok" : "err";
    msg.textContent = line("Main stream", r.main) + (r.sub ? "   ·   " + line("Substream", r.sub) : "");
    if (["auth", "path", "error"].includes(r.main.state)) msg.textContent += "   →  Try “Auto-detect” to find the right address.";
  } catch (err) { msg.className = "err"; msg.textContent = errText(err); }
};
// ---- every channel of an NVR
let chFound = [], chBase = null;
$("#camAllCh").onclick = async () => {
  const f = $("#camEdit"); chBase = formData(f);
  if (!chBase.ip) { $("#camMsg").className = "err"; $("#camMsg").textContent = "Enter the NVR's IP address, username and password first."; return; }
  $("#chRows").innerHTML = ""; $("#chTable").classList.add("hidden"); $("#chAdd").classList.add("hidden"); $("#chResult").textContent = "";
  $("#chMsg").className = "muted small"; $("#chMsg").textContent = "Asking the NVR for its channels and checking each one… (up to a minute)";
  $("#chDialog").showModal();
  try {
    const r = await api("/api/cameras/nvr-channels", { method: "POST", body: chBase });
    chFound = r.channels;
    const base = (chBase.name || "NVR").replace(/\s*(ch|channel)\s*\d+$/i, "").trim();
    $("#chRows").innerHTML = chFound.map((c, i) => {
      const [cls, text] = CAM_STATE[c.status.state] || CAM_STATE.checking, live = c.status.state === "online";
      return `<tr><td><input type="checkbox" class="chSel" value="${i}" ${c.existing ? "disabled" : live ? "checked" : ""}></td>
        <td>${c.channel}</td><td><input class="chName" data-i="${i}" value="${esc(c.name || `${base} Ch ${c.channel}`)}" ${c.existing ? "disabled" : ""}></td>
        <td class="small"><span class="dot ${cls}"></span> ${c.existing ? `already added as ${esc(c.existing)}` : esc(text)}${!live && !c.existing ? ` <span class="muted">${esc(c.status.detail || "")}</span>` : ""}</td></tr>`;
    }).join("");
    const live = chFound.filter((c) => c.status.state === "online" && !c.existing).length;
    $("#chMsg").textContent = `${chFound.length} channel(s) found via ${r.how} · ${live} new with video. Channels without a camera are not ticked.`;
    $("#chTable").classList.remove("hidden"); $("#chAdd").classList.toggle("hidden", !chFound.some((c) => !c.existing));
    $("#chAll").checked = !!live;
  } catch (err) { $("#chMsg").className = "err small"; $("#chMsg").textContent = errText(err); }
};
$("#chAll").onchange = () => $("#chRows").querySelectorAll(".chSel:not(:disabled)").forEach((cb) => (cb.checked = $("#chAll").checked));
$("#chAdd").onclick = async () => {
  const pick = [...$("#chRows").querySelectorAll(".chSel:checked")].map((cb) => +cb.value);
  if (!pick.length) return;
  const res = $("#chResult"), viaOnvif = /ONVIF/.test($("#chMsg").textContent); let added = 0; const failed = [];
  $("#chAdd").disabled = true;
  for (const [n, i] of pick.entries()) {
    const c = chFound[i], name = $(`.chName[data-i="${i}"]`).value.trim() || `NVR Ch ${c.channel}`;
    res.className = "small muted"; res.textContent = `Adding ${n + 1} of ${pick.length}: ${name}…`;
    const body = viaOnvif
      ? { ...chBase, name, brand: "rtsp", port: c.port, channel: 1, main_path: c.main_path, sub_path: c.sub_path }
      : { ...chBase, name, channel: c.channel };
    try { const cam = await api("/api/cameras", { method: "POST", body }); added++; c.existing = cam.name; }
    catch (err) { failed.push(`Ch ${c.channel}: ${errText(err)}`); }
  }
  $("#chAdd").disabled = false;
  res.className = failed.length ? "small err" : "small ok";
  res.innerHTML = `Added ${added} camera(s).` + (failed.length ? "<br>" + failed.map(esc).join("<br>") : "");
  $("#chRows").querySelectorAll(".chSel:checked").forEach((cb) => { if (chFound[+cb.value].existing) { cb.checked = false; cb.disabled = true; } });
  loadCameras();
};

$("#camAuto").onclick = async () => {
  const f = $("#camEdit"), msg = $("#camMsg"), btn = $("#camAuto");
  msg.className = "muted"; msg.textContent = "Looking for the right stream address (ONVIF, then known formats)…"; btn.disabled = true;
  try {
    const r = await api("/api/cameras/autodetect", { method: "POST", body: formData(f) });
    const lines = r.tried.map((t) => `${t.state === "online" ? "✓" : "✗"} ${t.what} - ${CAM_STATE[t.state]?.[1] || t.state}: ${t.detail}`);
    if (r.found) {
      el(f, "brand").value = "rtsp"; el(f, "port").value = r.found.port;
      el(f, "main_path").value = r.found.main_path; el(f, "sub_path").value = r.found.sub_path; brandUi(f);
      msg.className = "ok";
      msg.innerHTML = `<b>Found a working address (${esc(r.found.how)}).</b> It is filled in below as a custom address - click Save.<br><span class="small">${lines.map(esc).join("<br>")}</span>`;
    } else {
      msg.className = "err";
      msg.innerHTML = `<b>${esc(r.advice || "No working address found.")}</b><br><span class="small">${lines.map(esc).join("<br>")}</span>`;
    }
  } catch (err) { msg.className = "err"; msg.textContent = errText(err); }
  btn.disabled = false;
};
setInterval(() => { if (token && !$("#tab-cameras").classList.contains("hidden")) loadCameras(); }, 15000);

async function loadCameras() {
  cameras = await api("/api/cameras");
  await loadBrands().catch(() => {});
  const admin = me.role === "admin", yes = (v) => (v ? "✓" : "–");
  const label = (id) => brands.find((b) => b.id === id)?.label.replace(/ \(.*\)$/, "") || id || "";
  ["#camAdd", "#camBatch", "#camDiscover", "#camDelSel", "#camDelAll"].forEach((b) => $(b).classList.toggle("hidden", !admin));
  document.querySelectorAll("#tab-cameras .adm").forEach((e) => e.classList.toggle("hidden", !admin));
  for (const id of [...camSelected]) if (!cameras.some((c) => c.id === id)) camSelected.delete(id);
  $("#cameras").innerHTML = cameras.map((c) => { const s = c.settings || {}, onv = ["onvif", "flir"].includes(s.brand);
    return `<tr>${admin ? `<td><input type="checkbox" class="camSel" value="${c.id}" ${camSelected.has(c.id) ? "checked" : ""}></td>` : ""}<td>${c.id}</td><td>${esc(c.name)}</td><td>${camStateHtml(c.status)}</td><td>${esc(label(s.brand))}</td><td>${esc(s.ip)}</td><td>${onv ? "–" : esc(s.channel)}</td>
    <td>${esc(c.location || "")}</td><td>${yes(c.substream_url)}</td><td>${yes(c.record)}</td><td>${yes(c.analytics)}</td><td>${yes(c.face_recognition)}</td><td>${evStateHtml(c.events)}</td>
    <td class="nowrap">${admin ? `<button class="ghost" onclick="editCam(${c.id})">Edit</button> <button class="ghost" onclick="anOpen(${c.id})">Analytics</button> <button class="danger" onclick="delCam(${c.id})">Delete</button>` : ""}</td></tr>`; }).join("")
    || `<tr><td colspan=14 class="muted">No cameras yet</td></tr>`;
  $("#cameras").querySelectorAll(".camSel").forEach((cb) => (cb.onchange = () => { cb.checked ? camSelected.add(+cb.value) : camSelected.delete(+cb.value); camSelUi(); }));
  camSelUi();
  const n = (k) => cameras.filter((c) => c.status?.state === k).length, bad = cameras.filter((c) => ["offline", "auth", "path", "error"].includes(c.status?.state)).length;
  $("#camSummary").innerHTML = cameras.length ? `<b>${n("online")}</b> online · <b>${bad}</b> with problems${n("checking") ? ` · ${n("checking")} checking` : ""}` : "";
}
// ---- video analytics rules (zones, lines) drawn on the camera picture
const AN_CLASSES = [["person", "People"], ["car", "Cars"], ["truck", "Trucks"], ["bus", "Buses"], ["motorcycle", "Motorcycles"],
  ["bicycle", "Bicycles"], ["dog", "Dogs"], ["cat", "Cats"], ["backpack", "Bags (backpack)"], ["suitcase", "Suitcases"]];
const AN_COLORS = ["#ffc850", "#3ca0ff", "#78dc78", "#e678e6", "#ff5a5a", "#50dcff"];
const AN_TYPE = { intrusion: "Intrusion", line: "Line crossing", loitering: "Loitering", motion: "Motion" };
let anCam = null, anRules = [], anSel = null, anDrawing = false, anLiveTimer = null;
$("#arClasses").innerHTML = AN_CLASSES.map(([v, l]) => `<label><input type="checkbox" value="${v}"> ${l}</label>`).join("");
const anRule = () => anRules.find((r) => r.id === anSel);

window.anOpen = async (cid) => {
  anCam = cameras.find((c) => c.id === cid); if (!anCam) return;
  anRules = JSON.parse(JSON.stringify(anCam.analytics_rules || [])); anSel = null; anDrawing = false;
  $("#anTitle").textContent = `Analytics - ${anCam.name}`; $("#anOn").checked = !!anCam.analytics || !anRules.length;
  $("#anMsg").textContent = ""; $("#anLive").checked = false; $("#anEdit").classList.add("hidden");
  $("#anDialog").showModal(); anPicture(); anModuleStatus(); anList(); anDraw();
};
$("#anDialog").addEventListener("close", () => { clearInterval(anLiveTimer); anLiveTimer = null; });

async function anModuleStatus() {
  const st = $("#anStatus"), btn = $("#anStart");
  try {
    const m = await api("/api/modules/analytics");
    const run = m.container?.state === "running";
    st.className = "small " + (run ? "ok" : "err");
    st.textContent = run ? "● Analytics app running" : m.enabled ? "● Analytics app starting / not running" + (m.error ? ": " + m.error : "") : "● Analytics app is off";
    btn.classList.toggle("hidden", run || m.enabled);
  } catch { st.className = "small err"; st.textContent = "Analytics app not available"; btn.classList.add("hidden"); }
}
$("#anStart").onclick = async () => {
  $("#anStart").disabled = true;
  try { await api("/api/modules/analytics/enable", { method: "POST" }); }
  catch (err) {
    $("#anStatus").className = "small err";
    $("#anStatus").textContent = /not found|pull/i.test(errText(err))
      ? "The analytics app is not installed yet. On the server run:  sudo ellalon build analytics   then click Start again."
      : errText(err);
  }
  $("#anStart").disabled = false; setTimeout(anModuleStatus, 1500);
};

function anPicture(live) {
  const img = $("#anImg");
  img.onload = () => { $("#anNoImg").classList.add("hidden"); anDraw(); };
  img.onerror = () => {
    if (live) { $("#anHint").textContent = "No live analysis yet for this camera - save with analytics on and wait a few seconds."; return; }
    $("#anNoImg").classList.remove("hidden"); anDraw();
  };
  img.src = live ? `/modules/analytics/cameras/${anCam.id}/preview.jpg?t=${Date.now()}`
                 : `/modules/streaming/api/frame.jpeg?src=cam_${anCam.id}&t=${Date.now()}`;
}
$("#anRefresh").onclick = () => anPicture($("#anLive").checked);
$("#anLive").onchange = () => {
  clearInterval(anLiveTimer); anLiveTimer = null;
  if ($("#anLive").checked) { anPicture(true); anLiveTimer = setInterval(() => anPicture(true), 1000); $("#anHint").textContent = "Green boxes = objects being tracked. Rules shown as saved."; }
  else { anPicture(false); $("#anHint").textContent = ""; }
};

function anList() {
  $("#anRules").innerHTML = anRules.map((r, i) => `<div class="anRule ${r.id === anSel ? "sel" : ""}" onclick="anPick('${r.id}')">
      <span class="sw" style="background:${AN_COLORS[i % AN_COLORS.length]}"></span>
      <span class="grow"><b>${esc(r.name || AN_TYPE[r.type])}</b><br><span class="muted small">${AN_TYPE[r.type]}${r.type !== "motion" ? " · " + esc(r.classes.join(", ")) : ""}${r.alarm ? " · alarm " + r.severity : " · events only"}${r.enabled ? "" : " · off"}</span></span>
      <button type="button" class="ghost small" onclick="event.stopPropagation(); anDel('${r.id}')" title="Delete rule">×</button></div>`).join("")
    || `<p class="muted small">No rules yet: the camera only reports motion anywhere in the picture. Add a rule to watch for people or vehicles in a zone or crossing a line.</p>`;
}
window.anPick = (id) => { anSel = id; anDrawing = false; anFill(); anList(); anDraw(); };
window.anDel = (id) => { anRules = anRules.filter((r) => r.id !== id); if (anSel === id) { anSel = null; $("#anEdit").classList.add("hidden"); } anList(); anDraw(); };
$("#anAdd").onclick = () => {
  const r = { id: Math.random().toString(36).slice(2, 10), name: "", type: "intrusion", enabled: true, zone: [], line: [], direction: "both",
    classes: ["person"], min_confidence: 0.5, seconds: 10, sensitivity: 2, cooldown: 10, alarm: true, severity: "high" };
  r.name = `Zone ${anRules.length + 1}`; anRules.push(r); anSel = r.id; anDrawing = true; anFill(); anList(); anDraw();
};
function anFill() {
  const r = anRule(); $("#anEdit").classList.toggle("hidden", !r); if (!r) return;
  $("#arName").value = r.name; $("#arType").value = r.type; $("#arConf").value = Math.round(r.min_confidence * 100);
  $("#arSecs").value = r.seconds; $("#arDir").value = r.direction; $("#arSens").value = r.sensitivity; $("#arCool").value = r.cooldown;
  $("#arAlarm").checked = r.alarm; $("#arSev").value = r.severity; $("#arEnabled").checked = r.enabled;
  $("#arClasses").querySelectorAll("input").forEach((cb) => (cb.checked = r.classes.includes(cb.value)));
  anTypeUi();
}
function anTypeUi() {
  const r = anRule(); if (!r) return; const t = r.type;
  document.querySelectorAll("#anEdit .arObj").forEach((e) => e.classList.toggle("hidden", t === "motion"));
  document.querySelectorAll("#anEdit .arLoiter").forEach((e) => e.classList.toggle("hidden", t !== "loitering"));
  document.querySelectorAll("#anEdit .arLine").forEach((e) => e.classList.toggle("hidden", t !== "line"));
  document.querySelectorAll("#anEdit .arMotion").forEach((e) => e.classList.toggle("hidden", t !== "motion"));
  $("#arSev").disabled = !$("#arAlarm").checked;
  const pts = t === "line" ? r.line : r.zone;
  $("#arDraw").textContent = anDrawing ? "Stop drawing" : t === "line" ? (pts.length ? "Redraw the line" : "Draw the line") : (pts.length ? "Redraw the zone" : "Draw the zone");
  $("#anHint").textContent = anDrawing
    ? (t === "line" ? "Click the two ends of the line on the picture." : "Click the corners of the zone; click the first corner (or double-click) to close it.")
    : (t !== "line" && !r.zone.length ? "No zone drawn = the whole picture." : "");
}
["arName", "arConf", "arSecs", "arDir", "arSens", "arCool", "arSev"].forEach((id) => ($("#" + id).oninput = $("#" + id).onchange = anRead));
["arAlarm", "arEnabled"].forEach((id) => ($("#" + id).onchange = anRead));
$("#arClasses").onchange = anRead;
$("#arType").onchange = () => { const r = anRule(); r.type = $("#arType").value; r.zone = []; r.line = []; anDrawing = true;
  if (/^Zone \d+$|^Line \d+$|^$/.test(r.name)) r.name = `${r.type === "line" ? "Line" : "Zone"} ${anRules.indexOf(r) + 1}`; anFill(); anList(); anDraw(); };
function anRead() {
  const r = anRule(); if (!r) return;
  r.name = $("#arName").value; r.min_confidence = (+$("#arConf").value || 50) / 100; r.seconds = +$("#arSecs").value || 10;
  r.direction = $("#arDir").value; r.sensitivity = +$("#arSens").value || 2; r.cooldown = +$("#arCool").value || 0;
  r.alarm = $("#arAlarm").checked; r.severity = $("#arSev").value; r.enabled = $("#arEnabled").checked;
  r.classes = [...$("#arClasses").querySelectorAll("input:checked")].map((c) => c.value);
  anTypeUi(); anList(); anDraw();
}
$("#arDraw").onclick = () => { const r = anRule(); if (!anDrawing) { if (r.type === "line") r.line = []; else r.zone = []; } anDrawing = !anDrawing; anTypeUi(); anDraw(); };
$("#arClear").onclick = () => { const r = anRule(); r.zone = []; r.line = []; anDrawing = true; anTypeUi(); anDraw(); };
$("#arDone").onclick = () => { anDrawing = false; anSel = null; $("#anEdit").classList.add("hidden"); $("#anHint").textContent = ""; anList(); anDraw(); };

const anCanvas = $("#anCanvas");
function anXY(e) { const b = anCanvas.getBoundingClientRect(); return [Math.min(1, Math.max(0, (e.clientX - b.left) / b.width)), Math.min(1, Math.max(0, (e.clientY - b.top) / b.height))]; }
anCanvas.onclick = (e) => {
  const r = anRule(); if (!r || !anDrawing) return; const p = anXY(e);
  if (r.type === "line") { r.line.push(p); if (r.line.length >= 2) { r.line = r.line.slice(-2); anDrawing = false; } }
  else {
    const f = r.zone[0], b = anCanvas.getBoundingClientRect();
    if (f && r.zone.length >= 3 && Math.hypot((f[0] - p[0]) * b.width, (f[1] - p[1]) * b.height) < 12) anDrawing = false;
    else r.zone.push(p);
  }
  anTypeUi(); anDraw();
};
anCanvas.ondblclick = () => { const r = anRule(); if (r && anDrawing && r.type !== "line" && r.zone.length >= 3) { anDrawing = false; anTypeUi(); anDraw(); } };
function anDraw() {
  const img = $("#anImg"), stage = $("#anStage");
  const w = stage.clientWidth, h = img.naturalWidth && !$("#anNoImg").offsetParent ? Math.round(w * img.naturalHeight / img.naturalWidth) : Math.round(w * 9 / 16);
  stage.style.height = h + "px"; anCanvas.width = w; anCanvas.height = h;
  const g = anCanvas.getContext("2d"); g.clearRect(0, 0, w, h);
  if ($("#anLive").checked) return;                 // live preview already shows the saved rules
  anRules.forEach((r, i) => {
    const col = AN_COLORS[i % AN_COLORS.length], sel = r.id === anSel, P = (p) => [p[0] * w, p[1] * h];
    g.strokeStyle = col; g.fillStyle = col; g.lineWidth = sel ? 3 : 2; g.globalAlpha = r.enabled ? 1 : 0.4;
    if (r.type === "line" && r.line.length) {
      const [a, b] = [P(r.line[0]), r.line[1] ? P(r.line[1]) : null];
      g.beginPath(); g.arc(a[0], a[1], 4, 0, 7); g.fill();
      if (b) {
        g.beginPath(); g.moveTo(...a); g.lineTo(...b); g.stroke();
        const m = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2], d = [b[0] - a[0], b[1] - a[1]], L = Math.hypot(...d) || 1, n = [-d[1] / L * 28, d[0] / L * 28];
        const arrow = (sgn) => { const e = [m[0] + sgn * n[0], m[1] + sgn * n[1]]; g.beginPath(); g.moveTo(...m); g.lineTo(...e); g.stroke();
          const ang = Math.atan2(e[1] - m[1], e[0] - m[0]); g.beginPath(); g.moveTo(...e);
          g.lineTo(e[0] - 9 * Math.cos(ang - 0.5), e[1] - 9 * Math.sin(ang - 0.5)); g.lineTo(e[0] - 9 * Math.cos(ang + 0.5), e[1] - 9 * Math.sin(ang + 0.5)); g.closePath(); g.fill(); };
        if (r.direction !== "b_to_a") arrow(1); if (r.direction !== "a_to_b") arrow(-1);
        g.font = "bold 12px sans-serif"; g.fillText("A", a[0] + 6, a[1] - 6); g.fillText("B", b[0] + 6, b[1] - 6);
      }
    } else if (r.type !== "line" && r.zone.length) {
      g.beginPath(); r.zone.forEach((p, k) => (k ? g.lineTo(...P(p)) : g.moveTo(...P(p))));
      if (!(sel && anDrawing)) { g.closePath(); g.globalAlpha *= 0.2; g.fill(); g.globalAlpha = r.enabled ? 1 : 0.4; }
      g.stroke(); r.zone.forEach((p) => { g.beginPath(); g.arc(...P(p), sel ? 4 : 2.5, 0, 7); g.fill(); });
    } else return;
    const lp = P((r.type === "line" ? r.line : r.zone)[0]);
    g.globalAlpha = 1; g.font = "bold 13px sans-serif"; g.fillStyle = "#000a"; const t = r.name || AN_TYPE[r.type];
    g.fillRect(lp[0] + 6, lp[1] + 6, g.measureText(t).width + 8, 18); g.fillStyle = col; g.fillText(t, lp[0] + 10, lp[1] + 19);
  });
  anCanvas.style.cursor = anDrawing ? "crosshair" : "default";
}
window.addEventListener("resize", () => { if ($("#anDialog").open) anDraw(); });
$("#anSave").onclick = async () => {
  const msg = $("#anMsg"), bad = anRules.find((r) => (r.type === "line" && r.line.length !== 2) || (r.type !== "line" && r.zone.length && r.zone.length < 3) || (r.type !== "motion" && !r.classes.length));
  if (bad) { msg.className = "small err"; msg.textContent = `“${bad.name}”: ${bad.type === "line" ? "draw the line (2 points)" : !bad.classes.length ? "choose at least one object" : "a zone needs at least 3 corners"}`; return; }
  try { await api(`/api/cameras/${anCam.id}/analytics`, { method: "PUT", body: { enabled: $("#anOn").checked, rules: anRules } }); }
  catch (err) { msg.className = "small err"; msg.textContent = errText(err); return; }
  msg.className = "small ok"; msg.textContent = $("#anOn").checked ? "Saved. The analytics app picks up the new rules within a few seconds." : "Saved (analytics off for this camera).";
  await loadCameras(); anCam = cameras.find((x) => x.id === anCam.id) || anCam;
};

// ---- select & delete
const camSelected = new Set();
function camSelUi() {
  const n = camSelected.size;
  $("#camDelSel").disabled = !n; $("#camDelSel").textContent = n ? `Delete selected (${n})` : "Delete selected";
  $("#camSelAll").checked = n > 0 && n === cameras.length; $("#camSelAll").indeterminate = n > 0 && n < cameras.length;
}
$("#camSelAll").onchange = () => { camSelected.clear(); if ($("#camSelAll").checked) cameras.forEach((c) => camSelected.add(c.id)); loadCameras(); };
async function deleteCameras(body, what) {
  try { const r = await api("/api/cameras/delete", { method: "POST", body }); camSelected.clear(); alert(`Deleted ${r.deleted} camera(s).`); }
  catch (err) { alert(errText(err)); }
  loadCameras();
}
$("#camDelSel").onclick = () => {
  const names = cameras.filter((c) => camSelected.has(c.id)).map((c) => c.name);
  if (confirm(`Delete ${names.length} camera(s)?\n\n${names.slice(0, 15).join("\n")}${names.length > 15 ? "\n..." : ""}\n\nRecordings already saved are kept.`))
    deleteCameras({ ids: [...camSelected] });
};
$("#camDelAll").onclick = () => {
  if (!cameras.length) return;
  const t = prompt(`Delete ALL ${cameras.length} cameras? Recordings already saved are kept.\nType DELETE to confirm:`);
  if (t && t.trim().toUpperCase() === "DELETE") deleteCameras({ all: true });
};

// ---- discovery
let discFound = [];
const VENDOR_NAME = (d) => [d.vendor, d.model].filter(Boolean).join(" ") || '<span class="muted">unknown</span>';
$("#camDiscover").onclick = async () => {
  await loadBrands(); const f = $("#discForm"); f.reset();
  $("#discMsg").textContent = ""; $("#discResult").textContent = ""; discFound = [];
  ["#discTable", "#discAddBox", "#discAdd"].forEach((x) => $(x).classList.add("hidden"));
  document.querySelectorAll("#discForm .storageSel").forEach((sel) => storageOptions(sel, ""));
  $("#discDialog").showModal();
};
$("#discScan").onclick = async () => {
  const msg = $("#discMsg"), btn = $("#discScan"); btn.disabled = true; msg.className = "muted small";
  let t = 0; const timer = setInterval(() => (msg.textContent = `Scanning the network… ${++t}s`), 1000);
  try {
    const r = await api("/api/cameras/discover", { method: "POST", body: { ranges: el($("#discForm"), "ranges").value } });
    discFound = r.devices;
    const fresh = discFound.filter((d) => !d.existing).length;
    msg.textContent = `Found ${discFound.length} device(s), ${fresh} new · scanned ${r.scanned} addresses on ${r.networks.join(", ") || "the given ranges"} in ${r.seconds}s`;
    const opts = (sel) => brands.map((b) => `<option value="${b.id}" ${b.id === sel ? "selected" : ""}>${esc(b.label)}</option>`).join("");
    $("#discRows").innerHTML = discFound.map((d, i) => `<tr>
      <td><input type="checkbox" class="discSel" value="${i}" ${d.existing ? "disabled" : "checked"}></td>
      <td>${esc(d.ip)}</td><td>${VENDOR_NAME(d)}${d.note ? `<div class="err small">${esc(d.note)}</div>` : ""}</td>
      <td>${esc(d.name || "")}${d.mac ? `<div class="muted small">${esc(d.mac)}</div>` : ""}</td>
      <td class="small">${esc(d.sources.join(", "))}</td>
      <td>${d.existing ? `<span class="muted small">already added as ${esc(d.existing)}</span>` : `<select class="discBrand" data-i="${i}">${opts(d.brand)}</select>`}</td></tr>`).join("")
      || `<tr><td colspan=6 class="muted">No cameras found. Check that this server is on the cameras' network, or enter a range.</td></tr>`;
    $("#discTable").classList.remove("hidden");
    $("#discAddBox").classList.toggle("hidden", !fresh); $("#discAdd").classList.toggle("hidden", !fresh);
    $("#discAll").checked = !!fresh;
  } catch (err) { msg.className = "err small"; msg.textContent = errText(err); }
  clearInterval(timer); btn.disabled = false;
};
$("#discAll").onchange = () => $("#discRows").querySelectorAll(".discSel:not(:disabled)").forEach((cb) => (cb.checked = $("#discAll").checked));
$("#discAdd").onclick = async () => {
  const f = $("#discForm"), rows = [...$("#discRows").querySelectorAll(".discSel:checked")].map((cb) => +cb.value);
  if (!rows.length) return;
  const common = { username: el(f, "username").value.trim(), password: el(f, "password").value, location: el(f, "location").value.trim() || null,
    storage_id: el(f, "storage_id").value || null, use_substream: el(f, "use_substream").checked, record: el(f, "record").checked };
  const res = $("#discResult"); res.className = "small muted"; let added = 0; const failed = [];
  for (const [n, i] of rows.entries()) {
    const d = discFound[i], brand = $(`.discBrand[data-i="${i}"]`).value;
    res.textContent = `Adding ${n + 1} of ${rows.length}: ${d.ip}…`;
    const body = { ...common, brand, ip: d.ip, onvif_port: d.onvif_port || 80, name: `${d.model || d.vendor || "Camera"} ${d.ip}`.trim() };
    try { const c = await api("/api/cameras", { method: "POST", body }); added++; d.existing = c.name; }
    catch (err) { failed.push(`${d.ip}: ${errText(err)}`); }
  }
  res.className = failed.length ? "small err" : "small ok";
  res.innerHTML = `Added ${added} camera(s).` + (failed.length ? `<br>Not added:<br>${failed.map(esc).join("<br>")}` : "");
  $("#discRows").querySelectorAll(".discSel:checked").forEach((cb) => { if (discFound[+cb.value].existing) { cb.checked = false; cb.disabled = true; } });
  loadCameras();
};

window.delCam = async (id) => { if (confirm("Delete camera?")) { await api(`/api/cameras/${id}`, { method: "DELETE" }); loadCameras(); } };

// ---------------------------------------------------------------- integrations (3rd-party systems)
let intTypes = null, intSystems = [], intDevices = [], intEditing = null, linkDevice = null, intTimer = null;
const ACTION_LABEL = { arm: "Arm", stay: "Stay", disarm: "Disarm", bypass: "Bypass", unbypass: "Unbypass", unlock: "Unlock" };
const imod = (path, opts) => mod("integrations", path, opts);

function deviceState(d) {
  const s = d.state || {}, tag = (t, cls = "") => `<span class="sev ${cls}">${t}</span>`;
  if (d.kind === "partition") {
    if (s.triggered) return tag("ALARM", "critical");
    if (s.armed) return tag("Armed", "warning");
    if (s.partially_armed) return tag("Stay armed", "warning");
    if (s.arming) return tag("Arming…", "low");
    return tag(s.ready === false ? "Not ready" : "Disarmed");
  }
  if (d.kind === "zone") {
    if (s.alarmed) return tag("ALARM", "critical");
    if (s.bypassed) return tag("Bypassed", "low");
    return tag(s.triggered ? "Open" : "Closed", s.triggered ? "warning" : "");
  }
  return s.last_event ? esc(s.last_event) : "";
}
async function loadIntegrations() {
  try {
    if (!intTypes) intTypes = await imod("/types");
    [intSystems, intDevices] = await Promise.all([imod("/systems"), imod("/devices")]);
    if (!cameras.length) cameras = await api("/api/cameras");
  } catch (e) {
    $("#intSystems").innerHTML = `<p class="muted">Integrations module is not running (System tab → enable "3rd-party Integrations").</p>`;
    $("#intDevices").innerHTML = ""; return;
  }
  const admin = me.role === "admin", f = $("#intFilter"), keep = f.value;
  $("#intAdd").classList.toggle("hidden", !admin);
  f.innerHTML = `<option value="">All systems</option>` + intSystems.map((s) => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join("");
  f.value = intSystems.some((s) => s.id === keep) ? keep : "";
  $("#intSystems").innerHTML = intSystems.map((s) => `<div class="card mod">
    <h3><span class="dot ${s.status === "connected" ? "ok" : ["error", "connecting"].includes(s.status) ? "bad" : ""}"></span> ${esc(s.name)}</h3>
    <div class="muted">${esc(intTypes[s.type]?.label || s.type)} · ${esc(s.status)} · ${s.devices} devices</div>
    ${s.error ? `<p class="${s.status === "manual" ? "muted small" : "err"}">${esc(s.error)}</p>` : ""}
    ${s.inbound_url ? `<p class="muted small">Push events to: ${location.origin}${esc(s.inbound_url)}</p>` : ""}
    <div class="actions">
      <button onclick="intImport('${s.id}')">Import devices</button>
      <button class="ghost" onclick="intReconnect('${s.id}')">Reconnect</button>
      ${admin ? `<button class="ghost" onclick="intEdit('${s.id}')">Edit</button><button class="danger" onclick="intDelete('${s.id}')">Delete</button>` : ""}
    </div></div>`).join("") || `<p class="muted">No systems yet${admin ? " - click “Add system”." : "."}</p>`;
  renderIntDevices();
}
function renderIntDevices() {
  const sysF = $("#intFilter").value, sysName = (id) => intSystems.find((s) => s.id === id)?.name || id;
  const camName = (id) => cameras.find((c) => c.id === id)?.name || "#" + id;
  const rows = intDevices.filter((d) => !sysF || d.system_id === sysF);
  $("#intDevices").innerHTML = rows.map((d) => {
    const type = intSystems.find((s) => s.id === d.system_id)?.type, acts = intTypes?.[type]?.actions?.[d.kind] || [];
    const id = encodeURIComponent(d.id);
    return `<tr><td>${esc(sysName(d.system_id))}</td><td>${esc(d.kind)}</td><td>${esc(d.number)}</td><td>${esc(d.name)}</td><td>${deviceState(d)}</td>
      <td>${d.cameras.map((c) => esc(camName(c))).join(", ") || '<span class="muted">–</span>'} <a class="link" onclick="intLink('${id}')">link</a></td>
      <td>${acts.map((a) => `<button class="${a === "disarm" || a === "unlock" ? "" : "ghost"}" onclick="intAct('${id}','${a}')">${ACTION_LABEL[a] || a}</button>`).join(" ")}</td></tr>`;
  }).join("") || `<tr><td colspan=7 class="muted">No devices yet - add a system and click “Import devices”.</td></tr>`;
}
$("#intFilter").onchange = renderIntDevices;
function refreshIntegrationsSoon() { clearTimeout(intTimer); intTimer = setTimeout(loadIntegrations, 700); }

function intRenderFields(type, cfg = {}) {
  const t = intTypes[type];
  $("#intFields").innerHTML = t.fields.map((f) => {
    const v = cfg[f.key] ?? f.default ?? "";
    if (f.type === "checkbox") return `<label class="inline"><input type="checkbox" name="cfg_${f.key}" ${v ? "checked" : ""}> ${esc(f.label)}</label>`;
    const ph = f.type === "password" && cfg["has_" + f.key] ? "(unchanged)" : f.placeholder || "";
    return `<label>${esc(f.label)}<input name="cfg_${f.key}" type="${f.type || "text"}" value="${esc(v)}" placeholder="${esc(ph)}" ${f.required ? "required" : ""} autocomplete="off"></label>`;
  }).join("");
  $("#intHelp").textContent = type === "apollo"
    ? "Apollo: the live link needs the APACS API Service (APACS 3.7+). Its endpoint paths are listed in the API documentation on your APACS server. Without it, import doors from CSV and push events to the inbound URL shown on the system card."
    : "RISCO: connects to the panel's IP module on the LAN. On mono-socket IP modules, RISCO Cloud may have to be disabled for a local connection.";
}
$("#intAdd").onclick = async () => {
  if (!intTypes) intTypes = await imod("/types");
  intEditing = null; const f = $("#intForm"); f.reset();
  $("#intType").innerHTML = Object.entries(intTypes).map(([k, t]) => `<option value="${k}">${esc(t.label)}</option>`).join("");
  $("#intType").disabled = false; $("#intType").onchange = () => intRenderFields($("#intType").value);
  intRenderFields($("#intType").value); $("#intTitle").textContent = "Add system"; $("#intMsg").textContent = "";
  $("#intDialog").showModal();
};
window.intEdit = (sid) => {
  const s = intSystems.find((x) => x.id === sid); intEditing = s; const f = $("#intForm"); f.reset();
  $("#intType").innerHTML = `<option value="${s.type}">${esc(intTypes[s.type].label)}</option>`; $("#intType").disabled = true;
  el(f, "name").value = s.name; el(f, "enabled").checked = s.enabled;
  intRenderFields(s.type, s.config); $("#intTitle").textContent = "Edit " + s.name; $("#intMsg").textContent = "";
  $("#intDialog").showModal();
};
$("#intForm").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target, type = $("#intType").value, config = {};
  for (const fd of intTypes[type].fields) {
    const i = el(f, "cfg_" + fd.key);
    config[fd.key] = fd.type === "checkbox" ? i.checked : fd.type === "number" ? (i.value === "" ? fd.default : +i.value) : i.value.trim();
  }
  const body = { type, name: el(f, "name").value.trim(), enabled: el(f, "enabled").checked, config };
  const msg = $("#intMsg"); msg.className = "muted"; msg.textContent = "Saving…";
  try {
    await (intEditing ? imod(`/systems/${intEditing.id}`, { method: "PUT", body }) : imod("/systems", { method: "POST", body }));
    $("#intDialog").close(); loadIntegrations(); setTimeout(loadIntegrations, 4000);
  } catch (err) { msg.className = "err"; msg.textContent = errText(err); }
};
window.intDelete = async (sid) => { if (confirm("Delete this system and its devices?")) { await imod(`/systems/${sid}`, { method: "DELETE" }); loadIntegrations(); } };
window.intReconnect = async (sid) => { await imod(`/systems/${sid}/reconnect`, { method: "POST" }); loadIntegrations(); setTimeout(loadIntegrations, 4000); };
window.intImport = async (sid) => {
  const s = intSystems.find((x) => x.id === sid);
  if (s.type === "apollo") { const f = $("#importForm"); f.reset(); f.dataset.sid = sid; $("#importMsg").textContent = ""; $("#importDialog").showModal(); return; }
  try { const r = await imod(`/systems/${sid}/import`, { method: "POST", body: {} }); alert(`Imported ${r.imported} devices`); loadIntegrations(); }
  catch (err) { alert(errText(err)); }
};
$("#importForm").onsubmit = async (e) => {
  e.preventDefault(); const f = e.target, msg = $("#importMsg"); msg.className = "muted"; msg.textContent = "Importing…";
  try {
    const r = await imod(`/systems/${f.dataset.sid}/import`, { method: "POST", body: { csv: el(f, "csv").value } });
    msg.className = "ok"; msg.textContent = `Imported ${r.imported} doors`; loadIntegrations(); setTimeout(() => $("#importDialog").close(), 1000);
  } catch (err) { msg.className = "err"; msg.textContent = errText(err); }
};
window.intAct = async (id, action) => {
  if (["disarm", "unlock", "bypass"].includes(action) && !confirm(`${ACTION_LABEL[action]} - are you sure?`)) return;
  try { await imod(`/devices/${id}/action`, { method: "POST", body: { action } }); refreshIntegrationsSoon(); }
  catch (err) { alert(errText(err)); }
};
window.intLink = (id) => {
  linkDevice = intDevices.find((d) => d.id === decodeURIComponent(id));
  $("#linkTitle").textContent = "Cameras for " + linkDevice.name;
  $("#linkCams").innerHTML = cameras.map((c) => `<label><input type="checkbox" value="${c.id}" ${linkDevice.cameras.includes(c.id) ? "checked" : ""}> ${esc(c.name)}</label>`).join("") || `<span class="muted">Add cameras first</span>`;
  $("#linkDialog").showModal();
};
$("#linkForm").onsubmit = async (e) => {
  e.preventDefault();
  const ids = [...$("#linkCams").querySelectorAll("input:checked")].map((i) => +i.value);
  try { await imod(`/devices/${encodeURIComponent(linkDevice.id)}/cameras`, { method: "PUT", body: { cameras: ids } }); $("#linkDialog").close(); loadIntegrations(); }
  catch (err) { alert(errText(err)); }
};

// ---------------------------------------------------------------- storage
let storages = [], stEditing = null;
const ST_TYPE = { local: "Local disk / folder", smb: "SMB share", nfs: "NFS share", recorder: "Recording server" };
const ST_FIELDS = ["name", "type", "path", "server", "share", "username", "password", "domain", "options"];
const gb = (x) => (x == null ? "?" : x >= 1000 ? (x / 1000).toFixed(1) + " TB" : x.toFixed(x < 10 ? 1 : 0) + " GB");
async function loadStorages() { storages = await api("/api/storage"); return storages; }
function storageOptions(sel, current) {
  const def = storages.find((s) => s.is_default);
  sel.innerHTML = `<option value="">Default storage${def ? ` (${esc(def.name)})` : ""}</option>` +
    storages.filter((s) => s.enabled).map((s) => `<option value="${esc(s.id)}">${esc(s.name)}${s.type === "recorder" ? " (recording server)" : ""}</option>`).join("");
  sel.value = current && storages.some((s) => s.id === current) ? current : "";
}
async function loadStorageTab() {
  try { await loadStorages(); } catch { return; }
  let stat = {};
  try { stat = await mod("vms-recorder", "/storage"); } catch {}
  const admin = me.role === "admin";
  $("#stAdd").classList.toggle("hidden", !admin);
  $("#stList").innerHTML = storages.map((s) => {
    if (s.type === "recorder") return recorderCard(s, admin);
    const x = stat[s.id] || {}, used = s.max_gb ? Math.min(100, ((x.recordings_gb || 0) / s.max_gb) * 100) : x.total_gb ? 100 - (x.free_gb / x.total_gb) * 100 : 0;
    const st = !s.enabled ? ["", "Disabled"] : s.mount_error || x.mounted === false ? ["bad", "Not connected"]
      : x.writable === false ? ["bad", "Cannot write"] : x.mounted ? ["ok", "Connected"] : ["wait", "Checking…"];
    return `<div class="card mod">
      <h3><span class="dot ${st[0]}"></span> ${esc(s.name)} ${s.is_default ? '<span class="sev low">default</span>' : ""}</h3>
      <div class="muted">${ST_TYPE[s.type] || s.type} · ${esc(s.location)}</div>
      ${s.mount_error ? `<p class="err small">${esc(s.mount_error)}</p>` : ""}
      ${x.total_gb ? `<div class="bar"><span class="${used > 90 ? "full" : ""}" style="width:${used.toFixed(0)}%"></span></div>
        <div class="small muted">${reservedLine(s, x)}${x.oldest ? " · oldest " + new Date(x.oldest).toLocaleDateString() : ""}</div>` : ""}
      <p class="small">${st[1]} · ${s.cameras} camera(s) · keep ${s.retention_days} days · keep ${s.min_free_gb} GB free${s.max_gb ? ` · ${gb(s.max_gb)} reserved` : ""}</p>
      ${admin ? `<div class="actions"><button class="ghost" onclick="stEdit('${s.id}')">Edit</button>
        ${s.is_default ? "" : `<button class="ghost" onclick="stDefault('${s.id}')">Make default</button> <button class="danger" onclick="stDelete('${s.id}')">Delete</button>`}</div>` : ""}
    </div>`;
  }).join("");
}
function reservedLine(s, x) {
  if (!s.max_gb) return `${gb(x.free_gb)} free of ${gb(x.total_gb)} · recordings ${gb(x.recordings_gb || 0)}`;
  const warn = x.total_gb && s.max_gb > (x.recordings_gb || 0) + x.free_gb ? ` · <span class="err">only ${gb((x.recordings_gb || 0) + x.free_gb)} available on the disk</span>` : "";
  return `recordings ${gb(x.recordings_gb || 0)} of ${gb(s.max_gb)} reserved · disk ${gb(x.free_gb)} free of ${gb(x.total_gb)}${warn}`;
}
function recorderCard(s, admin) {
  const r = s.recorder || {}, cams = Object.entries(r.cameras || {}), d = r.disk || {};
  const used = s.max_gb ? Math.min(100, ((d.recordings_gb || 0) / s.max_gb) * 100) : d.total_gb ? 100 - (d.free_gb / d.total_gb) * 100 : 0;
  const rec = cams.filter(([, c]) => c.state === "recording").length, bad = cams.filter(([, c]) => c.state === "failing");
  const st = !r.registered ? ["wait", "Not installed yet"] : r.online ? ["ok", "Online"] : ["bad", "Offline (keeps recording on its own)"];
  return `<div class="card mod">
    <h3><span class="dot ${st[0]}"></span> ${esc(s.name)} <span class="sev">recording server</span></h3>
    <div class="muted">${r.registered ? `${esc(r.hostname)} · ${esc(r.address)} · ${esc(r.os)} · v${esc(r.version)}` : "Waiting for installation"}</div>
    ${d.total_gb ? `<div class="bar"><span class="${used > 90 ? "full" : ""}" style="width:${used.toFixed(0)}%"></span></div>
      <div class="small muted">${reservedLine(s, d)}${d.oldest ? " · oldest " + new Date(d.oldest).toLocaleDateString() : ""} · ${esc(r.folder || "")}</div>` : ""}
    <p class="small">${st[1]}${r.last_seen && !r.online && r.registered ? " · last seen " + new Date(r.last_seen).toLocaleString() : ""} · ${r.online ? `${rec} of ${cams.length} camera(s) recording` : `${s.cameras} camera(s) assigned`} · keep ${s.retention_days} days · keep ${s.min_free_gb} GB free${s.max_gb ? ` · ${gb(s.max_gb)} reserved` : ""}</p>
    ${bad.length ? `<ul class="camlist">${bad.map(([, c]) => `<li class="bad">${esc(c.name)}: ${esc((c.error || "not recording").split("\n").pop())}</li>`).join("")}</ul>` : ""}
    ${admin ? `<div class="actions"><button class="ghost" onclick="stEdit('${s.id}')">Edit</button>
      <button class="ghost" onclick="recInstall('${s.id}')">${r.registered ? "Reinstall / move" : "Install commands"}</button>
      <button class="danger" onclick="stDelete('${s.id}')">Delete</button></div>` : ""}
  </div>`;
}
function showInstall(name, code, expires) {
  const srv = location.origin, raw = "https://raw.githubusercontent.com/5kst5btj87-rgb/ellalon/main/recorder";
  $("#recTitle").textContent = "Install the recording server: " + name;
  $("#recCode").textContent = code;
  $("#recExp").textContent = expires ? "(valid until " + new Date(expires).toLocaleString() + ", one use)" : "";
  $("#recUbuntu").textContent = `curl -fsSL ${raw}/install-recorder.sh | sudo bash -s -- --server ${srv} --code ${code}`;
  $("#recWindows").textContent = `& ([scriptblock]::Create((irm ${raw}/install-recorder.ps1))) -Server ${srv} -Code ${code}`;
  $("#recDialog").showModal();
}
document.querySelectorAll("[data-copy]").forEach((b) => (b.onclick = async () => {
  try { await navigator.clipboard.writeText($("#" + b.dataset.copy).textContent); b.textContent = "Copied"; }
  catch { const r = document.createRange(); r.selectNodeContents($("#" + b.dataset.copy)); getSelection().removeAllRanges(); getSelection().addRange(r); b.textContent = "Selected - press Ctrl+C"; }
  setTimeout(() => (b.textContent = "Copy"), 2500);
}));
window.recInstall = async (id) => {
  const s = storages.find((x) => x.id === id);
  if (s.recorder?.registered && !confirm("Create a new pairing code? Installing it on a machine replaces the current recording server for this storage.")) return;
  try { const r = await api(`/api/storage/${id}/pairing`, { method: "POST" }); showInstall(s.name, r.code, r.code_expires); }
  catch (err) { alert(errText(err)); }
};
function stTypeUi() {
  const t = $("#stType").value, f = $("#stForm");
  f.querySelectorAll(".st-path").forEach((e) => e.classList.toggle("hidden", t !== "local" && t !== "recorder"));
  f.querySelectorAll(".st-remote").forEach((e) => e.classList.toggle("hidden", t === "local" || t === "recorder"));
  f.querySelectorAll(".st-smb").forEach((e) => e.classList.toggle("hidden", t !== "smb"));
  $("#stShareLbl").textContent = t === "nfs" ? "Export path" : "Share name (optionally /folder)";
  $("#stPathLbl").textContent = t === "recorder" ? "Recording folder on the recording server (empty = default)" : "Folder on this server";
  el(f, "path").placeholder = t === "recorder" ? "Ubuntu: /var/lib/ellalon-recorder/recordings · Windows: C:\\EllalonRecordings" : "/mnt/disk2/recordings";
  $("#stTest").classList.toggle("hidden", t === "recorder");
  el(f, "share").placeholder = t === "nfs" ? "/volume1/recordings" : "recordings";
  $("#stHelp").textContent = {
    local: "A folder on this server, for example a second disk mounted at /mnt/disk2. The folder is created if it does not exist.",
    smb: "A Windows file share or NAS share. The user needs read and write permission on the share. Use the server's IP address.",
    nfs: "An NFS export. On the NAS, allow this server's IP address with read/write access.",
    recorder: "Another Ubuntu or Windows machine records the cameras you assign to it. After saving you get a one-line install command for that machine.",
  }[t];
}
$("#stType").onchange = stTypeUi;
function stBody() {
  const f = $("#stForm"), b = {};
  for (const k of ST_FIELDS) b[k] = el(f, k).value.trim();
  b.retention_days = +el(f, "retention_days").value || 30;
  b.min_free_gb = +el(f, "min_free_gb").value || 0;
  b.max_gb = Math.max(0, +el(f, "max_gb").value || 0);
  b.enabled = el(f, "enabled").checked;
  return b;
}
$("#stAdd").onclick = () => {
  stEditing = null; const f = $("#stForm"); f.reset(); el(f, "password").placeholder = ""; $("#stType").disabled = false;
  $("#stTitle").textContent = "Add storage"; $("#stMsg").textContent = ""; stTypeUi(); $("#stDialog").showModal();
};
window.stEdit = (id) => {
  const s = storages.find((x) => x.id === id), f = $("#stForm"); stEditing = s; f.reset();
  for (const k of ST_FIELDS) if (s[k] != null) el(f, k).value = s[k];
  el(f, "password").value = ""; el(f, "password").placeholder = s.has_password ? "(unchanged)" : "";
  el(f, "retention_days").value = s.retention_days; el(f, "min_free_gb").value = s.min_free_gb; el(f, "max_gb").value = s.max_gb || 0; el(f, "enabled").checked = s.enabled;
  $("#stType").disabled = true;   // a storage keeps its type; add a new one to change it
  $("#stTitle").textContent = "Edit " + s.name; $("#stMsg").textContent = ""; stTypeUi(); $("#stDialog").showModal();
};
$("#stTest").onclick = async () => {
  const msg = $("#stMsg"); msg.className = "muted"; msg.textContent = "Testing: connecting and writing a test file…";
  try {
    const r = await api("/api/storage/test" + (stEditing ? `?id=${encodeURIComponent(stEditing.id)}` : ""), { method: "POST", body: stBody() });
    msg.className = r.ok ? "ok" : "err";
    msg.textContent = r.ok ? `OK - connected and writable, ${gb(r.free_gb)} free of ${gb(r.total_gb)}` : r.detail;
  } catch (err) { msg.className = "err"; msg.textContent = errText(err); }
};
$("#stForm").onsubmit = async (e) => {
  e.preventDefault(); const msg = $("#stMsg"); msg.className = "muted"; msg.textContent = "Saving…";
  try {
    const body = stBody();
    const r = await (stEditing ? api(`/api/storage/${stEditing.id}`, { method: "PUT", body }) : api("/api/storage", { method: "POST", body }));
    $("#stDialog").close(); loadStorageTab(); setTimeout(loadStorageTab, 20000);
    if (!stEditing && r.code) showInstall(body.name, r.code, new Date(Date.now() + 864e5).toISOString());
  } catch (err) { msg.className = "err"; msg.textContent = errText(err); }
};
window.stDefault = async (id) => {
  try { await api(`/api/storage/${id}/default`, { method: "POST" }); loadStorageTab(); setTimeout(loadStorageTab, 20000); }
  catch (err) { alert(errText(err)); }
};
window.stDelete = async (id) => {
  if (!confirm("Delete this storage? Recordings already saved on it are not deleted.")) return;
  try { await api(`/api/storage/${id}`, { method: "DELETE" }); loadStorageTab(); } catch (err) { alert(errText(err)); }
};

// ---------------------------------------------------------------- system / modules
$("#reload").onclick = async () => { await api("/api/modules/reload", { method: "POST" }); loadModules(); };
async function loadModules() {
  const mods = await api("/api/modules"), admin = me.role === "admin";
  $("#modules").innerHTML = mods.map((m) => `<div class="card mod">
    <h3><span class="dot ${m.healthy ? "ok" : m.enabled ? "bad" : ""}"></span> ${esc(m.display_name || m.name)}</h3>
    <div class="muted">${esc(m.category)} · v${esc(m.version)} · ${esc(m.container.state)}</div>
    <p>${esc(m.description)}</p>
    ${m.error ? `<p class="err">${esc(m.error)}</p>` : ""}
    ${m.heartbeat?.status ? `<code>${esc(JSON.stringify(m.heartbeat.status))}</code>` : ""}
    <div class="actions">
      ${admin ? (m.enabled ? `<button class="ghost" onclick="modAct('${m.name}','disable')">Disable</button>` : `<button onclick="modAct('${m.name}','enable')">Enable</button>`) : ""}
      <button class="ghost" onclick="modAct('${m.name}','restart')">Restart</button>
      <button class="ghost" onclick="modLogs('${m.name}')">Logs</button>
      ${m.url ? `<a class="muted" href="${m.url}docs" target="_blank">API</a>` : ""}
    </div></div>`).join("");
}
window.modAct = async (n, a) => { try { await api(`/api/modules/${n}/${a}`, { method: "POST" }); } catch (e) { alert(e.message); } loadModules(); };
window.modLogs = async (n) => { const l = $("#logs"); l.classList.remove("hidden"); l.textContent = (await api(`/api/modules/${n}/logs?tail=300`)).logs; l.scrollTop = l.scrollHeight; };

// ---------------------------------------------------------------- change password
$("#pwBtn").onclick = () => { $("#pwForm").reset(); $("#pwMsg").textContent = ""; $("#pwDialog").showModal(); };
$("#pwCancel").onclick = () => $("#pwDialog").close();
$("#pwForm").onsubmit = async (e) => {
  e.preventDefault();
  const f = new FormData(e.target), msg = $("#pwMsg");
  msg.className = "err";
  if (f.get("next") !== f.get("confirm")) { msg.textContent = "New passwords do not match"; return; }
  try {
    await api("/api/auth/password", { method: "POST", body: { current_password: f.get("current"), new_password: f.get("next") } });
    msg.className = "ok"; msg.textContent = "Password changed";
    setTimeout(() => $("#pwDialog").close(), 1200);
  } catch (err) {
    let t = err.message; try { t = JSON.parse(t).detail || t; } catch {}
    msg.textContent = t;
  }
};

if (token) start(); else $("#login").classList.remove("hidden");
