// ---------------------------------------------------------------- AI assistant
// Chat with the assistant module. Proposals (rules, maps, commands) are carried out HERE, in the
// browser, with the signed-in user's own permissions, and only after the user clicks Approve.
let aiDevs = [], aiCid = null, aiChat = null, aiPoll = null, aiFilesUp = [], aiSeen = 0, aiSettings = null;
let aiVoiceLang = (() => { try { return localStorage.getItem("ellalon_ai_lang") || "he-IL"; } catch { return "he-IL"; } })();
let aiSpeakOn = (() => { try { return localStorage.getItem("ellalon_ai_speak") === "1"; } catch { return false; } })();
const amod = (path, opts) => mod("assistant", path, opts);
const isHeb = (t) => /[֐-׿]/.test(t || "");

async function loadAssistant() {
  $("#aiSettingsBtn").classList.toggle("hidden", me.role !== "admin");
  aiLangUi(); aiSpeakUi();
  try { aiSettings = await amod("/settings"); }
  catch { aiNotice(`The AI Assistant app is not running. ${me.role === "admin" ? "Start “AI Assistant” in the System tab (first time on the server: <code>sudo ellalon build assistant</code>)." : ""}`); return; }
  if (!aiSettings.provider) aiNotice(me.role === "admin" ? `The assistant is not set up yet. <a class="link" onclick="aiOpenSettings()">Choose the AI engine…</a>` : "The assistant is not set up yet - ask an administrator.");
  else aiNotice("");
  mod("integrations", "/devices").then((d) => { aiDevs = d; if (aiChat) aiRender(); }).catch(() => {});
  await aiLoadChats();
  aiLoadReports();
}
function aiNotice(html) { $("#aiNotice").innerHTML = html; $("#aiNotice").classList.toggle("hidden", !html); }

async function aiLoadChats() {
  let list = [];
  try { list = await amod("/chats"); } catch {}
  $("#aiChats").innerHTML = list.map((c) => `<button class="mapli ${c.id === aiCid ? "active" : ""}" onclick="aiOpen('${c.id}')"><span dir="auto">${esc(c.title || "New conversation")}</span>
      <span class="aidel" title="Delete" onclick="event.stopPropagation();aiDelChat('${c.id}')">×</span></button>`).join("") || `<p class="muted small">No conversations yet.</p>`;
  if (!aiCid && list.length) aiOpen(list[0].id);
  if (!list.length && !aiCid) aiRender();
}
async function aiLoadReports() {
  let list = [];
  try { list = await amod("/reports"); } catch {}
  $("#aiReports").innerHTML = list.slice(0, 20).map((r) => `<button class="mapli" onclick="aiShowReport('${r.id}')">📄 <span dir="auto">${esc(r.title)}</span></button>`).join("")
    || `<p class="muted small">Ask for a report, e.g. “weekly report of alarms and door events”.</p>`;
}
window.aiOpen = async (id) => {
  aiCid = id; aiSeen = -1;
  await aiRefresh(true);
  aiLoadChats();
};
window.aiDelChat = async (id) => {
  if (!(await ask("Delete this conversation?", { ok: "Delete", danger: true }))) return;
  await amod(`/chats/${id}`, { method: "DELETE" }).catch(() => {});
  if (id === aiCid) { aiCid = null; aiChat = null; }
  aiLoadChats(); aiRender();
};
$("#aiNew").onclick = () => { aiCid = null; aiChat = null; aiRender(); aiLoadChats(); $("#aiText").focus(); };

async function aiRefresh(first = false) {
  if (!aiCid) return;
  try { aiChat = await amod(`/chats/${aiCid}`); } catch { aiChat = null; aiCid = null; aiRender(); return; }
  const n = aiChat.display.length;
  if (first) aiSeen = n;                         // screen actions run only for new answers
  else if (n > aiSeen) { for (const e of aiChat.display.slice(aiSeen)) aiFresh(e); aiSeen = n; }
  aiRender();
  $("#aiBusy").classList.toggle("hidden", !aiChat.busy);
  $("#aiStep").textContent = aiChat.busy ? (aiChat.busy.step || "thinking") + "…" : "";
  $("#aiSend").disabled = !!aiChat.busy;
  clearTimeout(aiPoll);
  if (aiChat.busy) aiPoll = setTimeout(() => aiRefresh(), 1000);
}
function aiFresh(e) {                             // a new answer arrived
  if (e.role !== "assistant") return;
  (e.screen || []).forEach((s, i) => setTimeout(() => aiScreen(s), 300 + i * 400));
  if (aiSpeakOn && e.text && !e.error) aiSay(e.text);
  if (e.reports?.length) aiLoadReports();
}

