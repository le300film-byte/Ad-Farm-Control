#!/usr/bin/env python3
"""Forensic reproductions for the V9 critical-fix list (TODO.md).

Every F-item and P-item gets one self-contained scenario that fails on the pre-fix code and
passes on the fixed code. Unlike ``pytest``, this prints a human-readable verdict per item so an
operator can re-run the whole audit after a deploy without knowing the test layout:

    python analysis/forensic_repros.py            # run everything, exit 1 if any item fails
    python analysis/forensic_repros.py F01 F04    # only those items

Nothing here touches the network: GitHub, Gists and Discord are the in-memory fakes from
``tests/fakes.py``, so the scenarios exercise the real ``GitHubClient`` / ``GistBackup`` /
service layers with only the transport replaced.
"""
from __future__ import annotations

import ast
import asyncio
import base64
import dataclasses
import inspect
import json
import os
import re
import sqlite3
import sys
import tempfile
import traceback
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SENDER = ROOT / "sender" / "send_ads.py"
WORKFLOW = ROOT / "sender" / "workflows" / "send_ads.yml"

ADMIN = "100000000000000001"
CUSTOMER = "200000000000000001"
STRANGER = "300000000000000001"
ADMIN_CH = "400000000000000001"
PUBLIC_CH = "400000000000000002"
TICKET_CH = "400000000000000003"
AUDIT_CH = "400000000000000004"
HUB_CATEGORY = "500000000000000001"


# ─────────────────────────────────────────────────────────────────────────────
# harness
# ─────────────────────────────────────────────────────────────────────────────
class Check:
    def __init__(self, item: str, title: str):
        self.item, self.title = item, title
        self.failures: list[str] = []

    def that(self, condition: bool, detail: str) -> None:
        if not condition:
            self.failures.append(detail)

    def eq(self, actual, expected, detail: str) -> None:
        if actual != expected:
            self.failures.append(f"{detail}: expected {_show(expected)}, got {_show(actual)}")


def _show(value, limit: int = 160) -> str:
    """repr() that keeps the report readable (a base64 SQLite blob is 20 KB on one line)."""
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + f"… ({len(text)} chars)"


def environment():
    """A fully faked Services bundle (real code, fake transports)."""
    from adfarm.app import build_services
    from adfarm.config import Settings, WorkerAccount
    from adfarm.core.clock import FakeClock
    from adfarm.github.client import GitHubClient
    from tests.fakes import FakeDiscord, FakeGitHubTransport, fake_token_checker, valid_token

    tmp = tempfile.mkdtemp(prefix="forensic-")
    settings = Settings(
        bot_token="bot", guild_id="1", owner_ids=frozenset({ADMIN}), admin_alerts_channel_id=ADMIN_CH,
        audit_log_channel_id=AUDIT_CH, ticket_channel_id=TICKET_CH, customer_hub_category_id=HUB_CATEGORY,
        github_token="tok-main", core_repo="mainacct/adfarm-core",
        workers=(WorkerAccount("worker1", "tok-w1"), WorkerAccount("worker2", "tok-w2"), WorkerAccount("worker3", "tok-w3")),
        control_gist_id="ctlgist", backup_gist_id="bkgist", gist_token="tok-main",
        db_path=os.path.join(tmp, "adfarm.db"), token_vault_key="forensic-vault-key-123",
        payment_address="0xPAYME", run_id="run-1", offline_after=900,
    )
    transport = FakeGitHubTransport(tokens={"tok-main": "mainacct", "tok-w1": "worker1", "tok-w2": "worker2", "tok-w3": "worker3"})
    transport.add_gist("ctlgist", {})
    transport.add_gist("bkgist", {})
    fake = FakeDiscord()
    for cid, name in ((ADMIN_CH, "admin-alerts"), (AUDIT_CH, "audit-logs"), (PUBLIC_CH, "general-chat"), (TICKET_CH, "open-ticket")):
        fake.add_channel(cid, name)
    fake.members |= {ADMIN, CUSTOMER}
    fake.names.update({CUSTOMER: "alice"})
    clock = FakeClock(1_757_000_000.0)
    s = build_services(settings, fake, clock=clock, github=GitHubClient("tok-main", transport=transport, retries=1),
                       token_checker=fake_token_checker())
    s.dispatcher.discover_wait = 0.0

    result = asyncio.run(s.customers.activate(discord_id=CUSTOMER, username="alice", alt_count=2, days=30, actor_id=ADMIN))
    customer = result.customer
    alt = asyncio.run(s.alts.store_credentials(s.guard.actor_for(CUSTOMER), 1, token=valid_token("A"),
                                               channel_ids="111111111111111111,222222222222222222", display_name="main"))
    return SimpleNamespace(s=s, transport=transport, discord=fake, clock=clock, tmp=tmp,
                           customer=customer, alt=alt, control=customer.thread("control"), dashboard=customer.thread("dashboard"))


