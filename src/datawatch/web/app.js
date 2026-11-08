// Data Watch UI: plain ES module, no build step. Hash routes:
// #/?day=N health overview, #/incidents, #/incident/<id>, #/table/<name>, #/evaluation
const app = document.getElementById("app");
const tip = document.getElementById("tip");

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const get = (url) => fetch(url).then((r) => { if (!r.ok) throw new Error(`${url}: ${r.status}`); return r.json(); });
const post = (url, body) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
  .then((r) => { if (!r.ok) throw new Error(`${url}: ${r.status}`); return r.json(); });
const pct = (v, d = 0) => (v == null ? "n/a" : `${(v * 100).toFixed(d)}%`);
const hrs = (v) => (v == null ? "missed" : `${v.toFixed(1)} h`);
const num = (v) => (v == null ? "n/a" : Math.abs(v) >= 1000 ? Math.round(v).toLocaleString() : Math.abs(v) >= 10 ? v.toFixed(1) : Number(v.toPrecision(3)).toString());
const STATUS_CHIP = { open: "bad", acknowledged: "warn", snoozed: "warn", resolved: "good", expected: "acc", recovered: "good" };
const LAYERS = [["ingestion", "Ingestion"], ["staging", "Staging"], ["marts", "Marts"]];
let summary = null;

