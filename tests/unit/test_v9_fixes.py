"""Regression tests for the V9 critical-fix list in TODO.md (F01–F09 + P1/P2).

Each test is named after the TODO item it closes, so the checklist at the bottom of TODO.md
can be re-verified with a single command:

    pytest tests/unit/test_v9_fixes.py -v
"""
from __future__ import annotations

import ast
import base64
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from adfarm.commands import admin as admin_cmds
from adfarm.commands import customer as cust
from adfarm.commands import public as pub
from adfarm.commands.registry import MODAL_TITLE_LIMIT, SetupModal, TicketModal, TicketPanelView, _view_for, modal_title
from adfarm.core.models import RunMode
from adfarm.core.models import DAY
from adfarm.core.rules import POLICY_VERSION
from adfarm.discord.permissions import (ADMIN_ALLOW, CUSTOMER_ALLOW, PUBLIC_ALLOW, VIEW, forum_overwrites, hub_overwrites,
                                       public_overwrites, staff_overwrites)
from adfarm.discord.policy import POLICY_ACCEPT_LABEL, POLICY_TEXT, POLICY_TITLE
from adfarm.github.repos import AD_IMAGE_PATH
from adfarm.github.workflows import ATTACH_IMAGE_VALUES, RUNTIME_LIMITLESS_VALUES, WORKFLOW_INPUTS, build_inputs
from adfarm.security.policy import ADMIN_ONLY_COMMANDS, COMMAND_TIERS, EXPIRED_ALLOWED_COMMANDS, ChannelKind, Tier, decide
from adfarm.services.runs import merged_renewal_payload
from tests.conftest import ADMIN, CUSTOMER, OTHER, PUBLIC_CH, TICKET_CH, run

ROOT = Path(__file__).resolve().parents[2]
SENDER = ROOT / "sender" / "send_ads.py"
WORKFLOW = ROOT / "sender" / "workflows" / "send_ads.yml"


# ═════════════════════════════════════════════════════════════════════════════
# F01 — webhook URLs
# ═════════════════════════════════════════════════════════════════════════════
def _sender_helpers():
    """Load ``_webhook_base`` / ``_webhook_execute`` out of send_ads.py without importing it.

    The sender needs curl_cffi / Pillow / websocket-client, none of which the control-bot test
    environment installs — so the two URL helpers are lifted out by AST and exec'd in isolation.
    """
    tree = ast.parse(SENDER.read_text(encoding="utf-8"))
    wanted = {"_webhook_base", "_webhook_execute"}
    src = "\n".join(ast.get_source_segment(SENDER.read_text(encoding="utf-8"), n) for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name in wanted)
    assert src, "sender webhook URL helpers are missing (F01)"
    ns: dict = {}
    exec(compile(src, str(SENDER), "exec"), ns)
    return ns["_webhook_base"], ns["_webhook_execute"]


def test_f01_sender_joins_wait_true_with_ampersand_for_forum_urls():
    _base, execute = _sender_helpers()
    forum_url = "https://discord.com/api/webhooks/111/tok?thread_id=456"
    assert execute(forum_url) == forum_url + "&wait=true"          # not a second "?"
    assert execute("https://discord.com/api/webhooks/111/tok") == "https://discord.com/api/webhooks/111/tok?wait=true"
    assert execute("") == ""


def test_f01_sender_strips_the_query_before_the_messages_endpoint():
    base, _execute = _sender_helpers()
    assert base("https://discord.com/api/webhooks/111/tok?thread_id=456") == "https://discord.com/api/webhooks/111/tok"
    edit = f"{base('https://discord.com/api/webhooks/111/tok?thread_id=456')}/messages/777"
    assert edit == "https://discord.com/api/webhooks/111/tok/messages/777"
    assert "?" not in edit


def test_f01_no_naive_query_appends_remain_in_the_sender():
    src = SENDER.read_text(encoding="utf-8")
    offenders = [line for line in src.splitlines()
                 if re.search(r'\+\s*"\?wait=true"', line) and not line.strip().startswith("#")]
    assert offenders == [], f"raw '?wait=true' appends survive: {offenders}"
    # and the edit path must not interpolate a query-carrying URL
    assert '{DASHBOARD_WEBHOOK_URL}/messages/' not in src


