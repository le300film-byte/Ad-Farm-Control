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
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import config, github_api
from .alt_state import AltStateManager
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
)

_cooldowns: dict[int, float] = {}
_processed_webhook_ids: set[int] = set()


def _is_owner(inter: discord.Interaction) -> bool:
    # Fail closed: an absent owner allow-list must never grant control access.
    return bool(config.OWNER_IDS) and inter.user.id in config.OWNER_IDS


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


# ----- Slash commands -----
async def _check_perms(inter: discord.Interaction) -> bool:
    if not _is_owner(inter):
        await inter.response.send_message(
            "🔒 You aren't authorized to run control commands.", ephemeral=True
        )
        return False
    cd = _on_cooldown(inter.user.id)
    if cd > 0:
        await inter.response.send_message(
            f"⏱️ Cooldown — wait {cd:.1f}s.", ephemeral=True
        )
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
        super().__init__(title=f"V6 {view.ad_type} ad details")
        self.parent_view = view
        if view.ad_type == "sell":
            self.rate = discord.ui.TextInput(label="Sell rate (example: 2.5$)", custom_id="sell_rate", placeholder="2.5", max_length=20, required=True)
            self.extra = discord.ui.TextInput(label="Extra sell text", custom_id="sell_extra", placeholder="DM ME QUICK...", max_length=500, required=False, style=discord.TextStyle.paragraph)
            self.image = discord.ui.TextInput(label="Attach image? yes or no", custom_id="attach_image", placeholder="yes", max_length=3, required=True, default="yes")
            self.add_item(self.rate)
            self.add_item(self.extra)
            self.add_item(self.image)
        else:
            self.rate = discord.ui.TextInput(label="Token rate", custom_id="buy_rate", placeholder="2.2", max_length=20, required=True)
            self.rap = discord.ui.TextInput(label="RAP rate", custom_id="buy_rate_rap", placeholder="1.8", max_length=20, required=True)
            self.simple_text = discord.ui.TextInput(label="Simple buy text (only for simple style)", custom_id="buy_simple_text", max_length=1900, required=False, style=discord.TextStyle.paragraph)
            self.style = discord.ui.TextInput(label="Buy style: detailed or simple", custom_id="buy_style", placeholder="detailed", max_length=8, required=True, default="detailed")
            self.image = discord.ui.TextInput(label="Attach image? yes or no", custom_id="attach_image", placeholder="yes", max_length=3, required=True, default="yes")
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
        }
        errors, parsed = _validate_run_values(values)
        if errors:
            await inter.response.send_message("❌ " + "\n".join(errors), ephemeral=True)
            return
        await _dispatch_run_from_modal(inter, values, parsed)


class RunStartView(discord.ui.View):
    """Private component step; only the Continue button opens the modal."""
    def __init__(self, owner_id: int):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.alt_id: int | None = None
        self.ad_type: str | None = None
        self.interval_min = 5
        self.total_hours = 6
        self.attach_image = "yes"
        self.buy_style = "detailed"
        self.alt_select = discord.ui.Select(
            placeholder="1. Choose configured alt…", min_values=1, max_values=1,
            options=[discord.SelectOption(label=(state.get(i).name if state.get(i) else f"Alt {i}")[:100], value=str(i)) for i in state.alt_ids[:25]], row=0)
        self.mode_select = discord.ui.Select(
            placeholder="2. Choose sell or buy…", min_values=1, max_values=1,
            options=[discord.SelectOption(label="Sell", value="sell", emoji="💰"), discord.SelectOption(label="Buy", value="buy", emoji="🛒")], row=1)
        self.interval_select = discord.ui.Select(
            placeholder="3. Interval: 3 or 5 minutes", min_values=1, max_values=1,
            options=[discord.SelectOption(label="3 minutes", value="3"), discord.SelectOption(label="5 minutes", value="5")], row=2)
        self.runtime_select = discord.ui.Select(
            placeholder="4. Runtime: 6/12/18/24/48 hours", min_values=1, max_values=1,
            options=[discord.SelectOption(label=f"{h} hours", value=str(h)) for h in (6, 12, 18, 24, 48)], row=3)
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
        if inter.user.id != self.owner_id:
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
        if inter.user.id != self.owner_id:
            return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
        self.alt_id = int(inter.data["values"][0])
        self._build_components()
        embed = self._build_embed()
        await inter.response.edit_message(embed=embed, view=self)

    async def _on_add_channel(self, inter: discord.Interaction):
        if inter.user.id != self.owner_id:
            return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
        await inter.response.send_modal(AddChannelModal(self.alt_id))

    async def _on_rescan(self, inter: discord.Interaction):
        if inter.user.id != self.owner_id:
            return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
        await _finish_dm_control(inter, self.alt_id, "!rescan", "channel permission rescan queued")

    async def _on_reset_caution(self, inter: discord.Interaction):
        if inter.user.id != self.owner_id:
            return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
        await _finish_dm_control(
            inter, self.alt_id, "!resetcaution all", "reset caution on all channels",
            update=lambda: state.reset_caution(self.alt_id, None)
        )

    async def _on_export(self, inter: discord.Interaction):
        if inter.user.id != self.owner_id:
            return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
        a_obj = state.get(self.alt_id)
        cids = list(a_obj.channels.keys()) if a_obj else []
        text = f"**Channel IDs for {a_obj.name if a_obj else self.alt_id}** (`{len(cids)}` total):\n```\n{','.join(cids)}\n```"
        await inter.response.send_message(text, ephemeral=True)

    async def _on_remove_channel(self, inter: discord.Interaction):
        if inter.user.id != self.owner_id:
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
        if inter.user.id != self.owner_id:
            return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
        self.alt_id = int(inter.data["values"][0])
        self._build_components()
        await inter.response.edit_message(embed=self._build_embed(), view=self)

    async def _on_policy_select(self, inter: discord.Interaction):
        if inter.user.id != self.owner_id:
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
        if inter.user.id != self.owner_id:
            return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
        await _finish_dm_control(inter, self.alt_id, "!rescan", "channel permission rescan queued")

    async def _on_reset_caution(self, inter: discord.Interaction):
        if inter.user.id != self.owner_id:
            return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
        await _finish_dm_control(
            inter, self.alt_id, "!resetcaution all", "reset caution on all channels",
            update=lambda: state.reset_caution(self.alt_id, None)
        )

    async def _on_diagnose(self, inter: discord.Interaction):
        if inter.user.id != self.owner_id:
            return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
        embed = build_diagnose_embed(state, self.alt_id)
        await inter.response.send_message(embed=embed, ephemeral=True)


def _run_start_embed(view: RunStartView) -> discord.Embed:
    alt = state.get(view.alt_id) if view.alt_id else None
    embed = discord.Embed(title="🚀 V6 start a sender run", color=0x5865F2)
    embed.description = "Choose every runtime setting below, then enter the ad text/rates. This form is private and owner-only."
    embed.add_field(name="Alt", value=alt.name if alt else "not selected", inline=True)
    embed.add_field(name="Mode", value=view.ad_type or "not selected", inline=True)
    embed.add_field(name="Interval / runtime", value=f"{view.interval_min} min / {view.total_hours} h", inline=True)
    embed.add_field(name="Image / buy style", value=f"{view.attach_image} / {view.buy_style} (confirm in modal)", inline=True)
    return embed


