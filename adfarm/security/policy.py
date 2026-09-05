"""Single source of truth for command tiers, the channel matrix and every denial message.

``/help`` renders from these tables, so documentation can never drift from enforcement (L-16).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass

from ..core.models import Tier


class ChannelKind(str, enum.Enum):
    PUBLIC = "public"          # welcome / pricing / announcements rooms
    TICKET = "ticket"          # #open-ticket
    OWN_HUB = "own_hub"        # thread/forum of the calling customer
    OTHER_HUB = "other_hub"    # somebody else's customer forum
    ADMIN = "admin"            # admin-* rooms
    DM = "dm"
    UNKNOWN = "unknown"


# ── command → minimum tier ──────────────────────────────────────────────────
COMMAND_TIERS: dict[str, Tier] = {
    "help": Tier.PUBLIC,
    "getstarted": Tier.PUBLIC,
    "account": Tier.CUSTOMER,
    "setup": Tier.CUSTOMER,
    "run": Tier.CUSTOMER,
    "stop": Tier.CUSTOMER,
    "pause": Tier.CUSTOMER,
    "resume": Tier.CUSTOMER,
    "tune": Tier.CUSTOMER,
    "channels": Tier.CUSTOMER,
    "deals": Tier.CUSTOMER,
    "status": Tier.CUSTOMER,
    "reply": Tier.CUSTOMER,
    "alt": Tier.CUSTOMER,
    "renew": Tier.CUSTOMER,
    "pause-billing": Tier.CUSTOMER,
    "proofs": Tier.CUSTOMER,
    "vip": Tier.VIP,
    "admin": Tier.ADMIN,
    "help-admin": Tier.ADMIN,
}

# Customer-tier commands that are also allowed in the ticket room (billing conversations).
TICKET_ROOM_COMMANDS = frozenset({"renew", "pause-billing", "proofs", "account"})

# P2-9: commands that Discord itself hides from non-administrators (default_permissions).
ADMIN_ONLY_COMMANDS = frozenset({"admin", "help-admin"})

# F09: a customer whose plan has expired drops to Tier.PUBLIC, which used to lock them out of
# ``/renew`` — the very command that tells them to renew. These commands stay reachable while
# expired (the channel rules below still apply, and ``/renew`` only opens a ticket).
EXPIRED_ALLOWED_COMMANDS = frozenset({"renew"})

# ── tier of command → channel kinds where it may run ────────────────────────
CHANNEL_MATRIX: dict[Tier, frozenset[ChannelKind]] = {
    Tier.PUBLIC: frozenset(ChannelKind),
    Tier.CUSTOMER: frozenset({ChannelKind.OWN_HUB, ChannelKind.ADMIN, ChannelKind.TICKET}),
    Tier.VIP: frozenset({ChannelKind.OWN_HUB, ChannelKind.ADMIN}),
    Tier.ADMIN: frozenset({ChannelKind.ADMIN}),
}

# Multi-signature actions: (group, action) → typed confirmation word.
MULTISIG_ACTIONS: dict[tuple[str, str], str] = {
    ("admin", "reset"): "RESET",
    ("admin", "shutdown-bot"): "SHUTDOWN",
}

# ── user-facing denial strings ──────────────────────────────────────────────
DENY_NOT_CUSTOMER = "❌ You do not have an active subscription. You are not authorized to use this command."
DENY_EXPIRED = "❌ Your subscription has expired. Run `/renew` here to open a renewal ticket."
DENY_VIP = "❌ This feature requires VIP. Ask an admin to upgrade your plan."
DENY_ADMIN = "❌ You are not authorized to use this command."
DENY_PUBLIC_ROOM = "❌ Customer commands are disabled in public channels. Use your private customer hub."
DENY_OTHER_HUB = "❌ This is not your customer hub. Use the forum that was created for you."
DENY_ADMIN_ROOM = "❌ Admin commands must be issued from an admin channel."
DENY_DM = "❌ This command only works inside the server (your customer hub)."
DENY_UNKNOWN_ROOM = "❌ This command is not available in this channel."
DENY_FAIL_CLOSED = "❌ The security check could not be completed. Please try again."
DENY_MULTISIG = "⚠️ A second admin must confirm this action within {seconds} seconds."


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""

    @staticmethod
    def ok() -> "Decision":
        return Decision(True, "")

    @staticmethod
    def deny(reason: str) -> "Decision":
        return Decision(False, reason)


def required_tier(command: str) -> Tier:
    return COMMAND_TIERS.get(command.split(" ", 1)[0].lower(), Tier.ADMIN)  # unknown ⇒ admin only (fail closed)


def decide(actor_tier: Tier, command: str, kind: ChannelKind, *, state: str = "") -> Decision:
    """Pure policy decision. ``kind`` must already account for hub ownership.

    ``state`` is the caller's subscription state (``roles.subscription_state``) and is only used
    for the F09 renewal escape hatch: an expired customer may still open a renewal ticket.
    """
    command = command.split(" ", 1)[0].lower()
    needed = required_tier(command)

    # 1. tier check
    if not actor_tier.covers(needed):
        if state == "expired" and needed is Tier.CUSTOMER and command in EXPIRED_ALLOWED_COMMANDS:
            pass  # allowed: the renewal path is the way back in (channel rules still apply)
        elif needed is Tier.ADMIN:
            return Decision.deny(DENY_ADMIN)
        elif needed is Tier.VIP and actor_tier is Tier.CUSTOMER:
            return Decision.deny(DENY_VIP)
        else:
            return Decision.deny(DENY_NOT_CUSTOMER)

    # 2. channel check
    if needed is Tier.PUBLIC:
        return Decision.ok()
    if needed is Tier.ADMIN:
        return Decision.ok() if kind is ChannelKind.ADMIN else Decision.deny(DENY_ADMIN_ROOM)
    if actor_tier is Tier.ADMIN:
        return Decision.ok()  # admins may operate customer/VIP commands anywhere (support)
    allowed_kinds = CHANNEL_MATRIX[needed]
    if kind is ChannelKind.TICKET and command not in TICKET_ROOM_COMMANDS:
        return Decision.deny(DENY_UNKNOWN_ROOM)
    if kind in allowed_kinds:
        return Decision.ok()
    return Decision.deny({
        ChannelKind.PUBLIC: DENY_PUBLIC_ROOM,
        ChannelKind.OTHER_HUB: DENY_OTHER_HUB,
        ChannelKind.DM: DENY_DM,
        ChannelKind.ADMIN: DENY_ADMIN_ROOM,
    }.get(kind, DENY_UNKNOWN_ROOM))


def commands_for(tier: Tier) -> list[str]:
    return sorted(cmd for cmd, needed in COMMAND_TIERS.items() if tier.covers(needed))
