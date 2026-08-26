"""Compute provider contract — the "all compute resources in one" seam.

A *provider* owns a set of resources of one kind and knows how to (a) probe
their live state into the federation's common ``ResourceRecord`` shape and
(b) run tasks on them (submit / status / cancel). The ``ResourcePool`` stacks
providers into one virtual resource pool, so the agent, the scheduler and the
UI never see provider-specific APIs — an HPC partition reached through
Diamond / Globus Compute, a lab GPU server over SSH and a cloud instance pool
all look like rows of the same table and accept the same TaskSpec.

Active today:   GlobusHpcProvider (Diamond / Globus Compute)
Reserved:       SshServerProvider, CloudProvider — contract + catalog schema
                are in place; see their modules for what is and isn't wired.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

from ..execution.base import JobHandle
from ..schemas import Candidate, ProviderKind, ResourceRecord, TaskSpec


@dataclass
class SystemInfo:
    """Static identity of one system a provider exposes (catalog-derived)."""

    name: str
    provider: ProviderKind
    endpoint: str
    partitions: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    location: Optional[str] = None      # free text, e.g. "NCSA" / "lab 301 rack 2"
    accounts: list[str] = field(default_factory=list)


@dataclass
class ProviderInfo:
    kind: ProviderKind
    label: str
    status: str                          # active | reserved | error | disabled
    systems: list[str] = field(default_factory=list)
    detail: str = ""
    last_error: Optional[str] = None


@dataclass
class ShellResult:
    """Outcome of a bounded command on a system's login node / host."""

    returncode: int
    stdout: str
    stderr: str


@runtime_checkable
class ComputeProvider(Protocol):
    """Everything the pool needs from one resource adapter.

    ``shell``/``workdir`` back the agent-facing environment inspection
    (env probe, bounded exec, file staging, job logs); a provider that cannot
    run commands raises ProviderNotReady.
    """

    kind: ProviderKind
    label: str

    def systems(self) -> list[SystemInfo]: ...

    def info(self) -> ProviderInfo: ...

    def probe(self) -> list[ResourceRecord]:
        """Live state of every resource this provider owns. Must never raise
        for a single unreachable system — mark it offline in its record."""
        ...

    def submit(self, task: TaskSpec, placement: Candidate) -> JobHandle: ...

    def status(self, job_id: str) -> tuple[str, str]:
        """-> (PENDING|RUNNING|COMPLETED|FAILED|CANCELLED|UNKNOWN, note)"""
        ...

    def cancel(self, job_id: str) -> bool: ...

    def shell(self, system: str, command: str, timeout_s: int = 60) -> ShellResult:
        """Run one command where jobs are submitted from (login node / host)."""
        ...

    def workdir(self, system: str) -> str:
        """Where tasks run and log on that system (e.g. $HOME/diamond)."""
        ...


class ProviderNotReady(RuntimeError):
    """Raised by reserved providers when execution is requested before the
    integration is enabled/verified — surfaces as JOB_SUBMIT_FAILED with a
    clear reason instead of a silent no-op."""