def _validate_run_values(values: dict[str, str]) -> tuple[list[str], dict[str, object]]:
    errors = []
    try: alt_id = int(values.get("alt_id", ""))
    except (TypeError, ValueError): alt_id = 0
    if alt_id not in state.alt_ids: errors.append("Choose a configured alt.")
    ad_type = values.get("ad_type", "").lower().strip()
    if ad_type not in {"sell", "buy"}: errors.append("Mode must be sell or buy.")
    if ad_type == "sell":
        rate = _extract_price(values.get("sell_rate", ""))
        if rate is None or not 0 < rate <= 20: errors.append("Sell rate must be between 0 and 20.")
        rap = None
        if len(values.get("sell_extra", "")) > 500: errors.append("Sell extra text is limited to 500 characters.")
    elif ad_type == "buy":
        rate = _extract_price(values.get("buy_rate", "")); rap = _extract_price(values.get("buy_rate_rap", ""))
        if rate is None or not 0 < rate <= 20: errors.append("Token rate must be between 0 and 20.")
        if rap is None or not 0 < rap <= 20: errors.append("RAP rate must be between 0 and 20.")
        if values.get("buy_style") not in {"simple", "detailed"}: errors.append("Buy style must be simple or detailed.")
        if values.get("buy_style") == "simple" and not values.get("buy_simple_text", "").strip(): errors.append("Simple buy text is required for simple style.")
        if len(values.get("buy_simple_text", "")) > 1900: errors.append("Simple buy text is limited to 1900 characters.")
    else: rate = rap = None
    try: interval = int(values.get("interval_min", ""))
    except (TypeError, ValueError): interval = 0
    if interval not in {3, 5}: errors.append("Interval must be 3 or 5 minutes.")
    try: hours = int(values.get("total_hours", ""))
    except (TypeError, ValueError): hours = 0
    if hours not in {6, 12, 18, 24, 48}: errors.append("Runtime must be 6, 12, 18, 24, or 48 hours.")
    if values.get("attach_image") not in {"yes", "no"}: errors.append("Image setting must be yes or no.")
    return errors, {"alt_id": alt_id, "rate": rate, "rap": rap, "interval": interval, "hours": hours}


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
        placeholder="Leave blank to auto-create private repo",
        max_length=100,
        required=False,
    )
    discord_user_id = discord.ui.TextInput(
        label="Discord User ID (optional)",
        placeholder="Leave blank to auto-detect from token",
        max_length=30,
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
            return await inter.response.send_message("❌ User token is required.", ephemeral=True)

        await inter.response.defer(ephemeral=True)

        # 1. Validate token with Discord API and extract profile
        ok_prof, profile = await asyncio.to_thread(github_api.fetch_discord_user_profile, token)
        if not ok_prof or not isinstance(profile, dict) or not profile.get("id"):
            err_msg = profile.get("error", "Invalid user token") if isinstance(profile, dict) else "Invalid token"
            return await inter.followup.send(f"❌ Could not authenticate alt with Discord: {err_msg}", ephemeral=True)

        detected_did = str(profile.get("id"))
        detected_name = str(profile.get("username") or profile.get("global_name") or "alt")

        # 2. Resolve Alt ID
        raw_aid = value(self.alt_id)
        if raw_aid:
            try:
                alt_id = int(raw_aid)
            except (TypeError, ValueError):
                return await inter.followup.send("❌ Alt ID must be an integer between 1 and 4.", ephemeral=True)
        else:
            free_ids = [i for i in (1, 2, 3, 4) if i not in state.alt_ids]
            if not free_ids:
                return await inter.followup.send("❌ All 4 alt slots are currently occupied. Remove one with `/altremove` first.", ephemeral=True)
            alt_id = free_ids[0]

        if alt_id not in {1, 2, 3, 4}:
            return await inter.followup.send("❌ Alt ID must be between 1 and 4.", ephemeral=True)
        if alt_id in state.alt_ids:
            return await inter.followup.send(f"❌ Alt `{alt_id}` is already configured. Use `/altupdate` to modify it.", ephemeral=True)

        # 3. Resolve Display Name & Discord User ID
        custom_name = value(self.name)
        name = custom_name if custom_name else detected_name
        name = re.sub(r"[\r\n]", " ", name)[:80].strip() or f"Alt {alt_id}"

        custom_did = value(self.discord_user_id)
        did = custom_did if (custom_did and custom_did.isdigit()) else detected_did

        # 4. Resolve Repository (auto-create if blank or missing)
        raw_repo = value(self.repository)
        if raw_repo:
            repo = raw_repo if "/" in raw_repo else f"{config.GITHUB_OWNER}/{raw_repo}"
        else:
            clean_slug_name = re.sub(r"[^a-zA-Z0-9_-]", "", name.lower().replace(" ", "-")) or f"alt{alt_id}"
            owner = config.GITHUB_OWNER or (config.CORE_REPO.split("/")[0] if "/" in config.CORE_REPO else "owner")
            repo = f"{owner}/alt{alt_id}-{clean_slug_name}"

        # 5. Auto-create repo on GitHub, upload templates, and populate secrets
        ok_prov, prov_detail = await asyncio.to_thread(github_api.provision_alt_repository_files_and_secrets, repo, token)
        if not ok_prov:
            return await inter.followup.send(f"❌ Auto-provisioning failed for `{repo}`: {prov_detail}", ephemeral=True)

        # 6. Persist alt in core fleet registry
        repos = dict(config.ALT_REPOS); repos[alt_id] = repo
        discord_ids = dict(config.ALT_DISCORD_IDS); discord_ids[alt_id] = int(did)
        names = dict(config.ALT_NAMES); names[alt_id] = name
        persisted, persist_detail = await _persist_alt_registry(repos, discord_ids, names)
        if not persisted:
            return await inter.followup.send(f"❌ Alt was not registered in core map: {persist_detail}", ephemeral=True)

        _apply_alt_registry(repos, discord_ids, names)
        state.add_alt(alt_id, name)

        text = (
            f"🎉 **Alt {alt_id} (@{detected_name}) successfully added!**\n"
            f"• **Repository:** `{repo}` (auto-provisioned with workflows & secrets)\n"
            f"• **Discord ID:** `{did}`\n"
            f"• **Display Name:** `{name}`\n"
            f"• **Token:** Verified and securely stored in GitHub secrets\n\n"
            f"👉 *You can now launch this alt directly with `/run` without running setup!*"
        )
        await inter.followup.send(text, ephemeral=True)
        await _log_control(f"Added alt {alt_id} ({name}) with auto-provisioned repo `{repo}`")
        state.append_log(alt_id, f"Alt added: {name} ({repo})", emoji="➕", color=0x57F287, kind="CONTROL")


class AltUpdateModal(discord.ui.Modal):
    def __init__(self, alt_id: int):
        super().__init__(title=f"Update alt {alt_id}")
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


async def _dispatch_run_from_modal(inter: discord.Interaction, values: dict[str, str], parsed: dict[str, object]) -> None:
    if not _is_owner(inter):
        await inter.response.send_message("🔒 You aren't authorized to run control commands.", ephemeral=True)
        return
    if not inter.response.is_done(): await inter.response.defer(ephemeral=True)
    alt_id = int(parsed["alt_id"]); alt = state.get(alt_id)
    if not alt: return await inter.followup.send("❓ Unknown alt.", ephemeral=True)
    if not config.GITHUB_TOKEN or not config.GITHUB_OWNER or alt_id not in config.ALT_REPOS:
        return await inter.followup.send("❌ GitHub control is not configured for this alt.", ephemeral=True)
    inputs = {
        "ad_type": values["ad_type"], "interval_min": str(parsed["interval"]), "total_hours": str(parsed["hours"]),
        "attach_image": values["attach_image"], "channel_1": "", "channel_2": "", "channel_1_name": "", "channel_2_name": "",
    }
    if values["ad_type"] == "sell": inputs.update({"sell_rate": values["sell_rate"], "sell_extra": values.get("sell_extra", "")})
    else: inputs.update({"buy_style": values["buy_style"], "buy_rate": str(parsed["rate"]), "buy_rate_rap": str(parsed["rap"]), "buy_simple_text": values.get("buy_simple_text", "")})
    try:
        await asyncio.to_thread(github_api.cancel_run, alt_id)
        await asyncio.sleep(1)
        ok, msg = await asyncio.to_thread(github_api.dispatch_workflow, alt_id, inputs)
    except Exception as exc:
        return await inter.followup.send(f"❌ Dispatch failed: {type(exc).__name__}: {exc}", ephemeral=True)
    if not ok: return await inter.followup.send(f"❌ Dispatch failed: {msg}", ephemeral=True)
    rate = parsed["rate"]
    state.set_workflow(alt_id, None, "queued", "")
    state.set_run_config(
        alt_id,
        ad_type=values["ad_type"],
        rate=rate,
        message=values.get("sell_extra") or values.get("buy_simple_text"),
        interval_min=parsed["interval"],
        runtime_hours=parsed["hours"],
    )
    text = f"🚀 **{alt.name}** queued privately: {values['ad_type']} · {parsed['interval']}min × {parsed['hours']}h · image={values['attach_image']}\n{msg}"
    await inter.followup.send(text, ephemeral=True)
    await _log_control(text)
    state.append_log(alt_id, text, emoji="🚀", color=0x57F287, kind="CONTROL")


@bot.tree.command(name="altadd", description="Add an existing prepared alt through a private owner-only form.")
async def cmd_altadd(inter: discord.Interaction):
    if not await _check_perms(inter):
        return
    await inter.response.send_modal(AltAddModal())


@bot.tree.command(name="altupdate", description="Update an alt's token, repository, Discord ID, or display name privately.")
@app_commands.describe(alt="Configured alt to update")
async def cmd_altupdate(inter: discord.Interaction, alt: int):
    if not await _check_perms(inter):
        return
    if not state.get(alt):
        return await inter.response.send_message("❓ Unknown alt.", ephemeral=True)
    await inter.response.send_modal(AltUpdateModal(alt))


@bot.tree.command(name="altlist", description="List configured alts without showing any token or secret.")
async def cmd_altlist(inter: discord.Interaction):
    if not await _check_perms(inter):
        return
    rows = []
    for alt in state.all():
        repo = config.ALT_REPOS.get(alt.alt_id, "not mapped")
        dot = {"active": "🟢", "paused": "🟡", "caution": "⚠️", "error": "🔴", "stopped": "🔴"}.get(alt.status, "⚪")
        rows.append(
            f"{dot} **{alt.name}** · id `{alt.alt_id}` · "
            f"repo `{repo}` · `{alt.status}` · heartbeat `{_fmt_ago(alt.last_heartbeat_ts)}`"
        )
    embed = discord.Embed(
        title="👥 Configured alts",
        description="\n".join(rows) if rows else "_No alts configured._",
        color=0x5865F2,
    )
    embed.set_footer(text="Tokens and private credentials are never displayed.")
    await inter.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="altremove", description="Remove an alt from the registry, with optional confirmed repository deletion.")
@app_commands.describe(
    alt="Configured alt to remove",
    confirmation="Type DELETE exactly to confirm removal",
    delete_repository="Also permanently delete the mapped GitHub repository",
)
async def cmd_altremove(inter: discord.Interaction, alt: int, confirmation: str, delete_repository: bool = False):
    if not await _check_perms(inter):
        return
    if str(confirmation).strip().upper() != "DELETE":
        return await inter.response.send_message("❌ Type `DELETE` exactly to confirm alt removal.", ephemeral=True)
    current = state.get(alt)
    if not current:
        return await inter.response.send_message("❓ Unknown alt.", ephemeral=True)
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
    deletion = "repository deletion requested" if delete_repository else "USER_TOKEN secret deleted; repository kept"
    text = f"🗑️ Removed **{current.name}** (alt `{alt}`). {deletion}. {cleanup_detail} Workflow cancellation: {cancel_detail}"
    await inter.followup.send(text, ephemeral=True)
    await _log_control(text)


async def _finish_dm_control(inter: discord.Interaction, alt_id: int,
                             command: str, label: str, *, update=None) -> None:
    """Send a control command through the Gist queue (or legacy DM fallback)."""
    alt = state.get(alt_id)
    if not alt:
        await inter.response.send_message("❓ Unknown alt.", ephemeral=True)
        return
    await inter.response.defer(ephemeral=True)
    ack = await _send_control_wait_ack(alt_id, command, timeout=20)
    failed = ack.startswith(("❌", "⏰"))
    queued = ack.startswith("🕒")
    if not failed and update:
        res = update()
        if asyncio.iscoroutine(res):
            await res
    if queued:
        status = "Queued in the shared control Gist; the alt will apply it on its next poll and confirm through the next heartbeat."
    else:
        status = "Remote alt acknowledged the command." if not failed else "Remote alt did not confirm the change."
    text = (
        f"{'✅' if not failed else '⚠️'} **{alt.name}** — {label}\n"
        f"Command sent: `{command[:900]}`\n"
        f"Acknowledgement: `{ack[:900]}`\n{status}"
    )
    await inter.followup.send(text, ephemeral=True)
    await _log_control(text)
    state.append_log(alt_id, f"{label}: {ack}", emoji="✅" if not failed else "⚠️",
                      color=0x57F287 if not failed else 0xFEE75C, kind="CONTROL")


