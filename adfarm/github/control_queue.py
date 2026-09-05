"""ControlQueue — the Gist command bus between the control bot and a running sender.

Protocol (unchanged from send_ads.py V6 ``_sync_control_gist``; the sender is byte-identical):

  file  : ``control_<ALT_ID>.json`` inside CONTROL_GIST_ID
  body  : {"alt_id": int, "command_id": str, "command": str, "args": str, "issued_at": float,
           ...runtime overrides (paused, rate, ad_type, message, interval_min, deal_keywords,
           deal_scan_enabled, deal_alert_delta)}
  ack   : the sender rewrites the same file adding ``ack_id``, ``ack``, ``ack_at``.

The sender polls every SYNC_GIST_INTERVAL_SEC (30 s) so a command is applied within ~30-45 s.
A ``stop`` issued before the current run started is ignored by the sender (stale-stop guard).
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from ..core.clock import Clock, SystemClock

log = logging.getLogger(__name__)

# Commands the sender understands (send_ads.py `_handle_controller_dm`).
SENDER_COMMANDS = frozenset({
    "ping", "status", "pause", "resume", "stop", "setprice", "setmode", "setmessage", "setinterval",
    "setruntime", "policy", "setdealkeywords", "setdealscan", "setdealdelta", "setchannel",
    "replacechannel", "removechannel", "setchannels", "rescan", "resetcaution", "reply", "sync", "help",
})


class GistFiles(Protocol):
    def gist_file(self, gist_id: str, filename: str) -> Optional[str]: ...
    def update_gist(self, gist_id: str, files: dict[str, Optional[str]]) -> dict: ...


@dataclass(frozen=True)
class QueuedCommand:
    command_id: str
    command: str
    args: str
    issued_at: float
    filename: str

    @property
    def summary(self) -> str:
        return (f"{self.command} {self.args}".strip())[:120]


@dataclass(frozen=True)
class Ack:
    command_id: str
    text: str
    acked_at: float


class ControlQueue:
    def __init__(self, gists: GistFiles | None, gist_id: str, *, clock: Clock | None = None):
        self.gists = gists
        self.gist_id = (gist_id or "").strip()
        self.clock = clock or SystemClock()

    @property
    def enabled(self) -> bool:
        return bool(self.gist_id and self.gists is not None)

    @staticmethod
    def filename(sender_alt_id: int) -> str:
        return f"control_{int(sender_alt_id)}.json"

    # ── write ───────────────────────────────────────────────────────────────
    def enqueue(self, sender_alt_id: int, command: str, args: str = "", *, overrides: dict[str, Any] | None = None) -> QueuedCommand:
        command = command.strip().lower()
        if command not in SENDER_COMMANDS:
            raise ValueError(f"unknown sender command: {command}")
        if not self.enabled:
            raise RuntimeError("CONTROL_GIST_ID is not configured")
        payload: dict[str, Any] = dict(self.read_raw(sender_alt_id) or {})
        # Drop the previous ack so the new command's ack is unambiguous.
        for key in ("ack", "ack_id", "ack_at"):
            payload.pop(key, None)
        payload.update(overrides or {})
        now = self.clock.now()
        cid = uuid.uuid4().hex[:16]
        payload.update({"alt_id": int(sender_alt_id), "command_id": cid, "command": command, "args": str(args or "")[:1900], "issued_at": now})
        fname = self.filename(sender_alt_id)
        self.gists.update_gist(self.gist_id, {fname: json.dumps(payload, ensure_ascii=False)})  # type: ignore[union-attr]
        return QueuedCommand(command_id=cid, command=command, args=str(args or ""), issued_at=now, filename=fname)

    def set_overrides(self, sender_alt_id: int, **overrides: Any) -> None:
        """Persist runtime overrides without a command (the sender applies them on every sync)."""
        if not self.enabled:
            raise RuntimeError("CONTROL_GIST_ID is not configured")
        payload = dict(self.read_raw(sender_alt_id) or {})
        payload.update({k: v for k, v in overrides.items() if v is not None})
        payload["alt_id"] = int(sender_alt_id)
        self.gists.update_gist(self.gist_id, {self.filename(sender_alt_id): json.dumps(payload, ensure_ascii=False)})  # type: ignore[union-attr]

    def clear(self, sender_alt_id: int) -> None:
        if self.enabled:
            self.gists.update_gist(self.gist_id, {self.filename(sender_alt_id): None})  # type: ignore[union-attr]

    # ── read ────────────────────────────────────────────────────────────────
    def read_raw(self, sender_alt_id: int) -> Optional[dict[str, Any]]:
        if not self.enabled:
            return None
        try:
            raw = self.gists.gist_file(self.gist_id, self.filename(sender_alt_id))  # type: ignore[union-attr]
        except Exception as exc:
            log.warning("control gist read failed: %s", exc)
            return None
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def ack_for(self, sender_alt_id: int, command_id: str) -> Optional[Ack]:
        data = self.read_raw(sender_alt_id)
        if not data or str(data.get("ack_id") or "") != command_id:
            return None
        try:
            acked_at = float(data.get("ack_at") or 0)
        except (TypeError, ValueError):
            acked_at = 0.0
        return Ack(command_id=command_id, text=str(data.get("ack") or ""), acked_at=acked_at)

    # ── convenience wrappers (one per customer-facing action) ───────────────
    def pause(self, alt: int) -> QueuedCommand:
        return self.enqueue(alt, "pause", overrides={"paused": True})

    def resume(self, alt: int) -> QueuedCommand:
        return self.enqueue(alt, "resume", overrides={"paused": False})

    def stop(self, alt: int) -> QueuedCommand:
        return self.enqueue(alt, "stop")

    def set_price(self, alt: int, rate: float) -> QueuedCommand:
        return self.enqueue(alt, "setprice", f"{rate:.2f}", overrides={"rate": float(rate)})

    def set_mode(self, alt: int, ad_type: str) -> QueuedCommand:
        return self.enqueue(alt, "setmode", ad_type, overrides={"ad_type": ad_type})

    def set_message(self, alt: int, message: str) -> QueuedCommand:
        return self.enqueue(alt, "setmessage", message, overrides={"message": message[:1900]})

    def set_interval(self, alt: int, minutes: int) -> QueuedCommand:
        return self.enqueue(alt, "setinterval", str(int(minutes)), overrides={"interval_min": int(minutes)})

    def set_runtime(self, alt: int, hours: int) -> QueuedCommand:
        return self.enqueue(alt, "setruntime", str(int(hours)))

    def set_policy(self, alt: int, template: str, defaults: dict[str, Any]) -> QueuedCommand:
        return self.enqueue(alt, "policy", template, overrides=defaults)

    def set_deal_keywords(self, alt: int, keywords: tuple[str, ...]) -> QueuedCommand:
        return self.enqueue(alt, "setdealkeywords", ",".join(keywords), overrides={"deal_keywords": list(keywords)})

    def set_deal_scan(self, alt: int, enabled: bool) -> QueuedCommand:
        return self.enqueue(alt, "setdealscan", "on" if enabled else "off", overrides={"deal_scan_enabled": bool(enabled)})

    def set_deal_delta(self, alt: int, delta: float) -> QueuedCommand:
        return self.enqueue(alt, "setdealdelta", f"{delta:.2f}", overrides={"deal_alert_delta": float(delta)})

    def set_channels(self, alt: int, channel_ids: tuple[str, ...]) -> QueuedCommand:
        return self.enqueue(alt, "setchannels", ",".join(channel_ids))

    def add_channel(self, alt: int, channel_id: str, name: str = "") -> QueuedCommand:
        return self.enqueue(alt, "setchannel", f"{channel_id} {name}".strip())

    def replace_channel(self, alt: int, old: str, new: str) -> QueuedCommand:
        return self.enqueue(alt, "replacechannel", f"{old} {new}")

    def remove_channel(self, alt: int, channel_id: str) -> QueuedCommand:
        return self.enqueue(alt, "removechannel", channel_id)

    def rescan(self, alt: int) -> QueuedCommand:
        return self.enqueue(alt, "rescan")

    def reset_caution(self, alt: int, channel_id: str = "") -> QueuedCommand:
        return self.enqueue(alt, "resetcaution", channel_id)

    def reply(self, alt: int, user_id: str, text: str) -> QueuedCommand:
        return self.enqueue(alt, "reply", f"{user_id} {text}")

    def sync(self, alt: int) -> QueuedCommand:
        return self.enqueue(alt, "sync")
