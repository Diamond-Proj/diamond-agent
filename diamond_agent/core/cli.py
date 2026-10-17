"""diamond-agent-core CLI — the shell surface other agents (and people) use.

Talks to a running core over HTTP (DIAMOND_AGENT_URL, default
http://127.0.0.1:8642). Every command accepts --json for machine output.

  diamond-agent-core status                    # supercomputers + queues, Markdown (agent view)
  diamond-agent-core skill                     # how to drive this system + API reference
  diamond-agent-core skill --systems           # per-system quirks (accounts, QOS, directives, containers)
  diamond-agent-core resources                 # all compute in one table
  diamond-agent-core policy                    # scheduling limits per partition (read-only; edited in the web UI)
  diamond-agent-core env Delta@NCSA            # what the target offers (python/torch, modules, workdir, logs)
  diamond-agent-core exec Delta@NCSA "module avail 2>&1 | grep -i torch"
  diamond-agent-core put-file Delta@NCSA train.py --to scripts/train.py
  diamond-agent-core logs <task id> [--tail 100]
  diamond-agent-core idle --gpus 2 --gpu-mem 40
  diamond-agent-core submit --name demo --command "python train.py" \
        --description "LoRA fine-tune 7B" --auto-envelope --wait
  diamond-agent-core submit --name demo --command "..." --target DeltaAI@NCSA:ghx4
  diamond-agent-core task <id> | wait <id> | cancel <id> | replan <id> --clear-target
  diamond-agent-core events --task <id>
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .client import CoreClient, CoreError


def _target(s):
    if not s:
        return None
    system, _, part = s.partition(":")
    return {"system": system, "partition": part or None}


def _table(rows, cols):
    if not rows:
        print("(none)")
        return
    widths = [max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols]
    print("  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(w) for c, w in zip(cols, widths)))


def cmd_status(c, a):
    return c.status() if a.json else print(c.status_text())


def cmd_skill(c, a):
    print(c.skill_systems() if a.systems else c.skill())


def cmd_env(c, a):
    e = c.env(a.system, timeout_s=a.timeout)
    if a.json:
        return e
    print(f"{e['system']}  host {e['host']}  {e['arch']}  {e['os'] or ''}\nuser {e['user']}  home {e['home']}")
    print(f"workdir {e['workdir']}  ({'exists' if e['workdir_exists'] else 'MISSING — created on first submit'})")
    print(f"python {e['python']}   {e['torch'] or 'torch: not importable without a module'}")
    print(f"container runtime: {e['container_runtime'] or '-'}")
    print("modules of interest:", ", ".join(e["modules"][:25]) or "-")
    if e.get("slurm"):
        print("slurm:", e["slurm"].replace(chr(10), " | ")[:300])
    if e.get("storage"):
        print("storage:", e["storage"].replace(chr(10), " | ")[:200])
    wd = e["sections"].get("workdir", "")
    if "--- logs" in wd:
        print("recent logs:", " ".join(wd.split("--- logs")[1].split()[:8]))


def cmd_exec(c, a):
    r = c.exec(a.system, " ".join(a.command), timeout_s=a.timeout)
    if a.json:
        return r
    print(r["stdout"], end="" if r["stdout"].endswith("\n") else "\n")
    if r["stderr"]:
        print(r["stderr"], file=sys.stderr)
    return None if r["returncode"] == 0 else _exit(r["returncode"])


def _exit(code):
    sys.exit(code)


def cmd_put_file(c, a):
    content = open(a.local, "rb").read()
    r = c.put_file(a.system, a.to or os.path.basename(a.local), content, mode=a.mode)
    return r if a.json else print(f"staged {r['bytes']} bytes -> {r['system']}:{r['path']}")


def cmd_logs(c, a):
    r = c.task_logs(a.task_id, tail=a.tail)
    if a.json:
        return r
    print(f"{r['task_id']}  {r.get('status')}  {r.get('system')}  job {r.get('job_id')}")
    if r.get("note"):
        print(r["note"]); return
    print(f"--- stdout ({r['stdout_path']})"); print(r["stdout"] or "(empty)")
    print(f"--- stderr ({r['stderr_path']})"); print(r["stderr"] or "(empty)")


def cmd_resources(c, a):
    res = c.resources()
    if a.json:
        return res
    rows = []
    for s in res["systems"]:
        for p in s["partitions"]:
            free = p["gpus_free"] if p["gpu_type"] else p["cpus_free"]
            rows.append({"system": s["name"], "partition": p["partition"], "provider": p["provider"],
                         "health": p["endpoint_health"],
                         "hardware": (f"{p['gpu_type']}x{p['gpus_per_node']} {p['gpu_mem_gb']}GB" if p["gpu_type"]
                                      else f"{p['cpus_per_node']}c {p['mem_per_node_gb']}GB"),
                         "free": f"{free} {'GPU' if p['gpu_type'] else 'cores'}",
                         "run/pend": f"{p['running_jobs']}/{p['pending_jobs']}",
                         "mine": p["user_running_jobs"] + p["user_pending_jobs"],
                         "max_wall": p["max_walltime_minutes"],
                         "max_nodes": (s.get("limits") or {}).get(p["partition"], {}).get("max_nodes_per_job", "")})
        if not s["partitions"]:
            rows.append({"system": s["name"], "partition": "-", "provider": s["provider"],
                         "health": "reserved"})
    _table(rows, ["system", "partition", "provider", "health", "hardware", "free", "run/pend", "mine", "max_wall", "max_nodes"])
    print(f"\nproviders: " + ", ".join(f"{p['kind']}={p['status']}" for p in res["providers"]))


def cmd_policy(c, a):
    pol = c.policy()
    if a.json:
        return pol
    print(f"defaults: max nodes/job {pol['max_nodes_per_job']} · max pending jobs/system "
          f"{pol['max_user_pending_per_system']}   (read-only here; the operator edits limits in the web UI)")
    rows = [{"system": r["system"], "partition": r["partition"],
             "max nodes/job": f"{r['max_nodes_per_job']}{' (override)' if r['override'] is not None else ''}",
             "blocked": "yes" if r["blocked"] else "", "nodes total": r["nodes_total"] if r["nodes_total"] is not None else "-"}
            for r in pol["partitions"]]
    _table(rows, ["system", "partition", "max nodes/job", "blocked", "nodes total"])


def cmd_providers(c, a):
    p = c.providers()
    return p if a.json else _table(p, ["kind", "status", "label", "detail"])


def cmd_idle(c, a):
    rows = c.idle(min_gpus=a.gpus, min_gpu_mem_gb=a.gpu_mem, gpu_type=a.gpu_type, arch=a.arch,
                  max_pending=a.max_pending, include_cpu=not a.gpu_only)
    if a.json:
        return rows
    for r in rows:
        r["free"] = f"{r['free']} {r['unit']}"
        r["why_not"] = "; ".join(r["reasons"])
    _table(rows, ["available", "system", "partition", "gpu_type", "free", "pending_jobs", "why_not"])


def cmd_envelope(c, a):
    r = c.draft_envelope(a.name, a.description or "", a.command or "")
    if a.json:
        return r
    print(f"[{r['source']} · confidence {r['confidence']:.2f}] {r['rationale']}\n")
    print(json.dumps(r["envelope"], indent=2))


def cmd_submit(c, a):
    env = None
    if a.envelope_json:
        env = json.load(sys.stdin if a.envelope_json == "-" else open(a.envelope_json))
    r = c.submit(name=a.name, command=a.command, description=a.description or "", envelope=env,
                 target=_target(a.target), container=a.container, max_retries=a.retries,
                 auto_continue=not a.no_continue, auto_generate_envelope=a.auto_envelope or env is None)
    t = r["task"]
    if a.wait:
        t = c.wait(t["task_id"], timeout_s=a.timeout)
        t = {k: t.get(k) for k in ("task_id", "status", "job_id", "attempts")} | {
            "system": (t.get("placement") or {}).get("system"), "partition": (t.get("placement") or {}).get("partition")}
    if a.json:
        return {"task": t, "envelope": r["envelope"], "envelope_rationale": r["envelope_rationale"]}
    print(f"task {t['task_id']}  {t['status']}  " + (f"{t['system']}:{t['partition']}" if t.get("system") else "(no placement)")
          + (f"  job {t['job_id']}" if t.get("job_id") else ""))
    if r["envelope_rationale"]:
        print(f"envelope: {r['envelope_rationale']}")


def cmd_task(c, a):
    t = c.task(a.task_id)
    if a.json:
        return t
    pl = t.get("placement") or {}
    print(f"{t['spec']['name']}  {t['status']}  {pl.get('system', '-')}:{pl.get('partition', '-')}  job {t.get('job_id') or '-'}  attempts {t['attempts']}")
    if t.get("target_system"):
        print(f"pinned to {t['target_system']}{':' + t['target_partition'] if t.get('target_partition') else ''}")
    d = t.get("decision")
    if d:
        print(f"decision: {d['mode']} — {d.get('summary', '')}")
        for cand in d.get("candidates", []):
            mark = "*" if d.get("chosen") and cand["system"] == d["chosen"]["system"] and cand["partition"] == d["chosen"]["partition"] else " "
            failed = [v["rule"] for v in cand.get("verdicts", []) if not v["passed"]]
            print(f"  {mark} {cand['system']}:{cand['partition']:<12} {'feasible' if cand['feasible'] else 'rejected: ' + ', '.join(failed)}"
                  + (f"  rank {cand['ai_rank']}" if cand.get("ai_rank") else ""))
    print("timeline:")
    for e in t.get("timeline", [])[-12:]:
        print(f"  {e['ts'][11:19]}  {e['kind']:<24} {e['message'][:110]}")


def cmd_tasks(c, a):
    rows = c.tasks(status=None if a.all else "active")
    if a.json:
        return rows
    for r in rows:
        r["placement"] = f"{r['system']}:{r['partition']}" if r["system"] else "-"
        r["pin"] = r["target_system"] or ""
    _table(rows, ["task_id", "name", "status", "placement", "job_id", "attempts", "mode", "pin"])


def cmd_wait(c, a):
    t = c.wait(a.task_id, timeout_s=a.timeout)
    return t if a.json else print(f"{t['task_id']}  {t['status']}  job {t.get('job_id') or '-'}")


def cmd_cancel(c, a):
    t = c.cancel(a.task_id)
    return t if a.json else print(f"{t['task_id']}  {t['status']}")


def cmd_replan(c, a):
    t = c.replan(a.task_id, exclude=a.exclude, target=_target(a.target), clear_target=a.clear_target)
    return t if a.json else print(f"{t['task_id']}  {t['status']}  " + (f"{t['system']}:{t['partition']}" if t["system"] else "(no placement)"))


def cmd_auth(c, a):
    st = c._call("GET", "/auth/session")
    if a.json:
        return st
    u = st.get("user") or {}
    print(f"mode: {st['mode']}  authenticated: {st['authenticated']}  required: {st['required']}")
    if u:
        print(f"user: {u.get('name') or ''} {u.get('email') or ''} ({u.get('sub') or ''})")
    print(f"hint: {st.get('hint')}")
    if st.get("web_login_available"):
        print("web sign-in: available at <core url>/auth/login (frontend)")


def cmd_events(c, a):
    ev = c.events(limit=a.limit, task_id=a.task)
    if a.json:
        return ev
    for e in reversed(ev):
        print(f"{e['ts'][11:19]}  {e['kind']:<24} {(e['task_id'] or '')[:8]:<8} {e['message'][:120]}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="diamond-agent-core", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=None, help="core base URL (default $DIAMOND_AGENT_URL or http://127.0.0.1:8642)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="supercomputers and queues at a glance (Markdown; --json for data)").set_defaults(fn=cmd_status)
    s = sub.add_parser("skill", help="print the agent skill: how to call this system + API reference"); s.set_defaults(fn=cmd_skill)
    s.add_argument("--systems", action="store_true", help="print the per-system notes (quirks and caveats of every supported machine) instead")
    sub.add_parser("resources", help="every partition of every provider").set_defaults(fn=cmd_resources)
    s = sub.add_parser("env", help="inspect a system's environment (workdir, python/torch, modules, accounts)"); s.set_defaults(fn=cmd_env)
    s.add_argument("system"); s.add_argument("--timeout", type=int, default=90)
    s = sub.add_parser("exec", help="run one bounded command on a system's login node"); s.set_defaults(fn=cmd_exec)
    s.add_argument("system"); s.add_argument("command", nargs="+"); s.add_argument("--timeout", type=int, default=60)
    s = sub.add_parser("put-file", help="stage a local file into the system's workdir"); s.set_defaults(fn=cmd_put_file)
    s.add_argument("system"); s.add_argument("local"); s.add_argument("--to", help="remote path (relative to workdir, or absolute)"); s.add_argument("--mode", default="0644")
    s = sub.add_parser("logs", help="stdout/stderr tails of a task's job"); s.set_defaults(fn=cmd_logs)
    s.add_argument("task_id"); s.add_argument("--tail", type=int, default=200)
    sub.add_parser("providers", help="provider kinds and their status").set_defaults(fn=cmd_providers)
    sub.add_parser("policy", help="scheduling limits (per-job node cap per partition, pending cap, blocked partitions) — read-only").set_defaults(fn=cmd_policy)
    s = sub.add_parser("idle", help="partitions that can start work now"); s.set_defaults(fn=cmd_idle)
    s.add_argument("--gpus", type=int, default=1); s.add_argument("--gpu-mem", type=float, default=0)
    s.add_argument("--gpu-type"); s.add_argument("--arch"); s.add_argument("--max-pending", type=int, default=10**9)
    s.add_argument("--gpu-only", action="store_true")
    s = sub.add_parser("envelope", help="draft a resource envelope with the agent"); s.set_defaults(fn=cmd_envelope)
    s.add_argument("--name", required=True); s.add_argument("--description"); s.add_argument("--command")
    s = sub.add_parser("submit", help="submit a task (auto placement, or --target to pin)"); s.set_defaults(fn=cmd_submit)
    s.add_argument("--name", required=True); s.add_argument("--command", required=True); s.add_argument("--description")
    s.add_argument("--target", help="SYSTEM or SYSTEM:PARTITION to pin the task to")
    s.add_argument("--envelope-json", help="file with a ResourceEnvelope JSON, or - for stdin")
    s.add_argument("--auto-envelope", action="store_true", help="let the agent draft the envelope (default when none given)")
    s.add_argument("--container"); s.add_argument("--retries", type=int, default=2)
    s.add_argument("--no-continue", action="store_true", help="disable agent continuation")
    s.add_argument("--wait", action="store_true"); s.add_argument("--timeout", type=float, default=3600)
    s = sub.add_parser("task", help="full task record incl. decision and timeline"); s.set_defaults(fn=cmd_task); s.add_argument("task_id")
    s = sub.add_parser("tasks", help="task list (active by default)"); s.set_defaults(fn=cmd_tasks); s.add_argument("--all", action="store_true")
    s = sub.add_parser("wait", help="block until a task is terminal"); s.set_defaults(fn=cmd_wait); s.add_argument("task_id"); s.add_argument("--timeout", type=float, default=3600)
    s = sub.add_parser("cancel"); s.set_defaults(fn=cmd_cancel); s.add_argument("task_id")
    s = sub.add_parser("replan", help="re-run placement; --clear-target lifts a pin, --target re-pins"); s.set_defaults(fn=cmd_replan)
    s.add_argument("task_id"); s.add_argument("--exclude", nargs="*"); s.add_argument("--target"); s.add_argument("--clear-target", action="store_true")
    sub.add_parser("auth", help="credential state of the core (web session / CLI tokens)").set_defaults(fn=cmd_auth)
    s = sub.add_parser("events", help="agent activity feed"); s.set_defaults(fn=cmd_events); s.add_argument("--task"); s.add_argument("--limit", type=int, default=40)
    a = p.parse_args(argv)
    client = CoreClient(a.url) if a.url else CoreClient()
    try:
        out = a.fn(client, a)
    except CoreError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if a.json and out is not None:
        print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
