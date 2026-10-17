/* Diamond Agent workbench — vanilla JS over the local API.
   Views: overview (resource wall) · idle compute · tasks · agent activity ·
   resource detail. Launch sheet = task → envelope → run location (auto | pin). */

const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => Array.from(root.querySelectorAll(s));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ------------------------------------------------------------ icons */
const ICONS = {
  grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  zap: '<path d="M13 2 3 14h9l-1 8 10-12h-9z"/>',
  list: '<path d="M8 6h13M8 12h13M8 18h13"/><circle cx="3.5" cy="6" r="1"/><circle cx="3.5" cy="12" r="1"/><circle cx="3.5" cy="18" r="1"/>',
  activity: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  bolt: '<path d="M12 2v6m0 8v6M4.9 4.9l4.2 4.2m5.8 5.8 4.2 4.2M2 12h6m8 0h6M4.9 19.1l4.2-4.2m5.8-5.8 4.2-4.2"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 3v6h-6"/>',
  play: '<path d="M6 4v16l14-8z"/>',
  bell: '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10 21a2 2 0 0 0 4 0"/>',
  server: '<rect x="2" y="3" width="20" height="7" rx="2"/><rect x="2" y="14" width="20" height="7" rx="2"/><path d="M6 6.5h.01M6 17.5h.01"/>',
  sliders: '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  sparkle: '<path d="M12 3v4M12 17v4M3 12h4M17 12h4M5.6 5.6l2.8 2.8M15.6 15.6l2.8 2.8M18.4 5.6l-2.8 2.8M8.4 15.6l-2.8 2.8"/>',
  chevron: '<path d="m9 6 6 6-6 6"/>',
  cpu: '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 14h3M1 9h3M1 14h3"/>',
  gpu: '<rect x="2" y="6" width="20" height="12" rx="2"/><circle cx="8" cy="12" r="2.5"/><path d="M14 9v6M17 9v6"/>',
  layers: '<path d="m12 2 10 5-10 5L2 7z"/><path d="m2 12 10 5 10-5"/><path d="m2 17 10 5 10-5"/>',
  cloud: '<path d="M17.5 19a4.5 4.5 0 0 0 .4-9A7 7 0 0 0 4.3 12.5 3.5 3.5 0 0 0 5.5 19z"/>',
  terminal: '<path d="m4 17 6-6-6-6"/><path d="M12 19h8"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  queue: '<path d="M4 6h16M4 12h10M4 18h6"/>',
  check: '<path d="m5 12 5 5L20 7"/>',
  shield: '<path d="M12 3 4 6v6c0 5 3.4 7.9 8 9 4.6-1.1 8-4 8-9V6z"/><path d="m9 12 2 2 4-4"/>',
};
const icon = (n) => `<span class="ico"><svg viewBox="0 0 24 24">${ICONS[n] || ""}</svg></span>`;
const mountIcons = (root = document) => $$("[data-icon]", root).forEach((el) => { el.innerHTML = `<svg viewBox="0 0 24 24">${ICONS[el.dataset.icon] || ""}</svg>`; });

/* ------------------------------------------------------------ state */
const S = {
  config: null, auth: null, resources: [], providers: [], tasks: [], events: [], history: [],
  takenAt: null, view: "overview", selectedSystem: null, resourceTab: "overview",
  detailTask: null, detailTab: "overview", taskFilter: "active", eventFilter: "all",
  wallFilter: "all", sort: "free", search: "", pinTarget: null, locMode: "auto",
  policy: null,
  settings: JSON.parse(localStorage.getItem("da.settings") || "{}"),
  lastSeen: Number(localStorage.getItem("da.lastSeen") || 0),
};
S.settings = { theme: "system", poll: 3000, reduceMotion: false, ...S.settings };

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) { let d = res.statusText; try { d = (await res.json()).detail || d; } catch {} throw new Error(d); }
  return res.json();
}
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });

/* ------------------------------------------------------------ helpers */
const level = (u) => u == null ? "off" : u < 0.01 ? 0 : u < 0.5 ? 1 : u < 0.85 ? 2 : 3;
const pct = (u) => `${Math.round(Math.min(1, Math.max(0, u || 0)) * 100)}%`;
const meter = (u) => `<span class="meter"><span class="meter__track"><span class="meter__fill lvl-${level(u)}" style="width:${pct(u)}"></span></span><span class="meter__pct">${u == null ? "—" : pct(u)}</span></span>`;
const fmtTime = (ts) => ts ? new Date(ts).toLocaleTimeString([], { hour12: false }) : "";
const ago = (ts) => { if (!ts) return "—"; const s = Math.max(0, (Date.now() - new Date(ts)) / 1000); return s < 5 ? "just now" : s < 60 ? `${Math.round(s)}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : `${(s / 3600).toFixed(1)}h ago`; };
const fmtWall = (m) => m == null ? "—" : m % 60 === 0 ? `${m / 60}h` : m > 90 ? `${(m / 60).toFixed(1)}h` : `${m}m`;
const sizingText = (s) => s ? `${s.nodes}n · ${s.gpus}g · ${s.cpus}c · ${fmtWall(s.walltime_minutes)}` : "—";
const hwText = (r) => r.gpu_type ? `${r.gpu_type} ×${r.gpus_per_node} · ${r.gpu_mem_gb} GB/GPU · ${r.cpus_per_node}c` : `${r.cpus_per_node} cores · ${r.mem_per_node_gb} GB`;
const freeOf = (r) => r.gpu_type ? [r.gpus_free ?? r.nodes_idle * r.gpus_per_node, "GPU"] : [r.cpus_free ?? r.nodes_idle * r.cpus_per_node, "cores"];
const utilOf = (r) => { const usable = Math.max(r.nodes_idle + r.nodes_mixed + r.nodes_allocated, 0); if (r.gpu_type && r.gpus_free != null) { const cap = Math.max(usable * r.gpus_per_node, r.gpus_free); return cap ? (cap - r.gpus_free) / cap : 0; } if (!r.gpu_type && r.cpus_free != null) { const cap = Math.max(usable * r.cpus_per_node, r.cpus_free); return cap ? (cap - r.cpus_free) / cap : 0; } const t = usable + r.nodes_down; return t ? (r.nodes_allocated + r.nodes_mixed) / t : 0; };
const PROVIDER_LABEL = { globus_hpc: "HPC · Globus Compute", ssh: "SSH server", cloud: "Cloud" };
const PROVIDER_ICON = { globus_hpc: "layers", ssh: "terminal", cloud: "cloud" };
const healthPill = (h) => `<span class="pill pill--${h}">${esc(h)}</span>`;
const TASK_CLASS = { COMPLETED: "st-good", RUNNING: "st-accent", PENDING: "st-warning", SUBMITTED: "st-muted", SCHEDULED: "st-muted", FAILED: "st-critical", CANCELLED: "st-muted", UNSCHEDULABLE: "st-serious" };
const statusLabel = (s) => `<span class="status ${TASK_CLASS[s] || "st-muted"}"><span class="dot"></span>${esc(s)}</span>`;
const BAD = ["JOB_FAILED", "ENDPOINT_DOWN", "PENDING_TIMEOUT", "TASK_UNSCHEDULABLE", "JOB_SUBMIT_FAILED", "JOB_ORPHANED", "POLICY_DENIED"];
const NOTABLE = (k) => k.startsWith("AGENT") || k.startsWith("POLICY") || BAD.includes(k);
const eventChip = (k) => `<span class="chip ${k.startsWith("AGENT") ? "chip--agent" : BAD.includes(k) ? "chip--bad" : k.startsWith("POLICY") ? "chip--policy" : ""}">${esc(k)}</span>`;
const allRecords = () => S.resources.flatMap((s) => s.partitions);
const systemOf = (name) => S.resources.find((s) => s.name === name);

/* ------------------------------------------------------------ data */
async function loadConfig() {
  S.config = await api("/api/config");
  S.providers = S.config.providers || [];
  renderAbout();
}
async function fetchAll() {
  const [res, tasks, events, hist, auth] = await Promise.all([api("/api/resources"), api("/api/tasks"), api("/api/events?limit=200"), api("/api/federation/history?limit=60"), api("/api/auth/session")]);
  S.resources = res.systems; S.providers = res.providers; S.takenAt = res.taken_at; S.tasks = tasks; S.events = events; S.history = hist; S.auth = auth;
  renderAuth();
}

/* ------------------------------------------------------------ sidebar */
function renderSidebar() {
  const q = S.search.trim().toLowerCase();
  const rows = S.resources.filter((s) => !q || s.name.toLowerCase().includes(q) || s.tags.join(" ").toLowerCase().includes(q) || s.provider.includes(q));
  $("#resource-list").innerHTML = rows.map((s) => {
    const mine = s.user_running_jobs + s.user_pending_jobs;
    const reserved = s.partitions.length === 0;
    const meta = reserved ? `${PROVIDER_LABEL[s.provider]} · not probing yet`
      : `${s.gpus_total ? `${s.gpus_free} GPU free` : `${s.cpus_free.toLocaleString()} cores free`} · ${s.pending_jobs} queued · ${s.partitions.length} part.`;
    return `<button class="resource-row ${S.view === "resource" && S.selectedSystem === s.name ? "is-selected" : ""}" data-system="${esc(s.name)}">
      <span class="dot dot--${reserved ? "reserved" : s.health}"></span>
      <span class="resource-row__content"><span class="resource-row__title">${esc(s.name)}</span><span class="resource-row__meta">${esc(meta)}</span></span>
      ${mine ? `<span class="own-dot" title="your jobs here">${mine}</span>` : ""}
      <span class="resource-row__chevron">${icon("chevron")}</span></button>`;
  }).join("") || `<div class="empty-copy">No resources match "${esc(S.search)}"</div>`;
  $$("#resource-list [data-system]").forEach((b) => b.addEventListener("click", () => openResource(b.dataset.system)));
  const online = S.resources.filter((s) => s.health === "online").length;
  $("#online-count").textContent = `${online}/${S.resources.length}`;
  $("#count-resources").textContent = S.resources.length;
  $("#count-idle").textContent = allRecords().filter((r) => r.endpoint_health === "online" && freeOf(r)[0] > 0).length;
  const active = S.tasks.filter((t) => ["PENDING", "RUNNING", "SUBMITTED", "SCHEDULED", "UNSCHEDULABLE"].includes(t.status)).length;
  $("#count-tasks").textContent = active;
  $("#count-events").textContent = S.events.length >= 200 ? "200+" : S.events.length;
  $("#refresh-text").textContent = S.takenAt ? `snapshot ${ago(S.takenAt)}` : "waiting for first snapshot";
}

/* ------------------------------------------------------------ overview */
const sparkline = (vals) => {
  const v = (vals || []).filter((x) => x != null); if (v.length < 2) return "";
  const W = 120, H = 36, P = 3, lo = Math.min(...v), hi = Math.max(...v), span = hi - lo || 1;
  const pts = v.map((y, i) => [P + (i * (W - 2 * P)) / (v.length - 1), H - P - ((y - lo) / span) * (H - 2 * P)]);
  const d = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
  const last = pts[pts.length - 1];
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" aria-hidden="true"><path class="area" d="${d} L${last[0].toFixed(1)},${H - P} L${pts[0][0].toFixed(1)},${H - P} Z"/><path class="line" d="${d}"/><circle cx="${last[0].toFixed(1)}" cy="${last[1].toFixed(1)}" r="2.5"/></svg>`;
};
function statStrip(el, stats) {
  el.innerHTML = stats.map((k) => k.hero
    ? `<div class="stat stat--hero"><div><strong>${esc(k.value)}${k.unit ? `<em>${esc(k.unit)}</em>` : ""}</strong><small>${esc(k.label)}</small></div>${sparkline(k.spark)}</div>`
    : `<div class="stat"><span class="stat__icon ${k.tone ? "stat__icon--" + k.tone : ""}">${icon(k.icon)}</span><div><strong>${esc(k.value)}</strong><small>${esc(k.label)}</small></div></div>`).join("");
}
const emptyState = (ic, title, sub) => `<div class="empty-state">${icon(ic)}<strong>${esc(title)}</strong><small>${esc(sub)}</small></div>`;
function renderOverview() {
  const recs = allRecords();
  const online = S.resources.filter((s) => s.health === "online").length;
  const gpusFree = S.resources.reduce((a, s) => a + s.gpus_free, 0), gpusTotal = S.resources.reduce((a, s) => a + s.gpus_total, 0);
  const coresFree = S.resources.reduce((a, s) => a + s.cpus_free, 0);
  const mine = S.resources.reduce((a, s) => a + s.user_running_jobs + s.user_pending_jobs, 0);
  statStrip($("#stat-strip"), [
    { hero: true, value: gpusFree.toLocaleString(), unit: `of ${gpusTotal.toLocaleString()} GPUs free`, label: `across ${S.resources.length} systems · last ${S.history.length} snapshots`, spark: S.history.map((h) => (h.aggregates || {}).gpus_idle) },
    { icon: "server", value: `${online} / ${S.resources.length}`, label: "systems online", tone: online === S.resources.length ? "good" : "warning" },
    { icon: "cpu", value: coresFree.toLocaleString(), label: "CPU cores free" },
    { icon: "queue", value: recs.reduce((a, r) => a + r.pending_jobs, 0).toLocaleString(), label: "jobs pending", tone: "neutral" },
    { icon: "activity", value: recs.reduce((a, r) => a + r.running_jobs, 0).toLocaleString(), label: "jobs running", tone: "neutral" },
    { icon: "user", value: mine, label: "my jobs in flight", tone: mine ? "good" : "neutral" },
  ]);
  let systems = S.resources.slice();
  const key = { free: (s) => -(s.gpus_free * 1000 + s.cpus_free / 100), util: (s) => s.utilization ?? 2, pending: (s) => -s.pending_jobs, name: (s) => s.name }[S.sort];
  systems.sort((a, b) => (key(a) > key(b) ? 1 : key(a) < key(b) ? -1 : 0));
  $("#fleet-grid").innerHTML = systems.map(fleetCard).join("");
  $$("#fleet-grid [data-open]").forEach((b) => b.addEventListener("click", () => openResource(b.dataset.open)));
  $$("#fleet-grid [data-launch]").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); const [sys, part] = b.dataset.launch.split("|"); openLaunch({ system: sys, partition: part || null }); }));
}
function fleetCard(s) {
  const parts = s.partitions.filter((r) => S.wallFilter === "all" || (S.wallFilter === "gpu") === !!r.gpu_type);
  if (S.wallFilter !== "all" && !parts.length) return "";
  const reserved = !s.partitions.length;
  const rows = parts.map((r) => {
    const [free, unit] = freeOf(r); const u = r.endpoint_health === "online" ? utilOf(r) : null;
    return `<div class="part-row">
      <div class="part-row__name lvl-${level(u)}"><strong>${esc(r.partition)}</strong><small>${esc(hwText(r))}</small></div>
      ${meter(u)}
      <div class="num num--free"><strong>${free.toLocaleString()}</strong><small>${unit} free</small></div>
      <div class="num"><strong>${r.running_jobs} / ${r.pending_jobs}</strong><small>run / pend</small></div></div>`;
  }).join("");
  return `<article class="fleet-card ${s.health === "offline" ? "fleet-card--offline" : ""}">
    <button class="fleet-card__header" data-open="${esc(s.name)}">
      <span class="dot dot--${reserved ? "reserved" : s.health}"></span>
      <span class="fleet-card__title"><strong>${esc(s.name)}</strong><small>${esc(PROVIDER_LABEL[s.provider] || s.provider)} · ${esc(s.endpoint ? s.endpoint.slice(0, 8) : "—")}${s.tags.length ? " · " + esc(s.tags.join(", ")) : ""}</small></span>
      ${reserved ? `<span class="pill pill--reserved">reserved</span>` : healthPill(s.health)}
      <span class="muted">${icon("chevron")}</span></button>
    ${reserved ? `<div class="fleet-card__rows"><div class="empty-copy">Provider slot registered — no live probe until enabled in the catalog.</div></div>` : `
    <div class="col-head"><span>Partition</span><span>Utilization</span><span>Free</span><span>Queue</span></div>
    <div class="fleet-card__rows">${rows}</div>
    <div class="fleet-card__foot"><span>${s.gpus_total ? `${s.gpus_free} of ${s.gpus_total} GPUs free` : `${s.cpus_free.toLocaleString()} cores free`} · ${s.user_running_jobs + s.user_pending_jobs} mine</span><span>${s.partitions[0] ? ago(s.partitions[0].timestamp) : ""}</span></div>`}
  </article>`;
}

