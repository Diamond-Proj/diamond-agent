"""Hybrid AI + rules meta-scheduler.

Pipeline for one placement decision:
  snapshot -> rule chain (deterministic, explainable)
           -> sizing inside the envelope (deterministic)
           -> wait estimation + heuristic scoring (deterministic)
           -> AI ranking of the feasible set (advisory, optional)
           -> chosen candidate

Rules bound what AI may choose; AI only orders what rules allow.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from ..schemas import (
    Candidate,
    Objective,
    PlacementDecision,
    ResourceEnvelope,
    ResourceSnapshot,
    TaskSpec,
)
from . import ai_ranker, rules

logger = logging.getLogger(__name__)

WaitEstimator = Callable[[str, str, int], float]  # (system, partition, nodes) -> minutes


def _fallback_wait(c: Candidate) -> float:
    """Queue-wait heuristic. Resource-level availability (free GPUs/cores from
    the GRES probe) beats node states: on shared-node systems every node can be
    'mixed' while GPUs sit free — and vice versa."""
    r = c.record
    s = c.sizing
    if s is not None:
        if s.gpus > 0 and r.gpus_free is not None and r.gpus_free >= s.gpus:
            return 2.0 + 0.05 * r.pending_jobs   # free GPUs now: backfill-friendly
        if s.gpus == 0 and r.cpus_free is not None and r.cpus_free >= s.cpus:
            return 2.0 + 0.05 * r.pending_jobs
    nodes = s.nodes if s else 1
    if r.nodes_idle >= nodes:
        return 1.0 + 0.2 * r.pending_jobs
    return 5.0 + 1.5 * r.pending_jobs


def _runtime_estimate(c: Candidate, env: ResourceEnvelope) -> float:
    """Assume scalable work: runtime shrinks with allocated share of max-useful size."""
    if c.sizing is None:
        return env.walltime.min_minutes
    if env.gpu.min_count > 0:
        useful, got = env.gpu.max_count, max(1, c.sizing.gpus)
    else:
        useful, got = max(env.cpu.max_count, 1), max(1, c.sizing.cpus)
    est = env.walltime.min_minutes * (useful / got)
    return min(max(est, env.walltime.min_minutes), env.walltime.max_minutes)


def _heuristic_score(c: Candidate, env: ResourceEnvelope) -> float:
    wait = c.est_wait_minutes if c.est_wait_minutes is not None else _fallback_wait(c)
    # Placing a CPU-only task on a GPU partition wastes accelerators (live
    # sites like Delta force every job on a GPU partition to request one) —
    # penalize hard enough that any CPU partition wins.
    waste = 200.0 if env.gpu.min_count == 0 and c.record.gpus_per_node > 0 else 0.0
    wait += waste
    if env.objective == Objective.MAX_THROUGHPUT:
        capability = (c.sizing.gpus * 100 + c.sizing.cpus) if c.sizing else 0
        return -capability + wait * 0.5
    if env.objective == Objective.MIN_QUEUE_IMPACT:
        nodes = c.sizing.nodes if c.sizing else 1
        return c.record.pending_jobs * 2 + nodes + wait * 0.5
    return wait + _runtime_estimate(c, env)  # earliest_completion


def _heuristic_rationale(c: Candidate, env: ResourceEnvelope) -> str:
    r = c.record
    hw = f"{r.gpu_type} x{r.gpus_per_node}/node" if r.gpu_type else f"{r.cpus_per_node} CPU cores/node"
    return (
        f"{hw}, {r.nodes_idle} idle node(s), {r.pending_jobs} pending job(s); "
        f"estimated wait {c.est_wait_minutes:.0f}m, sized {c.sizing.nodes} node(s) "
        f"within envelope ({env.objective.value})."
        if c.sizing and c.est_wait_minutes is not None
        else f"{hw}; heuristic score {c.score:.1f}"
    )


class MetaScheduler:
    def __init__(self, policy: dict, wait_estimator: Optional[WaitEstimator] = None,
                 use_ai: bool = True):
        self.policy = policy
        self.wait_estimator = wait_estimator
        self.use_ai = use_ai

    def decide(self, task: TaskSpec, snapshot: ResourceSnapshot) -> PlacementDecision:
        started = time.perf_counter()
        env = task.envelope
        candidates = [rules.evaluate_record(r, env, self.policy) for r in snapshot.records]
        feasible = [c for c in candidates if c.feasible]

        for c in feasible:
            c.sizing = rules.choose_sizing(c.record, env, self.policy)
            if self.wait_estimator is not None:
                try:
                    c.est_wait_minutes = self.wait_estimator(c.system, c.partition, c.sizing.nodes)
                except Exception:
                    c.est_wait_minutes = _fallback_wait(c)
            else:
                c.est_wait_minutes = _fallback_wait(c)
            c.score = _heuristic_score(c, env)

        decision = PlacementDecision(objective=env.objective, candidates=candidates)

        if not feasible:
            decision.mode = "rules"
            decision.summary = (
                "No feasible placement: every (system, partition) was eliminated by the rule chain. "
                "See per-candidate verdicts."
            )
            decision.decision_ms = round((time.perf_counter() - started) * 1000, 2)
            return decision

        ranking = ai_ranker.ai_rank(task, env, feasible) if self.use_ai else None
        if ranking is not None:
            ai_ranker.apply_ranking(feasible, ranking)
            decision.mode = "ai+rules"
            chosen = min(feasible, key=lambda c: c.ai_rank)
            decision.summary = ranking.summary
        else:
            decision.mode = "rules"
            chosen = min(feasible, key=lambda c: c.score)
            for i, c in enumerate(sorted(feasible, key=lambda c: c.score), start=1):
                c.ai_rank = i
                if c.rationale is None:
                    c.rationale = _heuristic_rationale(c, env)
            decision.summary = (
                f"Heuristic ranking ({env.objective.value}): chose {chosen.system}:{chosen.partition} "
                f"with score {chosen.score:.1f} among {len(feasible)} feasible candidate(s)."
            )

        decision.chosen = chosen
        decision.decision_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.info("placement: %s -> %s:%s nodes=%s mode=%s (%.0fms)",
                    task.name, chosen.system, chosen.partition,
                    chosen.sizing.nodes if chosen.sizing else "?", decision.mode,
                    decision.decision_ms)
        return decision
