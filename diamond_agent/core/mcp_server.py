"""MCP server for diamond-agent-core (optional: ``uv sync --extra mcp``).

Exposes the core to any MCP-capable agent (Claude Code, Claude Desktop, …) as
tools over stdio, proxying to a running core via the HTTP client:

    uv run --no-sync python -m diamond_agent.core.mcp_server
    # claude mcp add diamond-agent-core -- uv run --no-sync python -m diamond_agent.core.mcp_server
"""

from __future__ import annotations

from typing import Optional

from .client import CoreClient

try:                      # mcp >= 2: FastMCP was renamed MCPServer
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:
    try:                  # mcp 1.x
        from mcp.server.fastmcp import FastMCP as _Server
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("mcp is not installed — run `uv sync --extra mcp`") from exc

mcp = _Server("diamond-agent-core", instructions=(
    "Diamond Agent core: one virtual pool over federated HPC (and reserved SSH/cloud) "
    "resources. Use list_resources/find_idle to see the pool, submit_task with no target "
    "to let the meta-scheduler choose, or with target_system[/partition] to pin a resource. "
    "Every decision is explained in get_task (candidates, rule verdicts, AI rank, timeline)."))
_client = CoreClient()


@mcp.tool()
def federation_status(as_text: bool = True):
    """Supercomputers and queues at a glance: per partition free GPUs/cores, utilization,
    node states, queue depth, my jobs, wall-time cap, scheduler wait heuristic.
    as_text=True returns Markdown ready for reasoning; False returns the structured dict."""
    return _client.status_text() if as_text else _client.status()


@mcp.tool()
def agent_skill() -> str:
    """The skill for this system: workflow, how to call it, and the live HTTP API reference."""
    return _client.skill()


@mcp.tool()
def inspect_environment(system: str) -> dict:
    """What a system offers before you submit: host/arch, user + home, workdir (+ recent logs),
    python and whether torch imports, relevant modules (pytorch/cuda/conda/apptainer), container
    runtime, Slurm accounts/QOS, storage. Runs a fixed probe on the login node."""
    return _client.env(system)


@mcp.tool()
def run_on_login_node(system: str, command: str, timeout_s: int = 60) -> dict:
    """One bounded shell command on the system's login node — for looking around (ls, module show,
    cat a log). Never use it to run compute; submit_task does that through the batch system."""
    return _client.exec(system, command, timeout_s)


@mcp.tool()
def put_file(system: str, path: str, content: str, mode: str = "0644") -> dict:
    """Stage a text file (e.g. a script you wrote) into the system's workdir (relative path) or an
    absolute path, then reference it from the task command. Limit 2 MB."""
    return _client.put_file(system, path, content.encode("utf-8"), mode)


@mcp.tool()
def task_logs(task_id: str, tail: int = 200) -> dict:
    """stdout/stderr tails of the task's job from the remote workdir — read this before diagnosing a FAILED task."""
    return _client.task_logs(task_id, tail)


@mcp.tool()
def list_resources() -> dict:
    """All compute resources in one shape: systems, partitions, live free GPUs/cores, queues."""
    return _client.resources()


@mcp.tool()
def find_idle(min_gpus: int = 1, min_gpu_mem_gb: float = 0, gpu_type: Optional[str] = None,
              arch: Optional[str] = None, max_pending: int = 1000000, include_cpu: bool = True) -> list:
    """Partitions that can start work now, ranked by free units, with reasons for the rest."""
    return _client.idle(min_gpus=min_gpus, min_gpu_mem_gb=min_gpu_mem_gb, gpu_type=gpu_type,
                        arch=arch, max_pending=max_pending, include_cpu=include_cpu)


@mcp.tool()
def draft_envelope(name: str, description: str = "", command: str = "") -> dict:
    """Ask the agent to draft a [min viable, max useful] ResourceEnvelope from a task description."""
    return _client.draft_envelope(name, description, command)


@mcp.tool()
def submit_task(name: str, command: str, description: str = "", envelope: Optional[dict] = None,
                target_system: Optional[str] = None, target_partition: Optional[str] = None,
                container: Optional[str] = None, max_retries: int = 2, auto_continue: bool = True) -> dict:
    """Submit a task. No target => the hybrid meta-scheduler picks the best resource across the
    pool; target_system (and optional target_partition) => pinned to that resource."""
    target = {"system": target_system, "partition": target_partition} if target_system else None
    return _client.submit(name=name, command=command, description=description, envelope=envelope,
                          target=target, container=container, max_retries=max_retries,
                          auto_continue=auto_continue, auto_generate_envelope=envelope is None)


@mcp.tool()
def get_task(task_id: str) -> dict:
    """Full task record: status, placement, sizing, decision (candidates + verdicts + AI rank), timeline."""
    return _client.task(task_id)


@mcp.tool()
def list_tasks(active_only: bool = True) -> list:
    return _client.tasks(status="active" if active_only else None)


@mcp.tool()
def wait_task(task_id: str, timeout_s: float = 1800) -> dict:
    """Block until the task is COMPLETED/FAILED/CANCELLED (or timeout) and return it."""
    return _client.wait(task_id, timeout_s=timeout_s)


@mcp.tool()
def cancel_task(task_id: str) -> dict:
    return _client.cancel(task_id)


@mcp.tool()
def replan_task(task_id: str, exclude: Optional[list] = None, target_system: Optional[str] = None,
                target_partition: Optional[str] = None, clear_target: bool = False) -> dict:
    """Re-run placement; clear_target lifts a pin, target_system re-pins, exclude blocks systems."""
    target = {"system": target_system, "partition": target_partition} if target_system else None
    return _client.replan(task_id, exclude=exclude, target=target, clear_target=clear_target)


@mcp.tool()
def recent_events(limit: int = 40, task_id: Optional[str] = None) -> list:
    """Agent activity feed: decisions, dispatches, continuations with rationale."""
    return _client.events(limit=limit, task_id=task_id)


if __name__ == "__main__":
    mcp.run()
