"""End-to-end lifecycle through the real command gate: activate → setup → run → tune → heartbeat → stop → expiry.

Everything below runs against a temp SQLite DB, an in-memory GitHub (repos/secrets/dispatches/gists) and an in-memory
Discord. No network, no destructive operations.
"""
import json

from adfarm.commands import admin as adm
from adfarm.commands import customer as cust
from adfarm.commands import public as pub
from adfarm.core.models import DAY, AltStatus
from adfarm.security import policy
from adfarm.telemetry import IncomingMessage
from adfarm.telemetry.heartbeat import EmbedLike
from adfarm.app import build_ingestor, register_jobs
from adfarm.timers import Scheduler
from tests.conftest import ADMIN, ADMIN_CH, CUSTOMER, run
from tests.fakes import valid_token


def _heartbeat(alt_id: int, *, sent=12, status="active"):
    """Same layout as send_ads.py `_send_heartbeat`."""
    fields = [("Status", f"`{status}`"), ("Mode", "`sell`"), ("Rate", "`2.30$/1k`"), ("Cadence", "`5m`"),
              ("Activity", f"Sent: `{sent}` · Errors: `0` · Skips: `0`"), ("Uptime", "42.0 min"), ("Channels", "Active: `2/2`")]
    return EmbedLike(title="💓 Heartbeat · main", footer=f"alt_id={alt_id} · V6.0 · updated 10:00:00", fields=fields)


def test_full_customer_journey(invoke, services, transport, discord, clock):
    # 1. admin activates → hub, threads, webhooks, role
    r = invoke.call(ADMIN, "admin", adm.admin, ADMIN_CH, action="activate", user=CUSTOMER, days=30, alts=1, username="alice")
    assert r.content.startswith("✅ Activated")
    customer = services.repos.customers.get(CUSTOMER)
    control, dashboard, logs = customer.thread("control"), customer.thread("dashboard"), customer.thread("farm-logs")
    assert control and dashboard and logs and services.customers.webhooks(CUSTOMER).complete()

    # 2. customer registers the alt (modal submit) → repo exists on a worker with all secrets and the sender files
    done = invoke.call(CUSTOMER, "setup", cust.setup_submit, control, alt=1, token=valid_token("A"), channels="111111111111111111,222222222222222222")
    assert done.content.startswith("✅ Alt 1")
    alt = services.repos.alts.get(CUSTOMER, 1)
    repo = transport.repo(alt.repo_owner, alt.repo_name)
    assert alt.status is AltStatus.READY and {"USER_TOKEN", "CHANNEL_IDS", "DASHBOARD_WEBHOOK_URL", "LOG_WEBHOOK_URL", "CONTROL_GIST_ID", "GIST_TOKEN", "CONTROLLER_USER_IDS"} <= set(repo.secrets)
    assert repo.variables["ALT_ID"] == "1" and "send_ads.py" in repo.files and ".github/workflows/send_ads.yml" in repo.files

    # 3. /run → workflow dispatched with the right inputs; run tracked; control gist seeded
    started = invoke.call(CUSTOMER, "run", cust.run, control, mode="sell", rate="2.3", message="WTS gems", interval=5, hours=24, policy_ack=True)
    assert started.content.startswith("🚀")
    dispatch = repo.dispatches[-1]
    assert dispatch["ref"] == "main" and dispatch["inputs"]["ad_type"] == "sell" and dispatch["inputs"]["sell_rate"] == "2.30" and dispatch["inputs"]["total_hours"] == "24"
    assert dispatch["inputs"]["channel_1"] == "111111111111111111" and dispatch["inputs"]["channel_2"] == "222222222222222222"
    state = services.repos.runs.get(CUSTOMER, 1)
    assert state.status == "in_progress" and state.run_id == repo.runs[0]["id"]

    # 4. heartbeat arrives on THIS customer's dashboard webhook → routed to alice#1 only
    ingestor = build_ingestor(services)
    res = ingestor.ingest(IncomingMessage(channel_id=dashboard, author_name="alice-alt1", content="", embeds=[_heartbeat(1, sent=13)], is_webhook=True))
    assert res.kind == "heartbeat" and res.key == (CUSTOMER, 1)
    live = services.fleet.get((CUSTOMER, 1))
    assert live.online and live.total_sent == 13 and live.status == "active"
    st = invoke.call(CUSTOMER, "status", cust.status, control)
    fields = {f["name"]: f["value"] for f in st.embed.to_dict()["fields"]}
    assert "13" in fields["Activity"]

    # 5. live tuning goes through the control gist keyed by the sender ALT_ID
    tuned = invoke.call(CUSTOMER, "tune", cust.tune, control, price="2.9", message="WTS gems cheap")
    assert tuned.content.startswith("🎛️")
    ctl = json.loads(transport.gists["ctlgist"]["files"]["control_1.json"]["content"])
    assert ctl["rate"] == 2.9 and ctl["message"] == "WTS gems cheap" and ctl["command"] in ("setprice", "setmessage", "policy")

    # 6. log line lands in farm-logs → stored per alt, visible with /alt logs
    ingestor.ingest(IncomingMessage(channel_id=logs, author_name="alice-alt1", content="[INFO] posted in 111111111111111111", is_webhook=True))
    assert "posted in" in invoke.call(CUSTOMER, "alt", cust.alt, control, action="logs").content

    # 7. stop → GitHub run cancelled, control gist told, DB state cancelled
    stopped = invoke.call(CUSTOMER, "stop", cust.stop, control)
    assert stopped.content.startswith("🛑") and repo.runs[0]["conclusion"] == "cancelled"
    assert services.repos.runs.get(CUSTOMER, 1).status == "cancelled"

    # 8. the DB snapshot went to the backup gist (write-through) and is restorable
    services.backup.flush()
    files = transport.gists["bkgist"]["files"]
    meta = json.loads(files["db-meta.json"]["content"])
    assert "adfarm.db.b64" in files and meta["sha256"] and meta["schema"] >= 1

    # 9. time passes: reminders 7/3/1 then expiry → forum read-only, role removed, run state gone
    scheduler = Scheduler(on_error=lambda n, e: None)
    register_jobs(services, scheduler)
    expiry_job = next(j for j in scheduler.jobs() if j.name == "expiry").job
    sent_before = len(discord.dms)
    clock.advance(23 * DAY + 3600)         # 7 days left
    run(expiry_job())
    assert len(discord.dms) == sent_before + 1
    run(expiry_job())                   # idempotent within the same threshold
    assert len(discord.dms) == sent_before + 1
    clock.advance(7 * DAY)                 # expired
    run(expiry_job())
    c = services.repos.customers.get(CUSTOMER)
    assert not c.active and discord.readonly[c.forum_id] and "Customer" not in discord.roles[CUSTOMER]
    assert invoke.call(CUSTOMER, "run", cust.run, control, mode="sell", rate="2", message="m", interval=5, hours=24).content == policy.DENY_EXPIRED

    # 10. renewal re-activation keeps the hub, re-opens it and extends from now (not from the stale expiry)
    again = invoke.call(ADMIN, "admin", adm.admin, ADMIN_CH, action="activate", user=CUSTOMER, days=30, alts=1)
    assert again.content.startswith("✅")
    c = services.repos.customers.get(CUSTOMER)
    assert c.active and c.forum_id == customer.forum_id and not discord.readonly[c.forum_id]
    assert abs(c.expiry_date - (services.now() + 30 * DAY)) < 60
    assert invoke.call(CUSTOMER, "account", pub.account, control).embed.title == "Account · alice"


