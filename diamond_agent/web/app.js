/* Diamond Agent — the operator's console over diamond-agent-core.
   Vanilla JS over the local API. An overview (the pool, the channels, the
   machines), a page per channel (tasks with an inspector beside the list,
   releases, activity, rules) and a page per machine (queues, history,
   connection). The UI submits nothing: agents do, through their channel URL.

   Rendering: every region is an HTML string and paint() writes it only when it
   changed, so the 3 s poll never flickers, drops a hover or closes what the
   operator opened. One delegated click handler (data-act) serves everything. */

const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => Array.from(root.querySelectorAll(s));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ------------------------------------------------------------ icons */
const ICONS = {
  grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  list: '<path d="M8 6h13M8 12h13M8 18h13"/><circle cx="3.5" cy="6" r="1"/><circle cx="3.5" cy="12" r="1"/><circle cx="3.5" cy="18" r="1"/>',
  activity: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  bell: '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10 21a2 2 0 0 0 4 0"/>',
  server: '<rect x="2" y="3" width="20" height="7" rx="2"/><rect x="2" y="14" width="20" height="7" rx="2"/><path d="M6 6.5h.01M6 17.5h.01"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  chevron: '<path d="m9 6 6 6-6 6"/>',
  layers: '<path d="m12 2 10 5-10 5L2 7z"/><path d="m2 12 10 5 10-5"/><path d="m2 17 10 5 10-5"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  check: '<path d="m5 12 5 5L20 7"/>',
  shield: '<path d="M12 3 4 6v6c0 5 3.4 7.9 8 9 4.6-1.1 8-4 8-9V6z"/><path d="m9 12 2 2 4-4"/>',
  copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/>',
  menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
};
const icon = (n) => `<span class="ico"><svg viewBox="0 0 24 24">${ICONS[n]}</svg></span>`;
$$("[data-icon]").forEach((el) => { el.innerHTML = `<svg viewBox="0 0 24 24">${ICONS[el.dataset.icon]}</svg>`; });

/* ------------------------------------------------------------ state */
const POLL_MS = 3000;
const S = {
  auth: null, resources: [], providers: [], tasks: [], events: [], history: [], channels: [], machinePolicy: null, takenAt: null,
  view: "home", machine: null, machineTab: "overview",
  channelId: null, channelTab: "tasks", task: null, taskFilter: "active", eventFilter: "all",
  connect: {},                 // channel id -> its "connect an agent" block is open
  policy: null, sheetChannel: null, confirm: null,
  lastSeen: Number(localStorage.getItem("da.lastSeen") || 0),
};

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let d = res.statusText;
    try { const j = await res.json(); d = (j.detail && j.detail.message) || j.detail || d; } catch {}
    throw Object.assign(new Error(d), { status: res.status });
  }
  return res.json();
}
const send = (method, path, body) => api(path, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });

/* ------------------------------------------------------------ painting, feedback */
const painted = new WeakMap();
function paint(el, html) { if (painted.get(el) === html) return; painted.set(el, html); el.innerHTML = html; }

let toastTimer;
function toast(message, tone) {
  const el = $("#toast");
  el.textContent = message; el.className = `toast${tone ? ` toast--${tone}` : ""}`; el.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { el.hidden = true; }, tone === "bad" ? 6000 : 2200);
}
async function copyText(text, what) {
  try { await navigator.clipboard.writeText(text); }
  catch {       // no clipboard API on a plain-http origin, or the browser refused it
    const ta = document.createElement("textarea"); ta.value = text; document.body.append(ta); ta.select(); document.execCommand("copy"); ta.remove();
  }
  toast(`${what} copied`);
}
function ask({ title, body, confirm }) {       // -> Promise<boolean>; every confirmation in the console is destructive
  $("#confirm-title").textContent = title; $("#confirm-body").textContent = body;
  $("#confirm-ok").textContent = confirm;
  $("#confirm-sheet").hidden = false; $("#confirm-keep").focus();           // Enter keeps; the destructive button takes a deliberate click
  return new Promise((resolve) => { S.confirm = resolve; });
}
function closeSheet(id) { $(`#${id}`).hidden = true; if (id === "confirm-sheet") S.confirm(false); }

/* ------------------------------------------------------------ helpers */
const n = (x) => x.toLocaleString("en-US");
const compact = (x) => x >= 10000 ? `${(x / 1000).toFixed(x >= 100000 ? 0 : 1)}k` : n(x);
const plural = (k, word) => `${n(k)} ${word}${k === 1 ? "" : "s"}`;
const fig = (x) => x ? n(x) : `<span class="zero">–</span>`;
const range = (a, b) => a === b ? `${a}` : `${a}–${b}`;
const pct = (u) => `${Math.round(Math.min(1, Math.max(0, u)) * 100)}%`;
const level = (u) => u < 0.5 ? 1 : u < 0.85 ? 2 : 3;
const meter = (u) => u == null
  ? `<span class="meter meter--off"><span class="meter__track"></span><span class="meter__pct">—</span></span>`
  : `<span class="meter lvl-${level(u)}"><span class="meter__track"><span class="meter__fill" style="width:${pct(u)}"></span></span><span class="meter__pct">${pct(u)}</span></span>`;
