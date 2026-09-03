"""security.py – V8 Global Permission Checks & Decorator.

Provides:
  - @require_access(admin_only, vip_only) decorator for Discord slash commands.
  - Per-interaction fast-path checks used internally by admin_commands.py.

Error messages are consistent with the V8 plan specification.
"""
from __future__ import annotations

import functools
import os
import time
from typing import Any, Callable, Set

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
# Low-level checks (synchronous)
# ──────────────────────────────────────────────────────────────────────────────

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

    # Non-admin path: customer must exist and be active
    if not is_active_customer(uid_str):
        # Could be expired or never a customer
        from customer_manager import get_customer  # type: ignore
        c = get_customer(uid_str)
        if c is not None and not c["active"]:
            await _deny(inter, "❌ Your subscription has expired. Contact an admin to renew.")
        else:
            await _deny(inter, "❌ You are not authorized to use this command.")
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
    uid_str = str(inter.user.id)
    if is_active_customer(uid_str):
        return True
    from customer_manager import get_customer  # type: ignore
    c = get_customer(uid_str)
    if c is not None and not c["active"]:
        await _deny(inter, "❌ Your subscription has expired. Contact an admin to renew.")
    else:
        await _deny(inter, "❌ You are not authorized to use this command.")
    return False


async def check_vip(inter: discord.Interaction) -> bool:
    if is_vip_customer(str(inter.user.id)):
        return True
    await _deny(
        inter,
        "❌ This feature requires VIP. Run /admin activate @User vip:true to upgrade.",
    )
    return False


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
