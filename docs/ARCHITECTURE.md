# Diamond Agent — Architecture & Functional Design

Diamond Agent runs locally, serves a local web UI, and presents a user's
federated HPC allocations (Diamond / Globus Compute) as **one abstract
supercomputer**: tasks describe *what they need* (a ResourceEnvelope), and an
agentic meta-scheduler decides *where and at what size* they run, then keeps
them alive across queue stalls, job failures, and endpoint outages.

Four design pillars, each mapped to a package:

| # | Pillar | Package |
|---|--------|---------|
| 1 | Federated resource abstraction | `diamond_agent/federation/` |
| 2 | ResourceEnvelope interface | `diamond_agent/schemas.py`, `diamond_agent/agent/envelope_gen.py` |
| 3 | Hybrid AI + rule meta-scheduler | `diamond_agent/scheduler/` |
| 4 | Execution, monitoring, event-triggered continuation | `diamond_agent/execution/`, `diamond_agent/agent/orchestrator.py` |

---

## 1. System overview

```
                       ┌───────────────────────────────────────────────┐
                       │              Local Web UI (dark)              │
                       │  Overview · Idle · Tasks · Activity · Resource│
                       └───────────────┬───────────────────────────────┘
                                       │ REST (poll, 3s)
┌──────────────────────────────────────▼──────────────────────────────────────┐
│ FastAPI app (main.py + api.py)                                              │
│                                                                             │
│  ┌───────────────┐   tick loop (DIAMOND_AGENT_TICK, async→thread)           │
│  │ core.tick()   │──► 1. pool.probe()  (every provider, one snapshot)      │
│  └───────────────┘    2. collector.refresh()  ───► ResourceSnapshot         │
│                       3. manager.tick()       ───► monitor + continuation   │
│                                                                             │
│  ┌────────────────┐  ┌─────────────────┐  ┌──────────────────────────────┐  │
│  │ Federation     │  │ MetaScheduler   │  │ TaskManager (orchestrator)   │  │
│  │ Collector      │  │ rules → sizing  │  │ lifecycle, groups,           │  │
│  │ latest+history │  │ → wait → score  │  │ event-triggered continuation │  │
│  └───────┬────────┘  │ → AI ranking    │  └──────┬────────────┬──────────┘  │
│          │           └─────────────────┘         │            │             │
│  ┌───────▼─────────────────┐            ┌────────▼───────┐ ┌──▼──────────┐  │
│  │ FederationSource        │            │ Executor       │ │ Store       │  │
│  │  reserved: SSH · cloud  │            │  (per provider)│ │ SQLite:     │  │
│  │  live: GlobusProbe      │            │  live: Globus  │ │ tasks/groups│  │
│  └───────┬─────────────────┘            └────────┬───────┘ │ /events     │  │
│          │      Globus Compute (auth.py: 3 credential modes)└─────────────┘ │
└──────────┼───────────────────────────────────────┼──────────────────────────┘
           │ ShellFunction probes                  │ ShellFunction sbatch/sacct/scancel
   ┌───────▼───────────────────────────────────────▼──────────┐
   │  Federation: MEPs + self-owned login-node endpoints      │
   │  Delta@NCSA · DeltaAI@NCSA · Anvil@RCAC · Vista@TACC ·…  │
   └──────────────────────────────────────────────────────────┘
```

degrades to deterministic heuristics when credentials are absent).

**Single-clock design:** one tick loop drives everything. Each tick advances
refreshes the unified snapshot, then runs the monitor —
so continuation decisions always see a snapshot at most one tick old. Blocking
work (probes, AI calls) runs via `asyncio.to_thread`; the API stays responsive.

---

## 1.5 diamond-agent-core vs. the frontend

The service is split into two products that share one process by default:

```
┌───────────────────────── diamond-agent-core ─────────────────────────┐
│ providers/ (HPC via Globus Compute · SSH · cloud)  → ResourcePool     │
│ federation/collector  scheduler/  agent/orchestrator  store.py  auth │
│ core/service.py  DiamondAgentCore — the ONE programmatic facade       │
│ api.py           HTTP interface  /api  (+ OpenAPI at /docs)           │
│ core/client.py   stdlib HTTP client      core/cli.py  `diamond-agent-core`
│ core/mcp_server.py  MCP tools (optional extra)                        │
└───────────────┬───────────────────────┬──────────────────┬───────────┘
                │ /api                  │ CLI / MCP        │ Python
        web/ (static UI)        other agents (Claude Code, …)   in-process
```

* **Core** owns everything that touches HPC systems, tracks tasks and
  monitors the federation. Every capability is a method on
  `DiamondAgentCore` (`build`, `tick`, `resources`, `idle`, `draft_envelope`,
  `submit(target=…)`, `task`, `tasks`, `wait`, `cancel`, `replan`, `events`,
  `history`). `api.py` is a thin router over it; the CLI and
  the MCP server proxy to the HTTP API with `core/client.py`.
* **Frontend** (`web/`) is a static client of `/api` and nothing else. It is
  mounted by `main.py` only when `DIAMOND_AGENT_UI=1` (default);
  `DIAMOND_AGENT_UI=0` (`make core`) runs the core headless for other agents.
* **Other agents** drive the core through the CLI (`diamond-agent-core
  resources | idle | envelope | submit --target … | task | wait | replan`),
  through the MCP tools of the same names, or in-process. The Claude Code
  skill `.claude/skills/diamond-agent-core/SKILL.md` documents the workflow
  (look at the pool → draft envelope → submit auto or pinned → track →
  intervene) and how to read statuses and decisions.

## 2. Data model (`schemas.py`)

### 2.1 Federated resource abstraction

`ResourceRecord` — one (endpoint, partition) at one instant:

| Field group | Fields |
|---|---|
| identity | `timestamp, endpoint, system, partition, account` |
| hardware (catalog-sourced) | `node_arch, gpu_type, gpus_per_node, gpu_mem_gb, cpus_per_node, mem_per_node_gb, container_runtime` |
| node states (live) | `nodes_idle, nodes_mixed, nodes_allocated, nodes_down` |
| resource-level availability (live) | `gpus_free` (GRES), `cpus_free` |
| queues (live) | `running_jobs, pending_jobs, user_running_jobs, user_pending_jobs` |
| limits/health | `max_walltime_minutes` (= min(live, catalog cap)), `endpoint_health` |

`ResourceSnapshot` = timestamped list of records + `aggregates()` (GPUs free,
cores free, queue totals — GRES-level when available).

Design decision: **node states and resource-level availability are both kept**.
On shared-node systems every node can be `mixed` while GPUs are exhausted (or
free); `gpus_free`/`cpus_free` are the truth for placement, node states for
operators. (Lesson §10 of the evaluation.)

### 2.2 ResourceEnvelope

```yaml
gpu:      {min_count, max_count, min_memory}     # per-GPU memory floor
cpu:      {min_count, max_count}
memory:   {min, max}                             # per-node request floor
walltime: {min, max}                             # min useful … cap
compatibility: {cpu_arch[], accelerators[], container_required}
objective: earliest_completion | max_throughput | min_queue_impact
```

Semantics: `min` = floor below which the task cannot run; `max` = ceiling
beyond which more resources are wasted; **the scheduler chooses the concrete
shape inside the interval** (elasticity is the point). Envelopes come from
four sources, all recorded with provenance + confidence: `user`, `ai`
(claude-opus-5 structured output), `heuristic` (keyword fallback), `group`
(inherited by fan-out children).

### 2.3 Decisions and tasks

- `Candidate` = record + per-rule `verdicts[]` + `feasible` + `sizing` +
  `est_wait_minutes` + `score` + `ai_rank` + `rationale`.
- `PlacementDecision` = all candidates (kept for explainability), `chosen`,
  `mode` (`rules` | `ai+rules`), `summary`, `decision_ms`.
- `TaskRecord` (orchestrator) = spec + envelope provenance + decision +
  placement + job id + attempts/exclusions/counters + full event `timeline`.
- `TaskGroup` = parent task fanned into per-round children
  (`distinct_systems` soft pinning, round continuation).