const fmtTime = (ts) => new Date(ts).toLocaleTimeString([], { hour12: false });
const fmtDate = (ts) => new Date(ts).toLocaleString([], { hour12: false });
const ago = (ts) => { const s = Math.max(0, (Date.now() - new Date(ts)) / 1000); return s < 5 ? "just now" : s < 60 ? `${Math.round(s)}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : s < 86400 ? `${(s / 3600).toFixed(1)}h ago` : `${Math.round(s / 86400)}d ago`; };
const fmtWall = (m) => m == null ? "no limit" : m % 60 === 0 ? `${m / 60}h` : m > 90 ? `${(m / 60).toFixed(1)}h` : `${m}m`;
const sizingText = (s) => s ? `${s.nodes}n · ${s.gpus}g · ${s.cpus}c · ${fmtWall(s.walltime_minutes)}` : "—";
const sizingLong = (s) => s ? `${plural(s.nodes, "node")} · ${plural(s.gpus, "GPU")} · ${plural(s.cpus, "core")} · ${fmtWall(s.walltime_minutes)}` : "—";
const hwText = (r) => r.gpu_type ? `${r.gpu_type} ×${r.gpus_per_node} · ${r.gpu_mem_gb} GB/GPU · ${r.cpus_per_node}c` : `${r.cpus_per_node} cores · ${r.mem_per_node_gb} GB`;
const freeOf = (r) => r.gpu_type ? [r.gpus_free ?? r.nodes_idle * r.gpus_per_node, "GPU"] : [r.cpus_free ?? r.nodes_idle * r.cpus_per_node, "cores"];
const utilOf = (r) => { const usable = r.nodes_idle + r.nodes_mixed + r.nodes_allocated; if (r.gpu_type && r.gpus_free != null) { const cap = Math.max(usable * r.gpus_per_node, r.gpus_free); return cap ? (cap - r.gpus_free) / cap : 0; } if (!r.gpu_type && r.cpus_free != null) { const cap = Math.max(usable * r.cpus_per_node, r.cpus_free); return cap ? (cap - r.cpus_free) / cap : 0; } const t = usable + r.nodes_down; return t ? (r.nodes_allocated + r.nodes_mixed) / t : 0; };
const PROVIDER_LABEL = { globus_hpc: "HPC · Globus Compute", ssh: "SSH server", cloud: "Cloud" };
const providerOf = (s) => PROVIDER_LABEL[s.provider] || s.provider;
const HEALTH_TONE = { online: "good", degraded: "warn", offline: "bad", unknown: "off" };
const TASK_TONE = { COMPLETED: "good", RUNNING: "live", PENDING: "warn", SUBMITTED: "off", FAILED: "bad", CANCELLED: "off" };
const BUILD_TONE = { ready: "good", building: "", failed: "bad", stale: "warn" };
const led = (tone) => `<span class="led led--${tone}"></span>`;
const state = (label, tone) => `<span class="state state--${tone}">${led(tone)}${esc(label)}</span>`;
const taskState = (s) => state(s, TASK_TONE[s]);
const ACTIVE = ["PENDING", "RUNNING", "SUBMITTED"];
const BAD = ["JOB_FAILED", "JOB_SUBMIT_FAILED", "POLICY_DENIED", "AGENT_GAVE_UP"];
const NOTABLE = (k) => k.startsWith("AGENT") || k.startsWith("POLICY") || k.startsWith("CHANNEL") || BAD.includes(k);
const kindTone = (k) => k.startsWith("AGENT") ? "agent" : BAD.includes(k) ? "bad" : (k.startsWith("POLICY") || k.startsWith("CHANNEL")) ? "policy" : "plain";
const taskRef = (e) => e.task_id ? `<button class="task-ref" data-act="task-ref" data-id="${esc(e.task_id)}" data-channel="${esc(e.channel || "")}" title="Open this task">${esc(e.task_id.slice(0, 8))}</button>` : "";
const eventRow = (e, o = {}) => {       // o.stack: kind above the message, for narrow columns
  const body = `<span class="event__kind kind--${kindTone(e.kind)}"><span class="led"></span><span>${esc(e.kind.replaceAll("_", " "))}</span></span><span class="event__msg">${o.prefix || ""}${esc(e.message)}${o.ref ? taskRef(e) : ""}</span>`;
  return `<div class="event${o.stack ? " event--stack" : ""}"><span class="event__ts" title="${esc(fmtDate(e.ts))}">${fmtTime(e.ts)}</span>${o.stack ? `<div>${body}</div>` : body}</div>`;
};
const emptyState = (ic, title, sub, extra = "") => `<div class="empty">${icon(ic)}<strong>${esc(title)}</strong><small>${esc(sub)}</small>${extra}</div>`;
const tabs = (scope, items, current) => items.map(([t, label, count]) => `<button data-act="tab" data-scope="${scope}" data-t="${t}"${t === current ? ' class="is-active"' : ""}>${label}${count ? `<i>${count}</i>` : ""}</button>`).join("");
const systemOf = (name) => S.resources.find((s) => s.name === name);
const channelOf = (id) => S.channels.find((c) => c.id === id);
const channelUrl = (c) => `${location.origin}${c.path}`;
const channelsOn = (key) => S.channels.filter((c) => c.allowed[key] != null);
const channelsUsing = (system) => S.channels.filter((c) => Object.keys(c.allowed).some((k) => k.startsWith(system + ":")));
/* what the operator needs to know about a channel at a glance */
function channelFacts(c) {
  const tasks = S.tasks.filter((t) => t.channel === c.id);
  const last = S.events.find((e) => e.channel === c.id);
  const queues = {};          // machine -> how many of its queues the channel may use
  Object.keys(c.allowed).forEach((k) => { const sys = k.split(":")[0]; queues[sys] = (queues[sys] || 0) + 1; });
  const count = (...st) => tasks.filter((t) => st.includes(t.status)).length;
  return { tasks, queues, machines: Object.keys(queues), queueCount: Object.keys(c.allowed).length,
    running: count("RUNNING"), pending: count("PENDING", "SUBMITTED"), failed: count("FAILED"), done: count("COMPLETED"),
    active: tasks.filter((t) => ACTIVE.includes(t.status)),
    last: last ? last.ts : tasks[0] ? tasks[0].created_at : null };
}
const setupSnippet = (c) => ({
  shell: `export DIAMOND_AGENT_URL=${channelUrl(c)}\nuv run --no-sync diamond-agent-core status`,
  mcp: `claude mcp add diamond-agent-core -e DIAMOND_AGENT_URL=${channelUrl(c)} -- uv run --no-sync python -m diamond_agent.core.mcp_server`,
});
function setPage(eyebrow, title, actions) {
  $("#eyebrow").textContent = eyebrow; $("#page-title").textContent = title; paint($("#page-actions"), actions);
  document.title = S.view === "home" ? "Diamond Agent" : `${title} · Diamond Agent`;
}

/* ------------------------------------------------------------ data */
async function fetchAll() {
  // The operator cookie is new on every core start: a tab that outlived a
  // restart reloads once to be handed the new one.
  const channels = api("/ui/channels").catch((e) => { if (e.status === 403 && !sessionStorage.getItem("da.reloaded")) { sessionStorage.setItem("da.reloaded", "1"); location.reload(); } throw e; });
  const [res, tasks, events, hist, auth, pol, chans] = await Promise.all([api("/api/resources"), api("/api/tasks"), api("/api/events?limit=300"), api("/api/federation/history?limit=60"), api("/api/auth/session"), api("/api/policy"), channels]);
  sessionStorage.removeItem("da.reloaded");
  Object.assign(S, { resources: res.systems, providers: res.providers, takenAt: res.taken_at, tasks, events, history: hist, auth, machinePolicy: pol, channels: chans });
}

/* ------------------------------------------------------------ rail */
function renderRail() {
  paint($("#rail-channels"), S.channels.map((c) => {
    const f = channelFacts(c);
    return `<button class="rail-row${S.view === "channel" && S.channelId === c.id ? " is-selected" : ""}" data-act="open-channel" data-id="${esc(c.id)}">
      ${led(f.running ? "live" : "off")}<span class="rail-row__name">${esc(c.name)}</span>${f.running ? `<span class="rail-row__num is-live" title="${f.running} running">${f.running}</span>` : ""}</button>`;
  }).join(""));
  paint($("#rail-machines"), S.resources.map((s) => {
    const num = s.health === "offline" ? `<span class="rail-row__num is-bad">offline</span>`
      : !s.partitions.length ? ""
      : s.gpus_total ? `<span class="rail-row__num" title="${n(s.gpus_free)} GPUs free">${compact(s.gpus_free)}<small>gpu</small></span>`
      : `<span class="rail-row__num" title="${n(s.cpus_free)} cores free">${compact(s.cpus_free)}<small>cpu</small></span>`;
    return `<button class="rail-row${S.view === "machine" && S.machine === s.name ? " is-selected" : ""}" data-act="open-machine" data-id="${esc(s.name)}">
      ${led(HEALTH_TONE[s.health])}<span class="rail-row__name">${esc(s.name)}</span>${num}</button>`;
  }).join("") || `<div class="rail__empty">No machines yet</div>`);
  $("#rail-online").textContent = `${S.resources.filter((s) => s.health === "online").length}/${S.resources.length}`;
  $("#nav-home").classList.toggle("is-active", S.view === "home");
  $("#pulse-led").className = "led led--good";
  $("#pulse-text").textContent = S.takenAt ? `snapshot ${ago(S.takenAt)}` : "waiting for the first snapshot";
}

/* ------------------------------------------------------------ overview */
function sparkline(points, unit) {       // points: [{ v, t }], oldest first
  if (points.length < 2) return "";
  const W = 280, H = 56, P = 6, vals = points.map((p) => p.v), lo = Math.min(...vals), span = (Math.max(...vals) - lo) || 1;
  const step = (W - 2 * P) / (points.length - 1);
  const x = (i) => (P + i * step).toFixed(1), y = (v) => (H - P - ((v - lo) / span) * (H - 2 * P)).toFixed(1);
  const d = points.map((p, i) => `${i ? "L" : "M"}${x(i)},${y(p.v)}`).join(" ");
  const end = points.length - 1;
  const hits = points.map((p, i) => `<g class="spark__pt" data-tip="${esc(`${n(p.v)} ${unit} · ${fmtTime(p.t)}`)}"><rect x="${(P + i * step - step / 2).toFixed(1)}" y="0" width="${step.toFixed(1)}" height="${H}"/><line x1="${x(i)}" x2="${x(i)}" y1="${P}" y2="${H - P}"/><circle cx="${x(i)}" cy="${y(p.v)}" r="4"/></g>`).join("");
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="${esc(`${unit} over the last ${points.length} snapshots`)}"><path class="spark__area" d="${d} L${x(end)},${H - P} L${x(0)},${H - P} Z"/><path class="spark__line" d="${d}"/><circle class="spark__end" cx="${x(end)}" cy="${y(points[end].v)}" r="4"/>${hits}</svg>`;
}
function renderHome() {
  setPage("Diamond Agent", "Overview", "");
  const online = S.resources.filter((s) => s.health === "online").length;
  const sum = (key) => S.resources.reduce((a, s) => a + s[key], 0);
  const gpus = sum("gpus_total");
  const hero = gpus ? { value: sum("gpus_free"), text: `of ${n(gpus)} GPUs free`, unit: "GPUs free", series: "gpus_idle" } : { value: sum("cpus_free"), text: "cores free", unit: "cores free", series: "cpus_idle" };
  const count = (...st) => S.tasks.filter((t) => st.includes(t.status)).length;
  paint($("#readout"), `
    <div class="readout__main"><div class="readout__fig"><strong>${n(hero.value)}</strong><span>${hero.text} across ${plural(S.resources.length, "machine")}</span></div>
      <div class="readout__trend">${sparkline(S.history.map((h) => ({ v: h.aggregates[hero.series], t: h.taken_at })), hero.unit)}<span>last ${plural(S.history.length, "snapshot")}</span></div></div>
    <div class="readout__cell"><strong>${online}<i>/${S.resources.length}</i></strong><span>machines online</span></div>
    <div class="readout__cell"><strong>${count("RUNNING")}</strong><span>tasks running</span></div>
    <div class="readout__cell"><strong>${count("PENDING", "SUBMITTED")}</strong><span>tasks pending</span></div>`);

  paint($("#home-channels"), `<div class="scroll-x"><table class="t t--channels"><thead><tr><th>Channel</th><th class="n">Running</th><th class="n">Pending</th><th class="n">Failed</th><th class="n">Done</th><th>Queues</th><th>Last activity</th><th></th></tr></thead><tbody>
    ${S.channels.map((c) => { const f = channelFacts(c); return `<tr class="is-link" data-act="open-channel" data-id="${esc(c.id)}" tabindex="0">
      <td><div class="cell-main">${led(f.running ? "live" : "off")}<div><b>${esc(c.name)}</b>${c.description ? `<span class="sub">${esc(c.description)}</span>` : ""}</div></div></td>
      <td class="n">${f.running ? `<b>${f.running}</b>` : fig(0)}</td><td class="n">${fig(f.pending)}</td><td class="n">${fig(f.failed)}</td><td class="n">${fig(f.done)}</td>
      <td><div class="tags">${f.machines.map((m) => `<span class="tag">${esc(m)}<i>${f.queues[m]}</i></span>`).join("") || `<span class="muted">none</span>`}</div></td>
      <td class="muted">${f.last ? ago(f.last) : "—"}</td>
      <td class="act"><button class="btn btn--sm" data-act="copy" data-copy="${esc(channelUrl(c))}" data-what="Channel URL" title="Copy the URL an agent uses as DIAMOND_AGENT_URL">${icon("copy")}URL</button></td></tr>`; }).join("")}
    </tbody></table></div>`);

  paint($("#home-machines"), S.resources.length ? `<div class="scroll-x"><table class="t board"><thead><tr><th>Queue</th><th>Hardware</th><th>Utilization</th><th class="n">Free</th><th class="n">Running</th><th class="n">Pending</th><th class="n">Mine</th></tr></thead>
    ${S.resources.map((s) => {
      const sumText = !s.partitions.length ? "waiting for the first probe" : s.health === "offline" ? "endpoint offline" : s.gpus_total ? `${n(s.gpus_free)} of ${n(s.gpus_total)} GPUs free` : `${n(s.cpus_free)} cores free`;
      return `<tbody${s.health === "offline" ? ' class="is-off"' : ""}><tr class="board__machine is-link" data-act="open-machine" data-id="${esc(s.name)}" tabindex="0"><td colspan="7"><div class="board__head">
          ${led(HEALTH_TONE[s.health])}<b>${esc(s.name)}</b><span class="muted">${esc(providerOf(s))}${s.location ? " · " + esc(s.location) : ""}</span>
          <span class="board__sum">${sumText} · ${plural(channelsUsing(s.name).length, "channel")}</span>${icon("chevron")}</div></td></tr>
        ${s.partitions.map((r) => { const [free, unit] = freeOf(r); const u = r.endpoint_health === "online" ? utilOf(r) : null; return `<tr>
          <td class="q"><b>${esc(r.partition)}</b></td><td class="muted">${esc(hwText(r))}</td><td>${meter(u)}</td>
          <td class="n">${u == null ? "—" : `<b>${n(free)}</b><i class="unit">${unit}</i>`}</td>
          <td class="n">${fig(r.running_jobs)}</td><td class="n">${fig(r.pending_jobs)}</td><td class="n">${fig(r.user_running_jobs + r.user_pending_jobs)}</td></tr>`; }).join("")}</tbody>`; }).join("")}
    </table></div>` : emptyState("server", "No machines yet", "Machines are declared in sites.live.yaml (make init-sites)."));
}

