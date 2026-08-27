"""control_bot/bot.py — official Discord control bot.

Responsibilities:
  * Slash commands (/run, /pause, /resume, /stop, /status, /setprice, /setmode,
    /setmessage, /sync, /logs) with dropdowns/text/number inputs.
  * Permission-gated (OWNER_IDS) + 5s per-user cooldown.
  * GitHub Actions dispatch/cancel via the shared GitHub CLI token.
  * DMs commands to alts (send_message to the alt's user id), parses ack
    replies from alts, forwards them to #control.
  * Listens in #dashboard and the shared #farm-logs channel for webhook
    messages sent by the alts, parses embeds/content, and keeps state updated.
  * Periodically pushes a refreshed 3-embed unified dashboard.
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


class RunModal(discord.ui.Modal, title="Start an alt run · 1/2"):
    """First page of the run form.

    Discord limits a modal to five rows. The form therefore uses two
    consecutive modals rather than falling back to a long list of slash
    command arguments.
    """

    alt_id = discord.ui.TextInput(
        label="Alt ID (1–4)", custom_id="alt_id", placeholder="1",
        required=True, max_length=2,
    )
    ad_type = discord.ui.TextInput(
        label="Ad type (sell or buy)", custom_id="ad_type", placeholder="sell",
        default="sell", required=True, max_length=4,
    )
    sell_rate = discord.ui.TextInput(
        label="Sell/token rate (e.g. 2.5$)", custom_id="sell_rate",
        placeholder="2.5$", default="2.5$", required=True, max_length=20,
    )
    sell_extra = discord.ui.TextInput(
        label="Sell extra text", custom_id="sell_extra",
        placeholder="DM ME QUICK CAN DO SMALL AND BIG AMOUNTS",
        default="DM ME QUICK CAN DO SMALL AND BIG AMOUNTS",
        required=False, max_length=500,
    )
    buy_rate_rap = discord.ui.TextInput(
        label="Buy RAP rate (e.g. 1.8)", custom_id="buy_rate_rap",
        placeholder="1.8", default="1.8", required=False, max_length=20,
    )

    async def on_submit(self, inter: discord.Interaction) -> None:
        values = {
            "alt_id": (self.alt_id.value or "").strip(),
            "ad_type": (self.ad_type.value or "").strip().lower(),
            "sell_rate": (self.sell_rate.value or "").strip(),
            "sell_extra": (self.sell_extra.value or "").strip(),
            "buy_rate_rap": (self.buy_rate_rap.value or "").strip(),
        }
        errors = _validate_run_page_one(values)
        if errors:
            await inter.response.send_message("❌ " + " ".join(errors), ephemeral=True)
            return
        await inter.response.send_modal(RunOptionsModal(values))


class RunOptionsModal(discord.ui.Modal, title="Start an alt run · 2/2"):
    """Second page of the run form; submits the workflow after validation."""

    def __init__(self, page_one: dict[str, str]):
        super().__init__()
        self.page_one = page_one

    buy_style = discord.ui.TextInput(
        label="Buy style (detailed or simple)", custom_id="buy_style",
        placeholder="detailed", default="detailed", required=True, max_length=8,
    )
    buy_simple_text = discord.ui.TextInput(
        label="Buy simple text (full message)", custom_id="buy_simple_text",
        placeholder="BUYING ALL BLADE BALL DM ME QUICK",
        default="BUYING ALL BLADE BALL DM ME QUICK",
        required=False, max_length=1900,
    )
    interval_min = discord.ui.TextInput(
        label="Interval minutes (3 or 5)", custom_id="interval_min",
        placeholder="5", default="5", required=True, max_length=2,
    )
    total_hours = discord.ui.TextInput(
        label="Total hours (6, 12, 18, 24, or 48)", custom_id="total_hours",
        placeholder="6", default="6", required=True, max_length=2,
    )
    attach_image = discord.ui.TextInput(
        label="Attach image? (yes or no)", custom_id="attach_image",
        placeholder="yes", default="yes", required=True, max_length=3,
    )

    async def on_submit(self, inter: discord.Interaction) -> None:
        values = {
            **self.page_one,
            "buy_style": (self.buy_style.value or "").strip().lower(),
            "buy_simple_text": (self.buy_simple_text.value or "").strip(),
            "interval_min": (self.interval_min.value or "").strip(),
            "total_hours": (self.total_hours.value or "").strip(),
            "attach_image": (self.attach_image.value or "").strip().lower(),
        }
        errors, parsed = _validate_run_values(values)
        if errors:
            await inter.response.send_message("❌ " + " ".join(errors), ephemeral=True)
            return
        await _dispatch_run_from_modal(inter, values, parsed)


def _validate_run_page_one(values: dict[str, str]) -> list[str]:
    errors: list[str] = []
    raw_alt_id = values.get("alt_id", "").strip()
    if raw_alt_id not in {"1", "2", "3", "4"}:
        errors.append("alt_id must be exactly 1, 2, 3, or 4.")
    else:
        alt_id = int(raw_alt_id)
        if alt_id not in config.ALT_REPOS:
            errors.append(f"Alt {alt_id} is not configured in ALT_REPOS.")
    if values["ad_type"] not in ("sell", "buy"):
        errors.append("ad_type must be sell or buy.")
    rate = _extract_price(values["sell_rate"])
    if rate is None or not 0 < rate <= 20:
        errors.append("sell_rate must contain a number between 0 and 20, for example 2.5$.")
    rap = _extract_price(values["buy_rate_rap"])
    if values["ad_type"] == "buy" and (rap is None or not 0 < rap <= 20):
        errors.append("buy_rate_rap must contain a number between 0 and 20, for example 1.8.")
    if len(values["sell_extra"]) > 500:
        errors.append("sell_extra is limited to 500 characters.")
    return errors


def _validate_run_values(values: dict[str, str]) -> tuple[list[str], dict[str, object]]:
    errors = _validate_run_page_one(values)
    style = values["buy_style"]
    if style not in ("detailed", "simple"):
        errors.append("buy_style must be detailed or simple.")
    if style == "simple" and values["ad_type"] == "buy" and not values["buy_simple_text"]:
        errors.append("buy_simple_text is required for a simple buy run.")
    if len(values["buy_simple_text"]) > 1900:
        errors.append("buy_simple_text is limited to 1900 characters.")
    try:
        interval = int(values["interval_min"])
        if interval not in (3, 5):
            errors.append("interval_min must be 3 or 5.")
    except ValueError:
        interval = 0
        errors.append("interval_min must be 3 or 5.")
    try:
        hours = int(values["total_hours"])
        if hours not in (6, 12, 18, 24, 48):
            errors.append("total_hours must be 6, 12, 18, 24, or 48.")
    except ValueError:
        hours = 0
        errors.append("total_hours must be 6, 12, 18, 24, or 48.")
    if values["attach_image"] not in ("yes", "no"):
        errors.append("attach_image must be yes or no.")
    try:
        parsed_alt_id = int(values["alt_id"])
    except (TypeError, ValueError):
        parsed_alt_id = 0
    parsed = {
        "alt_id": parsed_alt_id,
        "rate": _extract_price(values["sell_rate"]),
        "rap": _extract_price(values["buy_rate_rap"]),
        "interval": interval,
        "hours": hours,
    }
    return errors, parsed


async def _dispatch_run_from_modal(
    inter: discord.Interaction, values: dict[str, str], parsed: dict[str, object]
) -> None:
    """Cancel the previous run, dispatch the new workflow, and acknowledge it."""
    # The /run command already consumed the owner's cooldown before opening
    # the first modal. Do not consume it a second time on modal submission.
    if not _is_owner(inter):
        await inter.response.send_message(
            "🔒 You aren't authorized to run control commands.", ephemeral=True
        )
        return
    # Defer ephemerally so every error path below remains private.
    await inter.response.defer(ephemeral=True)
    alt_id = int(parsed["alt_id"])
    a = state.get(alt_id)
    if not a:
        await inter.followup.send(f"❓ Unknown alt {alt_id}.", ephemeral=True)
        return

    ad_type = values["ad_type"]
    rate = float(parsed["rate"])
    rap = float(parsed["rap"] or 0)
    inputs = {
        "ad_type": ad_type,
        "interval_min": str(parsed["interval"]),
        "total_hours": str(parsed["hours"]),
        "attach_image": values["attach_image"],
        "channel_1": "",
        "channel_2": "",
        "channel_1_name": "",
        "channel_2_name": "",
    }
    if ad_type == "sell":
        inputs.update({
            "sell_rate": values["sell_rate"],
            "sell_extra": values["sell_extra"],
        })
    else:
        inputs.update({
            # The modal's sell_rate field is the token rate in buy mode. The
            # workflow already calls this input buy_rate.
            "buy_style": values["buy_style"],
            "buy_rate": f"{rate:g}",
            "buy_rate_rap": f"{rap:g}",
            "buy_simple_text": values["buy_simple_text"],
        })

    try:
        await asyncio.to_thread(github_api.cancel_run, alt_id)
        await asyncio.sleep(1.5)
        ok, msg = await asyncio.to_thread(github_api.dispatch_workflow, alt_id, inputs)
    except Exception as exc:
        await inter.followup.send(
            f"❌ Failed to start {a.name}: {type(exc).__name__}: {exc}",
            ephemeral=True,
        )
        return
    if not ok:
        await inter.followup.send(f"❌ Failed to start {a.name}: {msg}", ephemeral=True)
        return

    state.set_workflow(alt_id, run_id=None, status="queued", conclusion="")
    preview = values["sell_extra"] if ad_type == "sell" else (
        values["buy_simple_text"] or f"BUYING BLADE BALL TOKENS {rate:g}/1K RAP {rap:g}/1K"
    )
    state.set_run_config(alt_id, ad_type=ad_type, rate=rate, message=preview)
    rate_str = values["sell_rate"] if ad_type == "sell" else f"tok {rate:g} / rap {rap:g}"
    log_line = (
        f"🚀 **{a.name}** STARTED — {ad_type} {rate_str} · "
        f"{values['interval_min']}min × {values['total_hours']}h · img={values['attach_image']}\n{msg}"
    )
    await inter.followup.send(log_line, ephemeral=True)
    await _log_control(log_line)
    state.append_log(
        alt_id,
        f"Started: {ad_type} {rate_str} {values['interval_min']}m×{values['total_hours']}h img={values['attach_image']}",
        emoji="🚀", color=0x57F287,
    )


@bot.tree.command(name="run", description="Start an alt run using a pop-up form.")
async def cmd_run(inter: discord.Interaction):
    if not await _check_perms(inter):
        return
    await inter.response.send_modal(RunModal())


# Alt autocomplete: alt parameter on commands that take an int
async def alt_autocomplete(inter: discord.Interaction, current: str):
    cur = current.lower()
    out = []
    for i in state.alt_ids:
        a = state.get(i)
        label = _alt_label(i)
        if not cur or cur in label.lower():
            out.append(app_commands.Choice(name=label, value=i))
    return out[:25]


# Decorator to add alt autocomplete to the right params
def alt_param(fn):
    # We apply choices via autocomplete at registration time — see cmd registration below.
    return fn


@bot.tree.command(name="stop", description="Stop (cancel) an alt's current GitHub run.")
@app_commands.describe(alt="Which alt")
async def cmd_stop(inter: discord.Interaction, alt: int):
    if not await _check_perms(inter):
        return
    a = state.get(alt)
    if not a:
        return await inter.response.send_message("❓ Unknown alt.", ephemeral=True)
    # Defer before any network work so a slow Discord DM cannot exceed the
    # interaction's three-second initial-response window.
    await inter.response.defer(ephemeral=False)
    # Try sending !stop DM too (graceful shutdown via v5.3 panic)
    await _send_dm(alt, "!stop")
    await asyncio.sleep(1.0)
    ok, msg = await asyncio.to_thread(github_api.cancel_run, alt)
    state.set_workflow(alt, run_id=None, status="cancelled" if ok else state.get(alt).workflow_status,
                      conclusion="cancelled" if ok else "")
    text = f"🛑 **{a.name}** stop requested. {msg}"
    await inter.followup.send(text)
    await _log_control(text)
    state.append_log(alt, "Stop requested via /stop", emoji="🛑", color=0xED4245)


@bot.tree.command(name="pause", description="Ask an alt to pause public posting (sends !pause DM).")
@app_commands.describe(alt="Which alt")
async def cmd_pause(inter: discord.Interaction, alt: int):
    if not await _check_perms(inter):
        return
    a = state.get(alt)
    if not a:
        return await inter.response.send_message("❓ Unknown alt.", ephemeral=True)
    await inter.response.defer(ephemeral=False)
    ack = await _send_dm_wait_ack(alt, "!pause", timeout=15)
    text = f"⏸️ **{a.name}** pause sent. Reply: `{ack}`"
    await inter.followup.send(text)
    await _log_control(text)
    state.append_log(alt, "Paused via DM", emoji="⏸️", color=0xFEE75C)


@bot.tree.command(name="resume", description="Resume a paused alt (sends !resume DM).")
@app_commands.describe(alt="Which alt")
async def cmd_resume(inter: discord.Interaction, alt: int):
    if not await _check_perms(inter):
        return
    a = state.get(alt)
    if not a:
        return await inter.response.send_message("❓ Unknown alt.", ephemeral=True)
    await inter.response.defer(ephemeral=False)
    ack = await _send_dm_wait_ack(alt, "!resume", timeout=15)
    text = f"▶️ **{a.name}** resume sent. Reply: `{ack}`"
    await inter.followup.send(text)
    await _log_control(text)
    state.append_log(alt, "Resumed via DM", emoji="▶️", color=0x57F287)


@bot.tree.command(name="setprice", description="Change an alt's rate mid-run.")
@app_commands.describe(alt="Which alt", new_price="e.g. 2.3 or 2.3$")
async def cmd_setprice(inter: discord.Interaction, alt: int, new_price: str):
    if not await _check_perms(inter):
        return
    a = state.get(alt)
    if not a:
        return await inter.response.send_message("❓ Unknown alt.", ephemeral=True)
    price_val = _extract_price(new_price)
    if price_val is None or not 0 < price_val <= 20:
        return await inter.response.send_message(
            "❌ new_price must contain a number between 0 and 20, for example `2.3$`.",
            ephemeral=True,
        )
    await inter.response.defer(ephemeral=False)
    ack = await _send_dm_wait_ack(alt, f"!setprice {new_price}", timeout=20)
    text = f"💰 **{a.name}** setprice → `{new_price}`. Reply: `{ack}`"
    await inter.followup.send(text)
    await _log_control(text)
    if price_val is not None:
        state.set_run_config(alt, rate=price_val)
    state.append_log(alt, f"Price changed to {new_price}", emoji="💰", color=0x5865F2)


@bot.tree.command(name="setmode", description="Switch an alt between sell/buy mid-run.")
@app_commands.describe(alt="Which alt", mode="sell or buy")
@app_commands.choices(mode=[app_commands.Choice(name="sell", value="sell"),
                           app_commands.Choice(name="buy", value="buy")])
async def cmd_setmode(inter: discord.Interaction, alt: int, mode: str):
    if not await _check_perms(inter):
        return
    a = state.get(alt)
    if not a:
        return await inter.response.send_message("❓ Unknown alt.", ephemeral=True)
    await inter.response.defer(ephemeral=False)
    ack = await _send_dm_wait_ack(alt, f"!setmode {mode}", timeout=20)
    text = f"🔄 **{a.name}** setmode → `{mode}`. Reply: `{ack}`"
    await inter.followup.send(text)
    await _log_control(text)
    state.set_run_config(alt, ad_type=mode)
    state.append_log(alt, f"Mode set to {mode}", emoji="🔄", color=0x5865F2)


@bot.tree.command(name="setmessage", description="Change an alt's ad text mid-run.")
@app_commands.describe(alt="Which alt", new_message="New full ad message")
async def cmd_setmessage(inter: discord.Interaction, alt: int, new_message: str):
    if not await _check_perms(inter):
        return
    a = state.get(alt)
    if not a:
        return await inter.response.send_message("❓ Unknown alt.", ephemeral=True)
    if len(new_message) > 1900:
        return await inter.response.send_message("❌ Message too long (max 1900 chars).", ephemeral=True)
    await inter.response.defer(ephemeral=False)
    ack = await _send_dm_wait_ack(alt, f"!setmessage {new_message}", timeout=20)
    text = f"📝 **{a.name}** setmessage. Reply: `{ack}`"
    await inter.followup.send(text)
    await _log_control(text)
    state.set_run_config(alt, message=new_message)
    state.append_log(alt, "Message updated via DM", emoji="📝", color=0x5865F2)


@bot.tree.command(name="sync", description="Force all alts to reload their Gist blocklist + config.")
async def cmd_sync(inter: discord.Interaction):
    if not await _check_perms(inter):
        return
    await inter.response.defer(ephemeral=False)
    results = []
    for i in state.alt_ids:
        a = state.get(i)
        ack = await _send_dm_wait_ack(i, "!sync", timeout=12)
        results.append(f"**{a.name}**: `{ack}`")
    text = "🔄 **Sync sent to all alts**\n" + "\n".join(results)
    await inter.followup.send(text)
    await _log_control(text)


@bot.tree.command(name="status", description="Show detailed status for all alts or a specific one.")
@app_commands.describe(alt="Which alt (or All)")
async def cmd_status(inter: discord.Interaction, alt: int = 0):
    if not await _check_perms(inter):
        return
    if alt == 0:
        embeds = build_all(state)
        await inter.response.send_message(embeds=embeds[:10], ephemeral=False)
    else:
        em = build_single_alt_embed(state, alt)
        await inter.response.send_message(embed=em, ephemeral=False)


@bot.tree.command(name="logs", description="Show recent log lines for an alt.")
@app_commands.describe(alt="Which alt", limit="Number of lines (5-50)")
async def cmd_logs(inter: discord.Interaction, alt: int, limit: int = 10):
    if not await _check_perms(inter):
        return
    a = state.get(alt)
    if not a:
        return await inter.response.send_message("❓ Unknown alt.", ephemeral=True)
    limit = max(5, min(50, limit))
    entries = state.recent_logs(alt, limit)
    if not entries:
        return await inter.response.send_message(f"No buffered logs for {a.name} yet (logs populate when webhooks fire).", ephemeral=True)
    lines = []
    for ts, emo, _col, txt in entries:
        t = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
        lines.append(f"`[{t}]` {emo} {txt}")
    body = "\n".join(lines)
    if len(body) > 1900:
        body = body[-1900:]
    em = discord.Embed(title=f"📜 {a.name} — last {len(entries)} log lines",
                       color=0x2F3136, description=body[:4000])
    await inter.response.send_message(embed=em, ephemeral=False)


@bot.tree.command(name="dashboard", description="(Re)post the unified dashboard in #dashboard.")
async def cmd_dashboard(inter: discord.Interaction):
    if not await _check_perms(inter):
        return
    await inter.response.defer(ephemeral=False)
    embeds = build_all(state)
    msg = await _post_dashboard(embeds)
    await inter.followup.send(f"✅ Dashboard refreshed → {msg.jump_url if msg else '(failed)'}")


@bot.tree.command(name="help", description="List the available farm-control commands.")
async def cmd_help(inter: discord.Interaction):
    """Show the command reference privately to an authorized operator."""
    if not await _check_perms(inter):
        return
    lines = []
    for command in sorted(bot.tree.get_commands(), key=lambda item: item.name):
        lines.append(f"**/{command.name}** — {command.description}")
    embed = discord.Embed(
        title="🛠️ Farm control commands",
        description="\n".join(lines),
        color=0x5865F2,
    )
    await inter.response.send_message(embed=embed, ephemeral=True)


# Register autocomplete for the "alt" int parameter on every command that takes it
for cmd in (cmd_stop, cmd_pause, cmd_resume, cmd_setprice, cmd_setmode, cmd_setmessage, cmd_logs, cmd_status):
    # We do this by wrapping autocomplete at command-tree level — easier:
    # use a choice list with Alt 1..4 at minimum so the dropdown works even
    # without autocomplete, and add autocomplete dynamically.
    pass


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
    ("setmessage", cmd_setmessage), ("logs", cmd_logs), ("status", cmd_status),
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
    if message.author.id == bot.user.id:
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
    ch_id = message.channel.id
    if ch_id == config.DASHBOARD_CH_ID:
        _parse_dashboard_message(message)

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


def _parse_dashboard_message(message: discord.Message):
    """Extract per-alt heartbeat from structured embeds (v5.5 alts send JSON+embed)."""
    # Try JSON in content first — v5.5 wraps it in ```json ... ``` code fence
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
    state.append_log(alt_id, body[:300], emoji=emoji, color=color)
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
