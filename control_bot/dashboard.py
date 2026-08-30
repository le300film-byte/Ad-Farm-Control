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
        health = mgr.get_health_index(alt.alt_id)
        spark = mgr.get_activity_sparkline(alt.alt_id)
        lines.append(
            f"{dot} **{alt.name}** {_mode_icon(alt.ad_type)} `{alt.ad_type or 'unknown'}` "
            f"@ **{_rate_str(alt)}** · Health **{health}%** `[{spark}]` · `{alt.status}` · "
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
            yield_rating = mgr.get_channel_yield(alt.alt_id, cid)
            rows.append(
                f"{dot} **{alt.name} · #{name}** `{cid}` · Yield **{yield_rating}** · sent **{sent}** · "
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
    embed.add_field(name="Health Score", value=f"**{mgr.get_health_index(alt.alt_id)}%** `[{mgr.get_activity_sparkline(alt.alt_id)}]`", inline=True)
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


def build_topology_embed(mgr: AltStateManager) -> discord.Embed:
    """Render the live topological relationship graph of the fleet."""
    mgr.mark_offline_stale(config.OFFLINE_AFTER_SEC)
    alts = mgr.all()
    embed = discord.Embed(
        title="🌐 FLEET TOPOLOGY & ROUTING GRAPH",
        color=_BLUE,
        timestamp=datetime.now(timezone.utc),
    )
    if not alts:
        embed.description = "No configured alts in topology."
        return embed

    nodes = []
    for a in alts:
        dot, _ = _status_dot(a)
        squad_tag = f"[{a.squad}]" if a.squad else "[Unassigned]"
        health = mgr.get_health_index(a.alt_id)
        node = [
            f"**{dot} Alt {a.alt_id}: {a.name}** {squad_tag} · Health `{health}%`",
            f"   ├─ 📍 **Target Channels ({len(a.channels)})**:",
        ]
        if a.channels:
            for cid, ch in list(a.channels.items())[:6]:
                yield_grade = mgr.get_channel_yield(a.alt_id, cid)
                ch_name = ch.get("name") or cid
                node.append(f"   │  └─ `#{ch_name}` (`{cid}`) [{yield_grade}]")
            if len(a.channels) > 6:
                node.append(f"   │  └─ *+ {len(a.channels)-6} more channels*")
        else:
            node.append("   │  └─ *No channels active*")

        egress_info = f"{a.ip_org or 'Direct'} ({a.ip_country or 'Unknown'})"
        node.append(f"   ├─ 🛡️ **Egress Routing**: `{egress_info}`")
        node.append(f"   └─ 🔄 **Sync Bridge**: Gist `{_fmt_ago(a.last_heartbeat_ts)}`")
        nodes.append("\n".join(node))

    embed.description = "\n\n".join(nodes)[:4000]
    return embed


def build_diagnose_embed(mgr: AltStateManager, alt_id: int) -> discord.Embed:
    """Render the 'Why Did This Happen?' causal diagnostic explorer embed."""
    mgr.mark_offline_stale(config.OFFLINE_AFTER_SEC)
    alt = mgr.get(alt_id)
    if not alt:
        return discord.Embed(
            title="❓ Unknown Alt",
            description=f"Alt `{alt_id}` is not configured.",
            color=_RED,
        )
    dot, color = _status_dot(alt)
    health = mgr.get_health_index(alt_id)
    timeline = mgr.get_causal_timeline(alt_id)

    embed = discord.Embed(
        title=f"🔍 Causal Event Explorer · {dot} {alt.name} (#{alt.alt_id})",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Current Health Index", value=f"**{health}%** `[{mgr.get_activity_sparkline(alt_id)}]`", inline=True)
    embed.add_field(name="Operational Status", value=f"`{alt.status}` (online: {alt.online})", inline=True)
    embed.add_field(name="Active Policy", value=f"`{alt.policy_template}` ({alt.interval_min}m interval)", inline=True)

    if alt.status == "caution":
        assessment = "⚠️ **CAUTION MODE ACTIVE**: Message deletions or verification misses detected. The alt is throttling interval (2x) and sending text-only."
        action = f"Run `/resetcaution alt:{alt_id}` after verifying channel anti-spam or waiting for the 3-post survival streak."
    elif alt.status == "ip_pause":
        assessment = "🚨 **EGRESS IP PAUSE**: Outbound IP failed datacenter/WARP check. Public posting paused."
        action = f"Check Cloudflare WARP connection or restart workflow run with `/start alt:{alt_id}`."
    elif alt.status == "error":
        assessment = f"❌ **ERROR STATE**: Last recorded error: `{alt.last_error or 'Unknown exception'}`"
        action = f"Inspect recent execution output with `/logs alt:{alt_id}`."
    elif alt.status == "active":
        assessment = "✅ **HEALTHY**: Transmission cadence is steady, circuit breakers are CLOSED, and verification survival rate is optimal."
        action = "No operator intervention required."
    else:
        assessment = f"ℹ️ **STATUS: {alt.status.upper()}**: Alt is currently {alt.status}."
        action = f"Use `/start alt:{alt_id}` to dispatch a new posting cycle if desired."

    embed.add_field(name="Root-Cause Diagnostic Analysis", value=assessment, inline=False)
    embed.add_field(name="Recommended Operator Action", value=action, inline=False)

    if timeline:
        history_lines = []
        for ev in reversed(timeline[-8:]):
            ts_str = _fmt_ago(ev["ts"])
            ev_type = ev.get("type", "event").upper()
            desc = ev.get("description", "")
            detail = f" — `{ev['details']}`" if ev.get("details") else ""
            history_lines.append(f"• **[{ts_str}]** `{ev_type}`: {desc}{detail}")
        embed.add_field(name="Recent Causal Chain Timeline", value="\n".join(history_lines)[:1024], inline=False)
    else:
        embed.add_field(name="Recent Causal Chain Timeline", value="*No causal state transitions recorded during current run.*", inline=False)

    return embed


def _render_bar(val: float, max_val: float, length: int = 8) -> str:
    if max_val <= 0:
        return "▱" * length
    ratio = min(1.0, max(0.0, val / max_val))
    filled = int(round(ratio * length))
    return "▰" * filled + "▱" * (length - filled)


def build_analytics_embed(mgr: AltStateManager, target_alt: int = 0) -> discord.Embed:
    """Render comprehensive visual analytics, channel speeds, and cadence metrics."""
    mgr.mark_offline_stale(config.OFFLINE_AFTER_SEC)
    alts = mgr.all() if target_alt == 0 else ([mgr.get(target_alt)] if mgr.get(target_alt) else [])

    embed = discord.Embed(
        title="📊 ADVANCED FLEET ANALYTICS & SPEED MATRIX",
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc),
    )
    if not alts:
        embed.description = "No alt account telemetry available."
        return embed

    total_sent = sum(a.total_sent for a in alts)
    total_errors = sum(a.total_errors for a in alts)
    total_skips = sum(a.total_skips for a in alts)
    total_edits = sum(a.total_edits for a in alts)
    total_deals = sum(a.deal_alerts for a in alts)
    success_rate = (total_sent / (total_sent + total_errors) * 100) if (total_sent + total_errors) > 0 else 100.0

    embed.description = (
        f"**Fleet Scope**: `{len(alts)}` alt(s) | **Delivery Success Rate**: `{success_rate:.1f}%`\n"
        f"**Throughput**: `{total_sent}` posts | **Errors**: `{total_errors}` | **Random Skips**: `{total_skips}` | **Edits**: `{total_edits}` | **Deals**: `{total_deals}`"
    )

    # 1. Per-Alt Throughput & Health Performance
    perf_lines = []
    for a in alts:
        dot, _ = _status_dot(a)
        uptime_hr = max(0.1, a.uptime_sec / 3600.0)
        rate_hr = a.total_sent / uptime_hr
        health = mgr.get_health_index(a.alt_id)
        grade = mgr.get_yield_grade(a.alt_id)
        bar = _render_bar(health, 100, length=8)
        squad_tag = f"[{a.squad}]" if a.squad else ""
        perf_lines.append(
            f"**{dot} Alt {a.alt_id} ({a.name})** {squad_tag}\n"
            f"└ Health: `[{bar}] {health}% ({grade})` | Cadence: `~{rate_hr:.1f} posts/hr` | Mode: `{a.ad_type.upper()}`"
        )
    embed.add_field(name="🚀 Alt Throughput & Health Gauges", value="\n".join(perf_lines)[:1024] or "No active runners", inline=False)

    # 2. Per-Channel Speed & Cadence Matrix
    ch_map = {}
    for a in alts:
        for cid, ch in a.channels.items():
            ch_map.setdefault(cid, []).append((a, ch))

    if ch_map:
        ch_lines = []
        for cid, entries in list(ch_map.items())[:8]:
            a, ch = entries[0]
            ch_name = ch.get("name") or cid
            sent = ch.get("sent", 0)
            err = ch.get("errors", 0)
            slow = ch.get("slowmode", 0)
            rel = (sent / (sent + err) * 100) if (sent + err) > 0 else 100.0
            last_ts = ch.get("last_post", 0)
            last_str = f"<t:{int(last_ts)}:R>" if last_ts > 0 else "never"
            bar = _render_bar(rel, 100, length=6)
            ch_lines.append(
                f"**#{ch_name}** (`{cid}`)\n"
                f"└ Reliability: `[{bar}] {rel:.1f}%` ({sent} sent / {err} err) | Slowmode: `{slow}s` | Last: {last_str}"
            )
        embed.add_field(name="⚡ Channel Reliability & Slowmode Utilization", value="\n".join(ch_lines)[:1024], inline=False)

    # 3. Inter-Channel Interval & Anti-Detection Matrix
    matrix_lines = [
        "• **Typo Mutation Permutations**: `12–25%` dynamic character transposition with positive survival scoring",
        "• **Chat Velocity Cadence**: `3m–5m` interval ± dynamic jitter auto-scaled by channel message volume",
        "• **Multi-Alt Fleet Separation**: `90s` minimum window enforced to prevent channel collision",
        "• **Arbitrage Deal Scanner**: Real-time supplier under-market & buyer premium categorization",
    ]
    embed.add_field(name="🛡️ Anti-Detection & Traffic Cadence Engine", value="\n".join(matrix_lines), inline=False)
    embed.set_footer(text="Live Fleet Analytics • Updated in real time")
    return embed
