"""Channels: one URL per conversation (or group of agents), one rule set per URL.

A channel is the operator's unit of delegation. It names the machines and
queues its agents may use, the account each machine bills, and per-queue
node caps — always inside the machine rules (the operator's policy): the
effective cap on a queue is min(channel, machine), a queue blocked on the
machine exists in no channel. The account a queue bills is typed by the
operator (the catalog's accounts are suggestions; the catalog's per-queue
account is the default). Agents are handed the channel URL (``<core>/c/<token>``) and
see nothing outside it: status, candidates, submissions, tasks and events
are scoped to the channel. Channels live in the store and are read on each
use, so the UI, the running core and the operator's terminal
(``python -m diamond_agent.channels``) always agree.

Every core has the ``default`` channel (every machine, machine rules): created
with the core, printed at start as the URL to hand agents, never deletable —
a deployment without the web UI is usable from the first start.
"""

from __future__ import annotations

import os
import secrets
import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from .policy import effective_node_cap, is_blocked, partition_key
from .schemas import ResourceRecord, utcnow

DEFAULT_ID = "default"


def origin() -> str:
    """The core's base URL as printed for humans: DIAMOND_AGENT_PUBLIC_URL, else
    loopback on DIAMOND_AGENT_PORT (what ``make core`` / ``make live`` set)."""
    return (os.environ.get("DIAMOND_AGENT_PUBLIC_URL")
            or f"http://127.0.0.1:{os.environ.get('DIAMOND_AGENT_PORT', '8642')}").rstrip("/")


class PartitionRule(BaseModel):
    account: Optional[str] = None                                    # None => the channel's machine-level account, else the catalog's
    max_nodes_per_job: Optional[int] = Field(default=None, ge=1)     # None => the machine's cap
    node_hours: Optional[float] = Field(default=None, gt=0)          # the channel's budget on this queue; None => unlimited


class SystemRule(BaseModel):
    account: Optional[str] = None                                    # typed by the operator; None => the catalog's account per queue
    partitions: Optional[dict[str, PartitionRule]] = None            # None => every queue the machine allows
    node_hours: Optional[float] = Field(default=None, gt=0)          # the channel's budget on this machine; None => unlimited


class ChannelRules(BaseModel):
    systems: dict[str, SystemRule] = Field(default_factory=dict)
    max_user_pending_per_system: Optional[int] = Field(default=None, ge=1)   # None => the machine's cap


class Channel(BaseModel):
    id: str
    name: str
    token: str
    description: str = ""
    rules: ChannelRules = Field(default_factory=ChannelRules)
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def path(self) -> str:
        return f"/c/{self.token}"


