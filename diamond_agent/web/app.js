/* Diamond Agent — the operator's monitor and rule console over diamond-agent-core.
   Vanilla JS over the local API. One home page — Channels (one URL + one rule
   set per conversation of agents; their tasks and activity live inside) over
   Machines (the federation and its rules) — and a page per channel and per
   machine. The UI submits nothing: agents do, through their channel URL. */

const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => Array.from(root.querySelectorAll(s));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ------------------------------------------------------------ icons */
const ICONS = {
  grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  list: '<path d="M8 6h13M8 12h13M8 18h13"/><circle cx="3.5" cy="6" r="1"/><circle cx="3.5" cy="12" r="1"/><circle cx="3.5" cy="18" r="1"/>',
  activity: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  bell: '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10 21a2 2 0 0 0 4 0"/>',
  server: '<rect x="2" y="3" width="20" height="7" rx="2"/><rect x="2" y="14" width="20" height="7" rx="2"/><path d="M6 6.5h.01M6 17.5h.01"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  chevron: '<path d="m9 6 6 6-6 6"/>',
  layers: '<path d="m12 2 10 5-10 5L2 7z"/><path d="m2 12 10 5 10-5"/><path d="m2 17 10 5 10-5"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  check: '<path d="m5 12 5 5L20 7"/>',
  shield: '<path d="M12 3 4 6v6c0 5 3.4 7.9 8 9 4.6-1.1 8-4 8-9V6z"/><path d="m9 12 2 2 4-4"/>',
  copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/>',
  link: '<path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1"/><path d="M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1"/>',
};
const icon = (n) => `<span class="ico"><svg viewBox="0 0 24 24">${ICONS[n]}</svg></span>`;
const mountIcons = (root = document) => $$("[data-icon]", root).forEach((el) => { el.innerHTML = `<svg viewBox="0 0 24 24">${ICONS[el.dataset.icon]}</svg>`; });

