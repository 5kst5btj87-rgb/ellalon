// ---------------------------------------------------------------- analytics: find people by appearance
// The analytics module tags each person it tracks (man/woman, hair, hat, glasses, clothes, shoes,
// bag and colours). This tab searches those sightings by camera, time and tags.
let asSel = new Set(), asRows = [], asStatus = null, asT = null;

async function loadAnalytics() {
  $("#asCam").innerHTML = `<option value="">All cameras</option>` + cameras.map((c) => `<option value="${c.id}">${esc(c.name)}${c.analytics_tagging?.enabled ? "" : " (not tagged)"}</option>`).join("");
  $("#asCams").classList.toggle("hidden", me.role !== "admin");
  try { asStatus = await mod("analytics", "/status"); } catch { asStatus = null; }
  await loadTax();
  tagPicker($("#asTags"), asSel, asTagsChanged);
  const tagged = cameras.filter((c) => c.analytics_tagging?.enabled);
  const st = $("#asState");
  if (!asStatus) {
    st.innerHTML = `<span class="err">The analytics app is not running.</span> ${me.role === "admin" ? 'Start “Video Analytics” in the System tab (first time: <code>sudo ellalon build analytics</code>).' : ""}`;
  } else if (!asStatus.tagging?.ready) {
    st.innerHTML = asStatus.tagging?.error && asStatus.tagging.error !== "starting"
      ? `<span class="err">People tagging is not available: ${esc(asStatus.tagging.error)}</span> - rebuild with <code>sudo ellalon build analytics</code> (needs internet once to download the model).`
      : "People tagging is starting…";
  } else if (!tagged.length) {
    st.innerHTML = `No camera is tagging people yet - ${me.role === "admin" ? 'click “Tagging per camera…”' : "ask an administrator"} to switch it on.`;
  } else {
    st.textContent = `· ${tagged.length} camera(s) tagging · ${asStatus.tagging.tagged} people tagged since start${asStatus.tagging.last_ms ? ` · ${asStatus.tagging.last_ms} ms per person` : ""}`;
  }
  asSearch();
}
function asTagsChanged() { if (asMode === "rec") rsRender(); else asSearchSoon(); }
function tagsMatchJs(wanted, have) {     // every group asked for must match; several in one group = any of them
  const groups = {};
  wanted.forEach((t) => (groups[t.split(":")[0]] ||= []).push(t));
  return Object.values(groups).every((g) => g.some((t) => have.includes(t)));
}
function asSearchSoon() { clearTimeout(asT); asT = setTimeout(() => asSearch(), 250); }
function asRange() {
  const w = $("#asWhen").value, now = Date.now() / 1000;
  if (w === "today") { const d = new Date(); d.setHours(0, 0, 0, 0); return [d.getTime() / 1000, null]; }
  if (w === "custom") {
    const f = $("#asFrom").value, t = $("#asTo").value;
    return [f ? new Date(f).getTime() / 1000 : null, t ? new Date(t).getTime() / 1000 : null];
  }
  return [now - +w, null];
}
async function asSearch(more = false) {
  const [since, until] = asRange(), p = new URLSearchParams();
  if ($("#asCam").value) p.append("camera", $("#asCam").value);
  asSel.forEach((t) => p.append("tag", t));
  if (since) p.set("since", since);
  if (until) p.set("until", until);
  if (more && asRows.length) p.set("before", asRows[asRows.length - 1].ts);
  p.set("limit", 120);
  let rows;
  try { rows = await mod("analytics", "/sightings?" + p); }
  catch (err) { asRows = []; $("#asGrid").innerHTML = ""; $("#asCount").textContent = ""; $("#asMore").classList.add("hidden"); return; }
  asRows = more ? [...asRows, ...rows] : rows;
  $("#asCount").textContent = `${asRows.length}${rows.length === 120 ? "+" : ""} ${asRows.length === 1 ? "person" : "people"} found`;
  $("#asMore").classList.toggle("hidden", rows.length < 120);
  const t = (x) => new Date(x * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  $("#asGrid").innerHTML = asRows.map((r, i) => `<div class="card sight" onclick="asOpen(${i})">
      <img src="${r.image}" alt="" loading="lazy"><button class="playbtn" title="Play this moment" onclick="event.stopPropagation();playAt(${r.camera_id},${r.ts},'Person found')">▶</button>
      <div class="sinfo"><b>${esc(r.camera)}</b><span class="muted small">${t(r.ts)}</span>
      <div class="tagcloud">${r.tags.map((x) => `<span class="tag ${asSel.has(x) ? "hit" : ""}">${asSw(x)}${esc(tagLabelOf(x))}</span>`).join("")}</div></div></div>`).join("")
    || `<p class="muted">Nobody matches. ${asSel.size ? "Try fewer tags or a longer time." : ""}</p>`;
}
const asSw = (tag) => { const [g, v] = tag.split(":"); return /_color$/.test(g) ? `<i style="background:${SWATCH[v] || "#777"}"></i>` : ""; };
window.asOpen = (i, list) => {
  const r = (list || asRows)[i]; if (!r) return;
  $("#asViewImg").src = r.image;
  $("#asViewTitle").textContent = `${r.camera} · ${new Date(r.ts * 1000).toLocaleString()}`;
  $("#asViewTags").innerHTML = r.tags.map((x) => {
    const sc = r.scores?.[x.split(":")[0]];
    return `<span class="tag">${asSw(x)}${esc(tagLabelOf(x))}${sc ? ` <small>${Math.round(sc[1] * 100)}%</small>` : ""}</span>`;
  }).join("") || '<span class="muted">No tags (picture too small or unclear)</span>';
  $("#asViewPb").onclick = () => { $("#asView").close(); playAt(r.camera_id, r.ts, "Person found"); };
  $("#asViewSame").onclick = () => { asSel = new Set(r.tags); $("#asView").close(); tagPicker($("#asTags"), asSel, asTagsChanged); asTagsChanged(); };
  $("#asView").showModal();
};
$("#asCam").onchange = () => asSearch();
$("#asWhen").onchange = () => { $("#asCustom").classList.toggle("hidden", $("#asWhen").value !== "custom"); asSearch(); };
$("#asFrom").onchange = $("#asTo").onchange = () => asSearch();
$("#asReload").onclick = () => loadAnalytics();
$("#asMore").onclick = () => asSearch(true);
$("#asClear").onclick = () => { asSel.clear(); tagPicker($("#asTags"), asSel, asTagsChanged); asTagsChanged(); };
setInterval(() => { if (me && !$("#tab-analytics").classList.contains("hidden") && !document.querySelector("dialog[open]") && $("#asWhen").value !== "custom") asSearch(); }, 15000);

// ---- tagging switch per camera
$("#asCams").onclick = () => {
  $("#asCamMsg").textContent = "";
  $("#asCamRows").innerHTML = cameras.map((c) => {
    const t = c.analytics_tagging || {};
    return `<tr data-id="${c.id}"><td>${esc(c.name)}</td>
      <td><input type="checkbox" data-k="enabled" ${t.enabled ? "checked" : ""}></td>
      <td><input type="checkbox" data-k="main_stream" ${t.main_stream !== false ? "checked" : ""}></td>
      <td><input type="number" data-k="min_height" min="40" max="400" value="${t.min_height || 90}" style="width:80px"></td></tr>`;
  }).join("") || `<tr><td colspan=4 class="muted">No cameras yet.</td></tr>`;
  $("#asCamRows").querySelectorAll("tr[data-id]").forEach((tr) => tr.querySelectorAll("input").forEach((inp) => (inp.onchange = () => asCamSave(tr))));
  $("#asCamDialog").showModal();
};
async function asCamSave(tr) {
  const c = cameras.find((x) => x.id === +tr.dataset.id), msg = $("#asCamMsg");
  const val = (k) => tr.querySelector(`[data-k="${k}"]`);
  const tagging = { enabled: val("enabled").checked, main_stream: val("main_stream").checked, min_height: +val("min_height").value || 90 };
  try {
    const out = await api(`/api/cameras/${c.id}/analytics`, { method: "PUT", body: { enabled: c.analytics || tagging.enabled, rules: c.analytics_rules || [], tagging } });
    Object.assign(c, out);
    msg.className = "small ok"; msg.textContent = `${c.name}: saved - ${tagging.enabled ? "tagging starts within a few seconds" : "tagging off"}.`;
  } catch (err) { msg.className = "small err"; msg.textContent = `${c.name}: ${errText(err)}`; }
}
$("#asCamDialog").addEventListener("close", () => loadAnalytics());

// ---- search recordings (works on video recorded before tagging was switched on)
let asMode = "live", rsCur = null, rsTimer = null;
document.querySelectorAll("#tab-analytics .seg button").forEach((b) => (b.onclick = () => asSetMode(b.dataset.m)));
function asSetMode(mode) {
  asMode = mode;
  document.querySelectorAll("#tab-analytics .seg button").forEach((b) => b.classList.toggle("on", b.dataset.m === mode));
  document.querySelectorAll("#tab-analytics .as-live").forEach((e) => e.classList.toggle("hidden", mode !== "live"));
  document.querySelectorAll("#tab-analytics .as-rec").forEach((e) => e.classList.toggle("hidden", mode !== "rec"));
  if (mode === "rec") rsOpen();
}
const localInput = (sec) => { const d = new Date(sec * 1000 - new Date().getTimezoneOffset() * 60000); return d.toISOString().slice(0, 16); };
function rsOpen() {
  const keep = $("#rsCam").value;
  $("#rsCam").innerHTML = cameras.filter((c) => c.record).map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("") || `<option value="">No recorded cameras</option>`;
  if (keep) $("#rsCam").value = keep;
  if (!$("#rsFrom").value) { const now = Date.now() / 1000; $("#rsFrom").value = localInput(now - 3600); $("#rsTo").value = localInput(now); }
  rsRefreshList();
}
$("#rsStart").onclick = async () => {
  const msg = $("#rsMsg"), from = new Date($("#rsFrom").value).getTime() / 1000, to = new Date($("#rsTo").value).getTime() / 1000;
  if (!$("#rsCam").value) { msg.className = "small err"; msg.textContent = "Choose a camera."; return; }
  if (!(from < to)) { msg.className = "small err"; msg.textContent = "“To” must be after “From”."; return; }
  msg.className = "small muted"; msg.textContent = "Starting…";
  try {
    const s = await mod("analytics", "/scans", { method: "POST", body: { camera_id: +$("#rsCam").value, start: from, end: to, tags: [...asSel], mode: $("#rsMode").value } });
    msg.textContent = "Every person is kept: change the tags any time - during or after the search - and the results follow.";
    rsShow(s.id);
  } catch (err) { msg.className = "small err"; msg.textContent = errText(err); }
};
async function rsRefreshList() {
  let list = [];
  try { list = await mod("analytics", "/scans"); } catch { $("#rsList").innerHTML = `<p class="err small">The analytics app is not running.</p>`; return; }
  const t = (x) => new Date(x * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  $("#rsList").innerHTML = list.length ? `<span class="small muted">Searches:</span> ` + list.map((s) => `<button class="chip ${rsCur === s.id ? "on" : ""}" onclick="rsShow('${s.id}')">
      ${esc(s.camera)} ${t(s.start)}–${new Date(s.end * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} ·
      ${s.state === "running" ? Math.round(s.progress * 100) + "%" : s.state === "done" ? s.results + " people" : esc(s.state)}</button>`).join(" ") : `<p class="muted">Choose a camera, a time and the tags on the left, then “Search the recordings”.</p>`;
  if (!rsCur && list.length) rsShow(list[0].id);
}
window.rsShow = async (id) => {
  rsCur = id; clearTimeout(rsTimer);
  let s;
  try { s = await mod("analytics", `/scans/${id}`); } catch (err) { $("#rsHead").innerHTML = `<span class="err">${esc(errText(err))}</span>`; $("#rsGrid").innerHTML = ""; rsCur = null; return; }
  const running = s.state === "running" || s.state === "queued", pct = Math.round(s.progress * 100);
  const t = (x) => new Date(x * 1000).toLocaleString();
  $("#rsHead").innerHTML = `<div class="rshead"><b>${esc(s.camera)}</b> <span class="muted small">${t(s.start)} → ${t(s.end)} · ${s.mode === "fast" ? "fast" : "accurate"}
</span>
    <div class="bar"><i style="width:${pct}%"></i></div>
    <span class="small ${s.state === "error" ? "err" : "muted"}">${s.state === "queued" ? "Waiting for the previous search to finish…" : running ? `${s.phase === "looking closer" ? "Looking closer at a scene with people" : s.phase === "skimming" ? "Skimming for people" : "Searching"}… ${pct}% · ${s.people} people so far${s.mode === "fast" ? ` · ${s.scenes} scene(s) with people` : ""}` : s.state === "done" ? `Done · ${s.people} people found${s.mode === "fast" ? ` in ${s.scenes} scene(s)` : ""}` : s.state === "error" ? "Failed: " + esc(s.error) : "Cancelled"}</span>
    ${running ? `<button class="ghost small" onclick="rsCancel('${s.id}')">Stop</button>` : `<button class="ghost small" onclick="rsDelete('${s.id}')">Remove</button>`}
    <span id="rsCount" class="small"></span></div>`;
  rsScan = s; rsRender();
  if (running && !$("#tab-analytics").classList.contains("hidden") && asMode === "rec") rsTimer = setTimeout(() => { rsShow(id); rsRefreshList(); }, 2000);
  else rsRefreshList();
};
var rsRows = [], rsScan = null;
function rsRender() {
  const s = rsScan; if (!s) return;
  const want = [...asSel], running = s.state === "running" || s.state === "queued";
  const all = s.results.map((r) => ({ ...r, camera: s.camera, camera_id: s.camera_id }));
  rsRows = want.length ? all.filter((r) => (r.views || [r.tags]).some((v) => tagsMatchJs(want, v))) : all;
  $("#rsCount").innerHTML = all.length ? `<b>${rsRows.length}</b> of ${all.length} people ${want.length ? "match the tags on the left" : "(no tags chosen - showing everyone)"}` : "";
  const tt = (x) => new Date(x * 1000).toLocaleTimeString();
  $("#rsGrid").innerHTML = rsRows.map((r, i) => `<div class="card sight" onclick="asOpen(${i}, rsRows)">
      <img src="${r.image}" alt="" loading="lazy"><button class="playbtn" title="Play this moment" onclick="event.stopPropagation();playAt(${r.camera_id},${r.ts},'Person found in recordings')">▶</button>
      <div class="sinfo"><b>${tt(r.ts)}</b><span class="muted small">${new Date(r.ts * 1000).toLocaleDateString()}</span>
      <div class="tagcloud">${r.tags.map((x) => `<span class="tag ${asSel.has(x) ? "hit" : ""}">${asSw(x)}${esc(tagLabelOf(x))}</span>`).join("")}</div></div></div>`).join("")
    || (running ? "" : all.length ? `<p class="muted">Nobody here matches these tags - change the tags on the left (the results update right away).</p>`
      : `<p class="muted">Nobody seen in that time.${s.mode === "fast" ? " Try “Accurate”." : ""}</p>`);
}
window.rsCancel = async (id) => { await mod("analytics", `/scans/${id}/cancel`, { method: "POST" }).catch(() => {}); setTimeout(() => rsShow(id), 600); };
window.rsDelete = async (id) => { await mod("analytics", `/scans/${id}`, { method: "DELETE" }).catch(() => {}); rsCur = null; $("#rsHead").innerHTML = ""; $("#rsGrid").innerHTML = ""; rsRefreshList(); };
