"""Deterministic rule layer of the meta-scheduler.

Every (endpoint, partition) record is checked against the envelope by an
ordered chain of rules. Rules are authoritative: the AI ranker can only order
candidates that survived every rule — it can never resurrect a filtered one.
Each verdict carries a human-readable detail so the UI can explain exactly
why a partition was eliminated.
"""

from __future__ import annotations

import math
from typing import Callable, Optional

from ..policy import effective_node_cap, is_blocked
from ..schemas import (
    Candidate,
    EndpointHealth,
    ResourceEnvelope,
    ResourceRecord,
    RuleVerdict,
    Sizing,
)

Rule = Callable[[ResourceRecord, ResourceEnvelope, dict], RuleVerdict]


def _verdict(rule: str, passed: bool, detail: str) -> RuleVerdict:
    return RuleVerdict(rule=rule, passed=passed, detail=detail)


def min_nodes_needed(record: ResourceRecord, env: ResourceEnvelope) -> int:
    """Smallest node count that satisfies every *min* requirement on this record."""
    needs = [1]
    if env.gpu.min_count > 0 and record.gpus_per_node > 0:
        needs.append(math.ceil(env.gpu.min_count / record.gpus_per_node))
    if env.cpu.min_count > 0 and record.cpus_per_node > 0:
        needs.append(math.ceil(env.cpu.min_count / record.cpus_per_node))
    if env.memory.min_gb > 0 and record.mem_per_node_gb > 0:
        needs.append(math.ceil(env.memory.min_gb / record.mem_per_node_gb))
    return max(needs)


def rule_endpoint_health(r: ResourceRecord, env: ResourceEnvelope, policy: dict) -> RuleVerdict:
    ok = r.endpoint_health == EndpointHealth.ONLINE
    return _verdict("endpoint_health", ok, f"endpoint is {r.endpoint_health.value}")


def rule_authorization(r: ResourceRecord, env: ResourceEnvelope, policy: dict) -> RuleVerdict:
    if not r.account:
        return _verdict("authorization", False, "no allocation account on this system")
    if is_blocked(policy, r.system, r.partition):
        return _verdict("authorization", False,
                        f"partition {r.system}:{r.partition} blocked by operator policy")
    return _verdict("authorization", True, f"allocation '{r.account}' authorized")


def rule_cpu_arch(r: ResourceRecord, env: ResourceEnvelope, policy: dict) -> RuleVerdict:
    allowed = env.compatibility.cpu_arch
    ok = not allowed or r.node_arch in allowed
    return _verdict(
        "cpu_arch", ok,
        f"{r.node_arch} {'in' if ok else 'not in'} {allowed or ['any']}",
    )


def rule_accelerator(r: ResourceRecord, env: ResourceEnvelope, policy: dict) -> RuleVerdict:
    if env.gpu.min_count <= 0:
        return _verdict("accelerator", True, "task does not require GPUs")
    if r.gpus_per_node <= 0 or not r.gpu_type:
        return _verdict("accelerator", False, "partition has no GPUs")
    allowed = env.compatibility.accelerators
    if allowed and r.gpu_type not in allowed:
        return _verdict("accelerator", False, f"{r.gpu_type} not in compatible set {allowed}")
    return _verdict("accelerator", True, f"{r.gpu_type} x{r.gpus_per_node}/node compatible")


def rule_gpu_memory(r: ResourceRecord, env: ResourceEnvelope, policy: dict) -> RuleVerdict:
    need = env.gpu.min_memory_gb
    if env.gpu.min_count <= 0 or need <= 0:
        return _verdict("gpu_memory", True, "no per-GPU memory floor")
    ok = r.gpu_mem_gb >= need
    return _verdict("gpu_memory", ok, f"{r.gpu_mem_gb:g}GB/GPU vs required {need:g}GB")