/* ------------------------------------------------------------ state */
const POLL_MS = 3000;
const S = {
  auth: null, resources: [], providers: [], tasks: [], events: [], history: [], channels: [], machinePolicy: null,
  takenAt: null, view: "home", selectedSystem: null, resourceTab: "overview",
  channelId: null, channelTab: "tasks", detailTask: null, detailTab: "overview", taskFilter: "active", eventFilter: "all",
  policy: null, sheetChannel: null,
  lastSeen: Number(localStorage.getItem("da.lastSeen") || 0),
};

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) { let d = res.statusText; try { const j = await res.json(); d = (j.detail && j.detail.message) || j.detail || d; } catch {} throw new Error(d); }
  return res.json();
}
const send = (method, path, body) => api(path, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
const post = (path, body) => send("POST", path, body);
async function copyText(text, btn) { try { await navigator.clipboard.writeText(text); if (btn) { const old = btn.innerHTML; btn.innerHTML = `${icon("check")}Copied`; setTimeout(() => { btn.innerHTML = old; }, 1500); } } catch { prompt("Copy:", text); } }

/* ------------------------------------------------------------ helpers */
const level = (u) => u == null ? "off" : u < 0.01 ? 0 : u < 0.5 ? 1 : u < 0.85 ? 2 : 3;
const pct = (u) => `${Math.round(Math.min(1, Math.max(0, u || 0)) * 100)}%`;
const meter = (u) => `<span class="meter"><span class="meter__track"><span class="meter__fill lvl-${level(u)}" style="width:${pct(u)}"></span></span><span class="meter__pct">${u == null ? "—" : pct(u)}</span></span>`;
const fmtTime = (ts) => ts ? new Date(ts).toLocaleTimeString([], { hour12: false }) : "";
const ago = (ts) => { if (!ts) return "—"; const s = Math.max(0, (Date.now() - new Date(ts)) / 1000); return s < 5 ? "just now" : s < 60 ? `${Math.round(s)}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : s < 86400 ? `${(s / 3600).toFixed(1)}h ago` : `${Math.round(s / 86400)}d ago`; };
const fmtWall = (m) => m == null ? "—" : m % 60 === 0 ? `${m / 60}h` : m > 90 ? `${(m / 60).toFixed(1)}h` : `${m}m`;
const sizingText = (s) => s ? `${s.nodes}n · ${s.gpus}g · ${s.cpus}c · ${fmtWall(s.walltime_minutes)}` : "—";
const hwText = (r) => r.gpu_type ? `${r.gpu_type} ×${r.gpus_per_node} · ${r.gpu_mem_gb} GB/GPU · ${r.cpus_per_node}c` : `${r.cpus_per_node} cores · ${r.mem_per_node_gb} GB`;
const freeOf = (r) => r.gpu_type ? [r.gpus_free ?? r.nodes_idle * r.gpus_per_node, "GPU"] : [r.cpus_free ?? r.nodes_idle * r.cpus_per_node, "cores"];
const utilOf = (r) => { const usable = Math.max(r.nodes_idle + r.nodes_mixed + r.nodes_allocated, 0); if (r.gpu_type && r.gpus_free != null) { const cap = Math.max(usable * r.gpus_per_node, r.gpus_free); return cap ? (cap - r.gpus_free) / cap : 0; } if (!r.gpu_type && r.cpus_free != null) { const cap = Math.max(usable * r.cpus_per_node, r.cpus_free); return cap ? (cap - r.cpus_free) / cap : 0; } const t = usable + r.nodes_down; return t ? (r.nodes_allocated + r.nodes_mixed) / t : 0; };
const PROVIDER_LABEL = { globus_hpc: "HPC · Globus Compute", ssh: "SSH server", cloud: "Cloud" };
const healthPill = (h) => `<span class="pill pill--${h}">${esc(h)}</span>`;
const TASK_CLASS = { COMPLETED: "st-good", RUNNING: "st-accent", PENDING: "st-warning", SUBMITTED: "st-muted", FAILED: "st-critical", CANCELLED: "st-muted" };
const statusLabel = (s) => `<span class="status ${TASK_CLASS[s]}"><span class="dot"></span>${esc(s)}</span>`;
const ACTIVE = ["PENDING", "RUNNING", "SUBMITTED"];
const BAD = ["JOB_FAILED", "JOB_SUBMIT_FAILED", "POLICY_DENIED", "AGENT_GAVE_UP"];
const NOTABLE = (k) => k.startsWith("AGENT") || k.startsWith("POLICY") || k.startsWith("CHANNEL") || BAD.includes(k);
const eventChip = (k) => `<span class="chip ${k.startsWith("AGENT") ? "chip--agent" : BAD.includes(k) ? "chip--bad" : (k.startsWith("POLICY") || k.startsWith("CHANNEL")) ? "chip--policy" : ""}">${esc(k)}</span>`;
const systemOf = (name) => S.resources.find((s) => s.name === name);
const channelOf = (id) => S.channels.find((c) => c.id === id);
const channelUrl = (c) => `${location.origin}${c.path}`;
const eventRow = (e) => `<div class="event"><span class="ts">${fmtTime(e.ts)}</span>${eventChip(e.kind)}<span class="msg">${esc(e.message)}${e.task_id ? `<span class="task-ref" data-task="${esc(e.task_id)}" data-channel="${esc(e.channel || "")}">[${esc(e.task_id.slice(0, 8))}]</span>` : ""}</span></div>`;
/* what the operator needs to know about a channel at a glance */
function channelFacts(c) {
  const tasks = S.tasks.filter((t) => t.channel === c.id);
  const events = S.events.filter((e) => e.channel === c.id);
  const keys = Object.keys(c.allowed);
  return {
    running: tasks.filter((t) => t.status === "RUNNING").length,
    pending: tasks.filter((t) => ["PENDING", "SUBMITTED"].includes(t.status)).length,
    failed: tasks.filter((t) => t.status === "FAILED").length,
    done: tasks.filter((t) => t.status === "COMPLETED").length,
    total: tasks.length, queues: keys.length,
    machines: [...new Set(keys.map((k) => k.split(":")[0]))],
    last: events[0] ? events[0].ts : (tasks[0] ? tasks[0].created_at : null),
  };
}
const setupSnippet = (c) => ({
  shell: `export DIAMOND_AGENT_URL=${channelUrl(c)}\nuv run --no-sync diamond-agent-core status`,
  mcp: `claude mcp add diamond-agent-core -e DIAMOND_AGENT_URL=${channelUrl(c)} -- uv run --no-sync python -m diamond_agent.core.mcp_server`,
});

/* ------------------------------------------------------------ data */
async function fetchAll() {
  const [res, tasks, events, hist, auth, pol] = await Promise.all([api("/api/resources"), api("/api/tasks"), api("/api/events?limit=300"), api("/api/federation/history?limit=60"), api("/api/auth/session"), api("/api/policy")]);
  S.resources = res.systems; S.providers = res.providers; S.takenAt = res.taken_at; S.tasks = tasks; S.events = events; S.history = hist; S.auth = auth; S.machinePolicy = pol;
  try { S.channels = await api("/ui/channels"); } catch { S.channels = []; }
  renderAuth();
}

/* ------------------------------------------------------------ sidebar (an index) */
function renderSidebar() {
  $("#channel-list").innerHTML = S.channels.map((c) => {
    const f = channelFacts(c);
    const meta = f.running || f.pending ? `${f.running} running · ${f.pending} pending`
      : `${f.queues} queue${f.queues === 1 ? "" : "s"} · ${f.total ? `${f.total} task${f.total === 1 ? "" : "s"}` : "no tasks yet"}`;
    return `<button class="resource-row ${S.view === "channel" && S.channelId === c.id ? "is-selected" : ""}" data-channel="${esc(c.id)}">
      <span class="dot ${f.running ? "dot--online" : ""}"></span>
      <span class="resource-row__content"><span class="resource-row__title">${esc(c.name)}</span><span class="resource-row__meta">${esc(meta)}</span></span>
      <span class="resource-row__chevron">${icon("chevron")}</span></button>`;
  }).join("");
  $$("#channel-list [data-channel]").forEach((b) => b.addEventListener("click", () => openChannel(b.dataset.channel)));
  $("#resource-list").innerHTML = S.resources.map((s) => {
    const mine = s.user_running_jobs + s.user_pending_jobs;
    const meta = s.partitions.length ? `${s.gpus_total ? `${s.gpus_free} GPU free` : `${s.cpus_free.toLocaleString()} cores free`} · ${s.pending_jobs} queued` : "no live queues yet";
    return `<button class="resource-row ${S.view === "machine" && S.selectedSystem === s.name ? "is-selected" : ""}" data-system="${esc(s.name)}">
      <span class="dot dot--${s.health}"></span>
      <span class="resource-row__content"><span class="resource-row__title">${esc(s.name)}</span><span class="resource-row__meta">${esc(meta)}</span></span>
      ${mine ? `<span class="own-dot" title="your jobs here">${mine}</span>` : ""}
      <span class="resource-row__chevron">${icon("chevron")}</span></button>`;
  }).join("") || `<div class="empty-copy">No machines yet</div>`;
  $$("#resource-list [data-system]").forEach((b) => b.addEventListener("click", () => openMachine(b.dataset.system)));
  const online = S.resources.filter((s) => s.health === "online").length;
  $("#online-count").textContent = `${online}/${S.resources.length}`;
  $("#count-channels").textContent = S.channels.length;
  $("#refresh-text").textContent = S.takenAt ? `snapshot ${ago(S.takenAt)}` : "waiting for first snapshot";
}

/* ------------------------------------------------------------ home */
const sparkline = (vals) => {
  const v = vals.filter((x) => x != null); if (v.length < 2) return "";
  const W = 120, H = 36, P = 3, lo = Math.min(...v), hi = Math.max(...v), span = hi - lo || 1;
  const pts = v.map((y, i) => [P + (i * (W - 2 * P)) / (v.length - 1), H - P - ((y - lo) / span) * (H - 2 * P)]);
  const d = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
  const last = pts[pts.length - 1];
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" aria-hidden="true"><path class="area" d="${d} L${last[0].toFixed(1)},${H - P} L${pts[0][0].toFixed(1)},${H - P} Z"/><path class="line" d="${d}"/><circle cx="${last[0].toFixed(1)}" cy="${last[1].toFixed(1)}" r="2.5"/></svg>`;
};
function statStrip(el, stats) {
  el.innerHTML = stats.map((k) => k.hero
    ? `<div class="stat stat--hero"><div><strong>${esc(k.value)}${k.unit ? `<em>${esc(k.unit)}</em>` : ""}</strong><small>${esc(k.label)}</small></div>${sparkline(k.spark)}</div>`
    : `<div class="stat"><span class="stat__icon stat__icon--${k.tone}">${icon(k.icon)}</span><div><strong>${esc(k.value)}</strong><small>${esc(k.label)}</small></div></div>`).join("");
}
const emptyState = (ic, title, sub) => `<div class="empty-state">${icon(ic)}<strong>${esc(title)}</strong><small>${esc(sub)}</small></div>`;
function renderHome() {
  const online = S.resources.filter((s) => s.health === "online").length;
  const gpusFree = S.resources.reduce((a, s) => a + s.gpus_free, 0), gpusTotal = S.resources.reduce((a, s) => a + s.gpus_total, 0);
  const running = S.tasks.filter((t) => t.status === "RUNNING").length, pending = S.tasks.filter((t) => ["PENDING", "SUBMITTED"].includes(t.status)).length;
  statStrip($("#stat-strip"), [
    { hero: true, value: gpusFree.toLocaleString(), unit: `of ${gpusTotal.toLocaleString()} GPUs free`, label: `across ${S.resources.length} machine${S.resources.length === 1 ? "" : "s"} · last ${S.history.length} snapshots`, spark: S.history.map((h) => h.aggregates.gpus_idle) },
    { icon: "server", value: `${online} / ${S.resources.length}`, label: "machines online", tone: online === S.resources.length ? "good" : "warning" },
    { icon: "activity", value: `${running} / ${pending}`, label: "tasks running / pending", tone: running ? "good" : "neutral" },
  ]);
  $("#channel-cards").innerHTML = S.channels.map(channelCard).join("");
  $$("#channel-cards [data-open-channel]").forEach((el) => el.addEventListener("click", (e) => { if (e.target.closest("[data-copy-url]")) return; openChannel(el.dataset.openChannel); }));
  $$("#channel-cards [data-copy-url]").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); copyText(channelUrl(channelOf(b.dataset.copyUrl)), b); }));
  $("#fleet-grid").innerHTML = S.resources.map(fleetCard).join("") || emptyState("server", "No machines yet", "Machines are declared in sites.live.yaml (make init-sites).");
  $$("#fleet-grid [data-open]").forEach((b) => b.addEventListener("click", () => openMachine(b.dataset.open)));
}
function channelCard(c) {
  const f = channelFacts(c);
  return `<article class="chan-card" data-open-channel="${esc(c.id)}" tabindex="0">
    <div class="chan-card__top"><div><h3>${esc(c.name)}</h3>${c.description ? `<small>${esc(c.description)}</small>` : ""}</div>
      ${f.running ? `<span class="pill pill--online">${f.running} running</span>` : `<span class="pill">idle</span>`}</div>
    <div class="chan-card__stats">
      <div class="${f.running ? "is-good" : ""}"><strong>${f.running}</strong><small>running</small></div>
      <div><strong>${f.pending}</strong><small>pending</small></div>
      <div><strong>${f.failed}</strong><small>failed</small></div>
      <div><strong>${f.done}</strong><small>done</small></div></div>
    <div class="chan-card__foot"><span>${esc(f.machines.join(", ") || "no machine")} · ${f.queues} queue${f.queues === 1 ? "" : "s"}${f.last ? ` · active ${ago(f.last)}` : ""}</span>
      <span class="row-actions"><button class="button button--small" data-copy-url="${esc(c.id)}" title="Copy this channel's URL for an agent">${icon("copy")}URL</button></span></div>
  </article>`;
}
function fleetCard(s) {
  const using = S.channels.filter((c) => Object.keys(c.allowed).some((k) => k.startsWith(s.name + ":"))).length;
  const rows = s.partitions.map((r) => {
    const [free, unit] = freeOf(r); const u = r.endpoint_health === "online" ? utilOf(r) : null;
    return `<div class="part-row">
      <div class="part-row__name lvl-${level(u)}"><strong>${esc(r.partition)}</strong><small>${esc(hwText(r))}</small></div>
      ${meter(u)}
      <div class="num num--free"><strong>${free.toLocaleString()}</strong><small>${unit} free</small></div>
      <div class="num"><strong>${r.running_jobs} / ${r.pending_jobs}</strong><small>run / pend</small></div></div>`;
  }).join("");
  return `<article class="fleet-card ${s.health === "offline" ? "fleet-card--offline" : ""}">
    <button class="fleet-card__header" data-open="${esc(s.name)}">
      <span class="dot dot--${s.health}"></span>
      <span class="fleet-card__title"><strong>${esc(s.name)}</strong><small>${esc(PROVIDER_LABEL[s.provider] || s.provider)}${s.tags.length ? " · " + esc(s.tags.join(", ")) : ""}</small></span>
      ${healthPill(s.health)}
      <span class="muted">${icon("chevron")}</span></button>
    ${rows ? `<div class="col-head"><span>Queue</span><span>Utilization</span><span>Free</span><span>Queue</span></div><div class="fleet-card__rows">${rows}</div>`
           : `<div class="fleet-card__rows"><div class="empty-copy">No live queues yet — waiting for the first probe.</div></div>`}
    <div class="fleet-card__foot"><span>${s.gpus_total ? `${s.gpus_free} of ${s.gpus_total} GPUs free` : `${s.cpus_free.toLocaleString()} cores free`} · ${s.user_running_jobs + s.user_pending_jobs} mine · ${using} channel${using === 1 ? "" : "s"}</span><span>${s.partitions[0] ? ago(s.partitions[0].timestamp) : ""}</span></div>
  </article>`;
}

