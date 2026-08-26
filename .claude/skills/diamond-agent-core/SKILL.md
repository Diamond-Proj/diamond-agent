---
name: diamond-agent-core
description: Drive diamond-agent-core from another agent — read live supercomputer and queue status across the user's federated HPC allocations, inspect a target's environment, stage files, submit a task to any resource or let the meta-scheduler place it, then track it to completion with the decision rationale and job logs. Use when work must run on HPC/GPU resources managed by Diamond Agent.
---

# diamond-agent-core

A local service (default `http://127.0.0.1:8642`, override with `DIAMOND_AGENT_URL`) that
owns the user's HPC resource pool (Delta, DeltaAI, Anvil, Vista, … via Diamond / Globus
Compute), the scheduler, the task tracker and the monitor. It holds the credentials; you
never need HPC keys in your process. Everything below is one command in the CLI, one HTTP
call, or one MCP tool with the same name. The running core serves this file at `GET /skill`
with a generated HTTP reference appended; `GET /skill/api` is the reference alone.

Run CLI commands from the repo as `uv run --no-sync diamond-agent-core <cmd>`; add `--json`
whenever you will parse the output.

## 0. Check the core and its credentials first

```
diamond-agent-core auth
```
- `authenticated: True` → continue.
- `authenticated: False` or provider `needs-login` → a human must sign in once: in a
  terminal `python -m diamond_agent.auth login` (URL + one-time code), or the web UI's
  *Sign in with Globus*. Ask; never try to drive that browser flow yourself.
- "diamond-agent-core unreachable" → the service is not running: `make core` (headless)
  or `make live` (with UI) in the repo; `make status` shows what is on the port.
- Local configuration is `GET /api/config` (`diamond-agent-core providers` for the
  provider list). Do not read `.env` — it holds secrets you do not need.

## 1. Read the supercomputers and their queues

```
diamond-agent-core status                          # Markdown, one table per system
curl -s 'http://127.0.0.1:8642/api/status?format=text'
curl -s  http://127.0.0.1:8642/api/status         # same data, JSON
```
Each partition row gives: hardware (GPU model × per node, GB/GPU, cores, node memory,
arch); **free now** = GRES-level free GPUs (GPU partitions) or idle cores (CPU
partitions) — the number that gates placement; busy %; nodes idle/mixed/allocated/down;
queue running/pending; **mine** running/pending; max walltime; `est. wait` = the
scheduler's heuristic for a one-node job (a hint, not a promise). System `health` is the
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

## 3. Stage your script, then submit

Jobs run with the working directory at the system's `workdir` (usually `$HOME/diamond`);
stdout/stderr land in `<workdir>/logs/<task name>.stdout|.stderr`. Put your script there:
```
diamond-agent-core put-file DeltaAI@NCSA ./train.py --to scripts/train.py
```
(`POST /api/resources/{system}/files` `{"path": "scripts/train.py", "content_b64": "...", "mode": "0644"}`,
≤ 2 MB; relative paths are under the workdir). Reference it as `$HOME/diamond/scripts/train.py`
in the command.

Draft the resource envelope from a description, or write it yourself:
```
diamond-agent-core --json envelope --name llama-ft --description "LoRA fine-tune 7B, 2GB data, ckpt every 30 min" --command "..."
```
returns `{"envelope": {...}, "rationale": "...", "source": "ai"|"heuristic", "confidence": 0.0-1.0}`
with `envelope = {"gpu": {"min_count", "max_count", "min_memory"}, "cpu": {"min_count", "max_count"},
"memory": {"min", "max"}, "walltime": {"min", "max"}, "compatibility": {"cpu_arch", "accelerators",
"container_required"}, "objective": "earliest_completion"|"max_throughput"|"min_queue_impact"}`.

Submit — auto placement (default; the rules+AI meta-scheduler picks the best feasible
partition across the whole pool):
```
diamond-agent-core --json submit --name llama-ft --description "..." \
  --command "module load python/miniforge3_pytorch; python \$HOME/diamond/scripts/train.py" \
  --auto-envelope --wait
```
Pinned (only when the human named a system, the data already lives there, or the auto
decision was rejected): add `--target DeltaAI@NCSA:ghx4` or `--target DeltaAI@NCSA`.

HTTP body for `POST /api/tasks`:
```json
{"name": "llama-ft", "description": "...", "command": "module load ...; python ...",
 "envelope": {...} , "auto_generate_envelope": true, "max_retries": 2, "auto_continue": true,
 "target": {"system": "DeltaAI@NCSA", "partition": "ghx4"}}
```
(`envelope` may be omitted with `auto_generate_envelope: true`; omit `target` for auto.)
Response: `{"task": <summary>, "envelope": {...}, "envelope_rationale": "..."}`; summary =
`{"task_id", "name", "status", "system", "partition", "job_id", "sizing": {"nodes","gpus","cpus","walltime_minutes"},
"attempts", "mode": "rules"|"ai+rules", "envelope_source", "envelope_confidence", "target_system", "target_partition"}`.
Unknown targets → 404.

## 4. Track, read logs, explain

```
diamond-agent-core task <id>          # placement, sizing, every candidate + verdicts + AI rank, timeline
diamond-agent-core wait <id>          # block until COMPLETED / FAILED / CANCELLED
diamond-agent-core logs <id> --tail 200   # remote stdout/stderr tails (GET /api/tasks/{id}/logs)
diamond-agent-core events --task <id>     # the agent's own actions with rationale
```
`GET /api/tasks/{id}` → `{"task_id", "status", "spec": {"name","command","envelope"}, "placement": {"system","partition","sizing"},
"job_id", "attempts", "decision": {"mode", "summary", "chosen": {...}, "candidates": [{"system","partition","feasible",
"verdicts": [{"rule","passed","detail"}], "sizing", "est_wait_minutes", "ai_rank", "rationale"}]}, "timeline": [{"ts","kind","message"}]}`.
When you explain a placement, quote `decision.summary` / the chosen candidate's
`rationale`; never invent a reason the decision does not contain. Retries, resizes and
migrations happen on their own and appear in `timeline` as `AGENT_*` events.

## 5. Intervene

```
diamond-agent-core cancel <id>
diamond-agent-core replan <id>                      # re-run placement now
diamond-agent-core replan <id> --clear-target       # lift a pin
diamond-agent-core replan <id> --target Anvil@RCAC:gpu
diamond-agent-core replan <id> --exclude Delta@NCSA
```

## Statuses → what to do

| status | meaning | your move |
|---|---|---|
| PENDING | queued on the placed partition | wait; compare with `est. wait` from status |
| RUNNING | executing | wait; `logs` shows progress |
| COMPLETED | job exited 0 | report system/partition, sizing, timing from `task`; `logs` for results |
| FAILED | failed after retries | `logs` first; fix command/modules/paths; resubmit |
| UNSCHEDULABLE | nothing feasible now (pin offline, envelope too demanding, no credentials) | `replan --clear-target` or relax the envelope; the core retries by itself |
| CANCELLED | cancelled | — |

## Rules of the road

- Rules decide feasibility and sizing; the AI only orders feasible candidates.
- Wall-time caps are short on purpose (4 h on the live sites): checkpoint and chunk.
- `exec` is for inspection; compute goes through `submit`.
- All `/api` callers share the core's single identity; there is no per-agent auth.
- Reserved providers (`ssh`, `cloud`) show status `reserved` and cannot run anything yet.