// ---- rendering
function md(t) {                                  // small, safe markdown: headings, bold, lists, code, tables
  const lines = esc(t || "").split("\n"), out = [];
  let list = null, table = null;
  const inline = (s) => s.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`([^`]+)`/g, "<code>$1</code>");
  const flush = () => { if (list) { out.push(`<${list.t}>${list.items.map((x) => `<li>${inline(x)}</li>`).join("")}</${list.t}>`); list = null; }
    if (table) { out.push(`<table class="mdt">${table.map((r, i) => `<tr>${r.map((c) => i ? `<td>${inline(c)}</td>` : `<th>${inline(c)}</th>`).join("")}</tr>`).join("")}</table>`); table = null; } };
  for (const l of lines) {
    let mm;
    if (/^\s*\|.*\|\s*$/.test(l)) { if (/^\s*\|[\s:|-]+\|\s*$/.test(l)) continue; (table ||= []).push(l.trim().slice(1, -1).split("|").map((c) => c.trim())); continue; }
    if ((mm = l.match(/^\s*[-*•]\s+(.*)/))) { if (!list || list.t !== "ul") { flush(); list = { t: "ul", items: [] }; } list.items.push(mm[1]); continue; }
    if ((mm = l.match(/^\s*\d+[.)]\s+(.*)/))) { if (!list || list.t !== "ol") { flush(); list = { t: "ol", items: [] }; } list.items.push(mm[1]); continue; }
    flush();
    if ((mm = l.match(/^(#{1,4})\s+(.*)/))) out.push(`<h${mm[1].length + 2}>${inline(mm[2])}</h${mm[1].length + 2}>`);
    else if (l.trim()) out.push(`<p>${inline(l)}</p>`);
  }
  flush();
  return out.join("");
}
function aiRender() {
  const box = $("#aiMsgs");
  if (!aiChat || !aiChat.display.length) {
    box.innerHTML = `<div class="aihello"><div class="aiorb">${APPS.find((a) => a.id === "assistant").icon}</div>
      <h2>How can I help?</h2><p class="muted">I can look things up, build rules and maps for your approval, open doors after you approve, show layouts and write reports.</p>
      <div class="aiexamples">${["Is everything OK in the system?", "בנה כלל: אם דלת נפרצת בלילה, פתח התראה קריטית והצג את המצלמה בקיר", "Weekly report of alarms and door events", "Show me the Lobby layout", "Who was seen in a red shirt today?", "Build a map from the attached floor plan"]
        .map((x) => `<button class="chip" dir="auto" onclick="aiExample(this)">${esc(x)}</button>`).join("")}</div></div>`;
    return;
  }
  box.innerHTML = aiChat.display.map((e, i) => e.role === "user"
    ? `<div class="aimsg user" dir="auto">${esc(e.text)}${(e.files || []).map((f) => `<div class="aifile">📎 ${esc(f.name)}</div>`).join("")}</div>`
    : `<div class="aimsg bot ${e.error ? "err" : ""}"><div class="aibody" dir="auto">${md(e.text)}</div>
        ${(e.proposals || []).map((p) => aiCard(p, i)).join("")}
        ${(e.screen || []).map((s) => `<button class="chip" onclick='aiScreen(${esc(JSON.stringify(s))})'>↗ ${esc(aiScreenText(s))}</button>`).join(" ")}
        ${(e.reports || []).map((r) => `<button class="chip on" onclick="aiShowReport('${r.id}')">📄 ${esc(r.title)}</button>`).join(" ")}
        ${e.steps?.length ? `<div class="aisteps small muted">${[...new Set(e.steps)].map((s) => esc(s.replaceAll("_", " "))).join(" · ")}</div>` : ""}
        ${e.text && !e.error ? `<button class="aispk" title="Read aloud" onclick="aiSay(aiChat.display[${i}].text)">🔊</button>` : ""}</div>`).join("");
  box.scrollTop = box.scrollHeight;
}
window.aiExample = (b) => { $("#aiText").value = b.textContent; $("#aiText").focus(); };

// ---- proposal cards
const camN = (id) => cameras.find((c) => c.id === +id)?.name || `camera #${id}`;
const DAYN = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
function ruleText(r) {
  const t = r.trigger || {}, c = r.conditions || {};
  const when = `${(t.events || []).length ? t.events.join(" or ") : "any event"}${t.min_severity && t.min_severity !== "info" ? ` (at least ${t.min_severity})` : ""}`
    + `${t.cameras?.length ? ` · cameras: ${t.cameras.map(camN).join(", ")}` : ""}${t.devices?.length ? ` · ${t.devices.map((d) => aiDevs.find((x) => x.id === d)?.name || d).join(", ")}` : ""}${t.text ? ` · containing “${t.text}”` : ""}`;
  const only = [c.days?.length ? c.days.map((d) => DAYN[d]).join(", ") : "", c.time_from ? `${c.time_from}-${c.time_to}` : "",
    c.count > 1 ? `${c.count} times within ${c.window}s` : "", c.cooldown ? `at most every ${c.cooldown}s` : ""].filter(Boolean).join(" · ");
  const then = (r.actions || []).map((a) => a.do === "alarm" ? `alarm${a.severity && a.severity !== "event" ? ` (${a.severity})` : ""}`
    : a.do === "device" ? `${ACTION_LABEL[a.action] || a.action} ${a.device_name || a.device_id}` : a.do === "wall" ? `show ${a.camera && a.camera !== "event" ? camN(a.camera) : "the camera"} on the video wall`
    : a.do === "notify" ? `pop-up “${a.title || ""}”` : a.do === "event" ? `log event ${a.type || ""}` : a.do === "webhook" ? `webhook ${a.url}` : a.do).join(", ");
  return `<div class="rtext"><div><span class="rk">When</span> ${esc(when)}</div>${only ? `<div><span class="rk">Only if</span> ${esc(only)}</div>` : ""}<div><span class="rk">Then</span> ${esc(then)}</div></div>`;
}
function aiCard(p, i) {
  const done = p.status && p.status !== "pending";
  const head = { rules: `New rule${p.rules?.length > 1 ? "s" : ""} - demo`, map: `New map “${esc(p.name)}”`, commands: "Commands to run" }[p.kind];
  let body = "";
  if (p.kind === "rules") body = p.rules.map((r, k) => `<div class="pr"><b dir="auto">${esc(r.name)}</b>${ruleText(r)}
      <div class="demo">▶ Demo on the last 7 days: would have fired <b>${p.demo?.[k]?.would_fire ?? 0}</b> time(s)${(p.demo?.[k]?.examples || []).map((x) => `<div class="small muted">${new Date(x.time).toLocaleString()} - ${esc(x.title || x.type)}</div>`).join("")}</div></div>`).join("");
  if (p.kind === "map") body = `<div class="pmap"><img src="${p.image}" alt="">${p.items.map((it) => `<span class="pmk t-${it.type}" style="left:${it.x * 100}%;top:${it.y * 100}%" title="${esc(it.label || it.ref || "")}">${it.type === "camera" ? "📷" : it.type === "door" ? "🚪" : it.type === "input" ? "◉" : it.type === "output" ? "⏻" : "T"}</span>`).join("")}</div>
      <div class="small muted">${p.items.length} item(s): ${p.items.map((it) => esc(it.type === "camera" ? camN(it.ref) : it.label || it.ref || it.type)).slice(0, 30).join(", ")}${p.unknown?.length ? ` · ${p.unknown.length} not found in Ellalon (placed as text)` : ""}</div>`;
  if (p.kind === "commands") body = `<ul>${p.commands.map((c) => `<li><b>${esc(ACTION_LABEL[c.action] || c.action)}</b> - ${esc(c.device_name)}</li>`).join("")}</ul>`;
  return `<div class="pcard ${p.kind} ${p.status || "pending"}" id="pc_${p.id}"><div class="phead">${head}<span class="pst">${done ? esc(p.status) : "waiting for your approval"}</span></div>
    ${p.explanation ? `<div class="small" dir="auto">${esc(p.explanation)}</div>` : ""}${body}
    ${p.result ? `<div class="small ${p.status === "failed" ? "err" : "ok"}" dir="auto">${esc(p.result)}</div>` : ""}
    ${done ? "" : `<div class="pbtns"><button onclick="aiApprove('${p.id}')">✓ Approve</button> <button class="ghost" onclick="aiReject('${p.id}')">Reject</button></div>`}</div>`;
}
function aiFind(pid) { for (const e of aiChat?.display || []) for (const p of e.proposals || []) if (p.id === pid) return p; return null; }
window.aiReject = async (pid) => { await amod(`/chats/${aiCid}/proposals/${pid}`, { method: "POST", body: { status: "rejected" } }).catch(() => {}); aiRefresh(); };
window.aiApprove = async (pid) => {
  const p = aiFind(pid); if (!p) return;
  const what = p.kind === "commands" ? p.commands.map((c) => `${ACTION_LABEL[c.action] || c.action} ${c.device_name}`).join(", ") : p.kind === "rules" ? `create ${p.rules.length} rule(s)` : `create the map “${p.name}”`;
  if (!(await ask(`Approve: ${what}?`, { ok: "Approve" }))) return;
  const card = $(`#pc_${pid}`); if (card) card.classList.add("working");
  let status = "approved", result = "";
  try {
    if (p.kind === "rules") {
      const made = [];
      for (const r of p.rules) { const out = await mod("c2", "/rules", { method: "POST", body: r }); made.push(out.name); }
      result = `Created: ${made.join(", ")} (see the Rules tab).`;
    } else if (p.kind === "commands") {
      const res = [];
      for (const c of p.commands) {
        try { await mod("integrations", `/devices/${encodeURIComponent(c.device_id)}/action`, { method: "POST", body: { action: c.action } }); res.push(`✓ ${ACTION_LABEL[c.action] || c.action} ${c.device_name}`); }
        catch (err) { res.push(`✗ ${c.device_name}: ${errText(err)}`); status = "failed"; }
      }
      result = res.join(" · ");
    } else if (p.kind === "map") {
      const map = await api("/api/maps", { method: "POST", body: { name: p.name } });
      const img = await fetch(p.image, { headers: { Authorization: "Bearer " + token } });
      if (img.ok) {
        const blob = await img.blob(), fd = new FormData();
        fd.append("file", new File([blob], "plan." + (blob.type.includes("jpeg") ? "jpg" : "png"), { type: blob.type || "image/png" }));
        await fetch(`/api/maps/${map.id}/image`, { method: "PUT", body: fd, headers: { Authorization: "Bearer " + token } });
      }
      await api(`/api/maps/${map.id}`, { method: "PUT", body: { name: p.name, items: p.items } });
      result = `Map “${p.name}” created - open it in the Maps tab.`;
      try { localStorage.setItem("ellalon_map", map.id); } catch {}
      if (typeof maps !== "undefined") { maps = []; mapCur = null; }
    }
  } catch (err) { status = "failed"; result = errText(err); }
  await amod(`/chats/${aiCid}/proposals/${pid}`, { method: "POST", body: { status, result } }).catch(() => {});
  aiRefresh();
  if (status !== "failed" && p.kind === "map") show("maps");
};

