"""Pure embed builders (dict-like ``Embed`` objects; no discord.py)."""
from __future__ import annotations

from typing import Iterable, Sequence

from ..core.models import DAY, Alt, Customer, RunState
from ..security.policy import COMMAND_TIERS, Tier, commands_for
from ..telemetry.fleet_state import LiveAlt
from .ports import Embed

GREEN, YELLOW, RED, BLURPLE, GREY = 0x57F287, 0xFEE75C, 0xED4245, 0x5865F2, 0x2F3136
STATUS_COLORS = {"active": GREEN, "paused": YELLOW, "caution": YELLOW, "ip_pause": RED, "afk": BLURPLE, "stopped": RED, "error": RED, "offline": GREY, "queued": BLURPLE, "starting": BLURPLE}
STATUS_ICON = {"active": "🟢", "paused": "⏸️", "caution": "⚠️", "ip_pause": "🛑", "afk": "💤", "stopped": "🔴", "error": "❌", "offline": "⚫", "queued": "🕒", "starting": "🚀"}

COMMAND_HELP = {
    "help": "Show this help.",
    "getstarted": "How the service works and how to buy a plan.",
    "account": "Your plan, expiry, alts and ticket links.",
    "setup": "Store an alt's Discord token and target channels (private modal).",
    "run": "Start an alt: mode, price, message, cadence and runtime.",
    "stop": "Stop an alt's live run (30-45 s SLA).",
    "pause": "Pause public posts of an alt (keeps the run alive).",
    "resume": "Resume a paused alt.",
    "tune": "Change price, message, mode, cadence, runtime or policy without restarting.",
    "channels": "View / add / replace / remove / rescan target channels.",
    "deals": "Deal scanner keywords, edge threshold and on/off switch.",
    "status": "Live status of your alts (refresh from GitHub, post a dashboard card).",
    "reply": "Send a DM reply through an alt to a buyer.",
    "alt": "Overview, logs, runs and self-check for one alt.",
    "renew": "Open a renewal ticket (manual crypto payment).",
    "pause-billing": "Request a billing pause.",
    "proofs": "Post your payment proof (tx hash + screenshot).",
    "vip": "VIP features: DM auto-reply, squads.",
    "admin": "Operator tools (admin rooms only).",
}


def _ts(epoch: float) -> str:
    return f"<t:{int(epoch)}:R>" if epoch else "never"


def help_embed(tier: Tier) -> Embed:
    embed = Embed(title="AdFarm — commands", color=BLURPLE, description=f"Your access level: **{tier.value}**")
    for name in commands_for(tier):
        embed.add(f"/{name}", f"{COMMAND_HELP.get(name, '')}  ·  tier `{COMMAND_TIERS[name].value}`")
    embed.footer = "Customer commands work only inside your private customer hub."
    return embed


def account_embed(customer: Customer, alts: Sequence[Alt], now: float, *, policy_acked: bool) -> Embed:
    days = customer.days_remaining(now)
    color = GREEN if days > 7 else YELLOW if days > 1 else RED
    embed = Embed(title=f"Account · {customer.username}", color=color)
    embed.add("Plan", f"{'VIP' if customer.vip else 'Standard'} · {customer.alt_count} alt(s)", True)
    embed.add("Status", "✅ active" if customer.is_active(now) else "⛔ inactive", True)
    embed.add("Expires", f"<t:{int(customer.expiry_date)}:D> ({days:.1f} days left)", True)
    embed.add("Alts", "\n".join(f"`{a.alt_index}` {a.label} · {a.status.value} · {len(a.channel_ids)} ch" for a in alts) or "none registered — use /setup")
    embed.add("Policy", "acknowledged ✅" if policy_acked else "not acknowledged — you will be asked before the first run")
    return embed


