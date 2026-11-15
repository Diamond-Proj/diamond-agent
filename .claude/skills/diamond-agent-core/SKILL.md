---
name: diamond-agent-core
description: Drive diamond-agent-core from another agent — read live supercomputer and queue status across the user's federated HPC allocations, inspect a target's environment, write the job and its resource envelope, ask which queues can run it, choose one, submit there, then track it to completion with the queue's evaluation and job logs. Use when work must run on HPC/GPU resources managed by Diamond Agent.
---

# diamond-agent-core

A local service that owns the user's HPC resource pool (Delta, DeltaAI, Anvil, Vista,
Frontera, Lonestar6, Perlmutter, … via Diamond / Globus Compute), the feasibility and sizing
rules, the task tracker and the monitor. It holds the credentials; you never need HPC keys in
your process.

**You reach it through your channel URL** — `DIAMOND_AGENT_URL`, of the form
`http://127.0.0.1:8642/c/<token>`, handed to you by the operator. A channel is the operator's
delegation to you: the machines and queues you may use, the account each machine bills, and
the per-queue node caps (always inside the machine rules). Everything you see through it —
`status`, `candidates`, tasks, events — is your channel's and nothing else; other channels'
tasks do not exist for you. If you have no URL, ask the operator (the core prints the default
channel's URL at start and `make channels` lists them); do not use the core's bare
origin. Everything below is one command in the CLI, one HTTP call relative to that URL, or
one MCP tool with the same name. The running core serves this file at `<channel URL>/skill`
with your channel's rules and a generated HTTP reference appended; `/skill/api` is the
reference alone; `/skill/systems` is the per-system notes page
([references/systems.md](references/systems.md)).

Run CLI commands from the repo as `uv run --no-sync diamond-agent-core <cmd>` with
`DIAMOND_AGENT_URL` set to your channel URL; add `--json` whenever you will parse the output.

**The core is the only road to the supercomputers.** Use the CLI, the HTTP API or the MCP
tools below and nothing else: never import `globus_compute_sdk` / `globus_sdk`, never read
`~/.globus_compute` or `~/.diamond_agent`, never call the `diamond_agent.execution`,
`diamond_agent.federation` or `diamond_agent.auth` modules in a script of your own, and
never run `sbatch`/`srun`/`scancel` through `exec`. The operator's limits (per-queue node
caps, pending cap, blocked queues) live in the core and are enforced on every submission;
going around the core would go around them, and every `exec`, staged file and refusal is
written to the activity feed the human reads. If something you need is not reachable
through the interface, say so instead of working around it.

## 0. Check the core and its credentials first

```
diamond-agent-core auth
```
- `authenticated: True` → continue.
- `authenticated: False` or provider `needs-login` → a human must sign in once: in a
  terminal `python -m diamond_agent.auth login` (URL + one-time code), or the web UI's
  *Sign in with Globus*. Ask; never try to drive that browser flow yourself.
- "diamond-agent-core unreachable" → the service is not running (`make core` / `make live`
  in the repo; `make status` shows what is on the port) or `DIAMOND_AGENT_URL` is not your
  channel URL. "unknown channel" (404) → the channel was deleted; ask the operator for a new URL.
- `GET /api/config` shows your channel (`channel.name`) and the limits in force
  (`diamond-agent-core policy`). Do not read `.env` — it holds secrets you do not need.

## 1. Read the supercomputers and their queues

```
diamond-agent-core status                          # Markdown, one table per system
curl -s 'http://127.0.0.1:8642/api/status?format=text'
curl -s  http://127.0.0.1:8642/api/status         # same data, JSON
```
Only your channel's machines and queues appear. Each partition row gives: hardware (GPU
model × per node, GB/GPU, cores, node memory, arch); the **account** your channel bills;
**free now** = GRES-level free GPUs (GPU partitions) or idle cores (CPU partitions) — the
number that gates placement; busy %; nodes idle/mixed/allocated/down; queue running/pending;
**mine** running/pending; max walltime; **max nodes/job** = your channel's cap on that queue;
`est. wait` = a heuristic for a one-node job (a hint, not a promise). System `health` is the
Globus Compute endpoint: `offline` means the endpoint is down, not the machine.

