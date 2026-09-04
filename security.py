"""security.py – V8 Global Permission Checks, Command Tiers & Channel Policy.

Provides:
  - @require_access(admin_only, vip_only) decorator for Discord slash commands.
  - Per-interaction fast-path checks used internally by admin_commands.py.
  - The canonical V8 command-tier model (PUBLIC / CUSTOMER / VIP / ADMIN) that
    both /help filtering and the channel gate derive from (single source of
    truth — bot.py re-exports these sets for backward compatibility).
  - Channel-aware command visibility (V8 bug-fix plan #1):
      classify_channel_context()   → where is this interaction happening?
      command_allowed_in_context() → pure tier × channel-context matrix
      enforce_channel_gate()       → decorator + inline helper that denies
        wrong-context use with the canonical V8 message.

Everything channel-related is configuration-driven (env overrides) — no channel
name or id is hardcoded in the command flow.

Error messages are consistent with the V8 plan specification.
"""
from __future__ import annotations

import functools
import os
import time
from typing import Any, Callable, Optional, Set

import discord

# ──────────────────────────────────────────────────────────────────────────────
# Owner / admin allow-list
# ──────────────────────────────────────────────────────────────────────────────

def _load_owner_ids() -> Set[int]:
    ids: Set[int] = set()
    raw = os.environ.get("OWNER_IDS", "") or os.environ.get("OWNER_ID", "")
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


# Refreshed once at import time; bots should call reload_owner_ids() on ready.
OWNER_IDS: Set[int] = _load_owner_ids()


def reload_owner_ids() -> None:
    global OWNER_IDS
    OWNER_IDS = _load_owner_ids()


# ──────────────────────────────────────────────────────────────────────────────
# V8 command tiers — canonical definition (bot.py re-exports for back-compat)
# ──────────────────────────────────────────────────────────────────────────────

#: Everyone may use these, in every channel.
PUBLIC_COMMANDS = frozenset({"help", "getstarted"})
#: Customer-tier commands (active subscription required).
CUSTOMER_COMMANDS = frozenset({
    "setup", "run", "stop", "pause", "resume", "tune", "channels",
    "deals", "status", "reply", "refresh", "dashboard", "shutdown",
    "alt", "renew", "pause-billing", "proofs",
})
#: VIP-tier commands (VIP subscription required).
VIP_COMMANDS = frozenset({"squad", "script", "vip"})
#: Operator-tier commands (OWNER_IDS required).
ADMIN_COMMANDS = frozenset({"admin", "reset"})


def command_tier(name: str) -> str:
    """Tier of a top-level slash command name: admin | vip | customer | public."""
    key = str(name or "").strip().lstrip("/").lower()
    if key in ADMIN_COMMANDS:
        return "admin"
    if key in VIP_COMMANDS:
        return "vip"
    if key in PUBLIC_COMMANDS:
        return "public"
    return "customer"  # unknown top-level commands default to the customer tier


def allowed_commands_for_role(role: str) -> Set[str]:
    """Set of top-level command names visible to *role* (V8 bug-fix F)."""
    if role == "admin":
        return set(PUBLIC_COMMANDS | CUSTOMER_COMMANDS | VIP_COMMANDS | ADMIN_COMMANDS)
    if role == "vip":
        return set(VIP_COMMANDS | CUSTOMER_COMMANDS | PUBLIC_COMMANDS)
    if role == "customer":
        return set(CUSTOMER_COMMANDS | PUBLIC_COMMANDS)
    return set(PUBLIC_COMMANDS)  # non-customers: /help + /getstarted only


