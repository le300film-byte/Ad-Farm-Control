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
import random  # PRE-002 fix: /squad batches stagger with random.uniform
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import config, github_api, sandbox
from .alt_state import AltStateManager
from .command_sync import format_sync_summary, sync_guild_commands
from .persistence import ChannelRegistryStore
from .dashboard import (
    build_all,
    build_single_alt_embed,
    build_diagnose_embed,
    _status_dot,
)

# V8 imports (best-effort – missing modules don't crash the bot)
try:
    import sys as _sys
    import os as _os
    _sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import customer_manager as _cm
    import security as _security
    from security import is_admin as _is_admin_v8
    _V8_LOADED = True
except Exception as _v8_import_err:
    print(f"[V8] Warning: V8 modules not fully loaded: {_v8_import_err}")
    _V8_LOADED = False


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


def _is_operator(inter_or_user: Any) -> bool:
    """Whether the user may drive customer-tier command flows end-to-end.

    True for the bot owner, V8 admins (OWNER_IDS) and ACTIVE subscribers —
    used by modal/hub callback guards that sit *inside* a customer-tier
    command flow (which was already gated at the slash-command entry).
    """
    if _is_owner(inter_or_user):
        return True
    if not _V8_LOADED:
        return False
    try:
        uid = inter_or_user.user.id
        if _is_admin_v8(int(uid)):
            return True
        return bool(_security.is_active_customer(str(uid)))
    except Exception:
        return False


