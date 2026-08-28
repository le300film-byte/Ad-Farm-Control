"""control_bot/bot.py — official Discord control bot.

Responsibilities:
  * V6 slash commands for run/stop/pause/resume, validated message/rate/mode
    changes, live channel updates, interval/runtime updates, sync, status,
    typed logs, deals, refresh, dashboard, and a complete private help guide.
  * Permission-gated by comma-separated OWNER_IDS (fail closed) plus cooldown.
  * GitHub Actions dispatch/cancel via the shared GitHub CLI token.
  * DMs commands to alts and reports the exact remote acknowledgement privately.
  * Parses dashboard heartbeats, typed action logs, and a separate deal webhook.
  * Periodically refreshes live GitHub/heartbeat data into a stable three-embed dashboard.
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import config, github_api
from .alt_state import AltStateManager
from .dashboard import build_all, build_single_alt_embed


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


def _alt_label(alt_id: int) -> str:
    a = state.get(alt_id)
    if not a:
        return f"Alt {alt_id}"
    return f"{a.name} ({_ad_icon(a.ad_type)} {a.ad_type or 'unknown'})"


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
        channel = bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except Exception as exc:
                print(f"[STATE] Could not fetch channel {channel_id}: {type(exc).__name__}: {exc}")
                continue
        try:
            messages = [message async for message in channel.history(limit=100)]
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


async def _finish_dm_control(inter: discord.Interaction, alt_id: int,
                             command: str, label: str, *, update=None) -> None:
    """Send a DM control command and report the exact remote acknowledgement privately."""
    alt = state.get(alt_id)
    if not alt:
        await inter.response.send_message("❓ Unknown alt.", ephemeral=True)
        return
    await inter.response.defer(ephemeral=True)
    ack = await _send_dm_wait_ack(alt_id, command, timeout=20)
    failed = ack.startswith(("❌", "⏰"))
    if not failed and update:
        update()
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
    dm_ack = await _send_dm_wait_ack(alt, "!stop", timeout=15)
    ok, msg = await asyncio.to_thread(github_api.cancel_run, alt)
    state.set_workflow(alt, run_id=None, status="cancelled" if ok else a.workflow_status,
                       conclusion="cancelled" if ok else "")
    text = f"🛑 **{a.name}** stop requested. DM acknowledgement: `{dm_ack}`\nGitHub: {msg}"
    await inter.followup.send(text, ephemeral=True)
    await _log_control(text)
    state.append_log(alt, text, emoji="🛑", color=0xED4245, kind="CONTROL")


@bot.tree.command(name="pause", description="Pause one alt's public posting through its DM control channel.")
@app_commands.describe(alt="Configured alt to pause")
async def cmd_pause(inter: discord.Interaction, alt: int):
    if not await _check_perms(inter):
        return
    await _finish_dm_control(inter, alt, "!pause", "pause requested")


@bot.tree.command(name="resume", description="Resume one paused alt through its DM control channel.")
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


@bot.tree.command(name="setchannel", description="Verify and add/update one channel ID on an alt at runtime.")
@app_commands.describe(alt="Configured alt", channel_id="Numeric Discord channel ID", name="Optional channel label")
async def cmd_setchannel(inter: discord.Interaction, alt: int, channel_id: str, name: str = ""):
    if not await _check_perms(inter):
        return
    cid = channel_id.strip()
    if not cid.isdigit():
        return await inter.response.send_message("❌ Channel ID must contain digits only.", ephemeral=True)
    label = re.sub(r"[\r\n]", " ", name.strip())[:80]
    await _finish_dm_control(
        inter, alt, f"!setchannel {cid}{(' ' + label) if label else ''}", f"channel ID validated: `{cid}`",
        update=lambda: state.set_channel(alt, cid, label),
    )


@bot.tree.command(name="replacechannel", description="Replace one channel ID with another after remote verification.")
@app_commands.describe(alt="Configured alt", old_id="Old numeric channel ID", new_id="New numeric channel ID", name="Optional label")
async def cmd_replacechannel(inter: discord.Interaction, alt: int, old_id: str, new_id: str, name: str = ""):
    if not await _check_perms(inter):
        return
    if not old_id.isdigit() or not new_id.isdigit():
        return await inter.response.send_message("❌ Both channel IDs must contain digits only.", ephemeral=True)
    label = re.sub(r"[\r\n]", " ", name.strip())[:80]
    await _finish_dm_control(
        inter, alt, f"!replacechannel {old_id} {new_id}{(' ' + label) if label else ''}",
        f"channel replacement validated: `{old_id}` → `{new_id}`",
        update=lambda: state.replace_channel(alt, old_id, new_id, label),
    )


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
        ack = await _send_dm_wait_ack(alt_id, "!sync", timeout=12)
        results.append(f"**{alt.name}**: `{ack}`")
        state.append_log(alt_id, f"sync: {ack}", emoji="🔄", color=0x5865F2, kind="CONTROL")
    text = "🔄 **Sync sent to all configured alts**\n" + "\n".join(results)
    await inter.followup.send(text, ephemeral=True)
    await _log_control(text)


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


@bot.tree.command(name="logs", description="Show typed operational logs for an alt.")
@app_commands.describe(alt="Configured alt", limit="Number of lines (5-50)", kind="ALL, ERROR, DEAL, CONTROL, CHANNEL, CAUTION, or DEBUG")
@app_commands.choices(kind=[app_commands.Choice(name=k, value=k) for k in ("ALL", "ERROR", "DEAL", "CONTROL", "CHANNEL", "CAUTION", "DEBUG")])
async def cmd_logs(inter: discord.Interaction, alt: int, limit: int = 10, kind: str = "ALL"):
    if not await _check_perms(inter):
        return
    a = state.get(alt)
    if not a:
        return await inter.response.send_message("❓ Unknown alt.", ephemeral=True)
    entries = state.recent_logs(alt, max(5, min(50, limit)), kind)
    if not entries:
        return await inter.response.send_message(f"No `{kind}` logs buffered for {a.name}.", ephemeral=True)
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
        lines.append(f"**{item.name}** — alerts: `{item.deal_alerts}` · last: `{last}`")
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


_COMMAND_GUIDE = {
    "run": ("No slash arguments; private form.", "Choose one configured alt, sell/buy, price/style, image yes/no, interval 3/5, and runtime 6/12/18/24/48h. Dispatches its GitHub workflow."),
    "stop": ("`/stop alt:<alt>`", "Privately sends !stop and cancels the matching GitHub run."),
    "pause": ("`/pause alt:<alt>`", "Privately sends !pause; public posting should stop after the remote ack."),
    "resume": ("`/resume alt:<alt>`", "Privately sends !resume and reports the remote ack."),
    "setprice": ("`/setprice alt:<alt> new_price:<0..20>`", "Validates the value, sends !setprice, and updates live dashboard state only after an ack."),
    "setmode": ("`/setmode alt:<alt> mode:<sell|buy>`", "Sends !setmode and updates live mode after an ack."),
    "setmessage": ("`/setmessage alt:<alt> new_message:<text>`", "Rejects empty/text over 1900 chars, sends !setmessage, and updates the preview after an ack."),
    "setchannel": ("`/setchannel alt:<alt> channel_id:<digits> [name]`", "Sends !setchannel; the sender verifies the channel before adding it to its runtime scheduler."),
    "replacechannel": ("`/replacechannel alt:<alt> old_id:<digits> new_id:<digits> [name]`", "Sends !replacechannel; the sender verifies the replacement and safely rewrites scheduler state."),
    "setinterval": ("`/setinterval alt:<alt> interval:<3|5>`", "Sends !setinterval. The sender keeps the permitted 3/5-minute constraint."),
    "setruntime": ("`/setruntime alt:<alt> hours:<6|12|18|24|48>`", "Sends !setruntime. The sender caps all runtime to 48 hours."),
    "sync": ("`/sync`", "Sends !sync to every configured alt to reload shared control/Gist state."),
    "status": ("`/status [alt:<alt>|All alts]`", "Refreshes GitHub state and shows current heartbeat, workflow, counters, channels, errors, and alerts."),
    "logs": ("`/logs alt:<alt> [limit] [kind]`", "Shows typed buffered logs. Filter kinds include ERROR, DEAL, CONTROL, CHANNEL, CAUTION, and DEBUG."),
    "deals": ("`/deals [alt:<alt>|All alts]`", "Shows deal-alert counters from the separate deal webhook/state path."),
    "refresh": ("`/refresh`", "Fetches current GitHub run state and updates the persistent dashboard message."),
    "dashboard": ("`/dashboard`", "Posts a fresh dashboard snapshot; it does not run the sender or scan deals."),
    "help": ("`/help`", "Shows this private complete command reference."),
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
        if command_name == "status":
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
    ("setprice", cmd_setprice), ("setmode", cmd_setmode),
    ("setmessage", cmd_setmessage), ("setchannel", cmd_setchannel), ("replacechannel", cmd_replacechannel),
    ("setinterval", cmd_setinterval), ("setruntime", cmd_setruntime), ("logs", cmd_logs),
    ("deals", cmd_deals), ("status", cmd_status),
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


async def _handle_guild_webhook_message(message: discord.Message):
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
    message_id = getattr(message, "id", None)
    if message_id is not None:
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
    ch_id = message.channel.id
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
        if alt_id is not None:
            _parse_log_message(alt_id, message)
        else:
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
    """Extract live V6 heartbeat from structured dashboard webhook content."""
    # Try JSON in content first — V6 wraps it in a JSON code fence
    if message.content:
        raw = message.content.strip()
        # Strip code fence if present
        if raw.startswith("```"):
            # remove opening ```...\n line and closing ```
            lines = raw.split("\n")
            # skip first line (```json)
            inner = "\n".join(lines[1:])
            if inner.endswith("```"):
                inner = inner[:-3]
            raw = inner.strip()
        if raw.startswith("{"):
            try:
                payload = json.loads(raw)
                if isinstance(payload, dict) and (payload.get("heartbeat") or payload.get("type") == "heartbeat"):
                    alt_id = payload.get("alt_id") or _match_alt_name(payload.get("alt_name"))
                    if alt_id:
                        state.update_from_heartbeat(alt_id, payload)
            except Exception as e:
                dbg = False  # ignore parse errors
    for embed in message.embeds:
        # Our new heartbeat embed will have "HEARTBEAT: Alt N" in title/footer
        # Otherwise, parse per-field alt data from summary embeds.
        if embed.title and embed.title.lower().startswith("💓 heartbeat"):
            try:
                # Footer has alt id encoded
                alt_id = None
                if embed.footer and embed.footer.text:
                    m = re.search(r"alt[_\s-]?(\d+)", embed.footer.text, re.I)
                    if m:
                        alt_id = int(m.group(1))
                if alt_id is None:
                    for f in embed.fields or []:
                        if (f.name or "").lower() == "alt_id":
                            alt_id = int(f.value)
                # Build payload from embed fields
                payload = {}
                for f in embed.fields or []:
                    payload[f.name] = f.value
                # Also parse description for key=value pairs
                if embed.description:
                    for line in embed.description.split("\n"):
                        if ":" in line:
                            k, _, v = line.partition(":")
                            payload[k.strip().lower()] = v.strip()
                if alt_id:
                    state.update_from_heartbeat(alt_id, payload)
            except Exception:
                pass
        elif embed.title and embed.title.startswith("✅ **SEND**"):
            pass  # log webhook per-send message, not a heartbeat


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
    # Try to detect success counts from "total=`N`"
    m = re.search(r"total[`=]\s*(\d+)", body)
    if m:
        a = state.get(alt_id)
        if a:
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
    try:
        if _dash_message is None:
            # Try to load saved message id
            try:
                mid = int(Path(config.DASHBOARD_MSG_ID_FILE).read_text().strip())
                _dash_message = await ch.fetch_message(mid)
            except Exception:
                _dash_message = None
        if _dash_message is None:
            _dash_message = await ch.send(embeds=embeds[:10])
            try:
                Path(config.DASHBOARD_MSG_ID_FILE).write_text(str(_dash_message.id))
            except Exception:
                pass
            try:
                await _dash_message.pin()
            except Exception:
                pass
        else:
            await _dash_message.edit(embeds=embeds[:10])
    except Exception as e:
        print(f"[DASH] refresh failed: {type(e).__name__}: {e}")
        _dash_message = None


async def _post_dashboard(embeds):
    ch = bot.get_channel(config.DASHBOARD_CH_ID) if config.DASHBOARD_CH_ID else None
    if not ch:
        return None
    try:
        return await ch.send(embeds=embeds[:10])
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
    except Exception:
        pass


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