# Diamond Agent — Architecture & Functional Design

Diamond Agent runs locally, serves a local web UI, and presents a user's
federated HPC allocations (Diamond / Globus Compute) as **one abstract
supercomputer**: tasks describe *what they need* (a ResourceEnvelope); the core
tells the agent, per queue, whether and at what size it can run there, runs it
on the queue the agent chose, and watches it. The intelligence — writing the
envelope and the job, choosing the queue — lives in the agent driving the core.

Four design pillars, each mapped to a package:

| # | Pillar | Package |
|---|--------|---------|
| 1 | Federated resource abstraction | `diamond_agent/federation/` |
| 2 | ResourceEnvelope interface (written by the calling agent) | `diamond_agent/schemas.py` |
| 3 | Per-queue feasibility and sizing (the agent chooses) | `diamond_agent/scheduler/` |
| 4 | Execution, monitoring, in-place continuation | `diamond_agent/execution/`, `diamond_agent/agent/orchestrator.py` |

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
│  │ Federation     │  │ Evaluator       │  │ TaskManager (orchestrator)   │  │
│  │ Collector      │  │ rules → sizing  │  │ lifecycle on the chosen      │  │
│  │ latest+history │  │ → wait estimate │  │ queue, in-place continuation │  │
│  └───────┬────────┘  │   per queue     │  └──────┬────────────┬──────────┘  │
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

**Single-clock design:** one tick loop drives everything. Each tick advances
refreshes the unified snapshot, then runs the monitor —
so continuation decisions always see a snapshot at most one tick old. Blocking
work (probes) runs via `asyncio.to_thread`; the API stays responsive.

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
│ channels.py      one URL + one rule set per conversation of agents    │
└───────────────┬───────────────────────┬──────────────────┬───────────┘
                │ /api (operator)       │ /c/<token>/api   │ Python
        web/ (static UI)        other agents (Claude Code, …)   in-process
```

* **Core** owns everything that touches HPC systems, tracks tasks and
  monitors the federation. Every capability is a method on
  `DiamondAgentCore` (`build`, `tick`, `resources`, `idle`, `candidates`,
  `submit(target)`, `task`, `tasks`, `wait`, `cancel`, `retarget`, `events`,
  `history`). `api.py` is a thin router over it; the CLI and
  the MCP server proxy to the HTTP API with `core/client.py`.
* **Frontend** (`web/`) is a static client of `/api` and nothing else. It is
  mounted by `main.py` only when `DIAMOND_AGENT_UI=1` (default);
  `DIAMOND_AGENT_UI=0` (`make core`) runs the core headless for other agents.
* **Other agents** drive the core through their **channel URL**
  (`<core>/c/<token>`, section 2.4) with the CLI (`diamond-agent-core
  resources | idle | candidates | submit --target … | task | wait | retarget`),
  the MCP tools of the same names, or in-process. The Claude Code skill
  `.claude/skills/diamond-agent-core/SKILL.md` documents the workflow (look at
  the pool → specify the job → ask for candidates → choose and submit → track
  → retarget) and how to read statuses and evaluations.
* **The operator** uses the web UI on the unscoped `/api` (every machine,
  every channel) and the operator-only `/ui` routes (machine rules, channels).

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
```

Semantics: `min` = floor below which the task cannot run; `max` = ceiling
beyond which more resources are wasted; **the scheduler chooses the concrete
shape inside the interval** (elasticity is the point). The calling agent
writes the envelope — the skill gives the schema and rules of thumb and
recommends a subagent for it and for the job command — and the core validates
it; it never guesses one. Fan-out children inherit the parent's.

### 2.3 Evaluations and tasks

- `Candidate` = record + per-rule `verdicts[]` + `feasible` + `sizing` +
  `est_wait_minutes`: one queue evaluated for one envelope. `POST
  /api/candidates` returns one per queue; the agent chooses among them.
- `TaskRecord` (orchestrator) = spec + the chosen `target_system`/
  `target_partition` + that queue's `evaluation` at submit + `placement`
  (what was sent to Slurm) + job id + attempts/counters + full event `timeline`.

### 2.4 Channels (`channels.py`)

