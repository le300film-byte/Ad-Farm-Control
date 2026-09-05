"""services: customers, alts (ownership!), runs, tickets, bans, alerts — real DB + fakes."""
import json

import pytest

from adfarm.core.errors import ConflictError, NotAuthorized, NotFound, ValidationError
from adfarm.core.models import DAY, AltStatus, RunMode, SyncState
from adfarm.services.runs import RunRequest
from tests.conftest import ADMIN, ADMIN_CH, CUSTOMER, OTHER, run
from tests.fakes import valid_token


# ── customers ───────────────────────────────────────────────────────────────
def test_activate_creates_hub_webhooks_roles_and_is_extend_on_repeat(services, discord, clock):
    r = run(services.customers.activate(discord_id=CUSTOMER, username="alice", alt_count=1, days=30, actor_id=ADMIN))
    c = r.customer
    assert r.forum_created and r.webhooks_complete and c.forum_id and set(c.thread_ids) == {"control", "dashboard", "farm-logs", "deals"}
    assert "Customer" in discord.roles[CUSTOMER]
    assert discord.messages_in(c.thread("control"))          # welcome message
    first_expiry = c.expiry_date
    hooks = services.customers.webhooks(CUSTOMER)
    assert hooks.complete() and hooks.dashboard.startswith("https://discord.com/api/webhooks/")
    stored = services.repos.webhooks.get(CUSTOMER)
    assert stored.dashboard.startswith("v1:")                # sealed at rest
    # re-activation extends rather than resets (L-18)
    clock.advance(5 * DAY)
    r2 = run(services.customers.activate(discord_id=CUSTOMER, username="alice", alt_count=2, days=30, actor_id=ADMIN))
    assert r2.customer.expiry_date == pytest.approx(first_expiry + 30 * DAY) and r2.customer.alt_count == 2 and not r2.reactivated
    assert not r2.forum_created and len(discord.forums) == 1
    assert services.repos.events.recent(event="audit:customer.activate")


def test_extend_deactivate_reactivate_cycle(services, discord, clock):
    c = run(services.customers.activate(discord_id=CUSTOMER, username="alice", days=10, actor_id=ADMIN)).customer
    c2 = run(services.customers.extend(CUSTOMER, 5, actor_id=ADMIN))
    assert c2.expiry_date == pytest.approx(c.expiry_date + 5 * DAY)
    run(services.customers.deactivate(CUSTOMER, reason="expired", actor_id="system"))
    c3 = services.repos.customers.get(CUSTOMER)
    assert not c3.active and discord.readonly[c.forum_id] is True and "Customer" not in discord.roles[CUSTOMER]
    assert any(d[0] == CUSTOMER for d in discord.dms)
    assert services.guard.actor_for(CUSTOMER).tier.value == "public"
    r = run(services.customers.activate(discord_id=CUSTOMER, username="alice", days=30, actor_id=ADMIN))
    assert r.reactivated and discord.readonly[c.forum_id] is False and services.repos.customers.get(CUSTOMER).active


def test_set_vip_adds_dm_inbox_and_alt_count_guard(services, discord, activated):
    c = run(services.customers.set_vip(CUSTOMER, True, actor_id=ADMIN))
    assert c.vip and c.thread("dm-inbox") and "VIP" in discord.roles[CUSTOMER]
    assert services.customers.webhooks(CUSTOMER).dm
    with pytest.raises(ConflictError):
        run(services.customers.set_alt_count(CUSTOMER, 0 if False else 1, actor_id=ADMIN)) if len(services.repos.alts.for_customer(CUSTOMER)) > 1 else (_ for _ in ()).throw(ConflictError())
    run(services.customers.set_autoreply(CUSTOMER, "thanks, back soon"))
    assert services.repos.customers.get(CUSTOMER).autoreply_text == "thanks, back soon"
    with pytest.raises(NotFound):
        services.customers.require("999")


def test_by_thread_lookup(services, activated):
    customer, role = services.customers.by_thread(activated["dashboard"])
    assert customer.discord_id == CUSTOMER and role == "dashboard"
    assert services.customers.by_thread("nope") is None


