"""Globus authentication for live mode, aligned with Diamond's flow.

Two ways in, one core:

* **Standalone core (CLI / headless / other agents)** — the existing flow:
  ``python -m diamond_agent.auth login`` (Globus native-app login: open a
  URL, paste the temporary code), refresh tokens cached by the SDK; or the
  Diamond-tokens / service-identity variables below.
* **Web frontend** — the browser signs in through Globus OAuth exactly like
  the Diamond dashboard (authorization-code flow against a confidential
  client, ``/auth/login`` → Globus → ``/auth/callback``); the core keeps the
  resulting token bundle as a **web session** (``$DIAMOND_AGENT_HOME/
  web_session.json``, mode 0600) and drives Globus Compute with a
  RefreshTokenAuthorizer for the ``funcx_service`` token. See
  ``core/webauth.py``. Requires ``GLOBUS_CLIENT_ID`` / ``GLOBUS_CLIENT_SECRET``
  (aliases: ``PORTAL_CLIENT_ID`` / ``PORTAL_CLIENT_SECRET``) and the redirect
  URI ``<public url>/auth/callback`` registered on that Globus app.

Credential resolution order (first match wins):

0. **Web session** — a token bundle stored by the frontend login (above).
   Signing out from the UI removes it and the core falls through to 1–3.

1. **Diamond tokens** — ``DIAMOND_AGENT_TOKENS`` holds the same JSON the
   diamond-admin-dashboard stores in its ``tokens`` cookie (a map of
   resource_server -> token data), either inline or as ``@/path/to/file``.
   The ``funcx_service`` access token is wrapped in an AccessTokenAuthorizer —
   byte-for-byte diamond-admin-backend's ``initialize_token_authorizer``.
   Use this when driving Diamond Agent with a session exported from Diamond.

2. **Service identity** — ``GLOBUS_COMPUTE_CLIENT_ID`` +
   ``GLOBUS_COMPUTE_CLIENT_SECRET`` (Globus confidential client): the SDK
   builds a ClientApp; fully non-interactive, right choice for servers/CI.

3. **Native login** — the SDK's UserApp: one interactive browser login via
   ``python -m diamond_agent.auth login``; refresh tokens are stored in
   ``~/.globus_compute/storage.db`` so everything afterwards runs headless.

Functions are shipped by source (AllCodeStrategies) because the web process
and the endpoints run different Python environments — same reasoning as
diamond's AllCodeStrategies usage.

CLI:
  python -m diamond_agent.auth login | whoami | logout | endpoints | init-sites
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_client = None
_generation = 0          # bumped whenever credentials change (login / logout)

# Globus Compute scope + resource server (globus_sdk.scopes.ComputeScopes).
COMPUTE_SCOPE = "https://auth.globus.org/scopes/facd7ccc-c5f4-42aa-916b-a0e270e2c2a9/all"
COMPUTE_RESOURCE_SERVER = "funcx_service"
WEB_SCOPES = ["openid", "profile", "email", COMPUTE_SCOPE]


def agent_home() -> Path:
    return Path(os.environ.get("DIAMOND_AGENT_HOME", "~/.diamond_agent")).expanduser()


def web_session_path() -> Path:
    return agent_home() / "web_session.json"


def web_client_creds() -> tuple[Optional[str], Optional[str]]:
    """Confidential-client credentials for the browser login (Diamond names accepted)."""
    cid = (os.environ.get("GLOBUS_CLIENT_ID") or os.environ.get("PORTAL_CLIENT_ID")
           or os.environ.get("NEXT_PUBLIC_GLOBUS_CLIENT_ID"))
    sec = os.environ.get("GLOBUS_CLIENT_SECRET") or os.environ.get("PORTAL_CLIENT_SECRET")
    return cid or None, sec or None


def web_login_configured() -> bool:
    cid, sec = web_client_creds()
    return bool(cid and sec)


def load_web_session() -> Optional[dict]:
    """The stored frontend login, or None. Must contain a funcx_service token."""
    path = web_session_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        logger.warning("web session file unreadable; ignoring")
        return None
    tokens = data.get("tokens") or {}
    if not isinstance(tokens.get(COMPUTE_RESOURCE_SERVER), dict):
        return None
    return data


def save_web_session(session: dict) -> None:
    home = agent_home()
    home.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(home, 0o700)
    except OSError:
        pass
    path = web_session_path()
    path.write_text(json.dumps(session, indent=2, default=str))
    os.chmod(path, 0o600)
    invalidate_client()


def clear_web_session() -> bool:
    path = web_session_path()
    existed = path.exists()
    if existed:
        path.unlink()
    invalidate_client()
    return existed


def invalidate_client() -> None:
    """Drop the cached Globus Compute client so the next call re-resolves credentials."""
    global _client, _generation
    with _lock:
        _client = None
        _generation += 1


def client_generation() -> int:
    return _generation


def _web_authorizer():
    """Authorizer from the frontend's web session, or None.

    With a refresh token and the confidential-client credentials this is a
    RefreshTokenAuthorizer: the SDK renews the funcx_service access token
    before it expires and ``on_refresh`` persists the renewed bundle — the
    same job the dashboard's /api/auth/refresh route does, done in-process.
    """
    session = load_web_session()
    if session is None:
        return None
    import globus_sdk

    tok = session["tokens"][COMPUTE_RESOURCE_SERVER]
    cid, sec = web_client_creds()
    if tok.get("refresh_token") and cid and sec:
        auth_client = globus_sdk.ConfidentialAppAuthClient(cid, sec)

        def on_refresh(resp):
            fresh = load_web_session() or session
            for rs, data in resp.by_resource_server.items():
                merged = {**fresh["tokens"].get(rs, {}), **data}
                merged["refresh_token"] = data.get("refresh_token") or fresh["tokens"].get(rs, {}).get("refresh_token")
                fresh["tokens"][rs] = merged
            save_web_session(fresh)
            logger.info("auth: web session tokens refreshed")

        logger.info("auth: using web-session funcx_service token (auto-refresh)")
        return globus_sdk.RefreshTokenAuthorizer(
            tok["refresh_token"], auth_client, access_token=tok.get("access_token"),
            expires_at=tok.get("expires_at_seconds"), on_refresh=on_refresh)
    logger.info("auth: using web-session funcx_service token (no refresh token)")
    return globus_sdk.AccessTokenAuthorizer(access_token=tok["access_token"])

# Hostname regex -> canonical machine name (diamond's host_machine_mapping,
# plus the DeltaAI MEP host dtai-glbscmp01.delta...).
KNOWN_MACHINES: list[tuple[str, str]] = [
    (r"gh-login\d*\.delta\.ncsa\.", "DeltaAI@NCSA"),
    (r"dtai-[\w-]*\.delta\.ncsa\.", "DeltaAI@NCSA"),
    (r"\.delta\.ncsa\.", "Delta@NCSA"),
    (r"\.frontera\.tacc\.", "Frontera@TACC"),
    (r"\.ls6\.tacc\.", "Lonestar6@TACC"),
    (r"\.vista\.tacc\.", "Vista@TACC"),
    (r"\.anvil\.rcac\.purdue\.edu", "Anvil@RCAC"),
    (r"\.tacc\.utexas\.edu", "System@TACC"),
    (r"\.ncsa\.illinois\.edu", "System@NCSA"),
]

# Diamond's known MEP worker configs (account intentionally left to the user).
MEP_DEFAULT_UEP_CONFIG: dict[str, dict] = {
    "4a266c83-3c68-4a75-99b3-9c7459f2f7ef": {"partition": "cpu-interactive", "exclusive": False},
    "5aafb4c1-27b2-40d8-a038-a0277611868f": {"partition": "shared", "exclusive": False},
}


def resolve_host(hostname: str) -> str:
    for pattern, name in KNOWN_MACHINES:
        if re.search(pattern, hostname or ""):
            return name
    return "unknown"


def _diamond_authorizer():
    """AccessTokenAuthorizer from a Diamond-style tokens JSON, or None."""
    raw = os.environ.get("DIAMOND_AGENT_TOKENS")
    if not raw:
        return None
    if raw.startswith("@"):
        raw = Path(raw[1:]).expanduser().read_text()
    # Diamond's cookie arrives urlencoded with quote/comma escapes; tolerate it.
    import urllib.parse

    text = urllib.parse.unquote(raw).replace("'", '"').replace("\\054", ",")
    tokens = json.loads(text)
    access_token = None
    for value in tokens.values():
        if isinstance(value, dict) and value.get("resource_server") == "funcx_service":
            access_token = value.get("access_token")
    if access_token is None:
        raise ValueError(
            "DIAMOND_AGENT_TOKENS is set but contains no funcx_service token "
            "(expected the diamond dashboard 'tokens' cookie JSON)")
    import globus_sdk

    logger.info("auth: using Diamond-provided funcx_service token")
    return globus_sdk.AccessTokenAuthorizer(access_token=access_token)


class NonInteractiveAuthRequired(RuntimeError):
    """A Globus login/consent flow was requested in a headless context."""


def _build_app(interactive: bool):
    """GlobusApp mirroring the SDK default, but with auto-login control.

    interactive=False makes every path that would start a console login flow
    raise NonInteractiveAuthRequired instead of blocking on a prompt. That
    covers both token-validation re-logins and the compute SDK's GARE handler
    (client.py wraps API calls with `gare_handler(self.app.login, ...)`, which
    calls app.login() on policy-protected endpoints). Mandatory for the server
    and bulk endpoint listing; only the explicit `login` command is interactive.
    """
    import platform

    from globus_compute_sdk.sdk.auth.globus_app import (
        DEFAULT_CLIENT_ID,
        ClientApp,
        GlobusAppConfig,
        UserApp,
        get_client_creds,
        get_token_storage,
    )
    from globus_sdk.login_flows import LoginFlowManager

    class _RaisingLoginFlowManager(LoginFlowManager):
        def run_login_flow(self, auth_parameters):
            message = getattr(auth_parameters, "session_message", None)
            raise NonInteractiveAuthRequired(
                f"login/consent required ({message or 'no detail'}) — headless mode never prompts")

        @classmethod
        def for_globus_app(cls, app_name, login_client, config):
            return cls(login_client, request_refresh_tokens=config.request_refresh_tokens)

    client_id, client_secret = get_client_creds()
    app_name = platform.node()
    kwargs: dict = {"token_storage": get_token_storage(), "request_refresh_tokens": True}
    if client_id and client_secret:
        # A ClientApp (service identity) never runs a login flow, and the SDK
        # rejects a login_flow_manager on it — only the token-validation
        # handler is silenced.
        if not interactive:
            kwargs["token_validation_error_handler"] = None
        return ClientApp(app_name=app_name, client_id=client_id,
                         client_secret=client_secret, config=GlobusAppConfig(**kwargs))
    if not interactive:
        kwargs["token_validation_error_handler"] = None
        kwargs["login_flow_manager"] = _RaisingLoginFlowManager
    return UserApp(app_name=app_name, client_id=client_id or DEFAULT_CLIENT_ID,
                   config=GlobusAppConfig(**kwargs))


def build_compute_client(interactive: bool = False):
    """Globus Compute client for probe + executor (cached for the default case)."""
    global _client
    with _lock:
        if _client is not None and not interactive:
            return _client
        from globus_compute_sdk import Client
        from globus_compute_sdk.serialize import AllCodeStrategies

        authorizer = _web_authorizer() or _diamond_authorizer()
        if authorizer is not None:
            client = Client(authorizer=authorizer,
                            code_serialization_strategy=AllCodeStrategies())
        else:
            client = Client(app=_build_app(interactive),
                            code_serialization_strategy=AllCodeStrategies())
        if not interactive:
            _client = client
        return client


def auth_mode() -> str:
    if load_web_session() is not None:
        return "web-session"
    if os.environ.get("DIAMOND_AGENT_TOKENS"):
        return "diamond-tokens"
    if os.environ.get("GLOBUS_COMPUTE_CLIENT_ID"):
        return "service-identity"
    return "native-login"


def check_headless_ready() -> tuple[bool, str]:
    """Can live mode run without prompting? (The server must never prompt.)"""
    mode = auth_mode()
    if mode in ("web-session", "diamond-tokens", "service-identity"):
        return True, mode
    from globus_compute_sdk.sdk.auth.globus_app import get_globus_app

    if get_globus_app().login_required():
        return False, ("no cached Globus tokens for headless operation — run "
                       "`python -m diamond_agent.auth login` once, then restart"
                       + (" (or sign in from the web UI)" if web_login_configured() else ""))
    return True, mode


def credentials_available() -> bool:
    """True when the core can talk to Globus Compute right now. Never prompts."""
    try:
        return check_headless_ready()[0]
    except Exception:
        return False


def auth_status(required: bool = True) -> dict:
    """Auth state for the UI / CLI / MCP: no network, no prompts."""
    mode = auth_mode()
    session = load_web_session() if mode == "web-session" else None
    available = credentials_available() if required else True
    return {
        "required": required,
        "mode": mode,
        "authenticated": available,
        "web_login_available": web_login_configured(),
        "user": (session or {}).get("user"),
        "signed_in_at": (session or {}).get("created_at"),
        "hint": (None if not required else
                 "signed in from the web UI" if mode == "web-session" else
                 "Diamond-provided funcx_service token" if mode == "diamond-tokens" else
                 "Globus confidential client (service identity)" if mode == "service-identity" else
                 "cached Globus tokens from `diamond_agent.auth login`" if available else
                 "no credentials — sign in from the web UI or run `python -m diamond_agent.auth login`"),
    }


# ----------------------------------------------------------------------- CLI

def _cmd_login() -> None:
    client = build_compute_client(interactive=True)
    # Any authenticated call forces the login flow if tokens are absent.
    client.version_check()
    print(f"login OK (mode: {auth_mode()}); tokens cached for headless use")
    _cmd_whoami()


def identity_description() -> str:
    """One-line identity string for CLIs; never prompts, never raises."""
    if auth_mode() == "web-session":
        user = (load_web_session() or {}).get("user") or {}
        return f"web session: {user.get('name') or user.get('email') or user.get('sub') or 'unknown'}"
    if auth_mode() == "diamond-tokens":
        return "Diamond-provided funcx_service token"
    try:
        import globus_sdk
        from globus_sdk.scopes import Scope

        # openid is the only auth.globus.org scope the compute login grants;
        # asking for AuthClient's defaults (email/profile) would force re-consent.
        auth_client = globus_sdk.AuthClient(app=_build_app(interactive=False),
                                            app_scopes=[Scope("openid")])
        info = auth_client.userinfo()
        who = info.get("preferred_username") or info.get("name") or info.get("sub")
        return f"{who} ({info.get('sub')})"
    except Exception as exc:
        return f"identity lookup unavailable: {exc}"


def _cmd_whoami() -> None:
    print(f"identity: {identity_description()}")


def _cmd_logout() -> None:
    from globus_compute_sdk import Client

    Client().logout()
    print("local Globus Compute tokens revoked/cleared")


def _list_endpoints(check_all: bool = False) -> list[dict]:
    """Metadata first; status only for endpoints on known machines.

    Status checks against policy-protected endpoints trigger Globus session
    re-auth prompts, so by default we only check endpoints whose hostname maps
    to a machine we would actually federate (use --all to check everything).
    """
    client = build_compute_client()
    out = []
    for ep in client.get_endpoints(role="any"):
        uuid, name = ep["uuid"], ep.get("display_name") or "(unnamed)"
        hostname = ""
        try:
            metadata = client.get_endpoint_metadata(endpoint_uuid=uuid) or {}
            hostname = metadata.get("hostname") or ""
        except Exception:
            pass
        machine = resolve_host(hostname)
        if machine != "unknown" or check_all:
            try:
                status = client.get_endpoint_status(endpoint_uuid=uuid)["status"]
            except Exception as exc:
                status = f"status-error: {exc}"
        else:
            status = "skipped"
        out.append({"uuid": uuid, "name": name, "status": status,
                    "hostname": hostname, "machine": machine})
    return out


def _cmd_endpoints(check_all: bool = False) -> None:
    endpoints = _list_endpoints(check_all)
    if not endpoints:
        print("no Globus Compute endpoints visible to this identity")
        return
    # Known machines first, then the rest.
    endpoints.sort(key=lambda e: (e["machine"] == "unknown", e["machine"], e["name"]))
    width = max(len(e["name"]) for e in endpoints)
    for e in endpoints:
        print(f"{e['uuid']}  {e['status']:<8}  {e['name']:<{width}}  "
              f"{e['machine']:<15}  {e['hostname']}")
    if not check_all:
        print("\n(status checked only for known machines; --all checks every endpoint,"
              " which may trigger Globus session-policy prompts)")


def _bundled_shapes() -> dict[str, dict]:
    """Partition shapes for known machines (data/known_machines.yaml)."""
    from importlib import resources

    import yaml

    text = resources.files("diamond_agent").joinpath("data/known_machines.yaml").read_text()
    return {m["name"]: m for m in yaml.safe_load(text)["machines"]}


def _cmd_init_sites(path: str = "sites.live.yaml") -> None:
    """Write a live site catalog from discovered endpoints.

    One entry per known machine (online endpoints preferred); partition shapes
    are pre-filled from the bundled catalog for machines we know, so usually
    only the allocation account remains TODO.
    """
    endpoints = _list_endpoints()
    shapes = _bundled_shapes()

    per_machine: dict[str, dict] = {}
    for e in endpoints:
        if e["machine"] == "unknown":
            continue
        current = per_machine.get(e["machine"])
        if current is None or (current["status"] != "online" and e["status"] == "online"):
            per_machine[e["machine"]] = e

    lines = [
        "# Live site catalog generated by `python -m diamond_agent.auth init-sites`.",
        "# Shapes for known machines are pre-filled; set accounts to YOUR allocation",
        "# (and user_endpoint_config.account for MEPs), then: make preflight && make live",
        "",
        "systems:",
    ]
    written = 0
    for machine in sorted(per_machine):
        e = per_machine[machine]
        if e["status"] != "online":
            lines.append(f"  # {machine}: endpoint {e['uuid']} ({e['name']}) is "
                         f"{e['status']} — start it, then add it here")
            lines.append("")
            continue
        written += 1
        lines += [
            f"  - name: {machine}",
            f"    endpoint: {e['uuid']}   # {e['name']}",
            "    container_runtime: apptainer",
        ]
        uep = MEP_DEFAULT_UEP_CONFIG.get(e["uuid"])
        if uep is not None:
            lines.append("    # MEP worker placement (diamond defaults) — set account:")
            lines.append(f"    user_endpoint_config: "
                         f"{{account: TODO-your-allocation, "
                         f"partition: {uep['partition']}, exclusive: false}}")
        elif "Multi-User" in e["name"] or "MEP" in e["name"] or "glbscmp" in e["hostname"]:
            lines.append("    # Looks like a MEP — worker placement config likely required:")
            lines.append("    user_endpoint_config: {account: TODO-your-allocation, "
                         "partition: TODO-worker-partition, exclusive: false}")
        shape = shapes.get(machine)
        if shape is not None:
            lines.append(f"    accounts: [TODO-your-allocation]")
            lines.append("    partitions:")
            for p in shape["partitions"]:
                lines.append(f"      - name: {p['name']}")
                for key in ("arch", "gpu_type", "gpus_per_node", "gpu_mem_gb",
                            "cpus_per_node", "mem_per_node_gb", "nodes_total"):
                    value = p.get(key)
                    lines.append(f"        {key}: {'null' if value is None else value}")
                lines.append(f"        max_walltime: \"{p.get('max_walltime', '48:00:00')}\"")
        else:
            lines += [
                "    accounts: [TODO-your-allocation]",
                "    partitions:",
                "      - name: TODO-partition",
                "        arch: x86_64",
                "        gpu_type: A100        # null for CPU partitions",
                "        gpus_per_node: 4",
                "        gpu_mem_gb: 40",
                "        cpus_per_node: 64",
                "        mem_per_node_gb: 256",
                "        nodes_total: 100",
                '        max_walltime: "48:00:00"',
            ]
        lines.append("")
    lines += [
        "policy:",
        "  max_user_pending_per_system: 4",
        "  max_nodes_per_job: 8",
        "  blocked_partitions: []",
        "",
    ]
    Path(path).write_text("\n".join(lines))
    print(f"wrote {path}: {written} online system(s) "
          f"({len(per_machine) - written} known but offline, listed as comments) — "
          "fill in the TODO accounts")


def main() -> None:
    import argparse

    from dotenv import load_dotenv

    load_dotenv(override=False)
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command",
                        choices=["login", "whoami", "logout", "endpoints", "init-sites"])
    parser.add_argument("--out", default="sites.live.yaml",
                        help="output path for init-sites")
    parser.add_argument("--all", action="store_true",
                        help="endpoints: status-check every endpoint, not just known machines")
    args = parser.parse_args()
    if args.command == "login":
        _cmd_login()
    elif args.command == "whoami":
        _cmd_whoami()
    elif args.command == "logout":
        _cmd_logout()
    elif args.command == "endpoints":
        _cmd_endpoints(args.all)
    elif args.command == "init-sites":
        _cmd_init_sites(args.out)


if __name__ == "__main__":
    main()
