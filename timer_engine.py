"""timer_engine.py – V8 Subscription Timer, Reminders & Auto-Shutdown.

Runs as a background asyncio task inside the Discord bot process.
Checks every hour (configurable) for:
  - Customers within 7, 3, 1 day(s) of expiry → DM + forum reminder.
  - Customers whose subscription has already expired → shutdown + lock.

Phase 0 additions (TODO.md):
  * Reminder dedupe is PERSISTED in ``customers.db`` (R-08) — the bot may
    restart / chunk-handoff any number of times without re-sending DMs.
  * Run-ID lease is renewed while the bot runs (Gist startup lock).
  * ``dry_run_expiry_alerts`` powers ``/admin expiry-alerts``.
Phase 1 additions:
  * ``auto_redispatch_loop`` re-dispatches 48h-a-renew runs while the customer
    remains active (the honest "∞ = 48h auto-renew" behavior).
  * Weekly metrics summary (post-launch instrumentation).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional, TYPE_CHECKING

import discord
from discord.ext import tasks

import customer_manager as cm

if TYPE_CHECKING:
    from discord import Client

# How often to scan (seconds).  Default: 3600 (1 hour).
SCAN_INTERVAL_SEC: int = 3600

# Reminder thresholds in days (checked in descending order so a customer
# doesn't receive duplicate messages when they hover near multiple thresholds).
REMINDER_THRESHOLDS = [7, 3, 1]

# Callback registered by the bot to perform the actual Discord sends.
_bot_ref: Optional["discord.Client"] = None

# Audit-log channel ID (set by admin_commands on startup)
_audit_log_ch_id: Optional[int] = None

_RUN_ID: str = ""
_LEASE_TASK: Optional[asyncio.Task] = None


class ReminderTracker:
    """Set-like persistent reminder sent-state (backs ``_sent_reminders``).

    The public surface mirrors a ``set`` (add/discard/clear/contains/iter/len)
    so existing code and tests keep working, while every mutation is written
    through to the ``reminder_sent`` table in ``customers.db``.
    """

    def __init__(self) -> None:
        self._cache: set[tuple[str, int]] = set()
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        try:
            self._cache = set(cm.get_sent_reminders())
        except Exception:
            self._cache = set()
        self._loaded = True

    def add(self, key: tuple[str, int]) -> None:
        self._load()
        self._cache.add((str(key[0]), int(key[1])))
        try:
            cm.mark_reminder_sent(key[0], int(key[1]))
        except Exception:
            pass

    def discard(self, key: tuple[str, int]) -> None:
        self._load()
        self._cache.discard((str(key[0]), int(key[1])))
        try:
            cm.clear_reminder_sent(key[0])
        except Exception:
            pass

    def clear(self) -> None:
        self._cache.clear()
        self._loaded = True
        try:
            cm.clear_reminder_sent()
        except Exception:
            pass

    def __contains__(self, key: tuple[str, int]) -> bool:
        self._load()
        return (str(key[0]), int(key[1])) in self._cache

    def __iter__(self):
        self._load()
        return iter(self._cache)

    def __len__(self) -> int:
        self._load()
        return len(self._cache)


# Track which (discord_id, threshold) reminders have already been sent.
# Persisted in customers.db so restarts/chunk handoffs never re-send (R-08).
_sent_reminders = ReminderTracker()


def register_bot(bot: "discord.Client", audit_log_ch_id: Optional[int] = None) -> None:
    global _bot_ref, _audit_log_ch_id
    _bot_ref = bot
    _audit_log_ch_id = audit_log_ch_id


def set_run_id(run_id: str) -> None:
    global _RUN_ID
    _RUN_ID = (run_id or "").strip()


async def _send_dm(discord_id: str, message: str) -> bool:
    if _bot_ref is None:
        return False
    try:
        user = await _bot_ref.fetch_user(int(discord_id))
        await user.send(message)
        return True
    except Exception as exc:
        print(f"[TIMER] DM to {discord_id} failed: {exc}")
        return False


async def _send_forum_message(thread_id: str, message: str) -> bool:
    if _bot_ref is None or not thread_id or thread_id == "0":
        return False
    try:
        channel = _bot_ref.get_channel(int(thread_id))
        if channel is None:
            channel = await _bot_ref.fetch_channel(int(thread_id))
        await channel.send(message)
        return True
    except Exception as exc:
        print(f"[TIMER] Forum message to thread {thread_id} failed: {exc}")
        return False


async def _audit(message: str) -> None:
    if _bot_ref is None or not _audit_log_ch_id:
        print(f"[AUDIT] {message}")
        return
    try:
        ch = _bot_ref.get_channel(_audit_log_ch_id)
        if ch is None:
            ch = await _bot_ref.fetch_channel(_audit_log_ch_id)
        await ch.send(f"[TIMER] {message}"[:2000],
                      allowed_mentions=discord.AllowedMentions.none())
    except Exception as exc:
        print(f"[AUDIT] Could not post to audit log: {exc} | Message: {message}")


async def run_shutdown_for_customer(discord_id: str) -> None:
    """Perform expiry shutdown for a single customer:
    1. Cancel all their GitHub workflow runs.
    2. Set active = 0 in SQLite.
    3. Make their forum read-only.
    4. Send expiry DM.
    """
    c = cm.get_customer(discord_id)
    if c is None:
        return

    # 1. Cancel GitHub workflows
    try:
        from github_dispatch import cancel_workflow_runs
        owner = c.get("github_account", "")
        for repo in (c.get("repos") or []):
            try:
                n = await asyncio.to_thread(cancel_workflow_runs, owner, repo)
                print(f"[TIMER] Cancelled {n} run(s) for {owner}/{repo}")
            except Exception as exc:
                print(f"[TIMER] Could not cancel runs for {owner}/{repo}: {exc}")
    except ImportError:
        pass

    # 2. Deactivate in DB
    cm.deactivate_customer(discord_id)

    # 3. Make forum read-only (best-effort)
    if _bot_ref and c.get("forum_id") and c["forum_id"] != "0":
        try:
            import os as _os
            guild_id_env = _os.environ.get("GUILD_ID", "")
            if guild_id_env:
                guild = _bot_ref.get_guild(int(guild_id_env))
                if guild:
                    from discord_forum import make_forum_readonly
                    member = guild.get_member(int(discord_id))
                    await make_forum_readonly(guild, int(c["forum_id"]), member)
        except Exception as exc:
            print(f"[TIMER] Could not set forum read-only: {exc}")

    # 4. DM the customer
    await _send_dm(
        discord_id,
        "⚠️ **Your AdFarm subscription has expired.** "
        "All alts have been stopped and your commands are now locked. "
        "Contact an admin to reactivate your account.",
    )

    await _audit(
        f"🔴 Subscription expired and shutdown executed for "
        f"**{c.get('discord_username', discord_id)}** (`{discord_id}`)"
    )


async def _send_reminder(customer: dict, days_left: float, threshold: int) -> None:
    name = customer.get("discord_username", customer["discord_id"])
    days_int = int(days_left) + 1  # ceiling

    if threshold >= 7:
        icon = "📅"
        urgency = "in **7 days**"
    elif threshold >= 3:
        icon = "⚠️"
        urgency = "in **3 days**"
    else:
        icon = "🚨"
        urgency = "**tomorrow**"

    msg = (
        f"{icon} Hey **{name}**, your AdFarm subscription expires {urgency}. "
        f"Contact an admin to renew and keep your farm running!"
    )

    did = customer["discord_id"]
    await _send_dm(did, msg)
    await _send_forum_message(customer.get("control_thread_id", ""), msg)
    await _audit(f"{icon} Reminder sent to **{name}** (`{did}`) — {days_int}d left")
    cm.record_event(did, "reminder_sent", {"threshold": threshold})


async def scan_once() -> None:
    """Single scan: check all active customers for expiry / reminder."""
    now = time.time()

    # --- Expired customers ---
    expired = cm.get_expired_customers()
    for c in expired:
        did = c["discord_id"]
        print(f"[TIMER] Customer {did} has expired — running shutdown.")
        await run_shutdown_for_customer(did)
        # Clear any pending reminder state for this customer
        for t in REMINDER_THRESHOLDS:
            _sent_reminders.discard((did, t))

    # --- Reminder thresholds ---
    for threshold in REMINDER_THRESHOLDS:
        expiring = cm.get_expiring_customers(within_days=threshold)
        for c in expiring:
            did = c["discord_id"]
            key = (did, threshold)
            if key in _sent_reminders:
                continue
            days_left = (c["expiry_date"] - now) / 86400
            if days_left < 0:
                continue  # already handled above
            # Only send this threshold's reminder if not already sent a closer one
            closer_sent = any(
                (did, t) in _sent_reminders
                for t in REMINDER_THRESHOLDS
                if t < threshold
            )
            if closer_sent:
                continue
            await _send_reminder(c, days_left, threshold)
            _sent_reminders.add(key)


async def dry_run_expiry_alerts() -> dict[str, Any]:
    """Dry-run reminder/expiry path for ``/admin expiry-alerts`` (0.6).

    Reads the same data the real scan uses but sends nothing to customers.
    Returns a report that the caller posts to #admin-alerts.
    """
    now = time.time()
    report: dict[str, Any] = {"generated_at": now, "reminders": [], "expired": []}
    for threshold in REMINDER_THRESHOLDS:
        expiring = cm.get_expiring_customers(within_days=threshold)
        for c in expiring:
            did = c["discord_id"]
            key = (did, threshold)
            already = key in _sent_reminders
            days_left = (c["expiry_date"] - now) / 86400
            if days_left < 0:
                continue
            report["reminders"].append({
                "discord_id": did,
                "username": c.get("discord_username", did),
                "threshold": threshold,
                "days_left": round(days_left, 2),
                "would_send": not already,
                "control_thread": c.get("control_thread_id", ""),
            })
    for c in cm.get_expired_customers():
        report["expired"].append({
            "discord_id": c["discord_id"],
            "username": c.get("discord_username", c["discord_id"]),
            "repos": c.get("repos", []),
        })
    return report


async def auto_redispatch_loop_once() -> int:
    """Re-dispatch 48h auto-renew runs while the customer is still active.

    Every run launched with mode ``limitless`` / runtime 0 is recorded in
    ``run_state``.  Once ``started_at + 48h`` has elapsed (and the alt's
    workflow is not currently in progress) the exact same payload is
    re-dispatched, a renewal is posted to the customer's #control thread and
    the event is logged.  This is the honest implementation of the
    "∞ Limitless = 48h Auto-Renew" contract (TODO 1.3).
    """
    if _bot_ref is None:
        return 0
    dispatched = 0
    try:
        states = cm.get_run_states()
    except Exception:
        return 0
    now = time.time()
    for st in states:
        mode = (st.get("mode") or "").lower()
        runtime = float(st.get("runtime_hours") or 0)
        if mode not in ("limitless", "auto-renew", "0") and runtime != 0:
            continue
        customer = cm.get_customer(st["discord_id"])
        if not customer or not customer.get("active"):
            continue
        started = float(st.get("started_at") or 0)
        last = float(st.get("last_dispatch_at") or started or 0)
        if started <= 0 or (now - last) < 48 * 3600 - 300:  # 5-min slack
            continue
        try:
            from control_bot import github_api
            repo = github_api._repo_for(int(st.get("alt_index") or 1))
            runs = github_api.list_runs(int(st.get("alt_index") or 1), limit=1)
            status = str((runs[0].get("status") if runs else "") or "")
            conclusion = str((runs[0].get("conclusion") if runs else "") or "")
            if status in ("in_progress", "queued", "waiting", "pending", "requested"):
                continue
            payload = st.get("payload") or {}
            if isinstance(payload, str):
                import json as _json
                try:
                    payload = _json.loads(payload)
                except Exception:
                    payload = {}
            ok, msg = github_api.dispatch_workflow(int(st.get("alt_index") or 1), payload)
            if not ok:
                print(f"[AUTO-RENEW] re-dispatch failed for {st['discord_id']}: {msg}")
                continue
            cm.bump_run_renewal(st["discord_id"], int(st.get("alt_index") or 1))
            hdr = (
                f"♻️ **Auto-Renew** — alt {st.get('alt_index')} completed its "
                f"48-hour window and was automatically re-dispatched."
            )
            await _send_forum_message(customer.get("control_thread_id", ""), hdr)
            await _audit(f"♻️ Auto-renew dispatched for {st['discord_id']} alt {st.get('alt_index')}")
            dispatched += 1
        except Exception as exc:
            print(f"[AUTO-RENEW] error: {exc}")
    return dispatched


@tasks.loop(seconds=SCAN_INTERVAL_SEC)
async def subscription_timer() -> None:
    """Hourly background task — scans subscriptions for reminders and expiry."""
    try:
        await scan_once()
    except Exception as exc:
        print(f"[TIMER] Scan error: {type(exc).__name__}: {exc}")


@subscription_timer.before_loop
async def _before_timer() -> None:
    if _bot_ref is not None:
        await _bot_ref.wait_until_ready()
    # Brief startup delay so the bot is fully online before the first scan
    await asyncio.sleep(30)


@tasks.loop(seconds=SCAN_INTERVAL_SEC)
async def auto_redispatch_timer() -> None:
    try:
        n = await auto_redispatch_loop_once()
        if n:
            print(f"[AUTO-RENEW] {n} run(s) re-dispatched this cycle.")
    except Exception as exc:
        print(f"[AUTO-RENEW] scan error: {exc}")


@tasks.loop(seconds=300)
async def lease_renewal_loop() -> None:
    """Renew the Gist startup lease every 5 minutes while the bot runs."""
    if not _RUN_ID:
        return
    try:
        from gist_backup import renew_run_lease
        renew_run_lease(_RUN_ID)
    except Exception as exc:
        print(f"[LEASE] renewal error: {exc}")


def start(bot: "discord.Client", audit_log_ch_id: Optional[int] = None) -> None:
    """Register the bot reference and start the timer loops."""
    register_bot(bot, audit_log_ch_id)
    if not subscription_timer.is_running():
        subscription_timer.start()
    if not auto_redispatch_timer.is_running():
        auto_redispatch_timer.start()
    if not lease_renewal_loop.is_running():
        lease_renewal_loop.start()
    import os as _os
    set_run_id(
        _os.environ.get("GITHUB_RUN_ID", "")
        or _os.environ.get("CONTROL_BOT_RUN_ID", "")
        or ""
    )
