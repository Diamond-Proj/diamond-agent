"""Claude client wrapper for the agentic parts of Diamond Agent.

All AI usage is optional: if the anthropic SDK is missing or no credentials
resolve, callers get None and fall back to deterministic heuristics, so the
demo runs fully offline. AI output is advisory only — the deterministic rule
layer always has the final word on feasibility.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional, Type, TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

MODEL = os.environ.get("DIAMOND_AGENT_MODEL", "claude-opus-5")

T = TypeVar("T", bound=BaseModel)

_lock = threading.Lock()
_client = None
_unavailable_reason: Optional[str] = None


def _get_client():
    """Lazily build the Anthropic client; remember failures."""
    global _client, _unavailable_reason
    with _lock:
        if _client is not None or _unavailable_reason is not None:
            return _client
        try:
            import anthropic
        except ImportError:
            _unavailable_reason = "anthropic SDK not installed (pip install .[ai])"
            logger.info("AI disabled: %s", _unavailable_reason)
            return None
        try:
            client = anthropic.Anthropic()
            # Constructing succeeds even with no credentials — check that at
            # least one source (api key / auth token / stored credentials)
            # actually resolved, so the UI badge tells the truth.
            if (client.api_key is None and client.auth_token is None
                    and getattr(client, "credentials", None) is None):
                _unavailable_reason = ("no Anthropic credentials (set ANTHROPIC_API_KEY "
                                       "in .env, or `ant auth login`)")
                logger.info("AI disabled: %s", _unavailable_reason)
            else:
                _client = client
        except Exception as exc:
            _unavailable_reason = f"no Anthropic credentials: {exc}"
            logger.info("AI disabled: %s", _unavailable_reason)
        return _client


def ai_status() -> dict:
    _get_client()
    return {
        "enabled": _client is not None and _unavailable_reason is None,
        "model": MODEL,
        "reason": _unavailable_reason,
    }


def ask_structured(system: str, user: str, output_model: Type[T], max_tokens: int = 4096) -> Optional[T]:
    """One structured-output call to Claude; None on any failure."""
    global _unavailable_reason
    client = _get_client()
    if client is None:
        return None
    try:
        response = client.messages.parse(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=output_model,
        )
        if getattr(response, "stop_reason", None) == "refusal":
            logger.warning("AI request refused")
            return None
        return response.parsed_output
    except Exception as exc:
        import anthropic

        if isinstance(exc, anthropic.AuthenticationError):
            _unavailable_reason = "invalid Anthropic credentials"
        elif "Could not resolve authentication" in str(exc):
            _unavailable_reason = "no Anthropic credentials resolved at request time"
        logger.warning("AI call failed, falling back to heuristics: %s", exc)
        return None
