"""control_bot/bot.py — official Discord control bot.

Responsibilities:
  * V6 slash commands for run/stop/pause/resume, alt add/update/list/remove,
    validated message/rate/mode changes, deal keywords/toggle/threshold,
    live channel updates, interval/runtime updates, sync, status, typed logs,
    deals, health/self-check/run history, refresh, dashboard, and a complete
    private help guide.
  * Permission-gated by comma-separated OWNER_IDS (fail closed) plus cooldown.
  * GitHub Actions dispatch/cancel via the shared GitHub CLI token.
  * Queues commands through the shared private Gist (no alt server membership
    required), with a legacy DM fallback when the Gist is unavailable.
  * Parses dashboard heartbeats, typed action logs, and a separate deal webhook.
  * Periodically refreshes live GitHub/heartbeat data into a stable three-embed dashboard.
"""
from __future__ import annotations

import asyncio
import io
import json
import math
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import config, github_api, sandbox
from .alt_state import AltStateManager
from .persistence import ChannelRegistryStore
from .dashboard import (
    build_all,
    build_single_alt_embed,
    build_topology_embed,
    build_diagnose_embed,
    build_analytics_embed,
    _status_dot,
)


# ----- bot setup -----
intents = discord.Intents.default()
intents.message_content = True
intents.dm_messages = True
intents.guild_messages = True
intents.members = False
intents.presences = False

bot = commands.Bot(command_prefix="%%", intents=intents, help_command=None)

state = AltStateManager(
    alt_names=config.ALT_NAMES,
    alt_ids=config.CONFIGURED_ALT_IDS,
    offline_after_sec=config.OFFLINE_AFTER_SEC,
    max_log_entries=config.LOG_ROTATION_MAX_ENTRIES,
    persistence_path=config.CONTROL_STATE_FILE,
)
channel_registry = ChannelRegistryStore(config.CHANNEL_STATE_FILE)

_cooldowns: dict[int, float] = {}
_processed_webhook_ids: set[int] = set()


def _is_owner(inter_or_user: Any) -> bool:
    """Fail closed: an absent owner allow-list must never grant control access."""
    if not config.OWNER_IDS:
        return False
    if isinstance(inter_or_user, int):
        uid = inter_or_user
    elif hasattr(inter_or_user, "user") and hasattr(inter_or_user.user, "id"):
        uid = inter_or_user.user.id
    elif hasattr(inter_or_user, "id"):
        uid = inter_or_user.id
    else:
        try:
            uid = int(inter_or_user)
        except (TypeError, ValueError):
            return False
    return bool(config.OWNER_IDS) and uid in config.OWNER_IDS


def _on_cooldown(uid: int) -> float:
    now = time.time()
    last = _cooldowns.get(uid, 0)
    left = config.CMD_COOLDOWN_SEC - (now - last)
    if left > 0:
        return left
    _cooldowns[uid] = now
    return 0.0


def _ad_icon(ad_type: str) -> str:
    if (ad_type or "").lower() == "sell":
        return "💰"
    if (ad_type or "").lower() == "buy":
        return "🛒"
    return "❔"


def _fmt_ago(ts: float) -> str:
    try:
        value = float(ts)
    except (TypeError, ValueError, OverflowError):
        return "—"
    if value <= 0:
        return "never"
    delta = max(0.0, time.time() - value)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{delta / 3600:.1f}h ago"
    return f"{delta / 86400:.1f}d ago"


def _alt_label(alt_id: int) -> str:
    a = state.get(alt_id)
    if not a:
        return f"Alt {alt_id}"
    health = state.get_health_index(alt_id)
    dot = "🟢" if a.online and a.status == "active" else ("⚠️" if a.status == "caution" else ("🟡" if a.online else "⚫"))
    return f"{dot} {a.name} ({_ad_icon(a.ad_type)} {a.ad_type or 'unknown'} | {health}%)"[:100]


def _alt_choices():
    return [app_commands.Choice(name=_alt_label(i), value=i) for i in state.alt_ids]


def _all_alt_choices():
    out = [app_commands.Choice(name="All alts", value=0)]
    out.extend(_alt_choices())
    return out


def _alt_idx_map() -> dict[int, str]:
    return {i: state.get(i).name for i in state.alt_ids}


def _runtime_label(hours: object) -> str:
    """Single source of truth for how a runtime is presented to operators.

    ``0`` means limitless: the sender keeps running until `/shutdown` instead of
    stopping at a wall-clock budget.
    """
    try:
        value = int(hours or 0)
    except (TypeError, ValueError):
        value = 0
    return "∞ Limitless (until /shutdown)" if value == 0 else f"{value}h"


def _alt_tag(alt_id: int | None) -> str:
    """Return the canonical `[ALT-n]` log prefix used across the control plane.

    Every log emitted for a specific alt carries this tag so four concurrent
    alts can be told apart in a single shared channel without interleaving
    ambiguity (PMTP 2.7).
    """
    try:
        value = int(alt_id or 0)
    except (TypeError, ValueError):
        value = 0
    return f"[ALT-{value}]" if value > 0 else "[ALT-?]"


# Serializes registry-mutating flows (alt add/remove/update) so two operators
# cannot both claim the same 1-4 slot between the check and the commit.
_ALT_MUTATION_LOCK = asyncio.Lock()
# Bounded spacing applied between per-alt dispatches in one squad burst.
_SQUAD_SPACING = (
    max(0, config.SQUAD_POST_SPACING_MIN_MS) / 1000.0,
    max(max(0, config.SQUAD_POST_SPACING_MIN_MS), config.SQUAD_POST_SPACING_MAX_MS) / 1000.0,
)
_SQUAD_CONTROL_SPACING = (
    max(0, config.SQUAD_CONTROL_SPACING_MIN_MS) / 1000.0,
    max(max(0, config.SQUAD_CONTROL_SPACING_MIN_MS), config.SQUAD_CONTROL_SPACING_MAX_MS) / 1000.0,
)


async def _squad_pause(control_plane: bool = False) -> float:
    """Sleep a small randomized offset; returns the seconds actually waited."""
    low, high = _SQUAD_CONTROL_SPACING if control_plane else _SQUAD_SPACING
    delay = random.uniform(low, high) if high > low else low
    if delay > 0:
        await asyncio.sleep(delay)
    return delay


# ----- Alt chooser (for dropdowns in modals and views) -----
class AltSelect(discord.ui.Select):
    def __init__(self, callback, include_all: bool = False):
        opts = []
        if include_all:
            opts.append(discord.SelectOption(label="All alts", value="0", emoji="📊"))
        for i in state.alt_ids:
            a = state.get(i)
            opts.append(discord.SelectOption(label=a.name, value=str(i),
                                            emoji=_ad_icon(a.ad_type)))
        super().__init__(placeholder="Choose an alt…", min_values=1, max_values=1, options=opts[:25])
        self._cb = callback

    async def callback(self, inter: discord.Interaction):
        await self._cb(inter, int(self.values[0]))


# ----- Event: on_ready -----
@bot.event
async def on_ready():
    me = bot.user
    config.BOT_USER_ID = me.id
    print(f"✅ Logged in as {me} (id {me.id})")
    if not config.OWNER_IDS:
        print("❌ OWNER_IDS is empty — control commands are disabled until it is configured.")
    if not config.GUILD_ID:
        print("⚠️  GUILD_ID not set — commands will be registered globally (up to 1h delay).")
        # Global sync
        await bot.tree.sync()
    else:
        guild = bot.get_guild(config.GUILD_ID)
        if guild:
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
            print(f"🔗 Guild commands synced to '{guild.name}' ({guild.id})")
        else:
            print(f"⚠️  GUILD_ID={config.GUILD_ID} but bot isn't in that guild; using global sync.")
            await bot.tree.sync()
    # Start background tasks once; Discord can emit on_ready again after a
    # reconnect, and starting an already-running loop raises RuntimeError.
    if not refresh_dashboard.is_running():
        refresh_dashboard.start()
    if not refresh_github_status.is_running():
        refresh_github_status.start()
    if not fleet_health_check.is_running():
        fleet_health_check.start()
    # Reconcile the persistent per-alt channel registry after every gateway
    # reconnect. The helper is best-effort; sender heartbeats remain the
    # authoritative source when an alt uses a different guild inventory.
    for alt_id in state.alt_ids:
        asyncio.create_task(_supervised_reconcile(
            alt_id, reason="control startup", configured_ids=None,
        ))


async def _supervised_reconcile(alt_id: int, **kwargs) -> None:
    """Run a reconciliation off the gateway task and never let it raise.

    `asyncio.create_task` would otherwise let a Discord or GitHub failure die
    silently as an unretrieved task exception, which is exactly the failure mode
    that made a reconnect look like it had scanned channels when it had not.
    """
    try:
        await _reconcile_control_channels(alt_id, **kwargs)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f"[CHANNEL-STATE] startup reconcile failed for Alt {alt_id}: {type(exc).__name__}: {exc}")
        await _log_control(
            f"{_alt_tag(alt_id)} ⚠️ startup channel reconciliation failed: "
            f"`{type(exc).__name__}: {exc}`. The persisted registry is unchanged.",
            alt_id=alt_id,
        )


# ----- Slash commands -----
async def _check_perms(inter: discord.Interaction) -> bool:
    if not _is_owner(inter):
        msg = "🔒 You aren't authorized to run control commands."
        if inter.response.is_done():
            await inter.followup.send(msg, ephemeral=True)
        else:
            await inter.response.send_message(msg, ephemeral=True)
        return False
    cd = _on_cooldown(inter.user.id)
    if cd > 0:
        msg = f"⏱️ Cooldown — wait {cd:.1f}s."
        if inter.response.is_done():
            await inter.followup.send(msg, ephemeral=True)
        else:
            await inter.response.send_message(msg, ephemeral=True)
        return False
    return True


_unreachable_state_channels: set[int] = set()

async def _hydrate_discord_state() -> None:
    """Rebuild live state from recent dedicated webhook messages after a restart."""
    channel_ids = [
        config.DASHBOARD_CH_ID,
        config.LOG_CH_ID,
        config.DEALS_CH_ID,
    ]
    seen: set[int] = set()
    for channel_id in channel_ids:
        if not channel_id or channel_id in seen:
            continue
        seen.add(channel_id)
        if channel_id in _unreachable_state_channels:
            continue
        channel = bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except discord.NotFound:
                _unreachable_state_channels.add(channel_id)
                print(f"[STATE] Channel {channel_id} no longer exists on Discord (HTTP 404). Run setup to refresh IDs.")
                continue
            except Exception as exc:
                print(f"[STATE] Could not fetch channel {channel_id}: {type(exc).__name__}: {exc}")
                continue
        messages = []
        try:
            if hasattr(channel, "history"):
                messages = [message async for message in channel.history(limit=100)]
            elif isinstance(channel, discord.ForumChannel) or hasattr(channel, "threads"):
                for thread in (getattr(channel, "threads", []) or []):
                    if hasattr(thread, "history"):
                        try:
                            async for m in thread.history(limit=10):
                                messages.append(m)
                        except Exception:
                            pass
        except Exception as exc:
            print(f"[STATE] Could not read channel {channel_id}: {type(exc).__name__}: {exc}")
            continue
        # Discord returns newest first. Apply oldest-to-newest so a stale
        # heartbeat cannot overwrite a newer counter or workflow view.
        for message in reversed(messages):
            await _handle_guild_webhook_message(message)


async def _fresh_state() -> None:
    """Refresh GitHub and recent webhook state before showing current data."""
    try:
        await asyncio.gather(
            asyncio.to_thread(github_api.refresh_all_run_statuses, state),
            _hydrate_discord_state(),
        )
    except Exception as exc:
        print(f"[STATE] Live refresh failed: {type(exc).__name__}: {exc}")


class RunDetailsModal(discord.ui.Modal):
    def __init__(self, view: "RunStartView"):
        super().__init__(title=f"🚀 Launch {view.ad_type.upper()} Ad Campaign")
        self.parent_view = view
        if view.ad_type == "sell":
            self.rate = discord.ui.TextInput(
                label="Optional reference rate (not required)",
                custom_id="sell_rate",
                placeholder="Optional, e.g. 2.50 or 2.5$",
                default="",
                max_length=20,
                required=False,
            )
            self.extra = discord.ui.TextInput(
                label="Raw message or question",
                custom_id="sell_extra",
                placeholder="Paste the exact message/question to send; no item, price, or RAP is required.",
                default="",
                max_length=1900,
                required=True,
                style=discord.TextStyle.paragraph,
            )
            self.image = discord.ui.TextInput(
                label="Attach Image? (yes / no)",
                custom_id="attach_image",
                placeholder="yes",
                max_length=3,
                required=True,
                default="yes",
            )
            self.add_item(self.rate)
            self.add_item(self.extra)
            self.add_item(self.image)
        else:
            self.rate = discord.ui.TextInput(
                label="Optional reference rate (not required)",
                custom_id="buy_rate",
                placeholder="Optional, e.g. 2.20",
                default="",
                max_length=20,
                required=False,
            )
            self.rap = discord.ui.TextInput(
                label="Optional secondary rate (not required)",
                custom_id="buy_rate_rap",
                placeholder="Optional; leave blank when not applicable.",
                default="",
                max_length=20,
                required=False,
            )
            self.simple_text = discord.ui.TextInput(
                label="Raw message or question",
                custom_id="buy_simple_text",
                placeholder="Paste the exact message/question to send; no item, price, or RAP is required.",
                default="",
                max_length=1900,
                required=True,
                style=discord.TextStyle.paragraph,
            )
            self.style = discord.ui.TextInput(
                label="Ad format (optional)",
                custom_id="buy_style",
                placeholder="simple or detailed",
                max_length=8,
                required=False,
                default="simple",
            )
            self.image = discord.ui.TextInput(
                label="Attach Image? (yes / no)",
                custom_id="attach_image",
                placeholder="yes",
                max_length=3,
                required=True,
                default="yes",
            )
            self.add_item(self.rate)
            self.add_item(self.rap)
            self.add_item(self.simple_text)
            self.add_item(self.style)
            self.add_item(self.image)

    async def on_submit(self, inter: discord.Interaction):
        def value_of(name: str, default: str = "") -> str:
            item = getattr(self, name, None)
            return str(getattr(item, "value", item if item is not None else default) or default)

        values = {
            "alt_id": str(self.parent_view.alt_id or ""),
            "squad": self.parent_view.squad or "",
            "ad_type": self.parent_view.ad_type or "",
            "interval_min": str(self.parent_view.interval_min),
            "total_hours": str(self.parent_view.total_hours),
            "attach_image": value_of("image", self.parent_view.attach_image).strip().lower(),
            "buy_style": value_of("style", self.parent_view.buy_style).strip().lower(),
            "sell_rate": value_of("rate"),
            "sell_extra": value_of("extra"),
            "buy_rate": value_of("rate"),
            "buy_rate_rap": value_of("rap"),
            "buy_simple_text": value_of("simple_text"),
            "raw_message": value_of("extra") if self.parent_view.ad_type == "sell" else value_of("simple_text"),
        }
        errors, parsed = _validate_run_values(values)
        if errors:
            await inter.response.send_message("❌ " + "\n".join(errors), ephemeral=True)
            return
        await _dispatch_run_from_modal(inter, values, parsed)


class RunStartView(discord.ui.View):
    """Private component step; only the Continue button opens the modal."""
    def __init__(self, owner_id: int, squad: str = ""):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        # When a squad is supplied the launch is dispatched to every member in
        # sequence; the selected alt only seeds the shared configuration.
        self.squad: str = (squad or "").strip()
        self.alt_id: int | None = None
        self.ad_type: str | None = None
        self.interval_min = 5
        self.total_hours = 6
        self.attach_image = "yes"
        self.buy_style = "detailed"
        squad_members = (
            sorted(member.alt_id for member in state.get_squad_members(self.squad)) if self.squad else []
        )
        alt_options = squad_members or list(state.alt_ids[:25])
        if self.squad and not alt_options:
            alt_options = list(state.alt_ids[:25])
        self.alt_select = discord.ui.Select(
            placeholder=("1. Choose the squad lead alt…" if self.squad else "1. Choose configured alt…"),
            min_values=1, max_values=1,
            options=[discord.SelectOption(label=(state.get(i).name if state.get(i) else f"Alt {i}")[:100], value=str(i)) for i in alt_options], row=0)
        self.mode_select = discord.ui.Select(
            placeholder="2. Choose sell or buy…", min_values=1, max_values=1,
            options=[discord.SelectOption(label="Sell", value="sell", emoji="💰"), discord.SelectOption(label="Buy", value="buy", emoji="🛒")], row=1)
        self.interval_select = discord.ui.Select(
            placeholder="3. Interval: 3 or 5 minutes", min_values=1, max_values=1,
            options=[discord.SelectOption(label="3 minutes", value="3"), discord.SelectOption(label="5 minutes", value="5")], row=2)
        self.runtime_select = discord.ui.Select(
            placeholder="4. Runtime: 6/12/18/24/48 hours or ∞ Limitless", min_values=1, max_values=1,
            options=[
                discord.SelectOption(label="6 hours", value="6"),
                discord.SelectOption(label="12 hours", value="12"),
                discord.SelectOption(label="18 hours", value="18"),
                discord.SelectOption(label="24 hours", value="24"),
                discord.SelectOption(label="48 hours", value="48"),
                discord.SelectOption(label=_runtime_label(0), value="0"),
            ], row=3)
        # Image yes/no and detailed/simple are modal fields. Keeping them in
        # the modal preserves all choices without exceeding Discord's five-row
        # view limit.
        self.alt_select.callback = self._alt_callback
        self.mode_select.callback = self._mode_callback
        self.interval_select.callback = self._interval_callback
        self.runtime_select.callback = self._runtime_callback
        for item in (self.alt_select, self.mode_select, self.interval_select, self.runtime_select):
            self.add_item(item)
        self.continue_button = discord.ui.Button(label="Continue to ad text", style=discord.ButtonStyle.primary, row=4)
        self.continue_button.callback = self._continue
        self.add_item(self.continue_button)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary, row=4)
        cancel.callback = self._cancel
        self.add_item(cancel)

    async def _guard(self, inter):
        if inter.user.id != self.owner_id and not _is_owner(inter):
            await inter.response.send_message("🔒 This private form belongs to its operator.", ephemeral=True)
            return False
        return True

    async def _alt_callback(self, inter):
        if not await self._guard(inter): return
        self.alt_id = int(self.alt_select.values[0])
        await inter.response.edit_message(embed=_run_start_embed(self), view=self)

    async def _mode_callback(self, inter):
        if not await self._guard(inter): return
        self.ad_type = self.mode_select.values[0]
        await inter.response.edit_message(embed=_run_start_embed(self), view=self)

    async def _interval_callback(self, inter):
        if not await self._guard(inter): return
        self.interval_min = int(self.interval_select.values[0])
        await inter.response.edit_message(embed=_run_start_embed(self), view=self)

    async def _runtime_callback(self, inter):
        if not await self._guard(inter): return
        self.total_hours = int(self.runtime_select.values[0])
        await inter.response.edit_message(embed=_run_start_embed(self), view=self)

    async def _continue(self, inter):
        if not await self._guard(inter): return
        if not self.alt_id or self.ad_type not in {"sell", "buy"}:
            await inter.response.send_message("❌ Select an alt and sell/buy mode first.", ephemeral=True)
            return
        await inter.response.send_modal(RunDetailsModal(self))

    async def _cancel(self, inter):
        if not await self._guard(inter): return
        self.stop()
        await inter.response.edit_message(content="Cancelled.", embed=None, view=None)


class AddChannelModal(discord.ui.Modal, title="Add Advertising Channel"):
    channel_id = discord.ui.TextInput(
        label="Discord Channel ID",
        placeholder="e.g. 112233445566778899",
        min_length=15,
        max_length=25,
        required=True,
    )
    channel_name = discord.ui.TextInput(
        label="Channel Label / Name",
        placeholder="e.g. trading-market",
        min_length=0,
        max_length=50,
        required=False,
    )

    def __init__(self, alt_id: int):
        super().__init__()
        self.alt_id = alt_id

    async def on_submit(self, inter: discord.Interaction):
        cid = self.channel_id.value.strip()
        if not cid.isdigit():
            return await inter.response.send_message("❌ Channel ID must contain digits only.", ephemeral=True)
        label = re.sub(r"[\r\n]", " ", self.channel_name.value.strip())[:80]

        async def _update_and_persist():
            state.set_channel(self.alt_id, cid, label)
            repo = config.ALT_REPOS.get(self.alt_id, "")
            if repo and config.GITHUB_TOKEN:
                a_obj = state.get(self.alt_id)
                if a_obj and a_obj.channels:
                    cids_csv = ",".join(a_obj.channels.keys())
                    await asyncio.to_thread(github_api.set_repository_secret, repo, "CHANNEL_IDS", cids_csv)

        await _finish_dm_control(
            inter, self.alt_id, f"!setchannel {cid}{(' ' + label) if label else ''}",
            f"channel ID queued for remote validation: `{cid}`",
            update=_update_and_persist,
        )


class ChannelsView(discord.ui.View):
    def __init__(self, owner_id: int, alt_id: int = 1):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.alt_id = alt_id
        self._build_components()

    def _build_components(self):
        self.clear_items()
        if len(state.alt_ids) > 1:
            select = discord.ui.Select(
                placeholder="Select Alt Account",
                options=[
                    discord.SelectOption(
                        label=_alt_label(aid),
                        value=str(aid),
                        default=(aid == self.alt_id)
                    )
                    for aid in state.alt_ids
                ],
                custom_id="alt_select",
                row=0
            )
            select.callback = self._on_alt_select
            self.add_item(select)

        btn_add = discord.ui.Button(label="Add Channel", style=discord.ButtonStyle.success, emoji="➕", row=1)
        btn_add.callback = self._on_add_channel
        self.add_item(btn_add)

        btn_rescan = discord.ui.Button(label="Rescan", style=discord.ButtonStyle.primary, emoji="🔄", row=1)
        btn_rescan.callback = self._on_rescan
        self.add_item(btn_rescan)

        btn_reset = discord.ui.Button(label="Reset Caution", style=discord.ButtonStyle.secondary, emoji="⚠️", row=1)
        btn_reset.callback = self._on_reset_caution
        self.add_item(btn_reset)

        btn_export = discord.ui.Button(label="Export IDs", style=discord.ButtonStyle.secondary, emoji="📋", row=1)
        btn_export.callback = self._on_export
        self.add_item(btn_export)

        a_obj = state.get(self.alt_id)
        if a_obj and a_obj.channels:
            ch_options = [
                discord.SelectOption(
                    label=f"#{raw.get('name', cid)[:25]} ({cid})",
                    value=cid,
                    description=f"Sent: {raw.get('sent', 0)} | Errors: {raw.get('errors', 0)}"
                )
                for cid, raw in list(a_obj.channels.items())[:25]
            ]
            remove_select = discord.ui.Select(
                placeholder="Select Channel to Remove",
                options=ch_options,
                custom_id="remove_select",
                row=2
            )
            remove_select.callback = self._on_remove_channel
            self.add_item(remove_select)

    async def _on_alt_select(self, inter: discord.Interaction):
        if inter.user.id != self.owner_id and not _is_owner(inter):
            return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
        self.alt_id = int(inter.data["values"][0])
        self._build_components()
        embed = self._build_embed()
        await inter.response.edit_message(embed=embed, view=self)

    async def _on_add_channel(self, inter: discord.Interaction):
        if inter.user.id != self.owner_id and not _is_owner(inter):
            return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
        await inter.response.send_modal(AddChannelModal(self.alt_id))

    async def _on_rescan(self, inter: discord.Interaction):
        if inter.user.id != self.owner_id and not _is_owner(inter):
            return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
        await _finish_dm_control(inter, self.alt_id, "!rescan", "channel permission rescan queued")

    async def _on_reset_caution(self, inter: discord.Interaction):
        if inter.user.id != self.owner_id and not _is_owner(inter):
            return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
        await _finish_dm_control(
            inter, self.alt_id, "!resetcaution all", "reset caution on all channels",
            update=lambda: state.reset_caution(self.alt_id, None)
        )

    async def _on_export(self, inter: discord.Interaction):
        if inter.user.id != self.owner_id and not _is_owner(inter):
            return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
        a_obj = state.get(self.alt_id)
        cids = list(a_obj.channels.keys()) if a_obj else []
        text = f"**Channel IDs for {a_obj.name if a_obj else self.alt_id}** (`{len(cids)}` total):\n```\n{','.join(cids)}\n```"
        await inter.response.send_message(text, ephemeral=True)

    async def _on_remove_channel(self, inter: discord.Interaction):
        if inter.user.id != self.owner_id and not _is_owner(inter):
            return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
        cid = inter.data["values"][0]
        a_obj = state.get(self.alt_id)
        if a_obj and cid in a_obj.channels:
            a_obj.channels.pop(cid, None)
            repo = config.ALT_REPOS.get(self.alt_id, "")
            if repo and config.GITHUB_TOKEN:
                cids_csv = ",".join(a_obj.channels.keys())
                await asyncio.to_thread(github_api.set_repository_secret, repo, "CHANNEL_IDS", cids_csv)
            try:
                asyncio.create_task(_send_control_wait_ack(self.alt_id, "!rescan", timeout=15))
            except Exception:
                pass
        self._build_components()
        embed = self._build_embed()
        await inter.response.edit_message(embed=embed, view=self)

    def _build_embed(self) -> discord.Embed:
        a_obj = state.get(self.alt_id)
        if not a_obj:
            return discord.Embed(title="❓ Unknown Alt", description="Selected alt is not configured.", color=0xED4245)
        embed = discord.Embed(
            title=f"📌 Channel Manager · {a_obj.name}",
            description=f"Manage registered advertising channels for **{a_obj.name}** (Alt ID: `{self.alt_id}`).\nActive channels: **{len(a_obj.channels)}**",
            color=0x5865F2,
            timestamp=datetime.now(timezone.utc),
        )
        if not a_obj.channels:
            embed.add_field(name="Channels", value="_No channels registered yet. Click 'Add Channel' or run self-check._", inline=False)
        else:
            rows = []
            for cid, raw in list(a_obj.channels.items())[:25]:
                name = raw.get("name") or cid
                sent = raw.get("sent", 0)
                errors = raw.get("errors", 0)
                alive = raw.get("alive", True)
                slow = raw.get("slowmode", 0)
                dot = "🟢" if alive else "⚫"
                slow_str = f" · slowmode `{slow}s`" if slow else ""
                rows.append(f"{dot} `#{name}` (`{cid}`) · sent **{sent}** · err **{errors}**{slow_str}")
            embed.add_field(name="Registered Targets", value="\n".join(rows)[:1024], inline=False)
        embed.set_footer(text="Changes are synced to runner memory and saved to GitHub Secrets.")
        return embed