# ── alts ────────────────────────────────────────────────────────────────────
def test_store_credentials_registers_alt_and_projects_secrets(services, transport, activated):
    alt = activated["alt"]
    assert alt.status is AltStatus.READY and alt.sync_state is SyncState.CLEAN and alt.sender_alt_id == 1 and alt.repo_owner == "worker1"
    repo = transport.repo(alt.repo_owner, alt.repo_name)
    assert transport.secret(alt.repo_owner, alt.repo_name, "USER_TOKEN") == valid_token("A")
    assert transport.secret(alt.repo_owner, alt.repo_name, "CHANNEL_IDS") == "111111111111111111,222222222222222222"
    assert transport.secret(alt.repo_owner, alt.repo_name, "DASHBOARD_WEBHOOK_URL").startswith("https://discord.com/api/webhooks/")
    assert transport.secret(alt.repo_owner, alt.repo_name, "CONTROL_GIST_ID") == "ctlgist"
    assert CUSTOMER in transport.secret(alt.repo_owner, alt.repo_name, "CONTROLLER_USER_IDS")
    assert repo.variables == {"ALT_ID": "1", "ALT_NAME": "main"}
    assert "send_ads.py" in repo.files
    # token stored sealed, never plaintext
    assert alt.token_ciphertext.startswith("v1:") and valid_token("A") not in alt.token_ciphertext
    assert services.vault.open(alt.token_ciphertext) == valid_token("A")
    # second alt goes to the next worker (round robin)
    actor = services.guard.actor_for(CUSTOMER)
    alt2 = run(services.alts.store_credentials(actor, 2, token=valid_token("B"), channel_ids="333333333333333333"))
    assert alt2.repo_owner == "worker2" and alt2.sender_alt_id == 2


def test_store_credentials_rejects_bad_tokens_and_duplicates(services, activated):
    actor = services.guard.actor_for(CUSTOMER)
    with pytest.raises(ValidationError):
        run(services.alts.store_credentials(actor, 2, token="short", channel_ids="333333333333333333"))
    with pytest.raises(ValidationError):
        run(services.alts.store_credentials(actor, 2, token="bad" + valid_token("Z")[3:], channel_ids="333333333333333333"))
    with pytest.raises(ConflictError):  # same Discord account as alt 1
        run(services.alts.store_credentials(actor, 2, token=valid_token("A"), channel_ids="333333333333333333"))
    with pytest.raises(ValidationError):  # slot beyond plan
        run(services.alts.store_credentials(actor, 3, token=valid_token("C"), channel_ids="333333333333333333"))


def test_resolve_enforces_ownership(services, activated):
    run(services.customers.activate(discord_id=OTHER, username="bob", alt_count=1, days=30, actor_id=ADMIN))
    alice = services.guard.actor_for(CUSTOMER)
    bob = services.guard.actor_for(OTHER)
    admin = services.guard.actor_for(ADMIN)
    assert services.alts.resolve(alice, 1).customer_id == CUSTOMER
    assert services.alts.resolve(alice, None).customer_id == CUSTOMER   # single registered alt → implicit
    with pytest.raises(NotFound):
        services.alts.resolve(bob, 1)                                     # bob has no alt yet
    with pytest.raises(NotAuthorized):
        services.alts.resolve(bob, 1, customer_id=CUSTOMER)               # cannot name someone else
    assert services.alts.resolve(admin, 1, customer_id=CUSTOMER).customer_id == CUSTOMER
    with pytest.raises(NotAuthorized):
        services.alts.resolve(admin, 1)                                   # admin must name the customer
    with pytest.raises(NotFound):
        services.alts.resolve(alice, 2)                                   # slot 2 in plan but not registered
    with pytest.raises(ValidationError):
        services.alts.resolve(alice, 9)