def make_db(path: str, *, timeout: float = 30.0):
    """``Database(path, busy_timeout=…)`` was added by the F05 fix; on pre-fix code we patch
    ``connect`` instead so the scenario still reproduces the lock-timeout behaviour."""
    from adfarm.db import Database

    try:
        return Database(path, busy_timeout=timeout)
    except TypeError:
        db = Database(path)

        def connect() -> sqlite3.Connection:
            conn = sqlite3.connect(path, timeout=timeout, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            return conn

        db.connect = connect
        return db


def sender_helpers():
    text = SENDER.read_text(encoding="utf-8")
    wanted = {"_webhook_base", "_webhook_execute"}
    tree = ast.parse(text)
    src = "\n".join(ast.get_source_segment(text, n) for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name in wanted)
    ns: dict = {}
    exec(compile(src, str(SENDER), "exec"), ns)
    return ns["_webhook_base"], ns["_webhook_execute"], text


# ─────────────────────────────────────────────────────────────────────────────
# scenarios
# ─────────────────────────────────────────────────────────────────────────────
def scenario_f01(c: Check) -> None:
    text = SENDER.read_text(encoding="utf-8")
    # Source-level checks first: these fail on pre-fix code with a real reason, not an ImportError.
    naive = re.findall(r'.*\+\s*"\?wait=true".*', text)
    c.eq([l.strip() for l in naive if not l.strip().startswith("#")], [],
         "no raw '+ \"?wait=true\"' appends may survive in send_ads.py")
    c.that("{DASHBOARD_WEBHOOK_URL}/messages/" not in text,
           "the heartbeat PATCH must not interpolate a query-carrying URL")
    base, execute, _ = sender_helpers()
    forum = "https://discord.com/api/webhooks/111/tok?thread_id=456"
    c.eq(execute(forum), forum + "&wait=true", "forum webhook + wait=true must join with '&'")
    c.eq(execute("https://discord.com/api/webhooks/111/tok"), "https://discord.com/api/webhooks/111/tok?wait=true",
         "a plain webhook still gets '?'")
    c.eq(base(forum), "https://discord.com/api/webhooks/111/tok", "edit URL must drop the query string")
    c.eq(f"{base(forum)}/messages/777", "https://discord.com/api/webhooks/111/tok/messages/777", "heartbeat PATCH url")


def scenario_f02(c: Check) -> None:
    from adfarm.github.workflows import WORKFLOW_INPUTS, build_inputs

    declared = set()
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    start = next(i for i, l in enumerate(lines) if l.strip() == "inputs:")
    for line in lines[start + 1:]:
        if line.strip() and not line.startswith("      "):
            break
        m = re.match(r"^      ([a-z0-9_]+):\s*$", line)
        if m:
            declared.add(m.group(1))
    c.eq(WORKFLOW_INPUTS, declared, "build_inputs must only emit inputs send_ads.yml declares")

    env = environment()
    ctx_run(env, hours=0)
    inputs = env.transport.repo(env.alt.repo_owner, env.alt.repo_name).dispatches[-1]["inputs"]
    c.eq(inputs["runtime_limitless"], "1", "limitless must dispatch runtime_limitless=1 (choice 0/1)")
    c.eq(inputs["attach_image"], "no", "attach_image must be yes/no, never true/false")
    c.that(set(inputs) <= WORKFLOW_INPUTS, "every dispatched input must be declared by the workflow")

    ctx_run(env, hours=6, image=b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    from adfarm.github.repos import AD_IMAGE_PATH

    repo = env.transport.repo(env.alt.repo_owner, env.alt.repo_name)
    c.that(AD_IMAGE_PATH in repo.files, "the ad image must be committed to the repo (IMAGE_PATH default)")
    c.that("AD_IMAGE_B64" not in repo.secrets, "the image must no longer be pushed as a 48 KB-capped secret")


def ctx_run(env, *, hours: int, image: bytes | None = None):
    from adfarm.commands import customer as cust
    from adfarm.commands.context import CommandContext, run_handler
    from adfarm.discord.channels import ChannelClassifier

    classifier = ChannelClassifier(env.s.settings, env.s.customers.by_forum)
    info = classifier.classify(env.discord.channels[env.control])
    gate = env.s.guard.check(CUSTOMER, "run", info)
    ctx = CommandContext(services=env.s, user_id=CUSTOMER, username="alice", channel=env.discord.channels[env.control],
                         channel_info=info, kind=gate.kind, actor=gate.actor, command="run",
                         options={"mode": "sell", "rate": "2.3", "message": "hi", "interval": 5, "hours": hours, "alt": 1, "policy_ack": True})
    if image is not None:
        ctx.attachment_bytes = image
        ctx.attachment_content_type = "image/png"
    return asyncio.run(run_handler(cust.run, ctx))


def _hb(env, *, status: str, sent: int):
    embed = SimpleNamespace(
        title="💓 Heartbeat · main", description="",
        footer=SimpleNamespace(text=f"alt_id={env.alt.sender_alt_id} · V6.0 · updated 10:00:00"),
        fields=[SimpleNamespace(name="Status", value=f"`{status}`"),
                SimpleNamespace(name="Activity", value=f"Sent: `{sent}` · Errors: `0` · Skips: `0`")])
    return SimpleNamespace(id="900000000000000001", webhook_id=1, content=f"💓 **Heartbeat** · `{status}`",
                           channel=SimpleNamespace(id=env.dashboard), author=SimpleNamespace(name="main"), embeds=[embed])


def scenario_f03(c: Check) -> None:
    from adfarm.app import build_ingestor, ingest_message

    env = environment()
    ingestor = build_ingestor(env.s)
    asyncio.run(ingest_message(env.s, ingestor, _hb(env, status="active", sent=1)))
    live = env.s.fleet.get((CUSTOMER, 1))
    c.that(live is not None and live.online and live.total_sent == 1, "first heartbeat must be ingested")

    # the sender PATCHes the same message — this is the MESSAGE_UPDATE path F03 added
    asyncio.run(ingest_message(env.s, ingestor, _hb(env, status="paused", sent=42)))
    live = env.s.fleet.get((CUSTOMER, 1))
    c.eq(live.status, "paused", "an EDITED heartbeat must update the fleet state")
    c.eq(live.total_sent, 42, "an EDITED heartbeat must update the counters")

    src = (ROOT / "adfarm" / "app.py").read_text(encoding="utf-8")
    for handler in ("async def on_message_edit", "async def on_raw_message_edit"):
        c.that(handler in src, f"app.py must register {handler}")


def scenario_f04(c: Check) -> None:
    from adfarm.core.clock import FakeClock
    from adfarm.db import GistBackup
    from adfarm.github.client import GitHubClient
    from tests.fakes import FakeGitHubTransport

    transport = FakeGitHubTransport(tokens={"t": "main"})
    transport.add_gist("g1", {})
    client = GitHubClient("t", transport=transport, retries=1)
    with tempfile.TemporaryDirectory() as tmp:
        db = make_db(os.path.join(tmp, "b.db"), timeout=0.5)
        db.migrate()
        clock = FakeClock(1000)
        a = GistBackup(db, client, "g1", run_id="run-A", clock=clock, lease_ttl=600)
        b = GistBackup(db, client, "g1", run_id="run-B", clock=clock, lease_ttl=600)
        c.that(a.acquire_lease(), "run-A acquires the lease")
        lock = lambda: json.loads(transport.gists["g1"]["files"]["LOCK"]["content"])  # noqa: E731
        c.that(not b.acquire_lease(), "run-B must not steal a live lease")
        c.eq(lock()["run_id"], "run-A", "the lock still names run-A")
        c.that(a.release_lease(), "the holder releases its own lease")
        c.that(not a.release_lease(), "a second release by a non-holder must be refused")
        c.eq(lock()["run_id"], "", "the lock is now free")
        c.that(b.acquire_lease(), "run-B can take the freed lease")
        c.that(not a.release_lease(), "run-A must NOT be able to release run-B's lease")
        c.eq(lock()["run_id"], "run-B", "run-B's lease survived run-A's release attempt")
        clock.advance(601)                                   # run-B's lease has now expired
        c.that(a.acquire_lease(), "run-A takes the expired lease")
        c.eq(lock()["run_id"], "run-A", "run-A now holds it")
        c.that(not b.renew_lease(), "run-B must not renew a lease it no longer holds")
        c.eq(lock()["run_id"], "run-A", "run-B's renewal attempt must not have stolen it back")


def scenario_f05(c: Check) -> None:
    from adfarm.db import MetaRepo

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "locked.db")
        db = make_db(path, timeout=0.2)
        db.migrate()
        blocker = sqlite3.connect(path, isolation_level=None)
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute("CREATE TABLE IF NOT EXISTS _probe (x INTEGER)")
        raised = False
        try:
            with db.transaction() as conn:
                conn.execute("INSERT INTO meta(key, value) VALUES ('never', 'committed')")
        except sqlite3.OperationalError:
            raised = True
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()
        c.that(raised, "a lock timeout must surface as OperationalError")
        c.eq(getattr(db._local, "depth", 0), 0, "a failed BEGIN must leave depth=0 (not 1)")
        c.that(getattr(db._local, "conn", None) is None, "a failed BEGIN must clear the thread-local conn")
        MetaRepo(db).set("after", "1")
        c.eq(MetaRepo(db).get("after"), "1", "the next transaction must really commit")
        c.eq(MetaRepo(db).get("never"), "", "the failed transaction must not have committed")


def scenario_f06(c: Check) -> None:
    from adfarm.core.clock import FakeClock
    from adfarm.db import CustomerRepo, Database, GistBackup
    from adfarm.core.models import Customer
    from adfarm.github.client import GitHubClient
    from tests.fakes import FakeGitHubTransport

    transport = FakeGitHubTransport(tokens={"t": "main"})
    transport.add_gist("g1", {})
    client = GitHubClient("t", transport=transport, retries=1)
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(os.path.join(tmp, "good.db"))
        db.migrate()
        CustomerRepo(db).save(Customer("1", "a", 1, False, 0, 100, True), now=1.0)
        GistBackup(db, client, "g1", run_id="run-A", clock=FakeClock(1), retries=()).flush()
        transport.gists["g1"]["files"]["adfarm.db.b64"]["content"] = base64.b64encode(b"not a database").decode()
        transport.gists["g1"]["history"] = []

        fresh = Database(os.path.join(tmp, "empty.db"))
        fresh.migrate()
        restorer = GistBackup(fresh, client, "g1", run_id="run-B", clock=FakeClock(1), retries=())
        c.eq(restorer.restore_if_missing(), "none", "an unusable snapshot must restore to 'none'")
        c.that(getattr(restorer, "restore_blocked", False), "the upload interlock must arm after a failed restore")
        c.that(not restorer.flush(), "the empty local DB must NOT overwrite the remote snapshot")
        c.that("refusing to overwrite" in restorer.last_error, f"the operator must be told why: {restorer.last_error!r}")
        c.eq(transport.gists["g1"]["files"]["adfarm.db.b64"]["content"],
             base64.b64encode(b"not a database").decode(), "the remote file is untouched")
        c.that(restorer.flush(force=True) if "force" in inspect.signature(restorer.flush).parameters else False,
               "/admin backup sub:force is the escape hatch")
        c.that(not fresh.payload_is_usable(b"not a database"), "a non-SQLite payload must be rejected")
        c.that(fresh.payload_is_usable(fresh.snapshot_bytes()), "a real snapshot must be accepted")


def scenario_f09(c: Check) -> None:
    from adfarm.core.models import DAY, RunMode
    from adfarm.commands import customer as cust
    from adfarm.commands.context import run_handler
    from adfarm.security.policy import ChannelKind, Tier, decide
    from adfarm.services.runs import merged_renewal_payload

    merged = merged_renewal_payload({"rate": 2.0, "message": "old", "ad_type": "sell"}, {"rate": 3.5, "message": "new"})
    c.eq(merged["rate"], 3.5, "renewal must keep the tuned price")
    c.eq(merged["message"], "new", "renewal must keep the tuned message")
    c.eq(merged["ad_type"], "sell", "untuned keys must survive")

    c.that(decide(Tier.PUBLIC, "renew", ChannelKind.TICKET, state="expired").allowed,
           "an expired customer must be able to run /renew")
    c.that(not decide(Tier.PUBLIC, "run", ChannelKind.TICKET, state="expired").allowed,
           "…but not /run")

    env = environment()
    ctx_run(env, hours=0)
    run_state = env.s.repos.runs.get(CUSTOMER, 1)
    c.that(run_state.mode is RunMode.LIMITLESS, "hours:0 must create a LIMITLESS run")
    asyncio.run(env.s.runs.tune(env.alt, actor_id=CUSTOMER, price="9.99", message="tuned copy", interval=3))
    env.clock.advance(48 * 3600 + 60)
    asyncio.run(env.s.runs.renew(run_state))
    inputs = env.transport.repo(env.alt.repo_owner, env.alt.repo_name).dispatches[-1]["inputs"]
    c.eq(inputs["sell_rate"], "9.99", "the 48 h renewal must not revert the price")
    c.eq(inputs["message"], "tuned copy", "the 48 h renewal must not revert the message")
    c.eq(inputs["interval_min"], "3", "the 48 h renewal must not revert the cadence")

    customer = env.s.repos.customers.get(CUSTOMER)
    env.s.repos.customers.save(customer.with_(expiry_date=env.s.now() - DAY), now=env.s.now())
    c.that(env.s.guard.actor_for(CUSTOMER).tier is Tier.PUBLIC, "an expired customer drops to PUBLIC")
    from adfarm.discord.channels import ChannelClassifier

    classifier = ChannelClassifier(env.s.settings, env.s.customers.by_forum)
    info = classifier.classify(env.discord.channels[TICKET_CH])
    gate = env.s.guard.check(CUSTOMER, "renew", info)
    c.that(gate.decision.allowed, f"/renew must be allowed while expired ({gate.decision.reason})")
    reply = asyncio.run(run_handler(cust.renew, SimpleNamespace(
        s=env.s, actor=gate.actor, is_admin=False, user_id=CUSTOMER, integer=lambda k, d=None: d, text=lambda k, d="": d)))
    c.that(reply.content.startswith("🧾"), f"/renew must open a ticket: {reply.content!r}")


def scenario_titles(c: Check) -> None:
    from adfarm.commands.registry import MODAL_TITLE_LIMIT, SetupModal, TicketModal, modal_title

    c.eq(MODAL_TITLE_LIMIT, 45, "Discord caps modal titles at 45 characters")
    for i in range(1, 5):
        title = SetupModal(None, None, i).title
        c.that(len(title) <= 45, f"SetupModal(alt={i}) title is {len(title)} chars: {title!r}")
    c.that(len(TicketModal(None).title) <= 45, "TicketModal title must fit")
    c.eq(len(modal_title("x" * 200)), 45, "modal_title must clamp")
    c.eq(len("Setup alt 1 (never share this token elsewhere)"), 46, "the V9.1 title really was 46 chars")


def scenario_permissions(c: Check) -> None:
    from adfarm.discord.permissions import VIEW, forum_overwrites, hub_overwrites, public_overwrites, staff_overwrites

    by = lambda ows: {(o.target, o.target_id): o for o in ows}  # noqa: E731
    pub = by(public_overwrites("r1"))
    c.that(VIEW in pub[("everyone", "")].allow and "send_messages" in pub[("everyone", "")].allow,
           "public rooms must be readable/writable by @everyone")
    staff = by(staff_overwrites("r1", [ADMIN]))
    c.that(VIEW in staff[("everyone", "")].deny, "staff rooms must be hidden from @everyone")
    hub = by(hub_overwrites("r1", [ADMIN]))
    c.that(VIEW in hub[("everyone", "")].deny, "the Customer Hub category must be hidden from @everyone")
    forum = by(forum_overwrites(customer_user_id=CUSTOMER, admin_role_id="r1", admin_user_ids=(ADMIN,)))
    c.that(VIEW in forum[("everyone", "")].deny, "a customer forum must be hidden from @everyone")
    c.that("send_messages" in forum[("member", CUSTOMER)].allow, "the customer can post in their own forum")
    c.that(VIEW in forum[("member", ADMIN)].allow, "admins can see every forum")


def scenario_help_admin(c: Check) -> None:
    from adfarm.commands import admin as adm
    from adfarm.security.policy import COMMAND_TIERS, Tier

    documented = {name for name, _, _ in adm.ADMIN_HELP}
    missing = [a for a in adm.ADMIN_ACTIONS if a not in documented]
    c.eq(missing, [], "every ADMIN_ACTIONS entry must appear in /help-admin")
    c.that(hasattr(adm, "help_admin"), "admin.help_admin must exist")
    c.that(COMMAND_TIERS.get("help-admin") is Tier.ADMIN, "/help-admin must be admin-only")


def scenario_privacy(c: Check) -> None:
    from adfarm.commands import customer as cust
    from adfarm.commands.context import run_handler
    from adfarm.discord.channels import ChannelClassifier

    env = environment()
    classifier = ChannelClassifier(env.s.settings, env.s.customers.by_forum)

    def call(user, command, handler, channel, **options):
        info = classifier.classify(env.discord.channels[channel])
        gate = env.s.guard.check(user, command, info)
        ctx = SimpleNamespace(s=env.s, actor=gate.actor, is_admin=gate.actor.is_admin, user_id=user,
                              integer=lambda k, d=None: options.get(k, d), text=lambda k, d="": options.get(k, d),
                              flag=lambda k, d=None: options.get(k, d))
        return asyncio.run(run_handler(handler, ctx))

    reply = call(CUSTOMER, "status", cust.status, env.control)
    body = reply.embed.to_dict()
    c.that(env.alt.repo_slug not in json.dumps(body), "the repo slug must not reach a customer's /status")
    c.that("Repo" not in [f["name"] for f in body["fields"]], "/status must have no Repo field for customers")
    admin_reply = call(ADMIN, "status", cust.status, ADMIN_CH, customer=CUSTOMER)
    c.that("Repo" in [f["name"] for f in admin_reply.embed.to_dict()["fields"]], "admins still see the repo")


def scenario_policy(c: Check) -> None:
    from adfarm.discord.policy import POLICY_ACCEPT_LABEL, POLICY_TEXT, POLICY_TITLE

    c.eq(POLICY_TITLE, "📜 AdFarm V9 — Service Agreement", "policy title")
    for banned in ("ban risk", "without refund", "pro-rated"):
        c.that(banned not in POLICY_TEXT, f"risk-first wording must be gone: {banned!r}")
    for wanted in ("BEP-20", "main accounts are not supported", "stored encrypted"):
        c.that(wanted in POLICY_TEXT, f"the agreement must state: {wanted!r}")
    c.that("✅" in POLICY_ACCEPT_LABEL, "the accept button must carry the ✅ the text refers to")


def scenario_ticket_panel(c: Check) -> None:
    from adfarm.commands import admin as adm
    from adfarm.commands.context import run_handler
    from adfarm.discord.channels import ChannelClassifier
    from adfarm.security.guards import ChannelInfo
    from adfarm.security.policy import ChannelKind

    env = environment()
    classifier = ChannelClassifier(env.s.settings, env.s.customers.by_forum)
    info = classifier.classify(env.discord.channels[ADMIN_CH])
    gate = env.s.guard.check(ADMIN, "admin", info)
    ctx = SimpleNamespace(s=env.s, actor=gate.actor, is_admin=True, user_id=ADMIN,
                          channel=SimpleNamespace(id=ADMIN_CH),
                          text=lambda k, d="": TICKET_CH if k == "channel" else d,
                          integer=lambda k, d=None: d, flag=lambda k, d=None: d)
    reply = asyncio.run(run_handler(adm.admin, _ctx(ctx, {"action": "ticket-panel", "channel": TICKET_CH})))
    c.that(reply.content.startswith("📌"), f"ticket-panel must confirm: {reply.content!r}")
    c.eq(reply.view.get("kind"), "post_ticket_panel", "the handler must hand the posting to the registry")
    c.eq(reply.view.get("channel"), TICKET_CH, "…for the requested channel")

    ticket = asyncio.run(env.s.tickets.open_support(discord_id=CUSTOMER, topic="I want 2 alts for 30 days", username="alice"))
    c.that(ticket.kind == "support" and ticket.id > 0, "open_support must record a ticket")
    c.that(bool(env.discord.threads_created), "open_support must create a thread in the ticket channel")
    c.that(ticket.channel_id and ticket.channel_id != TICKET_CH, "the ticket points at the thread, not the parent")


def _ctx(base, options):
    from adfarm.commands.context import CommandContext
    from adfarm.security.guards import ChannelInfo
    from adfarm.security.policy import ChannelKind

    return CommandContext(services=base.s, user_id=base.user_id, username="admin", channel=SimpleNamespace(id=ADMIN_CH),
                          channel_info=ChannelInfo(channel_id=ADMIN_CH, kind_hint=ChannelKind.ADMIN),
                          kind=ChannelKind.ADMIN, actor=base.actor, command="admin", options=options)


def scenario_hardcoded(c: Check) -> None:
    offenders = []
    for path in sorted((ROOT / "adfarm").rglob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r'"\d{16,20}"', line) and not line.strip().startswith("#"):
                offenders.append(f"{path.name}:{lineno}")
    c.eq(offenders, [], "no hardcoded 16-20 digit ids in adfarm/")


def scenario_visibility(c: Check) -> None:
    import discord as _discord

    from adfarm.commands.registry import CommandRegistry
    from adfarm.discord.channels import ChannelClassifier
    from adfarm.security.guards import ChannelInfo
    from adfarm.security.policy import ADMIN_ONLY_COMMANDS, COMMAND_TIERS, ChannelKind

    env = environment()
    classifier = ChannelClassifier(env.s.settings, env.s.customers.by_forum)
    tree = _discord.app_commands.CommandTree(_discord.Client(intents=_discord.Intents.none()))
    registry = CommandRegistry(tree, env.s, classifier, guild_id="1")
    registry.register_all()
    by_name = {cmd.name: cmd for cmd in tree.get_commands()}
    for name in ADMIN_ONLY_COMMANDS:
        c.that(name in by_name, f"/{name} must be registered")
        perms = getattr(by_name.get(name), "default_permissions", None)
        c.that(perms is not None and perms.administrator is True, f"/{name} must require Administrator")
    for name, cmd in by_name.items():
        c.that(cmd.guild_only is True, f"/{name} must be guild-only")

    info = ChannelInfo(channel_id=PUBLIC_CH, kind_hint=ChannelKind.PUBLIC)
    usable = sorted(cmd for cmd in COMMAND_TIERS if env.s.guard.check(STRANGER, cmd, info).decision.allowed)
    c.eq(usable, ["getstarted", "help"], "a stranger may only use /help and /getstarted")


SCENARIOS = {
    "F01": ("Sender webhook URLs are well-formed", scenario_f01),
    "F02": ("Workflow flags + image transport match send_ads.yml", scenario_f02),
    "F03": ("Edited heartbeats update FleetState", scenario_f03),
    "F04": ("DB lease: ownership-checked release + CAS acquire", scenario_f04),
    "F05": ("A failed BEGIN does not poison later transactions", scenario_f05),
    "F06": ("A failed restore cannot be overwritten by an empty DB", scenario_f06),
    "F09": ("Renewal keeps tuning; expired customers can /renew", scenario_f09),
    "P1-1": ("Modal titles fit Discord's 45-char limit", scenario_titles),
    "P1-2": ("Channel/forum permission matrix", scenario_permissions),
    "P1-3": ("/help-admin documents every admin action", scenario_help_admin),
    "P1-4": ("Repo names hidden from customers", scenario_privacy),
    "P1-5": ("Service agreement replaces the risk-first policy", scenario_policy),
    "P1-7": ("Ticket panel has a working button", scenario_ticket_panel),
    "P2-8": ("No hardcoded Discord ids in the package", scenario_hardcoded),
    "P2-9": ("Command visibility", scenario_visibility),
}


def main(argv: list[str]) -> int:
    wanted = [a.upper() for a in argv if a.upper() in SCENARIOS] or list(SCENARIOS)
    width = max(len(k) for k in wanted)
    failures = 0
    for item in wanted:
        title, fn = SCENARIOS[item]
        check = Check(item, title)
        try:
            fn(check)
        except Exception:
            check.failures.append("raised:\n" + traceback.format_exc(limit=6))
        if check.failures:
            failures += 1
            print(f"  ✗ {item:<{width}}  {title}")
            for f in check.failures:
                print(f"      – {f}")
        else:
            print(f"  ✓ {item:<{width}}  {title}")
    print()
    print(f"{len(wanted) - failures}/{len(wanted)} forensic checks passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
