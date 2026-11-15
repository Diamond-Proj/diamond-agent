"""HTTP interface of diamond-agent-core.

Thin FastAPI router over ``DiamondAgentCore`` — every route is one facade
call, so the web frontend, the CLI, the MCP server and external agents share
one contract (OpenAPI at /docs). No scheduling or provider logic lives here.

Two mounts of the same router (see main.py):

* ``/c/{token}/api`` — **agents**, through their channel URL. Everything is
  scoped to that channel: its queues, account and caps; its tasks and events.
  This is the documented contract (OpenAPI, ``/skill/api``).
* ``/api`` — the **operator's** web UI, unscoped (every channel, every
  machine). Agent actions (submit, candidates, exec, files, retarget) need
  a channel and are refused here; channel and machine-rule edits live under
  ``/ui`` with the operator cookie.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import Base64Bytes, BaseModel, Field

from .agent.orchestrator import Infeasible
from .channels import Channel, ChannelRules
from .core.service import DiamondAgentCore
from .policy import PolicyDenied
from .releases import Conflict, Source
from .schemas import ResourceEnvelope

CHANNEL_PREFIX = "/c/{token}"


class AppContext:
    core: DiamondAgentCore


ctx = AppContext()
router = APIRouter(prefix="/api")


def _core() -> DiamondAgentCore:
    return ctx.core


def current_channel(request: Request) -> Optional[Channel]:
    """The channel named by the URL (``/c/{token}/…``), or None for the operator's unscoped ``/api``."""
    token = request.path_params.get("token")
    if token is None:
        return None
    channel = _core().channels.by_token(token)
    if channel is None:
        raise HTTPException(404, "unknown channel")
    return channel


Scope = Annotated[Optional[Channel], Depends(current_channel)]


def _agent(channel: Optional[Channel]) -> Channel:
    """Agent actions go through a channel URL; the operator's /api has none."""
    if channel is None:
        raise HTTPException(400, "this action goes through a channel URL (<core>/c/<token>); "
                                 "ask the operator for one")
    return channel


def _errors(fn, *args, **kw):
    """Map the facade's exceptions onto HTTP."""
    from .providers import ProviderNotReady
    try:
        return fn(*args, **kw)
    except KeyError as e:
        raise HTTPException(404, str(e.args[0]))
    except Infeasible as e:
        raise HTTPException(409, {"message": str(e), "verdicts": [v.model_dump() for v in e.verdicts]})
    except Conflict as e:
        raise HTTPException(409, str(e))
    except PolicyDenied as e:
        raise HTTPException(403, str(e))
    except ProviderNotReady as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))


# ------------------------------------------------------------------ status

@router.get("/config", tags=["status"], summary="Service info: auth state, your channel, systems, limits in force, providers")
def get_config(channel: Scope):
    return _core().info(channel)


@router.get("/policy", tags=["status"],
            summary="Limits in force (per-queue node cap, pending cap, account) — your channel's rules inside the machine rules; read-only")
def get_policy(channel: Scope):
    return _core().policy_view(channel)


@router.get("/providers", tags=["resources"], summary="Resource providers and their status (active / reserved / needs-login)")
def get_providers():
    return _core().providers()


# ---------------------------------------------------------- queue status

@router.get("/status", tags=["resources"], summary="Your channel's supercomputers and queues at a glance")
def get_status(channel: Scope, format: str = "json"):
    """Per-partition free units, utilization, node states, queue depth, my jobs,
    wall-time cap, node cap and the wait heuristic. ``?format=text`` returns a
    Markdown table an agent can paste straight into its context."""
    if format == "text":
        return PlainTextResponse(_core().status_text(channel), media_type="text/markdown; charset=utf-8")
    return _core().status(channel)


# --------------------------------------------------------------- resources

@router.get("/resources", tags=["resources"], summary="Your channel's compute resources in one list (full records per partition)")
def get_resources(channel: Scope):
    return _core().resources(channel)