function dateOf(day) {
  const d = new Date(`${summary.start_date}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + day);
  return d.toISOString().slice(0, 10);
}

function bindTips(root) {
  root.querySelectorAll("[data-tip]").forEach((el) => {
    el.addEventListener("mousemove", (ev) => {
      tip.hidden = false;
      tip.innerHTML = el.dataset.tip;
      const x = Math.min(ev.clientX + 12, window.innerWidth - tip.offsetWidth - 8);
      tip.style.left = `${Math.max(4, x)}px`;
      tip.style.top = `${ev.clientY + 12}px`;
    });
    el.addEventListener("mouseleave", () => { tip.hidden = true; });
  });
}

// ------------------------------------------------------------------ health overview
async function overview(params) {
  const lastIncidentDay = [...summary.timeline].reverse().find((t) => t.incidents.length)?.day ?? summary.last_day;
  const day = params.has("day") ? Number(params.get("day")) : lastIncidentDay;
  const [t, inc] = await Promise.all([get(`/api/tables?day=${day}`), get("/api/incidents")]);
  const counts = { healthy: 0, warning: 0, critical: 0, learning: 0 };
  t.tables.forEach((r) => { counts[r.status] += 1; });
  const open = inc.incidents.filter((i) => i.status === "open").length;
  const maxA = Math.max(...summary.timeline.map((x) => x.alerts));
  const cells = summary.timeline.map((x) => {
    const cls = x.alerts === 0 ? "" : x.alerts >= Math.max(4, maxA / 3) ? "a2" : "a1";
    return `<button class="${cls} ${x.day === day ? "on" : ""}" data-day="${x.day}" aria-label="day ${x.day}"
      data-tip="${esc(x.date)}: ${x.alerts} alerts${x.incidents.length ? `, opened ${x.incidents.length} incident(s)` : ""}"></button>`;
  }).join("");
  const today = inc.incidents.filter((i) => i.first_day <= day && day <= i.last_day);
  const grid = LAYERS.map(([layer, label]) => {
    const tiles = t.tables.filter((r) => r.layer === layer).map((r) => `
      <a class="tile ${r.status}" href="#/table/${r.name}" data-tip="${esc(r.owner)}${r.critical ? ", critical mart" : ""}">
        <div class="n">${esc(r.name)}</div>
        <div class="m">${r.status === "learning" ? "learning baseline" : r.alerts.length ? esc(r.alerts.join(", ")) : "healthy"}
          ${r.rows != null ? ` · ${Math.round(r.rows).toLocaleString()} rows` : ""}</div>
      </a>`).join("");
    return `<div class="layer-head"><h2 style="margin:0">${label}</h2><span class="muted">${t.tables.filter((r) => r.layer === layer).length} tables</span></div><div class="grid">${tiles}</div>`;
  }).join("");
  app.innerHTML = `
    <section class="panel c12">
      <div class="kpis">
        <div class="kpi"><div class="v">${summary.tables}</div><div class="l">tables monitored</div></div>
        <div class="kpi"><div class="v">${summary.days}</div><div class="l">simulated days</div></div>
        <div class="kpi"><div class="v">${summary.alerts}</div><div class="l">alerts raised</div></div>
        <div class="kpi"><div class="v">${summary.incidents}</div><div class="l">incidents after grouping</div></div>
        <div class="kpi"><div class="v">${open}</div><div class="l">open incidents</div></div>
      </div>
    </section>
    <section class="panel c12">
      <h2>Alerts per day, click a day to see the warehouse that morning</h2>
      <div class="timeline">${cells}</div>
      <div class="tl-axis"><span>${esc(dateOf(0))}</span><span>${esc(dateOf(summary.last_day))}</span></div>
    </section>
    <section class="panel c8">
      <div class="row" style="justify-content:space-between;margin-bottom:10px">
        <h2 style="margin:0">Table health on ${esc(t.date)} (day ${t.day})</h2>
        <div class="legend"><span><i style="background:var(--good)"></i>healthy ${counts.healthy}</span>
          <span><i style="background:var(--warn)"></i>warning ${counts.warning}</span>
          <span><i style="background:var(--bad)"></i>critical ${counts.critical}</span>
          <span><i style="background:var(--learning)"></i>learning ${counts.learning}</span></div>
      </div>
      ${grid}
    </section>
    <section class="panel c4">
      <h2>Incidents active on day ${t.day}</h2>
      ${today.length ? today.map(incCard).join("") : `<p class="muted">No incidents on this day. Days with orange or red bars on the timeline have alerts.</p>`}
    </section>`;
  app.querySelectorAll(".timeline button").forEach((b) => b.addEventListener("click", () => { location.hash = `#/?day=${b.dataset.day}`; }));
  bindTips(app);
}

function incCard(i) {
  return `<div style="border-top:1px solid var(--border);padding:8px 0">
    <a href="#/incident/${encodeURIComponent(i.id)}"><b>${esc(i.title)}</b></a>
    <div class="row" style="margin-top:4px"><span class="chip ${STATUS_CHIP[i.status] || ""}">${esc(i.status)}</span>
      <span class="chip ${i.routing?.action === "page" ? "bad" : ""}">${esc(i.routing?.action || "")} ${esc(i.routing?.target || "")}</span>
      <span class="muted">${i.alert_count} alerts, days ${i.first_day} to ${i.last_day}</span></div></div>`;
}

// ------------------------------------------------------------------ incidents
async function incidents() {
  const d = await get("/api/incidents");
  const rows = d.incidents.map((i) => `<tr>
    <td><a href="#/incident/${encodeURIComponent(i.id)}">${esc(i.id)}</a></td>
    <td class="wrap">${esc(i.title)}</td>
    <td><code>${esc(i.root_table)}</code></td>
    <td class="num">${i.first_day} to ${i.last_day}</td>
    <td class="num">${i.alert_count}</td>
    <td><span class="chip ${i.routing?.action === "page" ? "bad" : ""}">${esc(i.routing?.action || "")}</span> ${esc(i.routing?.target || "")}</td>
    <td><span class="chip ${STATUS_CHIP[i.status] || ""}">${esc(i.status)}</span></td></tr>`).join("");
  app.innerHTML = `<section class="panel c12"><h2>Incidents (${d.incidents.length})</h2>
    <p class="muted">Alerts on tables connected by lineage on the same day become one incident. Later alerts within ${2} days join it instead of paging again.</p>
    <div class="scroll"><table><thead><tr><th>ID</th><th>Incident</th><th>Root cause</th><th class="num">Days</th><th class="num">Alerts</th><th>Routing</th><th>Status</th></tr></thead>
    <tbody>${rows}</tbody></table></div></section>`;
}

function lineageSvg(g) {
  const W = 176, H = 34, GX = 54, GY = 14, P = 8;
  const width = g.columns * (W + GX) - GX + 2 * P;
  const height = g.rows * (H + GY) - GY + 2 * P;
  const pos = {};
  g.nodes.forEach((n) => { pos[n.id] = { x: P + n.col * (W + GX), y: P + n.row * (H + GY), n }; });
  const hot = new Set(g.nodes.filter((n) => n.role !== "context").map((n) => n.id));
  const edges = g.edges.map((e) => {
    const a = pos[e.from], b = pos[e.to];
    const x1 = a.x + W, y1 = a.y + H / 2, x2 = b.x, y2 = b.y + H / 2, mx = (x1 + x2) / 2;
    return `<path class="edge ${hot.has(e.from) && hot.has(e.to) ? "hot" : ""}" d="M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}"/>`;
  }).join("");
  const nodes = g.nodes.map((n) => {
    const p = pos[n.id];
    const tag = n.role === "root" ? "ROOT CAUSE" : n.role === "affected" ? "alerting" : n.layer;
    return `<a href="#/table/${n.id}"><g class="node ${n.role}" transform="translate(${p.x},${p.y})">
      <rect width="${W}" height="${H}" rx="6"/><text x="8" y="15">${esc(n.id)}${n.critical ? " *" : ""}</text>
      <text class="tag" x="8" y="28">${esc(tag)}</text></g></a>`;
  }).join("");
  return `<svg viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" role="img" aria-label="lineage graph">${edges}${nodes}</svg>`;
}

async function incident(id) {
  const i = await get(`/api/incidents/${encodeURIComponent(id)}`);
  const alerts = i.alerts.map((a) => a.findings.map((f, k) => `<tr>
      ${k === 0 ? `<td rowspan="${a.findings.length}" class="num">${a.day}</td><td rowspan="${a.findings.length}"><a href="#/table/${a.table}">${esc(a.table)}</a></td>
      <td rowspan="${a.findings.length}"><span class="chip ${a.severity === "high" ? "bad" : "warn"}">${esc(a.monitor)}</span></td>` : ""}
      <td class="wrap">${esc(f.message)}</td></tr>`).join("")).join("");
  const done = i.status === "expected";
  const notes = (i.notes || []).map((n) => `<li>${esc(n.user)}: ${esc(n.action)}${n.note ? `, "${esc(n.note)}"` : ""}</li>`).join("");
  const r = i.routing || {};
  app.innerHTML = `
    <section class="panel c12">
      <div class="row" style="justify-content:space-between">
        <div style="min-width:0"><h3>${esc(i.title)}</h3>
          <div class="row"><span class="chip ${STATUS_CHIP[i.status] || ""}">${esc(i.status)}</span>
          <span class="chip ${i.severity === "high" ? "bad" : "warn"}">${esc(i.severity)} severity</span>
          <span class="muted">${esc(i.id)}, opened ${esc(i.first_date)} (day ${i.first_day}), last alert day ${i.last_day}</span></div></div>
        <div class="row">
          <button class="act" data-a="acknowledge" ${done ? "disabled" : ""}>Acknowledge</button>
          <button class="act" data-a="snooze" ${done ? "disabled" : ""}>Snooze 3 days</button>
          <button class="act" data-a="resolve" ${done ? "disabled" : ""}>Resolve</button>
          <button class="act primary" data-a="expected" ${done ? "disabled" : ""} data-tip="Accept these values as the new normal. The baselines for the affected metrics are refit and the alerts go away.">Mark as expected</button>
        </div>
      </div>
    </section>
    <section class="panel c12">
      <h2>Lineage: root cause and affected tables</h2>
      <div class="legend" style="margin-bottom:8px"><span><i style="background:var(--bad)"></i>root cause candidate</span>
        <span><i style="background:var(--warn)"></i>alerting downstream</span><span><i style="background:var(--edge)"></i>context</span><span>* critical mart</span></div>
      <div class="scroll">${lineageSvg(i.lineage)}</div>
      <p class="note">${esc(i.root_reason)}.${i.root_candidates.length > 1 ? ` Other candidates: ${i.root_candidates.slice(1).map(esc).join(", ")}.` : ""}</p>
    </section>
    <section class="panel c8"><h2>Grouped alerts (${i.alert_count})</h2>
      <div class="scroll"><table><thead><tr><th class="num">Day</th><th>Table</th><th>Monitor</th><th>Finding</th></tr></thead><tbody>${alerts}</tbody></table></div></section>
    <section class="panel c4"><h2>Routing</h2>
      <dl class="kv"><dt>Rule</dt><dd>${esc(r.rule)}</dd><dt>Action</dt><dd><span class="chip ${r.action === "page" ? "bad" : ""}">${esc(r.action)}</span> ${esc(r.priority)}</dd>
      <dt>Assignee</dt><dd>${esc(r.assignee)}</dd><dt>Sent to</dt><dd><code>${esc(r.target)}</code></dd>
      <dt>Also notified</dt><dd>${esc((r.notify || []).join(", ") || "none")}</dd><dt>Updates</dt><dd><code>${esc(r.slack)}</code></dd>
      <dt>Critical marts</dt><dd>${esc((r.critical_tables || []).join(", ") || "none")}</dd></dl>
      <h2 style="margin-top:14px">Activity</h2>${notes ? `<ul style="margin:0;padding-left:18px">${notes}</ul>` : `<p class="muted">No actions yet.</p>`}
    </section>`;
  app.querySelectorAll("button[data-a]").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true;
    const out = await post(`/api/incidents/${encodeURIComponent(id)}/actions`, { action: b.dataset.a, user: "web" });
    summary = await get("/api/summary");
    if (out.id) incident(out.id);
  }));
  bindTips(app);
}