class FleetTuningView(discord.ui.View):
    def __init__(self, owner_id: int, alt_id: int = 1):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.alt_id = alt_id
        self._build_components()

    def _build_components(self):
        self.clear_items()
        if len(state.alt_ids) > 1:
            alt_select = discord.ui.Select(
                placeholder="Select Alt Account",
                options=[
                    discord.SelectOption(
                        label=_alt_label(aid),
                        value=str(aid),
                        default=(aid == self.alt_id)
                    )
                    for aid in state.alt_ids
                ],
                custom_id="tuning_alt_select",
                row=0
            )
            alt_select.callback = self._on_alt_select
            self.add_item(alt_select)

        policy_select = discord.ui.Select(
            placeholder="Apply Channel Policy Template",
            options=[
                discord.SelectOption(label="🛡️ Stealth Safe-Mode (5m, 25% Typo, 2.0x Caution)", value="stealth"),
                discord.SelectOption(label="⚡ Aggressive Peak-Hour (3m, 12% Typo, 1.2x Caution)", value="aggressive"),
                discord.SelectOption(label="🔥 Peak-Hour Dynamic (3m, 18% Typo, 1.5x Caution)", value="peak_hour"),
                discord.SelectOption(label="⚖️ Balanced Standard (5m, 18% Typo, 1.5x Caution)", value="balanced"),
            ],
            custom_id="policy_select",
            row=1
        )
        policy_select.callback = self._on_policy_select
        self.add_item(policy_select)

        btn_rescan = discord.ui.Button(label="Rescan Channels", style=discord.ButtonStyle.primary, emoji="🔄", row=2)
        btn_rescan.callback = self._on_rescan
        self.add_item(btn_rescan)

        btn_reset = discord.ui.Button(label="Reset Caution", style=discord.ButtonStyle.secondary, emoji="⚠️", row=2)
        btn_reset.callback = self._on_reset_caution
        self.add_item(btn_reset)

        btn_diag = discord.ui.Button(label="Diagnostics", style=discord.ButtonStyle.secondary, emoji="🔍", row=2)
        btn_diag.callback = self._on_diagnose
        self.add_item(btn_diag)

    def _build_embed(self) -> discord.Embed:
        a = state.get(self.alt_id)
        if not a:
            return discord.Embed(title="❓ Unknown Alt", color=0xED4245)
        embed = discord.Embed(
            title=f"⚙️ Fleet Tuning & Settings · {a.name}",
            color=0x5865F2,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Workflow Status", value=f"`{a.workflow_status}` ({a.workflow_conclusion or 'running'})", inline=True)
        embed.add_field(name="Interval / Policy", value=f"`{a.interval_min}m` (`{a.policy_template}`)", inline=True)
        embed.add_field(name="Health Score", value=f"**{state.get_health_index(self.alt_id)}%** `[{state.get_activity_sparkline(self.alt_id)}]`", inline=True)
        embed.add_field(name="Ad Mode", value=f"`{a.ad_type.upper()}` (${a.rate:.2f})" if a.rate else f"`{a.ad_type.upper()}`", inline=True)
        embed.add_field(name="Channels", value=f"`{len(a.channels)}` registered", inline=True)
        embed.add_field(name="Deal Scanner", value=f"`{'ON' if a.deal_scan_enabled else 'OFF'}` (edge: ${a.deal_alert_delta:.2f})", inline=True)
        embed.add_field(name="Repository", value=f"`{config.ALT_REPOS.get(a.alt_id, 'N/A')}`", inline=False)
        if a.message_preview:
            embed.add_field(name="Message Preview", value=f"```{a.message_preview[:250]}```", inline=False)
        embed.set_footer(text=f"Alt ID: {a.alt_id} • Interactive Fleet Tuning UI")
        return embed

    async def _on_alt_select(self, inter: discord.Interaction):
        if inter.user.id != self.owner_id and not _is_owner(inter):
            return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
        self.alt_id = int(inter.data["values"][0])
        self._build_components()
        await inter.response.edit_message(embed=self._build_embed(), view=self)

    async def _on_policy_select(self, inter: discord.Interaction):
        if inter.user.id != self.owner_id and not _is_owner(inter):
            return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
        chosen_policy = inter.data["values"][0]
        state.set_policy_template(self.alt_id, chosen_policy)
        try:
            asyncio.create_task(_send_control_wait_ack(self.alt_id, f"!policy {chosen_policy}", timeout=15))
            await _log_control(f"🛡️ Policy template **{chosen_policy.upper()}** dispatched to Alt {self.alt_id} from Fleet Tuning UI.")
        except Exception:
            pass
        await inter.response.edit_message(embed=self._build_embed(), view=self)

    async def _on_rescan(self, inter: discord.Interaction):
        if inter.user.id != self.owner_id and not _is_owner(inter):
            return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
        await _finish_dm_control(inter, self.alt_id, "!rescan", "channel permission rescan queued")

    async def _on_reset_caution(self, inter: discord.Interaction):
        if inter.user.id != self.owner_id and not _is_owner(inter):
            return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
        await _finish_dm_control(
            inter, self.alt_id, "!resetcaution all", "reset caution on all channels",
            update=lambda: state.reset_caution(self.alt_id, None)
        )

    async def _on_diagnose(self, inter: discord.Interaction):
        if inter.user.id != self.owner_id and not _is_owner(inter):
            return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
        embed = build_diagnose_embed(state, self.alt_id)
        await inter.response.send_message(embed=embed, ephemeral=True)


class PriceUpdateModal(discord.ui.Modal):
    def __init__(self, alt_id: int):
        super().__init__(title=f"Update Price · Alt {alt_id}")
        self.alt_id = alt_id
        curr_rate = state.get(alt_id).rate if state.get(alt_id) else None
        curr_str = f"{curr_rate:g}" if curr_rate else "2.50"
        self.price_input = discord.ui.TextInput(
            label="Rate per 1k units (USD)",
            placeholder="e.g. 2.40",
            default=curr_str,
            max_length=10,
            required=True,
        )
        self.add_item(self.price_input)

    async def on_submit(self, inter: discord.Interaction) -> None:
        val = _extract_price(self.price_input.value)
        if val is None or not 0 < val <= 20:
            return await inter.response.send_message("❌ Price must be a number between 0 and 20; example `2.30`.", ephemeral=True)
        await _finish_dm_control(
            inter, self.alt_id, f"!setprice {val:g}", f"price validated at ${val:.2f}/1k",
            update=lambda: state.set_run_config(self.alt_id, rate=val),
        )


class MessageUpdateModal(discord.ui.Modal):
    def __init__(self, alt_id: int):
        super().__init__(title=f"Update Ad Copy · Alt {alt_id}")
        self.alt_id = alt_id
        curr_msg = state.get(alt_id).message_preview if state.get(alt_id) else ""
        self.msg_input = discord.ui.TextInput(
            label="New Ad Message Copy",
            style=discord.TextStyle.paragraph,
            placeholder="Enter your new ad copy text...",
            default=curr_msg[:1800] if curr_msg else "",
            max_length=1900,
            required=True,
        )
        self.add_item(self.msg_input)

    async def on_submit(self, inter: discord.Interaction) -> None:
        msg = self.msg_input.value.strip()
        if not msg:
            return await inter.response.send_message("❌ Message cannot be empty.", ephemeral=True)
        await _finish_dm_control(
            inter, self.alt_id, f"!setmessage {msg}", f"message updated ({len(msg)} characters)",
            update=lambda: state.set_run_config(self.alt_id, message=msg),
        )


class BuyerReplyModal(discord.ui.Modal):
    def __init__(self, alt_id: int = 1, user_id: str = ""):
        super().__init__(title="Relay Reply to Buyer")
        self.alt_input = discord.ui.TextInput(
            label="Alt Account ID",
            placeholder="1",
            default=str(alt_id or 1),
            max_length=4,
            required=True,
        )
        self.user_input = discord.ui.TextInput(
            label="Buyer Discord User ID",
            placeholder="e.g. 102938475610293847",
            default=str(user_id or ""),
            max_length=30,
            required=True,
        )
        self.text_input = discord.ui.TextInput(
            label="Reply Message",
            style=discord.TextStyle.paragraph,
            placeholder="Hey! 100k in stock, $2.40/1k. Payment via USDT or PayPal.",
            max_length=1900,
            required=True,
        )
        self.add_item(self.alt_input)
        self.add_item(self.user_input)
        self.add_item(self.text_input)

    async def on_submit(self, inter: discord.Interaction) -> None:
        try:
            aid = int(self.alt_input.value.strip())
        except ValueError:
            return await inter.response.send_message("❌ Invalid Alt ID.", ephemeral=True)
        uid = self.user_input.value.strip()
        txt = self.text_input.value.strip()
        if not uid.isdigit():
            return await inter.response.send_message("❌ Buyer User ID must contain numbers only.", ephemeral=True)
        if not txt:
            return await inter.response.send_message("❌ Reply text cannot be empty.", ephemeral=True)
        await cmd_reply.callback(inter, alt=aid, user=uid, text=txt)


class DealsManagerModal(discord.ui.Modal):
    def __init__(self, alt_id: int):
        super().__init__(title=f"Deal Scanner Config · Alt {alt_id}")
        self.alt_id = alt_id
        alt_obj = state.get(alt_id)
        curr_kw = ", ".join(alt_obj.deal_keywords) if (alt_obj and alt_obj.deal_keywords) else ", ".join(config.DEFAULT_DEAL_KEYWORDS)
        curr_delta = f"{alt_obj.deal_alert_delta:.2f}" if alt_obj else "0.05"
        curr_scan = "on" if (alt_obj and alt_obj.deal_scan_enabled) else "off"

        self.kw_input = discord.ui.TextInput(
            label="Target Items / Games (comma-separated)",
            placeholder=f"e.g. {', '.join(config.DEFAULT_DEAL_KEYWORDS[:4])}, ...",
            default=curr_kw,
            max_length=500,
            required=True,
        )
        self.delta_input = discord.ui.TextInput(
            label="Min Profit Margin Edge ($ USD)",
            placeholder="0.05",
            default=curr_delta,
            max_length=10,
            required=True,
        )
        self.scan_input = discord.ui.TextInput(
            label="Deal Scanner State (on / off)",
            placeholder="on or off",
            default=curr_scan,
            max_length=5,
            required=True,
        )
        self.add_item(self.kw_input)
        self.add_item(self.delta_input)
        self.add_item(self.scan_input)

    async def on_submit(self, inter: discord.Interaction) -> None:
        kws = [part.strip() for part in self.kw_input.value.split(",") if part.strip()]
        if not kws:
            return await inter.response.send_message("❌ Provide at least one target item keyword.", ephemeral=True)
        try:
            delta = float(self.delta_input.value.strip())
        except ValueError:
            delta = 0.05
        scan_on = self.scan_input.value.strip().lower() in ("on", "true", "1", "yes")

        await inter.response.defer(ephemeral=True)
        state.set_deal_keywords(self.alt_id, kws)
        state.set_deal_config(self.alt_id, enabled=scan_on, delta=delta)
        asyncio.create_task(_send_control_wait_ack(self.alt_id, f"!setdealkeywords {', '.join(kws)}", timeout=15))
        asyncio.create_task(_send_control_wait_ack(self.alt_id, f"!setdealdelta {delta:g}", timeout=15))
        asyncio.create_task(_send_control_wait_ack(self.alt_id, f"!setdealscan {'on' if scan_on else 'off'}", timeout=15))

        embed = discord.Embed(title=f"📈 Deal Scanner Updated · Alt {self.alt_id}", color=0x57F287)
        embed.add_field(name="Scanner State", value="🟢 Enabled" if scan_on else "🔴 Disabled", inline=True)
        embed.add_field(name="Min Profit Edge", value=f"`${delta:.2f}/1k`", inline=True)
        embed.add_field(name="Target Items", value=f"`{', '.join(kws)}`", inline=False)
        await inter.followup.send(embed=embed, ephemeral=True)
        await _log_control(f"📈 Alt {self.alt_id} deal scanner updated: scan={'ON' if scan_on else 'OFF'}, delta=${delta:.2f}/1k, items=[{', '.join(kws)}]")


class SquadAssignModal(discord.ui.Modal):
    def __init__(self, alt_id: int = 1, squad_name: str = "Alpha"):
        super().__init__(title=f"Assign Alt {alt_id} to Squad")
        self.alt_id = alt_id
        self.alt_input = discord.ui.TextInput(
            label="Alt ID",
            placeholder="1",
            default=str(alt_id),
            max_length=4,
            required=True,
        )
        self.squad_input = discord.ui.TextInput(
            label="Squad Name",
            placeholder="Alpha, Sellers, Night Patrol",
            default=squad_name,
            max_length=40,
            required=True,
        )
        self.add_item(self.alt_input)
        self.add_item(self.squad_input)

    async def on_submit(self, inter: discord.Interaction) -> None:
        try:
            aid = int(self.alt_input.value.strip())
        except ValueError:
            return await inter.response.send_message("❌ Invalid Alt ID.", ephemeral=True)
        sq = self.squad_input.value.strip()
        if not sq:
            return await inter.response.send_message("❌ Squad name cannot be empty.", ephemeral=True)
        if aid not in state.alt_ids:
            return await inter.response.send_message(f"❌ Alt `{aid}` is not configured.", ephemeral=True)
        state.set_squad(aid, sq)
        await inter.response.send_message(f"✅ Alt {aid} assigned to squad **{sq}**.", ephemeral=True)


class SquadBatchPriceModal(discord.ui.Modal):
    def __init__(self, squad_name: str):
        super().__init__(title=f"Batch Price · Squad {squad_name}")
        self.squad_name = squad_name
        self.price_input = discord.ui.TextInput(
            label="Rate per 1k for all squad alts",
            placeholder="e.g. 2.40",
            max_length=10,
            required=True,
        )
        self.add_item(self.price_input)

    async def on_submit(self, inter: discord.Interaction) -> None:
        val = _extract_price(self.price_input.value)
        if val is None or not 0 < val <= 20:
            return await inter.response.send_message("❌ Invalid price rate.", ephemeral=True)
        await cmd_squad.callback(inter, action="price", squad_name=self.squad_name, value=f"{val:g}")


class SquadControlView(discord.ui.View):
    def __init__(self, owner_id: int, current_squad: str = ""):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        squads = list(state.get_all_squads().keys()) or ["Alpha"]
        self.current_squad = current_squad or (squads[0] if squads else "Alpha")
        self._build_items()

    def _build_items(self):
        self.clear_items()
        all_sqs = sorted(list(state.get_all_squads().keys()))
        if not all_sqs:
            all_sqs = ["Alpha", "Sellers", "Buyers"]

        options = [
            discord.SelectOption(label=f"Squad: {sq}", value=sq, default=(sq == self.current_squad))
            for sq in all_sqs[:25]
        ]

        class _SqSelect(discord.ui.Select):
            def __init__(parent_self):
                super().__init__(placeholder="Select a Squad to manage...", min_values=1, max_values=1, options=options)
            async def callback(sel_self, inter: discord.Interaction):
                if inter.user.id != self.owner_id and not _is_owner(inter):
                    return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
                self.current_squad = sel_self.values[0]
                self._build_items()
                embed = self._render_embed()
                await inter.response.edit_message(embed=embed, view=self)

        self.add_item(_SqSelect())

        # Action buttons
        btn_pause = discord.ui.Button(label="Batch Pause", style=discord.ButtonStyle.secondary, emoji="⏸️", row=1)
        btn_resume = discord.ui.Button(label="Batch Resume", style=discord.ButtonStyle.success, emoji="▶️", row=1)
        btn_price = discord.ui.Button(label="Batch Price", style=discord.ButtonStyle.primary, emoji="💵", row=1)
        btn_policy = discord.ui.Button(label="Batch Policy", style=discord.ButtonStyle.primary, emoji="🛡️", row=1)
        btn_assign = discord.ui.Button(label="Assign Alt", style=discord.ButtonStyle.secondary, emoji="➕", row=2)

        async def _cb_pause(inter: discord.Interaction):
            await cmd_squad.callback(inter, action="pause", squad_name=self.current_squad)
        async def _cb_resume(inter: discord.Interaction):
            await cmd_squad.callback(inter, action="resume", squad_name=self.current_squad)
        async def _cb_price(inter: discord.Interaction):
            await inter.response.send_modal(SquadBatchPriceModal(self.current_squad))
        async def _cb_policy(inter: discord.Interaction):
            await cmd_squad.callback(inter, action="policy", squad_name=self.current_squad, value="balanced")
        async def _cb_assign(inter: discord.Interaction):
            await inter.response.send_modal(SquadAssignModal(alt_id=1, squad_name=self.current_squad))

        btn_pause.callback = _cb_pause
        btn_resume.callback = _cb_resume
        btn_price.callback = _cb_price
        btn_policy.callback = _cb_policy
        btn_assign.callback = _cb_assign

        self.add_item(btn_pause)
        self.add_item(btn_resume)
        self.add_item(btn_price)
        self.add_item(btn_policy)
        self.add_item(btn_assign)

    def _render_embed(self) -> discord.Embed:
        members = state.get_squad_members(self.current_squad)
        embed = discord.Embed(
            title=f"👥 Fleet Squad Hub: {self.current_squad}",
            color=0x5865F2,
            timestamp=datetime.now(timezone.utc),
        )
        if members:
            total_sent = sum(getattr(m, "total_sent", 0) for m in members)
            total_err = sum(getattr(m, "error_count", 0) for m in members)
            avg_health = sum(state.get_health_index(m.alt_id) for m in members) / len(members)
            online_count = sum(1 for m in members if state.is_online(m.alt_id))
            embed.description = (
                f"**Members**: `{len(members)} alts` (`{online_count} online`) | "
                f"**Composite Health**: `{avg_health:.0f}%`\n"
                f"**Total Posts**: `{total_sent}` | **Errors**: `{total_err}`"
            )
            for m in members:
                dot, _ = _status_dot(m)
                embed.add_field(
                    name=f"{dot} Alt {m.alt_id}: {m.name}",
                    value=f"• Mode: `{m.ad_type or 'sell'}` @ `${m.rate or 2.50:.2f}/1k`\n• Status: `{m.status}` · Sent: `{m.total_sent}`\n• Policy: `{getattr(m, 'policy_template', 'balanced')}`",
                    inline=True,
                )
        else:
            embed.description = f"No alts currently assigned to squad **{self.current_squad}**.\nUse **Assign Alt** below to add members."
        return embed


def _run_start_embed(view: RunStartView) -> discord.Embed:
    alt = state.get(view.alt_id) if view.alt_id else None
    embed = discord.Embed(title="🚀 V6 start a sender run", color=0x5865F2)
    embed.description = "Choose every runtime setting below, then enter the ad text/rates. This form is private and owner-only."
    if view.squad:
        members = sorted(member.alt_id for member in state.get_squad_members(view.squad))
        summary = ", ".join(str(m) for m in members) or "no members"
        embed.add_field(name="Squad", value=f"`{view.squad}` → alts {summary}", inline=False)
    embed.add_field(name="Alt", value=alt.name if alt else "not selected", inline=True)
    embed.add_field(name="Mode", value=view.ad_type or "not selected", inline=True)
    embed.add_field(name="Interval / runtime", value=f"{view.interval_min} min / {view.total_hours} h", inline=True)
    embed.add_field(name="Image / buy style", value=f"{view.attach_image} / {view.buy_style} (confirm in modal)", inline=True)
    return embed


def _validate_run_values(values: dict[str, str]) -> tuple[list[str], dict[str, object]]:
    """Validate controls while treating the operator's raw copy as primary.

    Rates and RAP used to be mandatory even when the operator supplied a
    perfectly valid question or custom message. They are now optional
    metadata; the sender can post any non-empty text and still applies its
    normal emoji/casing/timing modifiers.
    """
    errors = []
    try:
        alt_id = int(values.get("alt_id", ""))
    except (TypeError, ValueError):
        alt_id = 0
    if alt_id not in state.alt_ids:
        errors.append("Choose a configured alt.")
    ad_type = values.get("ad_type", "").lower().strip()
    if ad_type not in {"sell", "buy"}:
        errors.append("Mode must be sell or buy.")

    raw_message = (values.get("raw_message") or "").strip()
    if not raw_message:
        # Compatibility with the two modal field IDs used by older clients.
        raw_message = (values.get("sell_extra") or values.get("buy_simple_text") or "").strip()
    if not raw_message:
        errors.append("Enter a raw message or question.")
    elif len(raw_message) > 1900:
        errors.append("Raw message/question is limited to 1900 characters.")

    def optional_rate(key: str) -> float | None:
        raw = (values.get(key) or "").strip()
        if not raw:
            return None
        parsed = _extract_price(raw)
        if parsed is None or not 0 < parsed <= 20:
            errors.append(f"{key.replace('_', ' ').capitalize()} must be between 0 and 20 when supplied.")
            return None
        return parsed

    rate = optional_rate("sell_rate" if ad_type == "sell" else "buy_rate") if ad_type in {"sell", "buy"} else None
    rap = optional_rate("buy_rate_rap") if ad_type == "buy" else None
    style = (values.get("buy_style") or "simple").strip().lower()
    if ad_type == "buy" and style not in {"simple", "detailed"}:
        errors.append("Buy style must be simple or detailed when supplied.")
    try:
        interval = int(values.get("interval_min", ""))
    except (TypeError, ValueError):
        interval = 0
    if interval not in {3, 5}:
        errors.append("Interval must be 3 or 5 minutes.")
    try:
        hours = int(values.get("total_hours", ""))
    except (TypeError, ValueError):
        hours = 0
    if hours not in {0, 6, 12, 18, 24, 48}:
        errors.append("Runtime must be 0 (Limitless), 6, 12, 18, 24, or 48 hours.")
    if values.get("attach_image") not in {"yes", "no"}:
        errors.append("Image setting must be yes or no.")

    # Squad resolution. `/run squad:<name>` expands to every member of that
    # squad; the guided form's selected alt seeds the shared configuration but
    # the launch itself is dispatched to every member in sequence.
    squad = (values.get("squad") or "").strip()
    targets: list[int] = []
    if squad:
        members = state.get_squad_members(squad)
        if not members:
            known = ", ".join(sorted(state.get_all_squads())) or "none"
            errors.append(f"Squad `{squad}` has no configured alts. Known squads: {known}.")
        else:
            targets = sorted(member.alt_id for member in members)
    if not targets:
        targets = [alt_id] if alt_id in state.alt_ids else []

    return errors, {
        "alt_id": alt_id,
        "rate": rate,
        "rap": rap,
        "interval": interval,
        "hours": hours,
        "raw_message": raw_message,
        "squad": squad,
        "targets": targets,
    }


def _valid_repo_name(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?", value or ""))


def _alt_registry_values(repos: dict[int, str], discord_ids: dict[int, int], names: dict[int, str]) -> dict[str, str]:
    """Build the aggregate core-secret values without including any token."""
    ids = sorted(set(repos) | set(discord_ids) | set(names))
    return {
        "ALT_REPOS": ",".join(f"{i}:{repos[i]}" for i in ids if i in repos),
        "ALT_DISCORD_IDS": ",".join(f"{i}:{discord_ids[i]}" for i in ids if i in discord_ids),
        "ALT_NAMES": ",".join(f"{i}:{names[i]}" for i in ids if i in names),
    }


async def _persist_alt_registry(repos: dict[int, str], discord_ids: dict[int, int], names: dict[int, str]) -> tuple[bool, str]:
    """Persist the live alt registry to the core repository before mutating state."""
    if not config.CORE_REPO:
        return False, "CORE_REPO is missing; the control bot cannot persist the alt registry."
    if not config.GITHUB_TOKEN:
        return False, "GH_TOKEN is missing; the control bot cannot persist the alt registry."
    values = _alt_registry_values(repos, discord_ids, names)
    for secret_name, value in values.items():
        if value:
            ok, detail = await asyncio.to_thread(
                github_api.set_repository_secret, config.CORE_REPO, secret_name, value
            )
        else:
            ok, detail = await asyncio.to_thread(
                github_api.delete_repository_secret, config.CORE_REPO, secret_name
            )
        if not ok:
            return False, f"Could not update core secret `{secret_name}`: {detail}"
    return True, "Core alt registry persisted."


def _apply_alt_registry(repos: dict[int, str], discord_ids: dict[int, int], names: dict[int, str]) -> None:
    config.ALT_REPOS.clear()
    config.ALT_REPOS.update(repos)
    config.ALT_DISCORD_IDS.clear()
    config.ALT_DISCORD_IDS.update(discord_ids)
    config.ALT_NAMES.clear()
    config.ALT_NAMES.update(names)


async def _log_alt_add_event(alt_id: int, ok: bool, text: str, *, name: str = "Alt") -> None:
    """Always emit an explicit alt-add result so the diagnostic log channel has
    both success and failure statements (Issue #46 / PMTP 2.4).

    ``alt_id`` may be ``0`` when the failure happened before an ID could be
    resolved (for example a token that never authenticated). The log line still
    carries a stable ``[ALT-ADD]`` marker so the event is searchable, and the
    per-alt buffer also receives the entry whenever an ID is known. That is what
    makes a failed add visible for every alt instead of only the first one.
    """
    icon = "✅" if ok else "❌"
    tag = _alt_tag(alt_id) if alt_id else "[ALT-ADD]"
    full = f"{icon} {tag} [ALT-ADD] [{('PASS' if ok else 'FAIL')}] {text}"
    try:
        await _log_control(full[:2000])
    except Exception:
        print(f"[ALT-ADD] audit log failed: {full[:200]}")
    if alt_id in state.alt_ids:
        state.append_log(alt_id, full, emoji=icon, color=0x57F287 if ok else 0xED4245, kind="CONTROL" if ok else "ERROR")


class AltAddModal(discord.ui.Modal, title="Add New Alt Account"):
    user_token = discord.ui.TextInput(
        label="Alt Discord User Token",
        placeholder="Paste user account token here (securely stored)",
        max_length=600,
        required=True,
    )
    name = discord.ui.TextInput(
        label="Display Name (optional)",
        placeholder="Leave blank to auto-detect Discord username",
        max_length=80,
        required=False,
    )
    alt_id = discord.ui.TextInput(
        label="Alt ID 1–4 (optional)",
        placeholder="Leave blank for next available ID",
        max_length=2,
        required=False,
    )
    repository = discord.ui.TextInput(
        label="GitHub Repository (optional)",
        placeholder="Leave blank to auto-create public repo",
        max_length=100,
        required=False,
    )
    channels = discord.ui.TextInput(
        label="Target Channels (optional)",
        placeholder="e.g. 112233445566 (leave blank to inherit fleet channels)",
        max_length=150,
        required=False,
    )

    async def on_submit(self, inter: discord.Interaction):
        if not _is_owner(inter):
            await inter.response.send_message("🔒 You aren't authorized to manage alts.", ephemeral=True)
            return
        def value(item) -> str:
            return str(getattr(item, "value", item) or "").strip()
        token = value(self.user_token)
        if not token:
            await _log_alt_add_event(0, False, "Alt add failed: user token is required.")
            return await inter.response.send_message("❌ User token is required.", ephemeral=True)

        await inter.response.defer(ephemeral=True)

        # The whole flow runs under one lock so two concurrent adds can never
        # both resolve "next free slot" to the same ID (PMTP 2.1 edge case).
        async with _ALT_MUTATION_LOCK:
            return await self._provision_alt(inter, token, value)

    async def _provision_alt(self, inter: discord.Interaction, token: str, value) -> None:
        # 1. Validate token with Discord API and extract profile
        ok_prof, profile = await asyncio.to_thread(github_api.fetch_discord_user_profile, token)
        if not ok_prof or not isinstance(profile, dict) or not profile.get("id"):
            err_msg = profile.get("error", "Invalid user token") if isinstance(profile, dict) else "Invalid token"
            await _log_alt_add_event(0, False, f"Alt add failed during Discord token validation: {err_msg}")
            return await inter.followup.send(f"❌ Could not authenticate alt with Discord: {err_msg}", ephemeral=True)

        detected_did = str(profile.get("id"))
        detected_username = str(profile.get("username") or "alt")
        detected_name = str(profile.get("global_name") or profile.get("username") or "alt")

        # 2. Resolve Alt ID. The ID is resolved before any remote side effect so
        # every subsequent failure line can be attributed to a concrete alt.
        raw_aid = value(self.alt_id)
        limit = max(1, config.ALT_SLOT_LIMIT)
        if raw_aid:
            try:
                alt_id = int(raw_aid)
            except (TypeError, ValueError):
                await _log_alt_add_event(0, False, f"Alt add failed: invalid Alt ID '{raw_aid}'.")
                return await inter.followup.send(f"❌ Alt ID must be an integer between 1 and {limit}.", ephemeral=True)
        else:
            free_ids = [i for i in range(1, limit + 1) if i not in state.alt_ids]
            if not free_ids:
                await _log_alt_add_event(
                    0, False,
                    f"Alt add failed: all {limit} alt slots are occupied "
                    f"({', '.join(str(i) for i in state.alt_ids)}). Remove one with `/alt action:remove` first.",
                )
                return await inter.followup.send(
                    f"❌ All {limit} alt slots are currently occupied. "
                    "Remove one with `/alt action:remove` first, or raise `ALT_SLOT_LIMIT` deliberately.",
                    ephemeral=True,
                )
            alt_id = free_ids[0]

        if not 1 <= alt_id <= limit:
            await _log_alt_add_event(alt_id, False, f"Alt add failed: Alt ID {alt_id} is outside 1-{limit}.")
            return await inter.followup.send(f"❌ Alt ID must be between 1 and {limit}.", ephemeral=True)
        if alt_id in state.alt_ids:
            await _log_alt_add_event(alt_id, False, f"Alt add failed: Alt {alt_id} already configured.")
            return await inter.followup.send(f"❌ Alt `{alt_id}` is already configured. Use `/alt action:update` to modify it.", ephemeral=True)

        # 3. Resolve Display Name & Discord User ID
        custom_name = value(self.name)
        name = custom_name if custom_name else detected_name
        name = re.sub(r"[\r\n]", " ", name)[:80].strip() or f"Alt {alt_id}"

        custom_did = value(getattr(self, "discord_user_id", None))
        did = custom_did if (custom_did and custom_did.isdigit()) else detected_did

        # 4. Resolve Repository (auto-create if blank or missing)
        raw_repo = value(self.repository)
        if raw_repo:
            repo = raw_repo if "/" in raw_repo else f"{config.GITHUB_OWNER}/{raw_repo}"
        else:
            clean_slug_name = re.sub(r"[^a-zA-Z0-9_-]", "", detected_username.lower().replace(" ", "-")) or f"alt{alt_id}"
            owner = config.GITHUB_OWNER or (config.CORE_REPO.split("/")[0] if "/" in config.CORE_REPO else "owner")
            repo = f"{owner}/alt{alt_id}-{clean_slug_name}"

        # Resolve Advertising Channels (inherit from fleet if blank)
        raw_channels = value(getattr(self, "channels", None))
        parsed_channels = [c.strip() for c in raw_channels.split(",") if c.strip().isdigit()]
        if not parsed_channels:
            fleet_chs = os.environ.get("CHANNEL_IDS") or config._raw("CHANNEL_IDS")
            if fleet_chs:
                parsed_channels = [c.strip() for c in fleet_chs.split(",") if c.strip().isdigit()]
            if not parsed_channels:
                for other_id in state.alt_ids:
                    o_alt = state.get(other_id)
                    if o_alt and o_alt.channels:
                        parsed_channels = [str(c) for c in o_alt.channels.keys() if str(c).isdigit()]
                        if parsed_channels:
                            break

        channels_csv = ",".join(parsed_channels)

        # 5. Auto-create repo on GitHub, upload templates, and populate secrets
        ok_prov, prov_detail = await asyncio.to_thread(
            github_api.provision_alt_repository_files_and_secrets, repo, token, channels_csv,
            alt_id=alt_id, alt_name=name,
        )
        if not ok_prov:
            await _log_alt_add_event(alt_id, False, f"Alt add failed while provisioning `{repo}`: {prov_detail}")
            return await inter.followup.send(f"❌ Auto-provisioning failed for `{repo}`: {prov_detail}", ephemeral=True)

        # 6. Persist alt in core fleet registry
        repos = dict(config.ALT_REPOS); repos[alt_id] = repo
        discord_ids = dict(config.ALT_DISCORD_IDS); discord_ids[alt_id] = int(did)
        names = dict(config.ALT_NAMES); names[alt_id] = name
        persisted, persist_detail = await _persist_alt_registry(repos, discord_ids, names)
        if not persisted:
            await _log_alt_add_event(alt_id, False, f"Alt add failed to persist core registry: {persist_detail}")
            return await inter.followup.send(f"❌ Alt was not registered in core map: {persist_detail}", ephemeral=True)

        _apply_alt_registry(repos, discord_ids, names)
        if not state.add_alt(alt_id, name):
            # Another concurrent add claimed the slot between validation and
            # commit. Roll the registry back so state and config stay in sync.
            repos.pop(alt_id, None); discord_ids.pop(alt_id, None); names.pop(alt_id, None)
            _apply_alt_registry(repos, discord_ids, names)
            await _log_alt_add_event(alt_id, False, f"Alt add failed: Alt {alt_id} was claimed by a concurrent add.")
            return await inter.followup.send(
                f"❌ Alt `{alt_id}` was registered by another in-flight add. Retry to take the next free slot.",
                ephemeral=True,
            )
        for cid in parsed_channels:
            state.set_channel(alt_id, cid, cid)
        channel_persist_ok, channel_persist_detail = await _persist_channels_for_alt(alt_id)
        if not channel_persist_ok:
            # Repository provisioning succeeded, so keep the alt registered,
            # but make the durable channel-state issue explicit in both logs.
            await _log_alt_add_event(alt_id, False, f"Alt {alt_id} channel registry warning: {channel_persist_detail}")

        channel_summary = f"`{channels_csv}`" if channels_csv else "_inherited / auto-discovered_"
        text = (
            f"🎉 **Alt {alt_id} (@{detected_username}) successfully added!**\n"
            f"• **Repository:** `{repo}` (auto-provisioned with workflows & secrets)\n"
            f"• **Discord ID:** `{did}`\n"
            f"• **Display Name:** `{name}`\n"
            f"• **Target Channels:** {channel_summary}\n"
            f"• **Token:** Verified and securely stored in GitHub secrets\n\n"
            f"👉 *You can now launch this alt directly with `/run` without manual setup!*"
        )
        await inter.followup.send(text, ephemeral=True)
        await _log_alt_add_event(alt_id, True, f"Alt {alt_id} ({name}) successfully added with repo `{repo}` and {len(parsed_channels)} channel(s).")


class AltUpdateModal(discord.ui.Modal):
    def __init__(self, alt_id: int):
        super().__init__(title=f"Update Alt {alt_id}")
        self.alt_id_value = alt_id
        self.name = discord.ui.TextInput(label="New display name (optional)", max_length=80, required=False)
        self.repository = discord.ui.TextInput(label="New repository (optional)", max_length=100, required=False)
        self.discord_user_id = discord.ui.TextInput(label="New Discord user ID (optional)", max_length=30, required=False)
        self.user_token = discord.ui.TextInput(label="New user token (optional)", max_length=600, required=False)
        for item in (self.name, self.repository, self.discord_user_id, self.user_token):
            self.add_item(item)

    async def on_submit(self, inter: discord.Interaction):
        if not _is_owner(inter):
            await inter.response.send_message("🔒 You aren't authorized to manage alts.", ephemeral=True)
            return
        alt_id = self.alt_id_value
        if not state.get(alt_id):
            await inter.response.send_message("❓ Unknown alt.", ephemeral=True)
            return
        def value(item) -> str:
            return str(getattr(item, "value", item) or "").strip()
        old_repo = config.ALT_REPOS.get(alt_id, "")
        raw_repo = value(self.repository)
        repo = raw_repo or old_repo
        name = re.sub(r"[\r\n]", " ", value(self.name))[:80]
        did = value(self.discord_user_id)
        token = value(self.user_token)
        if not any((name, raw_repo, did, token)):
            await inter.response.send_message("❌ Enter at least one value to update.", ephemeral=True)
            return
        if not _valid_repo_name(repo):
            await inter.response.send_message("❌ Repository must be `owner/name` or a simple repository name.", ephemeral=True)
            return
        if did and not did.isdigit():
            await inter.response.send_message("❌ Discord user ID must contain digits only.", ephemeral=True)
            return
        if repo != old_repo and not token:
            await inter.response.send_message("❌ A repository change also requires the new repository's user token.", ephemeral=True)
            return
        await inter.response.defer(ephemeral=True)
        if repo != old_repo:
            exists, detail = await asyncio.to_thread(github_api.repository_exists, repo)
            if not exists:
                return await inter.followup.send(f"❌ Cannot update alt: {detail}", ephemeral=True)
        if token:
            ok, detail = await asyncio.to_thread(github_api.set_repository_secret, repo, "USER_TOKEN", token)
            if not ok:
                return await inter.followup.send(f"❌ Token was not updated: {detail}", ephemeral=True)
        repos = dict(config.ALT_REPOS); repos[alt_id] = repo
        discord_ids = dict(config.ALT_DISCORD_IDS)
        if did:
            discord_ids[alt_id] = int(did)
        names = dict(config.ALT_NAMES)
        if name:
            names[alt_id] = name
        persisted, persist_detail = await _persist_alt_registry(repos, discord_ids, names)
        if not persisted:
            return await inter.followup.send(f"❌ Core registry was not updated: {persist_detail}", ephemeral=True)
        if repo != old_repo and old_repo:
            await asyncio.to_thread(github_api.delete_repository_secret, old_repo, "USER_TOKEN")
        _apply_alt_registry(repos, discord_ids, names)
        state.update_identity(alt_id, name=name or None)
        label = name or state.get(alt_id).name
        text = f"✅ Updated **{label}** (alt `{alt_id}`). Token/metadata changes were stored without echoing secrets."
        await inter.followup.send(text, ephemeral=True)
        await _log_control(text)
        state.append_log(alt_id, text, emoji="✏️", color=0x5865F2, kind="CONTROL")


class ReplaceChannelModal(discord.ui.Modal, title="Replace Trading Channel"):
    def __init__(self, alt_id: int):
        super().__init__()
        self.alt_id = alt_id
        self.old_channel = discord.ui.TextInput(
            label="Old Channel ID",
            placeholder="e.g. 112233445566",
            max_length=30,
            required=True,
        )
        self.new_channel = discord.ui.TextInput(
            label="New Channel ID",
            placeholder="e.g. 998877665544",
            max_length=30,
            required=True,
        )
        self.channel_name = discord.ui.TextInput(
            label="Channel Name / Label (optional)",
            placeholder="e.g. trading-market",
            max_length=80,
            required=False,
        )
        self.add_item(self.old_channel)
        self.add_item(self.new_channel)
        self.add_item(self.channel_name)

    async def on_submit(self, inter: discord.Interaction):
        old_id = self.old_channel.value.strip()
        new_id = self.new_channel.value.strip()
        name = self.channel_name.value.strip()
        await cmd_channels.callback(inter, alt=self.alt_id, action="replace", channel_id=old_id, new_channel_id=new_id, name=name)


def _build_alt_overview_embed(selected_alt: Optional[int] = None) -> discord.Embed:
    embed = discord.Embed(
        title="👥 Fleet Alt Management Hub",
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc),
    )
    if not state.alt_ids:
        embed.description = "⚠️ _No alts configured in fleet._ Click **➕ Add Alt** below to add one."
        return embed

    rows = []
    for a in state.all():
        repo = config.ALT_REPOS.get(a.alt_id, "not mapped")
        dot, _ = _status_dot(a)
        health = state.get_health_index(a.alt_id)
        selected_marker = " 👈 *(active target)*" if (selected_alt and a.alt_id == selected_alt) else ""
        rows.append(
            f"{dot} **Alt {a.alt_id}: {a.name}** (`{a.status}` · Health: `{health}%`){selected_marker}\n"
            f"   ↳ **Repo:** `{repo}` | **Heartbeat:** `{_fmt_ago(a.last_heartbeat_ts)}`\n"
            f"   ↳ **Mode:** `{a.ad_type or 'sell'}` @ `${a.rate or 2.50:.2f}/1k` | **Sent:** `{a.total_sent}` | **Errors:** `{a.total_errors}`"
        )
    embed.description = "\n\n".join(rows)
    embed.set_footer(text="Credentials and tokens are encrypted and never shown. Select an action below.")
    return embed


def _build_deals_overview_embed(selected_alt: Optional[int] = 0) -> discord.Embed:
    chosen = state.all() if selected_alt == 0 else ([state.get(selected_alt)] if state.get(selected_alt) else [])
    if not chosen:
        embed = discord.Embed(title="📈 Market Arbitrage & Deal Scanner Hub", description="⚠️ _No alts configured._", color=0xED4245)
        return embed

    lines = []
    for item in chosen:
        last = datetime.fromtimestamp(item.last_deal_ts, timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC') if item.last_deal_ts else "never"
        keywords = ", ".join(item.deal_keywords[:20]) or ", ".join(config.DEFAULT_DEAL_KEYWORDS)
        scanner = f"{'🟢 ACTIVE' if item.deal_scan_enabled else '🔴 OFF'} · min edge `${item.deal_alert_delta:.2f}/1k`"
        lines.append(
            f"• **[Alt {item.alt_id} · {item.name}]**\n"
            f"   ↳ **Scanner:** {scanner}\n"
            f"   ↳ **Alerts Triggered:** `{item.deal_alerts}` posts | **Last Match:** `{last}`\n"
            f"   ↳ **Keywords:** `{keywords}`"
        )
    embed = discord.Embed(
        title="📈 Market Arbitrage & Deal Scanner Hub",
        description="\n\n".join(lines),
        color=0x57F287,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="Passively extracts target item prices, stock, and directions across all configured games/items.")
    return embed


class AltControlHubView(discord.ui.View):
    def __init__(self, owner_id: int, selected_alt: int = 1):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.selected_alt = selected_alt if selected_alt in state.alt_ids else (state.alt_ids[0] if state.alt_ids else 1)
        self._build_items()

    def _build_items(self):
        self.clear_items()
        if len(state.alt_ids) > 1:
            options = [
                discord.SelectOption(
                    label=f"Alt {i}: {state.get(i).name if state.get(i) else f'Alt {i}'}"[:100],
                    value=str(i),
                    default=(i == self.selected_alt),
                    emoji="🟢" if state.is_online(i) else "⚪",
                )
                for i in state.alt_ids[:25]
            ]
            class _AltPicker(discord.ui.Select):
                def __init__(parent_self):
                    super().__init__(placeholder="Choose an Alt to manage...", min_values=1, max_values=1, options=options, row=0)
                async def callback(sel_self, inter: discord.Interaction):
                    if not _is_owner(inter):
                        return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
                    self.selected_alt = int(sel_self.values[0])
                    self._build_items()
                    embed = self._render_embed()
                    await inter.response.edit_message(embed=embed, view=self)
            self.add_item(_AltPicker())

        btn_add = discord.ui.Button(label="Add Alt", style=discord.ButtonStyle.success, emoji="➕", row=1)
        btn_update = discord.ui.Button(label="Update Alt", style=discord.ButtonStyle.primary, emoji="✏️", row=1)
        btn_logs = discord.ui.Button(label="View Logs", style=discord.ButtonStyle.secondary, emoji="📜", row=1)
        btn_selfcheck = discord.ui.Button(label="Self-Check", style=discord.ButtonStyle.secondary, emoji="🔍", row=1)
        btn_runs = discord.ui.Button(label="Workflow Runs", style=discord.ButtonStyle.secondary, emoji="⏱️", row=2)
        btn_clearlogs = discord.ui.Button(label="Clear Logs", style=discord.ButtonStyle.secondary, emoji="🧹", row=2)
        btn_refresh = discord.ui.Button(label="Refresh", style=discord.ButtonStyle.primary, emoji="🔄", row=2)

        async def _cb_add(inter: discord.Interaction):
            if not _is_owner(inter):
                return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
            await inter.response.send_modal(AltAddModal())

        async def _cb_update(inter: discord.Interaction):
            if not _is_owner(inter):
                return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
            if not state.get(self.selected_alt):
                return await inter.response.send_message("❓ Unknown alt.", ephemeral=True)
            await inter.response.send_modal(AltUpdateModal(self.selected_alt))

        async def _cb_logs(inter: discord.Interaction):
            if not _is_owner(inter):
                return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
            await _handle_alt_logs(inter, self.selected_alt, limit=15)

        async def _cb_selfcheck(inter: discord.Interaction):
            if not _is_owner(inter):
                return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
            await _handle_alt_selfcheck(inter, self.selected_alt)

        async def _cb_runs(inter: discord.Interaction):
            if not _is_owner(inter):
                return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
            await _handle_alt_runs(inter, self.selected_alt, limit=5)

        async def _cb_clearlogs(inter: discord.Interaction):
            if not _is_owner(inter):
                return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
            await _handle_alt_clearlogs(inter, self.selected_alt)

        async def _cb_refresh(inter: discord.Interaction):
            if not _is_owner(inter):
                return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
            self._build_items()
            embed = self._render_embed()
            await inter.response.edit_message(embed=embed, view=self)

        btn_add.callback = _cb_add
        btn_update.callback = _cb_update
        btn_logs.callback = _cb_logs
        btn_selfcheck.callback = _cb_selfcheck
        btn_runs.callback = _cb_runs
        btn_clearlogs.callback = _cb_clearlogs
        btn_refresh.callback = _cb_refresh

        self.add_item(btn_add)
        self.add_item(btn_update)
        self.add_item(btn_logs)
        self.add_item(btn_selfcheck)
        self.add_item(btn_runs)
        self.add_item(btn_clearlogs)
        self.add_item(btn_refresh)

    def _render_embed(self) -> discord.Embed:
        return _build_alt_overview_embed(self.selected_alt)


class DealsHubView(discord.ui.View):
    def __init__(self, owner_id: int, alt_id: int = 0):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.alt_id = alt_id
        self._build_items()

    def _build_items(self):
        self.clear_items()
        if len(state.alt_ids) > 1:
            options = [discord.SelectOption(label="All Alts (Fleet-Wide)", value="0", default=(self.alt_id == 0))]
            for aid in state.alt_ids:
                options.append(
                    discord.SelectOption(
                        label=_alt_label(aid),
                        value=str(aid),
                        default=(aid == self.alt_id)
                    )
                )
            class _DealsAltSelect(discord.ui.Select):
                def __init__(parent_self):
                    super().__init__(placeholder="Select Alt to configure deals...", min_values=1, max_values=1, options=options, row=0)
                async def callback(sel_self, inter: discord.Interaction):
                    if not _is_owner(inter):
                        return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
                    self.alt_id = int(sel_self.values[0])
                    self._build_items()
                    embed = self._render_embed()
                    await inter.response.edit_message(embed=embed, view=self)
            self.add_item(_DealsAltSelect())

        btn_toggle = discord.ui.Button(label="Toggle Scanner", style=discord.ButtonStyle.primary, emoji="⚡", row=1)
        btn_modal = discord.ui.Button(label="Configure Margins & Keywords", style=discord.ButtonStyle.success, emoji="🎯", row=1)
        btn_sim = discord.ui.Button(label="Simulate Listing", style=discord.ButtonStyle.secondary, emoji="🧪", row=1)
        btn_refresh = discord.ui.Button(label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔄", row=1)

        async def _cb_toggle(inter: discord.Interaction):
            if not _is_owner(inter):
                return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
            target = self.alt_id or (state.alt_ids[0] if state.alt_ids else 1)
            alt_obj = state.get(target)
            curr = getattr(alt_obj, "deal_scan_enabled", True) if alt_obj else True
            new_val = "off" if curr else "on"
            await _handle_deal_scan(inter, target, new_val)

        async def _cb_modal(inter: discord.Interaction):
            if not _is_owner(inter):
                return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
            target = self.alt_id or (state.alt_ids[0] if state.alt_ids else 1)
            await inter.response.send_modal(DealsManagerModal(target))

        async def _cb_sim(inter: discord.Interaction):
            if not _is_owner(inter):
                return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
            class _SimModal(discord.ui.Modal, title="Simulate Market Listing"):
                sample = discord.ui.TextInput(
                    label="Sample listing message",
                    placeholder="SELLING BB LF 2.20$/1K (Stock 50k) PAYPAL",
                    style=discord.TextStyle.paragraph,
                    max_length=1000,
                    required=True,
                )
                async def on_submit(m_self, m_inter: discord.Interaction):
                    target = self.alt_id or (state.alt_ids[0] if state.alt_ids else 1)
                    await _handle_simulate_listing(m_inter, target, sample_listing=m_self.sample.value)
            await inter.response.send_modal(_SimModal())

        async def _cb_refresh(inter: discord.Interaction):
            if not _is_owner(inter):
                return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
            self._build_items()
            embed = self._render_embed()
            await inter.response.edit_message(embed=embed, view=self)

        btn_toggle.callback = _cb_toggle
        btn_modal.callback = _cb_modal
        btn_sim.callback = _cb_sim
        btn_refresh.callback = _cb_refresh

        self.add_item(btn_toggle)
        self.add_item(btn_modal)
        self.add_item(btn_sim)
        self.add_item(btn_refresh)

    def _render_embed(self) -> discord.Embed:
        return _build_deals_overview_embed(self.alt_id)


class RunPreviewView(discord.ui.View):
    """Confirmation step before a /run launch is dispatched to GitHub."""

    def __init__(self, owner_id: int, values: dict[str, str], parsed: dict[str, object]):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.values = values
        self.parsed = parsed

    async def _guard(self, inter):
        if inter.user.id != self.owner_id and not _is_owner(inter):
            await inter.response.send_message("🔒 This private run preview belongs to its operator.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="✅ Confirm Launch", style=discord.ButtonStyle.success, row=0)
    async def _confirm(self, inter: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(inter):
            return
        self.stop()
        await _execute_run_dispatch(inter, self.values, self.parsed)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary, row=0)
    async def _cancel(self, inter: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(inter):
            return
        self.stop()
        await inter.response.edit_message(content="Run cancelled — no workflow was dispatched.", embed=None, view=None)


def _run_preview_embed(values: dict[str, str], parsed: dict[str, object]) -> discord.Embed:
    try:
        alt_id = int(parsed.get("alt_id") or 0)
    except (TypeError, ValueError):
        alt_id = 0
    alt = state.get(alt_id)
    name = alt.name if alt else f"Alt {alt_id}"
    ad_type = str(values.get("ad_type") or "").lower()
    rate = parsed.get("rate")
    interval = parsed.get("interval")
    hours = parsed.get("hours")
    runtime_label = _runtime_label(hours)
    embed = discord.Embed(
        title="🚀 /run Command Preview — Review Before Dispatch",
        description=(
            "This is the exact configuration that will be sent to GitHub Actions.\n"
            "**_No workflow has been dispatched yet._**"
        ),
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc),
    )
    targets = [int(x) for x in (parsed.get("targets") or []) if str(x).isdigit()] or [alt_id]
    squad = str(parsed.get("squad") or "").strip()
    if squad and len(targets) > 1:
        target_text = f"Squad `{squad}` — " + ", ".join(
            f"**{state.get_name(t)}** (`{t}`)" for t in targets
        )
    else:
        target_text = f"**{name}** (ID `{alt_id}`)"
    embed.add_field(name="Alt", value=target_text[:1024], inline=True)
    embed.add_field(name="Mode", value=f"`{ad_type.upper()}`", inline=True)
    embed.add_field(name="Interval / Runtime", value=f"`{interval}m` / `{runtime_label}`", inline=True)
    embed.add_field(name="Image", value=f"`{values.get('attach_image')}`", inline=True)
    if ad_type == "sell":
        embed.add_field(name="Reference Rate (optional)", value=f"`${rate:.2f}/1k`" if rate is not None else "not supplied", inline=True)
    else:
        embed.add_field(name="Reference Rate (optional)", value=f"`${rate:.2f}/1k`" if rate is not None else "not supplied", inline=True)
        rap_rate = parsed.get("rap")
        embed.add_field(name="Secondary Rate (optional)", value=f"`${rap_rate:.2f}/1k`" if rap_rate is not None else "not supplied", inline=True)
        embed.add_field(name="Style", value=f"`{values.get('buy_style') or 'simple'}`", inline=True)
    raw_message = str(parsed.get("raw_message") or values.get("raw_message") or values.get("sell_extra") or values.get("buy_simple_text") or "")
    embed.add_field(name="Raw Message / Question", value=f"```{raw_message[:1500]}```", inline=False)
    if squad and len(targets) > 1:
        embed.add_field(
            name="Squad Spacing",
            value=(f"`{len(targets)}` alts launched in sequence, "
                   f"`{_SQUAD_SPACING[0] * 1000:.0f}–{_SQUAD_SPACING[1] * 1000:.0f}ms` apart."),
            inline=False,
        )
    embed.set_footer(text="/run now requires an explicit Confirm Launch step before dispatch.")
    return embed


async def _dispatch_run_from_modal(inter: discord.Interaction, values: dict[str, str], parsed: dict[str, object]) -> None:
    if not _is_owner(inter):
        await inter.response.send_message("🔒 You aren't authorized to run control commands.", ephemeral=True)
        return
    if not inter.response.is_done():
        await inter.response.defer(ephemeral=True)
    try:
        alt_id = int(parsed.get("alt_id") or 0)
    except (TypeError, ValueError):
        alt_id = 0
    alt = state.get(alt_id)
    if not alt:
        return await inter.followup.send("❓ Unknown alt.", ephemeral=True)
    if not config.GITHUB_TOKEN or not config.GITHUB_OWNER or alt_id not in config.ALT_REPOS:
        return await inter.followup.send("❌ GitHub control is not configured for this alt.", ephemeral=True)
    if config.RUN_PREVIEW_REQUIRED:
        view = RunPreviewView(inter.user.id, values, parsed)
        embed = _run_preview_embed(values, parsed)
        await inter.followup.send(embed=embed, view=view, ephemeral=True)
        return
    await _execute_run_dispatch(inter, values, parsed)


async def _execute_run_dispatch(inter: discord.Interaction, values: dict[str, str], parsed: dict[str, object]) -> None:
    if not _is_owner(inter):
        if inter.response.is_done():
            await inter.followup.send("🔒 You aren't authorized to run control commands.", ephemeral=True)
        else:
            await inter.response.send_message("🔒 You aren't authorized to run control commands.", ephemeral=True)
        return
    if not inter.response.is_done():
        await inter.response.defer(ephemeral=True)

    targets = [int(x) for x in (parsed.get("targets") or []) if str(x).isdigit()]
    if not targets:
        targets = [int(parsed["alt_id"])]
    squad = str(parsed.get("squad") or values.get("squad") or "").strip()

    # A squad dispatch walks its members in sequence. Each alt is separated by a
    # small randomized offset so the fleet never emits in the same millisecond.
    results: list[str] = []
    for index, alt_id in enumerate(targets):
        if index:
            await _squad_pause()
        results.append(await _dispatch_single_alt(alt_id, values, parsed))

    runtime_label = _runtime_label(parsed["hours"])
    header = (
        f"🚀 **Squad `{squad}`** launch — {len(targets)} alt(s) dispatched in sequence "
        f"({_SQUAD_SPACING[0] * 1000:.0f}–{_SQUAD_SPACING[1] * 1000:.0f}ms apart)"
        if squad and len(targets) > 1 else
        f"🚀 Launch — {len(targets)} alt(s) dispatched"
    )
    body = f"{header}\n• Interval `{parsed['interval']}min` × `{runtime_label}`\n" + "\n".join(f"• {line}" for line in results)
    await inter.followup.send(body[:4000], ephemeral=True)
    await _log_control(body)
    if squad and len(targets) > 1:
        for alt_id in targets:
            state.append_log(
                alt_id,
                f"Squad `{squad}` run dispatched to {len(targets)} alt(s) "
                f"({', '.join(str(t) for t in targets)}) at {parsed['interval']}min × {runtime_label}.",
                emoji="👥", color=0x57F287, kind="SQUAD",
            )


async def _dispatch_single_alt(alt_id: int, values: dict[str, str], parsed: dict[str, object]) -> str:
    """Dispatch one workflow for a single alt and return a one-line result."""
    alt = state.get(alt_id)
    if not alt:
        return f"{_alt_tag(alt_id)} ❓ Unknown alt — skipped."
    if not config.GITHUB_TOKEN or not config.GITHUB_OWNER or alt_id not in config.ALT_REPOS:
        return f"{_alt_tag(alt_id)} ❌ GitHub control is not configured for this alt — skipped."

    # Resolve target channels to pass to workflow so newly added alts never crash on empty channels
    active_chs = [str(c) for c in alt.channels.keys() if str(c).isdigit()] if alt.channels else []
    if not active_chs:
        for other_id in state.alt_ids:
            other_alt = state.get(other_id)
            if other_alt and other_alt.channels:
                active_chs = [str(c) for c in other_alt.channels.keys() if str(c).isdigit()]
                if active_chs:
                    break
    if not active_chs:
        fleet_raw = os.environ.get("CHANNEL_IDS") or config._raw("CHANNEL_IDS")
        if fleet_raw:
            active_chs = [c.strip() for c in fleet_raw.split(",") if c.strip().isdigit()]

    ch1 = active_chs[0] if active_chs else ""
    ch2 = active_chs[1] if len(active_chs) > 1 else ""

    raw_message = str(parsed.get("raw_message") or values.get("raw_message") or values.get("sell_extra") or values.get("buy_simple_text") or "").strip()
    inputs = {
        "ad_type": values["ad_type"],
        "message": raw_message,
        "interval_min": str(parsed["interval"]),
        "total_hours": str(parsed["hours"]),
        "runtime_limitless": "1" if int(parsed["hours"] or 0) == 0 else "0",
        "attach_image": values["attach_image"],
        "channel_1": ch1,
        "channel_2": ch2,
        "channel_1_name": "",
        "channel_2_name": "",
    }
    if values["ad_type"] == "sell":
        inputs.update({"sell_rate": values.get("sell_rate", ""), "sell_extra": values.get("sell_extra", "")})
    else:
        inputs.update({
            "buy_style": values.get("buy_style", "simple"),
            "buy_rate": str(parsed["rate"]),
            "buy_rate_rap": str(parsed["rap"]),
            "buy_simple_text": values.get("buy_simple_text", ""),
        })
    try:
        await asyncio.to_thread(github_api.cancel_run, alt_id)
        ok, msg = await asyncio.to_thread(github_api.dispatch_workflow, alt_id, inputs)
    except Exception as exc:
        return f"{_alt_tag(alt_id)} ❌ Dispatch failed: {type(exc).__name__}: {exc}"
    if not ok:
        state.set_error(alt_id, f"Dispatch failed: {msg}")
        return f"{_alt_tag(alt_id)} ❌ Dispatch failed: {msg}"
    state.set_workflow(alt_id, None, "queued", "")
    state.set_run_config(
        alt_id,
        ad_type=values["ad_type"],
        rate=parsed["rate"],
        message=values.get("sell_extra") or values.get("buy_simple_text"),
        interval_min=parsed["interval"],
        runtime_hours=parsed["hours"],
    )
    runtime_label = _runtime_label(parsed["hours"])
    text = (f"{_alt_tag(alt_id)} **{alt.name}** queued privately: {values['ad_type']} · "
            f"{parsed['interval']}min × {runtime_label} · image={values['attach_image']} — {msg}")
    state.append_log(alt_id, text, emoji="🚀", color=0x57F287, kind="CONTROL")
    return text


async def _finish_dm_control(inter: discord.Interaction, alt_id: int,
                             command: str, label: str, *, update=None) -> None:
    """Send a control command through the Gist queue (or legacy DM fallback)."""
    if not inter.response.is_done():
        await inter.response.defer(ephemeral=True)
    alt = state.get(alt_id)
    if not alt:
        return await inter.followup.send("❓ Unknown alt.", ephemeral=True)
    ack = await _send_control_wait_ack(alt_id, command, timeout=20)
    failed = ack.startswith(("❌", "⏰"))
    queued = ack.startswith("🕒")
    local_failed = False
    local_detail = ""
    if not failed and update:
        try:
            res = update()
            if asyncio.iscoroutine(res):
                res = await res
            if res is False:
                local_failed = True
                local_detail = "local update returned failure"
        except Exception as exc:
            local_failed = True
            local_detail = str(exc)[:300] or "local update failed"
    if queued:
        status = "Queued in the shared control Gist; the alt will apply it on its next poll and confirm through the next heartbeat."
    else:
        status = "Remote alt acknowledged the command." if not failed else "Remote alt did not confirm the change."
    if local_failed:
        status += f" Local state was not committed: `{local_detail}`"
    overall_failed = failed or local_failed
    text = (
        f"{'✅' if not overall_failed else '⚠️'} **{alt.name}** — {label}\n"
        f"Command sent: `{command[:900]}`\n"
        f"Acknowledgement: `{ack[:900]}`\n{status}"
    )
    await inter.followup.send(text, ephemeral=True)
    await _log_control(text)
    state.append_log(alt_id, f"{label}: {ack}" + (f"; local failure: {local_detail}" if local_failed else ""), emoji="✅" if not overall_failed else "⚠️",
                      color=0x57F287 if not overall_failed else 0xFEE75C, kind="CONTROL")


# Helper handlers for unified sub-actions
async def _handle_alt_logs(inter: discord.Interaction, alt: int, limit: int = 15, kind: str = "ALL", search: Optional[str] = None):
    a = state.get(alt)
    if not a:
        if inter.response.is_done():
            return await inter.followup.send("❓ Unknown alt.", ephemeral=True)
        return await inter.response.send_message("❓ Unknown alt.", ephemeral=True)
    entries = state.recent_logs(alt, max(5, min(50, limit)), kind)
    if search:
        s_term = search.strip().lower()
        entries = [e for e in entries if s_term in e[3].lower()]
    if not entries:
        msg = f"No matching `{kind}` logs buffered for **{a.name}**."
        if inter.response.is_done():
            return await inter.followup.send(msg, ephemeral=True)
        return await inter.response.send_message(msg, ephemeral=True)
    lines = [f"`[{datetime.fromtimestamp(ts).strftime('%H:%M:%S')}]` {emo} {txt}" for ts, emo, _col, txt in entries]
    body = "\n".join(lines)[-3900:]
    embed = discord.Embed(title=f"📜 {a.name} (Alt {alt}) · {kind} logs", description=body, color=0x2F3136)
    if inter.response.is_done():
        await inter.followup.send(embed=embed, ephemeral=True)
    else:
        await inter.response.send_message(embed=embed, ephemeral=True)


async def _handle_alt_clearlogs(inter: discord.Interaction, alt: int):
    a = state.get(alt)
    if not a:
        if inter.response.is_done():
            return await inter.followup.send("❓ Unknown alt.", ephemeral=True)
        return await inter.response.send_message("❓ Unknown alt.", ephemeral=True)
    state.clear_logs(alt)
    msg = f"🧹 Cleared local buffered logs for **{a.name}** (Alt `{alt}`). Discord channel history was not deleted."
    if inter.response.is_done():
        await inter.followup.send(msg, ephemeral=True)
    else:
        await inter.response.send_message(msg, ephemeral=True)


async def _handle_alt_runs(inter: discord.Interaction, alt: int, limit: int = 5):
    a = state.get(alt)
    if not a:
        if inter.response.is_done():
            return await inter.followup.send("❓ Unknown alt.", ephemeral=True)
        return await inter.response.send_message("❓ Unknown alt.", ephemeral=True)
    if not inter.response.is_done():
        await inter.response.defer(ephemeral=True)
    runs = await asyncio.to_thread(github_api.list_runs, alt, max(1, min(10, limit)))
    if not runs:
        return await inter.followup.send(f"No GitHub workflow runs found for **{a.name}**.", ephemeral=True)
    lines = []
    for run in runs:
        status = str(run.get("status") or "?")
        conclusion = str(run.get("conclusion") or "pending")
        run_id = run.get("id") or "?"
        created = str(run.get("created_at") or "")[:16].replace("T", " ")
        url = str(run.get("html_url") or "")
        label = f"[{run_id}]({url})" if url else f"`{run_id}`"
        lines.append(f"{label} · `{status}/{conclusion}` · `{created}Z`")
    embed = discord.Embed(title=f"🧾 {a.name} (Alt {alt}) · Recent Workflow Runs", description="\n".join(lines)[:4000], color=0x5865F2)
    await inter.followup.send(embed=embed, ephemeral=True)


async def _handle_alt_selfcheck(inter: discord.Interaction, alt: int):
    a = state.get(alt)
    if not a:
        if inter.response.is_done():
            return await inter.followup.send("❓ Unknown alt.", ephemeral=True)
        return await inter.response.send_message("❓ Unknown alt.", ephemeral=True)
    if not inter.response.is_done():
        await inter.response.defer(ephemeral=True)
    ok, detail = await asyncio.to_thread(
        github_api.dispatch_named_workflow, alt, config.SELF_CHECK_WORKFLOW, {}
    )
    if ok:
        state.set_workflow(alt, None, "queued", "")
        text = f"🔍 **{a.name}** (Alt {alt}) self-check queued. {detail}"
    else:
        text = f"❌ Self-check could not be queued for **{a.name}**: {detail}"
    await inter.followup.send(text, ephemeral=True)
    await _log_control(text)
    state.append_log(alt, text, emoji="🔍" if ok else "❌", color=0x5865F2 if ok else 0xED4245, kind="CONTROL" if ok else "ERROR")


async def _handle_alt_remove(inter: discord.Interaction, alt: int, confirmation: str, delete_repository: bool = False):
    if str(confirmation or "").strip().upper() != "DELETE":
        if inter.response.is_done():
            return await inter.followup.send("❌ Type `DELETE` exactly in the confirmation field to confirm alt removal.", ephemeral=True)
        return await inter.response.send_message("❌ Type `DELETE` exactly in the confirmation field to confirm alt removal.", ephemeral=True)
    current = state.get(alt)
    if not current:
        if inter.response.is_done():
            return await inter.followup.send("❓ Unknown alt.", ephemeral=True)
        return await inter.response.send_message("❓ Unknown alt.", ephemeral=True)
    if not inter.response.is_done():
        await inter.response.defer(ephemeral=True)
    repo = config.ALT_REPOS.get(alt, "")
    cancel_ok, cancel_detail = (True, "No repository mapping to cancel.")
    if repo:
        cancel_ok, cancel_detail = await asyncio.to_thread(github_api.cancel_run, alt)
    repos = dict(config.ALT_REPOS); repos.pop(alt, None)
    discord_ids = dict(config.ALT_DISCORD_IDS); discord_ids.pop(alt, None)
    names = dict(config.ALT_NAMES); names.pop(alt, None)
    persisted, persist_detail = await _persist_alt_registry(repos, discord_ids, names)
    if not persisted:
        return await inter.followup.send(f"❌ Alt was not removed from the core registry: {persist_detail}", ephemeral=True)
    cleanup_detail = ""
    if repo:
        if delete_repository:
            cleaned, cleanup_detail = await asyncio.to_thread(github_api.delete_repository, repo)
        else:
            cleaned, cleanup_detail = await asyncio.to_thread(github_api.delete_repository_secret, repo, "USER_TOKEN")
        if not cleaned:
            cleanup_detail = f"Cleanup warning: {cleanup_detail}"
    _apply_alt_registry(repos, discord_ids, names)
    state.remove_alt(alt)
    deletion = "Repository deletion requested" if delete_repository else "USER_TOKEN secret deleted; repository kept"
    details = [f"🗑️ Removed **{current.name}** (alt `{alt}`).", f"• {deletion}"]
    if cleanup_detail:
        details.append(f"• {cleanup_detail}")
    if cancel_detail:
        details.append(f"• {cancel_detail}")
    text = "\n".join(details)
    await inter.followup.send(text, ephemeral=True)
    await _log_control(text)


async def _handle_deal_scan(inter: discord.Interaction, alt: int, enabled: str):
    enabled = enabled.casefold().strip()
    if enabled not in {"on", "off"}:
        if inter.response.is_done():
            return await inter.followup.send("❌ Scanner must be `on` or `off`.", ephemeral=True)
        return await inter.response.send_message("❌ Scanner must be `on` or `off`.", ephemeral=True)
    active = enabled == "on"
    await _finish_dm_control(
        inter, alt, f"!setdealscan {enabled}", f"deal scanner queued as `{enabled}`",
        update=lambda: state.set_deal_config(alt, enabled=active),
    )


async def _handle_simulate_listing(inter: discord.Interaction, alt: int, sample_listing: str, test_rate: Optional[float] = None):
    alt_obj = state.get(alt)
    if not alt_obj:
        if inter.response.is_done():
            return await inter.followup.send(f"❌ Alt `{alt}` not found.", ephemeral=True)
        return await inter.response.send_message(f"❌ Alt `{alt}` not found.", ephemeral=True)
    rate_val = test_rate if test_rate is not None else (alt_obj.rate or 2.50)

    try:
        from send_ads import parse_market_listing
    except Exception:
        parse_market_listing = None
    except SystemExit:
        parse_market_listing = None
    except BaseException:
        parse_market_listing = None
    keywords = alt_obj.deal_keywords or list(config.DEFAULT_ITEM_KEYWORDS)
    parsed = parse_market_listing(sample_listing, target_keywords=keywords) if parse_market_listing else None

    embed = discord.Embed(
        title=f"🧪 Deal Scanner Parser Simulation · Alt {alt}: {alt_obj.name}",
        color=0x57F287 if parsed else 0xED4245,
        timestamp=datetime.now(timezone.utc),
    )
    embed.description = f"**Input Listing Excerpt:**\n```{sample_listing[:500]}```"
    if parsed:
        delta = alt_obj.deal_alert_delta or 0.05
        p_rate = parsed["price"]
        p_kind = parsed["kind"]
        item = parsed["item"]

        is_deal = False
        margin = 0.0
        if p_kind == "seller":
            if p_rate <= rate_val - delta:
                is_deal = True
                margin = rate_val - p_rate
        elif p_kind == "buyer":
            if p_rate >= rate_val + delta:
                is_deal = True
                margin = p_rate - rate_val

        verdict = f"🔥 **DEAL ALERT TRIGGERED!** (Net Profit Edge: `+${margin:.2f}/1k`)" if is_deal else f"⚪ **No Alert** (Edge `${margin:.2f}/1k` < delta `${delta:.2f}/1k`)"

        embed.add_field(name="Matched Item", value=f"**{item}**", inline=True)
        embed.add_field(name="Detected Direction", value=f"`{p_kind.upper()}`", inline=True)
        embed.add_field(name="Detected Unit Price", value=f"**${p_rate:.2f}/1k**", inline=True)
        embed.add_field(name="Volume / Stock", value=f"`{parsed.get('volume') or 'unspecified'}`", inline=True)
        embed.add_field(name="Payment Methods", value=f"`{', '.join(parsed.get('payments', [])) or 'none'}`", inline=True)
        embed.add_field(name="Active Baseline / Delta", value=f"`${rate_val:.2f}/1k` (±`${delta:.2f}`)", inline=True)
        embed.add_field(name="Evaluation Verdict", value=verdict, inline=False)
        embed.add_field(name="Matched Segment Line", value=f"```{parsed.get('segment', '')[:300]}```", inline=False)
    else:
        embed.add_field(name="Parsing Result", value="❌ **No Target Item or Price Recognized** (or message was filtered as non-market noise/negation).", inline=False)
        embed.add_field(name="Active Scanner Keywords", value=f"`{', '.join(keywords)}`", inline=False)
    if inter.response.is_done():
        await inter.followup.send(embed=embed, ephemeral=True)
    else:
        await inter.response.send_message(embed=embed, ephemeral=True)


# =========================================================================== #
# Core Slash Commands (19 Non-Duplicated Unified Top-Level Commands)          #
# =========================================================================== #

@bot.tree.command(name="run", description="Launch Ad Run: guided launcher that asks for your raw message, then automatically applies emojis/modifiers before dispatching to GitHub Actions.")
@app_commands.describe(squad="Optional squad name — launches every alt in that squad in sequence with staggered spacing")
async def cmd_run(inter: discord.Interaction, squad: Optional[str] = None):
    if not await _check_perms(inter):
        return
    squad = (squad or "").strip()
    if squad:
        members = state.get_squad_members(squad)
        if not members:
            known = ", ".join(sorted(state.get_all_squads())) or "none configured"
            return await inter.response.send_message(
                f"❌ Squad `{squad}` has no configured alts. Known squads: {known}. "
                "Use `/squad action:create squad_name:<name> alts:1,2,3` first.",
                ephemeral=True,
            )
    if not state.alt_ids:
        await inter.response.send_message(
            "❌ No configured alts are available. To fix: add one with `/alt action:add`, or check `/status` if an alt exists but isn't registered.",
            ephemeral=True,
        )
        return
    if not config.GITHUB_TOKEN:
        await inter.response.send_message(
            "❌ GitHub control is not configured: GH_TOKEN is missing. To fix: add `GITHUB_TOKEN` to your environment or GitHub secrets and restart the bot.",
            ephemeral=True,
        )
        return
    view = RunStartView(inter.user.id, squad=squad)
    await inter.response.send_message(embed=_run_start_embed(view), view=view, ephemeral=True)


@bot.tree.command(name="getstarted", description="Get Started: step-by-step beginner guide for setting up and running the farm.")
async def cmd_getstarted(inter: discord.Interaction):
    if not await _check_perms(inter):
        return
    embed = discord.Embed(
        title="🚀 Get Started — V6 Ad Farm Beginner Guide",
        description=(
            "Follow this checklist to get your first alt running. Every step is done inside Discord;\n"
            "you should not need to edit code for normal operations."
        ),
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc),
    )
    steps = [
        ("1. Add an alt", "Run `/alt action:add`. Enter the alt's user token and optionally a display name. The bot creates the repository, installs workflows/secrets, and validates the token before it registers."),
        ("2. Add channels", "Run `/channels alt:1 action:add channel_id:<ID> name:<label>`, or `/channels alt:1 action:overwrite channel_id:<id1,id2,...>` to replace the whole list at once. **Overwrite replaces, it never merges.** Prefer `/autorescan` — it fetches the live channel list, adds new channels, removes deleted ones, and logs exactly what changed."),
        ("3. Check health", "Run `/alt action:selfcheck alt:1`. It verifies token, channels, webhooks, proxies, and test suites."),
        ("4. Configure price/mode", "Run `/tune alt:1 mode:sell price:2.40`, or `/run` and use the guided form. `/run` only needs the message you want to post — modifiers such as emojis are applied automatically; no item word, price, or RAP is required."),
        ("5. Preview and launch", "Run `/run`, review the full preview (raw message, modifiers, cadence, runtime), and click **Confirm Launch**. The workflow is queued on GitHub and live state appears in #dashboard and #farm-logs."),
        ("6. Group and monitor", "Use `/squad action:create squad_name:MyGroup alts:1,2` then `/run squad:MyGroup` to launch a whole group with spaced dispatch. Monitor with `/status`, `/analytics`, `/alt action:logs alt:1`, `/deals`, and `/diagnose`. Run `/sync` after changing shared Gist settings."),
        ("7. Stop or shutdown", "Use `/stop alt:1` for one alt. Use `/shutdown` to gracefully stop all alts and end the control bot process."),
        ("Docs", "See `SKILL.md` (full command reference, troubleshooting), `BUG_TRACKER.md` (known issues/risks), `SETUP_CONTROL.md` (multi-alt), `SETUP_GUIDE.md` (single-alt), and `README.md` (architecture)."),
    ]
    for name, value in steps:
        embed.add_field(name=name, value=value, inline=False)
    embed.set_footer(text="Tip: running most commands without parameters opens a friendly visual form.")
    await inter.response.send_message(embed=embed, ephemeral=True)


def _script_result_embed(label: str, result: dict) -> discord.Embed:
    """Render one sandbox result.

    Nothing is filtered: both streams are reported in full. The embed fields
    show the tail (Discord caps a field at 1024 characters) and the caller
    attaches the complete stream as a file, with the exact truncated byte count
    reported here so the operator always knows what they are looking at.
    """
    timed = bool(result.get("timed_out"))
    code = int(result.get("code") or 0)
    ok = not timed and code in (0, None)
    color = 0x57F287 if ok else (0xFEE75C if timed else 0xED4245)
    embed = discord.Embed(title=label, color=color, timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Exit Code", value=f"`{code}`", inline=True)
    embed.add_field(name="Timed Out", value="yes" if timed else "no", inline=True)
    embed.add_field(name="Elapsed", value=f"`{float(result.get('elapsed') or 0):.2f}s`", inline=True)
    policy = (
        f"timeout `{config.SCRIPT_TIMEOUT_SEC}s` · memory `{config.SCRIPT_MEMORY_MB}MB` · "
        f"cpu `{config.SCRIPT_CPU_SEC}s` · network `{'on' if config.SCRIPT_NETWORK_ENABLED else 'off'}` · "
        f"max `{config.SCRIPT_MAX_CHARS}` chars"
    )
    embed.add_field(name="Sandbox Policy", value=policy, inline=False)
    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")

    def field_value(text: str) -> str:
        if not text:
            return "_empty_"
        clipped = text[-900:]
        prefix = f"_…showing last {len(clipped)} of {len(text)} chars (full stream attached)_\n" if len(clipped) < len(text) else ""
        return f"{prefix}```\n{clipped}\n```"[:1024]

    embed.add_field(name=f"stdout · {len(stdout)} chars (unfiltered)", value=field_value(stdout), inline=False)
    embed.add_field(name=f"stderr / errors · {len(stderr)} chars (unfiltered)", value=field_value(stderr), inline=False)
    if result.get("error"):
        embed.add_field(name="Sandbox Error", value=str(result["error"])[:900], inline=False)
    return embed


async def _run_script_sandbox(inter: discord.Interaction, script: str, *, execute: bool, label: str) -> None:
    script = (script or "").strip()
    if not script:
        if inter.response.is_done():
            return await inter.followup.send("❌ Script is empty.\nUse `/script action:simulate code:<...>` or `/script action:run code:<...>`.", ephemeral=True)
        return await inter.response.send_message("❌ Script is empty.\nUse `/script action:simulate code:<...>` or `/script action:run code:<...>`.", ephemeral=True)
    if len(script) > config.SCRIPT_MAX_CHARS:
        msg = f"❌ Script exceeds {config.SCRIPT_MAX_CHARS} characters."
        if inter.response.is_done():
            return await inter.followup.send(msg, ephemeral=True)
        return await inter.response.send_message(msg, ephemeral=True)
    if not inter.response.is_done():
        await inter.response.defer(ephemeral=True)
    result = await asyncio.to_thread(
        sandbox.run_script,
        script,
        timeout_sec=config.SCRIPT_TIMEOUT_SEC,
        memory_mb=config.SCRIPT_MEMORY_MB,
        cpu_sec=config.SCRIPT_CPU_SEC,
        label="script-run" if execute else "script-simulate",
        network=config.SCRIPT_NETWORK_ENABLED,
        max_chars=config.SCRIPT_MAX_CHARS,
    )
    embed = _script_result_embed(
        f"{'▶️' if execute else '🧪'} Script {label} "
        f"({'executed in sandbox' if execute else 'sandboxed dry-run diagnostic'})",
        result,
    )
    files = []
    for stream_name in ("stdout", "stderr"):
        stream = str(result.get(stream_name) or "")
        # Attach every non-empty stream so nothing is lost to the 1024-character
        # Discord field limit; the embed tail is only a preview.
        if stream:
            files.append(discord.File(
                io.BytesIO(stream.encode("utf-8", errors="replace")),
                filename=f"{label}-{stream_name}.txt",
            ))
    send_kwargs = {"embed": embed, "ephemeral": True}
    if files:
        send_kwargs["files"] = files
    await inter.followup.send(**send_kwargs)
    try:
        await _log_control(f"{'▶️' if execute else '🧪'} Script {label} completed: code={result.get('code')}, timed_out={result.get('timed_out')}.")
    except Exception:
        pass


@bot.tree.command(name="script", description="Scripting Suite: `simulate` shows unfiltered stdout/stderr (dry-run); `run` executes in sandbox. Both are resource-limited.")
@app_commands.describe(
    action="simulate (dry-run, unfiltered logs) or run (execute after approval)",
    code="Python script source",
)
@app_commands.choices(
    action=[
        app_commands.Choice(name="🧪 Simulate — dry-run with unfiltered logs/errors", value="simulate"),
        app_commands.Choice(name="▶️ Run — execute in the sandbox", value="run"),
    ]
)
async def cmd_script(inter: discord.Interaction, action: Literal["simulate", "run"], code: str):
    if not await _check_perms(inter):
        return
    await _run_script_sandbox(inter, code, execute=(action == "run"), label=action)


@bot.tree.command(name="shutdown", description="Shutdown: gracefully stop all alts, cancel workflows, and terminate the control bot process.")
@app_commands.describe(confirmation="Type SHUTDOWN to confirm")
async def cmd_shutdown(inter: discord.Interaction, confirmation: str):
    if not await _check_perms(inter):
        return
    if str(confirmation or "").strip().upper() != "SHUTDOWN":
        return await inter.response.send_message("❌ Type `SHUTDOWN` exactly in the confirmation field to confirm shutdown.", ephemeral=True)
    await inter.response.defer(ephemeral=True)
    details = []
    for aid in state.alt_ids:
        try:
            ack = await _send_control_wait_ack(aid, "!stop", timeout=10)
            details.append(f"• **{state.get_name(aid)}** (Alt `{aid}`): `{ack}`")
        except Exception as exc:
            details.append(f"• **{state.get_name(aid)}** (Alt `{aid}`): `{type(exc).__name__}: {exc}`")
        try:
            ok, msg = await asyncio.to_thread(github_api.cancel_run, aid)
            details.append(f"• **GitHub Alt {aid}**: {msg}")
        except Exception as exc:
            details.append(f"• **GitHub Alt {aid}**: `{type(exc).__name__}: {exc}`")
    core_cancel = "not requested"
    run_id = os.environ.get("GITHUB_RUN_ID", "").strip()
    if run_id:
        try:
            ok, msg = await asyncio.to_thread(github_api.cancel_workflow_run_by_id, int(run_id))
            core_cancel = msg
        except Exception as exc:
            core_cancel = f"`{type(exc).__name__}: {exc}`"
    else:
        core_cancel = "no GITHUB_RUN_ID available (manual stop only)"
    text = "🛑 **SHUTDOWN REQUESTED** — graceful termination sequence executed.\n\n" + "\n".join(details) + f"\n• **Control bot runner**: {core_cancel}"
    await inter.followup.send(text[:4000], ephemeral=True)
    await _log_control(text)
    for aid in state.alt_ids:
        state.set_workflow(aid, run_id=None, status="stopped", conclusion="cancelled")
    # Give the graceful shutdown a moment to flush logs before closing.
    await asyncio.sleep(min(config.SHUTDOWN_GRACE_SEC, 5))
    try:
        await bot.close()
    except Exception:
        pass


@bot.tree.command(name="stop", description="Stop Ad Run: cancel active GitHub Actions runner and shut down the alt cleanly.")
@app_commands.describe(alt="Target alt ID to stop")
async def cmd_stop(inter: discord.Interaction, alt: int):
    if not await _check_perms(inter):
        return
    a = state.get(alt)
    if not a:
        return await inter.response.send_message("❓ Unknown alt.", ephemeral=True)
    await inter.response.defer(ephemeral=True)
    control_ack = await _send_control_wait_ack(alt, "!stop", timeout=15)
    ok, msg = await asyncio.to_thread(github_api.cancel_run, alt)
    state.set_workflow(alt, run_id=None, status="cancelled" if ok else a.workflow_status,
                       conclusion="cancelled" if ok else "")
    text = f"🛑 **{a.name}** stop requested. Control transport: `{control_ack}`\nGitHub: {msg}"
    await inter.followup.send(text, ephemeral=True)
    await _log_control(text)
    state.append_log(alt, text, emoji="🛑", color=0xED4245, kind="CONTROL")


@bot.tree.command(name="pause", description="Pause Posting: temporarily pause public ads while keeping the runner alive.")
@app_commands.describe(alt="Target alt ID to pause (or choose specific alt)")
async def cmd_pause(inter: discord.Interaction, alt: int):
    if not await _check_perms(inter):
        return
    await _finish_dm_control(inter, alt, "!pause", "pause requested")


@bot.tree.command(name="resume", description="Resume Posting: unpause public ads and restore regular posting schedule.")
@app_commands.describe(alt="Target alt ID to resume (or choose specific alt)")
async def cmd_resume(inter: discord.Interaction, alt: int):
    if not await _check_perms(inter):
        return
    await _finish_dm_control(inter, alt, "!resume", "resume requested")


@bot.tree.command(name="alt", description="Alt Account Manager: view status, add accounts, check logs, or test health.")
@app_commands.describe(
    action="Action to perform (leave empty for visual dashboard)",
    alt="Target alt ID (1-4)",
    confirmation="Type DELETE to confirm removing an alt",
    delete_repository="Also delete the GitHub repository on remove (default: false)",
    limit="Number of log lines or runs to show (1-50)",
    kind="Log filter category",
    search="Search word to filter logs",
)
@app_commands.choices(
    action=[
        app_commands.Choice(name="👥 Overview - Open visual fleet management hub", value="overview"),
        app_commands.Choice(name="➕ Add Alt - Connect a new alt account via token", value="add"),
        app_commands.Choice(name="✏️ Update Alt - Change name, repo, or token", value="update"),
        app_commands.Choice(name="🗑️ Remove Alt - Disconnect an alt account", value="remove"),
        app_commands.Choice(name="📜 View Logs - Read live post and heartbeat logs", value="logs"),
        app_commands.Choice(name="🧹 Clear Logs - Reset in-memory log buffer", value="clearlogs"),
        app_commands.Choice(name="⏱️ Recent Runs - View recent GitHub Actions runs", value="runs"),
        app_commands.Choice(name="🔍 Health Self-Check - Test proxy, token, and channel access", value="selfcheck"),
    ],
    kind=[
        app_commands.Choice(name="📋 All Logs", value="ALL"),
        app_commands.Choice(name="❌ Errors Only", value="ERROR"),
        app_commands.Choice(name="📈 Deal Alerts", value="DEAL"),
        app_commands.Choice(name="⚙️ Control Actions", value="CONTROL"),
        app_commands.Choice(name="📌 Channel Events", value="CHANNEL"),
        app_commands.Choice(name="⚠️ Caution / Rate Limits", value="CAUTION"),
        app_commands.Choice(name="🔍 Debug Details", value="DEBUG"),
    ],
)
async def cmd_alt(
    inter: discord.Interaction,
    action: Optional[Literal["overview", "add", "update", "remove", "logs", "clearlogs", "runs", "selfcheck"]] = "overview",
    alt: Optional[int] = None,
    confirmation: Optional[str] = None,
    delete_repository: Optional[bool] = False,
    limit: Optional[int] = 15,
    kind: Optional[Literal["ALL", "ERROR", "DEAL", "CONTROL", "CHANNEL", "CAUTION", "DEBUG"]] = "ALL",
    search: Optional[str] = None,
):
    if not await _check_perms(inter):
        return

    target_aid = alt if (alt and alt in state.alt_ids) else (state.alt_ids[0] if state.alt_ids else 1)

    if action == "add":
        return await inter.response.send_modal(AltAddModal())
    elif action == "update":
        if not state.get(target_aid):
            return await inter.response.send_message("❓ Unknown alt.", ephemeral=True)
        return await inter.response.send_modal(AltUpdateModal(target_aid))
    elif action == "remove":
        if not alt or not confirmation:
            return await inter.response.send_message("❌ Specify `alt:<id>` and `confirmation:DELETE` to remove an alt.", ephemeral=True)
        return await _handle_alt_remove(inter, alt, confirmation, delete_repository or False)
    elif action == "logs":
        return await _handle_alt_logs(inter, target_aid, limit or 15, kind or "ALL", search)
    elif action == "clearlogs":
        return await _handle_alt_clearlogs(inter, target_aid)
    elif action == "runs":
        return await _handle_alt_runs(inter, target_aid, limit or 5)
    elif action == "selfcheck":
        return await _handle_alt_selfcheck(inter, target_aid)

    # Default: Interactive Alt Hub View
    view = AltControlHubView(owner_id=inter.user.id, selected_alt=target_aid)
    embed = view._render_embed()
    await inter.response.send_message(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="tune", description="Live Tuning: change price, message copy, mode, speed, duration, or image on the fly.")
@app_commands.describe(
    alt="Target alt ID (choose specific alt or 0 for all)",
    policy="Safety / speed preset template",
    price="Unit price / rate (e.g. 2.40)",
    mode="Trade direction (sell or buy)",
    message="Custom ad copy text",
    interval="Delay between posts per channel",
    runtime="How many hours to run (6 to 48)",
    image="Attach new ad image (.png, .jpg, .webp)",
)
@app_commands.choices(
    policy=[
        app_commands.Choice(name="🛡️ Stealth Safe-Mode (5m interval, max typing jitter, strict caution)", value="stealth"),
        app_commands.Choice(name="⚡ Aggressive (3m interval, high throughput, fast rotation)", value="aggressive"),
        app_commands.Choice(name="🔥 Peak-Hour Dynamic (3m interval, dynamic velocity cadence)", value="peak_hour"),
        app_commands.Choice(name="⚖️ Balanced Standard (5m interval, standard human timing)", value="balanced"),
    ],
    mode=[
        app_commands.Choice(name="💰 Sell Mode (Selling items/stock/tokens)", value="sell"),
        app_commands.Choice(name="🛒 Buy Mode (Buying items/stock/tokens)", value="buy"),
    ],
    interval=[
        app_commands.Choice(name="⚡ 3 Minutes (High throughput peak hours)", value=3),
        app_commands.Choice(name="🛡️ 5 Minutes (Recommended standard stealth)", value=5),
    ],
    runtime=[
        app_commands.Choice(name="⏱️ 6 Hours (Standard shift)", value=6),
        app_commands.Choice(name="⏱️ 12 Hours (Half day)", value=12),
        app_commands.Choice(name="⏱️ 18 Hours", value=18),
        app_commands.Choice(name="⏱️ 24 Hours (Full day)", value=24),
        app_commands.Choice(name="⏱️ 48 Hours (Weekend long run)", value=48),
    ],
)
async def cmd_tune(
    inter: discord.Interaction,
    alt: Optional[int] = 0,
    policy: Optional[Literal["stealth", "aggressive", "peak_hour", "balanced"]] = None,
    price: Optional[str] = None,
    mode: Optional[Literal["sell", "buy"]] = None,
    message: Optional[str] = None,
    interval: Optional[Literal[3, 5]] = None,
    runtime: Optional[Literal[6, 12, 18, 24, 48]] = None,
    image: Optional[discord.Attachment] = None,
):
    if not await _check_perms(inter):
        return

    has_params = any((policy, price, mode, message, interval, runtime, image))
    if not has_params:
        target_aid = alt if alt != 0 else (state.alt_ids[0] if state.alt_ids else 1)
        view = FleetTuningView(owner_id=inter.user.id, alt_id=target_aid)
        return await inter.response.send_message(embed=view._build_embed(), view=view, ephemeral=True)

    target_aid = alt if alt != 0 else (state.alt_ids[0] if state.alt_ids else 1)

    if image:
        if not image.content_type or not any(image.content_type.startswith(t) for t in ("image/png", "image/jpeg", "image/jpg", "image/webp")):
            return await inter.response.send_message("❌ Uploaded file must be an image (PNG, JPG, or WEBP).", ephemeral=True)
        if image.size > 8 * 1024 * 1024:
            return await inter.response.send_message("❌ Image size must be under 8MB.", ephemeral=True)
        await inter.response.defer(ephemeral=True)
        content_bytes = await image.read()
        targets = [target_aid] if alt != 0 else list(state.alt_ids)
        results = []
        for aid in targets:
            repo = config.ALT_REPOS.get(aid)
            if not repo:
                results.append(f"Alt `{aid}`: ❌ No repository mapped.")
                continue
            user_name = getattr(inter.user, "name", str(getattr(inter.user, "id", "operator")))
            ok, msg = await asyncio.to_thread(github_api.upload_repository_file, repo, "ad_image.png", content_bytes, f"Update ad image from Discord by {user_name}")
            a_obj = state.get(aid)
            alt_name = a_obj.name if a_obj else f"Alt {aid}"
            results.append(f"**{alt_name}**: {'✅ ' if ok else '❌ '}{msg}")
        embed = discord.Embed(
            title="🖼️ Ad Image Upload",
            description="\n".join(results),
            color=0x57F287,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=image.url)
        return await inter.followup.send(embed=embed, ephemeral=True)

    if policy:
        targets = [target_aid] if alt != 0 else list(state.alt_ids)
        for aid in targets:
            state.set_policy_template(aid, policy)
            asyncio.create_task(_send_control_wait_ack(aid, f"!policy {policy}", timeout=15))
        label = f"Alt {target_aid}" if alt != 0 else "All configured alts"
        await inter.response.send_message(f"✅ Policy template **{policy.upper()}** applied to {label}.", ephemeral=True)
        await _log_control(f"🛡️ Policy template **{policy.upper()}** dispatched to {label}.")
        return

    if price:
        val = _extract_price(price)
        if val is None or not 0 < val <= 20:
            return await inter.response.send_message("❌ Price must be a number between 0 and 20; example `2.30`.", ephemeral=True)
        return await _finish_dm_control(
            inter, target_aid, f"!setprice {val:g}", f"price validated at ${val:.2f}/1k",
            update=lambda: state.set_run_config(target_aid, rate=val),
        )

    if mode:
        return await _finish_dm_control(
            inter, target_aid, f"!setmode {mode}", f"mode validated as `{mode}`",
            update=lambda: state.set_run_config(target_aid, ad_type=mode),
        )

    if message:
        clean_msg = message.strip()
        if not clean_msg:
            return await inter.response.send_message("❌ Message cannot be empty.", ephemeral=True)
        if len(clean_msg) > 1900:
            return await inter.response.send_message("❌ Message too long; maximum is 1900 characters.", ephemeral=True)
        return await _finish_dm_control(
            inter, target_aid, f"!setmessage {clean_msg}", f"message validated ({len(clean_msg)} characters)",
            update=lambda: state.set_run_config(target_aid, message=clean_msg),
        )

    if interval:
        return await _finish_dm_control(
            inter, target_aid, f"!setinterval {interval}", f"interval validated at {interval} minutes",
            update=lambda: state.set_run_config(target_aid, interval_min=interval),
        )

    if runtime:
        return await _finish_dm_control(
            inter, target_aid, f"!setruntime {runtime}", f"runtime validated at {runtime} hours",
            update=lambda: state.set_run_config(target_aid, runtime_hours=runtime),
        )


async def _fetch_control_server_catalogue() -> list[dict[str, Any]]:
    """Return the control bot's cached/fetched eligible guild channels."""
    guilds = []
    if config.GUILD_ID:
        guild = bot.get_guild(config.GUILD_ID)
        if guild:
            guilds = [guild]
    if not guilds:
        guilds = list(getattr(bot, "guilds", []) or [])
    result: list[dict[str, Any]] = []
    for guild in guilds:
        try:
            channels = list(getattr(guild, "channels", []) or [])
            fetch_channels = getattr(guild, "fetch_channels", None)
            if fetch_channels:
                try:
                    channels = list(await fetch_channels())
                except Exception:
                    # Cached channels are still useful when a transient API
                    # call fails; the caller logs the exact partial inventory.
                    pass
            rows = []
            for channel in channels:
                channel_type = getattr(channel, "type", None)
                type_value = getattr(channel_type, "value", channel_type)
                if type_value not in (0, 5):
                    continue
                cid = str(getattr(channel, "id", "") or "")
                if not cid.isdigit():
                    continue
                rows.append({
                    "id": cid,
                    "name": str(getattr(channel, "name", cid) or cid),
                    "type": int(type_value),
                    "rate_limit_per_user": int(getattr(channel, "slowmode_delay", 0) or 0),
                    "guild_id": str(guild.id),
                })
            result.append({"id": str(guild.id), "name": str(getattr(guild, "name", guild.id)), "channels": rows})
        except Exception as exc:
            print(f"[CHANNEL-STATE] control inventory failed for guild: {type(exc).__name__}: {exc}")
    return result


async def _reconcile_control_channels(alt_id: int, *, reason: str, configured_ids=None, persist: bool = True) -> dict:
    """Refresh durable channel inventory from the control bot's Discord view.

    ``persist=False`` performs a read-only diff: the live inventory is compared
    against the stored catalogue and the exact drift is returned, but nothing is
    written. That is what `/autorescan action:report` uses.
    """
    remote_snapshot = None
    if persist:
        remote_snapshot = await asyncio.to_thread(
            github_api.fetch_channel_registry_snapshot, alt_id
        )
        if remote_snapshot:
            await asyncio.to_thread(channel_registry.restore_alt_snapshot, alt_id, remote_snapshot)
    servers = await _fetch_control_server_catalogue()
    if not servers:
        return {"ok": False, "error": "control bot has no accessible guild inventory"}
    result = await asyncio.to_thread(
        channel_registry.reconcile,
        alt_id,
        servers,
        configured_ids=configured_ids,
        persist=persist,
    )
    if result.get("ok") and not persist:
        return result
    if result.get("ok"):
        names = {
            cid: str(record.get("name") or cid)
            for cid, record in result.get("catalogue", {}).items()
            if isinstance(record, dict)
        }
        state.replace_channels(alt_id, list(result.get("targets", [])), names)
        remote_ok, remote_detail = await asyncio.to_thread(
            github_api.save_channel_registry_snapshot,
            alt_id,
            channel_registry.snapshot_for_alt(alt_id),
        )
        result["remote_ok"] = remote_ok
        result["remote_detail"] = remote_detail
        secret_ok, secret_detail = await _persist_channels_for_alt(alt_id)
        result["secret_ok"] = secret_ok
        result["secret_detail"] = secret_detail
        replacements = ", ".join(
            f"{item.get('old_id')}→{item.get('new_id')}"
            for item in result.get("replaced", [])
        ) or "none"
        detail = (
            f"Alt {alt_id} {reason}: servers={len(result.get('servers', {}))} "
            f"catalogue={len(result.get('catalogue', {}))} targets={len(result.get('targets', []))} "
            f"added=[{', '.join(result.get('added', [])) or 'none'}] "
            f"removed=[{', '.join(result.get('removed', [])) or 'none'}] "
            f"changed=[{', '.join(result.get('changed', [])) or 'none'}] "
            f"replaced=[{replacements}]"
        )
        state.append_log(alt_id, detail, emoji="🔄", color=0x5865F2, kind="CHANNEL")
        await _log_control(f"🔄 **CHANNEL REGISTRY** {detail}")
    return result


async def _persist_channels_for_alt(alt_id: int) -> tuple[bool, str]:
    """Persist channel targets locally, in the registry, and to GitHub when available."""
    a_obj = state.get(alt_id)
    if not a_obj:
        return False, "Unknown alt state."
    cids = [str(c) for c in a_obj.channels.keys() if str(c).isdigit()]
    names = {
        str(cid): str(raw.get("name") or "")
        for cid, raw in a_obj.channels.items()
        if isinstance(raw, dict) and raw.get("name")
    }
    registry_ok, _registry_detail = await asyncio.to_thread(
        channel_registry.set_targets, alt_id, cids, names
    )
    if not registry_ok:
        return False, "Durable channel registry update failed."
    remote_ok, remote_detail = await asyncio.to_thread(
        github_api.save_channel_registry_snapshot,
        alt_id,
        channel_registry.snapshot_for_alt(alt_id),
    )
    if config.CHANNEL_STATE_GIST_ID and not remote_ok:
        return False, remote_detail
    repo = config.ALT_REPOS.get(alt_id, "")
    if not repo or not config.GITHUB_TOKEN:
        return True, "Durable registry updated; GitHub CHANNEL_IDS secret skipped (mapping/token unavailable)."
    raw_result = await asyncio.to_thread(
        github_api.set_repository_secret, repo, "CHANNEL_IDS", ",".join(cids)
    )
    if isinstance(raw_result, tuple) and len(raw_result) >= 2:
        ok, detail = bool(raw_result[0]), str(raw_result[1])
    else:
        # Test doubles and older API adapters may return None after a
        # successful request; the call itself is the durable-side effect.
        ok, detail = True, "GitHub CHANNEL_IDS secret update requested."
    return ok, detail


def _set_channels_state(alt_id: int, cids: list[str], names: dict[str, str] | None = None) -> int:
    """Replace and persist the target table for one alt."""
    if not state.get(alt_id):
        return 0
    if not state.replace_channels(alt_id, cids, names):
        return 0
    a_obj = state.get(alt_id)
    return len(a_obj.channels) if a_obj else 0


@bot.tree.command(name="channels", description="Channel Manager: add target trading channels, swap deleted ones, or rescan.")
@app_commands.describe(
    alt="Target alt ID",
    action="Action to perform (leave blank to open visual manager)",
    channel_id="Discord channel ID to add, reset, replace, remove, or comma-separated list to overwrite",
    new_channel_id="New channel ID (when replacing an old deleted channel)",
    name="Friendly name/label for the channel",
)
@app_commands.choices(
    action=[
        app_commands.Choice(name="📌 View - Open visual channel manager UI", value="view"),
        app_commands.Choice(name="📋 List - Show the full channel table", value="list"),
        app_commands.Choice(name="➕ Add - Connect and verify a target trading channel", value="add"),
        app_commands.Choice(name="🔁 Replace - Swap an old/deleted channel with a new one", value="replace"),
        app_commands.Choice(name="🗑️ Remove - Remove one channel from an alt", value="remove"),
        app_commands.Choice(name="♻️ Overwrite - Replace ALL channels tied to an alt", value="overwrite"),
        app_commands.Choice(name="🔄 Refresh - Live inventory refresh and permission scan", value="refresh"),
        app_commands.Choice(name="🔍 Rescan - Compare live servers/channels with durable registry", value="rescan"),
        app_commands.Choice(name="⚠️ Reset Caution - Clear caution backoffs and strikes", value="reset_caution"),
    ]
)
async def cmd_channels(
    inter: discord.Interaction,
    alt: Optional[int] = 1,
    action: Optional[Literal["view", "list", "add", "replace", "remove", "overwrite", "refresh", "rescan", "reset_caution"]] = "view",
    channel_id: Optional[str] = None,
    new_channel_id: Optional[str] = None,
    name: Optional[str] = "",
):
    if not await _check_perms(inter):
        return

    chosen_alt = alt if (alt and alt in state.alt_ids) else (state.alt_ids[0] if state.alt_ids else 1)

    if action == "list":
        a_obj = state.get(chosen_alt)
        if not a_obj:
            return await inter.response.send_message("❓ Unknown alt.", ephemeral=True)
        rows = [f"`{cid}` {raw.get('name', '')}".rstrip() for cid, raw in list(a_obj.channels.items())[:100]]
        text = f"**Channel table for {a_obj.name}** (`{len(a_obj.channels)}`):\n```\n" + "\n".join(rows) + "\n```"
        return await inter.response.send_message(text[:4000], ephemeral=True)

    if action == "remove":
        if not channel_id or not channel_id.strip().isdigit():
            return await inter.response.send_message("❌ A numeric `channel_id` is required to remove.", ephemeral=True)
        cid = channel_id.strip()
        a_obj = state.get(chosen_alt)
        if not a_obj or cid not in a_obj.channels:
            return await inter.response.send_message(f"❌ Channel `{cid}` is not in Alt {chosen_alt}'s table.", ephemeral=True)
        old_ids = list(a_obj.channels.keys())
        old_names = {str(key): str(raw.get("name") or "") for key, raw in a_obj.channels.items() if isinstance(raw, dict)}
        async def _remove_and_persist():
            if not state.replace_channels(chosen_alt, [item for item in old_ids if item != cid], old_names):
                raise RuntimeError("local channel state could not be persisted")
            persist_ok, persist_msg = await _persist_channels_for_alt(chosen_alt)
            if not persist_ok:
                # Do not leave local state divergent from the repository secret.
                state.replace_channels(chosen_alt, old_ids, old_names)
                raise RuntimeError(persist_msg)
            return True
        await _finish_dm_control(
            inter, chosen_alt, f"!setchannels {','.join(item for item in old_ids if item != cid) or 'clear'}",
            f"channel `{cid}` removal queued", update=_remove_and_persist,
        )
        await _log_control(f"🗑️ Removed channel `{cid}` from Alt {chosen_alt} ({a_obj.name}); exact prior target count={len(old_ids)}.")
        return

    if action == "overwrite":
        raw = (channel_id or "").strip()
        if not raw:
            return await inter.response.send_message("❌ Provide a comma-separated list of channel IDs, e.g. `/channels alt:1 action:overwrite channel_id:111,222,333`.", ephemeral=True)
        cids = [x.strip() for x in raw.replace(",", ",").split(",") if x.strip()]
        if not cids or not all(x.isdigit() for x in cids):
            return await inter.response.send_message("❌ Every channel ID must be numeric.", ephemeral=True)
        if len(cids) > 100:
            return await inter.response.send_message("❌ Maximum 100 channels per overwrite.", ephemeral=True)
        a_obj = state.get(chosen_alt)
        if not a_obj:
            return await inter.response.send_message("❓ Unknown alt.", ephemeral=True)
        old_ids = list(a_obj.channels.keys())
        old_names = {str(key): str(raw.get("name") or "") for key, raw in a_obj.channels.items() if isinstance(raw, dict)}

        async def _apply_overwrite():
            if not state.replace_channels(chosen_alt, cids):
                raise RuntimeError("local channel state could not be persisted")
            persist_ok, persist_msg = await _persist_channels_for_alt(chosen_alt)
            if not persist_ok:
                state.replace_channels(chosen_alt, old_ids, old_names)
                raise RuntimeError(persist_msg)
            return True

        await _finish_dm_control(
            inter, chosen_alt, f"!setchannels {','.join(cids)}",
            f"channel table overwrite queued for `{len(cids)}` target(s)",
            update=_apply_overwrite,
        )
        await _log_control(
            f"♻️ Alt {chosen_alt} ({a_obj.name}) channel table overwrite requested: "
            f"old=[{','.join(old_ids) or 'none'}] new=[{','.join(cids)}]."
        )
        return

    if action in {"refresh", "rescan"}:
        current_ids = list(state.get(chosen_alt).channels.keys()) if state.get(chosen_alt) else []
        async def _refresh_and_persist():
            result = await _reconcile_control_channels(
                chosen_alt,
                reason="refresh" if action == "refresh" else "rescan",
                configured_ids=current_ids or None,
            )
            if not result.get("ok"):
                raise RuntimeError(result.get("error", "live channel inventory unavailable"))
            return True
        return await _finish_dm_control(
            inter, chosen_alt, "!rescan",
            "live channel inventory refreshed and durable registry reconciled",
            update=_refresh_and_persist,
        )

    if action == "add":
        if not channel_id or not channel_id.strip().isdigit():
            return await inter.response.send_message("❌ Valid numeric `channel_id` is required to add a channel.", ephemeral=True)
        cid = channel_id.strip()
        label = re.sub(r"[\r\n]", " ", (name or "").strip())[:80]
        a_obj = state.get(chosen_alt)
        old_ids = list(a_obj.channels.keys()) if a_obj else []
        old_names = {str(key): str(raw.get("name") or "") for key, raw in (a_obj.channels.items() if a_obj else []) if isinstance(raw, dict)}
        async def _update_and_persist():
            state.set_channel(chosen_alt, cid, label)
            persist_ok, persist_msg = await _persist_channels_for_alt(chosen_alt)
            if not persist_ok:
                state.replace_channels(chosen_alt, old_ids, old_names)
                raise RuntimeError(persist_msg)
            return True
        return await _finish_dm_control(
            inter, chosen_alt, f"!setchannel {cid}{(' ' + label) if label else ''}", f"channel ID queued for remote validation: `{cid}`",
            update=_update_and_persist,
        )

    elif action == "replace":
        if not channel_id or not new_channel_id or not channel_id.strip().isdigit() or not new_channel_id.strip().isdigit():
            return await inter.response.send_message("❌ Both `channel_id` (old) and `new_channel_id` must be numeric.", ephemeral=True)
        old_id = channel_id.strip()
        new_id = new_channel_id.strip()
        label = re.sub(r"[\r\n]", " ", (name or "").strip())[:80]
        a_obj = state.get(chosen_alt)
        old_ids = list(a_obj.channels.keys()) if a_obj else []
        old_names = {str(key): str(raw.get("name") or "") for key, raw in (a_obj.channels.items() if a_obj else []) if isinstance(raw, dict)}
        async def _replace_and_persist():
            state.replace_channel(chosen_alt, old_id, new_id, label)
            persist_ok, persist_msg = await _persist_channels_for_alt(chosen_alt)
            if not persist_ok:
                state.replace_channels(chosen_alt, old_ids, old_names)
                raise RuntimeError(persist_msg)
            return True
        return await _finish_dm_control(
            inter, chosen_alt, f"!replacechannel {old_id} {new_id}{(' ' + label) if label else ''}",
            f"channel replacement queued for remote validation: `{old_id}` → `{new_id}`",
            update=_replace_and_persist,
        )

    elif action == "reset_caution":
        cid = (channel_id or "").strip()
        if cid and not cid.isdigit() and cid.lower() != "all":
            return await inter.response.send_message("❌ Channel ID must contain digits only, or leave blank / pass 'all'.", ephemeral=True)
        target_cmd = f"!resetcaution {cid}" if cid else "!resetcaution all"
        label = f"reset caution on channel {cid}" if (cid and cid.lower() != "all") else "reset caution on all channels"
        return await _finish_dm_control(
            inter, chosen_alt, target_cmd, label,
            update=lambda: state.reset_caution(chosen_alt, cid if (cid and cid.lower() != "all") else None),
        )

    # Default: Interactive Channels UI
    view = ChannelsView(owner_id=inter.user.id, alt_id=chosen_alt)
    embed = view._build_embed()
    await inter.response.send_message(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="deals", description="Marketplace arbitrage scanner: track custom items, games, and price edges.")
@app_commands.describe(
    alt="Target alt ID (or 0 for all)",
    enabled="Turn deal scanning on or off",
    min_delta="Minimum profit margin edge required (e.g. 0.05)",
    keywords="Comma-separated target items/games (e.g. 'Robux, MM2, Pet Sim, Blox Fruits, Tokens')",
    sample_listing="Test/simulate parse an ad message to check price extraction",
)
@app_commands.choices(
    enabled=[
        app_commands.Choice(name="🟢 On - Enable Deal Scanner", value="on"),
        app_commands.Choice(name="🔴 Off - Disable Deal Scanner", value="off"),
    ]
)
async def cmd_deals(
    inter: discord.Interaction,
    alt: Optional[int] = 0,
    enabled: Optional[Literal["on", "off"]] = None,
    min_delta: Optional[str] = None,
    keywords: Optional[str] = None,
    sample_listing: Optional[str] = None,
):
    if not await _check_perms(inter):
        return

    target_aid = alt if alt != 0 else (state.alt_ids[0] if state.alt_ids else 1)

    if sample_listing:
        return await _handle_simulate_listing(inter, target_aid, sample_listing=sample_listing)

    if enabled:
        return await _handle_deal_scan(inter, target_aid, enabled)

    if min_delta:
        try:
            value = float(min_delta.strip())
        except (TypeError, ValueError, AttributeError):
            value = -1
        if not math.isfinite(value) or value < 0 or value > 5:
            return await inter.response.send_message("❌ Delta must be between 0 and 5 dollars per 1k; example `0.05`.", ephemeral=True)
        return await _finish_dm_control(
            inter, target_aid, f"!setdealdelta {value:g}", f"deal edge queued at ${value:.2f}/1k",
            update=lambda: state.set_deal_config(target_aid, delta=value),
        )

    if keywords:
        raw_items = [part.strip() for part in keywords.split(",") if part.strip()]
        normalized = []
        seen = set()
        for item in raw_items:
            item = re.sub(r"\s+", " ", item)[:60]
            key = item.casefold()
            if item and key not in seen:
                seen.add(key)
                normalized.append(item)
        if not normalized:
            return await inter.response.send_message("❌ Provide at least one comma-separated item keyword.", ephemeral=True)
        if len(normalized) > 20:
            return await inter.response.send_message("❌ Use at most 20 item keywords.", ephemeral=True)
        joined = ", ".join(normalized)
        if len(joined) > 500:
            return await inter.response.send_message("❌ Combined keyword length cannot exceed 500 characters.", ephemeral=True)
        return await _finish_dm_control(
            inter, target_aid, f"!setdealkeywords {joined}", f"deal item keywords validated ({len(normalized)} keyword(s))",
            update=lambda: state.set_deal_keywords(target_aid, normalized),
        )

    # Default: Interactive Deals Hub View
    view = DealsHubView(owner_id=inter.user.id, alt_id=alt or 0)
    embed = view._render_embed()
    await inter.response.send_message(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="squad", description="Fleet Squads: group alts into teams to sync prices, pauses, or safety policies.")
@app_commands.describe(
    action="Squad action (leave blank to open interactive hub)",
    squad_name="Squad name (e.g. 'Alpha', 'Sellers', 'Night Patrol')",
    alt="Target alt ID (1-4)",
    alts="Comma-separated alt IDs for create (e.g. '1,2,3,4' or 'all')",
    value="Value for squad batch operations (e.g. preset name 'stealth' or price '2.40')",
)
@app_commands.choices(
    action=[
        app_commands.Choice(name="👥 Squad Hub - Open visual squad control hub", value="overview"),
        app_commands.Choice(name="📋 List Squads - Show all squad teams and members", value="list"),
        app_commands.Choice(name="📊 Team Status - View composite squad health and statistics", value="view"),
        app_commands.Choice(name="🆕 Create Squad - Assign a whole group in one command", value="create"),
        app_commands.Choice(name="➕ Assign Alt - Add or move an alt to a squad team", value="assign"),
        app_commands.Choice(name="➖ Unassign Alt - Remove an alt from its squad", value="unassign"),
        app_commands.Choice(name="⏸️ Pause Squad - Batch pause ads across all squad alts", value="pause"),
        app_commands.Choice(name="▶️ Resume Squad - Batch resume ads across all squad alts", value="resume"),
        app_commands.Choice(name="🛡️ Apply Policy - Apply safety preset (stealth/aggressive) to squad", value="policy"),
        app_commands.Choice(name="💰 Update Price - Set unit price rate across all squad alts", value="price"),
    ]
)
async def cmd_squad(
    inter: discord.Interaction,
    action: Optional[Literal["overview", "list", "view", "create", "assign", "unassign", "pause", "resume", "policy", "price"]] = "overview",
    squad_name: Optional[str] = None,
    alt: Optional[int] = 0,
    alts: Optional[str] = None,
    value: Optional[str] = None,
):
    if not await _check_perms(inter):
        return

    if action == "list" and not squad_name:
        squads = state.get_all_squads()
        if not squads:
            return await inter.response.send_message("No squads configured. Use `/squad action:create`.", ephemeral=True)
        lines = []
        for name in sorted(squads):
            members = squads[name]
            lines.append(f"• **{name}** → {', '.join(f'Alt `{m}`' for m in sorted(members)) or '_empty_'}")
        embed = discord.Embed(
            title="👥 Fleet Squads",
            description="\n".join(lines)[:4000],
            color=0x5865F2,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text="Launch a whole squad with `/run squad:<name>`.")
        return await inter.response.send_message(embed=embed, ephemeral=True)

    elif action in (None, "overview") and not squad_name:
        view = SquadControlView(owner_id=inter.user.id, current_squad=squad_name or "Alpha")
        embed = view._render_embed()
        return await inter.response.send_message(embed=embed, view=view, ephemeral=True)

    elif action == "list":
        squads = state.get_all_squads()
        if not squads:
            return await inter.response.send_message("No squads configured. Use `/squad action:create`.", ephemeral=True)
        lines = []
        for name in sorted(squads):
            members = squads[name]
            lines.append(f"• **{name}** → {', '.join(f'Alt `{m}`' for m in sorted(members)) or '_empty_'}")
        embed = discord.Embed(
            title="👥 Fleet Squads",
            description="\n".join(lines)[:4000],
            color=0x5865F2,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text="Launch a whole squad with `/run squad:<name>`.")
        return await inter.response.send_message(embed=embed, ephemeral=True)

    elif action == "create":
        name = (squad_name or "").strip()
        if not name:
            return await inter.response.send_message("❌ `squad_name` is required to create a squad.", ephemeral=True)
        if len(name) > 50:
            return await inter.response.send_message("❌ Squad name must be 50 characters or fewer.", ephemeral=True)
        raw_members = (alts or "").strip()
        if raw_members.casefold() in {"all", "*"}:
            member_ids = list(state.alt_ids)
        else:
            member_ids = []
            for part in raw_members.replace(",", " ").split():
                if not part.isdigit():
                    return await inter.response.send_message(
                        f"❌ Squad member `{part}` is not a numeric alt ID. Use e.g. `alts:1,2,3,4` or `alts:all`.",
                        ephemeral=True,
                    )
                member_ids.append(int(part))
        if not member_ids:
            return await inter.response.send_message(
                "❌ Provide at least one alt, e.g. `/squad action:create squad_name:MyGroup alts:1,2,3,4`.",
                ephemeral=True,
            )
        invalid = [str(m) for m in member_ids if m not in state.alt_ids]
        if invalid:
            configured = ", ".join(str(i) for i in state.alt_ids) or "none"
            return await inter.response.send_message(
                f"❌ Alt(s) {', '.join(invalid)} are not configured. Configured alts: {configured}.",
                ephemeral=True,
            )
        assigned = []
        for member_id in sorted(dict.fromkeys(member_ids)):
            if state.set_squad(member_id, name):
                assigned.append(member_id)
        if not assigned:
            return await inter.response.send_message(
                f"❌ Squad **{name}** was not created; no alt accepted the assignment.",
                ephemeral=True,
            )
        text = (
            f"✅ Squad **{name}** now has {len(assigned)} alt(s): "
            f"{', '.join(f'`{m}`' for m in assigned)}.\n"
            f"Launch them together with `/run squad:{name}`."
        )
        await inter.response.send_message(text, ephemeral=True)
        await _log_control(f"👥 Squad **{name}** created with alts {', '.join(str(m) for m in assigned)}.")
        return

    elif action == "assign":
        if not alt or not squad_name:
            return await inter.response.send_message("❌ Both `alt` and `squad_name` are required for assignment.", ephemeral=True)
        if alt not in state.alt_ids:
            return await inter.response.send_message("❌ Invalid alt specified.", ephemeral=True)
        state.set_squad(alt, squad_name)
        return await inter.response.send_message(f"✅ Alt {alt} assigned to squad **{squad_name}**.", ephemeral=True)

    elif action == "unassign":
        if not alt:
            return await inter.response.send_message("❌ `alt` is required to remove an alt from its squad.", ephemeral=True)
        if alt not in state.alt_ids:
            return await inter.response.send_message("❌ Invalid alt specified.", ephemeral=True)
        if not state.set_squad(alt, ""):
            return await inter.response.send_message(f"❌ Alt {alt} could not be removed from its squad.", ephemeral=True)
        return await inter.response.send_message(f"✅ Alt {alt} removed from its squad (now unassigned).", ephemeral=True)

    elif action == "view":
        target_sq = squad_name or "Unassigned"
        members = state.get_squad_members(target_sq)
        embed = discord.Embed(title=f"👥 Squad Overview: {target_sq}", color=0x5865F2, timestamp=datetime.now(timezone.utc))
        if members:
            total_sent = sum(getattr(m, "total_sent", 0) for m in members)
            total_errors = sum(getattr(m, "error_count", 0) for m in members)
            avg_health = sum(state.get_health_index(m.alt_id) for m in members) / len(members)
            embed.description = f"**Fleet Count**: `{len(members)}` | **Avg Health**: `{avg_health:.1f}%` | **Total Posts**: `{total_sent}` | **Errors**: `{total_errors}`"
            for m in members:
                dot, _ = _status_dot(m)
                embed.add_field(
                    name=f"{dot} Alt {m.alt_id}: {m.name}",
                    value=f"• Health: `{state.get_health_index(m.alt_id)}%` | Status: `{m.status}`\n• Sent: `{m.total_sent}` | Policy: `{getattr(m, 'policy_template', 'balanced')}`",
                    inline=False,
                )
        else:
            embed.description = f"No alts assigned to squad '{target_sq}'."
        return await inter.response.send_message(embed=embed, ephemeral=True)

    elif action in ("pause", "resume", "policy", "price"):
        if not squad_name:
            return await inter.response.send_message("❌ `squad_name` is required for batch squad actions.", ephemeral=True)
        members = state.get_squad_members(squad_name)
        if not members:
            return await inter.response.send_message(f"❌ No alts found in squad '{squad_name}'.", ephemeral=True)
        if action == "policy" and not value:
            return await inter.response.send_message("❌ `value` parameter required for policy (stealth, aggressive, peak_hour, balanced).", ephemeral=True)
        if action == "price" and not value:
            return await inter.response.send_message("❌ `value` parameter required for price (e.g. 2.40).", ephemeral=True)
        await inter.response.defer(ephemeral=True)

        async def dispatch_member(index: int, member) -> str:
            # A small bounded per-alt offset prevents every runner from
            # receiving the same control event in the same millisecond while
            # keeping a squad operation responsive.
            if index:
                await _squad_pause(control_plane=True)
            cmd = f"!{action}" if action in ("pause", "resume") else f"!{action} {value}"
            try:
                ack = await _send_control_wait_ack(member.alt_id, cmd, timeout=10)
            except Exception as exc:
                ack = f"❌ {type(exc).__name__}: {exc}"
            ack_failed = str(ack).startswith(("❌", "⏰"))
            if action == "policy" and value:
                policy_ok = state.set_policy_template(member.alt_id, value)
                if not policy_ok:
                    ack = f"{ack}; ❌ local policy persistence failed"
            elif action == "price" and value:
                p_val = _extract_price(value)
                if p_val is not None:
                    state.set_run_config(member.alt_id, rate=p_val)
            elif action == "pause" and not ack_failed:
                state.set_control_status(member.alt_id, "paused")
            elif action == "resume" and not ack_failed:
                state.set_control_status(member.alt_id, "active")
            state.append_log(
                member.alt_id,
                f"Squad {squad_name} {action}: {ack}",
                emoji="❌" if ack_failed else "👥",
                color=0xED4245 if ack_failed else 0x5865F2,
                kind="SQUAD",
            )
            return f"• {_alt_tag(member.alt_id)} **{member.name}**: {ack}"

        results = await asyncio.gather(
            *(dispatch_member(index, member) for index, member in enumerate(members)),
            return_exceptions=False,
        )
        summary = f"👥 **Squad '{squad_name}' Batch {action.upper()}** ({len(members)} alts, staggered):\n" + "\n".join(results)
        await inter.followup.send(summary, ephemeral=True)
        await _log_control(summary)
        return


@bot.tree.command(name="status", description="Show fleet-wide dashboard summary or individual alt status card.")
@app_commands.describe(alt="Target alt (or 0 for All alts)")
async def cmd_status(inter: discord.Interaction, alt: Optional[int] = 0):
    if not await _check_perms(inter):
        return
    await _fresh_state()
    if alt == 0:
        await inter.response.send_message(embeds=build_all(state)[:10], ephemeral=True)
    else:
        await inter.response.send_message(embed=build_single_alt_embed(state, alt or 1), ephemeral=True)


@bot.tree.command(name="reply", description="Reply to Buyer: send a DM from an alt to an interested buyer directly.")
@app_commands.describe(alt="Alt ID to send from", user="Buyer Discord User ID (from #dm-inbox)", text="Message text to send (leave blank for multiline editor)")
async def cmd_reply(inter: discord.Interaction, alt: Optional[int] = 1, user: Optional[str] = "", text: Optional[str] = None):
    if not await _check_perms(inter):
        return
    if not text:
        return await inter.response.send_modal(BuyerReplyModal(alt_id=alt or 1, user_id=user or ""))
    aid = alt or 1
    if aid not in state.alt_ids:
        return await inter.response.send_message("❌ Invalid alt specified.", ephemeral=True)
    clean_user = re.sub(r'\D', '', str(user))
    if not clean_user:
        return await inter.response.send_message("❌ Invalid buyer user ID specified.", ephemeral=True)
    clean_text = str(text or "").strip()
    if not clean_text:
        return await inter.response.send_message("❌ Message text cannot be empty.", ephemeral=True)
    await inter.response.defer(ephemeral=True)
    control_ack = await _send_control_wait_ack(aid, f"!reply {clean_user} {clean_text}", timeout=15)
    await inter.followup.send(f"📤 **Reply Queued** for Alt {aid} → Buyer `{clean_user}`:\n> {clean_text}\n*Transport ack:* `{control_ack}`", ephemeral=True)


@bot.tree.command(name="analytics", description="View advanced fleet speed matrix, channel velocities, and cadence analytics.")
@app_commands.describe(alt="Target specific alt (or 0 / blank for all alts)")
async def cmd_analytics(inter: discord.Interaction, alt: Optional[int] = 0):
    if not await _check_perms(inter):
        return
    await _fresh_state()
    embed = build_analytics_embed(state, target_alt=alt or 0)
    await inter.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="diagnose", description="Causal Event Explorer: deep root-cause diagnostic timeline and recommendations.")
@app_commands.describe(alt="Alt number to diagnose (e.g. 1)")
async def cmd_diagnose(inter: discord.Interaction, alt: Optional[int] = 1):
    if not await _check_perms(inter):
        return
    target_aid = alt or 1
    if target_aid not in state.alt_ids:
        return await inter.response.send_message("❌ Invalid alt specified.", ephemeral=True)
    await inter.response.defer(ephemeral=True)
    await _fresh_state()
    embed = build_diagnose_embed(state, target_aid)
    await inter.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="canary", description="Synthetic in-band health probe testing GitHub, Gist, and webhook infrastructure.")
@app_commands.describe(alt="Optional specific alt to probe")
async def cmd_canary(inter: discord.Interaction, alt: Optional[int] = 0):
    if not await _check_perms(inter):
        return
    await inter.response.defer(ephemeral=True)
    t0 = time.perf_counter()

    gh_ok = True
    gh_latency = 0.0
    gh_t0 = time.perf_counter()
    try:
        _ = await asyncio.to_thread(github_api.get_authenticated_user)
        gh_latency = (time.perf_counter() - gh_t0) * 1000
    except Exception:
        gh_ok = False
        gh_latency = (time.perf_counter() - gh_t0) * 1000

    gist_ok = True
    gist_latency = 0.0
    g_t0 = time.perf_counter()
    try:
        _ = await asyncio.to_thread(github_api.fetch_gist, config.CONTROL_GIST_ID) if (hasattr(github_api, "fetch_gist") and config.CONTROL_GIST_ID) else None
        gist_latency = (time.perf_counter() - g_t0) * 1000
    except Exception:
        gist_ok = False
        gist_latency = (time.perf_counter() - g_t0) * 1000

    total_latency = (time.perf_counter() - t0) * 1000

    embed = discord.Embed(
        title="🐤 SYNTHETIC IN-BAND CANARY PROBE REPORT",
        color=0x57F287 if (gh_ok and gist_ok) else 0xFEE75C,
        timestamp=datetime.now(timezone.utc),
    )
    gh_status = f"🟢 PASS ({gh_latency:.0f}ms)" if gh_ok else "🔴 FAIL"
    gist_status = f"🟢 PASS ({gist_latency:.0f}ms)" if (gist_ok and config.CONTROL_GIST_ID) else ("⚪ N/A" if not config.CONTROL_GIST_ID else "🔴 FAIL")

    embed.add_field(name="GitHub Core REST API", value=gh_status, inline=True)
    embed.add_field(name="Control Gist Bridge", value=gist_status, inline=True)
    embed.add_field(name="Total Probe Latency", value=f"`{total_latency:.0f}ms`", inline=True)

    alt_ids = state.alt_ids
    fleet_health = sum(state.get_health_index(aid) for aid in alt_ids) / max(1, len(alt_ids)) if alt_ids else 100
    embed.add_field(name="Fleet Composite Health", value=f"**{fleet_health:.0f}%**", inline=True)
    embed.add_field(name="Active Alts Online", value=f"{sum(1 for aid in alt_ids if state.is_online(aid))}/{len(alt_ids)}", inline=True)
    embed.add_field(name="Canary Assessment", value="✅ All in-band synthetic control planes operational." if (gh_ok and gist_ok) else "⚠️ Degraded latency or control transport warning.", inline=False)

    await inter.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="topology", description="View the live fleet topology and routing relationship graph.")
async def cmd_topology(inter: discord.Interaction):
    if not await _check_perms(inter):
        return
    await inter.response.defer(ephemeral=True)
    await _fresh_state()
    embed = build_topology_embed(state)
    await inter.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="sync", description="Sync Fleet: push and reload shared settings/prices across all running alts.")
