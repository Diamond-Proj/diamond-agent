"""ResourcePool: one virtual resource pool over many compute providers.

Implements both halves of what the agent consumes — ``FederationSource``
(``probe``) for the collector and ``Executor`` (``submit``/``status``/
``cancel``) for the orchestrator — by routing each call to the provider that
owns the system. Adding a resource kind = adding a provider; nothing above
this layer changes.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from ..execution.base import JobHandle
from ..schemas import Candidate, ProviderKind, ResourceRecord, TaskSpec
from .base import ComputeProvider, ProviderInfo, ProviderNotReady, ShellResult, SystemInfo

logger = logging.getLogger(__name__)


class ResourcePool:
    def __init__(self, providers: list[ComputeProvider]):
        self.providers = providers
        self._lock = threading.Lock()
        self._jobs: dict[str, ComputeProvider] = {}       # job id -> owning provider
        self._errors: dict[ProviderKind, Optional[str]] = {p.kind: None for p in providers}
        self.last_probe_calls = 0
        self._system_owner: dict[str, ComputeProvider] = {}
        for p in providers:
            for s in p.systems():
                self._system_owner[s.name] = p

    # ------------------------------------------------------------ identity

    def systems(self) -> list[SystemInfo]:
        return [s for p in self.providers for s in p.systems()]

    def provider_for(self, system: str) -> ComputeProvider:
        try:
            return self._system_owner[system]
        except KeyError:
            raise KeyError(f"no provider owns system {system!r}")

    def providers_info(self) -> list[ProviderInfo]:
        out = []
        for p in self.providers:
            info = p.info()
            err = self._errors.get(p.kind)
            if err and info.status == "active":
                info.status = "error"
                info.last_error = err
            out.append(info)
        return out

    # ------------------------------------------------------- federation side

    def probe(self) -> list[ResourceRecord]:
        records: list[ResourceRecord] = []
        calls = 0
        for p in self.providers:
            try:
                got = p.probe()
                for r in got:
                    r.provider = p.kind
                records.extend(got)
                calls += getattr(p, "last_probe_calls", 0)
                self._errors[p.kind] = None
            except Exception as exc:  # one broken provider must not blank the pool
                logger.exception("provider %s probe failed", p.kind.value)
                self._errors[p.kind] = str(exc)[:300]
        self.last_probe_calls = calls
        return records

    # -------------------------------------------------------- executor side

    def submit(self, task: TaskSpec, placement: Candidate) -> JobHandle:
        provider = self.provider_for(placement.system)
        handle = provider.submit(task, placement)
        with self._lock:
            self._jobs[handle.job_id] = provider
        return handle

    def status(self, job_id: str) -> tuple[str, str]:
        provider = self._jobs.get(job_id)
        if provider is None:
            return "UNKNOWN", "no provider recorded for job"
        return provider.status(job_id)

    def cancel(self, job_id: str) -> bool:
        provider = self._jobs.get(job_id)
        if provider is None:
            return False
        return provider.cancel(job_id)

    # ---------------------------------------------------- environment access

    def shell(self, system: str, command: str, timeout_s: int = 60) -> ShellResult:
        provider = self.provider_for(system)
        fn = getattr(provider, "shell", None)
        if fn is None:
            raise ProviderNotReady(f"{provider.kind.value} cannot run commands on {system}")
        return fn(system, command, timeout_s)

    def workdir(self, system: str) -> str:
        provider = self.provider_for(system)
        fn = getattr(provider, "workdir", None)
        return fn(system) if fn else "$HOME/diamond"

    def bind_job(self, job_id: str, system: str) -> None:
        """Re-attach a persisted in-flight job after a restart."""
        try:
            provider = self.provider_for(system)
        except KeyError:
            return
        with self._lock:
            self._jobs[job_id] = provider
        bind = getattr(provider, "bind_job", None)
        if callable(bind):
            bind(job_id, system)