@router.get("/resources/idle", tags=["resources"], summary="Partitions that can start work now, with reasons for the rest")
def get_idle(channel: Scope, min_gpus: int = 1, min_gpu_mem_gb: float = 0, gpu_type: Optional[str] = None,
             arch: Optional[str] = None, max_pending: int = 10**9, include_cpu: bool = True,
             provider: Optional[str] = None):
    return _core().idle(min_gpus=min_gpus, min_gpu_mem_gb=min_gpu_mem_gb, gpu_type=gpu_type,
                        arch=arch, max_pending=max_pending, include_cpu=include_cpu, provider=provider,
                        channel=channel)


class ExecRequest(BaseModel):
    command: str = Field(min_length=1)
    timeout_s: int = Field(default=60, ge=1, le=600)


class PutFileRequest(BaseModel):
    path: str                       # relative to the system's workdir
    content_b64: Base64Bytes        # decoded on the way in
    mode: str = Field(default="0644", pattern=r"^0?[0-7]{3}$")


@router.get("/resources/{system}/env", tags=["environment"],
            summary="Inspect a system's environment: host, workdir + logs, python/torch, modules, container runtime, Slurm accounts")
def get_env(system: str, channel: Scope, timeout_s: int = 90):
    return _errors(_core().env, system, _agent(channel), timeout_s)


@router.post("/resources/{system}/exec", tags=["environment"],
             summary="Run one bounded, inspection-only command on the system's login node (job submission/control is refused: submit a task)")
def post_exec(system: str, req: ExecRequest, channel: Scope):
    return _errors(_core().exec, system, req.command, _agent(channel), req.timeout_s)


@router.post("/resources/{system}/files", tags=["environment"],
             summary="Stage a file (base64, ≤2 MB) under the system's workdir (relative paths only)")
def post_file(system: str, req: PutFileRequest, channel: Scope):
    return _errors(_core().put_file, system, req.path, bytes(req.content_b64), _agent(channel), req.mode)


@router.get("/federation", tags=["resources"], summary="Latest raw snapshot + aggregates")
def get_federation(channel: Scope):
    return _core().snapshot(channel)


@router.get("/federation/history", tags=["resources"], summary="Recent snapshots (per-partition utilization) for trends")
def get_federation_history(channel: Scope, limit: int = 60):
    return _core().history(limit, channel)


# ------------------------------------------------------------------- tasks

class TargetRef(BaseModel):
    """The queue the agent chose: a system and one of its partitions."""
    system: str
    partition: str


class CandidatesRequest(BaseModel):
    envelope: ResourceEnvelope


@router.post("/candidates", tags=["tasks"],
             summary="Evaluate an envelope against every queue of your channel — verdicts, sizing, wait estimate, free units, caps — and choose from the result")
def post_candidates(req: CandidatesRequest, channel: Scope):
    return _core().candidates(req.envelope, _agent(channel))


class SubmitTaskRequest(BaseModel):
    """A task as the calling agent wrote it: command and envelope are its
    work (the skill says how); the core only sizes, runs and watches it."""
    name: str
    command: str
    envelope: ResourceEnvelope
    target: TargetRef                     # the queue you chose (see POST /api/candidates)
    description: str = ""
    container: Optional[str] = None
    release: Optional[int] = None         # the channel release the job runs in (cwd = its snapshot, DA_ENV = its env); see /releases
    max_retries: int = 2
    auto_continue: bool = True


@router.post("/tasks", tags=["tasks"],
             summary="Submit a task (command + envelope) to the queue you chose; 409 with the verdicts if that queue cannot run it, or if it is not aligned to the task's release")
def post_task(req: SubmitTaskRequest, channel: Scope):
    return _errors(_core().submit, name=req.name, command=req.command, envelope=req.envelope,
                   target=(req.target.system, req.target.partition), channel=_agent(channel),
                   description=req.description, container=req.container, max_retries=req.max_retries,
                   auto_continue=req.auto_continue, release=req.release)


