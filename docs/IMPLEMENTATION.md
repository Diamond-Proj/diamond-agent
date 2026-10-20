# Diamond Agent — Implementation Reference (as a Deployable Service)

Companion to [ARCHITECTURE.md](ARCHITECTURE.md) (which explains *why*); this
document explains *how each component is implemented* and how the whole thing
runs, persists, restarts, and is operated as a service.

---

## 0. Service form factor

| Property | Value |
|---|---|
| Process model | **One** `uvicorn` process (FastAPI app) + one asyncio background task (the tick loop). Blocking work runs via `asyncio.to_thread`; no worker pool, no message broker. |
| Network | Listens on `127.0.0.1:8642` (localhost-only by convention; UI + REST on the same port). **No inbound port is required by any other component.** All HPC interaction is *outbound* to Globus Compute cloud APIs. |
| Runtime | Python 3.13 (pinned in `.python-version`), dependencies locked in `uv.lock`, installed via `uv sync`. |
| Footprint (measured) | ~100 MB peak RSS, seconds of CPU per hour, ~25 outbound API calls + ~5 KB state per refresh round. |
| External dependencies | Globus Compute web service (task fan-out to endpoints), the HPC clusters' Slurm (indirectly, via endpoints). No model API: envelopes and job commands come from the calling agent. |
| Persistent state (working dir) | `.env`, `sites.live.yaml`, `diamond_agent.db` (SQLite), (append-only). Plus `~/.globus_compute/storage.db` (Globus tokens, user-level). |
| Resources | every resource is a live probe of a real system (no simulator); the reserved SSH/cloud providers register slots only |

### Startup sequence (`main.py:lifespan`)

1. `load_dotenv()` → module-level config (`MODE`, `TICK_SECONDS`, `DB_PATH`).
2. `wire()` — builds the object graph into `api.ctx`:
   catalog → providers → collector → policy → store → `ChannelRegistry`
   (creates the `default` channel when there is none) → `TaskManager`
   (which **restores tasks from SQLite and re-binds in-flight jobs to
   endpoints** via `Executor.bind_job`). Each channel's path is logged.
   In live mode, `check_headless_ready()` **refuses to start** if no cached
   Globus tokens exist (the server must never prompt).
3. One synchronous `_tick_once()` so the first snapshot exists before serving.
4. Spawn the tick loop; serve UI + REST.

### Shutdown / restart semantics

- SIGTERM: tick task cancelled; SQLite is transaction-per-write, so no
  flush step is needed. Kill-safe.
- On restart: tasks and timelines reload from SQLite; PENDING/RUNNING
  jobs re-bind to their endpoints (status/cancel keep working); the
  snapshot/history cache is rebuilt on the first tick (collector state is
  deliberately memory-only). Monitor tick counters (`pending_ticks` etc.)
  survive in the records; sacct remains the authoritative clock.

---

## 1. `main.py` — assembly & tick loop

- **Responsibility**: config, wiring, the single clock.
- **Key symbols**: `load_catalog()`, `wire()`, `_tick_once()`, `_loop()`,
  `lifespan()`, `app`.
- **Implementation notes**: `core.tick()` = ①
  ②`collector.refresh()` ③`manager.tick()` — strictly sequential so the
  monitor always sees a fresh snapshot. The whole tick runs in a worker
  thread; exceptions are logged and the loop continues (a bad round never
  kills the service). Step ② is timed and logged (duration, staleness,
  offline set, payload size, `getrusage`).
- **Config**: `DIAMOND_AGENT_SITES|TICK|DB|UI`; `.env` loaded with
  env-vars-win semantics.

## 2. `api.py` — REST surface

- **Responsibility**: everything the UI (or curl) can do. Sync handlers
  (FastAPI threadpool); all singletons on `api.ctx`.
- **Endpoints**:

Every `/api` route is mounted twice: under `/c/{token}/api` for agents
(scoped to that channel; the documented contract) and bare `/api` for the
operator's UI (unscoped; agent actions refused with 400). Operator-only
routes are under `/ui` (cookie).