A **channel** is the operator's unit of delegation: `{id, name, description,
token, rules}` where `rules = {systems: {System: {account?, node_hours?,
partitions?: {queue: {account?, max_nodes_per_job?, node_hours?}}}},
max_user_pending_per_system?}`. Nothing set means
no limit; the account a queue bills is the queue's, else the machine's, else
the catalog's — typed by the operator (one per queue; the catalog's accounts
are offered as suggestions, not enforced). Agents are
handed `<core>/c/<token>`; every route under it is scoped to the channel —
`status`/`candidates` show only its queues, billed to its account, with its
caps; tasks and events belong to it; other channels' tasks do not exist for
it. Channel rules **narrow** the machine rules and never widen them:
`ChannelRegistry.validate` refuses a cap above the machine's, a queue blocked
on the machine; `allowed(channel)`
recomputes the effective caps as `min(channel, machine)` on every use, so an
operator edit to the machine rules applies to all channels at once. The
registry reads the store on each use (channels are few), which is also what
lets the operator's terminal (`python -m diamond_agent.channels`, `make
channels`) create and revoke channels on the store with the core running.
Every core has the `default` channel (id `default`: every machine, machine
rules), created with the core and refused by `delete`, and prints its URL at
start as the line to hand agents — a headless deployment is usable without
ever opening the UI.

**Node-hour budgets** (`budget.py`). A job costs nodes × hours — the requested
wall time while it is active, the elapsed time `sacct` reports once it ended
(`TaskRecord.elapsed_minutes`). The ledger is derived from the task records
on every use. Three optional limits bound what a channel may still commit on
a queue: the channel's cap on the queue, its cap on the machine, and the
machine's own budget (machine rules `node_hours[M]`), which every channel
shares first come, first served. The evaluator shrinks a sizing to fit the
headroom (nodes first, then wall time) and marks the queue infeasible with a
`budget` verdict when even the minimum shape does not fit; the dispatch gate
re-checks a retry. Channels are created, edited and deleted only by the
operator (`/ui/channels`, operator cookie); deleting one cancels its active
tasks and kills its URL.

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

Consequence: **submit to any queue of your channel, chosen by the agent.**
`POST /api/tasks` (under the channel URL) takes `target: {system, partition}`
(required). The core evaluates that queue for the envelope under the
channel's effective rules (section 4), refuses one outside the channel or
infeasible with 409 and the verdicts, otherwise sizes and submits billed to
the channel's account. `POST /api/candidates` runs the same
evaluation over the whole pool so the agent can choose; `GET /api/resources`
lists every system of every provider in one shape. The core never ranks or
places — the agent has the task context (data locality, wheel architecture,
deadlines) that a ranking inside the core would lack.

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

Endpoints are login-node endpoints the user owns; multi-user endpoints are
not supported (per-user worker placement, queued workers).
2–4 s warm probes with zero queueing and zero GPU cost.

## 4. Per-queue evaluation (`scheduler/`)

`placement.evaluate(record, envelope, policy) -> Candidate` is the whole of
what the core decides about placement:

```
one queue + one envelope ──► rule chain (verdicts, feasible)
                        ──► elastic sizing inside the envelope and the operator's cap
                        ──► wait estimate
