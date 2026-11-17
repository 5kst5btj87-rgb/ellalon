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
  cameras = await api("/api/cameras");
  connectWs(); renderWall(); show("wall");
}

// ---------------------------------------------------------------- tabs
document.querySelectorAll("nav a").forEach((a) => (a.onclick = () => show(a.dataset.tab)));
function show(tab) {
  document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("active", a.dataset.tab === tab));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("hidden", t.id !== "tab-" + tab));
  ({ incidents: loadIncidents, events: loadEvents, cameras: loadCameras, modules: loadModules, integrations: loadIntegrations, storage: loadStorageTab, playback: loadPlayback }[tab] || (() => {}))();
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
    if (data.type === "wall.updated" && data.payload?.layout_data) applyWall(data.payload.layout_data);
    if (["high", "critical"].includes(data.severity)) flashCamera(data.camera_id);
    if (data.type.startsWith("camera.") && !$("#tab-cameras").classList.contains("hidden")) loadCameras();
    if (data.source === "integrations" && !$("#tab-integrations").classList.contains("hidden")) refreshIntegrationsSoon();
    if (data.type.startsWith("incident.") && !$("#tab-incidents").classList.contains("hidden")) loadIncidents();
  };
}
const eventRow = (e) => `<tr><td>${new Date(e.ts).toLocaleTimeString()}</td><td>${esc(e.source)}</td><td>${esc(e.type)}</td>
  <td><span class="sev ${esc(e.severity)}">${esc(e.severity)}</span></td><td>${esc(camName(e.camera_id))}</td><td><code>${esc(JSON.stringify(e.payload)).slice(0, 160)}</code></td></tr>`;
const camName = (id) => (id ? cameras.find((c) => c.id === id)?.name || "#" + id : "");
async function loadEvents() { $("#events").innerHTML = (await api("/api/events?limit=200")).map(eventRow).join(""); }

// ---------------------------------------------------------------- video wall
$("#grid").onchange = renderWall;
function renderWall() {
  const n = +$("#grid").value, wall = $("#wall");
  wall.style.gridTemplateColumns = `repeat(${n},1fr)`; wall.style.gridTemplateRows = `repeat(${n},1fr)`;
  wall.innerHTML = "";
  for (let i = 0; i < n * n; i++) {
    const cid = wallTiles[i], cam = cameras.find((c) => c.id === cid);
    const t = document.createElement("div");
    t.className = "tile" + (cam ? "" : " empty"); t.dataset.cam = cid || "";
    t.innerHTML = cam
      ? `<iframe src="/modules/streaming/stream.html?src=cam_${cam.id}${cam.substream_url && n > 2 ? "_sub" : ""}&mode=webrtc,mse" allow="autoplay"></iframe><span class="label">${esc(cam.name)}</span>`
      : "+ camera";
    t.onclick = () => pickCamera(i);
    wall.appendChild(t);
  }
}
function pickCamera(i) {
  $("#pickList").innerHTML = cameras.map((c) => `<button value="${c.id}">${esc(c.name)} <span class="muted">${esc(c.location || "")}</span></button>`).join("");
  const d = $("#picker"); d.showModal();
  d.onclose = () => {
    if (d.returnValue === "cancel") return;
    wallTiles[i] = d.returnValue === "clear" ? null : +d.returnValue;
    localStorage.setItem("ellalon_wall", JSON.stringify(wallTiles)); renderWall();
  };
}
function applyWall(layout) {
  $("#grid").value = String(Math.min(4, Math.max(1, layout.cols || 2)));
  wallTiles = layout.tiles || []; renderWall();
}
function flashCamera(id) {
  document.querySelectorAll(`.tile[data-cam="${id}"]`).forEach((t) => { t.classList.add("alert"); setTimeout(() => t.classList.remove("alert"), 8000); });
}

// ---------------------------------------------------------------- incidents (c2 module)
$("#incFilter").onchange = loadIncidents;
async function loadIncidents() {
  try {
    const rows = await mod("c2", "/incidents" + ($("#incFilter").value ? "?status=" + $("#incFilter").value : ""));
    $("#incidents").innerHTML = rows.map((i) => `<tr><td>${new Date(i.created).toLocaleString()}</td>
      <td><span class="sev ${esc(i.severity)}">${esc(i.severity)}</span></td><td>${esc(i.title)}</td><td>${esc(camName(i.camera_id))}</td><td>${esc(i.status)}</td>
      <td>${i.status === "open" ? `<button onclick="incSet('${i.id}','acknowledged')">Ack</button>` : ""}
          ${i.status !== "closed" ? `<button class="ghost" onclick="incSet('${i.id}','closed')">Close</button>` : ""}</td></tr>`).join("") || `<tr><td colspan=6 class="muted">No incidents</td></tr>`;
  } catch { $("#incidents").innerHTML = `<tr><td colspan=6 class="muted">C&C module not running</td></tr>`; }
}
window.incSet = async (id, status) => { const note = status === "closed" ? prompt("Closing note") || "" : ""; await mod("c2", `/incidents/${id}`, { method: "PATCH", body: { status, note } }); loadIncidents(); };