/* ------------------------------------------------------------ machine page */
async function renderMachine() {
  const s = systemOf(S.machine);
  if (!s) { setPage("Machine", S.machine, ""); paint($("#machine-head"), emptyState("server", "Unknown machine", "It is not in the site catalog (sites.live.yaml).")); paint($("#machine-tabs"), ""); paint($("#machine-body"), ""); return; }
  setPage(`Machine · ${providerOf(s)}${s.location ? " · " + s.location : ""}`, s.name, `<button class="btn" data-act="policy" title="Max nodes per job, budgets and blocked queues">${icon("shield")}Machine rules</button>`);
  const using = channelsUsing(s.name);
  paint($("#machine-head"), `<div class="meta">${state(s.health, HEALTH_TONE[s.health])}${s.tags.map((t) => `<span class="tag">${esc(t)}</span>`).join("")}
    <span>${using.length ? `Used by ${using.map((c) => `<b>${esc(c.name)}</b>`).join(", ")}` : "No channel uses this machine yet"}</span></div>`);
  paint($("#machine-tabs"), tabs("machine", [["overview", "Queues"], ["history", "Queue history"], ["connection", "Connection"]], S.machineTab));
  const body = $("#machine-body");
  if (S.machineTab === "overview") {
    paint(body, s.partitions.length ? `<div class="panel">${s.partitions.map((r) => {
      const key = `${s.name}:${r.partition}`, lim = s.limits[r.partition], chans = channelsOn(key);
      const [free, unit] = freeOf(r); const u = r.endpoint_health === "online" ? utilOf(r) : null;
      const nodes = [["alloc", "allocated", r.nodes_allocated], ["mixed", "mixed", r.nodes_mixed], ["idle", "idle", r.nodes_idle], ["down", "down", r.nodes_down]];
      const total = nodes.reduce((a, x) => a + x[2], 0);
      return `<article class="queue"><div>
          <h3>${esc(r.partition)}${lim.blocked ? `<span class="tag tag--bad">blocked</span>` : ""}</h3>
          <p class="queue__hw">${esc(hwText(r))} · ${esc(r.node_arch)}</p>
          <dl class="facts"><dt>Account</dt><dd class="mono">${esc(r.account)}</dd><dt>Max nodes / job</dt><dd>${lim.max_nodes_per_job}</dd><dt>Max wall time</dt><dd>${fmtWall(r.max_walltime_minutes)}</dd>
            <dt>Channels</dt><dd>${chans.map((c) => `${esc(c.name)} (≤${c.allowed[key]}n)`).join(", ") || `<span class="muted">none</span>`}</dd></dl></div>
        <div><div class="figs">
            <div><strong>${u == null ? "—" : n(free)}</strong><span>${unit} free</span></div>
            <div><strong>${u == null ? "—" : pct(u)}</strong><span>utilization</span></div>
            <div><strong>${n(r.running_jobs)} / ${n(r.pending_jobs)}</strong><span>jobs running / pending</span></div>
            <div><strong>${r.user_running_jobs + r.user_pending_jobs}</strong><span>mine</span></div></div>
          <div class="nodes" role="img" aria-label="Node states">${nodes.filter((x) => x[2]).map(([k, label, v]) => `<i class="k-${k}" style="flex:${v} 1 0" data-tip="${esc(`${n(v)} ${label} · ${pct(v / total)} of ${n(total)} nodes`)}"></i>`).join("")}</div>
          <div class="nodes__legend">${nodes.map(([k, label, v]) => `<span><i class="k-${k}"></i>${n(v)} ${label}</span>`).join("")}<span class="muted">${plural(total, "node")}</span></div></div></article>`; }).join("")}</div>`
      : `<div class="panel">${emptyState("server", "No live queues yet", "Waiting for the first probe of this machine.")}</div>`);
  } else if (S.machineTab === "history") {
    const hist = await api("/api/federation/history?limit=240");
    if (S.view !== "machine" || S.machine !== s.name || S.machineTab !== "history") return;
    const cell = (h, p) => { const rec = h.records.find((r) => r.system === s.name && r.partition === p); if (!rec) return `<i class="hgap"></i>`; if (rec.health !== "online") return `<i class="hoff" data-tip="${esc(fmtTime(h.taken_at))} · offline"></i>`; const u = rec.utilization; const c = u < 0.01 ? "" : u < 0.25 ? "h1" : u < 0.5 ? "h2" : u < 0.75 ? "h3" : u < 0.92 ? "h4" : "h5"; return `<i class="${c}" data-tip="${esc(`${fmtTime(h.taken_at)} · ${pct(u)} busy · ${n(rec.free)} free · ${n(rec.pending)} pending`)}"></i>`; };
    paint(body, `<div class="panel"><div class="panel__title"><strong>Busy fraction per queue</strong><span>one cell per snapshot · ${hist.length} kept</span></div>
      <div class="heat">${s.partitions.map((r) => `<div class="heat__row"><span>${esc(r.partition)}</span><div class="heat__cells">${hist.map((h) => cell(h, r.partition)).join("")}</div></div>`).join("")}
        ${hist.length ? `<div class="heat__axis"><span>${fmtTime(hist[0].taken_at)}</span><span>${fmtTime(hist[hist.length - 1].taken_at)}</span></div>` : ""}</div>
      <div class="heat__legend">idle <i style="background:var(--well)"></i><i style="background:var(--seq-1)"></i><i style="background:var(--seq-2)"></i><i style="background:var(--seq-3)"></i><i style="background:var(--seq-4)"></i><i style="background:var(--seq-5)"></i> saturated · <i class="k-down"></i> offline</div></div>`);
  } else {
    const p = S.providers.find((x) => x.kind === s.provider);
    paint(body, `<div class="panel" style="padding:18px 22px"><dl class="facts">
        <dt>Provider</dt><dd>${esc(providerOf(s))} <span class="muted">· ${esc(p.detail)} · ${esc(p.status)}</span></dd>
        <dt>Endpoint</dt><dd class="mono">${esc(s.endpoint || "—")}</dd>
        <dt>Declared queues</dt><dd class="mono">${esc(s.declared_partitions.join(", ") || "—")}</dd>
        <dt>Accounts</dt><dd class="mono">${esc(s.accounts.join(", ") || "—")}</dd>
        <dt>Container runtime</dt><dd>${esc(s.partitions.length ? s.partitions[0].container_runtime || "—" : "—")}</dd></dl></div>
      <p class="hint" style="margin-top:12px">Declared in the site catalog (<code>sites.live.yaml</code>): hardware facts, per-queue QOS and site dialect switches. Accounts are read from Slurm unless the catalog pins them; queue state comes from the probe every tick.</p>`);
  }
}

