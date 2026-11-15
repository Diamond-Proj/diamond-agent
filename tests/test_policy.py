"""The policy and channel boundary, offline (no Globus, no network):

    uv run --no-sync python -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import os
import time
import subprocess
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DIAMOND_AGENT_UI", "1")

from fastapi.testclient import TestClient

from diamond_agent import api, auth, policy
from diamond_agent import main as main_mod
from diamond_agent import releases
from diamond_agent.channels import ChannelRules
from diamond_agent.core.service import DiamondAgentCore
from diamond_agent.execution.base import JobHandle
from diamond_agent.providers.base import ShellResult
from diamond_agent.schemas import EndpointHealth, ResourceEnvelope, ResourceRecord, ResourceSnapshot, TaskSpec, utcnow

CATALOG = {
    "systems": [
        {"name": "Alpha@Test", "endpoint": "ep-alpha", "container_runtime": "apptainer", "accounts": ["acct", "acct-b"],
         "partitions": [
             {"name": "gpu", "arch": "x86_64", "gpu_type": "A100", "gpus_per_node": 4, "gpu_mem_gb": 40,
              "cpus_per_node": 64, "mem_per_node_gb": 256, "nodes_total": 50, "max_walltime": "4:00:00"},
             {"name": "cpu", "arch": "x86_64", "gpu_type": None, "gpus_per_node": 0, "gpu_mem_gb": 0,
              "cpus_per_node": 128, "mem_per_node_gb": 256, "nodes_total": 100, "max_walltime": "4:00:00",
              "max_nodes_per_job": 3},
         ]},
    ],
    "policy": {"max_nodes_per_job": 8, "max_user_pending_per_system": 4, "blocked_partitions": []},
}
SYSTEM = "Alpha%40Test"
GPU = {"system": "Alpha@Test", "partition": "gpu"}
CPU = {"system": "Alpha@Test", "partition": "cpu"}


def record(partition, gpus=4, cpus=64, idle=40, total=50):
    return ResourceRecord(
        timestamp=utcnow(), endpoint="ep-alpha", system="Alpha@Test", partition=partition, account="acct",
        node_arch="x86_64", gpu_type="A100" if gpus else None, gpus_per_node=gpus, gpu_mem_gb=40 if gpus else 0,
        cpus_per_node=cpus, mem_per_node_gb=256, nodes_idle=idle, nodes_allocated=total - idle,
        gpus_free=idle * gpus if gpus else None, cpus_free=idle * cpus, max_walltime_minutes=240,
        endpoint_health=EndpointHealth.ONLINE, container_runtime="apptainer")


def envelope(**kw):
    return ResourceEnvelope.model_validate(kw)


GPU_ENV = {"gpu": {"min_count": 1, "max_count": 4}}


class CoreFixture(unittest.TestCase):
    """A core over a fake pool, served by the real FastAPI app through TestClient.
    ``self.api`` is the default channel's API prefix — what an agent would use."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.db")
        self.ready = mock.patch.object(auth, "check_headless_ready", return_value=(True, "test"))
        self.ready.start()
        self.core = DiamondAgentCore.build(CATALOG, db_path=self.db)
        self.core.collector._latest = ResourceSnapshot(records=[record("gpu"), record("cpu", gpus=0, cpus=128, idle=90, total=100)])
        self.submitted = []
        self.builds = []
        self.core.pool.submit = lambda task, placement, build=None: (self.submitted.append(placement), self.builds.append(build), JobHandle("1", placement.system, placement.partition))[2]
        self.shell_calls = []
        self.core.pool.shell = lambda system, command, timeout_s=60: (self.shell_calls.append(command), ShellResult(0, "ok\n", ""))[1]
        api.ctx.core = self.core
        self.client = TestClient(main_mod.app)
        self.channel = self.core.channels.get("default")     # every machine, machine rules; created with the core
        self.api = f"{self.channel.path}/api"

    def tearDown(self):
        self.ready.stop()
        self.core.store.close()
        self.tmp.cleanup()

    def events(self, kind, channel=None):
        return [e for e in self.core.events(limit=200, channel_id=channel) if e["kind"] == kind]

    def operator(self):
        self.assertEqual(self.client.get("/").status_code, 200)   # the browser that loads the UI is the operator

    def new_channel(self, name, rules):
        self.operator()
        r = self.client.post("/ui/channels", json={"name": name, "rules": rules})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