// ---------------------------------------------------------------- playback (vms-recorder module)
function loadPlayback() {
  $("#pbCam").innerHTML = cameras.filter((c) => c.record).map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
  if (!$("#pbDay").value) $("#pbDay").value = new Date().toISOString().slice(0, 10);
}
$("#pbLoad").onclick = async () => {
  const day = $("#pbDay").value.replaceAll("-", "");
  try {
    const segs = await mod("vms-recorder", `/cameras/${$("#pbCam").value}/recordings?day=${day}`);
    $("#pbList").innerHTML = segs.map((s) => `<li data-url="/modules/vms-recorder/${esc(s.url)}">${new Date(s.start).toLocaleTimeString()} <span class="muted">${(s.bytes / 1e6).toFixed(0)} MB</span></li>`).join("") || `<li class="muted">No recordings</li>`;
    $("#pbList").querySelectorAll("li[data-url]").forEach((li) => (li.onclick = () => { $("#pbVideo").src = li.dataset.url; $("#pbVideo").play(); }));
  } catch { $("#pbList").innerHTML = `<li class="muted">Recorder module not running</li>`; }
};

// ---------------------------------------------------------------- cameras
let brands = [], editing = null;
const COPY_FIELDS = [["brand", "Brand & ports"], ["credentials", "Username & password"], ["substream", "Substream on/off"],
  ["recording", "Recording"], ["analytics", "Analytics"], ["face_recognition", "Face recognition"], ["location", "Location"], ["enabled", "Enabled"], ["storage", "Recording storage"]];
const F_STR = ["name", "location", "brand", "ip", "username", "password", "main_path", "sub_path", "storage_id"];
const F_NUM = ["port", "onvif_port", "channel", "start_number"];
const F_BOOL = ["use_substream", "record", "analytics", "face_recognition", "enabled"];
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
}
function formData(form) {
  const o = {};
  for (const k of F_STR) { const e = el(form, k); if (e) o[k] = e.value.trim(); }
  for (const k of F_NUM) { const e = el(form, k); if (e && e.value !== "") o[k] = +e.value; }
  for (const k of F_BOOL) { const e = el(form, k); if (e) o[k] = e.checked; }
  if (o.location === "") o.location = null;
  if (o.storage_id === "") o.storage_id = null;
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
  $("#copyBox").classList.add("hidden"); $("#camDialog").showModal();
};
window.editCam = async (id) => {
  await loadBrands(); editing = cameras.find((c) => c.id === id);
  const f = $("#camEdit"); f.reset(); fillForm(f, { ...editing.settings, name: editing.name });
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
  } catch (err) { msg.className = "err"; msg.textContent = errText(err); }
};
setInterval(() => { if (token && !$("#tab-cameras").classList.contains("hidden")) loadCameras(); }, 15000);