/* ------------------------------------------------------------ idle */
function renderIdle() {
  const f = { gpus: +$("#f-gpus").value || 0, mem: +$("#f-gpumem").value || 0, type: $("#f-gputype").value, arch: $("#f-arch").value, pending: +$("#f-pending").value, provider: $("#f-provider").value, cpu: $("#f-cpu").checked };
  const types = [...new Set(allRecords().map((r) => r.gpu_type).filter(Boolean))].sort();
  const sel = $("#f-gputype"); if (sel.options.length !== types.length + 1) sel.innerHTML = `<option value="">Any</option>` + types.map((t) => `<option ${t === f.type ? "selected" : ""}>${esc(t)}</option>`).join("");
  const cards = [];
  for (const s of S.resources) for (const r of s.partitions) {
    if (f.provider && r.provider !== f.provider) continue;
    if (!r.gpu_type && !f.cpu) continue;
    if (f.type && r.gpu_type !== f.type) continue;
    if (f.arch && r.node_arch !== f.arch) continue;
    const [free, unit] = freeOf(r);
    const reasons = [];
    if (r.endpoint_health !== "online") reasons.push("endpoint " + r.endpoint_health);
    if (r.gpu_type) { if (free < f.gpus) reasons.push(`${free} GPU free < ${f.gpus}`); if (r.gpu_mem_gb < f.mem) reasons.push(`${r.gpu_mem_gb} GB/GPU < ${f.mem}`); }
    else if (free <= 0) reasons.push("no idle cores");
    if (r.pending_jobs > f.pending) reasons.push(`${r.pending_jobs} pending > ${f.pending}`);
    cards.push({ s, r, free, unit, ok: reasons.length === 0, reasons });
  }
  cards.sort((a, b) => (a.ok === b.ok ? (b.free - a.free) : a.ok ? -1 : 1));
  const okN = cards.filter((c) => c.ok).length;
  $("#idle-summary").innerHTML = `<span class="chip">GPUs ≥ ${f.gpus}</span><span class="chip">GPU mem ≥ ${f.mem} GB</span>${f.type ? `<span class="chip">${esc(f.type)}</span>` : ""}${f.arch ? `<span class="chip">${esc(f.arch)}</span>` : ""}<span class="chip">pending ≤ ${f.pending}</span><span style="margin-left:auto">${okN} available · ${cards.length - okN} unavailable</span>`;
  $("#idle-grid").innerHTML = cards.map(({ s, r, free, unit, ok, reasons }) => `
    <article class="idle-card ${ok ? "" : "idle-card--no"}">
      <div class="idle-card__top">${ok ? `<span class="pill pill--online">available</span>` : `<span class="pill pill--degraded">unavailable</span>`}
        <div class="row-actions"><button class="button button--small button--primary" data-launch="${esc(s.name)}|${esc(r.partition)}" ${ok ? "" : "disabled"}>${icon("play")}Launch here</button><button class="button button--small" data-open="${esc(s.name)}">Details</button></div></div>
      <h3>${esc(s.name)} · ${esc(r.partition)}</h3>
      <div class="idle-card__sub">${esc(hwText(r))} · ${esc(r.node_arch)} · ${esc(PROVIDER_LABEL[r.provider] || r.provider)}</div>
      <div class="idle-card__stats"><div><strong>${free.toLocaleString()}</strong><small>${unit} free</small></div><div><strong>${r.pending_jobs}</strong><small>pending</small></div><div><strong>${fmtWall(r.max_walltime_minutes)}</strong><small>max wall</small></div></div>
      <div class="idle-card__foot"><div class="tagrow">${(s.tags || []).map((t) => `<span class="chip">${esc(t)}</span>`).join("")}${r.container_runtime ? `<span class="chip">${esc(r.container_runtime)}</span>` : ""}</div>
        <small class="muted">${ok ? `${r.user_pending_jobs} of mine pending` : esc(reasons.join(" · "))}</small></div>
    </article>`).join("") || emptyState("zap", "Nothing matches these conditions", "Loosen the filters — or wait a tick; the pool refreshes every minute in live mode.");
  $$("#idle-grid [data-open]").forEach((b) => b.addEventListener("click", () => openResource(b.dataset.open)));
  $$("#idle-grid [data-launch]").forEach((b) => b.addEventListener("click", () => { const [sys, part] = b.dataset.launch.split("|"); openLaunch({ system: sys, partition: part }); }));
}

