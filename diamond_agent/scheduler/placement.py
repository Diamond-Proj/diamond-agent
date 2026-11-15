"""Feasibility and sizing of one job on one queue.

``evaluate(record, envelope, policy)`` is the whole of what the core decides
about placement: the rule chain's verdicts, the elastic sizing inside the
envelope and the operator's cap, and a queue-wait estimate. The core never
chooses between queues — the agent asks for every queue's evaluation
(``POST /api/candidates``), picks one, and ``submit`` re-evaluates that queue
and refuses it if it is infeasible.
"""

from __future__ import annotations

from typing import Optional

from ..schemas import Candidate, ResourceEnvelope, ResourceRecord, RuleVerdict, Sizing
from . import rules


def estimate_wait(c: Candidate) -> float:
    """Queue-wait heuristic in minutes. Resource-level availability (free
    GPUs/cores from the GRES probe) beats node states: on shared-node systems
    every node can be 'mixed' while GPUs sit free — and vice versa."""
    r, s = c.record, c.sizing
    if s is not None:
        if s.gpus > 0 and r.gpus_free is not None and r.gpus_free >= s.gpus:
            return 2.0 + 0.05 * r.pending_jobs   # free GPUs now: backfill-friendly
        if s.gpus == 0 and r.cpus_free is not None and r.cpus_free >= s.cpus:
            return 2.0 + 0.05 * r.pending_jobs
    nodes = s.nodes if s else 1
    if r.nodes_idle >= nodes:
        return 1.0 + 0.2 * r.pending_jobs
    return 5.0 + 1.5 * r.pending_jobs


def evaluate(record: ResourceRecord, envelope: ResourceEnvelope, policy: dict,
             remaining_node_hours: Optional[float] = None) -> Candidate:
    """One queue, one envelope: verdicts; sizing and wait estimate when feasible.
    With a node-hour budget left (``remaining_node_hours``), the sizing is
    shrunk to fit it — nodes first, then wall time — and the queue is
    infeasible when even the minimum shape does not fit."""
    c = rules.evaluate_record(record, envelope, policy)
    if not c.feasible:
        return c
    sizing = rules.choose_sizing(record, envelope, policy)
    if remaining_node_hours is not None:
        sizing, verdict = fit_budget(sizing, record, envelope, remaining_node_hours)
        c.verdicts.append(verdict)
        c.feasible = verdict.passed
        if not c.feasible:
            return c
    c.sizing = sizing
    c.est_wait_minutes = estimate_wait(c)
    return c


def fit_budget(sizing: Sizing, record: ResourceRecord, env: ResourceEnvelope, remaining: float) -> tuple[Sizing, RuleVerdict]:
    lo = rules.min_nodes_needed(record, env)
    nodes, wall = sizing.nodes, sizing.walltime_minutes
    while nodes > lo and nodes * wall > remaining * 60:
        nodes -= 1
    if nodes * wall > remaining * 60:
        wall = int(remaining * 60 // nodes)
        if wall < env.walltime.min_minutes:
            return sizing, RuleVerdict(rule="budget", passed=False,
                                       detail=f"minimum shape needs {lo * env.walltime.min_minutes / 60:.2f} node-h, "
                                              f"{remaining:.2f} left")
    fitted = rules.shape(record, env, nodes, wall)
    shrunk = fitted.nodes != sizing.nodes or fitted.walltime_minutes != sizing.walltime_minutes
    return fitted, RuleVerdict(rule="budget", passed=True,
                               detail=f"{fitted.nodes * fitted.walltime_minutes / 60:.2f} node-h of {remaining:.2f} left"
                                      + (" (shrunk to fit)" if shrunk else ""))