def test_f01_customer_webhook_urls_keep_their_thread_selector(activated, services):
    """The stored URL still targets the forum thread; F01 is fixed on the joining side."""
    hooks = services.customers.webhooks(CUSTOMER)
    assert hooks and hooks.complete()
    for url in (hooks.dashboard, hooks.logs, hooks.deals):
        assert url and "?thread_id=" in url
        assert url.count("?") == 1


# ═════════════════════════════════════════════════════════════════════════════
# F02 — workflow input flags + image transport
# ═════════════════════════════════════════════════════════════════════════════
def _declared_workflow_inputs() -> set[str]:
    """Parse the real ``workflow_dispatch.inputs`` block out of send_ads.yml."""
    names: set[str] = set()
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == "inputs:")
    except StopIteration:  # pragma: no cover
        pytest.fail("send_ads.yml has no workflow_dispatch inputs block")
    for line in lines[start + 1:]:
        if line.strip() and not line.startswith("      "):
            break
        m = re.match(r"^      ([a-z0-9_]+):\s*$", line)
        if m:
            names.add(m.group(1))
    return names


def test_f02_workflow_input_names_match_the_yaml():
    declared = _declared_workflow_inputs()
    assert declared, "could not parse workflow inputs"
    assert WORKFLOW_INPUTS == declared, f"drift: only-in-code={sorted(WORKFLOW_INPUTS - declared)} only-in-yaml={sorted(declared - WORKFLOW_INPUTS)}"


def test_f02_flag_values_are_declared_choice_options():
    assert RUNTIME_LIMITLESS_VALUES == ("0", "1") and ATTACH_IMAGE_VALUES == ("yes", "no")
    for value in RUNTIME_LIMITLESS_VALUES:
        assert re.search(rf"^\s+- '{value}'$", WORKFLOW.read_text(encoding="utf-8"), re.M)
    for value in ATTACH_IMAGE_VALUES:
        assert re.search(rf"^\s+- '{value}'$", WORKFLOW.read_text(encoding="utf-8"), re.M)


def test_f02_never_emits_true_or_false_flags():
    for limitless in (True, False):
        for attach in (True, False):
            inputs = build_inputs(ad_type="sell", message="m", limitless=limitless, attach_image=attach)
            assert inputs["runtime_limitless"] in RUNTIME_LIMITLESS_VALUES
            assert inputs["attach_image"] in ATTACH_IMAGE_VALUES
            assert set(inputs) <= WORKFLOW_INPUTS


def test_f02_image_is_committed_to_the_repo_not_stored_as_a_secret(invoke, services, transport, activated):
    alt = activated["alt"]
    control = activated["control"]
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 512
    ctx = invoke.ctx(CUSTOMER, "run", control, mode="sell", rate="2.3", message="hi", interval=5, hours=6, alt=1)
    ctx.attachment_bytes = png
    ctx.attachment_content_type = "image/png"
    reply = run(_run_with_policy_ack(ctx))
    assert reply.content.startswith("🚀"), reply.content
    repo = transport.repo(alt.repo_owner, alt.repo_name)
    assert repo.files[AD_IMAGE_PATH] == png
    assert "AD_IMAGE_B64" not in repo.secrets


def test_f02_image_larger_than_the_contents_api_limit_is_rejected(invoke, activated):
    from adfarm.core.rules import MAX_IMAGE_BYTES

    control = activated["control"]
    ctx = invoke.ctx(CUSTOMER, "run", control, mode="sell", rate="2.3", message="hi", interval=5, hours=6, alt=1)
    ctx.attachment_bytes = b"x" * (MAX_IMAGE_BYTES + 1)
    ctx.attachment_content_type = "image/png"
    reply = run(_run_with_policy_ack(ctx))
    assert reply.content.startswith("❌ Image must be smaller than 1 MB")


async def _run_with_policy_ack(ctx):
    from adfarm.commands.context import run_handler

    ctx.options["policy_ack"] = True
    return await run_handler(cust.run, ctx)


# ═════════════════════════════════════════════════════════════════════════════
# F03 — heartbeat edits are ingested
# ═════════════════════════════════════════════════════════════════════════════
def _hb_message(channel_id, *, status="active", sent=1, message_id="900000000000000001"):
    """Duck-typed stand-in for a discord.Message posted by send_ads.py ``_send_heartbeat``."""
    embed = SimpleNamespace(
        title=f"💓 Heartbeat · main",
        description="",
        footer=SimpleNamespace(text=f"alt_id={_SENDER_ALT_ID} · V6.0 · updated 10:00:00"),
        fields=[SimpleNamespace(name="Status", value=f"`{status}`"), SimpleNamespace(name="Activity", value=f"Sent: `{sent}` · Errors: `0` · Skips: `0`")],
    )
    return SimpleNamespace(id=message_id, webhook_id=12345, content=f"💓 **Heartbeat** · `{status}`",
                           channel=SimpleNamespace(id=channel_id), author=SimpleNamespace(name="main"), embeds=[embed])


