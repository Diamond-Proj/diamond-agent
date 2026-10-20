"""SshServerProvider — RESERVED: single Linux GPU servers over OpenSSH.

Design (RackTop-style, agent-less): the local machine opens an OpenSSH
session with the user's own keys / ``~/.ssh/config`` / ProxyJump, runs
``nvidia-smi`` + ``/proc`` reads, and maps the result onto the federation's
common ``ResourceRecord`` — one record per server, ``partition="local"``,
``nodes_total=1``, ``gpus_free`` = GPUs whose memory use is below the idle
threshold. Execution is ``nohup`` + a managed log path; the pid is the job id.

What is wired now
  * catalog schema (``ssh_servers:`` in sites.live.yaml) and system listing
  * the probe command and its parser (``parse_probe``) — pure functions
  * provider status "reserved" until ``enabled: true`` is set on a server

What is NOT yet verified
  * live probing/execution against a real server — ``enabled`` servers run
    the code path below, but it has not been exercised in this repo's
    evaluation; ``submit`` raises ProviderNotReady unless
    ``execution: true`` is also set, so nothing runs silently.

Catalog example::

    ssh_servers:
      - name: lab-a100          # display name == system name
        host: 10.0.0.10
        port: 22
        user: researcher
        identity_file: ~/.ssh/id_ed25519   # optional; ssh config / agent otherwise
        proxy_jump: bastion               # optional
        tags: [lab, a100]
        enabled: false
        execution: false
"""

from __future__ import annotations

import shlex
import subprocess
from typing import Optional

from ..execution.base import JobHandle
from ..schemas import (Candidate, EndpointHealth, ProviderKind, ResourceRecord,
                       TaskSpec, utcnow)
from .base import ProviderInfo, ProviderNotReady, ShellResult, SystemInfo

IDLE_GPU_MEM_FRACTION = 0.01     # RackTop rule: <1% memory used == unused GPU

# Single round-trip probe. Sections are delimited so the parser stays simple.
PROBE_CMD = r"""
echo '@@HOST'; hostname; uname -m
echo '@@CPU'; nproc; awk '/MemTotal|MemAvailable/ {print $1, $2}' /proc/meminfo; cat /proc/loadavg
echo '@@GPU'
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits
else
  echo NO_NVIDIA_SMI
fi
"""


def parse_probe(text: str) -> dict:
    """Parse PROBE_CMD output into a plain dict (testable without SSH)."""
    section, out = None, {"host": None, "arch": "x86_64", "cpus": 0, "mem_total_gb": 0.0,
                          "mem_avail_gb": 0.0, "load1": 0.0, "gpus": [], "nvidia_smi": True}
    host_lines: list[str] = []
    cpu_lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("@@"):
            section = line[2:]
            continue
        if section == "HOST":
            host_lines.append(line)
        elif section == "CPU":
            cpu_lines.append(line)
        elif section == "GPU":
            if line == "NO_NVIDIA_SMI":
                out["nvidia_smi"] = False
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5:
                try:
                    out["gpus"].append({
                        "index": int(parts[0]), "name": parts[1],
                        "mem_used_mb": float(parts[2]), "mem_total_mb": float(parts[3]),
                        "util": float(parts[4])})
                except ValueError:
                    continue
    if host_lines:
        out["host"] = host_lines[0]
        if len(host_lines) > 1:
            out["arch"] = "aarch64" if host_lines[1] in ("aarch64", "arm64") else host_lines[1]
    if cpu_lines:
        try:
            out["cpus"] = int(cpu_lines[0])
        except ValueError:
            pass
        for l in cpu_lines[1:]:
            k, _, v = l.partition(" ")
            if k.startswith("MemTotal"):
                out["mem_total_gb"] = float(v) / 1024 / 1024
            elif k.startswith("MemAvailable"):
                out["mem_avail_gb"] = float(v) / 1024 / 1024
        try:
            out["load1"] = float(cpu_lines[-1].split()[0])
        except (ValueError, IndexError):
            pass
    return out