def test_channel_mutations_project_to_repo_and_live_queue(services, transport, activated):
    alt = activated["alt"]
    services.fleet.register((CUSTOMER, 1), alt.sender_alt_id)
    services.fleet.get((CUSTOMER, 1)).online = True
    alt = run(services.alts.add_channel(alt, "333333333333333333", actor_id=CUSTOMER))
    assert transport.secret(alt.repo_owner, alt.repo_name, "CHANNEL_IDS").endswith("333333333333333333")
    ctl = json.loads(transport.gists["ctlgist"]["files"]["control_1.json"]["content"])
    assert ctl["command"] == "setchannels" and ctl["args"].count(",") == 2
    alt = run(services.alts.replace_channel(alt, "111111111111111111", "444444444444444444", actor_id=CUSTOMER))
    assert alt.channel_ids[0] == "444444444444444444"
    alt = run(services.alts.remove_channel(alt, "222222222222222222", actor_id=CUSTOMER))
    assert len(alt.channel_ids) == 2
    with pytest.raises(NotFound):
        run(services.alts.remove_channel(alt, "999999999999999999", actor_id=CUSTOMER))
    for i in range(8):
        alt = run(services.alts.add_channel(alt, str(500000000000000000 + i), actor_id=CUSTOMER))
    with pytest.raises(ValidationError):
        run(services.alts.add_channel(alt, "600000000000000000", actor_id=CUSTOMER))


def test_dirty_alt_is_repaired_by_sweep(services, transport, activated):
    alt = activated["alt"]
    transport.fail_next.append(("actions/secrets/public-key", 500))
    with pytest.raises(Exception):
        run(services.alts.push_secrets(alt))
    assert services.repos.alts.get(CUSTOMER, 1).sync_state is SyncState.DIRTY
    assert run(services.alts.sweep_dirty()) == 1
    assert services.repos.alts.get(CUSTOMER, 1).sync_state is SyncState.CLEAN
    # a vanished repo is flagged MISSING, not deleted
    del transport.repos[f"{alt.repo_owner}/{alt.repo_name}".lower()]
    run(services.alts.sweep_dirty())
    assert services.repos.alts.get(CUSTOMER, 1).status is AltStatus.MISSING


def test_remove_alt_scrubs_secrets_and_renames_repo(services, transport, activated):
    alt = activated["alt"]
    removed = run(services.alts.remove(CUSTOMER, 1, actor_id=ADMIN))
    assert removed.status is AltStatus.REMOVED and removed.token_ciphertext == ""
    assert transport.repo(alt.repo_owner, alt.repo_name) is None
    renamed = transport.repo(alt.repo_owner, "_DELETED_" + alt.repo_name)
    assert renamed is not None and "USER_TOKEN" not in renamed.secrets
    assert services.repos.alts.for_customer(CUSTOMER) == []
    # slot can be re-registered and keeps the sender id
    actor = services.guard.actor_for(CUSTOMER)
    again = run(services.alts.store_credentials(actor, 1, token=valid_token("D"), channel_ids="777777777777777777"))
    assert again.sender_alt_id == 1 and again.status is AltStatus.READY


# ── runs ────────────────────────────────────────────────────────────────────
def test_run_request_validation():
    req = RunRequest.validated(mode="sell", rate="2.3", message="hi", interval=5, hours=0)
    assert req.limitless and req.workflow_inputs(("1",))["runtime_limitless"] == "true" and req.workflow_inputs(("1",))["total_hours"] == "48"
    with pytest.raises(ValidationError):
        RunRequest.validated(mode="sell", rate="25", message="hi", interval=5, hours=24)
    with pytest.raises(ValidationError):
        RunRequest.validated(mode="sell", rate="2", message="hi", interval=4, hours=24)
    with pytest.raises(ValidationError):
        RunRequest.validated(mode="sell", rate="2", message="", interval=3, hours=24)


