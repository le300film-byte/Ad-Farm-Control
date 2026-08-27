"""control_bot.dashboard — builds the unified dashboard embeds."""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone

import discord

from . import config
from .alt_state import AltState, AltStateManager


# Discord colors
_GREEN  = 0x57F287
_YELLOW = 0xFEE75C
_RED    = 0xED4245
_BLUE   = 0x5865F2
_GREY   = 0x2F3136


def _status_dot(a: AltState) -> tuple[str, int]:
    """Return (emoji dot, color) for the alt's current status."""
    s = a.status
    if s == "active":
        return "🟢", _GREEN
    if s in ("paused", "starting", "queued"):
        return "🟡", _YELLOW
    if s in ("caution", "ip_pause"):
        return "⚠️", _YELLOW
    if s in ("stopped", "error"):
        return "🔴", _RED
    if s == "offline":
        return "⚫", _GREY
    return "⚪", _GREY


def _fmt_ago(ts: float) -> str:
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(ts) or not ts:
        return "—"
    delta = time.time() - ts
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta//60)}m ago"
    if delta < 86400:
        return f"{delta/3600:.1f}h ago"
    return f"{delta/86400:.1f}d ago"


def _fmt_duration(sec: float) -> str:
    try:
        sec = float(sec)
    except (TypeError, ValueError):
        sec = 0.0
    if not math.isfinite(sec) or sec < 0:
        sec = 0.0
    if sec < 60:
        return f"{int(sec)}s"
    if sec < 3600:
        return f"{int(sec//60)}m"
    return f"{sec/3600:.1f}h"


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_float(value, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _rate_str(a: AltState) -> str:
    if a.rate is None:
        return "—"
    try:
        rate = float(a.rate)
        if not math.isfinite(rate):
            return "—"
    except (TypeError, ValueError, OverflowError):
        return "—"
    currency = str(a.rate_currency or "$/1k")[:30]
    return f"{rate:.2f}{currency}"


def _ad_icon(ad_type: str) -> str:
    """Choose the market icon from the live heartbeat mode."""
    if (ad_type or "").lower() == "sell":
        return "💰"
    if (ad_type or "").lower() == "buy":
        return "🛒"
    return "❔"


def build_summary_embed(mgr: AltStateManager) -> discord.Embed:
    now = time.time()
    mgr.mark_offline_stale(config.OFFLINE_AFTER_SEC)
    alts = mgr.all()

    online_count = sum(1 for a in alts if a.online and a.status not in ("stopped", "offline", "error"))
    total_count = len(alts)

    em = discord.Embed(
        title=f"📊 ALT DASHBOARD — {online_count}/{total_count} online",
        color=_BLUE,
        timestamp=datetime.now(timezone.utc),
    )
    em.set_footer(text="Auto-refreshed")

    body_lines = []
    tot_sent = tot_err = tot_skip = tot_edits = 0
    tot_ch = 0
    seller_rates, buyer_rates = [], []
    for a in alts:
        dot, _ = _status_dot(a)
        role_icon = _ad_icon(a.ad_type)
        rate = _rate_str(a)
        if (a.ad_type or "").lower() == "sell" and a.rate:
            seller_rates.append((a.name, a.rate))
        if (a.ad_type or "").lower() == "buy" and a.rate:
            buyer_rates.append((a.name, a.rate))
        last_post = _fmt_ago(a.last_post_ts) if a.last_post_ts else "—"
        uptime = _fmt_duration(a.uptime_sec)
        body_lines.append(
            f"{dot} **{a.name}** {role_icon} `{a.ad_type or '—'}` @ **{rate}** · "
            f"sent:**{a.total_sent}** err:**{a.total_errors}** · "
            f"ch:{a.active_channels}/{a.total_channels} · uptime:{uptime} · last:{last_post}"
        )
        tot_sent += a.total_sent
        tot_err += a.total_errors
        tot_skip += a.total_skips
        tot_edits += a.total_edits
        tot_ch += a.total_channels
    em.description = "\n".join(body_lines) if body_lines else "_No alts configured._"

    # Aggregated stats
    em.add_field(
        name="📈 Aggregate",
        value=(f"Sent: **{tot_sent}** · Err: **{tot_err}** · Edits: **{tot_edits}**\n"
               f"Skips: {tot_skip} · Total channels: {tot_ch}"),
        inline=False,
    )

    # Market view
    if seller_rates or buyer_rates:
        sell_str = " · ".join(f"{n}:${r:.2f}" for n, r in sorted(seller_rates, key=lambda x: x[1])) or "—"
        buy_str = " · ".join(f"{n}:${r:.2f}" for n, r in sorted(buyer_rates, key=lambda x: x[1], reverse=True)) or "—"
        em.add_field(name="💰 Sell prices", value=sell_str, inline=True)
        em.add_field(name="🛒 Buy prices",  value=buy_str,  inline=True)

    return em


def build_channels_embed(mgr: AltStateManager) -> discord.Embed:
    mgr.mark_offline_stale(config.OFFLINE_AFTER_SEC)
    em = discord.Embed(title="📌 Per-Channel Activity", color=_BLUE,
                       timestamp=datetime.now(timezone.utc))

    # Aggregate per channel: ch_name -> [(alt_name, sent, last_post)]
    per_ch: dict[str, list[tuple[str, int, float]]] = {}
    per_ch_cid: dict[str, str] = {}
    for a in mgr.all():
        for cid, info in (a.channels or {}).items():
            if not isinstance(info, dict):
                continue
            nm = str(info.get("name") or cid)
            key = f"{nm}|{cid}"
            per_ch.setdefault(key, []).append((a.name, _safe_int(info.get("sent", 0)), _safe_float(info.get("last_post", 0))))
            per_ch_cid[key] = cid

    if not per_ch:
        em.description = "_No channel data yet. Waiting for heartbeats…_"
        return em

    lines = []
    for key in sorted(per_ch.keys()):
        nm, cid = key.split("|", 1)
        entries = sorted(per_ch[key], key=lambda x: -x[2])  # newest first
        active = sum(1 for _, s, lp in entries if lp and (time.time() - lp) < 900)
        lines.append(f"**#{nm}** `{cid}` — {active}/{len(entries)} active")
        for aname, sent, lp in entries:
            dot = "🔥" if lp and (time.time() - lp) < 900 else "💤"
            lines.append(f"  {dot} {aname}: {sent} msgs · last: {_fmt_ago(lp)}")
        lines.append("")
    em.description = "\n".join(lines[:40])[:4000]
    return em


def build_alerts_embed(mgr: AltStateManager) -> discord.Embed:
    mgr.mark_offline_stale(config.OFFLINE_AFTER_SEC)
    lines = []
    for a in mgr.all():
        for w in (a.warnings or []):
            lines.append(f"⚠️ **{a.name}**: {w}")
        if a.status == "ip_pause":
            lines.append(f"🚨 **{a.name}**: IP health — WARP dropped / datacenter detected. Paused.")
        if a.status == "caution":
            lines.append(f"⚠️ **{a.name}**: SHADOWBAN CAUTION MODE — throttled back.")
        if a.status == "error":
            lines.append(f"🔴 **{a.name}**: workflow error / token invalid.")
        if not a.online and a.status == "offline" and a.workflow_status in ("in_progress", "queued"):
            lines.append(f"🟡 **{a.name}**: workflow running but no heartbeat — check logs.")
    if lines:
        title = "⚠️ Active Alerts"
        color = _YELLOW
        description = "\n".join(lines[-25:])[:4000]
    else:
        title = "✅ No active alerts"
        color = _GREEN
        description = "No active alerts. All configured alts are operating within the current safety policy."
    em = discord.Embed(
        title=title,
        color=color,
        description=description,
        timestamp=datetime.now(timezone.utc),
    )
    return em


def build_all(mgr: AltStateManager) -> list[discord.Embed]:
    embeds = [build_summary_embed(mgr), build_channels_embed(mgr)]
    # Always return the stable three-embed layout: summary, channels, alerts.
    embeds.append(build_alerts_embed(mgr))
    return embeds


def build_single_alt_embed(mgr: AltStateManager, alt_id: int) -> discord.Embed:
    a = mgr.get(alt_id)
    if not a:
        return discord.Embed(title="❓ Unknown alt", color=_RED,
                             description=f"No alt with id {alt_id}.")
    mgr.mark_offline_stale(config.OFFLINE_AFTER_SEC)
    dot, color = _status_dot(a)
    em = discord.Embed(
        title=f"{dot} {a.name} {_ad_icon(a.ad_type)}",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    em.add_field(name="Mode", value=f"`{a.ad_type or '—'}`", inline=True)
    em.add_field(name="Rate", value=_rate_str(a), inline=True)
    em.add_field(name="Status", value=a.status, inline=True)
    em.add_field(name="Sent / Err / Edits",
                 value=f"{a.total_sent} / {a.total_errors} / {a.total_edits}", inline=True)
    em.add_field(name="Channels (act/tot)",
                 value=f"{a.active_channels}/{a.total_channels}", inline=True)
    em.add_field(name="Uptime", value=_fmt_duration(a.uptime_sec), inline=True)
    em.add_field(name="Last post", value=_fmt_ago(a.last_post_ts), inline=True)
    em.add_field(name="Last heartbeat", value=_fmt_ago(a.last_heartbeat_ts), inline=True)
    if a.ip_org:
        em.add_field(name="IP",
                     value=f"{a.ip_org} ({a.ip_country or '?'})", inline=True)
    if a.message_preview:
        em.add_field(name="Message", value=f"```\n{a.message_preview[:200]}\n```", inline=False)
    # Warnings
    warns = list(a.warnings or [])
    if a.status == "caution":
        warns.insert(0, "Shadowban caution active")
    if a.status == "ip_pause":
        warns.insert(0, "IP-paused (WARP dropped)")
    if warns:
        em.add_field(name="⚠️ Warnings", value="\n".join(f"• {w}" for w in warns[:8])[:1024],
                     inline=False)
    # Channels
    ch_lines = []
    for cid, info in (a.channels or {}).items():
        if not isinstance(info, dict):
            continue
        nm = str(info.get("name") or cid)
        ch_lines.append(
            f"<#{cid}> (#{nm}): sent **{_safe_int(info.get('sent', 0))}** · last {_fmt_ago(_safe_float(info.get('last_post', 0)))}"
        )
    if ch_lines:
        em.add_field(name="Channels", value="\n".join(ch_lines[:10])[:1024], inline=False)
    if a.workflow_status:
        em.add_field(name="GitHub run",
                     value=f"`{a.workflow_status}`{(' / ' + a.workflow_conclusion) if a.workflow_conclusion else ''} id `{a.workflow_run_id}`",
                     inline=False)
    if a.dm_ack and (time.time() - a.dm_ack_ts) < 600:
        em.add_field(name="Last DM ack", value=a.dm_ack[:500], inline=False)
    return em