async function loadCameras() {
  cameras = await api("/api/cameras");
  await loadBrands().catch(() => {});
  const admin = me.role === "admin", yes = (v) => (v ? "✓" : "–");
  const label = (id) => brands.find((b) => b.id === id)?.label.replace(/ \(.*\)$/, "") || id || "";
  $("#camAdd").classList.toggle("hidden", !admin); $("#camBatch").classList.toggle("hidden", !admin);
  $("#cameras").innerHTML = cameras.map((c) => { const s = c.settings || {}, onv = ["onvif", "flir"].includes(s.brand);
    return `<tr><td>${c.id}</td><td>${esc(c.name)}</td><td>${camStateHtml(c.status)}</td><td>${esc(label(s.brand))}</td><td>${esc(s.ip)}</td><td>${onv ? "–" : esc(s.channel)}</td>
    <td>${esc(c.location || "")}</td><td>${yes(c.substream_url)}</td><td>${yes(c.record)}</td><td>${yes(c.analytics)}</td><td>${yes(c.face_recognition)}</td>
    <td>${admin ? `<button class="ghost" onclick="editCam(${c.id})">Edit</button> <button class="danger" onclick="delCam(${c.id})">Delete</button>` : ""}</td></tr>`; }).join("")
    || `<tr><td colspan=12 class="muted">No cameras yet</td></tr>`;
  const n = (k) => cameras.filter((c) => c.status?.state === k).length, bad = cameras.filter((c) => ["offline", "auth", "path", "error"].includes(c.status?.state)).length;
  $("#camSummary").innerHTML = cameras.length ? `<b>${n("online")}</b> online · <b>${bad}</b> with problems${n("checking") ? ` · ${n("checking")} checking` : ""}` : "";
}
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
const ST_TYPE = { local: "Local disk / folder", smb: "SMB share", nfs: "NFS share" };
const ST_FIELDS = ["name", "type", "path", "server", "share", "username", "password", "domain", "options"];
const gb = (x) => (x == null ? "?" : x >= 1000 ? (x / 1000).toFixed(1) + " TB" : x.toFixed(x < 10 ? 1 : 0) + " GB");
async function loadStorages() { storages = await api("/api/storage"); return storages; }
function storageOptions(sel, current) {
  const def = storages.find((s) => s.is_default);
  sel.innerHTML = `<option value="">Default storage${def ? ` (${esc(def.name)})` : ""}</option>` +
    storages.filter((s) => s.enabled).map((s) => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join("");
  sel.value = current && storages.some((s) => s.id === current) ? current : "";
}
async function loadStorageTab() {
  try { await loadStorages(); } catch { return; }
  let stat = {};
  try { stat = await mod("vms-recorder", "/storage"); } catch {}
  const admin = me.role === "admin";
  $("#stAdd").classList.toggle("hidden", !admin);
  $("#stList").innerHTML = storages.map((s) => {
    const x = stat[s.id] || {}, used = x.total_gb ? 100 - (x.free_gb / x.total_gb) * 100 : 0;
    const st = !s.enabled ? ["", "Disabled"] : s.mount_error || x.mounted === false ? ["bad", "Not connected"]
      : x.writable === false ? ["bad", "Cannot write"] : x.mounted ? ["ok", "Connected"] : ["wait", "Checking…"];
    return `<div class="card mod">
      <h3><span class="dot ${st[0]}"></span> ${esc(s.name)} ${s.is_default ? '<span class="sev low">default</span>' : ""}</h3>
      <div class="muted">${ST_TYPE[s.type] || s.type} · ${esc(s.location)}</div>
      ${s.mount_error ? `<p class="err small">${esc(s.mount_error)}</p>` : ""}
      ${x.total_gb ? `<div class="bar"><span class="${used > 90 ? "full" : ""}" style="width:${used.toFixed(0)}%"></span></div>
        <div class="small muted">${gb(x.free_gb)} free of ${gb(x.total_gb)} · recordings ${gb(x.recordings_gb || 0)}${x.oldest ? " · oldest " + new Date(x.oldest).toLocaleDateString() : ""}</div>` : ""}
      <p class="small">${st[1]} · ${s.cameras} camera(s) · keep ${s.retention_days} days · keep ${s.min_free_gb} GB free</p>
      ${admin ? `<div class="actions"><button class="ghost" onclick="stEdit('${s.id}')">Edit</button>
        ${s.is_default ? "" : `<button class="ghost" onclick="stDefault('${s.id}')">Make default</button> <button class="danger" onclick="stDelete('${s.id}')">Delete</button>`}</div>` : ""}
    </div>`;
  }).join("");
}
function stTypeUi() {
  const t = $("#stType").value, f = $("#stForm");
  f.querySelectorAll(".st-local").forEach((e) => e.classList.toggle("hidden", t !== "local"));
  f.querySelectorAll(".st-remote").forEach((e) => e.classList.toggle("hidden", t === "local"));
  f.querySelectorAll(".st-smb").forEach((e) => e.classList.toggle("hidden", t !== "smb"));
  $("#stShareLbl").textContent = t === "nfs" ? "Export path" : "Share name (optionally /folder)";
  el(f, "share").placeholder = t === "nfs" ? "/volume1/recordings" : "recordings";
  $("#stHelp").textContent = {
    local: "A folder on this server, for example a second disk mounted at /mnt/disk2. The folder is created if it does not exist.",
    smb: "A Windows file share or NAS share. The user needs read and write permission on the share. Use the server's IP address.",
    nfs: "An NFS export. On the NAS, allow this server's IP address with read/write access.",
  }[t];
}
$("#stType").onchange = stTypeUi;
function stBody() {
  const f = $("#stForm"), b = {};
  for (const k of ST_FIELDS) b[k] = el(f, k).value.trim();
  b.retention_days = +el(f, "retention_days").value || 30;
  b.min_free_gb = +el(f, "min_free_gb").value || 0;
  b.enabled = el(f, "enabled").checked;
  return b;
}
$("#stAdd").onclick = () => {
  stEditing = null; const f = $("#stForm"); f.reset(); el(f, "password").placeholder = "";
  $("#stTitle").textContent = "Add storage"; $("#stMsg").textContent = ""; stTypeUi(); $("#stDialog").showModal();
};
window.stEdit = (id) => {
  const s = storages.find((x) => x.id === id), f = $("#stForm"); stEditing = s; f.reset();
  for (const k of ST_FIELDS) if (s[k] != null) el(f, k).value = s[k];
  el(f, "password").value = ""; el(f, "password").placeholder = s.has_password ? "(unchanged)" : "";
  el(f, "retention_days").value = s.retention_days; el(f, "min_free_gb").value = s.min_free_gb; el(f, "enabled").checked = s.enabled;
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
    await (stEditing ? api(`/api/storage/${stEditing.id}`, { method: "PUT", body: stBody() }) : api("/api/storage", { method: "POST", body: stBody() }));
    $("#stDialog").close(); loadStorageTab(); setTimeout(loadStorageTab, 20000);
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