def probe_to_record(server: dict, parsed: Optional[dict], error: Optional[str] = None) -> ResourceRecord:
    """Map one server's probe onto the common record shape."""
    name = server["name"]
    if parsed is None:
        return ResourceRecord(
            timestamp=utcnow(), endpoint=server.get("host", ""), system=name,
            partition="local", account=server.get("user", ""), node_arch="x86_64",
            provider=ProviderKind.SSH, tags=list(server.get("tags", [])),
            nodes_down=1, endpoint_health=EndpointHealth.OFFLINE)
    gpus = parsed["gpus"]
    free = [g for g in gpus if g["mem_total_mb"] and
            g["mem_used_mb"] / g["mem_total_mb"] < IDLE_GPU_MEM_FRACTION]
    gpu_type = None
    if gpus:
        gpu_type = gpus[0]["name"].replace("NVIDIA ", "").replace("GeForce ", "").strip()
    busy_cores = min(parsed["cpus"], round(parsed["load1"]))
    return ResourceRecord(
        timestamp=utcnow(), endpoint=server.get("host", ""), system=name,
        partition="local", account=server.get("user", ""), node_arch=parsed["arch"],
        provider=ProviderKind.SSH, tags=list(server.get("tags", [])),
        gpu_type=gpu_type, gpus_per_node=len(gpus),
        gpu_mem_gb=round(gpus[0]["mem_total_mb"] / 1024, 1) if gpus else 0.0,
        cpus_per_node=parsed["cpus"], mem_per_node_gb=round(parsed["mem_total_gb"], 1),
        nodes_idle=1 if (not gpus or len(free) == len(gpus)) and busy_cores == 0 else 0,
        nodes_mixed=0 if (not gpus or len(free) == len(gpus)) and busy_cores == 0 else 1,
        gpus_free=len(free), cpus_free=max(parsed["cpus"] - busy_cores, 0),
        running_jobs=len(gpus) - len(free), pending_jobs=0,
        max_walltime_minutes=None, endpoint_health=EndpointHealth.ONLINE)


def ssh_argv(server: dict, remote_cmd: str) -> list[str]:
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            "-o", "StrictHostKeyChecking=accept-new"]
    if server.get("port"):
        argv += ["-p", str(server["port"])]
    if server.get("identity_file"):
        argv += ["-i", server["identity_file"]]
    if server.get("proxy_jump"):
        argv += ["-J", server["proxy_jump"]]
    target = f"{server['user']}@{server['host']}" if server.get("user") else server["host"]
    return argv + [target, remote_cmd]