// ---- screen actions (they only show things)
function aiScreenText(s) {
  return { open_tab: `Opened ${APPS.find((a) => a.id === s.tab)?.name || s.tab}`, show_layout: `Layout “${s.layout}”`, show_camera: `Live: ${camN(s.camera_id)}`,
    play_recording: `Recording of ${camN(s.camera_id)}${s.time ? " at " + new Date(s.time * 1000).toLocaleTimeString() : ""}`, open_map: "Map" }[s.what] || s.what;
}
window.aiScreen = async (s) => {
  if (s.what === "open_tab" && s.tab) show(s.tab);
  else if (s.what === "show_layout") {
    if (!wallLayouts.length) await loadLayouts();
    const l = wallLayouts.find((x) => x.name === s.layout) || wallLayouts.find((x) => x.name.toLowerCase() === String(s.layout || "").toLowerCase());
    if (!l) return notify(`There is no layout called “${s.layout}”.`);
    applyWall(l, l.name); show("wall");
  } else if (s.what === "show_camera") {
    const c = cameras.find((x) => x.id === +s.camera_id); if (!c) return;
    $("#aiLiveTitle").textContent = c.name; $("#aiLiveVid").innerHTML = `<iframe src="${tileSrc(c, 1, true)}" allow="autoplay; fullscreen"></iframe>`;
    $("#aiLive").showModal();
  } else if (s.what === "play_recording" && s.camera_id) playAt(s.camera_id, s.time || Date.now() / 1000 - 60, "Asked the assistant");
  else if (s.what === "open_map") { try { localStorage.setItem("ellalon_map", s.map_id); } catch {} if (typeof maps !== "undefined") mapCur = null; show("maps"); }
};
$("#aiLive").addEventListener("close", () => ($("#aiLiveVid").innerHTML = ""));