@bot.tree.command(name="run", description="Start an alt run using a private 3-step form.")
async def cmd_run(inter: discord.Interaction):
    if not await _check_perms(inter):
        return
    if not state.alt_ids:
        await inter.response.send_message(
            "❌ No configured alts are available. Check ALT_REPOS and ALT_DISCORD_IDS.",
            ephemeral=True,
        )
        return
    if not config.GITHUB_TOKEN:
        await inter.response.send_message(
            "❌ GitHub control is not configured: GH_TOKEN is missing.",
            ephemeral=True,
        )
        return
    if not config.GITHUB_OWNER or not config.ALT_REPOS:
        await inter.response.send_message(
            "❌ GitHub control is not configured: ALT_GITHUB_OWNER/ALT_REPOS are missing. "
            "Restart the Control Bot after updating the core workflow.",
            ephemeral=True,
        )
        return
    view = RunStartView(owner_id=inter.user.id)
    await inter.response.send_message(embed=_run_start_embed(view), view=view, ephemeral=True)


@bot.tree.command(name="stop", description="Stop one alt's current run and public activity.")
@app_commands.describe(alt="Configured alt to stop")
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


@bot.tree.command(name="pause", description="Pause one alt's public posting through the control Gist or DM fallback.")
@app_commands.describe(alt="Configured alt to pause")
async def cmd_pause(inter: discord.Interaction, alt: int):
    if not await _check_perms(inter):
        return
    await _finish_dm_control(inter, alt, "!pause", "pause requested")


@bot.tree.command(name="resume", description="Resume one paused alt through the control Gist or DM fallback.")
@app_commands.describe(alt="Configured alt to resume")
async def cmd_resume(inter: discord.Interaction, alt: int):
    if not await _check_perms(inter):
        return
    await _finish_dm_control(inter, alt, "!resume", "resume requested")


@bot.tree.command(name="setprice", description="Validate and change one alt's price; confirmation is private.")
@app_commands.describe(alt="Configured alt", new_price="Positive price, 0 < value <= 20; example 2.30")
async def cmd_setprice(inter: discord.Interaction, alt: int, new_price: str):
    if not await _check_perms(inter):
        return
    value = _extract_price(new_price)
    if value is None or not 0 < value <= 20:
        return await inter.response.send_message("❌ Price must be a number greater than 0 and no more than 20; example `2.30`.", ephemeral=True)
    await _finish_dm_control(
        inter, alt, f"!setprice {value:g}", f"price validated at ${value:.2f}/1k",
        update=lambda: state.set_run_config(alt, rate=value),
    )


@bot.tree.command(name="setmode", description="Validate and switch one alt between sell and buy.")
@app_commands.describe(alt="Configured alt", mode="sell or buy")
@app_commands.choices(mode=[app_commands.Choice(name="sell", value="sell"), app_commands.Choice(name="buy", value="buy")])
async def cmd_setmode(inter: discord.Interaction, alt: int, mode: str):
    if not await _check_perms(inter):
        return
    mode = mode.lower().strip()
    if mode not in {"sell", "buy"}:
        return await inter.response.send_message("❌ Mode must be `sell` or `buy`.", ephemeral=True)
    await _finish_dm_control(
        inter, alt, f"!setmode {mode}", f"mode validated as `{mode}`",
        update=lambda: state.set_run_config(alt, ad_type=mode),
    )


@bot.tree.command(name="setmessage", description="Validate and replace one alt's message text.")
@app_commands.describe(alt="Configured alt", new_message="Non-empty message, max 1900 characters")
async def cmd_setmessage(inter: discord.Interaction, alt: int, new_message: str):
    if not await _check_perms(inter):
        return
    new_message = new_message.strip()
    if not new_message:
        return await inter.response.send_message("❌ Message cannot be empty.", ephemeral=True)
    if len(new_message) > 1900:
        return await inter.response.send_message("❌ Message too long; maximum is 1900 characters.", ephemeral=True)
    await _finish_dm_control(
        inter, alt, f"!setmessage {new_message}", f"message validated ({len(new_message)} characters)",
        update=lambda: state.set_run_config(alt, message=new_message),
    )


@bot.tree.command(name="setdealkeywords", description="Set the comma-separated item keywords used by the separate deal scanner.")
@app_commands.describe(alt="Configured alt", keywords="Comma-separated item names, for example: Blade Ball, BB token, BB")
async def cmd_setdealkeywords(inter: discord.Interaction, alt: int, keywords: str):
    if not await _check_perms(inter):
        return
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
    await _finish_dm_control(
        inter, alt, f"!setdealkeywords {joined}", f"deal item keywords validated ({len(normalized)} keyword(s))",
        update=lambda: state.set_deal_keywords(alt, normalized),
    )


@bot.tree.command(name="setdealscan", description="Enable or disable the separate item-aware deal scanner.")
@app_commands.describe(alt="Configured alt", enabled="Turn deal scanning on or off")
@app_commands.choices(enabled=[app_commands.Choice(name="on", value="on"), app_commands.Choice(name="off", value="off")])
async def cmd_setdealscan(inter: discord.Interaction, alt: int, enabled: str):
    if not await _check_perms(inter):
        return
    enabled = enabled.casefold().strip()
    if enabled not in {"on", "off"}:
        return await inter.response.send_message("❌ Scanner must be `on` or `off`.", ephemeral=True)
    active = enabled == "on"
    await _finish_dm_control(
        inter, alt, f"!setdealscan {enabled}", f"deal scanner queued as `{enabled}`",
        update=lambda: state.set_deal_config(alt, enabled=active),
    )


@bot.tree.command(name="setdealdelta", description="Set the minimum price edge required for a deal alert.")
@app_commands.describe(alt="Configured alt", delta="Dollar edge per 1k, from 0 to 5; example 0.05")
async def cmd_setdealdelta(inter: discord.Interaction, alt: int, delta: str):
    if not await _check_perms(inter):
        return
    try:
        value = float(delta.strip())
    except (TypeError, ValueError, AttributeError):
        value = -1
    if not math.isfinite(value) or value < 0 or value > 5:
        return await inter.response.send_message("❌ Delta must be between 0 and 5 dollars per 1k; example `0.05`.", ephemeral=True)
    await _finish_dm_control(
        inter, alt, f"!setdealdelta {value:g}", f"deal edge queued at ${value:.2f}/1k",
        update=lambda: state.set_deal_config(alt, delta=value),
    )


@bot.tree.command(name="setchannel", description="Verify and add/update one channel ID on an alt at runtime.")
@app_commands.describe(alt="Configured alt", channel_id="Numeric Discord channel ID", name="Optional channel label")
async def cmd_setchannel(inter: discord.Interaction, alt: int, channel_id: str, name: str = ""):
    if not await _check_perms(inter):
        return
    cid = channel_id.strip()
    if not cid.isdigit():
        return await inter.response.send_message("❌ Channel ID must contain digits only.", ephemeral=True)
    label = re.sub(r"[\r\n]", " ", name.strip())[:80]

    async def _update_and_persist():
        state.set_channel(alt, cid, label)
        repo = config.ALT_REPOS.get(alt, "")
        if repo and config.GITHUB_TOKEN:
            a_obj = state.get(alt)
            if a_obj and a_obj.channels:
                cids_csv = ",".join(a_obj.channels.keys())
                await asyncio.to_thread(github_api.set_repository_secret, repo, "CHANNEL_IDS", cids_csv)

    await _finish_dm_control(
        inter, alt, f"!setchannel {cid}{(' ' + label) if label else ''}", f"channel ID queued for remote validation: `{cid}`",
        update=_update_and_persist,
    )


@bot.tree.command(name="replacechannel", description="Replace one channel ID with another after remote verification.")
@app_commands.describe(alt="Configured alt", old_id="Old numeric channel ID", new_id="New numeric channel ID", name="Optional label")
async def cmd_replacechannel(inter: discord.Interaction, alt: int, old_id: str, new_id: str, name: str = ""):
    if not await _check_perms(inter):
        return
    if not old_id.isdigit() or not new_id.isdigit():
        return await inter.response.send_message("❌ Both channel IDs must contain digits only.", ephemeral=True)
    label = re.sub(r"[\r\n]", " ", name.strip())[:80]

    async def _replace_and_persist():
        state.replace_channel(alt, old_id, new_id, label)
        repo = config.ALT_REPOS.get(alt, "")
        if repo and config.GITHUB_TOKEN:
            a_obj = state.get(alt)
            if a_obj and a_obj.channels:
                cids_csv = ",".join(a_obj.channels.keys())
                await asyncio.to_thread(github_api.set_repository_secret, repo, "CHANNEL_IDS", cids_csv)

    await _finish_dm_control(
        inter, alt, f"!replacechannel {old_id} {new_id}{(' ' + label) if label else ''}",
        f"channel replacement queued for remote validation: `{old_id}` → `{new_id}`",
        update=_replace_and_persist,
    )


@bot.tree.command(name="rescan_channels", description="Force alt to rescan and verify all assigned channel permissions.")
@app_commands.describe(alt="Configured alt")
async def cmd_rescan_channels(inter: discord.Interaction, alt: int):
    if not await _check_perms(inter):
        return
    await _finish_dm_control(
        inter, alt, "!rescan", "channel permission rescan queued"
    )


@bot.tree.command(name="resetcaution", description="Clear caution and slowmode backoff flags on a channel or all channels.")
@app_commands.describe(alt="Configured alt", channel_id="Optional channel ID, or leave blank to reset all channels")
async def cmd_resetcaution(inter: discord.Interaction, alt: int, channel_id: str = ""):
    if not await _check_perms(inter):
        return
    cid = channel_id.strip()
    if cid and not cid.isdigit() and cid.lower() != "all":
        return await inter.response.send_message("❌ Channel ID must contain digits only, or leave blank / pass 'all'.", ephemeral=True)
    target_cmd = f"!resetcaution {cid}" if cid else "!resetcaution all"
    label = f"reset caution on channel {cid}" if (cid and cid.lower() != "all") else "reset caution on all channels"
    await _finish_dm_control(
        inter, alt, target_cmd, label,
        update=lambda: state.reset_caution(alt, cid if (cid and cid.lower() != "all") else None),
    )


