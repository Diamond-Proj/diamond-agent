"""`make doctor` — onboarding check that never prompts.

Walks the first-run chain in order and stops at the first missing piece with
the exact command to run next:

    install → .env → Globus credentials → endpoints on the supercomputers →
    site catalog → (preflight) → run

Usage: python -m diamond_agent.doctor [--sites sites.live.yaml]
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from pathlib import Path

OK, WARN, BAD = "ok ", "!! ", "XX "

# Login hosts of the machines Diamond Agent knows shapes for (data/known_machines.yaml).
KNOWN_LOGIN_HOSTS = {
    "Delta@NCSA": "login.delta.ncsa.illinois.edu",
    "DeltaAI@NCSA": "gh-login01.delta.ncsa.illinois.edu",
    "Anvil@RCAC": "anvil.rcac.purdue.edu",
    "Vista@TACC": "vista.tacc.utexas.edu",
    "Frontera@TACC": "frontera.tacc.utexas.edu",
    "Lonestar6@TACC": "ls6.tacc.utexas.edu",
    "Perlmutter@NERSC": "perlmutter.nersc.gov",
}


def endpoint_setup_hint(machines=None) -> str:
    rows = [(m, KNOWN_LOGIN_HOSTS.get(m, "<login host>")) for m in (machines or KNOWN_LOGIN_HOSTS)]
    lines = ["Create an endpoint ON each supercomputer you want in the pool (one-time, ~2 min):"]
    for m, host in rows:
        lines.append(f"  make endpoint-setup HOST=<your-user>@{host:<38}  # {m}")
    lines.append("  (runs scripts/hpc_endpoint_setup.sh on the login node: installs globus-compute-endpoint\n"
                 "   in ~/.gce-venv, starts the 'diamond-agent' endpoint, opens a Globus login — use the\n"
                 "   SAME account as `make login`; the endpoint keeps running after you log out)")
    return "\n".join(lines)


def endpoint_worker_cap(client, endpoint_uuid: str):
    """'capped' | 'uncapped' | None (unreadable / not a login-node endpoint).

    Endpoint 4.x runs every endpoint as a manager that spawns a user-endpoint
    process (UEP) from ``user_config_template.yaml.j2``; the engine block —
    and therefore ``max_workers_per_node`` — lives in that template, not in
    ``config.yaml``. Older single-user endpoints keep it in ``config.yaml``.
    """
    try:
        md = client.get_endpoint_metadata(endpoint_uuid=endpoint_uuid) or {}
    except Exception:
        return None
    import yaml

    import re

    def engine_of(text):
        try:
            if isinstance(text, str):        # UEP templates are Jinja: blank the placeholders
                text = re.sub(r"\{\{[^}]*\}\}", "null", text)
            cfg = yaml.safe_load(text) if isinstance(text, str) else text
        except Exception:
            return None
        return ((cfg or {}).get("engine") or None) if isinstance(cfg, dict) else None

    for key in ("user_config_template", "config", "endpoint_config"):
        engine = engine_of(md.get(key))
        if not engine:
            continue
        provider = (engine.get("provider") or {}).get("type", "")
        if provider and provider != "LocalProvider":
            return None            # Slurm-provisioned workers: not a login-node concern
        return "capped" if engine.get("max_workers_per_node") else "uncapped"
    return None


def main(argv=None) -> int:
    from dotenv import load_dotenv

    load_dotenv(override=False)
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--sites", default=os.environ.get("DIAMOND_AGENT_SITES", "sites.live.yaml"))
    a = p.parse_args(argv)
    next_step = None

    def report(level, msg):
        print(f"{level}{msg}")

    # 1. install
    try:
        importlib.import_module("globus_compute_sdk")
        report(OK, "globus-compute-sdk installed")
    except ImportError:
        report(BAD, "globus-compute-sdk missing — run `make setup` (uv sync --all-extras)")
        next_step = next_step or "make setup"
    for mod, label, how in (("anthropic", "AI extra (scheduler ranking, envelope drafting)", "optional: `uv sync --extra ai`"),
                            ("mcp", "MCP extra (server for other agents)", "optional: `uv sync --extra mcp`")):
        try:
            importlib.import_module(mod); report(OK, label)
        except ImportError:
            report(WARN, f"{label} not installed — {how}")

    # 2. .env
    if Path(".env").exists():
        report(OK, ".env present")
    else:
        report(WARN, ".env missing — `cp .env.example .env` (make setup does it)")
    if os.environ.get("ANTHROPIC_API_KEY"):
        report(OK, "ANTHROPIC_API_KEY set (AI scheduler enabled)")
    else:
        report(WARN, "ANTHROPIC_API_KEY not set — scheduler uses the heuristic ranking (fine to start)")

    # 3. credentials
    authenticated = False
    if next_step is None:
        from . import auth
        try:
            ready, detail = auth.check_headless_ready()
        except Exception as exc:
            ready, detail = False, str(exc)
        authenticated = ready
        if ready:
            report(OK, f"Globus credentials: {detail}")
        else:
            web = auth.web_login_configured()
            report(BAD, "no Globus credentials — `make login` (URL + one-time code)"
                        + (" or sign in from the web UI after `make live`" if web else ""))
            next_step = next_step or "make login"
        if web := auth.web_login_configured():
            report(OK, "browser sign-in configured (GLOBUS_CLIENT_ID/SECRET)")
        else:
            report(WARN, "browser sign-in not configured — only needed for the web UI login; "
                         "set GLOBUS_CLIENT_ID / GLOBUS_CLIENT_SECRET in .env")

    # 4. catalog
    sites = Path(a.sites)
    catalog = None
    if sites.exists():
        import yaml
        try:
            catalog = yaml.safe_load(sites.read_text()) or {}
            systems = catalog.get("systems", [])
            todo = [s["name"] for s in systems if "TODO" in str(s) or "<endpoint-uuid>" in str(s)]
            if not systems:
                report(BAD, f"{sites}: no systems — `make endpoints && make init-sites`")
                next_step = next_step or "make endpoints && make init-sites"
            elif todo:
                report(BAD, f"{sites}: placeholders left in {', '.join(todo)} — fill endpoint UUIDs / allocation accounts")
                next_step = next_step or f"edit {sites}"
            elif mep := [x["name"] for x in systems if x.get("user_endpoint_config")]:
                report(BAD, f"{sites}: {', '.join(mep)} use a multi-user endpoint (user_endpoint_config) — "
                            "unsupported; create a login-node endpoint there (`make endpoint-setup HOST=...`), "
                            "use its UUID and delete the user_endpoint_config block")
                next_step = next_step or f"edit {sites}"
            else:
                report(OK, f"{sites}: {len(systems)} system(s): {', '.join(s['name'] for s in systems)}")
        except Exception as exc:
            report(BAD, f"{sites} does not parse: {exc}")
            next_step = next_step or f"fix {sites}"
    else:
        report(BAD, f"{sites} missing — after `make login`: `make endpoints && make init-sites` "
                    "(or `cp sites.example.yaml sites.live.yaml` and edit)")
        next_step = next_step or ("make login" if not authenticated else "make endpoints && make init-sites")

    # 5. endpoints reachable (only when we can ask without prompting)
    if authenticated and catalog and next_step is None:
        from .auth import build_compute_client
        client = build_compute_client()
        offline = []
        for s in catalog.get("systems", []):
            try:
                st = client.get_endpoint_status(endpoint_uuid=s["endpoint"])["status"]
            except Exception as exc:
                st = f"error ({str(exc)[:60]})"
            (report(OK, f"endpoint {s['name']}: {st}") if st == "online"
             else report(WARN, f"endpoint {s['name']}: {st}"))
            if st != "online":
                offline.append(s["name"])
            try:
                if (client.get_endpoint_metadata(endpoint_uuid=s["endpoint"]) or {}).get("multi_user"):
                    report(BAD, f"endpoint {s['name']}: is a multi-user endpoint — unsupported; "
                                "replace it with a login-node endpoint (`make endpoint-setup HOST=...`)")
                    next_step = next_step or f"edit {a.sites}"
            except Exception:
                pass
            cap = endpoint_worker_cap(client, s["endpoint"])
            if cap == "uncapped":
                report(WARN, f"endpoint {s['name']}: no max_workers_per_node in its UEP template — one worker "
                             "per core on a login node trips per-user process/thread limits (thread-pool "
                             "errors). Re-run `make endpoint-setup HOST=<you>@<login host>`: it adds the "
                             "2-worker cap to ~/.globus_compute/<endpoint>/user_config_template.yaml.j2 and restarts.")
        if offline:
            print(endpoint_setup_hint(offline).replace("Create an endpoint", "Endpoints are down — restart or create them"))
            print("  (an existing endpoint restarts with: ssh <host> '~/.gce-venv/bin/globus-compute-endpoint start --detach diamond-agent')")

    print()
    if next_step:
        print(f"NEXT: {next_step}")
        if next_step.startswith("make login") or "init-sites" in next_step:
            print()
            print(endpoint_setup_hint())
        return 1
    print("ready — `make preflight` for the full chain check, then `make live` or `make core`")
    return 0


if __name__ == "__main__":
    sys.exit(main())