# ──────────────────────────────────────────────────────────────────────────────
# Channel-aware command visibility (V8 bug-fix plan #1)
# ──────────────────────────────────────────────────────────────────────────────
#
# Every channel in the guild is classified into one of four contexts. The tier
# matrix below then decides which commands are usable there. Defaults follow
# the channel layout that ``setup.py`` creates; each list can be overridden
# per installation with an env var (comma-separated channel *names* or ids)
# so nothing here stays hardcoded when an operator renames their server:
#
#   PUBLIC_CHANNELS    → only public-tier commands (e.g. #announcements)
#   CUSTOMER_CHANNELS   → customer + vip + public commands (customer forum rooms)
#   VIP_CHANNELS        → customer forum rooms plus the VIP-tier commands
#   ADMIN_CHANNELS      → every command, incl. /admin + /reset (operator rooms)
#
# Contexts are matched by channel id first, then channel name, then the thread
# parent's name, then the category name (substring heuristics). A channel that
# matches nothing is *unclassified* (None): every tier except admin is allowed
# there, which keeps odd-shaped servers usable while still locking down the
# explicitly-classified public announcement channels.
#
# DM interactions classify as "dm" and are unrestricted (private room; role
# checks still apply). Bot owners (OWNER_IDS) are exempt from the channel gate
# by design — they run setup from wherever they happen to be (documented
# manager override); visibility hiding via Discord's per-channel permissions
# still applies to regular members.

DEFAULT_PUBLIC_CHANNELS = "welcome-about,pricing-plans,announcements"
DEFAULT_CUSTOMER_CHANNELS = "control,dashboard,farm-logs,deals,open-ticket,tickets"
DEFAULT_VIP_CHANNELS = "dm-inbox"
DEFAULT_ADMIN_CHANNELS = "admin-commands,admin-alerts,admin-chat,audit-logs"


def _channel_rule_set(env_name: str, default: str) -> frozenset:
    raw = os.environ.get(env_name, "").strip() or default
    parts = []
    for part in raw.split(","):
        token = part.strip()
        if token.startswith("<#") and token.endswith(">"):
            token = token[2:-1]
        token = token.lstrip("#")
        if token:
            parts.append(token.lower())
    return frozenset(parts)


def _rules_from_env() -> dict:
    return {
        "public": _channel_rule_set("PUBLIC_CHANNELS", DEFAULT_PUBLIC_CHANNELS),
        "customer": _channel_rule_set("CUSTOMER_CHANNELS", DEFAULT_CUSTOMER_CHANNELS),
        "vip": _channel_rule_set("VIP_CHANNELS", DEFAULT_VIP_CHANNELS),
        "admin": _channel_rule_set("ADMIN_CHANNELS", DEFAULT_ADMIN_CHANNELS),
    }


#: Resolved once from env at import; ``reload_channel_rules()`` refreshes it
#: after setup.py / /admin updates the environment without a code change.
CHANNEL_RULES: dict = _rules_from_env()


def reload_channel_rules() -> None:
    global CHANNEL_RULES
    CHANNEL_RULES = _rules_from_env()


def classify_channel_context(channel: Any) -> Optional[str]:
    """Return the policy context of a Discord channel-like object.

    "public" | "customer" | "vip" | "admin" | "dm" | None (unclassified).
    Duck-typed on purpose so the unit-test fakes (SimpleNamespace) work.
    """
    if channel is None:
        return None
    if getattr(channel, "type", None) == discord.ChannelType.private:
        return "dm"
    # DM channel or interaction without a guild → private context.
    try:
        guild = getattr(channel, "guild", "unknown")
        if guild is None:
            return "dm"
    except Exception:
        return None

    candidates = []
    ident = str(getattr(channel, "id", "") or "").strip().lower()
    name = str(getattr(channel, "name", "") or "").strip().lstrip("#").lower()
    if name:
        candidates.append(name)
    # Threads carry their own (user-chosen) title; the forum/category they sit
    # in is the reliable signal, so classify the parents too.
    for parent_attr in ("parent", "category"):
        parent = getattr(channel, parent_attr, None)
        if isinstance(parent, (list, tuple)):
            continue
        pname = str(getattr(parent, "name", "") or "").strip().lstrip("#").lower()
        if pname:
            candidates.append(pname)

    for ctx, rules in CHANNEL_RULES.items():
        if ident and ident in rules:
            return ctx
        for token in candidates:
            if token in rules:
                return ctx
    # Soft heuristics for category naming conventions (configurable servers).
    for token in candidates:
        if "admin" in token:
            return "admin"
        if "vip" in token:
            return "vip"
    # Threads inside a customer forum usually carry free-form titles; the
    # forum's CATEGORY is the reliable "this is a customer room" signal
    # (setup.py / discord_forum.py put customer hubs under one category).
    hub_marker = os.environ.get("CUSTOMER_HUB_MARKER", "customer hub").strip().lower()
    if hub_marker:
        for token in candidates:
            if hub_marker in token:
                return "customer"
    return None