_SENDER_ALT_ID = 0  # patched per test


def test_f03_edited_heartbeat_updates_fleet_state(services, activated):
    from adfarm.app import build_ingestor, ingest_message

    global _SENDER_ALT_ID
    _SENDER_ALT_ID = activated["alt"].sender_alt_id
    ingestor = build_ingestor(services)
    dashboard = activated["dashboard"]
    key = (CUSTOMER, 1)

    run(ingest_message(services, ingestor, _hb_message(dashboard, status="active", sent=1)))
    live = services.fleet.get(key)
    assert live is not None and live.online and live.total_sent == 1

    # The sender PATCHes the same message instead of posting a new one. Before F03 nothing
    # listened to MESSAGE_UPDATE, so this second heartbeat was invisible.
    run(ingest_message(services, ingestor, _hb_message(dashboard, status="paused", sent=42)))
    live = services.fleet.get(key)
    assert live.online and live.status == "paused" and live.total_sent == 42


def test_f03_non_webhook_messages_are_ignored(services, activated):
    """A human typing in #dashboard must not be mistaken for a heartbeat."""
    from adfarm.app import build_ingestor, ingest_message

    global _SENDER_ALT_ID
    _SENDER_ALT_ID = activated["alt"].sender_alt_id
    ingestor = build_ingestor(services)
    msg = _hb_message(activated["dashboard"])
    msg.webhook_id = None
    run(ingest_message(services, ingestor, msg))
    live = services.fleet.get((CUSTOMER, 1))
    # the alt row is registered by rehydrate(), but no heartbeat may have been applied to it
    assert live is not None and not live.online and live.last_heartbeat_at == 0.0 and live.total_sent == 0


# ═════════════════════════════════════════════════════════════════════════════
# F09 — renewal keeps tuning; expired customers can still /renew
# ═════════════════════════════════════════════════════════════════════════════
def test_f09_merged_renewal_payload_applies_tuning_overrides():
    payload = {"ad_type": "sell", "message": "old", "rate": 2.0, "interval_min": 5, "policy": "stealth", "buy_style": "simple"}
    overrides = {"rate": 3.5, "message": "new copy", "interval_min": 3, "policy": "aggressive", "deal_scan_enabled": True}
    merged = merged_renewal_payload(payload, overrides)
    assert merged["rate"] == 3.5 and merged["message"] == "new copy" and merged["interval_min"] == 3 and merged["policy"] == "aggressive"
    assert merged["ad_type"] == "sell" and merged["buy_style"] == "simple"   # untouched keys survive
    assert "deal_scan_enabled" not in merged                                # non-payload keys are not injected
    assert merged_renewal_payload(payload, {}) == payload
    assert merged_renewal_payload(payload, {"rate": None, "message": ""})["rate"] == 2.0


def test_f09_limitless_renewal_dispatches_the_tuned_settings(invoke, services, transport, activated, clock):
    alt = activated["alt"]
    control = activated["control"]
    invoke_ctx_run(invoke, CUSTOMER, control, hours=0)
    run_state = services.repos.runs.get(CUSTOMER, 1)
    assert run_state.mode is RunMode.LIMITLESS

    # customer tunes the live run
    applied = run(services.runs.tune(alt, actor_id=CUSTOMER, price="9.99", message="tuned copy", interval=3))
    assert applied
    first = transport.repo(alt.repo_owner, alt.repo_name).dispatches[-1]["inputs"]

    clock.advance(48 * 3600 + 60)
    result = run(services.runs.renew(run_state))
    assert result is not None and result.renewed
    second = transport.repo(alt.repo_owner, alt.repo_name).dispatches[-1]["inputs"]
    assert second["runtime_limitless"] == "1"
    assert second["message"] == "tuned copy" and second["interval_min"] == "3"
    assert second["sell_rate"] == "9.99", f"renewal reverted the price: {first} → {second}"