def alt_status_embed(alt: Alt, live: LiveAlt | None, run: RunState | None, now: float, *, reveal_infra: bool = False) -> Embed:
    """Customer-facing status card.

    ``reveal_infra`` is False for customers: the repo slug exposes the worker GitHub account and
    the internal repo layout, which is operator information only.
    """
    status = live.status if live and live.online else (run.status if run and run.status in ("queued", "in_progress") else "offline")
    embed = Embed(title=f"{STATUS_ICON.get(status, '•')} Alt {alt.alt_index} · {alt.label}", color=STATUS_COLORS.get(status, GREY))
    embed.add("Status", f"`{status}`", True)
    if reveal_infra:
        embed.add("Repo", f"`{alt.repo_slug}`", True)
    embed.add("Channels", f"{len(alt.channel_ids)} configured" + (f" · {live.active_channels}/{live.total_channels} alive" if live and live.online else ""), True)
    if run:
        embed.add("Run", f"{run.mode.value} · {run.runtime_hours or '∞'}h · started {_ts(run.started_at)} · renewals {run.renewals}", False)
    if live and live.online:
        embed.add("Mode / Rate", f"`{live.ad_type or '—'}` · `{live.rate if live.rate is not None else '—'}$/1k` · every {live.interval_min or '—'}m", True)
        embed.add("Activity", f"sent `{live.total_sent}` · errors `{live.total_errors}` · skips `{live.total_skips}`", True)
        embed.add("Health", f"{live.health_index}/100 · heartbeat {_ts(live.last_heartbeat_at)}", True)
        if live.last_error:
            embed.add("Latest issue", live.last_error[:300])
        if live.warnings:
            embed.add("Warnings", "\n".join(live.warnings[:5]))
    else:
        embed.add("Telemetry", "no heartbeat received yet" if not live or not live.last_heartbeat_at else f"last heartbeat {_ts(live.last_heartbeat_at)}")
    return embed


def fleet_overview_embed(rows: Iterable[tuple[Customer, Alt, LiveAlt | None, RunState | None]], now: float, *, title: str = "Fleet overview") -> Embed:
    embed = Embed(title=title, color=BLURPLE)
    lines = []
    for customer, alt, live, run in rows:
        status = live.status if live and live.online else (run.status if run and run.status in ("queued", "in_progress") else "offline")
        lines.append(f"{STATUS_ICON.get(status, '•')} **{customer.username}** · alt {alt.alt_index} `{alt.label}` · `{status}`"
                     + (f" · sent {live.total_sent}" if live and live.online else ""))
    embed.description = "\n".join(lines[:40]) or "No alts registered."
    embed.footer = f"{len(lines)} alt(s)"
    return embed


def customer_card(customer: Customer, alts: Sequence[Alt], now: float, *, webhooks_ok: bool) -> Embed:
    embed = Embed(title=f"Customer · {customer.username} ({customer.discord_id})", color=GREEN if customer.is_active(now) else RED)
    embed.add("Plan", f"{'VIP' if customer.vip else 'Standard'} · {customer.alt_count} alt(s)", True)
    embed.add("Active", "yes" if customer.active else "no", True)
    embed.add("Expiry", f"<t:{int(customer.expiry_date)}:F> ({customer.days_remaining(now):.1f} d)", True)
    embed.add("Forum", f"<#{customer.forum_id}>" if customer.forum_id else "none", True)
    embed.add("Webhooks", "✅ complete" if webhooks_ok else "❌ missing — run /admin alt action:sync", True)
    embed.add("Alts", "\n".join(f"`{a.alt_index}` {a.label} · `{a.repo_slug}` · {a.status.value} · sync {a.sync_state.value}" for a in alts) or "none")
    return embed


def reminder_embed(days_left: float, expiry: float, payment_address: str) -> Embed:
    embed = Embed(title="⏰ Subscription reminder", color=YELLOW if days_left > 1 else RED)
    embed.description = f"Your AdFarm plan expires <t:{int(expiry)}:R> (<t:{int(expiry)}:D>).\nUse `/renew` in your hub to keep your alts running."
    if payment_address:
        embed.add("Payment (BEP-20)", f"`{payment_address}`")
    return embed


def expiry_notice_embed(expiry: float) -> Embed:
    embed = Embed(title="⛔ Subscription expired", color=RED)
    embed.description = (f"Your plan expired <t:{int(expiry)}:R>. All alts were stopped and your hub is read-only.\n"
                         "Use `/renew` in the ticket channel to reactivate — your channel configuration is kept.")
    return embed


def ban_notice_embed(alt: Alt, credit_days: float) -> Embed:
    embed = Embed(title="⚠️ Alt banned", color=RED)
    embed.description = (f"Alt {alt.alt_index} (`{alt.label}`) reported a ban/invalid token.\n"
                         f"• Old repo renamed for audit; a replacement repo is ready.\n"
                         f"• Time credit applied: **{credit_days:.1f} day(s)**.\n"
                         f"• Run `/setup` with a fresh account to continue — your channels are reused.")
    return embed
