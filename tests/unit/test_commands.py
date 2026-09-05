"""commands: every handler through the same gate the registry uses (FakeContext), no discord.py."""
import json

from adfarm.commands import admin as adm
from adfarm.commands import customer as cust
from adfarm.commands import public as pub
from adfarm.commands import vip as vipc
from adfarm.security import policy
from tests.conftest import ADMIN, ADMIN2, ADMIN_CH, CUSTOMER, OTHER, PUBLIC_CH, STRANGER, TICKET_CH, run
from tests.fakes import valid_token


# ── gate behaviour through real channels ────────────────────────────────────
def test_public_commands_everywhere_and_help_scales_with_tier(invoke, activated):
    assert invoke.call(STRANGER, "help", pub.help_, PUBLIC_CH).embed.description.endswith("**public**")
    assert invoke.call(CUSTOMER, "help", pub.help_, activated["control"]).embed.description.endswith("**customer**")
    r = invoke.call(ADMIN, "getstarted", pub.getstarted, "some-dm")
    assert r.embed.title.startswith("🚀")


def test_customer_commands_denied_in_public_other_hub_and_for_strangers(invoke, activated, services, discord):
    assert invoke.call(CUSTOMER, "run", cust.run, PUBLIC_CH).content == policy.DENY_PUBLIC_ROOM
    assert invoke.call(STRANGER, "run", cust.run, activated["control"]).content == policy.DENY_NOT_CUSTOMER
    run(services.customers.activate(discord_id=OTHER, username="bob", days=30, actor_id=ADMIN))
    bob_control = services.repos.customers.get(OTHER).thread("control")
    assert invoke.call(CUSTOMER, "status", cust.status, bob_control).content == policy.DENY_OTHER_HUB
    assert invoke.call(CUSTOMER, "run", cust.run, "unknown-dm").content == policy.DENY_DM
    assert invoke.call(CUSTOMER, "run", cust.run, TICKET_CH).content == policy.DENY_UNKNOWN_ROOM
    assert "Renewal ticket" in invoke.call(CUSTOMER, "renew", cust.renew, TICKET_CH, days=30).content


def test_admin_commands_only_in_admin_rooms(invoke, activated):
    assert invoke.call(ADMIN, "admin", adm.admin, activated["control"], action="list").content == policy.DENY_ADMIN_ROOM
    assert invoke.call(CUSTOMER, "admin", adm.admin, ADMIN_CH, action="list").content == policy.DENY_ADMIN
    assert invoke.call(ADMIN, "admin", adm.admin, ADMIN_CH, action="list").embed.title.startswith("Customers")


def test_shutdown_is_admin_only_and_multisig(invoke, services):
    r1 = invoke.call(ADMIN, "admin", adm.admin, ADMIN_CH, action="shutdown-bot", confirm="SHUTDOWN")
    assert "armed (1/2)" in r1.content and not services.shutdown_requested
    r2 = invoke.call(ADMIN, "admin", adm.admin, ADMIN_CH, action="shutdown-bot", confirm="SHUTDOWN")
    assert "armed (1/2)" in r2.content                       # same admin twice does not count
    r3 = invoke.call(ADMIN2, "admin", adm.admin, ADMIN_CH, action="shutdown-bot", confirm="SHUTDOWN")
    assert "Shutdown confirmed" in r3.content and services.shutdown_requested
    bad = invoke.call(ADMIN, "admin", adm.admin, ADMIN_CH, action="shutdown-bot", confirm="yes")
    assert bad.content.startswith("❌ Type `SHUTDOWN`")