def rule_capacity(r: ResourceRecord, env: ResourceEnvelope, policy: dict) -> RuleVerdict:
    nodes = min_nodes_needed(r, env)
    cap = effective_node_cap(policy, r.system, r.partition)
    usable = r.nodes_total - r.nodes_down
    if nodes > cap:
        return _verdict("capacity", False,
                        f"needs {nodes} nodes > per-job cap {cap} on {r.system}:{r.partition}")
    if nodes > usable:
        return _verdict("capacity", False, f"needs {nodes} nodes > {usable} usable in partition")
    return _verdict("capacity", True, f"min shape fits in {nodes} node(s)")


def rule_walltime(r: ResourceRecord, env: ResourceEnvelope, policy: dict) -> RuleVerdict:
    if r.max_walltime_minutes is None:
        return _verdict("walltime", True, "partition has no walltime cap")
    ok = r.max_walltime_minutes >= env.walltime.min_minutes
    return _verdict(
        "walltime", ok,
        f"partition cap {r.max_walltime_minutes}m vs required min {env.walltime.min_minutes}m",
    )


def rule_container(r: ResourceRecord, env: ResourceEnvelope, policy: dict) -> RuleVerdict:
    if not env.compatibility.container_required:
        return _verdict("container", True, "container not required")
    ok = bool(r.container_runtime)
    return _verdict("container", ok, f"container runtime: {r.container_runtime or 'none'}")


def rule_user_limits(r: ResourceRecord, env: ResourceEnvelope, policy: dict) -> RuleVerdict:
    cap = policy["max_user_pending_per_system"]
    ok = r.user_pending_jobs < cap
    return _verdict(
        "user_limits", ok,
        f"{r.user_pending_jobs} pending job(s) on system vs cap {cap}",
    )


RULE_CHAIN: list[Rule] = [
    rule_endpoint_health,
    rule_authorization,
    rule_cpu_arch,
    rule_accelerator,
    rule_gpu_memory,
    rule_capacity,
    rule_walltime,
    rule_container,
    rule_user_limits,
]


def evaluate_record(record: ResourceRecord, env: ResourceEnvelope, policy: dict) -> Candidate:
    verdicts = [rule(record, env, policy) for rule in RULE_CHAIN]
    return Candidate(
        endpoint=record.endpoint,
        system=record.system,
        partition=record.partition,
        account=record.account,
        record=record,
        verdicts=verdicts,
        feasible=all(v.passed for v in verdicts),
    )


def choose_sizing(
    record: ResourceRecord,
    env: ResourceEnvelope,
    policy: dict,
    idle_nodes: Optional[int] = None,
) -> Sizing:
    """Pick a concrete shape inside the envelope for this partition.

    Strategy: start from the max-useful shape; if the partition cannot start it
    from idle capacity right now, shrink toward the min-viable shape so the job
    can begin sooner (resource elasticity is the point of the envelope).
    """
    idle = record.nodes_idle if idle_nodes is None else idle_nodes
    cap = effective_node_cap(policy, record.system, record.partition)
    lo = min_nodes_needed(record, env)

    if env.gpu.min_count > 0 and record.gpus_per_node > 0:
        hi = math.ceil(env.gpu.max_count / record.gpus_per_node)
    elif record.cpus_per_node > 0:
        hi = math.ceil(max(env.cpu.max_count, env.cpu.min_count) / record.cpus_per_node)
    else:
        hi = lo
    hi = max(lo, min(hi, cap, record.nodes_total - record.nodes_down))

    nodes = hi
    while nodes > lo and nodes > idle:
        nodes -= 1  # shrink until it fits idle capacity (or reaches min viable)

    gpus = min(env.gpu.max_count, nodes * record.gpus_per_node) if env.gpu.min_count > 0 else 0
    cpus = max(env.cpu.min_count, min(env.cpu.max_count, nodes * record.cpus_per_node))
    walltime = env.walltime.max_minutes
    if record.max_walltime_minutes is not None:
        walltime = min(walltime, record.max_walltime_minutes)
    return Sizing(nodes=nodes, gpus=gpus, cpus=cpus, walltime_minutes=walltime)
