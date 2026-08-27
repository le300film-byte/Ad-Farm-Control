"""control_bot.alt_state — in-memory state for all alts."""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AltState:
    """Live state for one alt. Updated from:
    1) Heartbeat webhook payloads sent every 5 min by send_ads.py.
    2) GitHub Actions API (run status, conclusion).
    3) DM replies the control bot gets from alts.
    """
    alt_id: int                         # 1..N
    name: str                           # "Alt 1"

    # From heartbeat payload
    online: bool = False                # has sent a heartbeat recently
    version: str = ""
    ad_type: str = ""                   # "sell" / "buy" / ""
    rate: Optional[float] = None        # e.g. 2.5
    rate_currency: str = "$/1k"
    message_preview: str = ""
    total_sent: int = 0
    total_errors: int = 0
    total_skips: int = 0
    total_edits: int = 0
    uptime_sec: float = 0.0
    active_channels: int = 0
    total_channels: int = 0
    last_post_ts: float = 0.0
    last_heartbeat_ts: float = 0.0
    status: str = "offline"             # active/paused/caution/ip_pause/stopped/offline/starting
    warnings: list = field(default_factory=list)
    channels: dict = field(default_factory=dict)   # cid -> {name, last_post, sent, ...}
    run_started_ts: float = 0.0
    workflow_run_id: Optional[int] = None
    workflow_status: str = ""           # github status: queued/in_progress/completed/failure...
    workflow_conclusion: str = ""
    dm_ack: str = ""                    # last ack from a DM control command
    dm_ack_ts: float = 0.0
    ip_org: str = ""
    ip_country: str = ""