// ------------------------------------------------------------------ table detail
function bandChart(points, { label, unit, height = 220 }) {
  const pts = points.filter((p) => p.value != null);
  if (!pts.length) return `<p class="muted">No data.</p>`;
  const W = 760, H = height, L = 56, R = 12, T = 10, B = 26;
  const vals = pts.flatMap((p) => [p.value, p.lower, p.upper]).filter((v) => v != null && Number.isFinite(v));
  let lo = Math.min(0, ...vals), hi = Math.max(...vals);
  if (hi === lo) hi = lo + 1;
  hi += (hi - lo) * 0.05;
  const n = points.length;
  const x = (d) => L + (d / Math.max(1, n - 1)) * (W - L - R);
  const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
  const banded = pts.filter((p) => p.lower != null || p.upper != null);
  let band = "";
  if (banded.length) {
    const up = banded.map((p) => `${x(p.day)},${y(Math.min(hi, p.upper ?? hi))}`);
    const dn = banded.slice().reverse().map((p) => `${x(p.day)},${y(Math.max(lo, p.lower ?? lo))}`);
    band = `<polygon points="${up.join(" ")} ${dn.join(" ")}" fill="var(--band)"/>`;
  }
  const line = pts.map((p, k) => `${k ? "L" : "M"}${x(p.day)},${y(p.value)}`).join(" ");
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => lo + f * (hi - lo));
  const grid = ticks.map((v) => `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/>`).join("");
  const yl = ticks.map((v) => `<text x="${L - 6}" y="${y(v) + 4}" text-anchor="end">${num(v)}</text>`).join("");
  const xl = [0, 30, 60, 90, n - 1].map((d) => `<text x="${x(d)}" y="${H - 6}" text-anchor="middle">${esc(dateOf(d).slice(5))}</text>`).join("");
  const dots = pts.filter((p) => p.status === "alert" || p.status === "expected").map((p) =>
    `<circle cx="${x(p.day)}" cy="${y(p.value)}" r="4.5" fill="${p.status === "alert" ? "var(--bad)" : "var(--accent)"}"
      data-tip="day ${p.day} (${esc(dateOf(p.day))}): ${num(p.value)} ${unit}, expected ${num(p.lower)} to ${num(p.upper)}"/>`).join("");
  const hover = pts.map((p) => `<rect x="${x(p.day) - 3}" y="${T}" width="6" height="${H - T - B}" fill="transparent"
      data-tip="day ${p.day} (${esc(dateOf(p.day))}): ${num(p.value)} ${unit}${p.upper != null || p.lower != null ? `<br>band ${num(p.lower)} to ${num(p.upper)}` : ""}<br>${p.status}"/>`).join("");
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto" role="img" aria-label="${esc(label)}">
    <g class="grid">${grid}</g><g class="axis">${yl}${xl}</g>${band}
    <path d="${line}" fill="none" stroke="var(--accent)" stroke-width="1.6"/>${hover}${dots}</svg>`;
}

async function table(name) {
  const t = await get(`/api/tables/${encodeURIComponent(name)}`);
  const mons = t.monitors.map((m) => `<tr><td><span class="chip">${esc(m.monitor)}</span></td><td><code>${esc(m.metric)}</code></td>
    <td class="num">${num(m.last)}</td><td class="num">${m.alerts ? `<span class="chip bad">${m.alerts}</span>` : "0"}</td></tr>`).join("");
  const alerts = t.alerts.map((a) => `<tr><td class="num">${a.day}</td><td><span class="chip ${a.severity === "high" ? "bad" : "warn"}">${esc(a.monitor)}</span></td>
    <td class="wrap">${esc(a.findings.map((f) => f.message).join("; "))}</td></tr>`).join("");
  const cols = Object.entries(t.columns).map(([c, ty]) => `<tr><td><code>${esc(c)}</code></td><td>${esc(ty)}</td></tr>`).join("");
  const schema = t.schema_changes.map((c) => `<li>day ${c.day}: ${esc(Object.keys(c.after).filter((k) => c.before[k] !== c.after[k]).map((k) => `${k} ${c.before[k] || "(none)"} to ${c.after[k]}`).join(", "))}</li>`).join("");
  app.innerHTML = `
    <section class="panel c12"><h3>${esc(t.name)}</h3>
      <div class="row"><span class="chip">${esc(t.layer)}</span>${t.critical ? `<span class="chip bad">critical mart</span>` : ""}
        <span class="muted">owner ${esc(t.owner)} (${esc(t.owner_contacts.slack)})</span></div>
      ${t.description ? `<p class="muted" style="margin:8px 0 0">${esc(t.description)}</p>` : ""}
      <p style="margin:8px 0 0">Upstream: ${t.upstream.map((u) => `<a href="#/table/${u}">${esc(u)}</a>`).join(", ") || "none (source)"}.
        Downstream: ${t.downstream.map((u) => `<a href="#/table/${u}">${esc(u)}</a>`).join(", ") || "none"}.
        ${t.incidents.length ? `Incidents: ${t.incidents.map((i) => `<a href="#/incident/${encodeURIComponent(i)}">${esc(i)}</a>`).join(", ")}.` : ""}</p></section>
    <section class="panel c6"><h2>Volume: rows per daily partition, with learned band</h2>${bandChart(t.volume, { label: "volume", unit: "rows" })}
      <p class="note">Shaded: the learned expected range. Red dots: alerts. Blue dots: marked as expected. No band during the 28-day warm-up.</p></section>
    <section class="panel c6"><h2>Freshness: hours after midnight the partition landed</h2>${bandChart(t.freshness, { label: "freshness", unit: "h" })}
      <p class="note">The alert fires when the partition is still missing at the top of the band.</p></section>
    <section class="panel c8"><h2>Alerts (${t.alerts.length})</h2>${t.alerts.length ? `<div class="scroll"><table><thead><tr><th class="num">Day</th><th>Monitor</th><th>Finding</th></tr></thead><tbody>${alerts}</tbody></table></div>` : `<p class="muted">No alerts in ${summary.days} days.</p>`}
      <h2 style="margin-top:14px">Monitors (${t.monitors.length + 1})</h2>
      <div class="scroll"><table><thead><tr><th>Monitor</th><th>Metric</th><th class="num">Latest</th><th class="num">Alerts</th></tr></thead>
      <tbody><tr><td><span class="chip">schema</span></td><td><code>columns</code></td><td class="num">${Object.keys(t.columns).length} cols</td><td class="num">${t.schema_changes.length}</td></tr>${mons}</tbody></table></div></section>
    <section class="panel c4"><h2>Schema</h2><table><tbody>${cols}</tbody></table>
      ${schema ? `<h2 style="margin-top:14px">Schema changes</h2><ul style="margin:0;padding-left:18px">${schema}</ul>` : ""}
      <p class="note">Primary key: ${esc(t.primary_key || "none")}. Foreign keys: ${esc(Object.entries(t.foreign_keys).map(([k, v]) => `${k} to ${v}`).join(", ") || "none")}.</p></section>`;
  bindTips(app);
}

// ------------------------------------------------------------------ evaluation
async function evaluation() {
  const e = await get("/api/evaluation");
  const yes = (v) => (v ? `<span class="chip good">yes</span>` : `<span class="chip bad">no</span>`);
  const rows = e.by_type.map((r) => `<tr><td>${esc(r.kind)}</td><td><code>${esc(r.table)}</code></td>
    <td>${yes(r.learned_detected)}</td><td class="num">${hrs(r.learned_ttd_hours)}</td>
    <td>${yes(r.static_detected)}</td><td class="num">${hrs(r.static_ttd_hours)}</td>
    <td>${r.root_cause_correct == null ? "n/a" : yes(r.root_cause_correct)}</td><td class="num">${r.tables_flagged} / ${r.tables_affected}</td></tr>`).join("");
  const L = e.learned, S = e.static;
  const m = [
    ["Detection rate", pct(L.detection_rate), pct(S.detection_rate)],
    ["Median time to detection", hrs(L.median_ttd_hours), hrs(S.median_ttd_hours)],
    ["Alerts (table, monitor, day)", L.alerts, S.alerts],
    ["False alerts", L.false_alerts, S.false_alerts],
    ["False alerts per table per week", L.false_alerts_per_table_week.toFixed(3), S.false_alerts_per_table_week.toFixed(3)],
    ["Incidents after grouping", L.incident_count, S.incident_count],
    ["Incidents with no planted cause", L.false_incidents, S.false_incidents],
    ["Root cause correct", pct(L.root_cause_accuracy), pct(S.root_cause_accuracy)],
    ["Alert volume removed by grouping", pct(L.alert_reduction), pct(S.alert_reduction)],
    ["Pages, tickets, and updates sent", e.notifications.learned, e.notifications.static],
  ].map(([a, b, c]) => `<tr><td>${a}</td><td class="num">${b}</td><td class="num">${c}</td></tr>`).join("");
  app.innerHTML = `
    <section class="panel c12"><h2>Planted incidents: learned baselines vs static thresholds</h2>
      <div class="scroll"><table><thead><tr><th>Incident type</th><th>Source table</th><th>Learned</th><th class="num">Time to detect</th>
      <th>Static</th><th class="num">Time to detect</th><th>Root cause correct</th><th class="num">Tables flagged / reach</th></tr></thead><tbody>${rows}</tbody></table></div>
      <p class="note">${e.tables} tables, ${e.days} days, scored after the ${28}-day warm-up. Static rules: change versus the trailing 7-day mean above 30% (volume) or 25% (values), null rate up 5 points, any duplicate key, orphans above 1%, PSI above 0.25, data later than 8 h.</p></section>
    <section class="panel c6"><h2>Totals</h2><div class="scroll"><table><thead><tr><th>Metric</th><th class="num">Learned</th><th class="num">Static</th></tr></thead><tbody>${m}</tbody></table></div></section>
    <section class="panel c6"><h2>Noisiest tables (learned)</h2><table><thead><tr><th>Table</th><th class="num">False alerts</th></tr></thead>
      <tbody>${L.noisiest_tables.map(([t, n]) => `<tr><td><a href="#/table/${t}">${esc(t)}</a></td><td class="num">${n}</td></tr>`).join("")}</tbody></table>
      <p class="note">Static rules alert every weekend and month-end because they compare to a flat 7-day mean. Grouping their alerts chains unrelated noise into a few huge incidents, which is why their root cause accuracy is low.</p></section>`;
}

// ------------------------------------------------------------------ router
async function route() {
  const [path, query] = (location.hash.slice(1) || "/").split("?");
  const params = new URLSearchParams(query || "");
  const parts = path.split("/").filter(Boolean);
  document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("on",
    (a.dataset.nav === "overview" && !parts.length) || a.dataset.nav === parts[0] || (a.dataset.nav === "incidents" && parts[0] === "incident")));
  tip.hidden = true;
  try {
    if (!summary) {
      summary = await get("/api/summary");
      document.getElementById("company").textContent = `${summary.company}, ${summary.tables} tables, ${summary.days} days`;
    }
    if (!parts.length) await overview(params);
    else if (parts[0] === "incidents") await incidents();
    else if (parts[0] === "incident") await incident(decodeURIComponent(parts[1]));
    else if (parts[0] === "table") await table(decodeURIComponent(parts[1]));
    else if (parts[0] === "evaluation") await evaluation();
    else app.innerHTML = `<section class="panel c12">Not found.</section>`;
  } catch (err) {
    app.innerHTML = `<section class="panel c12"><p>Could not load: ${esc(err.message)}</p></section>`;
  }
  window.scrollTo(0, 0);
}

window.addEventListener("hashchange", route);
route();
