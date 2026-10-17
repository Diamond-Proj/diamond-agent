"""Task orchestrator: lifecycle management + event-triggered agent continuation.

The monitor tick runs after every federation refresh. It advances task state
from executor status, detects events (job failed, endpoint down, queue wait
exceeding estimate), and lets the agent continue autonomously: retry in place,
resize within the envelope, or migrate to the next-best feasible system.
Every continuation action is recorded with a rationale on the task timeline.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from ..execution.base import Executor
from ..federation.collector import FederationCollector
from ..policy import effective_node_cap, is_blocked
from ..scheduler.meta import MetaScheduler
from ..schemas import (
    Candidate,
    EndpointHealth,
    PlacementDecision,
    ResourceSnapshot,
    Sizing,
    TaskSpec,
    TaskStatus,
    utcnow,
)
from ..store import Store

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = {TaskStatus.SUBMITTED, TaskStatus.SCHEDULED, TaskStatus.PENDING,
                   TaskStatus.RUNNING, TaskStatus.UNSCHEDULABLE}
PENDING_TIMEOUT_MIN_MINUTES = 16.0     # never fire before this much queue wait
UNSCHEDULABLE_RETRY_TICKS = 5          # re-run the scheduler for parked tasks


def _tick_minutes() -> float:
    """Monitor-tick duration in minutes (ticks are the monitor's clock)."""
    try:
        return max(float(os.environ.get("DIAMOND_AGENT_TICK", "120")), 1.0) / 60.0
    except ValueError:
        return 2.0


class TaskEvent(BaseModel):
    ts: datetime = Field(default_factory=utcnow)
    kind: str
    message: str
    detail: dict = Field(default_factory=dict)


class Placement(BaseModel):
    system: str
    partition: str
    endpoint: str
    account: str
    sizing: Sizing


class TaskRecord(BaseModel):
    task_id: str
    spec: TaskSpec
    status: TaskStatus = TaskStatus.SUBMITTED
    envelope_source: str = "user"
    envelope_rationale: str = ""
    envelope_confidence: Optional[float] = None
    decision: Optional[PlacementDecision] = None
    placement: Optional[Placement] = None
    job_id: Optional[str] = None
    attempts: int = 0
    excluded_systems: list[str] = Field(default_factory=list)
    # Explicit placement ("submit to THIS resource"): the pool-level "all
    # compute in one" API lets an agent pick any system[:partition]; the
    # meta-scheduler then only sizes inside the envelope on that target.
    target_system: Optional[str] = None
    target_partition: Optional[str] = None
    pending_ticks: int = 0
    running_ticks: int = 0
    unsched_ticks: int = 0
    timeout_migrations: int = 0
    group_id: Optional[str] = None
    group_round: int = 0
    queue_state_at_submit: Optional[dict] = None
    created_at: datetime = Field(default_factory=utcnow)
    finished_at: Optional[datetime] = None
    timeline: list[TaskEvent] = Field(default_factory=list)


class TaskGroup(BaseModel):
    """A parent task fanned out into child tasks across the federation (RQ4)."""

    group_id: str
    name: str
    description: str = ""
    rounds: int = 1
    current_round: int = 1
    distinct_systems: bool = True
    child_specs: list[TaskSpec] = Field(default_factory=list)
    round_children: list[list[str]] = Field(default_factory=list)  # round -> task ids
    status: str = "RUNNING"  # RUNNING | COMPLETED | FAILED
    created_at: datetime = Field(default_factory=utcnow)
    finished_at: Optional[datetime] = None


class TaskManager:
    def __init__(self, store: Store, collector: FederationCollector,
                 scheduler: MetaScheduler, executor: Executor):
        self.store = store
        self.collector = collector
        self.scheduler = scheduler
        self.executor = executor
        self.lock = threading.RLock()
        self.tasks: dict[str, TaskRecord] = {}
        self.groups: dict[str, TaskGroup] = {}
        for task_id, data in store.load_tasks().items():
            try:
                self.tasks[task_id] = TaskRecord.model_validate(data)
            except Exception:
                logger.exception("could not restore task %s", task_id)
        for group_id, data in store.load_groups().items():
            try:
                self.groups[group_id] = TaskGroup.model_validate(data)
            except Exception:
                logger.exception("could not restore group %s", group_id)
        # Re-attach persisted in-flight jobs to their endpoints (live executor
        # keeps the job->endpoint mapping in memory only).
        bind = getattr(executor, "bind_job", None)
        if callable(bind):
            for record in self.tasks.values():
                if (record.status in (TaskStatus.PENDING, TaskStatus.RUNNING)
                        and record.job_id and record.placement):
                    bind(record.job_id, record.placement.system)

    # ------------------------------------------------------------ utilities

    def _event(self, record: TaskRecord, kind: str, message: str, detail: Optional[dict] = None):
        event = TaskEvent(kind=kind, message=message, detail=detail or {})
        record.timeline.append(event)
        self.store.add_event(kind, message, task_id=record.task_id, detail=detail)
        logger.info("[%s] %s: %s", record.task_id[:8], kind, message)

    def _save(self, record: TaskRecord):
        self.store.save_task(record.task_id, record.model_dump(mode="json"))

    def _filtered_snapshot(self, record: TaskRecord) -> Optional[ResourceSnapshot]:
        snapshot = self.collector.latest
        if snapshot is None:
            return None
        excluded = set(record.excluded_systems)
        records = [r for r in snapshot.records if r.system not in excluded]
        if record.target_system:
            records = [r for r in records if r.system == record.target_system
                       and (record.target_partition is None
                            or r.partition == record.target_partition)]
        return ResourceSnapshot(taken_at=snapshot.taken_at, records=records)

    # ------------------------------------------------------------ lifecycle

    def submit(self, spec: TaskSpec, envelope_source: str = "user",
               envelope_rationale: str = "", envelope_confidence: Optional[float] = None,
               group_id: Optional[str] = None, group_round: int = 0,
               initial_excluded: Optional[list[str]] = None,
               target: Optional[tuple[str, Optional[str]]] = None) -> TaskRecord:
        with self.lock:
            record = TaskRecord(
                task_id=uuid.uuid4().hex[:12],
                spec=spec,
                envelope_source=envelope_source,
                envelope_rationale=envelope_rationale,
                envelope_confidence=envelope_confidence,
                group_id=group_id,
                group_round=group_round,
                excluded_systems=list(initial_excluded or []),
                target_system=target[0] if target else None,
                target_partition=target[1] if target else None,
            )
            self.tasks[record.task_id] = record
            where = (f", pinned to {record.target_system}"
                     + (f":{record.target_partition}" if record.target_partition else "")
                     if record.target_system else ", placement: auto")
            self._event(record, "TASK_SUBMITTED",
                        f"Task '{spec.name}' accepted (envelope: {envelope_source}, "
                        f"objective: {spec.envelope.objective.value}{where})")
            self._schedule_and_dispatch(record)
            self._save(record)
            return record

    def _schedule_and_dispatch(self, record: TaskRecord, reason: str = "initial placement"):
        snapshot = self._filtered_snapshot(record)
        if snapshot is None or not snapshot.records:
            record.status = TaskStatus.UNSCHEDULABLE
            self._event(record, "TASK_UNSCHEDULABLE",
                        "No federation snapshot available yet" if snapshot is None
                        else (f"Pinned target {record.target_system}"
                              f"{':' + record.target_partition if record.target_partition else ''}"
                              " is not in the current snapshot" if record.target_system
                              else "All systems excluded or unavailable"))
            return

        decision = self.scheduler.decide(record.spec, snapshot)
        record.decision = decision
        feasible = sum(1 for c in decision.candidates if c.feasible)
        self._event(record, "PLACEMENT_DECIDED",
                    f"Meta-scheduler ({decision.mode}) evaluated {len(decision.candidates)} "
                    f"candidates, {feasible} feasible ({reason}). {decision.summary}",
                    {"mode": decision.mode, "feasible": feasible})

        if decision.chosen is None:
            record.status = TaskStatus.UNSCHEDULABLE
            record.unsched_ticks = 0
            self._event(record, "TASK_UNSCHEDULABLE",
                        "No feasible placement right now; agent will retry as the "
                        "federation state changes")
            return
        self._dispatch(record, decision.chosen)

    def _policy_gate(self, record: TaskRecord, chosen: Candidate) -> Optional[str]:
        """Last check before anything reaches a batch system: the sizing the
        scheduler produced (or a continuation kept) must still respect the
        operator's limits, which may have changed since the decision."""
        policy = self.scheduler.policy
        if is_blocked(policy, chosen.system, chosen.partition):
            return f"{chosen.system}:{chosen.partition} is blocked by operator policy"
        cap = effective_node_cap(policy, chosen.system, chosen.partition)
        nodes = chosen.sizing.nodes if chosen.sizing else 1
        if nodes > cap:
            return (f"sizing asks for {nodes} node(s) but the per-job cap on "
                    f"{chosen.system}:{chosen.partition} is {cap}")
        return None

    def _dispatch(self, record: TaskRecord, chosen: Candidate):
        denied = self._policy_gate(record, chosen)
        if denied:
            self._event(record, "POLICY_DENIED",
                        f"Not submitted to {chosen.system}:{chosen.partition}: {denied}",
                        {"system": chosen.system, "partition": chosen.partition})
            if chosen.system not in record.excluded_systems:
                record.excluded_systems.append(chosen.system)
            self._schedule_and_dispatch(record, reason="placement refused by policy, trying next system")
            return
        try:
            handle = self.executor.submit(record.spec, chosen)
        except Exception as exc:
            self._event(record, "JOB_SUBMIT_FAILED",
                        f"Submission to {chosen.system}:{chosen.partition} failed: {exc}")
            if chosen.system not in record.excluded_systems:
                record.excluded_systems.append(chosen.system)
            self._schedule_and_dispatch(record, reason="submit failure, trying next system")
            return
        record.placement = Placement(
            system=chosen.system, partition=chosen.partition, endpoint=chosen.endpoint,
            account=chosen.account, sizing=chosen.sizing or Sizing(),
        )
        record.job_id = handle.job_id
        record.status = TaskStatus.PENDING
        record.pending_ticks = 0
        record.queue_state_at_submit = {
            "nodes_idle": chosen.record.nodes_idle,
            "pending_jobs": chosen.record.pending_jobs,
            "running_jobs": chosen.record.running_jobs,
            "est_wait_minutes": chosen.est_wait_minutes,
        }
        sizing = record.placement.sizing
        env = record.spec.envelope
        self._event(record, "JOB_SUBMITTED",
                    f"Job {handle.job_id} submitted to {chosen.system}:{chosen.partition} "
                    f"({sizing.nodes} node(s), {sizing.gpus} GPU(s), "
                    f"walltime {sizing.walltime_minutes}m). {chosen.rationale or ''}",
                    {"job_id": handle.job_id, "sizing": sizing.model_dump()})

    def cancel(self, task_id: str) -> Optional[TaskRecord]:
        with self.lock:
            record = self.tasks.get(task_id)
            if record is None:
                return None
            if record.job_id:
                self.executor.cancel(record.job_id)
            record.status = TaskStatus.CANCELLED
            record.finished_at = utcnow()
            self._event(record, "TASK_CANCELLED", "Cancelled by user")
            self._log_terminal(record, "user cancel")
            self._save(record)
            return record

    def replan(self, task_id: str, exclude: Optional[list[str]] = None,
               target: Optional[tuple[str, Optional[str]]] = None,
               clear_target: bool = False) -> Optional[TaskRecord]:
        with self.lock:
            record = self.tasks.get(task_id)
            if record is None:
                return None
            if record.job_id and record.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
                self.executor.cancel(record.job_id)
            record.excluded_systems = list(exclude) if exclude is not None else []
            if clear_target:
                record.target_system = record.target_partition = None
            elif target:
                record.target_system, record.target_partition = target
            detail = (f" (constrained to exclude {', '.join(record.excluded_systems)})"
                      if record.excluded_systems else "")
            self._event(record, "AGENT_REPLAN",
                        f"Manual replan requested; re-running meta-scheduler{detail}")
            self._schedule_and_dispatch(record, reason="manual replan")
            self._save(record)
            return record

    # ------------------------------------------------------------- monitor

    def tick(self):
        """Advance all active tasks. Called after every federation refresh."""
        with self.lock:
            snapshot = self.collector.latest
            for record in self.tasks.values():
                if record.status not in ACTIVE_STATUSES:
                    continue
                try:
                    self._tick_task(record, snapshot)
                except Exception:
                    logger.exception("monitor tick failed for %s", record.task_id)
                self._save(record)
            self._tick_groups()

    def _log_terminal(self, record: TaskRecord, note: str = ""):
        placement = record.placement

    def _tick_task(self, record: TaskRecord, snapshot: Optional[ResourceSnapshot]):
        if record.status == TaskStatus.UNSCHEDULABLE:
            if not record.spec.auto_continue:
                return
            record.unsched_ticks += 1
            if record.unsched_ticks >= UNSCHEDULABLE_RETRY_TICKS:
                record.unsched_ticks = 0
                record.excluded_systems = []
                self._schedule_and_dispatch(record, reason="periodic retry of parked task")
            return

        if record.status not in (TaskStatus.PENDING, TaskStatus.RUNNING) or record.job_id is None:
            return

        # Event: endpoint down while our job waits in its queue.
        if (record.status == TaskStatus.PENDING and snapshot is not None
                and record.placement is not None):
            health = self._system_health(snapshot, record.placement.system)
            if (health == EndpointHealth.OFFLINE and record.spec.auto_continue
                    and record.target_system):
                self._event(record, "ENDPOINT_DOWN",
                            f"Pinned target {record.target_system} went offline while job "
                            f"{record.job_id} was pending; task is pinned, so it parks "
                            "until the endpoint returns (replan without a pin to migrate)")
                try:
                    self.executor.cancel(record.job_id)
                except Exception as exc:
                    self._event(record, "JOB_ORPHANED",
                                f"Could not cancel job {record.job_id} through the offline "
                                f"endpoint ({exc})")
                record.status = TaskStatus.UNSCHEDULABLE
                record.job_id = None
                return
            if health == EndpointHealth.OFFLINE and record.spec.auto_continue:
                self._event(record, "ENDPOINT_DOWN",
                            f"Endpoint for {record.placement.system} went offline while job "
                            f"{record.job_id} was pending; migrating to another system")
                # A pin to a dead endpoint serves nothing — drop any sibling
                # exclusions so the task can go wherever is alive (same spirit
                # as AGENT_CONSTRAINT_RELAXED).
                record.excluded_systems = []
                try:
                    self.executor.cancel(record.job_id)
                except Exception as exc:
                    self._event(record, "JOB_ORPHANED",
                                f"Could not cancel job {record.job_id} through the offline "
                                f"endpoint ({exc}); it stays queued there and will run or "
                                "expire on its own")
                self._continue(record, exclude_current=True,
                               reason="endpoint offline during queue wait")
                return

        state, note = self.executor.status(record.job_id)

        if state == "PENDING":
            record.pending_ticks += 1
            self._check_pending_timeout(record)
        elif state == "RUNNING":
            if record.status != TaskStatus.RUNNING:
                record.status = TaskStatus.RUNNING
                self._event(record, "JOB_STARTED",
                            f"Job {record.job_id} started on {record.placement.system}:"
                            f"{record.placement.partition} after ~{record.pending_ticks} min in queue")
            else:
                record.running_ticks += 1
        elif state == "COMPLETED":
            record.status = TaskStatus.COMPLETED
            record.finished_at = utcnow()
            self._event(record, "JOB_COMPLETED",
                        f"Job {record.job_id} completed on {record.placement.system}")
            self._log_terminal(record)
        elif state == "FAILED":
            self._handle_failure(record, note)
        elif state == "CANCELLED":
            record.status = TaskStatus.CANCELLED
            record.finished_at = utcnow()
            self._event(record, "JOB_CANCELLED", f"Job {record.job_id} was cancelled ({note})")
            self._log_terminal(record)

    @staticmethod
    def _system_health(snapshot: ResourceSnapshot, system: str) -> Optional[EndpointHealth]:
        for r in snapshot.records:
            if r.system == system:
                return r.endpoint_health
        return None

    def _check_pending_timeout(self, record: TaskRecord):
        if not record.spec.auto_continue or record.timeout_migrations >= 1:
            return
        chosen = record.decision.chosen if record.decision else None
        est = chosen.est_wait_minutes if chosen and chosen.est_wait_minutes else 5.0
        waited_minutes = record.pending_ticks * _tick_minutes()
        threshold = max(PENDING_TIMEOUT_MIN_MINUTES, est * 3)
        if waited_minutes < threshold:
            return
        # Re-plan on the latest snapshot; migrate only if somewhere else is clearly better.
        snapshot = self._filtered_snapshot(record)
        if snapshot is None:
            return
        alt_records = [r for r in snapshot.records if r.system != record.placement.system]
        decision = self.scheduler.decide(record.spec,
                                         ResourceSnapshot(taken_at=snapshot.taken_at,
                                                          records=alt_records))
        alt = decision.chosen
        if alt is None or (alt.est_wait_minutes or 999) >= waited_minutes:
            record.timeout_migrations = 1  # nothing better; stop checking
            self._event(record, "PENDING_TIMEOUT",
                        f"Queue wait ({waited_minutes:.0f}m) exceeded estimate ({est:.0f}m) "
                        "but no better placement exists; staying in queue")
            return
        record.timeout_migrations = 1
        self._event(record, "PENDING_TIMEOUT",
                    f"Queue wait ({waited_minutes:.0f}m) exceeded 3x estimate ({est:.0f}m); "
                    f"migrating to {alt.system}:{alt.partition} "
                    f"(est wait {alt.est_wait_minutes:.0f}m)")
        self.executor.cancel(record.job_id)
        record.decision = decision
        self._event(record, "AGENT_MIGRATED",
                    f"Agent migrated task from {record.placement.system} to {alt.system} "
                    "to meet the earliest-completion objective")
        self._dispatch(record, alt)

    # ------------------------------------------------- failure continuation

    def _handle_failure(self, record: TaskRecord, note: str):
        record.attempts += 1
        self._event(record, "JOB_FAILED",
                    f"Job {record.job_id} failed on {record.placement.system}"
                    f"{f' ({note})' if note else ''} — attempt {record.attempts}")

        if not record.spec.auto_continue or record.attempts > record.spec.max_retries:
            record.status = TaskStatus.FAILED
            record.finished_at = utcnow()
            self._event(record, "AGENT_GAVE_UP",
                        "Retry budget exhausted" if record.spec.auto_continue
                        else "Auto-continuation disabled; task marked failed")
            self._log_terminal(record, note)
            return

        lower = (note or "").lower()
        if "timeout" in lower or "walltime" in lower:
            self._continue_resize(record)
        elif "offline" in lower:
            self._continue(record, exclude_current=True,
                           reason=f"failure looks site-related ({note}); trying a different system")
        elif record.attempts == 1:
            self._continue(record, exclude_current=False,
                           reason="first failure treated as transient; retrying same placement")
        else:
            self._continue(record, exclude_current=True,
                           reason="repeated failure; migrating to a different system")

    def _continue_resize(self, record: TaskRecord):
        """Timeout continuation: retry in place with a longer walltime."""
        placement = record.placement
        cap = None
        snapshot = self.collector.latest
        if snapshot:
            for r in snapshot.records:
                if r.system == placement.system and r.partition == placement.partition:
                    cap = r.max_walltime_minutes
        new_walltime = placement.sizing.walltime_minutes * 2
        if cap:
            new_walltime = min(new_walltime, cap)
        if new_walltime <= placement.sizing.walltime_minutes:
            self._continue(record, exclude_current=True,
                           reason="walltime already at partition cap; migrating instead")
            return
        placement.sizing.walltime_minutes = new_walltime
        self._event(record, "AGENT_RESIZED",
                    f"Job hit its walltime; agent resubmitting with walltime doubled to "
                    f"{new_walltime}m (within envelope/partition caps)")
        chosen = record.decision.chosen if record.decision else None
        if chosen is not None:
            chosen.sizing = placement.sizing
            self._dispatch(record, chosen)
        else:
            self._schedule_and_dispatch(record, reason="resize retry")

    def _continue(self, record: TaskRecord, exclude_current: bool, reason: str):
        if exclude_current and record.placement and record.target_system:
            # Migration would leave the pin; retry in place instead and say so.
            self._event(record, "AGENT_RETRY",
                        f"Agent continuation: {reason} — task is pinned to "
                        f"{record.target_system}, retrying there instead of migrating")
            self._schedule_and_dispatch(record, reason=reason)
            return
        if exclude_current and record.placement:
            if record.placement.system not in record.excluded_systems:
                record.excluded_systems.append(record.placement.system)
            self._event(record, "AGENT_MIGRATED",
                        f"Agent continuation: {reason} (excluding {record.placement.system})")
        else:
            self._event(record, "AGENT_RETRY", f"Agent continuation: {reason}")
        self._schedule_and_dispatch(record, reason=reason)

    # ------------------------------------------------- task groups (RQ4)

    def submit_group(self, name: str, child_specs: list[TaskSpec], rounds: int = 1,
                     distinct_systems: bool = True, description: str = "") -> TaskGroup:
        """Fan a parent task out into child tasks across the federation."""
        with self.lock:
            group = TaskGroup(
                group_id=uuid.uuid4().hex[:12], name=name, description=description,
                rounds=max(1, rounds), distinct_systems=distinct_systems,
                child_specs=child_specs,
            )
            self.groups[group.group_id] = group
            self.store.add_event(
                "GROUP_SUBMITTED",
                f"Parent task '{name}' fanned out into {len(child_specs)} child task(s)"
                f"{' on distinct systems' if distinct_systems else ''}, "
                f"{group.rounds} round(s) planned",
                detail={"group_id": group.group_id})
            self._launch_round(group)
            self.store.save_group(group.group_id, group.model_dump(mode="json"))
            return group

    def _launch_round(self, group: TaskGroup):
        round_no = group.current_round
        used_systems: list[str] = []
        child_ids = []
        for i, spec in enumerate(group.child_specs):
            child = spec.model_copy(deep=True)
            child.name = f"{group.name}-r{round_no}-c{i + 1}"
            record = self.submit(
                child, envelope_source="group",
                envelope_rationale=f"child {i + 1} of group '{group.name}' round {round_no}",
                group_id=group.group_id, group_round=round_no,
                initial_excluded=used_systems if group.distinct_systems else None,
            )
            child_ids.append(record.task_id)
            if record.placement is not None:
                used_systems.append(record.placement.system)
        group.round_children.append(child_ids)
        self.store.add_event(
            "GROUP_ROUND_LAUNCHED",
            f"Group '{group.name}' round {round_no}: {len(child_ids)} child task(s) placed on "
            f"{', '.join(used_systems) or 'no system yet'}",
            detail={"group_id": group.group_id, "round": round_no, "children": child_ids})

    def _tick_groups(self):
        for group in self.groups.values():
            if group.status != "RUNNING":
                continue
            children = [self.tasks[tid] for tid in group.round_children[-1]
                        if tid in self.tasks]
            self._relax_stalled_pinned_children(group, children)
            if any(r.status in ACTIVE_STATUSES for r in children):
                continue
            completed = [r for r in children if r.status == TaskStatus.COMPLETED]
            summary = (f"{len(completed)}/{len(children)} child task(s) completed on "
                       f"{', '.join(sorted({r.placement.system for r in completed if r.placement})) or '—'}")
            self.store.add_event(
                "GROUP_ROUND_COMPLETE",
                f"Group '{group.name}' round {group.current_round}: {summary}",
                detail={"group_id": group.group_id, "round": group.current_round})

            success = len(completed) >= max(1, len(children) // 2)
            if success and group.current_round < group.rounds:
                group.current_round += 1
                self.store.add_event(
                    "AGENT_CONTINUED",
                    f"Agent aggregated round results ({summary}) and is launching round "
                    f"{group.current_round}/{group.rounds} of group '{group.name}'",
                    detail={"group_id": group.group_id})
                self._launch_round(group)
            else:
                group.status = "COMPLETED" if success else "FAILED"
                group.finished_at = utcnow()
                self.store.add_event(
                    "GROUP_FINISHED",
                    f"Group '{group.name}' finished: {group.status} after "
                    f"{group.current_round} round(s) ({summary})",
                    detail={"group_id": group.group_id})
            self.store.save_group(group.group_id, group.model_dump(mode="json"))

    def _relax_stalled_pinned_children(self, group: TaskGroup, children: list[TaskRecord]):
        """Distinct-systems is a soft goal: once every sibling in the round is
        terminal, a still-queued pinned child no longer buys any fan-out — the
        agent relaxes the constraint and re-places it for earliest completion.
        (Lesson from the first live sam3 sweep, where a pinned child sat 100+
        minutes in a queue its siblings had long finished around.)"""
        pending = [r for r in children if r.status == TaskStatus.PENDING
                   and r.excluded_systems and r.spec.auto_continue]
        if not pending:
            return
        others_terminal = all(r.status not in ACTIVE_STATUSES
                              for r in children if r not in pending)
        if not others_terminal:
            return
        for record in pending:
            self._event(record, "AGENT_CONSTRAINT_RELAXED",
                        f"All round-{record.group_round} siblings of group "
                        f"'{group.name}' are terminal; distinct-systems pinning no "
                        f"longer serves the fan-out goal — relaxing it and re-placing "
                        f"for earliest completion")
            if record.job_id:
                self.executor.cancel(record.job_id)
            record.excluded_systems = []
            self._schedule_and_dispatch(record, reason="constraint relaxed after siblings finished")
            self._save(record)