@router.get("/tasks", tags=["tasks"], summary="Task summaries (status=active|COMPLETED|... filters)")
def list_tasks(channel: Scope, status: Optional[str] = None, channel_id: Optional[str] = None):
    return _core().tasks(status, channel, channel_id)


@router.get("/tasks/{task_id}", tags=["tasks"], summary="Full task record: the queue's evaluation, placement, timeline")
def get_task(task_id: str, channel: Scope):
    return _errors(_core().task, task_id, channel)


@router.get("/tasks/{task_id}/logs", tags=["environment"],
            summary="stdout/stderr tails of the task's job from the remote workdir")
def get_task_logs(task_id: str, channel: Scope, tail: int = 200):
    return _errors(_core().task_logs, task_id, _agent(channel), tail)


@router.post("/tasks/{task_id}/cancel", tags=["tasks"], summary="Cancel a task (and its job)")
def cancel_task(task_id: str, channel: Scope):
    return _errors(_core().cancel, task_id, channel)


class RetargetRequest(BaseModel):
    target: TargetRef


@router.post("/tasks/{task_id}/retarget", tags=["tasks"],
             summary="Move a task to another queue you chose (its current job is cancelled); 409 if that queue cannot run it")
def retarget_task(task_id: str, req: RetargetRequest, channel: Scope):
    return _errors(_core().retarget, task_id, (req.target.system, req.target.partition), _agent(channel))


# ------------------------------------------------------------------ releases

def _release_scope(channel: Optional[Channel], channel_id: Optional[str]) -> Channel:
    """Agents read their own channel's releases; the operator's UI names the channel."""
    if channel is not None:
        return channel
    found = _core().channels.get(channel_id) if channel_id else None
    if found is None:
        raise HTTPException(400, "this action goes through a channel URL (<core>/c/<token>), or channel_id for the operator")
    return found


class ReleaseBody(BaseModel):
    """A code tree you packed (tar, optionally gzip — the CLI/MCP pack a directory
    or `git archive <ref>` for you) with its provenance."""
    archive_b64: Base64Bytes
    source: Source
    base_version: int                     # the channel's latest version you built on: 0 for the first; 409 if someone published since
    env: Optional[str] = None             # environment reference the job receives as DA_ENV (a module line, an image path)
    systems: Optional[list[str]] = None   # machines the release targets; None = every machine of your channel


class AlignBody(BaseModel):
    system: str


@router.get("/releases", tags=["releases"],
            summary="Your channel's releases — code tree + environment + target machines as one version — with the build on each machine")
def list_releases(channel: Scope, channel_id: Optional[str] = None):
    return _core().releases_view(_release_scope(channel, channel_id))


@router.post("/releases", tags=["releases"],
             summary="Create the channel's next release from the archive you packed; base_version must be the latest (compare-and-swap, 409 otherwise)")
def post_release(req: ReleaseBody, channel: Scope):
    return _errors(_core().create_release, _agent(channel), bytes(req.archive_b64), req.source, req.base_version,
                   req.env, req.systems)


@router.get("/releases/{version}", tags=["releases"], summary="One release and its builds per machine")
def get_release(version: int, channel: Scope, channel_id: Optional[str] = None):
    return _errors(_core().release_view, _release_scope(channel, channel_id), version)


@router.post("/releases/{version}/align", tags=["releases"],
             summary="Materialise the release on one machine (background); poll GET /releases/{version} until its build is ready or failed")
def align_release(version: int, req: AlignBody, channel: Scope):
    return _errors(_core().align_release, _agent(channel), version, req.system)


@router.post("/releases/{version}/verify", tags=["releases"],
             summary="Re-check the snapshot on one machine against the manifest: ready, or stale with what differs")
def verify_release(version: int, req: AlignBody, channel: Scope):
    return _errors(_core().verify_release, _agent(channel), version, req.system)


# ------------------------------------------------------------------ events