/* ------------------------------------------------------------ channel page */
async function renderChannel() {
  const c = channelOf(S.channelId);
  if (!c) { setPage("Channel", "Not found", ""); paint($("#channel-head"), emptyState("layers", "This channel no longer exists", "Its URL has been revoked.")); paint($("#channel-tabs"), ""); paint($("#channel-body"), ""); return; }
  const f = channelFacts(c), url = channelUrl(c), snip = setupSnippet(c), open = !!S.connect[c.id];
  setPage("Channel", c.name, `<button class="btn" data-act="edit-channel">${icon("shield")}Edit rules</button>${c.id === "default" ? "" : `<button class="btn btn--danger" data-act="delete-channel">Delete</button>`}`);
  paint($("#channel-head"), `<div class="meta">${state(f.running ? `${f.running} running` : "idle", f.running ? "live" : "off")}${c.description ? `<span>${esc(c.description)}</span>` : ""}
      <span class="muted">${plural(f.queueCount, "queue")} on ${esc(f.machines.join(", ") || "no machine")}</span>
      ${c.id === "default" ? `<span class="tag" title="Created with the core so agents can be handed a URL before any channel is shaped">default · cannot be deleted</span>` : ""}</div>
    <div class="handover"><div class="handover__url"><span class="label">Agent URL</span><code>${esc(url)}</code>
        <button class="btn btn--primary btn--sm" data-act="copy" data-copy="${esc(url)}" data-what="Channel URL">${icon("copy")}Copy</button>
        <button class="btn btn--ghost btn--sm" data-act="toggle-connect" aria-expanded="${open}">${open ? "Hide setup" : "Connect an agent"}</button></div>
      ${open ? `<div class="handover__more">
        <div class="snippet"><span class="label">Shell · any agent with the CLI</span><pre class="codebox">${esc(snip.shell)}</pre><button class="btn btn--sm" data-act="copy" data-copy="${esc(snip.shell)}" data-what="Shell setup">${icon("copy")}Copy</button></div>
        <div class="snippet"><span class="label">Claude Code · MCP server</span><pre class="codebox">${esc(snip.mcp)}</pre><button class="btn btn--sm" data-act="copy" data-copy="${esc(snip.mcp)}" data-what="MCP command">${icon("copy")}Copy</button></div></div>` : ""}</div>`);
  paint($("#channel-tabs"), tabs("channel", [["tasks", "Tasks", f.active.length], ["releases", "Releases"], ["activity", "Activity"], ["rules", "Rules"]], S.channelTab));
  const body = $("#channel-body");
  if (S.channelTab === "tasks") {
    const list = S.taskFilter === "all" ? f.tasks : f.active;
    paint(body, `<div class="toolbar"><span class="caption">${f.running} running · ${f.pending} pending · ${f.failed} failed · ${f.done} done</span>
        <div class="seg"><button data-act="task-filter" data-v="active"${S.taskFilter === "active" ? ' class="is-active"' : ""}>Active<i>${f.active.length}</i></button><button data-act="task-filter" data-v="all"${S.taskFilter === "all" ? ' class="is-active"' : ""}>All<i>${f.tasks.length}</i></button></div></div>
      <div class="panel">${list.length ? `<div class="scroll-x"><table class="t"><thead><tr><th>Task</th><th>Status</th><th>Queue</th><th>Sizing</th><th>Job</th><th class="n">Attempts</th><th></th></tr></thead><tbody>
        ${list.map((t) => `<tr class="is-link${t.task_id === S.task ? " is-chosen" : ""}" data-act="task" data-id="${esc(t.task_id)}" tabindex="0">
          <td><b>${esc(t.name)}</b><span class="sub mono">${esc(t.task_id)}</span></td><td>${taskState(t.status)}</td>
          <td>${esc(t.target_system)}<span class="sub qname">${esc(t.target_partition)}</span></td>
          <td class="sizing">${sizingText(t.sizing)}</td><td class="mono">${esc(t.job_id || "—")}</td><td class="n">${fig(t.attempts)}</td>
          <td class="act">${ACTIVE.includes(t.status) ? `<button class="btn btn--sm btn--danger" data-act="cancel-task" data-id="${esc(t.task_id)}">Cancel</button>` : ""}</td></tr>`).join("")}</tbody></table></div>`
        : f.tasks.length ? emptyState("list", "No active tasks", "Nothing is queued or running in this channel.", `<button class="btn btn--sm" data-act="task-filter" data-v="all">Show all ${f.tasks.length}</button>`)
        : emptyState("list", "No tasks yet", "Agents submit through this channel's URL.", `<button class="btn btn--sm" data-act="copy" data-copy="${esc(url)}" data-what="Channel URL">${icon("copy")}Copy the URL</button>`)}</div>`);
  } else if (S.channelTab === "releases") {
    const rels = await api(`/api/releases?channel_id=${encodeURIComponent(c.id)}`);
    if (S.view !== "channel" || S.channelId !== c.id || S.channelTab !== "releases") return;
    const build = (b) => b ? `<span class="tag tag--${BUILD_TONE[b.status]}"${b.error || b.dir ? ` data-tip="${esc((b.error || b.dir).slice(0, 140))}"` : ""}>${esc(b.status)}<i>#${b.number}</i></span>` : `<span class="tag">not aligned</span>`;
    paint(body, `<div class="panel">${rels.length ? `<div class="scroll-x"><table class="t"><thead><tr><th>Version</th><th>Source</th><th>Environment</th><th>Machines</th><th>Created</th></tr></thead><tbody>
      ${rels.slice().reverse().map((r) => `<tr><td class="top"><b>R${r.version}</b><span class="sub mono">${esc(r.tree.slice(0, 12))}</span></td>
        <td class="top">${esc(r.source.kind)} <span class="mono">${esc(r.source.path)}${r.source.commit ? "@" + esc(r.source.commit.slice(0, 10)) : ""}</span><span class="sub">${plural(r.files, "file")} · ${n(Math.round(r.bytes / 1024))} KB</span></td>
        <td class="top mono">${esc(r.env || "—")}</td>
        <td class="top">${r.systems.map((sys) => `<div class="row" style="margin-bottom:4px">${esc(sys)} ${build(r.builds[sys])}</div>`).join("")}</td>
        <td class="top muted" title="${esc(fmtDate(r.created_at))}">${ago(r.created_at)}</td></tr>`).join("")}</tbody></table></div>`
      : emptyState("layers", "No releases yet", "A release is code, environment and machines as one version. Agents publish them through this channel's URL (diamond-agent-core release create).")}</div>`);
  } else if (S.channelTab === "activity") {
    const all = S.events.filter((e) => e.channel === c.id);
    const list = all.filter((e) => S.eventFilter === "all" || (S.eventFilter === "agent" && e.kind.startsWith("AGENT")) || (S.eventFilter === "bad" && BAD.includes(e.kind)));
    const seg = (v, label) => `<button data-act="event-filter" data-v="${v}"${S.eventFilter === v ? ' class="is-active"' : ""}>${label}</button>`;
    paint(body, `<div class="toolbar"><span class="caption">${plural(all.length, "event")} · newest first</span><div class="seg">${seg("all", "All")}${seg("agent", "Agent decisions")}${seg("bad", "Failures and refusals")}</div></div>
      <div class="panel">${list.map((e) => eventRow(e, { ref: true })).join("") || emptyState("activity", all.length ? "Nothing matches this filter" : "No activity yet", "Submissions, dispatches, retries, exec, staged files and refusals land here.")}</div>`);
  } else {
    const mp = S.machinePolicy, hours = (v) => v == null ? "∞" : `${(+v).toFixed(1)} h`;
    const spent = (b) => b.cap == null ? `${b.used.toFixed(1)} node-h <span class="muted">· no cap</span>`
      : `<span class="meter lvl-${level(b.used / b.cap)}"><span class="meter__track"><span class="meter__fill" style="width:${pct(b.used / b.cap)}"></span></span></span><span class="sub">${b.used.toFixed(1)} of ${b.cap} node-h</span>`;
    const machineRows = f.machines.map((sys) => { const sr = c.rules.systems[sys], b = c.budget.machines[sys]; return `<tr><td><b>${esc(sys)}</b></td>
      <td>${sr.account ? `<span class="mono">${esc(sr.account)}</span>` : `<span class="muted">catalog default per queue</span>`}</td><td>${spent(b)}</td><td class="n">${hours(b.machine_budget)}</td><td class="n"><b>${hours(b.left)}</b></td></tr>`; }).join("");
    const queueRows = Object.keys(c.allowed).map((k) => { const [sys, part] = k.split(":"); const sr = c.rules.systems[sys], pr = sr.partitions ? sr.partitions[part] : {}; const s = systemOf(sys), rec = s && s.partitions.find((r) => r.partition === part); const b = c.budget.queues[k];
      const acct = pr.account ? [pr.account, "queue"] : sr.account ? [sr.account, "machine"] : rec ? [rec.account, "catalog"] : null;
      return `<tr><td>${esc(sys)}</td><td><span class="qname">${esc(part)}</span>${rec ? `<span class="sub">${esc(hwText(rec))}</span>` : ""}</td>
        <td>${acct ? `<span class="mono">${esc(acct[0])}</span><span class="sub">from the ${acct[1]}</span>` : `<span class="muted">catalog default</span>`}</td>
        <td class="n"><b>${c.allowed[k]}</b><i class="unit">of ${mp.partition_limits[k] ?? mp.max_nodes_per_job}</i></td><td>${spent(b)}</td><td class="n"><b>${hours(b.left)}</b></td></tr>`; }).join("");
    paint(body, `<p class="hint" style="margin-bottom:12px">This channel's rules sit inside the machine rules. Node-hours are nodes × wall time: what a job requested while it runs, what it used once it ends. Max pending jobs per machine: <b>${c.rules.max_user_pending_per_system ?? mp.max_user_pending_per_system}</b> (machine rule ${mp.max_user_pending_per_system}).</p>
      <div class="panel"><div class="panel__title"><strong>Machines</strong><span>the channel's budget on each machine</span></div><div class="scroll-x"><table class="t"><thead><tr><th>Machine</th><th>Account</th><th>Node-hours used</th><th class="n">Machine budget</th><th class="n">Left</th></tr></thead><tbody>${machineRows}</tbody></table></div>${machineRows ? "" : emptyState("server", "No machines", "Agents in this channel can submit nothing until you enable a queue.")}</div>
      <div class="panel"><div class="panel__title"><strong>Queues</strong><span>where agents may submit, and how large</span></div><div class="scroll-x"><table class="t"><thead><tr><th>Machine</th><th>Queue</th><th>Account</th><th class="n">Max nodes / job</th><th>Node-hours used</th><th class="n">Left</th></tr></thead><tbody>${queueRows}</tbody></table></div></div>`);
  }
}

