"""HTTP interface of diamond-agent-core.

Thin FastAPI router over ``DiamondAgentCore`` — every route is one facade
call, so the web frontend, the CLI, the MCP server and external agents share
one contract (OpenAPI at /docs). No scheduling or provider logic lives here.
"""

from __future__ import annotations

from typing import Optional

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from .core.service import DiamondAgentCore, UnknownTarget
from .schemas import ResourceEnvelope


class AppContext:
    core: DiamondAgentCore


ctx = AppContext()
router = APIRouter(prefix="/api")


def _core() -> DiamondAgentCore:
    return ctx.core


# ------------------------------------------------------------------ status

@router.get("/config", tags=["status"], summary="Service info: AI, auth state, providers, catalog policy")
def get_config():
    return _core().info()


@router.get("/providers", tags=["resources"], summary="Resource providers and their status (active / reserved / needs-login)")
def get_providers():
    return _core().providers()


# ---------------------------------------------------------- queue status

@router.get("/status", tags=["resources"], summary="Supercomputers and queues at a glance")
def get_status(format: str = "json"):
    """Per-partition free units, utilization, node states, queue depth, my jobs,
    wall-time cap and the scheduler's wait heuristic. ``?format=text`` returns
    a Markdown table an agent can paste straight into its context."""
    if format == "text":
        return PlainTextResponse(_core().status_text(), media_type="text/markdown; charset=utf-8")
    return _core().status()


# --------------------------------------------------------------- resources

@router.get("/resources", tags=["resources"], summary="All compute resources in one list (full records per partition)")
def get_resources():
    """All compute resources in one list — every provider, one shape."""
    return _core().resources()


@router.get("/resources/idle", tags=["resources"], summary="Partitions that can start work now, with reasons for the rest")
def get_idle(min_gpus: int = 1, min_gpu_mem_gb: float = 0, gpu_type: Optional[str] = None,
             arch: Optional[str] = None, max_pending: int = 10**9, include_cpu: bool = True,
             provider: Optional[str] = None):
    return _core().idle(min_gpus=min_gpus, min_gpu_mem_gb=min_gpu_mem_gb, gpu_type=gpu_type,
                        arch=arch, max_pending=max_pending, include_cpu=include_cpu, provider=provider)


class ExecRequest(BaseModel):
    command: str
    timeout_s: int = Field(default=60, ge=1, le=600)


class PutFileRequest(BaseModel):
    path: str                       # relative to the system's workdir, or absolute
    content_b64: str
    mode: str = "0644"


def _env_errors(fn, *args, **kw):
    from .providers import ProviderNotReady
    try:
        return fn(*args, **kw)
    except KeyError as e:
        raise HTTPException(404, str(e.args[0]))
    except ProviderNotReady as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@router.get("/resources/{system}/env", tags=["environment"],
            summary="Inspect a system's environment: host, workdir + logs, python/torch, modules, container runtime, Slurm accounts")
def get_env(system: str, timeout_s: int = 90):
    return _env_errors(_core().env, system, timeout_s)


@router.post("/resources/{system}/exec", tags=["environment"],
             summary="Run one bounded command on the system's login node (look around; submit tasks for real work)")
def post_exec(system: str, req: ExecRequest):
    return _env_errors(_core().exec, system, req.command, req.timeout_s)


@router.post("/resources/{system}/files", tags=["environment"],
             summary="Stage a file (base64, ≤2 MB) into the system's workdir or an absolute path")
def post_file(system: str, req: PutFileRequest):
    import base64
    try:
        content = base64.b64decode(req.content_b64, validate=True)
    except Exception:
        raise HTTPException(400, "content_b64 is not valid base64")
    return _env_errors(_core().put_file, system, req.path, content, req.mode)


@router.get("/federation", tags=["resources"], summary="Latest raw snapshot + aggregates")
def get_federation():
    return _core().snapshot()