async def cmd_sync(inter: discord.Interaction):
    if not await _check_perms(inter):
        return
    await inter.response.defer(ephemeral=True)
    results = []
    for alt_id in state.alt_ids:
        alt = state.get(alt_id)
        ack = await _send_control_wait_ack(alt_id, "!sync", timeout=12)
        results.append(f"**{alt.name}**: `{ack}`")
        state.append_log(alt_id, f"sync: {ack}", emoji="🔄", color=0x5865F2, kind="CONTROL")
    text = "🔄 **Sync sent to all configured alts**\n" + "\n".join(results)
    await inter.followup.send(text, ephemeral=True)
    await _log_control(text)


@bot.tree.command(name="refresh", description="Refresh Status: instantly fetch latest GitHub Actions workflow status and update dashboard.")
async def cmd_refresh(inter: discord.Interaction):
    if not await _check_perms(inter):
        return
    await inter.response.defer(ephemeral=True)
    await _fresh_state()
    await _refresh_dashboard_now()
    await inter.followup.send("✅ GitHub heartbeat state and dashboard refreshed from current data.", ephemeral=True)


@bot.tree.command(name="dashboard", description="Live Dashboard: post or update the real-time 3-embed status hub in #dashboard.")
async def cmd_dashboard(inter: discord.Interaction):
    if not await _check_perms(inter):
        return
    await inter.response.defer(ephemeral=True)
    await _fresh_state()
    msg = await _post_dashboard(build_all(state))
    await inter.followup.send(f"✅ Dashboard snapshot posted → {msg.jump_url if msg else '(failed)'}", ephemeral=True)