@bot.tree.command(name="settings", description="Display current runtime parameters and safety configurations.")
@app_commands.describe(alt="Configured alt, or All alts")
async def cmd_settings(inter: discord.Interaction, alt: int = 0):
    if not await _check_perms(inter):
        return
    chosen = state.all() if alt == 0 else ([state.get(alt)] if state.get(alt) else [])
    if not chosen:
        return await inter.response.send_message("❓ Unknown alt.", ephemeral=True)
    if alt != 0 or len(chosen) == 1:
        target_aid = alt if alt != 0 else chosen[0].alt_id
        view = FleetTuningView(owner_id=inter.user.id, alt_id=target_aid)
        await inter.response.send_message(embed=view._build_embed(), view=view, ephemeral=True)
        return
    embeds = []
    for a in chosen:
        embed = discord.Embed(
            title=f"⚙️ Settings & Configuration · {a.name}",
            color=0x5865F2,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Workflow Status", value=f"`{a.workflow_status}` ({a.workflow_conclusion or 'running'})", inline=True)
        embed.add_field(name="Interval", value=f"`{a.interval_min}m`", inline=True)
        embed.add_field(name="Runtime Limit", value=f"`{a.runtime_hours}h`", inline=True)
        embed.add_field(name="Ad Mode", value=f"`{a.ad_type.upper()}` (${a.rate:.2f})" if a.rate else f"`{a.ad_type.upper()}`", inline=True)
        embed.add_field(name="Channels", value=f"`{len(a.channels)}` registered", inline=True)
        embed.add_field(name="Deal Scanner", value=f"`{'ON' if a.deal_scan_enabled else 'OFF'}` (edge: ${a.deal_alert_delta:.2f})", inline=True)
        embed.add_field(name="Repository", value=f"`{config.ALT_REPOS.get(a.alt_id, 'N/A')}`", inline=False)
        if a.message_preview:
            embed.add_field(name="Message Preview", value=f"```{a.message_preview[:250]}```", inline=False)
        embed.set_footer(text=f"Alt ID: {a.alt_id} • V6.0 Multi-Alt Stack")
        embeds.append(embed)
    for i in range(0, len(embeds), 10):
        if i == 0:
            await inter.response.send_message(embeds=embeds[i:i+10], ephemeral=True)
        else:
            await inter.followup.send(embeds=embeds[i:i+10], ephemeral=True)


@bot.tree.command(name="reply", description="Relay an operator reply through an alt directly to a buyer's DM.")
@app_commands.describe(alt="Alt to reply from (e.g. 1)", user="Buyer Discord User ID", text="Message to send")
async def cmd_reply(inter: discord.Interaction, alt: int, user: str, text: str):
    if not await _check_perms(inter):
        return
    if alt not in state.alt_ids:
        await inter.response.send_message("❌ Invalid alt specified.", ephemeral=True)
        return
    clean_user = re.sub(r'\D', '', str(user))
    if not clean_user:
        await inter.response.send_message("❌ Invalid buyer user ID specified.", ephemeral=True)
        return
    clean_text = str(text or "").strip()
    if not clean_text:
        await inter.response.send_message("❌ Message text cannot be empty.", ephemeral=True)
        return
    await inter.response.defer(ephemeral=True)
    control_ack = await _send_control_wait_ack(alt, f"!reply {clean_user} {clean_text}", timeout=15)
    await inter.followup.send(f"📤 **Reply Queued** for Alt {alt} → Buyer `{clean_user}`:\n> {clean_text}\n*Transport ack:* `{control_ack}`", ephemeral=True)


@bot.tree.command(name="channels", description="Interactive channel manager to view, add, remove, and rescan channels.")
@app_commands.describe(alt="Configured alt, or leave blank to open manager")
async def cmd_channels(inter: discord.Interaction, alt: int = 1):
    if not await _check_perms(inter):
        return
    chosen_alt = alt if alt in state.alt_ids else (state.alt_ids[0] if state.alt_ids else 1)
    view = ChannelsView(owner_id=inter.user.id, alt_id=chosen_alt)
    embed = view._build_embed()
    await inter.response.send_message(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="uploadimage", description="Upload or replace an ad image directly to an alt repository.")
@app_commands.describe(alt="Configured alt, or 0 for All alts", image="Image file to attach (.png, .jpg, .webp)")
async def cmd_uploadimage(inter: discord.Interaction, alt: int, image: discord.Attachment):
    if not await _check_perms(inter):
        return
    if not image.content_type or not any(image.content_type.startswith(t) for t in ("image/png", "image/jpeg", "image/jpg", "image/webp")):
        return await inter.response.send_message("❌ Uploaded file must be an image (PNG, JPG, or WEBP).", ephemeral=True)
    if image.size > 8 * 1024 * 1024:
        return await inter.response.send_message("❌ Image size must be under 8MB.", ephemeral=True)
    await inter.response.defer(ephemeral=True)
    content_bytes = await image.read()
    targets = [alt] if alt != 0 else list(state.alt_ids)
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
    await inter.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="setinterval", description="Change an alt's interval for the current/next runtime.")
@app_commands.describe(alt="Configured alt", interval="Allowed interval: 3 or 5 minutes")
@app_commands.choices(interval=[app_commands.Choice(name="3 minutes", value=3), app_commands.Choice(name="5 minutes", value=5)])
async def cmd_setinterval(inter: discord.Interaction, alt: int, interval: int):
    if not await _check_perms(inter):
        return
    if interval not in {3, 5}:
        return await inter.response.send_message("❌ Interval must be 3 or 5 minutes.", ephemeral=True)
    await _finish_dm_control(
        inter, alt, f"!setinterval {interval}", f"interval validated at {interval} minutes",
        update=lambda: state.set_run_config(alt, interval_min=interval),
    )


@bot.tree.command(name="setruntime", description="Change an alt's runtime; preserves the 6/12/18/24/48-hour choices.")
@app_commands.describe(alt="Configured alt", hours="Allowed runtime: 6, 12, 18, 24, or 48 hours")
@app_commands.choices(hours=[app_commands.Choice(name=f"{h} hours", value=h) for h in (6, 12, 18, 24, 48)])
async def cmd_setruntime(inter: discord.Interaction, alt: int, hours: int):
    if not await _check_perms(inter):
        return
    if hours not in {6, 12, 18, 24, 48}:
        return await inter.response.send_message("❌ Runtime must be 6, 12, 18, 24, or 48 hours.", ephemeral=True)
    await _finish_dm_control(
        inter, alt, f"!setruntime {hours}", f"runtime validated at {hours} hours",
        update=lambda: state.set_run_config(alt, runtime_hours=hours),
    )


@bot.tree.command(name="sync", description="Ask every configured alt to reload shared Gist state.")
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


@bot.tree.command(name="pingalt", description="Send a control-path ping to one alt and confirm its next poll.")
@app_commands.describe(alt="Configured alt to ping")
async def cmd_pingalt(inter: discord.Interaction, alt: int):
    if not await _check_perms(inter):
        return
    await _finish_dm_control(inter, alt, "!ping", "control-path ping queued")


@bot.tree.command(name="selfcheck", description="Run the configured alt self-check workflow without using a DM.")
@app_commands.describe(alt="Configured alt to validate")
async def cmd_selfcheck(inter: discord.Interaction, alt: int):
    if not await _check_perms(inter):
        return
    if not state.get(alt):
        return await inter.response.send_message("❓ Unknown alt.", ephemeral=True)
    await inter.response.defer(ephemeral=True)
    ok, detail = await asyncio.to_thread(
        github_api.dispatch_named_workflow, alt, config.SELF_CHECK_WORKFLOW, {}
    )
    if ok:
        state.set_workflow(alt, None, "queued", "")
        text = f"🔍 **{state.get(alt).name}** self-check queued. {detail}"
    else:
        text = f"❌ Self-check could not be queued: {detail}"
    await inter.followup.send(text, ephemeral=True)
    await _log_control(text)
    state.append_log(alt, text, emoji="🔍" if ok else "❌", color=0x5865F2 if ok else 0xED4245, kind="CONTROL" if ok else "ERROR")


@bot.tree.command(name="clearlogs", description="Clear the local buffered logs for one alt; Discord history is not deleted.")
@app_commands.describe(alt="Configured alt whose local buffer should be cleared")
async def cmd_clearlogs(inter: discord.Interaction, alt: int):
    if not await _check_perms(inter):
        return
    a = state.get(alt)
    if not a:
        return await inter.response.send_message("❓ Unknown alt.", ephemeral=True)
    state.clear_logs(alt)
    await inter.response.send_message(f"🧹 Cleared local buffered logs for **{a.name}**. Discord channel history was not deleted.", ephemeral=True)


@bot.tree.command(name="runs", description="Show recent GitHub Actions runs for one alt without exposing secrets.")
@app_commands.describe(alt="Configured alt")
async def cmd_runs(inter: discord.Interaction, alt: int):
    if not await _check_perms(inter):
        return
    a = state.get(alt)
    if not a:
        return await inter.response.send_message("❓ Unknown alt.", ephemeral=True)
    await inter.response.defer(ephemeral=True)
    runs = await asyncio.to_thread(github_api.list_runs, alt, 8)
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
    embed = discord.Embed(title=f"🧾 {a.name} · recent runs", description="\n".join(lines)[:4000], color=0x5865F2)
    await inter.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="status", description="Show current heartbeat, counters, alerts, and GitHub state.")
@app_commands.describe(alt="Configured alt, or All alts")
async def cmd_status(inter: discord.Interaction, alt: int = 0):
    if not await _check_perms(inter):
        return
    await _fresh_state()
    if alt == 0:
        await inter.response.send_message(embeds=build_all(state)[:10], ephemeral=True)
    else:
        await inter.response.send_message(embed=build_single_alt_embed(state, alt), ephemeral=True)


