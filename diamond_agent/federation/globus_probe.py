"""Live-mode federation source: probe real systems through Globus Compute.

Mirrors the diamond-admin-backend pattern: one ShellFunction per endpoint runs
a heredoc'd Python program on the login node, gathers sinfo/squeue/sacctmgr
output, and prints a single JSON blob. Static per-node shape (GPU memory,
cpus/mem per node, container runtime) comes from the site catalog; dynamic
queue state comes from the probe. Requires ``pip install .[live]`` and a
Globus login (globus_compute_sdk handles the native-app flow interactively).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from ..schemas import EndpointHealth, ResourceRecord, parse_duration_minutes, utcnow

logger = logging.getLogger(__name__)

# Runs on the HPC login node via ShellFunction. Prints one JSON document:
#   {"partitions": {name: {"idle": n, "alloc": n, "down": n, "max_walltime": "2-00:00:00"}},
#    "queue": {name: {"running": n, "pending": n}},
#    "user_queue": {name: {"running": n, "pending": n}},
#    "accounts": [...], "arch": "x86_64", "slow": [commands that hit their timeout]}
# __GPU_PARTITIONS__ is the one run-time argument (see _probe_shell): the Slurm
# partitions worth a per-node GRES query, or "" to skip it.
PROBE_CMD = r"""
export DA_GPU_PARTITIONS="__GPU_PARTITIONS__"
PYTHON_CMD="$(command -v python3 || command -v python || true)"
"$PYTHON_CMD" <<'PYTHON'
import json, os, platform, subprocess

slow = []
def run(cmd, timeout=60):
    # stdout/stderr PIPE + universal_newlines instead of capture_output/text:
    # login nodes may only offer python 3.6 (e.g. Anvil's /usr/bin/python3).
    try:
        p = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        # One slow query (per-node GRES on a 5,000-node system such as
        # Perlmutter) must degrade its own counter, not kill the whole probe.
        slow.append(cmd.split(" -O ")[0][:80])
        return ""
    return p.stdout if p.returncode == 0 else ""

partitions = {}
for line in run('sinfo -h -o "%R|%D|%T|%l"').splitlines():
    try:
        part, nodes, state, tlimit = line.strip().split("|")
        nodes = int(nodes)
    except ValueError:
        continue
    part = part.rstrip("*")
    rec = partitions.setdefault(part, {"idle": 0, "mixed": 0, "alloc": 0, "down": 0,
                                       "max_walltime": tlimit})
    # Strip every Slurm node-state suffix flag (* ~ # % $ @ + - !), e.g.
    # Anvil reports busy nodes as "mixed-".
    s = state.rstrip("*~#%$@+-!")
    if s == "idle":
        rec["idle"] += nodes
    elif s in ("mix", "mixed"):
        rec["mixed"] += nodes          # shared nodes with free capacity
    elif s in ("alloc", "allocated", "completing", "comp"):
        rec["alloc"] += nodes
    else:
        rec["down"] += nodes           # down/drained/draining/reserved/planned/...

def count_queue(extra=""):
    out = {}
    for line in run(f'squeue -h {extra} -o "%P|%T"').splitlines():
        try:
            part, state = line.strip().split("|")
        except ValueError:
            continue
        rec = out.setdefault(part.rstrip("*"), {"running": 0, "pending": 0})
        if state == "RUNNING":
            rec["running"] += 1
        elif state == "PENDING":
            rec["pending"] += 1
    return out

# Free CPUs per partition: %C = alloc/idle/other/total.
cpu_free = {}
for line in run('sinfo -h -o "%R|%C"').splitlines():
    try:
        part, counts = line.strip().split("|")
        alloc_c, idle_c, other_c, total_c = counts.split("/")
        cpu_free[part.rstrip("*")] = cpu_free.get(part.rstrip("*"), 0) + int(idle_c)
    except ValueError:
        continue

# Free GPUs per partition: per-node Gres vs GresUsed on usable (non-down) nodes.
def gpu_count(token):
    total = 0
    for entry in token.split(","):
        if entry.startswith("gpu"):
            head = entry.split("(")[0]
            try:
                total += int(head.split(":")[-1])
            except ValueError:
                pass
    return total

