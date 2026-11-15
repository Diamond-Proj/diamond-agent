# Diamond Agent

Diamond Agent turns the HPC allocations you hold on different supercomputers,
reached through Diamond / Globus Compute, into one pool that AI agents can
use under your rules. It runs on your workstation, talks to each machine
through a Globus Compute endpoint that you own, and hands every group of
agents a **channel**: one URL that carries the machines, queues, accounts and
budgets you allow. Through that URL an agent reads live queue state, picks a
queue, submits batch jobs and follows them. You watch and set the limits in a
local web UI or from the terminal.

Machines it already knows (hardware and site quirks built in): Delta, DeltaAI,
Anvil, Vista, Horizon, Frontera, Lonestar6 and Perlmutter. For any other Slurm
machine you describe the queues yourself in the catalog.

## Quickstart

The first machine takes about 15 minutes. You need:

- [uv](https://docs.astral.sh/uv/), which installs the pinned Python and every dependency;
- SSH access to at least one Slurm supercomputer where you hold an allocation;
- the Globus account you use for those machines.

```bash
make setup                                    # 1. install, create .env
make login                                    # 2. Globus login: open the URL, paste the code
make endpoint-setup HOST=<you>@<login-host>   # 3. once per supercomputer
make endpoints && make init-sites             # 4. write sites.live.yaml from your endpoints
make preflight                                # 5. check the whole chain
make live                                     # 6. core + web UI at http://127.0.0.1:8642
```

`make doctor` tells you at any point which step is missing, and `make` alone
prints the steps.

| Step | What it does | Run it again |
|---|---|---|
| 1 `make setup` | `uv sync --all-extras`, copies `.env.example` to `.env`, runs `make doctor` | after pulling new dependencies |
| 2 `make login` | Globus login for the core. The tokens are cached, so the core starts without prompting. You can skip this and use **Sign in with Globus** in the web UI instead (see [Signing in](#signing-in)) | when the login expires |
| 3 `make endpoint-setup` | Over one SSH connection (one password or MFA prompt): installs `globus-compute-endpoint` on the login node, starts an endpoint named `diamond-agent` and prints its id. It asks for a Globus login there: **use the same account as step 2**. Login hosts are listed [below](#an-endpoint-on-every-supercomputer) | per machine; after login-node maintenance; after upgrading the SDK here |
| 4 `make endpoints && make init-sites` | Lists your endpoints with the machine recognised from each hostname, then writes `sites.live.yaml` with the hardware of known machines. Accounts are read from Slurm. A field is left as `TODO` only where Slurm cannot choose for you, such as Perlmutter's GPU sub-account | after adding or re-creating an endpoint (a re-run keeps your edits) |
| 5 `make preflight` | Credentials, catalog, endpoint status and version, one timed real probe per machine. `make smoke SYSTEM=Delta@NCSA` adds one real 1-CPU, 5-minute job, checked through `sacct` and then cancelled | whenever something looks wrong |
| 6 `make live` | The core and the web UI. `make core` runs the same core headless (API only) | |

### Hand it to an agent

`make live` prints the default channel's URL when it starts, and
`make channels` prints it again. Give that URL to the agent, then try it
yourself from another shell:

```bash
export DIAMOND_AGENT_URL=http://127.0.0.1:8642/c/<token>

uv run --no-sync diamond-agent-core status          # the queues this channel may use, live

echo '{"cpu": {"min_count": 1, "max_count": 1}, "walltime": {"min": "5m", "max": "10m"}}' > env.json
uv run --no-sync diamond-agent-core candidates --envelope-json env.json    # which queues can run it, sized, with a wait estimate
uv run --no-sync diamond-agent-core submit --name hello --command "hostname" \
    --envelope-json env.json --target Delta@NCSA:cpu --wait                # the queue you picked from that list
```

For Claude Code, register the MCP server instead:

```bash
claude mcp add diamond-agent-core -e DIAMOND_AGENT_URL=http://127.0.0.1:8642/c/<token> \
    -- uv run --no-sync python -m diamond_agent.core.mcp_server
```

The channel page in the web UI has both lines ready to copy with the token
filled in. The default channel allows every machine. To give a group of
agents narrower rules, create a channel: **New channel** in the web UI, or
`make channels ARGS="create NAME"`. The core needs no model API key: the
agent that drives it writes the job command and its resource envelope.

### Everyday commands

| Command | What it does |
|---|---|
| `make live` · `make core` | run the core with the web UI · headless. Both refuse a busy port; `make live PORT=8643` picks another |
| `make stop` · `make status` | stop the core · show what is listening and whether it serves the UI |
| `make channels` | print every channel URL. `ARGS="create NAME"` and `ARGS="delete ID"` manage channels without the UI |
| `make doctor` | what is missing and the next step |
| `make preflight` · `make smoke SYSTEM=…` | check the chain · and run one real job |
| `make endpoints` · `make init-sites` | list your endpoints · update `sites.live.yaml` from them |
| `make cli ARGS="…"` | the `diamond-agent-core` CLI |
| `make clean-state` | delete the local database. Globus tokens and the catalog stay |

The catalog is read at start: after editing `sites.live.yaml`, run
`make stop && make live`.

## Using it from an agent

The core (providers, collector, evaluator, orchestrator, store, HTTP API) is a
standalone service; the web UI is one client of its `/api`. Agents talk to it
through their channel URL, and every surface shares one contract:

| Surface | How |
|---|---|
| **CLI** | `uv run diamond-agent-core resources` · `idle --gpus 2 --gpu-mem 40` · `candidates --envelope-json env.json` (which queues can run it, sized, with wait estimates) · `submit --name X --command "…" --envelope-json env.json --target DeltaAI@NCSA:ghx4 --wait` · `task <id>` · `retarget <id> --target Anvil@RCAC:gpu` · `events --task <id>` (`--json` everywhere) |
| **MCP** | `uv sync --extra mcp` then `claude mcp add diamond-agent-core -- uv run --no-sync python -m diamond_agent.core.mcp_server` — tools `federation_status`, `list_resources`, `find_idle`, `find_candidates`, `inspect_environment`, `run_on_login_node`, `put_file`, `submit_task`, `get_task`, `list_tasks`, `wait_task`, `cancel_task`, `retarget_task`, `task_logs`, `recent_events`, `scheduling_policy`, `agent_skill`, `system_notes` (each annotated read-only / destructive for the host's consent policy; refusals come back with their reason); resources `diamond://skill`, `diamond://skill/systems`, `diamond://status`, `diamond://policy` |
| **HTTP / Python** | `/api` with OpenAPI at `/docs`; `diamond_agent.core.CoreClient` (stdlib) or `DiamondAgentCore` in-process |
| **Queue status** | `diamond-agent-core status` · `GET /api/status?format=text` (Markdown) or `/api/status` (JSON) — per partition free GPUs/cores, busy %, node states, queue depth, my jobs, wall cap, wait heuristic |
| **Environment** | `diamond-agent-core env SYSTEM` (login-node probe: host, workdir + logs, python/torch, modules, container runtime, Slurm accounts) · `exec SYSTEM "cmd"` (bounded, **inspection-only**: Slurm job submission/control and detached processes are refused) · `put-file SYSTEM local --to path` (stage ≤2 MB **under the workdir**) · `logs TASK` (remote stdout/stderr tails) — `GET /api/resources/{system}/env`, `POST …/exec`, `POST …/files`, `GET /api/tasks/{id}/logs` |
| **Limits** | `diamond-agent-core policy` · `GET /api/policy` · MCP `scheduling_policy` — per-queue max nodes per job, pending cap, blocked queues. **Read-only from every agent surface**; the operator edits them in the web UI (Overview → *Machine rules*), see [Keeping agents on the interface](#keeping-agents-on-the-interface) |
| **Skill** | `make core` prints the path; the running core serves it at `<channel URL>/skill` with the channel's rules appended (+ generated API reference at `/skill/api`, per-system notes at `/skill/systems`); `diamond-agent-core skill [--systems]` prints them |

A Claude Code skill, [`.claude/skills/diamond-agent-core/SKILL.md`](.claude/skills/diamond-agent-core/SKILL.md),
teaches an agent the workflow (see the pool → specify the job → ask which
queues can run it → choose and submit → track → retarget) and how to read
statuses and evaluations. Its
[`references/systems.md`](.claude/skills/diamond-agent-core/references/systems.md)
page collects the quirks and caveats of every supported machine (account forms,
QOS, rejected directives, modules, containers, login-node limits).

The HTTP API at a glance: `GET /api/status` queues · `GET /api/resources` records ·
`POST /api/candidates` evaluate an envelope against every queue ·
`POST /api/tasks` submit to the queue you chose (409 + verdicts if it cannot
run the envelope) · `GET /api/tasks[/{id}]` status + evaluation + timeline ·
`POST /api/tasks/{id}/cancel|retarget` · `GET /api/events` activity ·
`GET /api/policy` limits (read-only; `PUT /ui/policy` from the web UI).

## How it works

### The four core mechanisms

| # | Mechanism | Where |
|---|-----------|-------|
| 1 | **Federated resource abstraction (all compute in one)** — a provider layer (`providers/`: HPC via Diamond/Globus Compute today, SSH servers and cloud pools reserved behind the same contract) merges endpoints, site configs, allocations, and minute-level queue state into one virtual resource pool (`ResourceSnapshot` of per-partition `ResourceRecord`s: timestamp, endpoint, system, partition, account, arch, GPU type/count/memory, idle/allocated/down nodes, queue depths, user's own jobs, walltime caps, endpoint health) | `diamond_agent/providers/`, `diamond_agent/federation/` |
| 2 | **Resource envelope** — tasks declare `[min viable, max useful]` ranges for GPU/CPU/memory/walltime plus compatibility constraints. The calling agent writes it (the skill gives the schema and rules of thumb, and recommends a subagent for it and for the job command); the core validates it and never guesses one | `diamond_agent/schemas.py` |
| 3 | **Per-queue feasibility and sizing** — `POST /api/candidates` evaluates an envelope against every queue with a deterministic rule chain (health, authorization, arch, accelerator, GPU memory, capacity with per-queue node caps, walltime, container, user limits), sizes it inside the envelope and the operator's cap, and estimates the wait. **The agent chooses the queue**; the core never ranks or places, and `submit` re-evaluates the chosen queue and refuses an infeasible one with the verdicts | `diamond_agent/scheduler/` |
| 4 | **Execution and monitoring with in-place continuation** — the chosen queue's evaluation becomes a rendered sbatch shipped through Globus Compute; a monitor polls `sacct` every tick, retries a transient failure in place and resubmits a wall-time hit with a longer wall time within the caps; anything else ends FAILED with the reason for the agent to act on (`retarget` to another queue, re-specify, or stop). Every step is on the task timeline | `diamond_agent/execution/`, `diamond_agent/agent/orchestrator.py` |

On the wire it follows the diamond-admin-backend pattern: a ShellFunction
probe (`sinfo`/`squeue`/`sacctmgr` → one JSON blob) refreshes the snapshot per
endpoint; submission renders a Jinja sbatch script shipped through
`create_batch()`/`batch_run` on the machine's login-node endpoint; monitoring
polls `sacct` keyed on the parsed Slurm job id. Every resource the UI shows
comes from a live probe of a real system.

### Channels — one URL and one rule set per conversation

Agents never get the core's bare address. The operator creates **channels**
in the web UI (Channels → New channel) — one per conversation window or team
of agents — and hands each agent its channel URL, `<core>/c/<token>`, as
`DIAMOND_AGENT_URL`. A channel's rules say which machines and queues its
agents may use, which account each machine (or a single queue) bills, and the per-queue node cap
(and pending cap); everything an agent sees through the URL — status,
candidates, submissions, tasks, events — is that channel's and nothing else.

Channel rules live **inside the machine rules** (Overview → Machine rules): a
queue blocked on the machine exists in no channel, and the effective cap on a
queue is the lower of the channel's and the machine's — checked when the channel is saved and again at
every evaluation and dispatch, so tightening a machine rule tightens every
channel at once. A channel can also carry **node-hour budgets** (per queue
and per machine) inside a machine's own budget, which every channel shares
first come, first served. Nothing configured means no limit; a job is charged
nodes × requested wall time while it runs and nodes × elapsed time once
`sacct` reports it ended, sizing shrinks to fit what is left, and
`candidates` shows the headroom per queue.

Every core has a **`default` channel** — every machine, machine rules — created
with the core and never deletable, so agents can be handed a URL before any
channel is shaped.

**Without the web UI** the terminal is the operator's console: `make channels`
prints every channel's URL, `make channels ARGS="create NAME"` makes one (the
default rules, or `--rules FILE.json` with a channel's `rules` object) and
`make channels ARGS="delete ID"` revokes one — on the store directly, so the
running core sees it at once. Machine rules come from the catalog's `policy:`
block in a headless deployment (edited in YAML, applied at start).

### Releases — code + environment + machine as one version

A **release** `R_n` belongs to a channel and pins a code tree (a directory or a
git commit, packed on the agent's side and content-addressed in the core), an
environment reference (`DA_ENV` in the job) and the machines it targets.
Creating `R_n` is compare-and-swap on the channel's latest version, so two
agents cannot both publish "the next" release. A **build** `R_n @ machine # k`
is that release materialised on one machine: an immutable snapshot under the
workdir (`releases/<channel>/r<n>-<tree>/`, blobs cached and hard-linked, only
missing content transferred, verified file by file, `current` swapped with an
atomic symlink). A machine is **aligned** to `R_n` when its build is `ready`; a
task that names `R_n` can only be dispatched there, `retarget` checks the same,
the job starts inside the snapshot, and the task record keeps the build it ran
in. `verify` re-checks a snapshot and marks it `stale` when it drifted; `align`
repairs it as build `k+1`. Phase 1 ships code; environment builds per
architecture are the next step.

```
diamond-agent-core release create --git . --ref HEAD --base 0 --env "module load pytorch/2.4"
diamond-agent-core release align 1 --system DeltaAI@NCSA --wait
diamond-agent-core submit --release 1 --name t --command "python train.py" --envelope-json e.json --target DeltaAI@NCSA:ghx4
```

## The web UI: a monitor and a rule console

The local UI is an **overview** (the pool, the channels that delegate to
agents, the machines of the federation) plus a page per channel and per
machine. The dark rail on the left is the index of both: a lamp per channel
(lit while it has a running task), a lamp per machine (its health) with its
free capacity, and the age of the last probe. **It submits nothing**: jobs
come from agents through their channel URL; the operator's write actions are
creating and editing channels, editing the machine rules, and cancelling a
task.

1. **Overview** — the pool readout (GPUs free with a trend over the last
   snapshots, machines online, tasks running, tasks pending), then
   **Channels**: one row per channel — running or idle (the only two states a
   channel has), its running / pending / failed / done counts, how many
   queues it has on each machine, when it was last active, a one-click
   **URL** copy — with **New channel** in the section header; then
   **Machines**: one table for the whole federation, a group per machine
   (health, free capacity, how many channels use it) and a row per queue
   with the busy fraction of the gating resource, free units, queue depth
   and your own jobs, so queues compare across machines, with **Machine
   rules** in the section header. Failures and refusals reach the operator
   through the notification bell, which can be marked read; they never
   colour a channel.
2. **Channel page** — the agent URL with **Copy**, and **Connect an agent**:
   ready-made `export DIAMOND_AGENT_URL=…` and `claude mcp add …` lines, each
   with Copy (open after creating a channel, and it stays as you left it).
   Tabs: **Tasks** (a row opens the task inspector beside the list: status,
   sizing and the billed account, the command, the envelope, the queue's
   evaluation at submit rule by rule, the timeline; **Cancel** asks first),
   **Releases** (each version and its build per machine), **Activity** (this
   channel's events), **Rules** (its queues, accounts, caps and the
   node-hours spent against each budget). **Edit rules** and **Delete** sit
   in the top bar (the default channel has no Delete).
3. **Machine page** — a block per queue (hardware, account, cap, which
   channels may use it and with what cap; free units, utilization, queue
   depth and a bar of node states: allocated, mixed, idle, down), the
   **queue history** strip (one cell per snapshot) and connection facts
   (provider, endpoint, declared queues, accounts, container runtime).
4. **Machine rules** (sheet) — default and per-queue **max nodes per job**,
   max pending jobs per machine, per-machine node-hour budget, blocked
   queues, and how many channels use each queue. Every channel lives inside
   these.
5. **New / Edit channel** (sheet) — name, description, max pending; per
   machine: on/off, the account it bills, a node-hour cap; per queue: on/off,
   node cap, node-hour cap, account (never above the machine's;
   machine-blocked queues are disabled).

Nothing else: no settings, no search, no manual refresh. The page polls every
3 s and repaints only what changed; the rail shows the age of the last probe,
or that the core is unreachable. Back and Forward move between pages, every
page has a link (`#channel/<id>`, `#machine/<system>`, …), it works at phone
width, and the theme follows the system.

## Setup in detail

### Signing in

Two ways in, one core — pick by how you use it:

| You use… | Sign in with | What happens |
|---|---|---|
| the **standalone core** — CLI, MCP, other agents, `make core` | `make login` (native-app flow: open the URL, paste the one-time code) | refresh tokens cached by the Globus Compute SDK (`~/.globus_compute/storage.db`); the core starts headless without prompting, and refuses to start if no cached login exists (it never prompts from a background thread) |
| the **web frontend** | **Sign in with Globus** button (Globus OAuth, authorization-code flow like the Diamond dashboard) | the core exchanges the code with your confidential client, keeps the token bundle as a *web session* (`~/.diamond_agent/web_session.json`, mode 0600) and drives Globus Compute with an auto-refreshing `funcx_service` token; **Sign out** in the rail drops it |

Frontend sign-in needs a Globus confidential client (`GLOBUS_CLIENT_ID` /
`GLOBUS_CLIENT_SECRET` in `.env`; `PORTAL_CLIENT_ID` / `PORTAL_CLIENT_SECRET`
from a Diamond deployment work too) with `http://127.0.0.1:8642/auth/callback`
registered as a redirect URI (`DIAMOND_AGENT_PUBLIC_URL` overrides the base
behind a proxy). With it configured, `make live` starts even before anyone
has signed in: the HPC provider reports `needs-login` and the UI shows the
sign-in gate.

When several credentials exist, the core uses the first of:

1. the web session;
2. `DIAMOND_AGENT_TOKENS` — the Diamond dashboard's `tokens` cookie JSON
   (inline or `@/path/file`); its `funcx_service` access token is used exactly
   like diamond-admin-backend's `initialize_token_authorizer`;
3. `GLOBUS_COMPUTE_CLIENT_ID` / `GLOBUS_COMPUTE_CLIENT_SECRET` — a Globus
   confidential client acting as a service identity, fully non-interactive;
4. the cached `make login`.

`diamond-agent-core auth` prints the current state.

> Don't confuse the two client variables: `GLOBUS_CLIENT_ID/SECRET` is the
> confidential client for the **browser login** (you remain the identity);
> `GLOBUS_COMPUTE_CLIENT_ID/SECRET` makes the core run as a **service identity**
> (the app itself, which owns no endpoints of yours) — only for servers/CI.

### An endpoint on every supercomputer

Diamond Agent reaches each machine through a **Globus Compute endpoint that
you own**, running on that machine's login node, so probes and `sbatch` run
at once and nothing has to be configured per user. (Multi-user endpoints are
deliberately unsupported: they need account/partition worker placement and
their workers ride a batch queue.)

`make endpoint-setup HOST=user@login-host` ships
[`scripts/hpc_endpoint_setup.sh`](scripts/hpc_endpoint_setup.sh) inline over a
**single SSH connection** (you authenticate once — no separate `scp`) and runs
it; the script is also left at `~/hpc_endpoint_setup.sh` on the node for
re-runs after maintenance. It installs `globus-compute-endpoint` into
`~/.gce-venv` (via uv, no system python needed), configures and starts an
endpoint named `diamond-agent` **detached** (endpoint 4.x runs in the
foreground unless started with `--detach`), and prints its UUID. Its first
start opens a Globus login — **use the same Globus account as `make login`**.
The endpoint keeps running after you log out; after login-node maintenance,
`ssh <host> '~/.gce-venv/bin/globus-compute-endpoint start --detach diamond-agent'`.

| machine | login host |
|---|---|
| Delta@NCSA | `login.delta.ncsa.illinois.edu` |
| DeltaAI@NCSA | `gh-login01.delta.ncsa.illinois.edu` |
| Anvil@RCAC | `anvil.rcac.purdue.edu` |
| Vista@TACC | `vista.tacc.utexas.edu` |
| Horizon@TACC | `horizon.tacc.utexas.edu` |
| Frontera@TACC | `frontera.tacc.utexas.edu` |
| Lonestar6@TACC | `ls6.tacc.utexas.edu` |
| Perlmutter@NERSC | `perlmutter.nersc.gov` (NERSC MFA: password + OTP, or an `sshproxy.sh` key; the endpoint reports itself as `loginNN.chn`) |

`HOST` goes to `ssh` as it is, so a `Host` alias from your `~/.ssh/config`
works too. If that config enables connection sharing, every later `ssh` to
the same host reuses the first authentication (including Duo/MFA):

```
Host *.tacc.utexas.edu *.ncsa.illinois.edu *.rcac.purdue.edu *.nersc.gov
  ControlMaster auto
  ControlPath ~/.ssh/cm-%r@%h-%p
  ControlPersist 10m
```

**Keep the endpoint and the SDK in step.** The endpoint is installed at
**exactly this workstation's `globus-compute-sdk` version** (`make endpoints`
shows each endpoint's version; `make preflight` flags a mismatch). Endpoint
4.x runs its workers in a venv matched to the *submitting* SDK's version, so
an endpoint manager newer than your SDK passes options the matched worker pool
does not know and every task dies with "workers failed to register"
(`--logconf`, exit 2). After upgrading the SDK here, re-run
`make endpoint-setup` on each machine.

**Start it by its absolute path.** Start an endpoint by hand only as
`~/.gce-venv/bin/globus-compute-endpoint start --detach diamond-agent`, never
as `./globus-compute-endpoint` from inside the bin directory: the detached
manager re-execs its own argv[0] to launch the user endpoint process and then
fails every task with "Unable to start user endpoint process … No such file or
directory: './globus-compute-endpoint'" (preflight names this).

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

### The site catalog

`make endpoints` lists your endpoints with the machine it recognised from the
hostname and flags known machines that still have none; `make init-sites`
creates `sites.live.yaml` with the hardware shapes pre-filled for known
machines. **Accounts are not written**: the probe reads your associations from
Slurm on every tick and uses Slurm's `DefaultAccount`, spelled the way the
site wants it (`account_case: upper` on TACC, whose submit filter validates
the uppercase project name). Pin one with `accounts: [...]`, or per channel in
the UI, only to override that. The one choice left to you is a per-partition
sub-account where a site demands one (Perlmutter's `_g` GPU account), which
stays `TODO`; for a machine the catalog does not know, you also fill in its
queues' node shapes.

Re-running `init-sites` is a **merge**: entries already in the file are kept
exactly as they are (accounts, switches, hand edits, machines whose endpoint
is offline right now), a machine whose endpoint was re-created gets its new
id, new machines are added; the previous file is saved as
`sites.live.yaml.bak`. `sites.example.yaml` shows a complete catalog.

Knobs in `sites.live.yaml` beyond the node shapes. Most are the *site
dialect*, each one demanded by a real site: see
[docs/ARCHITECTURE.md §5.1](docs/ARCHITECTURE.md) and the per-system notes in
[`references/systems.md`](.claude/skills/diamond-agent-core/references/systems.md).

| knob | level | used by |
|---|---|---|
| `account` | partition | GPU vs CPU sub-accounts (Delta `proj-gpu`, NERSC `m1234_g`) |
| `qos` | partition | Anvil (account-bound QOS), NERSC (`regular`/`debug`/…) |
| `constraint` | partition | NERSC `-C gpu` / `-C cpu` / `-C gpu&hbm80g` |
| `slurm_partition` | partition | the `sinfo` name when the catalog name is logical (NERSC `gpu` → `gpu_ss11`) |
| `max_walltime` | partition | the wall-time cap where the queue itself reports none (`null` = no cap); otherwise the queue's own limit applies |
| `accounts` | system | pins the account instead of Slurm's `DefaultAccount` |
| `account_case: upper` | system | TACC — the submit filter wants the uppercase project name, `sacctmgr` prints it lowercase |
| `partition_directive: false` | system | NERSC — no `--partition`, the job_submit plugin routes by constraint + QOS |
| `mem_directive: false` | system | TACC, NERSC whole-node QOS — no `--mem` |
| `gpu_directive: false` | system | TACC — gres directives rejected |
| `gres_probe: false` | system | NERSC — per-node GRES query too slow on ~5,000 nodes; free GPUs = idle nodes × GPUs/node |
| `container_runtime` | system | `apptainer`/`singularity` (`exec --nv`), `shifter` (`--image`), `podman-hpc` (`run --gpu`) |
| `diamond_dir` | system | remote workdir (default `$HOME/diamond`) |

### Configuration

Configuration lives in `.env` (see `.env.example`); real environment variables
always override it.

| Variable | Default | Meaning |
|---|---|---|
| `DIAMOND_AGENT_SITES` | `sites.live.yaml` | the site catalog (from `make init-sites`) |
| `DIAMOND_AGENT_TICK` | `60` | seconds between federation refreshes; raise it if preflight reports slow probes |
| `DIAMOND_AGENT_DB` | `./diamond_agent.db` | SQLite persistence (tasks, events, channels, releases, rule overrides) |
| `DIAMOND_AGENT_UI` | `1` | `0` runs the core headless: `/api` and `/docs` only. `make core` sets it |
| `DIAMOND_AGENT_PUBLIC_URL` | this machine's own address | the external base URL behind a proxy: used for the OAuth redirect and the printed channel URLs |
| `DIAMOND_AGENT_HOME` | `~/.diamond_agent` | where the web session is kept |
| `GLOBUS_CLIENT_ID` / `GLOBUS_CLIENT_SECRET` | — | the confidential client behind **Sign in with Globus** |
| `DIAMOND_AGENT_TOKENS` | — | a Diamond dashboard `tokens` cookie JSON, inline or `@file` |
| `GLOBUS_COMPUTE_CLIENT_ID` / `GLOBUS_COMPUTE_CLIENT_SECRET` | — | run the core as a service identity (servers/CI only) |
| `DIAMOND_AGENT_URL` | — | on the agent's side: the channel URL the CLI and the MCP server talk to |

The port is a `make` variable: `make live PORT=8643`.

The environment is uv-native: `uv.lock` pins every dependency,
`.python-version` pins the interpreter, `make setup` installs the core plus
the `mcp` extra; `make setup-min` installs the core only.

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
   (`GET /api/events`, each channel's **Activity** tab, the bell) next to the
   task events.
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

## More documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — architecture and functional
  design: data model, scheduler pipeline, continuation catalog, site
  dialects, auth, extension points.
- [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md) — per-component
  implementation reference and the operations runbook: process model,
  persistence, restart semantics, troubleshooting.
- [`.claude/skills/diamond-agent-core/`](.claude/skills/diamond-agent-core/) —
  the skill agents read, with the per-machine notes.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