```

`TaskManager.candidates` runs it over every record for `POST /api/candidates`
(the agent chooses from the result); `submit`/`retarget` run it on the chosen
queue and refuse an infeasible one with the verdicts.

### 4.1 Deterministic rule chain (`rules.py`) — authoritative

| Rule | Rejects when |
|---|---|
| `endpoint_health` | endpoint not ONLINE |
| `authorization` | no allocation account / partition blocked by operator policy |
| `cpu_arch` | node arch ∉ envelope compatibility |
| `accelerator` | GPU required but absent, or type ∉ allowed set |
| `gpu_memory` | per-GPU memory < envelope floor |
| `capacity` | min-viable node count > the partition's per-job node cap (`policy.py::effective_node_cap`: per-`System:partition` override, else `max_nodes_per_job`) or > usable nodes |
| `walltime` | partition cap < envelope minimum |
| `container` | container required but runtime absent |
| `user_limits` | user's pending jobs on system ≥ policy cap |
| `budget` (after sizing) | even the minimum shape exceeds the node-hours the channel may still commit on the queue (section 2.4) |

Every verdict carries a human-readable detail; `candidates`, the task's
evaluation and the UI show the full chain, so the agent sees exactly which
range or constraint to change when a queue is infeasible.

The per-job node cap (`policy.py::effective_node_cap`) bounds the `capacity`
rule and the high end of `choose_sizing`; the orchestrator's dispatch gate
(`_policy_gate`) re-checks the sizing right before `executor.submit`, so an
in-place retry that re-uses a sizing the operator has since lowered ends in
`POLICY_DENIED` instead of an over-cap `sbatch`.

### 4.2 Elastic sizing

Start from the max-useful shape; shrink toward min-viable until it fits the
partition's idle capacity (start sooner beats run bigger); clamp to the
queue's cap and walltime to the effective wall-time cap. Result: `Sizing
{nodes, gpus, cpus, walltime_minutes}` — shown per queue in `candidates`
before the agent commits.

### 4.3 Wait estimate

`estimate_wait` prefers **resource-level signals**: if the sized request fits
`gpus_free`/`cpus_free`, the wait is modeled as backfill-friendly
(`2 + 0.05·pending`); else node-idle (`1 + 0.2·pending`); else deep-queue
(`5 + 1.5·pending`). It is a hint for the agent's choice, not a promise.

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
| `qos` | partition | Anvil binds accounts to mandatory QOS |
| `mem_directive: false` | system | TACC rejects `--mem` (whole-node) |
| `gpu_directive: false` | system | TACC rejects gres directives |
| `account` | partition | GPU vs CPU sub-accounts (e.g. proj-gpu / proj-cpu) |
| `diamond_dir` | system | remote workdir override (default `$HOME/diamond`) |
| `constraint` | partition | NERSC selects the node type with `-C gpu`/`-C cpu` (`gpu&hbm80g` = 80 GB A100) |
| `partition_directive: false` | system | NERSC's job_submit plugin assigns the Slurm partition from constraint + QOS; no `--partition` is emitted |
| `slurm_partition` | partition | the `sinfo`/`squeue` name a logical catalog partition maps to (NERSC `gpu` → `gpu_ss11`); used by the probe and, where `--partition` is emitted, by sbatch |
| `gres_probe: false` | system | skip the per-node `sinfo -N … GresUsed` query (> 60 s on Perlmutter's ~5,000 nodes); free GPUs fall back to idle nodes × GPUs per node. The probe only ever asks for the system's GPU partitions and caps that query at 30 s |
| `container_runtime` | system | `apptainer`/`singularity` → `srun <runtime> exec --nv`; `shifter` → `#SBATCH --image=docker:…` + `srun shifter`; `podman-hpc` → `srun podman-hpc run --rm [--gpu]` (image refs are rewritten per runtime) |

The per-system reasons behind these switches — and everything else an agent must
know before submitting to a given machine — are collected in the skill's
`references/systems.md` (served at `GET /skill/systems`).

Command handling: task commands run **raw** in the batch script (they may be
full shell snippets; `srun <loop>` cannot launch them) with `$`/backticks
escaped so the unquoted heredoc doesn't pre-expand them at write time. On
GPU partitions at least one GPU is always requested (sites reject 0-GPU
jobs there) unless `gpu_directive` is off. Memory requests come from the
envelope floor, never the node size.

`bind_job(job_id, system)` re-attaches persisted in-flight jobs after an
agent restart, so status/cancel survive process lifecycles.

## 6. Orchestrator (`agent/orchestrator.py`)

Owns `TaskRecord`s; persists every mutation; mirrors every event to the task
timeline and the global event feed. It works on the queue the agent chose and
never moves a task on its own.

### 6.1 Lifecycle

```
submit(target) ─► evaluate the queue ─► Infeasible (409 + verdicts, no task)
                                     └► TASK_SUBMITTED ─► JOB_SUBMITTED ─► PENDING ─► RUNNING ─► COMPLETED
                                                     │                                      │
                                                     ├─ policy gate / sbatch error ─► FAILED  └─ FAILED ─► in-place continuation
retarget(target) ─► evaluate the new queue ─► Infeasible (task untouched)
                                           └► cancel job ─► TASK_RETARGETED ─► JOB_SUBMITTED …
```