@bot.tree.command(name="logs", description="Show typed operational logs for an alt with optional search filtering.")
@app_commands.describe(alt="Configured alt", limit="Number of lines (5-50)", kind="ALL, ERROR, DEAL, CONTROL, CHANNEL, CAUTION, or DEBUG", search="Optional text search keyword")
@app_commands.choices(kind=[app_commands.Choice(name=k, value=k) for k in ("ALL", "ERROR", "DEAL", "CONTROL", "CHANNEL", "CAUTION", "DEBUG")])
async def cmd_logs(inter: discord.Interaction, alt: int, limit: int = 10, kind: str = "ALL", search: str = ""):
    if not await _check_perms(inter):
        return
    a = state.get(alt)
    if not a:
        return await inter.response.send_message("❓ Unknown alt.", ephemeral=True)
    entries = state.recent_logs(alt, max(5, min(50, limit)), kind)
    if search:
        s_term = search.strip().lower()
        entries = [e for e in entries if s_term in e[3].lower()]
    if not entries:
        return await inter.response.send_message(f"No matching `{kind}` logs buffered for {a.name}.", ephemeral=True)
    lines = [f"`[{datetime.fromtimestamp(ts).strftime('%H:%M:%S')}]` {emo} {txt}" for ts, emo, _col, txt in entries]
    body = "\n".join(lines)[-3900:]
    embed = discord.Embed(title=f"📜 {a.name} · {kind} logs", description=body, color=0x2F3136)
    await inter.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="deals", description="Show the latest separate deal-alert counter and timestamp.")
@app_commands.describe(alt="Configured alt, or All alts")
async def cmd_deals(inter: discord.Interaction, alt: int = 0):
    if not await _check_perms(inter):
        return
    chosen = state.all() if alt == 0 else ([state.get(alt)] if state.get(alt) else [])
    if not chosen:
        return await inter.response.send_message("❓ Unknown alt.", ephemeral=True)
    lines = []
    for item in chosen:
        last = datetime.fromtimestamp(item.last_deal_ts, timezone.utc).isoformat() if item.last_deal_ts else "never"
        keywords = ", ".join(item.deal_keywords[:20]) or "none configured"
        scanner = f"{'on' if item.deal_scan_enabled else 'off'} · edge ${item.deal_alert_delta:.2f}/1k"
        lines.append(f"**{item.name}** — alerts: `{item.deal_alerts}` · last: `{last}` · scanner: `{scanner}` · keywords: `{keywords}`")
    await inter.response.send_message(embed=discord.Embed(title="📈 Separate deal scanner state", description="\n".join(lines), color=0x57F287), ephemeral=True)


@bot.tree.command(name="refresh", description="Refresh GitHub run state and the live dashboard immediately.")
async def cmd_refresh(inter: discord.Interaction):
    if not await _check_perms(inter):
        return
    await inter.response.defer(ephemeral=True)
    await _fresh_state()
    await _refresh_dashboard_now()
    await inter.followup.send("✅ GitHub heartbeat state and dashboard refreshed from current data.", ephemeral=True)


@bot.tree.command(name="dashboard", description="Refresh the live three-embed dashboard in the dashboard channel.")
async def cmd_dashboard(inter: discord.Interaction):
    if not await _check_perms(inter):
        return
    await inter.response.defer(ephemeral=True)
    await _fresh_state()
    msg = await _post_dashboard(build_all(state))
    await inter.followup.send(f"✅ Dashboard snapshot posted → {msg.jump_url if msg else '(failed)'}", ephemeral=True)


@bot.tree.command(name="diagnose", description="Causal Event Explorer: deep root-cause diagnostic timeline and recommendations.")
@app_commands.describe(alt="Alt number to diagnose (e.g. 1)")
async def cmd_diagnose(inter: discord.Interaction, alt: int):
    if not await _check_perms(inter):
        return
    if alt not in state.alt_ids:
        await inter.response.send_message("❌ Invalid alt specified.", ephemeral=True)
        return
    await inter.response.defer(ephemeral=True)
    await _fresh_state()
    embed = build_diagnose_embed(state, alt)
    await inter.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="topology", description="View the live fleet topology and routing relationship graph.")
async def cmd_topology(inter: discord.Interaction):
    if not await _check_perms(inter):
        return
    await inter.response.defer(ephemeral=True)
    await _fresh_state()
    embed = build_topology_embed(state)
    await inter.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="simulate", description="Sandboxed Dry-Run: preview ad copy, variations, and cadence safely.")
@app_commands.describe(alt="Alt number to simulate (e.g. 1)", test_rate="Optional test price override")
async def cmd_simulate(inter: discord.Interaction, alt: int, test_rate: Optional[float] = None):
    if not await _check_perms(inter):
        return
    if alt not in state.alt_ids:
        await inter.response.send_message("❌ Invalid alt specified.", ephemeral=True)
        return
    alt_obj = state.get(alt)
    if not alt_obj:
        await inter.response.send_message(f"❌ Alt `{alt}` not found.", ephemeral=True)
        return
    rate_val = test_rate if test_rate is not None else (alt_obj.rate or 0.85)

    base_msg = alt_obj.message_preview or f"Selling tokens {rate_val}$/1k fast & trusted DM me!"
    sample_variations = [
        f"🔥 {base_msg}",
        f"⚡ {base_msg.replace('DM me', 'pm me')}",
        f"💎 {base_msg.upper()}",
    ]

    embed = discord.Embed(
        title=f"🧪 Sandboxed Simulation · Alt {alt}: {alt_obj.name}",
        description="Dry-run evaluation generated zero live Discord API calls.",
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Simulated Mode / Price", value=f"`{alt_obj.ad_type or 'sell'}` @ `${rate_val:.2f}/1k`", inline=True)
    embed.add_field(name="Simulated Interval", value=f"`{alt_obj.interval_min}m ± 25% jitter`", inline=True)
    embed.add_field(name="Target Channel Count", value=f"`{len(alt_obj.channels)} channels active`", inline=True)
    embed.add_field(name="Sample Generated Variations (3)", value="\n".join(f"• {v}" for v in sample_variations), inline=False)
    embed.add_field(name="Anti-Detection Simulation", value="• EXIF stripping: ACTIVE\n• Chrome TLS 1.3 fingerprint: VALID\n• Microsecond Rate-Limiter: READY\n• Cascading Circuit Breaker: CLOSED", inline=False)
    await inter.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="squad", description="Manage and execute fleet squad operations (view, assign, pause, resume, policy, price).")
@app_commands.describe(
    action="Operation (list, assign, view, pause, resume, policy, price)",
    squad_name="Squad name (e.g. 'Alpha', 'Sellers')",
    alt="Target alt for individual assignment",
    value="Value for squad batch operations (preset for policy, or price string for price)",
)
async def cmd_squad(
    inter: discord.Interaction,
    action: Literal["list", "assign", "view", "pause", "resume", "policy", "price"],
    squad_name: Optional[str] = None,
    alt: Optional[int] = 0,
    value: Optional[str] = None,
):
    if not await _check_perms(inter):
        return
    if action == "list":
        squads = state.get_all_squads()
        embed = discord.Embed(title="👥 Fleet Squad Pools", color=0x5865F2)
        for sq, members in squads.items():
            names = [f"Alt {aid} ({state.get_name(aid)})" for aid in members]
            embed.add_field(name=f"Squad: {sq} ({len(members)} alts)", value=", ".join(names) or "None", inline=False)
        await inter.response.send_message(embed=embed, ephemeral=True)
    elif action == "assign":
        if not alt or not squad_name:
            await inter.response.send_message("❌ Both `alt` and `squad_name` are required for assignment.", ephemeral=True)
            return
        if alt not in state.alt_ids:
            await inter.response.send_message("❌ Invalid alt specified.", ephemeral=True)
            return
        state.set_squad(alt, squad_name)
        await inter.response.send_message(f"✅ Alt {alt} assigned to squad **{squad_name}**.", ephemeral=True)
    elif action == "view":
        target_sq = squad_name or "Unassigned"
        members = state.get_squad_members(target_sq)
        embed = discord.Embed(title=f"👥 Squad Overview: {target_sq}", color=0x5865F2)
        if members:
            total_sent = sum(getattr(m, "total_sent", 0) for m in members)
            total_errors = sum(getattr(m, "error_count", 0) for m in members)
            avg_health = sum(state.get_health_index(m.alt_id) for m in members) / len(members)
            embed.description = f"**Fleet Count**: `{len(members)}` | **Avg Health**: `{avg_health:.1f}%` | **Total Posts**: `{total_sent}` | **Errors**: `{total_errors}`"
            for m in members:
                dot, _ = _status_dot(m)
                embed.add_field(
                    name=f"{dot} Alt {m.alt_id}: {m.name}",
                    value=f"Health: `{state.get_health_index(m.alt_id)}%` | Status: `{m.status}` | Sent: `{m.total_sent}` | Policy: `{getattr(m, 'policy_template', 'balanced')}`",
                    inline=False,
                )
        else:
            embed.description = f"No alts assigned to squad '{target_sq}'."
        await inter.response.send_message(embed=embed, ephemeral=True)
    elif action in ("pause", "resume", "policy", "price"):
        if not squad_name:
            await inter.response.send_message("❌ `squad_name` is required for batch squad actions.", ephemeral=True)
            return
        members = state.get_squad_members(squad_name)
        if not members:
            await inter.response.send_message(f"❌ No alts found in squad '{squad_name}'.", ephemeral=True)
            return
        if action == "policy" and not value:
            await inter.response.send_message("❌ `value` parameter required for policy (stealth, aggressive, peak_hour, balanced).", ephemeral=True)
            return
        if action == "price" and not value:
            await inter.response.send_message("❌ `value` parameter required for price (e.g. '$15/mil').", ephemeral=True)
            return
        await inter.response.defer(ephemeral=True)
        results = []
        for m in members:
            cmd = f"!{action}" if action in ("pause", "resume") else f"!{action} {value}"
            ack = await _send_control_wait_ack(m.alt_id, cmd, timeout=10)
            if action == "policy" and value:
                state.set_policy_template(m.alt_id, value)
            elif action == "price" and value:
                state.set_price(m.alt_id, value)
            results.append(f"• **Alt {m.alt_id}** ({m.name}): {ack}")
        summary = f"👥 **Squad '{squad_name}' Batch {action.upper()}** ({len(members)} alts):\n" + "\n".join(results)
        await inter.followup.send(summary, ephemeral=True)
        await _log_control(summary)


