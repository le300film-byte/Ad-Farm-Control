"""security: policy matrix, guard, multisig, redaction."""
import pytest

from adfarm.core.clock import FakeClock
from adfarm.core.models import DAY, Customer, Tier
from adfarm.security import ChannelInfo, ChannelKind, Guard, MultiSig, commands_for, decide, policy, redact, required_tier, resolve_tier
from adfarm.security.redact import mask

NOW = 1000.0


def customer(**kw) -> Customer:
    base = dict(discord_id="c1", username="alice", alt_count=1, vip=False, start_date=0, expiry_date=NOW + 10 * DAY, active=True)
    base.update(kw)
    return Customer(**base)


# ── policy table ────────────────────────────────────────────────────────────
def test_required_tier_defaults_to_admin_for_unknown_commands():
    assert required_tier("help") is Tier.PUBLIC
    assert required_tier("run alt:1") is Tier.CUSTOMER
    assert required_tier("vip") is Tier.VIP
    assert required_tier("admin") is Tier.ADMIN
    assert required_tier("does-not-exist") is Tier.ADMIN


def test_shutdown_is_not_a_customer_command():
    assert "shutdown" not in policy.COMMAND_TIERS
    assert required_tier("shutdown") is Tier.ADMIN


@pytest.mark.parametrize("kind", list(ChannelKind))
def test_public_commands_work_everywhere(kind):
    assert decide(Tier.PUBLIC, "help", kind).allowed


@pytest.mark.parametrize("kind,expected", [
    (ChannelKind.OWN_HUB, True), (ChannelKind.ADMIN, True), (ChannelKind.PUBLIC, False), (ChannelKind.OTHER_HUB, False),
    (ChannelKind.DM, False), (ChannelKind.UNKNOWN, False), (ChannelKind.TICKET, False),
])
def test_customer_commands_channel_matrix(kind, expected):
    d = decide(Tier.CUSTOMER, "run", kind)
    assert d.allowed is expected
    if not expected:
        assert d.reason in {policy.DENY_PUBLIC_ROOM, policy.DENY_OTHER_HUB, policy.DENY_DM, policy.DENY_UNKNOWN_ROOM}


def test_ticket_room_allows_only_billing_commands():
    assert decide(Tier.CUSTOMER, "renew", ChannelKind.TICKET).allowed
    assert decide(Tier.CUSTOMER, "proofs", ChannelKind.TICKET).allowed
    assert not decide(Tier.CUSTOMER, "run", ChannelKind.TICKET).allowed


def test_tier_denials_have_specific_messages():
    assert decide(Tier.PUBLIC, "run", ChannelKind.OWN_HUB).reason == policy.DENY_NOT_CUSTOMER
    assert decide(Tier.CUSTOMER, "vip", ChannelKind.OWN_HUB).reason == policy.DENY_VIP
    assert decide(Tier.VIP, "admin", ChannelKind.ADMIN).reason == policy.DENY_ADMIN
    assert decide(Tier.PUBLIC, "admin", ChannelKind.ADMIN).reason == policy.DENY_ADMIN


def test_admin_commands_only_in_admin_rooms_even_for_admins():
    assert decide(Tier.ADMIN, "admin", ChannelKind.ADMIN).allowed
    for kind in (ChannelKind.PUBLIC, ChannelKind.OWN_HUB, ChannelKind.DM, ChannelKind.UNKNOWN):
        d = decide(Tier.ADMIN, "admin", kind)
        assert not d.allowed and d.reason == policy.DENY_ADMIN_ROOM


def test_admins_may_run_customer_commands_anywhere():
    for kind in ChannelKind:
        assert decide(Tier.ADMIN, "run", kind).allowed
        assert decide(Tier.ADMIN, "vip", kind).allowed


def test_commands_for_tier_is_monotonic():
    pub, cust, vip, adm = (set(commands_for(t)) for t in (Tier.PUBLIC, Tier.CUSTOMER, Tier.VIP, Tier.ADMIN))
    assert pub < cust < vip < adm
    assert pub == {"help", "getstarted"}
    assert "admin" in adm and "admin" not in vip


# ── roles / guard ───────────────────────────────────────────────────────────
def test_resolve_tier_fail_closed_without_owner_ids():
    assert resolve_tier("1", frozenset(), None, NOW) is Tier.PUBLIC
    assert resolve_tier("1", frozenset({"1"}), None, NOW) is Tier.ADMIN
    assert resolve_tier("c1", frozenset({"1"}), customer(), NOW) is Tier.CUSTOMER
    assert resolve_tier("c1", frozenset({"1"}), customer(vip=True), NOW) is Tier.VIP
    assert resolve_tier("c1", frozenset({"1"}), customer(expiry_date=NOW - 1), NOW) is Tier.PUBLIC


def test_guard_ownership_of_hub_and_expired_message():
    clock = FakeClock(NOW)
    table = {"c1": customer(), "c2": customer(discord_id="c2", expiry_date=NOW - 5)}
    guard = Guard(frozenset({"admin"}), table.get, clock=clock)
    own = ChannelInfo(channel_id="t1", hub_owner_id="c1")
    other = ChannelInfo(channel_id="t2", hub_owner_id="zz")
    assert guard.check("c1", "run", own).decision.allowed
    res = guard.check("c1", "run", other)
    assert not res.decision.allowed and res.decision.reason == policy.DENY_OTHER_HUB and res.kind is ChannelKind.OTHER_HUB
    expired = guard.check("c2", "run", ChannelInfo(channel_id="t3", hub_owner_id="c2"))
    assert expired.decision.reason == policy.DENY_EXPIRED
    stranger = guard.check("nobody", "run", own)
    assert stranger.decision.reason == policy.DENY_NOT_CUSTOMER
    admin = guard.check("admin", "admin", ChannelInfo(channel_id="a", kind_hint=ChannelKind.ADMIN))
    assert admin.decision.allowed and admin.actor.is_admin


def test_guard_fails_closed_on_lookup_error():
    def boom(_):
        raise RuntimeError("db down")

    guard = Guard(frozenset({"admin"}), boom, clock=FakeClock(NOW))
    res = guard.check("c1", "run", ChannelInfo(channel_id="x", kind_hint=ChannelKind.OWN_HUB))
    assert not res.decision.allowed and res.decision.reason == policy.DENY_FAIL_CLOSED


# ── multisig ────────────────────────────────────────────────────────────────
def test_multisig_requires_two_distinct_admins_within_window():
    clock = FakeClock(0)
    ms = MultiSig(window=120, clock=clock)
    assert ms.confirm("reset", "a") == (False, 1)
    assert ms.confirm("reset", "a") == (False, 1)        # same admin does not count twice
    clock.advance(121)
    assert ms.confirm("reset", "b") == (False, 1)        # first confirmation expired
    assert ms.confirm("reset", "a") == (True, 2)
    assert ms.pending("reset") == 0


# ── redaction ───────────────────────────────────────────────────────────────
def test_redact_masks_tokens_and_webhooks():
    text = ("gh ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 "
            "discord MTIzNDU2Nzg5MDEyMzQ1Njc4.Gabcde.abcdefghijklmnopqrstuvwxyz0123456789ABCD "
            "hook https://discord.com/api/webhooks/123456789/AbCdEfGhIjKlMnOpQrStUvWxYz0123456789")
    out = redact(text)
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in out and "ghp_AB***" in out
    assert "abcdefghijklmnopqrstuvwxyz0123456789ABCD" not in out
    assert "/webhooks/123456789/***" in out
    assert mask("abcdefghijkl") == "abcd…ijkl" and mask("short") == "***" and mask("") == ""
