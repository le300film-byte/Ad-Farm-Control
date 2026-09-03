"""customer_manager.py – V8 Customer Data & SQLite Operations.

Manages all customer records including activation, expiry tracking,
VIP status, GitHub repo mapping, and Discord forum thread IDs.

Phase 0.1 additions:
  * Write-through persistence — every mutating call enqueues a WAL-checkpointed
    upload of customers.db to the private backup Gist (``gist_backup``).
  * ``reminder_sent`` — persistent reminder dedupe state (R-08).
  * ``policy_acks`` — click-through ToS acknowledgement ledger (0.4).
  * ``events`` — append-only metric/audit events for TTFTV, bans, restores,
    churn and the nine watch metrics (Phase 1/2).
  * ``alt_credentials`` — per-alt token/channel snapshot used by the nightly
    token validation sweep (Phase 2.4). Tokens are stored obfuscated; the
    table is never logged or included in human-readable output.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator, Optional

DB_PATH = os.environ.get("CUSTOMERS_DB", "customers.db")
BACKUP_SUFFIX = ".backup"

# If set, alt tokens are obfuscated with this key before storage (XOR+base64).
TOKEN_VAULT_KEY = os.environ.get("TOKEN_VAULT_KEY", "").strip()
STORE_ALT_TOKENS = os.environ.get("STORE_ALT_TOKENS_IN_DB", "1").strip().lower() in {"1", "true", "yes", "on"}

# Schema version — bump when tables change so the runbook can detect staleness.
SCHEMA_VERSION = 2


def _db_path() -> str:
    return DB_PATH


@contextmanager
def _conn() -> Generator[sqlite3.Connection, None, None]:
    con = sqlite3.connect(_db_path())
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ─── write-through hook ─────────────────────────────────────────────────────

def _after_write(reason: str) -> None:
    """Enqueue a Gist write-through backup after every DB mutation.

    No-op when the backup Gist is not configured (local/dev/test mode), so
    existing tests and offline installs work exactly as before. The upload
    happens on a single background worker → single-writer discipline.
    """
    try:
        from gist_backup import enqueue_backup, gist_configured
        if gist_configured():
            enqueue_backup(reason)
    except Exception:
        pass


def init_db() -> None:
    """Create all tables if they do not exist."""
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS customers (
                discord_id          TEXT PRIMARY KEY,
                discord_username    TEXT,
                alt_count           INTEGER DEFAULT 1,
                vip                 BOOLEAN DEFAULT 0,
                start_date          INTEGER,
                expiry_date         INTEGER,
                active              BOOLEAN DEFAULT 1,
                github_account      TEXT,
                repos               TEXT,
                forum_id            TEXT,
                control_thread_id   TEXT,
                dashboard_thread_id TEXT,
                logs_thread_id      TEXT,
                dm_thread_id        TEXT,
                deals_thread_id     TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS reminder_sent (
                discord_id TEXT NOT NULL,
                threshold  INTEGER NOT NULL,
                sent_at    INTEGER NOT NULL,
                PRIMARY KEY (discord_id, threshold)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS policy_acks (
                discord_id TEXT PRIMARY KEY,
                acked_at   INTEGER NOT NULL,
                version    TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id TEXT,
                event      TEXT NOT NULL,
                ts         REAL NOT NULL,
                payload    TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS alt_credentials (
                discord_id  TEXT NOT NULL,
                alt_index   INTEGER NOT NULL,
                token       TEXT,
                channel_ids TEXT,
                username    TEXT,
                updated_at  INTEGER NOT NULL,
                PRIMARY KEY (discord_id, alt_index)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS run_state (
                discord_id       TEXT NOT NULL,
                alt_index        INTEGER NOT NULL,
                mode             TEXT,
                runtime_hours    INTEGER DEFAULT 0,
                started_at       REAL DEFAULT 0,
                last_dispatch_at REAL DEFAULT 0,
                renewals         INTEGER DEFAULT 0,
                payload          TEXT,
                PRIMARY KEY (discord_id, alt_index)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        con.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
    print(f"[DB] customers.db initialized at {_db_path()} (schema v{SCHEMA_VERSION})")


def backup_db() -> str:
    """Create a timestamped backup of customers.db before destructive ops."""
    src = Path(_db_path())
    if not src.exists():
        return ""
    ts = int(time.time())
    dest = src.with_name(f"customers_{ts}{BACKUP_SUFFIX}")
    shutil.copy2(src, dest)
    print(f"[DB] Backup created: {dest}")
    return str(dest)


# ──────────────────────────────────────────────────────────────────────────────
# CRUD helpers
# ──────────────────────────────────────────────────────────────────────────────

def add_customer(
    discord_id: str,
    discord_username: str,
    alt_count: int,
    vip: bool,
    days: int,
    github_account: str = "",
    repos: Optional[list[str]] = None,
    forum_id: str = "",
    control_thread_id: str = "",
    dashboard_thread_id: str = "",
    logs_thread_id: str = "",
    dm_thread_id: str = "",
    deals_thread_id: str = "",
) -> None:
    """Insert or replace a customer record."""
    now = int(time.time())
    expiry = now + days * 86400
    with _conn() as con:
        con.execute(
            """
            INSERT INTO customers
                (discord_id, discord_username, alt_count, vip,
                 start_date, expiry_date, active, github_account, repos,
                 forum_id, control_thread_id, dashboard_thread_id,
                 logs_thread_id, dm_thread_id, deals_thread_id)
            VALUES (?,?,?,?,?,?,1,?,?,?,?,?,?,?,?)
            ON CONFLICT(discord_id) DO UPDATE SET
                discord_username    = excluded.discord_username,
                alt_count           = excluded.alt_count,
                vip                 = excluded.vip,
                start_date          = excluded.start_date,
                expiry_date         = excluded.expiry_date,
                active              = 1,
                github_account      = excluded.github_account,
                repos               = excluded.repos,
                forum_id            = excluded.forum_id,
                control_thread_id   = excluded.control_thread_id,
                dashboard_thread_id = excluded.dashboard_thread_id,
                logs_thread_id      = excluded.logs_thread_id,
                dm_thread_id        = excluded.dm_thread_id,
                deals_thread_id     = excluded.deals_thread_id
            """,
            (
                discord_id, discord_username, alt_count, int(vip),
                now, expiry, github_account,
                json.dumps(repos or []),
                forum_id, control_thread_id, dashboard_thread_id,
                logs_thread_id, dm_thread_id, deals_thread_id,
            ),
        )
    record_event(discord_id, "customer_activated", {"days": days, "alts": alt_count, "vip": bool(vip)})
    _after_write("add_customer")


def get_customer(discord_id: str) -> Optional[dict[str, Any]]:
    """Return a customer record as a dict, or None if not found."""
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM customers WHERE discord_id = ?", (discord_id,)
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    try:
        d["repos"] = json.loads(d.get("repos") or "[]")
    except (json.JSONDecodeError, TypeError):
        d["repos"] = []
    return d


def list_customers(active_only: bool = True) -> list[dict[str, Any]]:
    """Return all customers, optionally filtered to active ones."""
    with _conn() as con:
        if active_only:
            rows = con.execute(
                "SELECT * FROM customers WHERE active = 1"
            ).fetchall()
        else:
            rows = con.execute("SELECT * FROM customers").fetchall()
    result = []
    for row in rows:
        d = dict(row)
        try:
            d["repos"] = json.loads(d.get("repos") or "[]")
        except (json.JSONDecodeError, TypeError):
            d["repos"] = []
        result.append(d)
    return result


def extend_customer(discord_id: str, days: int) -> bool:
    """Add *days* to an existing customer's expiry_date."""
    with _conn() as con:
        rows = con.execute(
            "UPDATE customers SET expiry_date = expiry_date + ?, active = 1 "
            "WHERE discord_id = ?",
            (days * 86400, discord_id),
        ).rowcount
    if rows > 0:
        record_event(discord_id, "customer_extended", {"days": days})
        _after_write("extend_customer")
    return rows > 0