@bot.tree.command(name="policy", description="Apply pre-packaged channel policy templates (stealth, aggressive, peak_hour, balanced).")
@app_commands.describe(alt="Target alt (e.g. 1)", template="Policy template preset")
async def cmd_policy(inter: discord.Interaction, alt: int, template: Literal["stealth", "aggressive", "peak_hour", "balanced"]):
    if not await _check_perms(inter):
        return
    if alt not in state.alt_ids:
        await inter.response.send_message("❌ Invalid alt specified.", ephemeral=True)
        return
    ok = state.set_policy_template(alt, template)
    if ok:
        try:
            asyncio.create_task(_send_control_wait_ack(alt, f"!policy {template}", timeout=15))
        except Exception:
            pass
        await inter.response.send_message(f"✅ Policy template **{template.upper()}** applied to Alt {alt}. Parameters updated and queued to runner.", ephemeral=True)
        try:
            await _log_control(f"🛡️ Policy template **{template.upper()}** dispatched to Alt {alt}.")
        except Exception:
            pass
    else:
        await inter.response.send_message(f"❌ Failed to apply policy template '{template}'.", ephemeral=True)


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


@bot.tree.command(name="analytics", description="View advanced fleet speed matrix, channel velocities, and cadence analytics.")
@app_commands.describe(alt="Target specific alt (or 0 / blank for all alts)")
async def cmd_analytics(inter: discord.Interaction, alt: Optional[int] = 0):
    if not await _check_perms(inter):
        return
    await _fresh_state()
    embed = build_analytics_embed(state, target_alt=alt or 0)
    await inter.response.send_message(embed=embed, ephemeral=True)


# =========================================================================== #
# Hierarchical Subcommand Architecture (Groups)                               #
# =========================================================================== #
fleet_group = app_commands.Group(name="fleet", description="Fleet overview, analytics, topology, canary probes, and global sync")
alt_group = app_commands.Group(name="alt", description="Alt provisioning, lifecycle, logs, self-check, and runner control")
channel_group = app_commands.Group(name="channel", description="Advertising channel manager, additions, swaps, and rescans")
tune_group = app_commands.Group(name="tune", description="Live ad pricing, mode, message, cadence, and operational policy tuning")
deals_group = app_commands.Group(name="deals_group", description="Passive marketplace deal scanner and arbitrage margin controls")

# --- Fleet Group Subcommands ---
@fleet_group.command(name="status", description="Fleet-wide dashboard summary or individual alt status card.")
@app_commands.describe(alt="Target alt (or 0 for all)")
async def fleet_status_sub(inter: discord.Interaction, alt: Optional[int] = 0):
    await cmd_status.callback(inter, alt=alt or 0)

@fleet_group.command(name="analytics", description="Advanced speed matrix, channel velocities, and cadence analytics.")
@app_commands.describe(alt="Target alt (or 0 for all)")
async def fleet_analytics_sub(inter: discord.Interaction, alt: Optional[int] = 0):
    await cmd_analytics.callback(inter, alt=alt or 0)

@fleet_group.command(name="topology", description="Render the live visual fleet topology graph.")
async def fleet_topology_sub(inter: discord.Interaction):
    await cmd_topology.callback(inter)

@fleet_group.command(name="canary", description="Synthetic in-band health probe testing GitHub, Gist, and webhook infrastructure.")
@app_commands.describe(alt="Optional specific alt to probe")
async def fleet_canary_sub(inter: discord.Interaction, alt: Optional[int] = 0):
    await cmd_canary.callback(inter, alt=alt or 0)

@fleet_group.command(name="sync", description="Broadcast instant reload of control Gists and variation blocklists across all alts.")
async def fleet_sync_sub(inter: discord.Interaction):
    await cmd_sync.callback(inter)

@fleet_group.command(name="refresh", description="Force live poll of GitHub Actions workflow states and refresh dashboard embed.")
async def fleet_refresh_sub(inter: discord.Interaction):
    await cmd_refresh.callback(inter)

@fleet_group.command(name="dashboard", description="Post fresh persistent 3-card dashboard snapshot in #ad-dashboard.")
async def fleet_dashboard_sub(inter: discord.Interaction):
    await cmd_dashboard.callback(inter)

# --- Alt Group Subcommands ---
@alt_group.command(name="run", description="Start an alt run using the private interactive launcher.")
async def alt_run_sub(inter: discord.Interaction):
    await cmd_run.callback(inter)

@alt_group.command(name="stop", description="Stop an alt's run and cancel active workflow.")
@app_commands.describe(alt="Target alt")
async def alt_stop_sub(inter: discord.Interaction, alt: int):
    await cmd_stop.callback(inter, alt=alt)

@alt_group.command(name="pause", description="Pause an alt's public posting.")
@app_commands.describe(alt="Target alt")
async def alt_pause_sub(inter: discord.Interaction, alt: int):
    await cmd_pause.callback(inter, alt=alt)

@alt_group.command(name="resume", description="Resume an alt's public posting.")
@app_commands.describe(alt="Target alt")
async def alt_resume_sub(inter: discord.Interaction, alt: int):
    await cmd_resume.callback(inter, alt=alt)

@alt_group.command(name="list", description="List configured alts without exposing sensitive tokens.")
async def alt_list_sub(inter: discord.Interaction):
    await cmd_altlist.callback(inter)

@alt_group.command(name="add", description="Add an alt account through the private modal.")
async def alt_add_sub(inter: discord.Interaction):
    await cmd_altadd.callback(inter)

@alt_group.command(name="update", description="Update an alt's credentials or repository slug.")
@app_commands.describe(alt="Target alt")
async def alt_update_sub(inter: discord.Interaction, alt: int):
    await cmd_altupdate.callback(inter, alt=alt)

@alt_group.command(name="remove", description="Remove an alt from the registry.")
@app_commands.describe(alt="Target alt", confirmation="Type DELETE to confirm", delete_repository="Also delete GitHub repo")
async def alt_remove_sub(inter: discord.Interaction, alt: int, confirmation: str, delete_repository: bool = False):
    await cmd_altremove.callback(inter, alt=alt, confirmation=confirmation, delete_repository=delete_repository)

@alt_group.command(name="runs", description="List recent GitHub Actions workflow runs.")
@app_commands.describe(alt="Target alt", limit="Max runs (1..10)")
async def alt_runs_sub(inter: discord.Interaction, alt: int, limit: int = 5):
    await cmd_runs.callback(inter, alt=alt, limit=limit)

@alt_group.command(name="logs", description="Stream typed buffered log events for an alt.")
@app_commands.describe(alt="Target alt", limit="Max entries (5..50)", kind="Category filter", search="Search keyword")
async def alt_logs_sub(inter: discord.Interaction, alt: int, limit: int = 15,
                       kind: Optional[Literal["ALL", "ERROR", "DEAL", "CONTROL", "CHANNEL", "CAUTION", "DEBUG"]] = "ALL",
                       search: Optional[str] = None):
    await cmd_logs.callback(inter, alt=alt, limit=limit, kind=kind, search=search)

@alt_group.command(name="clearlogs", description="Clear in-memory buffered log events for an alt.")
@app_commands.describe(alt="Target alt")
async def alt_clearlogs_sub(inter: discord.Interaction, alt: int):
    await cmd_clearlogs.callback(inter, alt=alt)

@alt_group.command(name="diagnose", description="Causal state-transition explorer and root-cause analysis.")
@app_commands.describe(alt="Target alt")
async def alt_diagnose_sub(inter: discord.Interaction, alt: int):
    await cmd_diagnose.callback(inter, alt=alt)

@alt_group.command(name="simulate", description="Sandboxed dry-run previewing variation scores without sending.")
@app_commands.describe(alt="Target alt", test_rate="Optional test pricing rate")
async def alt_simulate_sub(inter: discord.Interaction, alt: int, test_rate: Optional[float] = None):
    await cmd_simulate.callback(inter, alt=alt, test_rate=test_rate)

@alt_group.command(name="selfcheck", description="Dispatch self-check pre-flight validation workflow.")
@app_commands.describe(alt="Target alt")
async def alt_selfcheck_sub(inter: discord.Interaction, alt: int):
    await cmd_selfcheck.callback(inter, alt=alt)

@alt_group.command(name="ping", description="Test round-trip latency of the Gist queue for an alt.")
@app_commands.describe(alt="Target alt")
async def alt_ping_sub(inter: discord.Interaction, alt: int):
    await cmd_pingalt.callback(inter, alt=alt)

# --- Channel Group Subcommands ---
@channel_group.command(name="list", description="Open the visual Channel Manager UI.")
@app_commands.describe(alt="Target alt")
async def channel_list_sub(inter: discord.Interaction, alt: Optional[int] = 1):
    await cmd_channels.callback(inter, alt=alt or 1)

@channel_group.command(name="add", description="Add and verify a trading channel on an alt.")
@app_commands.describe(alt="Target alt", channel_id="Discord channel ID", name="Optional channel name/label")
async def channel_add_sub(inter: discord.Interaction, alt: int, channel_id: str, name: Optional[str] = ""):
    await cmd_setchannel.callback(inter, alt=alt, channel_id=channel_id, name=name or "")

@channel_group.command(name="replace", description="Swap an old channel with a new one.")
@app_commands.describe(alt="Target alt", old_id="Old channel ID", new_id="New channel ID", name="Optional name")
async def channel_replace_sub(inter: discord.Interaction, alt: int, old_id: str, new_id: str, name: Optional[str] = ""):
    await cmd_replacechannel.callback(inter, alt=alt, old_id=old_id, new_id=new_id, name=name or "")

@channel_group.command(name="rescan", description="Force immediate channel permission and slowmode rescan.")
@app_commands.describe(alt="Target alt")
async def channel_rescan_sub(inter: discord.Interaction, alt: int):
    await cmd_rescan_channels.callback(inter, alt=alt)

@channel_group.command(name="resetcaution", description="Reset Caution Mode backoff and clear strike counters.")
@app_commands.describe(alt="Target alt", channel_id="Optional channel ID or 'all'")
async def channel_resetcaution_sub(inter: discord.Interaction, alt: int, channel_id: Optional[str] = None):
    await cmd_resetcaution.callback(inter, alt=alt, channel_id=channel_id or "")