class AltStateManager:
    """Thread-safe collection of AltState objects."""

    def __init__(self, alt_names: dict[int, str], alt_ids=None,
                 offline_after_sec: float = 300):
        self._lock = threading.Lock()
        self._offline_after_sec = offline_after_sec
        self._alts: dict[int, AltState] = {}
        # When the caller supplies the configured ID set, do not let an
        # orphaned display-name entry create a phantom alt. The name-only
        # fallback is retained for small standalone/test callers.
        configured = set(alt_ids) if alt_ids is not None else set(alt_names)
        for idx in sorted(configured):
            self._alts[idx] = AltState(
                alt_id=idx,
                name=alt_names.get(idx, f"Alt {idx}"),
            )
        self._log_buffer: dict[int, list[tuple[float, str, int, str]]] = {
            i: [] for i in self._alts
        }  # alt_id -> [(ts, emoji, color, text), ...] capped

    # ----- basic access -----
    @property
    def alt_ids(self) -> list[int]:
        with self._lock:
            return sorted(self._alts.keys())

    def get(self, alt_id: int) -> Optional[AltState]:
        with self._lock:
            return self._alts.get(alt_id)

    def all(self) -> list[AltState]:
        with self._lock:
            return list(self._alts.values())

    # ----- heartbeat ingestion -----
    def update_from_heartbeat(self, alt_id: int, payload: dict) -> None:
        if not isinstance(payload, dict):
            return
        with self._lock:
            a = self._alts.get(alt_id)
            if not a:
                # Ignore heartbeats from repositories that are not part of the
                # configured installation; do not create phantom dashboard alts.
                return
            a.online = True
            a.last_heartbeat_ts = time.time()

            def text_value(name: str, old: str, *, limit: int | None = None) -> str:
                value = payload.get(name)
                if not isinstance(value, str):
                    return old
                return value[:limit] if limit is not None else value

            a.version = text_value("version", a.version, limit=80)
            incoming_type = text_value("ad_type", a.ad_type, limit=10).lower()
            if incoming_type in ("sell", "buy"):
                a.ad_type = incoming_type
            try:
                r = payload.get("rate")
                if r is not None:
                    value = float(r)
                    if math.isfinite(value):
                        a.rate = value
            except (TypeError, ValueError, OverflowError):
                pass
            a.rate_currency = text_value("rate_currency", a.rate_currency, limit=30)
            a.message_preview = text_value("message_preview", a.message_preview, limit=120)

            def as_int(name: str, old: int) -> int:
                try:
                    value = int(payload[name]) if name in payload else old
                    return max(0, value)
                except (TypeError, ValueError, OverflowError):
                    return old

            def as_float(name: str, old: float) -> float:
                try:
                    value = float(payload[name]) if name in payload else old
                    return value if math.isfinite(value) and value >= 0 else old
                except (TypeError, ValueError, OverflowError):
                    return old

            a.total_sent = as_int("total_sent", a.total_sent)
            a.total_errors = as_int("total_errors", a.total_errors)
            a.total_skips = as_int("total_skips", a.total_skips)
            a.total_edits = as_int("total_edits", a.total_edits)
            a.uptime_sec = as_float("uptime_sec", a.uptime_sec)
            a.active_channels = as_int("active_channels", a.active_channels)
            a.total_channels = as_int("total_channels", a.total_channels)
            a.last_post_ts = as_float("last_post_ts", a.last_post_ts)
            incoming_status = text_value("status", a.status, limit=20)
            if incoming_status in {
                "active", "paused", "caution", "ip_pause", "afk", "stopped", "error", "offline",
            }:
                a.status = incoming_status
            if "warnings" in payload and isinstance(payload.get("warnings"), list):
                a.warnings = [str(w)[:300] for w in payload["warnings"][:25]]
            if isinstance(payload.get("channels"), dict):
                a.channels = dict(payload["channels"])
            a.run_started_ts = as_float("run_started_ts", a.run_started_ts)
            a.ip_org = text_value("ip_org", a.ip_org, limit=120)
            a.ip_country = text_value("ip_country", a.ip_country, limit=20)

    def mark_offline_stale(self, offline_after_sec: float) -> None:
        with self._lock:
            now = time.time()
            for a in self._alts.values():
                if a.last_heartbeat_ts and (now - a.last_heartbeat_ts) > offline_after_sec:
                    a.online = False
                    if a.status not in ("stopped", "offline"):
                        a.status = "offline"

    def set_workflow(self, alt_id: int, run_id: Optional[int], status: str, conclusion: str = "") -> None:
        with self._lock:
            a = self._alts.get(alt_id)
            if not a:
                return
            a.workflow_run_id = run_id
            a.workflow_status = status
            a.workflow_conclusion = conclusion
            if status in ("completed", "failure", "cancelled"):
                if conclusion == "failure":
                    a.status = "error"
                elif conclusion == "cancelled":
                    a.status = "stopped"
                else:
                    # Completed successfully — a later dashboard refresh
                    # applies the configured stale-heartbeat threshold.
                    if a.last_heartbeat_ts and (time.time() - a.last_heartbeat_ts) > self._offline_after_sec:
                        a.status = "offline"
            elif status == "in_progress":
                if a.status in ("offline", "stopped", "error", ""):
                    a.status = "starting"
            elif status == "queued":
                a.status = "queued"

    def set_dm_ack(self, alt_id: int, text: str) -> None:
        with self._lock:
            a = self._alts.get(alt_id)
            if not a:
                return
            a.dm_ack = text
            a.dm_ack_ts = time.time()

    def set_run_config(self, alt_id: int, *, ad_type=None, rate=None, message=None) -> None:
        """Called when we issue a setprice/setmode/setmessage DM."""
        with self._lock:
            a = self._alts.get(alt_id)
            if not a:
                return
            if ad_type in ("sell", "buy"):
                a.ad_type = ad_type
            if rate is not None:
                try:
                    value = float(rate)
                    if math.isfinite(value):
                        a.rate = value
                except (TypeError, ValueError, OverflowError):
                    pass
            if message is not None and isinstance(message, str):
                a.message_preview = message[:120]

    # ----- per-alt log ring buffer -----
    def append_log(self, alt_id: int, text: str, emoji: str = "•", color: int = 0x2F3136) -> None:
        with self._lock:
            if alt_id not in self._alts:
                return
            buf = self._log_buffer.setdefault(alt_id, [])
            buf.append((time.time(), emoji, color, text))
            if len(buf) > 500:
                del buf[:-500]

    def recent_logs(self, alt_id: int, limit: int = 20) -> list[tuple[float, str, int, str]]:
        with self._lock:
            buf = list(self._log_buffer.get(alt_id, []))
        return buf[-limit:]