/* ------------------------------------------------------------ task inspector */
async function renderInspector() {
  const id = S.view === "channel" && S.channelTab === "tasks" ? S.task : null;
  $("#app").classList.toggle("has-inspector", !!id);
  $("#inspector").hidden = !id;
  if (!id) return;
  const t = await api(`/api/tasks/${id}`).catch((e) => { if (e.status === 404) return null; throw e; });
  if (S.task !== id) return;
  if (!t) { S.task = null; syncHash(false); toast("That task no longer exists", "bad"); return render(); }
  const env = t.spec.envelope, ev = t.evaluation, pl = t.placement;
  const fact = (k, v) => `<dt>${k}</dt><dd>${v}</dd>`;
  $("#insp-title").textContent = t.spec.name;
  paint($("#insp-actions"), ACTIVE.includes(t.status) ? `<button class="btn btn--sm btn--danger" data-act="cancel-task" data-id="${esc(t.task_id)}">Cancel task</button>` : "");
  paint($("#insp-body"), `
    <div class="block"><dl class="facts">
      ${fact("Status", taskState(t.status))}
      ${fact("Queue", `${esc(t.target_system)} · <span class="qname">${esc(t.target_partition)}</span>`)}
      ${fact("Sizing", esc(sizingLong(pl && pl.sizing)))}
      ${fact("Account", `<span class="mono">${esc(pl ? pl.account : ev ? ev.account : "—")}</span>`)}
      ${fact("Job", `<span class="mono">${esc(t.job_id || "—")}</span>`)}
      ${fact("Attempts", t.attempts)}
      ${t.spec.release != null ? fact("Release", `R${t.spec.release}${t.release_build ? ` · build #${t.release_build.number}` : ""}`) : ""}
      ${fact("Created", esc(fmtDate(t.created_at)))}
      ${fact("Task id", `<span class="mono">${esc(t.task_id)}</span>`)}</dl></div>
    <div class="block"><div class="label"><span>Command</span><button class="btn btn--sm btn--ghost" data-act="copy" data-copy="${esc(t.spec.command)}" data-what="Command">${icon("copy")}Copy</button></div><pre class="codebox">${esc(t.spec.command)}</pre></div>
    <div class="block"><div class="label"><span>Envelope the agent asked for</span></div><dl class="facts">
      ${fact("GPUs", `${range(env.gpu.min_count, env.gpu.max_count)}${env.gpu.min_memory ? ` · at least ${esc(env.gpu.min_memory)} each` : ""}`)}
      ${fact("Cores", range(env.cpu.min_count, env.cpu.max_count))}
      ${fact("Memory", env.memory.min || env.memory.max ? `${esc(env.memory.min || "any")}–${esc(env.memory.max || "any")}` : "any")}
      ${fact("Wall time", `${esc(env.walltime.min)}–${esc(env.walltime.max)}`)}
      ${fact("Accelerators", esc(env.compatibility.accelerators.join(", ") || "any"))}</dl></div>
    <div class="block"><div class="label"><span>Queue evaluation at submit</span>${ev ? state(ev.feasible ? "feasible" : "not feasible", ev.feasible ? "good" : "bad") : ""}</div>
      ${ev ? `<dl class="facts" style="margin-bottom:10px">${fact("Est. wait", ev.est_wait_minutes != null ? `${ev.est_wait_minutes.toFixed(0)} min` : "—")}${fact("Free then", `${n(freeOf(ev.record)[0])} ${freeOf(ev.record)[1]} · ${n(ev.record.pending_jobs)} jobs pending`)}</dl>
        ${ev.verdicts.map((v) => `<div class="verdict verdict--${v.passed ? "pass" : "fail"}">${icon(v.passed ? "check" : "x")}<b>${esc(v.rule)}</b><span>${esc(v.detail)}</span></div>`).join("")}` : `<span class="muted">No evaluation recorded.</span>`}</div>
    <div class="block"><div class="label"><span>Timeline</span></div>${t.timeline.map((e) => eventRow(e, { stack: true })).join("")}</div>`);
}