// ---- sending
async function aiSend() {
  const text = $("#aiText").value.trim();
  if (!text && !aiFilesUp.length) return;
  if (!aiCid) { try { aiCid = (await amod("/chats", { method: "POST" })).id; } catch (err) { return notify(errText(err)); } aiSeen = 0; }
  $("#aiSend").disabled = true;
  try {
    await amod(`/chats/${aiCid}/send`, { method: "POST", body: { text, files: aiFilesUp.map((f) => f.id), tz: Intl.DateTimeFormat().resolvedOptions().timeZone } });
    $("#aiText").value = ""; aiFilesUp = []; aiFilesUi();
    if (aiSeen < 0) aiSeen = 0;
    aiChat = await amod(`/chats/${aiCid}`); aiSeen = aiChat.display.length; aiRender();
    aiRefresh(); aiLoadChats();
  } catch (err) { notify(errText(err)); $("#aiSend").disabled = false; }
}
$("#aiSend").onclick = aiSend;
$("#aiText").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); aiSend(); } });
$("#aiAttach").onclick = () => $("#aiFile").click();
$("#aiFile").onchange = async (e) => {
  for (const f of [...e.target.files].slice(0, 4)) {
    const fd = new FormData(); fd.append("file", f);
    aiFilesUp.push({ id: null, name: f.name, uploading: true }); aiFilesUi();
    try {
      const r = await fetch("/modules/assistant/files", { method: "POST", body: fd, headers: { Authorization: "Bearer " + token } });
      if (!r.ok) { let d = `HTTP ${r.status}`; try { d = (await r.json()).detail || d; } catch {} throw new Error(d); }
      const info = await r.json(); aiFilesUp = aiFilesUp.filter((x) => !(x.uploading && x.name === f.name)); aiFilesUp.push(info);
    } catch (err) { aiFilesUp = aiFilesUp.filter((x) => !(x.uploading && x.name === f.name)); notify(`${f.name}: ${err.message}`); }
    aiFilesUi();
  }
  e.target.value = "";
};
function aiFilesUi() {
  $("#aiFiles").innerHTML = aiFilesUp.map((f, i) => `<span class="chip">${f.uploading ? "⏳" : f.type === "pdf" ? "📄" : "🖼"} ${esc(f.name)}${f.pages?.length > 1 ? ` (${f.pages.length} pages)` : ""}
    ${f.uploading ? "" : `<b onclick="aiFilesUp.splice(${i},1);aiFilesUi()" style="cursor:pointer">×</b>`}</span>`).join("");
}