/* ------------------------------------------------------------ tasks */
const ACTIVE = ["PENDING", "RUNNING", "SUBMITTED", "SCHEDULED", "UNSCHEDULABLE"];
function renderTasks() {
  const list = S.tasks.filter((t) => S.taskFilter === "all" || (S.taskFilter === "active") === ACTIVE.includes(t.status));
  $("#tasks-caption").textContent = `${list.length} shown · ${S.tasks.filter((t) => ACTIVE.includes(t.status)).length} active of ${S.tasks.length}`;
  $("#tasks-table tbody").innerHTML = list.map((t) => `<tr class="${t.task_id === S.detailTask ? "is-chosen" : ""}">
      <td><b>${esc(t.name)}</b><br><small class="muted mono">${esc(t.task_id)}</small></td>
      <td>${statusLabel(t.status)}</td>
      <td>${t.system ? `${esc(t.system)}<br><small class="muted">${esc(t.partition)}</small>` : "—"} ${t.target_system ? `<span class="chip chip--pin" title="explicit placement">pinned</span>` : ""}</td>
      <td>${sizingText(t.sizing)}</td><td class="mono">${esc(t.job_id || "—")}</td><td>${t.attempts}</td>
      <td><small>${esc(t.mode || "—")}</small><br><small class="muted">envelope: ${esc(t.envelope_source)}${t.envelope_confidence != null ? ` · ${Math.round(t.envelope_confidence * 100)}%` : ""}</small></td>
      <td><div class="row-actions"><button class="button button--small" data-open="${esc(t.task_id)}">Detail</button>
        ${ACTIVE.includes(t.status) ? `<button class="button button--small button--danger" data-cancel="${esc(t.task_id)}">Cancel</button>` : ""}
        ${["UNSCHEDULABLE", "FAILED", "CANCELLED"].includes(t.status) ? `<button class="button button--small" data-replan="${esc(t.task_id)}">Replan</button>` : ""}</div></td></tr>`).join("")
    || `<tr><td colspan="8">${emptyState("list", S.taskFilter === "active" ? "No active tasks" : "No tasks yet", "Launch a task and the meta-scheduler places it across the pool — or pin it to any resource.")}</td></tr>`;
  const tb = $("#tasks-table tbody");
  $$("[data-open]", tb).forEach((b) => b.addEventListener("click", () => openTask(b.dataset.open)));
  $$("[data-cancel]", tb).forEach((b) => b.addEventListener("click", async () => { await post(`/api/tasks/${b.dataset.cancel}/cancel`); refresh(); }));
  $$("[data-replan]", tb).forEach((b) => b.addEventListener("click", async () => { await post(`/api/tasks/${b.dataset.replan}/replan`); refresh(); }));
  if (S.detailTask) renderTaskDetail();
}
function openTask(id) { S.detailTask = id; const t = S.tasks.find((x) => x.task_id === id); if (t && !ACTIVE.includes(t.status) && S.taskFilter === "active") { S.taskFilter = "all"; $$("#task-filter button").forEach((x) => x.classList.toggle("is-active", x.dataset.v === "all")); } switchView("tasks"); $("#task-detail").classList.remove("hidden"); renderTaskDetail(); }
async function renderTaskDetail() {
  let t; try { t = await api(`/api/tasks/${S.detailTask}`); } catch { return; }
  $("#detail-title").textContent = t.spec.name;
  $("#detail-sub").textContent = `${t.task_id} · created ${fmtTime(t.created_at)}${t.target_system ? ` · pinned to ${t.target_system}${t.target_partition ? ":" + t.target_partition : ""}` : " · auto placement"}`;
  $("#detail-actions").innerHTML = `${t.target_system && ["UNSCHEDULABLE", "FAILED", "CANCELLED", "PENDING"].includes(t.status) ? `<button class="button button--small" id="btn-unpin">Unpin & replan</button>` : ""}<button class="button button--small" id="btn-close-detail">Close</button>`;
  $("#btn-close-detail").addEventListener("click", () => { S.detailTask = null; $("#task-detail").classList.add("hidden"); renderTasks(); });
  const unpin = $("#btn-unpin"); if (unpin) unpin.addEventListener("click", async () => { await post(`/api/tasks/${t.task_id}/replan`, { clear_target: true }); refresh(); });
  const env = t.spec.envelope;
  const kv = (l, v) => `<div class="kv"><small>${esc(l)}</small><strong>${v}</strong></div>`;
  let body = "";
  if (S.detailTab === "overview") {
    body = `<div class="detail-grid">
      ${kv("Status", statusLabel(t.status))}
      ${kv("Placement", t.placement ? `${esc(t.placement.system)}:${esc(t.placement.partition)}` : "—")}
      ${kv("Sizing", esc(sizingText(t.placement && t.placement.sizing)))}
      ${kv("Job", esc(t.job_id || "—"))}
      ${kv("Attempts", `${t.attempts}`)}
      ${kv("Wait / run (ticks)", `${t.pending_ticks} / ${t.running_ticks}`)}
      ${kv("Envelope source", `${esc(t.envelope_source)}${t.envelope_confidence != null ? ` · confidence ${Math.round(t.envelope_confidence * 100)}%` : ""}`)}
      ${kv("Scheduler mode", esc(t.decision ? t.decision.mode : "—"))}</div>
      <div class="kv" style="margin-bottom:12px"><small>Envelope</small><span class="mono">gpu ${env.gpu.min_count}–${env.gpu.max_count}${env.gpu.min_memory ? " ≥" + esc(env.gpu.min_memory) + "/GPU" : ""} · cpu ${env.cpu.min_count}–${env.cpu.max_count} · mem ${esc(env.memory.min || "?")}–${esc(env.memory.max || "?")} · wall ${esc(env.walltime.min)}–${esc(env.walltime.max)} · accel [${(env.compatibility.accelerators || []).map(esc).join(", ") || "any"}] · ${esc(env.objective)}</span></div>
      ${t.envelope_rationale ? `<div class="hint">${esc(t.envelope_rationale)}</div>` : ""}
      <div class="kv" style="margin-top:12px"><small>Command</small><span class="mono">${esc(t.spec.command)}</span></div>`;
  } else if (S.detailTab === "decision") {
    if (!t.decision) body = `<div class="muted">No placement decision yet.</div>`;
    else {
      const ch = t.decision.chosen;
      const rows = (t.decision.candidates || []).slice().sort((a, b) => (a.feasible === b.feasible) ? (a.ai_rank ?? 99) - (b.ai_rank ?? 99) : (a.feasible ? -1 : 1)).map((c) => {
        const chosen = ch && c.system === ch.system && c.partition === ch.partition;
        return `<tr class="${chosen ? "is-chosen" : ""}"><td>${chosen ? "◆ " : ""}${esc(c.system)}<br><small class="muted">${esc(c.partition)}</small></td>
          <td>${c.feasible ? `<span class="status st-good"><span class="dot"></span>feasible</span>` : `<span class="status st-critical"><span class="dot"></span>rejected</span>`}</td>
          <td class="wrap"><div class="verdicts">${(c.verdicts || []).map((v) => `<span class="chip ${v.passed ? "chip--pass" : "chip--fail"}" title="${esc(v.detail)}">${v.passed ? "✓" : "✕"} ${esc(v.rule)}</span>`).join("")}</div></td>
          <td>${esc(sizingText(c.sizing))}</td><td>${c.est_wait_minutes != null ? c.est_wait_minutes.toFixed(0) + "m" : "—"}</td><td>${c.ai_rank ?? "—"}</td><td class="wrap"><small>${esc(c.rationale || "")}</small></td></tr>`;
      }).join("");
      body = `<div class="hint" style="margin-bottom:10px">mode <b>${esc(t.decision.mode)}</b> · objective <b>${esc(t.decision.objective)}</b>${t.decision.decision_ms != null ? ` · ${t.decision.decision_ms.toFixed(0)} ms` : ""} — ${esc(t.decision.summary || "")}</div>
        <div class="table-wrap"><table class="table"><thead><tr><th>Candidate</th><th>Verdict</th><th>Rule chain</th><th>Sizing</th><th>Est. wait</th><th>Rank</th><th>Rationale</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    }
  } else {
    body = `<div class="timeline">${(t.timeline || []).map((e) => `<div class="event"><span class="ts">${fmtTime(e.ts)}</span>${eventChip(e.kind)}<span class="msg">${esc(e.message)}</span></div>`).join("")}</div>`;
  }
  $("#detail-body").innerHTML = body;
}

/* ------------------------------------------------------------ activity */
function renderActivity() {
  const list = S.events.filter((e) => S.eventFilter === "all" || (S.eventFilter === "agent" && e.kind.startsWith("AGENT")) || (S.eventFilter === "bad" && BAD.includes(e.kind)));
  $("#event-feed").innerHTML = list.map((e) => `<div class="event"><span class="ts">${fmtTime(e.ts)}</span>${eventChip(e.kind)}<span class="msg">${esc(e.message)}${e.task_id ? `<span class="task-ref" data-task="${esc(e.task_id)}">[${esc(e.task_id.slice(0, 8))}]</span>` : ""}</span></div>`).join("") || emptyState("activity", "No agent activity yet", "Every placement decision, dispatch and continuation shows up here with its rationale.");
  $$("#event-feed .task-ref").forEach((el) => el.addEventListener("click", () => openTask(el.dataset.task)));
}

/* ------------------------------------------------------------ resource */
function openResource(name) { S.selectedSystem = name; S.resourceTab = "overview"; $$("#resource-tabs button").forEach((b) => b.classList.toggle("is-active", b.dataset.t === "overview")); switchView("resource"); }
async function renderResource() {
  const s = systemOf(S.selectedSystem);
  if (!s) { $("#resource-head").innerHTML = `<div class="muted">Unknown resource</div>`; $("#resource-body").innerHTML = ""; return; }
  $("#eyebrow").textContent = `${PROVIDER_LABEL[s.provider] || s.provider}${s.location ? " · " + s.location : ""}`;
  $("#page-title").textContent = s.name;
  const reserved = !s.partitions.length;
  $("#resource-head").innerHTML = `<div><div class="pills">${reserved ? `<span class="pill pill--reserved">reserved provider</span>` : healthPill(s.health)}<span class="pill pill--provider">${icon(PROVIDER_ICON[s.provider] || "layers")} ${esc(PROVIDER_LABEL[s.provider] || s.provider)}</span><span class="pill mono">${esc(s.endpoint || "—")}</span>${s.tags.map((t) => `<span class="pill">${esc(t)}</span>`).join("")}</div></div>
    <div class="row-actions"><button class="button" id="btn-limits-here" ${reserved ? "disabled" : ""} title="Max nodes per job on each queue of this system">${icon("shield")}Limits</button><button class="button button--primary" id="btn-launch-here" ${reserved ? "disabled" : ""}>${icon("play")}Launch here</button></div>`;
  $("#btn-launch-here").addEventListener("click", () => openLaunch({ system: s.name, partition: null }));
  $("#btn-limits-here").addEventListener("click", openPolicy);
  if (S.resourceTab === "overview") {
    const strip = document.createElement("div"); strip.className = "stat-strip";
    const gpuSys = s.gpus_total > 0;
    statStrip(strip, [
      { hero: true, value: (gpuSys ? s.gpus_free : s.cpus_free).toLocaleString(), unit: gpuSys ? `of ${s.gpus_total.toLocaleString()} GPUs free` : "cores free", label: `${s.partitions.length} partition${s.partitions.length === 1 ? "" : "s"} · ${PROVIDER_LABEL[s.provider] || s.provider}`,
        spark: S.history.map((h) => (h.records || []).filter((r) => r.system === s.name).reduce((a, r) => a + (r.free || 0), 0)) },
      { icon: gpuSys ? "cpu" : "gpu", value: (gpuSys ? s.cpus_free : s.gpus_free).toLocaleString(), label: gpuSys ? "CPU cores free" : "GPUs free" },
      { icon: "queue", value: `${s.running_jobs} / ${s.pending_jobs}`, label: "running / pending", tone: "neutral" },
      { icon: "user", value: `${s.user_running_jobs} / ${s.user_pending_jobs}`, label: "mine running / pending", tone: (s.user_running_jobs + s.user_pending_jobs) ? "good" : "neutral" },
    ]);
    const cards = s.partitions.map((r) => { const [free, unit] = freeOf(r); const u = r.endpoint_health === "online" ? utilOf(r) : null; return `
      <article class="part-card"><div class="part-card__top"><div><h3>${esc(r.partition)}${(s.limits || {})[r.partition]?.blocked ? ` <span class="chip chip--bad">blocked</span>` : ""}</h3><div class="sub">${esc(hwText(r))} · ${esc(r.node_arch)} · account ${esc(r.account)} · max ${esc((s.limits || {})[r.partition]?.max_nodes_per_job ?? "?")} nodes/job</div></div><button class="button button--small" data-launch="${esc(s.name)}|${esc(r.partition)}" ${(s.limits || {})[r.partition]?.blocked ? "disabled" : ""}>${icon("play")}Launch</button></div>
        <div class="metric"><small><span>Utilization</span><span>${free.toLocaleString()} ${unit} free</span></small>${meter(u)}</div>
        <div class="stat-row"><span><small>Nodes idle / mixed</small><strong>${r.nodes_idle} / ${r.nodes_mixed}</strong></span><span><small>Allocated / down</small><strong>${r.nodes_allocated} / ${r.nodes_down}</strong></span><span><small>Queue run / pend</small><strong>${r.running_jobs} / ${r.pending_jobs}</strong></span><span><small>Mine · max wall</small><strong>${r.user_running_jobs + r.user_pending_jobs} · ${fmtWall(r.max_walltime_minutes)}</strong></span></div></article>`; }).join("");
    $("#resource-body").innerHTML = "";
    $("#resource-body").appendChild(strip);
    $("#resource-body").insertAdjacentHTML("beforeend", `<div class="part-cards">${cards || `<div class="empty-copy">No live partitions — this provider is reserved until enabled.</div>`}</div>`);
    $$("#resource-body [data-launch]").forEach((b) => b.addEventListener("click", () => { const [sys, part] = b.dataset.launch.split("|"); openLaunch({ system: sys, partition: part }); }));
  } else if (S.resourceTab === "history") {
    try { S.history = await api("/api/federation/history?limit=240"); } catch { S.history = []; }
    const parts = s.partitions.map((r) => r.partition);
    const cells = (p) => S.history.map((h) => { const rec = (h.records || []).find((r) => r.system === s.name && r.partition === p); if (!rec) return `<td></td>`; if (rec.health !== "online") return `<td class="hoff" title="${esc(fmtTime(h.taken_at))} · offline"></td>`; const u = rec.utilization; const c = u < 0.01 ? "" : u < 0.25 ? "h1" : u < 0.5 ? "h2" : u < 0.75 ? "h3" : u < 0.92 ? "h4" : "h5"; return `<td class="${c}" title="${esc(fmtTime(h.taken_at))} · ${pct(u)} busy · ${rec.free} free · ${rec.pending} pending"></td>`; }).join("");
    $("#resource-body").innerHTML = `<div class="panel"><div class="panel__head"><div><strong>Queue history</strong><small>one cell per snapshot (${S.history.length} kept in memory · minute-level probes) · busy fraction of the gating resource</small></div></div>
      <div class="heat"><table>${parts.map((p) => `<tr><th>${esc(p)}</th>${cells(p)}</tr>`).join("")}</table></div>
      <div class="heat__legend">idle <i style="background:var(--surface-2)"></i><i style="background:var(--seq-1)"></i><i style="background:var(--seq-2)"></i><i style="background:var(--seq-3)"></i><i style="background:var(--seq-4)"></i><i style="background:var(--seq-5)"></i> saturated · <i style="background:repeating-linear-gradient(135deg, var(--critical) 0 2px, transparent 2px 5px)"></i> offline</div></div>`;
  } else {
    const p = S.providers.find((x) => x.kind === s.provider) || {};
    $("#resource-body").innerHTML = `<div class="detail-grid">
      <div class="kv"><small>Provider</small><strong>${esc(PROVIDER_LABEL[s.provider] || s.provider)}</strong><div class="hint">${esc(p.detail || "")} · status ${esc(p.status || "?")}</div></div>
      <div class="kv"><small>Endpoint</small><strong class="mono">${esc(s.endpoint || "—")}</strong></div>
      <div class="kv"><small>Accounts</small><strong>${esc(s.accounts.join(", ") || "—")}</strong></div>
      <div class="kv"><small>Declared partitions</small><strong>${esc(s.declared_partitions.join(", ") || "—")}</strong></div>
      <div class="kv"><small>Container runtime</small><strong>${esc((s.partitions[0] || {}).container_runtime || "—")}</strong></div>
      <div class="kv"><small>Tags</small><strong>${esc(s.tags.join(", ") || "—")}</strong></div></div>
      <div class="hint">Resources are declared in the site catalog (<code>sites.yaml</code> / <code>sites.live.yaml</code>): hardware facts, accounts, per-partition QOS and site dialect switches. Live queue state is merged in from the provider probe every tick. Use <b>Add resource</b> for the provider-specific steps.</div>`;
  }
}

/* ------------------------------------------------------------ launch sheet */
function openLaunch(pin) {
  S.pinTarget = pin || null; S.locMode = pin ? "pin" : "auto";
  $$("#loc-mode button").forEach((b) => b.classList.toggle("is-active", b.dataset.v === S.locMode));
  renderPinGrid();
  $("#launch-status").textContent = "";
  $("#launch-sheet").classList.remove("hidden");
  $("#t-name").focus();
}
function renderPinGrid() {
  const grid = $("#pin-grid"); grid.classList.toggle("hidden", S.locMode !== "pin");
  $("#loc-hint").textContent = S.locMode === "pin" ? "The task goes to exactly this resource; the scheduler still sizes it inside the envelope and continuation retries in place instead of migrating." : "Rules compute the feasible set and size inside the envelope; the AI ranks feasible candidates for the objective. The decision and its rationale are shown on the task.";
  grid.innerHTML = S.resources.map((s) => { const sel = S.pinTarget && S.pinTarget.system === s.name; const reserved = !s.partitions.length; return `
    <button class="pin-card ${sel ? "is-selected" : ""}" data-pin="${esc(s.name)}" ${reserved ? "disabled" : ""}><strong><span class="dot dot--${reserved ? "reserved" : s.health}"></span>${esc(s.name)}</strong><small>${reserved ? "reserved provider" : `${s.gpus_total ? `${s.gpus_free} GPU free` : `${s.cpus_free.toLocaleString()} cores free`} · ${s.pending_jobs} pending`}</small>
      <select data-part="${esc(s.name)}" ${sel ? "" : "disabled"}><option value="">any partition</option>${s.partitions.map((r) => `<option ${sel && S.pinTarget.partition === r.partition ? "selected" : ""}>${esc(r.partition)}</option>`).join("")}</select></button>`; }).join("");
  $$("#pin-grid [data-pin]").forEach((b) => b.addEventListener("click", (e) => { if (e.target.tagName === "SELECT") return; S.pinTarget = { system: b.dataset.pin, partition: null }; renderPinGrid(); }));
  $$("#pin-grid select").forEach((sel) => sel.addEventListener("change", () => { S.pinTarget = { system: sel.dataset.part, partition: sel.value || null }; }));
}
const listField = (id) => $(id).value.split(",").map((x) => x.trim()).filter(Boolean);
function envelopeFromForm() {
  return { gpu: { min_count: +$("#e-gmin").value, max_count: +$("#e-gmax").value, min_memory: +$("#e-gmem").value > 0 ? `${$("#e-gmem").value}GB` : null },
    cpu: { min_count: +$("#e-cmin").value, max_count: +$("#e-cmax").value },
    memory: { min: `${$("#e-mmin").value}GB`, max: `${$("#e-mmax").value}GB` },
    walltime: { min: $("#e-wmin").value, max: $("#e-wmax").value },
    compatibility: { cpu_arch: listField("#e-arch"), accelerators: listField("#e-accel"), container_required: !!$("#t-container").value.trim() },
    objective: $("#e-obj").value };
}
function envelopeToForm(e) {
  $("#e-gmin").value = e.gpu.min_count; $("#e-gmax").value = e.gpu.max_count; $("#e-gmem").value = parseInt(e.gpu.min_memory || "0", 10) || 0;
  $("#e-cmin").value = e.cpu.min_count; $("#e-cmax").value = e.cpu.max_count;
  $("#e-mmin").value = parseInt(e.memory.min || "0", 10) || 0; $("#e-mmax").value = parseInt(e.memory.max || "0", 10) || 0;
  $("#e-wmin").value = e.walltime.min; $("#e-wmax").value = e.walltime.max; $("#e-obj").value = e.objective;
  $("#e-accel").value = (e.compatibility.accelerators || []).join(", "); $("#e-arch").value = (e.compatibility.cpu_arch || []).join(", ");
}
async function draftEnvelope() {
  const btn = $("#btn-draft"); btn.disabled = true; $("#env-hint").textContent = "Agent is drafting the envelope…";
  try { const r = await post("/api/envelope/generate", { name: $("#t-name").value, description: $("#t-desc").value, command: $("#t-cmd").value }); envelopeToForm(r.envelope); $("#env-hint").innerHTML = `<b>${esc(r.source)}</b> · confidence ${Math.round((r.confidence || 0) * 100)}% — ${esc(r.rationale)}`; }
  catch (e) { $("#env-hint").textContent = `Draft failed: ${e.message}`; } finally { btn.disabled = false; }
}
async function submitTask() {
  const btn = $("#btn-submit"); btn.disabled = true; $("#launch-status").textContent = "Submitting — meta-scheduler is deciding placement…";
  const body = { name: $("#t-name").value.trim() || "task", description: $("#t-desc").value, command: $("#t-cmd").value, container: $("#t-container").value.trim() || null, envelope: envelopeFromForm(), max_retries: +$("#t-retries").value || 0, auto_continue: $("#t-autocontinue").checked };
  if (S.locMode === "pin") { if (!S.pinTarget) { $("#launch-status").textContent = "Pick a resource to pin to."; btn.disabled = false; return; } body.target = S.pinTarget; }
  try { const r = await post("/api/tasks", body); $("#launch-sheet").classList.add("hidden"); await fetchAll(); openTask(r.task.task_id); }
  catch (e) { $("#launch-status").textContent = `Submit failed: ${e.message}`; } finally { btn.disabled = false; }
}

/* ------------------------------------------------------------ limits (operator policy) */
async function openPolicy() {
  $("#policy-status").textContent = "Limits apply to every client of this core — web UI, CLI, MCP, HTTP — and can only be changed here.";
  $("#policy-body").innerHTML = `<div class="empty-copy">Loading limits…</div>`;
  $("#policy-sheet").classList.remove("hidden");
  try { S.policy = await api("/api/policy"); } catch (e) { $("#policy-body").innerHTML = `<div class="notice">Could not load limits: ${esc(e.message)}</div>`; return; }
  renderPolicySheet();
}
function renderPolicySheet() {
  const p = S.policy;
  const rec = (r) => ((systemOf(r.system) || {}).partitions || []).find((x) => x.partition === r.partition);
  $("#policy-body").innerHTML = `
    <div class="policy-defaults">
      <label>Default max nodes per job <input type="number" id="p-nodes" min="1" value="${p.max_nodes_per_job}"></label>
      <label>Max pending jobs per system <input type="number" id="p-pending" min="1" value="${p.max_user_pending_per_system}"></label>
    </div>
    <div class="hint" style="margin:0 0 12px">Per queue: the largest job the scheduler may build there. Every task is sized inside this cap and the dispatcher refuses anything above it, whichever client submitted it. Leave a field empty to use the default. The CLI and MCP can read these limits (<code>diamond-agent-core policy</code>) but not change them.</div>
    <div class="table-wrap"><table class="table policy-table"><thead><tr><th>System</th><th>Queue</th><th>Hardware</th><th>Nodes</th><th>Max nodes / job</th><th>Blocked</th></tr></thead><tbody>
    ${p.partitions.map((r) => { const x = rec(r); return `<tr class="${r.blocked ? "is-blocked" : ""}">
      <td>${esc(r.system)}</td><td><b>${esc(r.partition)}</b></td><td><small class="muted">${x ? esc(hwText(x)) : "—"}</small></td><td>${r.nodes_total ?? "—"}</td>
      <td><span class="unit-input"><input type="number" min="1" ${r.nodes_total ? `max="${r.nodes_total}"` : ""} data-cap="${esc(r.key)}" value="${r.override ?? ""}" placeholder="${p.max_nodes_per_job}"><span class="cap-default">${r.override != null ? "override" : `default ${p.max_nodes_per_job}`}</span></span></td>
      <td><label class="check"><input type="checkbox" data-block="${esc(r.key)}" ${r.blocked ? "checked" : ""}> blocked</label></td></tr>`; }).join("") || `<tr><td colspan="6"><div class="empty-copy">No queues in the pool yet.</div></td></tr>`}
    </tbody></table></div>`;
}
async function savePolicy() {
  const btn = $("#btn-policy-save"); btn.disabled = true; $("#policy-status").textContent = "Saving…";
  const body = { max_nodes_per_job: +$("#p-nodes").value || null, max_user_pending_per_system: +$("#p-pending").value || null,
    partition_limits: Object.fromEntries($$("#policy-body [data-cap]").map((el) => [el.dataset.cap, el.value ? +el.value : null])),
    blocked_partitions: $$("#policy-body [data-block]").filter((el) => el.checked).map((el) => el.dataset.block) };
  try { S.policy = await api("/ui/policy", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); renderPolicySheet(); $("#policy-status").textContent = "Saved — in force for the next placement and recorded in Agent activity."; refresh(); }
  catch (e) { $("#policy-status").textContent = `Not saved: ${e.message}`; } finally { btn.disabled = false; }
}

/* ------------------------------------------------------------ sheets */
function renderAddSheet() {
  const p = Object.fromEntries(S.providers.map((x) => [x.kind, x]));
  const card = (kind, title, sub, steps) => { const info = p[kind] || { status: "reserved" }; return `<div class="option-card"><span class="option-card__icon">${icon(PROVIDER_ICON[kind])}</span><div><strong>${esc(title)}</strong><small>${esc(sub)}</small><div class="hint" style="margin-top:4px">${esc(info.detail || "")}</div></div><span class="pill ${info.status === "active" ? "pill--online" : info.status === "error" ? "pill--offline" : "pill--reserved"}">${esc(info.status)}</span><pre>${esc(steps)}</pre></div>`; };
  $("#add-body").innerHTML = `<p class="hint" style="margin:0 0 12px">Every provider maps its resources onto the same record shape and accepts the same task, so the agent can submit to any of them — and the meta-scheduler can choose among all of them.</p>` +
    card("globus_hpc", "HPC system via Diamond / Globus Compute", "Slurm partitions probed minute-level through a login-node endpoint you own; jobs submitted as rendered sbatch scripts.",
      "make login                                   # Globus auth\nmake endpoint-setup HOST=you@login.delta.ncsa.illinois.edu   # once per supercomputer: creates the endpoint there\nmake endpoints && make init-sites            # scaffold sites.live.yaml; fill allocation accounts\nmake preflight && make live") +
    card("ssh", "SSH server (reserved)", "Agent-less: nvidia-smi + /proc over your own OpenSSH config, one record per server; nohup launch with a managed log.",
      "ssh_servers:\n  - name: lab-a100\n    host: 10.0.0.10\n    user: researcher\n    tags: [lab]\n    enabled: false      # flip to probe\n    execution: false    # flip when verified") +
    card("cloud", "Cloud instance pool (reserved)", "One record per (region, instance family): quota headroom as idle nodes, price carried for the objective scorer.",
      "cloud:\n  - name: aws-us-east-1\n    provider: aws\n    region: us-east-1\n    instance_types: [g5.xlarge]\n    enabled: false");
  $("#add-sheet").classList.remove("hidden");
}
function renderAbout() {
  const ai = (S.config && S.config.ai) || {};
  $("#about-box").innerHTML = `<b>Diamond Agent</b> · scheduler AI: <b>${ai.enabled ? esc(ai.model) : "heuristic fallback"}</b>${ai.reason && !ai.enabled ? ` (${esc(ai.reason)})` : ""}<br>Providers: ${S.providers.map((p) => `${esc(p.kind)} <i>${esc(p.status)}</i>`).join(" · ")}`;
}
function applySettings() {
  const st = S.settings; localStorage.setItem("da.settings", JSON.stringify(st));
  if (st.theme === "system") document.documentElement.removeAttribute("data-theme"); else document.documentElement.setAttribute("data-theme", st.theme);
  document.documentElement.setAttribute("data-reduce-motion", st.reduceMotion ? "true" : "false");
  $$("#theme-seg button").forEach((b) => b.classList.toggle("is-active", b.dataset.v === st.theme));
  $$("#poll-seg button").forEach((b) => b.classList.toggle("is-active", +b.dataset.v === st.poll));
  $("#reduce-motion").checked = !!st.reduceMotion;
  restartPolling();
}
function renderBell() {
  const notable = S.events.filter((e) => NOTABLE(e.kind));
  const unread = notable.filter((e) => new Date(e.ts).getTime() > S.lastSeen);
  $("#bell-dot").classList.toggle("hidden", unread.length === 0);
  $("#bell-list").innerHTML = notable.slice(0, 30).map((e) => `<div class="event"><span class="ts">${fmtTime(e.ts)}</span>${eventChip(e.kind)}<span class="msg">${esc(e.message)}${e.task_id ? `<span class="task-ref" data-task="${esc(e.task_id)}">[${esc(e.task_id.slice(0, 8))}]</span>` : ""}</span></div>`).join("") || `<div class="empty-copy">Nothing notable yet.</div>`;
  $$("#bell-list .task-ref").forEach((el) => el.addEventListener("click", () => { $("#bell-pop").classList.add("hidden"); openTask(el.dataset.task); }));
}

/* ------------------------------------------------------------ auth */
function renderAuth() {
  const a = S.auth; if (!a) return;
  const gate = $("#signin-sheet");
  const needs = a.required && !a.authenticated;
  gate.classList.toggle("hidden", !needs);
  if (needs) {
    $("#signin-systems").textContent = S.resources.length;
    $("#signin-providers").textContent = S.providers.filter((p) => p.status === "needs-login").length;
    const cta = $("#btn-signin"); cta.classList.toggle("hidden", !a.web_login_available);
    const err = new URLSearchParams(location.search).get("auth_error");
    const box = $("#signin-error");
    if (err) { box.textContent = `Sign-in did not complete (${err}). Try again; if it repeats, check the redirect URI registered for your Globus app.`; box.classList.remove("hidden"); }
    else if (!a.web_login_available) { box.textContent = "Browser sign-in is not configured on this core (GLOBUS_CLIENT_ID / GLOBUS_CLIENT_SECRET). Use the shell login below."; box.classList.remove("hidden"); }
    else box.classList.add("hidden");
  }
  const id = $("#identity"); id.classList.toggle("hidden", !a.required);
  if (!a.required) return;
  const u = a.user || {};
  const name = u.name || u.email || u.username || (a.mode === "web-session" ? "Globus user" : a.authenticated ? "Signed in" : "Not signed in");
  $("#identity-name").textContent = name;
  $("#identity-sub").textContent = a.mode === "web-session" ? (u.email || "web session · auto-refresh") : (a.hint || a.mode);
  $("#identity-avatar").textContent = a.authenticated ? (name.trim()[0] || "·") : "!";
  $("#identity-avatar").style.background = a.authenticated ? "" : "var(--critical)";
  $("#btn-signout").classList.toggle("hidden", a.mode !== "web-session");
  $("#btn-websignin").classList.toggle("hidden", !(a.web_login_available && a.mode !== "web-session"));
}
async function signOut() {
  await post("/api/auth/logout");
  await refresh();
}

/* ------------------------------------------------------------ views */
const TITLES = { overview: ["Federation", "Compute overview"], idle: ["Resource discovery", "Find idle compute"], tasks: ["My tasks", "Tasks & placements"], activity: ["Agent", "Agent activity"], resource: ["Resource", ""] };
function switchView(name) {
  S.view = name;
  history.replaceState(null, "", "#" + (name === "resource" ? "resource/" + encodeURIComponent(S.selectedSystem || "") + (S.resourceTab !== "overview" ? "/" + S.resourceTab : "") : name));
  $$(".primary-nav button").forEach((b) => b.classList.toggle("is-active", b.dataset.view === name));
  $$(".view").forEach((v) => v.classList.toggle("is-active", v.id === `view-${name}`));
  if (name !== "resource") { $("#eyebrow").textContent = TITLES[name][0]; $("#page-title").textContent = TITLES[name][1]; }
  render();
}
async function render() {
  renderSidebar(); renderBell();
  if (S.view === "overview") renderOverview();
  else if (S.view === "idle") renderIdle();
  else if (S.view === "tasks") renderTasks();
  else if (S.view === "activity") renderActivity();
  else if (S.view === "resource") await renderResource();
}
async function refresh() { try { await fetchAll(); await render(); } catch (e) { $("#refresh-text").textContent = `offline: ${e.message}`; } }
let timer = null;
function restartPolling() { if (timer) clearInterval(timer); timer = setInterval(() => { if (!document.hidden) refresh(); }, S.settings.poll); }

/* ------------------------------------------------------------ wiring */
mountIcons();
$$(".primary-nav button").forEach((b) => b.addEventListener("click", () => switchView(b.dataset.view)));
$("#resource-search").addEventListener("input", (e) => { S.search = e.target.value; renderSidebar(); });
$("#sort-select").addEventListener("change", (e) => { S.sort = e.target.value; renderOverview(); });
$$("#wall-filter button").forEach((b) => b.addEventListener("click", () => { S.wallFilter = b.dataset.v; $$("#wall-filter button").forEach((x) => x.classList.toggle("is-active", x === b)); renderOverview(); }));
$$("#task-filter button").forEach((b) => b.addEventListener("click", () => { S.taskFilter = b.dataset.v; $$("#task-filter button").forEach((x) => x.classList.toggle("is-active", x === b)); renderTasks(); }));
$$("#event-filter button").forEach((b) => b.addEventListener("click", () => { S.eventFilter = b.dataset.v; $$("#event-filter button").forEach((x) => x.classList.toggle("is-active", x === b)); renderActivity(); }));
$$("#detail-tabs button").forEach((b) => b.addEventListener("click", () => { S.detailTab = b.dataset.t; $$("#detail-tabs button").forEach((x) => x.classList.toggle("is-active", x === b)); renderTaskDetail(); }));
$$("#resource-tabs button").forEach((b) => b.addEventListener("click", () => { S.resourceTab = b.dataset.t; $$("#resource-tabs button").forEach((x) => x.classList.toggle("is-active", x === b)); renderResource(); }));
$$(".filter-grid input, .filter-grid select").forEach((el) => el.addEventListener("input", renderIdle));
$("#btn-idle-reset").addEventListener("click", () => { $("#f-gpus").value = 1; $("#f-gpumem").value = 0; $("#f-gputype").value = ""; $("#f-arch").value = ""; $("#f-pending").value = 9999; $("#f-provider").value = ""; $("#f-cpu").checked = true; renderIdle(); });
$("#btn-refresh").addEventListener("click", refresh);
$("#btn-signout").addEventListener("click", signOut);
$("#btn-launch").addEventListener("click", () => openLaunch(null));
$("#btn-draft").addEventListener("click", draftEnvelope);
$("#btn-submit").addEventListener("click", submitTask);
$$("#loc-mode button").forEach((b) => b.addEventListener("click", () => { S.locMode = b.dataset.v; $$("#loc-mode button").forEach((x) => x.classList.toggle("is-active", x === b)); renderPinGrid(); }));
$("#btn-add-resource").addEventListener("click", renderAddSheet);
$("#btn-policy").addEventListener("click", openPolicy);
$("#btn-policy-save").addEventListener("click", savePolicy);
$("#btn-settings").addEventListener("click", () => { renderAbout(); $("#settings-sheet").classList.remove("hidden"); });
$("#btn-brand").addEventListener("click", () => { renderAbout(); $("#settings-sheet").classList.remove("hidden"); });
$$("[data-close]").forEach((b) => b.addEventListener("click", () => $(`#${b.dataset.close}`).classList.add("hidden")));
$$(".sheet-backdrop:not(#signin-sheet)").forEach((bd) => bd.addEventListener("mousedown", (e) => { if (e.target === bd) bd.classList.add("hidden"); }));
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { $$(".sheet-backdrop:not(#signin-sheet)").forEach((x) => x.classList.add("hidden")); $("#bell-pop").classList.add("hidden"); } });
$("#btn-bell").addEventListener("click", () => { renderBell(); $("#bell-pop").classList.toggle("hidden"); });
$("#btn-bell-clear").addEventListener("click", () => { S.lastSeen = Date.now(); localStorage.setItem("da.lastSeen", String(S.lastSeen)); renderBell(); });
$$("#theme-seg button").forEach((b) => b.addEventListener("click", () => { S.settings.theme = b.dataset.v; applySettings(); }));
$$("#poll-seg button").forEach((b) => b.addEventListener("click", () => { S.settings.poll = +b.dataset.v; applySettings(); }));
$("#reduce-motion").addEventListener("change", (e) => { S.settings.reduceMotion = e.target.checked; applySettings(); });

/* deep links: #overview · #idle · #tasks · #activity · #task/<id> · #resource/<system>[/history|/connection] · #launch[/<system>] · #limits */
function applyHash() {
  const h = decodeURIComponent(location.hash.slice(1));
  if (!h) return;
  const [head, ...rest] = h.split("/");
  if (head === "resource" && rest[0]) { openResource(rest[0]); if (rest[1]) { S.resourceTab = rest[1]; $$("#resource-tabs button").forEach((b) => b.classList.toggle("is-active", b.dataset.t === rest[1])); renderResource(); } }
  else if (head === "task" && rest[0]) openTask(rest[0]);
  else if (head === "launch") openLaunch(rest[0] ? { system: rest[0], partition: rest[1] || null } : null);
  else if (head === "limits") openPolicy();
  else if (TITLES[head]) switchView(head);
}
window.addEventListener("hashchange", applyHash);
const qTheme = new URLSearchParams(location.search).get("theme");
applySettings();
if (qTheme) document.documentElement.setAttribute("data-theme", qTheme);   // preview override, not persisted
loadConfig().then(refresh).then(applyHash).catch((e) => { $("#refresh-text").textContent = `offline: ${e.message}`; });