#: tier → contexts where the tier is usable.  "dm" and unclassified channels
#: allow everything; admin-tier additionally requires an admin context (or DM)
#: whenever the guild defines admin channels at all.
_CONTEXT_ALLOWED_TIERS = {
    "public": {"public"},
    "customer": {"public", "customer", "vip"},
    "vip": {"public", "customer", "vip"},
    "admin": {"public", "customer", "vip", "admin"},
    "dm": {"public", "customer", "vip", "admin"},
    None: {"public", "customer", "vip", "admin"},
}


def command_allowed_in_context(command_name: str, ctx: Optional[str]) -> tuple[bool, str]:
    """Pure policy check: (allowed, hint_when_denied) for *command_name* in *ctx*."""
    tier = command_tier(command_name)
    allowed = _CONTEXT_ALLOWED_TIERS.get(ctx, _CONTEXT_ALLOWED_TIERS[None])
    if tier in allowed:
        return True, ""
    if ctx == "public":
        return False, "Public channels only host /help and /getstarted — run this in your forum rooms."
    if ctx in ("customer", "vip"):
        return False, "Admin commands belong in #admin-commands."
    return False, "This command is not usable from this channel context."


CHANNEL_GATE_DENIAL = "❌ This command is not available in this channel."


def _interaction_channel(inter: Any) -> Any:
    channel = getattr(inter, "channel", None)
    if channel is None:
        data = getattr(inter, "data", None)
        if isinstance(data, dict):
            cid = data.get("channel_id")
            guild = getattr(inter, "guild", None)
            if cid and guild is not None:
                try:
                    channel = guild.get_channel(int(cid))
                except (TypeError, ValueError):
                    channel = None
    return channel


async def enforce_channel_gate(inter: Any, command_name: str) -> bool:
    """Channel-context gate for a slash command invocation.

    Returns True when the command may proceed. On denial, sends the canonical
    V8 message ephemerally and returns False. Bot owners bypass the gate (they
    drive setup from any channel); interactions with no resolvable channel
    (DMs, test fakes) are treated as unclassified → allowed, with the tier's
    own role check still enforced by the caller.
    """
    try:
        try:
            uid = int(getattr(getattr(inter, "user", None), "id", 0) or 0)
        except (TypeError, ValueError):
            uid = 0
        if uid and is_admin(uid):
            return True
        ctx = classify_channel_context(_interaction_channel(inter))
        ok, hint = command_allowed_in_context(command_name, ctx)
        if ok:
            return True
        message = CHANNEL_GATE_DENIAL + (f"\n{hint}" if hint else "")
        await _deny(inter, message)
        return False
    except Exception as exc:  # classifier blew up on an exotic channel type
        print(f"[SECURITY] Channel gate degraded for '{command_name}': "
              f"{type(exc).__name__}: {exc}")
        return True  # fail-open on infrastructure error: role checks still apply


def require_channel(command_name: Optional[str] = None) -> Callable:
    """Decorator: deny a slash command when invoked in the wrong channel context.

    Applied *underneath* ``@bot.tree.command``/``@app_commands.command``::

        @bot.tree.command(name="run", ...)
        @require_channel()          # command name inferred from the callback
        async def cmd_run(inter):  # or (self, inter) inside a cog
            ...

    ``command_name`` overrides the inference when the callback name does not
    match the registered command (e.g. ``cmd_pause_billing`` for ``/pause-billing``).
    """
    def decorator(func: Callable) -> Callable:
        inferred = (command_name or func.__name__)
        inferred = inferred.removeprefix("cmd_").replace("_", "-").lower()

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            inter = next(
                (a for a in args
                 if hasattr(a, "user") and hasattr(a, "response")),
                None,
            )
            if inter is not None and not await enforce_channel_gate(inter, inferred):
                return None
            return await func(*args, **kwargs)

        return wrapper
    return decorator

