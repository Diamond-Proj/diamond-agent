"""Compute providers: one contract, many resource kinds (see base.py)."""

from __future__ import annotations

from .base import ComputeProvider, ProviderInfo, ProviderNotReady, SystemInfo
from .pool import ResourcePool


def build_pool(catalog: dict) -> ResourcePool:
    """Assemble the pool: HPC via Diamond / Globus Compute, plus the reserved
    SSH-server and cloud providers behind the same contract."""
    from .cloud import CloudProvider
    from .globus_hpc import GlobusHpcProvider
    from .ssh import SshServerProvider

    return ResourcePool([GlobusHpcProvider(catalog),
                         SshServerProvider(catalog.get("ssh_servers", [])),
                         CloudProvider(catalog.get("cloud", []))])


__all__ = ["ComputeProvider", "ProviderInfo", "ProviderNotReady", "SystemInfo",
           "ResourcePool", "build_pool"]
