"""diamond-agent-core: the programmatic facade.

Everything that talks to HPC systems, tracks tasks and monitors the
federation lives behind this one class. The HTTP API (``api.py``), the CLI
(``core/cli.py``), the MCP server (``core/mcp_server.py``) and any in-process
agent all call the same methods, so the frontend is just one more client.

    core = DiamondAgentCore.build(catalog)
    core.tick()                                  # one agent cycle
    core.submit(name="demo", command="python train.py", auto_generate_envelope=True)
    core.submit(..., target=("DeltaAI@NCSA", "ghx4"))   # any resource, pinned
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from ..agent.ai import ai_status
from ..agent.envelope_gen import generate_envelope
from ..agent.orchestrator import TaskManager, TaskRecord
from ..federation.collector import FederationCollector
from ..policy import (PolicyDenied, build_policy, check_shell_command, check_stage_path,
                      effective_node_cap, is_blocked, partition_key)
from ..providers import ResourcePool, build_pool
from ..scheduler.meta import MetaScheduler
from ..schemas import EndpointHealth, ResourceEnvelope, TaskSpec, TaskStatus
from ..store import Store

logger = logging.getLogger(__name__)

TERMINAL = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
Target = Optional[tuple[str, Optional[str]]]
POLICY_SETTING = "policy_overrides"     # settings-table key for the operator's overrides


class UnknownTarget(KeyError):
    """Raised when a pin names a system/partition the pool does not own."""


def summarize_task(record: TaskRecord) -> dict:
    """The compact task row shared by every interface."""
    placement = record.placement
    return {
        "task_id": record.task_id,
        "name": record.spec.name,
        "description": record.spec.description,
        "status": record.status.value,
        "objective": record.spec.envelope.objective.value,
        "envelope_source": record.envelope_source,
        "envelope_confidence": record.envelope_confidence,
        "system": placement.system if placement else None,
        "partition": placement.partition if placement else None,
        "job_id": record.job_id,
        "sizing": placement.sizing.model_dump() if placement and placement.sizing else None,
        "attempts": record.attempts,
        "pending_ticks": record.pending_ticks,
        "created_at": record.created_at,
        "finished_at": record.finished_at,
        "mode": record.decision.mode if record.decision else None,
        "group_id": record.group_id,
        "target_system": record.target_system,
        "target_partition": record.target_partition,
    }


def _flatten(policy: dict) -> dict:
    """One entry per setting — for naming what an edit changed."""
    return {"max nodes/job": policy["max_nodes_per_job"],
            "max pending/system": policy["max_user_pending_per_system"],
            **{f"{k} max nodes": v for k, v in policy["partition_limits"].items()},
            **{f"{k} blocked": True for k in policy["blocked_partitions"]}}


class DiamondAgentCore:
    def __init__(self, manager: TaskManager, pool: ResourcePool, catalog: dict):
        self.manager = manager
        self.pool = pool
        self.catalog = catalog
        self.collector: FederationCollector = manager.collector
        self.store: Store = manager.store
        # The effective policy is the scheduler's dict; the orchestrator's
        # dispatch gate reads the same object, so an edit applies at once.
        self.policy: dict = manager.scheduler.policy

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
        scheduler = MetaScheduler(build_policy(catalog, overrides))
        manager = TaskManager(store, collector, scheduler, pool)
        return cls(manager, pool, catalog)

    def tick(self) -> None:
        """One agent cycle: refresh the federation snapshot, then run the monitor."""
        try:
            self.collector.refresh()
        except Exception:
            logger.exception("federation refresh failed; monitor runs on the last snapshot")
        self.manager.tick()

    # ------------------------------------------------------------ status

    def info(self) -> dict:
        from .. import auth

        return {
            "ai": ai_status(),
            "auth": auth.auth_status(required=True),
            "systems": [{"name": s["name"], "endpoint": str(s.get("endpoint", "")),
                         "partitions": [p["name"] for p in s.get("partitions", [])]}
                        for s in self.catalog.get("systems", [])],
            "policy": self.policy,
            "providers": self.providers(),
        }

    # ------------------------------------------------------------- policy

    def _partition_keys(self) -> set[str]:
        return {partition_key(s.name, p) for s in self.pool.systems() for p in s.partitions}

    def policy_view(self) -> dict:
        """The limits in force plus one resolved row per partition. Every
        client reads this; only the web UI (PUT /ui/policy) changes it."""
        snap = self.collector.latest
        totals = {(r.system, r.partition): r.nodes_total for r in (snap.records if snap else [])}
        rows = [{"system": s.name, "partition": p, "key": partition_key(s.name, p),
                 "max_nodes_per_job": effective_node_cap(self.policy, s.name, p),
                 "override": self.policy["partition_limits"].get(partition_key(s.name, p)),
                 "blocked": is_blocked(self.policy, s.name, p),
                 "nodes_total": totals.get((s.name, p))}
                for s in self.pool.systems() for p in s.partitions]
        return {**self.policy, "partitions": rows}

    def update_policy(self, patch: dict) -> dict:
        """Operator edit (shape validated at the HTTP boundary). A key present
        in the patch replaces the stored override; ``None`` returns it to the
        catalog value; ``partition_limits`` is the whole per-queue map. Persisted,
        applied in place to the shared policy dict, recorded as POLICY_CHANGED."""
        if "partition_limits" in patch:
            patch["partition_limits"] = {k: v for k, v in patch["partition_limits"].items() if v is not None}
        unknown = (set(patch.get("partition_limits") or [])
                   | set(patch.get("blocked_partitions") or [])) - self._partition_keys()
        if unknown:
            raise ValueError(f"unknown partition(s): {', '.join(sorted(unknown))}")
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
                             "Operator changed scheduling limits: " + ("; ".join(changes) or "no effective change"),
                             detail={"changes": changes, "overrides": overrides})
        return self.policy_view()

    def providers(self) -> list[dict]:
        return [{**p.__dict__, "kind": p.kind.value} for p in self.pool.providers_info()]

    # ------------------------------------------------------------ resources

    def snapshot(self) -> dict:
        snap = self.collector.latest
        if snap is None:
            return {"taken_at": None, "aggregates": {}, "records": []}
        return {"taken_at": snap.taken_at, "aggregates": snap.aggregates(),
                "records": [r.model_dump(mode="json") for r in snap.records]}

    def history(self, limit: int = 60) -> list[dict]:
        return [{"taken_at": s.taken_at, "aggregates": s.aggregates(),
                 "records": [{"system": r.system, "partition": r.partition,
                              "utilization": round(r.utilization(), 3),
                              "free": r.free_units()[0], "pending": r.pending_jobs,
                              "health": r.endpoint_health.value} for r in s.records]}
                for s in self.collector.history(limit)]

    def resources(self) -> dict:
        """All compute resources in one list — every provider, one shape."""
        snap = self.collector.latest
        records = snap.records if snap else []
        systems = []
        for info in self.pool.systems():
            recs = [r for r in records if r.system == info.name]
            hs = {r.endpoint_health.value for r in recs}
            health = ("unknown" if not recs else "online" if hs == {"online"}
                      else "offline" if hs == {"offline"} else "degraded")
            systems.append({
                "name": info.name, "provider": info.provider.value, "endpoint": info.endpoint,
                "tags": info.tags, "location": info.location, "accounts": info.accounts,
                "health": health, "declared_partitions": info.partitions,
                "partitions": [r.model_dump(mode="json") for r in recs],
                "limits": {p: {"max_nodes_per_job": effective_node_cap(self.policy, info.name, p),
                               "blocked": is_blocked(self.policy, info.name, p)}
                           for p in info.partitions},
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
        return {"taken_at": snap.taken_at if snap else None,
                "providers": self.providers(), "systems": systems}

    def idle(self, min_gpus: int = 1, min_gpu_mem_gb: float = 0, gpu_type: Optional[str] = None,
             arch: Optional[str] = None, max_pending: int = 10**9, include_cpu: bool = True,
             provider: Optional[str] = None) -> list[dict]:
        """Partitions that can start work now, ranked by free units."""
        out = []
        for r in (self.collector.latest.records if self.collector.latest else []):
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

    def status(self) -> dict:
        """Supercomputers and queues at a glance — the agent-facing view.

        One row per partition with the numbers an agent needs to reason
        about placement: what is free right now, how deep the queue is,
        what of it is mine, the wall-time cap, and the scheduler's own
        wait heuristic for a nominal one-node job (1 GPU on GPU
        partitions, min CPUs on CPU partitions).
        """
        from ..scheduler.meta import _fallback_wait
        from ..schemas import Candidate, Sizing
        from .. import auth

        snap = self.collector.latest
        records = snap.records if snap else []
        systems = []
        for info in self.pool.systems():
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
                    "max_nodes_per_job": effective_node_cap(self.policy, r.system, r.partition),
                    "blocked": is_blocked(self.policy, r.system, r.partition),
                    "est_wait_minutes": round(_fallback_wait(cand), 1) if online else None,
                    "timestamp": r.timestamp,
                })
            systems.append({"name": info.name, "provider": info.provider.value,
                            "endpoint": info.endpoint, "health": health,
                            "tags": info.tags, "partitions": parts})
        active = [t for t in self.tasks("active")]
        return {
            "taken_at": snap.taken_at if snap else None,
            "auth": {k: v for k, v in auth.auth_status(required=True).items()
                     if k in ("mode", "authenticated", "hint")},
            "providers": self.providers(),
            "systems": systems,
            "my_tasks": {"active": len(active),
                         "pending": sum(1 for t in active if t["status"] == "PENDING"),
                         "running": sum(1 for t in active if t["status"] == "RUNNING")},
        }

    def status_text(self) -> str:
        """Markdown rendering of status() — paste-into-context friendly."""
        st = self.status()
        wall = lambda m: "-" if m is None else f"{m // 60}h" if m % 60 == 0 else f"{m}m"
        taken = st["taken_at"].strftime("%Y-%m-%d %H:%M:%S UTC") if st["taken_at"] else "no snapshot yet"
        lines = [f"# Federation status — {taken}",
                 f"auth: {st['auth']['mode']} ({'ok' if st['auth']['authenticated'] else 'NOT authenticated: ' + str(st['auth']['hint'])})"
                 f" · my tasks: {st['my_tasks']['active']} active ({st['my_tasks']['pending']} pending, {st['my_tasks']['running']} running)",
                 ""]
        for sy in st["systems"]:
            lines.append(f"## {sy['name']} — {sy['health']} ({sy['provider']}, endpoint {sy['endpoint'][:8] or '-'})")
            if not sy["partitions"]:
                lines += ["(no live partitions — provider "
                          + next((p["status"] for p in st["providers"] if p["kind"] == sy["provider"]), "?") + ")", ""]
                continue
            lines += ["| partition | hardware | free now | busy | nodes idle/mixed/alloc/down | queue run/pend | mine | max wall | max nodes/job | est. wait |",
                      "|---|---|---|---|---|---|---|---|---|---|"]
            for p in sy["partitions"]:
                n = p["nodes"]
                lines.append(
                    f"| {p['partition']} | {p['hardware']} · {p['arch']} | "
                    f"{'-' if p['free'] is None else f'{p['free']} {p['unit']}'} | "
                    f"{'-' if p['utilization'] is None else f'{p['utilization']:.0%}'} | "
                    f"{n['idle']}/{n['mixed']}/{n['allocated']}/{n['down']} | "
                    f"{p['queue']['running']}/{p['queue']['pending']} | "
                    f"{p['mine']['running']}/{p['mine']['pending']} | {wall(p['max_walltime_minutes'])} | "
                    f"{'blocked' if p['blocked'] else p['max_nodes_per_job']} | "
                    f"{'-' if p['est_wait_minutes'] is None else f'~{p['est_wait_minutes']:.0f}m'} |")
            lines.append("")
        lines.append("free now = GRES-level free GPUs (GPU partitions) or idle cores (CPU partitions); "
                     "max nodes/job = operator cap the scheduler sizes within (set in the web UI); "
                     "est. wait = scheduler heuristic for a 1-node job, not a promise.")
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

    def env(self, system: str, timeout_s: int = 90) -> dict:
        """What the target offers: host, user, workdir + recent logs, python/torch,
        relevant modules, container runtime, Slurm accounts/QOS, storage.
        Runs on the system's login node (self-owned endpoint)."""
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

    def exec(self, system: str, command: str, timeout_s: int = 60) -> dict:
        """One bounded command on the system's login node — for looking around
        (ls, module show, cat a log), not for running work (submit a task).
        Job submission/control and detached processes are refused by policy;
        both refusals and accepted commands go to the activity feed."""
        shown = command.strip().replace("\n", " ")[:200]
        denied = check_shell_command(command)
        if denied:
            self.store.add_event("POLICY_DENIED", f"exec on {system} refused: {denied} — `{shown}`",
                                 detail={"system": system, "command": shown, "reason": denied})
            raise PolicyDenied(f"exec refused: {denied}")
        r = self.pool.shell(system, command, timeout_s)
        self.store.add_event("SHELL_EXEC", f"exec on {system} (rc={r.returncode}): `{shown}`",
                             detail={"system": system, "command": shown, "returncode": r.returncode})
        return {"system": system, "command": command, "returncode": r.returncode,
                "stdout": r.stdout[-20000:], "stderr": r.stderr[-4000:]}

    def put_file(self, system: str, path: str, content: bytes, mode: str = "0644") -> dict:
        """Stage a file under the system's workdir (relative path)."""
        import base64

        if len(content) > 2_000_000:
            raise ValueError("file too large for staging (limit 2 MB); use Globus Transfer / scp")
        denied = check_stage_path(path)
        if denied:
            self.store.add_event("POLICY_DENIED", f"put-file on {system} refused: {denied} ({path[:120]})",
                                 detail={"system": system, "path": path[:200], "reason": denied})
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
                             detail={"system": system, "path": target, "bytes": len(content)})
        return {"system": system, "path": target, "bytes": len(content), "listing": r.stdout.strip()}

    def task_logs(self, task_id: str, tail: int = 200) -> dict:
        """stdout/stderr tails of a task's (latest) job from the remote workdir."""
        record = self.manager.tasks.get(task_id)
        if record is None:
            raise KeyError(task_id)
        if record.placement is None:
            return {"task_id": task_id, "system": None, "stdout": "", "stderr": "",
                    "note": "task has no placement yet"}
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

    # ------------------------------------------------------------ envelope

    def draft_envelope(self, name: str, description: str = "", command: str = "") -> dict:
        env, rationale, source, confidence = generate_envelope(name, description, command)
        return {"envelope": env.model_dump(mode="json"), "rationale": rationale,
                "source": source, "confidence": confidence}

    # ------------------------------------------------------------ tasks

    def validate_target(self, target: Target) -> Target:
        if target is None:
            return None
        system, partition = target
        known = {s.name: s for s in self.pool.systems()}
        if system not in known:
            raise UnknownTarget(f"unknown system {system!r}")
        if partition and partition not in known[system].partitions:
            raise UnknownTarget(f"unknown partition {partition!r} on {system}")
        return system, partition

    def submit(self, name: str, command: str, description: str = "",
               envelope: ResourceEnvelope | dict | None = None, target: Target = None,
               container: Optional[str] = None, max_retries: int = 2,
               auto_continue: bool = True, auto_generate_envelope: bool = False) -> dict:
        """Submit one task. ``target=None`` lets the meta-scheduler choose;
        ``target=(system, partition|None)`` pins it to that resource."""
        target = self.validate_target(target)
        source, rationale, confidence = "user", "", None
        if isinstance(envelope, dict):
            envelope = ResourceEnvelope.model_validate(envelope)
        if envelope is None or auto_generate_envelope:
            envelope, rationale, source, confidence = generate_envelope(name, description, command)
        spec = TaskSpec(name=name, description=description, command=command, container=container,
                        envelope=envelope, max_retries=max_retries, auto_continue=auto_continue)
        record = self.manager.submit(spec, envelope_source=source, envelope_rationale=rationale,
                                     envelope_confidence=confidence, target=target)
        return {"task": summarize_task(record), "envelope": envelope.model_dump(mode="json"),
                "envelope_rationale": rationale}

    def tasks(self, status: Optional[str] = None) -> list[dict]:
        records = sorted(self.manager.tasks.values(), key=lambda r: r.created_at, reverse=True)
        if status == "active":
            records = [r for r in records if r.status not in TERMINAL]
        elif status:
            records = [r for r in records if r.status.value == status.upper()]
        return [summarize_task(r) for r in records]

    def task(self, task_id: str) -> Optional[dict]:
        record = self.manager.tasks.get(task_id)
        return record.model_dump(mode="json") if record else None

    def cancel(self, task_id: str) -> Optional[dict]:
        record = self.manager.cancel(task_id)
        return summarize_task(record) if record else None

    def replan(self, task_id: str, exclude: Optional[list[str]] = None, target: Target = None,
               clear_target: bool = False) -> Optional[dict]:
        record = self.manager.replan(task_id, exclude=exclude,
                                     target=self.validate_target(target), clear_target=clear_target)
        return summarize_task(record) if record else None

    def wait(self, task_id: str, timeout_s: float = 3600, poll_s: float = 2.0) -> dict:
        """Block until the task is terminal (in-process; the CLI/MCP poll over HTTP)."""
        deadline = time.time() + timeout_s
        while True:
            record = self.manager.tasks.get(task_id)
            if record is None:
                raise KeyError(task_id)
            if record.status in TERMINAL or time.time() >= deadline:
                return summarize_task(record)
            time.sleep(poll_s)

    def submit_group(self, name: str, children: list[dict], rounds: int = 1,
                     distinct_systems: bool = True, description: str = "") -> dict:
        specs = []
        for child in children:
            env = child.get("envelope")
            if isinstance(env, dict):
                env = ResourceEnvelope.model_validate(env)
            if env is None:
                env, _, _, _ = generate_envelope(child["name"], child.get("description", ""),
                                                 child.get("command", ""))
            specs.append(TaskSpec(name=child["name"], description=child.get("description", ""),
                                  command=child.get("command", "echo"), container=child.get("container"),
                                  envelope=env, max_retries=child.get("max_retries", 2),
                                  auto_continue=child.get("auto_continue", True)))
        group = self.manager.submit_group(name, specs, rounds=rounds,
                                          distinct_systems=distinct_systems, description=description)
        return group.model_dump(mode="json")

    def groups(self) -> list[dict]:
        out = []
        for g in sorted(self.manager.groups.values(), key=lambda g: g.created_at, reverse=True):
            children = [tid for round_ids in g.round_children for tid in round_ids]
            out.append({**g.model_dump(mode="json"),
                        "children": [summarize_task(self.manager.tasks[t])
                                     for t in children if t in self.manager.tasks]})
        return out

    def events(self, limit: int = 100, task_id: Optional[str] = None) -> list[dict]:
        return self.store.events(limit=limit, task_id=task_id)
