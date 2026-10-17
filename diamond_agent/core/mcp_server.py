"""MCP server for diamond-agent-core (``uv sync --extra mcp``).

Exposes the running core to any MCP host (Claude Code, Claude Desktop, …)
over stdio, proxying to ``/api``:

    uv run --no-sync python -m diamond_agent.core.mcp_server
    # claude mcp add diamond-agent-core -- uv run --no-sync python -m diamond_agent.core.mcp_server

How the core maps onto MCP:

* **Tools** are the actions the model may take. Each carries annotations
  (read-only / destructive / idempotent / open-world) so the host can apply
  its consent policy; the core enforces its own rules regardless. There is
  deliberately no tool that changes scheduling limits — that is the
  operator's, in the web UI.
* **Resources** are the reference documents a host or operator attaches to a
  conversation: the skill, the per-system notes, the Markdown queue status
  and the limits in force.
* **Errors**: a core refusal (policy 403, unknown task, no credentials) is an
  anticipated ``ToolError``, so the model reads the reason and can act on it.
  Any other exception would reach it only as "Error executing tool".
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from mcp.types import ToolAnnotations

from .client import TERMINAL, CoreClient

mcp = MCPServer(
    "diamond-agent-core", title="Diamond Agent core", version="0.2.0",
    instructions=(
        "Diamond Agent core: one virtual pool over the user's federated HPC systems (Delta, DeltaAI, "
        "Anvil, Vista, Perlmutter, …). Read federation_status / find_idle, system_notes and "
        "inspect_environment before submitting. submit_task with no target lets the meta-scheduler "
        "choose; target_system[/partition] pins. Every decision is explained in get_task. The "
        "operator's scheduling limits (scheduling_policy) are enforced on every submission and cannot "
        "be changed from here. run_on_login_node is for inspection only: job submission/control is "
        "refused. Resources diamond://skill, diamond://skill/systems, diamond://status and "
        "diamond://policy hold the same reference material as text."))

_tools = CoreClient(error=ToolError)        # a core refusal is an anticipated tool error
_docs = CoreClient(error=ResourceError)

# Hints for the host's consent policy.
READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)          # answered from the core's own state
READ_REMOTE = ToolAnnotations(read_only_hint=True, open_world_hint=True)    # reads a login node
ADDITIVE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)
MUTATING = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True)
OVERWRITING = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True)


# ------------------------------------------------------------- resources

@mcp.resource("diamond://skill", title="Agent skill", mime_type="text/markdown",
              description="How to drive diamond-agent-core: workflow, calls, statuses, rules of the road, API reference.")
def skill_doc() -> str:
    return _docs.skill()


@mcp.resource("diamond://skill/systems", title="System notes", mime_type="text/markdown",
              description="Per-system quirks and caveats: account form, QOS, rejected directives, modules, containers.")
def systems_doc() -> str:
    return _docs.skill_systems()


@mcp.resource("diamond://status", title="Federation status", mime_type="text/markdown",
              description="Supercomputers and queues right now, one Markdown table per system.")
def status_doc() -> str:
    return _docs.status_text()


@mcp.resource("diamond://policy", title="Scheduling limits", mime_type="application/json",
              description="The operator's limits in force: max nodes per job per queue, pending cap, blocked queues.")
def policy_doc() -> str:
    return json.dumps(_docs.policy(), indent=1)


# ------------------------------------------------------------- look

@mcp.tool(title="Federation status", annotations=READ)
def federation_status() -> dict[str, Any]:
    """Supercomputers and queues at a glance: per partition free GPUs/cores, utilization, node
    states, queue depth, my jobs, wall-time cap, max nodes per job, scheduler wait heuristic.
    The same as a Markdown table: resource diamond://status."""
    return _tools.status()


@mcp.tool(title="Agent skill", annotations=READ)
def agent_skill() -> str:
    """The skill for this system: workflow, how to call it, and the live HTTP API reference."""
    return _tools.skill()


@mcp.tool(title="System notes", annotations=READ)
def system_notes() -> str:
    """Per-system notes: the quirks and caveats of every supported supercomputer (account form,
    QOS, which sbatch directives the site rejects, modules for python/torch, container runtime,
    login-node limits). Read the target's section before the first submission to it."""
    return _tools.skill_systems()


@mcp.tool(title="Scheduling limits", annotations=READ)
def scheduling_policy() -> dict[str, Any]:
    """The operator's scheduling limits, read-only: per-job node cap per partition (the scheduler
    sizes inside it), max pending jobs per system, blocked partitions. The human changes them in
    the web UI; there is no tool to change them."""
    return _tools.policy()


@mcp.tool(title="List resources", annotations=READ)
def list_resources() -> dict[str, Any]:
    """All compute resources in one shape: systems, partitions, live free GPUs/cores, queues, limits."""
    return _tools.resources()


@mcp.tool(title="Find idle compute", annotations=READ)
def find_idle(min_gpus: int = 1, min_gpu_mem_gb: float = 0, gpu_type: Optional[str] = None,
              arch: Optional[str] = None, max_pending: int = 1000000, include_cpu: bool = True) -> list[dict[str, Any]]:
    """Partitions that can start work now, ranked by free units, with reasons for the rest."""
    return _tools.idle(min_gpus=min_gpus, min_gpu_mem_gb=min_gpu_mem_gb, gpu_type=gpu_type,
                       arch=arch, max_pending=max_pending, include_cpu=include_cpu)