/* ------------------------------------------------------------ channel editor (create / edit) */
function openChannelSheet(c) {
  S.sheetChannel = c || null;
  $("#channel-sheet-title").textContent = c ? `Edit ${c.name}` : "New channel";
  $("#channel-sheet-status").textContent = "Rules can narrow the machine rules, never widen them.";
  const mp = S.machinePolicy;
  const rules = c ? c.rules : { systems: {}, max_user_pending_per_system: null };
  const blocks = S.resources.filter((s) => s.partitions.length).map((s) => {
    const sr = rules.systems[s.name];
    const enabled = !!sr || !c;                                  // a new channel starts with every machine on
    const machineBudget = mp.node_hours[s.name];
    const id = esc(s.name);
    const parts = s.declared_partitions.map((p) => {
      const key = esc(`${s.name}:${p}`), machineCap = mp.partition_limits[`${s.name}:${p}`] ?? mp.max_nodes_per_job, blocked = mp.blocked_partitions.includes(`${s.name}:${p}`);
      const pr = sr && sr.partitions ? sr.partitions[p] : null, on = blocked ? false : (sr ? (sr.partitions == null || !!pr) : true);
      const rec = s.partitions.find((r) => r.partition === p), off = blocked ? " disabled" : "";
      return `<tr${blocked ? ' class="is-blocked"' : ""}><td><label class="check"><input type="checkbox" data-part="${key}"${on ? " checked" : ""}${off}><span class="qname">${esc(p)}</span></label>${blocked ? ` <span class="tag tag--bad">blocked on the machine</span>` : ""}</td>
        <td class="muted">${rec ? esc(hwText(rec)) : ""}</td>
        <td><span class="unit-input"><input type="number" min="1" max="${machineCap}" data-cap="${key}" value="${pr && pr.max_nodes_per_job != null ? pr.max_nodes_per_job : ""}" placeholder="${machineCap}"${off}><span>of ${machineCap}</span></span></td>
        <td><span class="unit-input"><input type="number" min="0.1" step="0.5" data-qh="${key}" value="${pr && pr.node_hours != null ? pr.node_hours : ""}" placeholder="∞"${off}><span>node-h</span></span></td>
        <td><input type="text" list="accts-${id}" data-qacct="${key}" data-catalog-acct="${esc(rec ? rec.account : (s.accounts[0] || ""))}" value="${esc(pr && pr.account ? pr.account : "")}" placeholder="${esc((sr && sr.account) || (rec ? rec.account : s.accounts[0]) || "")}" title="Account billed on this queue; empty = the default shown" spellcheck="false"${off}></td></tr>`;
    }).join("");
    return `<div class="rules-system${enabled ? "" : " is-off"}" data-system="${id}">
      <div class="rules-system__head"><label class="check"><input type="checkbox" class="switch" data-sys="${id}"${enabled ? " checked" : ""}>${id}</label><span class="muted">${esc(providerOf(s))}</span>
        <div class="rules-system__fields">
          <label>Account <input type="text" list="accts-${id}" data-account="${id}" value="${esc(sr && sr.account ? sr.account : "")}" placeholder="catalog default per queue" title="Account this channel bills on every queue of ${id} unless a queue says otherwise; type any account you hold" spellcheck="false"></label>
          <label>Node-hour cap <span class="unit-input"><input type="number" min="0.1" step="1" data-mh-cap="${id}" value="${sr && sr.node_hours != null ? sr.node_hours : ""}" placeholder="∞"><span>${machineBudget != null ? `of ${machineBudget} on the machine` : "no machine budget"}</span></span></label></div>
        <datalist id="accts-${id}">${s.accounts.map((a) => `<option value="${esc(a)}">`).join("")}</datalist></div>
      <div class="rules-system__body scroll-x"><table class="t rules-table"><thead><tr><th>Queue</th><th>Hardware</th><th>Max nodes / job</th><th>Node-hours</th><th>Account</th></tr></thead><tbody>${parts}</tbody></table></div></div>`;
  }).join("");
  $("#channel-sheet-body").innerHTML = `<div class="form-grid">
      <label class="field">Name <input type="text" id="ch-name" value="${esc(c ? c.name : "")}" placeholder="e.g. llm-finetune" spellcheck="false"></label>
      <label class="field">Description <input type="text" id="ch-desc" value="${esc(c ? c.description : "")}" placeholder="what this channel is for (shown to you, not to the agent)" spellcheck="false"></label>
      <label class="field">Max pending jobs / machine <input type="number" id="ch-pending" min="1" max="${mp.max_user_pending_per_system}" value="${rules.max_user_pending_per_system ?? ""}" placeholder="${mp.max_user_pending_per_system}"></label></div>
    <p class="hint">Switch on the machines and tick the queues this channel's agents may use. An empty field means the machine's cap, the catalog's account, no budget.</p>
    ${blocks || emptyState("server", "No machines in the pool yet", "Machines appear here after their first probe.")}`;
  $("#channel-sheet").hidden = false;
  $("#ch-name").focus();
}
function channelRulesFromForm() {
  const systems = {};
  $$("#channel-sheet-body .rules-system").forEach((block) => {
    if (!$("[data-sys]", block).checked) return;
    const partitions = {};
    const num = (sel) => { const v = $(sel, block).value; return v ? +v : null; };
    $$("[data-part]", block).forEach((cb) => { if (!cb.checked || cb.disabled) return; const key = cb.dataset.part, q = CSS.escape(key); partitions[key.split(":").slice(1).join(":")] = { max_nodes_per_job: num(`[data-cap="${q}"]`), node_hours: num(`[data-qh="${q}"]`), account: $(`[data-qacct="${q}"]`, block).value.trim() || null }; });
    systems[block.dataset.system] = { account: $("[data-account]", block).value.trim() || null, partitions, node_hours: num("[data-mh-cap]") };
  });
  const pending = $("#ch-pending").value;
  return { systems, max_user_pending_per_system: pending ? +pending : null };
}
async function saveChannel() {
  const btn = $("#btn-channel-save"), status = $("#channel-sheet-status"), editing = S.sheetChannel;
  const body = { name: $("#ch-name").value.trim(), description: $("#ch-desc").value.trim(), rules: channelRulesFromForm() };
  if (!body.name) { status.textContent = "Give the channel a name."; $("#ch-name").focus(); return; }
  btn.disabled = true; status.textContent = "Saving…";
  try {
    const saved = editing ? await send("PUT", `/ui/channels/${editing.id}`, body) : await send("POST", "/ui/channels", body);
    $("#channel-sheet").hidden = true;
    if (!editing) S.connect[saved.id] = true;                   // a new channel: show how to hand it over
    await fetchAll();
    openChannel(saved.id, editing ? "rules" : "tasks");
    toast(editing ? "Channel rules saved" : `Channel ${saved.name} created`);
  } catch (e) { status.textContent = `Not saved: ${e.message}`; } finally { btn.disabled = false; }
}
async function deleteChannel() {
  const c = channelOf(S.channelId), active = channelFacts(c).active.length;
  if (!await ask({ title: `Delete channel ${c.name}?`, body: `Its URL stops working at once${active ? ` and its ${plural(active, "active task")} ${active === 1 ? "is" : "are"} cancelled` : ""}. This cannot be undone.`, confirm: "Delete channel" })) return;
  await api(`/ui/channels/${c.id}`, { method: "DELETE" });
  await fetchAll(); go("home"); toast(`Channel ${c.name} deleted`);
}
async function cancelTask(el) {
  const t = S.tasks.find((x) => x.task_id === el.dataset.id);
  if (!await ask({ title: `Cancel ${t.name}?`, body: "The task stops and its batch job is cancelled on the machine.", confirm: "Cancel task" })) return;
  await send("POST", `/api/tasks/${t.task_id}/cancel`);
  await refresh(); toast(`${t.name} cancelled`);
}