class PolicyTest(CoreFixture):

    # ------------------------------------------------------------ node caps

    def test_queue_cap_bounds_sizing_and_refuses_shapes_above_it(self):
        wide = self.core.submit(name="wide", command="echo", target=("Alpha@Test", "cpu"), channel=self.channel,
                                envelope=envelope(gpu={"min_count": 0, "max_count": 0}, cpu={"min_count": 4, "max_count": 1280}))
        self.assertEqual(wide["sizing"]["nodes"], 3)           # the cpu queue's own cap, not the default 8
        r = self.client.post(f"{self.api}/tasks", json={"name": "huge", "command": "echo", "target": GPU,
                                                        "envelope": {"gpu": {"min_count": 40, "max_count": 40}}})   # 10 nodes min > cap 8
        self.assertEqual(r.status_code, 409, r.text)
        failed = {v["rule"]: v["detail"] for v in r.json()["detail"]["verdicts"] if not v["passed"]}
        self.assertIn("per-job cap 8", failed["capacity"])
        self.assertEqual(len(self.core.tasks()), 1)             # a refused submission creates no task

    def test_candidates_evaluate_every_queue_and_target_is_required(self):
        rows = {c["partition"]: c for c in self.client.post(f"{self.api}/candidates", json={"envelope": GPU_ENV}).json()}
        self.assertTrue(rows["gpu"]["feasible"])
        self.assertEqual(rows["gpu"]["sizing"]["nodes"], 1)
        self.assertFalse(rows["cpu"]["feasible"])
        self.assertIn("accelerator", [v["rule"] for v in rows["cpu"]["verdicts"] if not v["passed"]])
        self.assertEqual(self.client.post(f"{self.api}/tasks", json={"name": "x", "command": "echo", "envelope": {}}).status_code, 422)

    def test_dispatch_gate_refuses_a_sizing_above_the_cap(self):
        task = self.core.manager.submit(TaskSpec(name="t", command="echo", envelope=envelope(**GPU_ENV)),
                                        ("Alpha@Test", "gpu"), self.channel)
        stale = task.evaluation.model_copy(deep=True)
        stale.sizing.nodes = 9
        self.submitted.clear()
        self.core.manager._dispatch(task, stale)
        self.assertEqual(task.status.value, "FAILED")
        self.assertTrue(self.events("POLICY_DENIED"))
        self.assertEqual(self.submitted, [])

    # ------------------------------------------------------ operator edits

    def test_machine_rules_are_read_everywhere_and_edited_only_from_the_ui(self):
        self.assertEqual(self.client.get(f"{self.api}/policy").status_code, 200)
        self.assertEqual(self.client.put(f"{self.api}/policy", json={"max_nodes_per_job": 99}).status_code, 405)
        self.assertEqual(self.client.put("/ui/policy", json={"max_nodes_per_job": 99}).status_code, 403)   # no operator cookie
        self.assertNotIn("/ui/policy", self.client.get(f"{self.channel.path}/skill/api").text)
        self.assertEqual(self.core.policy["max_nodes_per_job"], 8)

        self.operator()
        r = self.client.put("/ui/policy", json={"max_nodes_per_job": 4, "partition_limits": {"Alpha@Test:gpu": 2}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(policy.effective_node_cap(self.core.policy, "Alpha@Test", "gpu"), 2)
        self.assertEqual(policy.effective_node_cap(self.core.policy, "Alpha@Test", "cpu"), 3)   # catalog value kept
        t = self.core.submit(name="g", command="echo", target=("Alpha@Test", "gpu"), channel=self.channel,
                             envelope=envelope(gpu={"min_count": 1, "max_count": 32}))
        self.assertEqual(t["sizing"]["nodes"], 2)
        self.assertTrue(self.events("POLICY_CHANGED"))
        reloaded = DiamondAgentCore.build(CATALOG, db_path=self.db)          # overrides survive a restart
        self.assertEqual(policy.effective_node_cap(reloaded.policy, "Alpha@Test", "gpu"), 2)
        reloaded.store.close()

        self.assertEqual(self.client.put("/ui/policy", json={"partition_limits": {"Alpha@Test:gpu": None}}).status_code, 200)
        self.assertEqual(policy.effective_node_cap(self.core.policy, "Alpha@Test", "gpu"), 4)
        self.assertEqual(self.client.put("/ui/policy", json={"max_nodes_per_job": 0}).status_code, 422)
        self.assertEqual(self.client.put("/ui/policy", json={"partition_limits": {"Nope:x": 1}}).status_code, 400)

    def test_blocked_queue_is_refused_and_shown(self):
        self.operator()
        self.client.put("/ui/policy", json={"blocked_partitions": ["Alpha@Test:gpu"]})
        r = self.client.post(f"{self.api}/tasks", json={"name": "g", "command": "echo", "target": GPU, "envelope": GPU_ENV})
        self.assertEqual(r.status_code, 409)
        self.assertIn("not available", r.text)
        self.assertIn("blocked", self.core.status_text())

    def test_retarget_moves_a_task_or_leaves_it_untouched(self):
        t = self.core.submit(name="m", command="echo", target=("Alpha@Test", "gpu"), channel=self.channel, envelope=envelope(**GPU_ENV))
        bad = self.client.post(f"{self.api}/tasks/{t['task_id']}/retarget", json={"target": CPU})
        self.assertEqual(bad.status_code, 409)
        self.assertEqual(self.core.task(t["task_id"])["target_partition"], "gpu")
        self.assertEqual(self.core.task(t["task_id"])["status"], "PENDING")
        ok = self.client.post(f"{self.api}/tasks/{t['task_id']}/retarget", json={"target": GPU})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertTrue(self.events("TASK_RETARGETED"))
        self.assertEqual(self.client.post(f"{self.api}/tasks/{t['task_id']}/retarget",
                                          json={"target": {"system": "Nope", "partition": "x"}}).status_code, 409)   # not a queue of the channel

    # ------------------------------------------- inspection-only agent surfaces

    def test_exec_runs_inspection_and_refuses_job_control(self):
        for cmd in ("module avail 2>&1 | grep -i torch", "ls -la $HOME/diamond/logs | tail", "scontrol show job 1", "squeue -u $USER"):
            self.assertEqual(self.client.post(f"{self.api}/resources/{SYSTEM}/exec", json={"command": cmd}).status_code, 200, cmd)
        self.assertEqual(len(self.shell_calls), 4)
        self.assertEqual(len(self.events("SHELL_EXEC")), 4)
        for cmd in ("sbatch job.sh", "cd scripts && /usr/bin/sbatch run.sh", "srun --pty bash", "scancel 123",
                    "scontrol update jobid=1", "nohup python train.py", "ls & sleep 5"):
            self.assertEqual(self.client.post(f"{self.api}/resources/{SYSTEM}/exec", json={"command": cmd}).status_code, 403, cmd)
        self.assertEqual(len(self.shell_calls), 4)
        self.assertEqual(len(self.events("POLICY_DENIED")), 7)

    def test_put_file_stays_under_the_workdir(self):
        b64 = base64.b64encode(b"print(1)\n").decode()
        ok = self.client.post(f"{self.api}/resources/{SYSTEM}/files", json={"path": "scripts/train.py", "content_b64": b64})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json()["path"], "$HOME/diamond/scripts/train.py")
        self.assertTrue(self.events("FILE_STAGED"))
        for path in ("/etc/x", "../escape.py", "$HOME/.bashrc", "~/x", "a b", "a;b"):
            self.assertEqual(self.client.post(f"{self.api}/resources/{SYSTEM}/files", json={"path": path, "content_b64": b64}).status_code, 403, path)
        self.assertEqual(self.client.post(f"{self.api}/resources/{SYSTEM}/files", json={"path": "x", "content_b64": b64, "mode": "rwx"}).status_code, 422)


class ChannelTest(CoreFixture):

    SMALL = {"systems": {"Alpha@Test": {"account": "acct", "partitions": {"gpu": {"max_nodes_per_job": 2, "account": "acct-b"}}}}}   # queue account wins

    def test_a_channel_sees_only_its_queues_with_its_account_and_caps(self):
        small = self.new_channel("small", self.SMALL)
        sapi = f"{small['path']}/api"
        rows = self.client.post(f"{sapi}/candidates", json={"envelope": GPU_ENV}).json()
        self.assertEqual([(r["partition"], r["account"], r["max_nodes_per_job"]) for r in rows], [("gpu", "acct-b", 2)])
        status = self.client.get(f"{sapi}/status").json()
        self.assertEqual([p["partition"] for p in status["systems"][0]["partitions"]], ["gpu"])
        self.assertEqual(status["channel"]["name"], "small")
        self.assertIn("Your channel: small", self.client.get(f"{small['path']}/skill").text)

        self.assertEqual(self.client.post(f"{sapi}/tasks", json={"name": "c", "command": "echo", "target": CPU,
                                                                 "envelope": {"gpu": {"min_count": 0, "max_count": 0}}}).status_code, 409)
        t = self.client.post(f"{sapi}/tasks", json={"name": "g", "command": "echo", "target": GPU,
                                                    "envelope": {"gpu": {"min_count": 1, "max_count": 32}}}).json()
        self.assertEqual(t["sizing"]["nodes"], 2)                          # channel cap 2 inside machine cap 8
        self.assertEqual(self.submitted[-1].account, "acct-b")             # billed to the channel's account
        self.assertEqual(self.client.get(f"{sapi}/tasks/{t['task_id']}").status_code, 200)
        self.assertEqual(self.client.get(f"{self.api}/tasks/{t['task_id']}").status_code, 404)   # invisible to another channel
        self.assertEqual(self.client.get(f"/api/tasks/{t['task_id']}").status_code, 200)         # the operator sees all
        self.assertEqual([x["task_id"] for x in self.client.get(f"/api/tasks?channel_id={small['id']}").json()], [t["task_id"]])
        self.assertTrue(self.events("TASK_SUBMITTED", channel=small["id"]))
        self.assertFalse(self.events("TASK_SUBMITTED", channel=self.channel.id))

    def test_channel_rules_stay_inside_the_machine_rules(self):
        self.operator()
        for rules, why in (({"systems": {"Alpha@Test": {"partitions": {"gpu": {"max_nodes_per_job": 99}}}}}, "exceeds the machine cap"),
                           ({"systems": {"Nope@X": {}}}, "unknown system"),
                           ({"systems": {"Alpha@Test": {"partitions": {"xx": {}}}}}, "unknown queue"),
                           ({"systems": {}, "max_user_pending_per_system": 99}, "exceeds the machine cap")):
            r = self.client.post("/ui/channels", json={"name": "bad", "rules": rules})
            self.assertEqual(r.status_code, 400, rules)
            self.assertIn(why, r.text)
        small = self.new_channel("small", self.SMALL)
        self.client.put("/ui/policy", json={"partition_limits": {"Alpha@Test:gpu": 1}})     # operator tightens the machine
        self.assertEqual(self.client.get(f"{small['path']}/api/policy").json()["partition_limits"], {"Alpha@Test:gpu": 1})
        self.client.put("/ui/policy", json={"blocked_partitions": ["Alpha@Test:gpu"]})      # and blocks the queue
        self.assertEqual(self.client.post(f"{small['path']}/api/candidates", json={"envelope": GPU_ENV}).json(), [])
        self.assertEqual(self.client.get(f"{small['path']}/api/status").json()["systems"], [])          # the machine is gone for the channel
        self.assertEqual(self.client.post(f"{small['path']}/api/resources/{SYSTEM}/exec", json={"command": "ls"}).status_code, 403)

    def test_agent_actions_need_a_channel_url(self):
        for call in (lambda: self.client.post("/api/tasks", json={"name": "x", "command": "echo", "target": GPU, "envelope": GPU_ENV}),
                     lambda: self.client.post("/api/candidates", json={"envelope": GPU_ENV}),
                     lambda: self.client.post(f"/api/resources/{SYSTEM}/exec", json={"command": "ls"})):
            self.assertEqual(call().status_code, 400)
        self.assertEqual(self.client.get("/api/tasks").status_code, 200)             # the operator reads everything
        self.assertEqual(self.client.get("/c/not-a-token/api/status").status_code, 404)

    def test_node_hour_budget_shrinks_then_refuses(self):
        ch = self.new_channel("budget", {"systems": {"Alpha@Test": {"partitions": {"gpu": {"node_hours": 1.0}}}}})
        env = {"gpu": {"min_count": 1, "max_count": 8}, "walltime": {"min": "30m", "max": "1h"}}
        row = self.client.post(f"{ch['path']}/api/candidates", json={"envelope": env}).json()[0]
        self.assertEqual((row["sizing"]["nodes"], row["sizing"]["walltime_minutes"], row["node_hours_left"]), (1, 60, 1.0))
        self.assertIn("shrunk", next(v["detail"] for v in row["verdicts"] if v["rule"] == "budget"))
        t = self.client.post(f"{ch['path']}/api/tasks", json={"name": "a", "command": "echo", "target": GPU, "envelope": env}).json()
        self.assertEqual(t["sizing"]["nodes"], 1)                                   # 1 node × 1 h = the whole budget
        r = self.client.post(f"{ch['path']}/api/tasks", json={"name": "b", "command": "echo", "target": GPU, "envelope": env})
        self.assertEqual(r.status_code, 409)
        self.assertIn("budget", r.text)
        self.client.post(f"{ch['path']}/api/tasks/{t['task_id']}/cancel")             # never ran: nothing billed
        self.assertEqual(self.client.post(f"{ch['path']}/api/tasks", json={"name": "c", "command": "echo", "target": GPU, "envelope": env}).status_code, 200)

    def test_machine_budget_is_shared_by_channels_first_come_first_served(self):
        self.operator()
        self.assertEqual(self.client.put("/ui/policy", json={"node_hours": {"Alpha@Test": 10}}).status_code, 200)
        A = self.new_channel("A", {"systems": {"Alpha@Test": {}}})
        B = self.new_channel("B", {"systems": {"Alpha@Test": {}}})
        six = {"gpu": {"min_count": 8, "max_count": 8}, "walltime": {"min": "3h", "max": "3h"}}    # 2 nodes × 3 h, rigid
        self.assertEqual(self.client.post(f"{A['path']}/api/tasks", json={"name": "a", "command": "echo", "target": GPU, "envelope": six}).status_code, 200)
        self.assertEqual(self.client.post(f"{B['path']}/api/tasks", json={"name": "b", "command": "echo", "target": GPU, "envelope": six}).status_code, 409)   # 4 left
        one = {"gpu": {"min_count": 1, "max_count": 1}, "walltime": {"min": "1h", "max": "1h"}}
        self.assertEqual(self.client.post(f"{B['path']}/api/tasks", json={"name": "b1", "command": "echo", "target": GPU, "envelope": one}).status_code, 200)
        view = self.client.get(f"/ui/channels/{B['id']}").json()["budget"]["machines"]["Alpha@Test"]
        self.assertEqual((view["machine_budget"], view["left"]), (10.0, 3.0))

    def test_elapsed_time_is_what_counts_once_a_job_ends(self):
        ch = self.new_channel("e", {"systems": {"Alpha@Test": {"partitions": {"gpu": {"node_hours": 1.0}}}}})
        hour = {"gpu": {"min_count": 1, "max_count": 1}, "walltime": {"min": "1h", "max": "1h"}}
        t = self.client.post(f"{ch['path']}/api/tasks", json={"name": "a", "command": "echo", "target": GPU, "envelope": hour}).json()
        self.core.pool.status = lambda job_id: ("COMPLETED", "sacct: COMPLETED", 30.0)      # ran for half an hour
        self.core.manager.tick()
        self.assertEqual(self.core.task(t["task_id"])["status"], "COMPLETED")
        half = {"gpu": {"min_count": 1, "max_count": 1}, "walltime": {"min": "30m", "max": "30m"}}
        self.assertEqual(self.client.post(f"{ch['path']}/api/tasks", json={"name": "b", "command": "echo", "target": GPU, "envelope": half}).status_code, 200)

    def test_tasks_from_before_channels_are_adopted_by_the_default_channel(self):
        t = self.core.submit(name="old", command="echo", target=("Alpha@Test", "gpu"), channel=self.channel, envelope=envelope(**GPU_ENV))
        legacy = self.core.task(t["task_id"]); del legacy["channel"]                 # a row written before channels existed
        self.core.store.save_task(t["task_id"], legacy)
        self.core.store._conn.execute("UPDATE events SET channel = NULL"); self.core.store._conn.commit()
        reloaded = DiamondAgentCore.build(CATALOG, db_path=self.db)
        self.assertEqual(reloaded.task(t["task_id"])["channel"], self.channel.id)
        self.assertTrue(reloaded.events(channel_id=self.channel.id))
        reloaded.store.close()

    def test_channels_are_managed_only_by_the_operator(self):
        self.assertEqual(self.client.get("/ui/channels").status_code, 403)
        small = self.new_channel("small", self.SMALL)
        self.assertEqual([c["name"] for c in self.client.get("/ui/channels").json()], ["default", "small"])
        self.assertEqual(self.client.put(f"/ui/channels/{small['id']}", json={"name": "tiny"}).json()["name"], "tiny")
        self.assertTrue(self.events("CHANNEL_CREATED", channel=small["id"]))
        self.assertEqual(self.client.delete(f"/ui/channels/{small['id']}").status_code, 200)
        self.assertEqual(self.client.get(f"{small['path']}/api/status").status_code, 404)   # the URL dies with the channel
        self.assertEqual(self.client.delete("/ui/channels/default").status_code, 400)       # the default channel stays
        self.assertEqual(self.client.get(f"{self.api}/status").status_code, 200)

    def test_the_terminal_manages_channels_without_the_ui(self):
        import yaml
        from diamond_agent.channels import main as channels_main
        sites = os.path.join(self.tmp.name, "sites.yaml")
        with open(sites, "w") as f:
            yaml.safe_dump(CATALOG, f)
        channels_main(["--db", self.db, "--sites", sites, "create", "from-terminal"])
        made = next(c for c in self.core.channels.list() if c.name == "from-terminal")   # live at once in the running core
        self.assertEqual(self.client.get(f"{made.path}/api/status").status_code, 200)
        self.assertTrue(self.events("CHANNEL_CREATED", channel=made.id))
        with self.assertRaises(SystemExit):
            channels_main(["--db", self.db, "--sites", sites, "delete", "nope"])
        with self.assertRaises(ValueError):
            channels_main(["--db", self.db, "--sites", sites, "delete", "default"])
        channels_main(["--db", self.db, "--sites", sites, "delete", made.id])
        self.assertEqual(self.client.get(f"{made.path}/api/status").status_code, 404)


class ReleaseTest(CoreFixture):
    """Releases materialise through the pool's shell; here the shell is a local
    bash with a temp directory standing in for the machine's workdir, so the
    real staging scripts run end to end."""

    def setUp(self):
        super().setUp()
        self.sc = os.path.join(self.tmp.name, "sc")
        os.makedirs(self.sc)
        self.core.pool.workdir = lambda system: self.sc
        self.core.pool.shell = lambda system, command, timeout_s=60: (lambda r: ShellResult(r.returncode, r.stdout, r.stderr))(
            subprocess.run(["bash", "-c", command], capture_output=True, text=True))
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(os.path.join(self.repo, "cfg"))
        with open(os.path.join(self.repo, "train.py"), "w") as f:
            f.write("print('v1')\n")
        with open(os.path.join(self.repo, "cfg", "model.yaml"), "w") as f:
            f.write("layers: 12\n")
        for cmd in (["git", "init", "-q"], ["git", "add", "."],
                    ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "v1"]):
            subprocess.run(cmd, cwd=self.repo, check=True, capture_output=True)

    def release_body(self, base, **extra):
        archive, source = releases.pack_git(self.repo, "HEAD")
        return {"archive_b64": base64.b64encode(archive).decode(), "source": source.model_dump(), "base_version": base, **extra}

    def build(self, version, system="Alpha@Test"):
        for _ in range(200):
            b = self.client.get(f"{self.api}/releases/{version}").json()["builds"].get(system)
            if b and b["status"] != "building":
                return b
            time.sleep(0.05)
        self.fail("build did not finish")

    def test_release_is_one_version_per_channel_aligned_per_machine_and_gates_dispatch(self):
        r1 = self.client.post(f"{self.api}/releases", json=self.release_body(0, env="module load pytorch"))
        self.assertEqual(r1.status_code, 200, r1.text)
        rel = r1.json()
        self.assertEqual((rel["version"], rel["files"], rel["systems"]), (1, 2, ["Alpha@Test"]))
        self.assertEqual(len(rel["source"]["commit"]), 40)
        self.assertEqual(self.client.post(f"{self.api}/releases", json=self.release_body(0)).status_code, 409)   # CAS: base must be the latest
        self.assertTrue(self.events("RELEASE_CREATED", channel=self.channel.id))
        task = {"name": "t", "command": "python train.py", "target": GPU, "envelope": GPU_ENV, "release": 1}
        r = self.client.post(f"{self.api}/tasks", json=task)
        self.assertEqual(r.status_code, 409)
        self.assertIn("not aligned", r.json()["detail"]["message"])
        self.assertEqual(self.client.post(f"{self.api}/releases/1/align", json={"system": "Alpha@Test"}).json()["status"], "building")
        b = self.build(1)
        self.assertEqual((b["status"], b["number"]), ("ready", 1), b)
        snap = f"{self.sc}/releases/{self.channel.id}/{rel['snapshot']}"
        self.assertEqual(b["dir"], snap)
        with open(os.path.join(snap, "cfg", "model.yaml")) as f:
            self.assertEqual(f.read(), "layers: 12\n")
        self.assertEqual(os.readlink(f"{self.sc}/releases/{self.channel.id}/current"), rel["snapshot"])
        self.assertTrue(os.path.exists(os.path.join(snap, ".da-release")))
        r = self.client.post(f"{self.api}/tasks", json=task)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((self.builds[-1].dir, self.builds[-1].version, self.builds[-1].env), (snap, 1, "module load pytorch"))
        self.assertEqual(self.core.task(r.json()["task_id"])["release_build"]["number"], 1)
        with open(os.path.join(snap, "train.py"), "w") as f:                       # drift on the machine
            f.write("tampered\n")
        v = self.client.post(f"{self.api}/releases/1/verify", json={"system": "Alpha@Test"}).json()
        self.assertEqual(v["status"], "stale")
        self.assertIn("train.py", v["error"])
        self.assertEqual(self.client.post(f"{self.api}/tasks", json=task).status_code, 409)   # a stale machine is not aligned
        self.client.post(f"{self.api}/releases/1/align", json={"system": "Alpha@Test"})
        b = self.build(1)
        self.assertEqual((b["status"], b["number"]), ("ready", 2), b)                   # repaired: a new build number of the same release
        with open(os.path.join(snap, "train.py")) as f:
            self.assertEqual(f.read(), "print('v1')\n")
        self.assertEqual(self.client.post(f"{self.api}/tasks", json=task).status_code, 200)
        self.assertEqual(self.client.post(f"{self.api}/releases/1/align", json={"system": "Nope"}).status_code, 403)   # not a machine of the channel


if __name__ == "__main__":
    unittest.main()