def invoke_ctx_run(invoke, user_id, channel, **options):
    """Run /run the way the registry does, with the policy already acknowledged."""
    import asyncio

    from adfarm.commands.context import run_handler

    ctx = invoke.ctx(user_id, "run", channel, mode="sell", rate="2.3", message="hi", interval=5, hours=options.pop("hours", 24), alt=1, **options)
    ctx.options["policy_ack"] = True
    return asyncio.run(run_handler(cust.run, ctx))


def test_f09_expired_customer_can_still_open_a_renewal_ticket(invoke, services, activated):
    customer = services.repos.customers.get(CUSTOMER)
    services.repos.customers.save(customer.with_(expiry_date=services.now() - DAY), now=services.now())
    assert services.guard.actor_for(CUSTOMER).tier is Tier.PUBLIC     # dropped out of the customer tier

    denied = invoke.call(CUSTOMER, "setup", cust.setup, TICKET_CH)
    assert denied.content.startswith("❌ Your subscription has expired")

    reply = invoke.call(CUSTOMER, "renew", cust.renew, TICKET_CH, days=30)
    assert reply.content.startswith("🧾 Renewal ticket"), reply.content
    assert services.repos.tickets.find_open(CUSTOMER, "renew") is not None


def test_f09_policy_decision_table_for_expired_callers():
    assert decide(Tier.PUBLIC, "renew", ChannelKind.TICKET, state="expired").allowed
    assert decide(Tier.PUBLIC, "renew", ChannelKind.OWN_HUB, state="expired").allowed
    assert not decide(Tier.PUBLIC, "renew", ChannelKind.PUBLIC, state="expired").allowed
    assert not decide(Tier.PUBLIC, "run", ChannelKind.TICKET, state="expired").allowed
    assert not decide(Tier.PUBLIC, "setup", ChannelKind.OWN_HUB, state="expired").allowed
    assert "renew" in EXPIRED_ALLOWED_COMMANDS and "run" not in EXPIRED_ALLOWED_COMMANDS
    # a brand-new stranger (never a customer) still gets nothing
    assert not decide(Tier.PUBLIC, "renew", ChannelKind.TICKET, state="none").allowed


# ═════════════════════════════════════════════════════════════════════════════
# P1-1 — modal titles
# ═════════════════════════════════════════════════════════════════════════════
def test_p1_1_modal_titles_fit_discords_45_character_limit():
    for index in range(1, 5):
        title = SetupModal(None, None, index).title
        assert 1 <= len(title) <= MODAL_TITLE_LIMIT == 45, (index, title, len(title))
    assert 1 <= len(TicketModal(None).title) <= MODAL_TITLE_LIMIT
    assert len(modal_title("x" * 200)) == MODAL_TITLE_LIMIT


def test_p1_1_the_old_setup_title_was_the_offender():
    # Documents exactly what broke: the V9.1 title was 46 characters.
    assert len("Setup alt 1 (never share this token elsewhere)") == 46 > MODAL_TITLE_LIMIT


# ═════════════════════════════════════════════════════════════════════════════
# P1-2 / P2-7 — channel + forum permission matrix
# ═════════════════════════════════════════════════════════════════════════════
def _by_target(overwrites):
    return {(o.target, o.target_id): o for o in overwrites}


def test_p1_2_public_rooms_are_open_to_everyone():
    ow = _by_target(public_overwrites("role1"))
    assert VIEW in ow[("everyone", "")].allow and "send_messages" in ow[("everyone", "")].allow
    assert ow[("everyone", "")].deny == frozenset()
    assert "use_application_commands" in ow[("everyone", "")].allow      # /help + /getstarted


def test_p1_2_staff_rooms_are_hidden_from_everyone():
    ow = _by_target(staff_overwrites("role1", ["111", "222"]))
    assert VIEW in ow[("everyone", "")].deny and "use_application_commands" in ow[("everyone", "")].deny
    assert ow[("role", "role1")].allow >= ADMIN_ALLOW
    assert {t for (k, t) in ow if k == "member"} == {"111", "222"}


def test_p1_2_customer_hub_category_is_hidden_from_everyone():
    ow = _by_target(hub_overwrites("role1", ["111"]))
    assert VIEW in ow[("everyone", "")].deny


