"""Operator policy — the limits the core enforces on every client.

The input boundary of the service, stated once:

* ``submit`` is sized inside the queue's per-job node cap (rule chain +
  sizing), and the orchestrator re-checks the final sizing right before
  dispatch so an operator edit between decision and dispatch cannot let an
  over-cap job through.
* ``exec`` is for looking around: it refuses the Slurm commands that submit or
  steer jobs, and anything that would outlive the bounded call. Everything
  else runs and is written to the activity feed.
* ``put-file`` writes under the system's workdir only.

Effective policy = the catalog's ``policy:`` block, per-partition
``max_nodes_per_job`` declared on partition entries, and the operator's
overrides from the settings table (edited in the web UI only) on top. It is
one dict shared by the scheduler and the orchestrator, updated in place.
"""

from __future__ import annotations

import re
from typing import Optional


class PolicyDenied(PermissionError):
    """Refused by operator policy (HTTP 403), as opposed to a remote failure."""


def partition_key(system: str, partition: str) -> str:
    return f"{system}:{partition}"


def build_policy(catalog: dict, overrides: Optional[dict] = None) -> dict:
    base = catalog.get("policy") or {}
    ov = overrides or {}
    limits = {partition_key(s["name"], p["name"]): int(p["max_nodes_per_job"])
              for s in catalog["systems"] for p in s["partitions"] if p.get("max_nodes_per_job")}
    limits.update(base.get("partition_limits") or {})
    limits.update(ov.get("partition_limits") or {})
    blocked = (ov["blocked_partitions"] if ov.get("blocked_partitions") is not None
               else base.get("blocked_partitions") or [])
    budgets = {**(base.get("node_hours") or {}), **(ov.get("node_hours") or {})}     # machine -> node-hours
    return {
        "max_nodes_per_job": ov.get("max_nodes_per_job") or base.get("max_nodes_per_job") or 8,
        "max_user_pending_per_system": (ov.get("max_user_pending_per_system")
                                        or base.get("max_user_pending_per_system") or 4),
        "blocked_partitions": list(blocked),
        "partition_limits": limits,
        "node_hours": {k: float(v) for k, v in budgets.items()},
    }


def effective_node_cap(policy: dict, system: str, partition: str) -> int:
    return policy["partition_limits"].get(partition_key(system, partition)) or policy["max_nodes_per_job"]


def is_blocked(policy: dict, system: str, partition: str) -> bool:
    return partition_key(system, partition) in policy["blocked_partitions"]


# ------------------------------------------------------------------ exec

JOB_CONTROL = frozenset({"sbatch", "srun", "salloc", "scancel", "scrontab"})
DETACHING = frozenset({"nohup", "setsid", "disown", "screen", "tmux"})
_WORDS = re.compile(r"[\s;|&(){}]+")


def check_shell_command(command: str) -> Optional[str]:
    """Why ``exec`` refuses this command line, or None when it may run."""
    words = {tok.strip("'\"").rsplit("/", 1)[-1] for tok in _WORDS.split(command)}
    if hit := words & JOB_CONTROL:
        return (f"`{min(hit)}` submits or controls jobs; `exec` is for inspection — "
                "submit a task so the operator's limits apply")
    if re.search(r"\bscontrol\b(?!\s+show\b)", command):
        return "`scontrol` other than `scontrol show` controls jobs; `exec` is for inspection"
    if hit := words & DETACHING:
        return f"`{min(hit)}` would outlive the bounded call"
    if re.search(r"(?<![&>|])&(?![&>])", command):
        return "background execution (&) would outlive the bounded call"
    return None


# -------------------------------------------------------------- put-file

_STAGE_PATH = re.compile(r"^(?:[A-Za-z0-9._+-]+/)*[A-Za-z0-9._+-]+$")


def check_stage_path(path: str) -> Optional[str]:
    """Why ``put-file`` refuses this path, or None when it stays in the workdir."""
    if not _STAGE_PATH.match(path) or ".." in path.split("/"):
        return "paths are relative to the workdir: letters, digits, . _ + - and /, no .. and no leading /"
    return None
