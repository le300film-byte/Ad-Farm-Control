"""Shared fixtures: scrubbed environment, temp database, fully faked external systems."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adfarm.app import build_services  # noqa: E402
from adfarm.commands.context import CommandContext, run_handler  # noqa: E402
from adfarm.config import Settings, WorkerAccount  # noqa: E402
from adfarm.core.clock import FakeClock  # noqa: E402
from adfarm.discord.channels import ChannelClassifier  # noqa: E402
from adfarm.discord.ports import ChannelRef  # noqa: E402
from adfarm.github.client import GitHubClient  # noqa: E402
from adfarm.security.policy import ChannelKind  # noqa: E402
from tests.fakes import FakeDiscord, FakeGitHubTransport, fake_token_checker  # noqa: E402

SCRUB = ("BOT_TOKEN", "GH_TOKEN", "GH_ADMIN_TOKEN", "GITHUB_PAT", "GIST_TOKEN", "WORKER_TOKENS", "WORKER_TOKENS_LIST", "WORKER_GITHUB_OWNERS",
         "WORKER_1_TOKEN", "WORKER_2_TOKEN", "WORKER_3_TOKEN", "WORKER_1_USER", "WORKER_2_USER", "WORKER_3_USER", "TOKEN_VAULT_KEY", "OWNER_IDS",
         "CONTROL_GIST_ID", "ADFARM_GIST_ID", "CUSTOMERS_GIST_ID", "ADFARM_DB", "CUSTOMERS_DB", "TUNING_JSON", "GITHUB_REPOSITORY", "GITHUB_RUN_ID")

ADMIN = "100000000000000001"
ADMIN2 = "100000000000000002"
CUSTOMER = "200000000000000001"
OTHER = "200000000000000002"
STRANGER = "300000000000000001"
ADMIN_CH = "400000000000000001"
PUBLIC_CH = "400000000000000002"
TICKET_CH = "400000000000000003"
AUDIT_CH = "400000000000000004"
HUB_CATEGORY = "500000000000000001"


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch):
    for key in SCRUB:
        monkeypatch.delenv(key, raising=False)


def run(coro):
    """Run a coroutine to completion (tests stay free of an asyncio plugin)."""
    return asyncio.run(coro)


@pytest.fixture
def clock():
    return FakeClock(1_757_000_000.0)  # 2025-09-04-ish


@pytest.fixture
def settings(tmp_path):
    return Settings(
        bot_token="bot", guild_id="1", owner_ids=frozenset({ADMIN, ADMIN2}), admin_alerts_channel_id=ADMIN_CH, audit_log_channel_id=AUDIT_CH,
        ticket_channel_id=TICKET_CH, customer_hub_category_id=HUB_CATEGORY, github_token="tok-main", core_repo="mainacct/adfarm-core",
        workers=(WorkerAccount("worker1", "tok-w1"), WorkerAccount("worker2", "tok-w2"), WorkerAccount("worker3", "tok-w3")),
        control_gist_id="ctlgist", backup_gist_id="bkgist", gist_token="tok-main", db_path=str(tmp_path / "adfarm.db"), token_vault_key="unit-test-vault-key-123",
        payment_address="0xPAYME", run_id="run-1", multisig_window=120, offline_after=900,
    )


@pytest.fixture
def transport():
    t = FakeGitHubTransport(tokens={"tok-main": "mainacct", "tok-w1": "worker1", "tok-w2": "worker2", "tok-w3": "worker3"})
    t.add_gist("ctlgist", {})
    t.add_gist("bkgist", {})
    return t


@pytest.fixture
def discord():
    d = FakeDiscord()
    d.add_channel(ADMIN_CH, "admin-alerts")
    d.add_channel(AUDIT_CH, "audit-logs")
    d.add_channel(PUBLIC_CH, "general-chat")
    d.add_channel(TICKET_CH, "open-ticket")
    d.members |= {ADMIN, ADMIN2, CUSTOMER, OTHER}
    d.names.update({CUSTOMER: "alice", OTHER: "bob"})
    return d


@pytest.fixture
def services(settings, transport, discord, clock):
    gh = GitHubClient("tok-main", transport=transport, retries=1)
    s = build_services(settings, discord, clock=clock, github=gh, token_checker=fake_token_checker())
    s.dispatcher.discover_wait = 0.0
    return s


@pytest.fixture
def classifier(settings, services):
    return ChannelClassifier(settings, services.customers.by_forum)


class Invoker:
    """Drive a command exactly like the registry does: classify → guard → handler → Reply."""

    def __init__(self, services, classifier, discord):
        self.s, self.classifier, self.discord = services, classifier, discord

    def ctx(self, user_id: str, command: str, channel_id: str, **options) -> CommandContext:
        ref = self.discord.channels.get(channel_id) or ChannelRef(id=channel_id, kind="dm")
        info = self.classifier.classify(ref)
        gate = self.s.guard.check(user_id, command, info)
        ctx = CommandContext(services=self.s, user_id=user_id, username=self.discord.names.get(user_id, user_id), channel=ref, channel_info=info, kind=gate.kind,
                             actor=gate.actor, command=command, options=options)
        ctx.gate = gate  # type: ignore[attr-defined]
        return ctx

    def call(self, user_id: str, command: str, handler, channel_id: str, **options):
        ctx = self.ctx(user_id, command, channel_id, **options)
        if not ctx.gate.decision.allowed:  # type: ignore[attr-defined]
            from adfarm.discord.replies import Reply

            return Reply.error(ctx.gate.decision.reason)  # type: ignore[attr-defined]
        return run(run_handler(handler, ctx))


@pytest.fixture
def invoke(services, classifier, discord):
    return Invoker(services, classifier, discord)


@pytest.fixture
def activated(services, discord):
    """A customer with a hub and one registered alt, ready to run."""
    from adfarm.commands import customer as cust
    from tests.fakes import valid_token

    result = run(services.customers.activate(discord_id=CUSTOMER, username="alice", alt_count=2, days=30, actor_id=ADMIN))
    customer = result.customer
    actor = services.guard.actor_for(CUSTOMER)
    alt = run(services.alts.store_credentials(actor, 1, token=valid_token("A"), channel_ids="111111111111111111,222222222222222222", display_name="main"))
    return {"customer": services.repos.customers.get(CUSTOMER), "alt": alt, "control": customer.thread("control"), "dashboard": customer.thread("dashboard"),
            "logs": customer.thread("farm-logs"), "forum": customer.forum_id}
