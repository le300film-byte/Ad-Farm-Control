"""control_bot.metrics — TTFTV + 9 watch metrics (TODO 1.5 / 2.1).

All numbers are computed from the ``events`` table in customers.db so they
survive chunk handoffs (the table itself is Gist-backed via write-through).
Posting helpers are separate from pure calculations for offline testability.
"""
from __future__ import annotations

import statistics
import time
from typing import Any

import customer_manager as cm

from . import alerts
from . import ops

ALERT_SURVIVAL_DAYS = 7.0
ALERT_CHURN_PCT = 30.0
ALERT_GITHUB_429_PER_DAY = 50
ALERT_DOWNTIME_HOURS_PER_WEEK = 2.0
ALERT_TICKETS_PER_CUSTOMER = 0.5


# ── event recording helpers ────────────────────────────────────────────────

def note_ticket_open(discord_id: str, source: str = "ticket") -> None:
    cm.record_event(discord_id, "ticket_open", {"source": source})


def note_first_successful_post(discord_id: str) -> None:
    cm.record_event(discord_id, "first_successful_post", {})


def note_ban(discord_id: str, alt: int = 0, reason: str = "") -> None:
    cm.record_event(discord_id, "alt_banned", {"alt": alt, "reason": reason})


def note_kick(discord_id: str, alt: int = 0, reason: str = "") -> None:
    cm.record_event(discord_id, "alt_kicked", {"alt": alt, "reason": reason})


def note_db_restore(source: str = "") -> None:
    cm.record_event("", "db_restore", {"source": source})


def note_github_429(count: int = 1) -> None:
    cm.record_event("", "github_429", {"count": count})


def note_downtime(seconds: float) -> None:
    cm.record_event("", "control_bot_downtime", {"seconds": seconds})


def note_reminder() -> None:
    cm.record_event("", "reminder_dm_sent", {})


def note_ticket() -> None:
    cm.record_event("", "support_ticket", {})


def note_worker_health(ok: int, critical: int) -> None:
    cm.record_event("", "worker_health", {"ok": ok, "critical": critical})


# ── pure calculations ───────────────────────────────────────────────────────

def compute_ttftv(now: float | None = None) -> float | None:
    """Median (ticket_open → first_successful_post) in minutes."""
    now = now or time.time()
    opens = {e["discord_id"]: e["ts"] for e in cm.get_events("ticket_open", until=now, limit=2000)}
    posts = {e["discord_id"]: e["ts"] for e in cm.get_events("first_successful_post", until=now, limit=2000)}
    deltas = []
    for uid, t0 in opens.items():
        t1 = posts.get(uid)
        if t1 and t1 > t0:
            deltas.append((t1 - t0) / 60)
    if not deltas:
        return None
    return float(statistics.median(deltas))


def _median_survival(events: list[dict[str, Any]]) -> float | None:
    lifetimes = []
    for c in cm.list_customers(active_only=False):
        first = min(
            (e["ts"] for e in events if e["discord_id"] == c["discord_id"] and e["event"] == "first_successful_post"),
            default=None,
        )
        banned = min(
            (e["ts"] for e in events if e["discord_id"] == c["discord_id"] and e["event"] in ("alt_banned", "alt_kicked")),
            default=None,
        )
        if first and banned and banned >= first:
            lifetimes.append((banned - first) / 86400)
    if not lifetimes:
        return None
    return float(statistics.median(lifetimes))


def compute_watch_metrics(now: float | None = None) -> dict[str, Any]:
    """All 9 metrics from TODO 2.1 in one shot."""
    now = now or time.time()
    week_ago = now - 7 * 86400
    events = cm.get_events(since=week_ago, limit=5000)

    banned = [e for e in events if e["event"] in ("alt_banned", "alt_kicked")]
    survival = _median_survival(cm.get_events(limit=10000))

    customers_before = cm.get_events("customer_activated", until=now - 7 * 86400, limit=10000)
    active_now = [c for c in cm.list_customers(active_only=True)]
    # churn proxy: distinct customers deactivated in the last 7 days vs active 7d ago
    deactivated_7d = len({e["discord_id"] for e in events if e["event"] == "customer_deactivated"})

    restores = len(cm.get_events("db_restore", since=week_ago))
    gh429 = sum(int((e.get("payload") or {}).get("count", 1)) for e in events if e["event"] == "github_429")
    downtime = sum(float((e.get("payload") or {}).get("seconds", 0)) for e in events if e["event"] == "control_bot_downtime")
    tickets = len(cm.get_events("support_ticket", since=week_ago))
    worker = cm.get_events("worker_token_health", since=week_ago, limit=1)

    churn_pct = round(100 * deactivated_7d / max(1, len(customers_before)), 1)
    return {
        "median_alt_survival_days": survival,
        "survival_alert": bool(survival is not None and survival < ALERT_SURVIVAL_DAYS),
        "customers_active_7d_ago": len(customers_before),
        "customers_deactivated_7d": deactivated_7d,
        "churn_pct": churn_pct,
        "churn_alert": churn_pct > ALERT_CHURN_PCT,
        "db_restores_7d": restores,
        "db_restore_alert": restores > 0,
        "github_429_7d": gh429,
        "github_429_alert": gh429 > ALERT_GITHUB_429_PER_DAY * 7,
        "downtime_7d_hours": round(downtime / 3600, 2),
        "downtime_alert": downtime > ALERT_DOWNTIME_HOURS_PER_WEEK * 3600,
        "reminders_7d": len(events_reminders(events)),
        "tickets_7d": tickets,
        "tickets_alert": tickets > ALERT_TICKETS_PER_CUSTOMER * max(1, len(active_now)),
        "worker_health": worker[0].get("payload", {}) if worker else {},
        "banned_7d": len(banned),
        "mrr": float(cm.get_meta("mrr_ledger", "0") or 0),
    }


def events_reminders(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in events if e["event"] == "reminder_sent"]


def compute_weekly_summary() -> str:
    """Human-readable weekly summary posted to #admin-chat (1.5)."""
    m = compute_watch_metrics()
    ttftv = compute_ttftv()
    lines = [
        "📊 **Weekly Operations Summary**",
        f"• Median alt survival: `{_fmt_days(m['median_alt_survival_days'])}`",
        f"• Bans/kicks (7d): `{m['banned_7d']}`",
        f"• Churn (7d): `{m['churn_pct']}%`",
        f"• DB restores (7d): `{m['db_restores_7d']}`",
        f"• GitHub 429 (7d): `{m['github_429_7d']}`",
        f"• Control-bot downtime (7d): `{m['downtime_7d_hours']}h`",
        f"• Reminder DMs (7d): `{m['reminders_7d']}`",
        f"• Support tickets (7d): `{m['tickets_7d']}`",
        f"• MRR: `${m['mrr']:.2f}`",
        f"• Median TTFTV: `{_fmt_minutes(ttftv)}`",
    ]
    return "\n".join(lines)


def _fmt_days(value: float | None) -> str:
    return f"{value:.1f}d" if value is not None else "n/a"


def _fmt_minutes(value: float | None) -> str:
    return f"{value:.0f} min" if value is not None else "n/a"


async def post_weekly_summary(bot: Any) -> None:
    await alerts.post_admin_chat(compute_weekly_summary())