Everything is Pydantic; task records round-trip through SQLite as JSON.

---

## 3. Federation layer (`federation/`)

### 3.0 Compute provider layer (`providers/`) — all compute resources in one

The federation is no longer bound to one resource kind. A **provider** owns a
set of resources of one kind and implements a single contract
(`providers/base.py::ComputeProvider`): `systems()` (catalog identity),
`probe()` (live state as `ResourceRecord`s) and `submit()/status()/cancel()`
(execution), plus `shell(system, command, timeout_s)` / `workdir(system)` for the agent-facing environment inspection (env probe, bounded exec, file staging, job logs). `ResourcePool` (`providers/pool.py`) stacks providers into one
virtual pool and is what the collector and the orchestrator consume — it is
simultaneously the `FederationSource` and the `Executor`, routing every call
to the provider that owns the system and remembering which provider owns each
job (rehydrated after restarts via `bind_job`).

| Provider | Kind | Status | Backing |
|---|---|---|---|
| `GlobusHpcProvider` | `globus_hpc` | active | `GlobusFederationSource` + `GlobusExecutor` — Slurm probes and sbatch through Diamond / Globus Compute endpoints |
| `SshServerProvider` | `ssh` | **reserved** | agent-less OpenSSH: `nvidia-smi` + `/proc` probe (parser implemented and unit-checked), `nohup` launch; disabled until `enabled`/`execution` are set per server |
| `CloudProvider` | `cloud` | **reserved** | contract only: one record per (region, instance family), quota headroom as idle nodes |

Two consequences, matching the product goals:

1. **Submit to any resource.** `POST /api/tasks` accepts an optional
   `target: {system, partition?}`. A pinned task is scheduled only on that
   target (the scheduler still sizes it inside the envelope); continuation
   retries *in place* instead of migrating, and an offline pinned endpoint
   parks the task (`UNSCHEDULABLE`) until it returns or the pin is lifted
   (`POST /tasks/{id}/replan {clear_target: true}`). `GET /api/resources`
   lists every system of every provider in one shape.
2. **Or let the platform choose.** Without `target`, the hybrid
   meta-scheduler (section 4) decides across the whole pool — the second
   product goal is the existing decision pipeline applied to the union of
   providers.

`ResourceRecord` gained `provider` (kind) and `tags`, plus `utilization()` /
`free_units()` helpers so the UI, the idle finder and the queue-history
heatmap share one definition of "busy": free GPUs (GRES) on GPU partitions,
free cores on CPU partitions, node states only as a fallback.

### 3.1 Collector

Source-agnostic: anything with `probe() -> list[ResourceRecord]`. Keeps the
latest snapshot plus a bounded in-memory history. **Probe failure never
empties the pool** — the previous snapshot survives a bad round.

### 3.2 `GlobusFederationSource`

Per system, per refresh:

1. `get_endpoint_status` → health (ONLINE/OFFLINE); failures ⇒ OFFLINE.
2. If online: one **ShellFunction probe** — a heredoc'd Python (3.6-compatible)
   that runs on the endpoint and prints a single JSON blob:
   - `sinfo -h -o "%R|%D|%T|%l"` → per-partition idle/mixed/alloc/down +
     time limit (full Slurm state-suffix set `*~#%$@+-!` stripped);
   - `sinfo -h -o "%R|%C"` → free CPU cores per partition;
   - `sinfo -h -N -O Partition,StateCompact,Gres,GresUsed` → **free GPUs**
     per partition on usable nodes (GRES-level);
   - `squeue` global + `-u $USER` → queue depths and the user's own jobs;
   - `sacctmgr` → accounts; `platform.machine()` → arch.
