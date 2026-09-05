"""Failure-path scenarios: bans, backup/restore on a fresh runner, worker outage, dirty-sync repair, lease conflicts."""
import json
import os

from adfarm.app import build_ingestor, build_services, rehydrate
from adfarm.commands import admin as adm
from adfarm.commands import customer as cust
from adfarm.core.clock import FakeClock
from adfarm.core.models import DAY, AltStatus, SyncState
from adfarm.github.client import GitHubClient
from adfarm.telemetry import IncomingMessage
from tests.conftest import ADMIN, ADMIN_CH, CUSTOMER, run
from tests.fakes import FakeDiscord, fake_token_checker, valid_token


def test_ban_detected_from_log_webhook_credits_customer_and_allows_resetup(invoke, services, transport, discord, activated, clock):
    control, logs = activated["control"], activated["logs"]
    invoke.call(CUSTOMER, "run", cust.run, control, mode="sell", rate="2", message="m", interval=5, hours=24, policy_ack=True)
    expiry_before = services.repos.customers.get(CUSTOMER).expiry_date
    ingestor = build_ingestor(services)
    res = ingestor.ingest(IncomingMessage(channel_id=logs, author_name="main", content="[CRITICAL] 401 Unauthorized — token invalidated (account disabled)", is_webhook=True))
    assert res.ban_detected and res.key == (CUSTOMER, 1)
    # what app.on_message does with the result:
    run(services.bans.handle(services.repos.alts.get(CUSTOMER, 1), reason="token invalidated"))
    alt = services.repos.alts.get(CUSTOMER, 1)
    assert alt.status is AltStatus.PENDING and transport.repo("worker1", "_BANNED_alice_alt1") is not None
    credited = (services.repos.customers.get(CUSTOMER).expiry_date - expiry_before) / DAY
    assert 14.9 <= credited <= 15.1            # 30 days remaining ÷ 2 alts, full credit because the ban hit within 48 h of the run
    assert services.repos.runs.get(CUSTOMER, 1) is None
    assert any("banned" in (m.embed.title if m.embed else m.content).lower() for m in discord.messages_in(control))
    assert any("ban detected" in m.content.lower() for m in discord.messages_in(ADMIN_CH))
    # customer cannot run the banned slot, but can re-setup a fresh token into it
    assert "no credentials" in invoke.call(CUSTOMER, "run", cust.run, control, mode="sell", rate="2", message="m", interval=5, hours=24).content
    ok = invoke.call(CUSTOMER, "setup", cust.setup_submit, control, alt=1, token=valid_token("N"), channels="111111111111111111")
    assert ok.content.startswith("✅ Alt 1")
    fresh = services.repos.alts.get(CUSTOMER, 1)
    assert fresh.status is AltStatus.READY and fresh.sender_alt_id == alt.sender_alt_id and transport.repo(fresh.repo_owner, fresh.repo_name) is not None
    assert invoke.call(CUSTOMER, "run", cust.run, control, mode="sell", rate="2", message="m", interval=5, hours=24).content.startswith("🚀")


def test_fresh_runner_restores_from_gist_and_keeps_serving(settings, transport, discord, activated, services, tmp_path):
    """Simulate the next cron chunk: empty disk, same Gist. The new process must restore and see the same fleet."""
    services.backup.flush()
    assert transport.gists["bkgist"]["files"]["adfarm.db.b64"]["content"]
    new_db_path = str(tmp_path / "runner2" / "adfarm.db")
    settings2 = settings.__class__(**{**settings.__dict__, "db_path": new_db_path, "run_id": "run-2"})
    gh = GitHubClient("tok-main", transport=transport, retries=1)
    s2 = build_services(settings2, FakeDiscord(), clock=FakeClock(services.now() + 60), github=gh, token_checker=fake_token_checker())
    assert s2.repos.customers.all() == []                     # brand-new disk
    source = s2.backup.restore_if_missing()
    assert source == "current"
    assert rehydrate(s2) == 1
    c = s2.repos.customers.get(CUSTOMER)
    alt = s2.repos.alts.get(CUSTOMER, 1)
    assert c and c.active and alt and alt.status is AltStatus.READY and s2.vault.open(alt.token_ciphertext) == valid_token("A")
    assert s2.customers.webhooks(CUSTOMER).complete()          # sealed webhooks survive the round trip
    assert s2.fleet.get((CUSTOMER, 1)).sender_alt_id == alt.sender_alt_id
    # lease: runner-1 still holds it → runner-2 must back off; after expiry it takes over
    assert services.backup.acquire_lease()
    assert not s2.backup.acquire_lease() and s2.backup.lease_holder == "run-1"
    services.backup.release_lease()
    assert s2.backup.acquire_lease()