JSON shape (per partition):
```json
{"partition": "gpuA100x4", "hardware": "A100 x4/node, 40 GB/GPU, 64c, 256 GB", "arch": "x86_64",
 "account": "my-allocation-gpu", "container_runtime": "apptainer", "health": "online",
 "free": 25, "unit": "GPU", "utilization": 0.936,
 "nodes": {"idle": 0, "mixed": 68, "allocated": 30, "down": 1, "total": 99},
 "queue": {"running": 243, "pending": 1533}, "mine": {"running": 0, "pending": 0},
 "max_walltime_minutes": 240, "est_wait_minutes": 78.7}
```
Narrower views: `diamond-agent-core idle --gpus 2 --gpu-mem 40 [--gpu-type H100] [--arch aarch64]`
(`GET /api/resources/idle`) lists only partitions that can start now and says why the
others cannot; `resources` (`GET /api/resources`) is the full record per partition.

## 2. Inspect the target before you write a command

```
diamond-agent-core env Anvil@RCAC
```
(`GET /api/resources/{system}/env`) runs a fixed probe on the system's login node and
returns `host`, `arch`, `os`, `user`, `home`, `workdir` (+ `workdir_exists`, recent
logs), `python`, `torch` (whether it imports without a module), `modules` (pytorch / cuda
/ conda / apptainer entries of `module avail`), `container_runtime`, `slurm`
(accounts/QOS you hold), `storage`, plus `sections` and `raw` for anything else.

Use it to decide what to put in the command: e.g. on DeltaAI torch comes from
`module load python/miniforge3_pytorch`; on Delta from `module load pytorch-conda`; Vista
has no torch module. Load modules **inside the command** — jobs start from a clean shell.

To look at something specific:
```
diamond-agent-core exec Delta@NCSA "module show pytorch-conda 2>&1 | head; ls -la \$HOME/diamond/logs | tail"
```
(`POST /api/resources/{system}/exec` `{"command": "...", "timeout_s": 60}`, max 600 s).
This is for looking around; it runs on the login node, so never use it for compute.
`exec` is **inspection-only by policy**: `sbatch`, `srun`, `salloc`, `scancel`, `scrontab`,
`scontrol` (other than `scontrol show`) and anything that would outlive the bounded call
(`&`, `nohup`, `setsid`, `screen`, `tmux`) are refused with HTTP 403 and recorded as
`POLICY_DENIED`. Everything else runs — and is recorded as `SHELL_EXEC`, so use it for
looking (`ls`, `cat`, `tail`, `grep`, `module`, `sinfo`, `squeue`, `sacct`, `scontrol show`,
`df`, `python -c '…'`), not for doing.

## 2b. Know the system's quirks before you write the command

Every machine has its own rules: account form, QOS, which `#SBATCH` directives the site
rejects, where torch comes from, how containers run, login-node limits. They are collected
one section per system in [references/systems.md](references/systems.md) — served by the
running core at `GET /skill/systems`, printed by `diamond-agent-core skill --systems`, MCP
tool `system_notes`. Read the target's section before the first submission to it in a
session. The one-line version:

| system | arch | GPUs / node | remember |
|---|---|---|---|
| Delta@NCSA | x86_64 | A100 40 GB ×4 · A40 48 GB ×4 | GPU vs CPU sub-accounts; `module load pytorch-conda` |
| DeltaAI@NCSA | aarch64 | GH200 ×4 | arm64 only (no x86 wheels/containers); `module load python/miniforge3_pytorch` |
| Anvil@RCAC | x86_64 | A100 ×4 (`gpu`) · H100 ×4 (`ai`) | each account is bound to one QOS |
| Vista@TACC | aarch64 | GH200 ×1 | whole nodes: no `--mem`, no gres; project name UPPERCASE; `gh-dev` for smoke tests; no torch module |
| Frontera@TACC · Lonestar6@TACC | x86_64 | RTX5000 ×4 · A100 ×3 | TACC conventions as Vista; not yet verified live |
| Perlmutter@NERSC | x86_64 | A100 40 GB ×4 (`gpu`; 80 GB via `gpu&hbm80g`) | routed by constraint + QOS, no `--partition`; GPU account ends in `_g`; `regular`/`debug` bill the whole node even for 1 GPU; shifter/podman-hpc, no apptainer; `module load pytorch` on its own line (never piped) |