# ── customer flow ───────────────────────────────────────────────────────────
def test_account_setup_run_status_flow(invoke, services, transport, discord):
    run(services.customers.activate(discord_id=CUSTOMER, username="alice", alt_count=1, days=30, actor_id=ADMIN))
    control = services.repos.customers.get(CUSTOMER).thread("control")
    acct = invoke.call(CUSTOMER, "account", pub.account, control)
    assert "none registered" in acct.embed.to_dict()["fields"][3]["value"]
    modal = invoke.call(CUSTOMER, "setup", cust.setup, control, alt=1)
    assert modal.modal["kind"] == "setup"
    done = invoke.call(CUSTOMER, "setup", cust.setup_submit, control, alt=1, token=valid_token("Q"), channels="111111111111111111", display_name="main")
    assert done.content.startswith("✅ Alt 1") and "never in chat" in done.content
    # first /run asks for the policy acknowledgement
    ask = invoke.call(CUSTOMER, "run", cust.run, control, mode="sell", rate="2.3", message="WTS", interval=5, hours=24)
    assert ask.content == "policy:ack-required" and ask.view == {"kind": "policy_ack"}
    ok = invoke.call(CUSTOMER, "run", cust.run, control, mode="sell", rate="2.3", message="WTS", interval=5, hours=24, policy_ack=True)
    assert ok.content.startswith("🚀 Alt 1") and services.tickets.policy_acked(CUSTOMER)
    assert transport.repo("worker1", "alice_alt1").dispatches
    st = invoke.call(CUSTOMER, "status", cust.status, control)
    assert st.embed.title.endswith("main") and st.ephemeral
    posted = invoke.call(CUSTOMER, "status", cust.status, control, post=True, refresh=True)
    assert "posted" in posted.content and discord.messages_in(services.repos.customers.get(CUSTOMER).thread("dashboard"))
    bad = invoke.call(CUSTOMER, "run", cust.run, control, mode="sell", rate="99", message="WTS", interval=5, hours=24)
    assert bad.content.startswith("❌ Price")


def test_stop_pause_resume_tune_deals_reply_channels_alt(invoke, services, transport, activated):
    control = activated["control"]
    assert invoke.call(CUSTOMER, "run", cust.run, control, mode="sell", rate="2", message="m", interval=5, hours=24, policy_ack=True).content.startswith("🚀")
    assert invoke.call(CUSTOMER, "pause", cust.pause, control).content.startswith("⏸️")
    assert invoke.call(CUSTOMER, "resume", cust.resume, control).content.startswith("▶️")
    assert "price → `$3.00/1k`" in invoke.call(CUSTOMER, "tune", cust.tune, control, price="3").content
    assert invoke.call(CUSTOMER, "tune", cust.tune, control).content.startswith("❌ Provide")
    assert "keywords" in invoke.call(CUSTOMER, "deals", cust.deals, control, keywords="skins").content
    assert invoke.call(CUSTOMER, "reply", cust.reply, control, user="123456789012345678", text="hi").content.startswith("📩")
    assert invoke.call(CUSTOMER, "reply", cust.reply, control, user="abc", text="hi").content.startswith("❌ User ID")
    view = invoke.call(CUSTOMER, "channels", cust.channels, control, action="view")
    assert "2/10" in view.content
    assert "3 channel" in invoke.call(CUSTOMER, "channels", cust.channels, control, action="add", channel="333333333333333333").content
    assert invoke.call(CUSTOMER, "channels", cust.channels, control, action="replace", channel="333333333333333333", new_channel="444444444444444444").content.startswith("✅")
    assert invoke.call(CUSTOMER, "channels", cust.channels, control, action="remove", channel="444444444444444444").content.startswith("✅")
    assert invoke.call(CUSTOMER, "channels", cust.channels, control, action="overwrite", channels="555555555555555555").content.startswith("✅")
    assert invoke.call(CUSTOMER, "channels", cust.channels, control, action="rescan").content.startswith("🔎")
    assert invoke.call(CUSTOMER, "channels", cust.channels, control, action="bogus").content.startswith("❌ Action")
    over = invoke.call(CUSTOMER, "alt", cust.alt, control, action="overview")
    fields = [f["name"] for f in over.embed.to_dict()["fields"]]
    # P1-4: repo slug / sender ALT_ID are operator internals, hidden from customers
    assert "Sender ALT_ID" not in fields and "Repo" not in fields
    admin_over = invoke.call(ADMIN, "alt", cust.alt, control, action="overview", alt=1, customer=CUSTOMER)
    assert "Sender ALT_ID" in [f["name"] for f in admin_over.embed.to_dict()["fields"]]
    assert invoke.call(CUSTOMER, "alt", cust.alt, control, action="logs").content.startswith("📜")
    assert invoke.call(CUSTOMER, "alt", cust.alt, control, action="runs").content.startswith("🏃")
    assert invoke.call(CUSTOMER, "alt", cust.alt, control, action="selfcheck").content.startswith("🩺")
    assert invoke.call(CUSTOMER, "alt", cust.alt, control, action="remove").content.startswith("❌ Type `REMOVE`")
    assert invoke.call(CUSTOMER, "alt", cust.alt, control, action="remove", confirm="REMOVE").content.startswith("🗑️")
    assert invoke.call(CUSTOMER, "stop", cust.stop, control).content == "❓ No alt registered yet. Run `/setup` first."


