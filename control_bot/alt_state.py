"""V6 control-bot state for configured alts.

Only configured alt IDs are accepted. Unknown heartbeat payloads and malformed
fields are ignored so stale/orphaned installations cannot create phantom state.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AltState:
    alt_id: int
    name: str
    online: bool = False
    version: str = ""
    ad_type: str = ""
    rate: Optional[float] = None
    rate_currency: str = "$/1k"
    message_preview: str = ""
    interval_min: int = 5
    runtime_hours: int = 6
    total_sent: int = 0
    total_errors: int = 0
    total_skips: int = 0
    total_edits: int = 0
    uptime_sec: float = 0.0
    active_channels: int = 0
    total_channels: int = 0
    last_post_ts: float = 0.0
    last_heartbeat_ts: float = 0.0
    status: str = "offline"
    warnings: list = field(default_factory=list)
    channels: dict = field(default_factory=dict)
    run_started_ts: float = 0.0
    workflow_run_id: Optional[int] = None
    workflow_status: str = ""
    workflow_conclusion: str = ""
    dm_ack: str = ""
    dm_ack_ts: float = 0.0
    ip_org: str = ""
    ip_country: str = ""
    deal_alerts: int = 0
    last_deal_ts: float = 0.0
    deal_keywords: list[str] = field(default_factory=list)
    deal_scan_enabled: bool = True
    deal_alert_delta: float = 0.05
    last_error: str = ""
    log_counts: dict = field(default_factory=dict)
    squad: str = ""
    tags: list[str] = field(default_factory=list)
    policy_template: str = "balanced"
    causal_history: list = field(default_factory=list)
    hourly_activity: list = field(default_factory=lambda: [0] * 24)


class AltStateManager:
    """Thread-safe collection of live state for known alts."""

    def __init__(self, alt_names: dict[int, str], alt_ids=None,
                 offline_after_sec: float = 300):
        self._lock = threading.RLock()
        self._offline_after_sec = offline_after_sec
        configured = set(alt_ids) if alt_ids is not None else set(alt_names)
        self._alts: dict[int, AltState] = {
            idx: AltState(idx, alt_names.get(idx, f"Alt {idx}"))
            for idx in sorted(configured)
        }
        self._log_buffer: dict[int, list[tuple[float, str, int, str]]] = {
            idx: [] for idx in self._alts
        }

    @property
    def alt_ids(self) -> list[int]:
        with self._lock:
            return sorted(self._alts)

    def get(self, alt_id: int) -> Optional[AltState]:
        with self._lock:
            return self._alts.get(alt_id)

    def get_name(self, alt_id: int) -> str:
        with self._lock:
            a = self._alts.get(alt_id)
            return a.name if a else f"Alt {alt_id}"

    def add_alt(self, alt_id: int, name: str) -> bool:
        """Add one validated alt to the live control-bot registry."""
        with self._lock:
            if alt_id in self._alts:
                return False
            self._alts[alt_id] = AltState(alt_id, str(name).strip()[:80] or f"Alt {alt_id}")
            self._log_buffer[alt_id] = []
            return True

    def update_identity(self, alt_id: int, *, name: str | None = None) -> bool:
        with self._lock:
            alt = self._alts.get(alt_id)
            if not alt:
                return False
            if name is not None and str(name).strip():
                alt.name = str(name).strip()[:80]
            return True

    def remove_alt(self, alt_id: int) -> bool:
        """Remove an alt from the live registry without touching GitHub."""
        with self._lock:
            if alt_id not in self._alts:
                return False
            self._alts.pop(alt_id, None)
            self._log_buffer.pop(alt_id, None)
            return True

    def is_online(self, alt_id: int) -> bool:
        with self._lock:
            alt = self._alts.get(alt_id)
            return bool(alt and alt.online)

    def all(self) -> list[AltState]:
        with self._lock:
            return list(self._alts.values())

    @staticmethod
    def _int_value(value, default: int = 0) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError, OverflowError):
            return default

    @staticmethod
    def _float_value(value, default: float = 0.0) -> float:
        try:
            result = float(value)
            return result if math.isfinite(result) and result >= 0 else default
        except (TypeError, ValueError, OverflowError):
            return default

    @staticmethod
    def _text_value(payload: dict, name: str, old: str, limit: int) -> str:
        value = payload.get(name)
        return value[:limit] if isinstance(value, str) else old

    def update_from_heartbeat(self, alt_id: int, payload: dict) -> None:
        if not isinstance(payload, dict):
            return
        with self._lock:
            alt = self._alts.get(alt_id)
            if not alt:
                return
            alt.online = True
            alt.last_heartbeat_ts = time.time()
            alt.version = self._text_value(payload, "version", alt.version, 80)
            incoming_type = self._text_value(payload, "ad_type", alt.ad_type, 10).lower()
            if incoming_type in {"sell", "buy"}:
                alt.ad_type = incoming_type
            if "rate" in payload:
                try:
                    rate = float(payload["rate"])
                    if math.isfinite(rate):
                        alt.rate = rate
                except (TypeError, ValueError, OverflowError):
                    pass
            alt.rate_currency = self._text_value(payload, "rate_currency", alt.rate_currency, 30)
            alt.message_preview = self._text_value(payload, "message_preview", alt.message_preview, 120)
            for field_name in ("interval_min", "runtime_hours"):
                if field_name in payload:
                    value = self._int_value(payload[field_name], getattr(alt, field_name))
                    if field_name == "interval_min" and value in {3, 5}:
                        alt.interval_min = value
                    elif field_name == "runtime_hours" and value in {6, 12, 18, 24, 48}:
                        alt.runtime_hours = value
            if isinstance(payload.get("policy_template"), str) and payload["policy_template"] in {"stealth", "aggressive", "peak_hour", "balanced"}:
                alt.policy_template = payload["policy_template"]
            for field_name in (
                "total_sent", "total_errors", "total_skips", "total_edits",
                "active_channels", "total_channels", "deal_alerts",
            ):
                if field_name in payload:
                    setattr(alt, field_name, self._int_value(payload[field_name], getattr(alt, field_name)))
            for field_name in ("uptime_sec", "last_post_ts", "run_started_ts", "last_deal_ts"):
                if field_name in payload:
                    setattr(alt, field_name, self._float_value(payload[field_name], getattr(alt, field_name)))
            old_status = alt.status
            status = self._text_value(payload, "status", alt.status, 20).lower()
            if status == "running":
                status = "active"
            if status in {"active", "paused", "caution", "ip_pause", "afk", "stopped", "error", "offline", "starting", "queued"}:
                if old_status != status:
                    self.record_causal_event(alt_id, "status_transition", f"Status changed from {old_status} -> {status}", details=payload.get("last_error", ""))
                alt.status = status
            if isinstance(payload.get("warnings"), list):
                alt.warnings = [str(item)[:300] for item in payload["warnings"][:25]]
            if isinstance(payload.get("channels"), dict):
                # Keep only bounded, dictionary-shaped channel records.
                alt.channels = {
                    str(cid)[:30]: dict(raw) for cid, raw in list(payload["channels"].items())[:100]
                    if isinstance(raw, dict)
                }
            alt.ip_org = self._text_value(payload, "ip_org", alt.ip_org, 120)
            alt.ip_country = self._text_value(payload, "ip_country", alt.ip_country, 20)
            if isinstance(payload.get("last_error"), str):
                alt.last_error = payload["last_error"][:300]
            if isinstance(payload.get("deal_keywords"), list):
                alt.deal_keywords = [str(item)[:60] for item in payload["deal_keywords"][:20] if str(item).strip()]
            if isinstance(payload.get("deal_scan_enabled"), bool):
                alt.deal_scan_enabled = payload["deal_scan_enabled"]
            if "deal_alert_delta" in payload:
                try:
                    delta = float(payload["deal_alert_delta"])
                    if math.isfinite(delta) and 0 <= delta <= 5:
                        alt.deal_alert_delta = delta
                except (TypeError, ValueError, OverflowError):
                    pass
            if isinstance(payload.get("log_counts"), dict):
                alt.log_counts = {
                    str(key)[:40]: self._int_value(value)
                    for key, value in list(payload["log_counts"].items())[:30]
                }

    def mark_offline_stale(self, offline_after_sec: float | None = None) -> None:
        threshold = self._offline_after_sec if offline_after_sec is None else offline_after_sec
        with self._lock:
            now = time.time()
            for alt in self._alts.values():
                effective_last_seen = max(alt.last_heartbeat_ts, alt.last_post_ts)
                if effective_last_seen and now - effective_last_seen > threshold:
                    alt.online = False
                    if alt.status not in {"stopped", "offline"}:
                        alt.status = "offline"

    def set_workflow(self, alt_id: int, run_id: Optional[int], status: str, conclusion: str = "") -> None:
        with self._lock:
            alt = self._alts.get(alt_id)
            if not alt:
                return
            alt.workflow_run_id = run_id
            alt.workflow_status = str(status or "")[:30]
            alt.workflow_conclusion = str(conclusion or "")[:30]
            if conclusion == "failure":
                alt.status = "error"
            elif conclusion == "cancelled":
                alt.status = "stopped"
            elif status == "in_progress" and alt.status in {"offline", "stopped", "error", ""}:
                alt.status = "starting"
            elif status == "queued":
                if not alt.online and alt.status in {"offline", "stopped", "error", ""}:
                    alt.status = "queued"

    def set_dm_ack(self, alt_id: int, text: str) -> None:
        with self._lock:
            alt = self._alts.get(alt_id)
            if alt:
                alt.dm_ack, alt.dm_ack_ts = str(text)[:500], time.time()

    def set_run_config(self, alt_id: int, *, ad_type=None, rate=None, message=None,
                       interval_min=None, runtime_hours=None) -> None:
        with self._lock:
            alt = self._alts.get(alt_id)
            if not alt:
                return
            if ad_type in {"sell", "buy"}:
                alt.ad_type = ad_type
            if rate is not None:
                try:
                    value = float(rate)
                    if math.isfinite(value):
                        alt.rate = value
                except (TypeError, ValueError, OverflowError):
                    pass
            if isinstance(message, str):
                alt.message_preview = message[:120]
            if interval_min in {3, 5}:
                alt.interval_min = int(interval_min)
            if runtime_hours in {6, 12, 18, 24, 48}:
                alt.runtime_hours = int(runtime_hours)

    def set_deal_keywords(self, alt_id: int, keywords: list[str]) -> None:
        with self._lock:
            alt = self._alts.get(alt_id)
            if alt:
                alt.deal_keywords = [str(item).strip()[:60] for item in keywords[:20] if str(item).strip()]

    def set_deal_config(self, alt_id: int, *, enabled=None, delta=None) -> None:
        with self._lock:
            alt = self._alts.get(alt_id)
            if not alt:
                return
            if isinstance(enabled, bool):
                alt.deal_scan_enabled = enabled
            if delta is not None:
                try:
                    value = float(delta)
                    if math.isfinite(value) and 0 <= value <= 5:
                        alt.deal_alert_delta = value
                except (TypeError, ValueError, OverflowError):
                    pass

    def set_error(self, alt_id: int, text: str) -> None:
        with self._lock:
            alt = self._alts.get(alt_id)
            if alt:
                alt.last_error = str(text)[:300]
                alt.status = "error"

    def record_deal(self, alt_id: int, text: str = "") -> None:
        """Record a deal when a caller has no heartbeat counter available."""
        with self._lock:
            alt = self._alts.get(alt_id)
            if alt:
                alt.deal_alerts += 1
                alt.last_deal_ts = time.time()

    def mark_deal_seen(self, alt_id: int) -> None:
        """Update deal recency without double-counting heartbeat totals."""
        with self._lock:
            alt = self._alts.get(alt_id)
            if alt:
                alt.last_deal_ts = time.time()

    def set_channel(self, alt_id: int, channel_id: str, name: str = "") -> None:
        with self._lock:
            alt = self._alts.get(alt_id)
            cid = str(channel_id).strip()
            if not alt or not cid.isdigit():
                return
            record = alt.channels.setdefault(cid, {})
            if name:
                record["name"] = str(name)[:80]
            record.setdefault("sent", 0)
            record.setdefault("errors", 0)
            record.setdefault("last_post", 0)
            alt.total_channels = max(alt.total_channels, len(alt.channels))

    def replace_channel(self, alt_id: int, old_id: str, new_id: str, name: str = "") -> None:
        with self._lock:
            alt = self._alts.get(alt_id)
            old, new = str(old_id).strip(), str(new_id).strip()
            if not alt or not new.isdigit():
                return
            record = alt.channels.pop(old, {})
            if name:
                record["name"] = str(name)[:80]
            alt.channels[new] = record

    def append_log(self, alt_id: int, text: str, emoji: str = "•",
                   color: int = 0x2F3136, kind: str = "INFO") -> None:
        with self._lock:
            if alt_id not in self._alts:
                return
            category = str(kind or "INFO").upper()[:24]
            alt = self._alts[alt_id]
            alt.log_counts[category] = self._int_value(alt.log_counts.get(category)) + 1
            body = f"[{category}] {str(text)[:500]}"
            buf = self._log_buffer.setdefault(alt_id, [])
            buf.append((time.time(), emoji, color, body))
            if len(buf) > 500:
                del buf[:-500]

    def recent_logs(self, alt_id: int, limit: int = 20, kind: str | None = None) -> list[tuple[float, str, int, str]]:
        with self._lock:
            entries = list(self._log_buffer.get(alt_id, []))
        if kind and kind.upper() != "ALL":
            marker = f"[{kind.upper()}]"
            entries = [entry for entry in entries if marker in entry[3]]
        return entries[-max(1, min(100, int(limit))):]

    def clear_logs(self, alt_id: int) -> None:
        with self._lock:
            if alt_id in self._log_buffer:
                self._log_buffer[alt_id].clear()

    def reset_caution(self, alt_id: int, channel_id: str | None = None) -> bool:
        with self._lock:
            alt = self._alts.get(alt_id)
            if not alt:
                return False
            if alt.status in {"caution", "ip_pause"}:
                alt.status = "active"
            if channel_id:
                cid_str = str(channel_id).strip()
                ch = alt.channels.get(cid_str)
                if ch:
                    ch["errors"] = 0
                    ch["slowmode"] = 0
                    ch["alive"] = True
            else:
                for ch in alt.channels.values():
                    ch["errors"] = 0
                    ch["slowmode"] = 0
                    ch["alive"] = True
            alt.warnings = [w for w in alt.warnings if "caution" not in str(w).lower() and "strike" not in str(w).lower()]
            alt.last_error = ""
            return True

    def get_health_index(self, alt_id: int) -> int:
        with self._lock:
            alt = self._alts.get(alt_id)
            if not alt:
                return 0
            if not alt.online or alt.status in {"offline", "stopped"}:
                return 0
            score = 100
            if alt.status == "caution":
                score -= 35
            elif alt.status in {"ip_pause", "error"}:
                score -= 60
            elif alt.status in {"paused", "afk"}:
                score -= 10
            total_ops = alt.total_sent + alt.total_errors
            if total_ops > 0:
                err_ratio = alt.total_errors / total_ops
                score -= int(err_ratio * 40)
            if alt.last_heartbeat_ts > 0:
                age = time.time() - alt.last_heartbeat_ts
                if age > 180:
                    score -= min(30, int((age - 180) / 10))
            return max(0, min(100, score))

    def get_activity_sparkline(self, alt_id: int) -> str:
        with self._lock:
            buf = self._log_buffer.get(alt_id, [])
            if not buf:
                return "      "
            now = time.time()
            buckets = [0] * 6
            for ts, _emo, _col, _body in buf:
                age = now - ts
                idx = int(age // 600)
                if 0 <= idx < 6:
                    buckets[5 - idx] += 1
            max_v = max(buckets) if max(buckets) > 0 else 1
            chars = [" ", " ", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
            return "".join(chars[min(len(chars) - 1, int((v / max_v) * (len(chars) - 1)))] for v in buckets)

    def get_channel_yield(self, alt_id: int, channel_id: str) -> str:
        with self._lock:
            alt = self._alts.get(alt_id)
            if not alt or channel_id not in alt.channels:
                return "N/A"
            ch = alt.channels[channel_id]
            sent = ch.get("sent", 0)
            err = ch.get("errors", 0)
            alive = ch.get("alive", True)
            if not alive or ch.get("slowmode", 0) >= 300:
                return "⚠️ Caution"
            if sent + err == 0:
                return "✨ Fresh"
            ratio = sent / max(1, sent + err * 2)
            if ratio >= 0.95:
                return "🟢 A+"
            if ratio >= 0.80:
                return "🟢 A"
            if ratio >= 0.60:
                return "🟡 B"
            return "🔴 C"

    def get_yield_grade(self, alt_id: int) -> str:
        score = self.get_health_index(alt_id)
        if score >= 90:
            return "Grade A+ (Prime)"
        if score >= 75:
            return "Grade A (Good)"
        if score >= 50:
            return "Grade B (Fair)"
        if score > 0:
            return "Grade C (Degraded)"
        return "Grade F (Offline)"

    def record_causal_event(self, alt_id: int, event_type: str, description: str, details: str = "") -> None:
        with self._lock:
            alt = self._alts.get(alt_id)
            if not alt:
                return
            event = {
                "ts": time.time(),
                "type": str(event_type)[:30],
                "description": str(description)[:200],
                "details": str(details)[:300],
            }
            alt.causal_history.append(event)
            if len(alt.causal_history) > 25:
                alt.causal_history = alt.causal_history[-25:]

    def get_causal_timeline(self, alt_id: int) -> list[dict]:
        with self._lock:
            alt = self._alts.get(alt_id)
            if not alt:
                return []
            return list(alt.causal_history)

    def set_squad(self, alt_id: int, squad: str) -> bool:
        with self._lock:
            alt = self._alts.get(alt_id)
            if not alt:
                return False
            alt.squad = str(squad or "").strip()[:50]
            self.record_causal_event(alt_id, "squad_assigned", f"Assigned to squad '{alt.squad or 'None'}'")
            return True

    def set_tags(self, alt_id: int, tags: list[str]) -> bool:
        with self._lock:
            alt = self._alts.get(alt_id)
            if not alt:
                return False
            alt.tags = [str(t).strip()[:30] for t in tags if str(t).strip()][:10]
            return True

    def get_squad_members(self, squad: str) -> list[AltState]:
        target = str(squad or "").strip().lower()
        with self._lock:
            return [alt for alt in self._alts.values() if alt.squad.lower() == target]

    def get_all_squads(self) -> dict[str, list[int]]:
        with self._lock:
            squads: dict[str, list[int]] = {}
            for alt in self._alts.values():
                sq = alt.squad or "Unassigned"
                squads.setdefault(sq, []).append(alt.alt_id)
            return squads

    def set_policy_template(self, alt_id: int, template: str) -> bool:
        t_clean = str(template or "").strip().lower()
        if t_clean not in {"stealth", "aggressive", "peak_hour", "balanced"}:
            return False
        with self._lock:
            alt = self._alts.get(alt_id)
            if not alt:
                return False
            alt.policy_template = t_clean
            if t_clean == "stealth":
                alt.interval_min = 5
            elif t_clean in {"aggressive", "peak_hour"}:
                alt.interval_min = 3
            elif t_clean == "balanced":
                alt.interval_min = 5
            self.record_causal_event(alt_id, "policy_applied", f"Applied policy template '{t_clean}'")
            return True

    def get_hourly_heatmap(self, alt_id: int) -> str:
        with self._lock:
            alt = self._alts.get(alt_id)
            if not alt:
                return "░" * 24
            buf = self._log_buffer.get(alt_id, [])
            now = time.time()
            hours = [0] * 24
            for ts, _emo, _col, _body in buf:
                age_hr = int((now - ts) // 3600)
                if 0 <= age_hr < 24:
                    hours[23 - age_hr] += 1
            max_h = max(hours) if max(hours) > 0 else 1
            shades = ["░", "▒", "▓", "█"]
            return "".join(shades[min(len(shades) - 1, int((v / max_h) * (len(shades) - 1)))] for v in hours)
