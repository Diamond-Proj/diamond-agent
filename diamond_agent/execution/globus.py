"""Live executor: submit placed tasks through Globus Compute (diamond pattern).

Mirrors diamond-admin-backend's task_runtime: render a submit script with
Jinja, wrap in a ShellFunction (escaping braces), register, batch_run on the
system's login-node endpoint, poll for the sbatch result, and parse the SLURM job id
out of stdout. Status checks run `sacct` remotely the same way.

Requires ``pip install .[live]`` and a Globus login. Untested in the demo —
kept as the reference implementation for real deployments.
"""

from __future__ import annotations

import logging
import re
import time
from importlib import resources

import jinja2

from ..schemas import Candidate, TaskSpec
from .base import JobHandle, walltime_hms

logger = logging.getLogger(__name__)

SBATCH_JOB_ID_PATTERN = re.compile(r"Submitted batch job (\d+)")


def _shq(s: str) -> str:
    """POSIX single-quote shell escaping."""
    return "'" + s.replace("'", "'\\''") + "'"

SLURM_STATE_MAPPING = {
    "COMPLETED": "COMPLETED", "COMPLETING": "COMPLETED", "CANCELLED": "CANCELLED",
    "FAILED": "FAILED", "TIMEOUT": "FAILED", "OUT_OF_MEMORY": "FAILED",
    "RUNNING": "RUNNING", "PENDING": "PENDING",
}


def _load_template() -> jinja2.Template:
    text = resources.files("diamond_agent.execution").joinpath("templates/submit.sh.j2").read_text()
    return jinja2.Environment(undefined=jinja2.StrictUndefined).from_string(text)


