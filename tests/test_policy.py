"""The policy boundary, offline (no Globus, no network):

    uv run --no-sync python -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DIAMOND_AGENT_UI", "1")

from fastapi.testclient import TestClient

from diamond_agent import api, auth, policy
from diamond_agent import main as main_mod
from diamond_agent.core.service import DiamondAgentCore
from diamond_agent.execution.base import JobHandle
from diamond_agent.providers.base import ShellResult
from diamond_agent.schemas import (
    Candidate, EndpointHealth, ResourceEnvelope, ResourceRecord, ResourceSnapshot, Sizing, TaskSpec, utcnow,
)

CATALOG = {
    "systems": [
        {"name": "Alpha@Test", "endpoint": "ep-alpha", "container_runtime": "apptainer", "accounts": ["acct"],
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


def record(partition, gpus=4, cpus=64, idle=40, total=50):
    return ResourceRecord(
        timestamp=utcnow(), endpoint="ep-alpha", system="Alpha@Test", partition=partition, account="acct",
        node_arch="x86_64", gpu_type="A100" if gpus else None, gpus_per_node=gpus, gpu_mem_gb=40 if gpus else 0,
        cpus_per_node=cpus, mem_per_node_gb=256, nodes_idle=idle, nodes_allocated=total - idle,
        gpus_free=idle * gpus if gpus else None, cpus_free=idle * cpus, max_walltime_minutes=240,
        endpoint_health=EndpointHealth.ONLINE, container_runtime="apptainer")


def envelope(**kw):
    return ResourceEnvelope.model_validate(kw)


class CoreFixture(unittest.TestCase):
    """A core over a fake pool, served by the real FastAPI app through TestClient."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.db")
        self.ready = mock.patch.object(auth, "check_headless_ready", return_value=(True, "test"))
        self.ready.start()
        self.core = DiamondAgentCore.build(CATALOG, db_path=self.db)
        self.core.collector._latest = ResourceSnapshot(records=[record("gpu"), record("cpu", gpus=0, cpus=128, idle=90, total=100)])
        self.submitted = []
        self.core.pool.submit = lambda task, placement: (self.submitted.append(placement), JobHandle("1", placement.system, placement.partition))[1]
        self.shell_calls = []
        self.core.pool.shell = lambda system, command, timeout_s=60: (self.shell_calls.append(command), ShellResult(0, "ok\n", ""))[1]
        api.ctx.core = self.core
        self.client = TestClient(main_mod.app)

    def tearDown(self):
        self.ready.stop()
        self.core.store.close()
        self.tmp.cleanup()

    def events(self, kind):
        return [e for e in self.core.events(limit=200) if e["kind"] == kind]