class SshServerProvider:
    kind = ProviderKind.SSH
    label = "SSH servers (reserved)"

    def __init__(self, servers: list[dict], workdir: str = "$HOME/diamond"):
        self.servers = list(servers)
        self.workdir = workdir
        self.last_probe_calls = 0
        self._by_name = {s["name"]: s for s in self.servers}

    def systems(self) -> list[SystemInfo]:
        return [SystemInfo(name=s["name"], provider=self.kind, endpoint=s.get("host", ""),
                           partitions=["local"], tags=list(s.get("tags", [])),
                           location=s.get("location"), accounts=[s.get("user", "")])
                for s in self.servers]

    def info(self) -> ProviderInfo:
        enabled = [s["name"] for s in self.servers if s.get("enabled")]
        status = "active" if enabled else ("disabled" if self.servers else "reserved")
        return ProviderInfo(
            kind=self.kind, label=self.label, status=status,
            systems=[s["name"] for s in self.servers],
            detail=("agent-less OpenSSH probing; execution path not yet verified"
                    if enabled else "contract + catalog schema in place; add ssh_servers: "
                                    "with enabled: true to start probing"))

    # ---------------------------------------------------------------- probe

    def probe(self) -> list[ResourceRecord]:
        records = []
        self.last_probe_calls = 0
        for server in self.servers:
            if not server.get("enabled"):
                continue
            self.last_probe_calls += 1
            try:
                r = subprocess.run(ssh_argv(server, PROBE_CMD), capture_output=True,
                                   text=True, timeout=20)
                if r.returncode != 0:
                    records.append(probe_to_record(server, None, r.stderr[-200:]))
                    continue
                records.append(probe_to_record(server, parse_probe(r.stdout)))
            except Exception as exc:
                records.append(probe_to_record(server, None, str(exc)[:200]))
        return records

    # ------------------------------------------------------------ execution

    def _server(self, system: str) -> dict:
        try:
            return self._by_name[system]
        except KeyError:
            raise KeyError(f"unknown ssh server {system}")

    def submit(self, task: TaskSpec, placement: Candidate) -> JobHandle:
        server = self._server(placement.system)
        if not (server.get("enabled") and server.get("execution")):
            raise ProviderNotReady(
                f"SSH execution on {placement.system} is reserved (set enabled+execution "
                "in the catalog once the integration is verified)")
        gpus = placement.sizing.gpus if placement.sizing else 0
        log = f"{self.workdir}/logs/{task.name}.log"
        visible = ",".join(str(i) for i in range(gpus)) if gpus else ""
        remote = (f"mkdir -p {self.workdir}/logs && cd {self.workdir} && "
                  f"CUDA_VISIBLE_DEVICES={shlex.quote(visible)} nohup bash -lc "
                  f"{shlex.quote(task.command + '; echo EXIT=$? >> ' + log)} > {log} 2>&1 & echo $!")
        r = subprocess.run(ssh_argv(server, remote), capture_output=True, text=True, timeout=30)
        pid = (r.stdout or "").strip().splitlines()[-1] if r.stdout.strip() else ""
        if r.returncode != 0 or not pid.isdigit():
            raise RuntimeError(f"ssh launch failed on {placement.system}: {r.stderr[-300:]}")
        return JobHandle(f"{placement.system}:{pid}", placement.system, "local")

    def status(self, job_id: str) -> tuple[str, str, Optional[float]]:
        system, _, pid = job_id.partition(":")
        server = self._server(system)
        remote = (f"if kill -0 {pid} 2>/dev/null; then echo RUNNING; "
                  f"else grep -h 'EXIT=' {self.workdir}/logs/*.log 2>/dev/null | tail -1; fi")
        r = subprocess.run(ssh_argv(server, remote), capture_output=True, text=True, timeout=20)
        out = (r.stdout or "").strip()
        if out == "RUNNING":
            return "RUNNING", "process alive", None
        if out.startswith("EXIT="):
            return ("COMPLETED" if out == "EXIT=0" else "FAILED"), out, None
        return "UNKNOWN", r.stderr[-200:] or "no status", None

    def cancel(self, job_id: str) -> bool:
        system, _, pid = job_id.partition(":")
        server = self._server(system)
        r = subprocess.run(ssh_argv(server, f"kill {pid}"), capture_output=True, text=True, timeout=20)
        return r.returncode == 0

    def bind_job(self, job_id: str, system: str) -> None:
        pass                                    # nothing can be in flight: submit is reserved

    def shell(self, system: str, command: str, timeout_s: int = 60) -> ShellResult:
        server = self._server(system)
        if not server.get("enabled"):
            raise ProviderNotReady(f"SSH server {system} is not enabled in the catalog")
        r = subprocess.run(ssh_argv(server, f"timeout {int(timeout_s)} bash -c {shlex.quote(command)}"),
                           capture_output=True, text=True, timeout=timeout_s + 15)
        return ShellResult(returncode=r.returncode, stdout=r.stdout, stderr=r.stderr)

    def workdir(self, system: str) -> str:
        return self.workdir