3. Merge: **catalog supplies hardware facts** (GPU model/memory, cores — sinfo
   can't see these), **probe supplies dynamics**; per-partition `account`
   override beats the system default; effective walltime cap =
   `min(live limit, catalog cap)` so the catalog acts as a budget guard.
4. Probe errors mark the system DEGRADED (records kept, health rule will
   reject them) — never crash the round.

MEP nuance: probes ride `create_batch(user_endpoint_config=…)` so multi-user
endpoints can place their worker (account/partition/worker_init) — identical
to task submission. Self-owned login-node endpoints need no config and give
2–4 s warm probes with zero queueing and zero GPU cost.

## 4. Meta-scheduler (`scheduler/`)

Pipeline per decision (`meta.py`):

```
snapshot ──► rule chain (per record) ──► feasible set
         ──► elastic sizing (inside envelope, per candidate)
         ──► wait estimate ──► heuristic score (objective-aware)
         ──► [optional] AI ranking of the feasible set
         ──► chosen candidate  (full trace retained)
```

### 4.1 Deterministic rule chain (`rules.py`) — authoritative

| Rule | Rejects when |
|---|---|
| `endpoint_health` | endpoint not ONLINE |
| `authorization` | no allocation account / partition blocked by policy |
| `cpu_arch` | node arch ∉ envelope compatibility |
| `accelerator` | GPU required but absent, or type ∉ allowed set |
| `gpu_memory` | per-GPU memory < envelope floor |
| `capacity` | min-viable node count > per-job cap or > usable nodes |
| `walltime` | partition cap < envelope minimum |
| `container` | container required but runtime absent |
| `user_limits` | user's pending jobs on system ≥ policy cap |

Every verdict carries a human-readable detail; the UI shows the full chain
per candidate. **AI can only order candidates that passed every rule.**

### 4.2 Elastic sizing

Start from the max-useful shape; shrink toward min-viable until it fits the
partition's idle capacity (start sooner beats run bigger, for
earliest-completion); clamp walltime to the effective cap. Result: `Sizing
{nodes, gpus, cpus, walltime_minutes}`.

### 4.3 Wait estimation & scoring

Wait estimator prefers **resource-level signals**: if the sized request fits
`gpus_free`/`cpus_free`, the wait is modeled as backfill-friendly
(`2 + 0.05·pending`); else node-idle; else deep-queue
(`5 + 1.5·pending`).

Objective-aware score (lower = better): `earliest_completion` = wait +
runtime model (runtime shrinks with granted share of max-useful size);
`max_throughput` = −capability + wait; `min_queue_impact` = queue depth +
footprint. A CPU-only task carries a heavy penalty on GPU partitions (live
sites force GPU requests there — waste).

### 4.4 AI ranking (`ai_ranker.py`)

claude-opus-5 receives the task, envelope, objective, and per-candidate
facts (hardware, node states, GRES free, queues, proposed sizing, heuristic
score) and returns a structured `RankingResult` (rank + per-candidate
rationale + summary). Hard validation: the returned set must equal the
feasible set exactly, else it is discarded. Any API failure ⇒ silent
fallback to heuristic ordering (`mode` records which path ran). Measured
trade-off: a few percent better placement at ~36 s vs 0.12 ms per decision —
the reason the hybrid design exists.

---

## 5. Execution layer (`execution/`)

`Executor` protocol: `submit(task, placement) -> JobHandle`,
`status(job_id) -> (state, note)`, `cancel(job_id)`.

### 5.1 `GlobusExecutor`

Submission = render `templates/submit.sh.j2` (heredoc-writes a `.submit`
file then `sbatch`s it — the diamond-admin-backend pattern), ship as a
ShellFunction, parse `Submitted batch job <id>`, then track via remote
`sacct` and cancel via `scancel`.

**Site-dialect switches** (per system/partition in the catalog — each one
exists because a real site demanded it):

| Switch | Where | Why |
|---|---|---|
| `user_endpoint_config` | system | MEP worker placement (account/partition/worker_init) |
| `qos` | partition | Anvil binds accounts to mandatory QOS |
| `mem_directive: false` | system | TACC rejects `--mem` (whole-node) |
| `gpu_directive: false` | system | TACC rejects gres directives |
| `account` | partition | GPU vs CPU sub-accounts (e.g. proj-gpu / proj-cpu) |
| `diamond_dir` | system | remote workdir override (default `$HOME/diamond`) |