def deactivate_customer(discord_id: str) -> bool:
    """Set active = 0 for a customer (subscription expired or manual)."""
    backup_db()
    with _conn() as con:
        rows = con.execute(
            "UPDATE customers SET active = 0 WHERE discord_id = ?",
            (discord_id,),
        ).rowcount
    if rows > 0:
        record_event(discord_id, "customer_deactivated", {})
        _after_write("deactivate_customer")
    return rows > 0


def update_forum_ids(
    discord_id: str,
    forum_id: str = "",
    control_thread_id: str = "",
    dashboard_thread_id: str = "",
    logs_thread_id: str = "",
    dm_thread_id: str = "",
    deals_thread_id: str = "",
) -> None:
    with _conn() as con:
        con.execute(
            """
            UPDATE customers SET
                forum_id            = COALESCE(NULLIF(?, ''), forum_id),
                control_thread_id   = COALESCE(NULLIF(?, ''), control_thread_id),
                dashboard_thread_id = COALESCE(NULLIF(?, ''), dashboard_thread_id),
                logs_thread_id      = COALESCE(NULLIF(?, ''), logs_thread_id),
                dm_thread_id        = COALESCE(NULLIF(?, ''), dm_thread_id),
                deals_thread_id     = COALESCE(NULLIF(?, ''), deals_thread_id)
            WHERE discord_id = ?
            """,
            (
                forum_id, control_thread_id, dashboard_thread_id,
                logs_thread_id, dm_thread_id, deals_thread_id,
                discord_id,
            ),
        )
    _after_write("update_forum_ids")