def _customer_owned_alt_ids(uid: Any) -> set[int]:
    """Fleet alt IDs that belong to customer *uid* (V8 bug-fix, plan #4).

    Ownership is resolved from the invoking customer's OWN database record —
    never from the global fleet list — so one customer can never see another
    customer's (or an admin's) alts:

      1. Repo match: the fleet ``ALT_REPOS`` entry for the alt points at the
         same repository (basename) provisioned for this customer by
         ``/admin activate``.
      2. The alt account's Discord ID (``ALT_DISCORD_IDS``) equals the
         customer's own Discord ID.
      3. ``/setup`` credentials: the alt-account username captured during THIS
         customer's setup wizard matches the fleet alt's display name.
    """
    owned: set[int] = set()
    if not _V8_LOADED:
        return owned
    try:
        uid_s = str(int(uid))
    except (TypeError, ValueError):
        return owned
    try:
        customer = _cm.get_customer(uid_s)
    except Exception:
        return owned
    if not customer:
        return owned

    def _base(repo: Any) -> str:
        return str(repo or "").strip().strip("/").split("/")[-1].lower()

    cust_repos = {_base(r) for r in (customer.get("repos") or [])}
    cust_repos.discard("")
    if cust_repos:
        for alt_id, repo in dict(config.ALT_REPOS).items():
            base = _base(repo)
            if base and base in cust_repos:
                owned.add(int(alt_id))
    for alt_id, did in dict(config.ALT_DISCORD_IDS).items():
        if str(did) == uid_s:
            owned.add(int(alt_id))
    try:
        fleet_names = {
            int(aid): str(getattr(state.get(aid), "name", "") or "").strip().lower()
            for aid in state.alt_ids if state.get(aid)
        }
        for cred in _cm.get_alt_credentials(uid_s):
            uname = str(cred.get("username") or "").split("#")[0].strip().lower()
            if not uname:
                continue
            for aid, fname in fleet_names.items():
                if fname and fname == uname:
                    owned.add(int(aid))
    except Exception as _ignored_exc:
        print(f"[BOT] _customer_owned_alt_ids: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
    return owned


def _visible_alt_ids(uid: Any) -> tuple[bool, list[int]]:
    """Return ``(is_admin, alt_ids_visible_to_user)`` for *uid*.

    Admins/owners manage the whole fleet; every other user only sees the alts
    owned by their own customer record (V8 bug-fix, plan #4 — `/alt` used to
    render the operator's full fleet to freshly-activated accounts).
    """
    is_admin = False
    try:
        is_admin = bool(_is_owner(uid))
        if not is_admin and _V8_LOADED:
            is_admin = bool(_is_admin_v8(int(uid)))
    except Exception:
        is_admin = False
    if is_admin:
        return True, list(state.alt_ids)
    owned = _customer_owned_alt_ids(uid)
    return False, [aid for aid in state.alt_ids if aid in owned]


def _customer_for_alt(alt_id: int) -> Optional[dict]:
    """Return the ACTIVE customer record that owns fleet alt *alt_id*.

    Reverse mapping of :func:`_customer_owned_alt_ids` — used by the VIP DM
    auto-reply watcher (plan feature #5) to attribute an incoming buyer DM to
    the right customer. Returns None when the alt belongs to the operator's
    own fleet (no customer record matches).
    """
    if not _V8_LOADED:
        return None
    try:
        repo = str(config.ALT_REPOS.get(int(alt_id), "") or "")
        repo_base = repo.strip().strip("/").split("/")[-1].lower()
        alt_discord_id = str(config.ALT_DISCORD_IDS.get(int(alt_id), "") or "")
        alt_obj = state.get(int(alt_id))
        alt_name = str(getattr(alt_obj, "name", "") or "").strip().lower()
        for c in _cm.list_customers(active_only=True):
            cust_repos = {
                str(r).strip().strip("/").split("/")[-1].lower()
                for r in (c.get("repos") or [])
            }
            if repo_base and repo_base in cust_repos:
                return c
            if alt_discord_id and alt_discord_id == str(c.get("discord_id") or ""):
                return c
            if alt_name:
                try:
                    for cred in _cm.get_alt_credentials(str(c.get("discord_id"))):
                        uname = str(cred.get("username") or "").split("#")[0].strip().lower()
                        if uname and uname == alt_name:
                            return c
                except Exception as _ignored_exc:
                    print(f"[BOT] _customer_for_alt: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
    except Exception:
        return None
    return None


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
async def _load_admin_cog_once(bot_obj: "commands.Bot") -> None:
    """Load the admin cog + register the /admin group — idempotently.

    V8 bug-fix plan #3: /admin used to be loaded only after the first sync and
    a second on_ready (gateway reconnect) re-ran the load, logging
    `Admin cog warning: Command 'admin' already registered.` while leaving
    admins staring at a missing command until the next restart. The cog now
    loads *before* the first sync and the load is a no-op when already present.
    """
    import admin_commands as _ac
    if bot_obj.get_cog("AdminCog") is None:
        await _ac.setup(bot_obj)
    print("[V8] ✅ Admin cog loaded.")


async def _sync_and_hide(bot_obj: "commands.Bot") -> dict:
    """The single command-sync path (shared with /admin sync-commands).

    Copies globals into the control guild, syncs, and best-effort pushes
    per-channel visibility so Discord itself shows only the commands that are
    appropriate for each room (V8 bug-fix plan #1/#3).
    """
    guild = bot_obj.get_guild(config.GUILD_ID) if config.GUILD_ID else None
    if config.GUILD_ID and guild is None:
        print(f"⚠️  GUILD_ID={config.GUILD_ID} but bot isn't in that guild; using global sync.")
    summary = await sync_guild_commands(bot_obj, guild)
    if summary.get("mode") == "guild" and guild is not None:
        print(f"🔗 Guild commands synced to '{guild.name}' ({guild.id})")
    print(f"[CMD] {format_sync_summary(summary)}")
    return summary


_views_registered = False


def _register_persistent_views() -> None:
    """Register the persistent ticket panel view exactly once per process."""
    global _views_registered
    if _views_registered:
        return
    from control_bot.tickets import TicketPanelView
    bot.add_view(TicketPanelView())
    _views_registered = True


@bot.event
async def on_ready():
    """Login/reconnect handler. Every step is idempotent (on_ready re-fires
    after gateway reconnects)."""
    me = bot.user
    config.BOT_USER_ID = me.id
    print(f"✅ Logged in as {me} (id {me.id})")
    if not config.OWNER_IDS:
        print("❌ OWNER_IDS is empty — control commands are disabled until it is configured.")
    if not config.GUILD_ID:
        print("⚠️  GUILD_ID not set — commands will be registered globally (up to 1h delay).")

    # V8: initialize the customer DB and load the admin cog BEFORE the first
    # sync so /admin appears for admins on this very sync (plan #3).
    if _V8_LOADED:
        try:
            _cm.init_db()
        except Exception as _e:
            print(f"[V8] DB init warning: {_e}")
        try:
            await _load_admin_cog_once(bot)
        except Exception as _e:
            print(f"[V8] Admin cog warning: {_e}")

    # One sync path for everything (guild + channel visibility), replacing the
    # three copy-pasted sync blocks that used to live in here.
    try:
        await _sync_and_hide(bot)
    except Exception as _e:
        print(f"[CMD] Command sync warning: {type(_e).__name__}: {_e}")

    # Persistent interaction views (registered once per process).
    _register_persistent_views()

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
        asyncio.create_task(_reconcile_control_channels(
            alt_id, reason="control startup", configured_ids=None,
        ))

    # V8: restore DB from Gist, acquire startup lease, start subscription
    # timer + operational monitors
    if _V8_LOADED:
        # 0.1 — Gist restore-on-startup + startup lock (run-ID lease).
        # A lost lease hard-exits the process (split-brain guard) — commands
        # are already synced above, so the guild UX degrades gracefully.
        try:
            await _phase0_startup(bot)
        except Exception as _e:
            print(f"[V8] Phase 0 startup warning: {_e}")
        try:
            import timer_engine as _te
            _audit_ch = int(_os.environ.get("AUDIT_LOG_CH_ID", "0") or "0") or None
            _te.start(bot, _audit_ch)
            print("[V8] ✅ Subscription timer started.")
        except Exception as _e:
            print(f"[V8] Timer start warning: {_e}")
        # Phase 1.4 + Phase 2 monitors
        try:
            await _start_ops_monitors(bot)
        except Exception as _e:
            print(f"[V8] Ops monitor warning: {_e}")
        # V8 bug-fix plan #2: prune fleet alt mappings whose GitHub repository
        # no longer exists so ghost "Alt N" health spam cannot come back.
        # Fire-and-forget: startup latency and network errors must not block.
        asyncio.create_task(_sweep_stale_fleet_alts())


# ----- Phase 0 startup: Gist restore + lease + monitors -----

async def _phase0_startup(bot: "commands.Bot") -> None:
    """Restore customers.db from the backup Gist and acquire the run lease.

    Order (0.1): restore → lease. If a concurrent boot holds the lease the
    process aborts immediately with a critical alert (prevents split-brain).
    """
    from gist_backup import (
        gist_configured, acquire_run_lease, register_alert_callback,
        restore_db_from_gist,
    )
    from control_bot import ops

    loop = asyncio.get_running_loop()
    ops.ensure_alert_queue()
    from control_bot.alerts import set_event_loop
    set_event_loop(loop)
    register_alert_callback(ops.enqueue_async_alert)

    if not gist_configured():
        print("[V8] ⚠️  CUSTOMERS_GIST_ID/GIST_TOKEN not configured — local-only DB mode "
              "(no cross-chunk durability). Set both in repo secrets (V8_RUNBOOKS §1).")

    # Restore before any write so we never boot from a stale local file.
    if gist_configured():
        restored = await asyncio.to_thread(restore_db_from_gist)
        if restored.get("ok"):
            print(f"[V8] ✅ Restored customers.db from Gist ({restored.get('source')}, "
                  f"rev {restored.get('revision')})")
            from control_bot import metrics
            metrics.note_db_restore(str(restored.get("source", "")))
            # The restored file may predate the current schema (e.g. a v2
            # backup restored by a v3 bot — missing customers.autoreply_text).
            # init_db() is idempotent and applies the lightweight migration.
            try:
                _cm.init_db()
            except Exception as _mig_exc:
                print(f"[V8] Post-restore schema migration warning: {_mig_exc}")

    run_id = _os.environ.get("GITHUB_RUN_ID", "") or f"local-{os.getpid()}"
    lease = await asyncio.to_thread(acquire_run_lease, run_id)
    if not lease.get("ok"):
        holder = lease.get("holder", {})
        _msg = (
            f"🚨 **Startup aborted: another control bot holds the Gist lease** "
            f"(run `{holder.get('run_id')}`, host `{holder.get('host')}`, pid "
            f"{holder.get('pid')}). Refusing to boot a second writer — check the "
            "workflow Actions tab and cancel the duplicate run."
        )
        print(f"[V8] {_msg}")
        try:
            from control_bot.alerts import post_admin_alert
            await post_admin_alert(_msg)
        finally:
            os._exit(1)  # hard abort: two writers would risk split-brain

    print(f"[V8] ✅ Startup lease acquired (run {run_id})")
    import timer_engine as _te_mod
    _te_mod.set_run_id(run_id)

    # heartbeat endpoint (Phase 1.4) — external pinger target
    port = ops.start_heartbeat_server()
    if port:
        print(f"[V8] ✅ Heartbeat endpoint on /healthz (port {port}). Point "
              "UptimeRobot/Healthchecks at it and set a 15-min alert.")


async def _start_ops_monitors(bot: "commands.Bot") -> None:
    """Start continuous background loops for health/metrics (Phases 1.4-2.4)."""
    from discord.ext import tasks as _tasks
    import control_bot.ops as ops_mod
    from control_bot import alerts

    alerts.wire(bot)
    from control_bot.proofs import wire_proofs_channel
    wire_proofs_channel()

    @_tasks.loop(seconds=3600)
    async def _token_health():
        await ops_mod.post_worker_token_health(bot)

    @_tasks.loop(seconds=3600)
    async def _gist_usage():
        await ops_mod.gist_usage_check(bot)

    @_tasks.loop(seconds=1800)
    async def _rss_mem():
        await ops_mod.rss_memory_check(bot)

    @_tasks.loop(seconds=86400)
    async def _nightly_sweep():
        await ops_mod.nightly_token_sweep(bot)

    @_tasks.loop(seconds=86400)
    async def _tune_hints():
        await ops_mod.post_tune_hints(bot)

    @_tasks.loop(seconds=300)
    async def _heartbeat_watch():
        await ops_mod.check_missed_external_beat(bot)
        await ops_mod.flush_alerts(bot)

    @_tasks.loop(seconds=7 * 86400)
    async def _weekly():
        from control_bot import metrics
        await metrics.post_weekly_summary(bot)

    @_tasks.loop(seconds=86400)
    async def _forum_perm_check():
        from discord_forum import startup_forum_permission_self_check
        report = await startup_forum_permission_self_check(bot)
        if report.get("problems"):
            await alerts.post_admin_alert(
                "🔒 **Forum permission mismatch detected** (TODO 0.6).\n"
                + "\n".join(
                    f"- customer `{p.get('customer')}` forum `{p.get('forum')}`: "
                    f"{'; '.join(p.get('mismatches', []))}"
                    for p in report["problems"][:10]
                )
            )
        elif report.get("error"):
            await alerts.post_admin_alert(f"🔒 Forum self-check skipped: {report['error']}")

    # Run once at startup, then daily (0.6 startup self-check).
    try:
        from discord_forum import startup_forum_permission_self_check as _forum_check
        _initial = await _forum_check(bot)
        if _initial.get("problems"):
            await alerts.post_admin_alert(
                "🔒 **Forum permission mismatch detected at startup** (TODO 0.6).\n"
                + "\n".join(
                    f"- customer `{p.get('customer')}` forum `{p.get('forum')}`: "
                    f"{'; '.join(p.get('mismatches', []))}"
                    for p in _initial["problems"][:10]
                )
            )
    except Exception as exc:
        print(f"[V8] Forum self-check failed: {exc}")

    for loop in (_token_health, _gist_usage, _rss_mem, _nightly_sweep,
                 _tune_hints, _heartbeat_watch, _weekly, _forum_perm_check):
        if not loop.is_running():
            loop.start()
    print("[V8] ✅ Ops monitors started (tokens, gist, RSS, sweep, heartbeat, forum).")


# ----- Slash commands -----
# Max trading channels per alt (V8 bug-fix M).  The canonical limit lives in
# customer_manager.MAX_CHANNELS_PER_ALT; mirror it here with a safe fallback
# so /setup and /channels enforce the cap even if V8 imports fail.
try:
    from customer_manager import (  # noqa: F401
        MAX_CHANNELS_PER_ALT as _MAX_CHANNELS_PER_ALT,
        channel_limit_message as _channel_limit_message,
    )
except Exception:
    _MAX_CHANNELS_PER_ALT = 10

    def _channel_limit_message(limit: int = 10) -> str:
        return (
            f"❌ Maximum {limit} channels per alt. "
            "Remove one before adding a new one."
        )


# Role sets (V8 bug-fix F): slash commands are grouped by the viewer's tier so
# /help and the interaction gate apply one consistent rule per command. The
# canonical definitions live in security.py (single source of truth shared by
# /help filtering, the tier checks and the new channel gate, V8 bug-fix plan #1)
# — these module-level aliases exist for back-compat with existing callers/tests.
try:
    from security import (  # noqa: F401  (re-exported for back-compat)
        ADMIN_COMMANDS as ROLE_ADMIN_COMMANDS,
        VIP_COMMANDS as ROLE_VIP_COMMANDS,
        PUBLIC_COMMANDS as ROLE_PUBLIC_COMMANDS,
        CUSTOMER_COMMANDS as ROLE_CUSTOMER_COMMANDS,
    )
except Exception:  # V8 modules unavailable → no tier filtering at all
    ROLE_ADMIN_COMMANDS = ROLE_VIP_COMMANDS = ROLE_PUBLIC_COMMANDS = ROLE_CUSTOMER_COMMANDS = frozenset()


def viewer_role(inter: discord.Interaction) -> str:
    """Classify a user for command visibility: admin / vip / customer / public.

    V8 bug-fix F: admins (OWNER_IDS) see everything; VIPs see VIP + customer +
    public commands; active customers see customer + public; everyone else sees
    only public commands (/help, /getstarted).
    """
    try:
        if _V8_LOADED:
            uid = inter.user.id if hasattr(inter, "user") else None
            if uid is not None:
                if _is_owner(inter) or _is_admin_v8(int(uid)):
                    return "admin"
                if _security.is_active_customer(str(uid)):
                    if _security.is_vip_customer(str(uid)):
                        return "vip"
                    return "customer"
            return "public"
        return "admin" if _is_owner(inter) else "public"
    except Exception:
        try:
            return "admin" if _is_owner(inter) else "public"
        except Exception:
            return "public"


def commands_for_role(role: str) -> Optional[set[str]]:
    """Top-level command names visible to *role* (V8 bug-fix F).

    Delegates to the canonical tier model in ``security.allowed_commands_for_role``.
    Returns ``None`` when the V8 modules are unavailable — callers treat that
    as "no filtering" so a degraded deployment never shows a blank /help.
    """
    if _V8_LOADED:
        try:
            return set(_security.allowed_commands_for_role(role))
        except Exception as exc:
            print(f"[V8] role filter degraded ({type(exc).__name__}: {exc}); using fallback sets")
            if role == "admin":
                return set(ROLE_ADMIN_COMMANDS | ROLE_VIP_COMMANDS | ROLE_CUSTOMER_COMMANDS | ROLE_PUBLIC_COMMANDS)
            if role == "vip":
                return set(ROLE_VIP_COMMANDS | ROLE_CUSTOMER_COMMANDS | ROLE_PUBLIC_COMMANDS)
            if role == "customer":
                return set(ROLE_CUSTOMER_COMMANDS | ROLE_PUBLIC_COMMANDS)
            return set(ROLE_PUBLIC_COMMANDS)
    return None  # no V8 stack → /help lists every registered command


async def _ephemeral_reply(inter: discord.Interaction, msg: str) -> None:
    """Send *msg* ephemerally via whichever response stage is still open."""
    if inter.response.is_done():
        await inter.followup.send(msg, ephemeral=True)
    else:
        await inter.response.send_message(msg, ephemeral=True)


async def _cooldown_allowed(inter: discord.Interaction) -> bool:
    """Enforce the command cooldown; replies and returns False when hot."""
    cd = _on_cooldown(inter.user.id)
    if cd > 0:
        await _ephemeral_reply(inter, f"⏱️ Cooldown — wait {cd:.1f}s.")
        return False
    return True


async def _check_perms(
    inter: discord.Interaction,
    role: str = "owner",
    command: Optional[str] = None,
) -> bool:
    """Gate a slash command by *role* (V8 bug-fix F/J) and channel context.

    role="owner"  → legacy behaviour: OWNER_IDS only (fail closed).
    role="customer" → admins OR active subscribers (subscription denial for
        everyone else — plan J).
    role="vip"    → admins OR active VIP subscribers.
    command=<name> → additionally enforces the V8 channel-context policy
        (plan #1): customer-tier commands are refused in public announcement
        rooms, VIP-only rooms stay customer-safe, and bot owners bypass the
        gate to keep operating from wherever setup requires. Denials use the
        canonical "❌ This command is not available in this channel." message.

    The bot owner (config OWNER_IDS — the same allow-list security.py reads)
    always passes every role; the legacy cooldown keeps applying to owners.
    When the V8 modules are unavailable every role falls back to the legacy
    owner-only gate so deployments never open up accidentally.
    """
    # Channel-context gate first (cheap, synchronous classification) so a
    # wrong-room invocation never consumes the cooldown or hits the DB.
    if command is not None and _V8_LOADED:
        try:
            if not await _security.enforce_channel_gate(inter, command):
                return False
        except AttributeError:
            print("[V8] security.enforce_channel_gate missing; channel gate skipped.")

    # V8 role-aware gating.
    if _V8_LOADED and role in ("customer", "vip"):
        try:
            from security import check_customer_access as _cca
            owner = _is_owner(inter)
            if not owner:
                if not await _cca(inter, vip_only=(role == "vip")):
                    return False
            else:
                # Owners keep the legacy cooldown behaviour.
                if not await _cooldown_allowed(inter):
                    return False
            return True
        except Exception as exc:
            print(f"[V8] Role gate degraded ({exc}); falling back to owner gate.")
            # fall through to the legacy owner gate below

    if not _is_owner(inter):
        # Non-owner on a customer command → the active-subscription denial
        # (never-customer case, V8 bug-fix J) is sent by the role gate above;
        # here we keep the legacy denial for non-V8 / owner-role paths.
        await _ephemeral_reply(inter, "🔒 You aren't authorized to run control commands.")
        return False
    if not await _cooldown_allowed(inter):
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
                        except Exception as _ignored_exc:
                            print(f"[BOT] _hydrate_discord_state: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
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
            placeholder="4. Runtime: 6/12/18/24/48 hours or ∞ Limitless", min_values=1, max_values=1,
            options=[
                discord.SelectOption(label="6 hours", value="6"),
                discord.SelectOption(label="12 hours", value="12"),
                discord.SelectOption(label="18 hours", value="18"),
                discord.SelectOption(label="24 hours", value="24"),
                discord.SelectOption(label="48 hours", value="48"),
                discord.SelectOption(label="∞ Limitless (until /shutdown)", value="0"),
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
        # V8 bug-fix M: the interactive add flow enforces the 10-channel cap too.
        a_obj = state.get(self.alt_id)
        if a_obj is not None and len(a_obj.channels) >= _MAX_CHANNELS_PER_ALT:
            return await inter.response.send_message(
                _channel_limit_message(_MAX_CHANNELS_PER_ALT), ephemeral=True
            )
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
            except Exception as _ignored_exc:
                print(f"[BOT] _on_remove_channel: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
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
        except Exception as _ignored_exc:
            print(f"[BOT] _on_policy_select: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
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
    embed = discord.Embed(title="🚀 V8 Ad Run Launcher", color=0x5865F2)
    embed.description = "Choose every runtime setting below, then enter the ad text/rates. This form is private and owner-only."
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

    raw_message = (values.get("raw_message") or "").strip()
    if not raw_message:
        # Compatibility with the two modal field IDs used by older clients.
        raw_message = (values.get("sell_extra") or values.get("buy_simple_text") or "").strip()
    if not raw_message:
        # Legacy contract (test_validation_preserves_modal_constraints): a
        # rate is enough to build the ad copy; the raw message is required
        # only when no rate was supplied. The wizard itself still requires the
        # message field, so this path is a backend compatibility fallback.
        if ad_type == "sell" and rate is not None:
            raw_message = f"💸 Selling at {rate:g}/1K — DM me!"
        elif ad_type == "buy" and rate is not None:
            raw_message = f"🛒 Buying at {rate:g}/1K — DM me!"
        else:
            errors.append("Enter a raw message or question.")
    elif len(raw_message) > 1900:
        errors.append("Raw message/question is limited to 1900 characters.")

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
    return errors, {
        "alt_id": alt_id,
        "rate": rate,
        "rap": rap,
        "interval": interval,
        "hours": hours,
        "raw_message": raw_message,
    }


def _valid_repo_name(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?", value or ""))


def _modal_value(item) -> str:
    """Trimmed string value of a discord TextInput (or a raw string)."""
    return str(getattr(item, "value", item) or "").strip()


def _fleet_default_channels() -> list[str]:
    """Fleet-level default channel IDs from CHANNEL_IDS (env or tuning JSON)."""
    raw = os.environ.get("CHANNEL_IDS") or config._raw("CHANNEL_IDS")
    return [c.strip() for c in (raw or "").split(",") if c.strip().isdigit()]


def _resolve_new_alt_id(raw_aid: str) -> tuple[Optional[int], str]:
    """Pick the alt slot for AltAddModal → ``(alt_id, error_text)``.

    Explicit ids are validated against the 1–4 fleet window; when blank, the
    first free slot wins. ``error_text`` is empty on success.
    """
    if raw_aid:
        try:
            alt_id = int(raw_aid)
        except (TypeError, ValueError):
            return None, "❌ Alt ID must be an integer between 1 and 4."
    else:
        free_ids = [i for i in (1, 2, 3, 4) if i not in state.alt_ids]
        if not free_ids:
            return None, "❌ All 4 alt slots are currently occupied. Remove one with `/alt action:remove` first."
        alt_id = free_ids[0]
    if alt_id not in {1, 2, 3, 4}:
        return None, "❌ Alt ID must be between 1 and 4."
    if alt_id in state.alt_ids:
        return None, f"❌ Alt `{alt_id}` is already configured. Use `/alt action:update` to modify it."
    return alt_id, ""


def _resolve_fleet_repo(alt_id: int, detected_username: str, raw_repo: str) -> str:
    """Repository slug for a new fleet alt (auto-created names round-robin)."""
    if raw_repo:
        return raw_repo if "/" in raw_repo else f"{config.GITHUB_OWNER}/{raw_repo}"
    clean_slug_name = re.sub(r"[^a-zA-Z0-9_-]", "", detected_username.lower().replace(" ", "-")) or f"alt{alt_id}"
    # V8 bug-fix (plan #1): auto-created alt repos round-robin onto
    # the WORKER GitHub accounts — never the main account. github_api
    # resolves the matching worker PAT for create/secret calls.
    owner = _pick_fleet_repo_owner()
    return f"{owner}/alt{alt_id}-{clean_slug_name}"


def _resolve_inherited_channels(raw_channels: str) -> list[str]:
    """Channel ids for a new alt: explicit list → fleet CHANNEL_IDS → a sibling.

    Keeps new alts usable out of the box without hardcoding channel ids in
    code: the fallbacks are read from the runtime configuration and the live
    fleet state.
    """
    parsed = [c.strip() for c in raw_channels.split(",") if c.strip().isdigit()]
    if parsed:
        return parsed
    parsed = _fleet_default_channels()
    if parsed:
        return parsed
    for other_id in state.alt_ids:
        o_alt = state.get(other_id)
        if o_alt and o_alt.channels:
            found = [str(c) for c in o_alt.channels.keys() if str(c).isdigit()]
            if found:
                return found
    return []


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


def _registry_without(alts: list[int]) -> tuple[dict[int, str], dict[int, int], dict[int, str]]:
    """Snapshot the live alt registries with *alts* removed."""
    drop = {int(a) for a in alts}
    repos = {aid: repo for aid, repo in dict(config.ALT_REPOS).items() if aid not in drop}
    discord_ids = {aid: did for aid, did in dict(config.ALT_DISCORD_IDS).items() if aid not in drop}
    names = {aid: nm for aid, nm in dict(config.ALT_NAMES).items() if aid not in drop}
    return repos, discord_ids, names


async def _drop_alts_from_everywhere(
    alts: list[int],
) -> tuple[bool, str]:
    """Prune alt IDs from secrets, in-memory config, live state and the
    persisted state file — the ONLY sanctioned way to retire an alt.

    V8 bug-fix plan #2: alt removal used to touch some of these stores but not
    all of them, so a "deleted" alt could resurface as a ghost (stale heartbeat
    alerts, /alt listings) after a restart or from the JSON state snapshot.
    Returns (persisted_ok, detail).
    """
    if not alts:
        return True, "nothing to prune"
    repos, discord_ids, names = _registry_without(alts)
    persisted, detail = await _persist_alt_registry(repos, discord_ids, names)
    if not persisted:
        return False, detail
    _apply_alt_registry(repos, discord_ids, names)
    for alt_id in alts:
        if state.get(alt_id):
            state.remove_alt(alt_id)
    return True, detail


async def _sweep_stale_fleet_alts(*, prune: bool = True) -> dict:
    """Verify every ALT_REPOS mapping still has a live GitHub repo (plan #2).

    Repos deleted directly on GitHub (or left over from an old installation)
    keep the fleet — and with it the health monitor, /alt and the startup
    banner — talking about alts that no longer exist. This sweep runs once
    after boot and removes only *confirmed* 404 mappings; any API hiccup is
    logged and the entry is kept, so a rate limit can never delete a live alt.
    """
    summary: dict[str, Any] = {"checked": 0, "pruned": [], "kept": 0, "skipped": 0}
    if not config.GITHUB_TOKEN or not config.ALT_REPOS:
        summary["note"] = "no token or no fleet mapping to verify"
        return summary
    missing: list[int] = []
    for alt_id, repo in sorted(dict(config.ALT_REPOS).items()):
        try:
            exists, detail = await asyncio.to_thread(github_api.repository_exists, repo)
        except Exception as exc:
            summary["skipped"] += 1
            print(f"[SWEEP] repo check for alt {alt_id} (`{repo}`) failed: "
                  f"{type(exc).__name__}: {exc} — keeping mapping.")
            continue
        summary["checked"] += 1
        if exists:
            summary["kept"] += 1
        elif "not found" in str(detail).lower():
            missing.append(int(alt_id))
        else:
            summary["skipped"] += 1
            print(f"[SWEEP] alt {alt_id} (`{repo}`) unverifiable ({detail}) — keeping mapping.")
    if not missing:
        return summary
    summary["pruned"] = missing
    if not prune:
        return summary
    ok, detail = await _drop_alts_from_everywhere(missing)
    text = (
        f"🧹 **Fleet sweep** — {len(missing)} stale alt mapping(s) pruned: "
        f"alts {', '.join(str(a) for a in missing)} (repositories deleted upstream). "
        + ("Registry secrets updated." if ok else f"⚠️ Persist failed: {detail}")
    )
    print(f"[SWEEP] {text}")
    await _log_control(text)
    return summary


async def _log_alt_add_event(alt_id: int, ok: bool, text: str, *, name: str = "Alt") -> None:
    """Always emit an explicit alt-add result so the diagnostic log channel has
    both success and failure statements (Issue #46 / PMTP 2.4)."""
    icon = "✅" if ok else "❌"
    full = f"{icon} [ALT-ADD] [{('PASS' if ok else 'FAIL')}] {text}"
    try:
        await _log_control(full[:2000])
    except Exception:
        print(f"[ALT-ADD] audit log failed: {full[:200]}")
    if ok and alt_id in state.alt_ids:
        state.append_log(alt_id, full, emoji=icon if ok else "❌", color=0x57F287 if ok else 0xED4245, kind="CONTROL" if ok else "ERROR")


def _pick_fleet_repo_owner() -> str:
    """Owner for auto-created fleet alt repos (V8 bug-fix, plan #1).

    Round-robins across the configured WORKER GitHub accounts when worker
    credentials exist; falls back to the fleet owner (GITHUB_OWNER or the
    core repo owner) only when no workers are configured at all.
    """
    try:
        from github_dispatch import pick_worker
        worker_user, _tok = pick_worker()
        worker_user = (worker_user or "").strip().strip("/")
        if worker_user:
            return worker_user
    except Exception as exc:
        print(f"[ALT-ADD] worker round-robin unavailable ({type(exc).__name__}: {exc})")
    return config.GITHUB_OWNER or (config.CORE_REPO.split("/")[0] if "/" in config.CORE_REPO else "owner")


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
        """Add a fleet alt: verify token → allocate slot → provision repo →
        persist registry. (V8 cleanup: slot/repo/channel resolution split out
        into the module-level ``_resolve_*`` helpers so each rule is reusable
        and unit-testable.)"""
        if not _is_operator(inter):
            await inter.response.send_message("🔒 You aren't authorized to manage alts.", ephemeral=True)
            return
        value = _modal_value
        token = value(self.user_token)
        if not token:
            await _log_alt_add_event(0, False, "Alt add failed: user token is required.")
            return await inter.response.send_message("❌ User token is required.", ephemeral=True)

        await inter.response.defer(ephemeral=True)

        # 1. Validate token with Discord API and extract profile
        ok_prof, profile = await asyncio.to_thread(github_api.fetch_discord_user_profile, token)
        if not ok_prof or not isinstance(profile, dict) or not profile.get("id"):
            err_msg = profile.get("error", "Invalid user token") if isinstance(profile, dict) else "Invalid token"
            await _log_alt_add_event(0, False, f"Alt add failed during Discord token validation: {err_msg}")
            return await inter.followup.send(f"❌ Could not authenticate alt with Discord: {err_msg}", ephemeral=True)

        detected_did = str(profile.get("id"))
        detected_username = str(profile.get("username") or "alt")
        detected_name = str(profile.get("global_name") or profile.get("username") or "alt")

        # 2. Resolve Alt ID (explicit or first free slot, validated)
        alt_id, alt_id_err = _resolve_new_alt_id(value(self.alt_id))
        if alt_id is None:
            await _log_alt_add_event(0, False, f"Alt add failed: {alt_id_err.lstrip('❌ ')}")
            return await inter.followup.send(alt_id_err, ephemeral=True)

        # 3. Resolve Display Name & Discord User ID
        custom_name = value(self.name)
        name = custom_name if custom_name else detected_name
        name = re.sub(r"[\r\n]", " ", name)[:80].strip() or f"Alt {alt_id}"

        custom_did = value(getattr(self, "discord_user_id", None))
        did = custom_did if (custom_did and custom_did.isdigit()) else detected_did

        # 4. Resolve repository + channels (auto-create/inherit when blank)
        repo = _resolve_fleet_repo(alt_id, detected_username, value(self.repository))
        parsed_channels = _resolve_inherited_channels(value(getattr(self, "channels", None)))
        channels_csv = ",".join(parsed_channels)

        # 5. Auto-create repo on GitHub, upload templates, and populate secrets
        ok_prov, prov_detail = await asyncio.to_thread(
            github_api.provision_alt_repository_files_and_secrets, repo, token, channels_csv
        )
        if not ok_prov:
            await _log_alt_add_event(0, False, f"Alt add failed while provisioning `{repo}`: {prov_detail}")
            return await inter.followup.send(f"❌ Auto-provisioning failed for `{repo}`: {prov_detail}", ephemeral=True)

        # 6. Persist alt in core fleet registry
        repos = dict(config.ALT_REPOS); repos[alt_id] = repo
        discord_ids = dict(config.ALT_DISCORD_IDS); discord_ids[alt_id] = int(did)
        names = dict(config.ALT_NAMES); names[alt_id] = name
        persisted, persist_detail = await _persist_alt_registry(repos, discord_ids, names)
        if not persisted:
            await _log_alt_add_event(0, False, f"Alt add failed to persist core registry: {persist_detail}")
            return await inter.followup.send(f"❌ Alt was not registered in core map: {persist_detail}", ephemeral=True)

        _apply_alt_registry(repos, discord_ids, names)
        state.add_alt(alt_id, name)
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
        if not _is_operator(inter):
            await inter.response.send_message("🔒 You aren't authorized to manage alts.", ephemeral=True)
            return
        alt_id = self.alt_id_value
        if not state.get(alt_id):
            await inter.response.send_message("❓ Unknown alt.", ephemeral=True)
            return
        value = _modal_value
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


def _build_alt_overview_embed(
    selected_alt: Optional[int] = None,
    allowed_ids: Optional[list[int]] = None,
) -> discord.Embed:
    """Fleet hub embed. ``allowed_ids`` restricts the rows to the caller's own
    alts (V8 bug-fix, plan #4); ``None`` keeps the full-fleet admin view."""
    embed = discord.Embed(
        title="👥 Fleet Alt Management Hub",
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc),
    )
    visible = list(state.alt_ids) if allowed_ids is None else [
        aid for aid in allowed_ids if aid in state.alt_ids
    ]
    if not visible:
        embed.description = "⚠️ _No alts configured in fleet._ Click **➕ Add Alt** below to add one."
        return embed

    rows = []
    for a in state.all():
        if a.alt_id not in visible:
            continue
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
    if allowed_ids is not None:
        embed.set_footer(text="Showing only the alts that belong to your account. Tokens are encrypted and never shown.")
    else:
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
    def __init__(self, owner_id: int, selected_alt: int = 1,
                 visible_alts: Optional[list[int]] = None):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        # V8 bug-fix (plan #4): the hub only ever lists/controls the alts the
        # invoking user owns; admins pass None and keep the full fleet view.
        self._full_fleet = visible_alts is None
        if visible_alts is None:
            self.visible: list[int] = list(state.alt_ids)
        else:
            self.visible = [aid for aid in visible_alts if aid in state.alt_ids]
        self.selected_alt = (
            selected_alt if selected_alt in self.visible
            else (self.visible[0] if self.visible else 1)
        )
        self._build_items()

    def _build_items(self):
        self.clear_items()
        if len(self.visible) > 1:
            options = [
                discord.SelectOption(
                    label=f"Alt {i}: {state.get(i).name if state.get(i) else f'Alt {i}'}"[:100],
                    value=str(i),
                    default=(i == self.selected_alt),
                    emoji="🟢" if state.is_online(i) else "⚪",
                )
                for i in self.visible[:25]
            ]
            class _AltPicker(discord.ui.Select):
                def __init__(parent_self):
                    super().__init__(placeholder="Choose an Alt to manage...", min_values=1, max_values=1, options=options, row=0)
                async def callback(sel_self, inter: discord.Interaction):
                    if not _is_operator(inter):
                        return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
                    picked = int(sel_self.values[0])
                    if picked not in self.visible:  # plan #4: never switch to a hidden alt
                        return await inter.response.send_message("❓ That alt is not available to you.", ephemeral=True)
                    self.selected_alt = picked
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
            if not _is_operator(inter):
                return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
            await inter.response.send_modal(AltAddModal())

        async def _cb_update(inter: discord.Interaction):
            if not _is_operator(inter):
                return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
            if not state.get(self.selected_alt):
                return await inter.response.send_message("❓ Unknown alt.", ephemeral=True)
            await inter.response.send_modal(AltUpdateModal(self.selected_alt))

        async def _cb_logs(inter: discord.Interaction):
            if not _is_operator(inter):
                return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
            await _handle_alt_logs(inter, self.selected_alt, limit=15)

        async def _cb_selfcheck(inter: discord.Interaction):
            if not _is_operator(inter):
                return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
            await _handle_alt_selfcheck(inter, self.selected_alt)

        async def _cb_runs(inter: discord.Interaction):
            if not _is_operator(inter):
                return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
            await _handle_alt_runs(inter, self.selected_alt, limit=5)

        async def _cb_clearlogs(inter: discord.Interaction):
            if not _is_operator(inter):
                return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
            await _handle_alt_clearlogs(inter, self.selected_alt)

        async def _cb_refresh(inter: discord.Interaction):
            if not _is_operator(inter):
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
        # Non-admin viewers get the ownership-filtered embed (plan #4).
        return _build_alt_overview_embed(
            self.selected_alt,
            allowed_ids=None if self._full_fleet else self.visible,
        )


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
                    if not _is_operator(inter):
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
            if not _is_operator(inter):
                return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
            target = self.alt_id or (state.alt_ids[0] if state.alt_ids else 1)
            alt_obj = state.get(target)
            curr = getattr(alt_obj, "deal_scan_enabled", True) if alt_obj else True
            new_val = "off" if curr else "on"
            await _handle_deal_scan(inter, target, new_val)

        async def _cb_modal(inter: discord.Interaction):
            if not _is_operator(inter):
                return await inter.response.send_message("❌ Unauthorized.", ephemeral=True)
            target = self.alt_id or (state.alt_ids[0] if state.alt_ids else 1)
            await inter.response.send_modal(DealsManagerModal(target))

        async def _cb_sim(inter: discord.Interaction):
            if not _is_operator(inter):
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
            if not _is_operator(inter):
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
    alt_id = int(parsed["alt_id"])
    alt = state.get(alt_id)
    name = alt.name if alt else f"Alt {alt_id}"
    ad_type = str(values.get("ad_type") or "").lower()
    rate = parsed.get("rate")
    interval = parsed.get("interval")
    hours = parsed.get("hours")
    runtime_label = "∞ Limitless (until /shutdown)" if int(hours or 0) == 0 else f"{hours}h"
    embed = discord.Embed(
        title="🚀 /run Command Preview — Review Before Dispatch",
        description=(
            "This is the exact configuration that will be sent to GitHub Actions.\n"
            "**_No workflow has been dispatched yet._**"
        ),
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Alt", value=f"**{name}** (ID `{alt_id}`)", inline=True)
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
    if int(hours or 0) == 0:
        # TODO 1.3 — honest copy: ∞ Limitless runs 48h max per dispatch; the
        # control bot auto-renews it while the subscription stays active.
        embed.add_field(
            name="🟡 ∞ Limitless — read this",
            value="∞ Limitless runs for a **maximum of 48 hours per dispatch**. "
                  "A new run will be required to continue. If auto-renew is "
                  "enabled, the timer engine re-dispatches it automatically "
                  "while your subscription is active.",
            inline=False,
        )
    embed.set_footer(text="/run now requires an explicit Confirm Launch step before dispatch.")
    return embed


async def _dispatch_run_from_modal(inter: discord.Interaction, values: dict[str, str], parsed: dict[str, object]) -> None:
    if not _is_operator(inter):
        await inter.response.send_message("🔒 You aren't authorized to run control commands.", ephemeral=True)
        return
    if not inter.response.is_done():
        await inter.response.defer(ephemeral=True)
    alt_id = int(parsed["alt_id"]); alt = state.get(alt_id)
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
    if not _is_operator(inter):
        if inter.response.is_done():
            await inter.followup.send("🔒 You aren't authorized to run control commands.", ephemeral=True)
        else:
            await inter.response.send_message("🔒 You aren't authorized to run control commands.", ephemeral=True)
        return
    if not inter.response.is_done():
        await inter.response.defer(ephemeral=True)
    alt_id = int(parsed["alt_id"]); alt = state.get(alt_id)
    if not alt:
        return await inter.followup.send("❓ Unknown alt.", ephemeral=True)
    if not config.GITHUB_TOKEN or not config.GITHUB_OWNER or alt_id not in config.ALT_REPOS:
        return await inter.followup.send("❌ GitHub control is not configured for this alt.", ephemeral=True)

    # Resolve target channels to pass to workflow so newly added alts never crash on empty channels
    active_chs = [str(c) for c in alt.channels.keys() if str(c).isdigit()] if alt and alt.channels else []
    if not active_chs:
        for other_id in state.alt_ids:
            other_alt = state.get(other_id)
            if other_alt and other_alt.channels:
                active_chs = [str(c) for c in other_alt.channels.keys() if str(c).isdigit()]
                if active_chs:
                    break
    if not active_chs:
        active_chs = _fleet_default_channels()

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
    if values["ad_type"] == "sell": inputs.update({"sell_rate": values["sell_rate"], "sell_extra": values.get("sell_extra", "")})
    else: inputs.update({"buy_style": values["buy_style"], "buy_rate": str(parsed["rate"]), "buy_rate_rap": str(parsed["rap"]), "buy_simple_text": values.get("buy_simple_text", "")})
    try:
        await asyncio.to_thread(github_api.cancel_run, alt_id)
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
    # V8: record run_state so the timer engine can auto-renew (1.3) and TTFTV
    #/metrics can attribute the run to the customer.
    if _V8_LOADED:
        try:
            cust = _cm.get_customer(str(inter.user.id))
            if cust:
                user_id = str(inter.user.id)
                _cm.record_run_state(
                    user_id, alt_id,
                    mode="limitless" if int(parsed["hours"] or 0) == 0 else "timed",
                    runtime_hours=int(parsed["hours"] or 0),
                    payload=inputs,
                )
        except Exception as exc:
            print(f"[V8] run_state record warning: {exc}")
    runtime_label = "∞ Limitless (until /shutdown)" if int(parsed["hours"] or 0) == 0 else f"{parsed['hours']}h"
    text = f"🚀 **{alt.name}** queued privately: {values['ad_type']} · {parsed['interval']}min × {runtime_label} · image={values['attach_image']}\n{msg}"
    await inter.followup.send(text, ephemeral=True)
    await _log_control(text)
    state.append_log(alt_id, text, emoji="🚀", color=0x57F287, kind="CONTROL")


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

@bot.tree.command(name="run", description="Launch Ad Run — pick alt, enter ad text, preview & confirm dispatch")
async def cmd_run(inter: discord.Interaction):
    if not await _check_perms(inter, role="customer", command="run"):
        return
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
    view = RunStartView(inter.user.id)
    await inter.response.send_message(embed=_run_start_embed(view), view=view, ephemeral=True)


@bot.tree.command(name="getstarted", description="Quick-Start Guide — step-by-step V8 checklist from policy acceptance to first ad run.")
async def cmd_getstarted(inter: discord.Interaction):
    """Public onboarding guide (V8 bug-fix F/G).

    Paid customers get a short "already set up" note instead of the full
    onboarding tour; only non-customers see the step-by-step guide.
    """
    if _V8_LOADED:
        try:
            if _security.is_active_customer(str(inter.user.id)):
                embed = discord.Embed(
                    title="✅ Already set up",
                    description=(
                        "You're already set up! Use /help to see available commands."
                    ),
                    color=0x57F287,
                    timestamp=datetime.now(timezone.utc),
                )
                embed.set_footer(text="AdFarm V8 · /getstarted")
                await inter.response.send_message(embed=embed, ephemeral=True)
                return
        except Exception as exc:
            print(f"[BOT] /getstarted customer check skipped (DB hiccup): {type(exc).__name__}: {exc}")
    embed = discord.Embed(
        title="🚀 Get Started — V8 Ad Farm Quick-Start Guide",
        description=(
            "Follow this checklist to get your first alt running. Every step is done inside Discord;\n"
            "you should not need to edit code for normal operations."
        ),
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc),
    )
    steps = [
        ("1. Accept the Policy", "Read the pinned policy card in `#open-ticket` and click **✅ I Agree**. This is required before any payment address is shared (money-gate)."),
        ("2. Pay & Activate", "Send BEP-20 USDT/BUSD via Trust Wallet. An admin verifies on BSCScan, then runs `/admin activate @You days:30 alts:N` to create your private forum and repos."),
        ("3. Run /setup", "Type `/setup` in your `#control` thread. Enter your alt tokens and channel IDs — the wizard validates each one before moving to the next. Need help finding your token? See the text guide in `docs/SETUP_GUIDE.md` (section 9)."),
        ("4. Launch your farm", "Run `/run` — pick your alt, mode (Sell/Buy), interval (3/5 min), runtime (6–48h or ∞ Limitless), enter your ad text, and click **Confirm Launch**."),
        ("5. Monitor", "Check `#dashboard` for live status (updated every 5 min), `#farm-logs` for action logs, and `#deals` for arbitrage alerts. Use `/status` for a quick overview."),
        ("6. Tune on the fly", "Use `/tune alt:1 price:2.50` to change your rate, `/tune alt:1 message:New text` to change your ad copy, or `/channels` to add/replace trading channels."),
        ("7. Alt banned?", "Don't panic — the bot auto-detects bans, posts to your `#control` thread with time credit info, and offers a one-click re-setup button for the replacement alt."),
        ("8. Renew", "Use `/renew` before your subscription expires (you'll get DM reminders at 7d, 3d, 1d). An admin confirms payment and extends your farm."),
        ("Docs", "See `SETUP_GUIDE.md` (customer A-to-Z), `SETUP_CONTROL.md` (admin A-to-Z), `SKILL.md` (full reference), `V8_RUNBOOKS.md` (runbooks), and `ROADMAP.md` (commitment artifact)."),
    ]
    for name, value in steps:
        embed.add_field(name=name, value=value, inline=False)
    embed.set_footer(text="Tip: running most commands without parameters opens a friendly visual form.")
    await inter.response.send_message(embed=embed, ephemeral=True)


def _script_result_embed(label: str, result: dict) -> discord.Embed:
    timed = bool(result.get("timed_out"))
    code = int(result.get("code") or 0)
    ok = not timed and code in (0, None)
    color = 0x57F287 if ok else (0xFEE75C if timed else 0xED4245)
    embed = discord.Embed(title=label, color=color, timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Exit Code", value=f"`{code}`", inline=True)
    embed.add_field(name="Timed Out", value="yes" if timed else "no", inline=True)
    embed.add_field(name="Elapsed", value=f"`{float(result.get('elapsed') or 0):.2f}s`", inline=True)
    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")
    def field_value(text: str) -> str:
        if not text:
            return "_empty_"
        # Discord embed fields are limited to 1024 characters. The complete
        # unfiltered stream is attached by _run_script_sandbox below.
        clipped = text[-900:]
        return f"```\n{clipped}\n```"[:1024]
    embed.add_field(name="stdout (unfiltered; full stream attached when long)", value=field_value(stdout), inline=False)
    embed.add_field(name="stderr / errors (unfiltered; full stream attached when long)", value=field_value(stderr), inline=False)
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
    embed = _script_result_embed(f"{'▶️' if execute else '🧪'} Script {label}", result)
    files = []
    for stream_name in ("stdout", "stderr"):
        stream = str(result.get(stream_name) or "")
        if len(stream) > 900:
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
    except Exception as _ignored_exc:
        print(f"[BOT] _run_script_sandbox: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)


@bot.tree.command(name="script", description="Scripting — simulate dry-runs or execute Python in a sandbox")
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
    if not await _check_perms(inter, role="vip", command="script"):
        return
    await _run_script_sandbox(inter, code, execute=(action == "run"), label=action)


@bot.tree.command(name="shutdown", description="Shutdown — stop all alts and terminate the bot (requires SHUTDOWN)")
@app_commands.describe(confirmation="Type SHUTDOWN to confirm")
async def cmd_shutdown(inter: discord.Interaction, confirmation: str):
    if not await _check_perms(inter, role="customer", command="shutdown"):
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
    except Exception as _ignored_exc:
        print(f"[BOT] cmd_shutdown: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)


@bot.tree.command(name="stop", description="Stop Ad Run — sends stop command via Gist queue and cancels the GitHub Actions workflow (~30-45s).")
@app_commands.describe(alt="Target alt ID to stop")
async def cmd_stop(inter: discord.Interaction, alt: int):
    if not await _check_perms(inter, role="customer", command="stop"):
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


@bot.tree.command(name="pause", description="Pause Posting — temporarily halt ad delivery on all channels without stopping the GitHub runner.")
@app_commands.describe(alt="Target alt ID to pause (or choose specific alt)")
async def cmd_pause(inter: discord.Interaction, alt: int):
    if not await _check_perms(inter, role="customer", command="pause"):
        return
    await _finish_dm_control(inter, alt, "!pause", "pause requested")


@bot.tree.command(name="resume", description="Resume Posting — unpause ad delivery and restore the regular posting schedule.")
@app_commands.describe(alt="Target alt ID to resume (or choose specific alt)")
async def cmd_resume(inter: discord.Interaction, alt: int):
    if not await _check_perms(inter, role="customer", command="resume"):
        return
    await _finish_dm_control(inter, alt, "!resume", "resume requested")


@bot.tree.command(name="alt", description="Alt Manager — add/update/remove alts, view typed logs, check workflow runs, run self-check.")
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
        app_commands.Choice(name="👥 Overview — Open visual fleet management hub", value="overview"),
        app_commands.Choice(name="➕ Add Alt — Connect a new alt account via token", value="add"),
        app_commands.Choice(name="✏️ Update Alt — Change name, repo, or token", value="update"),
        app_commands.Choice(name="🗑️ Remove Alt — Disconnect an alt account", value="remove"),
        app_commands.Choice(name="📜 View Logs — Stream typed action logs for an alt", value="logs"),
        app_commands.Choice(name="🧹 Clear Logs — Purge local log buffer for an alt", value="clearlogs"),
        app_commands.Choice(name="⏱️ Recent Runs — View recent GitHub Actions runs", value="runs"),
        app_commands.Choice(name="🔍 Self-Check — Run pre-flight validation on an alt", value="selfcheck"),
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
    if not await _check_perms(inter, role="customer", command="alt"):
        return

    # V8 bug-fix (plan #4): /alt must only ever show alts that belong to the
    # customer running the command — never other customers' or the admins'
    # fleet alts. Ownership is resolved from the invoker's discord_id.
    is_admin, visible = _visible_alt_ids(inter.user.id)

    if action != "add" and not is_admin and not visible:
        # Activated but nothing connected yet: explain what THEIR account has
        # (provisioned repos from the customer record) and point at /setup.
        embed = discord.Embed(
            title="👥 Your Alt Accounts",
            description="You have no alt accounts connected yet, so there is nothing to show.\n\n",
            color=0x5865F2,
            timestamp=datetime.now(timezone.utc),
        )
        if _V8_LOADED:
            try:
                cust = _cm.get_customer(str(inter.user.id))
            except Exception:
                cust = None
            if cust:
                repos = [str(r) for r in (cust.get("repos") or []) if str(r).strip()]
                plan_alts = int(cust.get("alt_count") or 0) or len(repos)
                if repos:
                    owner = str(cust.get("github_account") or "").strip()
                    embed.description += (
                        f"Your plan includes **{plan_alts}** alt(s). Provisioned "
                        f"repo(s){f' on `{owner}`' if owner else ''}:\n"
                        + "\n".join(f"• `{r}`" for r in repos[:10])
                        + "\n\n"
                    )
                elif plan_alts:
                    embed.description += (
                        f"Your plan includes **{plan_alts}** alt(s), but no repos "
                        "are provisioned yet — contact an admin.\n\n"
                    )
        embed.description += (
            "👉 Run `/setup` to connect your alt token(s). Once connected, your "
            "alts appear here — and only yours."
        )
        embed.set_footer(text="For privacy, /alt never shows other members' alts.")
        return await inter.response.send_message(embed=embed, ephemeral=True)

    if alt is not None and alt not in visible:
        return await inter.response.send_message(
            "❓ That alt does not exist or does not belong to your account.",
            ephemeral=True,
        )

    target_aid = alt if (alt and alt in visible) else (visible[0] if visible else 1)

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

    # Default: Interactive Alt Hub View (filtered to the caller's own alts)
    view = AltControlHubView(
        owner_id=inter.user.id,
        selected_alt=target_aid,
        visible_alts=None if is_admin else visible,
    )
    embed = view._render_embed()
    await inter.response.send_message(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="tune", description="Live Tuning — change price, ad copy, mode, interval, runtime, or images")
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
    if not await _check_perms(inter, role="customer", command="tune"):
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
                except Exception as _ignored_exc:
                    # Cached channels are still useful when a transient API
                    # call fails; the caller logs the exact partial inventory.
                    print(f"[BOT] _fetch_control_server_catalogue: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
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


async def _reconcile_control_channels(alt_id: int, *, reason: str, configured_ids=None) -> dict:
    """Refresh durable channel inventory from the control bot's Discord view."""
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
    )
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


def _snapshot_channel_table(alt_id: int) -> tuple[list[str], dict[str, str]]:
    """Current (ids, names) channel table of an alt — the rollback baseline."""
    alt_obj = state.get(alt_id)
    old_ids = list(alt_obj.channels.keys()) if alt_obj else []
    old_names = {
        str(key): str(raw.get("name") or "")
        for key, raw in (alt_obj.channels.items() if alt_obj else [])
        if isinstance(raw, dict)
    }
    return old_ids, old_names


def _rollback_capable_update(alt_id: int, apply_fn: Callable[[], Any]):
    """Build the ``update`` callback used by every mutating /channels action.

    Local mutation → secret persistence; if the secret push fails, the local
    table is rolled back so Discord state and the repository secret can never
    diverge. Single source of the pattern previously copy-pasted three times
    (V8 manager cleanup).
    """
    old_ids, old_names = _snapshot_channel_table(alt_id)

    async def _update():
        result = apply_fn()
        if result is False:
            raise RuntimeError("local channel state could not be persisted")
        persist_ok, persist_msg = await _persist_channels_for_alt(alt_id)
        if not persist_ok:
            state.replace_channels(alt_id, old_ids, old_names)
            raise RuntimeError(persist_msg)
        return True

    return _update


async def _channels_action_list(inter: discord.Interaction, alt_id: int) -> None:
    a_obj = state.get(alt_id)
    if not a_obj:
        return await _ephemeral_reply(inter, "❓ Unknown alt.")
    rows = [f"`{cid}` {raw.get('name', '')}".rstrip() for cid, raw in list(a_obj.channels.items())[:100]]
    text = f"**Channel table for {a_obj.name}** (`{len(a_obj.channels)}`):\n```\n" + "\n".join(rows) + "\n```"
    return await inter.response.send_message(text[:4000], ephemeral=True)


async def _channels_action_remove(inter: discord.Interaction, alt_id: int, cid: str) -> None:
    a_obj = state.get(alt_id)
    if not a_obj or cid not in a_obj.channels:
        return await _ephemeral_reply(inter, f"❌ Channel `{cid}` is not in Alt {alt_id}'s table.")
    old_ids, old_names = _snapshot_channel_table(alt_id)
    remaining = [item for item in old_ids if item != cid]
    await _finish_dm_control(
        inter, alt_id, f"!setchannels {','.join(remaining) or 'clear'}",
        f"channel `{cid}` removal queued",
        update=_rollback_capable_update(
            alt_id,
            lambda: state.replace_channels(alt_id, remaining, old_names),
        ),
    )
    await _log_control(
        f"🗑️ Removed channel `{cid}` from Alt {alt_id} ({a_obj.name}); exact prior target count={len(old_ids)}."
    )


async def _channels_action_overwrite(inter: discord.Interaction, alt_id: int, cids: list[str]) -> None:
    a_obj = state.get(alt_id)
    if not a_obj:
        return await _ephemeral_reply(inter, "❓ Unknown alt.")
    old_ids, _ = _snapshot_channel_table(alt_id)
    await _finish_dm_control(
        inter, alt_id, f"!setchannels {','.join(cids)}",
        f"channel table overwrite queued for `{len(cids)}` target(s)",
        update=_rollback_capable_update(alt_id, lambda: state.replace_channels(alt_id, cids)),
    )
    await _log_control(
        f"♻️ Alt {alt_id} ({a_obj.name}) channel table overwrite requested: "
        f"old=[{','.join(old_ids) or 'none'}] new=[{','.join(cids)}]."
    )


async def _channels_action_refresh(inter: discord.Interaction, alt_id: int, action: str) -> None:
    current_ids = list(state.get(alt_id).channels.keys()) if state.get(alt_id) else []

    async def _refresh_and_persist():
        result = await _reconcile_control_channels(
            alt_id,
            reason="refresh" if action == "refresh" else "rescan",
            configured_ids=current_ids or None,
        )
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "live channel inventory unavailable"))
        return True

    return await _finish_dm_control(
        inter, alt_id, "!rescan",
        "live channel inventory refreshed and durable registry reconciled",
        update=_refresh_and_persist,
    )


async def _channels_action_add(inter: discord.Interaction, alt_id: int, cid: str, label: str) -> None:
    return await _finish_dm_control(
        inter, alt_id, f"!setchannel {cid}{(' ' + label) if label else ''}",
        f"channel ID queued for remote validation: `{cid}`",
        update=_rollback_capable_update(alt_id, lambda: state.set_channel(alt_id, cid, label)),
    )


async def _channels_action_replace(
    inter: discord.Interaction, alt_id: int, old_id: str, new_id: str, label: str
) -> None:
    return await _finish_dm_control(
        inter, alt_id, f"!replacechannel {old_id} {new_id}{(' ' + label) if label else ''}",
        f"channel replacement queued for remote validation: `{old_id}` → `{new_id}`",
        update=_rollback_capable_update(alt_id, lambda: state.replace_channel(alt_id, old_id, new_id, label)),
    )


async def _channels_action_reset_caution(inter: discord.Interaction, alt_id: int, cid: str) -> None:
    specific = bool(cid and cid.lower() != "all")
    target_cmd = f"!resetcaution {cid}" if cid else "!resetcaution all"
    label = f"reset caution on channel {cid}" if specific else "reset caution on all channels"
    return await _finish_dm_control(
        inter, alt_id, target_cmd, label,
        update=lambda: state.reset_caution(alt_id, cid if specific else None),
    )


def _parse_channel_overwrite_list(raw: str) -> tuple[Optional[list[str]], str]:
    """Validate the comma-separated overwrite input → ``(cids, error_text)``.

    Accepts both ASCII and full-width commas (users paste from Discord).
    ``error_text`` is empty on success.
    """
    cids = [x.strip() for x in (raw or "").replace(",", ",").split(",") if x.strip()]
    if not cids or not all(x.isdigit() for x in cids):
        return None, "❌ Every channel ID must be numeric."
    # V8 bug-fix M: the per-alt cap is 10 channels — enforced on overwrite too.
    if len(cids) > _MAX_CHANNELS_PER_ALT:
        return None, _channel_limit_message(_MAX_CHANNELS_PER_ALT)
    return cids, ""


@bot.tree.command(name="channels", description="Channel Manager — add, replace, remove, or refresh trading channels")
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
    """Channel Manager — validates input here, delegates to focused actions.

    V8 manager cleanup: the 163-line mega-handler was decomposed per action and
    the triple-duplicated "mutate → persist → rollback" closure became
    :func:`_rollback_capable_update`.
    """
    if not await _check_perms(inter, role="customer", command="channels"):
        return

    chosen_alt = alt if (alt and alt in state.alt_ids) else (state.alt_ids[0] if state.alt_ids else 1)

    if action == "list":
        return await _channels_action_list(inter, chosen_alt)

    if action == "remove":
        if not channel_id or not channel_id.strip().isdigit():
            return await _ephemeral_reply(inter, "❌ A numeric `channel_id` is required to remove.")
        return await _channels_action_remove(inter, chosen_alt, channel_id.strip())

    if action == "overwrite":
        raw = (channel_id or "").strip()
        if not raw:
            return await _ephemeral_reply(
                inter,
                "❌ Provide a comma-separated list of channel IDs, e.g. `/channels alt:1 action:overwrite channel_id:111,222,333`.",
            )
        cids, err = _parse_channel_overwrite_list(raw)
        if cids is None:
            return await _ephemeral_reply(inter, err)
        return await _channels_action_overwrite(inter, chosen_alt, cids)

    if action in {"refresh", "rescan"}:
        return await _channels_action_refresh(inter, chosen_alt, action)

    if action == "add":
        if not channel_id or not channel_id.strip().isdigit():
            return await _ephemeral_reply(inter, "❌ Valid numeric `channel_id` is required to add a channel.")
        cid = channel_id.strip()
        label = re.sub(r"[\r\n]", " ", (name or "").strip())[:80]
        a_obj = state.get(chosen_alt)
        old_ids = list(a_obj.channels.keys()) if a_obj else []
        # V8 bug-fix M: adding past the 10-channel per-alt cap is rejected.
        if len(old_ids) >= _MAX_CHANNELS_PER_ALT:
            return await _ephemeral_reply(inter, _channel_limit_message(_MAX_CHANNELS_PER_ALT))
        return await _channels_action_add(inter, chosen_alt, cid, label)

    elif action == "replace":
        if not channel_id or not new_channel_id or not channel_id.strip().isdigit() or not new_channel_id.strip().isdigit():
            return await _ephemeral_reply(inter, "❌ Both `channel_id` (old) and `new_channel_id` must be numeric.")
        label = re.sub(r"[\r\n]", " ", (name or "").strip())[:80]
        return await _channels_action_replace(
            inter, chosen_alt, channel_id.strip(), new_channel_id.strip(), label
        )

    elif action == "reset_caution":
        cid = (channel_id or "").strip()
        if cid and not cid.isdigit() and cid.lower() != "all":
            return await _ephemeral_reply(inter, "❌ Channel ID must contain digits only, or leave blank / pass 'all'.")
        return await _channels_action_reset_caution(inter, chosen_alt, cid)

    # Default: Interactive Channels UI
    view = ChannelsView(owner_id=inter.user.id, alt_id=chosen_alt)
    embed = view._build_embed()
    await inter.response.send_message(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="deals", description="Deal Scanner — configure marketplace arbitrage keywords and alerts")
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
    if not await _check_perms(inter, role="customer", command="deals"):
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


@bot.tree.command(name="squad", description="Fleet Squads — group alts into named teams for batch pause/resume/price/policy operations.")
@app_commands.describe(
    action="Squad action (leave blank to open interactive hub)",
    squad_name="Squad name (e.g. 'Alpha', 'Sellers', 'Night Patrol')",
    alt="Target alt ID (1-4)",
    value="Value for squad batch operations (e.g. preset name 'stealth' or price '2.40')",
)
@app_commands.choices(
    action=[
        app_commands.Choice(name="👥 Squad Hub - Open visual squad control hub", value="overview"),
        app_commands.Choice(name="📋 List Squads - Show all squad teams and members", value="list"),
        app_commands.Choice(name="📊 Team Status - View composite squad health and statistics", value="view"),
        app_commands.Choice(name="➕ Assign Alt - Add or move an alt to a squad team", value="assign"),
        app_commands.Choice(name="⏸️ Pause Squad - Batch pause ads across all squad alts", value="pause"),
        app_commands.Choice(name="▶️ Resume Squad - Batch resume ads across all squad alts", value="resume"),
        app_commands.Choice(name="🛡️ Apply Policy - Apply safety preset (stealth/aggressive) to squad", value="policy"),
        app_commands.Choice(name="💰 Update Price - Set unit price rate across all squad alts", value="price"),
    ]
)
async def cmd_squad(
    inter: discord.Interaction,
    action: Optional[Literal["overview", "list", "view", "assign", "pause", "resume", "policy", "price"]] = "overview",
    squad_name: Optional[str] = None,
    alt: Optional[int] = 0,
    value: Optional[str] = None,
):
    if not await _check_perms(inter, role="vip", command="squad"):
        return

    if action in (None, "overview", "list") and not squad_name:
        view = SquadControlView(owner_id=inter.user.id, current_squad=squad_name or "Alpha")
        embed = view._render_embed()
        return await inter.response.send_message(embed=embed, view=view, ephemeral=True)

    elif action == "assign":
        if not alt or not squad_name:
            return await inter.response.send_message("❌ Both `alt` and `squad_name` are required for assignment.", ephemeral=True)
        if alt not in state.alt_ids:
            return await inter.response.send_message("❌ Invalid alt specified.", ephemeral=True)
        state.set_squad(alt, squad_name)
        return await inter.response.send_message(f"✅ Alt {alt} assigned to squad **{squad_name}**.", ephemeral=True)

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
                await asyncio.sleep(random.uniform(0.25, 1.25))
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
            return f"• **Alt {member.alt_id}** ({member.name}): {ack}"

        results = await asyncio.gather(
            *(dispatch_member(index, member) for index, member in enumerate(members)),
            return_exceptions=False,
        )
        summary = f"👥 **Squad '{squad_name}' Batch {action.upper()}** ({len(members)} alts, staggered):\n" + "\n".join(results)
        await inter.followup.send(summary, ephemeral=True)
        await _log_control(summary)
        return


@bot.tree.command(name="status", description="Live Status — fleet-wide dashboard or single-alt diagnostic card")
@app_commands.describe(alt="Target alt (or 0 for All alts)")
async def cmd_status(inter: discord.Interaction, alt: Optional[int] = 0):
    if not await _check_perms(inter, role="customer", command="status"):
        return
    await _fresh_state()
    if alt == 0:
        await inter.response.send_message(embeds=build_all(state)[:10], ephemeral=True)
    else:
        await inter.response.send_message(embed=build_single_alt_embed(state, alt or 1), ephemeral=True)


@bot.tree.command(name="reply", description="DM Relay — send a message through an alt account directly to a buyer's DM.")
@app_commands.describe(alt="Alt ID to send from", user="Buyer Discord User ID (from #dm-inbox)", text="Message text to send (leave blank for multiline editor)")
async def cmd_reply(inter: discord.Interaction, alt: Optional[int] = 1, user: Optional[str] = "", text: Optional[str] = None):
    if not await _check_perms(inter, role="customer", command="reply"):
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


# V8: /analytics and /diagnose removed per V8_PLAN.md Phase 3.5
# These commands are no longer available to customers or admins.


# V8: /canary, /topology, and /sync removed per V8_PLAN.md Phase 3.5.


@bot.tree.command(name="refresh", description="Force Refresh — instantly poll latest GitHub workflow states and update the dashboard.")
async def cmd_refresh(inter: discord.Interaction):
    if not await _check_perms(inter, role="customer", command="refresh"):
        return
    await inter.response.defer(ephemeral=True)
    await _fresh_state()
    await _refresh_dashboard_now()
    await inter.followup.send("✅ GitHub heartbeat state and dashboard refreshed from current data.", ephemeral=True)


@bot.tree.command(name="dashboard", description="Dashboard — post a fresh live status snapshot (health, sent/errors, channels, deals) to #dashboard.")
async def cmd_dashboard(inter: discord.Interaction):
    if not await _check_perms(inter, role="customer", command="dashboard"):
        return
    await inter.response.defer(ephemeral=True)
    await _fresh_state()
    msg = await _post_dashboard(build_all(state))
    await inter.followup.send(f"✅ Dashboard snapshot posted → {msg.jump_url if msg else '(failed)'}", ephemeral=True)


_COMMAND_GUIDE = {
    # ── V8 Customer Commands ──
    "setup": (
        "`/setup`",
        "V8 Setup Wizard — enter your alt tokens and trading channel IDs. "
        "Step 1 asks how many alts (1–4). Step 2 opens one modal per alt "
        "(token + channels), each validated against Discord before the next. "
        "Tokens are uploaded to GitHub secrets and cleared from memory."
    ),
    "run": (
        "`/run`",
        "Launch an ad run — interactive 3-step launcher: (1) choose alt, mode "
        "(Sell/Buy), interval (3/5 min), runtime (6–48h or ∞ Limitless); "
        "(2) enter ad text, optional rate, image toggle; (3) review preview "
        "and click Confirm Launch. Dispatches `send_ads.yml` to GitHub "
        "Actions. ∞ Limitless auto-renews every 48h while subscription is active."
    ),
    "stop": (
        "`/stop alt:<id>`",
        "Stop an ad run — sends `!stop` through the Gist control queue, "
        "syncs the variation blocklist, and cancels the active GitHub "
        "Actions workflow. Takes ~30–45s (documented SLA)."
    ),
    "pause": (
        "`/pause alt:<id>`",
        "Temporarily pause public ad delivery on all target channels "
        "without terminating the GitHub Actions runner."
    ),
    "resume": (
        "`/resume alt:<id>`",
        "Resume public ad delivery from a paused state."
    ),
    "status": (
        "`/status [alt:<id|0>]`",
        "Show fleet-wide dashboard (alt:0 or omitted) or a single-alt "
        "status card. Displays health index, workflow state, sent/error "
        "counts, per-channel slowmode, deal scanner status, and uptime."
    ),
    "tune": (
        "`/tune [alt:<id|0>] [policy:<preset>] [price:<rate>] [mode:<sell|buy>] "
        "[message:<text>] [interval:<3|5>] [runtime:<6–48>] [image:<file>]`",
        "Live fleet tuning — change price, ad copy, mode, interval, runtime, "
        "or image on the fly. Policy presets: stealth (5m, max jitter), "
        "aggressive (3m, high throughput), peak_hour (3m, dynamic cadence), "
        "balanced (5m, standard). Opens interactive UI when called without args."
    ),
    "channels": (
        "`/channels [alt:<id>] [action:<view|list|add|replace|remove|overwrite|"
        "refresh|rescan|reset_caution>] [channel_id:<id>] [new_channel_id:<id>] "
        "[name:<label>]`",
        "Channel manager — add/replace/remove trading channels, overwrite "
        "the full list, rescan permissions, or reset caution mode. "
        "Opens visual manager UI when called without args."
    ),
    "deals": (
        "`/deals [alt:<id|0>] [enabled:<on|off>] [min_delta:<rate>] "
        "[keywords:<items>] [sample_listing:<text>]`",
        "Marketplace arbitrage scanner — passively reads messages the bot "
        "already fetches and alerts when someone sells below your price "
        "(🟢 SUPPLIER ALERT) or buys above it (🔵 ARBITRAGE SALE). "
        "Configure keywords, minimum profit edge, and test-parse listings."
    ),
    "squad": (
        "`/squad [action:<overview|list|view|assign|pause|resume|policy|price>] "
        "[squad_name:<name>] [alt:<id>] [value:<value>]`",
        "Fleet squads — group alts into named teams for batch operations. "
        "Batch pause/resume, batch price updates, batch policy presets. "
        "Opens interactive squad hub when called without args."
    ),
    "alt": (
        "`/alt [action:<overview|add|update|remove|logs|clearlogs|runs|selfcheck>] "
        "[alt:<id>] [confirmation:DELETE] [limit:<1-50>] "
        "[kind:<ALL|ERROR|DEAL|CONTROL|CHANNEL|CAUTION|DEBUG>] [search:<text>]`",
        "Alt account lifecycle — add new alts (auto-provisions GitHub repo, "
        "uploads workflows, sets secrets), update name/token/repo, remove "
        "with confirmation, view typed logs, check workflow runs, or run "
        "pre-flight self-check. Opens fleet management hub without args."
    ),
    "reply": (
        "`/reply alt:<id> user:<buyer_id> text:<message>`",
        "Operator DM relay — send a message through an alt account "
        "directly into a buyer's DM. Opens a multiline editor modal "
        "when `text` is omitted."
    ),
    "renew": (
        "`/renew`",
        "Open a renewal ticket — pre-filled with your customer ID and "
        "days remaining. Posts to the #open-ticket channel for admin "
        "payment verification and `/admin extend`."
    ),
    "pause-billing": (
        "`/pause-billing`",
        "Request a subscription pause — opens a ticket for admin review. "
        "If approved, your subscription is extended by the paused days."
    ),
    "proofs": (
        "`/proofs`",
        "Opt-in proof sharing — post first-post screenshots or supplier "
        "alert wins to the public #proofs channel with your customer "
        "ID redacted."
    ),
    # ── VIP Commands ──
    "vip": (
        "`/vip autoreply [message:<text>|off]`",
        "VIP DM auto-reply — set a custom message that is automatically "
        "relayed to buyers who DM your alt(s) (max once per 30 min per "
        "buyer, while your farm runner is active). `message:off` disables "
        "it; run without arguments to view the current setting."
    ),
    # ── Admin / Operator Commands ──
    "admin": (
        "`/admin <subcommand>`",
        "Admin panel (OWNER_IDS only). Subcommands: "
        "list (show all customers), activate (onboard customer with repos/forum/DB), "
        "extend (add subscription days), deactivate (shut down and lock), "
        "shutdown (emergency kill-switch, 2-admin multi-sig), "
        "repos (list every repo across all worker accounts), "
        "repo sync (push code to all repos) / repo delete (delete one repo, confirm:DELETE), "
        "expiry-alerts (dry-run reminders), pin-policy (pin ToS in #open-ticket), "
        "activate-template (pre-filled activation), payment-address (share wallet, gated on policy ack), "
        "verify-tokens (write-proof + expiry health), logs (link to #farm-logs)."
    ),
    # ── System Commands ──
    "getstarted": (
        "`/getstarted`",
        "Quick-start guide — step-by-step checklist: add an alt, add "
        "channels, self-check, tune rates, launch, monitor, stop. "
        "Includes documentation links."
    ),
    "script": (
        "`/script action:<simulate|run> code:<python>`",
        "Sandboxed scripting — `simulate` dry-runs with unfiltered "
        "stdout/stderr; `run` executes in a resource-limited sandbox. "
        "Useful for debugging or quick calculations."
    ),
    "shutdown": (
        "`/shutdown confirmation:SHUTDOWN`",
        "Graceful shutdown — stops all alts, cancels workflows, "
        "terminates the control bot. Requires typing `SHUTDOWN` to confirm."
    ),
    "refresh": (
        "`/refresh`",
        "Force-refresh — poll latest GitHub Actions workflow states "
        "and update the persistent dashboard embed."
    ),
    "dashboard": (
        "`/dashboard`",
        "Post a fresh 3-card dashboard snapshot to #dashboard "
        "without running or scanning ads."
    ),
    "help": (
        "`/help`",
        "This command reference — shows usage, arguments, and "
        "descriptions for every registered command."
    ),
    "reset": (
        "`/reset confirmation:RESET`",
        "FACTORY RESET (admin only) — wipes every customer record, stored "
        "alt credential, run state and the fleet ALT_REPOS/ALT_NAMES mapping "
        "(core secrets included), then cancels active workflows. Use it after "
        "deleting alt repos manually so no ghost alts resurface. Channels and "
        "repos themselves are untouched."
    ),
}


@bot.tree.command(name="help", description="V8 Command Reference — complete guide to all commands, arguments, and features.")
async def cmd_help(inter: discord.Interaction):
    """Role-aware command reference (V8 bug-fix F/I).

    Admins see every command; VIPs see VIP + customer + public commands;
    active customers see customer + public commands; non-customers see only
    /help and /getstarted.  Commands outside the viewer's tier are never
    listed.
    """
    role = viewer_role(inter)
    allowed = commands_for_role(role)  # None → no tier filtering (degraded V8)

    registered = {cmd.name: cmd for cmd in bot.tree.get_commands()}

    # Categorize commands for V8-aware help (top-level names only)
    categories = [
        ("🚀 Getting Started", ["getstarted", "setup", "run", "help"]),
        ("⚙️ Ad Farm Controls", ["stop", "pause", "resume", "status", "tune"]),
        ("📌 Channels & Deals", ["channels", "deals", "squad", "reply"]),
        ("👥 Alt Management", ["alt"]),
        ("⭐ VIP Features", ["vip"]),
        ("💳 Billing & Proofs", ["renew", "pause-billing", "proofs"]),
        ("🔧 Admin Panel", ["admin", "reset"]),
        ("🖥️ System", ["script", "shutdown", "refresh", "dashboard"]),
    ]

    pages = []
    for cat_name, cmd_names in categories:
        embed = discord.Embed(
            title=f"{cat_name}",
            color=0x5865F2,
            timestamp=datetime.now(timezone.utc),
        )
        for name in cmd_names:
            if allowed is not None and name not in allowed:
                continue  # hide commands above the viewer's tier (bug-fix F)
            if name not in registered:
                continue
            cmd_obj = registered[name]
            usage, effect = _COMMAND_GUIDE.get(
                name, (f"`/{name}`", cmd_obj.description or "No description available.")
            )
            # Truncate to fit embed field limits
            effect_short = effect[:300] + ("…" if len(effect) > 300 else "")
            embed.add_field(name=f"`/{name}`", value=effect_short, inline=False)
        if embed.fields:
            pages.append(embed)

    # Build paginated output
    total = len(pages)
    for i, page in enumerate(pages):
        page.set_footer(
            text=f"📖 Page {i + 1}/{total} · 💡 Most commands open a visual UI when run without args!"
        )
        if i == 0:
            # First page gets an intro embed
            intro = discord.Embed(
                title="📖 AdFarm V8 — Complete Command Reference",
                description=(
                    "**AdFarm V8** is a managed 24/7 ad farm for Roblox trading-game "
                    "Discord servers. You bring the alt accounts and channel IDs; we "
                    "handle the runners, anti-detection, deal scanning, and safety.\n\n"
                    "**How it works:** `/setup` → `/run` → farm posts 24/7 → "
                    "auto-renews every 48h while your subscription is active.\n\n"
                    "**Need help?** Run `/getstarted` for a step-by-step guide, "
                    "or contact an admin in `#open-ticket`.\n\n"
                    "**Channels matter:** commands are channel-aware — customer "
                    "commands run in your forum rooms (`#control`, `#dashboard`, …), "
                    "VIP commands in `#dm-inbox`, `/admin` in the admin channels; "
                    "public rooms like `#announcements` only host `/help` and "
                    "`/getstarted`."
                ),
                color=0x5865F2,
                timestamp=datetime.now(timezone.utc),
            )
            intro.set_footer(text=f"📖 Page 0/{total} · AdFarm V8 · 2026-09-03")
            await inter.response.send_message(embed=intro, ephemeral=True)
            await inter.followup.send(embed=page, ephemeral=True)
        else:
            await inter.followup.send(embed=page, ephemeral=True)


# Add autocomplete helpers
def make_alt_autocompleter(command_name: str = ""):
    async def _autocompleter(inter: discord.Interaction, current: str):
        try:
            cur = str(current or "").strip().lower()
            out = []
            # V8 bug-fix (plan #4): suggest only the alts visible to the
            # invoking user — customers never see other operators' alt names.
            _is_adm, visible = _visible_alt_ids(inter.user.id)
            if command_name in ("status", "deals", "tune"):
                out.append(app_commands.Choice(name="All alts (0)", value=0))
            for i in visible:
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
    ("reply", cmd_reply),
    # V8: analytics, diagnose, canary removed
):
    try:
        command.autocomplete("alt")(make_alt_autocompleter(command_name))
    except Exception as _ignored_exc:
        print(f"[BOT] <module>: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)

try:
    cmd_squad.autocomplete("squad_name")(make_squad_autocompleter())
except Exception as _ignored_exc:
    print(f"[BOT] <module>: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)

for param_name in ("channel_id", "new_channel_id"):
    try:
        cmd_channels.autocomplete(param_name)(make_channel_autocompleter())
    except Exception as _ignored_exc:
        print(f"[BOT] <module>: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)


# ──────────────────────────────────────────────────────────────────────────────
# V8 Customer-Facing Slash Commands
# ──────────────────────────────────────────────────────────────────────────────

def _validate_discord_token_sync(token: str) -> tuple[bool, str]:
    """Validate an alt token against Discord /users/@me (blocking; run in thread)."""
    import urllib.error
    import urllib.request
    import json as _json
    try:
        req = urllib.request.Request(
            "https://discord.com/api/v10/users/@me",
            headers={"Authorization": token.strip()},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            _data = _json.loads(r.read())
            username = f"{_data.get('username', '?')}#{_data.get('discriminator', '0')}"
        return True, username
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False, f"invalid token (HTTP {exc.code})"
        return False, f"validation blocked (HTTP {exc.code})"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _valid_channel_ids(raw: str) -> tuple[bool, list[str]]:
    parts = [p.strip() for p in (raw or "").split(",") if p.strip()]
    clean = list(dict.fromkeys(parts))
    if not clean:
        return False, []
    if len(clean) > _MAX_CHANNELS_PER_ALT:  # V8 bug-fix M: 10 channels per alt
        return False, []
    if not all(p.isdigit() and 10 <= len(p) <= 20 for p in clean):
        return False, []
    return True, clean


# V8 bug-fix (plan #3): the _VideoButton view (link to the cancelled 3-min
# token walkthrough video) was removed entirely from the /setup wizard.


class SetupCountModal(discord.ui.Modal, title="Setup — Step 1 of 2"):
    """Step 1: how many alts to configure (clamped to the paid alt_count)."""
    count = discord.ui.TextInput(
        label="How many alts do you want to set up? (1-4)",
        placeholder="e.g. 2",
        max_length=2,
        required=True,
    )

    def __init__(self, max_count: int, owner_id: int):
        super().__init__()
        self._max_count = max_count
        self._owner_id = owner_id

    async def on_submit(self, inter: discord.Interaction) -> None:
        raw = self.count.value.strip()
        try:
            n = int(raw)
        except (TypeError, ValueError):
            n = 0
        if not (1 <= n <= min(4, self._max_count)):
            await inter.response.send_message(
                f"❌ Enter a number between **1 and {min(4, self._max_count)}** "
                f"(your plan includes {self._max_count} alt(s)).",
                ephemeral=True,
            )
            return
        await inter.response.send_message(
            f"✅ **{n} alt(s)** selected. Fill in each alt's token + channels below.",
            ephemeral=True,
        )
        session = SetupSession(owner_id=self._owner_id, total=n)
        await _offer_alt_modal(inter, session, alt_num=1)


class SetupAltModal(discord.ui.Modal, title="Alt Setup — Credentials"):
    """One modal per alt; validates token + channels before moving on (0.5)."""
    token = discord.ui.TextInput(
        label="Alt Token",
        placeholder="Discord user token for this alt",
        style=discord.TextStyle.short,
        max_length=100,
    )
    channels = discord.ui.TextInput(
        label="Channel IDs (comma-separated)",
        placeholder="123456789012345678,987654321012345678",
        style=discord.TextStyle.paragraph,
        max_length=500,
    )

    def __init__(self, alt_num: int, total: int, session: "SetupSession"):
        super().__init__(title=f"Alt {alt_num} of {total} — Setup")
        self._alt_num = alt_num
        self._total = total
        self._session = session

    async def on_submit(self, inter: discord.Interaction) -> None:
        tok = self.token.value.strip()
        # V8 bug-fix M: reject more than MAX_CHANNELS_PER_ALT (10) channels up
        # front with the canonical error before any validation work happens.
        submitted = [p.strip() for p in (self.channels.value or "").split(",") if p.strip()]
        if len(submitted) > _MAX_CHANNELS_PER_ALT:
            await inter.response.send_message(
                _channel_limit_message(_MAX_CHANNELS_PER_ALT),
                ephemeral=True,
            )
            view = _NextAltButton(self._alt_num, self._total, self._session)
            await inter.followup.send(
                "👉 You can re-open this alt with fewer channels:",
                view=view, ephemeral=True,
            )
            return
        ok_chs, ch_ids = _valid_channel_ids(self.channels.value)
        valid_tok, username = await asyncio.to_thread(_validate_discord_token_sync, tok)
        if not ok_chs or not valid_tok:
            problems = []
            if not valid_tok:
                problems.append(f"token invalid ({username})")
            if not ok_chs:
                problems.append(
                    "channel IDs must be comma-separated numeric snowflakes "
                    f"(1-{_MAX_CHANNELS_PER_ALT})"
                )
            await inter.response.send_message(
                f"❌ **Alt {self._alt_num} rejected** — {', '.join(problems)}.\n"
                "Fix the values and click the retry button below.",
                ephemeral=True,
            )
            view = _NextAltButton(self._alt_num, self._total, self._session)
            await inter.followup.send("👉 Retry this alt:", view=view, ephemeral=True)
            return

        self._session.results.append({
            "alt": self._alt_num,
            "token": tok,
            "channels": ch_ids,
            "username": username,
            "valid": True,
        })
        await inter.response.send_message(
            f"✅ **Alt {self._alt_num}** accepted — `{username}` — "
            f"{len(ch_ids)} channel(s) queued.",
            ephemeral=True,
        )
        if self._alt_num < self._total:
            await _offer_alt_modal(inter, self._session, alt_num=self._alt_num + 1)
        else:
            asyncio.create_task(_finalize_setup(inter, self._session))


class SetupSession:
    """Shared state/order for one wizard run."""
    def __init__(self, owner_id: int, total: int):
        self.owner_id = owner_id
        self.total = total
        self.results: list[dict] = []


class _NextAltButton(discord.ui.View):
    """One button per pending alt: opens the modal, then chains to the next."""

    def __init__(self, alt_num: int, total: int, session: SetupSession):
        super().__init__(timeout=300)
        self._alt_num = alt_num
        self._total = total
        self._session = session

    @discord.ui.button(label="🧙 Configure alt now", style=discord.ButtonStyle.success)
    async def _open(self, inter: discord.Interaction, _btn: discord.ui.Button):
        if str(inter.user.id) != str(self._session.owner_id):
            await inter.response.send_message("❌ Not your setup session.", ephemeral=True)
            return
        await inter.response.send_modal(
            SetupAltModal(self._alt_num, self._total, self._session)
        )


async def _offer_alt_modal(inter: discord.Interaction, session: SetupSession, alt_num: int) -> None:
    """Offer the next alt modal via a button (Discord opens one modal per interaction)."""
    view = _NextAltButton(alt_num, session.total, session)
    try:
        await inter.followup.send(
            f"👉 **Alt {alt_num} of {session.total}** — click the button to enter "
            "its token and channels.",
            view=view, ephemeral=True,
        )
    except Exception as _ignored_exc:
        print(f"[BOT] _offer_alt_modal: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)


async def _finalize_setup(inter: discord.Interaction, session: SetupSession) -> None:
    """Upload secrets + credentials, clear token memory, mark complete."""
    uid = str(session.owner_id)
    customer = _cm.get_customer(uid) if _V8_LOADED else None
    results = session.results

    if _V8_LOADED and customer and results:
        owner = customer.get("github_account", "")
        repos = customer.get("repos", [])
        if owner and repos:
            try:
                from github_dispatch import set_repo_secret
                for entry in results:
                    idx = entry.get("alt", 1)
                    if idx <= len(repos) and entry.get("valid"):
                        repo = repos[idx - 1]
                        await asyncio.to_thread(
                            set_repo_secret, owner, repo, "USER_TOKEN", entry["token"]
                        )
                        ch_str = ",".join(entry.get("channels", []))
                        await asyncio.to_thread(
                            set_repo_secret, owner, repo, "CHANNEL_IDS", ch_str
                        )
                        _cm.store_alt_credential(
                            uid, idx, entry["token"], entry.get("channels", []),
                            username=entry.get("username", ""),
                        )
            except Exception as exc:
                try:
                    await inter.followup.send(
                        f"⚠️ GitHub secret upload failed: {exc}", ephemeral=True
                    )
                except Exception as _ignored_exc:
                    print(f"[BOT] _finalize_setup: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
    # 0.8: memory-clear hygiene — tokens must not linger in heap after upload.
    try:
        results.clear()
    except Exception as _ignored_exc:
        print(f"[BOT] _finalize_setup: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
    cm_events = getattr(_cm, "record_event", None)
    if cm_events:
        cm_events(uid, "setup_completed", {"alts": session.total})
    try:
        await inter.followup.send(
            "✅ **Setup complete!** Run `/run` in your `#control` thread to start "
            "your ad farm.",
            ephemeral=True,
        )
    except Exception as _ignored_exc:
        print(f"[BOT] _finalize_setup: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)


@bot.tree.command(
    name="setup",
    description="V8 Setup Wizard: enter your alt tokens and channels to get started.",
)
async def cmd_setup(inter: discord.Interaction) -> None:
    """Customer-only /setup wizard (hybrid modal flow, TODO 0.5).

    Step 1: "How many alts? (1-4)" modal.
    Step 2: one modal per alt (token + channels), each validated before the
            next one is offered.
    """
    if not await _check_perms(inter, role="customer", command="setup"):
        return
    uid = str(inter.user.id)
    if _V8_LOADED:
        customer = _cm.get_customer(uid)
        if not customer:
            await inter.response.send_message(
                "❌ No customer record found. Contact an admin to activate your account.",
                ephemeral=True,
            )
            return
        # 0.6: re-setup owner-ID assertion (only the forum owner may configure).
        try:
            from discord_forum import assert_forum_owner
            if not await assert_forum_owner(inter, customer):
                return
        except Exception as exc:
            print(f"[SETUP] owner assertion skipped: {exc}")
        alt_count = int(customer.get("alt_count", 1) or 1)
    else:
        alt_count = 1
        customer = {}

    await inter.response.send_modal(SetupCountModal(max_count=max(1, alt_count), owner_id=inter.user.id))


# TODO 2.7 — /renew: open a pre-filled ticket
@bot.tree.command(name="renew", description="Open a renewal ticket (pre-filled with your customer ID).")
async def cmd_renew(inter: discord.Interaction) -> None:
    if not await _check_perms(inter, role="customer", command="renew"):
        return
    uid = str(inter.user.id)
    c = _cm.get_customer(uid) if _V8_LOADED else None
    days = ""
    if c:
        from customer_manager import days_remaining
        left = days_remaining(uid)
        days = f"\nDays remaining: **{left:.1f}**" if left is not None else ""
    # V8 bug-fix (plan #2): fall back to the channel persisted by
    # /admin ticket-panel (DB meta) and to a guild name lookup.
    from control_bot.tickets import resolve_ticket_channel_id
    _ticket_id = resolve_ticket_channel_id(inter.guild)
    ticket_ch = str(_ticket_id or "")
    try:
        if ticket_ch:
            ch = bot.get_channel(int(ticket_ch)) or await bot.fetch_channel(int(ticket_ch))
            await ch.send(
                f"🔄 **Renewal request** — customer `{uid}` (`{inter.user.display_name}`){days}\n"
                f"Admin: verify payment and run `/admin extend user:@{inter.user.display_name} days:30`.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        # Also notify admins in #admin-alerts
        alert_ch_id = _os.environ.get("ADMIN_ALERTS_CH_ID", "")
        if alert_ch_id:
            try:
                alert_ch = bot.get_channel(int(alert_ch_id)) or await bot.fetch_channel(int(alert_ch_id))
                await alert_ch.send(
                    f"🎫 **New ticket:** 🔄 Renewal from `{inter.user.display_name}` (`{uid}`){days}\n"
                    f"Check <#{ticket_ch}> for details.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception as _ignored_exc:
                print(f"[BOT] cmd_renew: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
        await inter.response.send_message(
            "✅ Your renewal ticket was opened. An admin will confirm shortly "
            "(best-effort — no SLA).", ephemeral=True,
        )
    except Exception as exc:
        await inter.response.send_message(
            f"⚠️ Could not open ticket channel ({exc}). Contact an admin directly.",
            ephemeral=True,
        )


# TODO 3.4 — /pause-billing: pause + extend by requested days (admin approval)
@bot.tree.command(name="pause-billing", description="Pause billing and extend your subscription (manual admin approval).")
async def cmd_pause_billing(inter: discord.Interaction) -> None:
    if not await _check_perms(inter, role="customer", command="pause-billing"):
        return
    # V8 bug-fix (plan #2): same resolver chain as /renew (env → DB meta →
    # panel channel → guild name lookup).
    from control_bot.tickets import resolve_ticket_channel_id
    ticket_ch = str(resolve_ticket_channel_id(inter.guild) or "")
    uid = str(inter.user.id)
    if ticket_ch:
        try:
            ch = bot.get_channel(int(ticket_ch)) or await bot.fetch_channel(int(ticket_ch))
            await ch.send(
                f"⏸️ **Pause-billing request** — customer `{uid}` (`{inter.user.display_name}`). "
                "Expected admin action: extend subscription by the paused days.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as _ignored_exc:
            print(f"[BOT] cmd_pause_billing: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
    # Also notify admins in #admin-alerts
    alert_ch_id = _os.environ.get("ADMIN_ALERTS_CH_ID", "")
    if alert_ch_id:
        try:
            alert_ch = bot.get_channel(int(alert_ch_id)) or await bot.fetch_channel(int(alert_ch_id))
            await alert_ch.send(
                f"🎫 **New ticket:** ⏸️ Pause-billing from `{inter.user.display_name}` (`{uid}`)\n"
                f"Check <#{ticket_ch}> for details.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as _ignored_exc:
            print(f"[BOT] cmd_pause_billing: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
    await inter.response.send_message(
        "✅ Pause-billing ticket opened. An admin will extend your subscription "
        "by the paused days after confirmation.", ephemeral=True,
    )


# TODO 3.3 — /proofs: opt-in anonymous proof sharing
@bot.tree.command(name="proofs", description="Opt in to post redacted farm proof to the public channel.")
async def cmd_proofs(inter: discord.Interaction) -> None:
    if not await _check_perms(inter, role="customer", command="proofs"):
        return
    from control_bot import proofs
    await inter.response.send_message(
        "🏆 **Proofs are opt-in.** Only first-post screenshots and supplier "
        "alert wins are shared; your customer ID is **redacted** (e.g. `1234…`).",
        view=proofs.ProofsView(), ephemeral=True,
    )


# ──────────────────────────────────────────────────────────────────────────────
# V8 bug-fix plan #2 — /reset: factory-fresh state
# ──────────────────────────────────────────────────────────────────────────────

def _reset_impact_summary() -> str:
    """Count what a factory reset would destroy (for the confirmation prompt)."""
    customers = alt_creds = fleet = 0
    try:
        customers = len(_cm.list_customers(active_only=False))
    except Exception as exc:
        print(f"[RESET] customer count unavailable: {type(exc).__name__}: {exc}")
    try:
        con = _cm._conn()
        with con:
            alt_creds = con.execute("SELECT COUNT(*) FROM alt_credentials").fetchone()[0]
    except Exception as exc:
        print(f"[RESET] credential count unavailable: {type(exc).__name__}: {exc}")
    fleet = len(state.alt_ids)
    return (
        f"• **{customers}** customer record(s) — activations, subscriptions, forum links\n"
        f"• **{alt_creds}** stored alt credential(s) (tokens + channels)\n"
        f"• **{fleet}** fleet alt mapping(s) in ALT_REPOS/ALT_DISCORD_IDS (+ core secrets)\n"
        f"• run-state, reminders, policy acks and the event ledger\n\n"
        "Repos, forums and Discord channels are NOT deleted — only the bot's "
        "memory of them. This cannot be undone from Discord; restore "
        "customers.db from a numbered backup if you change your mind."
    )


@bot.tree.command(
    name="reset",
    description="FACTORY RESET — wipe every customer record and alt mapping (admin only).",
)
@app_commands.describe(confirmation="Type RESET to confirm this irreversible action")
async def cmd_reset(inter: discord.Interaction, confirmation: Optional[str] = "") -> None:
    """Clear all customer data + alt state with a typed confirmation.

    V8 bug-fix plan #2: stale alt mappings survived repo deletions in the DB,
    the in-memory state file and the ALT_REPOS secret. ``/reset`` wipes every
    source in one auditable, confirmed action. Owner-only (fail closed).
    """
    if not await _check_perms(inter, command="reset"):
        return
    if str(confirmation or "").strip().upper() != "RESET":
        await inter.response.send_message(
            "⚠️ **Factory reset armed.** This will erase:\n" + _reset_impact_summary() +
            "\n\nRe-run with `confirmation:RESET` to execute.",
            ephemeral=True,
        )
        return
    await inter.response.defer(ephemeral=True)
    await _log_control(
        f"🧨 **FACTORY RESET initiated by `{inter.user.display_name}`** (`{inter.user.id}`)."
    )
    # 1. Best-effort: stop every runner first so nothing keeps posting against
    #    wiped configuration. Cancellation failures must not block the wipe.
    stop_results = await asyncio.gather(
        *(asyncio.to_thread(github_api.cancel_run, aid) for aid in list(state.alt_ids)),
        return_exceptions=True,
    )
    canceled = sum(1 for r in stop_results if isinstance(r, tuple) and r[0])
    for r in stop_results:
        if isinstance(r, Exception):
            print(f"[RESET] workflow cancel failed (ignored): {type(r).__name__}: {r}")

    # 2. Customers DB (local file + write-through to the Gist).
    counts: dict[str, int] = {}
    try:
        counts = _cm.reset_all_data()
    except Exception as exc:
        await inter.followup.send(
            f"❌ **Reset aborted:** the database could not be cleared ({exc}). "
            "No in-memory or secret changes were made.",
            ephemeral=True,
        )
        return

    # 3. Fleet registry: secrets, in-memory config, live state, persisted file.
    registry_ok, registry_detail = True, "no fleet mapping configured"
    if config.ALT_REPOS or config.ALT_DISCORD_IDS or config.ALT_NAMES:
        registry_ok, registry_detail = await _drop_alts_from_everywhere(
            sorted(set(config.ALT_REPOS) | set(config.ALT_DISCORD_IDS) | set(config.ALT_NAMES))
        )

    # 4. Process-local caches.
    _cooldowns.clear()
    _processed_webhook_ids.clear()
    _unreachable_state_channels.clear()
    if _V8_LOADED:
        try:
            _security.reload_channel_rules()
        except Exception as exc:
            print(f"[RESET] channel rules reload skipped: {type(exc).__name__}: {exc}")

    lines = [
        "🧹 **Factory reset complete.**",
        f"• customers: {counts.get('customers', 0)} · alt credentials: {counts.get('alt_credentials', 0)} · "
        f"runs: {counts.get('run_state', 0)} · events: {counts.get('events', 0)} removed",
        f"• fleet alts un-mapped: {len(list(state.alt_ids))} remaining in live state",
        f"• {canceled} active workflow(s) canceled",
    ]
    if registry_ok:
        lines.append("• ALT_REPOS/ALT_NAMES/ALT_DISCORD_IDS core secrets cleared")
    else:
        lines.append(f"• ⚠️ Core registry secrets NOT cleared ({registry_detail}) — "
                     "update them manually or mappings will return on next boot")
    await inter.followup.send("\n".join(lines), ephemeral=True)
    await _log_control("\n".join(lines))


# ──────────────────────────────────────────────────────────────────────────────
# V8 plan feature #5 — VIP DM Auto-Reply
# ──────────────────────────────────────────────────────────────────────────────

# One auto-reply per (alt, buyer) inside this window so a chatty buyer never
# gets spammed by the relay.
AUTOREPLY_COOLDOWN_SEC = int(_os.environ.get("AUTOREPLY_COOLDOWN_SEC", "1800") or "1800")
_autoreply_last_sent: dict[tuple[int, str], float] = {}

vip_group = app_commands.Group(
    name="vip",
    description="VIP plan features (DM auto-reply, …)",
)


@vip_group.command(
    name="autoreply",
    description="VIP: auto-reply message sent to buyers who DM your alts.",
)
@app_commands.describe(
    message="The auto-reply text — 'off' disables it, leave blank to view the current setting.",
)
async def vip_autoreply(inter: discord.Interaction, message: Optional[str] = None) -> None:
    """Set/clear/view the VIP DM auto-reply (V8 plan feature #5).

    When a buyer DMs one of THIS customer's alts and the DM lands in the
    #dm-inbox, the control bot relays the saved message back to the buyer
    through the alt (via the `!reply` Gist control command) — at most once
    per buyer per cooldown window.
    """
    if not await _check_perms(inter, role="vip", command="vip"):
        return
    uid = str(inter.user.id)
    raw = str(message or "").strip()

    # View current setting
    if not raw:
        current = ""
        if _V8_LOADED:
            try:
                current = _cm.get_autoreply(uid)
            except Exception:
                current = ""
        embed = discord.Embed(
            title="⭐ VIP DM Auto-Reply",
            color=0xFEE75C if current else 0x5865F2,
            timestamp=datetime.now(timezone.utc),
        )
        if current:
            embed.description = (
                f"**Status:** 🟢 Enabled\n**Message:**\n```{current[:1000]}```\n"
                f"Buyers who DM your alts receive this automatically (max once "
                f"per {AUTOREPLY_COOLDOWN_SEC // 60} min per buyer)."
            )
        else:
            embed.description = (
                "**Status:** ⚪ Disabled\n\n"
                "Set it with `/vip autoreply message:Hey! I'm away from my "
                "desk — leave your offer and I'll reply within the hour.`\n"
                "Disable it any time with `/vip autoreply message:off`."
            )
        embed.set_footer(text="Relayed through your alt while its farm runner is active.")
        return await inter.response.send_message(embed=embed, ephemeral=True)

    # Disable
    if raw.lower() in {"off", "disable", "disabled", "none", "stop"}:
        ok = False
        if _V8_LOADED:
            try:
                ok = _cm.set_autoreply(uid, "")
            except Exception as exc:
                return await inter.response.send_message(
                    f"❌ Could not update your auto-reply: {exc}", ephemeral=True
                )
        if not ok:
            return await inter.response.send_message(
                "❌ No customer record found for you — contact an admin.", ephemeral=True
            )
        return await inter.response.send_message(
            "⚪ **VIP auto-reply disabled.** Buyers will no longer receive an "
            "automatic message.", ephemeral=True,
        )

    # Enable / update
    if len(raw) > 1500:
        return await inter.response.send_message(
            f"❌ Auto-reply message is too long ({len(raw)}/1500 characters).",
            ephemeral=True,
        )
    # Sanitize mass mentions — an auto-reply must never ping everyone.
    lowered = raw.lower()
    if "@everyone" in lowered or "@here" in lowered:
        raw = re.sub(r"@(everyone|here)", r"(mention:\1)", raw, flags=re.I)
    ok = False
    if _V8_LOADED:
        try:
            ok = _cm.set_autoreply(uid, raw)
        except Exception as exc:
            return await inter.response.send_message(
                f"❌ Could not save your auto-reply: {exc}", ephemeral=True
            )
    if not ok:
        return await inter.response.send_message(
            "❌ No customer record found for you — contact an admin to be "
            "activated first.", ephemeral=True,
        )
    try:
        _cm.record_event(uid, "vip_autoreply_set", {"chars": len(raw)})
    except Exception as _ignored_exc:
        print(f"[BOT] vip_autoreply: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
    await inter.response.send_message(
        "✅ **VIP auto-reply saved!**\n"
        f"```{raw[:1000]}```\n"
        f"When a buyer DMs your alt(s), this message is relayed back automatically "
        f"(at most once per {AUTOREPLY_COOLDOWN_SEC // 60} min per buyer) while "
        "your farm runner is active.\n"
        "• Change it any time: `/vip autoreply message:<new text>`\n"
        "• Disable it: `/vip autoreply message:off`",
        ephemeral=True,
    )


bot.tree.add_command(vip_group)


# ──────────────────────────────────────────────────────────────────────────────
# End V8 Customer Commands
# ──────────────────────────────────────────────────────────────────────────────

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

    # ---- V8 hooks: payments auto-ack, ban watch, TTFTV ----
    if _V8_LOADED:
        try:
            await _v8_message_hooks(message)
        except Exception as exc:
            print(f"[V8-HOOK] error: {exc}")


async def _v8_message_hooks(message: discord.Message) -> None:
    """Payments (1.1), ban alerts (1.2), TTFTV (1.5) message-level hooks."""
    if not message.guild or message.author.bot:
        return
    ch_id = str(message.channel.id)
    customers = _cm.list_customers(active_only=False)

    # Map the channel to a customer (logs / control / ticket threads).
    matched = None
    is_logs = False
    for c in customers:
        ids = {str(c.get("logs_thread_id", "")): "logs",
               str(c.get("control_thread_id", "")): "control",
               str(c.get("dashboard_thread_id", "")): "dashboard",
               str(c.get("deals_thread_id", "")): "deals",
               str(c.get("forum_id", "")): "forum"}
        if ch_id in ids:
            matched = c
            is_logs = ids[ch_id] == "logs"
            break

    # Ban detection in farm-logs / any customer channel (1.2)
    if matched is not None:
        from control_bot import ban_watch
        if is_logs or ban_watch.detect_ban_events(message.content or ""):
            await ban_watch.handle_ban_message(bot, message, matched)
            return

    # TTFTV: first successful post observed in a customer logs thread (1.5)
    if matched is not None and is_logs:
        from control_bot import metrics
        if re.search(r"(posted|sent=|✅.*(posted|sent)|total_sent|first post)", message.content or "", re.I):
            metrics.note_first_successful_post(matched["discord_id"])

    # Payment TX auto-ack in the ticket area (1.1)
    # V8 bug-fix (plan #2): resolve via env → DB meta → name lookup.
    try:
        from control_bot.tickets import resolve_ticket_channel_id
        _ticket_id = resolve_ticket_channel_id(getattr(message, "guild", None))
    except Exception:
        _ticket_id = None
    ticket_ch = str(_ticket_id or "")
    is_ticket = (ch_id == ticket_ch) if ticket_ch else str(message.channel.name).lower() in ("open-ticket", "tickets")
    if is_ticket:
        from control_bot import payments
        if _cm.get_customer(str(message.author.id)):
            await payments.maybe_auto_ack(message.channel, str(message.author.id), message.content or "")


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
        except Exception as _ignored_exc:
            print(f"[BOT] on_raw_message_edit: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)


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


def _is_dm_inbox_message(message: discord.Message) -> bool:
    """Whether *message* was posted into a buyer-DM inbox destination.

    Candidates (V8 plan feature #5):
      * the global ``DM_INBOX_CH_ID`` channel (when configured),
      * any channel/thread literally named ``dm-inbox``,
      * a customer's private VIP forum dm-inbox thread (``dm_thread_id`` in
        customers.db — only checked for forum threads to stay cheap).
    """
    ch_id = message.channel.id
    if config.DM_INBOX_CH_ID and ch_id == config.DM_INBOX_CH_ID:
        return True
    name = str(getattr(message.channel, "name", "") or "").strip().lower()
    if name in ("dm-inbox", "dm_inbox", "buyer-dms"):
        return True
    if _V8_LOADED and isinstance(message.channel, discord.Thread):
        try:
            for c in _cm.list_customers(active_only=True):
                dm_t = str(c.get("dm_thread_id") or "").strip()
                if dm_t and dm_t != "0" and dm_t == str(ch_id):
                    return True
        except Exception as _ignored_exc:
            print(f"[BOT] _is_dm_inbox_message: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
    return False


def _is_alt_self_echo(message: discord.Message) -> bool:
    """True when the message is an echo of the alt's own DM (FORWARD_OWN_DMS)."""
    author_name = str(
        getattr(message.author, "name", "") or getattr(message.author, "display_name", "") or ""
    )
    return author_name.lower().endswith("(alt)")


def _extract_forwarded_reply_target(message: discord.Message) -> tuple[Optional[int], str]:
    """Parse ``(alt_id, buyer_uid)`` from a forwarded #dm-inbox embed.

    Primary source: the ``/reply alt:N user:<buyer_id>`` quick-reply field;
    fallback for the alt: the embed footer ("… Alt 2"). Missing buyer → ("",).
    """
    alt_id: Optional[int] = None
    buyer_uid = ""
    for embed in getattr(message, "embeds", []) or []:
        for field in getattr(embed, "fields", []) or []:
            m = re.search(
                r"alt:\s*(\d+)\s+user:\s*(\d+)",
                str(getattr(field, "value", "") or ""), re.I,
            )
            if m:
                alt_id = int(m.group(1))
                buyer_uid = m.group(2)
        if alt_id is None:
            footer = getattr(embed, "footer", None)
            footer_text = str(getattr(footer, "text", "") or "") if footer else ""
            fm = re.search(r"Alt\s+(\d+)", footer_text)
            if fm:
                alt_id = int(fm.group(1))
    return alt_id, buyer_uid


def _attribute_inbox_message(ch_id: str, alt_id: Optional[int]) -> tuple[Optional[dict], Optional[int]]:
    """Attribute an inbox post to ``(customer, alt_id)`` — thread first, alt next."""
    customer = None
    try:
        for c in _cm.list_customers(active_only=True):
            dm_t = str(c.get("dm_thread_id") or "").strip()
            if dm_t and dm_t != "0" and dm_t == ch_id:
                customer = c
                break
    except Exception as exc:
        print(f"[AUTOREPLY] customer lookup failed: {type(exc).__name__}: {exc}")
        customer = None
    if alt_id is None and customer is not None:
        owned = _customer_owned_alt_ids(customer.get("discord_id"))
        if len(owned) == 1:
            alt_id = next(iter(owned))
    if customer is None and alt_id is not None:
        customer = _customer_for_alt(alt_id)
    return customer, alt_id


def _vip_autoreply_cooldown_ok(alt_id: int, buyer_uid: str) -> bool:
    """Per-buyer rate limit — never spam a buyer who writes several messages.

    Records the send timestamp as a side effect when allowed and prunes the
    bookkeeping map past 5 000 entries (bounded memory, one process).
    """
    now = time.time()
    key = (int(alt_id), str(buyer_uid))
    if now - _autoreply_last_sent.get(key, 0.0) < AUTOREPLY_COOLDOWN_SEC:
        return False
    _autoreply_last_sent[key] = now
    if len(_autoreply_last_sent) > 5000:
        cutoff = now - AUTOREPLY_COOLDOWN_SEC
        for k in [k for k, ts in _autoreply_last_sent.items() if ts < cutoff]:
            _autoreply_last_sent.pop(k, None)
    return True


async def _maybe_vip_autoreply(message: discord.Message) -> None:
    """VIP DM auto-reply relay (V8 plan feature #5).

    Fires on buyer-DM posts forwarded by an alt runner into a #dm-inbox.
    The forwarded embed carries a ``/reply alt:N user:<buyer_id>`` quick-reply
    field; we parse the target alt + buyer id from it, attribute the alt to
    its owning customer, and — when that customer is an ACTIVE VIP with a
    saved auto-reply — queue ``!reply <buyer> <text>`` through the control
    Gist so the runner DMs the buyer from the alt account. Rate-limited to
    one auto-reply per buyer per ``AUTOREPLY_COOLDOWN_SEC``.

    V8 cleanup: parsing/attribution/rate-limiting each live in a named,
    individually-tested helper; this function only orchestrates.
    """
    if not _V8_LOADED:
        return
    if _is_alt_self_echo(message):
        return  # echo of the alt's own DM — not a buyer DM

    alt_id, buyer_uid = _extract_forwarded_reply_target(message)
    if not buyer_uid:
        return  # nothing we can relay to (no quick-reply field)

    customer, alt_id = _attribute_inbox_message(str(message.channel.id), alt_id)
    if customer is None or alt_id is None:
        return

    uid = str(customer.get("discord_id") or "")
    try:
        if not customer.get("vip") or not _security.is_vip_customer(uid):
            return  # VIP-plan feature only
        if not _cm.is_active(uid):
            return
        text = _cm.get_autoreply(uid)
    except Exception as exc:
        print(f"[AUTOREPLY] eligibility check failed for `{uid}`: {type(exc).__name__}: {exc}")
        return
    if not text:
        return
    if not _vip_autoreply_cooldown_ok(int(alt_id), buyer_uid):
        return

    ack = await _send_control_wait_ack(int(alt_id), f"!reply {buyer_uid} {text}", timeout=15)
    ok = ack.startswith(("🕒", "✅"))
    log_text = (
        f"⭐ VIP auto-reply {'queued' if ok else 'FAILED'} for buyer "
        f"`…{buyer_uid[-4:]}` via Alt {alt_id} "
        f"({customer.get('discord_username', uid)}): {ack[:200]}"
    )
    try:
        state.append_log(int(alt_id), log_text, emoji="⭐", color=0xFEE75C, kind="CONTROL")
    except Exception as exc:
        print(f"[AUTOREPLY] per-alt log append failed: {type(exc).__name__}: {exc}")
    try:
        await _log_control(log_text)
    except Exception as exc:
        print(f"[AUTOREPLY] control-log relay failed: {type(exc).__name__}: {exc}")
    try:
        _cm.record_event(
            uid, "vip_autoreply_sent",
            {"alt": int(alt_id), "buyer_suffix": buyer_uid[-4:], "ok": bool(ok)},
        )
    except Exception as exc:
        print(f"[AUTOREPLY] event ledger write failed: {type(exc).__name__}: {exc}")


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

    # V8 plan feature #5 — VIP DM auto-reply watcher: buyer DMs forwarded
    # into a #dm-inbox trigger the customer's saved auto-reply relay.
    if not is_edit:
        try:
            if _is_dm_inbox_message(message):
                await _maybe_vip_autoreply(message)
        except Exception as exc:
            print(f"[VIP-AUTOREPLY] watcher error: {type(exc).__name__}: {exc}")

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


def _strip_code_fence(raw: str) -> str:
    """Unwrap a ```json … ``` fenced message body (legacy heartbeats)."""
    raw = (raw or "").strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:])
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()
    return raw


def _consume_json_heartbeat(raw: str) -> bool:
    """Apply a legacy JSON heartbeat message; True when one was consumed.

    Malformed JSON is logged and treated as "not a heartbeat" (the embed path
    may still produce state), matching the old silent behaviour minus the
    silence.
    """
    raw = _strip_code_fence(raw)
    if not raw.startswith("{"):
        return False
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"[STATE] malformed JSON heartbeat ignored: {exc}")
        return False
    if isinstance(payload, dict) and (payload.get("heartbeat") or payload.get("type") == "heartbeat"):
        alt_id = payload.get("alt_id") or _match_alt_name(payload.get("alt_name"))
        if alt_id:
            try:
                state.update_from_heartbeat(alt_id, payload)
            except Exception as exc:
                print(f"[STATE] legacy JSON heartbeat update failed for alt {alt_id}: "
                      f"{type(exc).__name__}: {exc}")
            return True
    return False


def _heartbeat_embed_scalar_field(key: str, name: str, value: str, payload: dict, channels: dict) -> None:
    """One heartbeat embed field → payload/channel mutation (no I/O)."""
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


def _consume_embed_heartbeat(embed) -> bool:
    """Apply one 💓-Heartbeat embed to live state. True when consumed."""
    title = getattr(embed, "title", "") or ""
    if not title.lower().startswith("💓 heartbeat"):
        return False
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
            return False
        payload = {"heartbeat": True, "type": "heartbeat", "alt_id": alt_id}
        channels: dict = {}
        for field in embed.fields or []:
            name = str(getattr(field, "name", "") or "").strip()
            value = str(getattr(field, "value", "") or "").strip()
            _heartbeat_embed_scalar_field(name.casefold(), name, value, payload, channels)
        if channels:
            payload["channels"] = channels
            max_ch_post = max((int(ch.get("last_post") or 0) for ch in channels.values()), default=0)
            if max_ch_post > 0:
                payload["last_post_ts"] = max_ch_post
        state.update_from_heartbeat(alt_id, payload)
        return True
    except Exception as exc:
        # A malformed optional field must not discard the rest of a live
        # heartbeat or crash the bot's event loop.
        print(f"[STATE] heartbeat embed field parse skipped: {type(exc).__name__}: {exc}")
        return False


def _parse_dashboard_message(message: discord.Message):
    """Extract live state from a structured heartbeat (old JSON or new embed).

    Kept compatible with heartbeat messages written before the readable embed
    format was deployed. V8 cleanup: split into _consume_* helpers per format.
    """
    # Legacy JSON format first; readable embeds may refine further.
    _consume_json_heartbeat(message.content or "")
    for embed in message.embeds:
        _consume_embed_heartbeat(embed)


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
            except Exception as _ignored_exc:
                print(f"[BOT] _parse_log_message: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)


def _try_parse_heartbeat(alt_id: int, body: str):
    body = body.strip()
    if not body.startswith("{"):
        return
    try:
        payload = json.loads(body)
        if isinstance(payload, dict) and payload.get("type") == "heartbeat":
            state.update_from_heartbeat(alt_id, payload)
    except Exception as _ignored_exc:
        print(f"[BOT] _try_parse_heartbeat: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)


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
            except Exception as _ignored_exc:
                print(f"[BOT] _refresh_dashboard_now: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
            try:
                await _dash_message.pin()
            except Exception as _ignored_exc:
                print(f"[BOT] _refresh_dashboard_now: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
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
            except Exception as _ignored_exc:
                print(f"[BOT] fleet_health_check: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
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
        except Exception as _ignored_exc:
            print(f"[BOT] fleet_health_check: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)


async def _log_control(text: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    full = f"[{ts}] [CONTROL] {text}"
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
    """V8: Control bot runs continuously — no session timer, no total_hours limit.
    The GitHub Actions watchdog in control_bot.yml restarts on crash automatically.
    """
    if not config.BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN env var is not set.")
    if not config.GITHUB_TOKEN:
        print("⚠️  GH_TOKEN not set — /run and /stop will not work.")
    if not config.GITHUB_OWNER or not config.ALT_REPOS:
        print("⚠️  GITHUB_OWNER / ALT_REPOS not set — /run will fail.")
    print("[V8] Control bot starting — continuous runtime mode (no session limit).")
    print(f"Alt mapping: {config.ALT_REPOS}")
    print(f"Discord IDs: {config.ALT_DISCORD_IDS}")
    # V8: initialise the SQLite DB before the gateway connects
    if _V8_LOADED:
        try:
            _cm.init_db()
        except Exception as _e:
            print(f"[V8] DB pre-init warning: {_e}")
    bot.run(config.BOT_TOKEN)
