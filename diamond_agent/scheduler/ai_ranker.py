"""AI half of the hybrid meta-scheduler.

The deterministic rule layer computes the feasible set; this module orders it.
Claude sees the envelope, the task intent, and per-candidate queue statistics,
and returns a ranking with rationales. If AI is unavailable (or returns
garbage) the heuristic scores computed by the rule layer stand — AI can never
introduce a candidate that rules rejected.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from pydantic import BaseModel

from ..agent.ai import ask_structured
from ..schemas import Candidate, ResourceEnvelope, TaskSpec

logger = logging.getLogger(__name__)

_SYSTEM = """You are the placement ranker of Diamond Agent, a meta-scheduler that treats a
user's federated HPC allocations as one abstract supercomputer.

You receive a task, its resource envelope (min viable / max useful ranges), and the
feasible candidates that already passed every deterministic rule (authorization,
compatibility, walltime, site policy, user limits). Rank the candidates for the stated
objective:

- earliest_completion: minimize (queue wait + expected runtime). Larger sizing inside the
  envelope shortens runtime roughly proportionally for scalable work, but deep queues can
  dominate. Prefer idle capacity now over slightly better hardware later.
- max_throughput: maximize delivered GPU/CPU capability within the envelope.
- min_queue_impact: prefer lightly-loaded partitions and modest sizing near envelope min.

Consider GPU generation quality (GH200 > H100 > A100 > A40 > RTX5000 for ML), per-node
bandwidth, and queue depth. Give each candidate a one-sentence rationale a user can read
on a dashboard. Rank 1 = best. Keep every candidate in the output exactly once, using the
same system and partition strings you were given."""


class RankedCandidate(BaseModel):
    system: str
    partition: str
    rank: int
    est_wait_minutes: float
    rationale: str


class RankingResult(BaseModel):
    ranking: list[RankedCandidate]
    summary: str


def _candidate_facts(c: Candidate) -> dict:
    r = c.record
    return {
        "system": c.system,
        "partition": c.partition,
        "account": c.account,
        "gpu_type": r.gpu_type,
        "gpus_per_node": r.gpus_per_node,
        "gpu_mem_gb": r.gpu_mem_gb,
        "cpus_per_node": r.cpus_per_node,
        "mem_per_node_gb": r.mem_per_node_gb,
        "node_arch": r.node_arch,
        "nodes_idle": r.nodes_idle,
        "nodes_total": r.nodes_total,
        "pending_jobs": r.pending_jobs,
        "running_jobs": r.running_jobs,
        "user_pending_jobs": r.user_pending_jobs,
        "max_walltime_minutes": r.max_walltime_minutes,
        "estimated_wait_minutes": c.est_wait_minutes,
        "proposed_sizing": c.sizing.model_dump() if c.sizing else None,
        "heuristic_score": c.score,
    }


def ai_rank(
    task: TaskSpec,
    envelope: ResourceEnvelope,
    feasible: list[Candidate],
) -> Optional[RankingResult]:
    if len(feasible) < 2:
        return None
    user = json.dumps(
        {
            "task": {"name": task.name, "description": task.description, "command": task.command},
            "objective": envelope.objective.value,
            "envelope": envelope.model_dump(),
            "candidates": [_candidate_facts(c) for c in feasible],
        },
        default=str,
    )
    result = ask_structured(_SYSTEM, user, RankingResult)
    if result is None:
        return None

    # Validate: AI must cover exactly the feasible set — otherwise discard.
    given = {(c.system, c.partition) for c in feasible}
    returned = {(r.system, r.partition) for r in result.ranking}
    if given != returned or len(result.ranking) != len(feasible):
        logger.warning("AI ranking did not match feasible set; ignoring (given=%s returned=%s)",
                       given, returned)
        return None
    return result


def apply_ranking(feasible: list[Candidate], result: RankingResult) -> None:
    by_key = {(r.system, r.partition): r for r in result.ranking}
    for c in feasible:
        r = by_key[(c.system, c.partition)]
        c.ai_rank = r.rank
        c.rationale = r.rationale
        c.est_wait_minutes = r.est_wait_minutes