def update_repos(discord_id: str, repos: list[str]) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE customers SET repos = ? WHERE discord_id = ?",
            (json.dumps(repos), discord_id),
        )
    _after_write("update_repos")


def days_remaining(discord_id: str) -> Optional[float]:
    """Return days until expiry (negative if already expired), or None."""
    c = get_customer(discord_id)
    if c is None:
        return None
    return (c["expiry_date"] - time.time()) / 86400


def is_active(discord_id: str) -> bool:
    c = get_customer(discord_id)
    if c is None:
        return False
    return bool(c["active"]) and c["expiry_date"] > time.time()


def is_vip(discord_id: str) -> bool:
    c = get_customer(discord_id)
    return bool(c and c.get("vip"))


def set_vip(discord_id: str, vip: bool) -> bool:
    with _conn() as con:
        rows = con.execute(
            "UPDATE customers SET vip = ? WHERE discord_id = ?",
            (int(vip), discord_id),
        ).rowcount
    if rows > 0:
        record_event(discord_id, "vip_changed", {"vip": bool(vip)})
        _after_write("set_vip")
    return rows > 0


# ──────────────────────────────────────────────────────────────────────────────
# Expiry scan (called by timer_engine)
# ──────────────────────────────────────────────────────────────────────────────

def get_expiring_customers(within_days: float) -> list[dict[str, Any]]:
    """Return active customers whose subscription expires within *within_days*."""
    cutoff = time.time() + within_days * 86400
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM customers WHERE active = 1 AND expiry_date <= ?",
            (cutoff,),
        ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        try:
            d["repos"] = json.loads(d.get("repos") or "[]")
        except (json.JSONDecodeError, TypeError):
            d["repos"] = []
        result.append(d)
    return result


def get_expired_customers() -> list[dict[str, Any]]:
    """Return active customers whose expiry_date has already passed or is now."""
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM customers WHERE active = 1 AND expiry_date <= ?",
            (int(time.time()),),
        ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        try:
            d["repos"] = json.loads(d.get("repos") or "[]")
        except (json.JSONDecodeError, TypeError):
            d["repos"] = []
        result.append(d)
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Reminder dedupe (R-08) — persisted across restarts / chunk handoffs
# ──────────────────────────────────────────────────────────────────────────────

