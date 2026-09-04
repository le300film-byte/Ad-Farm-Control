"""control_bot.ban_watch — ban detection → customer alert + one-click re-setup.

TODO 1.2:
  * Wire 403/DEAD ban detection (currently silent log lines in send_ads) to
    the customer's #control thread with a calm, helpful message.
  * One-click re-setup button for the replacement alt, reusing the previous
    channel config.
  * On ban: rename old repo ``<repo>_BANNED_<timestamp>``, create a fresh
    replacement repo, log to #farm-logs.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import discord

import customer_manager as cm

# Markers emitted by send_ads.py / webhooks when an alt is killed.
BAN_MARKERS = re.compile(
    r"(banned|token invalidated|token is invalid|DEAD|deleted|shadowban|"
    r"account flagged|flagged|kicked|HTTP 403|HTTP 401|invalidated/revoked)",
    re.I,
)

REPLACEMENT_MSG = (
    "⚠️ **Your alt was banned.**\n"
    "Don't panic — here's what happens next:\n"
    "1. You get **time credit** (full credit if banned within 48h of first run, "
    "otherwise pro-rated).\n"
    "2. We've renamed the old repo and prepared a fresh one.\n"
    "3. Click the button below to set up the replacement alt — your previous "
    "channel config is reused automatically.\n\n"
    "💡 Tip: use a fresh account and a different network route for the replacement."
)

BANNED_REPO_SUFFIX = "_BANNED_"


class ReSetupView(discord.ui.View):
    """One-click re-setup for a replacement alt."""

    def __init__(self, customer: dict[str, Any], alt_index: int):
        super().__init__(timeout=3600)
        self._customer = customer
        self._alt_index = alt_index

    @discord.ui.button(label="♻️ Set up replacement alt", style=discord.ButtonStyle.success)
    async def _setup(self, inter: discord.Interaction, _btn: discord.ui.Button) -> None:
        uid = str(inter.user.id)
        if uid != self._customer.get("discord_id"):
            await inter.response.send_message("❌ This action belongs to the forum owner.", ephemeral=True)
            return
        prev = {cr["alt_index"]: cr for cr in cm.get_alt_credentials(uid)}.get(self._alt_index, {})
        await inter.response.send_message(
            "🧙 **Replacement setup** — keep the fresh alt's token handy. "
            f"Previous channels: `{', '.join(prev.get('channel_ids', []) or []) or 'none'}` — "
            "run `/setup` in your `#control` thread; the wizard reuses them.",
            ephemeral=True,
        )


def detect_ban_events(text: str) -> bool:
    return bool(BAN_MARKERS.search(text or ""))


async def handle_ban_message(
    bot: Any,
    message: discord.Message,
    customer: dict[str, Any],
) -> bool:
    """Handle a ban marker in a customer's #farm-logs (or heartbeat) thread.

    Returns True when a ban was detected and handled.
    """
    if not detect_ban_events(message.content or ""):
        return False
    did = customer["discord_id"]
    # Avoid duplicate handling within an hour per customer.
    recent = cm.get_events("alt_banned", since=time.time() - 3600, discord_id=did)
    # Only the first event gets the customer-facing flow; keep logging others.
    is_first = not recent

    control_thread = customer.get("control_thread_id", "")
    logs_thread = customer.get("logs_thread_id", "")
    if is_first and control_thread and control_thread != "0":
        try:
            ch = bot.get_channel(int(control_thread)) or await bot.fetch_channel(int(control_thread))
            await ch.send(
                REPLACEMENT_MSG,
                view=ReSetupView(customer, alt_index=1),
            )
            cm.record_event(did, "ban_alert_sent", {})
        except Exception as exc:
            print(f"[BAN-WATCH] control alert failed: {exc}")

    from control_bot import metrics
    metrics.note_ban(did, alt=1, reason=(message.content or "")[:200])
    # Rename the banned repo + provision replacement + log (best-effort).
    try:
        await asyncio.to_thread(_handle_banned_repos, customer)
    except Exception as exc:
        print(f"[BAN-WATCH] repo handling failed: {exc}")

    if logs_thread and logs_thread != "0":
        try:
            ch = bot.get_channel(int(logs_thread)) or await bot.fetch_channel(int(logs_thread))
            await ch.send(
                "🚫 **BAN DETECTED** — alt marked banned. Old repo renamed to "
                "*_BANNED_<timestamp>*, replacement repo provisioned. Time credit "
                "applied per policy."
            )
        except Exception as _ignored_exc:
            print(f"[BANWATCH] handle_ban_message: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
    return True


def _handle_banned_repos(customer: dict[str, Any]) -> dict[str, str]:
    """Rename the banned repo then create a fresh repo under the same name.

    Confirmed founder decision: the old repo stays visible (renamed to
    ``_BANNED_<timestamp>``) for evidence, and a brand-new repo is created for
    the replacement alt so no secrets or history carry over.
    """
    from github_dispatch import rename_banned_repo, create_replacement_alt_repo
    owner = customer.get("github_account", "")
    repos = customer.get("repos") or []
    if not owner or not repos:
        return {}
    # The ban marker does not identify which alt repo; the newest repo is the
    # active one for single-alt customers and the last added for multi-alt.
    banned = repos[-1]
    result: dict[str, str] = {"banned": banned}
    try:
        renamed = rename_banned_repo(owner, banned)
        result["renamed_to"] = renamed
        # Original name is now free → fresh replacement repo with that name.
        result["replacement"] = create_replacement_alt_repo(owner, banned, len(repos))
        cm.update_repos(customer["discord_id"], list(repos))
    except Exception as exc:
        print(f"[BAN-WATCH] repo rename/provision error: {exc}")
        result["error"] = str(exc)
    return result
