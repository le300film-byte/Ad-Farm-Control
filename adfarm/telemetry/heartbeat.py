"""Heartbeat parsing — pure functions turning a sender webhook message into a ``Heartbeat``.

The sender (send_ads.py ``_send_heartbeat``) posts an embed titled ``💓 Heartbeat · <ALT_NAME>``
whose footer carries ``alt_id=<ALT_ID> · <VERSION>`` and whose fields are:
Status, Mode, Rate, Cadence, Activity, Deals, Scanner, Keywords, Uptime, Channels, Message,
Latest issue, Warnings and one ``Channel: <id> · #<name>`` field per channel.
A legacy JSON body (``{"heartbeat": true, ...}``) is also accepted.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

STATUSES = ("active", "paused", "caution", "ip_pause", "afk", "stopped", "error", "offline", "starting", "queued")
_STATUS_RE = re.compile(r"\b(" + "|".join(STATUSES) + r")\b", re.I)
_NUM = r"(\d+(?:\.\d+)?)"


@dataclass(frozen=True)
class ChannelStat:
    channel_id: str
    name: str = ""
    sent: int = 0
    errors: int = 0
    slowmode: int = 0
    alive: bool = True
    last_post: int = 0


@dataclass(frozen=True)
class Heartbeat:
    sender_alt_id: int
    alt_name: str = ""
    version: str = ""
    status: str = "active"
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
    channels: tuple[ChannelStat, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class EmbedLike:
    """Framework-neutral embed (discord.Embed → EmbedLike is a one-liner in discord/replies.py)."""

    title: str = ""
    description: str = ""
    footer: str = ""
    fields: Sequence[tuple[str, str]] = ()


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _float(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def parse_json_heartbeat(raw: str) -> Optional[Heartbeat]:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`").split("\n", 1)[-1].strip()
    if not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, Mapping) or not (data.get("heartbeat") or data.get("type") == "heartbeat"):
        return None
    alt_id = _int(data.get("alt_id"), 0)
    if alt_id <= 0:
        return None
    channels = tuple(
        ChannelStat(channel_id=str(cid), name=str(info.get("name") or ""), sent=_int(info.get("sent")), errors=_int(info.get("errors")),
                    slowmode=_int(info.get("slowmode")), alive=bool(info.get("alive", True)), last_post=_int(info.get("last_post")))
        for cid, info in (data.get("channels") or {}).items() if isinstance(info, Mapping)
    ) if isinstance(data.get("channels"), Mapping) else ()
    status = str(data.get("status") or "active").lower()
    return Heartbeat(
        sender_alt_id=alt_id, alt_name=str(data.get("alt_name") or ""), version=str(data.get("version") or ""),
        status="active" if status == "running" else (status if status in STATUSES else "active"),
        ad_type=str(data.get("ad_type") or "").lower(), rate=_float(data.get("rate")),
        interval_min=_int(data.get("interval_min")) or None,
        total_sent=_int(data.get("total_sent")), total_errors=_int(data.get("total_errors")), total_skips=_int(data.get("total_skips")),
        deal_alerts=_int(data.get("deal_alerts")),
        deal_scan_enabled=data.get("deal_scan_enabled") if isinstance(data.get("deal_scan_enabled"), bool) else None,
        deal_alert_delta=_float(data.get("deal_alert_delta")),
        deal_keywords=tuple(str(k) for k in data.get("deal_keywords", []) if str(k).strip()) if isinstance(data.get("deal_keywords"), list) else (),
        uptime_sec=_float(data.get("uptime_sec")) or 0.0,
        active_channels=_int(data.get("active_channels")), total_channels=_int(data.get("total_channels")),
        message_preview=str(data.get("message_preview") or "")[:120], last_error=str(data.get("last_error") or "")[:300],
        warnings=tuple(str(w)[:300] for w in data.get("warnings", [])[:25]) if isinstance(data.get("warnings"), list) else (),
        channels=channels, raw=dict(data),
    )


def parse_embed_heartbeat(embed: EmbedLike) -> Optional[Heartbeat]:
    title = str(embed.title or "")
    if not title.lower().startswith("💓 heartbeat"):
        return None
    m = re.search(r"alt[_\s-]?id\s*=\s*(\d+)|alt[_\s-]?(\d+)", embed.footer or "", re.I)
    if not m:
        return None
    alt_id = int(m.group(1) or m.group(2))
    alt_name = title.split("·", 1)[1].strip() if "·" in title else ""
    version = ""
    vm = re.search(r"·\s*(V[\d.]+[^·]*)", embed.footer or "")
    if vm:
        version = vm.group(1).strip()

    values: dict[str, Any] = {}
    channels: list[ChannelStat] = []
    for name, value in embed.fields:
        key = str(name or "").strip().lower()
        value = str(value or "").strip()
        if key == "status":
            sm = _STATUS_RE.search(value)
            if sm:
                values["status"] = sm.group(1).lower()
        elif key == "mode":
            mm = re.search(r"\b(sell|buy)\b", value, re.I)
            if mm:
                values["ad_type"] = mm.group(1).lower()
        elif key == "rate":
            rm = re.search(_NUM, value)
            if rm:
                values["rate"] = float(rm.group(1))
        elif key in {"cadence", "interval"}:
            im = re.search(r"(\d+)", value)
            if im:
                values["interval_min"] = int(im.group(1))
        elif key == "activity":
            for label, target in (("sent", "total_sent"), ("errors", "total_errors"), ("skips", "total_skips")):
                am = re.search(rf"{label}:?\s*`?(\d+)", value, re.I)
                if am:
                    values[target] = int(am.group(1))
        elif key == "deals":
            dm = re.search(r"(\d+)", value)
            if dm:
                values["deal_alerts"] = int(dm.group(1))
        elif key == "keywords":
            values["deal_keywords"] = tuple(x.strip() for x in value.split(",") if x.strip() and x.strip().lower() != "none configured")
        elif key == "scanner":
            values["deal_scan_enabled"] = value.casefold().startswith("on")
            em = re.search(r"edge\s*\$?" + _NUM, value, re.I)
            if em:
                values["deal_alert_delta"] = float(em.group(1))
        elif key == "uptime":
            um = re.search(_NUM + r"\s*min", value, re.I)
            if um:
                values["uptime_sec"] = float(um.group(1)) * 60
        elif key == "channels":
            cm = re.search(r"(\d+)\s*/\s*(\d+)", value)
            if cm:
                values["active_channels"], values["total_channels"] = int(cm.group(1)), int(cm.group(2))
        elif key == "message":
            values["message_preview"] = value[:120]
        elif key in {"latest issue", "latest error"}:
            values["last_error"] = value[:300]
        elif key == "warnings":
            values["warnings"] = tuple(x.strip() for x in value.splitlines() if x.strip())[:25]
        elif key.startswith("channel:"):
            cid_m = re.search(r"channel:\s*(\d+)", name, re.I)
            if not cid_m:
                continue
            cid = cid_m.group(1)
            ch_name = name.split("· #", 1)[1].strip() if "· #" in name else cid
            sent = re.search(r"sent\s*`?(\d+)", value, re.I)
            errors = re.search(r"errors\s*`?(\d+)", value, re.I)
            slow = re.search(r"slowmode\s*`?(\d+)", value, re.I)
            last = re.search(r"last\s+<t:(\d+):", value, re.I)
            channels.append(ChannelStat(
                channel_id=cid, name=ch_name[:80], sent=int(sent.group(1)) if sent else 0, errors=int(errors.group(1)) if errors else 0,
                slowmode=int(slow.group(1)) if slow else 0, alive="alive" in value.casefold(), last_post=int(last.group(1)) if last else 0,
            ))
    return Heartbeat(sender_alt_id=alt_id, alt_name=alt_name, version=version, channels=tuple(channels), raw={"embed": True},
                     **{k: v for k, v in values.items()})


def parse_heartbeat(content: str, embeds: Sequence[EmbedLike]) -> Optional[Heartbeat]:
    for embed in embeds:
        hb = parse_embed_heartbeat(embed)
        if hb:
            return hb
    return parse_json_heartbeat(content)