### 6.2 In-place continuation

| Trigger | Guard | Action | Event |
|---|---|---|---|
| job failed, note says timeout/walltime | retries left, wall time below envelope max and queue cap | resubmit on the same queue with wall time doubled | `AGENT_RESIZED` |
| job failed, any other note | retries left | retry on the same queue | `AGENT_RETRY` |
| retries exhausted / auto off / wall time at cap | — | FAILED | `AGENT_GAVE_UP` |
| sizing above the (changed) cap or queue blocked at dispatch | — | FAILED | `POLICY_DENIED` |
| sbatch error at dispatch | — | FAILED | `JOB_SUBMIT_FAILED` |
| agent `retarget` | new queue feasible | cancel, resubmit there | `TASK_RETARGETED` |

What is deliberately **not** here: migration to another queue, pending-time
escalation, a parked UNSCHEDULABLE state, fan-out task groups. The agent
watches `status`/`candidates` and retargets or re-specifies when it wants to;
the core reports and enforces.

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
  What separates the **operator** from an **agent** is therefore not identity
  but surface: agents hold a **channel URL** (`/c/<token>/…`, the token is
  the channel's credential and scopes everything to it); operator-only routes
  live under `/ui` (`PUT /ui/policy`, `/ui/channels`), are mounted only when
  the web UI is served, are excluded from the OpenAPI schema
  and the generated skill reference, and require the operator cookie
  (`webauth.OPERATOR_COOKIE`: a per-process secret handed to the browser that
  loads `/`, never written to disk). The headless core has no mutation route
  for limits. The agent-facing routes are policy-checked (`policy.py`:
  inspection-only `exec`, workdir-only `files`, node caps on `tasks`) and
  audited (`SHELL_EXEC`, `FILE_STAGED`, `POLICY_DENIED`, `POLICY_CHANGED`
  events). Credential
  isolation — the agent process not holding the core's Globus tokens — is a
  deployment property (run the core under its own account, hand agents only
  `DIAMOND_AGENT_URL`); see README *Keeping agents on the interface*.

## 8. Configuration

- **`.env`** (dotenv, real env wins): tick, DB path, live credentials.
  `.env.example` documents every knob.
- **Channels** — in the SQLite `channels` table, created in the web UI
  (never in the YAML); the catalog's `accounts` list is what a channel may
  pick an account from.
- **`sites.live.yaml`** — the federation catalog: per system
  `{name, endpoint, container_runtime, accounts[],
  mem_directive?, gpu_directive?, partition_directive?, diamond_dir?, partitions[]}`; per partition
  `{name, slurm_partition?, constraint?, account?, qos?, arch, gpu_type, gpus_per_node, gpu_mem_gb,
  cpus_per_node, mem_per_node_gb, nodes_total, max_walltime, max_nodes_per_job?}`;
  plus a `policy` block (`max_user_pending_per_system`, `max_nodes_per_job`,
  `blocked_partitions[]`, optional `partition_limits{}`) consumed by the rule
  chain. **Effective policy** = `policy.build_policy(catalog, overrides)`:
  the block, per-partition caps from partition entries, and the operator's
  overrides from the `settings` table (`policy_overrides`, edited in the web
  UI) on top — one dict held by the scheduler and read by the orchestrator
  gate, updated in place by `DiamondAgentCore.update_policy`.
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
  (`TaskRecord.timeline`, `GET /api/tasks/{id}`), with the queue's
  and the queue's evaluation at submit (verdicts, sizing, wait estimate) attached.
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
  through the new endpoint), `make preflight`. If the site needs a new
  sbatch dialect, add a catalog switch (§5.1) rather than a code path, and
  give the machine a section in the skill's `references/systems.md`.
- **Add a continuation policy**: implement trigger+guard+action in the
  orchestrator; emit a named event; it is automatically part of timelines,

- **Known next steps**: parallel per-system probes (round duration →
  max instead of sum); per-system probe intervals; learned wait model
  (predicted-vs-sacct data already logged)