The catalog switches do the `#SBATCH` work (`constraint`, `qos`, `partition_directive`,
`mem_directive`, `gpu_directive`, `container_runtime`); you never add directives yourself.

## 3. Specify the job yourself, then stage and submit

The core places, sizes, runs and watches jobs; **you specify them**. Two pieces are
yours to write, and each is a good job for a focused subagent handed the target's
section of [references/systems.md](references/systems.md) and the `env` output:

- **The command** — what runs inside the batch job: module loads or environment
  activation, data paths under the workdir, `srun`/`torchrun` lines for multi-node work,
  checkpointing that fits the wall-time cap. A subagent per command keeps the system's
  quirks (account form, modules, container runtime) in one small context.
- **The ResourceEnvelope** — `[min viable, max useful]` ranges; the scheduler picks the
  concrete shape inside them:

  ```json
  {"gpu": {"min_count": 2, "max_count": 8, "min_memory": "40GB"},
   "cpu": {"min_count": 8, "max_count": 64},
   "memory": {"min": "64GB", "max": "512GB"},
   "walltime": {"min": "30m", "max": "4h"},
   "compatibility": {"cpu_arch": ["x86_64", "aarch64"], "accelerators": ["A100", "H100", "GH200"],
                     "container_required": false}}
  ```
  `min` is the floor below which the job cannot run (too high blocks scheduling); `max` is
  where more resources stop helping (too high wastes allocation). Rules of thumb: per-GPU
  memory ≈ 2 GB per billion parameters for fp16 inference, 16–20 GB per billion for full
  training, far less for LoRA; `gpu.min_count: 0` for CPU-only work; list only accelerators
  that satisfy `min_memory`; `walltime.min` = the shortest useful run, `walltime.max` = a
  generous cap with checkpointing in mind (the queue's own limit applies; `candidates` shows
  it as `max_walltime_minutes`, null = none); `cpu_arch` must include
  `aarch64` to be eligible for DeltaAI and Vista. The core validates the envelope and never
  guesses one.

Jobs run with the working directory at the system's `workdir` (usually `$HOME/diamond`);
stdout/stderr land in `<workdir>/logs/<task name>.stdout|.stderr`. Put your script there:
```
diamond-agent-core put-file DeltaAI@NCSA ./train.py --to scripts/train.py
```
(`POST /api/resources/{system}/files` `{"path": "scripts/train.py", "content_b64": "...", "mode": "0644"}`,
≤ 2 MB). Paths are **relative to the workdir only**: letters, digits, `. _ + -` and `/`, no
leading `/`, no `..` (anything else is refused with 403). Reference the file as
`$HOME/diamond/scripts/train.py` in the command.

**Choose the queue.** The core never places a job for you; it tells you, per queue, whether
your envelope can run there and how:
```
diamond-agent-core candidates --envelope-json envelope.json
```
(`POST /api/candidates` `{"envelope": {...}}`, MCP `find_candidates`) returns one row per
queue, feasible ones first by wait estimate:
```json
{"system": "DeltaAI@NCSA", "partition": "ghx4", "account": "bcdx-dtai-gh", "feasible": true,
 "verdicts": [{"rule": "capacity", "passed": true, "detail": "min shape fits in 1 node(s)"},
              {"rule": "budget", "passed": true, "detail": "8.00 node-h of 42.50 left"}, ...],
 "sizing": {"nodes": 2, "gpus": 8, "cpus": 64, "walltime_minutes": 240}, "est_wait_minutes": 2.3,
 "node_hours_left": 42.5,
 "health": "online", "arch": "aarch64", "gpu_type": "GH200", "free": 96, "unit": "GPU",
 "nodes_idle": 24, "pending_jobs": 12, "user_pending_jobs": 0,
 "max_walltime_minutes": 240, "max_nodes_per_job": 8}
```
`sizing` is what you would get there (the core sizes inside your envelope and the queue's
cap, shrinking toward the minimum to fit idle capacity and, when your channel has a
node-hour budget there, to fit what is left — the `budget` verdict says so); an infeasible
row names the failing rules. Choose with your own judgment — data locality, the wait
estimate versus the free units, the architecture your wheels were built for, how many of
your jobs are already pending — and say why in your report. `node_hours_left` is what your
channel may still commit on that queue (absent = no budget); plan a sweep against it rather
than discovering the limit job by job. If nothing is feasible, change the envelope (the
verdicts tell you which range or constraint to relax); do not retry blindly.

