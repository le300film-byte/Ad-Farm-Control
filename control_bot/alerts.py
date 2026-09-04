"""control_bot.alerts — operator alert sinks (#admin-alerts / #admin-chat).

Flowing text into the right staff channel without importing the bot module
into low-level modules (avoiding import cycles).  The bot wires the real
channel IDs at startup via ``wire(bot)``.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Callable, Optional

import discord

_bot: Any = None
_ADMIN_ALERTS_CH_ID: Optional[int] = None
_ADMIN_CHAT_CH_ID: Optional[int] = None

# debounce identical alerts
_last: dict[str, float] = {}
MIN_INTERVAL_SEC = 60.0


def wire(bot: Any) -> None:
    """Register the live bot and channel IDs (call from on_ready)."""
    global _bot, _ADMIN_ALERTS_CH_ID, _ADMIN_CHAT_CH_ID
    _bot = bot
    _ADMIN_ALERTS_CH_ID = int(os.environ.get("ADMIN_ALERTS_CH_ID", "0") or "0") or None
    _ADMIN_CHAT_CH_ID = int(os.environ.get("ADMIN_CHAT_CH_ID", "0") or "0") or None


def _channel(chan_id: Optional[int]):
    if _bot is None or not chan_id:
        return None
    ch = _bot.get_channel(chan_id)
    return ch


async def _post(chan_id: Optional[int], text: str, mention: bool = False) -> bool:
    ch = _channel(chan_id)
    if ch is None:
        print(f"[ALERT] (no channel) {text}")
        return False
    try:
        kwargs: dict[str, Any] = {"content": text[:2000]}
        if mention:
            kwargs["allowed_mentions"] = discord.AllowedMentions.none()
        await ch.send(**kwargs)
        return True
    except Exception as exc:
        print(f"[ALERT] send failed: {exc}")
        return False


async def post_admin_alert(text: str, debounce_key: str = "") -> bool:
    """Critical alert → #admin-alerts (fallback: stdout)."""
    key = debounce_key or text[:80]
    now = time.time()
    if now - _last.get(key, 0) < MIN_INTERVAL_SEC:
        return True  # deduplicated
    _last[key] = now
    return await _post(_ADMIN_ALERTS_CH_ID, text)


async def post_admin_chat(text: str) -> bool:
    """Operational note → #admin-chat."""
    return await _post(_ADMIN_CHAT_CH_ID, text)


def register_sync_alert_sink() -> Callable[[str], None]:
    """Synchronous sink for gist_backup (runs on a background worker thread).

    The worker cannot await, so alerts are queued and drained by the bot's
    alert flush loop (see ops.flush_alert_queue).
    """
    def _sink(message: str) -> None:
        print(f"[ALERT-SYNC] {message}")
        try:
            asyncio.run_coroutine_threadsafe(post_admin_alert(message), _LOOP)
        except Exception as _ignored_exc:
            print(f"[ALERT] _sink: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
    return _sink


_LOOP: Optional[asyncio.AbstractEventLoop] = None


def set_event_loop(loop) -> None:
    global _LOOP
    _LOOP = loop
