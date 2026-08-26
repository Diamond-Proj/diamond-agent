"""HTTP client for a running diamond-agent-core — stdlib only.

Mirrors ``DiamondAgentCore``'s methods over ``/api``; used by the CLI, the MCP
server and any external agent that prefers Python to raw HTTP.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

DEFAULT_URL = os.environ.get("DIAMOND_AGENT_URL", "http://127.0.0.1:8642")
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}


class CoreError(RuntimeError):
    pass


class CoreClient:
    def __init__(self, base_url: str = DEFAULT_URL, timeout: float = 150):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def _call(self, method: str, path: str, body: Any = None, **query) -> Any:
        url = self.base + "/api" + path
        q = {k: v for k, v in query.items() if v is not None}
        if q:
            url += "?" + urllib.parse.urlencode(q)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read()).get("detail", e.reason)
            except Exception:
                detail = e.reason
            raise CoreError(f"{method} {path}: {detail}") from None
        except urllib.error.URLError as e:
            raise CoreError(f"diamond-agent-core unreachable at {self.base} ({e.reason}); "
                            "start it with `make live` (or `make core`) or set DIAMOND_AGENT_URL") from None

    # status / resources
    def info(self) -> dict: return self._call("GET", "/config")
    def providers(self) -> list: return self._call("GET", "/providers")
    def resources(self) -> dict: return self._call("GET", "/resources")
    def snapshot(self) -> dict: return self._call("GET", "/federation")
    def history(self, limit: int = 60) -> list: return self._call("GET", "/federation/history", limit=limit)
    def idle(self, **filters) -> list: return self._call("GET", "/resources/idle", **filters)
    def status(self) -> dict: return self._call("GET", "/status")
    def env(self, system: str, timeout_s: int = 90) -> dict:
        return self._call("GET", f"/resources/{urllib.parse.quote(system, safe='')}/env", timeout_s=timeout_s)
    def exec(self, system: str, command: str, timeout_s: int = 60) -> dict:
        return self._call("POST", f"/resources/{urllib.parse.quote(system, safe='')}/exec", {"command": command, "timeout_s": timeout_s})
    def put_file(self, system: str, path: str, content: bytes, mode: str = "0644") -> dict:
        import base64
        return self._call("POST", f"/resources/{urllib.parse.quote(system, safe='')}/files",
                          {"path": path, "content_b64": base64.b64encode(content).decode(), "mode": mode})
    def task_logs(self, task_id: str, tail: int = 200) -> dict:
        return self._call("GET", f"/tasks/{task_id}/logs", tail=tail)
    def status_text(self) -> str: return self._text("/api/status?format=text")
    def skill(self) -> str: return self._text("/skill")

    def _text(self, path: str) -> str:
        req = urllib.request.Request(self.base + path)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.read().decode("utf-8")
        except urllib.error.URLError as e:
            raise CoreError(f"diamond-agent-core unreachable at {self.base} ({e.reason})") from None

    # envelope / tasks
    def draft_envelope(self, name: str, description: str = "", command: str = "") -> dict:
        return self._call("POST", "/envelope/generate", {"name": name, "description": description, "command": command})

    def submit(self, name: str, command: str, description: str = "", envelope: Optional[dict] = None,
               target: Optional[dict] = None, container: Optional[str] = None, max_retries: int = 2,
               auto_continue: bool = True, auto_generate_envelope: bool = False) -> dict:
        body = {"name": name, "command": command, "description": description, "envelope": envelope,
                "target": target, "container": container, "max_retries": max_retries,
                "auto_continue": auto_continue, "auto_generate_envelope": auto_generate_envelope}
        return self._call("POST", "/tasks", body)

    def tasks(self, status: Optional[str] = None) -> list: return self._call("GET", "/tasks", status=status)
    def task(self, task_id: str) -> dict: return self._call("GET", f"/tasks/{task_id}")
    def cancel(self, task_id: str) -> dict: return self._call("POST", f"/tasks/{task_id}/cancel")
    def replan(self, task_id: str, exclude: Optional[list] = None, target: Optional[dict] = None,
               clear_target: bool = False) -> dict:
        return self._call("POST", f"/tasks/{task_id}/replan",
                          {"exclude": exclude, "target": target, "clear_target": clear_target})
    def events(self, limit: int = 100, task_id: Optional[str] = None) -> list:
        return self._call("GET", "/events", limit=limit, task_id=task_id)

    def wait(self, task_id: str, timeout_s: float = 3600, poll_s: float = 3.0) -> dict:
        deadline = time.time() + timeout_s
        while True:
            t = self.task(task_id)
            if t["status"] in TERMINAL or time.time() >= deadline:
                return t
            time.sleep(poll_s)