**Submit to the queue you chose** (`--target` is required):
```
diamond-agent-core --json submit --name llama-ft --description "..." \
  --command "module load python/miniforge3_pytorch; python \$HOME/diamond/scripts/train.py" \
  --envelope-json envelope.json --target DeltaAI@NCSA:ghx4 --wait
```
HTTP body for `POST /api/tasks` (MCP `submit_task` takes the same fields, with
`target_system` / `target_partition`):
```json
{"name": "llama-ft", "description": "...", "command": "module load ...; python ...",
 "envelope": {...}, "target": {"system": "DeltaAI@NCSA", "partition": "ghx4"},
 "max_retries": 2, "auto_continue": true}
```
The core re-evaluates that queue at submit; if it cannot run the envelope now you get HTTP 409
with the verdicts and no task is created. Response = the task summary
`{"task_id", "name", "status", "system", "partition", "job_id", "sizing": {"nodes","gpus","cpus","walltime_minutes"},
"attempts", "target_system", "target_partition"}`. A queue that is not in your channel (or does not exist) → 409 as well.

## 3b. Put your code on the machines: releases

A **release** is your channel's code + environment + machines as one version, `R1, R2, …`.
It pins a code tree (from a directory or a git commit), an environment reference (a module
line or image path the job receives as `DA_ENV`) and the machines it targets. A release is
**aligned** on a machine once its **build** there is `ready`: an immutable snapshot under the
machine's workdir, transferred incrementally and verified file by file. Builds are numbered
per machine (`R3 @ DeltaAI#2`): a repaired or rebuilt snapshot of the same release.

```
diamond-agent-core release list                                        # what exists, aligned where
diamond-agent-core release create --git . --ref HEAD --base 2 --env "module load pytorch/2.4"
diamond-agent-core release align 3 --system DeltaAI@NCSA --wait        # materialise + verify there
diamond-agent-core release verify 3 --system DeltaAI@NCSA              # ready, or stale with what differs
```
(`GET/POST /api/releases`, `GET /api/releases/{n}`, `POST /api/releases/{n}/align|verify`; MCP
`list_releases`, `create_release`, `align_release`, `verify_release`.) `--base` is the latest
version you saw: creation is compare-and-swap, so if another agent published since you get
409 — read the list again and build on the latest. The archive is packed on your side (the
CLI/MCP run `git archive` or tar the directory, `.git` excluded, ≤ 64 MB); the core never
reads your disk.

Submit **with** the release and the job starts inside its snapshot:
```
diamond-agent-core submit --name llama-ft --release 3 --command "eval \"\$DA_ENV\"; python train.py" \
  --envelope-json envelope.json --target DeltaAI@NCSA:ghx4 --wait
```
Inside the job: cwd = `$DA_RELEASE_DIR` (the snapshot), `$DA_RELEASE` = 3, `$DA_RELEASE_BUILD`
= the build number, `$DA_ENV` = the environment reference. A target that is not aligned to
the release (no build, or a `stale` one) is refused with 409 at submit and at `retarget`, so
every machine that runs `R3` runs exactly `R3`; the task record keeps the build it ran in.
Publishing `R4` never touches a job running on `R3`'s snapshot. Prefer releases over
`put-file` whenever more than one file or more than one machine is involved.

## 4. Track, read logs, explain

