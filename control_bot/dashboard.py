"""V6 dashboard builders for live alt state.

The dashboard is deliberately derived from the latest heartbeat and GitHub run
state, not from setup-time assumptions. It produces three stable embeds:
summary, channels, and alerts.
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone

import discord

from . import config
from .alt_state import AltState, AltStateManager

_GREEN = 0x57F287
_YELLOW = 0xFEE75C
_RED = 0xED4245
_BLUE = 0x5865F2
_GREY = 0x2F3136


def _status_dot(a: AltState) -> tuple[str, int]:
    status = (a.status or "offline").lower()
    if status == "active":
        return "🟢", _GREEN
    if status in {"starting", "queued", "paused", "afk"}:
        return "🟡", _YELLOW
    if status in {"caution", "ip_pause"}:
        return "⚠️", _YELLOW
    if status in {"error", "stopped"}:
        return "🔴", _RED
    if status == "offline":
        return "⚫", _GREY
    return "⚪", _GREY


def _fmt_ago(ts: float) -> str:
    try:
        value = float(ts)
    except (TypeError, ValueError, OverflowError):
        return "—"
    if not math.isfinite(value) or value <= 0:
        return "—"
    delta = max(0.0, time.time() - value)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{delta / 3600:.1f}h ago"
    return f"{delta / 86400:.1f}d ago"


def _fmt_duration(seconds: float) -> str:
    try:
        value = float(seconds)
    except (TypeError, ValueError, OverflowError):
        return "—"
    if not math.isfinite(value) or value < 0:
        return "—"
    if value < 60:
        return f"{int(value)}s"
    if value < 3600:
        return f"{int(value // 60)}m"
    return f"{value / 3600:.1f}h"


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_float(value, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _rate_str(a: AltState) -> str:
    if a.rate is None:
        return "—"
    try:
        value = float(a.rate)
    except (TypeError, ValueError, OverflowError):
        return "—"
    if not math.isfinite(value):
        return "—"
    return f"{value:.2f}{str(a.rate_currency or '$/1k')[:20]}"


def _mode_icon(mode: str) -> str:
    return "💰" if (mode or "").lower() == "sell" else "🛒" if (mode or "").lower() == "buy" else "❔"


def build_summary_embed(mgr: AltStateManager) -> discord.Embed:
    mgr.mark_offline_stale(config.OFFLINE_AFTER_SEC)
    alts = mgr.all()
    online = sum(
        1 for a in alts
        if a.online and a.status not in {"offline", "stopped", "error"}
    )
    embed = discord.Embed(
        title=f"📊 V6 ALT DASHBOARD · {online}/{len(alts)} online",
        color=_BLUE,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="Live heartbeat + GitHub Actions state")

    lines: list[str] = []
    sent = errors = skips = edits = deals = channels = 0
    for alt in alts:
        dot, _ = _status_dot(alt)
        workflow = alt.workflow_status or "no workflow data"
        if alt.workflow_conclusion:
            workflow += f"/{alt.workflow_conclusion}"
        lines.append(
            f"{dot} **{alt.name}** {_mode_icon(alt.ad_type)} `{alt.ad_type or 'unknown'}` "
            f"@ **{_rate_str(alt)}** · `{alt.status}` · "
            f"sent **{alt.total_sent}** · err **{alt.total_errors}** · "
            f"ch **{alt.active_channels}/{alt.total_channels}** · "
            f"cadence **{alt.interval_min}m/{alt.runtime_hours}h** · "
            f"last {_fmt_ago(alt.last_post_ts)} · run `{workflow}`"
        )
        sent += alt.total_sent
        errors += alt.total_errors
        skips += alt.total_skips
        edits += alt.total_edits
        deals += alt.deal_alerts
        channels += alt.total_channels

    embed.description = "\n".join(lines)[:4000] if lines else "_No configured alts._"
    embed.add_field(
        name="📈 Current totals",
        value=(
            f"Sent: **{sent}** · Errors: **{errors}** · Skips: **{skips}**\n"
            f"Edits: **{edits}** · Deal alerts: **{deals}** · Channels: **{channels}**"
        ),
        inline=False,
    )
    return embed


def build_channels_embed(mgr: AltStateManager) -> discord.Embed:
    mgr.mark_offline_stale(config.OFFLINE_AFTER_SEC)
    embed = discord.Embed(
        title="📌 V6 CHANNEL ACTIVITY",
        color=_BLUE,
        timestamp=datetime.now(timezone.utc),
    )
    rows: list[str] = []
    for alt in mgr.all():
        if not alt.channels:
            rows.append(f"**{alt.name}** — no channel heartbeat received yet")
            continue
        for cid, raw in alt.channels.items():
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or cid)[:70]
            sent = _safe_int(raw.get("sent"))
            errors = _safe_int(raw.get("errors"))
            last_post = _safe_float(raw.get("last_post"))
            alive = bool(raw.get("alive"))
            dot = "🟢" if alive else "⚫"
            rows.append(
                f"{dot} **{alt.name} · #{name}** `{cid}` · sent **{sent}** · "
                f"err **{errors}** · last {_fmt_ago(last_post)}"
            )
    if not rows:
        embed.description = "_No channel data yet. Waiting for a V6 heartbeat._"
    else:
        embed.description = "\n".join(rows[:45])[:4000]
    return embed


def build_alerts_embed(mgr: AltStateManager) -> discord.Embed:
    mgr.mark_offline_stale(config.OFFLINE_AFTER_SEC)
    rows: list[str] = []
    for alt in mgr.all():
        for warning in (alt.warnings or []):
            rows.append(f"⚠️ **{alt.name}** — {str(warning)[:300]}")
        if alt.last_error:
            rows.append(f"🔴 **{alt.name}** — {alt.last_error[:300]}")
        if alt.status == "caution":
            rows.append(f"⚠️ **{alt.name}** — caution mode is active")
        elif alt.status == "ip_pause":
            rows.append(f"🚨 **{alt.name}** — egress safety pause is active")
        elif alt.status == "error":
            rows.append(f"🔴 **{alt.name}** — workflow or sender error")
        if alt.workflow_status in {"queued", "in_progress"} and not alt.online:
            rows.append(f"🟡 **{alt.name}** — workflow is {alt.workflow_status}, but heartbeat is stale")
    if rows:
        return discord.Embed(
            title="⚠️ V6 ACTIVE ALERTS",
            description="\n".join(rows[-30:])[:4000],
            color=_YELLOW,
            timestamp=datetime.now(timezone.utc),
        )
    return discord.Embed(
        title="✅ V6 NO ACTIVE ALERTS",
        description="No active alerts from the latest heartbeat or GitHub state.",
        color=_GREEN,
        timestamp=datetime.now(timezone.utc),
    )


def build_all(mgr: AltStateManager) -> list[discord.Embed]:
    """Return the current three-embed dashboard snapshot."""
    return [build_summary_embed(mgr), build_channels_embed(mgr), build_alerts_embed(mgr)]


def build_single_alt_embed(mgr: AltStateManager, alt_id: int) -> discord.Embed:
    mgr.mark_offline_stale(config.OFFLINE_AFTER_SEC)
    alt = mgr.get(alt_id)
    if not alt:
        return discord.Embed(
            title="❓ Unknown alt",
            description=f"Alt `{alt_id}` is not configured.",
            color=_RED,
        )
    dot, color = _status_dot(alt)
    embed = discord.Embed(
        title=f"{dot} {alt.name} {_mode_icon(alt.ad_type)} · V6",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Mode", value=f"`{alt.ad_type or '—'}`", inline=True)
    embed.add_field(name="Rate", value=_rate_str(alt), inline=True)
    embed.add_field(name="Status", value=f"`{alt.status}`", inline=True)
    embed.add_field(name="Cadence / runtime", value=f"{alt.interval_min}m / {alt.runtime_hours}h", inline=True)
    embed.add_field(name="Online", value="yes" if alt.online else "no", inline=True)
    embed.add_field(name="Sent / errors / skips", value=f"{alt.total_sent} / {alt.total_errors} / {alt.total_skips}", inline=True)
    embed.add_field(name="Edits / deals", value=f"{alt.total_edits} / {alt.deal_alerts}", inline=True)
    embed.add_field(name="Channels", value=f"{alt.active_channels}/{alt.total_channels}", inline=True)
    embed.add_field(name="Deal scanner", value=f"{'ON' if alt.deal_scan_enabled else 'OFF'} · edge ${alt.deal_alert_delta:.2f}/1k", inline=True)
    embed.add_field(name="Deal item keywords", value=", ".join(alt.deal_keywords[:20]) or "—", inline=False)
    embed.add_field(name="Uptime", value=_fmt_duration(alt.uptime_sec), inline=True)
    embed.add_field(name="Last heartbeat", value=_fmt_ago(alt.last_heartbeat_ts), inline=True)
    embed.add_field(name="Last post", value=_fmt_ago(alt.last_post_ts), inline=True)
    if alt.workflow_status:
        value = f"`{alt.workflow_status}`"
        if alt.workflow_conclusion:
            value += f" / `{alt.workflow_conclusion}`"
        if alt.workflow_run_id:
            value += f"\nrun `{alt.workflow_run_id}`"
        embed.add_field(name="GitHub", value=value, inline=False)
    if alt.message_preview:
        embed.add_field(name="Message preview", value=f"```\n{alt.message_preview[:300]}\n```", inline=False)
    if alt.ip_org or alt.ip_country:
        embed.add_field(name="Egress", value=f"{alt.ip_org or '?'} · {alt.ip_country or '?'}", inline=True)
    if alt.warnings or alt.last_error:
        alert_text = "\n".join([*(str(x)[:300] for x in alt.warnings[:6]), alt.last_error[:300]])
        embed.add_field(name="Alerts", value=alert_text[:1024], inline=False)
    channel_lines = []
    for cid, raw in list((alt.channels or {}).items())[:15]:
        if isinstance(raw, dict):
            channel_lines.append(
                f"`{cid}` {raw.get('name') or ''} · sent {_safe_int(raw.get('sent'))} · "
                f"last {_fmt_ago(_safe_float(raw.get('last_post')))}"
            )
    if channel_lines:
        embed.add_field(name="Channel detail", value="\n".join(channel_lines)[:1024], inline=False)
    return embed