class PolicyTest(CoreFixture):

    # ------------------------------------------------------------ node caps

    def test_queue_cap_bounds_sizing_and_rejects_shapes_above_it(self):
        wide = self.core.submit(name="wide", command="echo", target=("Alpha@Test", "cpu"),
                                envelope=envelope(gpu={"min_count": 0, "max_count": 0}, cpu={"min_count": 4, "max_count": 1280}))
        self.assertEqual(wide["task"]["sizing"]["nodes"], 3)           # the cpu queue's own cap, not the default 8
        huge = self.core.submit(name="huge", command="echo", envelope=envelope(gpu={"min_count": 40, "max_count": 40}))
        self.assertEqual(huge["task"]["status"], "UNSCHEDULABLE")      # 10 nodes minimum > cap 8
        verdicts = {v["rule"]: v for c in self.core.task(huge["task"]["task_id"])["decision"]["candidates"]
                    if c["partition"] == "gpu" for v in c["verdicts"]}
        self.assertIn("per-job cap 8", verdicts["capacity"]["detail"])

    def test_dispatch_gate_refuses_a_sizing_above_the_cap(self):
        rec = record("gpu")
        stale = Candidate(endpoint=rec.endpoint, system=rec.system, partition=rec.partition, account="acct",
                          record=rec, feasible=True, sizing=Sizing(nodes=9, gpus=36, cpus=64, walltime_minutes=60))
        task = self.core.manager.submit(TaskSpec(name="t", command="echo", envelope=envelope(gpu={"min_count": 1, "max_count": 4})))
        self.submitted.clear()
        self.core.manager._dispatch(task, stale)
        self.assertTrue(self.events("POLICY_DENIED"))
        self.assertTrue(all(p.sizing.nodes <= 8 for p in self.submitted))

    # ------------------------------------------------------ operator edits

    def test_limits_are_read_everywhere_and_edited_only_from_the_ui(self):
        self.assertEqual(self.client.get("/api/policy").status_code, 200)
        self.assertEqual(self.client.put("/api/policy", json={"max_nodes_per_job": 99}).status_code, 405)
        self.assertEqual(self.client.put("/ui/policy", json={"max_nodes_per_job": 99}).status_code, 403)   # no operator cookie
        self.assertNotIn("/ui/policy", self.client.get("/skill/api").text)
        self.assertEqual(self.core.policy["max_nodes_per_job"], 8)

        self.assertEqual(self.client.get("/").status_code, 200)        # the browser that loads the UI is the operator
        r = self.client.put("/ui/policy", json={"max_nodes_per_job": 4, "partition_limits": {"Alpha@Test:gpu": 2}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(policy.effective_node_cap(self.core.policy, "Alpha@Test", "gpu"), 2)
        self.assertEqual(policy.effective_node_cap(self.core.policy, "Alpha@Test", "cpu"), 3)   # catalog value kept
        t = self.core.submit(name="g", command="echo", target=("Alpha@Test", "gpu"),
                             envelope=envelope(gpu={"min_count": 1, "max_count": 32}))
        self.assertEqual(t["task"]["sizing"]["nodes"], 2)
        self.assertTrue(self.events("POLICY_CHANGED"))
        self.assertEqual(policy.effective_node_cap(DiamondAgentCore.build(CATALOG, db_path=self.db).policy, "Alpha@Test", "gpu"), 2)

        self.assertEqual(self.client.put("/ui/policy", json={"partition_limits": {"Alpha@Test:gpu": None}}).status_code, 200)
        self.assertEqual(policy.effective_node_cap(self.core.policy, "Alpha@Test", "gpu"), 4)
        self.assertEqual(self.client.put("/ui/policy", json={"max_nodes_per_job": 0}).status_code, 422)
        self.assertEqual(self.client.put("/ui/policy", json={"partition_limits": {"Nope:x": 1}}).status_code, 400)

    def test_blocked_queue_is_rejected_and_shown(self):
        self.client.get("/")
        self.client.put("/ui/policy", json={"blocked_partitions": ["Alpha@Test:gpu"]})
        t = self.core.submit(name="g", command="echo", envelope=envelope(gpu={"min_count": 1, "max_count": 4}))
        self.assertEqual(t["task"]["status"], "UNSCHEDULABLE")
        self.assertIn("blocked", self.core.status_text())

    # ------------------------------------------- inspection-only agent surfaces

    def test_exec_runs_inspection_and_refuses_job_control(self):
        for cmd in ("module avail 2>&1 | grep -i torch", "ls -la $HOME/diamond/logs | tail", "scontrol show job 1", "squeue -u $USER"):
            self.assertEqual(self.client.post(f"/api/resources/{SYSTEM}/exec", json={"command": cmd}).status_code, 200, cmd)
        self.assertEqual(len(self.shell_calls), 4)
        self.assertEqual(len(self.events("SHELL_EXEC")), 4)
        for cmd in ("sbatch job.sh", "cd scripts && /usr/bin/sbatch run.sh", "srun --pty bash", "scancel 123",
                    "scontrol update jobid=1", "nohup python train.py", "ls & sleep 5"):
            self.assertEqual(self.client.post(f"/api/resources/{SYSTEM}/exec", json={"command": cmd}).status_code, 403, cmd)
        self.assertEqual(len(self.shell_calls), 4)
        self.assertEqual(len(self.events("POLICY_DENIED")), 7)

    def test_put_file_stays_under_the_workdir(self):
        b64 = base64.b64encode(b"print(1)\n").decode()
        ok = self.client.post(f"/api/resources/{SYSTEM}/files", json={"path": "scripts/train.py", "content_b64": b64})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json()["path"], "$HOME/diamond/scripts/train.py")
        self.assertTrue(self.events("FILE_STAGED"))
        for path in ("/etc/x", "../escape.py", "$HOME/.bashrc", "~/x", "a b", "a;b"):
            self.assertEqual(self.client.post(f"/api/resources/{SYSTEM}/files", json={"path": path, "content_b64": b64}).status_code, 403, path)
        self.assertEqual(self.client.post(f"/api/resources/{SYSTEM}/files", json={"path": "x", "content_b64": b64, "mode": "rwx"}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