// ---- reports
let aiRep = null;
window.aiShowReport = async (id) => {
  try { aiRep = await amod(`/reports/${id}`); } catch (err) { return notify(errText(err)); }
  const r = aiRep;
  $("#aiRepBody").innerHTML = `<header><div class="rbrand">ELLALON</div><h1>${esc(r.title)}</h1><div class="muted">${esc(r.period || "")} · ${new Date(r.created).toLocaleString()}</div></header>
    ${md(r.body)}
    ${(r.charts || []).map(chartSvg).join("")}
    ${(r.tables || []).map((t) => `<h3>${esc(t.title || "")}</h3><table class="mdt"><tr>${(t.columns || []).map((c) => `<th>${esc(c)}</th>`).join("")}</tr>${(t.rows || []).map((row) => `<tr>${row.map((c) => `<td>${esc(c)}</td>`).join("")}</tr>`).join("")}</table>`).join("")}`;
  $("#aiReport").showModal();
};
function chartSvg(c) {
  const labels = c.labels || [], series = c.series || [], W = 640, H = 240, L = 44, B = 46, T = 14;
  const max = Math.max(1, ...series.flatMap((s) => s.values || []));
  const pal = ["#3fa7ff", "#ff7a59", "#34c77b", "#a78bfa", "#ffb340"], n = Math.max(1, labels.length), bw = (W - L - 10) / n;
  const y = (v) => T + (H - T - B) * (1 - v / max);
  let g = `<line x1="${L}" y1="${H - B}" x2="${W - 6}" y2="${H - B}" stroke="#8884"/>`;
  for (let k = 0; k <= 4; k++) { const v = Math.round(max * k / 4); g += `<text x="${L - 6}" y="${y(v) + 4}" text-anchor="end" font-size="10" fill="#888">${v}</text><line x1="${L}" x2="${W - 6}" y1="${y(v)}" y2="${y(v)}" stroke="#8882"/>`; }
  series.forEach((s, si) => {
    const col = pal[si % pal.length];
    if (c.type === "line") g += `<polyline fill="none" stroke="${col}" stroke-width="2.5" points="${(s.values || []).map((v, i) => `${L + bw * i + bw / 2},${y(v)}`).join(" ")}"/>`;
    else (s.values || []).forEach((v, i) => { const w = (bw * 0.7) / series.length; g += `<rect x="${L + bw * i + bw * 0.15 + w * si}" y="${y(v)}" width="${w}" height="${H - B - y(v)}" rx="3" fill="${col}"><title>${esc(labels[i])}: ${v}</title></rect>`; });
  });
  labels.forEach((l, i) => { if (n <= 16 || i % Math.ceil(n / 16) === 0) g += `<text x="${L + bw * i + bw / 2}" y="${H - B + 14}" text-anchor="end" font-size="10" fill="#888" transform="rotate(-30 ${L + bw * i + bw / 2} ${H - B + 14})">${esc(String(l).slice(0, 16))}</text>`; });
  const legend = series.length > 1 ? `<div class="small">${series.map((s, si) => `<span style="color:${pal[si % pal.length]}">■</span> ${esc(s.name)}`).join(" &nbsp; ")}</div>` : "";
  return `<figure class="rchart"><figcaption>${esc(c.title || "")}</figcaption><svg viewBox="0 0 ${W} ${H}" width="100%">${g}</svg>${legend}</figure>`;
}
$("#aiRepPrint").onclick = () => {
  const w = window.open("", "_blank");
  if (!w) return notify("Allow pop-ups for this page to print the report.");
  w.document.write(`<!doctype html><html><head><meta charset="utf-8"><title>${esc(aiRep.title)}</title><style>
    body{font-family:system-ui,sans-serif;color:#111;margin:32px}h1{margin:4px 0}.rbrand{letter-spacing:.3em;font-size:12px;color:#666}.muted{color:#666}
    table{border-collapse:collapse;width:100%;margin:8px 0 18px}th,td{border:1px solid #ccc;padding:5px 8px;text-align:start}th{background:#f2f2f2}
    figure{margin:14px 0}figcaption{font-weight:600}</style></head><body dir="auto">${$("#aiRepBody").innerHTML}</body></html>`);
  w.document.close(); setTimeout(() => w.print(), 300);
};
$("#aiRepDel").onclick = async () => {
  if (!(await ask("Delete this report?", { ok: "Delete", danger: true }))) return;
  await amod(`/reports/${aiRep.id}`, { method: "DELETE" }).catch(() => {}); $("#aiReport").close(); aiLoadReports();
};
$("#aiRepClose").onclick = () => $("#aiReport").close();