Command handling: task commands run **raw** in the batch script (they may be
full shell snippets; `srun <loop>` cannot launch them) with `$`/backticks
escaped so the unquoted heredoc doesn't pre-expand them at write time. On
GPU partitions at least one GPU is always requested (sites reject 0-GPU
jobs there) unless `gpu_directive` is off. Memory requests come from the
envelope floor, never the node size.

`bind_job(job_id, system)` re-attaches persisted in-flight jobs after an
agent restart, so status/cancel survive process lifecycles.

## 6. Orchestrator (`agent/orchestrator.py`)

Owns `TaskRecord`s and `TaskGroup`s; persists every mutation; mirrors every
event to the task timeline and the global event feed.

### 6.1 Lifecycle

```
submit ─► PLACEMENT_DECIDED ─► JOB_SUBMITTED ─► PENDING ─► RUNNING ─► COMPLETED
   │              │                                │           │
   │              └─ no feasible ─► UNSCHEDULABLE  │           └─ FAILED ─► continuation
   │                                (periodic retry, exclusions reset)
   └─ submit error ─► exclude system, try next (JOB_SUBMIT_FAILED)
```

### 6.2 Event-triggered continuation catalog

| Trigger | Guard | Action | Event |
|---|---|---|---|
| job failed (timeout/walltime note) | retries left | double walltime within caps, resubmit in place | `AGENT_RESIZED` |
| job failed (site-flavored note) | retries left | exclude system, re-place | `AGENT_MIGRATED` |
| job failed (first, generic) | retries left | retry same placement (transient) | `AGENT_RETRY` |
| retries exhausted / auto off | — | FAILED | `AGENT_GAVE_UP` |
| endpoint OFFLINE while PENDING | auto on | **pin dissolved**, cancel best-effort (`JOB_ORPHANED` if endpoint dead), re-place elsewhere | `ENDPOINT_DOWN` + `AGENT_MIGRATED` |
| queue wait > max(16 m, 3×estimate) | once per task; better alternative exists | migrate to it | `PENDING_TIMEOUT` + `AGENT_MIGRATED` |
| group child pinned & PENDING, **all siblings terminal** | auto on | relax distinct-systems pin, re-place for earliest completion | `AGENT_CONSTRAINT_RELAXED` |
| UNSCHEDULABLE parked | every 5 ticks | clear exclusions, re-run scheduler | (periodic) |
| user `replan` (optionally with `exclude[]`) | — | constrained or free re-placement | `AGENT_REPLAN` |

Measured behavior (live): endpoint-death detection ≤ one refresh period;
detection→resubmission 34 s; sibling-relaxation fired 9 s after the last
sibling finished.

### 6.3 Task groups

`submit_group(name, child_specs, rounds, distinct_systems)`: children are
scheduled with sibling-used systems excluded (soft fan-out pinning). When a
round's children are all terminal: aggregate → if ≥ half succeeded and
rounds remain, launch the next round (`AGENT_CONTINUED`); else finalize.
The constraint-relaxation rule (above) prevents a pinned straggler from
stalling a round.

---

## 7. Auth (`auth.py`, `core/webauth.py`)

Two entry paths share one credential resolver:

```
standalone core ──► python -m diamond_agent.auth login   (native app: URL + one-time code)
   CLI / MCP /       DIAMOND_AGENT_TOKENS (Diamond cookie JSON) · service identity
   headless                                   │
                                              ▼
web frontend ───► /auth/login → auth.globus.org → /auth/callback
                  (confidential client, code flow, offline access)
                  → web session file (tokens by resource server, 0600)
                                              │
                                              ▼
                              auth.build_compute_client()
                    web session ▸ Diamond tokens ▸ service identity ▸ native cache
                                              │
                        RefreshTokenAuthorizer / AccessTokenAuthorizer / GlobusApp
                                              ▼
                     GlobusHpcProvider (probe + submit) — rebuilt on credential change
```

* **Standalone core**: unchanged. `check_headless_ready()` refuses to start
  live mode without credentials so a server never prompts.