/* ------------------------------------------------------------ machine page */
function openMachine(name, tab) { S.selectedSystem = name; S.resourceTab = tab || "overview"; $$("#resource-tabs button").forEach((b) => b.classList.toggle("is-active", b.dataset.t === S.resourceTab)); switchView("machine"); }
async function renderMachine() {
  const s = systemOf(S.selectedSystem);
  if (!s) { $("#resource-head").innerHTML = `<div class="muted">Unknown machine</div>`; $("#resource-body").innerHTML = ""; return; }
  $("#eyebrow").textContent = `Machine · ${PROVIDER_LABEL[s.provider] || s.provider}${s.location ? " · " + s.location : ""}`;
  $("#page-title").textContent = s.name;
  const using = S.channels.filter((c) => Object.keys(c.allowed).some((k) => k.startsWith(s.name + ":")));
  $("#resource-head").innerHTML = `<div><div class="pills">${healthPill(s.health)}${s.tags.map((t) => `<span class="pill pill--plain">${esc(t)}</span>`).join("")}</div>
      <div class="hint" style="margin-top:6px">${using.length ? `Used by ${using.map((c) => `<b>${esc(c.name)}</b>`).join(", ")}` : "No channel uses this machine yet"}</div></div>
    <div class="row-actions"><button class="button" id="btn-limits-here" title="Max nodes per job on each queue of this machine">${icon("shield")}Machine rules</button></div>`;
  $("#btn-limits-here").addEventListener("click", openPolicy);
  if (S.resourceTab === "overview") {
    const cards = s.partitions.map((r) => { const [free, unit] = freeOf(r); const u = r.endpoint_health === "online" ? utilOf(r) : null; const lim = s.limits[r.partition]; const chans = S.channels.filter((c) => c.allowed[`${s.name}:${r.partition}`] != null); return `
      <article class="part-card"><div class="part-card__top"><div><h3>${esc(r.partition)}${lim.blocked ? ` <span class="chip chip--bad">blocked</span>` : ""}</h3><div class="sub">${esc(hwText(r))} · ${esc(r.node_arch)} · account ${esc(r.account)} · max ${lim.max_nodes_per_job} nodes/job</div></div></div>
        <div class="metric"><small><span>Utilization</span><span>${free.toLocaleString()} ${unit} free</span></small>${meter(u)}</div>
        <div class="stat-row"><span><small>Nodes idle / mixed</small><strong>${r.nodes_idle} / ${r.nodes_mixed}</strong></span><span><small>Allocated / down</small><strong>${r.nodes_allocated} / ${r.nodes_down}</strong></span><span><small>Queue run / pend</small><strong>${r.running_jobs} / ${r.pending_jobs}</strong></span><span><small>Mine · max wall</small><strong>${r.user_running_jobs + r.user_pending_jobs} · ${fmtWall(r.max_walltime_minutes)}</strong></span></div>
        <div class="hint" style="margin-top:8px">${chans.length ? `channels: ${chans.map((c) => `${esc(c.name)} (≤${c.allowed[`${s.name}:${r.partition}`]}n)`).join(", ")}` : "in no channel"}</div></article>`; }).join("");
    $("#resource-body").innerHTML = `<div class="part-cards">${cards || `<div class="empty-copy">No live queues yet — waiting for the first probe.</div>`}</div>`;
  } else if (S.resourceTab === "history") {
    try { S.history = await api("/api/federation/history?limit=240"); } catch { S.history = []; }
    const parts = s.partitions.map((r) => r.partition);
    const cells = (p) => S.history.map((h) => { const rec = h.records.find((r) => r.system === s.name && r.partition === p); if (!rec) return `<td></td>`; if (rec.health !== "online") return `<td class="hoff" title="${esc(fmtTime(h.taken_at))} · offline"></td>`; const u = rec.utilization; const c = u < 0.01 ? "" : u < 0.25 ? "h1" : u < 0.5 ? "h2" : u < 0.75 ? "h3" : u < 0.92 ? "h4" : "h5"; return `<td class="${c}" title="${esc(fmtTime(h.taken_at))} · ${pct(u)} busy · ${rec.free} free · ${rec.pending} pending"></td>`; }).join("");
    $("#resource-body").innerHTML = `<div class="panel"><div class="panel__head"><div><strong>Queue history</strong><small>one cell per snapshot (${S.history.length} kept) · busy fraction of the gating resource</small></div></div>
      <div class="heat"><table>${parts.map((p) => `<tr><th>${esc(p)}</th>${cells(p)}</tr>`).join("")}</table></div>
      <div class="heat__legend">idle <i style="background:var(--surface-2)"></i><i style="background:var(--seq-1)"></i><i style="background:var(--seq-2)"></i><i style="background:var(--seq-3)"></i><i style="background:var(--seq-4)"></i><i style="background:var(--seq-5)"></i> saturated · <i style="background:repeating-linear-gradient(135deg, var(--critical) 0 2px, transparent 2px 5px)"></i> offline</div></div>`;
  } else {
    const p = S.providers.find((x) => x.kind === s.provider);
    $("#resource-body").innerHTML = `<div class="detail-grid">
      <div class="kv"><small>Provider</small><strong>${esc(PROVIDER_LABEL[s.provider] || s.provider)}</strong><div class="hint">${esc(p.detail)} · status ${esc(p.status)}</div></div>
      <div class="kv"><small>Endpoint</small><strong class="mono">${esc(s.endpoint || "—")}</strong></div>
      <div class="kv"><small>Declared queues</small><strong>${esc(s.declared_partitions.join(", ") || "—")}</strong></div>
      <div class="kv"><small>Container runtime</small><strong>${esc((s.partitions[0] || {}).container_runtime || "—")}</strong></div></div>
      <div class="hint">Declared in the site catalog (<code>sites.live.yaml</code>): hardware facts, accounts, per-queue QOS and site dialect switches. Live queue state comes from the probe every tick.</div>`;
  }
}