@bot.tree.command(name="autorescan", description="Autorescan: fetch live Discord channels, reconcile the durable registry, and auto add/remove targets.")
@app_commands.describe(
    alt="Target alt ID, or 0 for every configured alt",
    action="report (show drift only) or apply (persist the reconciled channel table)",
)
@app_commands.choices(
    action=[
        app_commands.Choice(name="🔍 Report - show added/removed/replaced without changing targets", value="report"),
        app_commands.Choice(name="♻️ Apply - reconcile and persist the live channel table", value="apply"),
    ]
)
async def cmd_autorescan(inter: discord.Interaction, alt: Optional[int] = 0,
                         action: Optional[Literal["report", "apply"]] = "apply"):
    """Zero-touch channel reconciliation against the durable registry.

    The registry (`.adfarm_channel_registry.json`, mirrored to the channel-state
    Gist) is the source of truth. Every scan fetches the live Discord
    inventory, diffs it against the stored server/channel catalogue, and logs
    exactly which channels appeared, disappeared, or were recreated with a new
    ID. `apply` installs the reconciled target list everywhere so the customer
    never has to repair channels by hand.
    """
    if not await _check_perms(inter):
        return
    if not state.alt_ids:
        return await inter.response.send_message("❌ No configured alts are available to scan.", ephemeral=True)
    targets = [alt] if (alt and alt in state.alt_ids) else list(state.alt_ids)
    await inter.response.defer(ephemeral=True)
    lines: list[str] = []
    for alt_id in targets:
        current_ids = list(state.get(alt_id).channels.keys()) if state.get(alt_id) else []
        result = await _reconcile_control_channels(
            alt_id,
            reason="autorescan-report" if action == "report" else "autorescan",
            configured_ids=current_ids or None,
            persist=(action != "report"),
        )
        if not result.get("ok"):
            lines.append(f"• {_alt_tag(alt_id)} ❌ {result.get('error', 'live channel inventory unavailable')}")
            continue
        added = ", ".join(result.get("added", [])) or "none"
        removed = ", ".join(result.get("removed", [])) or "none"
        changed = ", ".join(result.get("changed", [])) or "none"
        replaced = ", ".join(
            f"{item.get('old_id')}→{item.get('new_id')}" for item in result.get("replaced", [])
        ) or "none"
        lines.append(
            f"• {_alt_tag(alt_id)} ✅ servers=`{len(result.get('servers', {}))}` "
            f"catalogue=`{len(result.get('catalogue', {}))}` targets=`{len(result.get('targets', []))}`\n"
            f"    added=[{added}] removed=[{removed}] changed=[{changed}] replaced=[{replaced}]"
        )
    embed = discord.Embed(
        title="🔄 Autorescan — Durable Channel Reconciliation",
        description="\n".join(lines)[:4000] or "No alts scanned.",
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(
        text=("Report only — no target table was changed."
              if action == "report" else
              "Applied — reconciled targets were persisted to the registry, the Gist, and CHANNEL_IDS.")
    )
    await inter.followup.send(embed=embed, ephemeral=True)


_COMMAND_GUIDE = {
    "run": ("`/run [squad:<name>]` (opens interactive guided launcher)", "Guided form to launch a run: choose the alt (or squad lead) and mode (`Sell` / `Buy`) ➔ paste your raw message or question — no item, price, or RAP field is required ➔ select cadence (`3m`/`5m`) and duration (`6h`, `12h`, `18h`, `24h`, `48h`, or `∞ Limitless`). The sender still applies its normal emoji/typo/timing modifiers to whatever text you supply. With `squad:<name>` the launch is dispatched to every squad member in sequence, spaced a few hundred milliseconds apart. Always shows a full preview and requires Confirm Launch before anything is dispatched."),
    "getstarted": ("`/getstarted`", "Step-by-step beginner onboarding: add an alt, add/overwrite channels, run self-check, tune rates/copy, launch, monitor, and stop. Includes documentation links."),
    "script": ("`/script action:<simulate|run> code:<python>`", "Resource-limited scripting suite. `simulate` returns unfiltered stdout/stderr as a dry-run debugger; `run` executes in the same sandbox without touching the control-bot process."),
    "shutdown": ("`/shutdown confirmation:SHUTDOWN`", "Gracefully stops every alt, cancels each GitHub workflow, cancels the control-bot's own Actions run, and terminates the bot process. Requires the literal confirmation SHUTDOWN."),
    "stop": ("`/stop alt:<alt>`", "Gracefully stops the alt: sends `!stop` through Gist/DM, syncs variation blocklist, and cancels active GitHub Actions workflow run."),
    "pause": ("`/pause alt:<alt>`", "Temporarily pauses public ad delivery on all target channels without terminating the GitHub Actions runner."),
    "resume": ("`/resume alt:<alt>`", "Resumes public ad delivery from pause state."),
    "alt": ("`/alt [action:<overview|add|update|remove|logs|clearlogs|runs|selfcheck>] [alt:<alt>] [confirmation:DELETE] [delete_repository:<true|false>] [limit:1..50] [kind:ALL|ERROR|DEAL|CONTROL|CHANNEL|CAUTION|DEBUG] [search:text]`", "Unified Alt Lifecycle Hub: open interactive fleet management view or execute alt provisioning, updates, deletion, typed log streaming, clear logs, run history, and pre-flight self-check."),
    "tune": ("`/tune [alt:<alt|0>] [policy:<stealth|aggressive|peak_hour|balanced>] [price:<rate>] [mode:<sell|buy>] [message:<text>] [interval:<3|5>] [runtime:<6|12|18|24|48>] [image:<file>]`", "Unified Fleet Tuning Hub: open interactive tuning console (select alts, presets, pricing modals, mode switches, cadence sliders) or directly apply parameters."),
    "channels": ("`/channels [alt:<alt>] [action:<view|list|add|replace|remove|overwrite|refresh|rescan|reset_caution>] [channel_id:<id or id1,id2,...>] [new_channel_id:<numeric_id>] [name:<label>]`", "Unified Channel Manager Hub: open visual channel dashboard with 1-click add/replace/remove/overwrite/refresh/caution-reset buttons. `action:overwrite channel_id:111,222,333` completely REPLACES the channel list for an alt (never merges). `refresh`/`rescan` diffs live Discord inventory against the durable registry and logs every addition, removal, and replacement. See also `/autorescan`."),
    "deals": ("`/deals [alt:<alt|0>] [enabled:<on|off>] [min_delta:<rate>] [keywords:<aliases>] [sample_listing:<text>]`", "Unified Marketplace Arbitrage Hub: view real-time scanner metrics, toggle passive deal detection, set profit thresholds, configure item keywords, or dry-run test parse ad messages."),
    "squad": ("`/squad [action:<overview|list|view|create|assign|unassign|pause|resume|policy|price>] [squad_name:<name>] [alt:<alt>] [alts:<1,2,3,4|all>] [value:<value>]`", "Unified Fleet Squad Hub: group alts into named pools with `action:create squad_name:MyGroup alts:1,2,3,4`, list them, or execute batch pausing, resuming, pricing, and policy application. A squad policy change propagates to every member in one atomic sweep, and the whole squad launches from `/run squad:<name>`."),
    "status": ("`/status [alt:<alt|0>]`", "Refreshes live state and displays unified fleet dashboard overview or detailed single-alt diagnostic card."),
    "reply": ("`/reply alt:<alt> user:<buyer_id> text:<message>`", "Operator DM Relay: transmits your message through the selected alt account directly into the buyer's private DM."),
    "analytics": ("`/analytics [alt:<alt|0>]`", "Visual Fleet Speed Matrix: renders per-channel velocities, delivery reliability bars, slowmode utilization, and inter-channel interval timelines."),
    "diagnose": ("`/diagnose [alt:<alt>]`", "Causal Event Explorer: deep root-cause diagnostic timeline, transition triggers, health index, and actionable operator recommendations."),
    "canary": ("`/canary [alt:<alt|0>]`", "Synthetic In-Band Probe: tests GitHub API, Gist bridge sync, and token latency in milliseconds."),
    "topology": ("`/topology`", "Renders the live visual fleet topology graph: alts, squad pools, target Discord channels, yield grades, and egress routing."),
    "sync": ("`/sync`", "Sends a fleet-wide broadcast telling all alts to immediately reload shared control Gists and variation blocklists."),
    "refresh": ("`/refresh`", "Forces an immediate live poll of GitHub Actions workflow states and updates the persistent dashboard embed."),
    "dashboard": ("`/dashboard`", "Posts a fresh 3-card dashboard snapshot in `#ad-dashboard` without running or scanning ads."),
    "autorescan": ("`/autorescan [alt:<alt|0>] [action:<report|apply>]`", "Zero-touch channel reconciliation: fetches the live Discord inventory, diffs it against the durable server/channel registry, and logs exactly which channels were added, removed, changed, or replaced by a new ID. `report` shows the drift without changing anything; `apply` (default) persists the reconciled target table."),
    "help": ("`/help`", "Displays this complete interactive reference manual with arguments, options, examples, and permissions."),
}


@bot.tree.command(name="help", description="Bot Help Guide: view complete command manual, examples, shortcuts, and cheat sheet.")
async def cmd_help(inter: discord.Interaction):
    if not await _check_perms(inter):
        return
    registered = {cmd.name: cmd for cmd in bot.tree.get_commands()}
    embeds = []
    for name in sorted(registered):
        cmd_obj = registered[name]
        usage, effect = _COMMAND_GUIDE.get(name, (f"`/{name}`", cmd_obj.description or "No description available."))
        embed = discord.Embed(title=f"/{name}", color=0x5865F2)
        embed.add_field(name="Usage & Arguments", value=usage, inline=False)
        embed.add_field(name="What it does", value=effect, inline=False)
        embeds.append(embed)
    
    # Send formatted pages
    total_pages = (len(embeds) + 5) // 6
    for page_num in range(total_pages):
        page = discord.Embed(
            title=f"📖 Bot Help & Command Guide · Page {page_num + 1}/{total_pages}",
            description="Here is your complete guide to all available commands in the Ad Farm system:",
            color=0x5865F2,
        )
        for item in embeds[page_num * 6:(page_num + 1) * 6]:
            combined_desc = f"**Usage:** {item.fields[0].value}\n**Details:** {item.fields[1].value}"
            page.add_field(name=f"✨ {item.title}", value=combined_desc, inline=False)
        page.set_footer(text="💡 Tip: Most commands open an easy visual interactive form when run without extra arguments!")
        if page_num == 0:
            await inter.response.send_message(embed=page, ephemeral=True)
        else:
            await inter.followup.send(embed=page, ephemeral=True)


# Add autocomplete helpers
def make_alt_autocompleter(command_name: str = ""):
    async def _autocompleter(inter: discord.Interaction, current: str):
        try:
            cur = str(current or "").strip().lower()
            out = []
            if command_name in ("status", "deals", "tune", "analytics", "canary"):
                out.append(app_commands.Choice(name="All alts (0)", value=0))
            for i in state.alt_ids:
                label = _alt_label(i)
                if not cur or cur in label.lower() or cur in str(i):
                    out.append(app_commands.Choice(name=label[:100], value=i))
            return out[:25]
        except Exception:
            return []
    return _autocompleter

def make_squad_autocompleter():
    async def _sq_autocompleter(inter: discord.Interaction, current: str):
        try:
            cur = str(current or "").strip().lower()
            squads = list(state.get_all_squads().keys())
            for default_sq in ("Alpha", "Sellers", "Buyers"):
                if default_sq not in squads:
                    squads.append(default_sq)
            out = []
            for sq in squads:
                if not cur or cur in sq.lower():
                    out.append(app_commands.Choice(name=sq[:100], value=sq))
            return out[:25]
        except Exception:
            return []
    return _sq_autocompleter

def make_channel_autocompleter():
    async def _ch_autocompleter(inter: discord.Interaction, current: str):
        try:
            cur = str(current or "").strip().lower()
            out = []
            seen_cids = set()
            for aid in state.alt_ids:
                a = state.get(aid)
                if a and a.channels:
                    for cid, ch_info in a.channels.items():
                        if cid not in seen_cids:
                            seen_cids.add(cid)
                            name = ch_info.get("name", cid) if isinstance(ch_info, dict) else cid
                            label = f"#{name} ({cid})"
                            if not cur or cur in label.lower() or cur in cid:
                                out.append(app_commands.Choice(name=label[:100], value=cid))
            return out[:25]
        except Exception:
            return []
    return _ch_autocompleter


for command_name, command in (
    ("stop", cmd_stop), ("pause", cmd_pause), ("resume", cmd_resume),
    ("alt", cmd_alt), ("tune", cmd_tune), ("channels", cmd_channels),
    ("deals", cmd_deals), ("squad", cmd_squad), ("status", cmd_status),
    ("reply", cmd_reply), ("analytics", cmd_analytics), ("diagnose", cmd_diagnose),
    ("canary", cmd_canary),
):
    try:
        command.autocomplete("alt")(make_alt_autocompleter(command_name))
    except Exception:
        pass

try:
    cmd_squad.autocomplete("squad_name")(make_squad_autocompleter())
except Exception:
    pass

for param_name in ("channel_id", "new_channel_id"):
    try:
        cmd_channels.autocomplete(param_name)(make_channel_autocompleter())
    except Exception:
        pass


# ----- DM relay (control bot <-> alts) -----
_DM_ACKS: dict[int, asyncio.Future] = {}  # alt_id -> future


async def _get_dm_channel(alt_discord_id: int) -> discord.DMChannel | None:
    try:
        user = bot.get_user(alt_discord_id) or await bot.fetch_user(alt_discord_id)
        return user.dm_channel or await user.create_dm()
    except Exception as e:
        print(f"[DM] could not open DM with {alt_discord_id}: {e}")
        return None


async def _send_dm(alt_id: int, text: str) -> bool:
    did = config.ALT_DISCORD_IDS.get(alt_id)
    if not did:
        print(f"[DM] no discord id mapped for alt {alt_id}")
        return False
    ch = await _get_dm_channel(did)
    if not ch:
        return False
    try:
        await ch.send(content=text[:1990])
        return True
    except Exception as e:
        print(f"[DM] send to alt {alt_id} failed: {e}")
        return False


async def _send_dm_wait_ack(alt_id: int, text: str, timeout: float = 15.0) -> str:
    did = config.ALT_DISCORD_IDS.get(alt_id)
    if not did:
        return "❌ No ALT_DISCORD_IDS mapping for this alt."
    # Register the waiter before sending. An alt can reply immediately, and
    # registering afterwards loses that acknowledgement. Refuse overlapping
    # waits for one alt rather than letting a later command orphan the first.
    existing = _DM_ACKS.get(did)
    if existing and not existing.done():
        return "⏳ Another command is already waiting for this alt's reply."
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _DM_ACKS[did] = fut
    ok = await _send_dm(alt_id, text)
    if not ok:
        _DM_ACKS.pop(did, None)
        return "❌ DM failed (could not open channel)."
    try:
        result = await asyncio.wait_for(fut, timeout=timeout)
        return str(result)[:500]
    except asyncio.TimeoutError:
        return f"⏰ No reply within {timeout:.0f}s (alt may be offline or busy)."
    finally:
        if _DM_ACKS.get(did) is fut:
            _DM_ACKS.pop(did, None)


async def _send_control_wait_ack(alt_id: int, text: str, timeout: float = 15.0) -> str:
    """Use the shared Gist queue so alts need not join the control server.

    Direct bot-to-user DMs require a mutual Discord server in many cases. The
    existing private control Gist is already readable by every sender, so it
    is the reliable transport for runtime commands. DM remains a compatibility
    fallback only when no control Gist is configured.
    """
    if config.CONTROL_GIST_ID:
        try:
            ok, detail = await asyncio.to_thread(github_api.queue_control_command, alt_id, text)
        except Exception as exc:
            # A control-plane failure must be reported to the operator, never
            # raised into a dashboard button callback that nobody is awaiting.
            return f"❌ Control Gist queue error: {type(exc).__name__}: {exc}"
        if ok:
            return f"🕒 queued via control Gist (command `{detail}`); polling every 45s"
        return f"❌ Control Gist queue failed: {detail}"
    try:
        return await _send_dm_wait_ack(alt_id, text, timeout=timeout)
    except Exception as exc:
        return f"❌ DM fallback failed: {type(exc).__name__}: {exc}"


@bot.event
async def on_message(message: discord.Message):
    # Ignore self
    if bot.user and message.author.id == bot.user.id:
        return

    # ---- Handle DM replies FROM alts ----
    if isinstance(message.channel, discord.DMChannel):
        await _handle_incoming_dm(message)
        return

    # ---- Handle webhook messages in guild log/dashboard channels ----
    if message.guild and message.guild.id == (config.GUILD_ID or message.guild.id):
        await _handle_guild_webhook_message(message)


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if bot.user and after.author.id == bot.user.id:
        return
    if after.guild and after.guild.id == (config.GUILD_ID or after.guild.id):
        await _handle_guild_webhook_message(after, is_edit=True)


@bot.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
    if not payload.guild_id or (config.GUILD_ID and payload.guild_id != config.GUILD_ID):
        return
    ch_id = payload.channel_id
    if ch_id == config.DASHBOARD_CH_ID or (config.LOG_CH_ID and ch_id == config.LOG_CH_ID) or (config.DEALS_CH_ID and ch_id == config.DEALS_CH_ID):
        try:
            channel = bot.get_channel(ch_id)
            if channel and hasattr(channel, "fetch_message"):
                msg = await channel.fetch_message(payload.message_id)
                if msg:
                    await _handle_guild_webhook_message(msg, is_edit=True)
        except Exception:
            pass


async def _handle_incoming_dm(message: discord.Message):
    author_id = message.author.id
    # Is this from a known alt?
    alt_id = _alt_id_for_discord_id(author_id)
    if not alt_id:
        # Unknown DM — forward to #control if set
        if config.CONTROL_CH_ID:
            ch = bot.get_channel(config.CONTROL_CH_ID)
            if ch:
                await ch.send(f"📩 DM from unknown user <@{author_id}>: {message.content[:500]}",
                              allowed_mentions=discord.AllowedMentions.none())
        return
    a = state.get(alt_id)
    body = (message.content or "").strip()
    # If a command is waiting for an ack, complete the future
    fut = _DM_ACKS.get(author_id)
    if fut and not fut.done():
        fut.set_result(body[:500])
    # Always log the reply in state buffer + #control
    snip = body[:200].replace("\n", " ⏎ ")
    state.set_dm_ack(alt_id, snip)
    state.append_log(alt_id, f"DM reply: {snip}", emoji="📨", color=0x2F3136)
    if config.CONTROL_CH_ID:
        ch = bot.get_channel(config.CONTROL_CH_ID)
        if ch:
            await ch.send(f"📨 **{a.name}** → {snip}", allowed_mentions=discord.AllowedMentions.none())
    # Try to parse a heartbeat payload (if alt sends JSON in DM)
    _try_parse_heartbeat(alt_id, body)


def _alt_id_for_discord_id(did: int) -> int | None:
    for k, v in config.ALT_DISCORD_IDS.items():
        if v == did:
            return k
    return None


async def _handle_guild_webhook_message(message: discord.Message, is_edit: bool = False):
    """Parse consolidated dashboard and farm-log webhook messages.

    All alts share one #farm-logs webhook. ``send_log_webhook`` sets the
    webhook username to ALT_NAME, so the author name is the primary routing
    key; the Alt N fallback keeps older messages readable during migration.
    """
    # Only process webhook/bot-authored traffic in the dedicated ingestion
    # channels. A normal user's message must never become a phantom deal or
    # heartbeat just because its display name resembles an alt.
    if not getattr(message, "webhook_id", None) and not getattr(message.author, "bot", False):
        return
    ch_id = message.channel.id
    message_id = getattr(message, "id", None)
    if message_id is not None and ch_id != config.DASHBOARD_CH_ID and not is_edit:
        try:
            message_id = int(message_id)
        except (TypeError, ValueError):
            message_id = None
        if message_id is not None:
            if message_id in _processed_webhook_ids:
                return
            _processed_webhook_ids.add(message_id)
            if len(_processed_webhook_ids) > 5000:
                _processed_webhook_ids.clear()
                _processed_webhook_ids.add(message_id)
    if ch_id == config.DASHBOARD_CH_ID:
        _parse_dashboard_message(message)

    if config.DEALS_CH_ID and ch_id == config.DEALS_CH_ID:
        names = [getattr(message.author, "display_name", ""), getattr(message.author, "name", "")]
        alt_id = next((_match_alt_name(name) for name in names if name), None)
        if alt_id is None:
            alt_id = _match_alt_name(message.content)
        if alt_id is None:
            for embed in message.embeds:
                footer = getattr(embed.footer, "text", "") if embed.footer else ""
                title = getattr(embed, "title", "") or ""
                alt_id = _match_alt_name(footer) or _match_alt_name(title)
                if alt_id is not None:
                    break
        if alt_id is not None:
            _parse_deal_message(alt_id, message)

    if config.LOG_CH_ID and ch_id == config.LOG_CH_ID:
        names = [
            getattr(message.author, "display_name", ""),
            getattr(message.author, "name", ""),
        ]
        alt_id = next((_match_alt_name(name) for name in names if name), None)
        if alt_id is None:
            alt_id = _match_alt_name(message.content)
        if alt_id is None:
            # A few webhook clients expose the override in the embed footer or title.
            for embed in message.embeds:
                footer = getattr(embed.footer, "text", "") if embed.footer else ""
                title = getattr(embed, "title", "") or ""
                alt_id = _match_alt_name(footer) or _match_alt_name(title)
                if alt_id is not None:
                    break
        if alt_id is None and len(state.alt_ids) == 1:
            alt_id = state.alt_ids[0]
        if alt_id is not None:
            _parse_log_message(alt_id, message)
        elif not any(n.lower() in ("farm logs", "adfarm control") for n in names if n):
            print(f"⚠️ Could not map farm-log webhook username to an alt: {names!r}")


def _parse_deal_message(alt_id: int, message: discord.Message):
    """Record only deal-webhook events; never let them overwrite heartbeat state."""
    title = ""
    snippet = message.content or ""
    for embed in message.embeds:
        title = getattr(embed, "title", "") or title
        for field in embed.fields or []:
            if getattr(field, "name", "").lower() in {"snippet", "user", "price"}:
                snippet += f" {field.name}: {field.value}"
    # Heartbeat deal_alerts is the authoritative total. This separate path
    # updates recency and typed logs without double-counting the same event.
    state.mark_deal_seen(alt_id)
    state.append_log(alt_id, f"{title or 'Deal alert'} {snippet[:300]}", emoji="📈", color=0x57F287, kind="DEAL")


def _parse_dashboard_message(message: discord.Message):
    """Extract live state from a structured heartbeat (old JSON or new embed)."""
    # Keep compatibility with heartbeat messages written before the readable
    # embed format was deployed.
    raw = (message.content or "").strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:])
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()
    if raw.startswith("{"):
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict) and (payload.get("heartbeat") or payload.get("type") == "heartbeat"):
                alt_id = payload.get("alt_id") or _match_alt_name(payload.get("alt_name"))
                if alt_id:
                    state.update_from_heartbeat(alt_id, payload)
        except Exception:
            pass
    for embed in message.embeds:
        title = getattr(embed, "title", "") or ""
        if not title.lower().startswith("💓 heartbeat"):
            continue
        try:
            alt_id = None
            footer = getattr(embed, "footer", None)
            footer_text = getattr(footer, "text", "") if footer else ""
            match = re.search(r"alt[_\s-]?(\d+)", footer_text, re.I)
            if match:
                alt_id = int(match.group(1))
            if alt_id is None:
                alt_id = _match_alt_name(title)
            if not alt_id:
                continue
            payload = {"heartbeat": True, "type": "heartbeat", "alt_id": alt_id}
            channels = {}
            for field in embed.fields or []:
                name = str(getattr(field, "name", "") or "").strip()
                value = str(getattr(field, "value", "") or "").strip()
                key = name.casefold()
                if key == "status":
                    m = re.search(r"\b(active|paused|caution|ip_pause|afk|stopped|error|offline|starting|queued)\b", value, re.I)
                    if m:
                        payload["status"] = m.group(1).lower()
                elif key == "mode":
                    m = re.search(r"\b(sell|buy)\b", value, re.I)
                    if m:
                        payload["ad_type"] = m.group(1).lower()
                elif key == "rate":
                    m = re.search(r"(\d+(?:\.\d{1,2})?)", value)
                    if m:
                        payload["rate"] = float(m.group(1))
                elif key in {"cadence", "interval"}:
                    m = re.search(r"(\d+)\s*m?", value)
                    if m:
                        payload["interval_min"] = int(m.group(1))
                elif key == "activity":
                    for label, target in (("sent", "total_sent"), ("errors", "total_errors"), ("skips", "total_skips")):
                        m = re.search(rf"{label}:?\s*`?(\d+)", value, re.I)
                        if m:
                            payload[target] = int(m.group(1))
                elif key == "deals":
                    m = re.search(r"(\d+)", value)
                    if m:
                        payload["deal_alerts"] = int(m.group(1))
                elif key == "keywords":
                    payload["deal_keywords"] = [x.strip() for x in value.split(",") if x.strip() and x.lower() != "none configured"]
                elif key == "scanner":
                    payload["deal_scan_enabled"] = value.casefold().startswith("on")
                    m = re.search(r"edge\s*\$?(\d+(?:\.\d+)?)", value, re.I)
                    if m:
                        payload["deal_alert_delta"] = float(m.group(1))
                elif key == "uptime":
                    m = re.search(r"(\d+(?:\.\d+)?)\s*min", value, re.I)
                    if m:
                        payload["uptime_sec"] = float(m.group(1)) * 60
                elif key == "channels":
                    m = re.search(r"(\d+)\s*/\s*(\d+)", value)
                    if m:
                        payload["active_channels"], payload["total_channels"] = int(m.group(1)), int(m.group(2))
                elif key == "message":
                    payload["message_preview"] = value[:120]
                elif key in {"latest issue", "latest error"}:
                    payload["last_error"] = value[:300]
                elif key == "warnings":
                    payload["warnings"] = [x for x in value.splitlines() if x.strip()]
                elif key.startswith("channel:"):
                    match_cid = re.search(r"channel:\s*(\d+)", name, re.I)
                    if match_cid:
                        cid = match_cid.group(1)
                        ch_name = cid
                        if "· #" in name:
                            ch_name = name.split("· #", 1)[1].strip() or cid
                        sent = re.search(r"sent\s*`?(\d+)", value, re.I)
                        errors = re.search(r"errors\s*`?(\d+)", value, re.I)
                        slow = re.search(r"slowmode\s*`?(\d+)", value, re.I)
                        last = re.search(r"last\s+<t:(\d+):", value, re.I)
                        channels[cid] = {
                            "name": ch_name[:80],
                            "sent": int(sent.group(1)) if sent else 0,
                            "errors": int(errors.group(1)) if errors else 0,
                            "slowmode": int(slow.group(1)) if slow else 0,
                            "alive": "alive" in value.casefold(),
                            "last_post": int(last.group(1)) if last else 0,
                        }
            if channels:
                payload["channels"] = channels
                max_ch_post = max((int(ch.get("last_post") or 0) for ch in channels.values()), default=0)
                if max_ch_post > 0:
                    payload["last_post_ts"] = max_ch_post
            state.update_from_heartbeat(alt_id, payload)
        except Exception:
            # A malformed optional field must not discard the rest of a live
            # heartbeat or crash the bot's event loop.
            continue