@router.get("/events", tags=["events"], summary="Activity feed: submissions, dispatches, retries, exec, staged files, refusals")
def list_events(channel: Scope, limit: int = 100, task_id: Optional[str] = None, channel_id: Optional[str] = None):
    return _core().events(limit=limit, task_id=task_id, channel=channel, channel_id=channel_id)


# ---------------------------------------------------------- operator (UI)
# Mounted only with the web UI (main.py). Changing the machine rules or the
# channels is an operator action: it needs the cookie the core hands the
# browser that loaded the UI. The CLI, the MCP server and the generated API
# reference carry no such route, so agents can read limits but never raise
# them, and never see another channel's URL.

ui_router = APIRouter(prefix="/ui", include_in_schema=False)
Cap = Annotated[int, Field(ge=1)]


def _operator(request: Request) -> None:
    from .core import webauth
    if not webauth.is_operator(request):
        raise HTTPException(403, "this is an operator action: use the web UI")


class PolicyPatch(BaseModel):
    """A field that is present replaces the stored override; null returns it
    to the catalog value. partition_limits is the whole "System:partition" ->
    max-nodes map (a null entry clears that queue's override)."""
    max_nodes_per_job: Optional[Cap] = None
    max_user_pending_per_system: Optional[Cap] = None
    blocked_partitions: Optional[list[str]] = None
    partition_limits: Optional[dict[str, Optional[Cap]]] = None
    node_hours: Optional[dict[str, Optional[Annotated[float, Field(gt=0)]]]] = None   # machine -> budget (null clears)


@ui_router.put("/policy")
def ui_put_policy(req: PolicyPatch, request: Request):
    _operator(request)
    return _errors(_core().update_policy, req.model_dump(exclude_unset=True))


class ChannelBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = ""
    rules: Optional[ChannelRules] = None      # None on create => every machine, machine rules


class ChannelPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    description: Optional[str] = None
    rules: Optional[ChannelRules] = None


@ui_router.get("/channels")
def ui_channels(request: Request):
    _operator(request)
    return _core().channels_view()


@ui_router.post("/channels")
def ui_create_channel(req: ChannelBody, request: Request):
    _operator(request)
    return _errors(_core().create_channel, req.name, req.description, req.rules)


@ui_router.get("/channels/{channel_id}")
def ui_channel(channel_id: str, request: Request):
    _operator(request)
    channel = _core().channels.get(channel_id)
    if channel is None:
        raise HTTPException(404, "unknown channel")
    return _core().channel_view(channel)


@ui_router.put("/channels/{channel_id}")
def ui_update_channel(channel_id: str, req: ChannelPatch, request: Request):
    _operator(request)
    out = _errors(_core().update_channel, channel_id, name=req.name, description=req.description, rules=req.rules)
    if out is None:
        raise HTTPException(404, "unknown channel")
    return out


@ui_router.delete("/channels/{channel_id}")
def ui_delete_channel(channel_id: str, request: Request):
    _operator(request)
    if not _errors(_core().delete_channel, channel_id):
        raise HTTPException(404, "unknown channel")
    return {"deleted": channel_id}


# ------------------------------------------------------------------ skill
# The agent skill ships with the repo (.claude/skills/diamond-agent-core/SKILL.md)
# and is also served by the core itself, so an agent that only knows its
# channel URL can fetch how to drive the system. /skill/api is generated from
# the live OpenAPI schema and therefore never drifts from the routes;
# /skill/systems is the skill's per-system notes page (references/systems.md).

SKILL_DIR = Path(__file__).resolve().parent.parent / ".claude" / "skills" / "diamond-agent-core"
SKILL_PATH = SKILL_DIR / "SKILL.md"
SYSTEMS_NOTES_PATH = SKILL_DIR / "references" / "systems.md"
skill_router = APIRouter(tags=["skill"])