| Method & path | Purpose |
|---|---|
| `GET /api/config` | auth state, your channel, systems, limits in force, providers |
| `GET /api/policy` | effective scheduling limits + one resolved row per partition (read-only) |
| `PUT /ui/policy` | **operator only** (UI-mounted, operator cookie): edit the machine rules — default + per-queue `max_nodes_per_job`, pending cap, blocked queues, per-machine `node_hours` budgets → `settings` table + in-place policy update + `POLICY_CHANGED` |
| `GET/POST /ui/channels`, `GET/PUT/DELETE /ui/channels/{id}` | **operator only**: channels with their URL path, rules, effective `allowed` caps and task counts; create/edit validated against the machine rules (400 with the reasons); delete cancels active tasks and kills the URL |
| `POST /api/resources/{system}/exec` | inspection-only shell (`policy.check_shell_command`), 403 + `POLICY_DENIED` otherwise; accepted runs logged as `SHELL_EXEC` |
| `POST /api/resources/{system}/files` | staging under the workdir only (`policy.check_stage_path`), logged as `FILE_STAGED` |
| `GET /api/federation` | latest snapshot + aggregates |
| `GET /api/federation/history?limit` | aggregate trend |
| `POST /api/candidates` | evaluate an envelope against every queue: verdicts, sizing, wait estimate, free units, caps — what the agent chooses from |
| `POST /api/tasks` | submit (command + envelope + the chosen `target`, all from the caller) — re-evaluates the queue, 409 + verdicts if infeasible, else submits synchronously and returns the task summary |
| `GET /api/tasks` / `GET /api/tasks/{id}` | summaries / full record (the queue's evaluation + timeline) |
| `POST /api/tasks/{id}/cancel` | cancel job + task |
| `POST /api/tasks/{id}/retarget` | move a task to another queue the agent chose (cancels its job); 409 + verdicts if that queue cannot run it |
| `GET /api/events?limit&task_id` | agent activity feed |

- **Ops note**: `GET /api/config` doubles as the health endpoint (cheap, no
  remote calls).

## 3. `auth.py` — Globus credentials

- **Responsibility**: one cached Globus Compute `Client` for probes and
  execution; CLI (`login/whoami/logout/endpoints/init-sites`).
- **Key symbols**: `build_compute_client(interactive=False)`, `_build_app()`,
  `check_headless_ready()`, `identity_description()`, `KNOWN_MACHINES`,
  `NonInteractiveAuthRequired`.
- **Implementation notes**: three credential modes (Diamond cookie-token →
  `AccessTokenAuthorizer`; confidential client; native login). The
  non-interactive GlobusApp injects a **raising LoginFlowManager** — this is
  what makes the service safe: the compute SDK's `gare_handler` otherwise
  starts console logins on policy-gated endpoints. Functions serialize by
  source (`AllCodeStrategies`) so endpoint Python versions are decoupled.
  `endpoints` status-checks only hostname-mapped known machines by default
  (unknown, policy-protected endpoints would trigger session re-auth).
  `init-sites` pre-fills partition shapes for known machines; multi-user endpoints are listed as unsupported.
  worker configs, and comments out offline endpoints.
- **Deployment**: run `make login` **once, interactively**, before first
  live start; tokens live in `~/.globus_compute/storage.db` (0600, per-user).


## Component: diamond-agent-core facade & agent surfaces (`diamond_agent/core/`)

**Role.** One programmatic facade for everything the service does, so the
frontend, the CLI, the MCP server and in-process agents call identical
methods. `main.py` assembles it (`DiamondAgentCore.build(catalog, mode,
db_path)`), drives it (`core.tick()` every `DIAMOND_AGENT_TICK` seconds) and
mounts the HTTP router; the web UI is mounted only when `DIAMOND_AGENT_UI=1`.

| File | Responsibility |
|---|---|
| `service.py` | `DiamondAgentCore`: assembly, `tick`, `info/providers`, `snapshot/history/resources/idle`, `candidates`, `submit(target)`, `tasks/task/wait/cancel/retarget`, `events`; `summarize_task` (the shared task row) |
| `client.py` | `CoreClient`: stdlib HTTP client mirroring the facade over `/api` (`DIAMOND_AGENT_URL`) |
| `cli.py` | `diamond-agent-core` console script (pyproject `[project.scripts]`): human tables by default, `--json` for agents, `submit --wait` |
| `mcp_server.py` | stdio MCP server (optional extra `mcp`, mcp ≥ 2.1): the operations as annotated tools (read-only / destructive / idempotent / open-world hints, structured results), the reference documents as resources (`diamond://skill`, `diamond://skill/systems`, `diamond://status`, `diamond://policy`), core refusals raised as `ToolError` so the model reads the reason, `wait_task` reporting progress. No tool changes limits. Tested through a real MCP session in `tests/test_mcp.py` |
| `../api.py` | FastAPI router: one facade call per route, tagged for OpenAPI (`/docs`) |
| `../budget.py` | node-hour ledger from the task records (`task_cost`, `Ledger`) and `remaining(channel, system, partition)` = min over the channel's queue cap, its machine cap and the shared machine budget |
| `service.py` (environment inspection) | `env(system)` = fixed login-node probe (`ENV_PROBE`, parsed into sections); `exec(system, cmd, timeout_s)` = `timeout N bash -c …` through the provider's `shell()`; `put_file(system, path, bytes)` = base64 round-trip into the workdir (≤2 MB); `task_logs(task_id, tail)` = tails of `<workdir>/logs/<task>.stdout|stderr` on the placement system |
| `../api.py` (`skill_router`) | `GET /skill` = SKILL.md + API reference generated from `app.openapi()`; `GET /skill/api`; `GET /api/status[?format=text]` = `core.status()` / `status_text()` |
| `../../.claude/skills/diamond-agent-core/SKILL.md` | agent-facing workflow + status/evaluation reading guide |

**Operational notes.** `make core` = headless core (`/` returns a service
descriptor, `/static` is not mounted). The CLI exits 2 with a clear message
when the core is unreachable. `wait` polls every 3 s over HTTP (in-process
`core.wait` every 2 s). A target is evaluated inside the caller's channel
before a task record is created: a queue outside the channel (or unknown) and
a queue that cannot run the envelope both answer `409` with the verdicts.

## Component: browser sign-in (`diamond_agent/core/webauth.py`, `auth.py` web session)

**Role.** Let the frontend authenticate through Globus OAuth while the
standalone core keeps the native-app login. Mounted only with
`DIAMOND_AGENT_UI=1` (`login_router`: `/auth/login`, `/auth/callback`,
`/auth/logout`); `session_router` (`/api/auth/session`, `/api/auth/logout`)
is always mounted.

| Piece | Behaviour |
|---|---|
| `auth.web_client_creds()` | `GLOBUS_CLIENT_ID`/`GLOBUS_CLIENT_SECRET` (aliases `PORTAL_*`, `NEXT_PUBLIC_GLOBUS_CLIENT_ID`) |
| `auth.load/save/clear_web_session()` | `$DIAMOND_AGENT_HOME/web_session.json` (dir 0700, file 0600): `session_id`, `created_at`, `tokens` by resource server, `user` claims |
| `auth._web_authorizer()` | `RefreshTokenAuthorizer(funcx_service)` with `on_refresh` persisting renewals; falls back to `AccessTokenAuthorizer` without a refresh token |
| `auth.invalidate_client()` / `client_generation()` | credential change → providers rebuild their Globus Compute client |
| `auth.auth_status(required)` | UI/CLI/MCP payload: mode, authenticated, web_login_available, user, hint — no network, no prompts |
| `webauth.exchange_code()` | the only network call of the flow (monkeypatched in tests) |

**Operations.** Register `<public url>/auth/callback` on the Globus app.
Behind a proxy set `DIAMOND_AGENT_PUBLIC_URL`. `diamond-agent-core auth`
shows the state; `POST /api/auth/logout` or the sidebar **Sign out** drops the
session and the core falls back to CLI/Diamond/service credentials if any.
Failure paths redirect to `/?auth_error=…` (`state_mismatch`,
`exchange_failed:…`, `no_compute_token` — the last one means the Compute scope
was not granted). Verified in-process with FastAPI's TestClient (login
redirect, state check, callback → session → authorizer → logout).

