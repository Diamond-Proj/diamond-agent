"""The catalog and what is read live: init-sites is a merge (a rerun never costs an
account, a switch or a hand edit), and accounts come from Slurm unless pinned."""

import json
import types
import unittest

import yaml

from diamond_agent.auth import _bundled_shapes, render_catalog
from diamond_agent.federation.globus_probe import GlobusFederationSource
from diamond_agent.schemas import utcnow

EXISTING = """# an older header
systems:
  - name: Vista@TACC
    endpoint: aaa1acb6-6fab-45fe-a464-80210b4ca659   # diamond-agent (login1)
    container_runtime: apptainer
    accounts: [CCR24047]
    gpu_directive: false
    mem_directive: false
    partitions:
      - name: gh
        arch: aarch64
        gpu_type: GH200
        gpus_per_node: 1
        gpu_mem_gb: 96
        cpus_per_node: 72
        mem_per_node_gb: 120
        nodes_total: 576
        max_walltime: "4:00:00"

  - name: Horizon@TACC
    endpoint: 666f2c48-3787-40ce-90e7-623d55bdd98f   # diamond-agent (login1)
    container_runtime: apptainer
    accounts: [CCR26006]       # sacctmgr: ccr26006
    gpu_directive: false
    mem_directive: false
    partitions:
      - name: debug
        arch: aarch64
        gpu_type: GB200
        gpus_per_node: 4
        gpu_mem_gb: 180
        cpus_per_node: 144
        mem_per_node_gb: 956
        nodes_total: 1000
        max_walltime: null

# Scheduling limits (hand-tuned).
policy:
  max_user_pending_per_system: 4
  max_nodes_per_job: 5
  blocked_partitions: [Vista@TACC:gg]
"""

ENDPOINTS = [
    {"uuid": "aaa1acb6-6fab-45fe-a464-80210b4ca659", "name": "diamond-agent (login1)", "status": "offline",
     "hostname": "login2.vista.tacc.utexas.edu", "machine": "Vista@TACC", "version": "4.15.0"},
    {"uuid": "29d93d90-bbbe-4b85-a966-14171200594b", "name": "diamond-agent", "status": "online",
     "hostname": "login1.horizon.tacc.utexas.edu", "machine": "Horizon@TACC", "version": "4.15.0"},
    {"uuid": "a73ed726-929e-4173-ad00-c9ee4a6e33fe", "name": "diamond-agent", "status": "online",
     "hostname": "login03.anvil.rcac.purdue.edu", "machine": "Anvil@RCAC", "version": "4.15.0"},
    {"uuid": "578cd700-6d9c-4e57-b46e-703461fbfa21", "name": "nersc-perlmutter", "status": "offline",
     "hostname": "login17.chn", "machine": "Perlmutter@NERSC", "version": "4.15.0"},
    {"uuid": "f9737942-884a-42bb-b2df-fcbc671aa39a", "name": "test", "status": "multi-user (unsupported)",
     "hostname": "docker-desktop", "machine": "unknown", "version": ""},
]


class InitSitesMergeTest(unittest.TestCase):

    def test_rerun_keeps_accounts_and_hand_edits_and_refreshes_endpoint_ids(self):
        text, stats = render_catalog(EXISTING, ENDPOINTS, _bundled_shapes())
        catalog = yaml.safe_load(text)
        by_name = {s["name"]: s for s in catalog["systems"]}
        self.assertEqual(stats, {"kept": ["Vista@TACC"], "refreshed": ["Horizon@TACC"], "added": ["Anvil@RCAC"],
                                 "absent": ["Perlmutter@NERSC"]})
        self.assertEqual(by_name["Vista@TACC"]["accounts"], ["CCR24047"])                  # offline now, still there
        self.assertEqual(by_name["Vista@TACC"]["endpoint"], "aaa1acb6-6fab-45fe-a464-80210b4ca659")
        self.assertEqual(by_name["Horizon@TACC"]["endpoint"], "29d93d90-bbbe-4b85-a966-14171200594b")   # re-created
        self.assertEqual(by_name["Horizon@TACC"]["accounts"], ["CCR26006"])
        self.assertFalse(by_name["Horizon@TACC"]["gpu_directive"])
        self.assertIsNone(by_name["Horizon@TACC"]["partitions"][0]["max_walltime"])
        self.assertIn("# sacctmgr: ccr26006", text)                                        # comments survive
        self.assertNotIn("accounts", by_name["Anvil@RCAC"])                                # read live from Slurm
        self.assertTrue(by_name["Anvil@RCAC"]["partitions"])
        self.assertNotIn("TODO", text)                                                    # nothing left to fill
        self.assertEqual(catalog["policy"], {"max_user_pending_per_system": 4, "max_nodes_per_job": 5,
                                             "blocked_partitions": ["Vista@TACC:gg"]})
        self.assertIn("# Perlmutter@NERSC: endpoint 578cd700", text)
        again, stats2 = render_catalog(text, ENDPOINTS, _bundled_shapes())
        self.assertEqual(again, text)                                                     # idempotent
        self.assertEqual(stats2["refreshed"], [])

    def test_first_run_has_no_existing_file(self):
        text, stats = render_catalog(None, ENDPOINTS, _bundled_shapes())
        catalog = yaml.safe_load(text)
        self.assertEqual(sorted(s["name"] for s in catalog["systems"]), ["Anvil@RCAC", "Horizon@TACC"])
        self.assertEqual(stats["kept"], [])
        self.assertEqual(catalog["policy"]["max_nodes_per_job"], 8)
        self.assertEqual(next(s for s in catalog["systems"] if s["name"] == "Horizon@TACC")["account_case"], "upper")
        self.assertNotIn("TODO", text)


class LiveAccountsTest(unittest.TestCase):
    """The probe reports the user's associations and Slurm's DefaultAccount;
    the catalog's ``accounts`` only pins one."""

    def probe(self, system, accounts, default):
        out = json.dumps({"partitions": {"debug": {"idle": 3}}, "accounts": accounts, "default_account": default})
        client = types.SimpleNamespace(
            get_endpoint_status=lambda endpoint_uuid: {"status": "online"},
            create_batch=lambda: types.SimpleNamespace(add=lambda *a, **k: None),
            batch_run=lambda endpoint, batch: {"tasks": {"fn": ["t1"]}},
            get_result=lambda task_id: types.SimpleNamespace(stdout=out, stderr=""))
        source = GlobusFederationSource({"systems": [system]})
        source._probe_fn_id = "fn"
        return source._probe_system(client, system, utcnow())[0]

    def system(self, **extra):
        return {"name": "Horizon@TACC", "endpoint": "ep", **extra,
                "partitions": [{"name": "debug", "arch": "aarch64", "gpu_type": "GB200", "gpus_per_node": 4}]}

    def test_slurm_default_account_in_the_site_spelling_unless_pinned(self):
        r = self.probe(self.system(account_case="upper"), ["ccr26006", "default"], "ccr26006")
        self.assertEqual((r.account, r.accounts), ("CCR26006", ["CCR26006", "DEFAULT"]))
        r = self.probe(self.system(), ["m4431", "m4431_g"], "m4431")                       # no spelling rule
        self.assertEqual(r.account, "m4431")
        r = self.probe(self.system(account_case="upper", accounts=["CCR99999"]), ["ccr26006"], "ccr26006")
        self.assertEqual((r.account, r.accounts), ("CCR99999", ["CCR99999"]))           # the catalog pins


if __name__ == "__main__":
    unittest.main()