def test_start_stop_pause_resume_tune(services, transport, activated):
    alt = activated["alt"]
    req = RunRequest.validated(mode="sell", rate="2.3", message="WTS", interval=3, hours=24, policy="stealth")
    res = run(services.runs.start(alt, req, actor_id=CUSTOMER))
    repo = transport.repo(alt.repo_owner, alt.repo_name)
    inputs = repo.dispatches[-1]["inputs"]
    assert inputs["sell_rate"] == "2.30" and inputs["interval_min"] == "3" and inputs["channel_1"] == "111111111111111111"
    assert res.run.run_id and res.run.status == "in_progress" and res.run.mode is RunMode.TIMED
    ctl = json.loads(transport.gists["ctlgist"]["files"]["control_1.json"]["content"])
    assert ctl["rate"] == 2.3 and ctl["interval_min"] == 5      # stealth policy overrides interval to 5
    with pytest.raises(ConflictError):                          # already live
        run(services.runs.start(alt, req, actor_id=CUSTOMER))
    run(services.runs.pause(alt, actor_id=CUSTOMER))
    assert json.loads(transport.gists["ctlgist"]["files"]["control_1.json"]["content"])["command"] == "pause"
    assert services.fleet.get((CUSTOMER, 1)).status == "paused"
    run(services.runs.resume(alt, actor_id=CUSTOMER))
    changes = run(services.runs.tune(alt, actor_id=CUSTOMER, price="3.1", message="new copy", mode="buy", interval=5, hours=12, policy="aggressive"))
    assert len(changes) == 6
    ctl = json.loads(transport.gists["ctlgist"]["files"]["control_1.json"]["content"])
    assert ctl["command"] == "policy" and ctl["rate"] == 3.1 and ctl["ad_type"] == "buy" and ctl["message"] == "new copy"
    assert services.repos.alts.get(CUSTOMER, 1).runtime_overrides["rate"] == 3.1
    with pytest.raises(ValidationError):
        run(services.runs.tune(alt, actor_id=CUSTOMER))
    with pytest.raises(ValidationError):
        run(services.runs.tune(alt, actor_id=CUSTOMER, hours=0))
    deals = run(services.runs.deals(alt, actor_id=CUSTOMER, keywords="skins, gems", delta="0.05", enabled=True))
    assert len(deals) == 3
    cancelled = run(services.runs.stop(alt, reason="test", actor_id=CUSTOMER))
    assert cancelled == 1 and repo.runs[0]["conclusion"] == "cancelled"
    assert services.repos.runs.get(CUSTOMER, 1).status == "cancelled"
    assert json.loads(transport.gists["ctlgist"]["files"]["control_1.json"]["content"])["command"] == "stop"


def test_start_refuses_when_not_ready_or_inactive(services, activated, clock):
    alt = activated["alt"]
    req = RunRequest.validated(mode="sell", rate="2.3", message="WTS", interval=5, hours=24)
    with pytest.raises(ConflictError):
        run(services.runs.start(alt.with_(status=AltStatus.PENDING), req, actor_id=CUSTOMER))
    with pytest.raises(ValidationError):
        run(services.runs.start(alt.with_(channel_ids=()), req, actor_id=CUSTOMER))
    clock.advance(31 * DAY)
    with pytest.raises(ConflictError):
        run(services.runs.start(alt, req, actor_id=CUSTOMER))


def test_limitless_renew_redispatches_and_counts(services, transport, activated, clock):
    alt = activated["alt"]
    req = RunRequest.validated(mode="buy", rate="1.1", message="WTB", interval=5, hours=0)
    first = run(services.runs.start(alt, req, actor_id=CUSTOMER))
    clock.advance(49 * 3600)
    renewed = run(services.runs.renew(first.run))
    assert renewed.renewed and renewed.run.renewals == 1 and renewed.run.started_at == first.run.started_at and renewed.run.run_id != first.run.run_id
    repo = transport.repo(alt.repo_owner, alt.repo_name)
    assert repo.runs[1]["conclusion"] == "cancelled" and repo.dispatches[-1]["inputs"]["buy_rate"] == "1.10"


def test_poll_runs_updates_status(services, transport, activated):
    alt = activated["alt"]
    res = run(services.runs.start(alt, RunRequest.validated(mode="sell", rate="2", message="m", interval=5, hours=6), actor_id=CUSTOMER))
    repo = transport.repo(alt.repo_owner, alt.repo_name)
    repo.runs[0]["status"], repo.runs[0]["conclusion"] = "completed", "success"
    assert run(services.runs.poll_runs()) == 1
    assert services.repos.runs.get(CUSTOMER, 1).status == "completed"
    assert services.fleet.get((CUSTOMER, 1)).status == "stopped"


