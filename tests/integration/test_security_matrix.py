"""Security matrix: every registered command × every actor class × every room class, through the real gate.

This is the executable form of 02_REDESIGN.md §"Security model". If a cell changes, the redesign doc must change too.
"""
import pytest

from adfarm.security import commands_for, policy
from adfarm.security.policy import COMMAND_TIERS, Tier
from tests.conftest import ADMIN, ADMIN_CH, CUSTOMER, OTHER, PUBLIC_CH, STRANGER, TICKET_CH, run

ALL_COMMANDS = sorted(COMMAND_TIERS)


@pytest.fixture
def rooms(services, discord, activated):
    run(services.customers.activate(discord_id=OTHER, username="bob", days=30, actor_id=ADMIN))
    return {
        "public": PUBLIC_CH,
        "ticket": TICKET_CH,
        "admin": ADMIN_CH,
        "own_hub": activated["control"],
        "other_hub": services.repos.customers.get(OTHER).thread("control"),
        "dm": "dm-channel",
    }


def _expect(actor: str, command: str, room: str, vip: bool) -> bool:
    tier = COMMAND_TIERS[command]
    is_admin = actor == ADMIN
    is_customer = actor in (CUSTOMER, OTHER)
    if tier is Tier.PUBLIC:
        return True
    if tier is Tier.ADMIN:
        return is_admin and room == "admin"            # admin-tier commands only in admin rooms (single audit trail); not even DMs
    if is_admin:                                       # admins bypass the channel gate for customer/VIP commands (support use); replies are ephemeral
        return True
    if not is_customer:
        return False
    if tier is Tier.VIP and not vip:
        return False
    if room == "public":
        return False
    if room == "ticket":
        return command in policy.TICKET_ROOM_COMMANDS
    if room in ("own_hub", "admin"):                   # admin rooms are Discord-permission-gated to staff already (see 02_REDESIGN §5.2)
        return True
    return False                                       # other_hub, dm, unknown


def test_matrix(invoke, services, rooms):
    """One fixture set, every cell; failures are collected so a single run shows the whole diff."""
    actors = {ADMIN: "admin", CUSTOMER: "customer", STRANGER: "stranger"}
    mismatches: list[str] = []
    cells = 0
    for command in ALL_COMMANDS:
        for actor, label in actors.items():
            for room, channel_id in rooms.items():
                ctx = invoke.ctx(actor, command, channel_id)
                allowed = ctx.gate.decision.allowed
                expected = _expect(actor, command, room, vip=False)
                cells += 1
                if allowed != expected:
                    mismatches.append(f"{label} /{command} in {room}: got {'allow' if allowed else 'deny'} ({ctx.gate.decision.reason!r}), expected {'allow' if expected else 'deny'}")
    assert cells == len(ALL_COMMANDS) * len(actors) * len(rooms)
    assert not mismatches, "\n".join(mismatches)


def test_vip_tier_unlocks_after_flag(invoke, services, rooms):
    assert not invoke.ctx(CUSTOMER, "vip", rooms["own_hub"]).gate.decision.allowed
    run(services.customers.set_vip(CUSTOMER, True, actor_id=ADMIN))
    assert invoke.ctx(CUSTOMER, "vip", rooms["own_hub"]).gate.decision.allowed
    assert not invoke.ctx(CUSTOMER, "vip", rooms["other_hub"]).gate.decision.allowed
    assert not invoke.ctx(CUSTOMER, "vip", rooms["public"]).gate.decision.allowed


def test_help_lists_exactly_the_tier_commands(services):
    assert set(commands_for(Tier.PUBLIC)) == {c for c, t in COMMAND_TIERS.items() if t is Tier.PUBLIC}
    assert set(commands_for(Tier.CUSTOMER)) == {c for c, t in COMMAND_TIERS.items() if t in (Tier.PUBLIC, Tier.CUSTOMER)}
    assert "admin" in commands_for(Tier.ADMIN) and "admin" not in commands_for(Tier.VIP)


def test_owner_ids_fail_closed(settings, discord, transport, clock, tmp_path):
    from adfarm.app import build_services
    from adfarm.github.client import GitHubClient
    from tests.fakes import fake_token_checker

    no_admins = settings.__class__(**{**settings.__dict__, "owner_ids": frozenset(), "db_path": str(tmp_path / "x.db")})
    s = build_services(no_admins, discord, clock=clock, github=GitHubClient("tok-main", transport=transport, retries=1), token_checker=fake_token_checker())
    from adfarm.security import ChannelInfo, ChannelKind

    gate = s.guard.check(ADMIN, "admin", ChannelInfo(channel_id=ADMIN_CH, kind_hint=ChannelKind.ADMIN))
    assert not gate.decision.allowed
