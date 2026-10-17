# Diamond Agent

An agentic meta-scheduler that treats a user's federated HPC allocations —
managed through [Diamond](https://github.com/) / Globus Compute — as **one
abstract supercomputer**. It runs locally, serves a local web UI, and lets an
agent place, monitor, and autonomously continue batch workloads across
independently operated systems (Delta, DeltaAI, Anvil, Vista, Perlmutter, …).

**Docs**: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — architecture &
functional design (data model, scheduler pipeline, continuation catalog, site
dialects, auth, extension points). [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md)
— per-component implementation reference & deployment/operations runbook
(process model, persistence, restart semantics, troubleshooting).
## The four core mechanisms

| # | Mechanism | Where |
|---|-----------|-------|
| 1 | **Federated resource abstraction (all compute in one)** — a provider layer (`providers/`: HPC via Diamond/Globus Compute today, SSH servers and cloud pools reserved behind the same contract) merges endpoints, site configs, allocations, and minute-level queue state into one virtual resource pool (`ResourceSnapshot` of per-partition `ResourceRecord`s: timestamp, endpoint, system, partition, account, arch, GPU type/count/memory, idle/allocated/down nodes, queue depths, user's own jobs, walltime caps, endpoint health) | `diamond_agent/providers/`, `diamond_agent/federation/` |
| 2 | **Resource envelope** — tasks declare `[min viable, max useful]` ranges for GPU/CPU/memory/walltime plus compatibility constraints and a placement objective; users write it or the agent drafts it from the task description | `diamond_agent/schemas.py`, `diamond_agent/agent/envelope_gen.py` |
| 3 | **Hybrid AI + rules meta-scheduler** — a deterministic rule chain (health, authorization, arch, accelerator, GPU memory, capacity, walltime, container, user limits) computes the feasible set and a concrete sizing inside the envelope; Claude ranks the feasible set for the objective. AI can order but never overrule the rules. | `diamond_agent/scheduler/` |
| 4 | **Execution, monitoring, and event-triggered continuation** — placement flows into Globus Compute submission; a persistent monitor turns job failures, endpoint outages, and queue-wait overruns into agent actions: retry in place, resize within the envelope, or migrate to the next-best system — every action logged with a rationale | `diamond_agent/execution/`, `diamond_agent/agent/orchestrator.py` |

## diamond-agent-core — use it from any agent

The core (providers, collector, scheduler, orchestrator, store, HTTP API) is a
standalone service; the web UI is one client of its `/api`. Other agents talk
to it three ways, all sharing one contract:

| Surface | How |
|---|---|
| **CLI** | `uv run diamond-agent-core resources` · `idle --gpus 2 --gpu-mem 40` · `envelope --name X --description "…"` · `submit --name X --command "…" --auto-envelope --wait` · `submit … --target DeltaAI@NCSA:ghx4` (pin) · `task <id>` · `replan <id> --clear-target` · `events --task <id>` (`--json` everywhere) |
| **MCP** | `uv sync --extra mcp` then `claude mcp add diamond-agent-core -- uv run --no-sync python -m diamond_agent.core.mcp_server` — tools `federation_status`, `list_resources`, `find_idle`, `inspect_environment`, `run_on_login_node`, `put_file`, `draft_envelope`, `submit_task`, `get_task`, `list_tasks`, `wait_task`, `cancel_task`, `replan_task`, `task_logs`, `recent_events`, `scheduling_policy`, `agent_skill`, `system_notes` (each annotated read-only / destructive for the host's consent policy; refusals come back with their reason); resources `diamond://skill`, `diamond://skill/systems`, `diamond://status`, `diamond://policy` |
| **HTTP / Python** | `/api` with OpenAPI at `/docs`; `diamond_agent.core.CoreClient` (stdlib) or `DiamondAgentCore` in-process |
| **Queue status** | `diamond-agent-core status` · `GET /api/status?format=text` (Markdown) or `/api/status` (JSON) — per partition free GPUs/cores, busy %, node states, queue depth, my jobs, wall cap, wait heuristic |
| **Environment** | `diamond-agent-core env SYSTEM` (login-node probe: host, workdir + logs, python/torch, modules, container runtime, Slurm accounts) · `exec SYSTEM "cmd"` (bounded, **inspection-only**: Slurm job submission/control and detached processes are refused) · `put-file SYSTEM local --to path` (stage ≤2 MB **under the workdir**) · `logs TASK` (remote stdout/stderr tails) — `GET /api/resources/{system}/env`, `POST …/exec`, `POST …/files`, `GET /api/tasks/{id}/logs` |
| **Limits** | `diamond-agent-core policy` · `GET /api/policy` · MCP `scheduling_policy` — per-queue max nodes per job, pending cap, blocked queues. **Read-only from every agent surface**; the operator edits them in the web UI (sidebar → *Limits*), see [Keeping agents on the interface](#keeping-agents-on-the-interface) |
| **Skill** | `make core` prints the path; the running core serves it at `GET /skill` (+ generated API reference at `/skill/api`, per-system notes at `/skill/systems`); `diamond-agent-core skill [--systems]` prints them |

A Claude Code skill, [`.claude/skills/diamond-agent-core/SKILL.md`](.claude/skills/diamond-agent-core/SKILL.md),
teaches an agent the workflow (see the pool → draft an envelope → submit auto
or pinned → track → intervene) and how to read statuses and decisions. Its
[`references/systems.md`](.claude/skills/diamond-agent-core/references/systems.md)
page collects the quirks and caveats of every supported machine (account forms,
QOS, rejected directives, modules, containers, login-node limits).
`make core` runs the core headless (API only); `make live` adds the web UI. Both refuse to start on a busy port (`make status` shows what is listening and whether it serves the UI; `make stop` ends it).

## Signing in (live mode)

Two ways in, one core — pick by how you use it:

| You use… | Sign in with | What happens |
|---|---|---|
| the **web frontend** | **Sign in with Globus** button (Globus OAuth, authorization-code flow like the Diamond dashboard) | the core exchanges the code with your confidential client, keeps the token bundle as a *web session* (`~/.diamond_agent/web_session.json`, mode 0600) and drives Globus Compute with an auto-refreshing `funcx_service` token; **Sign out** in the sidebar drops it |
| the **standalone core** — CLI, MCP, other agents, `make core` | `python -m diamond_agent.auth login` (native-app flow: open the URL, paste the one-time code) — unchanged | refresh tokens cached by the Globus Compute SDK; the core starts headless without prompting |

Frontend sign-in needs a Globus confidential client (`GLOBUS_CLIENT_ID` /
`GLOBUS_CLIENT_SECRET` in `.env`; `PORTAL_CLIENT_ID` / `PORTAL_CLIENT_SECRET`
from a Diamond deployment work too) with `http://127.0.0.1:8642/auth/callback`
registered as a redirect URI (`DIAMOND_AGENT_PUBLIC_URL` overrides the base
behind a proxy). With it configured, `make live` starts even before anyone
has signed in: the HPC provider reports `needs-login` and the UI shows the
sign-in gate. Resolution order when several credentials exist: web session →
`DIAMOND_AGENT_TOKENS` → service identity → cached CLI login.
`diamond-agent-core auth` prints the current state.

> Don't confuse the two client variables: `GLOBUS_CLIENT_ID/SECRET` is the
> confidential client for the **browser login** (you remain the identity);
> `GLOBUS_COMPUTE_CLIENT_ID/SECRET` makes the core run as a **service identity**
> (the app itself, which owns no endpoints of yours) — only for servers/CI.

## Quickstart (first run, ~15 minutes)

```bash
make setup      # 1. uv sync --all-extras + .env, then runs `make doctor`
make login      # 2. Globus login for the standalone core (opens a URL, paste the one-time code)
                #    — or skip and sign in from the web UI after `make live` (needs GLOBUS_CLIENT_ID/SECRET)
make endpoint-setup HOST=<you>@login.delta.ncsa.illinois.edu     # 3. once per supercomputer
make endpoints && make init-sites                                # 4. scaffold sites.live.yaml, fill accounts
make preflight                                                   # 5. whole-chain check
make live                                                        # 6. http://127.0.0.1:8642  (or: make core)
```

`make doctor` at any point tells you which step is missing. `make` alone
prints the steps.

### Step 3 explained — an endpoint on every supercomputer

Diamond Agent reaches each machine through a **Globus Compute endpoint that
you own**, running on that machine's login node. `make endpoint-setup
HOST=user@login-host` ships [`scripts/hpc_endpoint_setup.sh`](scripts/hpc_endpoint_setup.sh)
inline over a **single SSH connection** (you authenticate once — no separate
`scp`) and runs it; the script is also left at `~/hpc_endpoint_setup.sh` on
the node for re-runs after maintenance. the script installs `globus-compute-endpoint`
into `~/.gce-venv` (via uv, no system python needed), configures and starts
an endpoint named `diamond-agent` **detached** (endpoint 4.x runs in the
foreground unless started with `--detach`), and prints its UUID. Its first
start opens a Globus login — **use the same Globus account as `make login`**. The
endpoint keeps running after you log out; after login-node maintenance,
`ssh <host> '~/.gce-venv/bin/globus-compute-endpoint start --detach diamond-agent'`.

**Login-node limits.** Sites cap what one user may run on a login node — TACC
Vista enforces `ulimit -u 100`, and that limit counts *threads*, not just
processes. Endpoint 4.x runs a manager process plus a user-endpoint process
(UEP) whose engine is defined in
`~/.globus_compute/diamond-agent/user_config_template.yaml.j2`; the stock
template already limits the UEP to one worker, and the setup script
additionally pins BLAS/OpenMP threads to 1 for everything that worker runs
(`OMP_NUM_THREADS=1 …`), pins uv's own thread pools (`RAYON_NUM_THREADS=2`,
`TOKIO_WORKER_THREADS=2`, `UV_CONCURRENT_INSTALLS/DOWNLOADS=2` — a stock `uv pip install` sizes
its rayon pool to the core count and aborts with *failed to initialize global
rayon pool … Resource temporarily unavailable* under that limit), moves a
legacy `engine` block out of `config.yaml` into the template (4.x refuses to start
with *contains an `engine` field* — endpoints created under 3.x have exactly
that layout), and prints your current thread budget. If you still
see "can't start new thread" / thread-pool errors on a login node, check
`ulimit -u` and `ps -u $USER -o nlwp= | awk '{s+=$1} END {print s}'` — other
sessions on the same node (VS Code remote servers are the usual culprit)
count against the same budget. Re-running `make endpoint-setup` on an existing
endpoint applies the template changes and restarts it; `config.yaml` is never
overwritten.

| machine | login host |
|---|---|
| Delta@NCSA | `login.delta.ncsa.illinois.edu` |
| DeltaAI@NCSA | `gh-login01.delta.ncsa.illinois.edu` |
| Anvil@RCAC | `anvil.rcac.purdue.edu` |
| Vista@TACC | `vista.tacc.utexas.edu` |
| Frontera@TACC | `frontera.tacc.utexas.edu` |
| Lonestar6@TACC | `ls6.tacc.utexas.edu` |
| Perlmutter@NERSC | `perlmutter.nersc.gov` (NERSC MFA: password + OTP, or an `sshproxy.sh` key; the endpoint reports itself as `loginNN.chn`) |

Tip: if your `~/.ssh/config` enables connection sharing, every later `ssh`
to the same host reuses the first authentication (including Duo/MFA):

```
Host *.tacc.utexas.edu *.ncsa.illinois.edu *.rcac.purdue.edu *.nersc.gov
  ControlMaster auto
  ControlPath ~/.ssh/cm-%r@%h-%p
  ControlPersist 10m
```

`make endpoints` then lists your endpoints with the machine it recognised
from the hostname and flags known machines that still have none;
`make init-sites` writes `sites.live.yaml` with the hardware shapes
pre-filled for known machines — you only fill in your allocation accounts
(`TODO-your-allocation`). `sites.example.yaml` shows a complete catalog.

The environment is uv-native: `uv.lock` pins every dependency,
`.python-version` pins the interpreter, `make setup` installs the core plus
the `ai` (Claude ranking + envelope drafting) and `mcp` extras;
`make setup-min` installs the core only. Configuration lives in `.env`
(see `.env.example`); real environment variables always override it. Every
resource the UI shows comes from a live probe of a real system.

### Optional: the AI half of the scheduler

Set `ANTHROPIC_API_KEY` in `.env`. Without it the scheduler transparently
falls back to deterministic heuristics (Settings → About shows which is
active).

## Demo walkthrough

The local UI (RackTop-style workbench: sidebar of resources, one workspace)
serves two product goals — *submit a task to any resource* and *let the
platform pick the most suitable one*.

1. **Overview** — the resource wall: one card per system (provider badge,
   health), one row per partition with the busy fraction of the gating
   resource (free GPUs on GPU partitions, free cores on CPU partitions), free
   units and queue depth; stat strip on top; sort by free GPUs / utilization /
   queue depth.
2. **Idle compute** — filter partitions by free GPUs, GPU memory, model,
   architecture, queue depth and provider; every available card has
   **Launch here** (a pinned submission) and **Details**.
3. **Launch task** (top-right) — three steps in one sheet: task & command →
   resource envelope (fill by hand or **Draft with agent**; source and
   confidence are recorded) → run location: **Auto** (hybrid rules + AI
   meta-scheduler across the whole pool) or **Pin to a resource** (any system,
   optionally one partition).
4. **My tasks** — placement, sizing, job id, attempts, decision mode; detail
   tabs show the full candidate table with the rule-chain verdicts and AI
   rank, and the event timeline. Pinned tasks can be **unpinned & replanned**.
5. **Resource detail** (click a system) — partition cards (with each queue's
   max nodes per job), a **queue history** heatmap built from the in-memory
   snapshot history, the connection facts (provider, endpoint, accounts), and
   a **Limits** button.
6. **Agent activity** — every decision, dispatch and continuation with its
   rationale; the bell collects agent actions and failures.
7. **Add resource** — provider slots with their status: HPC via Globus Compute
   (active), SSH server (reserved), cloud pool (reserved), each with the
   catalog snippet it expects.
8. **Limits** (sidebar) — the operator's scheduling policy: default and
   per-queue **max nodes per job**, max pending jobs per system, blocked
   queues. Saved limits take effect on the next placement, persist across
   restarts and are recorded in Agent activity; agents can read them but
   only this sheet can change them.

## Keeping agents on the interface

The core is the policy enforcement point, so the limits only mean something if
every job goes through it. Three things make that hold:

1. **Nothing on the agent surfaces can change or sidestep the limits.** The
   per-queue node cap bounds the `capacity` rule and the sizing step, and the
   dispatcher re-checks the final sizing right before `sbatch` (`POLICY_DENIED`
   if a continuation reused a sizing the operator has since lowered). `exec`
   refuses the Slurm commands that submit or control jobs (`sbatch`, `srun`,
   `salloc`, `scancel`, `scrontab`, `scontrol` other than `show`) and anything
   that would outlive the bounded call (`&`, `nohup`, `tmux`, …); `put-file`
   writes relative paths under the workdir only. `GET /api/policy` is
   read-only; `PUT /ui/policy` is mounted only with the web UI and requires
   the cookie the core hands the browser that loaded the page. `make core`
   (headless, what agents use) has no route that changes limits at all.
2. **Everything is on the record.** Every `exec` (`SHELL_EXEC`), staged file
   (`FILE_STAGED`), refusal (`POLICY_DENIED`) and limit change
   (`POLICY_CHANGED`, with what changed) lands in the activity feed
   (`GET /api/events`, *Agent activity*, the bell) next to the task events.
3. **The agent must not hold the keys.** The skill tells agents never to use
   the Globus SDK, the token caches or the core's internal modules directly.
   To make that structural rather than advisory, run the core under its own
   account (its own `~/.globus_compute` / `~/.diamond_agent`, `make core`)
   and give agent processes only `DIAMOND_AGENT_URL`: a process without the
   core's credentials cannot reach an endpoint except through the API. On a
   shared account, a Claude Code `PreToolUse` hook that denies Bash commands
   mentioning `globus_compute_sdk`, `globus_sdk` or `.globus_compute` is a
   cheap extra fence; the project does not install one for you.

Catalog side: `policy.max_nodes_per_job` is the default per-job cap and a
partition entry may carry its own `max_nodes_per_job`. Overrides made in the
UI are stored in the service database (`settings` table) and layered over the
catalog, so `sites.live.yaml` is never rewritten by the service.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `DIAMOND_AGENT_TICK` | `5` / `60` | seconds per federation refresh |
| `DIAMOND_AGENT_DB` | `./diamond_agent.db` | SQLite persistence |
| `DIAMOND_AGENT_SITES` | `sites.live.yaml` | site catalog (from `make init-sites`) |
| `DIAMOND_AGENT_MODEL` | `claude-opus-5` | model for the AI scheduler/envelope |

## Production (live mode) runbook

Live mode follows the diamond-admin-backend pattern end to end: a
ShellFunction probe (`sinfo`/`squeue`/`sacctmgr` → one JSON blob) refreshes
the snapshot per endpoint; submission renders a Jinja sbatch script shipped
through `create_batch()`/`batch_run` on the system's login-node endpoint; monitoring polls
`sacct` keyed on the parsed SLURM job id.

**Auth** (`diamond_agent/auth.py`) resolves credentials in this order:

1. `DIAMOND_AGENT_TOKENS` — the diamond dashboard's `tokens` cookie JSON
   (inline or `@/path/file`); the `funcx_service` access token is used exactly
   like diamond-admin-backend's `initialize_token_authorizer`.
2. `GLOBUS_COMPUTE_CLIENT_ID`/`GLOBUS_COMPUTE_CLIENT_SECRET` — Globus
   confidential-client (service identity), fully non-interactive.
3. Native login — one interactive browser flow, refresh tokens cached in
   `~/.globus_compute/storage.db`; everything afterwards runs headless.
   The server refuses to start live mode if no cached tokens exist (it will
   never prompt from a background thread).

**Phase 0 — an endpoint on each supercomputer (one-time).** Diamond Agent
drives only endpoints **you own**, running on the login node, so probes and
`sbatch` execute immediately and nothing has to be configured per user.
(Multi-user endpoints are deliberately unsupported: they require
account/partition worker placement and their workers ride a batch queue.)

```bash
make endpoint-setup HOST=<you>@<login host>     # = scp scripts/hpc_endpoint_setup.sh + ssh -t bash …
# installs globus-compute-endpoint in ~/.gce-venv, logs in (same Globus
# account as `make login`), starts the 'diamond-agent' endpoint detached,
# prints the UUID.
```

**Go-live procedure (local):**

```bash
make setup
make login          # one-time browser login (Globus)
make endpoints      # list endpoint UUIDs + hostname->machine mapping
make init-sites     # writes sites.live.yaml skeleton
$EDITOR sites.live.yaml            # fill node shapes, accounts,
make preflight
make smoke SYSTEM=Delta@NCSA
    # ^ optional: one real 1-CPU/5-min sbatch job, verified via sacct, then scancel'ed
make live
```

Preflight checks: credentials, catalog completeness, endpoint status +
hostname sanity, a timed real probe roundtrip per system (warns if a probe
outruns the 60s tick), and optionally one end-to-end submit/cancel smoke job.

Per-system knobs in `sites.live.yaml` (the *site dialect*, each one demanded by a
real site — see [docs/ARCHITECTURE.md §5.1](docs/ARCHITECTURE.md) and the
per-system notes in [`references/systems.md`](.claude/skills/diamond-agent-core/references/systems.md)):

| knob | level | used by |
|---|---|---|
| `account` | partition | GPU vs CPU sub-accounts (Delta `proj-gpu`, NERSC `m1234_g`) |
| `qos` | partition | Anvil (account-bound QOS), NERSC (`regular`/`debug`/…) |
| `constraint` | partition | NERSC `-C gpu` / `-C cpu` / `-C gpu&hbm80g` |
| `slurm_partition` | partition | the `sinfo` name when the catalog name is logical (NERSC `gpu` → `gpu_ss11`) |
| `partition_directive: false` | system | NERSC — no `--partition`, the job_submit plugin routes by constraint + QOS |
| `mem_directive: false` | system | TACC, NERSC whole-node QOS — no `--mem` |
| `gpu_directive: false` | system | TACC — gres directives rejected |
| `gres_probe: false` | system | NERSC — per-node GRES query too slow on ~5,000 nodes; free GPUs = idle nodes × GPUs/node |
| `container_runtime` | system | `apptainer`/`singularity` (`exec --nv`), `shifter` (`--image`), `podman-hpc` (`run --gpu`) |
| `diamond_dir` | system | remote workdir (default `$HOME/diamond`) |

## API sketch

`GET /api/federation` snapshot · `GET /api/federation/history` trend ·
`POST /api/envelope/generate` draft envelope · `POST /api/tasks` submit ·
`GET /api/tasks[/{id}]` status + decision trace + timeline ·
`POST /api/tasks/{id}/cancel|replan` · `GET /api/events` agent activity ·

## License

Apache License 2.0 — see [LICENSE](LICENSE).
