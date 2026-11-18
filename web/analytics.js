// ---------------------------------------------------------------- analytics: find people by appearance
// The analytics module tags each person it tracks (man/woman, hair, hat, glasses, clothes, shoes,
// bag and colours). This tab searches those sightings by camera, time and tags.
let asSel = new Set(), asRows = [], asStatus = null, asT = null;

async function loadAnalytics() {
  $("#asCam").innerHTML = `<option value="">All cameras</option>` + cameras.map((c) => `<option value="${c.id}">${esc(c.name)}${c.analytics_tagging?.enabled ? "" : " (not tagged)"}</option>`).join("");
  $("#asCams").classList.toggle("hidden", me.role !== "admin");
  try { asStatus = await mod("analytics", "/status"); } catch { asStatus = null; }
  await loadTax();
  tagPicker($("#asTags"), asSel, asSearchSoon);
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
      <img src="${r.image}" alt="" loading="lazy">
      <div class="sinfo"><b>${esc(r.camera)}</b><span class="muted small">${t(r.ts)}</span>
      <div class="tagcloud">${r.tags.map((x) => `<span class="tag ${asSel.has(x) ? "hit" : ""}">${asSw(x)}${esc(tagLabelOf(x))}</span>`).join("")}</div></div></div>`).join("")
    || `<p class="muted">Nobody matches. ${asSel.size ? "Try fewer tags or a longer time." : ""}</p>`;
}
const asSw = (tag) => { const [g, v] = tag.split(":"); return /_color$/.test(g) ? `<i style="background:${SWATCH[v] || "#777"}"></i>` : ""; };
window.asOpen = (i) => {
  const r = asRows[i]; if (!r) return;
  $("#asViewImg").src = r.image;
  $("#asViewTitle").textContent = `${r.camera} · ${new Date(r.ts * 1000).toLocaleString()}`;
  $("#asViewTags").innerHTML = r.tags.map((x) => {
    const sc = r.scores?.[x.split(":")[0]];
    return `<span class="tag">${asSw(x)}${esc(tagLabelOf(x))}${sc ? ` <small>${Math.round(sc[1] * 100)}%</small>` : ""}</span>`;
  }).join("") || '<span class="muted">No tags (picture too small or unclear)</span>';
  $("#asViewPb").onclick = () => {
    $("#asView").close(); show("playback");
    setTimeout(() => {
      const d = new Date(r.ts * 1000), day = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
      if ([...$("#pbCam").options].some((o) => o.value === String(r.camera_id))) $("#pbCam").value = String(r.camera_id);
      $("#pbDay").value = day; $("#pbLoad").click();
    }, 80);
  };
  $("#asViewSame").onclick = () => { asSel = new Set(r.tags); $("#asView").close(); tagPicker($("#asTags"), asSel, asSearchSoon); asSearch(); };
  $("#asView").showModal();
};
$("#asCam").onchange = () => asSearch();
$("#asWhen").onchange = () => { $("#asCustom").classList.toggle("hidden", $("#asWhen").value !== "custom"); asSearch(); };
$("#asFrom").onchange = $("#asTo").onchange = () => asSearch();
$("#asReload").onclick = () => loadAnalytics();
$("#asMore").onclick = () => asSearch(true);
$("#asClear").onclick = () => { asSel.clear(); tagPicker($("#asTags"), asSel, asSearchSoon); asSearch(); };
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
