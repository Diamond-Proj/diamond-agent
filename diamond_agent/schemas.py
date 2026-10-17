"""Core data contracts of Diamond Agent.

Three first-class objects:

- ResourceRecord / ResourceSnapshot : the federated resource abstraction.
  One record per (endpoint, partition) refreshed every collector tick,
  merged into a snapshot of the whole virtual resource pool.

- ResourceEnvelope : what a task needs, expressed as [min viable, max useful]
  ranges plus compatibility constraints and a placement objective.
  The meta-scheduler picks a concrete shape inside the envelope.

- PlacementDecision : the fully-explained output of the meta-scheduler —
  every candidate with its rule-by-rule verdicts, sizing, score and
  AI rationale, so the UI can show *why* a placement was chosen.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(r"^(?:(\d+)-)?(\d{1,3}):(\d{2})(?::(\d{2}))?$|^(\d+)([smhd])$|^(\d+)$")


def parse_duration_minutes(value: str | int | float | None) -> Optional[int]:
    """Parse '30m', '4h', '02:00:00', '1-12:00:00', 90 -> minutes."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower()
    if not text or text in ("infinite", "unlimited", "n/a"):
        return None
    m = _DURATION_RE.match(text)
    if not m:
        raise ValueError(f"unrecognized duration: {value!r}")
    if m.group(5):  # "30m" style
        n, unit = int(m.group(5)), m.group(6)
        if unit == "s":
            return max(1, n // 60)
        return n * {"m": 1, "h": 60, "d": 1440}[unit]
    if m.group(7):  # bare number == minutes
        return int(m.group(7))
    days = int(m.group(1) or 0)
    a, b, c = int(m.group(2)), int(m.group(3)), m.group(4)
    if c is not None:  # H:MM:SS
        return days * 1440 + a * 60 + b + (1 if int(c) else 0)
    return days * 1440 + a * 60 + b  # H:MM


def parse_mem_gb(value: str | int | float | None) -> Optional[float]:
    """Parse '64GB', '512G', '40 GiB', 64 -> GB (float)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().upper().replace(" ", "")
    m = re.match(r"^([\d.]+)(GB|GIB|G|TB|TIB|T|MB|MIB|M)?$", text)
    if not m:
        raise ValueError(f"unrecognized memory size: {value!r}")
    n = float(m.group(1))
    unit = m.group(2) or "GB"
    if unit.startswith("T"):
        return n * 1024
    if unit.startswith("M"):
        return n / 1024
    return n


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 1. Federated resource abstraction
# ---------------------------------------------------------------------------

class EndpointHealth(str, Enum):
    ONLINE = "online"
    DEGRADED = "degraded"
    OFFLINE = "offline"


class ProviderKind(str, Enum):
    """Which adapter owns a resource — the 'all compute in one' seam."""

    GLOBUS_HPC = "globus_hpc"   # batch system reached through Diamond / Globus Compute
    SSH = "ssh"                 # single Linux server over OpenSSH (reserved)
    CLOUD = "cloud"             # on-demand cloud instances (reserved)


class ResourceRecord(BaseModel):
    """Minute-level state of one (endpoint, partition) in the federation.

    One record per schedulable unit: an HPC (system, partition); for the
    reserved SSH/cloud providers a whole server / instance pool maps to one
    record with partition="local" / the instance family.
    """

    timestamp: datetime
    endpoint: str                      # Globus Compute endpoint UUID (or sim id / host)
    system: str                        # e.g. "Delta@NCSA"
    partition: str
    provider: ProviderKind = ProviderKind.GLOBUS_HPC
    tags: list[str] = Field(default_factory=list)   # free-form labels from the catalog
    account: str                       # allocation/account usable on this partition
    node_arch: str                     # x86_64 | aarch64
    gpu_type: Optional[str] = None     # A100 / H100 / GH200 / ... ; None = CPU-only
    gpus_per_node: int = 0
    gpu_mem_gb: float = 0.0
    cpus_per_node: int = 0
    mem_per_node_gb: float = 0.0
    nodes_idle: int = 0
    nodes_mixed: int = 0        # partially allocated — has free capacity (shared nodes)
    nodes_allocated: int = 0
    nodes_down: int = 0
    gpus_free: Optional[int] = None   # GRES-level free GPUs on usable nodes (live probe)
    cpus_free: Optional[int] = None   # partition-level idle CPUs (live probe)
    running_jobs: int = 0
    pending_jobs: int = 0
    user_running_jobs: int = 0
    user_pending_jobs: int = 0
    max_walltime_minutes: Optional[int] = None
    endpoint_health: EndpointHealth = EndpointHealth.ONLINE
    container_runtime: Optional[str] = None   # apptainer | singularity | shifter | podman-hpc | None

    @property
    def nodes_total(self) -> int:
        return self.nodes_idle + self.nodes_mixed + self.nodes_allocated + self.nodes_down

    def utilization(self) -> float:
        """Busy fraction of the resource that actually gates placement:
        free GPUs (GRES) on GPU partitions, free cores on CPU partitions,
        node states as the fallback when the live probe has neither."""
        usable = max(self.nodes_total - self.nodes_down, 0)
        if self.gpu_type and self.gpus_free is not None:
            cap = max(usable * self.gpus_per_node, self.gpus_free)
            return (cap - self.gpus_free) / cap if cap else 0.0
        if not self.gpu_type and self.cpus_free is not None:
            cap = max(usable * self.cpus_per_node, self.cpus_free)
            return (cap - self.cpus_free) / cap if cap else 0.0
        total = self.nodes_total
        return (self.nodes_allocated + self.nodes_mixed) / total if total else 0.0

    def free_units(self) -> tuple[int, str]:
        """(count, unit) of the gating free resource — 'GPU' or 'cores'."""
        if self.gpu_type:
            n = self.gpus_free if self.gpus_free is not None else self.nodes_idle * self.gpus_per_node
            return n, "GPU"
        n = self.cpus_free if self.cpus_free is not None else self.nodes_idle * self.cpus_per_node
        return n, "cores"


class ResourceSnapshot(BaseModel):
    """The unified view of the virtual resource pool at one instant."""

    taken_at: datetime = Field(default_factory=utcnow)
    records: list[ResourceRecord] = Field(default_factory=list)

    def aggregates(self) -> dict[str, Any]:
        gpu_idle = sum(
            (r.gpus_free if r.gpus_free is not None else r.nodes_idle * r.gpus_per_node)
            for r in self.records)
        gpu_total = sum(r.nodes_total * r.gpus_per_node for r in self.records)
        cpu_idle = sum(
            (r.cpus_free if r.cpus_free is not None else r.nodes_idle * r.cpus_per_node)
            for r in self.records)
        systems = {r.system for r in self.records}
        online = {r.system for r in self.records if r.endpoint_health == EndpointHealth.ONLINE}
        return {
            "systems": len(systems),
            "systems_online": len(online),
            "partitions": len({(r.system, r.partition) for r in self.records}),
            "gpus_idle": gpu_idle,
            "gpus_total": gpu_total,
            "cpus_idle": cpu_idle,
            "nodes_idle": sum(r.nodes_idle for r in self.records),
            "nodes_total": sum(r.nodes_total for r in self.records),
            "pending_jobs": sum(r.pending_jobs for r in self.records),
            "running_jobs": sum(r.running_jobs for r in self.records),
        }


# ---------------------------------------------------------------------------
# 2. Resource envelope
# ---------------------------------------------------------------------------

class GpuEnvelope(BaseModel):
    min_count: int = 0                 # 0 => GPU not required
    max_count: int = 0
    min_memory: Optional[str] = None   # per-GPU memory, e.g. "40GB"

    @property
    def min_memory_gb(self) -> float:
        return parse_mem_gb(self.min_memory) or 0.0


class CpuEnvelope(BaseModel):
    min_count: int = 1
    max_count: int = 1


class MemoryEnvelope(BaseModel):
    min: Optional[str] = None
    max: Optional[str] = None

    @property
    def min_gb(self) -> float:
        return parse_mem_gb(self.min) or 0.0


class WalltimeEnvelope(BaseModel):
    min: str = "10m"                   # minimum useful runtime
    max: str = "1h"                    # cap the scheduler may request

    @property
    def min_minutes(self) -> int:
        return parse_duration_minutes(self.min) or 10

    @property
    def max_minutes(self) -> int:
        return parse_duration_minutes(self.max) or 60


class Compatibility(BaseModel):
    cpu_arch: list[str] = Field(default_factory=lambda: ["x86_64", "aarch64"])
    accelerators: list[str] = Field(default_factory=list)   # empty = any
    container_required: bool = False


class Objective(str, Enum):
    EARLIEST_COMPLETION = "earliest_completion"
    MAX_THROUGHPUT = "max_throughput"
    MIN_QUEUE_IMPACT = "min_queue_impact"


class ResourceEnvelope(BaseModel):
    """[min viable, max useful] resource ranges for a task.

    min = the floor below which the task cannot run;
    max = the ceiling above which more resources are wasted;
    the meta-scheduler picks the concrete shape inside the interval.
    """

    gpu: GpuEnvelope = Field(default_factory=GpuEnvelope)
    cpu: CpuEnvelope = Field(default_factory=CpuEnvelope)
    memory: MemoryEnvelope = Field(default_factory=MemoryEnvelope)
    walltime: WalltimeEnvelope = Field(default_factory=WalltimeEnvelope)
    compatibility: Compatibility = Field(default_factory=Compatibility)
    objective: Objective = Objective.EARLIEST_COMPLETION

    @field_validator("objective", mode="before")
    @classmethod
    def _coerce_objective(cls, v: Any) -> Any:
        if isinstance(v, list) and v:
            return v[0]
        return v


# ---------------------------------------------------------------------------
# 3. Placement decision (explainable scheduler output)
# ---------------------------------------------------------------------------

class RuleVerdict(BaseModel):
    rule: str
    passed: bool
    detail: str


class Sizing(BaseModel):
    """Concrete shape chosen inside the envelope for one candidate."""

    nodes: int = 1
    gpus: int = 0
    cpus: int = 1
    walltime_minutes: int = 60


class Candidate(BaseModel):
    endpoint: str
    system: str
    partition: str
    account: str
    record: ResourceRecord
    verdicts: list[RuleVerdict] = Field(default_factory=list)
    feasible: bool = False
    sizing: Optional[Sizing] = None
    est_wait_minutes: Optional[float] = None
    score: Optional[float] = None            # heuristic score (lower = better)
    ai_rank: Optional[int] = None            # 1 = best, set by AI ranker
    rationale: Optional[str] = None


class PlacementDecision(BaseModel):
    decided_at: datetime = Field(default_factory=utcnow)
    objective: Objective
    mode: str = "rules"                      # "rules" | "ai+rules"
    candidates: list[Candidate] = Field(default_factory=list)
    chosen: Optional[Candidate] = None
    summary: str = ""
    decision_ms: Optional[float] = None      # wall-clock latency of decide()


# ---------------------------------------------------------------------------
# Task lifecycle
# ---------------------------------------------------------------------------

class TaskStatus(str, Enum):
    SUBMITTED = "SUBMITTED"      # accepted by agent, scheduling in progress
    SCHEDULED = "SCHEDULED"      # placement chosen, handed to executor
    PENDING = "PENDING"          # waiting in a batch queue
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNSCHEDULABLE = "UNSCHEDULABLE"


class TaskSpec(BaseModel):
    name: str
    description: str = ""
    command: str = "echo hello from diamond agent"
    container: Optional[str] = None
    envelope: ResourceEnvelope = Field(default_factory=ResourceEnvelope)
    max_retries: int = 2
    auto_continue: bool = True    # allow event-triggered agent continuation