/* ------------------------------------------------------------ machine rules (operator policy) */
async function openPolicy() {
  $("#policy-status").textContent = "Machine rules bound every channel; a channel can only narrow them.";
  S.policy = await api("/api/policy");
  renderPolicySheet();
  $("#policy-sheet").hidden = false;
}
function renderPolicySheet() {
  const p = S.policy;
  const rec = (r) => { const s = systemOf(r.system); return s && s.partitions.find((x) => x.partition === r.partition); };
  $("#policy-body").innerHTML = `<div class="form-grid" style="grid-template-columns:repeat(2,minmax(0,240px))">
      <label class="field">Default max nodes per job <input type="number" id="p-nodes" min="1" value="${p.max_nodes_per_job}"></label>
      <label class="field">Max pending jobs per machine <input type="number" id="p-pending" min="1" value="${p.max_user_pending_per_system}"></label></div>
    <div class="panel"><div class="panel__title"><strong>Node-hour budgets</strong><span>shared by every channel, first come first served</span></div><div class="scroll-x"><table class="t rules-table"><thead><tr><th>Machine</th><th>Budget</th><th class="n">Used by all channels</th></tr></thead><tbody>
      ${S.resources.filter((s) => s.partitions.length).map((s) => `<tr><td><b>${esc(s.name)}</b></td>
        <td><span class="unit-input"><input type="number" min="0.1" step="1" data-budget="${esc(s.name)}" value="${p.node_hours[s.name] ?? ""}" placeholder="∞"><span>node-h</span></span></td>
        <td class="n">${p.node_hours_used[s.name].toFixed(1)} h</td></tr>`).join("")}</tbody></table></div></div>
    <div class="panel"><div class="panel__title"><strong>Queues</strong><span>the largest job any channel may build; a blocked queue disappears from every channel</span></div><div class="scroll-x"><table class="t rules-table"><thead><tr><th>Machine</th><th>Queue</th><th>Hardware</th><th class="n">Nodes</th><th>Max nodes / job</th><th>Blocked</th><th class="n">Channels</th></tr></thead><tbody>
      ${p.partitions.map((r) => { const x = rec(r); return `<tr${r.blocked ? ' class="is-blocked"' : ""}>
        <td>${esc(r.system)}</td><td><span class="qname">${esc(r.partition)}</span></td><td class="muted">${x ? esc(hwText(x)) : "—"}</td><td class="n">${r.nodes_total != null ? n(r.nodes_total) : "—"}</td>
        <td><span class="unit-input"><input type="number" min="1"${r.nodes_total ? ` max="${r.nodes_total}"` : ""} data-cap="${esc(r.key)}" value="${r.override ?? ""}" placeholder="${p.max_nodes_per_job}"><span>${r.override != null ? "override" : `default ${p.max_nodes_per_job}`}</span></span></td>
        <td><input type="checkbox" data-block="${esc(r.key)}"${r.blocked ? " checked" : ""} aria-label="Block ${esc(r.key)}" title="No channel may use this queue"></td>
        <td class="n">${fig(channelsOn(r.key).length)}</td></tr>`; }).join("")}</tbody></table></div>${p.partitions.length ? "" : emptyState("server", "No queues in the pool yet", "Queues appear here after the first probe.")}</div>`;
}
async function savePolicy() {
  const btn = $("#btn-policy-save"), status = $("#policy-status");
  btn.disabled = true; status.textContent = "Saving…";
  const body = { max_nodes_per_job: +$("#p-nodes").value || null, max_user_pending_per_system: +$("#p-pending").value || null,
    partition_limits: Object.fromEntries($$("#policy-body [data-cap]").map((el) => [el.dataset.cap, el.value ? +el.value : null])),
    node_hours: Object.fromEntries($$("#policy-body [data-budget]").map((el) => [el.dataset.budget, el.value ? +el.value : null])),
    blocked_partitions: $$("#policy-body [data-block]").filter((el) => el.checked).map((el) => el.dataset.block) };
  try { S.policy = await send("PUT", "/ui/policy", body); renderPolicySheet(); status.textContent = "Saved. Every channel's effective rules follow."; toast("Machine rules saved"); refresh(); }
  catch (e) { status.textContent = `Not saved: ${e.message}`; } finally { btn.disabled = false; }
}

/* ------------------------------------------------------------ notifications */
function renderBell() {
  const notable = S.events.filter((e) => NOTABLE(e.kind));
  $("#bell-dot").hidden = !notable.some((e) => new Date(e.ts).getTime() > S.lastSeen);
  paint($("#bell-list"), notable.slice(0, 30).map((e) => { const c = e.channel ? channelOf(e.channel) : null; return eventRow(e, { stack: true, ref: !!c, prefix: c ? `<b>${esc(c.name)}</b> · ` : "" }); }).join("")
    || emptyState("bell", "Nothing notable yet", "Agent decisions, refusals and failures show up here."));
}

