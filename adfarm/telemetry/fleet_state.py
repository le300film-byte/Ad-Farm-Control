"""FleetState — in-memory live telemetry per alt, keyed by ``(customer_id, alt_index)``.

This is a cache of what the senders report; the database never stores it. Losing it on a
restart is harmless: the ingestor rebuilds it from the last heartbeat message of each dashboard
thread and, failing that, the next heartbeat arrives within 5 minutes.
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Optional

from ..core.clock import Clock, SystemClock
from .heartbeat import ChannelStat, Heartbeat

AltKey = tuple[str, int]


@dataclass
class LogLine:
    ts: float
    text: str
    kind: str = "INFO"     # INFO | WARN | ERROR | DEAL | DM | BAN | CONTROL


@dataclass
class LiveAlt:
    key: AltKey
    sender_alt_id: int
    online: bool = False
    last_heartbeat_at: float = 0.0
    status: str = "offline"
    version: str = ""
    ad_type: str = ""
    rate: Optional[float] = None
    interval_min: Optional[int] = None
    total_sent: int = 0
    total_errors: int = 0
    total_skips: int = 0
    deal_alerts: int = 0
    deal_scan_enabled: Optional[bool] = None
    deal_alert_delta: Optional[float] = None
    deal_keywords: tuple[str, ...] = ()
    uptime_sec: float = 0.0
    active_channels: int = 0
    total_channels: int = 0
    message_preview: str = ""
    last_error: str = ""
    warnings: tuple[str, ...] = ()
    channels: dict[str, ChannelStat] = field(default_factory=dict)
    last_ack: str = ""
    logs: Deque[LogLine] = field(default_factory=lambda: deque(maxlen=200))
    dm_last_reply_at: dict[str, float] = field(default_factory=dict)   # buyer id → last auto-reply ts

    @property
    def health_index(self) -> int:
        """0-100 heuristic used by dashboards."""
        if not self.online:
            return 0
        score = 100
        if self.status in ("caution", "ip_pause"):
            score -= 35
        elif self.status in ("paused", "afk"):
            score -= 15
        elif self.status in ("error", "stopped"):
            score -= 70
        total = self.total_sent + self.total_errors
        if total:
            score -= int(min(40, 100 * self.total_errors / total))
        if self.total_channels and self.active_channels < self.total_channels:
            score -= int(20 * (1 - self.active_channels / self.total_channels))
        return max(0, min(100, score))


class FleetState:
    def __init__(self, *, clock: Clock | None = None, offline_after: int = 900):
        self.clock = clock or SystemClock()
        self.offline_after = int(offline_after)
        self._lock = threading.RLock()
        self._alts: dict[AltKey, LiveAlt] = {}
        self._by_sender: dict[int, AltKey] = {}

    # ── registry ────────────────────────────────────────────────────────────
    def register(self, key: AltKey, sender_alt_id: int) -> LiveAlt:
        with self._lock:
            live = self._alts.get(key)
            if live is None:
                live = LiveAlt(key=key, sender_alt_id=int(sender_alt_id))
                self._alts[key] = live
            live.sender_alt_id = int(sender_alt_id)
            self._by_sender[int(sender_alt_id)] = key
            return live

    def forget(self, key: AltKey) -> None:
        with self._lock:
            live = self._alts.pop(key, None)
            if live:
                self._by_sender.pop(live.sender_alt_id, None)

    def get(self, key: AltKey) -> Optional[LiveAlt]:
        with self._lock:
            return self._alts.get(key)

    def by_sender(self, sender_alt_id: int) -> Optional[LiveAlt]:
        with self._lock:
            key = self._by_sender.get(int(sender_alt_id))
            return self._alts.get(key) if key else None

    def for_customer(self, customer_id: str) -> list[LiveAlt]:
        with self._lock:
            return sorted((a for a in self._alts.values() if a.key[0] == customer_id), key=lambda a: a.key[1])

    def all(self) -> list[LiveAlt]:
        with self._lock:
            return sorted(self._alts.values(), key=lambda a: a.key)

    # ── updates ─────────────────────────────────────────────────────────────
    def apply_heartbeat(self, hb: Heartbeat, *, key: AltKey | None = None) -> Optional[LiveAlt]:
        with self._lock:
            live = self._alts.get(key) if key else self.by_sender(hb.sender_alt_id)
            if live is None:
                return None
            live.online = True
            live.last_heartbeat_at = self.clock.now()
            live.status = hb.status or live.status
            live.version = hb.version or live.version
            live.ad_type = hb.ad_type or live.ad_type
            if hb.rate is not None:
                live.rate = hb.rate
            if hb.interval_min in (3, 5):
                live.interval_min = hb.interval_min
            live.total_sent, live.total_errors, live.total_skips = hb.total_sent, hb.total_errors, hb.total_skips
            live.deal_alerts = hb.deal_alerts
            if hb.deal_scan_enabled is not None:
                live.deal_scan_enabled = hb.deal_scan_enabled
            if hb.deal_alert_delta is not None:
                live.deal_alert_delta = hb.deal_alert_delta
            if hb.deal_keywords:
                live.deal_keywords = hb.deal_keywords
            live.uptime_sec = hb.uptime_sec or live.uptime_sec
            live.active_channels, live.total_channels = hb.active_channels, hb.total_channels
            live.message_preview = hb.message_preview or live.message_preview
            live.last_error = hb.last_error
            live.warnings = hb.warnings
            if hb.channels:
                live.channels = {c.channel_id: c for c in hb.channels}
            return live

    def append_log(self, key: AltKey, text: str, kind: str = "INFO") -> None:
        with self._lock:
            live = self._alts.get(key)
            if live is not None:
                live.logs.append(LogLine(ts=self.clock.now(), text=str(text)[:500], kind=kind))

    def recent_logs(self, key: AltKey, limit: int = 20, kind: str | None = None) -> list[LogLine]:
        with self._lock:
            live = self._alts.get(key)
            if live is None:
                return []
            lines = [l for l in live.logs if kind is None or l.kind == kind]
            return lines[-limit:]

    def clear_logs(self, key: AltKey) -> None:
        with self._lock:
            live = self._alts.get(key)
            if live is not None:
                live.logs.clear()

    def set_ack(self, key: AltKey, text: str) -> None:
        with self._lock:
            live = self._alts.get(key)
            if live is not None:
                live.last_ack = str(text)[:300]

    def set_status(self, key: AltKey, status: str) -> None:
        with self._lock:
            live = self._alts.get(key)
            if live is not None:
                live.status = status
                if status in ("stopped", "offline"):
                    live.online = False

    def mark_stale(self) -> list[AltKey]:
        """Flip alts to offline when no heartbeat arrived for ``offline_after`` seconds."""
        now = self.clock.now()
        flipped: list[AltKey] = []
        with self._lock:
            for live in self._alts.values():
                if live.online and now - live.last_heartbeat_at > self.offline_after:
                    live.online = False
                    live.status = "offline"
                    flipped.append(live.key)
        return flipped

    def should_autoreply(self, key: AltKey, buyer_id: str, cooldown: int) -> bool:
        now = self.clock.now()
        with self._lock:
            live = self._alts.get(key)
            if live is None:
                return False
            last = live.dm_last_reply_at.get(buyer_id, 0.0)
            if now - last < cooldown:
                return False
            live.dm_last_reply_at[buyer_id] = now
            return True