class GlobusExecutor:
    def __init__(self, catalog: dict, workdir: str = "$HOME/diamond"):
        self.catalog = catalog
        self.workdir = workdir  # per-system override: sites.live.yaml `diamond_dir`
        self._client = None
        # slurm job id -> endpoint uuid
        self._job_endpoint: dict[str, str] = {}

    def _ensure_client(self):
        from ..auth import build_compute_client, client_generation

        if self._client is None or getattr(self, "_client_gen", None) != client_generation():
            self._client = build_compute_client()
            self._client_gen = client_generation()
        return self._client

    def _system_entry(self, system: str) -> dict:
        for s in self.catalog["systems"]:
            if s["name"] == system:
                return s
        raise KeyError(system)

    def _partition_entry(self, system: str, partition: str) -> dict:
        for p in self._system_entry(system).get("partitions") or []:
            if p.get("name") == partition:
                return p
        return {}

    def bind_job(self, job_id: str, system: str) -> None:
        """Re-attach a persisted job to its endpoint after a restart, so
        status/cancel keep working across agent restarts."""
        try:
            entry = self._system_entry(system)
        except KeyError:
            return
        self._job_endpoint[job_id] = entry["endpoint"]

    def _run_shell(self, endpoint: str, cmd: str):
        """Run one shell snippet on a self-owned (login-node) endpoint.

        Multi-user endpoints are deliberately unsupported: they need per-user
        worker placement (account, partition, worker_init) and their workers
        ride a batch queue, which defeats minute-level probing.
        """
        from globus_compute_sdk import ShellFunction
        from globus_compute_sdk.errors import TaskPending

        client = self._ensure_client()
        fn = ShellFunction(cmd.replace("{", "{{").replace("}", "}}"))
        fn_id = client.register_function(fn)
        batch = client.create_batch()
        batch.add(fn_id)
        task_id = client.batch_run(endpoint, batch)["tasks"][fn_id][0]
        for attempt in range(12):
            try:
                return client.get_result(task_id)
            except TaskPending:
                time.sleep(attempt + 1)
        raise TimeoutError(f"globus compute task {task_id} still pending")

    def workdir_for(self, system: str) -> str:
        return self._system_entry(system).get("diamond_dir") or self.workdir

    def run_shell(self, system: str, command: str, timeout_s: int = 60):
        """Bounded command on the system's endpoint (login node for login-node
        endpoints; a worker for MEPs). ``timeout`` (coreutils) caps the remote
        run; the Globus round-trip is polled for ~80 s on top."""
        entry = self._system_entry(system)
        bounded = f"timeout {int(timeout_s)} bash -c {_shq(command)} 2>&1"
        return self._run_shell(entry["endpoint"], bounded)

    def submit(self, task: TaskSpec, placement: Candidate) -> JobHandle:
        sizing = placement.sizing
        entry = self._system_entry(placement.system)
        workdir = entry.get("diamond_dir") or self.workdir
        record = placement.record
        nodes = sizing.nodes if sizing else 1
        # GPU partitions on sites like Delta reject jobs that request no GPUs,
        # so on a GPU partition always ask for at least one per node. Sites
        # that allocate whole nodes and reject gres directives entirely (TACC
        # Vista: "Invalid generic resource specification") set
        # gpu_directive: false.
        if record.gpus_per_node > 0 and entry.get("gpu_directive", True):
            per_node = -(-max(sizing.gpus if sizing else 0, 1) // nodes)
            gpus_per_node = min(max(per_node, 1), record.gpus_per_node)
        else:
            gpus_per_node = 0
        # Request the envelope's memory floor, not the whole node. Sites that
        # allocate whole nodes and reject --mem (TACC) set mem_directive: false.
        mem_gb = (int(min(task.envelope.memory.min_gb, record.mem_per_node_gb))
                  if task.envelope.memory.min_gb and entry.get("mem_directive", True)
                  else 0)
        # The submit file is written through an unquoted heredoc on the login
        # node (so $HOME in paths expands there); escape the task command so
        # its own $vars, $(...) and backticks survive until the job runs.
        command = (task.command.replace("\\", "\\\\").replace("$", "\\$")
                   .replace("`", "\\`"))
        # CPU cores must be requested explicitly (Slurm default is 1 core).
        cpus_per_task = 0
        if sizing and sizing.cpus > 0:
            cpus_per_task = min(-(-sizing.cpus // nodes), record.cpus_per_node or sizing.cpus)
        script = _load_template().render(
            task_name=task.name,
            workdir=workdir,
            cpus_per_task=cpus_per_task,
            nodes=nodes,
            walltime=walltime_hms(sizing.walltime_minutes if sizing else 60),
            partition=placement.partition,
            account=placement.account,
            # Sites like Anvil bind each account to a mandatory QOS.
            qos=self._partition_entry(placement.system, placement.partition).get("qos"),
            gpus_per_node=gpus_per_node,
            mem_gb=mem_gb,
            container=task.container,
            container_module_command="",
            command=command,
        )
        endpoint = entry["endpoint"]
        result = self._run_shell(endpoint, script)
        stdout = getattr(result, "stdout", "") or ""
        match = SBATCH_JOB_ID_PATTERN.search(stdout)
        if not match:
            stderr = getattr(result, "stderr", "") or ""
            returncode = getattr(result, "returncode", "?")
            raise RuntimeError(
                f"could not parse SLURM job id from sbatch output "
                f"(rc={returncode}, stdout={stdout!r}, stderr={stderr[-500:]!r})")
        job_id = match.group(1)
        self._job_endpoint[job_id] = endpoint
        return JobHandle(job_id, placement.system, placement.partition)

    def status(self, job_id: str) -> tuple[str, str]:
        endpoint = self._job_endpoint.get(job_id)
        if endpoint is None:
            return "UNKNOWN", "no endpoint recorded for job"
        result = self._run_shell(endpoint, f"sacct -j {job_id} -o State -n | head -n 1")
        raw = (getattr(result, "stdout", "") or "").strip().split()[0] if getattr(result, "stdout", "") else ""
        state = SLURM_STATE_MAPPING.get(raw.rstrip("+"), "UNKNOWN")
        return state, f"sacct: {raw}" if raw else "sacct returned no state"

    def cancel(self, job_id: str) -> bool:
        endpoint = self._job_endpoint.get(job_id)
        if endpoint is None:
            return False
        self._run_shell(endpoint, f"scancel {job_id}")
        return True
