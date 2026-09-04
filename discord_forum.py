"""discord_forum.py – V8 Discord Forum Channel & Thread Manager.

Creates and deletes private forum channels and threads for each customer
inside the 🏢 Customer Hub category. Each customer gets:
  - A private Forum Channel named after their Discord display name.
  - Up to 5 forum threads: #control, #dashboard, #farm-logs, #dm-inbox (VIP), #deals.

Uses discord.py 2.x APIs.
"""
from __future__ import annotations

import asyncio
import os as _os
from typing import Any, Optional

import discord

CUSTOMER_HUB_CATEGORY = "🏢 Customer Hub"

THREAD_NAMES = {
    "control":   "control",
    "dashboard": "dashboard",
    "logs":      "farm-logs",
    "dm_inbox":  "dm-inbox",
    "deals":     "deals",
}


async def _get_or_create_category(
    guild: discord.Guild,
    category_name: str,
    bot_member: discord.Member,
    admin_role: Optional[discord.Role] = None,
) -> discord.CategoryChannel:
    """Return the existing category or create it with restricted permissions."""
    existing = discord.utils.get(guild.categories, name=category_name)
    if existing:
        return existing

    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        bot_member: discord.PermissionOverwrite(
            view_channel=True,
            manage_channels=True,
            manage_messages=True,
        ),
    }
    if admin_role:
        overwrites[admin_role] = discord.PermissionOverwrite(view_channel=True)

    cat = await guild.create_category(
        category_name,
        overwrites=overwrites,
        reason="V8 Customer Hub category",
    )
    return cat


async def find_customer_forum(
    guild: discord.Guild,
    customer_member: discord.Member,
    display_name: str,
) -> Optional[discord.ForumChannel]:
    """Find a customer's existing private forum inside the Customer Hub.

    V8 bug-fix E: ``/admin activate`` used to create a brand-new forum on
    every run, so repeated activations piled up duplicate forums.  This
    searches every forum channel in the ``🏢 Customer Hub`` category and
    returns the first one that belongs to the customer — matched by:

    1. the customer member appearing in the channel's permission overwrites
       (the strongest signal), or
    2. the channel name matching the customer's display name.

    Returns ``None`` when no existing forum belongs to the customer.
    """
    try:
        display_name = (display_name or "").strip().lower()
        category = discord.utils.get(guild.categories, name=CUSTOMER_HUB_CATEGORY)
        if category is None:
            return None
        candidates: list[discord.ForumChannel] = []
        for channel in getattr(category, "channels", []) or []:
            if _looks_like_forum_channel(channel):
                candidates.append(channel)
        customer_id = getattr(customer_member, "id", None)
        for forum in candidates:
            try:
                overwrites = forum.overwrites or {}
            except Exception:
                overwrites = {}
            # 1. Permission-overwrite match: the customer is allowed in it.
            for target in overwrites:
                if customer_id is not None and getattr(target, "id", None) == customer_id:
                    ov = overwrites[target]
                    if ov is None or ov.view_channel:
                        return forum
            # 2. Name match on the display name.
            name = str(getattr(forum, "name", "") or "").strip().lower()
            if display_name and name == display_name:
                return forum
        return None
    except Exception as exc:
        print(f"[FORUM] Warning: customer-forum lookup failed: {exc}")
        return None


def _looks_like_forum_channel(channel: Any) -> bool:
    """True for real ``discord.ForumChannel`` objects and duck-typed stand-ins.

    Real guild categories hold ``ForumChannel`` instances (type 15).  Minimal
    mocks used by offline tests may carry neither, so a channel with a
    ``create_thread`` capability or permission-overwrite state and no plain
    ``send`` method (text channels have one, forums do not) is treated as a
    forum for the purposes of the reuse lookup.
    """
    if isinstance(channel, discord.ForumChannel):
        return True
    type_name = str(getattr(channel, "type", "")).lower()
    if "forum" in type_name or type_name == "15":
        return True
    if hasattr(channel, "create_thread"):
        return True
    return hasattr(channel, "overwrites") and not hasattr(channel, "send")