class ChannelRegistry:
    """Channels in the store, bounded by the catalog and the machine policy
    (the shared dict the operator edits; read live, never copied)."""

    def __init__(self, store, catalog: dict, machine_policy: dict):
        self.store = store
        self.machine_policy = machine_policy
        self._partitions = {s["name"]: [p["name"] for p in s["partitions"]] for s in catalog["systems"]}

    # ------------------------------------------------------------ storage

    def list(self) -> list[Channel]:
        return sorted((Channel.model_validate(d) for d in self.store.load_channels().values()),
                      key=lambda c: c.created_at)

    def get(self, channel_id: str) -> Optional[Channel]:
        data = self.store.load_channels().get(channel_id)
        return Channel.model_validate(data) if data else None

    def by_token(self, token: str) -> Optional[Channel]:
        return next((c for c in self.list() if secrets.compare_digest(c.token, token)), None)

    def create(self, name: str, description: str = "", rules: Optional[ChannelRules] = None,
               channel_id: Optional[str] = None) -> Channel:
        rules = rules or ChannelRules(systems={s: SystemRule() for s in self._partitions})
        self.validate(rules)
        channel = Channel(id=channel_id or uuid.uuid4().hex[:8], name=name, description=description,
                          token=secrets.token_urlsafe(24), rules=rules)
        self.store.save_channel(channel.id, channel.model_dump(mode="json"))
        return channel

    def update(self, channel_id: str, name: Optional[str] = None, description: Optional[str] = None,
               rules: Optional[ChannelRules] = None) -> Optional[Channel]:
        channel = self.get(channel_id)
        if channel is None:
            return None
        if rules is not None:
            self.validate(rules)
            channel.rules = rules
        if name is not None:
            channel.name = name
        if description is not None:
            channel.description = description
        self.store.save_channel(channel.id, channel.model_dump(mode="json"))
        return channel

    def delete(self, channel_id: str) -> bool:
        if channel_id == DEFAULT_ID:
            raise ValueError("the default channel cannot be deleted; narrow its rules instead")
        return self.store.delete_channel(channel_id)

    def ensure_default(self) -> Channel:
        return self.get(DEFAULT_ID) or self.create("default", "every machine, machine rules", channel_id=DEFAULT_ID)

    # ---------------------------------------------------------- the bounds

    def validate(self, rules: ChannelRules) -> None:
        """Channel rules may narrow the machine rules, never widen them."""
        problems = []
        m = self.machine_policy
        for system, srule in rules.systems.items():
            if system not in self._partitions:
                problems.append(f"unknown system {system!r}")
                continue
            for part, prule in (srule.partitions or {}).items():
                if part not in self._partitions[system]:
                    problems.append(f"{system}: unknown queue {part!r}")
                    continue
                if is_blocked(m, system, part):
                    problems.append(f"{system}:{part} is blocked on the machine")
                cap = effective_node_cap(m, system, part)
                if prule.max_nodes_per_job is not None and prule.max_nodes_per_job > cap:
                    problems.append(f"{system}:{part} max nodes {prule.max_nodes_per_job} exceeds the machine cap {cap}")
        if (rules.max_user_pending_per_system is not None
                and rules.max_user_pending_per_system > m["max_user_pending_per_system"]):
            problems.append(f"max pending {rules.max_user_pending_per_system} exceeds the machine cap "
                            f"{m['max_user_pending_per_system']}")
        if problems:
            raise ValueError("; ".join(problems))

    def allowed(self, channel: Channel) -> dict[str, int]:
        """The channel's queues and their effective caps: min(channel, machine),
        machine-blocked queues excluded — evaluated against the machine rules
        as they are now, so an operator edit applies to every channel at once."""
        m, out = self.machine_policy, {}
        for system, srule in channel.rules.systems.items():
            known = self._partitions.get(system)
            if known is None:
                continue                        # dropped from the catalog since the channel was shaped
            parts = srule.partitions if srule.partitions is not None else {p: PartitionRule() for p in known}
            for part, prule in parts.items():
                if part not in known or is_blocked(m, system, part):
                    continue
                cap = effective_node_cap(m, system, part)
                out[partition_key(system, part)] = min(cap, prule.max_nodes_per_job or cap)
        return out

    def account(self, channel: Channel, system: str, partition: str) -> Optional[str]:
        """The account the channel bills on a queue: the queue's rule, else the
        machine's, else None (= the catalog's default for that queue)."""
        srule = channel.rules.systems[system]
        prule = (srule.partitions or {}).get(partition)
        return (prule.account if prule else None) or srule.account

    def machines(self, channel: Channel) -> set[str]:
        """The machines the channel can actually use: those with at least one
        allowed queue. A machine listed in the rules but with every queue
        blocked on the machine (or none enabled) is not part of the channel."""
        return {key.split(":", 1)[0] for key in self.allowed(channel)}

    def policy(self, channel: Channel) -> dict:
        """The policy dict the rule chain and the dispatch gate consume for this channel."""
        m = self.machine_policy
        allowed = self.allowed(channel)
        pending = channel.rules.max_user_pending_per_system
        return {
            "max_nodes_per_job": m["max_nodes_per_job"],
            "max_user_pending_per_system": min(m["max_user_pending_per_system"], pending or m["max_user_pending_per_system"]),
            "blocked_partitions": list(m["blocked_partitions"]),
            "partition_limits": allowed,
            "allowed_partitions": sorted(allowed),
        }

    def scope(self, records: list[ResourceRecord], channel: Channel) -> list[ResourceRecord]:
        """What this channel's agents see: only its queues, billed to its account."""
        allowed = self.allowed(channel)
        out = []
        for r in records:
            if partition_key(r.system, r.partition) not in allowed:
                continue
            account = self.account(channel, r.system, r.partition)
            out.append(r.model_copy(update={"account": account}) if account else r)
        return out