/* ------------------------------------------------------------ channel page */
function openChannel(id, tab) { S.channelId = id; S.channelTab = tab || "tasks"; S.detailTask = null; $$("#channel-tabs button").forEach((b) => b.classList.toggle("is-active", b.dataset.t === S.channelTab)); switchView("channel"); }
function openTaskIn(channelId, taskId) { S.channelId = channelId; S.channelTab = "tasks"; S.detailTask = taskId; const t = S.tasks.find((x) => x.task_id === taskId); if (t && !ACTIVE.includes(t.status)) S.taskFilter = "all"; $$("#channel-tabs button").forEach((b) => b.classList.toggle("is-active", b.dataset.t === "tasks")); switchView("channel"); }
async function renderChannel() {
  const c = channelOf(S.channelId);
  if (!c) { $("#channel-head").innerHTML = `<div class="muted">This channel no longer exists.</div>`; $("#channel-body").innerHTML = ""; return; }
  const f = channelFacts(c);
  $("#eyebrow").textContent = "Channel"; $("#page-title").textContent = c.name;
  const snip = setupSnippet(c);
  $("#channel-head").innerHTML = `<div><div class="pills"><span class="pill pill--provider">${icon("layers")} ${f.queues} queue${f.queues === 1 ? "" : "s"} on ${esc(f.machines.join(", ") || "no machine")}</span>${c.id === "default" ? `<span class="pill pill--plain" title="Created with the core so agents can be handed a URL before any channel is shaped">default · cannot be deleted</span>` : ""}</div>
      ${c.description ? `<div class="hint" style="margin-top:6px">${esc(c.description)}</div>` : ""}
      <div class="url-box">${icon("link")}<code>${esc(channelUrl(c))}</code><small>the agent's DIAMOND_AGENT_URL</small><button class="button button--small" id="btn-copy-url">${icon("copy")}Copy URL</button></div>
      <details class="setup-box"><summary>How to hand this channel to an agent</summary>
        <pre>${esc(snip.shell)}<button class="button button--small" data-copy-snippet="shell">${icon("copy")}Copy</button></pre>
        <pre>${esc(snip.mcp)}<button class="button button--small" data-copy-snippet="mcp">${icon("copy")}Copy</button></pre></details></div>
    <div class="row-actions"><button class="button" id="btn-edit-channel">${icon("shield")}Edit rules</button>${c.id === "default" ? "" : `<button class="button button--danger" id="btn-delete-channel">Delete</button>`}</div>`;
  $("#btn-copy-url").addEventListener("click", (e) => copyText(channelUrl(c), e.currentTarget));
  $$("[data-copy-snippet]").forEach((b) => b.addEventListener("click", () => copyText(snip[b.dataset.copySnippet], b)));
  $("#btn-edit-channel").addEventListener("click", () => openChannelSheet(c));
  if (c.id !== "default") $("#btn-delete-channel").addEventListener("click", async () => { if (!confirm(`Delete channel "${c.name}"?\n\nIts URL stops working and its ${f.running + f.pending} active task(s) are cancelled.`)) return; try { await api(`/ui/channels/${c.id}`, { method: "DELETE" }); await fetchAll(); switchView("home"); } catch (e) { alert(`Not deleted: ${e.message}`); } });
  const body = $("#channel-body");
  if (S.channelTab === "tasks") {
    body.innerHTML = `<div class="panel">
        <div class="panel__head"><div><strong>Tasks</strong><small id="tasks-caption"></small></div>
          <div class="segmented" id="task-filter"><button data-v="active" ${S.taskFilter === "active" ? 'class="is-active"' : ""}>Active</button><button data-v="all" ${S.taskFilter === "all" ? 'class="is-active"' : ""}>All</button></div></div>
        <div class="table-wrap"><table class="table" id="tasks-table"><thead><tr><th>Task</th><th>Status</th><th>Queue</th><th>Sizing</th><th>Job</th><th>Attempts</th><th></th></tr></thead><tbody></tbody></table></div></div>
      <div class="panel hidden" id="task-detail">
        <div class="panel__head"><div><strong id="detail-title">Task</strong><small id="detail-sub"></small></div><div class="row-actions" id="detail-actions"></div></div>
        <div class="tabs" id="detail-tabs"><button data-t="overview" ${S.detailTab === "overview" ? 'class="is-active"' : ""}>Overview</button><button data-t="evaluation" ${S.detailTab === "evaluation" ? 'class="is-active"' : ""}>Queue evaluation</button><button data-t="timeline" ${S.detailTab === "timeline" ? 'class="is-active"' : ""}>Timeline</button></div>
        <div id="detail-body"></div></div>`;
    $$("#task-filter button").forEach((b) => b.addEventListener("click", () => { S.taskFilter = b.dataset.v; $$("#task-filter button").forEach((x) => x.classList.toggle("is-active", x === b)); renderTasks(c); }));
    $$("#detail-tabs button").forEach((b) => b.addEventListener("click", () => { S.detailTab = b.dataset.t; $$("#detail-tabs button").forEach((x) => x.classList.toggle("is-active", x === b)); renderTaskDetail(); }));
    renderTasks(c);
  } else if (S.channelTab === "activity") {
    body.innerHTML = `<div class="panel"><div class="panel__head"><div><strong>Activity</strong><small>newest first</small></div>
        <div class="segmented" id="event-filter"><button data-v="all" ${S.eventFilter === "all" ? 'class="is-active"' : ""}>All</button><button data-v="agent" ${S.eventFilter === "agent" ? 'class="is-active"' : ""}>Agent actions</button><button data-v="bad" ${S.eventFilter === "bad" ? 'class="is-active"' : ""}>Failures & refusals</button></div></div>
      <div class="event-feed" id="event-feed"></div></div>`;
    $$("#event-filter button").forEach((b) => b.addEventListener("click", () => { S.eventFilter = b.dataset.v; $$("#event-filter button").forEach((x) => x.classList.toggle("is-active", x === b)); renderActivity(c); }));
    renderActivity(c);
  } else {
    const mp = S.machinePolicy;
    const keys = Object.keys(c.allowed);
    const nh = (v) => v == null ? "∞" : `${(+v).toFixed(1)} h`;
    const bm = c.budget.machines, bq = c.budget.queues;
    const machineRows = f.machines.map((sys) => { const sr = c.rules.systems[sys]; const b = bm[sys]; return `<tr><td><b>${esc(sys)}</b></td><td>${esc(sr.account || "catalog default")} <small class="muted">(queues may override)</small></td><td>${nh(b.cap)}</td><td>${nh(b.machine_budget)}</td><td>${b.used.toFixed(1)} h</td><td><b>${nh(b.left)}</b></td></tr>`; }).join("");
    const queueRows = keys.map((k) => { const [sys, part] = k.split(":"); const sr = c.rules.systems[sys]; const pr = sr.partitions ? sr.partitions[part] : {}; const machineCap = mp.partition_limits[k] ?? mp.max_nodes_per_job; const rec = (systemOf(sys) || { partitions: [] }).partitions.find((r) => r.partition === part); const b = bq[k]; const acct = pr.account ? `${esc(pr.account)} <small class="muted">(queue)</small>` : sr.account ? `${esc(sr.account)} <small class="muted">(machine)</small>` : rec ? `${esc(rec.account)} <small class="muted">(catalog)</small>` : "catalog default"; return `<tr><td>${esc(sys)}</td><td><b>${esc(part)}</b><br><small class="muted">${rec ? esc(hwText(rec)) : ""}</small></td><td>${acct}</td><td><b>${c.allowed[k]}</b> <small class="muted">of ${machineCap}</small></td><td>${nh(b.cap)}</td><td>${b.used.toFixed(1)} h</td><td><b>${nh(b.left)}</b></td></tr>`; }).join("");
    body.innerHTML = `<div class="panel"><div class="panel__head"><div><strong>Channel rules</strong><small>inside the machine rules · node-hours = nodes × wall time, requested while a job runs, elapsed once it ends</small></div></div>
      <div class="hint" style="margin-bottom:12px">Max pending jobs per machine: <b>${esc(c.rules.max_user_pending_per_system ?? mp.max_user_pending_per_system)}</b> (machine rule ${esc(mp.max_user_pending_per_system)})</div>
      <div class="table-wrap" style="margin-bottom:14px"><table class="table"><thead><tr><th>Machine</th><th>Account</th><th>Node-h cap</th><th>Machine budget</th><th>Used</th><th>Left</th></tr></thead><tbody>${machineRows || `<tr><td colspan="6"><div class="empty-copy">No machines.</div></td></tr>`}</tbody></table></div>
      <div class="table-wrap"><table class="table"><thead><tr><th>Machine</th><th>Queue</th><th>Account</th><th>Max nodes / job</th><th>Node-h cap</th><th>Used</th><th>Left</th></tr></thead><tbody>${queueRows || `<tr><td colspan="7"><div class="empty-copy">No queues — agents in this channel can submit nothing until you enable some.</div></td></tr>`}</tbody></table></div></div>`;
  }
}