async def _collect_or_create_threads(
    forum: discord.ForumChannel,
    vip: bool,
    display_name: str,
) -> dict[str, int]:
    """Return {forum_id, ..._thread_id} for an EXISTING forum.

    Reuses already-present threads (#control, #dashboard, #farm-logs, #deals,
    #dm-inbox) and creates only the ones that are missing — e.g. #dm-inbox
    when a customer is upgraded to VIP after activation.
    """
    ids: dict[str, int] = {"forum_id": forum.id}
    thread_map = [
        ("control", "control"),
        ("dashboard", "dashboard"),
        ("logs", "farm-logs"),
        ("deals", "deals"),
    ]
    if vip:
        # Key must be "dm" so the id lands in `dm_thread_id` — the column
        # admin_activate persists. ("dm_inbox" produced a phantom
        # `dm_inbox_thread_id` key and dm_thread_id was always 0, breaking
        # the VIP DM auto-reply watcher — V8 plan feature #5.)
        thread_map.insert(3, ("dm", "dm-inbox"))

    existing: dict[str, discord.Thread] = {}
    try:
        for thread in getattr(forum, "threads", []) or []:
            existing[str(getattr(thread, "name", "") or "").strip()] = thread
    except Exception:
        existing = {}

    for key, thread_name in thread_map:
        thread = existing.get(thread_name)
        if thread is None:
            try:
                thread, _ = await forum.create_thread(
                    name=thread_name,
                    content=f"📌 **#{thread_name}** — {_thread_description(thread_name)}",
                    reason=f"V8 customer thread: #{thread_name}",
                )
            except Exception as exc:
                print(f"[FORUM] Warning: could not create thread #{thread_name}: {exc}")
                ids[f"{key}_thread_id"] = 0
                continue
        ids[f"{key}_thread_id"] = getattr(thread, "id", 0) or 0

    # Ensure non-VIP keys always exist
    for k in ("control_thread_id", "dashboard_thread_id", "logs_thread_id",
              "dm_thread_id", "deals_thread_id"):
        ids.setdefault(k, 0)
    print(f"[FORUM] Reused existing customer forum {forum.id} for {display_name} "
          f"(no duplicate created).")
    return ids


async def create_customer_forum(
    guild: discord.Guild,
    bot_member: discord.Member,
    customer_member: discord.Member,
    display_name: str,
    vip: bool = False,
    admin_role: Optional[discord.Role] = None,
) -> dict[str, int]:
    """Create a private forum channel for a customer with the required threads.

    V8 bug-fix E: when the customer already has a forum inside the Customer Hub
    (e.g. /admin activate is run twice, or a re-activation after expiry), the
    existing forum + threads are REUSED instead of creating duplicates.

    Returns a dict with keys: forum_id, control_thread_id, dashboard_thread_id,
    logs_thread_id, dm_thread_id, deals_thread_id.
    All values are Discord snowflake IDs (int).
    """
    category = await _get_or_create_category(
        guild, CUSTOMER_HUB_CATEGORY, bot_member, admin_role
    )

    # Reuse an existing forum for this customer (no duplicates).
    existing_forum = await find_customer_forum(guild, customer_member, display_name)
    if existing_forum is not None:
        return await _collect_or_create_threads(existing_forum, vip, display_name)

    # Forum channel overwrites: only the customer and admins/bot can see it
    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        bot_member: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            manage_channels=True,
            manage_messages=True,
            create_public_threads=True,
            create_private_threads=True,
        ),
        customer_member: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
        ),
    }
    if admin_role:
        overwrites[admin_role] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            manage_messages=True,
        )

    forum: discord.ForumChannel = await guild.create_forum(
        name=display_name,
        category=category,
        overwrites=overwrites,
        reason=f"V8 customer forum: {display_name}",
    )

    ids: dict[str, int] = {"forum_id": forum.id}

    # Create the standard threads
    thread_map = [
        ("control", "control"),
        ("dashboard", "dashboard"),
        ("logs", "farm-logs"),
        ("deals", "deals"),
    ]
    if vip:
        # See _collect_or_create_threads: key "dm" → `dm_thread_id` (V8 plan
        # feature #5 — the VIP DM auto-reply watcher reads this column).
        thread_map.insert(3, ("dm", "dm-inbox"))

    for key, thread_name in thread_map:
        try:
            thread, _ = await forum.create_thread(
                name=thread_name,
                content=f"📌 **#{thread_name}** — {_thread_description(thread_name)}",
                reason=f"V8 customer thread: #{thread_name}",
            )
            ids[f"{key}_thread_id"] = thread.id
        except Exception as exc:
            print(f"[FORUM] Warning: could not create thread #{thread_name}: {exc}")
            ids[f"{key}_thread_id"] = 0

    # Ensure non-VIP keys always exist
    for k in ("control_thread_id", "dashboard_thread_id", "logs_thread_id",
              "dm_thread_id", "deals_thread_id"):
        ids.setdefault(k, 0)

    return ids


def _thread_description(name: str) -> str:
    descriptions = {
        "control":    "Run, stop, pause and manage your alts from here.",
        "dashboard":  "Live status dashboard for your ad farm.",
        "farm-logs":  "Detailed activity logs from all your alts.",
        "dm-inbox":   "Incoming DMs forwarded from your alts (VIP only).",
        "deals":      "Deal alerts and high-value match notifications.",
    }
    return descriptions.get(name, "Customer channel.")