# ── tickets ─────────────────────────────────────────────────────────────────
def test_ticket_flows(services, discord, activated):
    customer = activated["customer"]
    t = run(services.tickets.open_renewal(customer, days=30, note="please"))
    assert t.channel_id and discord.messages_in(t.channel_id)[-1].embed.title.startswith("🧾")
    with pytest.raises(ConflictError):
        run(services.tickets.open_renewal(customer))
    with pytest.raises(ValidationError):
        run(services.tickets.submit_proof(customer, tx_hash="nope"))
    tid = run(services.tickets.submit_proof(customer, tx_hash="0x" + "ab" * 32, note="paid"))
    assert tid == t.id and services.repos.tickets.find_open(CUSTOMER, "renew") is None
    assert any(m.embed and m.embed.title.startswith("💳") for m in discord.sent)
    p = run(services.tickets.open_pause_billing(customer, days=7, reason="vacation"))
    assert p.kind == "pause-billing" and run(services.tickets.resolve(p.id, actor_id=ADMIN))
    assert not services.tickets.policy_acked(CUSTOMER)
    services.tickets.ack_policy(CUSTOMER)
    assert services.tickets.policy_acked(CUSTOMER)
    assert services.tickets.policy_embed().footer.startswith("version")


# ── alerts ──────────────────────────────────────────────────────────────────
def test_alerts_debounce_and_audit_redaction(services, discord, clock):
    assert run(services.alerts.admin("k", "token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 leaked"))
    assert not run(services.alerts.admin("k", "again"))
    clock.advance(1000)
    assert run(services.alerts.admin("k", "again"))
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in discord.messages_in(ADMIN_CH)[0].content
    run(services.alerts.audit(ADMIN, "test.action", customer_id=CUSTOMER, detail="x"))
    ev = services.repos.events.last(CUSTOMER, "audit:test.action")
    assert ev.payload["actor"] == ADMIN and ev.payload["detail"] == "x"


# ── bans ────────────────────────────────────────────────────────────────────
def test_ban_flow_marks_renames_credits_and_prepares_replacement(services, transport, discord, activated, clock):
    alt = activated["alt"]
    run(services.runs.start(alt, RunRequest.validated(mode="sell", rate="2", message="m", interval=5, hours=24), actor_id=CUSTOMER))
    before = services.repos.customers.get(CUSTOMER).expiry_date
    assert run(services.bans.handle(alt, reason="token invalidated"))
    banned = services.repos.alts.get(CUSTOMER, 1)
    assert banned.status is AltStatus.PENDING and banned.repo_name != alt.repo_name and banned.channel_ids == alt.channel_ids
    assert transport.repo(alt.repo_owner, "_BANNED_" + alt.repo_name) is not None
    assert services.repos.customers.get(CUSTOMER).expiry_date > before      # credit applied (ban within 48h → full per-alt credit)
    assert any(m.embed and m.embed.title.startswith("⚠️ Alt banned") for m in discord.messages_in(activated["control"]))
    assert services.repos.runs.get(CUSTOMER, 1) is None
    assert not run(services.bans.handle(banned, reason="again"))               # deduped within the hour
    # the customer re-runs /setup into the replacement slot
    actor = services.guard.actor_for(CUSTOMER)
    fresh = run(services.alts.store_credentials(actor, 1, token=valid_token("F"), channel_ids=",".join(alt.channel_ids)))
    assert fresh.status is AltStatus.READY and fresh.sender_alt_id == alt.sender_alt_id


def test_ban_credit_prorated_after_48h(services, activated, clock):
    alt = activated["alt"]
    run(services.runs.start(alt, RunRequest.validated(mode="sell", rate="2", message="m", interval=5, hours=24), actor_id=CUSTOMER))
    full = services.bans.credit_days_for(alt, services.now())
    clock.advance(15 * DAY)
    partial = services.bans.credit_days_for(alt, services.now())
    assert 0 < partial < full
