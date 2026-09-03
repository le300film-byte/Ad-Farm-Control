"""control_bot.ops — Phase 0.3 / 1.4 / 2.x operational monitors.

Pure functions (computing health results, stats, snapshots) are separated from
Discord posting so they can be unit-tested offline.

Contains:
  * worker-token health checks (0.3): hourly GET /user per org PAT,
    401 → critical alert, 7-day/1-day pre-expiry warnings.
  * heartbeat HTTP endpoint (1.4): lightweight /healthz for external pingers.
  * RSS memory logging every 30 min via /proc/self/status (2.3), alert on
    doubling across a chunk.
  * Gist queue monitoring (2.2): 429s/latency counters, 4000 req/hour alert.
  * Nightly token validation sweep (2.4) via Discord /users/@me.
  * Contextual /tune hints after day 3 (2.6).
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

import discord

import customer_manager as cm

from . import alerts
from . import github_api

VERSION = "v8-phase0"
BOT_START_TS = time.time()

# ── worker token health (0.3) ────────────────────────────────────────────────

WARN_DAYS_7 = 7.0
WARN_DAYS_1 = 1.0


def worker_token_statuses() -> list[dict[str, Any]]:
    """Check every configured worker PAT; no network in pure form (see _check)."""
    from github_dispatch import list_worker_tokens, check_token_status
    results = []
    for entry in list_worker_tokens():
        try:
            status = check_token_status(entry.get("token"), owner=entry.get("owner", ""))
            status.update({"owner": entry.get("owner", "")})
        except Exception as exc:  # noqa: BLE001
            status = {"ok": False, "owner": entry.get("owner", ""), "error": str(exc)}
        results.append(status)
    return results


def classify_token_health(status: dict[str, Any]) -> str:
    """→ 'critical' | 'warning' | 'ok'."""
    if not status.get("ok") and status.get("error"):
        return "critical"
    days = status.get("days_left")
    if days is not None:
        if days <= WARN_DAYS_1:
            return "critical"
        if days <= WARN_DAYS_7:
            return "warning"
    return "ok"


async def post_worker_token_health(bot: Any) -> dict[str, Any]:
    """Run the health check and post any critical/warning to #admin-alerts."""
    results = worker_token_statuses()
    lines = []
    critical = 0
    for r in results:
        level = classify_token_health(r)
        owner = r.get("owner") or r.get("login") or "?"
        if level == "critical":
            critical += 1
            lines.append(f"🔴 `{owner}`: **INVALID/EXPIRED** — {r.get('error') or 'expiring within 1 day'}")
        elif level == "warning":
            lines.append(f"🟡 `{owner}`: expires in **{r.get('days_left', 0):.1f} days** — rotate now")
        else:
            if r.get("days_left") is not None:
                lines.append(f"🟢 `{owner}`: valid, expires {r.get('days_left'):.1f}d")
    if lines:
        await alerts.post_admin_alert(
            "**🤖 Worker token health check**\n" + "\n".join(lines[:20]),
            debounce_key="worker-token-health",
        )
    cm.record_event("", "worker_token_health", {"critical": critical, "ok": len(results) - critical})
    return {"critical": critical, "total": len(results), "results": results}


# ── heartbeat endpoint (1.4) ────────────────────────────────────────────────

_last_external_beat: float = time.time()
_heartbeat_server: Optional[ThreadingHTTPServer] = None


def heartbeat_payload() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "adfarm-control-bot",
        "version": VERSION,
        "uptime_sec": round(time.time() - BOT_START_TS, 1),
        "ts": time.time(),
        "db_gist_ok": bool((_import_gist_backup().LAST_BACKUP or {}).get("ok")) if _gist_known() else None,
        "db_restore": (_import_gist_backup().LAST_RESTORE or {}).get("ok"),
        "lease": (_import_gist_backup().LAST_LEASE or {}).get("ok"),
    }


def _gist_known() -> bool:
    try:
        from gist_backup import gist_configured
        return gist_configured()
    except Exception:
        return False