def mark_reminder_sent(discord_id: str, threshold: int) -> None:
    with _conn() as con:
        con.execute(
            "INSERT OR REPLACE INTO reminder_sent (discord_id, threshold, sent_at) VALUES (?,?,?)",
            (discord_id, int(threshold), int(time.time())),
        )
    _after_write("reminder_sent")


def clear_reminder_sent(discord_id: Optional[str] = None) -> None:
    with _conn() as con:
        if discord_id:
            con.execute("DELETE FROM reminder_sent WHERE discord_id = ?", (discord_id,))
        else:
            con.execute("DELETE FROM reminder_sent")
    _after_write("reminder_cleared")


def get_sent_reminders() -> set[tuple[str, int]]:
    """Return the persisted {(discord_id, threshold)} sent-state."""
    with _conn() as con:
        rows = con.execute("SELECT discord_id, threshold FROM reminder_sent").fetchall()
    return {(str(r["discord_id"]), int(r["threshold"])) for r in rows}


# ──────────────────────────────────────────────────────────────────────────────
# ToS / policy acknowledgement (0.4)
# ──────────────────────────────────────────────────────────────────────────────

def ack_policy(discord_id: str, version: str) -> None:
    with _conn() as con:
        con.execute(
            "INSERT OR REPLACE INTO policy_acks (discord_id, acked_at, version) VALUES (?,?,?)",
            (discord_id, int(time.time()), version),
        )
    record_event(discord_id, "policy_acked", {"version": version})
    _after_write("policy_ack")


def has_policy_ack(discord_id: str, version: str = "") -> bool:
    with _conn() as con:
        row = con.execute(
            "SELECT version, acked_at FROM policy_acks WHERE discord_id = ?", (discord_id,)
        ).fetchone()
    if row is None:
        return False
    if version and row["version"] != version:
        return False
    return True


# ──────────────────────────────────────────────────────────────────────────────
# Events / metrics (Phase 1.5 + Phase 2 watch metrics)
# ──────────────────────────────────────────────────────────────────────────────

def record_event(discord_id: str, event: str, payload: Optional[dict[str, Any]] = None) -> None:
    with _conn() as con:
        con.execute(
            "INSERT INTO events (discord_id, event, ts, payload) VALUES (?,?,?,?)",
            (discord_id or "", event, time.time(), json.dumps(payload or {})),
        )


