"""CloudProvider — RESERVED: on-demand cloud instance pools.

Intended mapping onto the common record shape: one ``ResourceRecord`` per
(region, instance family) with ``nodes_total`` = quota, ``nodes_idle`` =
launchable headroom, ``max_walltime_minutes=None`` and the hourly price
carried in ``tags``/detail so the objective scorer can trade cost against
queue wait. Execution = launch instance → run container → collect → terminate.

Nothing is wired: the class only registers the contract so the pool, the UI
("Add resource" sheet) and the docs already show the slot. Catalog schema::

    cloud:
      - name: aws-us-east-1
        provider: aws            # aws | gcp | azure
        region: us-east-1
        instance_types: [g5.xlarge, p4d.24xlarge]
        tags: [cloud, on-demand]
        enabled: false
"""

from __future__ import annotations

from typing import Optional

from ..execution.base import BuildRef, JobHandle
from ..schemas import Candidate, ProviderKind, ResourceRecord, TaskSpec
from .base import ProviderInfo, ProviderNotReady, ShellResult, SystemInfo


class CloudProvider:
    kind = ProviderKind.CLOUD
    label = "Cloud instances (reserved)"

    def __init__(self, pools: list[dict]):
        self.pools = list(pools)
        self.last_probe_calls = 0

    def systems(self) -> list[SystemInfo]:
        return [SystemInfo(name=p["name"], provider=self.kind, endpoint=p.get("region", ""),
                           partitions=list(p.get("instance_types", [])),
                           tags=list(p.get("tags", [])), location=p.get("region"))
                for p in self.pools]

    def info(self) -> ProviderInfo:
        return ProviderInfo(kind=self.kind, label=self.label,
                            status="disabled" if self.pools else "reserved",
                            systems=[p["name"] for p in self.pools],
                            detail="contract only — no cloud SDK wired yet")

    def probe(self) -> list[ResourceRecord]:
        return []   # reserved: no live state until an SDK adapter lands

    def submit(self, task: TaskSpec, placement: Candidate, build: Optional[BuildRef] = None) -> JobHandle:
        raise ProviderNotReady(f"cloud execution on {placement.system} is reserved")

    def status(self, job_id: str) -> tuple[str, str, None]:
        return "UNKNOWN", "cloud provider reserved", None

    def cancel(self, job_id: str) -> bool:
        return False

    def bind_job(self, job_id: str, system: str) -> None:
        pass                                    # nothing can be in flight: submit is reserved

    def shell(self, system: str, command: str, timeout_s: int = 60) -> ShellResult:
        raise ProviderNotReady(f"cloud execution on {system} is reserved")

    def workdir(self, system: str) -> str:
        return "$HOME/diamond"
