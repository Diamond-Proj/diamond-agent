"""Diamond Agent application assembly.

Run locally:
    uv run uvicorn diamond_agent.main:app --port 8642
then open http://127.0.0.1:8642

Environment:
    DIAMOND_AGENT_SITES  site catalog (default ./sites.live.yaml; see
                         `python -m diamond_agent.auth init-sites`)
    DIAMOND_AGENT_TICK   seconds per federation refresh (default 60)
    DIAMOND_AGENT_DB     sqlite path (default ./diamond_agent.db)
    DIAMOND_AGENT_UI     1 (default) serves the web frontend; 0 runs the core
                         headless — HTTP API (/api, /docs) only, for other agents.
                         Operator-only routes (/ui/policy: editing scheduling
                         limits) exist only when the UI is served.
    GLOBUS_CLIENT_ID / GLOBUS_CLIENT_SECRET
                         enable "Sign in with Globus" in the frontend (confidential
                         client with <url>/auth/callback registered); the headless
                         core keeps `python -m diamond_agent.auth login`
    DIAMOND_AGENT_PUBLIC_URL  external base URL (proxies): the OAuth redirect and the
                         channel URLs printed at start; default http://127.0.0.1:$DIAMOND_AGENT_PORT
    DIAMOND_AGENT_PORT   the port the core is served on (make sets it); only the
                         printed URLs depend on it
    DIAMOND_AGENT_HOME   where the web session is kept (default ~/.diamond_agent)

Process model: one process = diamond-agent-core (providers, collector,
scheduler, orchestrator, store, HTTP API) + optionally the static web UI,
which is just another client of /api.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from dotenv import load_dotenv

# .env in the working directory seeds configuration (diamond pattern);
# real environment variables always win.
load_dotenv(override=False)
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import api, channels
from .core import webauth
from .core.service import DiamondAgentCore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SERVE_UI = os.environ.get("DIAMOND_AGENT_UI", "1") not in ("0", "false", "no")
TICK_SECONDS = float(os.environ.get("DIAMOND_AGENT_TICK", "60"))
DB_PATH = os.environ.get("DIAMOND_AGENT_DB", "diamond_agent.db")
SITES_PATH = os.environ.get("DIAMOND_AGENT_SITES", "sites.live.yaml")


def load_catalog() -> dict:
    path = Path(SITES_PATH)
    if not path.exists():
        raise RuntimeError(
            f"site catalog {path} not found — run `make doctor` for the next step "
            "(typically `make login`, `make endpoint-setup HOST=...` on each supercomputer, "
            "then `make endpoints && make init-sites`), or set DIAMOND_AGENT_SITES")
    catalog = yaml.safe_load(path.read_text())
    mep = [s["name"] for s in catalog["systems"] if s.get("user_endpoint_config")]
    if mep:
        raise RuntimeError(
            f"{path}: {', '.join(mep)} carry `user_endpoint_config` (multi-user endpoint "
            "worker placement). Multi-user endpoints are not supported: create a login-node "
            "endpoint on that system with `make endpoint-setup HOST=...`, put its UUID in the "
            "entry and delete the user_endpoint_config block.")
    return catalog


def wire() -> None:
    """Assemble diamond-agent-core; the web UI is mounted separately (see below)."""
    catalog = load_catalog()
    from . import auth

    # Frontend deployments may come up before anyone has signed in: the
    # browser login (core/webauth.py) supplies credentials later. Headless /
    # CLI use keeps the strict rule — credentials must exist at start.
    api.ctx.core = DiamondAgentCore.build(
        catalog, db_path=DB_PATH,
        allow_unauthenticated=SERVE_UI and auth.web_login_configured())


def _tick_once() -> None:
    api.ctx.core.tick()


async def _loop() -> None:
    while True:
        try:
            await asyncio.to_thread(_tick_once)
        except Exception:
            logger.exception("agent tick failed")
        await asyncio.sleep(TICK_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    wire()
    await asyncio.to_thread(_tick_once)  # first snapshot before serving
    task = asyncio.create_task(_loop())
    logger.info("diamond-agent-core up (sites=%s, tick=%.0fs, db=%s, ui=%s)",
                SITES_PATH, TICK_SECONDS, DB_PATH, "on" if SERVE_UI else "off")
    logger.info("agent skill: %s  (served at <channel url>/skill; API reference /skill/api; per-system notes "
                "/skill/systems; queue status /api/status?format=text)", api.SKILL_PATH)
    base = channels.origin()
    for channel in api.ctx.core.channels.list():
        if channel.id == channels.DEFAULT_ID:
            logger.info("default channel (every machine, machine rules; cannot be deleted) — hand agents:\n"
                        "    export DIAMOND_AGENT_URL=%s%s", base, channel.path)
        else:
            logger.info("channel %-16s %s%s", channel.name, base, channel.path)
    logger.info("channels from the terminal: make channels  (python -m diamond_agent.channels list|create|delete)")
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="diamond-agent-core", version="0.2.0", lifespan=lifespan,
              description="One virtual pool over federated HPC (Diamond / Globus Compute) and "
                          "reserved SSH/cloud resources: submit to any resource, or let the "
                          "rule-based meta-scheduler choose. Web UI, CLI and MCP server are clients "
                          "of this API.")
# Agents come in through their channel URL (/c/{token}/…): that mount is the
# documented contract. The unscoped mount serves the operator's web UI.
for r in (api.router, api.skill_router, webauth.session_router):
    app.include_router(r, prefix=api.CHANNEL_PREFIX)
    app.include_router(r, include_in_schema=False)


@app.middleware("http")
async def _no_cache(request, call_next):
    """The UI is optional per process: never let a browser cache it."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response

_WEB_DIR = Path(__file__).parent / "web"
if SERVE_UI:
    app.include_router(webauth.login_router)    # /auth/login · /auth/callback — frontend only
    app.include_router(api.ui_router)           # /ui/policy — operator edits, frontend only
    app.mount("/static", StaticFiles(directory=_WEB_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        # The browser that loads the UI becomes the operator for this core
        # process: it alone may change scheduling limits (see api.ui_router).
        response = FileResponse(_WEB_DIR / "index.html")
        webauth.issue_operator_cookie(response)
        return response
else:
    @app.get("/", include_in_schema=False)
    def index_headless():
        return {"service": "diamond-agent-core", "docs": "/docs",
                "agents": "use your channel URL: <origin>/c/<token> — printed at start, listed by `make channels`",
                "skill_path": str(api.SKILL_PATH)}