## Component: compute providers (`diamond_agent/providers/`)

**Role.** The "all compute resources in one" seam. Each provider adapts one
resource kind to a single contract (`base.py::ComputeProvider`: `systems()`,
`info()`, `probe()`, `submit()`, `status()`, `cancel()`); `pool.py::ResourcePool`
stacks them and is what the rest of the service consumes — it is the
collector's `FederationSource` *and* the orchestrator's `Executor`.

**Modules.**

| File | Responsibility |
|---|---|
| `base.py` | `ComputeProvider` protocol, `SystemInfo` (catalog identity), `ProviderInfo` (kind/label/status/detail), `ProviderNotReady` |
| `pool.py` | routing by system → provider; job → provider map with `bind_job` rehydration after restart; per-provider probe isolation (one broken provider never blanks the pool); `providers_info()` for the UI |
| `globus_hpc.py` | live adapter over `GlobusFederationSource` + `GlobusExecutor` (owns catalog `systems:` whose `provider` is unset or `globus_hpc`) |
| `ssh.py` | **reserved** SSH-server adapter: `PROBE_CMD` (`nvidia-smi` + `/proc`), pure `parse_probe()` / `probe_to_record()` (unit-checked offline), `nohup` launch; gated by `enabled` / `execution` flags per server |
| `cloud.py` | **reserved** cloud-pool adapter: contract + catalog schema only |
| `__init__.py` | `build_pool(catalog) -> ResourcePool` — the only place `main.py`/`core` touch |