/* ------------------------------------------------------------ tasks (inside a channel) */
function renderTasks(c) {
  const f = channelFacts(c);
  const list = S.tasks.filter((t) => t.channel === c.id && (S.taskFilter === "all" || ACTIVE.includes(t.status)));
  $("#tasks-caption").textContent = `${f.running} running · ${f.pending} pending · ${f.failed} failed · ${f.done} done`;
  $("#tasks-table tbody").innerHTML = list.map((t) => `<tr class="is-clickable ${t.task_id === S.detailTask ? "is-chosen" : ""}" data-row="${esc(t.task_id)}">
      <td><b>${esc(t.name)}</b><br><small class="muted mono">${esc(t.task_id)}</small></td>
      <td>${statusLabel(t.status)}</td>
      <td>${esc(t.target_system)}<br><small class="muted">${esc(t.target_partition)}</small></td>
      <td>${sizingText(t.sizing)}</td><td class="mono">${esc(t.job_id || "—")}</td><td>${t.attempts}</td>
      <td>${ACTIVE.includes(t.status) ? `<button class="button button--small button--danger" data-cancel="${esc(t.task_id)}">Cancel</button>` : ""}</td></tr>`).join("")
    || `<tr><td colspan="7">${emptyState("list", S.taskFilter === "active" ? "No active tasks" : "No tasks yet", "Agents submit through this channel's URL.")}</td></tr>`;
  const tb = $("#tasks-table tbody");
  $$("[data-row]", tb).forEach((tr) => tr.addEventListener("click", (e) => { if (e.target.closest("[data-cancel]")) return; S.detailTask = S.detailTask === tr.dataset.row ? null : tr.dataset.row; history.replaceState(null, "", "#" + hashFor()); renderTasks(c); }));
  $$("[data-cancel]", tb).forEach((b) => b.addEventListener("click", async (e) => { e.stopPropagation(); const t = S.tasks.find((x) => x.task_id === b.dataset.cancel); if (!confirm(`Cancel "${t ? t.name : b.dataset.cancel}"? Its batch job is cancelled too.`)) return; await post(`/api/tasks/${b.dataset.cancel}/cancel`); refresh(); }));
  $("#task-detail").classList.toggle("hidden", !S.detailTask);
  if (S.detailTask) renderTaskDetail();
}
async function renderTaskDetail() {
  let t; try { t = await api(`/api/tasks/${S.detailTask}`); } catch { return; }
  if (!$("#detail-title")) return;
  $("#detail-title").textContent = t.spec.name;
  $("#detail-sub").textContent = `${t.task_id} · created ${fmtTime(t.created_at)} · ${t.target_system}:${t.target_partition}`;
  $("#detail-actions").innerHTML = `<button class="button button--small" id="btn-close-detail">Close</button>`;
  $("#btn-close-detail").addEventListener("click", () => { S.detailTask = null; history.replaceState(null, "", "#" + hashFor()); renderTasks(channelOf(S.channelId)); });
  const env = t.spec.envelope;
  const kv = (l, v) => `<div class="kv"><small>${esc(l)}</small><strong>${v}</strong></div>`;
  let body = "";
  if (S.detailTab === "overview") {
    body = `<div class="detail-grid">
      ${kv("Status", statusLabel(t.status))}
      ${kv("Sizing", esc(sizingText(t.placement && t.placement.sizing)))}
      ${kv("Account", esc(t.placement ? t.placement.account : (t.evaluation ? t.evaluation.account : "—")))}
      ${kv("Job", esc(t.job_id || "—"))}
      ${kv("Attempts", `${t.attempts}`)}</div>
      <div class="kv" style="margin-bottom:12px"><small>Envelope</small><span class="mono">gpu ${env.gpu.min_count}–${env.gpu.max_count}${env.gpu.min_memory ? " ≥" + esc(env.gpu.min_memory) + "/GPU" : ""} · cpu ${env.cpu.min_count}–${env.cpu.max_count} · mem ${esc(env.memory.min || "?")}–${esc(env.memory.max || "?")} · wall ${esc(env.walltime.min)}–${esc(env.walltime.max)} · accel [${env.compatibility.accelerators.map(esc).join(", ") || "any"}]</span></div>
      <div class="kv"><small>Command</small><span class="mono">${esc(t.spec.command)}</span></div>`;
  } else if (S.detailTab === "evaluation") {
    const ev = t.evaluation;
    if (!ev) body = `<div class="muted">No evaluation recorded.</div>`;
    else body = `<div class="detail-grid">
        ${kv("Feasible", ev.feasible ? `<span class="status st-good"><span class="dot"></span>yes</span>` : `<span class="status st-critical"><span class="dot"></span>no</span>`)}
        ${kv("Sizing", esc(sizingText(ev.sizing)))}
        ${kv("Est. wait at submit", ev.est_wait_minutes != null ? `${ev.est_wait_minutes.toFixed(0)}m` : "—")}
        ${kv("Free at submit", `${freeOf(ev.record)[0].toLocaleString()} ${freeOf(ev.record)[1]} · ${ev.record.pending_jobs} pending`)}</div>
      <div class="verdicts">${ev.verdicts.map((v) => `<span class="chip ${v.passed ? "chip--pass" : "chip--fail"}" title="${esc(v.detail)}">${v.passed ? "✓" : "✕"} ${esc(v.rule)} · ${esc(v.detail)}</span>`).join("")}</div>`;
  } else {
    body = `<div class="timeline">${t.timeline.map((e) => `<div class="event"><span class="ts">${fmtTime(e.ts)}</span>${eventChip(e.kind)}<span class="msg">${esc(e.message)}</span></div>`).join("")}</div>`;
  }
  $("#detail-body").innerHTML = body;
}
function renderActivity(c) {
  const list = S.events.filter((e) => e.channel === c.id).filter((e) => S.eventFilter === "all" || (S.eventFilter === "agent" && e.kind.startsWith("AGENT")) || (S.eventFilter === "bad" && BAD.includes(e.kind)));
  $("#event-feed").innerHTML = list.map(eventRow).join("") || emptyState("activity", "No activity yet in this channel", "Submissions, dispatches, retries, exec, staged files and refusals land here.");
  $$("#event-feed .task-ref").forEach((el) => el.addEventListener("click", () => openTaskIn(c.id, el.dataset.task)));
}