def test_p2_7_forum_is_private_to_the_customer_and_visible_to_admins():
    ow = _by_target(forum_overwrites(customer_user_id=CUSTOMER, admin_role_id="role1", admin_user_ids=(ADMIN, OTHER)))
    assert VIEW in ow[("everyone", "")].deny and "send_messages" in ow[("everyone", "")].deny
    assert ow[("member", CUSTOMER)].allow == CUSTOMER_ALLOW
    assert ow[("member", ADMIN)].allow >= ADMIN_ALLOW and "send_messages_in_threads" in ow[("member", ADMIN)].allow
    assert ow[("role", "role1")].allow >= ADMIN_ALLOW
    assert "manage_webhooks" in ow[("bot", "")].allow                     # the bot must be able to create the hooks


def test_p2_7_forum_provisioner_passes_admin_ids_to_the_adapter(services, discord, activated):
    spec = discord.forum_specs[0]
    assert spec.customer_user_id == CUSTOMER
    assert set(spec.admin_user_ids) == services.settings.owner_ids


# ═════════════════════════════════════════════════════════════════════════════
# P1-3 — /help-admin
# ═════════════════════════════════════════════════════════════════════════════
def test_p1_3_help_admin_documents_every_action(invoke):
    reply = invoke.call(ADMIN, "help-admin", admin_cmds.help_admin, "400000000000000001")
    body = reply.embed.to_dict()
    names = [f["name"] for f in body["fields"]]
    desc = body.get("description") or ""
    for action in admin_cmds.ADMIN_ACTIONS:
        assert f"/admin action:{action}" in names, f"{action} is missing from /help-admin"
        assert f"• **{action}**" in desc or any("•" in (f.get("value") or "") for f in body["fields"])
    for category, _names in admin_cmds.ADMIN_HELP_CATEGORIES:
        assert category in desc
    assert "⚠️ undocumented" not in names
    assert COMMAND_TIERS["help-admin"] is Tier.ADMIN


def test_p1_3_help_admin_is_refused_outside_admin_rooms(invoke):
    reply = invoke.call(ADMIN, "help-admin", admin_cmds.help_admin, PUBLIC_CH)
    assert reply.content.startswith("❌ Admin commands must be issued from an admin channel")
    stranger = invoke.call(CUSTOMER, "help-admin", admin_cmds.help_admin, "400000000000000001")
    assert stranger.content.startswith("❌ You are not authorized")


# ═════════════════════════════════════════════════════════════════════════════
# P1-4 — repo names / GitHub accounts hidden from customers
# ═════════════════════════════════════════════════════════════════════════════
def test_p1_4_status_hides_the_repo_slug_from_customers(invoke, activated, services):
    alt = activated["alt"]
    customer_reply = invoke.call(CUSTOMER, "status", cust.status, activated["control"])
    assert alt.repo_slug not in customer_reply.embed.to_dict()["description"] + str(customer_reply.embed.to_dict()["fields"])
    assert "Repo" not in [f["name"] for f in customer_reply.embed.to_dict()["fields"]]

    admin_reply = invoke.call(ADMIN, "status", cust.status, "400000000000000001", customer=CUSTOMER)
    assert "Repo" in [f["name"] for f in admin_reply.embed.to_dict()["fields"]]


def test_p1_4_setup_and_run_replies_hide_the_repo_from_customers(invoke, services, transport, activated):
    from tests.fakes import valid_token

    alt = activated["alt"]
    control = activated["control"]
    ctx = invoke.ctx(CUSTOMER, "setup", control, alt=1, token=valid_token("Z"), channels="111111111111111111")
    reply = run(_call_setup(ctx))
    assert alt.repo_slug not in reply.content

    run_reply = invoke_ctx_run(invoke, CUSTOMER, control, hours=6)
    assert alt.repo_slug not in run_reply.content and "github.com" not in run_reply.content

    runs_reply = invoke.call(CUSTOMER, "alt", cust.alt, control, action="runs", alt=1)
    assert alt.repo_slug not in runs_reply.content


async def _call_setup(ctx):
    from adfarm.commands.context import run_handler

    return await run_handler(cust.setup_submit, ctx)


# ═════════════════════════════════════════════════════════════════════════════
# P1-5 — welcoming service agreement
# ═════════════════════════════════════════════════════════════════════════════
def test_p1_5_policy_text_is_the_service_agreement():
    assert POLICY_TITLE == "📜 AdFarm V9 — Service Agreement"
    assert "Service Agreement" in POLICY_TEXT
    for point in ("BEP-20", "main accounts are not supported", "stored encrypted"):
        assert point in POLICY_TEXT
    assert "✅" in POLICY_ACCEPT_LABEL
    # the risk-first V9.1 wording is gone
    for banned in ("ban risk", "without refund", "pro-rated"):
        assert banned not in POLICY_TEXT, banned