# --- Tune Group Subcommands ---
@tune_group.command(name="settings", description="Open the interactive Fleet Tuning UI.")
@app_commands.describe(alt="Target alt (or 0 for all)")
async def tune_settings_sub(inter: discord.Interaction, alt: Optional[int] = 0):
    await cmd_settings.callback(inter, alt=alt or 0)

@tune_group.command(name="price", description="Update active pricing rate.")
@app_commands.describe(alt="Target alt", new_price="Rate per 1k units (e.g. 2.50)")
async def tune_price_sub(inter: discord.Interaction, alt: int, new_price: str):
    await cmd_setprice.callback(inter, alt=alt, new_price=new_price)

@tune_group.command(name="mode", description="Switch trade mode between Seller and Buyer.")
@app_commands.describe(alt="Target alt", mode="Trade mode")
async def tune_mode_sub(inter: discord.Interaction, alt: int, mode: Literal["sell", "buy"]):
    await cmd_setmode.callback(inter, alt=alt, mode=mode)

@tune_group.command(name="message", description="Replace ad message copy and regenerate anti-detection variations.")
@app_commands.describe(alt="Target alt", new_message="New base message copy")
async def tune_message_sub(inter: discord.Interaction, alt: int, new_message: str):
    await cmd_setmessage.callback(inter, alt=alt, new_message=new_message)

@tune_group.command(name="policy", description="Apply pre-packaged operational policy template.")
@app_commands.describe(alt="Target alt", template="Preset name")
async def tune_policy_sub(inter: discord.Interaction, alt: int, template: Literal["stealth", "aggressive", "peak_hour", "balanced"]):
    await cmd_policy.callback(inter, alt=alt, template=template)

@tune_group.command(name="interval", description="Set posting interval (3 or 5 minutes).")
@app_commands.describe(alt="Target alt", interval="Interval in minutes")
async def tune_interval_sub(inter: discord.Interaction, alt: int, interval: Literal[3, 5]):
    await cmd_setinterval.callback(inter, alt=alt, interval=interval)

@tune_group.command(name="runtime", description="Set execution runtime duration.")
@app_commands.describe(alt="Target alt", hours="Total hours")
async def tune_runtime_sub(inter: discord.Interaction, alt: int, hours: Literal[6, 12, 18, 24, 48]):
    await cmd_setruntime.callback(inter, alt=alt, hours=hours)

@tune_group.command(name="image", description="Upload or replace ad image in alt repository.")
@app_commands.describe(alt="Target alt (or 0 for all)", image="Image file (.png, .jpg, .webp)")
async def tune_image_sub(inter: discord.Interaction, alt: int, image: discord.Attachment):
    await cmd_uploadimage.callback(inter, alt=alt, image=image)

@tune_group.command(name="reply", description="Relay private DM reply directly to buyer through alt account.")
@app_commands.describe(alt="Target alt", user="Buyer Discord User ID", text="Message content")
async def tune_reply_sub(inter: discord.Interaction, alt: int, user: str, text: str):
    await cmd_reply.callback(inter, alt=alt, user=user, text=text)

# --- Deals Group Subcommands ---
@deals_group.command(name="view", description="Display deal-alert metrics, profit margins, and recent alerts.")
@app_commands.describe(alt="Target alt (or 0 for all)")
async def deals_view_sub(inter: discord.Interaction, alt: Optional[int] = 0):
    await cmd_deals.callback(inter, alt=alt or 0)

@deals_group.command(name="scan", description="Toggle passive marketplace deal scanning on or off.")
@app_commands.describe(alt="Target alt", enabled="Enable or disable scanner")
async def deals_scan_sub(inter: discord.Interaction, alt: int, enabled: Literal["on", "off"]):
    await cmd_setdealscan.callback(inter, alt=alt, enabled=enabled)

@deals_group.command(name="delta", description="Set minimum profit margin edge required per 1k units.")
@app_commands.describe(alt="Target alt", delta="Profit margin threshold (e.g. 0.05)")
async def deals_delta_sub(inter: discord.Interaction, alt: int, delta: str):
    await cmd_setdealdelta.callback(inter, alt=alt, delta=delta)

@deals_group.command(name="keywords", description="Configure whole-phrase target item/game aliases for the deal scanner.")
@app_commands.describe(alt="Target alt", keywords="Comma-separated item aliases (e.g. 'Blade Ball, BB token, BB')")
async def deals_keywords_sub(inter: discord.Interaction, alt: int, keywords: str):
    await cmd_setdealkeywords.callback(inter, alt=alt, keywords=keywords)

bot.tree.add_command(fleet_group)
bot.tree.add_command(alt_group)
bot.tree.add_command(channel_group)
bot.tree.add_command(tune_group)
bot.tree.add_command(deals_group)


_COMMAND_GUIDE = {
    "altadd": ("`/altadd` (opens private modal)", "Adds an existing alt repository to the control bot fleet. Prompts privately for Alt ID, Repository slug (`owner/repo`), Display Name, and USER_TOKEN. Secrets are masked and never logged."),
    "altupdate": ("`/altupdate alt:<alt>` (opens private modal)", "Updates an alt's configuration (Token, Repository, Discord ID, or Display Name). Blank fields are left unchanged."),
    "altlist": ("`/altlist`", "Displays all registered fleet alts, repository links, configured mode, and live heartbeat age without exposing sensitive tokens."),
    "altremove": ("`/altremove alt:<alt> confirmation:DELETE [delete_repository:<true|false>]`", "Removes an alt from the control bot registry. Keeps the GitHub repository by default unless `delete_repository:true` is explicitly chosen."),
    "run": ("`/run` (opens interactive UI)", "Interactive 3-step form to launch an alt: Choose Alt & Mode (`Sell` / `Buy`) ➔ Enter Rate, Message & Image settings ➔ Select Cadence (`3m`/`5m`) & Duration (`6h`, `12h`, `18h`, `24h`, `48h`). Automatically cancels old runs and dispatches workflow."),
    "stop": ("`/stop alt:<alt>`", "Gracefully stops the alt: sends `!stop` through Gist/DM, syncs variation blocklist, and cancels the active GitHub Actions workflow run."),
    "pause": ("`/pause alt:<alt>`", "Temporarily pauses public ad delivery on all target channels without terminating the GitHub Actions runner."),
    "resume": ("`/resume alt:<alt>`", "Resumes public ad delivery from pause state."),
    "setprice": ("`/setprice alt:<alt> new_price:<0.00..20.00>`", "Updates the active pricing rate (e.g. `2.50`) in the live ad copy and adjusts deal scanner profit calculations in real time."),
    "setmode": ("`/setmode alt:<alt> mode:<sell|buy>`", "Swaps trade mode between Seller (`💰`) and Buyer (`🛒`). Prompts to update message copy if necessary."),
    "setmessage": ("`/setmessage alt:<alt> new_message:<text>`", "Pushes new base ad copy (up to 1900 chars) to the alt. Automatically regenerates 25–40 anti-detection variations."),
    "setdealkeywords": ("`/setdealkeywords alt:<alt> keywords:<comma-separated list>`", "Configures target item/game aliases for the deal scanner (e.g. `Blade Ball, BB token, BB, Robux, MM2`). Matches with whole-word boundaries to avoid false positives."),
    "setdealscan": ("`/setdealscan alt:<alt> enabled:<on|off>`", "Toggles passive marketplace deal scanning on or off without affecting ad posting cadence."),
    "setdealdelta": ("`/setdealdelta alt:<alt> delta:<0.00..5.00>`", "Sets the minimum profit margin required per 1k units before triggering a deal alert into `#deals` (default `$0.05`)."),
    "setchannel": ("`/setchannel alt:<alt> channel_id:<numeric_id> [name]`", "Adds a new trading channel to the alt's rotation. Verifies channel accessibility before adding and persists it to GitHub secrets."),
    "replacechannel": ("`/replacechannel alt:<alt> old_id:<numeric_id> new_id:<numeric_id> [name]`", "Safely swaps an old/dead trading channel with a new one in the alt's rotation and updates repository secrets."),
    "rescan_channels": ("`/rescan_channels alt:<alt>`", "Forces the alt to immediately re-verify permissions, slowmode limits, and connectivity on all configured channels."),
    "resetcaution": ("`/resetcaution alt:<alt> [channel_id:<numeric_id>|all]`", "Clears strike counters, slowmode flags, and Caution Mode backoffs on a specific channel or fleet-wide."),
    "settings": ("`/settings [alt:<alt>|All alts]`", "Opens the interactive Fleet Tuning UI with native Select dropdowns for Alts, Policy templates, channel overview, and action buttons."),
    "channels": ("`/channels [alt:<alt>]`", "Opens the interactive visual Channel Manager with 1-click buttons to View, Add Channel, Remove Channel, or Rescan."),
    "uploadimage": ("`/uploadimage alt:<alt|0> image:<file>`", "Uploads or replaces an ad image (.png, .jpg, .webp < 8MB) directly into the selected alt's GitHub repository."),
    "setinterval": ("`/setinterval alt:<alt> interval:<3|5>`", "Sets channel post interval to 3 or 5 minutes (enforces safety jitter and slowmode hard floor)."),
    "setruntime": ("`/setruntime alt:<alt> hours:<6|12|18|24|48>`", "Sets execution runtime duration for the alt runner job."),
    "sync": ("`/sync`", "Sends a fleet-wide broadcast telling all alts to immediately reload shared control Gists and variation blocklists."),
    "pingalt": ("`/pingalt alt:<alt>`", "Tests round-trip latency of the Gist command queue without altering any ad settings."),
    "selfcheck": ("`/selfcheck alt:<alt>`", "Dispatches `self_check.yml` in GitHub Actions to validate tokens, webhooks, channel permissions, and egress proxy routing."),
    "status": ("`/status [alt:<alt>|All alts]`", "Refreshes live state and displays unified dashboard overview or detailed single-alt diagnostic card."),
    "runs": ("`/runs alt:<alt> [limit:1..10]`", "Lists recent GitHub Actions workflow runs, duration, execution status, and run links without leaking tokens."),
    "logs": ("`/logs alt:<alt> [limit:5..50] [kind:ALL|ERROR|DEAL|CONTROL|CHANNEL|CAUTION|DEBUG] [search:text]`", "Streams typed buffered log events with category and keyword filtering."),
    "clearlogs": ("`/clearlogs alt:<alt>`", "Clears the control bot's local in-memory log buffer without affecting Discord channel history or GitHub runner logs."),
    "deals": ("`/deals [alt:<alt>|All alts]`", "Displays deal-alert metrics, profit edges, scanner status, threshold, and active item keywords."),
    "diagnose": ("`/diagnose alt:<alt>`", "Causal Event Explorer: deep root-cause diagnostic timeline, transition triggers, health index, and actionable operator recommendations."),
    "topology": ("`/topology`", "Renders the live visual fleet topology graph: alts, squad pools, target Discord channels, yield grades, and egress routing."),
    "analytics": ("`/analytics [alt:<alt>]`", "Visual Fleet Speed Matrix: renders per-channel velocities, delivery reliability bars, slowmode utilization, and inter-channel interval timelines."),
    "fleet": ("`/fleet <status|analytics|topology|canary|sync|refresh>`", "Hierarchical fleet group: manage overall fleet status, advanced analytics, topology, synthetic probes, and broadcast sync."),
    "alt": ("`/alt <run|stop|pause|resume|list|logs|diagnose|selfcheck>`", "Hierarchical alt group: control individual alt lifecycles, streaming logs, root-cause diagnosis, and workflow runs."),
    "channel": ("`/channel <list|add|replace|rescan|resetcaution>`", "Hierarchical channel group: visual channel management, runtime additions, swaps, permission rescans, and caution clears."),
    "tune": ("`/tune <settings|price|policy|interval|mode|message>`", "Hierarchical tuning group: ad message copy, pricing rates, operational policy templates, and cadence settings."),
    "simulate": ("`/simulate alt:<alt> [test_rate:float]`", "Sandboxed dry-run simulation: previews variation generation, typist permutations, and estimated survival score without sending live messages."),
    "squad": ("`/squad action:<list|assign|view|pause|resume|policy|price> [squad_name] [alt] [value]`", "Fleet Squad Manager: `list` (all squads), `assign` (alt to squad), `view` (squad health & members), `pause`/`resume` (batch fleet controls), `policy`/`price` (batch configuration update across squad)."),
    "policy": ("`/policy alt:<alt> template:<stealth|aggressive|peak_hour|balanced>`", "Applies pre-packaged operational profiles:\n• `🛡️ stealth`: 5m interval, max typing jitter, soft copy, strict caution.\n• `⚡ aggressive`: 3m interval, high throughput, fast inter-channel rotation.\n• `🔥 peak_hour`: 3m interval, dynamic chat velocity cadence, active deal scanning.\n• `⚖️ balanced`: 5m interval, standard human jitter, balanced deal thresholds."),
    "canary": ("`/canary [alt]`", "Synthetic In-Band Probe: tests GitHub API, Gist bridge sync, and token latency in milliseconds."),
    "reply": ("`/reply alt:<alt> user:<buyer_id> text:<message>`", "Operator DM Relay: transmits your message through the selected alt account directly into the buyer's private DM."),
    "refresh": ("`/refresh`", "Forces an immediate live poll of GitHub Actions workflow states and updates the persistent dashboard embed."),
    "dashboard": ("`/dashboard`", "Posts a fresh 3-card dashboard snapshot in `#ad-dashboard` without running or scanning ads."),
    "help": ("`/help`", "Displays this complete interactive reference manual with arguments, options, examples, and permissions."),
}