/* ------------------------------------------------------------ channel sheet (create / edit) */
function openChannelSheet(c) {
  S.sheetChannel = c || null;
  $("#channel-sheet-title").textContent = c ? `Edit channel · ${c.name}` : "New channel";
  $("#channel-sheet-status").textContent = "Rules can narrow the machine rules, never widen them.";
  const mp = S.machinePolicy;
  const rules = (c && c.rules) || { systems: {}, max_user_pending_per_system: null };
  const sysBlocks = S.resources.filter((s) => s.partitions.length).map((s) => {
    const sr = rules.systems[s.name];
    const enabled = !!sr || !c;                                  // a new channel starts with every machine on
    const accounts = s.accounts;
    const machineBudget = mp.node_hours[s.name];
    const parts = s.declared_partitions.map((p) => {
      const key = `${s.name}:${p}`; const machineCap = mp.partition_limits[key] ?? mp.max_nodes_per_job; const blocked = mp.blocked_partitions.includes(key);
      const pr = sr && sr.partitions ? sr.partitions[p] : null; const on = blocked ? false : (sr ? (sr.partitions == null || !!pr) : true);
      const rec = s.partitions.find((r) => r.partition === p);
      return `<tr><td><label class="check"><input type="checkbox" data-part="${esc(key)}" ${on ? "checked" : ""} ${blocked ? "disabled" : ""}> <b>${esc(p)}</b></label>${blocked ? ` <span class="chip chip--bad">blocked on the machine</span>` : ""}</td>
        <td><small class="muted">${rec ? esc(hwText(rec)) : ""}</small></td>
        <td><span class="unit-input"><input type="number" min="1" max="${machineCap}" data-cap="${esc(key)}" value="${pr && pr.max_nodes_per_job != null ? pr.max_nodes_per_job : ""}" placeholder="${machineCap}" ${blocked ? "disabled" : ""}><span class="cap-default">of ${machineCap}</span></span></td>
        <td><span class="unit-input"><input type="number" min="0.1" step="0.5" data-qh="${esc(key)}" value="${pr && pr.node_hours != null ? pr.node_hours : ""}" placeholder="∞" ${blocked ? "disabled" : ""}><span class="cap-default">node-h</span></span></td>
        <td><input type="text" list="accts-${esc(s.name)}" data-qacct="${esc(key)}" data-catalog-acct="${esc(rec ? rec.account : (accounts[0] || ""))}" value="${esc(pr && pr.account ? pr.account : "")}" placeholder="${esc((sr && sr.account) || (rec ? rec.account : accounts[0]) || "")}" title="Account billed on this queue; empty = the default shown" spellcheck="false" ${blocked ? "disabled" : ""}></td></tr>`;
    }).join("");
    return `<div class="rules-system ${enabled ? "" : "is-off"}" data-system="${esc(s.name)}">
      <div class="rules-system__head"><label class="check"><input type="checkbox" data-sys="${esc(s.name)}" ${enabled ? "checked" : ""}> ${esc(s.name)}</label><small class="muted">${esc(PROVIDER_LABEL[s.provider] || s.provider)}</small>
        <label class="acct-field">account <input type="text" list="accts-${esc(s.name)}" data-account="${esc(s.name)}" value="${esc(sr && sr.account ? sr.account : "")}" placeholder="catalog default per queue" title="Account this channel bills on every queue of ${esc(s.name)} unless a queue says otherwise; type any account you hold" spellcheck="false"></label>
        <datalist id="accts-${esc(s.name)}">${accounts.map((a) => `<option value="${esc(a)}">`).join("")}</datalist></div>
      <div class="rules-system__budget">
        <label>Node-hour cap on this machine <span class="unit-input"><input type="number" min="0.1" step="1" data-mh-cap="${esc(s.name)}" value="${sr && sr.node_hours != null ? sr.node_hours : ""}" placeholder="∞"><span>node-h${machineBudget != null ? ` · machine budget ${machineBudget}` : " · no machine budget"}</span></span></label>
      </div>
      <div class="table-wrap"><table class="table rules-table"><thead><tr><th>Queue</th><th>Hardware</th><th>Max nodes / job</th><th>Node-hours</th><th>Account</th></tr></thead><tbody>${parts}</tbody></table></div></div>`;
  }).join("");
  $("#channel-sheet-body").innerHTML = `<div class="rules-form">
      <div class="form-head">
        <label>Name <input type="text" id="ch-name" value="${esc(c ? c.name : "")}" placeholder="e.g. llm-finetune" spellcheck="false"></label>
        <label>Description <input type="text" id="ch-desc" value="${esc(c ? c.description : "")}" placeholder="what this channel is for (shown to you, not the agent)" spellcheck="false"></label>
        <label>Max pending jobs / machine <input type="number" id="ch-pending" min="1" max="${mp.max_user_pending_per_system}" value="${rules.max_user_pending_per_system ?? ""}" placeholder="${mp.max_user_pending_per_system}"></label>
      </div>
      <div class="hint">Tick what this channel's agents may use. Empty fields mean: the machine's cap, the catalog's account, no budget.</div>
      ${sysBlocks || `<div class="empty-copy">No machines in the pool yet.</div>`}
    </div>`;
  $$("#channel-sheet-body [data-sys]").forEach((cb) => cb.addEventListener("change", () => cb.closest(".rules-system").classList.toggle("is-off", !cb.checked)));
  $$("#channel-sheet-body [data-account]").forEach((inp) => inp.addEventListener("input", () => {   // queues inherit the machine-level account unless they say otherwise
    $$("[data-qacct]", inp.closest(".rules-system")).forEach((q) => { q.placeholder = inp.value.trim() || q.dataset.catalogAcct; }); }));
  $("#channel-sheet").classList.remove("hidden");
  $("#ch-name").focus();
}
function channelRulesFromForm() {
  const systems = {};
  $$("#channel-sheet-body .rules-system").forEach((block) => {
    if (!$("[data-sys]", block).checked) return;
    const partitions = {};
    const num = (sel) => { const el = $(sel, block); return el && el.value ? +el.value : null; };
    $$("[data-part]", block).forEach((cb) => { if (!cb.checked || cb.disabled) return; const key = cb.dataset.part; const q = CSS.escape(key); partitions[key.split(":").slice(1).join(":")] = { max_nodes_per_job: num(`[data-cap="${q}"]`), node_hours: num(`[data-qh="${q}"]`), account: $(`[data-qacct="${q}"]`, block).value.trim() || null }; });
    systems[block.dataset.system] = { account: $("[data-account]", block).value.trim() || null, partitions, node_hours: num("[data-mh-cap]") };
  });
  const pending = $("#ch-pending").value;
  return { systems, max_user_pending_per_system: pending ? +pending : null };
}
async function saveChannel() {
  const btn = $("#btn-channel-save"); btn.disabled = true; $("#channel-sheet-status").textContent = "Saving…";
  const body = { name: $("#ch-name").value.trim(), description: $("#ch-desc").value.trim(), rules: channelRulesFromForm() };
  if (!body.name) { $("#channel-sheet-status").textContent = "Give the channel a name."; btn.disabled = false; $("#ch-name").focus(); return; }
  try {
    const saved = S.sheetChannel ? await send("PUT", `/ui/channels/${S.sheetChannel.id}`, body) : await post("/ui/channels", body);
    $("#channel-sheet").classList.add("hidden");
    await fetchAll();
    openChannel(saved.id, S.sheetChannel ? "rules" : "tasks");
    if (!S.sheetChannel) { const d = $(".setup-box"); if (d) d.open = true; }   // a new channel: show how to hand it over
  } catch (e) { $("#channel-sheet-status").textContent = `Not saved: ${e.message}`; } finally { btn.disabled = false; }
}