@router.get("/federation/history", tags=["resources"], summary="Recent snapshots (per-partition utilization) for trends")
def get_federation_history(limit: int = 60):
    return _core().history(limit)


# ---------------------------------------------------------------- envelope

class EnvelopeRequest(BaseModel):
    name: str = "task"
    description: str = ""
    command: str = ""


@router.post("/envelope/generate", tags=["tasks"], summary="Draft a [min viable, max useful] ResourceEnvelope from a task description")
def post_generate_envelope(req: EnvelopeRequest):
    return _core().draft_envelope(req.name, req.description, req.command)


# ------------------------------------------------------------------- tasks

class TargetRef(BaseModel):
    """Explicit placement: any system the pool knows, optionally one partition."""
    system: str
    partition: Optional[str] = None


class SubmitTaskRequest(BaseModel):
    name: str
    description: str = ""
    command: str = "echo hello from diamond agent"
    container: Optional[str] = None
    envelope: Optional[ResourceEnvelope] = None
    auto_generate_envelope: bool = False
    max_retries: int = 2
    auto_continue: bool = True
    target: Optional[TargetRef] = None    # None => meta-scheduler chooses


def _tgt(t: Optional[TargetRef]):
    return (t.system, t.partition) if t else None


@router.post("/tasks", tags=["tasks"], summary="Submit a task: auto placement, or target={system,partition?} to pin")
def post_task(req: SubmitTaskRequest):
    try:
        return _core().submit(name=req.name, command=req.command, description=req.description,
                              envelope=req.envelope, target=_tgt(req.target), container=req.container,
                              max_retries=req.max_retries, auto_continue=req.auto_continue,
                              auto_generate_envelope=req.auto_generate_envelope)
    except UnknownTarget as e:
        raise HTTPException(404, str(e.args[0]))


@router.get("/tasks", tags=["tasks"], summary="Task summaries (status=active|COMPLETED|... filters)")
def list_tasks(status: Optional[str] = None):
    return _core().tasks(status)


@router.get("/tasks/{task_id}", tags=["tasks"], summary="Full task record: decision, candidates, verdicts, timeline")
def get_task(task_id: str):
    t = _core().task(task_id)
    if t is None:
        raise HTTPException(404, "task not found")
    return t


@router.get("/tasks/{task_id}/logs", tags=["environment"],
            summary="stdout/stderr tails of the task's job from the remote workdir")
def get_task_logs(task_id: str, tail: int = 200):
    return _env_errors(_core().task_logs, task_id, tail)


@router.post("/tasks/{task_id}/cancel", tags=["tasks"], summary="Cancel a task (and its job)")
def cancel_task(task_id: str):
    t = _core().cancel(task_id)
    if t is None:
        raise HTTPException(404, "task not found")
    return t


class ReplanRequest(BaseModel):
    exclude: Optional[list[str]] = None   # systems the re-decision may NOT use
    target: Optional[TargetRef] = None    # re-pin to a resource
    clear_target: bool = False            # lift an existing pin


@router.post("/tasks/{task_id}/replan", tags=["tasks"], summary="Re-run placement; exclude systems, re-pin, or clear the pin")
def replan_task(task_id: str, req: Optional[ReplanRequest] = None):
    req = req or ReplanRequest()
    try:
        t = _core().replan(task_id, exclude=req.exclude, target=_tgt(req.target),
                           clear_target=req.clear_target)
    except UnknownTarget as e:
        raise HTTPException(404, str(e.args[0]))
    if t is None:
        raise HTTPException(404, "task not found")
    return t


# ------------------------------------------------------------- task groups

class SubmitGroupRequest(BaseModel):
    name: str
    description: str = ""
    children: list[SubmitTaskRequest]
    rounds: int = Field(default=1, ge=1, le=10)
    distinct_systems: bool = True