async def delete_customer_forum(
    guild: discord.Guild,
    forum_id: int,
    reason: str = "V8 customer subscription expired",
) -> bool:
    """Delete a customer's forum channel. Returns True on success."""
    channel = guild.get_channel(forum_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(forum_id)
        except Exception:
            return False
    try:
        await channel.delete(reason=reason)
        return True
    except Exception as exc:
        print(f"[FORUM] Could not delete forum {forum_id}: {exc}")
        return False


async def make_forum_readonly(
    guild: discord.Guild,
    forum_id: int,
    customer_member: Optional[discord.Member] = None,
) -> bool:
    """Set the customer's forum to read-only (expired subscription)."""
    channel = guild.get_channel(forum_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(forum_id)
        except Exception:
            return False
    try:
        if customer_member:
            await channel.set_permissions(
                customer_member,
                view_channel=True,
                send_messages=False,
                read_message_history=True,
            )
        return True
    except Exception as exc:
        print(f"[FORUM] Could not set read-only on forum {forum_id}: {exc}")
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Phase 0.6 — permission hardening verification helpers
# ──────────────────────────────────────────────────────────────────────────────

def expected_permission_set(vip: bool = False, admin_role: Optional[discord.Role] = None) -> dict[str, set[str]]:
    """Canonical customer-forum overwrite set (allow/deny per target role)."""
    return {
        "default_role": {"deny:view_channel"},
        "bot": {
            "allow:view_channel", "allow:send_messages", "allow:manage_channels",
            "allow:manage_messages", "allow:create_public_threads",
            "allow:create_private_threads",
        },
        "customer": {
            "allow:view_channel", "allow:send_messages", "allow:read_message_history",
            "allow:attach_files",
        },
        "admin_role": {
            "allow:view_channel", "allow:send_messages", "allow:manage_messages",
        } if admin_role else set(),
    }


def _overwrite_to_set(overwrite: discord.PermissionOverwrite) -> set[str]:
    out = set()
    for perm, value in overwrite:
        if value is not None:
            # discord.py iterates PermissionOverwrite using legacy aliases:
            # view_channel is exposed as read_messages (and vice versa).
            if perm == "read_messages":
                perm = "view_channel"
            out.add(f"{'allow' if value else 'deny'}:{perm}")
    return out


def serialize_forum_permissions(forum: discord.ForumChannel) -> dict[str, Any]:
    """Human-readable permission overview of a forum channel."""
    out: dict[str, Any] = {}
    for target, overwrite in (forum.overwrites or {}).items():
        label = getattr(target, "name", None) or getattr(target, "id", None) or str(target)
        out[str(label)] = sorted(_overwrite_to_set(overwrite))
    return out


def check_forum_permission_overrides(
    forum: discord.ForumChannel,
    customer_member: Optional[discord.Member] = None,
    admin_role: Optional[discord.Role] = None,
    bot_member: Optional[discord.Member] = None,
) -> dict[str, Any]:
    """Diff a customer forum's overwrites against the expected set (0.6).

    Returns ``{"ok": bool, "mismatches": [str, ...]}``.  "ok" simply means no
    *unexpected* discrepancy; missing bot manager permissions are flagged too.
    """
    expected = expected_permission_set(admin_role=admin_role)
    actual: dict[str, list[str]] = {}

    def _label(target: Any) -> str:
        is_default = getattr(target, "is_default", lambda: False)
        if (callable(is_default) and is_default()) or getattr(target, "name", "") == "@everyone":
            return "default_role"
        if bot_member is not None and getattr(target, "id", None) == bot_member.id:
            return "bot"
        if customer_member is not None and getattr(target, "id", None) == customer_member.id:
            return f"customer:{customer_member.id}"
        if admin_role is not None and getattr(target, "id", None) == admin_role.id:
            return f"admin:{admin_role.name}"
        return str(target)

    for target, overwrite in (forum.overwrites or {}).items():
        actual[_label(target)] = sorted(_overwrite_to_set(overwrite))

    mismatches: list[str] = []

    def _defected(label: str, want: set[str]) -> None:
        got = set(actual.get(str(label), []))
        missing = want - got
        extra = got - want
        if missing:
            mismatches.append(f"{label}: missing {sorted(missing)}")
        if extra:
            mismatches.append(f"{label}: unexpected {sorted(extra)}")

    _defected("default_role", expected["default_role"])
    _defected("bot", expected["bot"])
    if customer_member is not None:
        _defected(f"customer:{customer_member.id}", expected["customer"])
    if admin_role is not None:
        _defected(f"admin:{admin_role.name}", expected["admin_role"])
    return {"ok": not mismatches, "mismatches": mismatches, "actual": actual}


async def startup_forum_permission_self_check(bot: Any, guild: Optional[discord.Guild] = None) -> dict[str, Any]:
    """Iterate every customer forum, diff overrides, alert on mismatch (0.6)."""
    import os as _os
    import customer_manager as _cm
    report: dict[str, Any] = {"checked": 0, "problems": []}
    guild_id = _os.environ.get("GUILD_ID", "")
    if guild is None and guild_id:
        guild = bot.get_guild(int(guild_id))
    if guild is None:
        report["error"] = "no guild available — self-check skipped"
        return report
    admin_role = discord.utils.get(guild.roles, name="Admin")
    for customer in _cm.list_customers(active_only=False):
        forum_id = customer.get("forum_id", "")
        if not forum_id or forum_id == "0":
            continue
        try:
            forum = guild.get_channel(int(forum_id))
            if forum is None:
                forum = await guild.fetch_channel(int(forum_id))
            member = guild.get_member(int(customer["discord_id"]))
            res = check_forum_permission_overrides(forum, member, admin_role)
            report["checked"] += 1
            if not res["ok"]:
                report["problems"].append({
                    "customer": customer["discord_id"],
                    "forum": forum_id,
                    "mismatches": res["mismatches"],
                })
        except Exception as exc:
            report["problems"].append({
                "customer": customer["discord_id"],
                "forum": forum_id,
                "mismatches": [f"check error: {exc}"],
            })
    return report


async def assert_forum_owner(inter: discord.Interaction, customer: dict[str, Any]) -> bool:
    """Phase 0.6: verify the requester is the forum owner before re-setup.

    The owner is the member whose ID appears as an allowed overwrite on the
    customer's forum channel (threads are bot-owned, so thread owner_id cannot
    be used). Returns True and sends an ephemeral denial otherwise.
    """
    forum_id = str(customer.get("forum_id", "") or "")
    if not forum_id or forum_id == "0":
        return True  # nothing to assert
    try:
        guild = inter.guild or inter.client.get_guild(int(_os.environ.get("GUILD_ID", "0") or 0))
        if guild is None:
            return True
        forum = guild.get_channel(int(forum_id))
        if forum is None:
            forum = await guild.fetch_channel(int(forum_id))
        member = guild.get_member(inter.user.id)
        overwrite = (forum.overwrites or {}).get(member)
        allowed = overwrite is not None and bool(overwrite.view_channel)
        if not allowed:
            await inter.response.send_message(
                "🔒 This command may only be used in your own customer forum.",
                ephemeral=True,
            )
            return False
        return True
    except Exception as exc:
        print(f"[FORUM] owner assertion failed closed: {exc}")
        try:
            await inter.response.send_message(
                "🔒 Could not verify forum ownership; ask an admin.", ephemeral=True
            )
        except Exception:
            pass
        return False


async def create_public_channels(
    guild: discord.Guild,
    bot_member: discord.Member,
    admin_role: Optional[discord.Role] = None,
) -> dict[str, int]:
    """Create V8 standard public and staff channels on the master server.

    Returns a dict of channel_name -> channel_id for newly created channels.
    Skips channels that already exist.
    """
    created: dict[str, int] = {}

    public_channels = [
        "welcome-about",
        "pricing-plans",
        "whats-new",
        "open-ticket",
        "general-chat",
    ]
    staff_channels = [
        "admin-commands",
        "admin-chat",
        "audit-logs",
    ]

    # Public channels
    public_overwrite: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
        ),
        bot_member: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            manage_messages=True,
        ),
    }

    for name in public_channels:
        if discord.utils.get(guild.text_channels, name=name):
            continue
        try:
            ch = await guild.create_text_channel(
                name,
                overwrites=public_overwrite,
                reason="V8 master server setup",
            )
            created[name] = ch.id
        except Exception as exc:
            print(f"[FORUM] Could not create #{name}: {exc}")

    # Staff-only channels
    staff_overwrite: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        bot_member: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            manage_messages=True,
        ),
    }
    if admin_role:
        staff_overwrite[admin_role] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
        )

    for name in staff_channels:
        if discord.utils.get(guild.text_channels, name=name):
            continue
        try:
            ch = await guild.create_text_channel(
                name,
                overwrites=staff_overwrite,
                reason="V8 master server setup (staff only)",
            )
            created[name] = ch.id
        except Exception as exc:
            print(f"[FORUM] Could not create staff channel #{name}: {exc}")

    return created