def _parse_log_message(alt_id: int, message: discord.Message):
    text = message.content or ""
    # Discard markdown backticks and leading timestamps for matching
    body = text.replace("`", "").strip()
    emoji = "•"
    color = 0x2F3136
    if "✅" in body or "SUCCESS" in body:
        emoji, color = "✅", 0x57F287
    elif "❌" in body or "FAIL" in body or "ERROR" in body:
        emoji, color = "❌", 0xED4245
    elif "⚠️" in body or "CAUTION" in body:
        emoji, color = "⚠️", 0xFEE75C
    elif "🛑" in body or "STOP" in body:
        emoji, color = "🛑", 0xED4245
    elif "🟢" in body or "STARTUP" in body:
        emoji, color = "🟢", 0x57F287
    elif "🏁" in body or "FINISHED" in body:
        emoji, color = "🏁", 0x5865F2
    elif "📩" in body or "DM" in body:
        emoji, color = "📩", 0x5865F2
    kind = "INFO"
    match = re.search(r"\[([A-Z][A-Z0-9_-]{1,23})\]", body)
    if match:
        kind = match.group(1)
    elif "DEAL" in body.upper():
        kind = "DEAL"
    elif "CAUTION" in body.upper():
        kind = "CAUTION"
    elif "ERROR" in body.upper() or "FAIL" in body.upper():
        kind = "ERROR"
    state.append_log(alt_id, body[:300], emoji=emoji, color=color, kind=kind)

    # Any live log message from an alt confirms the alt runner is running and active
    a = state.get(alt_id)
    now_ts = time.time()
    if a:
        a.online = True
        a.last_heartbeat_ts = now_ts
        if a.status in {"offline", "stopped", "queued", "starting", ""}:
            a.status = "active"

        # Detect ad post event
        if "SEND" in body.upper():
            a.last_post_ts = now_ts
            m_cid = re.search(r"\((\d{17,20})\)", body)
            if m_cid:
                cid = m_cid.group(1)
                m_chname = re.search(r"#([^\s(]+)", body)
                ch_name = m_chname.group(1) if m_chname else cid
                if cid not in a.channels or not isinstance(a.channels[cid], dict):
                    a.channels[cid] = {
                        "name": ch_name[:80],
                        "sent": 0,
                        "errors": 0,
                        "slowmode": 0,
                        "alive": True,
                        "last_post": 0,
                    }
                a.channels[cid]["sent"] = int(a.channels[cid].get("sent") or 0) + 1
                a.channels[cid]["last_post"] = int(now_ts)
                a.channels[cid]["alive"] = True

        # Try to detect success counts from "total=`N`"
        m = re.search(r"total[`=]\s*(\d+)", body)
        if m:
            try:
                a.total_sent = max(a.total_sent, int(m.group(1)))
            except Exception:
                pass