def test_p1_5_policy_embed_and_getstarted_render_the_agreement(invoke, services, activated):
    embed = services.tickets.policy_embed()
    assert embed.title == POLICY_TITLE and POLICY_TEXT in embed.description
    assert POLICY_VERSION in embed.footer

    started = invoke.call(CUSTOMER, "getstarted", pub.getstarted, PUBLIC_CH)
    assert "Service Agreement" in started.embed.to_dict()["description"] + str(started.embed.to_dict()["fields"])


def test_p1_5_bumping_the_version_re_asks_for_acknowledgement(invoke, services, activated):
    services.tickets.ack_policy(CUSTOMER)
    assert services.tickets.policy_acked(CUSTOMER)
    import dataclasses

    services.settings = dataclasses.replace(services.settings, policy_version="v9-next")
    assert not services.tickets.policy_acked(CUSTOMER)   # the ack was recorded for the old wording


# ═════════════════════════════════════════════════════════════════════════════
# P1-6 — /run hours:Limitless
# ═════════════════════════════════════════════════════════════════════════════
def test_p1_6_limitless_run_dispatches_with_the_limitless_flag(invoke, services, transport, activated):
    alt = activated["alt"]
    reply = invoke_ctx_run(invoke, CUSTOMER, activated["control"], hours=0)
    assert reply.content.startswith("🚀") and "limitless" in reply.content
    inputs = transport.repo(alt.repo_owner, alt.repo_name).dispatches[-1]["inputs"]
    assert inputs["runtime_limitless"] == "1" and inputs["total_hours"] == "48"
    stored = services.repos.runs.get(CUSTOMER, 1)
    assert stored.mode is RunMode.LIMITLESS and stored.runtime_hours == 0
    assert "limitless" not in inputs.values()          # no true/false leakage anywhere


def test_p1_6_timed_run_dispatches_without_the_limitless_flag(invoke, services, transport, activated):
    alt = activated["alt"]
    invoke_ctx_run(invoke, CUSTOMER, activated["control"], hours=24)
    inputs = transport.repo(alt.repo_owner, alt.repo_name).dispatches[-1]["inputs"]
    assert inputs["runtime_limitless"] == "0" and inputs["total_hours"] == "24"


# ═════════════════════════════════════════════════════════════════════════════
# P1-7 — the ticket panel actually has a working button
# ═════════════════════════════════════════════════════════════════════════════
def test_p1_7_ticket_panel_reply_hands_the_posting_to_the_registry(invoke, discord):
    reply = invoke.call(ADMIN, "admin", admin_cmds.admin, "400000000000000001", action="ticket-panel", channel=TICKET_CH)
    assert reply.content.startswith("📌 Ticket panel posted")
    # handlers stay framework-neutral: the marker names the channel and carries the embed,
    # and CommandRegistry.post_ticket_panel attaches the real discord view.
    assert reply.view["kind"] == "post_ticket_panel"
    assert reply.view["channel"] == TICKET_CH
    assert "Open Ticket" in reply.view["embed"].description
    assert services_ticket_channel(invoke) == TICKET_CH


def services_ticket_channel(invoke):
    return invoke.s.tickets.ticket_channel_id


def test_p1_7_registry_posts_the_panel_with_a_real_view_and_pins_it(invoke, services, discord, classifier):
    import discord as _discord

    from adfarm.commands.registry import CommandRegistry

    panel = invoke.call(ADMIN, "admin", admin_cmds.admin, "400000000000000001", action="ticket-panel", channel=TICKET_CH)
    registry = CommandRegistry(_discord.app_commands.CommandTree(_discord.Client(intents=_discord.Intents.none())),
                               services, classifier, guild_id="1")
    message_id = run(registry.post_ticket_panel(TICKET_CH, panel.view["embed"]))
    assert message_id
    assert discord.sent[-1].channel_id == TICKET_CH
    assert discord.pinned == [(TICKET_CH, message_id)]          # the panel message itself is pinned
    assert isinstance(discord.sent[-1].view, TicketPanelView)   # a real discord view, not a marker dict
    assert "Open Ticket" in discord.sent[-1].view.children[0].label