/* ------------------------------------------------------------ machine rules (operator policy) */
async function openPolicy() {
  $("#policy-status").textContent = "Machine rules bound every channel; a channel can only narrow them.";
  $("#policy-body").innerHTML = `<div class="empty-copy">Loading rules…</div>`;
  $("#policy-sheet").classList.remove("hidden");
  try { S.policy = await api("/api/policy"); } catch (e) { $("#policy-body").innerHTML = `<div class="notice">Could not load rules: ${esc(e.message)}</div>`; return; }
  renderPolicySheet();
}
function renderPolicySheet() {
  const p = S.policy;
  const rec = (r) => (systemOf(r.system) || { partitions: [] }).partitions.find((x) => x.partition === r.partition);
  const users = (key) => S.channels.filter((c) => c.allowed[key] != null).length;
  $("#policy-body").innerHTML = `
    <div class="policy-defaults">
      <label>Default max nodes per job <input type="number" id="p-nodes" min="1" value="${p.max_nodes_per_job}"></label>
      <label>Max pending jobs per machine <input type="number" id="p-pending" min="1" value="${p.max_user_pending_per_system}"></label>
    </div>
    <div class="table-wrap" style="margin-bottom:14px"><table class="table policy-table"><thead><tr><th>Machine</th><th>Node-hour budget</th><th>Used (all channels)</th></tr></thead><tbody>
    ${S.resources.filter((s) => s.partitions.length).map((s) => `<tr><td><b>${esc(s.name)}</b></td>
      <td><span class="unit-input"><input type="number" min="0.1" step="1" data-budget="${esc(s.name)}" value="${p.node_hours[s.name] ?? ""}" placeholder="unlimited"><span class="cap-default">node-h, shared by every channel, first come first served</span></span></td>
      <td>${p.node_hours_used[s.name].toFixed(1)} h</td></tr>`).join("")}
    </tbody></table></div>
    <div class="table-wrap"><table class="table policy-table"><thead><tr><th>Machine</th><th>Queue</th><th>Hardware</th><th>Nodes</th><th>Max nodes / job</th><th>Blocked</th><th>Channels</th></tr></thead><tbody>
    ${p.partitions.map((r) => { const x = rec(r); return `<tr class="${r.blocked ? "is-blocked" : ""}">
      <td>${esc(r.system)}</td><td><b>${esc(r.partition)}</b></td><td><small class="muted">${x ? esc(hwText(x)) : "—"}</small></td><td>${r.nodes_total ?? "—"}</td>
      <td><span class="unit-input"><input type="number" min="1" ${r.nodes_total ? `max="${r.nodes_total}"` : ""} data-cap="${esc(r.key)}" value="${r.override ?? ""}" placeholder="${p.max_nodes_per_job}"><span class="cap-default">${r.override != null ? "override" : `default ${p.max_nodes_per_job}`}</span></span></td>
      <td><label class="check"><input type="checkbox" data-block="${esc(r.key)}" ${r.blocked ? "checked" : ""}> blocked</label></td>
      <td><small class="muted">${users(r.key)}</small></td></tr>`; }).join("") || `<tr><td colspan="7"><div class="empty-copy">No queues in the pool yet.</div></td></tr>`}
    </tbody></table></div>
    <div class="hint" style="margin-top:12px">Per queue: the largest job any channel may build there. Channels can lower a cap, never raise it; a blocked queue disappears from every channel.</div>`;
}
async function savePolicy() {
  const btn = $("#btn-policy-save"); btn.disabled = true; $("#policy-status").textContent = "Saving…";
  const body = { max_nodes_per_job: +$("#p-nodes").value || null, max_user_pending_per_system: +$("#p-pending").value || null,
    partition_limits: Object.fromEntries($$("#policy-body [data-cap]").map((el) => [el.dataset.cap, el.value ? +el.value : null])),
    node_hours: Object.fromEntries($$("#policy-body [data-budget]").map((el) => [el.dataset.budget, el.value ? +el.value : null])),
    blocked_partitions: $$("#policy-body [data-block]").filter((el) => el.checked).map((el) => el.dataset.block) };
  try { S.policy = await send("PUT", "/ui/policy", body); renderPolicySheet(); $("#policy-status").textContent = "Saved — every channel's effective rules follow."; refresh(); }
  catch (e) { $("#policy-status").textContent = `Not saved: ${e.message}`; } finally { btn.disabled = false; }
}