@mcp.tool(title="Inspect environment", annotations=READ_REMOTE)
def inspect_environment(system: str) -> dict[str, Any]:
    """What a system offers before you submit: host/arch, user + home, workdir (+ recent logs),
    python and whether torch imports, relevant modules (pytorch/cuda/conda/apptainer/shifter),
    container runtime, Slurm accounts/QOS, storage. Runs a fixed probe on the login node."""
    return _tools.env(system)


@mcp.tool(title="Run on login node", annotations=MUTATING)
def run_on_login_node(system: str, command: str, timeout_s: int = 60) -> dict[str, Any]:
    """One bounded shell command on the system's login node — for looking around (ls, module show,
    cat a log). Inspection-only by policy: sbatch/srun/salloc/scancel, scontrol (other than show)
    and detached processes are refused with the reason. Compute goes through submit_task."""
    return _tools.exec(system, command, timeout_s)


@mcp.tool(title="Stage a file", annotations=OVERWRITING)
def put_file(system: str, path: str, content: str, mode: str = "0644") -> dict[str, Any]:
    """Stage a text file (e.g. a script you wrote) under the system's workdir — relative paths only
    (e.g. scripts/train.py) — then reference it from the task command. Limit 2 MB."""
    return _tools.put_file(system, path, content.encode("utf-8"), mode)


@mcp.tool(title="Task logs", annotations=READ_REMOTE)
def task_logs(task_id: str, tail: int = 200) -> dict[str, Any]:
    """stdout/stderr tails of the task's job from the remote workdir — read this before diagnosing a FAILED task."""
    return _tools.task_logs(task_id, tail)


# ------------------------------------------------------------- act

@mcp.tool(title="Draft envelope", annotations=READ)
def draft_envelope(name: str, description: str = "", command: str = "") -> dict[str, Any]:
    """Ask the agent to draft a [min viable, max useful] ResourceEnvelope from a task description."""
    return _tools.draft_envelope(name, description, command)


@mcp.tool(title="Submit task", annotations=ADDITIVE)
def submit_task(name: str, command: str, description: str = "", envelope: Optional[dict] = None,
                target_system: Optional[str] = None, target_partition: Optional[str] = None,
                container: Optional[str] = None, max_retries: int = 2, auto_continue: bool = True) -> dict[str, Any]:
    """Submit a task. No target => the hybrid meta-scheduler picks the best resource across the
    pool; target_system (and optional target_partition) => pinned to that resource. Sized inside
    the operator's per-queue node cap."""
    target = {"system": target_system, "partition": target_partition} if target_system else None
    return _tools.submit(name=name, command=command, description=description, envelope=envelope,
                         target=target, container=container, max_retries=max_retries,
                         auto_continue=auto_continue, auto_generate_envelope=envelope is None)


@mcp.tool(title="Get task", annotations=READ)
def get_task(task_id: str) -> dict[str, Any]:
    """Full task record: status, placement, sizing, decision (candidates + verdicts + AI rank), timeline."""
    return _tools.task(task_id)


@mcp.tool(title="List tasks", annotations=READ)
def list_tasks(active_only: bool = True) -> list[dict[str, Any]]:
    """Task summaries, active ones by default."""
    return _tools.tasks(status="active" if active_only else None)


@mcp.tool(title="Wait for task", annotations=READ)
async def wait_task(task_id: str, ctx: Context, timeout_s: float = 1800) -> dict[str, Any]:
    """Block until the task is COMPLETED/FAILED/CANCELLED (or timeout_s) and return it, reporting
    progress every few seconds so the host keeps the call alive. If your host caps tool-call
    duration, poll get_task instead."""
    started = time.monotonic()
    while True:
        task = await anyio.to_thread.run_sync(_tools.task, task_id)
        elapsed = time.monotonic() - started
        if task["status"] in TERMINAL or elapsed >= timeout_s:
            return task
        where = (task.get("placement") or {}).get("system") or "no placement yet"
        await ctx.report_progress(elapsed, timeout_s, f"{task['status']} · {where}")
        await anyio.sleep(3)


@mcp.tool(title="Cancel task", annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True, open_world_hint=True))
def cancel_task(task_id: str) -> dict[str, Any]:
    """Cancel a task and its batch job."""
    return _tools.cancel(task_id)


@mcp.tool(title="Replan task", annotations=MUTATING)
def replan_task(task_id: str, exclude: Optional[list[str]] = None, target_system: Optional[str] = None,
                target_partition: Optional[str] = None, clear_target: bool = False) -> dict[str, Any]:
    """Re-run placement (cancelling the current job): clear_target lifts a pin, target_system
    re-pins, exclude blocks systems."""
    target = {"system": target_system, "partition": target_partition} if target_system else None
    return _tools.replan(task_id, exclude=exclude, target=target, clear_target=clear_target)


@mcp.tool(title="Recent events", annotations=READ)
def recent_events(limit: int = 40, task_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Agent activity feed: decisions, dispatches, continuations with rationale — and every exec,
    staged file and policy refusal."""
    return _tools.events(limit=limit, task_id=task_id)


if __name__ == "__main__":
    mcp.run()