gpu_free = {}
usable_states = ("idle", "mix", "mixed", "alloc", "allocated", "completing", "comp")
# The per-node listing is the expensive query on big systems: ask only for the
# GPU partitions, cap it at 30 s, and let free_units() fall back to idle nodes x
# GPUs per node when it is skipped or times out (gres_probe: false skips it).
gpu_parts = os.environ.get("DA_GPU_PARTITIONS", "").strip()
gres_lines = run('sinfo -h -N -p %s -O "Partition:40,StateCompact:14,Gres:80,GresUsed:80"' % gpu_parts,
                 timeout=30).splitlines() if gpu_parts else []
for line in gres_lines:
    fields = line.split()
    if len(fields) < 3 or "gpu" not in fields[2]:
        continue
    part = fields[0].rstrip("*")
    state = fields[1].rstrip("*~#%$@+-!")
    if state not in usable_states:
        continue
    total = gpu_count(fields[2])
    used = gpu_count(fields[3]) if len(fields) > 3 else 0
    gpu_free[part] = gpu_free.get(part, 0) + max(0, total - used)

accounts = [a.strip() for a in run(
    "sacctmgr show associations --noheader -P user=$USER format=Account").splitlines() if a.strip()]

print(json.dumps({
    "partitions": partitions,
    "queue": count_queue(),
    "user_queue": count_queue("-u $USER"),
    "cpu_free": cpu_free,
    "gpu_free": gpu_free,
    "accounts": accounts,
    "arch": platform.machine(),
    "slow": slow,
}))
PYTHON
"""


def _probe_shell() -> str:
    """PROBE_CMD with its braces escaped for ShellFunction's ``.format()`` and
    the one real format field put back."""
    return (PROBE_CMD.replace("{", "{{").replace("}", "}}")
            .replace("__GPU_PARTITIONS__", "{gpu_partitions}"))


def gres_probe_partitions(system: dict) -> str:
    """Comma-joined Slurm partitions to GRES-probe on this system: its GPU
    partitions (by slurm_partition or name), or "" when the system opts out
    with ``gres_probe: false`` or has no GPU partition."""
    if not system.get("gres_probe", True):
        return ""
    names = {str(p.get("slurm_partition") or p["name"]) for p in system.get("partitions") or []
             if (p.get("gpus_per_node") or 0) > 0}
    return ",".join(sorted(re.sub(r"[^A-Za-z0-9_.-]", "", n) for n in names if n))


class GlobusFederationSource:
    """probe() fans out the PROBE_CMD to every configured endpoint."""

    def __init__(self, catalog: dict):
        self.catalog = catalog
        self._client = None
        self._probe_fn_id: Optional[str] = None
        self.last_probe_calls = 0  # Globus Compute API calls in the last probe()

    def _ensure_client(self):
        from ..auth import build_compute_client, client_generation

        # Credentials can change at runtime (web sign-in / sign-out): rebuild
        # the client — and re-register the probe function — on a new generation.
        if self._client is None or getattr(self, "_client_gen", None) != client_generation():
            from globus_compute_sdk import ShellFunction

            self._client = build_compute_client()
            self._client_gen = client_generation()
            fn = ShellFunction(_probe_shell())
            self._probe_fn_id = self._client.register_function(fn)
        return self._client

    def probe(self) -> list[ResourceRecord]:
        client = self._ensure_client()
        now = utcnow()
        self.last_probe_calls = 0
        records: list[ResourceRecord] = []
        for system in self.catalog["systems"]:
            records.extend(self._probe_system(client, system, now))
        return records

    def _probe_system(self, client, system: dict, now) -> list[ResourceRecord]:
        name, endpoint = system["name"], system["endpoint"]
        static = {p["name"]: p for p in system["partitions"]}
        try:
            self.last_probe_calls += 1
            status = client.get_endpoint_status(endpoint_uuid=endpoint)["status"]
            health = EndpointHealth.ONLINE if status == "online" else EndpointHealth.OFFLINE
        except Exception:
            logger.exception("endpoint status failed for %s", name)
            health = EndpointHealth.OFFLINE

        data: dict[str, Any] = {}
        if health == EndpointHealth.ONLINE:
            try:
                # MEPs need per-user worker config (account/partition) to place
                # the probe worker — same create_batch path diamond uses.
                batch = client.create_batch()
                batch.add(self._probe_fn_id,
                          kwargs={"gpu_partitions": gres_probe_partitions(system)})
                self.last_probe_calls += 1
                task_id = client.batch_run(endpoint, batch)["tasks"][self._probe_fn_id][0]
                result = self._wait(client, task_id)
                stdout_lines = (getattr(result, "stdout", "") or "").strip().splitlines()
                if not stdout_lines:
                    stderr = (getattr(result, "stderr", "") or "")[-300:]
                    raise ValueError(f"probe produced no stdout; stderr tail: {stderr!r}")
                data = json.loads(stdout_lines[-1])
                if data.get("slow"):
                    logger.warning("probe on %s: %d command(s) hit their timeout, counters "
                                   "degraded to node states: %s", name, len(data["slow"]),
                                   "; ".join(data["slow"]))
            except Exception:
                logger.exception("probe failed for %s", name)
                health = EndpointHealth.DEGRADED

        default_account = (system.get("accounts") or data.get("accounts") or ["default"])[0]
        out = []
        for pname, shape in static.items():
            # A catalog entry may be a logical partition that lives inside a
            # Slurm partition of another name (NERSC: gpu -> gpu_ss11); the
            # live counters are keyed by what sinfo/squeue print.
            sname = shape.get("slurm_partition") or pname
            live = (data.get("partitions") or {}).get(sname, {})
            q = (data.get("queue") or {}).get(sname, {})
            uq = (data.get("user_queue") or {}).get(sname, {})
            out.append(
                ResourceRecord(
                    timestamp=now,
                    endpoint=endpoint,
                    system=name,
                    partition=pname,
                    # GPU and CPU partitions often bill different sub-accounts
                    # (e.g. proj-gpu vs proj-cpu) — partition wins.
                    account=shape.get("account") or default_account,
                    node_arch=shape.get("arch") or data.get("arch", "x86_64"),
                    gpu_type=shape.get("gpu_type"),
                    gpus_per_node=shape.get("gpus_per_node", 0),
                    gpu_mem_gb=shape.get("gpu_mem_gb", 0),
                    cpus_per_node=shape.get("cpus_per_node", 0),
                    mem_per_node_gb=shape.get("mem_per_node_gb", 0),
                    nodes_idle=live.get("idle", 0),
                    nodes_mixed=live.get("mixed", 0),
                    nodes_allocated=live.get("alloc", 0),
                    nodes_down=live.get("down", 0),
                    gpus_free=(data.get("gpu_free") or {}).get(sname),
                    cpus_free=(data.get("cpu_free") or {}).get(sname),
                    running_jobs=q.get("running", 0),
                    pending_jobs=q.get("pending", 0),
                    user_running_jobs=uq.get("running", 0),
                    user_pending_jobs=uq.get("pending", 0),
                    # Effective cap = min(live partition limit, catalog policy
                    # cap): the catalog value is a per-partition budget guard,
                    # not just a fallback (live sinfo often says "infinite").
                    max_walltime_minutes=min(
                        (c for c in (
                            parse_duration_minutes(live.get("max_walltime")),
                            parse_duration_minutes(shape.get("max_walltime")),
                        ) if c is not None),
                        default=None,
                    ),
                    endpoint_health=health,
                    container_runtime=system.get("container_runtime"),
                )
            )
        return out

    def _wait(self, client, task_id: str, max_attempts: int = 20):
        # ~210s total: login-node endpoints answer in seconds; the margin covers
        # first probe result can arrive.
        import time

        from globus_compute_sdk.errors import TaskPending

        for attempt in range(max_attempts):
            try:
                self.last_probe_calls += 1
                return client.get_result(task_id)
            except TaskPending:
                time.sleep(attempt + 1)
        raise TimeoutError(f"probe task {task_id} did not finish")