def api_reference_markdown(app) -> str:
    """Per-endpoint list generated from the live OpenAPI schema (never drifts)."""
    schema = app.openapi()
    comps = schema.get("components", {}).get("schemas", {})
    lines = ["# diamond-agent-core — HTTP API reference (generated from the live OpenAPI schema)", "",
             "Base URL = **your channel URL** (`<core>/c/<token>`, what `DIAMOND_AGENT_URL` holds); the paths "
             "below are relative to it. JSON in, JSON out unless noted. Interactive docs: `<core>/docs`.", ""]
    by_tag: dict = {}
    for path, ops in schema.get("paths", {}).items():
        shown = path[len(CHANNEL_PREFIX):] if path.startswith(CHANNEL_PREFIX) else path
        for method, op in ops.items():
            by_tag.setdefault((op.get("tags") or ["other"])[0], []).append((shown, method, op))
    for tag in ("status", "resources", "environment", "tasks", "events", "auth", "skill", "other"):
        if tag not in by_tag:
            continue
        lines.append(f"## {tag}")
        for path, method, op in sorted(by_tag[tag]):
            lines.append(f"- **{method.upper()} `{path}`** — {op.get('summary') or ''}")
            params = [f"`{q['name']}`{'*' if q.get('required') else ''}" for q in op.get("parameters", [])
                      if q.get("in") == "query"]
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
    lines += ["`*` = required. `target` = `{\"system\": ..., \"partition\": ...}` is the queue you chose; "
              "`POST /api/candidates` evaluates an envelope against every queue of your channel so you can choose. "
              "System names contain `@` (e.g. `Delta@NCSA`) — URL-encode them in paths.",
              "Limits (`GET /api/policy`) are read-only here: your channel's per-queue node caps, account and pending cap, "
              "set by the operator inside the machine rules. `exec` is inspection-only and `files` stays under the "
              "workdir; refusals come back as HTTP 403 and are logged in `/api/events` as `POLICY_DENIED`."]
    return "\n".join(lines)


def channel_brief_markdown(core: DiamondAgentCore, channel: Channel) -> str:
    view = core.policy_view(channel)
    lines = [f"## Your channel: {channel.name}", channel.description or "", "",
             "| queue | account | max nodes/job |", "|---|---|---|"]
    lines += [f"| {r['system']}:{r['partition']} | {r['account'] or 'catalog default'} | {r['max_nodes_per_job']} |"
              for r in view["partitions"]]
    lines += ["", f"max pending jobs per system: {view['max_user_pending_per_system']}. Only these queues exist for you; "
                  "`candidates` and `status` show them with your account and caps."]
    return "\n".join(lines)


@skill_router.get("/skill", summary="Agent skill: how to drive this system (Markdown)")
def get_skill(request: Request, channel: Scope):
    text = SKILL_PATH.read_text() if SKILL_PATH.exists() else "# diamond-agent-core\n(skill file missing)\n"
    if channel is not None:
        text += "\n\n---\n\n" + channel_brief_markdown(_core(), channel)
    text += "\n\n---\n\n" + api_reference_markdown(request.app)
    text += (f"\n\nThis skill lives at `{SKILL_PATH}` in the repo and at `<your channel URL>/skill` on the running core. "
             f"Per-system quirks and caveats: `<your channel URL>/skill/systems`.")
    return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")


@skill_router.get("/skill/api", summary="HTTP API reference (Markdown, generated)")
def get_skill_api(request: Request):
    return PlainTextResponse(api_reference_markdown(request.app), media_type="text/markdown; charset=utf-8")


@skill_router.get("/skill/systems", summary="Per-system notes: quirks and caveats of every supported supercomputer (Markdown)")
def get_skill_systems(request: Request):
    text = (SYSTEMS_NOTES_PATH.read_text() if SYSTEMS_NOTES_PATH.exists()
            else "# Supported systems\n(reference file missing)\n")
    text += f"\n\nThis page lives at `{SYSTEMS_NOTES_PATH}` in the repo and at `<your channel URL>/skill/systems` on the running core."
    return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")