/* ------------------------------------------------------------ notifications */
function renderBell() {
  const notable = S.events.filter((e) => NOTABLE(e.kind));
  const unread = notable.filter((e) => new Date(e.ts).getTime() > S.lastSeen);
  $("#bell-dot").classList.toggle("hidden", unread.length === 0);
  $("#bell-list").innerHTML = notable.slice(0, 30).map((e) => { const c = e.channel ? channelOf(e.channel) : null; return `<div class="event"><span class="ts">${fmtTime(e.ts)}</span>${eventChip(e.kind)}<span class="msg">${c ? `<b>${esc(c.name)}</b> · ` : ""}${esc(e.message)}${e.task_id ? `<span class="task-ref" data-task="${esc(e.task_id)}" data-channel="${esc(e.channel || "")}">[${esc(e.task_id.slice(0, 8))}]</span>` : ""}</span></div>`; }).join("") || `<div class="empty-copy">Nothing notable yet.</div>`;
  $$("#bell-list .task-ref").forEach((el) => el.addEventListener("click", () => { $("#bell-pop").classList.add("hidden"); if (el.dataset.channel) openTaskIn(el.dataset.channel, el.dataset.task); }));
}

/* ------------------------------------------------------------ auth */
function renderAuth() {
  const a = S.auth; if (!a) return;
  const needs = a.required && !a.authenticated;
  $("#signin-sheet").classList.toggle("hidden", !needs);
  if (needs) {
    $("#btn-signin").classList.toggle("hidden", !a.web_login_available);
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
async function signOut() { await post("/api/auth/logout"); await refresh(); }

/* ------------------------------------------------------------ views */
function hashFor() {
  if (S.view === "machine") return "machine/" + encodeURIComponent(S.selectedSystem || "") + (S.resourceTab !== "overview" ? "/" + S.resourceTab : "");
  if (S.view === "channel") return "channel/" + encodeURIComponent(S.channelId || "") + (S.detailTask ? "/task/" + S.detailTask : S.channelTab !== "tasks" ? "/" + S.channelTab : "");
  return "home";
}
function switchView(name) {
  S.view = name;
  history.replaceState(null, "", "#" + hashFor());
  $("#nav-home").classList.toggle("is-active", name === "home");
  $$(".view").forEach((v) => v.classList.toggle("is-active", v.id === `view-${name}`));
  if (name === "home") { $("#eyebrow").textContent = "Diamond Agent"; $("#page-title").textContent = "Home"; $("#scroll").scrollTop = 0; }
  render();
}
async function render() {
  renderSidebar(); renderBell();
  if (S.view === "home") renderHome();
  else if (S.view === "machine") await renderMachine();
  else if (S.view === "channel") await renderChannel();
}
async function refresh() { try { await fetchAll(); await render(); } catch (e) { $("#refresh-text").textContent = `offline: ${e.message}`; } }
setInterval(() => { if (!document.hidden && $(".sheet-backdrop:not(.hidden):not(#signin-sheet)") === null) refresh(); }, POLL_MS);

/* ------------------------------------------------------------ wiring */
mountIcons();
$("#nav-home").addEventListener("click", () => switchView("home"));
$$("#resource-tabs button").forEach((b) => b.addEventListener("click", () => { S.resourceTab = b.dataset.t; $$("#resource-tabs button").forEach((x) => x.classList.toggle("is-active", x === b)); history.replaceState(null, "", "#" + hashFor()); renderMachine(); }));
$$("#channel-tabs button").forEach((b) => b.addEventListener("click", () => { S.channelTab = b.dataset.t; S.detailTask = null; $$("#channel-tabs button").forEach((x) => x.classList.toggle("is-active", x === b)); history.replaceState(null, "", "#" + hashFor()); renderChannel(); }));
$("#btn-signout").addEventListener("click", signOut);
$("#btn-new-channel").addEventListener("click", () => openChannelSheet(null));
$("#btn-channel-save").addEventListener("click", saveChannel);
$("#btn-policy-home").addEventListener("click", openPolicy);
$("#btn-policy-save").addEventListener("click", savePolicy);
$$("[data-close]").forEach((b) => b.addEventListener("click", () => $(`#${b.dataset.close}`).classList.add("hidden")));
$$(".sheet-backdrop:not(#signin-sheet)").forEach((bd) => bd.addEventListener("mousedown", (e) => { if (e.target === bd) bd.classList.add("hidden"); }));
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { $$(".sheet-backdrop:not(#signin-sheet)").forEach((x) => x.classList.add("hidden")); $("#bell-pop").classList.add("hidden"); } });
$("#btn-bell").addEventListener("click", () => { renderBell(); $("#bell-pop").classList.toggle("hidden"); });
$("#btn-bell-clear").addEventListener("click", () => { S.lastSeen = Date.now(); localStorage.setItem("da.lastSeen", String(S.lastSeen)); renderBell(); });

/* deep links: #home · #channel/<id>[/tasks|/activity|/rules|/task/<task id>] · #machine/<system>[/history|/connection] · #limits · #new-channel */
function applyHash() {
  const h = decodeURIComponent(location.hash.slice(1));
  if (!h) return;
  const [head, ...rest] = h.split("/");
  if (head === "machine" && rest[0]) openMachine(rest[0], rest[1]);
  else if (head === "channel" && rest[0]) { if (rest[1] === "task" && rest[2]) openTaskIn(rest[0], rest[2]); else openChannel(rest[0], rest[1]); }
  else if (head === "limits") openPolicy();
  else if (head === "new-channel") openChannelSheet(null);
  else switchView("home");
}
window.addEventListener("hashchange", applyHash);
refresh().then(applyHash);
