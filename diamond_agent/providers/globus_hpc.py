"""GlobusHpcProvider — batch HPC systems reached through Diamond / Globus Compute.

Thin adapter over the existing live source (``GlobusFederationSource``,
minute-level Slurm probes on each endpoint) and executor
(``GlobusExecutor``, rendered sbatch scripts as ShellFunctions). It owns
every ``systems:`` entry of the catalog whose ``provider`` is missing or
``globus_hpc`` — the production path proven on Delta / DeltaAI / Anvil / Vista.
"""

from __future__ import annotations

import logging
from typing import Optional

from ..execution.base import JobHandle
from ..schemas import Candidate, ProviderKind, ResourceRecord, TaskSpec
from .base import ProviderInfo, ProviderNotReady, ShellResult, SystemInfo

logger = logging.getLogger(__name__)


def hpc_systems(catalog: dict) -> list[dict]:
    return [s for s in catalog["systems"]
            if s.get("provider", "globus_hpc") == "globus_hpc"]


def system_infos(catalog: dict, kind: ProviderKind) -> list[SystemInfo]:
    return [SystemInfo(
        name=s["name"], provider=kind, endpoint=str(s.get("endpoint", "")),
        partitions=[p["name"] for p in s["partitions"]],
        tags=list(s.get("tags", [])), location=s.get("location"),
        accounts=list(s.get("accounts", [])),
    ) for s in hpc_systems(catalog)]


class GlobusHpcProvider:
    kind = ProviderKind.GLOBUS_HPC
    label = "HPC via Diamond / Globus Compute"

    def __init__(self, catalog: dict):
        from ..execution.globus import GlobusExecutor
        from ..federation.globus_probe import GlobusFederationSource

        self.catalog = {**catalog, "systems": hpc_systems(catalog)}
        self._source = GlobusFederationSource(self.catalog)
        self._executor = GlobusExecutor(self.catalog)
        self._tags = {s["name"]: list(s.get("tags", [])) for s in self.catalog["systems"]}
        self._warned = False

    @property
    def last_probe_calls(self) -> int:
        return self._source.last_probe_calls

    def systems(self) -> list[SystemInfo]:
        return system_infos(self.catalog, self.kind)

    def info(self) -> ProviderInfo:
        from .. import auth

        if not auth.credentials_available():
            return ProviderInfo(kind=self.kind, label=self.label, status="needs-login",
                                systems=[s["name"] for s in self.catalog["systems"]],
                                detail=("no Globus credentials — sign in from the web UI or run "
                                        "`python -m diamond_agent.auth login`"))
        return ProviderInfo(kind=self.kind, label=self.label, status="active",
                            systems=[s["name"] for s in self.catalog["systems"]],
                            detail=f"Slurm probes + sbatch through Globus Compute endpoints ({auth.auth_mode()})")

    def probe(self) -> list[ResourceRecord]:
        from .. import auth

        if not auth.credentials_available():
            if not self._warned:
                logger.warning("globus_hpc: waiting for credentials (web sign-in or CLI login)")
                self._warned = True
            return []
        self._warned = False
        records = self._source.probe()
        for r in records:
            r.tags = self._tags.get(r.system, [])
        return records

    def submit(self, task: TaskSpec, placement: Candidate) -> JobHandle:
        return self._executor.submit(task, placement)

    def status(self, job_id: str) -> tuple[str, str, Optional[float]]:
        return self._executor.status(job_id)

    def cancel(self, job_id: str) -> bool:
        return self._executor.cancel(job_id)

    def bind_job(self, job_id: str, system: str) -> None:
        self._executor.bind_job(job_id, system)

    def shell(self, system: str, command: str, timeout_s: int = 60) -> ShellResult:
        from .. import auth

        if not auth.credentials_available():
            raise ProviderNotReady("no Globus credentials — sign in first")
        r = self._executor.run_shell(system, command, timeout_s)
        # The endpoint prefixes its own sandboxing notice to every ShellFunction
        # result; it is about the endpoint, not the command — drop it.
        noise = "WARNING: Task sandboxing will not work"
        out = "\n".join(l for l in r.stdout.splitlines() if not l.startswith(noise)).lstrip("\n")
        return ShellResult(returncode=r.returncode, stdout=out, stderr=r.stderr)

    def workdir(self, system: str) -> str:
        return self._executor.workdir_for(system)