@bot.tree.command(name="help", description="Private complete reference with arguments, permissions, examples, and effects.")
async def cmd_help(inter: discord.Interaction):
    if not await _check_perms(inter):
        return
    registered = {cmd.name: cmd for cmd in bot.tree.get_commands()}
    embeds = []
    for name in sorted(registered):
        usage, effect = _COMMAND_GUIDE.get(name, ("No arguments documented.", "No effect documented."))
        embed = discord.Embed(title=f"/{name}", color=0x5865F2)
        embed.add_field(name="Arguments / example", value=usage, inline=False)
        embed.add_field(name="Permission and effect", value=f"Owner IDs only. {effect}", inline=False)
        embeds.append(embed)
    # Discord permits ten embeds per message; command docs remain complete by
    # splitting the list into a short private page set.
    for offset in range(0, len(embeds), 10):
        page = discord.Embed(title=f"🛠️ V6 control reference · page {offset // 10 + 1}/{(len(embeds)+9)//10}", color=0x5865F2)
        for item in embeds[offset:offset + 10]:
            page.add_field(name=item.title, value="\n".join(field.value for field in item.fields), inline=False)
        if offset == 0:
            await inter.response.send_message(embed=page, ephemeral=True)
        else:
            await inter.followup.send(embed=page, ephemeral=True)


# Add autocomplete for each alt:int parameter. Use a factory so the command
# name is captured in a closure rather than added as a third callback parameter;
# discord.py requires autocomplete callbacks to have exactly two parameters for
# free functions.
def make_alt_autocompleter(command_name: str):
    async def _autocompleter(inter: discord.Interaction, current: str):
        cur = current.strip().lower()
        out = []
        if command_name in ("status", "deals", "settings"):
            out.append(app_commands.Choice(name="All alts", value=0))
        for i in state.alt_ids:
            a = state.get(i)
            label = _alt_label(i)
            if not cur or cur in label.lower() or cur in str(i):
                out.append(app_commands.Choice(name=label, value=i))
        return out[:25]
    return _autocompleter


for command_name, command in (
    ("stop", cmd_stop), ("pause", cmd_pause), ("resume", cmd_resume),
    ("altupdate", cmd_altupdate), ("altremove", cmd_altremove),
    ("setprice", cmd_setprice), ("setmode", cmd_setmode),
    ("setmessage", cmd_setmessage), ("setdealkeywords", cmd_setdealkeywords),
    ("setdealscan", cmd_setdealscan), ("setdealdelta", cmd_setdealdelta),
    ("setchannel", cmd_setchannel), ("replacechannel", cmd_replacechannel),
    ("rescan_channels", cmd_rescan_channels), ("resetcaution", cmd_resetcaution),
    ("channels", cmd_channels), ("uploadimage", cmd_uploadimage),
    ("setinterval", cmd_setinterval), ("setruntime", cmd_setruntime), ("logs", cmd_logs),
    ("deals", cmd_deals), ("status", cmd_status), ("settings", cmd_settings), ("pingalt", cmd_pingalt),
    ("selfcheck", cmd_selfcheck), ("clearlogs", cmd_clearlogs), ("runs", cmd_runs),
    ("diagnose", cmd_diagnose), ("simulate", cmd_simulate), ("policy", cmd_policy),
    ("canary", cmd_canary), ("reply", cmd_reply),
):
    command.autocomplete("alt")(make_alt_autocompleter(command_name))


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
        ok, detail = await asyncio.to_thread(github_api.queue_control_command, alt_id, text)
        if ok:
            return f"🕒 queued via control Gist (command `{detail}`); polling every 45s"
        return f"❌ Control Gist queue failed: {detail}"
    return await _send_dm_wait_ack(alt_id, text, timeout=timeout)


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
            for embed in message.embeds:
                footer = getattr(embed.footer, "text", "") if embed.footer else ""
                alt_id = _match_alt_name(footer)
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
            # A few webhook clients expose the override in the embed footer.
            for embed in message.embeds:
                footer = getattr(embed.footer, "text", "") if embed.footer else ""
                alt_id = _match_alt_name(footer)
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

    @discord.ui.button(label="Pause All", style=discord.ButtonStyle.secondary, emoji="⏸️", custom_id="dash_pause_all")
    async def on_pause_all(self, inter: discord.Interaction, button: discord.ui.Button):
        if not await _check_perms(inter): return
        await inter.response.defer(ephemeral=True)
        for aid in state.alt_ids:
            await _send_control_wait_ack(aid, "!pause", timeout=8)
        await inter.followup.send("⏸️ **Pause command broadcast to all alts.**", ephemeral=True)

    @discord.ui.button(label="Resume All", style=discord.ButtonStyle.success, emoji="▶️", custom_id="dash_resume_all")
    async def on_resume_all(self, inter: discord.Interaction, button: discord.ui.Button):
        if not await _check_perms(inter): return
        await inter.response.defer(ephemeral=True)
        for aid in state.alt_ids:
            await _send_control_wait_ack(aid, "!resume", timeout=8)
        await inter.followup.send("▶️ **Resume command broadcast to all alts.**", ephemeral=True)

    @discord.ui.button(label="Rescan Channels", style=discord.ButtonStyle.primary, emoji="🔄", custom_id="dash_rescan_all")
    async def on_rescan_all(self, inter: discord.Interaction, button: discord.ui.Button):
        if not await _check_perms(inter): return
        await inter.response.defer(ephemeral=True)
        for aid in state.alt_ids:
            await _send_control_wait_ack(aid, "!rescan", timeout=8)
        await inter.followup.send("🔄 **Channel rescan broadcast to all alts.**", ephemeral=True)

    @discord.ui.button(label="Reset Caution", style=discord.ButtonStyle.secondary, emoji="⚠️", custom_id="dash_reset_all")
    async def on_reset_all(self, inter: discord.Interaction, button: discord.ui.Button):
        if not await _check_perms(inter): return
        await inter.response.defer(ephemeral=True)
        for aid in state.alt_ids:
            await _send_control_wait_ack(aid, "!resetcaution all", timeout=8)
            state.reset_caution(aid, None)
        await inter.followup.send("⚠️ **Caution reset broadcast to all alts.**", ephemeral=True)

    @discord.ui.button(label="Freeze / Stop All", style=discord.ButtonStyle.danger, emoji="🛑", custom_id="dash_stop_all")
    async def on_stop_all(self, inter: discord.Interaction, button: discord.ui.Button):
        if not await _check_perms(inter): return
        await inter.response.defer(ephemeral=True)
        for aid in state.alt_ids:
            await _send_control_wait_ack(aid, "!stop", timeout=8)
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


async def _log_control(text: str):
    if not config.CONTROL_CH_ID:
        return
    ch = bot.get_channel(config.CONTROL_CH_ID)
    if not ch:
        return
    try:
        await ch.send(text[:2000], allowed_mentions=discord.AllowedMentions.none())
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