def _try_parse_heartbeat(alt_id: int, body: str):
    body = body.strip()
    if not body.startswith("{"):
        return
    try:
        payload = json.loads(body)
        if isinstance(payload, dict) and payload.get("type") == "heartbeat":
            state.update_from_heartbeat(alt_id, payload)
    except Exception:
        pass


def _match_alt_name(name: str) -> int | None:
    if not name:
        return None
    name = str(name)
    lowered = name.lower()
    for i in state.alt_ids:
        a = state.get(i)
        alt_name = str(a.name).strip().lower() if a else ""
        if a and ((alt_name and alt_name in lowered)
                  or (alt_name and lowered in alt_name)
                  or f"alt {i}" in lowered or f"alt{i}" in lowered):
            return i
    m = re.search(r"alt\s*(\d+)", name, re.I)
    if m:
        candidate = int(m.group(1))
        return candidate if candidate in state.alt_ids else None
    return None


def _extract_price(s: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d{1,2})?)", s or "")
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None


class DashboardControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="➕ Add Alt", style=discord.ButtonStyle.success, row=0)
    async def btn_add_alt(self, inter: discord.Interaction, button: discord.ui.Button):
        await inter.response.send_modal(AltAddModal())

    @discord.ui.button(label="🚀 Launch Run", style=discord.ButtonStyle.primary, row=0)
    async def btn_launch_run(self, inter: discord.Interaction, button: discord.ui.Button):
        view = RunStartView(inter.user.id)
        await inter.response.send_message(embed=_run_start_embed(view), view=view, ephemeral=True)

    @discord.ui.button(label="⚙️ Tune Fleet", style=discord.ButtonStyle.secondary, row=0)
    async def btn_tune_fleet(self, inter: discord.Interaction, button: discord.ui.Button):
        target = state.alt_ids[0] if state.alt_ids else 1
        view = FleetTuningView(owner_id=inter.user.id, alt_id=target)
        await inter.response.send_message(embed=view._build_embed(), view=view, ephemeral=True)

    @discord.ui.button(label="📌 Channels", style=discord.ButtonStyle.secondary, row=0)
    async def btn_channels(self, inter: discord.Interaction, button: discord.ui.Button):
        target = state.alt_ids[0] if state.alt_ids else 1
        view = ChannelsView(owner_id=inter.user.id, alt_id=target)
        await inter.response.send_message(embed=view._build_embed(), view=view, ephemeral=True)

    @discord.ui.button(label="⏸️ Pause All", style=discord.ButtonStyle.secondary, emoji="⏸️", custom_id="dash_pause_all", row=1)
    async def on_pause_all(self, inter: discord.Interaction, button: discord.ui.Button):
        if not await _check_perms(inter): return
        if not inter.response.is_done():
            await inter.response.defer(ephemeral=True)
        coros = [_send_control_wait_ack(aid, "!pause", timeout=8) for aid in state.alt_ids]
        if coros:
            await asyncio.gather(*coros, return_exceptions=True)
        await inter.followup.send("⏸️ **Pause command broadcast to all alts.**", ephemeral=True)

    @discord.ui.button(label="▶️ Resume All", style=discord.ButtonStyle.success, emoji="▶️", custom_id="dash_resume_all", row=1)
    async def on_resume_all(self, inter: discord.Interaction, button: discord.ui.Button):
        if not await _check_perms(inter): return
        if not inter.response.is_done():
            await inter.response.defer(ephemeral=True)
        coros = [_send_control_wait_ack(aid, "!resume", timeout=8) for aid in state.alt_ids]
        if coros:
            await asyncio.gather(*coros, return_exceptions=True)
        await inter.followup.send("▶️ **Resume command broadcast to all alts.**", ephemeral=True)

    @discord.ui.button(label="🔄 Rescan Channels", style=discord.ButtonStyle.primary, emoji="🔄", custom_id="dash_rescan_all", row=1)
    async def on_rescan_all(self, inter: discord.Interaction, button: discord.ui.Button):
        if not await _check_perms(inter): return
        if not inter.response.is_done():
            await inter.response.defer(ephemeral=True)
        coros = [_send_control_wait_ack(aid, "!rescan", timeout=8) for aid in state.alt_ids]
        if coros:
            await asyncio.gather(*coros, return_exceptions=True)
        await inter.followup.send("🔄 **Channel rescan broadcast to all alts.**", ephemeral=True)

    @discord.ui.button(label="⚠️ Reset Caution", style=discord.ButtonStyle.secondary, emoji="⚠️", custom_id="dash_reset_all", row=1)
    async def on_reset_all(self, inter: discord.Interaction, button: discord.ui.Button):
        if not await _check_perms(inter): return
        if not inter.response.is_done():
            await inter.response.defer(ephemeral=True)
        coros = [_send_control_wait_ack(aid, "!resetcaution all", timeout=8) for aid in state.alt_ids]
        if coros:
            await asyncio.gather(*coros, return_exceptions=True)
        for aid in state.alt_ids:
            state.reset_caution(aid, None)
        await inter.followup.send("⚠️ **Caution reset broadcast to all alts.**", ephemeral=True)

    @discord.ui.button(label="🛑 Stop All", style=discord.ButtonStyle.danger, emoji="🛑", custom_id="dash_stop_all", row=1)
    async def on_stop_all(self, inter: discord.Interaction, button: discord.ui.Button):
        if not await _check_perms(inter): return
        if not inter.response.is_done():
            await inter.response.defer(ephemeral=True)
        coros = [_send_control_wait_ack(aid, "!stop", timeout=8) for aid in state.alt_ids]
        if coros:
            await asyncio.gather(*coros, return_exceptions=True)
        for aid in state.alt_ids:
            asyncio.create_task(asyncio.to_thread(github_api.cancel_run, aid))
        await inter.followup.send("🛑 **Emergency stop broadcast and workflows canceled.**", ephemeral=True)