```
diamond-agent-core task <id>          # the queue's evaluation at submit (verdicts, sizing, wait), timeline
diamond-agent-core wait <id>          # block until COMPLETED / FAILED / CANCELLED
diamond-agent-core logs <id> --tail 200   # remote stdout/stderr tails (GET /api/tasks/{id}/logs)
diamond-agent-core events --task <id>     # the agent's own actions with rationale
```
`GET /api/tasks/{id}` → `{"task_id", "status", "spec": {"name","command","envelope"}, "target_system", "target_partition",
"evaluation": {"feasible", "verdicts": [{"rule","passed","detail"}], "sizing", "est_wait_minutes", "record": {...}},
"placement": {"system","partition","sizing"}, "job_id", "attempts", "timeline": [{"ts","kind","message"}]}`.
When you explain where a task runs, the reason is yours — quote what you saw in `candidates`
and the evaluation's verdicts; never invent facts the core did not return. A transient failure
is retried in place and a wall-time hit is resubmitted with a longer wall time (within your
envelope's maximum and the queue's cap); these appear in `timeline` as `AGENT_*` events. The
core never moves a task to another queue on its own.

## 5. Intervene

```
diamond-agent-core cancel <id>
diamond-agent-core retarget <id> --target Anvil@RCAC:gpu   # move it to another queue you chose (cancels the job)
```
(`POST /api/tasks/{id}/retarget` `{"target": {...}}`, MCP `retarget_task`.) Run `candidates`
first; an infeasible target is refused with 409 and the task is left as it was.

## Limits the core works within

```
diamond-agent-core policy             # your channel's queues, account, max nodes/job, pending cap
```
(`GET /api/policy`, MCP `scheduling_policy`). `status` also prints a **max nodes/job** column.
Your channel's rules sit inside the machine rules: a queue not in your channel does not
appear at all, and the cap you see is the lower of the channel's and the machine's. The core
sizes every task inside that cap and the dispatcher refuses anything above it; a queue whose
cap is below your *minimum* shape shows up infeasible in `candidates` (the `capacity` verdict
names the cap). Your channel may also carry **node-hour budgets** (per queue, per machine,
and the machine's own budget shared with other channels; a job costs nodes × wall time,
charged at the requested wall time while it runs and at the elapsed time once it ends):
`status` and `candidates` show `node-h left` / `node_hours_left`, sizing shrinks to fit what
is left, and a job that cannot fit even at its minimum is refused with the `budget` verdict.
A retry in place is re-checked against the budget too (`POLICY_DENIED` if it no longer fits). All of this is set by the human in the web UI (Channels → rules; Machine
rules) and is read-only from the CLI, the API and MCP — do not ask for a way to raise them;
ask the human. A `POLICY_DENIED` event on a task means the rules changed between its
evaluation and a retry.

## Statuses → what to do

| status | meaning | your move |
|---|---|---|
| PENDING | queued on the placed partition | wait; compare with `est. wait` from status |
| RUNNING | executing | wait; `logs` shows progress |
| COMPLETED | job exited 0 | report system/partition, sizing, timing from `task`; `logs` for results |
| FAILED | failed after in-place retries, or refused at dispatch (`POLICY_DENIED`, `JOB_SUBMIT_FAILED` in the timeline) | `logs` first; fix command/modules/paths and resubmit, or `candidates` again and `retarget` to another queue |
| CANCELLED | cancelled | — |

## Rules of the road

- The core never chooses a queue. `candidates` tells you which queues can run your envelope,
  sized, with a wait estimate; you choose and submit there. It has no model of its own — you
  (and your subagents) write the envelope and the command.
- Wall-time caps are short on purpose (4 h on the live sites): checkpoint and chunk.
- Site dialects live in the catalog, not in your command: pick the account form, QOS and
  modules per [references/systems.md](references/systems.md); never add `#SBATCH` lines.
- `exec` is for inspection (enforced); compute goes through `submit`, files through `put-file`.
- Scheduling limits are the operator's: read them (`policy`), never try to change or bypass them.
- Your channel URL is your identity to the core: everything you do is recorded in that
  channel's activity feed (every exec, staged file, submission and refusal), which the
  operator reads. Do not share the URL or use another channel's.
- Reserved providers (`ssh`, `cloud`) show status `reserved` and cannot run anything yet.
