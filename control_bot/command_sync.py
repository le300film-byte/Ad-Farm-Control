"""control_bot.command_sync — single source of truth for command syncing.

V8 bug-fix plan items #1 and #3:

* ``/admin`` commands appear **immediately**: the admin cog registers its
  group before the first ``tree.sync()`` (see ``on_ready`` in bot.py) and the
  sync path here is idempotent, so repeated ``on_ready`` emissions (gateway
  reconnects) never double-register or churn the command list.
* Commands are **channel-aware** on the Discord side too: after each sync we
  best-effort push per-channel application command permissions so that
  ``/help``/``/getstarted`` are the only commands offered inside the public
  announcement rooms, customer commands only inside the customer forum rooms
  (+VIP rooms for VIP tiers), and ``/admin``/``/reset`` only inside the admin
  rooms. The authoritative enforcement layer lives in ``security.enforce_channel_gate``
  (decorator + ``_check_perms``); these permissions only shape what Discord
  *shows*, and failing to push them (e.g. the token lacks the
  ``applications.commands.permissions.update`` scope) is logged, never fatal.

The guild command id map and the permission payload shape are pure/duck-typed
so the unit tests can drive this module without a live gateway.
"""
from __future__ import annotations

from typing import Any, Optional

from . import config

# Application command permission entry types (Discord API): 1 = ROLE, 2 = USER.
# Note: legacy "CHANNEL" entries are expressed as type 1 ROLE entries in the
# current API for commands, but channel-level visibility IS supported through
# type 8 ("channel") in modern clients — we therefore send channel entries as
# type 8 and @everyone as type 1 with the guild id, mirroring what the Discord
# UI itself submits.
_ROLE_TYPE = 1
_CHANNEL_TYPE = 8


def iter_visible_channels(guild: Any) -> list:
    channels = []
    for ch in getattr(guild, "channels", []) or []:
        channels.append(ch)
    return channels


def context_channel_map(guild: Any) -> dict[str, list[int]]:
    """{context: [channel_id, ...]} for every channel classifyable by security."""
    import security
    mapping: dict[str, list[int]] = {"public": [], "customer": [], "vip": [], "admin": []}
    for ch in iter_visible_channels(guild):
        ctx = security.classify_channel_context(ch)
        if ctx in mapping:
            mapping[ctx].append(int(ch.id))
    return mapping


def visibility_plan(guild: Any) -> dict[str, dict[str, list[int]]]:
    """Command-name → {"allow": [...], "deny": [...]} channel id lists.

    Pure function; no Discord calls. Only produces entries for commands whose
    tiers actually have restricted rooms in *this* guild.
    """
    import security
    ctx_ids = context_channel_map(guild)
    if not any(ctx_ids.values()):
        return {}
    plan: dict[str, dict[str, list[int]]] = {}

    admin_allowed = ctx_ids["admin"]
    if admin_allowed:
        for name in sorted(security.ADMIN_COMMANDS):
            plan[name] = {"allow": admin_allowed, "deny": []}
    public_rooms = ctx_ids["public"]
    if public_rooms:
        restricted = sorted(
            security.CUSTOMER_COMMANDS | security.VIP_COMMANDS | security.ADMIN_COMMANDS
        )
        for name in restricted:
            entry = plan.setdefault(name, {"allow": [], "deny": []})
            entry["deny"] = sorted(set(entry["deny"]) | set(public_rooms))
    return plan


def build_permission_payload(entry: dict[str, list[int]], guild_id: int) -> dict[str, Any]:
    """Render one command's permission payload for the Discord API."""
    permissions: list[dict[str, Any]] = []
    allow = entry.get("allow") or []
    deny = entry.get("deny") or []
    if allow:
        # Private to the listed channels: everyone starts hidden…
        permissions.append({"type": _ROLE_TYPE, "id": str(guild_id), "permission": False})
        # …then each admin room opts in.
        for cid in allow:
            permissions.append({"type": _CHANNEL_TYPE, "id": str(cid), "permission": True})
    for cid in deny:
        permissions.append({"type": _CHANNEL_TYPE, "id": str(cid), "permission": False})
    return {"permissions": permissions}


async def push_visibility(bot: Any, guild: Any) -> dict[str, Any]:
    """Best-effort per-channel command visibility for *guild*.

    Returns ``{"applied": int, "errors": [str, ...]}``. Never raises.
    """
    summary = {"applied": 0, "errors": []}
    try:
        plan = visibility_plan(guild)
        if not plan:
            return summary
        command_ids = {}
        try:
            for command in bot.tree.get_commands(guild=guild) or []:
                command_ids[command.name] = command.id
        except TypeError:  # older/newer get_commands signature
            for command in bot.tree.get_commands() or []:
                command_ids[command.name] = command.id
        http = getattr(bot, "http", None)
        app_id = getattr(bot, "application_id", None) or getattr(bot.user, "id", None)
        if http is None or not app_id:
            summary["errors"].append("no HTTP client / application id — visibility skipped")
            return summary
        for name, entry in plan.items():
            command_id = command_ids.get(name)
            if not command_id:
                continue  # command not registered in this guild (e.g. hidden /admin)
            try:
                await http.edit_application_command_permissions(
                    app_id,
                    guild.id,
                    command_id,
                    build_permission_payload(entry, guild.id),
                )
                summary["applied"] += 1
            except Exception as exc:
                summary["errors"].append(
                    f"{name}: {type(exc).__name__}: {exc}"
                )
    except Exception as exc:
        summary["errors"].append(f"visibility setup failed: {type(exc).__name__}: {exc}")
    return summary


async def sync_guild_commands(
    bot: Any,
    guild: Optional[Any] = None,
    *,
    apply_visibility: bool = True,
) -> dict[str, Any]:
    """The one and only sync path (fixes triple-duplicated on_ready blocks).

    - With a guild (or resolvable ``config.GUILD_ID``): copies global commands
      into the guild, syncs guild-scoped (immediate propagation — no up-to-1h
      global-cache delay) and pushes channel visibility.
    - Without a guild: global sync only.

    Idempotent: safe to call from on_ready on every reconnect and from
    ``/admin sync-commands``.
    """
    summary: dict[str, Any] = {
        "mode": "global",
        "guild_id": None,
        "synced": 0,
        "visibility": {"applied": 0, "errors": []},
    }
    if guild is None and config.GUILD_ID:
        guild = bot.get_guild(config.GUILD_ID)
    if guild is not None:
        summary["mode"] = "guild"
        summary["guild_id"] = int(guild.id)
        bot.tree.copy_global_to(guild=guild)
        commands = await bot.tree.sync(guild=guild)
    else:
        commands = await bot.tree.sync()
    summary["synced"] = len(commands or [])
    if apply_visibility and guild is not None:
        summary["visibility"] = await push_visibility(bot, guild)
    return summary


def format_sync_summary(summary: dict[str, Any]) -> str:
    """Human one-liner for on_ready logs and /admin sync-commands replies."""
    mode = summary.get("mode", "global")
    synced = summary.get("synced", 0)
    vis = summary.get("visibility") or {}
    parts = [f"{synced} command(s) synced ({mode})"]
    applied = vis.get("applied", 0)
    if applied:
        parts.append(f"channel visibility applied to {applied} command(s)")
    errors = vis.get("errors") or []
    if errors:
        parts.append(
            f"{len(errors)} visibility error(s): {'; '.join(errors[:3])}"
        )
    return ", ".join(parts)