def test_restore_falls_back_to_previous_snapshot_when_current_is_corrupt(services, transport, activated):
    services.backup.flush()
    files = transport.gists["bkgist"]["files"]
    good = files["adfarm.db.b64"]["content"]
    run(services.customers.set_autoreply(CUSTOMER, "v2"))
    services.backup.flush()
    assert files["adfarm.prev.db.b64"]["content"] == good
    files["adfarm.db.b64"]["content"] = "not base64 at all!!"
    assert services.backup.restore() == "previous"
    assert services.repos.customers.get(CUSTOMER).autoreply_text == ""   # previous snapshot predates v2


def test_worker_outage_fails_over_and_flags_dirty_state(invoke, services, transport, activated, discord):
    """worker2 is down: the next alt goes to worker3; a mid-flight failure leaves the alt DIRTY, and the sweep repairs it."""
    transport.down.add("worker2")
    ok = invoke.call(CUSTOMER, "setup", cust.setup_submit, activated["control"], alt=2, token=valid_token("B"), channels="333333333333333333")
    assert ok.content.startswith("✅ Alt 2")
    alt2 = services.repos.alts.get(CUSTOMER, 2)
    assert alt2.repo_owner == "worker3" and alt2.status is AltStatus.READY
    health = invoke.call(ADMIN, "admin", adm.admin, ADMIN_CH, action="health")
    workers = {f["name"]: f["value"] for f in health.embed.to_dict()["fields"]}["Workers"]
    assert "❌ `worker2`" in workers and "✅ `worker3`" in workers
    # secrets API fails once while adding a channel → DIRTY, sweep repairs
    transport.fail_next.append(("actions/secrets/public-key", 502))
    r = invoke.call(CUSTOMER, "channels", cust.channels, activated["control"], action="add", channel="444444444444444444", alt=1)
    assert r.content.startswith("⚠️") and services.repos.alts.get(CUSTOMER, 1).sync_state is SyncState.DIRTY
    assert "444444444444444444" in services.repos.alts.get(CUSTOMER, 1).channel_ids      # DB is the source of truth
    assert run(services.alts.sweep_dirty()) == 1
    alt1 = services.repos.alts.get(CUSTOMER, 1)
    assert alt1.sync_state is SyncState.CLEAN and transport.secret(alt1.repo_owner, alt1.repo_name, "CHANNEL_IDS").endswith("444444444444444444")


def test_control_gist_missing_is_reported_not_fatal(invoke, services, transport, activated):
    control = activated["control"]
    invoke.call(CUSTOMER, "run", cust.run, control, mode="sell", rate="2", message="m", interval=5, hours=24, policy_ack=True)
    del transport.gists["ctlgist"]
    r = invoke.call(CUSTOMER, "tune", cust.tune, control, price="3")
    assert r.content.startswith("⚠️") or r.content.startswith("❌")
    assert services.repos.alts.get(CUSTOMER, 1).runtime_overrides.get("rate") in (None, 3.0)   # never half-applied silently
    assert services.repos.runs.get(CUSTOMER, 1).status == "in_progress"                         # the run itself is untouched