/* ------------------------------------------------------------ auth */
function renderAuth() {
  const a = S.auth, needs = a.required && !a.authenticated;
  $("#signin-sheet").hidden = !needs;
  if (needs) {
    $("#btn-signin").hidden = !a.web_login_available;
    const err = new URLSearchParams(location.search).get("auth_error"), box = $("#signin-error");
    box.hidden = !err && a.web_login_available;
    box.textContent = err ? `Sign-in did not complete (${err}). Try again; if it repeats, check the redirect URI registered for your Globus app.`
      : "Browser sign-in is not configured on this core (GLOBUS_CLIENT_ID / GLOBUS_CLIENT_SECRET). Use the shell login below.";
  }
  $("#identity").hidden = !a.required;
  if (!a.required) return;
  const u = a.user || {};
  const name = u.name || u.email || u.username || (a.mode === "web-session" ? "Globus user" : a.authenticated ? "Signed in" : "Not signed in");
  $("#identity-name").textContent = name;
  $("#identity-sub").textContent = a.mode === "web-session" ? (u.email || "web session · auto-refresh") : (a.hint || a.mode);
  $("#identity-avatar").textContent = a.authenticated ? name.trim()[0] : "!";
  $("#identity-avatar").classList.toggle("is-out", !a.authenticated);
  $("#btn-signout").hidden = a.mode !== "web-session";
  $("#btn-websignin").hidden = !(a.web_login_available && a.mode !== "web-session");
}

/* ------------------------------------------------------------ views, routing */
/* deep links: #home · #channel/<id>[/releases|/activity|/rules|/task/<task id>] · #machine/<system>[/history|/connection] · #limits · #new-channel */
function hashFor() {
  if (S.view === "machine") return "machine/" + encodeURIComponent(S.machine) + (S.machineTab !== "overview" ? "/" + S.machineTab : "");
  if (S.view === "channel") return "channel/" + encodeURIComponent(S.channelId) + (S.task ? "/task/" + S.task : S.channelTab !== "tasks" ? "/" + S.channelTab : "");
  return "home";
}
function syncHash(push) { const h = "#" + hashFor(); if (location.hash !== h) history[push ? "pushState" : "replaceState"](null, "", h); }
async function render() {
  renderAuth(); renderRail(); renderBell();
  $$(".view").forEach((v) => v.classList.toggle("is-active", v.id === `view-${S.view}`));
  if (S.view === "home") renderHome(); else if (S.view === "machine") await renderMachine(); else await renderChannel();
  await renderInspector();
}
/* A click moves between pages with one history entry (Back works); following
   the address bar (load, Back, Forward) only brings the page in line with it. */
function go(view, push = true) {
  S.view = view; syncHash(push);
  $("#app").classList.remove("rail-open"); $("#scroll").scrollTop = 0;
  return render();
}
function openMachine(name, tab, push) { S.machine = name; S.machineTab = tab || "overview"; S.task = null; return go("machine", push); }
function openChannel(id, tab, push) { S.channelId = id; S.channelTab = tab || "tasks"; S.task = null; return go("channel", push); }
function openTaskIn(channelId, taskId, push = true) {
  const t = S.tasks.find((x) => x.task_id === taskId);
  if (t && !ACTIVE.includes(t.status)) S.taskFilter = "all";
  S.channelId = channelId; S.channelTab = "tasks"; S.task = taskId; S.view = "channel"; syncHash(push);
  return render();
}
function applyHash() {
  const [head, ...rest] = decodeURIComponent(location.hash.slice(1)).split("/");
  if (head === "machine" && rest[0]) return openMachine(rest[0], rest[1], false);
  if (head === "channel" && rest[0]) return rest[1] === "task" && rest[2] ? openTaskIn(rest[0], rest[2], false) : openChannel(rest[0], rest[1], false);
  if (head === "limits") openPolicy();
  if (head === "new-channel") openChannelSheet(null);
  return go("home", false);
}
async function refresh() {
  try { await fetchAll(); }
  catch (e) { $("#pulse-led").className = "led led--bad"; $("#pulse-text").textContent = `core unreachable · ${e.message}`; return; }
  await render();
}

/* ------------------------------------------------------------ wiring: one handler per kind of input */
const ACT = {
  home: () => go("home"),
  menu: () => $("#app").classList.toggle("rail-open"),
  "open-channel": (el) => openChannel(el.dataset.id),
  "open-machine": (el) => openMachine(el.dataset.id),
  "new-channel": () => openChannelSheet(null),
  "edit-channel": () => openChannelSheet(channelOf(S.channelId)),
  "delete-channel": deleteChannel,
  "save-channel": saveChannel,
  policy: openPolicy,
  "save-policy": savePolicy,
  tab: (el) => { if (el.dataset.scope === "machine") S.machineTab = el.dataset.t; else { S.channelTab = el.dataset.t; S.task = null; } syncHash(false); return render(); },
  "task-filter": (el) => { S.taskFilter = el.dataset.v; return render(); },
  "event-filter": (el) => { S.eventFilter = el.dataset.v; return render(); },
  task: (el) => { S.task = S.task === el.dataset.id ? null : el.dataset.id; syncHash(false); return render(); },
  "task-ref": (el) => { $("#bell-pop").hidden = true; return openTaskIn(el.dataset.channel, el.dataset.id); },
  "close-task": () => { S.task = null; syncHash(false); return render(); },
  "cancel-task": cancelTask,
  copy: (el) => copyText(el.dataset.copy, el.dataset.what),
  "toggle-connect": () => { S.connect[S.channelId] = !S.connect[S.channelId]; return render(); },
  bell: () => { $("#bell-pop").hidden = !$("#bell-pop").hidden; },
  "bell-clear": () => { S.lastSeen = Date.now(); localStorage.setItem("da.lastSeen", String(S.lastSeen)); renderBell(); },
  signout: async () => { await send("POST", "/api/auth/logout"); return refresh(); },
  "close-sheet": (el) => closeSheet(el.dataset.id),
  "confirm-yes": () => { $("#confirm-sheet").hidden = true; S.confirm(true); },
};
document.addEventListener("click", (e) => {
  $("#tip").hidden = true;
  if (!e.target.closest("#bell-pop, #btn-bell")) $("#bell-pop").hidden = true;
  if (!e.target.closest("#rail, [data-act='menu']")) $("#app").classList.remove("rail-open");
  const el = e.target.closest("[data-act]");
  if (el) (async () => ACT[el.dataset.act](el))().catch((err) => toast(err.message, "bad"));
});
document.addEventListener("keydown", (e) => {
  if ((e.key === "Enter" || e.key === " ") && e.target.matches("tr[data-act]")) { e.preventDefault(); e.target.click(); }
  if (e.key !== "Escape") return;
  const sheet = $(".backdrop:not([hidden]):not(#signin-sheet)");
  if (sheet) closeSheet(sheet.id);
  else if (!$("#bell-pop").hidden) $("#bell-pop").hidden = true;
  else if (S.task) ACT["close-task"]();
});
$$(".backdrop:not(#signin-sheet)").forEach((bd) => bd.addEventListener("mousedown", (e) => { if (e.target === bd) closeSheet(bd.id); }));
document.addEventListener("change", (e) => { if (e.target.matches("[data-sys]")) e.target.closest(".rules-system").classList.toggle("is-off", !e.target.checked); });
document.addEventListener("input", (e) => {       // queues inherit the machine-level account unless they say otherwise
  if (e.target.matches("[data-account]")) $$("[data-qacct]", e.target.closest(".rules-system")).forEach((q) => { q.placeholder = e.target.value.trim() || q.dataset.catalogAcct; });
});
document.addEventListener("pointermove", (e) => {
  const el = e.target.closest("[data-tip]"), tip = $("#tip");
  tip.hidden = !el;
  if (!el) return;
  tip.textContent = el.dataset.tip;
  tip.style.left = `${Math.min(Math.max(e.clientX, 120), innerWidth - 120)}px`; tip.style.top = `${e.clientY}px`;
});
document.addEventListener("scroll", () => { $("#tip").hidden = true; }, true);
window.addEventListener("popstate", applyHash);
setInterval(() => { if (!document.hidden && !$(".backdrop:not([hidden]):not(#signin-sheet)")) refresh(); }, POLL_MS);
fetchAll().then(applyHash, (e) => { $("#pulse-led").className = "led led--bad"; $("#pulse-text").textContent = `core unreachable · ${e.message}`; });