def _import_gist_backup():
    from gist_backup import LAST_BACKUP, LAST_RESTORE, LAST_LEASE  # noqa: F401
    return type("_GB", (), {"LAST_BACKUP": LAST_BACKUP, "LAST_RESTORE": LAST_RESTORE, "LAST_LEASE": LAST_LEASE})


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path not in ("/healthz", "/health"):
            self.send_response(404)
            self.end_headers()
            return
        mark_external_beat()
        body = json.dumps(heartbeat_payload()).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # silence request spam
        pass


def start_heartbeat_server(host: str = "0.0.0.0", port: int = 0) -> Optional[int]:
    """Start the lightweight HTTP health endpoint in a daemon thread."""
    global _heartbeat_server
    if _heartbeat_server is not None:
        return _heartbeat_server.server_port
    port = int(os.environ.get("HEARTBEAT_PORT", str(port or 8080)) or 8080)
    try:
        _heartbeat_server = ThreadingHTTPServer((host, port), _HealthHandler)
    except OSError as exc:
        print(f"[HEARTBEAT] could not bind {host}:{port}: {exc}")
        return None
    thread = threading.Thread(target=_heartbeat_server.serve_forever, daemon=True,
                              name="heartbeat-http")
    thread.start()
    print(f"[HEARTBEAT] listening on http://{host}:{port}/healthz")
    return _heartbeat_server.server_port


async def check_missed_external_beat(bot: Any, max_gap_sec: float = 15 * 60) -> bool:
    """Alert when no external pinger has prodded /healthz for too long."""
    global _last_external_beat
    gap = time.time() - _last_external_beat
    if gap <= max_gap_sec:
        return False
    await alerts.post_admin_alert(
        f"⚠️ **Heartbeat MISSED** — no external ping for {gap / 60:.0f} min "
        "(expected every 1-5 min from UptimeRobot/Healthchecks). The control "
        "bot may be down or GitHub skipped the cron. Check the workflow.",
        debounce_key="heartbeat-missed",
    )
    _last_external_beat = time.time()
    cm.record_event("", "heartbeat_missed", {"gap_sec": gap})
    return True


def mark_external_beat() -> None:
    global _last_external_beat
    _last_external_beat = time.time()


# ── RSS memory logging (2.3) ────────────────────────────────────────────────

_rss_history: list[dict[str, float]] = []


def rss_snapshot() -> dict[str, float]:
    """Read VmRSS from /proc/self/status (Linux only, no psutil)."""
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    kb = float(line.split()[1])
                    return {"rss_bytes": kb * 1024, "ts": time.time()}
    except (OSError, ValueError, IndexError):
        pass
    return {"rss_bytes": 0.0, "ts": time.time()}


def rss_doubled_over_chunk(chunk_sec: float = 5.8 * 3600) -> bool:
    """True when RSS more than doubled against the oldest snapshot in a chunk."""
    now = time.time()
    _rss_history.append(rss_snapshot())
    _rss_history[:] = [s for s in _rss_history if now - s["ts"] <= chunk_sec]
    if len(_rss_history) < 2:
        return False
    baseline = _rss_history[0]["rss_bytes"]
    latest = _rss_history[-1]["rss_bytes"]
    return bool(baseline > 0 and latest > baseline * 2)


async def rss_memory_check(bot: Any) -> None:
    snap = rss_snapshot()
    if rss_doubled_over_chunk():
        await alerts.post_admin_alert(
            f"🧠 **Memory doubling detected** — RSS {snap['rss_bytes'] / 1e6:.1f} MB "
            "is >2× the baseline for this chunk. Investigate before the next handoff.",
            debounce_key="rss-double",
        )
        cm.record_event("", "rss_doubled", {"rss_bytes": snap["rss_bytes"]})


# ── Gist queue monitoring (2.2) ──────────────────────────────────────────────


def gist_usage_stats() -> dict[str, Any]:
    """Aggregate Gist API counters (429s, latency) from github_api + gist_backup."""
    return github_api.gist_usage_stats()