def get_events(
    event: Optional[str] = None,
    since: Optional[float] = None,
    until: Optional[float] = None,
    limit: int = 500,
    discord_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if event:
        clauses.append("event = ?")
        params.append(event)
    if since is not None:
        clauses.append("ts >= ?")
        params.append(float(since))
    if until is not None:
        clauses.append("ts <= ?")
        params.append(float(until))
    if discord_id:
        clauses.append("discord_id = ?")
        params.append(discord_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _conn() as con:
        rows = con.execute(
            f"SELECT * FROM events {where} ORDER BY ts DESC LIMIT ?",
            (*params, int(limit)),
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        try:
            d["payload"] = json.loads(d.get("payload") or "{}")
        except (json.JSONDecodeError, TypeError):
            d["payload"] = {}
        out.append(d)
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Alt credentials (Phase 2.4 nightly validation; tokens never logged)
# ──────────────────────────────────────────────────────────────────────────────

def _obfuscate(value: str) -> str:
    if not STORE_ALT_TOKENS:
        return ""
    if not TOKEN_VAULT_KEY:
        # base64 only — obfuscation, not encryption; Gist/DB remain private.
        return base64.b64encode(value.encode("utf-8")).decode("ascii")
    key = (TOKEN_VAULT_KEY.encode("utf-8") * (len(value) // len(TOKEN_VAULT_KEY) + 1))[: len(value)]
    mixed = bytes(a ^ b for a, b in zip(value.encode("utf-8"), key))
    return base64.b64encode(mixed).decode("ascii")


def _deobfuscate(value: str) -> str:
    if not value:
        return ""
    raw = base64.b64decode(value)
    if not TOKEN_VAULT_KEY:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return ""
    key = (TOKEN_VAULT_KEY.encode("utf-8") * (len(raw) // len(TOKEN_VAULT_KEY) + 1))[: len(raw)]
    return bytes(a ^ b for a, b in zip(raw, key)).decode("utf-8", errors="replace")


def store_alt_credential(
    discord_id: str, alt_index: int, token: str, channel_ids: list[str], username: str = ""
) -> None:
    with _conn() as con:
        con.execute(
            "INSERT OR REPLACE INTO alt_credentials "
            "(discord_id, alt_index, token, channel_ids, username, updated_at) VALUES (?,?,?,?,?,?)",
            (
                discord_id, int(alt_index), _obfuscate(token),
                json.dumps(channel_ids or []), (username or "")[:100], int(time.time()),
            ),
        )
    _after_write("alt_credential")


def get_alt_credentials(discord_id: str) -> list[dict[str, Any]]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM alt_credentials WHERE discord_id = ? ORDER BY alt_index", (discord_id,)
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["token"] = _deobfuscate(d.get("token") or "")
        try:
            d["channel_ids"] = json.loads(d.get("channel_ids") or "[]")
        except (json.JSONDecodeError, TypeError):
            d["channel_ids"] = []
        out.append(d)
    return out


def clear_alt_credentials(discord_id: str) -> None:
    with _conn() as con:
        con.execute("DELETE FROM alt_credentials WHERE discord_id = ?", (discord_id,))
    _after_write("alt_credential_cleared")


# ──────────────────────────────────────────────────────────────────────────────
# Run state (Phase 1.3 auto-renew; "∞ = 48h auto-renew" contract)
# ──────────────────────────────────────────────────────────────────────────────

def record_run_state(
    discord_id: str,
    alt_index: int,
    mode: str,
    runtime_hours: int,
    payload: Optional[dict[str, Any]] = None,
) -> None:
    """Record a /run dispatch so the timer engine can auto-renew it."""
    now = time.time()
    with _conn() as con:
        con.execute(
            "INSERT OR REPLACE INTO run_state "
            "(discord_id, alt_index, mode, runtime_hours, started_at, "
            " last_dispatch_at, renewals, payload) VALUES (?,?,?,?,?,?,0,?)",
            (
                discord_id, int(alt_index), (mode or "manual").lower(),
                int(runtime_hours or 0), now, now,
                json.dumps(payload or {}),
            ),
        )
    record_event(discord_id, "run_dispatched", {"alt": alt_index, "mode": mode, "hours": runtime_hours})
    _after_write("run_state")


def get_run_states(discord_id: Optional[str] = None) -> list[dict[str, Any]]:
    with _conn() as con:
        if discord_id:
            rows = con.execute(
                "SELECT * FROM run_state WHERE discord_id = ? ORDER BY alt_index", (discord_id,)
            ).fetchall()
        else:
            rows = con.execute("SELECT * FROM run_state ORDER BY discord_id, alt_index").fetchall()
    out = []
    for row in rows:
        d = dict(row)
        try:
            d["payload"] = json.loads(d.get("payload") or "{}")
        except (json.JSONDecodeError, TypeError):
            d["payload"] = {}
        out.append(d)
    return out


def bump_run_renewal(discord_id: str, alt_index: int) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE run_state SET renewals = renewals + 1, last_dispatch_at = ? "
            "WHERE discord_id = ? AND alt_index = ?",
            (time.time(), discord_id, int(alt_index)),
        )
    record_event(discord_id, "run_renewed", {"alt": alt_index})
    _after_write("run_renewed")


# ──────────────────────────────────────────────────────────────────────────────
# Meta helpers
# ──────────────────────────────────────────────────────────────────────────────

def get_meta(key: str, default: str = "") -> str:
    with _conn() as con:
        row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(key: str, value: str) -> None:
    with _conn() as con:
        con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, str(value)))
    _after_write("meta")


if __name__ == "__main__":
    init_db()
    print("[DB] Schema ready.")