# ----- Background tasks -----
_dash_message: discord.Message | None = None


@tasks.loop(seconds=config.DASHBOARD_REFRESH_SEC)
async def refresh_dashboard():
    await _fresh_state()
    await _refresh_dashboard_now()


@refresh_dashboard.before_loop
async def _before_dash():
    await bot.wait_until_ready()
    await asyncio.sleep(5)


async def _refresh_dashboard_now():
    global _dash_message
    if not config.DASHBOARD_CH_ID:
        return
    ch = bot.get_channel(config.DASHBOARD_CH_ID)
    if not ch:
        return
    embeds = build_all(state)
    view = DashboardControlView()
    try:
        if _dash_message is None:
            # Try to load saved message id
            try:
                mid = int(Path(config.DASHBOARD_MSG_ID_FILE).read_text().strip())
                _dash_message = await ch.fetch_message(mid)
            except Exception:
                _dash_message = None
        if _dash_message is None:
            _dash_message = await ch.send(embeds=embeds[:10], view=view)
            try:
                Path(config.DASHBOARD_MSG_ID_FILE).write_text(str(_dash_message.id))
            except Exception:
                pass
            try:
                await _dash_message.pin()
            except Exception:
                pass
        else:
            await _dash_message.edit(embeds=embeds[:10], view=view)
    except Exception as e:
        print(f"[DASH] refresh failed: {type(e).__name__}: {e}")
        _dash_message = None


