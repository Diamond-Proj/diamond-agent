"""The MCP surface, driven through a real MCP session over a real HTTP round trip
to the in-process core (an ephemeral uvicorn listener on 127.0.0.1)."""

from __future__ import annotations

import asyncio
import json
import threading
import time

import anyio
import uvicorn
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from diamond_agent import main as main_mod
from diamond_agent.core import mcp_server
from test_policy import CoreFixture


class McpTest(CoreFixture):
    def setUp(self):
        super().setUp()
        self.server = uvicorn.Server(uvicorn.Config(main_mod.app, host="127.0.0.1", port=0, log_level="warning", lifespan="off"))
        threading.Thread(target=self.server.run, daemon=True).start()
        while not self.server.started:
            time.sleep(0.02)
        url = "http://127.0.0.1:%d%s" % (self.server.servers[0].sockets[0].getsockname()[1], self.channel.path)
        mcp_server._tools.base = mcp_server._docs.base = url          # agents talk to their channel URL

    def tearDown(self):
        self.server.should_exit = True
        super().tearDown()

    def session(self, fn):
        async def run():
            srv = mcp_server.mcp._lowlevel_server
            async with create_client_server_memory_streams() as ((cr, cw), (sr, sw)):
                async with anyio.create_task_group() as tg:
                    tg.start_soon(srv.run, sr, sw, srv.create_initialization_options(), True)
                    async with ClientSession(cr, cw) as s:
                        await s.initialize()
                        out = await fn(s)
                    tg.cancel_scope.cancel()
            return out
        return asyncio.run(run())

    def test_every_tool_is_annotated_and_none_can_change_limits(self):
        tools = {t.name: t for t in self.session(lambda s: s.list_tools()).tools}
        self.assertTrue(all(t.annotations is not None and t.title for t in tools.values()))
        self.assertTrue(tools["scheduling_policy"].annotations.read_only_hint)
        self.assertTrue(tools["find_candidates"].annotations.read_only_hint)
        self.assertFalse(tools["submit_task"].annotations.destructive_hint)
        self.assertTrue(tools["cancel_task"].annotations.destructive_hint)
        self.assertTrue(tools["retarget_task"].annotations.destructive_hint)
        self.assertEqual(set(tools["submit_task"].input_schema["required"]), {"name", "command", "envelope", "target_system", "target_partition"})
        self.assertFalse(any(name for name in tools if "policy" in name and not tools[name].annotations.read_only_hint))

    def test_refusals_reach_the_model_with_their_reason(self):
        async def go(s):
            return (await s.call_tool("run_on_login_node", {"system": "Alpha@Test", "command": "sbatch x.sh"}),
                    await s.call_tool("get_task", {"task_id": "nope"}),
                    await s.call_tool("run_on_login_node", {"system": "Alpha@Test", "command": "ls"}))
        refused, missing, ok = self.session(go)
        self.assertTrue(refused.is_error)
        self.assertIn("submits or controls jobs", refused.content[0].text)
        self.assertTrue(missing.is_error)
        self.assertIn("task not found", missing.content[0].text)
        self.assertFalse(ok.is_error)
        self.assertEqual(ok.structured_content["returncode"], 0)

    def test_reference_documents_are_resources(self):
        async def go(s):
            uris = sorted(str(r.uri) for r in (await s.list_resources()).resources)
            status = (await s.read_resource("diamond://status")).contents[0].text
            policy = json.loads((await s.read_resource("diamond://policy")).contents[0].text)
            return uris, status, policy
        uris, status, policy = self.session(go)
        self.assertEqual(uris, ["diamond://policy", "diamond://skill", "diamond://skill/systems", "diamond://status"])
        self.assertIn("# Federation status", status)
        self.assertEqual(policy["max_nodes_per_job"], 8)