**Configuration.** `sites.live.yaml`: `systems:` (HPC), plus
optional `ssh_servers:` and `cloud:` lists (schemas documented inline in the
catalogs and in each provider's docstring). Systems may carry `tags:` and
`location:` for the UI and the idle finder.

**Runtime behaviour.** `ResourcePool.probe()` concatenates provider probes
and stamps `record.provider`; `submit()` resolves the owning provider from
`placement.system`. Every task names its queue (`target` on `POST
/api/tasks`, `TaskRecord.target_system/target_partition`): the orchestrator
evaluates that one record, continuation retries in place, and the agent
moves a task with `retarget` when it wants to.

**Operational notes.** A provider whose probe raises is reported as
`status: error` with the message in `/api/providers`; its last good records
simply disappear from the snapshot (the orchestrator then treats jobs there
under the usual ENDPOINT_DOWN rules once the HPC source marks them offline).
Reserved providers report `reserved` (nothing configured) or `disabled`
(configured, not enabled) and never run anything silently — `submit` raises
`ProviderNotReady`, which surfaces as `JOB_SUBMIT_FAILED`.

## 4. `federation/` — the unified resource view

### 4.1 `collector.py`
`FederationCollector.refresh()` calls `source.probe()`, wraps records into a
timestamped `ResourceSnapshot`, keeps `latest` + a `deque(240)` history under
a lock. Probe exceptions keep the previous snapshot (log-and-continue).

### 4.2 `globus_probe.py` (live source)
- `PROBE_CMD`: a **single ShellFunction** whose payload is a heredoc'd,
  Python-3.6-compatible script (old login-node pythons) emitting one JSON
  line: partition node-state buckets (full Slurm suffix set `*~#%$@+-!`
  stripped; `mixed` is its own bucket), per-partition free CPUs (`%C`),
  **per-partition free GPUs** (per-node `Gres` vs `GresUsed` on usable
  nodes), global + `-u $USER` queue counts, accounts, arch.
- `GlobusFederationSource.probe()`: per system — endpoint status →
  (if online) probe via `create_batch()` on the login-node endpoint → merge with
  catalog (hardware facts from catalog; per-partition `account` override;
  `max_walltime = min(live, catalog)`); failures ⇒ DEGRADED records, never
  an empty round; counts its Globus API calls (`last_probe_calls`).
- **Known limitation**: systems are probed sequentially; parallelizing is
  the top performance TODO.

## 5. `scheduler/` — rule-based placement

### 5.1 `rules.py`
Nine pure functions `(record, envelope, policy) -> RuleVerdict`;
`evaluate_record` runs the chain and returns a `Candidate` with all
verdicts. `choose_sizing` implements envelope elasticity (max-useful,
shrink to idle capacity, never below min-viable; walltime clamped to the
effective cap). `min_nodes_needed` is the shared floor computation.

### 5.2 `placement.py`
`evaluate(record, envelope, policy) -> Candidate`: the rule chain's verdicts,
then — when feasible — `choose_sizing` and `estimate_wait` (prefers
`gpus_free`/`cpus_free`, the backfill model, over node-idle). That is the
whole of what the core decides about placement; `TaskManager.candidates`
runs it over every record for `POST /api/candidates`, and `submit` runs it
on the chosen queue and refuses an infeasible one.

## 6. `agent/` — orchestration

### 6.1 `orchestrator.py`
The service's state machine, on the queue the agent chose:

- `TaskManager.__init__`: restore tasks from the store; `bind_job`
  re-attachment for in-flight jobs.
- `candidates(envelope)` / `evaluate(envelope, system, partition)`: the
  per-queue evaluation; `evaluate` raises `Infeasible` (with the verdicts),
  which the API maps to 409.
- `submit(spec, target)`: evaluate the chosen queue → create the record with
  its `evaluation` → `_dispatch` (policy gate, then `executor.submit`;
  a gate refusal or a submit error ends the task FAILED with the reason —
  there is no "try the next system").
- `retarget(task_id, target)`: evaluate the new queue first (an infeasible
  one leaves the task untouched), cancel the current job, dispatch there.
- `tick()`: for each PENDING/RUNNING task → `executor.status` → state
  transitions and `pending_ticks`/`running_ticks`.
- In-place continuation: `_handle_failure` (retry budget; a wall-time hit →
  `_resize_in_place`, doubled within the envelope's maximum and the queue's
  cap; anything else → retry on the same queue; budget exhausted →
  `AGENT_GAVE_UP`). No migration, no pending-timeout escalation, no
  parked/UNSCHEDULABLE state: the agent watches `status`/`candidates` and
  retargets when it wants to.
- Every event goes to the task timeline (persisted) and the global feed.
- Concurrency: one `RLock` around all mutations; the tick thread and API
  threadpool share it; the core takes it to swap the policy dict.

## 7. `execution/` — batch-system I/O

### 7.1 `base.py`
`Executor` protocol (`submit/status/cancel`), `JobHandle`, `walltime_hms`.

### 7.2 `globus.py` (live)
- `submit()`: resolve system entry → site-dialect switches
  (`qos` per partition; `mem_directive`/`gpu_directive` per system;
  per-partition account came in via the record; `diamond_dir` workdir) →
  escape `$`/backticks in the command → render `templates/submit.sh.j2`
  (mkdir logs; heredoc-write `.submit`; `sbatch`) → ShellFunction with
  the login-node endpoint → parse `Submitted batch job (\d+)`; errors carry
  rc/stdout/stderr verbatim.
- `status()`: remote `sacct -j <id> -o State,ElapsedRaw -nP`, state mapped
  via `SLURM_STATE_MAPPING`, elapsed minutes returned alongside (what the
  budget ledger charges once a job ends); `cancel()`: remote `scancel`.
- `_job_endpoint` map + `bind_job()` for restart survival.
- GPU-partition invariant: request ≥1 GPU/node unless `gpu_directive: false`;
  memory request = envelope floor, never node size, unless
  `mem_directive: false`.

## 8. `store.py` — persistence

SQLite, WAL-free simple mode, `check_same_thread=False` + process-level
lock. Three tables:

| Table | Schema | Notes |
|---|---|---|
| `tasks` | `task_id PK, data JSON, updated_at` | full `TaskRecord` dump per mutation (upsert) |
| `events` | `id, ts, task_id, kind, message, detail JSON` | flat feed, append-only; also the audit trail of agent shell access and policy (`SHELL_EXEC`, `FILE_STAGED`, `POLICY_DENIED`, `POLICY_CHANGED`) |
| `settings` | `key PK, data JSON, updated_at` | operator settings; `policy_overrides` = the machine rules edited in the web UI, layered over the catalog at build time (the YAML is never rewritten) |
| `channels` | `channel_id PK, data JSON, updated_at` | one row per channel (name, token, rules); read on every use. The `default` row (id `default`) is created with the core and refused by delete; `python -m diamond_agent.channels` (`make channels`) lists, creates and deletes rows from the terminal for deployments without the UI. `events.channel` (added by an in-place `ALTER TABLE` on older databases) scopes the feed |

Backup = copy `diamond_agent.db` — safe while the service runs (short
transactions).
`make clean-state` removes local state without touching Globus tokens.


## 9. `web/` — local UI

Three static files (no build step): `index.html`, `app.js` (vanilla, polls
`/api` every 3 s, deep links `#home | #channel/<id>[/tasks|/activity|/rules|/task/<id>]
| #machine/<system>[/history|/connection] | #limits | #new-channel`),
`style.css` (light/dark tokens following the system preference; status
colors always paired with labels). One home page — a three-number strip,
channel cards (running or idle; running / pending / failed / done counts,
machines, last activity, one-click URL copy; failures only in the
notification bell, never as a channel state) over the machine wall — plus a
channel page (URL + copy-ready hand-over snippets for `DIAMOND_AGENT_URL` and
`claude mcp add`; Tasks with clickable rows and detail tabs overview / queue
evaluation / timeline, Cancel with confirmation; Activity; Rules) and a
machine page (queue cards with caps, accounts and the channels using each
queue; queue-history heatmap; connection facts). Two sheets: New/Edit channel
and Machine rules. The sidebar indexes channels and machines; each fact is
shown once per page. The UI submits nothing: it is the operator's monitor and
rule console on the unscoped `/api` + `/ui`; `DIAMOND_AGENT_UI=0` leaves it
unmounted.

## 10. `preflight.py` & `scripts/hpc_endpoint_setup.sh`

- **Preflight** — the deployment gate, ordered: credentials → catalog
  completeness (TODO markers, sim- endpoints) → endpoint status + hostname
  sanity → timed real probe per system (warns if a probe threatens the
  tick) → optional `--submit-test SYSTEM` (schedule → sbatch → sacct →
  scancel; zero SUs). Non-zero exit on any hard failure.
- **Endpoint bootstrap** (run on each login node): installs uv → uv-managed
  CPython 3.12 venv → `globus-compute-endpoint` → **`~/.local/bin` symlink**
  (the endpoint manager re-execs itself via PATH) → configure + start
  (default LocalProvider = workers on the login node) → prints UUID.
  Idempotent; re-run after login-node maintenance.

---

## 11. Deployment runbook

### Install & first run

```bash
git clone … && cd diamond_agent
make setup            # uv sync + .env from template
make login && make endpoints && make init-sites && make preflight
make live             # http://127.0.0.1:8642
```

### Going live

```bash
make setup-all                       # + globus SDK, + mcp
make login                           # one-time interactive Globus auth
# per supercomputer:
scp scripts/hpc_endpoint_setup.sh <login-node>: && ssh -t <login-node> bash hpc_endpoint_setup.sh
make endpoints && make init-sites    # discover; generate sites.live.yaml
$EDITOR sites.live.yaml              # accounts, per-partition qos, dialect switches
make preflight && make smoke SYSTEM=<Sys>
make live
```

### Running as a managed service (macOS example)

`uvicorn` is the only process; wrap `make live` (or the underlying
`uv run --no-sync uvicorn diamond_agent.main:app --port 8642`) in launchd/
systemd with `WorkingDirectory=` the repo, `Restart=on-failure`. Restart is
always safe (§0). Log to a file; rotate freely (the process only appends).

### Upgrade

`git pull && uv sync --all-extras && restart`. SQLite schema is
`CREATE TABLE IF NOT EXISTS` — additive; task JSON is Pydantic-validated on
load with per-record error tolerance (a bad record is logged and skipped,
never fatal).

### Monitoring

- Liveness: `GET /api/config` (no remote calls).
- Federation health: `GET /api/federation` → `endpoint_health` per record;
- The tick loop logs every placement and continuation event at INFO.

### Troubleshooting (symptom → cause → fix)

| Symptom | Likely cause | Fix |
|---|---|---|
| live mode refuses to start: "no cached Globus tokens" | never logged in on this machine | `make login` |
| endpoint listed but submit fails `Unable to start user endpoint process … './globus-compute-endpoint'` | endpoint manager can't resolve its executable via PATH | re-run bootstrap script (creates `~/.local/bin` symlink); restart endpoint **with an absolute path** |
| `sacctmgr`-reported account rejected at TACC | TACC validates the portal-case project name | use the uppercase portal name in the catalog |
| sbatch rejected: `Invalid qos` | site binds accounts to mandatory QOS | set per-partition `qos:` in the catalog |
| sbatch rejected: `--mem`/gres invalid | whole-node site (TACC) | `mem_directive: false` / `gpu_directive: false` |
| probe returns empty stdout on one system | ancient login-node python choked | already 3.6-compatible; check `stderr` tail in the DEGRADED log line |
| every node of a partition shows `down` | unrecognized Slurm state suffix | extend the strip set in `PROBE_CMD` (currently `*~#%$@+-!`) |
| whole partitions "busy" but jobs backfill instantly (or inverse) | node states vs GRES reality | trust the `Free` column (`gpus_free`); the wait estimator already does |
| stop/start of an endpoint has no effect | multi-login-node cluster, wrong node | target the daemon's node (`ssh loginNN …`), see endpoint metadata hostname |
| task PENDING forever after agent restart | (pre-`bind_job` versions only) job mapping lost | upgrade; bind_job rehydrates on startup |

### Security notes

- No inbound exposure beyond localhost UI; consider `--host 127.0.0.1`
  explicitly if the default ever changes.
- Secrets: Globus refresh tokens in `~/.globus_compute/storage.db` (0600);
  `sites.live.yaml` holds endpoint UUIDs + account names (git-ignored by
  default). No model API key anywhere in the core.
- The agent submits only what the operator's own allocations permit; the
  rule chain (`authorization`, `user_limits`, `capacity` with per-queue node
  caps) plus the orchestrator's dispatch gate are the policy enforcement
  points (`diamond_agent/policy.py`).
- A channel token is an agent's credential: it scopes every call to that
  channel's queues, account, caps, tasks and events, and never appears on an
  agent route (`/ui/channels` is operator-only). Hand each conversation its
  own channel; delete the channel to revoke the URL.
- Agent surfaces cannot change limits: `PUT /ui/policy` and `/ui/channels`
  exist only with the UI, need the per-process operator cookie, and are
  absent from `/docs` and `/skill/api`. `exec` refuses Slurm job submission/control and detached
  processes; `put-file` is workdir-only; refusals are 403 and audited.
- Residual risk is credential sharing, not the API: a process that holds the
  core's Globus tokens could use the SDK directly. Run the core under its own
  account (`make core`) and give agents only `DIAMOND_AGENT_URL` when that
  matters; a Claude Code `PreToolUse` hook denying `globus_compute_sdk` /
  `globus_sdk` / `.globus_compute` in Bash is a cheap extra fence on a shared
  account.
- Offline tests of all of the above: `uv run --no-sync python -m unittest discover -s tests -v`.