// ---- voice (browser speech recognition; needs HTTPS)
let aiRec = null;
function aiLangUi() { $("#aiLang").textContent = aiVoiceLang === "he-IL" ? "עב" : "EN"; }
function aiSpeakUi() { $("#aiSpeak").textContent = aiSpeakOn ? "🔊" : "🔇"; }
$("#aiLang").onclick = () => { aiVoiceLang = aiVoiceLang === "he-IL" ? "en-US" : "he-IL"; try { localStorage.setItem("ellalon_ai_lang", aiVoiceLang); } catch {} aiLangUi(); };
$("#aiSpeak").onclick = () => { aiSpeakOn = !aiSpeakOn; try { localStorage.setItem("ellalon_ai_speak", aiSpeakOn ? "1" : "0"); } catch {} aiSpeakUi(); if (!aiSpeakOn) speechSynthesis?.cancel(); };
$("#aiMic").onclick = () => {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!window.isSecureContext) return notify(`Voice needs the secure address. Open https://${location.hostname}/ (accept the certificate warning once), then try again.`);
  if (!SR) return notify("This browser has no speech recognition - use Chrome or Edge.");
  if (aiRec) { aiRec.stop(); return; }
  aiRec = new SR(); aiRec.lang = aiVoiceLang; aiRec.interimResults = true; aiRec.continuous = false;
  const before = $("#aiText").value;
  aiRec.onresult = (e) => { $("#aiText").value = (before ? before + " " : "") + [...e.results].map((r) => r[0].transcript).join(""); };
  aiRec.onerror = (e) => { if (e.error === "not-allowed") notify("The microphone is blocked for this page - allow it in the browser's address bar."); };
  aiRec.onend = () => { $("#aiMic").classList.remove("rec"); aiRec = null; if ($("#aiText").value.trim() && $("#aiText").value !== before) aiSend(); };
  $("#aiMic").classList.add("rec"); aiRec.start();
};
window.aiSay = (text) => {
  if (!window.speechSynthesis) return;
  speechSynthesis.cancel();
  const clean = String(text).replace(/[#*`|>_-]+/g, " ").replace(/\s+/g, " ").slice(0, 1500);
  const u = new SpeechSynthesisUtterance(clean), lang = isHeb(clean) ? "he" : "en";
  u.lang = lang === "he" ? "he-IL" : "en-US";
  const v = speechSynthesis.getVoices().find((x) => x.lang?.toLowerCase().startsWith(lang)); if (v) u.voice = v;
  speechSynthesis.speak(u);
};

// ---- settings (admins)
let aiOlT = null;
window.aiOpenSettings = async () => {
  const f = $("#aiSetForm"), s = aiSettings || (await amod("/settings").catch(() => ({})));
  f.querySelectorAll('input[name="provider"]').forEach((r) => (r.checked = r.value === (s.provider || "")));
  el(f, "anthropic_key").value = ""; el(f, "anthropic_key").placeholder = s.has_anthropic_key ? "(saved - leave empty to keep)" : "sk-ant-…";
  el(f, "anthropic_model").value = s.anthropic_model || ""; el(f, "ollama_url").value = s.ollama_url || ""; el(f, "ollama_model").value = s.ollama_model || ""; el(f, "ollama_ctx").value = s.ollama_ctx || 16384;
  $("#aiSetMsg").textContent = ""; aiProvUi(); $("#aiSetDialog").showModal();
};
$("#aiSettingsBtn").onclick = aiOpenSettings;
function aiProvUi() {
  const p = $("#aiSetForm").querySelector('input[name="provider"]:checked')?.value || "";
  document.querySelectorAll(".aiprov").forEach((d) => d.classList.toggle("hidden", d.dataset.p !== p));
  if (p === "ollama") aiOlStatus();
}
$("#aiSetForm").querySelectorAll('input[name="provider"]').forEach((r) => (r.onchange = aiProvUi));
$("#aiSetDialog").addEventListener("close", () => clearTimeout(aiOlT));
async function aiOlStatus() {
  clearTimeout(aiOlT);
  const box = $("#aiOlStatus");
  let st = null, modInfo = null;
  try { st = await amod("/ollama/models"); } catch {}
  if (st?.builtin) modInfo = await api("/api/modules/ollama").catch(() => null);
  if (!st) box.innerHTML = `<span class="err">The AI Assistant app is not answering.</span>`;
  else if (st.reachable) box.innerHTML = `<span class="ok">● ${st.builtin ? "Local AI app running" : "Ollama reachable at " + esc(st.url)}</span> · installed models: ${st.models.length ? st.models.map((m) => `<b>${esc(m.name)}</b> (${m.gb} GB)`).join(", ") : "<b>none yet - download one below</b>"}`;
  else if (st.builtin) box.innerHTML = modInfo?.enabled
    ? `<span class="muted">● Local AI app is starting… (the first start downloads the Ollama program, about 1-3 GB - can take several minutes)</span>`
    : `<span class="err">● The Local AI app is off.</span> <button type="button" class="small" onclick="aiOlStart(this)">Start Local AI</button>`;
  else box.innerHTML = `<span class="err">● Cannot reach Ollama at ${esc(st.url)}.</span> If you did not install Ollama there yourself:
      <button type="button" class="small" onclick="aiUseBuiltin()">Use Ellalon's own Local AI instead</button>`;
  $("#aiOlModels").innerHTML = (st?.models || []).map((m) => `<option value="${esc(m.name)}">`).join("") + ["qwen2.5vl:7b", "qwen2.5vl:3b", "qwen3:8b"].map((x) => `<option value="${x}">`).join("");
  const p = st?.pull || {}, bar = $("#aiOlPullBar");
  bar.classList.toggle("hidden", !p.model);
  if (p.model) {
    bar.querySelector("i").style.width = (p.pct || 0) + "%";
    bar.querySelector("span").innerHTML = p.error ? `<span class="err">${esc(p.model)}: ${esc(p.error)}</span>` : `${esc(p.model)}: ${esc(p.status)} ${p.running ? (p.pct || 0) + "%" : ""}`;
  }
  $("#aiOlPull").disabled = !!p.running || !st?.reachable;
  if ($("#aiSetDialog").open && (p.running || (st && !st.reachable && modInfo?.enabled))) aiOlT = setTimeout(aiOlStatus, 3000);
}
window.aiUseBuiltin = async () => {
  el($("#aiSetForm"), "ollama_url").value = "";
  try { await aiSaveSettings(); } catch (err) { return notify(errText(err)); }
  $("#aiSetMsg").textContent = "";
  aiOlStatus();
};
window.aiOlStart = async (b) => {
  b.disabled = true;
  try { await api("/api/modules/ollama/enable", { method: "POST" }); }
  catch (err) { notify(/not found|pull/i.test(errText(err)) ? "Could not download the Ollama program - the server needs internet access for this." : errText(err)); }
  setTimeout(aiOlStatus, 2000);
};
$("#aiOlPull").onclick = async () => {
  const model = el($("#aiSetForm"), "ollama_model").value.trim() || "qwen2.5vl:7b";
  try { await aiSaveSettings(); await amod("/ollama/pull", { method: "POST", body: { model } }); } catch (err) { return notify(errText(err)); }
  setTimeout(aiOlStatus, 800);
};
async function aiSaveSettings() {
  const f = $("#aiSetForm");
  const body = { provider: f.querySelector('input[name="provider"]:checked')?.value || "", anthropic_key: el(f, "anthropic_key").value.trim() || null,
    anthropic_model: el(f, "anthropic_model").value.trim() || "claude-sonnet-5-5", ollama_url: el(f, "ollama_url").value.trim(),
    ollama_model: el(f, "ollama_model").value.trim() || "qwen2.5vl:7b", ollama_ctx: +el(f, "ollama_ctx").value || 16384 };
  aiSettings = await amod("/settings", { method: "PUT", body });
}
$("#aiSetSave").onclick = async () => {
  const msg = $("#aiSetMsg");
  try { await aiSaveSettings(); msg.className = "small ok"; msg.textContent = "Saved."; loadAssistant(); } catch (err) { msg.className = "small err"; msg.textContent = errText(err); }
};
$("#aiSetTest").onclick = async () => {
  const msg = $("#aiSetMsg"); msg.className = "small muted"; msg.textContent = "Saving and asking the model… (a local model's first answer can take a minute)";
  try { await aiSaveSettings(); const r = await amod("/settings/test", { method: "POST" }); msg.className = "small ok"; msg.textContent = `✓ Works (${r.seconds}s): ${r.answer}`; loadAssistant(); }
  catch (err) { msg.className = "small err"; msg.textContent = errText(err); }
};