def is_admin(user_id: int) -> bool:
    """Fail-closed: empty OWNER_IDS never grants access."""
    return bool(OWNER_IDS) and user_id in OWNER_IDS


def is_active_customer(discord_id: str) -> bool:
    """Check customers.db for an active, non-expired subscription."""
    try:
        from customer_manager import is_active  # type: ignore
        return is_active(discord_id)
    except Exception:
        return False


def is_vip_customer(discord_id: str) -> bool:
    """Check customers.db for VIP flag."""
    try:
        from customer_manager import is_vip  # type: ignore
        return is_vip(discord_id)
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Async helper used by the decorator
# ──────────────────────────────────────────────────────────────────────────────

async def _deny(inter: discord.Interaction, msg: str) -> None:
    if inter.response.is_done():
        await inter.followup.send(msg, ephemeral=True)
    else:
        await inter.response.send_message(msg, ephemeral=True)


# ──────────────────────────────────────────────────────────────────────────────
# Public decorator
# ──────────────────────────────────────────────────────────────────────────────

def require_access(admin_only: bool = False, vip_only: bool = False) -> Callable:
    """Decorator that gates a Discord slash command callback.

    Checks are applied in order:
      1. Admin check  (if admin_only=True)
      2. Active-customer check (always, unless admin_only is the sole requirement)
      3. VIP check    (if vip_only=True)

    Admin users bypass the customer/VIP checks because they control the bot.
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(self_or_inter: Any, *args: Any, **kwargs: Any) -> Any:
            # Detect whether this is a plain function call (first arg is the
            # interaction) or a cog method call (self is first, interaction is
            # the second positional arg).  We identify an interaction by
            # checking for the 'user' and 'response' attributes that both
            # discord.Interaction and our test fakes expose.
            def _is_inter(obj: Any) -> bool:
                return (
                    isinstance(obj, discord.Interaction)
                    or (hasattr(obj, "user") and hasattr(obj, "response") and hasattr(obj, "followup"))
                )

            if _is_inter(self_or_inter):
                # Plain function: wrapper(inter, ...)
                inter: Any = self_or_inter
                return await _guarded(inter, func, (self_or_inter,) + args, kwargs,
                                      admin_only, vip_only)
            else:
                # Cog method: wrapper(self, inter, ...)
                if not args:
                    return
                inter = args[0]
                return await _guarded(inter, func, (self_or_inter,) + args, kwargs,
                                      admin_only, vip_only)

        return wrapper
    return decorator


async def _guarded(
    inter: discord.Interaction,
    func: Callable,
    call_args: tuple,
    call_kwargs: dict,
    admin_only: bool,
    vip_only: bool,
) -> Any:
    uid = inter.user.id
    uid_str = str(uid)

    if admin_only:
        if not is_admin(uid):
            await _deny(inter, "❌ You are not authorized to use this command.")
            return
        # Admins bypass customer/VIP checks
        return await func(*call_args, **call_kwargs)

    # Admins always bypass the customer/VIP gates (documented contract):
    # they control the bot and are not necessarily customers themselves.
    if is_admin(uid):
        return await func(*call_args, **call_kwargs)

    # Non-admin path: customer must exist and be active
    if not is_active_customer(uid_str):
        # Could be expired or never a customer
        from customer_manager import get_customer  # type: ignore
        try:
            c = get_customer(uid_str)
        except Exception:
            c = None  # DB hiccup → fail closed with the subscription denial
        if c is not None and not c["active"]:
            await _deny(inter, "❌ Your subscription has expired. Contact an admin to renew.")
        else:
            # V8 bug-fix J: non-customers must see the subscription message.
            await _deny(
                inter,
                "❌ You do not have an active subscription. "
                "You are not authorized to use this command.",
            )
        return

    if vip_only and not is_vip_customer(uid_str):
        await _deny(
            inter,
            "❌ This feature requires VIP. Run /admin activate @User vip:true to upgrade.",
        )
        return

    return await func(*call_args, **call_kwargs)


# ──────────────────────────────────────────────────────────────────────────────
# Standalone check helpers (used by admin_commands.py without the decorator)
# ──────────────────────────────────────────────────────────────────────────────

async def check_admin(inter: discord.Interaction) -> bool:
    """Returns True and does nothing if the user is an admin.
    Sends an ephemeral error and returns False otherwise."""
    if is_admin(inter.user.id):
        return True
    await _deny(inter, "❌ You are not authorized to use this command.")
    return False


async def check_active(inter: discord.Interaction) -> bool:
    """Active-customer gate; admins (OWNER_IDS) always pass."""
    if is_admin(inter.user.id):
        return True
    uid_str = str(inter.user.id)
    if is_active_customer(uid_str):
        return True
    from customer_manager import get_customer  # type: ignore
    try:
        c = get_customer(uid_str)
    except Exception:
        c = None  # DB hiccup → fail closed with the subscription denial
    if c is not None and not c["active"]:
        await _deny(inter, "❌ Your subscription has expired. Contact an admin to renew.")
    else:
        # V8 bug-fix J: non-customers get the active-subscription message.
        await _deny(
            inter,
            "❌ You do not have an active subscription. "
            "You are not authorized to use this command.",
        )
    return False


async def check_vip(inter: discord.Interaction) -> bool:
    if is_vip_customer(str(inter.user.id)):
        return True
    await _deny(
        inter,
        "❌ This feature requires VIP. Run /admin activate @User vip:true to upgrade.",
    )
    return False


async def check_customer_access(inter: discord.Interaction, *, vip_only: bool = False) -> bool:
    """V8 role gate shared by the customer-facing slash commands.

    Resolution order (V8 bug-fixes F & J):
      1. Admins (OWNER_IDS) always pass — they control the bot.
      2. Everyone else needs an ACTIVE subscription (non-customers and
         expired customers get the canonical denial messages).
      3. With ``vip_only=True`` the caller must additionally be a VIP
         customer (powers /squad and /script).

    Returns True when the command may proceed, False after an ephemeral denial
    was already sent.
    """
    if is_admin(inter.user.id):
        return True
    if not await check_active(inter):
        return False
    if vip_only:
        return await check_vip(inter)
    return True


# ──────────────────────────────────────────────────────────────────────────────
# Multi-sig destructive commands (TODO 3.2)
# ──────────────────────────────────────────────────────────────────────────────

MULTISIG_WINDOW_SEC = 120


class MultiSigConfirm:
    """Two-admin confirmation for destructive commands.

    ``request(action, admin_id)`` → "initiated" (first admin) or "confirmed"
    (second, different admin within 120s). State is process-local by design:
    a restart erases pending confirmations (fail safe).
    """

    def __init__(self, window_sec: int = MULTISIG_WINDOW_SEC):
        self.window = window_sec
        self._pending: dict[str, tuple[str, float]] = {}  # action -> (admin_id, ts)

    def request(self, action: str, admin_id: int) -> tuple[str, str]:
        now = time.time()
        for key in list(self._pending):
            if now - self._pending[key][1] > self.window:
                del self._pending[key]
        pending = self._pending.get(action)
        if pending is None:
            self._pending[action] = (str(admin_id), now)
            return "initiated", (
                f"⚠️ **{action} INITIATED** — a second admin must confirm within "
                f"{self.window}s. Run the same command again."
            )
        first_id, ts = pending
        if str(admin_id) == first_id:
            return "waiting", (
                f"⏳ Already initiated by you — waiting for a **different admin** "
                f"({int(self.window - (now - ts))}s left)."
            )
        del self._pending[action]
        return "confirmed", f"✅ Confirmed by a second admin. Executing {action}…"


MULTISIG = MultiSigConfirm()
