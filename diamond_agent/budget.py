"""Node-hour budgets: what a channel may still spend on a machine or a queue.

A task costs nodes × hours: while it is active, at the wall time it asked for
(what Slurm may bill); once it has ended, at the elapsed time ``sacct``
reported. The ledger is derived from the task records every time — there is
nothing else to keep in sync.

Three limits, each optional (not configured = unlimited):

* the channel's cap on a queue      ``rules.systems[M].partitions[Q].node_hours``
* the channel's cap on a machine    ``rules.systems[M].node_hours``
* the machine budget, shared by every channel (machine rules ``node_hours[M]``),
  first come, first served.

``remaining`` is the smallest headroom among the limits that exist; the
evaluator shrinks a sizing (nodes first, then wall time) to fit it and
refuses the queue when even the minimum shape does not fit.
"""

from __future__ import annotations

from typing import Iterable, Optional

from .channels import Channel, ChannelRegistry

TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}


def node_hours(nodes: int, minutes: float) -> float:
    return nodes * minutes / 60


def task_cost(record) -> float:
    """Node-hours a placed task counts for right now."""
    sizing = record.placement.sizing
    ended = record.status.value in TERMINAL and record.elapsed_minutes is not None
    return node_hours(sizing.nodes, record.elapsed_minutes if ended else sizing.walltime_minutes)


class Ledger:
    """Node-hours charged so far, from the task records."""

    def __init__(self, records: Iterable, exclude: Optional[str] = None):
        self._rows = [(r.channel, r.placement.system, r.placement.partition, task_cost(r))
                      for r in records if r.placement is not None and r.task_id != exclude]

    def used(self, channel: Optional[str], system: str, partition: Optional[str] = None) -> float:
        return sum(cost for ch, s, p, cost in self._rows
                   if (channel is None or ch == channel) and s == system and (partition is None or p == partition))


def remaining(ledger: Ledger, channels: ChannelRegistry, channel: Channel, system: str,
              partition: Optional[str]) -> Optional[float]:
    """Node-hours the channel may still commit on this queue (or on the machine
    as a whole when ``partition`` is None); None when no limit applies."""
    limits = []
    srule = channel.rules.systems[system]
    prule = (srule.partitions or {}).get(partition) if partition else None
    if prule and prule.node_hours is not None:
        limits.append(prule.node_hours - ledger.used(channel.id, system, partition))
    if srule.node_hours is not None:
        limits.append(srule.node_hours - ledger.used(channel.id, system))
    budget = channels.machine_policy["node_hours"].get(system)
    if budget is not None:
        limits.append(budget - ledger.used(None, system))
    return min(limits) if limits else None
