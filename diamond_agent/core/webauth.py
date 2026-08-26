"""Browser login for the web frontend — Globus OAuth, Diamond-dashboard style.

Flow (same shape as diamond-admin-dashboard's /login → auth.globus.org →
/auth/callback, but the token bundle lives in the core instead of a cookie):

    GET  /auth/login          302 → https://auth.globus.org/v2/oauth2/authorize
                              (client_id, redirect_uri, code, offline, state)
    GET  /auth/callback       exchange code with the confidential client, keep
                              tokens by resource server (+ id_token claims for
                              display), store the web session, set a session
                              cookie, 302 → /
    GET  /api/auth/session    auth state for the UI / CLI / MCP
    POST /api/auth/logout     drop the web session (core falls back to CLI creds)

Only mounted when the frontend is served (DIAMOND_AGENT_UI=1); the standalone
core keeps the native-app login of ``diamond_agent.auth``.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import time
import urllib.parse
from typing import Optional

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from .. import auth

logger = logging.getLogger(__name__)

GLOBUS_AUTHORIZE_URL = "https://auth.globus.org/v2/oauth2/authorize"
STATE_COOKIE = "da_oauth_state"
SESSION_COOKIE = "da_session"
SESSION_MAX_AGE = 7 * 24 * 3600

login_router = APIRouter(tags=["auth"])      # /auth/* — frontend only
session_router = APIRouter(tags=["auth"])    # /api/auth/* — always


def public_base(request: Request) -> str:
    """Where Globus should send the browser back (override for proxies)."""
    return (os.environ.get("DIAMOND_AGENT_PUBLIC_URL") or str(request.base_url)).rstrip("/")


def redirect_uri(request: Request) -> str:
    return public_base(request) + "/auth/callback"


def authorize_url(client_id: str, redirect: str, state: str) -> str:
    params = {"client_id": client_id, "redirect_uri": redirect, "response_type": "code",
              "scope": " ".join(auth.WEB_SCOPES), "access_type": "offline", "state": state}
    return GLOBUS_AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)


def _decode_jwt_claims(token: Optional[str]) -> dict:
    """Unverified payload decode — display only (name/email), never authorization."""
    if not token or token.count(".") < 2:
        return {}
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def exchange_code(code: str, redirect: str) -> dict:
    """Authorization code → token bundle by resource server (+ id_token).

    Separated so tests can monkeypatch it; the real call is globus_sdk's
    ConfidentialAppAuthClient (same client the Diamond backend uses).
    """
    import globus_sdk

    cid, sec = auth.web_client_creds()
    client = globus_sdk.ConfidentialAppAuthClient(cid, sec)
    client.oauth2_start_flow(redirect, requested_scopes=auth.WEB_SCOPES, refresh_tokens=True)
    resp = client.oauth2_exchange_code_for_tokens(code)
    return {"by_resource_server": dict(resp.by_resource_server),
            "id_token": resp.get("id_token")}


def _user_from(bundle: dict) -> dict:
    claims = _decode_jwt_claims(bundle.get("id_token"))
    return {"sub": claims.get("sub"), "name": claims.get("name"), "email": claims.get("email"),
            "username": claims.get("preferred_username"),
            "organization": claims.get("organization"),
            "identity_provider": claims.get("identity_provider_display_name")}


def _unconfigured() -> JSONResponse:
    return JSONResponse(status_code=503, content={
        "detail": "web login is not configured: set GLOBUS_CLIENT_ID and GLOBUS_CLIENT_SECRET "
                  "(a Globus confidential client with <public url>/auth/callback as a redirect URI), "
                  "or sign in from the shell with `python -m diamond_agent.auth login`"})


# ------------------------------------------------------------------ routes

@login_router.get("/auth/login", include_in_schema=True, summary="Start the Globus browser login")
def login(request: Request):
    cid, _ = auth.web_client_creds()
    if not auth.web_login_configured():
        return _unconfigured()
    state = secrets.token_urlsafe(24)
    resp = RedirectResponse(authorize_url(cid, redirect_uri(request), state), status_code=302)
    resp.set_cookie(STATE_COOKIE, state, max_age=600, httponly=True, samesite="lax")
    return resp


@login_router.get("/auth/callback", include_in_schema=False)
def callback(request: Request, code: Optional[str] = None, state: Optional[str] = None,
             error: Optional[str] = None):
    if not auth.web_login_configured():
        return _unconfigured()
    expected = request.cookies.get(STATE_COOKIE)
    if error:
        logger.warning("auth callback: provider error %s", error)
        return RedirectResponse(f"/?auth_error={urllib.parse.quote(error)}", status_code=302)
    if not code or not state or state != expected:
        logger.warning("auth callback: missing code or state mismatch")
        return RedirectResponse("/?auth_error=state_mismatch", status_code=302)
    try:
        bundle = exchange_code(code, redirect_uri(request))
    except Exception as exc:
        logger.exception("auth callback: token exchange failed")
        return RedirectResponse(f"/?auth_error=exchange_failed:{urllib.parse.quote(str(exc)[:120])}",
                                status_code=302)
    tokens = bundle["by_resource_server"]
    if auth.COMPUTE_RESOURCE_SERVER not in tokens:
        logger.error("auth callback: no funcx_service token in response (scopes: %s)", auth.WEB_SCOPES)
        return RedirectResponse("/?auth_error=no_compute_token", status_code=302)
    session = {"session_id": secrets.token_urlsafe(32), "created_at": int(time.time()),
               "tokens": tokens, "user": _user_from(bundle)}
    auth.save_web_session(session)
    logger.info("auth: web session started for %s", session["user"].get("email") or session["user"].get("sub"))
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie(SESSION_COOKIE, session["session_id"], max_age=SESSION_MAX_AGE,
                    httponly=True, samesite="lax")
    resp.delete_cookie(STATE_COOKIE)
    return resp


@login_router.get("/auth/logout", include_in_schema=False)
def logout_redirect(response: Response):
    auth.clear_web_session()
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


def session_payload(request: Request, required: bool) -> dict:
    st = auth.auth_status(required=required)
    session = auth.load_web_session() if st["mode"] == "web-session" else None
    st["browser_bound"] = bool(session) and request.cookies.get(SESSION_COOKIE) == session.get("session_id")
    st["login_url"] = "/auth/login" if st["web_login_available"] else None
    return st


@session_router.get("/api/auth/session", summary="Auth state (no prompts, no network)")
def session_status(request: Request):
    return session_payload(request, required=True)


@session_router.post("/api/auth/logout", summary="Drop the web session")
def logout():
    existed = auth.clear_web_session()
    resp = JSONResponse({"signed_out": existed, "mode": auth.auth_mode()})
    resp.delete_cookie(SESSION_COOKIE)
    return resp