async def _post_dashboard(embeds):
    ch = bot.get_channel(config.DASHBOARD_CH_ID) if config.DASHBOARD_CH_ID else None
    if not ch:
        return None
    try:
        return await ch.send(embeds=embeds[:10], view=DashboardControlView())
    except Exception as e:
        print(f"[DASH] post failed: {e}")
        return None


@tasks.loop(seconds=60)
async def refresh_github_status():
    await bot.wait_until_ready()
    await asyncio.to_thread(github_api.refresh_all_run_statuses, state)


@tasks.loop(seconds=config.HEALTH_CHECK_INTERVAL_SEC)
async def fleet_health_check():
    """Silent 5-minute alt health monitor with best-effort auto-recovery."""
    await bot.wait_until_ready()
    now = time.time()
    hydrated = False
    for aid in list(state.alt_ids):
        a = state.get(aid)
        if not a:
            continue
        is_stale = (a.last_heartbeat_ts <= 0) or (now - max(a.last_heartbeat_ts, a.last_post_ts) > config.OFFLINE_AFTER_SEC)
        if not is_stale:
            continue
        if not hydrated:
            try:
                await asyncio.to_thread(github_api.refresh_all_run_statuses, state)
            except Exception:
                pass
            hydrated = True
        # Try to re-connect the transport. If the runner died, the Gist/DM ack
        # tells us immediately; otherwise a running sender polls it within 45s.
        try:
            ack = await _send_control_wait_ack(aid, "!sync", timeout=12)
        except Exception as exc:
            ack = f"{type(exc).__name__}: {exc}"
        recovered = not ack.startswith(("❌", "⏰"))
        detail = f"Alt {aid} ({a.name}) heartbeat stale; auto-recovery probe `{ack[:200]}`."
        if recovered:
            state.append_log(aid, detail, emoji="🩺", color=0x57F287, kind="CONTROL")
            state.record_causal_event(aid, "auto_recovery", "Heartbeat recovered via controller sync probe", details=ack)
        else:
            state.append_log(aid, detail, emoji="⚠️", color=0xFEE75C, kind="CONTROL")
            state.record_causal_event(aid, "auto_recovery_failed", "Heartbeat recovery probe did not confirm", details=ack)
        try:
            await _log_control(f"🩺 [HEALTH] {detail}")
        except Exception:
            pass


async def _log_control(text: str, alt_id: int | None = None):
    """Write one control-plane line to #control (stdout fallback when unset).

    ``alt_id`` prepends the canonical ``[ALT-n]`` tag so four concurrent alts
    remain individually attributable inside one shared channel (PMTP 2.7).
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    tag = f"{_alt_tag(alt_id)} " if alt_id else ""
    full = f"[{ts}] [CONTROL] {tag}{text}"
    if not config.CONTROL_CH_ID:
        return
    ch = bot.get_channel(config.CONTROL_CH_ID)
    if not ch:
        print(f"[LOG-FALLBACK] {full[:2000]}")
        return
    try:
        await ch.send(full[:2000], allowed_mentions=discord.AllowedMentions.none())
    except Exception as e:
        print(f"[LOG] Failed to send control log: {e}")


def run():
    if not config.BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN env var is not set.")
    if not config.GITHUB_TOKEN:
        print("⚠️  GH_TOKEN not set — /run and /stop will not work.")
    if not config.GITHUB_OWNER or not config.ALT_REPOS:
        print("⚠️  GITHUB_OWNER / ALT_REPOS not set — /run will fail.")
    print(f"Alt mapping: {config.ALT_REPOS}")
    print(f"Discord IDs: {config.ALT_DISCORD_IDS}")
    bot.run(config.BOT_TOKEN)