* **Frontend**: `core/webauth.py` implements the Diamond-dashboard flow
  server-side — `GET /auth/login` (302 to Globus with `openid profile email`
  + the Compute scope, `access_type=offline`, CSRF `state` cookie),
  `GET /auth/callback` (state check, `ConfidentialAppAuthClient.
  oauth2_exchange_code_for_tokens`, id_token claims for display only,
  `auth.save_web_session`, httpOnly session cookie), `POST /api/auth/logout`,
  `GET /api/auth/session`. The token bundle stays in the core, not in the
  browser; the `funcx_service` token is wrapped in a `RefreshTokenAuthorizer`
  whose `on_refresh` persists renewals — the same job the dashboard's
  `/api/auth/refresh` route performs.
* **Runtime switch**: `auth.invalidate_client()` bumps a generation counter on
  sign-in/out; `GlobusFederationSource` / `GlobusExecutor` rebuild their
  client (and re-register the probe function) when the generation changes.
  Live mode may start unauthenticated only when the UI is served *and* web
  login is configured (`DiamondAgentCore.build(allow_unauthenticated=…)`);
  until sign-in the HPC provider reports `needs-login` and probes nothing.
* **Trust boundary**: the core is a local, single-identity service — every
  `/api` caller (UI, CLI, MCP) acts with the core's current credentials; the
  session cookie only tells the UI which browser signed in (`browser_bound`).

## 8. Configuration

- **`.env`** (dotenv, real env wins): tick, DB path,
  `ANTHROPIC_API_KEY` (optional AI), live credentials. `.env.example`
  documents every knob.
- **`sites.live.yaml`** — the federation catalog: per system
  `{name, endpoint, container_runtime, accounts[], user_endpoint_config?,
  mem_directive?, gpu_directive?, diamond_dir?, partitions[]}`; per partition
  `{name, account?, qos?, arch, gpu_type, gpus_per_node, gpu_mem_gb,
  cpus_per_node, mem_per_node_gb, nodes_total, max_walltime}`; plus a
  `policy` block (`max_user_pending_per_system`, `max_nodes_per_job`,
  `blocked_partitions[]`) consumed by the rule chain.
- **Makefile** — uv-native workflow: `setup*/live/core/login/endpoints/
  init-sites/preflight/smoke/clean-state`.
- **`scripts/hpc_endpoint_setup.sh`** — per-machine login-node endpoint
  bootstrap (uv-managed CPython, PATH symlink for the endpoint manager,
  idempotent).

**Preflight gate** (`preflight.py`): credentials → catalog completeness →
endpoint status + hostname sanity → timed real probe per system → optional
one-job submit/cancel smoke test. Live deployment is preflight-gated.

---

## 9. Observability

- **Task timeline** — every lifecycle step and agent action on a task
  (`TaskRecord.timeline`, `GET /api/tasks/{id}`), with the placement decision
  (candidates, rule verdicts, AI rank, rationale) attached.
- **Event feed** — the same events flattened across tasks (`events` table,
  `GET /api/events`, the UI's *Agent activity* view and notification bell).
- **Queue history** — the collector keeps the last 240 snapshots in memory
  (`GET /api/federation/history`) for the UI heatmap and `federation_status`.
- **Service log** — provider status, auth mode and probe failures are logged at
  INFO/WARNING by the process (`make live` / `make core` output).

## 10. Extension points

- **New resource kind** — implement `ComputeProvider` (`providers/base.py`),
  register it in `providers.build_pool`, declare its catalog section. The
  reserved `SshServerProvider` / `CloudProvider` are the templates; nothing
  above the pool (collector, scheduler, orchestrator, UI) changes.

- **Add a system**: run the endpoint script on its login node, `make
  endpoints`, add a catalog block (shapes verifiable in-band via scontrol
  through the new endpoint), `make preflight`.
- **Add a continuation policy**: implement trigger+guard+action in the
  orchestrator; emit a named event; it is automatically part of timelines,
- **Add a placement policy** (for evaluation): one function in
  `scheduler/policies.py`; the replay harness scores it with the production
  rule chain.
- **Known next steps**: parallel per-system probes (round duration →
  max instead of sum); per-system probe intervals; learned wait model
  (predicted-vs-sacct data already logged); pinned-task timeout escalation
  to user; DeltaAI MEP template fix upstream (admin-side).
