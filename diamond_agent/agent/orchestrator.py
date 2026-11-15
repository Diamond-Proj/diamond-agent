"""Task orchestrator: lifecycle and in-place continuation on the queue the agent chose.

The agent decides where a job runs, after asking which queues can run its
envelope (``POST /api/candidates``) — inside its channel: the queues, account
and caps the operator delegated to it. The core re-evaluates that queue at
submit — rule chain, sizing inside the envelope and the operator's cap, wait
estimate — refuses an infeasible one synchronously, submits, and watches the
job every tick. Continuation never leaves the chosen queue: a transient
failure is retried there, a wall-time hit is resubmitted with a longer wall
time within the caps; anything else ends FAILED with the reason and the agent
decides what happens next (``retarget`` the task to another queue, change the
envelope, or stop). Every step is on the task's timeline and in the event feed.
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from .. import budget
from ..channels import Channel, ChannelRegistry, DEFAULT_ID
from ..execution.base import BuildRef
from ..releases import ReleaseRegistry
from ..execution.base import Executor
from ..federation.collector import FederationCollector
from ..scheduler import placement
from ..schemas import Candidate, ResourceEnvelope, ResourceRecord, RuleVerdict, Sizing, TaskSpec, TaskStatus, utcnow
from ..store import Store

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = {TaskStatus.SUBMITTED, TaskStatus.PENDING, TaskStatus.RUNNING}


class Infeasible(ValueError):
    """The chosen queue cannot run this envelope right now; ``verdicts`` say why."""

    def __init__(self, message: str, verdicts: tuple[RuleVerdict, ...] = ()):
        super().__init__(message)
        self.verdicts = list(verdicts)


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
    channel: str                               # the channel it was submitted through
    spec: TaskSpec
    target_system: str
    target_partition: str
    status: TaskStatus = TaskStatus.SUBMITTED
    evaluation: Optional[Candidate] = None     # the chosen queue at submit: verdicts, sizing, wait estimate
    placement: Optional[Placement] = None      # what was actually sent to the batch system
    job_id: Optional[str] = None
    elapsed_minutes: Optional[float] = None    # sacct Elapsed — what the allocation is billed for
    release_build: Optional[dict] = None       # the release build the job ran in: version, number, dir
    attempts: int = 0
    pending_ticks: int = 0
    running_ticks: int = 0
    created_at: datetime = Field(default_factory=utcnow)
    finished_at: Optional[datetime] = None
    timeline: list[TaskEvent] = Field(default_factory=list)


class TaskManager:
    def __init__(self, store: Store, collector: FederationCollector, channels: ChannelRegistry,
                 executor: Executor, releases: ReleaseRegistry):
        self.store = store
        self.collector = collector
        self.channels = channels
        self.executor = executor
        self.releases = releases
        self.lock = threading.RLock()
        self.tasks: dict[str, TaskRecord] = {}
        # Tasks and events from before channels existed belong to the oldest
        # channel (the default one on a core that never had another).
        home = DEFAULT_ID
        for task_id, data in store.load_tasks().items():
            migrated = "channel" not in data
            data.setdefault("channel", home)
            try:
                record = TaskRecord.model_validate(data)
            except Exception as exc:
                logger.warning("dropping task %s from an older schema: %s", task_id, str(exc).splitlines()[0])
                continue
            self.tasks[task_id] = record
            if migrated:
                self._save(record)
        adopted = store.adopt_untagged_events(home)
        if adopted:
            logger.info("%d pre-channel event(s) now belong to channel %s", adopted, home)
        # Re-attach persisted in-flight jobs to their endpoints (the executor
        # keeps the job->endpoint mapping in memory only).
        for record in self.tasks.values():
            if record.status in (TaskStatus.PENDING, TaskStatus.RUNNING) and record.job_id and record.placement:
                executor.bind_job(record.job_id, record.placement.system)

    # ------------------------------------------------------------ utilities

    def _event(self, record: TaskRecord, kind: str, message: str, detail: Optional[dict] = None):
        record.timeline.append(TaskEvent(kind=kind, message=message, detail=detail or {}))
        self.store.add_event(kind, message, task_id=record.task_id, detail=detail, channel=record.channel)
        logger.info("[%s] %s: %s", record.task_id[:8], kind, message)

    def _save(self, record: TaskRecord):
        self.store.save_task(record.task_id, record.model_dump(mode="json"))

    def _fail(self, record: TaskRecord, kind: str, message: str):
        record.status = TaskStatus.FAILED
        record.finished_at = utcnow()
        self._event(record, kind, message)

    # ----------------------------------------------------------- evaluation

    def _records(self, channel: Channel) -> list[ResourceRecord]:
        snapshot = self.collector.latest
        return self.channels.scope(snapshot.records if snapshot else [], channel)

    def ledger(self, exclude: Optional[str] = None) -> budget.Ledger:
        return budget.Ledger(self.tasks.values(), exclude=exclude)

    def remaining(self, channel: Channel, system: str, partition: str, exclude: Optional[str] = None) -> Optional[float]:
        return budget.remaining(self.ledger(exclude), self.channels, channel, system, partition)

    def candidates(self, envelope: ResourceEnvelope, channel: Channel) -> list[Candidate]:
        """Every queue of the channel evaluated for this envelope, under the channel's caps and budgets."""
        policy, ledger = self.channels.policy(channel), self.ledger()
        return [placement.evaluate(r, envelope, policy, budget.remaining(ledger, self.channels, channel, r.system, r.partition))
                for r in self._records(channel)]

    def evaluate(self, envelope: ResourceEnvelope, system: str, partition: str, channel: Channel,
                 exclude_task: Optional[str] = None) -> Candidate:
        """The chosen queue, or Infeasible with the failed verdicts."""
        record = next((r for r in self._records(channel) if r.system == system and r.partition == partition), None)
        if record is None:
            raise Infeasible(f"{system}:{partition} is not available in channel '{channel.name}' "
                             "(not in its rules, blocked on the machine, or not probed yet)")
        cand = placement.evaluate(record, envelope, self.channels.policy(channel),
                                  self.remaining(channel, system, partition, exclude=exclude_task))
        if not cand.feasible:
            failed = "; ".join(f"{v.rule}: {v.detail}" for v in cand.verdicts if not v.passed)
            raise Infeasible(f"{system}:{partition} cannot run this envelope — {failed}", tuple(cand.verdicts))
        return cand

    def _build(self, channel_id: str, spec: TaskSpec, system: str) -> Optional[BuildRef]:
        """The ready build the job runs in; None for a task without a release;
        Infeasible when the release is not aligned on that machine."""
        if spec.release is None:
            return None
        release = self.releases.get(channel_id, spec.release)
        build = self.releases.aligned(channel_id, spec.release, system)
        if build is None:
            raise Infeasible(f"release {spec.release} is not aligned on {system}: align it there first "
                             f"(POST /releases/{spec.release}/align)")
        return BuildRef(dir=build.dir, version=build.version, number=build.number, env=release.env)

    # ------------------------------------------------------------ lifecycle

    def submit(self, spec: TaskSpec, target: tuple[str, str], channel: Channel) -> TaskRecord:
        system, partition = target
        with self.lock:
            cand = self.evaluate(spec.envelope, system, partition, channel)
            self._build(channel.id, spec, system)
            record = TaskRecord(task_id=uuid.uuid4().hex[:12], channel=channel.id, spec=spec,
                                target_system=system, target_partition=partition, evaluation=cand)
            self.tasks[record.task_id] = record
            self._event(record, "TASK_SUBMITTED",
                        f"Task '{spec.name}' accepted for {system}:{partition}: {self._sized(cand)}")
            self._dispatch(record, cand)
            self._save(record)
            return record

    @staticmethod
    def _sized(cand: Candidate) -> str:
        s = cand.sizing
        return (f"sized {s.nodes} node(s), {s.gpus} GPU(s), walltime {s.walltime_minutes}m "
                f"({budget.node_hours(s.nodes, s.walltime_minutes):.2f} node-h); est. wait ~{cand.est_wait_minutes:.0f}m")

    def _policy_gate(self, record: TaskRecord, cand: Candidate) -> Optional[str]:
        """Last check before anything reaches a batch system: the queue must
        still be in the channel and the sizing within the caps, which may have
        changed since the evaluation (a retry re-uses the sizing it was
        accepted with)."""
        channel = self.channels.get(record.channel)
        allowed = self.channels.allowed(channel)
        key = f"{cand.system}:{cand.partition}"
        if key not in allowed:
            return f"{key} is no longer available in channel '{channel.name}'"
        if cand.sizing.nodes > allowed[key]:
            return f"sizing asks for {cand.sizing.nodes} node(s) but the cap on {key} is now {allowed[key]}"
        left = self.remaining(channel, cand.system, cand.partition, exclude=record.task_id)
        cost = budget.node_hours(cand.sizing.nodes, cand.sizing.walltime_minutes)
        if left is not None and cost > left + 1e-9:
            return f"{cost:.2f} node-h would exceed the budget left on {key} ({left:.2f} node-h)"
        return None

    def _dispatch(self, record: TaskRecord, cand: Candidate):
        denied = self._policy_gate(record, cand)
        if denied:
            self._fail(record, "POLICY_DENIED", f"Not submitted to {cand.system}:{cand.partition}: {denied}")
            return
        try:
            build = self._build(record.channel, record.spec, cand.system)
        except Infeasible as exc:                      # the build went stale between evaluation and dispatch
            self._fail(record, "POLICY_DENIED", f"Not submitted to {cand.system}:{cand.partition}: {exc}")
            return
        try:
            handle = self.executor.submit(record.spec, cand, build)
        except Exception as exc:
            self._fail(record, "JOB_SUBMIT_FAILED", f"Submission to {cand.system}:{cand.partition} failed: {exc}")
            return
        record.placement = Placement(system=cand.system, partition=cand.partition, endpoint=cand.endpoint,
                                     account=cand.account, sizing=cand.sizing)
        record.release_build = {"version": build.version, "number": build.number, "dir": build.dir} if build else None
        record.job_id = handle.job_id
        record.status = TaskStatus.PENDING
        record.pending_ticks = 0
        s = cand.sizing
        self._event(record, "JOB_SUBMITTED",
                    f"Job {handle.job_id} submitted to {cand.system}:{cand.partition} "
                    f"({s.nodes} node(s), {s.gpus} GPU(s), walltime {s.walltime_minutes}m)",
                    {"job_id": handle.job_id, "sizing": s.model_dump()})

    def cancel(self, record: TaskRecord) -> TaskRecord:
        with self.lock:
            if record.job_id and record.status in ACTIVE_STATUSES:
                self.executor.cancel(record.job_id)
            if record.status != TaskStatus.RUNNING:
                record.elapsed_minutes = 0.0           # never ran: nothing billed
            record.status = TaskStatus.CANCELLED
            record.finished_at = utcnow()
            self._event(record, "TASK_CANCELLED", "Cancelled by user")
            self._save(record)
            return record

    def retarget(self, record: TaskRecord, target: tuple[str, str], channel: Channel) -> TaskRecord:
        """Move a task to another queue the agent chose (its current job, if
        any, is cancelled). An infeasible target leaves the task untouched."""
        system, partition = target
        with self.lock:
            cand = self.evaluate(record.spec.envelope, system, partition, channel, exclude_task=record.task_id)
            self._build(channel.id, record.spec, system)
            if record.job_id and record.status in ACTIVE_STATUSES:
                self.executor.cancel(record.job_id)
            record.target_system, record.target_partition = system, partition
            record.evaluation, record.job_id, record.finished_at, record.elapsed_minutes = cand, None, None, None
            self._event(record, "TASK_RETARGETED", f"Retargeted to {system}:{partition}: {self._sized(cand)}")
            self._dispatch(record, cand)
            self._save(record)
            return record

    # ------------------------------------------------------------- monitor

    def tick(self):
        """Advance every active task from its job's state. Called after every federation refresh."""
        with self.lock:
            for record in self.tasks.values():
                if record.status not in (TaskStatus.PENDING, TaskStatus.RUNNING) or record.job_id is None:
                    continue
                try:
                    self._tick_task(record)
                except Exception:
                    logger.exception("monitor tick failed for %s", record.task_id)
                self._save(record)

    def _tick_task(self, record: TaskRecord):
        state, note, elapsed = self.executor.status(record.job_id)
        if elapsed is not None:
            record.elapsed_minutes = elapsed
        where = f"{record.placement.system}:{record.placement.partition}"
        if state == "PENDING":
            record.pending_ticks += 1
        elif state == "RUNNING":
            if record.status != TaskStatus.RUNNING:
                record.status = TaskStatus.RUNNING
                self._event(record, "JOB_STARTED",
                            f"Job {record.job_id} started on {where} after {record.pending_ticks} tick(s) in queue")
            else:
                record.running_ticks += 1
        elif state == "COMPLETED":
            record.status = TaskStatus.COMPLETED
            record.finished_at = utcnow()
            self._event(record, "JOB_COMPLETED", f"Job {record.job_id} completed on {where}")
        elif state == "FAILED":
            self._handle_failure(record, note)
        elif state == "CANCELLED":
            record.status = TaskStatus.CANCELLED
            record.finished_at = utcnow()
            self._event(record, "JOB_CANCELLED", f"Job {record.job_id} was cancelled ({note})")

    # ------------------------------------------------- in-place continuation

    def _handle_failure(self, record: TaskRecord, note: str):
        record.attempts += 1
        where = f"{record.placement.system}:{record.placement.partition}"
        self._event(record, "JOB_FAILED",
                    f"Job {record.job_id} failed on {where}{f' ({note})' if note else ''} — attempt {record.attempts}")
        if not record.spec.auto_continue or record.attempts > record.spec.max_retries:
            self._fail(record, "AGENT_GAVE_UP",
                       "Retry budget exhausted on the chosen queue" if record.spec.auto_continue
                       else "Auto-continuation disabled; task marked failed")
            return
        if "timeout" in (note or "").lower() or "walltime" in (note or "").lower():
            self._resize_in_place(record)
            return
        self._event(record, "AGENT_RETRY",
                    f"Retrying in place on {where} (attempt {record.attempts} of {record.spec.max_retries})")
        self._dispatch(record, record.evaluation)

    def _resize_in_place(self, record: TaskRecord):
        """Wall-time hit: resubmit on the same queue with the wall time doubled,
        within the envelope's maximum and the queue's cap; at the cap, stop."""
        cand = record.evaluation
        current = cand.sizing.walltime_minutes
        caps = [current * 2, record.spec.envelope.walltime.max_minutes]
        if cand.record.max_walltime_minutes is not None:
            caps.append(cand.record.max_walltime_minutes)
        new = min(caps)
        if new <= current:
            self._fail(record, "AGENT_GAVE_UP",
                       f"Job hit its wall time and {current}m is already the cap on this queue; "
                       "resubmit with a different envelope or queue")
            return
        cand.sizing.walltime_minutes = new
        self._event(record, "AGENT_RESIZED",
                    f"Job hit its wall time; resubmitting in place with walltime {new}m")
        self._dispatch(record, cand)
