"""Production preflight for live mode: verify the whole chain before serving.

Checks, in order:
  1. globus-compute-sdk import + credentials (auth mode, identity)
  2. site catalog loads and has no TODO placeholders
  3. every configured endpoint is visible and online, hostname matches
  4. a real probe roundtrip per system (sinfo/squeue/sacctmgr via ShellFunction),
     with timing — this is the minute-level refresh RQ1 will measure
  5. optionally (--submit-test SYSTEM) one real sbatch smoke job: minimal
     resources, echoes a marker, then is scancel'ed immediately

Usage:
  python -m diamond_agent.preflight [--sites sites.live.yaml] [--submit-test Delta@NCSA]

Exit code 0 only if every non-optional check passes.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import yaml

OK, BAD, WARN = "  ok ", "FAIL ", "warn "


class Preflight:
    def __init__(self) -> None:
        self.failures = 0

    def report(self, level: str, message: str) -> None:
        if level == BAD:
            self.failures += 1
        print(f"[{level.strip():>4}] {message}")


def check_auth(pf: Preflight):
    try:
        from . import auth
    except ImportError as exc:
        pf.report(BAD, f"globus-compute-sdk not importable: {exc} (pip install .[live])")
        return None
    try:
        client = auth.build_compute_client()
        client.version_check()
        pf.report(OK, f"authenticated (mode: {auth.auth_mode()})")
    except Exception as exc:
        pf.report(BAD, f"authentication failed: {exc} — run `python -m diamond_agent.auth login`")
        return None
    pf.report(OK, f"identity: {auth.identity_description()}")
    return client


def check_catalog(pf: Preflight, sites_path: str):
    path = Path(sites_path)
    if not path.exists():
        pf.report(BAD, f"site catalog {sites_path} not found — "
                       "run `python -m diamond_agent.auth init-sites` and fill it in")
        return None
    text = path.read_text()
    catalog = yaml.safe_load(text)
    systems = catalog.get("systems") or []
    if not systems:
        pf.report(BAD, f"{sites_path} has no systems")
        return None
    todo_lines = [f"L{i}" for i, line in enumerate(text.splitlines(), 1)
                  if "TODO" in line and not line.lstrip().startswith("#")]
    if todo_lines:
        pf.report(BAD, f"{sites_path} still contains TODO placeholders "
                       f"({', '.join(todo_lines)}) — usually the allocation accounts; "
                       "find yours with: sacctmgr show associations user=$USER format=Account")
    for s in systems:
        parts = s.get("partitions") or []
        if not parts:
            pf.report(BAD, f"{s.get('name')}: no partitions configured")
        if str(s.get("endpoint", "")).startswith("sim-"):
            pf.report(BAD, f"{s.get('name')}: endpoint is a placeholder, not a Globus Compute UUID ({s.get('endpoint')}) — "
                           "point it at a real Globus Compute endpoint UUID")
    pf.report(OK, f"catalog: {len(systems)} system(s), "
                  f"{sum(len(s.get('partitions') or []) for s in systems)} partition(s)")
    return catalog


def check_endpoints(pf: Preflight, client, catalog) -> None:
    from .auth import resolve_host

    for s in catalog["systems"]:
        name, endpoint = s["name"], s["endpoint"]
        try:
            status = client.get_endpoint_status(endpoint_uuid=endpoint)["status"]
        except Exception as exc:
            pf.report(BAD, f"{name}: endpoint {endpoint} unreachable: {exc}")
            continue
        level = OK if status == "online" else BAD
        pf.report(level, f"{name}: endpoint status {status}")
        try:
            metadata = client.get_endpoint_metadata(endpoint_uuid=endpoint) or {}
            hostname = metadata.get("hostname") or ""
            machine = resolve_host(hostname)
            if machine not in ("unknown", name):
                pf.report(WARN, f"{name}: hostname {hostname} maps to {machine} — "
                               "check the endpoint UUID is on the system you think it is")
            elif hostname:
                pf.report(OK, f"{name}: hostname {hostname}")
        except Exception:
            pf.report(WARN, f"{name}: endpoint metadata unavailable")


def check_probe(pf: Preflight, catalog) -> None:
    from .federation.globus_probe import GlobusFederationSource
    from .schemas import EndpointHealth, utcnow

    source = GlobusFederationSource(catalog)
    for system in catalog["systems"]:
        name = system["name"]
        started = time.perf_counter()
        try:
            records = source._probe_system(source._ensure_client(), system, utcnow())
        except Exception as exc:
            pf.report(BAD, f"{name}: probe raised: {exc}")
            continue
        elapsed = time.perf_counter() - started
        degraded = [r for r in records if r.endpoint_health != EndpointHealth.ONLINE]
        if degraded:
            pf.report(BAD, f"{name}: probe degraded ({elapsed:.1f}s) — "
                           "check user_endpoint_config / endpoint logs")
            continue
        live = [r for r in records if r.nodes_total > 0]
        pf.report(OK, f"{name}: probe ok in {elapsed:.1f}s — "
                      f"{len(records)} partition record(s), "
                      f"{sum(r.nodes_idle for r in records)} idle node(s), "
                      f"live sinfo data on {len(live)}/{len(records)}")
        if elapsed > 55:
            pf.report(WARN, f"{name}: probe took {elapsed:.0f}s — a 60s tick will lag; "
                           "raise DIAMOND_AGENT_TICK")


def submit_smoke_test(pf: Preflight, catalog, system_name: str) -> None:
    from .execution.globus import GlobusExecutor
    from .scheduler.meta import MetaScheduler
    from .federation.globus_probe import GlobusFederationSource
    from .schemas import ResourceEnvelope, ResourceSnapshot, TaskSpec

    spec = TaskSpec(
        name=f"da-preflight-{int(time.time())}",
        description="diamond agent preflight smoke test",
        command="echo DIAMOND_AGENT_PREFLIGHT_OK && hostname",
        envelope=ResourceEnvelope.model_validate({
            "gpu": {"min_count": 0, "max_count": 0},
            "cpu": {"min_count": 1, "max_count": 1},
            "memory": {"min": "1GB", "max": "4GB"},
            "walltime": {"min": "5m", "max": "5m"},
            "compatibility": {"accelerators": []},
            "objective": "min_queue_impact",
        }),
    )
    source = GlobusFederationSource(catalog)
    records = [r for r in source.probe() if r.system == system_name]
    if not records:
        pf.report(BAD, f"smoke test: no records for {system_name}")
        return
    scheduler = MetaScheduler(catalog.get("policy", {}), use_ai=False)
    decision = scheduler.decide(spec, ResourceSnapshot(records=records))
    if decision.chosen is None:
        pf.report(BAD, f"smoke test: no feasible partition on {system_name} for a 1-CPU job")
        return
    executor = GlobusExecutor(catalog)
    try:
        handle = executor.submit(spec, decision.chosen)
        pf.report(OK, f"smoke test: sbatch accepted, SLURM job {handle.job_id} on "
                      f"{decision.chosen.system}:{decision.chosen.partition}")
    except Exception as exc:
        pf.report(BAD, f"smoke test: submission failed: {exc}")
        return
    state, note = executor.status(handle.job_id)
    pf.report(OK, f"smoke test: sacct reports {state} ({note})")
    if executor.cancel(handle.job_id):
        pf.report(OK, f"smoke test: job {handle.job_id} cancelled (no SUs consumed)")
    else:
        pf.report(WARN, f"smoke test: could not cancel job {handle.job_id} — "
                       "it will exit on its own within 5m walltime")


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv(override=False)
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sites", default="sites.live.yaml")
    parser.add_argument("--submit-test", metavar="SYSTEM", default=None,
                        help="also submit+cancel one minimal real job on SYSTEM")
    args = parser.parse_args()

    pf = Preflight()
    client = check_auth(pf)
    catalog = check_catalog(pf, args.sites)
    if client is not None and catalog is not None and pf.failures == 0:
        check_endpoints(pf, client, catalog)
        if pf.failures == 0:
            check_probe(pf, catalog)
        if args.submit_test and pf.failures == 0:
            submit_smoke_test(pf, catalog, args.submit_test)

    print()
    if pf.failures:
        print(f"preflight FAILED ({pf.failures} problem(s)) — fix before going live")
        sys.exit(1)
    print("preflight passed — start the service with:")
    print(f"  DIAMOND_AGENT_SITES={args.sites} "
          "uv run uvicorn diamond_agent.main:app --port 8642")


if __name__ == "__main__":
    main()