@router.post("/taskgroups", tags=["tasks"], summary="Submit a parent task with children spread across systems")
def post_taskgroup(req: SubmitGroupRequest):
    children = [{**c.model_dump(exclude={"target"}), "envelope": c.envelope} for c in req.children]
    return _core().submit_group(req.name, children, rounds=req.rounds,
                                distinct_systems=req.distinct_systems, description=req.description)


@router.get("/taskgroups", tags=["tasks"], summary="Task groups with their children")
def list_taskgroups():
    return _core().groups()


# ------------------------------------------------------------------ events

@router.get("/events", tags=["events"], summary="Agent activity feed (decisions, dispatches, continuations)")
def list_events(limit: int = 100, task_id: Optional[str] = None):
    return _core().events(limit=limit, task_id=task_id)


# ------------------------------------------------------------------ skill
# The agent skill ships with the repo (.claude/skills/diamond-agent-core/SKILL.md)
# and is also served by the core itself, so an agent that only knows the URL
# can fetch how to drive the system. /skill/api is generated from the live
# OpenAPI schema and therefore never drifts from the routes.

SKILL_PATH = Path(__file__).resolve().parent.parent / ".claude" / "skills" / "diamond-agent-core" / "SKILL.md"
skill_router = APIRouter(tags=["skill"])


def api_reference_markdown(app) -> str:
    """Per-endpoint list generated from the live OpenAPI schema (never drifts)."""
    schema = app.openapi()
    comps = schema.get("components", {}).get("schemas", {})
    lines = ["# diamond-agent-core — HTTP API reference (generated from the live OpenAPI schema)", "",
             "Base URL = the core's origin (default http://127.0.0.1:8642). JSON in, JSON out unless noted. "
             "Interactive docs: `/docs`; raw schema: `/openapi.json`.", ""]
    by_tag: dict = {}
    for path, ops in schema.get("paths", {}).items():
        for method, op in ops.items():
            by_tag.setdefault((op.get("tags") or ["other"])[0], []).append((path, method, op))
    for tag in ("status", "resources", "environment", "tasks", "events", "auth", "skill", "other"):
        if tag not in by_tag:
            continue
        lines.append(f"## {tag}")
        for path, method, op in sorted(by_tag[tag]):
            lines.append(f"- **{method.upper()} `{path}`** — {op.get('summary') or ''}")
            params = [f"`{q['name']}`{'*' if q.get('required') else ''}" for q in op.get("parameters", []) if q.get("in") == "query"]
            if params:
                lines.append(f"  - query: {', '.join(params)}")
            body = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {})
            ref = body.get("$ref", "").split("/")[-1]
            if ref and ref in comps:
                props = comps[ref].get("properties", {})
                req = set(comps[ref].get("required", []))
                fields = []
                for k, v in props.items():
                    t = v.get("type") or ("object" if "$ref" in v else "/".join(x.get("type", "?") for x in v.get("anyOf", [])) or "?")
                    fields.append(f"`{k}`{'*' if k in req else ''}: {t}")
                lines.append(f"  - body: {', '.join(fields)}")
        lines.append("")
    lines += ["`*` = required. `target` = `{\"system\": ..., \"partition\": ...|null}` pins a task; omit it to let the "
              "meta-scheduler choose. System names contain `@` (e.g. `Delta@NCSA`) — URL-encode them in paths."]
    return "\n".join(lines)


@skill_router.get("/skill", summary="Agent skill: how to drive this system (Markdown)")
def get_skill(request: Request):
    text = SKILL_PATH.read_text() if SKILL_PATH.exists() else "# diamond-agent-core\n(skill file missing)\n"
    text += "\n\n---\n\n" + api_reference_markdown(request.app)
    text += f"\n\nThis skill lives at `{SKILL_PATH}` in the repo and at `{request.base_url}skill` on the running core."
    return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")


@skill_router.get("/skill/api", summary="HTTP API reference (Markdown, generated)")
def get_skill_api(request: Request):
    return PlainTextResponse(api_reference_markdown(request.app), media_type="text/markdown; charset=utf-8")