# -------------------------------------------------------- the terminal

def main(argv: Optional[list[str]] = None) -> None:
    """The operator's console for channels where there is no web UI: what the
    Channels page does, on the store directly. The running core reads channels
    on every use, so a channel created here answers at once and one deleted
    here stops answering at once."""
    import argparse
    import json
    from pathlib import Path

    import yaml
    from dotenv import load_dotenv

    from .core.service import POLICY_SETTING
    from .policy import build_policy
    from .store import Store

    load_dotenv(override=False)
    parser = argparse.ArgumentParser(prog="python -m diamond_agent.channels", description=main.__doc__)
    parser.add_argument("--db", default=os.environ.get("DIAMOND_AGENT_DB", "diamond_agent.db"))
    parser.add_argument("--sites", default=os.environ.get("DIAMOND_AGENT_SITES", "sites.live.yaml"))
    sub = parser.add_subparsers(dest="command", required=True)
    ls = sub.add_parser("list", help="every channel and its URL (the line to hand an agent)")
    ls.add_argument("--json", action="store_true")
    cr = sub.add_parser("create", help="a new channel: every machine with the machine rules, or --rules")
    cr.add_argument("name")
    cr.add_argument("-d", "--description", default="")
    cr.add_argument("--rules", help="JSON file holding the channel's rules object (what PUT /ui/channels takes)")
    rm = sub.add_parser("delete", help="revoke a channel: its URL stops working (cancel its tasks first)")
    rm.add_argument("channel_id")
    a = parser.parse_args(argv)

    store = Store(a.db)
    try:
        _run(a, store, yaml.safe_load(Path(a.sites).read_text()), POLICY_SETTING, build_policy)
    finally:
        store.close()


def _run(a, store, catalog: dict, policy_setting: str, build_policy) -> None:
    import json
    from pathlib import Path

    registry = ChannelRegistry(store, catalog, build_policy(catalog, store.load_setting(policy_setting)))
    registry.ensure_default()
    base = origin()
    url = lambda c: f"{base}{c.path}"

    if a.command == "list":
        channels = registry.list()
        if a.json:
            print(json.dumps([{"id": c.id, "name": c.name, "url": url(c), "description": c.description,
                               "rules": c.rules.model_dump(mode="json")} for c in channels], indent=2))
            return
        for c in channels:
            note = "  (cannot be deleted)" if c.id == DEFAULT_ID else ""
            print(f"{c.id:<10} {c.name:<18} {url(c)}   {c.description}{note}")
        print("\nhand an agent its channel URL as DIAMOND_AGENT_URL, e.g.\n"
              f"    export DIAMOND_AGENT_URL={url(registry.get(DEFAULT_ID))}")
    elif a.command == "create":
        rules = ChannelRules.model_validate(json.loads(Path(a.rules).read_text())) if a.rules else None
        c = registry.create(a.name, a.description, rules)
        store.add_event("CHANNEL_CREATED", f"Operator created channel '{c.name}' ({c.id}) from the terminal",
                        detail={"allowed": registry.allowed(c)}, channel=c.id)
        print(f"created channel {c.id} '{c.name}'\n    export DIAMOND_AGENT_URL={url(c)}")
    else:
        c = registry.get(a.channel_id)
        if c is None:
            raise SystemExit(f"no channel {a.channel_id!r} (python -m diamond_agent.channels list)")
        active = [t for t in store.load_tasks().values()
                  if t["channel"] == c.id and t["status"] in ("SUBMITTED", "PENDING", "RUNNING")]
        if active:
            raise SystemExit(f"channel {c.id} has {len(active)} active task(s); cancel them first "
                             "(diamond-agent-core cancel <task_id> through its URL, or the web UI)")
        registry.delete(c.id)
        store.add_event("CHANNEL_DELETED", f"Operator deleted channel '{c.name}' ({c.id}) from the terminal", channel=c.id)
        print(f"deleted channel {c.id} '{c.name}'; its URL no longer answers")


if __name__ == "__main__":
    main()