async def gist_usage_check(bot: Any) -> None:
    stats = gist_usage_stats()
    per_hour = int(stats.get("requests_last_hour", 0) or 0)
    if per_hour > 4000:
        await alerts.post_admin_alert(
            f"📈 **Gist request rate {per_hour}/hr** — approaching the 4,000/hr "
            "secondary token limit. Reduce CHANNEL_STATE sync frequency or "
            "split Gists before GitHub throttles the control bot.",
            debounce_key="gist-rate",
        )
    if stats.get("429_count", 0):
        await alerts.post_admin_alert(
            f"📈 Gist API returned **{stats['429_count']} rate-limits** (last hour). "
            "Backing off is automatic; investigate if it persists.",
            debounce_key="gist-429",
        )


# ── Nightly token validation sweep (2.4) ────────────────────────────────────


def validate_discord_token(token: str) -> bool:
    """True when the token authenticates against /users/@me."""
    ok, _ = github_api.fetch_discord_user_profile(token)
    return ok


async def nightly_token_sweep(bot: Any) -> dict[str, Any]:
    """Validate each stored alt token; report failures to #admin-alerts."""
    bad: list[dict[str, Any]] = []
    checked = 0
    for customer in cm.list_customers(active_only=False):
        creds = cm.get_alt_credentials(customer["discord_id"])
        for cred in creds:
            if not cred.get("token"):
                continue
            checked += 1
            if not validate_discord_token(cred["token"]):
                bad.append({
                    "discord_id": customer["discord_id"],
                    "username": customer.get("discord_username", ""),
                    "alt": cred["alt_index"],
                })
    if bad:
        lines = "\n".join(
            f"• `{b['username']}` ({b['discord_id']}) alt {b['alt']}" for b in bad[:25]
        )
        await alerts.post_admin_alert(
            f"🌙 **Nightly token sweep**: {len(bad)} invalid token(s) found.\n{lines}\n"
            "Ask these customers to re-run /setup with fresh tokens.",
            debounce_key="nightly-token",
        )
    cm.record_event("", "nightly_token_sweep", {"checked": checked, "invalid": len(bad)})
    return {"checked": checked, "invalid": len(bad)}


# ── Contextual /tune hints after day 3 (2.6) ────────────────────────────────

async def post_tune_hints(bot: Any) -> int:
    """Post one /tune hint per customer after they own the farm for 3 days."""
    posted = 0
    now = time.time()
    for customer in cm.list_customers(active_only=True):
        did = customer["discord_id"]
        start = float(customer.get("start_date") or 0)
        if start <= 0 or (now - start) < 3 * 86400:
            continue
        existing = cm.get_events("tune_hint_sent", since=start, discord_id=did)
        if existing:
            continue
        thread = customer.get("control_thread_id", "")
        if not thread or thread == "0":
            continue
        try:
            ch = bot.get_channel(int(thread)) or await bot.fetch_channel(int(thread))
            await ch.send(
                "💡 **Need to change your price or message?** Use "
                "`/tune alt:1 price:2.50` or `/tune alt:1 message:New text`."
            )
            cm.record_event(did, "tune_hint_sent", {})
            posted += 1
        except Exception:
            continue
    return posted


# ── alert queue flush (gist worker → bot loop) ──────────────────────────────
_alert_queue: asyncio.Queue = None  # type: ignore[assignment]


def enqueue_async_alert(text: str) -> None:
    """Called from sync background threads; bot loop drains via flush_alerts."""
    if _alert_queue is None:
        return
    try:
        _alert_queue.put_nowait(text)
    except Exception:
        pass


def ensure_alert_queue() -> asyncio.Queue:
    global _alert_queue
    if _alert_queue is None:
        _alert_queue = asyncio.Queue()
    return _alert_queue


async def flush_alerts(bot: Any) -> int:
    q = ensure_alert_queue()
    n = 0
    while not q.empty():
        try:
            text = q.get_nowait()
        except asyncio.QueueEmpty:
            break
        await alerts.post_admin_alert(text)
        n += 1
    return n
