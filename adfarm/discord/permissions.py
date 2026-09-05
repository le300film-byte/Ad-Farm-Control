"""Discord permission model — the single home for every permission override AdFarm applies.

Framework neutral on purpose: these builders return :class:`Overwrite` values which
``adapter.DiscordPyGuildAdmin._resolve`` / ``adapter.DiscordPyAdapter`` translate into
``discord.PermissionOverwrite`` objects, so the whole matrix is unit-testable without
discord.py and cannot drift between the guild provisioner and the per-customer forums.

The matrix (V9 hardening):

=========================  =========  =========  =========  ==========================
Room                       @everyone  customer   admins     rationale
=========================  =========  =========  =========  ==========================
#welcome-about, #pricing   view+send  —          view+send  public: discovery & sales
#general-chat, #whats-new
#open-ticket               view+send  —          view+send  public: on-ramp to /renew
#admin-commands/-chat,     hidden     hidden     view+send  staff only
#audit-logs
🏢 Customer Hub category   hidden     hidden     view+send  container for all hubs
<customer>-hub forum       hidden     view+send  view+send  one hub per customer
=========================  =========  =========  =========  ==========================

Channel *permissions* decide who can speak; command *visibility* is decided separately by
``security.policy`` + ``Guard`` (see ``COMMAND_TIERS``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


# ── permission names (discord.py PermissionOverwrite attribute names) ────────
VIEW = "view_channel"
SEND = "send_messages"
HISTORY = "read_message_history"
CMDS = "use_application_commands"
THREADS_IN = "send_messages_in_threads"
THREADS_PUBLIC = "create_public_threads"
THREADS_PRIVATE = "create_private_threads"
ATTACH = "attach_files"
MANAGE = "manage_channels"
MANAGE_MSG = "manage_messages"
MANAGE_HOOKS = "manage_webhooks"


@dataclass(frozen=True)
class Overwrite:
    """One permission override. ``target`` is ``everyone`` | ``role`` | ``member`` | ``bot``."""

    target: str
    target_id: str = ""
    allow: frozenset[str] = frozenset()
    deny: frozenset[str] = frozenset()


# Everything the bot needs to run a customer hub end to end.
BOT_FULL = Overwrite("bot", allow=frozenset({VIEW, SEND, HISTORY, CMDS, THREADS_IN, THREADS_PUBLIC, THREADS_PRIVATE,
                                             MANAGE, MANAGE_MSG, MANAGE_HOOKS}))

# What an admin needs everywhere.
ADMIN_ALLOW = frozenset({VIEW, SEND, HISTORY, CMDS, MANAGE_MSG})

# What a customer needs inside their own hub.
CUSTOMER_ALLOW = frozenset({VIEW, SEND, HISTORY, CMDS, THREADS_IN, ATTACH})

# What @everyone gets in a public room. Note that *which slash commands* a stranger sees is
# enforced by the command registry / Guard, not by Discord permissions.
PUBLIC_ALLOW = frozenset({VIEW, SEND, HISTORY, CMDS})


def public_overwrites(admin_role_id: str = "") -> tuple[Overwrite, ...]:
    """@everyone may read/write/slash in the public rooms."""
    out = [Overwrite("everyone", allow=PUBLIC_ALLOW), BOT_FULL]
    if admin_role_id:
        out.append(Overwrite("role", admin_role_id, allow=ADMIN_ALLOW | {MANAGE_MSG}))
    return tuple(out)


def staff_overwrites(admin_role_id: str = "", owner_ids: Iterable[str] = ()) -> tuple[Overwrite, ...]:
    """Staff rooms are invisible to @everyone; admins (role + explicit owners) may use them."""
    out = [Overwrite("everyone", deny=frozenset({VIEW, SEND, CMDS})), BOT_FULL]
    if admin_role_id:
        out.append(Overwrite("role", admin_role_id, allow=ADMIN_ALLOW | {MANAGE_MSG}))
    for uid in owner_ids:
        out.append(Overwrite("member", str(uid), allow=ADMIN_ALLOW | {MANAGE_MSG}))
    return tuple(out)


def hub_overwrites(admin_role_id: str = "", owner_ids: Iterable[str] = ()) -> tuple[Overwrite, ...]:
    """Customer Hub category: invisible to @everyone. Per-customer forums created inside it add
    the customer as an explicit member overwrite (see :func:`forum_overwrites`)."""
    return staff_overwrites(admin_role_id, owner_ids)


def forum_overwrites(*, customer_user_id: str, admin_role_id: str = "", admin_user_ids: Iterable[str] = ()) -> tuple[Overwrite, ...]:
    """One customer forum: hidden from @everyone, writable by that customer, visible to staff.

    ``admin_user_ids`` are granted explicitly so an operator always sees every hub even when
    their role lacks ``manage_channels`` (which is what would otherwise grant the implicit
    view-all-channels permission).
    """
    out = [Overwrite("everyone", deny=frozenset({VIEW, SEND, CMDS, THREADS_IN})), BOT_FULL]
    if customer_user_id:
        out.append(Overwrite("member", str(customer_user_id), allow=CUSTOMER_ALLOW))
    if admin_role_id:
        out.append(Overwrite("role", admin_role_id, allow=ADMIN_ALLOW | {MANAGE_MSG, THREADS_IN}))
    for uid in admin_user_ids:
        out.append(Overwrite("member", str(uid), allow=ADMIN_ALLOW | {MANAGE_MSG, THREADS_IN}))
    return tuple(out)
