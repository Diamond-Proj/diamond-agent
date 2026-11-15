"""diamond-agent-core: the programmatic facade.

Everything that talks to HPC systems, tracks tasks and monitors the
federation lives behind this one class. The HTTP API (``api.py``), the CLI
(``core/cli.py``), the MCP server (``core/mcp_server.py``) and any in-process
agent all call the same methods, so the frontend is just one more client.

Agents act through a **channel** (``channels.py``): the queues, account and
caps the operator delegated to them. Methods that take ``channel`` see only
that channel's world; the operator (web UI, no channel) sees everything.

    core = DiamondAgentCore.build(catalog)
    core.tick()                                                  # one agent cycle
    core.candidates(envelope, channel)                           # which of the channel's queues can run it
    core.submit(name="demo", command="python train.py", envelope=ResourceEnvelope(...),
                target=("DeltaAI@NCSA", "ghx4"), channel=channel)  # the queue the agent chose
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Optional

from .. import budget
from ..agent.orchestrator import Infeasible, TaskManager, TaskRecord
from ..channels import Channel, ChannelRegistry, ChannelRules
from ..federation.collector import FederationCollector
from ..policy import (PolicyDenied, build_policy, check_shell_command, check_stage_path,
                      effective_node_cap, is_blocked, partition_key)
from ..providers import ResourcePool, build_pool
from ..releases import Aligner, BlobStore, Build, Release, ReleaseRegistry, Source
from ..scheduler import placement
from ..schemas import Candidate, EndpointHealth, ResourceEnvelope, ResourceRecord, Sizing, TaskSpec, TaskStatus, utcnow
from ..store import Store

logger = logging.getLogger(__name__)

TERMINAL = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
Target = tuple[str, str]        # (system, partition): the queue the agent chose
POLICY_SETTING = "policy_overrides"     # settings-table key for the operator's machine-rule overrides


def summarize_task(record: TaskRecord) -> dict:
    """The compact task row shared by every interface."""
    placement_ = record.placement
    return {
        "task_id": record.task_id,
        "channel": record.channel,
        "name": record.spec.name,
        "description": record.spec.description,
        "status": record.status.value,
        "system": placement_.system if placement_ else None,
        "partition": placement_.partition if placement_ else None,
        "job_id": record.job_id,
        "sizing": placement_.sizing.model_dump() if placement_ and placement_.sizing else None,
        "attempts": record.attempts,
        "pending_ticks": record.pending_ticks,
        "created_at": record.created_at,
        "finished_at": record.finished_at,
        "target_system": record.target_system,
        "target_partition": record.target_partition,
        "release": record.spec.release,
    }


def _flatten(policy: dict) -> dict:
    """One entry per setting — for naming what an edit changed."""
    return {"max nodes/job": policy["max_nodes_per_job"],
            "max pending/system": policy["max_user_pending_per_system"],
            **{f"{k} max nodes": v for k, v in policy["partition_limits"].items()},
            **{f"{k} blocked": True for k in policy["blocked_partitions"]},
            **{f"{k} node-hours": v for k, v in policy["node_hours"].items()}}


class DiamondAgentCore:
    def __init__(self, manager: TaskManager, pool: ResourcePool, catalog: dict):
        self.manager = manager
        self.pool = pool
        self.catalog = catalog
        self.collector: FederationCollector = manager.collector
        self.store: Store = manager.store
        self.channels: ChannelRegistry = manager.channels
        self.releases: ReleaseRegistry = manager.releases
        self.aligner = Aligner(pool, manager.releases.blobs)
        # The machine rules: one dict, read live by every channel's effective
        # policy and by the dispatch gate, so an operator edit applies at once.
        self.policy: dict = manager.channels.machine_policy

    # ------------------------------------------------------------ assembly

    @classmethod
    def build(cls, catalog: dict, db_path: str = "diamond_agent.db",
              allow_unauthenticated: bool = False) -> "DiamondAgentCore":
        """allow_unauthenticated: start without credentials so the frontend
        can sign in through Globus; the HPC provider reports ``needs-login``
        and probes nothing until then. Headless use keeps the strict rule."""
        from ..auth import check_headless_ready

        ready, detail = check_headless_ready()
        if not ready and not allow_unauthenticated:
            raise RuntimeError(f"cannot start: {detail}")
        if not ready:
            logger.warning("starting WITHOUT credentials — %s", detail)
        else:
            logger.info("auth: %s", detail)
        store = Store(db_path)
        overrides = store.load_setting(POLICY_SETTING)
        if overrides:
            logger.info("policy: operator overrides loaded (%s)", ", ".join(sorted(overrides)))
        pool = build_pool(catalog)
        for info in pool.providers_info():
            logger.info("provider %-10s %-8s %s", info.kind.value, info.status,
                        ", ".join(info.systems) or "-")
        collector = FederationCollector(pool)
        channels = ChannelRegistry(store, catalog, build_policy(catalog, overrides))
        channels.ensure_default()
        releases = ReleaseRegistry(store, BlobStore(Path(db_path).with_suffix(".blobs")))
        manager = TaskManager(store, collector, channels, pool, releases)
        return cls(manager, pool, catalog)

    def tick(self) -> None:
        """One agent cycle: refresh the federation snapshot, then run the monitor."""
        try:
            self.collector.refresh()
        except Exception:
            logger.exception("federation refresh failed; monitor runs on the last snapshot")
        self.manager.tick()

    # ------------------------------------------------------------ scoping

    def _records(self, channel: Optional[Channel]) -> list[ResourceRecord]:
        snap = self.collector.latest
        records = snap.records if snap else []
        return self.channels.scope(records, channel) if channel else records

    def _policy(self, channel: Optional[Channel]) -> dict:
        return self.channels.policy(channel) if channel else self.policy

    def _systems(self, channel: Optional[Channel]):
        infos = self.pool.systems()
        if channel is None:
            return infos
        machines = self.channels.machines(channel)
        return [s for s in infos if s.name in machines]

    def _in_channel(self, system: str, channel: Channel) -> None:
        if system not in self.channels.machines(channel):
            raise PolicyDenied(f"{system} is not part of channel '{channel.name}'")

    @staticmethod
    def _brief(channel: Optional[Channel]) -> Optional[dict]:
        return {"id": channel.id, "name": channel.name, "description": channel.description} if channel else None

    # ------------------------------------------------------------ status

    def info(self, channel: Optional[Channel] = None) -> dict:
        from .. import auth

        return {
            "auth": auth.auth_status(required=True),
            "channel": self._brief(channel),
            "systems": [{"name": s.name, "endpoint": s.endpoint, "partitions": s.partitions}
                        for s in self._systems(channel)],
            "policy": self._policy(channel),
            "providers": self.providers(),
        }

    def providers(self) -> list[dict]:
        return [{**p.__dict__, "kind": p.kind.value} for p in self.pool.providers_info()]

    # ------------------------------------------------------------- policy

    def _partition_keys(self) -> set[str]:
        return {partition_key(s.name, p) for s in self.pool.systems() for p in s.partitions}

    def policy_view(self, channel: Optional[Channel] = None) -> dict:
        """The limits in force plus one resolved row per queue. Without a
        channel: the machine rules (edited only by the operator, PUT
        /ui/policy). With one: that channel's effective rules."""
        policy = self._policy(channel)
        snap = self.collector.latest
        totals = {(r.system, r.partition): r.nodes_total for r in (snap.records if snap else [])}
        allowed = self.channels.allowed(channel) if channel else None
        ledger = self.manager.ledger()
        rows = []
        for s in self._systems(channel):
            for p in s.partitions:
                key = partition_key(s.name, p)
                if allowed is not None and key not in allowed:
                    continue
                rows.append({"system": s.name, "partition": p, "key": key,
                             "max_nodes_per_job": effective_node_cap(policy, s.name, p),
                             "override": policy["partition_limits"].get(key),
                             "blocked": is_blocked(policy, s.name, p),
                             "account": self.channels.account(channel, s.name, p) if channel else None,
                             "node_hours_left": self.manager.remaining(channel, s.name, p) if channel else None,
                             "nodes_total": totals.get((s.name, p))})
        view = {**policy, "channel": self._brief(channel), "partitions": rows}
        if channel:
            view["budget"] = self._budget_summary(channel, ledger)
        else:
            view["node_hours_used"] = {s.name: round(ledger.used(None, s.name), 3) for s in self.pool.systems()}
        return view

    def _budget_summary(self, channel: Channel, ledger: budget.Ledger) -> dict:
        """Caps and what has been spent, per machine and per queue of the channel."""
        machines, queues = {}, {}
        for system in sorted(self.channels.machines(channel)):
            srule = channel.rules.systems[system]
            left = budget.remaining(ledger, self.channels, channel, system, None)    # machine-level limits only
            machines[system] = {"cap": srule.node_hours,
                                "machine_budget": self.policy["node_hours"].get(system),
                                "used": round(ledger.used(channel.id, system), 3),
                                "left": None if left is None else round(left, 3)}
        for key in self.channels.allowed(channel):
            system, part = key.split(":", 1)
            prule = (channel.rules.systems[system].partitions or {}).get(part)
            left = budget.remaining(ledger, self.channels, channel, system, part)
            queues[key] = {"cap": prule.node_hours if prule else None,
                           "used": round(ledger.used(channel.id, system, part), 3),
                           "left": None if left is None else round(left, 3)}
        return {"machines": machines, "queues": queues}

    def update_policy(self, patch: dict) -> dict:
        """Operator edit of the machine rules (shape validated at the HTTP
        boundary). A key present in the patch replaces the stored override;
        ``None`` returns it to the catalog value; ``partition_limits`` is the
        whole per-queue map. Persisted, applied in place to the shared policy
        dict (every channel's effective rules follow), recorded as POLICY_CHANGED."""
        if "partition_limits" in patch:
            patch["partition_limits"] = {k: v for k, v in patch["partition_limits"].items() if v is not None}
        if "node_hours" in patch:
            patch["node_hours"] = {k: v for k, v in patch["node_hours"].items() if v is not None}
        unknown = (set(patch.get("partition_limits") or [])
                   | set(patch.get("blocked_partitions") or [])) - self._partition_keys()
        if unknown:
            raise ValueError(f"unknown partition(s): {', '.join(sorted(unknown))}")
        unknown_machines = set(patch.get("node_hours") or []) - {s.name for s in self.pool.systems()}
        if unknown_machines:
            raise ValueError(f"unknown machine(s): {', '.join(sorted(unknown_machines))}")
        overrides = {**(self.store.load_setting(POLICY_SETTING) or {}), **patch}
        before = _flatten(self.policy)
        with self.manager.lock:
            self.store.save_setting(POLICY_SETTING, overrides)
            self.policy.clear()
            self.policy.update(build_policy(self.catalog, overrides))
        after = _flatten(self.policy)
        changes = [f"{k}: {before.get(k, 'default')} -> {after.get(k, 'default')}"
                   for k in sorted(set(before) | set(after)) if before.get(k) != after.get(k)]
        self.store.add_event("POLICY_CHANGED",
                             "Operator changed the machine rules: " + ("; ".join(changes) or "no effective change"),
                             detail={"changes": changes, "overrides": overrides})
        return self.policy_view()

    # ------------------------------------------------------------ channels

    def channel_view(self, channel: Channel) -> dict:
        active = sum(1 for r in self.manager.tasks.values()
                     if r.channel == channel.id and r.status not in TERMINAL)
        return {**channel.model_dump(mode="json"), "path": channel.path,
                "allowed": self.channels.allowed(channel), "active_tasks": active,
                "tasks": sum(1 for r in self.manager.tasks.values() if r.channel == channel.id),
                "budget": self._budget_summary(channel, self.manager.ledger())}

    def channels_view(self) -> list[dict]:
        return [self.channel_view(c) for c in self.channels.list()]

    def create_channel(self, name: str, description: str = "", rules: Optional[ChannelRules] = None) -> dict:
        channel = self.channels.create(name, description, rules)
        self.store.add_event("CHANNEL_CREATED", f"Operator created channel '{name}' ({channel.id})",
                             detail={"allowed": self.channels.allowed(channel)}, channel=channel.id)
        return self.channel_view(channel)

    def update_channel(self, channel_id: str, name: Optional[str] = None, description: Optional[str] = None,
                       rules: Optional[ChannelRules] = None) -> Optional[dict]:
        channel = self.channels.update(channel_id, name=name, description=description, rules=rules)
        if channel is None:
            return None
        self.store.add_event("CHANNEL_CHANGED", f"Operator changed channel '{channel.name}'",
                             detail={"allowed": self.channels.allowed(channel)}, channel=channel.id)
        return self.channel_view(channel)

    def delete_channel(self, channel_id: str) -> bool:
        """Revoke a channel: its URL dies and its active tasks are cancelled.
        Raises ValueError for the default channel."""
        channel = self.channels.get(channel_id)
        if channel is None:
            return False
        self.channels.delete(channel_id)
        for record in self.manager.tasks.values():
            if record.channel == channel_id and record.status not in TERMINAL:
                self.manager.cancel(record)
        self.store.add_event("CHANNEL_DELETED", f"Operator deleted channel '{channel.name}' ({channel_id})",
                             channel=channel_id)
        return True

    # ------------------------------------------------------------ resources

    def snapshot(self, channel: Optional[Channel] = None) -> dict:
        snap = self.collector.latest
        if snap is None:
            return {"taken_at": None, "aggregates": {}, "records": []}
        records = self._records(channel)
        from ..schemas import ResourceSnapshot
        return {"taken_at": snap.taken_at, "aggregates": ResourceSnapshot(records=records).aggregates(),
                "records": [r.model_dump(mode="json") for r in records]}

    def history(self, limit: int = 60, channel: Optional[Channel] = None) -> list[dict]:
        out = []
        for s in self.collector.history(limit):
            records = self.channels.scope(s.records, channel) if channel else s.records
            out.append({"taken_at": s.taken_at, "aggregates": s.aggregates(),
                        "records": [{"system": r.system, "partition": r.partition,
                                     "utilization": round(r.utilization(), 3),
                                     "free": r.free_units()[0], "pending": r.pending_jobs,
                                     "health": r.endpoint_health.value} for r in records]})
        return out

    def resources(self, channel: Optional[Channel] = None) -> dict:
        """All compute resources in one list — every provider, one shape;
        inside a channel, only its queues, caps and account."""
        snap = self.collector.latest
        records = self._records(channel)
        policy = self._policy(channel)
        allowed = self.channels.allowed(channel) if channel else None
        systems = []
        for info in self._systems(channel):
            recs = [r for r in records if r.system == info.name]
            parts = [p for p in info.partitions if allowed is None or partition_key(info.name, p) in allowed]
            hs = {r.endpoint_health.value for r in recs}
            health = ("unknown" if not recs else "online" if hs == {"online"}
                      else "offline" if hs == {"offline"} else "degraded")
            account = channel.rules.systems[info.name].account if channel else None
            systems.append({
                "name": info.name, "provider": info.provider.value, "endpoint": info.endpoint,
                "tags": info.tags, "location": info.location,
                "accounts": [account] if account else (info.accounts or next((r.accounts for r in recs if r.accounts), [])),
                "health": health, "declared_partitions": parts,
                "partitions": [r.model_dump(mode="json") for r in recs],
                "limits": {p: {"max_nodes_per_job": effective_node_cap(policy, info.name, p),
                               "blocked": is_blocked(policy, info.name, p)} for p in parts},
                "gpus_free": sum(r.free_units()[0] for r in recs if r.gpu_type),
                "gpus_total": sum((r.nodes_total - r.nodes_down) * r.gpus_per_node for r in recs),
                "cpus_free": sum(r.free_units()[0] for r in recs if not r.gpu_type),
                "nodes_idle": sum(r.nodes_idle for r in recs),
                "running_jobs": sum(r.running_jobs for r in recs),
                "pending_jobs": sum(r.pending_jobs for r in recs),
                "user_running_jobs": sum(r.user_running_jobs for r in recs),
                "user_pending_jobs": sum(r.user_pending_jobs for r in recs),
                "utilization": (sum(r.utilization() for r in recs) / len(recs)) if recs else None,
            })
        return {"taken_at": snap.taken_at if snap else None, "channel": self._brief(channel),
                "providers": self.providers(), "systems": systems}

    def idle(self, min_gpus: int = 1, min_gpu_mem_gb: float = 0, gpu_type: Optional[str] = None,
             arch: Optional[str] = None, max_pending: int = 10**9, include_cpu: bool = True,
             provider: Optional[str] = None, channel: Optional[Channel] = None) -> list[dict]:
        """Partitions that can start work now, ranked by free units."""
        out = []
        for r in self._records(channel):
            if provider and r.provider.value != provider:
                continue
            if not r.gpu_type and not include_cpu:
                continue
            if gpu_type and r.gpu_type != gpu_type:
                continue
            if arch and r.node_arch != arch:
                continue
            free, unit = r.free_units()
            reasons = []
            if r.endpoint_health != EndpointHealth.ONLINE:
                reasons.append(f"endpoint {r.endpoint_health.value}")
            if r.gpu_type:
                if free < min_gpus:
                    reasons.append(f"{free} GPU free < {min_gpus}")
                if r.gpu_mem_gb < min_gpu_mem_gb:
                    reasons.append(f"{r.gpu_mem_gb} GB/GPU < {min_gpu_mem_gb}")
            elif free <= 0:
                reasons.append("no idle cores")
            if r.pending_jobs > max_pending:
                reasons.append(f"{r.pending_jobs} pending > {max_pending}")
            out.append({"system": r.system, "partition": r.partition, "provider": r.provider.value,
                        "gpu_type": r.gpu_type, "gpu_mem_gb": r.gpu_mem_gb, "arch": r.node_arch,
                        "free": free, "unit": unit, "pending_jobs": r.pending_jobs,
                        "max_walltime_minutes": r.max_walltime_minutes,
                        "available": not reasons, "reasons": reasons})
        out.sort(key=lambda x: (not x["available"], -x["free"]))
        return out

    # ------------------------------------------------------------ queue status

    def status(self, channel: Optional[Channel] = None) -> dict:
        """Supercomputers and queues at a glance — the agent-facing view.

        One row per partition with the numbers an agent needs to reason
        about placement: what is free right now, how deep the queue is,
        what of it is mine, the wall-time cap, the per-job node cap, and the
        wait heuristic for a nominal one-node job (1 GPU on GPU partitions,
        min CPUs on CPU partitions). Inside a channel: its queues and account.
        """
        from .. import auth

        snap = self.collector.latest
        records = self._records(channel)
        policy = self._policy(channel)
        systems = []
        for info in self._systems(channel):
            recs = [r for r in records if r.system == info.name]
            hs = {r.endpoint_health.value for r in recs}
            health = ("no data" if not recs else "online" if hs == {"online"}
                      else "offline" if hs == {"offline"} else "degraded")
            parts = []
            for r in recs:
                free, unit = r.free_units()
                nominal = Sizing(nodes=1, gpus=1 if r.gpu_type else 0,
                                 cpus=min(8, r.cpus_per_node or 8), walltime_minutes=60)
                cand = Candidate(endpoint=r.endpoint, system=r.system, partition=r.partition,
                                 account=r.account, record=r, feasible=True, sizing=nominal)
                online = r.endpoint_health == EndpointHealth.ONLINE
                parts.append({
                    "partition": r.partition,
                    "hardware": (f"{r.gpu_type} x{r.gpus_per_node}/node, {r.gpu_mem_gb:g} GB/GPU, "
                                 f"{r.cpus_per_node}c, {r.mem_per_node_gb:g} GB" if r.gpu_type
                                 else f"{r.cpus_per_node}c, {r.mem_per_node_gb:g} GB/node"),
                    "arch": r.node_arch, "account": r.account,
                    "container_runtime": r.container_runtime,
                    "health": r.endpoint_health.value,
                    "free": free if online else None, "unit": unit,
                    "utilization": round(r.utilization(), 3) if online else None,
                    "nodes": {"idle": r.nodes_idle, "mixed": r.nodes_mixed,
                              "allocated": r.nodes_allocated, "down": r.nodes_down,
                              "total": r.nodes_total},
                    "queue": {"running": r.running_jobs, "pending": r.pending_jobs},
                    "mine": {"running": r.user_running_jobs, "pending": r.user_pending_jobs},
                    "max_walltime_minutes": r.max_walltime_minutes,
                    "max_nodes_per_job": effective_node_cap(policy, r.system, r.partition),
                    "blocked": is_blocked(policy, r.system, r.partition),
                    "node_hours_left": self.manager.remaining(channel, r.system, r.partition) if channel else None,
                    "est_wait_minutes": round(placement.estimate_wait(cand), 1) if online else None,
                    "timestamp": r.timestamp,
                })
            systems.append({"name": info.name, "provider": info.provider.value,
                            "endpoint": info.endpoint, "health": health,
                            "tags": info.tags, "partitions": parts})
        active = self.tasks("active", channel)
        return {
            "taken_at": snap.taken_at if snap else None,
            "channel": self._brief(channel),
            "auth": {k: v for k, v in auth.auth_status(required=True).items()
                     if k in ("mode", "authenticated", "hint")},
            "providers": self.providers(),
            "systems": systems,
            "my_tasks": {"active": len(active),
                         "pending": sum(1 for t in active if t["status"] == "PENDING"),
                         "running": sum(1 for t in active if t["status"] == "RUNNING")},
        }

    def status_text(self, channel: Optional[Channel] = None) -> str:
        """Markdown rendering of status() — paste-into-context friendly."""
        st = self.status(channel)
        wall = lambda m: "-" if m is None else f"{m // 60}h" if m % 60 == 0 else f"{m}m"
        taken = st["taken_at"].strftime("%Y-%m-%d %H:%M:%S UTC") if st["taken_at"] else "no snapshot yet"
        title = f"# Federation status — {taken}" + (f" — channel {st['channel']['name']}" if st["channel"] else "")
        lines = [title,
                 f"auth: {st['auth']['mode']} ({'ok' if st['auth']['authenticated'] else 'NOT authenticated: ' + str(st['auth']['hint'])})"
                 f" · my tasks: {st['my_tasks']['active']} active ({st['my_tasks']['pending']} pending, {st['my_tasks']['running']} running)",
                 ""]
        for sy in st["systems"]:
            lines.append(f"## {sy['name']} — {sy['health']} ({sy['provider']}, endpoint {sy['endpoint'][:8] or '-'})")
            if not sy["partitions"]:
                lines += ["(no live partitions — provider "
                          + next((p["status"] for p in st["providers"] if p["kind"] == sy["provider"]), "?") + ")", ""]
                continue
            lines += ["| partition | hardware | account | free now | busy | nodes idle/mixed/alloc/down | queue run/pend | mine | max wall | max nodes/job | node-h left | est. wait |",
                      "|---|---|---|---|---|---|---|---|---|---|---|---|"]
            for p in sy["partitions"]:
                n = p["nodes"]
                lines.append(
                    f"| {p['partition']} | {p['hardware']} · {p['arch']} | {p['account']} | "
                    f"{'-' if p['free'] is None else f'{p['free']} {p['unit']}'} | "
                    f"{'-' if p['utilization'] is None else f'{p['utilization']:.0%}'} | "
                    f"{n['idle']}/{n['mixed']}/{n['allocated']}/{n['down']} | "
                    f"{p['queue']['running']}/{p['queue']['pending']} | "
                    f"{p['mine']['running']}/{p['mine']['pending']} | {wall(p['max_walltime_minutes'])} | "
                    f"{'blocked' if p['blocked'] else p['max_nodes_per_job']} | "
                    f"{'∞' if p['node_hours_left'] is None else f'{p['node_hours_left']:.1f}'} | "
                    f"{'-' if p['est_wait_minutes'] is None else f'~{p['est_wait_minutes']:.0f}m'} |")
            lines.append("")
        lines.append("free now = GRES-level free GPUs (GPU partitions) or idle cores (CPU partitions); "
                     "max nodes/job = the cap the job is sized within (channel rule inside the machine rule); "
                     "node-h left = node-hours your channel may still commit there (∞ = no budget set); "
                     "est. wait = heuristic for a 1-node job, not a promise.")
        return "\n".join(lines)

    # ------------------------------------------------ environment inspection

    ENV_PROBE = r"""
echo '@@HOST'; hostname; uname -m; cat /etc/os-release 2>/dev/null | grep -E '^PRETTY_NAME' | cut -d= -f2 | tr -d '"'
echo '@@USER'; id -un; echo "$HOME"
echo '@@WORKDIR'; WD="__WORKDIR__"; echo "$WD"; if [ -d "$WD" ]; then echo exists; ls -la "$WD" 2>/dev/null | head -20; echo '--- logs'; ls -t "$WD"/logs 2>/dev/null | head -10; else echo missing; fi
echo '@@PYTHON'; command -v python3 && python3 --version 2>&1; python3 -c 'import torch,sys; print("torch", torch.__version__, "cuda", torch.cuda.is_available())' 2>&1 | tail -1
echo '@@MODULES'; (module avail 2>&1 | tr ' ' '\n' | grep -iE 'pytorch|torch|cuda|conda|python|apptainer|singularity|shifter|podman|nccl|openmpi' | head -40)
echo '@@CONTAINER'; command -v apptainer singularity shifter podman-hpc 2>/dev/null
echo '@@SLURM'; sinfo --version 2>&1 | head -1; sacctmgr -nP show assoc user=$(id -un) format=account,partition,qos 2>/dev/null | head -20
echo '@@STORAGE'; df -h "$HOME" 2>/dev/null | tail -1; quota -s 2>/dev/null | tail -3
"""

    @staticmethod
    def _sections(text: str) -> dict:
        out, cur = {}, None
        for line in text.splitlines():
            if line.startswith("@@"):
                cur = line[2:].strip().lower(); out[cur] = []
            elif cur is not None:
                out[cur].append(line.rstrip())
        return {k: "\n".join(v).strip() for k, v in out.items()}

    def env(self, system: str, channel: Channel, timeout_s: int = 90) -> dict:
        """What the target offers: host, user, workdir + recent logs, python/torch,
        relevant modules, container runtime, Slurm accounts/QOS, storage.
        Runs on the system's login node (self-owned endpoint)."""
        self._in_channel(system, channel)
        workdir = self.pool.workdir(system)
        r = self.pool.shell(system, self.ENV_PROBE.replace("__WORKDIR__", workdir), timeout_s)
        sec = self._sections(r.stdout)
        py = sec.get("python", "").splitlines()
        mods = sec.get("modules", "").split()
        return {
            "system": system, "workdir": workdir, "returncode": r.returncode,
            "host": sec.get("host", "").splitlines()[0] if sec.get("host") else None,
            "arch": (sec.get("host", "").splitlines()[1:2] or [None])[0],
            "os": (sec.get("host", "").splitlines()[2:3] or [None])[0],
            "user": (sec.get("user", "").splitlines()[0:1] or [None])[0],
            "home": (sec.get("user", "").splitlines()[1:2] or [None])[0],
            "workdir_exists": "exists" in sec.get("workdir", ""),
            "python": py[1] if len(py) > 1 else None,
            "torch": next((l for l in py if l.startswith("torch ")), None),
            "modules": mods, "container_runtime": (sec.get("container", "").split() or [None])[0],
            "slurm": sec.get("slurm"), "storage": sec.get("storage"),
            "sections": sec, "raw": r.stdout[-6000:],
        }

    def exec(self, system: str, command: str, channel: Channel, timeout_s: int = 60) -> dict:
        """One bounded command on the system's login node — for looking around
        (ls, module show, cat a log), not for running work (submit a task).
        Job submission/control and detached processes are refused by policy;
        both refusals and accepted commands go to the channel's activity feed."""
        self._in_channel(system, channel)
        shown = command.strip().replace("\n", " ")[:200]
        denied = check_shell_command(command)
        if denied:
            self.store.add_event("POLICY_DENIED", f"exec on {system} refused: {denied} — `{shown}`",
                                 detail={"system": system, "command": shown, "reason": denied}, channel=channel.id)
            raise PolicyDenied(f"exec refused: {denied}")
        r = self.pool.shell(system, command, timeout_s)
        self.store.add_event("SHELL_EXEC", f"exec on {system} (rc={r.returncode}): `{shown}`",
                             detail={"system": system, "command": shown, "returncode": r.returncode},
                             channel=channel.id)
        return {"system": system, "command": command, "returncode": r.returncode,
                "stdout": r.stdout[-20000:], "stderr": r.stderr[-4000:]}

    def put_file(self, system: str, path: str, content: bytes, channel: Channel, mode: str = "0644") -> dict:
        """Stage a file under the system's workdir (relative path)."""
        import base64

        self._in_channel(system, channel)
        if len(content) > 2_000_000:
            raise ValueError("file too large for staging (limit 2 MB); use Globus Transfer / scp")
        denied = check_stage_path(path)
        if denied:
            self.store.add_event("POLICY_DENIED", f"put-file on {system} refused: {denied} ({path[:120]})",
                                 detail={"system": system, "path": path[:200], "reason": denied}, channel=channel.id)
            raise PolicyDenied(f"put-file refused: {denied}")
        workdir = self.pool.workdir(system)
        target = f"{workdir}/{path}"
        b64 = base64.b64encode(content).decode()
        cmd = (f"mkdir -p \"$(dirname {target})\" && echo {b64} | base64 -d > {target} && "
               f"chmod {mode} {target} && ls -l {target}")
        r = self.pool.shell(system, cmd, 60)
        if r.returncode != 0:
            raise RuntimeError(f"staging failed on {system}: {(r.stderr or r.stdout)[-300:]}")
        self.store.add_event("FILE_STAGED", f"Staged {len(content)} bytes to {system}:{target}",
                             detail={"system": system, "path": target, "bytes": len(content)}, channel=channel.id)
        return {"system": system, "path": target, "bytes": len(content), "listing": r.stdout.strip()}

    def task_logs(self, task_id: str, channel: Channel, tail: int = 200) -> dict:
        """stdout/stderr tails of a task's (latest) job from the remote workdir."""
        record = self._record(task_id, channel)
        if record.placement is None:
            return {"task_id": task_id, "system": None, "stdout": "", "stderr": "",
                    "note": "task was never submitted to a queue"}
        system, name = record.placement.system, record.spec.name
        workdir = self.pool.workdir(system)
        cmd = (f"echo '@@STDOUT'; tail -n {int(tail)} {workdir}/logs/{name}.stdout 2>&1; "
               f"echo '@@STDERR'; tail -n {int(tail)} {workdir}/logs/{name}.stderr 2>&1")
        r = self.pool.shell(system, cmd, 60)
        sec = self._sections(r.stdout)
        return {"task_id": task_id, "system": system, "job_id": record.job_id,
                "status": record.status.value,
                "stdout_path": f"{workdir}/logs/{name}.stdout",
                "stderr_path": f"{workdir}/logs/{name}.stderr",
                "stdout": sec.get("stdout", ""), "stderr": sec.get("stderr", "")}

    # ------------------------------------------------------------ tasks

    def _record(self, task_id: str, channel: Optional[Channel]) -> TaskRecord:
        """A task visible from this scope: any task for the operator, the
        channel's own for an agent (anything else does not exist for it: KeyError)."""
        record = self.manager.tasks.get(task_id)
        if record is None or (channel is not None and record.channel != channel.id):
            raise KeyError(f"task not found: {task_id}")
        return record

    def candidates(self, envelope: ResourceEnvelope, channel: Channel) -> list[dict]:
        """Every queue of the channel evaluated for this envelope — the facts an
        agent chooses from: verdicts, the sizing it would get, the wait
        estimate, what is free, how deep the queue is, the caps. Feasible
        queues first, by wait."""
        caps = self.channels.allowed(channel)
        ledger = self.manager.ledger()
        out = []
        for c in self.manager.candidates(envelope, channel):
            r = c.record
            free, unit = r.free_units()
            left = budget.remaining(ledger, self.channels, channel, c.system, c.partition)
            out.append({
                "system": c.system, "partition": c.partition, "account": c.account, "feasible": c.feasible,
                "node_hours_left": None if left is None else round(left, 3),
                "verdicts": [v.model_dump() for v in c.verdicts],
                "sizing": c.sizing.model_dump() if c.sizing else None,
                "est_wait_minutes": round(c.est_wait_minutes, 1) if c.est_wait_minutes is not None else None,
                "health": r.endpoint_health.value, "arch": r.node_arch, "gpu_type": r.gpu_type,
                "free": free, "unit": unit, "nodes_idle": r.nodes_idle,
                "pending_jobs": r.pending_jobs, "user_pending_jobs": r.user_pending_jobs,
                "max_walltime_minutes": r.max_walltime_minutes,
                "max_nodes_per_job": caps[partition_key(c.system, c.partition)],
            })
        out.sort(key=lambda x: (not x["feasible"], x["est_wait_minutes"] if x["est_wait_minutes"] is not None else 1e9))
        return out

    def submit(self, name: str, command: str, envelope: ResourceEnvelope, target: Target,
               channel: Channel, description: str = "", container: Optional[str] = None,
               max_retries: int = 2, auto_continue: bool = True, release: Optional[int] = None) -> dict:
        """Submit one task to the queue the caller chose, through its channel.
        Raises Infeasible when the queue is not in the channel, cannot run this
        envelope now (with the verdicts) or is not aligned to the task's
        release. Returns the task summary."""
        spec = TaskSpec(name=name, description=description, command=command, container=container,
                        envelope=envelope, max_retries=max_retries, auto_continue=auto_continue, release=release)
        return summarize_task(self.manager.submit(spec, target, channel))

    def tasks(self, status: Optional[str] = None, channel: Optional[Channel] = None,
              channel_id: Optional[str] = None) -> list[dict]:
        scope = channel.id if channel else channel_id
        records = sorted((r for r in self.manager.tasks.values() if scope is None or r.channel == scope),
                         key=lambda r: r.created_at, reverse=True)
        if status == "active":
            records = [r for r in records if r.status not in TERMINAL]
        elif status:
            records = [r for r in records if r.status.value == status.upper()]
        return [summarize_task(r) for r in records]

    def task(self, task_id: str, channel: Optional[Channel] = None) -> dict:
        return self._record(task_id, channel).model_dump(mode="json")

    def cancel(self, task_id: str, channel: Optional[Channel] = None) -> dict:
        return summarize_task(self.manager.cancel(self._record(task_id, channel)))

    def retarget(self, task_id: str, target: Target, channel: Channel) -> dict:
        """Move a task to another queue the caller chose (same errors as submit)."""
        return summarize_task(self.manager.retarget(self._record(task_id, channel), target, channel))

    # ------------------------------------------------------------ releases

    def _release_view(self, release: Release) -> dict:
        return {**release.summary(),
                "builds": {s: b.model_dump(mode="json") for s, b in self.releases.builds(release.channel, release.version).items()}}

    def releases_view(self, channel: Channel) -> list[dict]:
        return [self._release_view(r) for r in self.releases.list(channel.id)]

    def release_view(self, channel: Channel, version: int) -> dict:
        return self._release_view(self.releases.get(channel.id, version))

    def create_release(self, channel: Channel, archive: bytes, source: Source, base_version: int,
                       env: Optional[str] = None, systems: Optional[list[str]] = None) -> dict:
        """The channel's next release from an archive the caller packed.
        ``base_version`` must be the channel's latest (Conflict otherwise)."""
        systems = sorted(self.channels.machines(channel)) if systems is None else systems
        for system in systems:
            self._in_channel(system, channel)
        release = self.releases.create(channel.id, archive, source, env, systems, base_version)
        origin = f"{source.kind} {source.path}" + (f"@{source.commit[:10]}" if source.commit else "")
        self.store.add_event("RELEASE_CREATED", f"Release {release.version} created from {origin}: "
                             f"{len(release.files)} file(s), {release.bytes} bytes, for {', '.join(systems)}",
                             detail={"version": release.version, "tree": release.tree, "source": source.model_dump(),
                                     "systems": systems}, channel=channel.id)
        return self._release_view(release)

    def align_release(self, channel: Channel, version: int, system: str, background: bool = True) -> dict:
        """Materialise the release on one machine: a new build number, run in
        the background (poll the release until it is ready or failed)."""
        release = self.releases.get(channel.id, version)
        self._in_channel(system, channel)
        if system not in release.systems:
            raise ValueError(f"release {version} does not target {system} (targets: {', '.join(release.systems)})")
        current = self.releases.build(channel.id, version, system)
        if current and current.status == "building":
            return current.model_dump(mode="json")
        build = self.releases.save(Build(channel=channel.id, version=version, system=system,
                                         number=current.number + 1 if current else 1, status="building"))
        if background:
            threading.Thread(target=self._run_align, args=(release, build), daemon=True,
                             name=f"align-{channel.id}-r{version}-{system}").start()
        else:
            self._run_align(release, build)
        return self.releases.build(channel.id, version, system).model_dump(mode="json")

    def _run_align(self, release: Release, build: Build) -> None:
        try:
            facts = self.aligner.align(release, build.system)
            probe = next((r for r in self._records(None) if r.system == build.system), None)
            if probe:
                facts["container_runtime"] = probe.container_runtime
            build.status, build.machine, build.error = "ready", facts, None
            build.dir = f"{self.pool.workdir(build.system)}/releases/{release.channel}/{release.snapshot}"
            build.finished_at = build.verified_at = utcnow()
            self.store.add_event("RELEASE_ALIGNED", f"Release {release.version} aligned on {build.system} "
                                 f"(build #{build.number}): {build.dir}",
                                 detail={"version": release.version, "system": build.system, "build": build.number,
                                         "dir": build.dir, "machine": facts}, channel=release.channel)
        except Exception as exc:
            build.status, build.error, build.finished_at = "failed", str(exc)[-600:], utcnow()
            self.store.add_event("RELEASE_ALIGN_FAILED", f"Release {release.version} failed to align on {build.system} "
                                 f"(build #{build.number}): {build.error[-200:]}",
                                 detail={"version": release.version, "system": build.system, "build": build.number},
                                 channel=release.channel)
        self.releases.save(build)

    def verify_release(self, channel: Channel, version: int, system: str) -> dict:
        """Re-check the snapshot on the machine against the manifest."""
        release = self.releases.get(channel.id, version)
        build = self.releases.build(channel.id, version, system)
        if build is None:
            raise KeyError(f"release {version} was never aligned on {system}")
        bad = self.aligner.verify(release, system)
        if bad:
            build.status, build.error = "stale", f"{len(bad)} file(s) differ from the release: {', '.join(bad[:8])}"
            self.store.add_event("RELEASE_DRIFT", f"Release {version} on {system} drifted: {build.error}",
                                 detail={"version": version, "system": system, "paths": bad[:50]}, channel=channel.id)
        else:
            build.status, build.error, build.verified_at = "ready", None, utcnow()
        return self.releases.save(build).model_dump(mode="json")

    def wait(self, task_id: str, timeout_s: float = 3600, poll_s: float = 2.0,
             channel: Optional[Channel] = None) -> dict:
        """Block until the task is terminal (in-process; the CLI/MCP poll over HTTP)."""
        deadline = time.time() + timeout_s
        while True:
            record = self._record(task_id, channel)
            if record.status in TERMINAL or time.time() >= deadline:
                return summarize_task(record)
            time.sleep(poll_s)

    def events(self, limit: int = 100, task_id: Optional[str] = None,
               channel: Optional[Channel] = None, channel_id: Optional[str] = None) -> list[dict]:
        return self.store.events(limit=limit, task_id=task_id, channel=channel.id if channel else channel_id)
