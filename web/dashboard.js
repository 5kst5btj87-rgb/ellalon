// ---------------------------------------------------------------- dashboard
// One screen with the state of everything: cameras, alarms, recording, access control, modules,
// event charts, storage, things that need attention, and reports. Refreshes itself.
let dbT = null, dbLast = null, dbBusy = false;
const SEVC = { info: "#5b8def", low: "#58a6ff", warning: "#ffb340", high: "#ff7a45", critical: "#ff4d4f" };
const SEVS = ["info", "low", "warning", "high", "critical"];

async function loadDashboard() {
  if (dbBusy) return;
  dbBusy = true;
  const hours = +$("#dbRange").value, tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const since = new Date(Date.now() - hours * 3600e3).toISOString(), group = hours <= 24 ? "hour" : "day";
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const q = (o) => new URLSearchParams(o).toString();
  const get = (p, d) => p.then((x) => x).catch(() => d);
  const [cams, mods, stors, recStats, systems, devs, incs, evTime, evType, evCam, evToday, reports] = await Promise.all([
    get(api("/api/cameras"), []), get(api("/api/modules"), []), get(api("/api/storage"), []), get(mod("vms-recorder", "/storage"), {}),
    get(mod("integrations", "/systems"), []), get(mod("integrations", "/devices"), []), get(mod("c2", "/incidents"), null),
    get(api("/api/events/stats?" + q({ group, since, tz })), null), get(api("/api/events/stats?" + q({ group: "type", since, top: 8 })), null),
    get(api("/api/events/stats?" + q({ group: "camera_id", since, top: 8 })), null), get(api("/api/events/stats?" + q({ group: "type", since: today.toISOString(), top: 100 })), null),
    get(mod("assistant", "/reports"), []),
  ]);
  cameras = cams.length ? cams : cameras;
  dbLast = { cams, incs };
  dbBusy = false;

  // ---- KPI tiles
  const online = cams.filter((c) => c.enabled && c.status?.state === "online").length, enabled = cams.filter((c) => c.enabled).length;
  const open = (incs || []).filter((i) => i.status !== "closed"), crit = open.filter((i) => ["critical", "high"].includes(i.severity)).length;
  const recMod = mods.find((m) => m.name === "vms-recorder"), recording = recMod?.heartbeat?.status?.recording ?? cams.filter((c) => c.record && c.enabled).length;
  const free = Object.values(recStats || {}).filter((s) => s.mounted && s.free_gb != null).map((s) => s.free_gb);
  const doors = devs.filter((d) => d.kind === "door"), dstate = (d) => String(d.state?.state || "").toLowerCase();
  const forced = doors.filter((d) => /forced|alarm|tamper/.test(dstate(d))).length, unlocked = doors.filter((d) => !/forced|alarm|tamper/.test(dstate(d)) && /unlock|open|held/.test(dstate(d))).length;
  const sysOk = systems.filter((s) => ["connected", "manual"].includes(s.status)).length;
  const enabledMods = mods.filter((m) => m.enabled), healthy = enabledMods.filter((m) => m.healthy).length;
  const todayCount = (pat) => (evToday?.rows || []).filter((r) => new RegExp("^" + pat.replace(/\./g, "\\.").replace(/\*/g, ".*") + "$").test(r.key)).reduce((a, r) => a + r.total, 0);
  const ring = (v, max, col) => { const pct = max ? Math.round(100 * v / max) : 0; return `<div class="ring" style="--p:${pct};--c:${col}"><span>${pct}%</span></div>`; };
  const tile = (app, title, big, sub, col, extra = "", bad = false) => `<div class="kpi ${bad ? "bad" : ""}" style="--c:${col}" onclick="show('${app}')">
      <div class="kpit">${title}</div><div class="kpiv">${big}</div><div class="kpis2">${sub}</div>${extra}</div>`;
  $("#dbKpis").innerHTML = [
    tile("cameras", "Cameras online", `${online}<small>/${enabled}</small>`, enabled - online ? `${enabled - online} offline` : "all online", online === enabled ? "#34c77b" : "#ffb340", ring(online, enabled, online === enabled ? "#34c77b" : "#ffb340"), online < enabled),
    tile("incidents", "Open alarms", open.length, incs === null ? "C&C not running" : crit ? `${crit} high / critical` : open.length ? "none high" : "all quiet", open.length ? "#ff4d4f" : "#34c77b", "", crit > 0),
    tile("storage", "Recording", recording, free.length ? `cameras · ${Math.min(...free).toFixed(0)} GB free (lowest)` : "cameras", "#3fa7ff"),
    tile("integrations", "Access control", doors.length ? `${doors.length}<small> doors</small>` : "–", systems.length ? `${forced ? `<b class="err">${forced} forced</b> · ` : ""}${unlocked} open/unlocked · ${sysOk}/${systems.length} systems connected` : "no systems", forced ? "#ff4d4f" : "#c9a227", "", forced > 0 || sysOk < systems.length),
    tile("modules", "System health", `${healthy}<small>/${enabledMods.length}</small>`, healthy === enabledMods.length ? "all apps running" : `${enabledMods.length - healthy} app(s) not healthy`, healthy === enabledMods.length ? "#34c77b" : "#ff4d4f", ring(healthy, enabledMods.length, healthy === enabledMods.length ? "#34c77b" : "#ff4d4f"), healthy < enabledMods.length),
    tile("events", "Today", evToday ? evToday.total : "–", `events · ${todayCount("*.alarm") + todayCount("apollo.door_forced")} alarms · ${todayCount("face.match")} face matches`, "#9b8cff"),
  ].join("");

  // ---- events over time (stacked by severity)
  $("#dbEvTitle").textContent = `Events ${hours <= 24 ? "per hour" : "per day"}`;
  $("#dbEvTotal").textContent = evTime ? `${evTime.total} in ${$("#dbRange").selectedOptions[0].textContent.toLowerCase()}` : "";
  $("#dbEvents").innerHTML = evTime ? stackChart(fillTime(evTime.rows, group, hours)) : `<p class="muted small">No data.</p>`;

  // ---- top lists
  const hbars = (rows, label) => rows.length ? `<div class="hbars">${rows.map((r) => `<div class="hb"><span class="hbl" title="${esc(label(r))}">${esc(label(r))}</span>
      <span class="hbt"><i style="width:${(100 * r.total / rows[0].total).toFixed(1)}%;background:linear-gradient(90deg,${SEVC[topSev(r)]},${SEVC[topSev(r)]}aa)"></i></span><b>${r.total}</b></div>`).join("")}</div>` : `<p class="muted small">Nothing yet.</p>`;
  $("#dbTypes").innerHTML = hbars(evType?.rows || [], (r) => r.key);
  $("#dbCams").innerHTML = hbars((evCam?.rows || []).filter((r) => r.key), (r) => cams.find((c) => c.id === r.key)?.name || `#${r.key}`);

  // ---- storage
  $("#dbStorage").innerHTML = stors.map((s) => {
    const st = recStats?.[s.id] || {}, total = st.total_gb || 0, used = total ? total - (st.free_gb || 0) : 0, rec = st.recordings_gb || 0;
    const pct = total ? (100 * used / total) : 0, low = st.free_gb != null && st.free_gb < (s.min_free_gb || 20);
    return `<div class="stor"><div class="storh"><b>${esc(s.name)}</b><span class="small ${!st.mounted && s.type !== "recorder" ? "err" : "muted"}">${s.type === "recorder" ? "recording server" : st.mounted === false ? "not available" : `${(st.free_gb ?? 0).toFixed(0)} GB free of ${total.toFixed(0)} GB`}</span></div>
      ${total ? `<div class="sbar ${low ? "low" : ""}"><i style="width:${pct.toFixed(1)}%"></i><em style="width:${(100 * rec / total).toFixed(1)}%"></em></div>
      <div class="small muted">${rec.toFixed(1)} GB recordings · ${s.cameras || 0} camera(s) · keeps ${s.retention_days} days${st.oldest ? ` · oldest ${new Date(st.oldest).toLocaleDateString()}` : ""}</div>` : ""}</div>`;
  }).join("") || `<p class="muted small">No storage.</p>`;

  // ---- open alarms
  $("#dbAlarms").innerHTML = open.slice(0, 7).map((i) => `<div class="dbal" onclick="show('incidents')"><span class="sev ${esc(i.severity)}">${esc(i.severity)}</span>
      <span class="grow" dir="auto">${esc(i.title)}</span><span class="small muted">${new Date(i.created).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span></div>`).join("")
    || `<p class="muted small">${incs === null ? "The C&C module is not running." : "No open alarms. 👍"}</p>`;

  // ---- modules
  $("#dbModules").innerHTML = `<div class="modgrid">${mods.filter((m) => m.enabled).map((m) => `<span class="modpill ${m.healthy ? "ok" : "bad"}" title="${esc(m.error || m.container?.state || "")}"><span class="mdot" style="--c:${m.healthy ? "#34c77b" : "#ff4d4f"}"></span>${esc(m.display_name || m.name)}</span>`).join("")}</div>
    ${mods.some((m) => !m.enabled) ? `<div class="small muted" style="margin-top:6px">Off: ${mods.filter((m) => !m.enabled).map((m) => esc(m.display_name || m.name)).join(", ")}</div>` : ""}`;

  // ---- attention
  const att = [];
  cams.filter((c) => c.enabled && c.status?.state !== "online").forEach((c) => att.push(["cameras", "#ffb340", `Camera ${c.name}: ${c.status?.state || "unknown"}${c.status?.detail ? " - " + c.status.detail : ""}`]));
  enabledMods.filter((m) => !m.healthy).forEach((m) => att.push(["modules", "#ff4d4f", `${m.display_name || m.name} is not running properly${m.error ? ": " + m.error : ""}`]));
  systems.filter((s) => !["connected", "manual"].includes(s.status)).forEach((s) => att.push(["integrations", "#ff4d4f", `${s.name}: ${s.status}${s.error ? " - " + s.error : ""}`]));
  doors.filter((d) => /forced|alarm|tamper/.test(dstate(d))).forEach((d) => att.push(["maps", "#ff4d4f", `Door ${d.name}: ${d.state.state}`]));
  stors.forEach((s) => { const st = recStats?.[s.id]; if (st && st.mounted === false && s.type !== "recorder") att.push(["storage", "#ff4d4f", `Storage ${s.name} is not available`]);
    else if (st?.free_gb != null && st.free_gb < (s.min_free_gb || 20)) att.push(["storage", "#ffb340", `Storage ${s.name}: only ${st.free_gb} GB free`]); });
  $("#dbAttention").innerHTML = att.slice(0, 12).map(([tab, col, t]) => `<div class="dbal" onclick="show('${tab}')"><span class="mdot" style="--c:${col}"></span><span class="grow">${esc(t)}</span></div>`).join("")
    + (att.length > 12 ? `<div class="small muted">and ${att.length - 12} more…</div>` : "") || `<p class="ok small">✓ Everything looks fine.</p>`;

  // ---- reports
  $("#dbReports").innerHTML = (reports || []).slice(0, 6).map((r) => `<div class="dbal" onclick="show('assistant');setTimeout(()=>aiShowReport('${r.id}'),300)">📄 <span class="grow" dir="auto">${esc(r.title)}</span><span class="small muted">${new Date(r.created).toLocaleDateString()}</span></div>`).join("")
    || `<p class="muted small">No reports yet. Click “Ask AI for a report”, or print this dashboard.</p>`;

  $("#dbUpdated").textContent = `· updated ${new Date().toLocaleTimeString()}`;
  clearTimeout(dbT);
  dbT = setTimeout(() => { if (!$("#tab-dashboard").classList.contains("hidden")) loadDashboard(); }, 30000);
}
const topSev = (r) => SEVS.slice().reverse().find((s) => r.by_severity?.[s]) || "info";
function fillTime(rows, group, hours) {          // include empty hours/days so the axis is continuous
  const map = Object.fromEntries(rows.map((r) => [r.key.slice(0, group === "hour" ? 13 : 10), r])), out = [];
  const step = group === "hour" ? 3600e3 : 86400e3, n = group === "hour" ? hours : Math.ceil(hours / 24);
  const pad = (x) => String(x).padStart(2, "0");
  for (let i = n - 1; i >= 0; i--) {
    const d = new Date(Date.now() - i * step);
    const key = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` + (group === "hour" ? `T${pad(d.getHours())}` : "");
    out.push({ label: group === "hour" ? `${pad(d.getHours())}:00` : `${pad(d.getDate())}/${pad(d.getMonth() + 1)}`, ...(map[key] || { total: 0, by_severity: {} }) });
  }
  return out;
}
function stackChart(rows) {
  const W = 900, H = 230, L = 40, B = 34, T = 10, max = Math.max(1, ...rows.map((r) => r.total)), bw = (W - L - 8) / rows.length;
  const y = (v) => T + (H - T - B) * (1 - v / max);
  let g = "";
  for (let k = 0; k <= 4; k++) { const v = Math.round(max * k / 4); g += `<line x1="${L}" x2="${W - 4}" y1="${y(v)}" y2="${y(v)}" stroke="#ffffff12"/><text x="${L - 6}" y="${y(v) + 4}" text-anchor="end" font-size="11" fill="#8d98a8">${v}</text>`; }
  rows.forEach((r, i) => {
    let base = 0;
    for (const s of SEVS) {
      const v = r.by_severity?.[s] || 0; if (!v) continue;
      g += `<rect x="${L + i * bw + bw * 0.14}" y="${y(base + v)}" width="${bw * 0.72}" height="${y(base) - y(base + v)}" rx="2" fill="${SEVC[s]}"><title>${esc(r.label)} · ${s}: ${v}</title></rect>`;
      base += v;
    }
    if (rows.length <= 31 || i % Math.ceil(rows.length / 31) === 0) g += `<text x="${L + i * bw + bw / 2}" y="${H - B + 15}" text-anchor="middle" font-size="10" fill="#8d98a8">${esc(r.label)}</text>`;
  });
  const legend = SEVS.map((s) => `<span><i style="background:${SEVC[s]}"></i>${s}</span>`).join("");
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" class="dbchart"><defs></defs>${g}</svg><div class="legend">${legend}</div>`;
}
function dbStat() {
  if (!dbLast) return "";
  const off = dbLast.cams.filter((c) => c.enabled && c.status?.state !== "online").length, open = (dbLast.incs || []).filter((i) => i.status !== "closed").length;
  return `${open} open alarm(s)${off ? ` · ${off} camera(s) offline` : ""}`;
}
let dbEvT = null;
function dbOnEvent(ev) {
  if ($("#tab-dashboard").classList.contains("hidden")) return;
  if (ev.type.startsWith("incident.") || ev.type.startsWith("camera.") || ev.source === "integrations") { clearTimeout(dbEvT); dbEvT = setTimeout(loadDashboard, 1500); }
}
$("#dbRange").onchange = () => loadDashboard();
$("#dbPrint").onclick = () => window.print();
$("#dbAskReport").onclick = () => {
  show("assistant");
  setTimeout(() => {
    $("#aiNew").click();
    $("#aiText").value = `Write a report for the ${$("#dbRange").selectedOptions[0].textContent.toLowerCase()}: system health, cameras offline, alarms by type and severity, door events, busiest cameras, with tables and charts.`;
    $("#aiText").focus();
  }, 400);
};