def test_limitless_run_is_renewed_by_the_scheduler_job(invoke, services, transport, activated, clock):
    control = activated["control"]
    assert invoke.call(CUSTOMER, "run", cust.run, control, mode="buy", rate="1.5", message="WTB", interval=5, hours=0, policy_ack=True).content.startswith("🚀")
    repo = transport.repo("worker1", "alice_alt1")
    scheduler = Scheduler(on_error=lambda n, e: None)
    register_jobs(services, scheduler)
    renewal = next(j for j in scheduler.jobs() if j.name == "renewal").job
    run(renewal())
    assert len(repo.dispatches) == 1                       # not due yet
    clock.advance(48 * 3600 + 60)
    run(renewal())
    assert len(repo.dispatches) == 2 and services.repos.runs.get(CUSTOMER, 1).renewals == 1
    # subscription lapses → orphaned limitless run is stopped instead of renewed
    run(services.customers.deactivate(CUSTOMER, reason="test", actor_id=ADMIN))
    clock.advance(48 * 3600 + 60)
    run(renewal())
    assert len(repo.dispatches) == 2 and services.repos.runs.get(CUSTOMER, 1).status == "cancelled"


def test_heartbeat_from_another_hub_never_touches_this_customer(invoke, services, transport, activated):
    control = activated["control"]
    invoke.call(CUSTOMER, "run", cust.run, control, mode="sell", rate="2", message="m", interval=5, hours=24, policy_ack=True)
    ingestor = build_ingestor(services)
    # a heartbeat claiming ALT_ID=1 but posted in an unrelated channel is ignored (no global fuzzy match)
    res = ingestor.ingest(IncomingMessage(channel_id="000000000000000000", author_name="alice", content="", embeds=[_heartbeat(1, sent=999)], is_webhook=True))
    assert res.key is None
    assert services.fleet.get((CUSTOMER, 1)).total_sent == 0