def test_customer_cannot_target_another_customers_alt(invoke, services, activated):
    run(services.customers.activate(discord_id=OTHER, username="bob", days=30, actor_id=ADMIN))
    bob_control = services.repos.customers.get(OTHER).thread("control")
    r = invoke.call(OTHER, "stop", cust.stop, bob_control, alt=1, customer=CUSTOMER)
    assert r.content == policy.DENY_ADMIN                      # NotAuthorized → generic denial
    assert invoke.call(OTHER, "status", cust.status, bob_control).content.startswith("ℹ️ No alt")


def test_billing_commands(invoke, services, activated):
    control = activated["control"]
    assert "Renewal ticket #" in invoke.call(CUSTOMER, "renew", cust.renew, control, days=30).content
    assert invoke.call(CUSTOMER, "renew", cust.renew, control, days=30).content.startswith("⚠️ You already")
    assert invoke.call(CUSTOMER, "proofs", cust.proofs, control, tx_hash="ab" * 32).content.startswith("💳")
    assert invoke.call(CUSTOMER, "pause-billing", cust.pause_billing, control, days=5, reason="trip").content.startswith("⏸️")


def test_vip_commands(invoke, services, activated):
    control = activated["control"]
    assert invoke.call(CUSTOMER, "vip", vipc.vip, control, action="autoreply", text="hi").content == policy.DENY_VIP
    run(services.customers.set_vip(CUSTOMER, True, actor_id=ADMIN))
    assert invoke.call(CUSTOMER, "vip", vipc.vip, control, action="autoreply", text="hi @everyone").content.startswith("🤖 DM auto-reply saved")
    assert "(mention:everyone)" in invoke.call(CUSTOMER, "vip", vipc.vip, control, action="autoreply").content
    assert invoke.call(CUSTOMER, "vip", vipc.vip, control, action="autoreply", text="off").content.endswith("disabled.")
    assert invoke.call(CUSTOMER, "vip", vipc.vip, control, action="squad", name="a", alts="1").content.startswith("👥 Squad **a**")
    assert invoke.call(CUSTOMER, "vip", vipc.vip, control, action="squad", name="b", alts="1,2").content.startswith("❌ You can only")
    assert "**a**" in invoke.call(CUSTOMER, "vip", vipc.vip, control, action="squad").content