def test_p1_7_the_marker_renders_a_button_with_a_stable_custom_id():
    view = _view_for(SimpleNamespace(s=None), type("R", (), {"view": {"kind": "ticket_panel"}})(), None)
    assert isinstance(view, TicketPanelView)
    assert view.timeout is None                                   # persistent across restarts
    buttons = [c for c in view.children]
    assert len(buttons) == 1
    assert buttons[0].custom_id == TicketPanelView.CUSTOM_ID == "adfarm:ticket:open"
    assert "Open Ticket" in buttons[0].label
    assert len(buttons[0].label) <= 80                            # Discord button label limit


def test_p1_7_the_button_opens_a_modal_that_creates_a_ticket(services, discord):
    modal = TicketModal(SimpleNamespace(s=services))
    assert 1 <= len(modal.title) <= MODAL_TITLE_LIMIT
    assert modal.topic.min_length == 5 and modal.topic.max_length == 300


def test_p1_7_open_support_creates_a_thread_in_the_ticket_channel(services, discord):
    ticket = run(services.tickets.open_support(discord_id=CUSTOMER, topic="I want 2 alts for 30 days", username="alice"))
    assert ticket.kind == "support" and ticket.id > 0
    assert discord.threads_created and discord.threads_created[0][0] == TICKET_CH
    assert f"ticket-{ticket.id}" == discord.threads_created[0][1]
    assert ticket.channel_id and ticket.channel_id != TICKET_CH     # the thread, not the parent channel
    assert "I want 2 alts for 30 days" in discord.threads_created[0][2]

    with pytest.raises(Exception) as dup:
        run(services.tickets.open_support(discord_id=CUSTOMER, topic="another one"))
    assert "still open" in str(dup.value) or "already" in str(dup.value)

    with pytest.raises(Exception) as short:
        run(services.tickets.open_support(discord_id=OTHER, topic="hi"))
    assert "at least" in str(short.value)


# ═════════════════════════════════════════════════════════════════════════════
# P2-8 — no hardcoded snowflakes in the shipped package
# ═════════════════════════════════════════════════════════════════════════════
def test_p2_8_no_hardcoded_discord_ids_in_the_package():
    offenders = []
    for path in sorted((ROOT / "adfarm").rglob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r'"\d{16,20}"', line) and not line.strip().startswith("#"):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert offenders == [], "\n".join(offenders)


# ═════════════════════════════════════════════════════════════════════════════
# P2-9 — command visibility
# ═════════════════════════════════════════════════════════════════════════════
def test_p2_9_registry_hides_operator_commands_from_non_admins(services, classifier):
    import discord

    from adfarm.commands.registry import CommandRegistry

    tree = discord.app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
    registry = CommandRegistry(tree, services, classifier, guild_id="1")
    registry.register_all()

    by_name = {c.name: c for c in tree.get_commands()}
    assert {"admin", "help-admin"} <= set(by_name)
    for name in ADMIN_ONLY_COMMANDS:
        # Visible to OWNER_IDS who may not have Discord Server Administrator.
        assert by_name[name].default_permissions is None or by_name[name].default_permissions.administrator is not True
    for name, cmd in by_name.items():
        assert cmd.guild_only is True, name
        assert cmd.default_permissions is None or cmd.default_permissions.administrator is not True


def test_ip_lookup_falls_back_when_ipwho_rate_limits():
    src = SENDER.read_text(encoding="utf-8")
    assert "https://ipwho.is/" in src
    assert "https://ipapi.co/" in src
    assert "https://ipinfo.io/" in src
    assert "if not payload or payload.get(\"success\") is False" in src


def test_skill_md_has_admin_operator_guide():
    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert "## Admin Operator Guide" in skill
    assert "10 most common admin commands" in skill
    assert "Heartbeat stale" in skill
    assert "Ticket not working" in skill


def test_p2_9_a_stranger_only_ever_gets_help_and_getstarted(services):
    from tests.conftest import STRANGER

    from adfarm.security.guards import ChannelInfo

    usable = [cmd for cmd in COMMAND_TIERS if services.guard.check(STRANGER, cmd, ChannelInfo(channel_id=PUBLIC_CH, kind_hint=ChannelKind.PUBLIC)).decision.allowed]
    assert sorted(usable) == ["getstarted", "help"]