# ── admin flow ──────────────────────────────────────────────────────────────
def test_admin_lifecycle_actions(invoke, services, transport, discord):
    act = invoke.call(ADMIN, "admin", adm.admin, ADMIN_CH, action="activate", user=CUSTOMER, days=30, alts=2, username="alice")
    assert act.content.startswith("✅ Activated") and "webhooks ✅" in act.content
    card = invoke.call(ADMIN, "admin", adm.admin, ADMIN_CH, action="customer", user=CUSTOMER)
    assert card.embed.title.startswith("Customer · alice")
    add = invoke.call(ADMIN, "admin", adm.admin, ADMIN_CH, action="alt", sub="add", user=CUSTOMER)
    assert "Alt 1 registered at `worker1/alice_alt1`" in add.content
    assert transport.repo("worker1", "alice_alt1") is not None
    lst = invoke.call(ADMIN, "admin", adm.admin, ADMIN_CH, action="alt", sub="list", user=CUSTOMER)
    assert "pending" in lst.content
    ext = invoke.call(ADMIN, "admin", adm.admin, ADMIN_CH, action="extend", user=CUSTOMER, days=10)
    assert ext.content.startswith("✅") and "extended" in ext.content
    assert invoke.call(ADMIN, "admin", adm.admin, ADMIN_CH, action="vip", user=CUSTOMER, enabled=True).content.startswith("⭐")
    health = invoke.call(ADMIN, "admin", adm.admin, ADMIN_CH, action="health")
    fields = {f["name"]: f["value"] for f in health.embed.to_dict()["fields"]}
    assert "✅ `worker1`" in fields["Workers"] and "enabled" in fields["Backup"]
    assert invoke.call(ADMIN, "admin", adm.admin, ADMIN_CH, action="backup", sub="now").content.startswith("💾 Backup uploaded")
    assert invoke.call(ADMIN, "admin", adm.admin, ADMIN_CH, action="repo", sub="list").content.startswith("📦")
    assert invoke.call(ADMIN, "admin", adm.admin, ADMIN_CH, action="repo", sub="sync").content.startswith("🔁")
    assert invoke.call(ADMIN, "admin", adm.admin, ADMIN_CH, action="alt", sub="sync", user=CUSTOMER).content.startswith("🔄")
    panel = invoke.call(ADMIN, "admin", adm.admin, ADMIN_CH, action="ticket-panel", channel=TICKET_CH)
    assert panel.content.startswith("📌")
    assert services.tickets.ticket_channel_id == TICKET_CH
    # P1-7: the handler hands the actual posting to the registry, which is the only module
    # allowed to import discord.py and therefore the only one that can attach a real view.
    # The send+pin itself is asserted in test_v9_fixes.py (this module stays discord.py-free).
    assert panel.view["kind"] == "post_ticket_panel" and panel.view["channel"] == TICKET_CH
    assert panel.view["embed"].description.count("Open Ticket") == 1
    assert invoke.call(ADMIN, "admin", adm.admin, ADMIN_CH, action="tickets").content.startswith("🎫")
    assert invoke.call(ADMIN, "admin", adm.admin, ADMIN_CH, action="logs").content.startswith("📜")
    assert invoke.call(ADMIN, "admin", adm.admin, ADMIN_CH, action="payment-address").content.startswith("💳")
    assert invoke.call(ADMIN, "admin", adm.admin, ADMIN_CH, action="deactivate", user=CUSTOMER).content.startswith("❌ Type `DEACTIVATE`")
    assert invoke.call(ADMIN, "admin", adm.admin, ADMIN_CH, action="deactivate", user=CUSTOMER, confirm="DEACTIVATE").content.startswith("⛔")
    assert invoke.call(ADMIN, "admin", adm.admin, ADMIN_CH, action="alt", sub="remove", user=CUSTOMER, alt=1, confirm="REMOVE").content.startswith("🗑️")
    assert invoke.call(ADMIN, "admin", adm.admin, ADMIN_CH, action="nope").content.startswith("❌ Unknown admin action")
    assert invoke.call(ADMIN, "admin", adm.admin, ADMIN_CH, action="customer", user="abc").content.startswith("❌ User ID")


def test_admin_can_operate_customer_alt_by_naming_customer(invoke, activated):
    r = invoke.call(ADMIN, "stop", cust.stop, ADMIN_CH, alt=1, customer=CUSTOMER)
    assert r.content.startswith("🛑 Stop sent to alt 1")
    fleet = invoke.call(ADMIN, "status", cust.status, ADMIN_CH, fleet=True)
    assert fleet.embed.title == "Fleet overview" and "alice" in fleet.embed.description


def test_admin_reset_requires_two_admins(invoke, services, activated):
    assert "armed (1/2)" in invoke.call(ADMIN, "admin", adm.admin, ADMIN_CH, action="reset", confirm="RESET").content
    assert services.repos.alts.for_customer(CUSTOMER)
    done = invoke.call(ADMIN2, "admin", adm.admin, ADMIN_CH, action="reset", confirm="RESET")
    assert done.content.startswith("🧨") and services.repos.alts.for_customer(CUSTOMER) == []
    assert services.repos.customers.get(CUSTOMER) is not None
