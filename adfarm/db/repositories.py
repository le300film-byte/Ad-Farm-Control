"""Repositories: the only code that knows SQL. They return domain models and never raise
``sqlite3`` errors to callers for "not found" cases (they return ``None``)."""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable, Optional

from ..core.models import Alt, AltStatus, Customer, Event, RunMode, RunState, SyncState, Webhooks
from .database import Database


def _j(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def _load(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


# ═════════════════════════════════════════════════════════════════════════════
class CustomerRepo:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _row(row: sqlite3.Row | None) -> Optional[Customer]:
        if row is None:
            return None
        return Customer(
            discord_id=str(row["discord_id"]),
            username=row["username"],
            alt_count=int(row["alt_count"]),
            vip=bool(row["vip"]),
            start_date=float(row["start_date"]),
            expiry_date=float(row["expiry_date"]),
            active=bool(row["active"]),
            github_account=row["github_account"],
            forum_id=row["forum_id"],
            thread_ids=_load(row["thread_ids"], {}),
            autoreply_text=row["autoreply_text"],
            notes=row["notes"],
        )

    def get(self, discord_id: str) -> Optional[Customer]:
        with self.db.read() as conn:
            return self._row(conn.execute("SELECT * FROM customers WHERE discord_id=?", (str(discord_id),)).fetchone())

    def by_forum(self, forum_id: str) -> Optional[Customer]:
        with self.db.read() as conn:
            return self._row(conn.execute("SELECT * FROM customers WHERE forum_id=? AND forum_id<>''", (str(forum_id),)).fetchone())

    def all(self, *, active_only: bool = False) -> list[Customer]:
        sql = "SELECT * FROM customers" + (" WHERE active=1" if active_only else "") + " ORDER BY expiry_date"
        with self.db.read() as conn:
            return [self._row(r) for r in conn.execute(sql).fetchall()]  # type: ignore[misc]

    def save(self, customer: Customer, *, now: float) -> Customer:
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO customers(discord_id, username, alt_count, vip, start_date, expiry_date, active,
                                         github_account, forum_id, thread_ids, autoreply_text, notes, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(discord_id) DO UPDATE SET
                        username=excluded.username, alt_count=excluded.alt_count, vip=excluded.vip,
                        start_date=excluded.start_date, expiry_date=excluded.expiry_date, active=excluded.active,
                        github_account=excluded.github_account, forum_id=excluded.forum_id, thread_ids=excluded.thread_ids,
                        autoreply_text=excluded.autoreply_text, notes=excluded.notes, updated_at=excluded.updated_at""",
                (customer.discord_id, customer.username, int(customer.alt_count), int(customer.vip), customer.start_date,
                 customer.expiry_date, int(customer.active), customer.github_account, customer.forum_id,
                 _j(customer.thread_ids), customer.autoreply_text, customer.notes, now, now),
            )
        return customer

    def delete(self, discord_id: str) -> bool:
        with self.db.transaction() as conn:
            cur = conn.execute("DELETE FROM customers WHERE discord_id=?", (str(discord_id),))
            return cur.rowcount > 0

    def expiring_between(self, start: float, end: float) -> list[Customer]:
        with self.db.read() as conn:
            rows = conn.execute("SELECT * FROM customers WHERE active=1 AND expiry_date>? AND expiry_date<=? ORDER BY expiry_date", (start, end)).fetchall()
            return [self._row(r) for r in rows]  # type: ignore[misc]

    def expired(self, now: float) -> list[Customer]:
        with self.db.read() as conn:
            rows = conn.execute("SELECT * FROM customers WHERE active=1 AND expiry_date<=? ORDER BY expiry_date", (now,)).fetchall()
            return [self._row(r) for r in rows]  # type: ignore[misc]


# ═════════════════════════════════════════════════════════════════════════════
class AltRepo:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _row(row: sqlite3.Row | None) -> Optional[Alt]:
        if row is None:
            return None
        return Alt(
            customer_id=str(row["customer_id"]),
            alt_index=int(row["alt_index"]),
            sender_alt_id=int(row["sender_alt_id"]),
            repo_owner=row["repo_owner"],
            repo_name=row["repo_name"],
            status=AltStatus(row["status"]),
            discord_user_id=row["discord_user_id"],
            username=row["username"],
            display_name=row["display_name"],
            channel_ids=tuple(_load(row["channel_ids"], [])),
            token_ciphertext=row["token_ciphertext"],
            sync_state=SyncState(row["sync_state"]),
            runtime_overrides=_load(row["runtime_overrides"], {}),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def get(self, customer_id: str, alt_index: int) -> Optional[Alt]:
        with self.db.read() as conn:
            return self._row(conn.execute("SELECT * FROM alts WHERE customer_id=? AND alt_index=?", (str(customer_id), int(alt_index))).fetchone())

    def by_sender_id(self, sender_alt_id: int) -> Optional[Alt]:
        with self.db.read() as conn:
            return self._row(conn.execute("SELECT * FROM alts WHERE sender_alt_id=?", (int(sender_alt_id),)).fetchone())

    def by_repo(self, owner: str, name: str) -> Optional[Alt]:
        with self.db.read() as conn:
            return self._row(conn.execute("SELECT * FROM alts WHERE lower(repo_owner)=lower(?) AND lower(repo_name)=lower(?)", (owner, name)).fetchone())

    def for_customer(self, customer_id: str, *, include_removed: bool = False) -> list[Alt]:
        sql = "SELECT * FROM alts WHERE customer_id=?" + ("" if include_removed else " AND status<>'removed'") + " ORDER BY alt_index"
        with self.db.read() as conn:
            return [self._row(r) for r in conn.execute(sql, (str(customer_id),)).fetchall()]  # type: ignore[misc]

    def all(self, *, statuses: Iterable[AltStatus] | None = None) -> list[Alt]:
        with self.db.read() as conn:
            if statuses:
                marks = ",".join("?" for _ in statuses)
                rows = conn.execute(f"SELECT * FROM alts WHERE status IN ({marks}) ORDER BY customer_id, alt_index", [s.value for s in statuses]).fetchall()
            else:
                rows = conn.execute("SELECT * FROM alts WHERE status<>'removed' ORDER BY customer_id, alt_index").fetchall()
            return [self._row(r) for r in rows]  # type: ignore[misc]

    def dirty(self) -> list[Alt]:
        with self.db.read() as conn:
            rows = conn.execute("SELECT * FROM alts WHERE sync_state='dirty' AND status<>'removed'").fetchall()
            return [self._row(r) for r in rows]  # type: ignore[misc]

    def next_sender_alt_id(self) -> int:
        with self.db.read() as conn:
            row = conn.execute("SELECT COALESCE(MAX(sender_alt_id), 0) FROM alts").fetchone()
            return int(row[0]) + 1

    def save(self, alt: Alt, *, now: float) -> Alt:
        created = alt.created_at or now
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO alts(customer_id, alt_index, sender_alt_id, repo_owner, repo_name, status, discord_user_id,
                                    username, display_name, channel_ids, token_ciphertext, sync_state, runtime_overrides,
                                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(customer_id, alt_index) DO UPDATE SET
                        sender_alt_id=excluded.sender_alt_id, repo_owner=excluded.repo_owner, repo_name=excluded.repo_name,
                        status=excluded.status, discord_user_id=excluded.discord_user_id, username=excluded.username,
                        display_name=excluded.display_name, channel_ids=excluded.channel_ids,
                        token_ciphertext=excluded.token_ciphertext, sync_state=excluded.sync_state,
                        runtime_overrides=excluded.runtime_overrides, updated_at=excluded.updated_at""",
                (alt.customer_id, int(alt.alt_index), int(alt.sender_alt_id), alt.repo_owner, alt.repo_name, alt.status.value,
                 alt.discord_user_id, alt.username, alt.display_name, _j(list(alt.channel_ids)), alt.token_ciphertext,
                 alt.sync_state.value, _j(alt.runtime_overrides), created, now),
            )
        return alt.with_(created_at=created, updated_at=now)

    def delete(self, customer_id: str, alt_index: int) -> bool:
        with self.db.transaction() as conn:
            cur = conn.execute("DELETE FROM alts WHERE customer_id=? AND alt_index=?", (str(customer_id), int(alt_index)))
            return cur.rowcount > 0


# ═════════════════════════════════════════════════════════════════════════════
class RunRepo:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _row(row: sqlite3.Row | None) -> Optional[RunState]:
        if row is None:
            return None
        return RunState(
            customer_id=str(row["customer_id"]),
            alt_index=int(row["alt_index"]),
            mode=RunMode(row["mode"]),
            runtime_hours=int(row["runtime_hours"]),
            started_at=float(row["started_at"]),
            last_dispatch_at=float(row["last_dispatch_at"]),
            renewals=int(row["renewals"]),
            payload=_load(row["payload"], {}),
            run_id=int(row["run_id"]) if row["run_id"] is not None else None,
            status=row["status"],
            conclusion=row["conclusion"],
        )

    def get(self, customer_id: str, alt_index: int) -> Optional[RunState]:
        with self.db.read() as conn:
            return self._row(conn.execute("SELECT * FROM runs WHERE customer_id=? AND alt_index=?", (str(customer_id), int(alt_index))).fetchone())

    def for_customer(self, customer_id: str) -> list[RunState]:
        with self.db.read() as conn:
            return [self._row(r) for r in conn.execute("SELECT * FROM runs WHERE customer_id=? ORDER BY alt_index", (str(customer_id),)).fetchall()]  # type: ignore[misc]

    def all(self) -> list[RunState]:
        with self.db.read() as conn:
            return [self._row(r) for r in conn.execute("SELECT * FROM runs ORDER BY customer_id, alt_index").fetchall()]  # type: ignore[misc]

    def active(self) -> list[RunState]:
        with self.db.read() as conn:
            rows = conn.execute("SELECT * FROM runs WHERE status IN ('queued','in_progress') ORDER BY customer_id, alt_index").fetchall()
            return [self._row(r) for r in rows]  # type: ignore[misc]

    def save(self, run: RunState) -> RunState:
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO runs(customer_id, alt_index, mode, runtime_hours, started_at, last_dispatch_at, renewals, payload, run_id, status, conclusion)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(customer_id, alt_index) DO UPDATE SET
                        mode=excluded.mode, runtime_hours=excluded.runtime_hours, started_at=excluded.started_at,
                        last_dispatch_at=excluded.last_dispatch_at, renewals=excluded.renewals, payload=excluded.payload,
                        run_id=excluded.run_id, status=excluded.status, conclusion=excluded.conclusion""",
                (run.customer_id, int(run.alt_index), run.mode.value, int(run.runtime_hours), run.started_at, run.last_dispatch_at,
                 int(run.renewals), _j(run.payload), run.run_id, run.status, run.conclusion),
            )
        return run

    def delete(self, customer_id: str, alt_index: int) -> bool:
        with self.db.transaction() as conn:
            return conn.execute("DELETE FROM runs WHERE customer_id=? AND alt_index=?", (str(customer_id), int(alt_index))).rowcount > 0


# ═════════════════════════════════════════════════════════════════════════════
class WebhookRepo:
    """Stores per-customer webhook URLs. Values are passed through the vault by the service."""

    def __init__(self, db: Database):
        self.db = db

    def get(self, customer_id: str) -> Optional[Webhooks]:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM customer_webhooks WHERE customer_id=?", (str(customer_id),)).fetchone()
        if row is None:
            return None
        return Webhooks(customer_id=str(row["customer_id"]), dashboard=row["dashboard"], logs=row["logs"], deals=row["deals"], dm=row["dm"])

    def save(self, hooks: Webhooks, *, now: float) -> Webhooks:
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO customer_webhooks(customer_id, dashboard, logs, deals, dm, updated_at) VALUES (?,?,?,?,?,?)
                   ON CONFLICT(customer_id) DO UPDATE SET dashboard=excluded.dashboard, logs=excluded.logs,
                        deals=excluded.deals, dm=excluded.dm, updated_at=excluded.updated_at""",
                (hooks.customer_id, hooks.dashboard, hooks.logs, hooks.deals, hooks.dm, now),
            )
        return hooks

    def delete(self, customer_id: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM customer_webhooks WHERE customer_id=?", (str(customer_id),))


# ═════════════════════════════════════════════════════════════════════════════
class ReminderRepo:
    def __init__(self, db: Database):
        self.db = db

    def was_sent(self, customer_id: str, threshold_days: int, expiry_date: float) -> bool:
        with self.db.read() as conn:
            row = conn.execute("SELECT 1 FROM reminders_sent WHERE customer_id=? AND threshold_days=? AND expiry_date=?",
                               (str(customer_id), int(threshold_days), float(expiry_date))).fetchone()
            return row is not None

    def mark(self, customer_id: str, threshold_days: int, expiry_date: float, *, now: float) -> None:
        with self.db.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO reminders_sent(customer_id, threshold_days, expiry_date, sent_at) VALUES (?,?,?,?)",
                         (str(customer_id), int(threshold_days), float(expiry_date), now))

    def clear(self, customer_id: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM reminders_sent WHERE customer_id=?", (str(customer_id),))


# ═════════════════════════════════════════════════════════════════════════════
class PolicyAckRepo:
    def __init__(self, db: Database):
        self.db = db

    def has_acked(self, discord_id: str, version: str) -> bool:
        with self.db.read() as conn:
            return conn.execute("SELECT 1 FROM policy_acks WHERE discord_id=? AND policy_version=?", (str(discord_id), version)).fetchone() is not None

    def ack(self, discord_id: str, version: str, *, now: float) -> None:
        with self.db.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO policy_acks(discord_id, policy_version, acked_at) VALUES (?,?,?)", (str(discord_id), version, now))


# ═════════════════════════════════════════════════════════════════════════════
class EventRepo:
    def __init__(self, db: Database):
        self.db = db

    def log(self, discord_id: str, event: str, *, now: float, **payload: Any) -> int:
        with self.db.transaction() as conn:
            cur = conn.execute("INSERT INTO events(discord_id, event, ts, payload) VALUES (?,?,?,?)", (str(discord_id or ""), event, now, _j(payload)))
            return int(cur.lastrowid)

    def recent(self, *, limit: int = 50, discord_id: str | None = None, event: str | None = None) -> list[Event]:
        sql, args = "SELECT * FROM events", []
        clauses = []
        if discord_id:
            clauses.append("discord_id=?"); args.append(str(discord_id))
        if event:
            clauses.append("event=?"); args.append(event)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        with self.db.read() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [Event(id=int(r["id"]), discord_id=r["discord_id"], event=r["event"], ts=float(r["ts"]), payload=Event.decode_payload(r["payload"])) for r in rows]

    def last(self, discord_id: str, event: str) -> Optional[Event]:
        rows = self.recent(limit=1, discord_id=discord_id, event=event)
        return rows[0] if rows else None


# ═════════════════════════════════════════════════════════════════════════════
class TicketRepo:
    def __init__(self, db: Database):
        self.db = db

    def open(self, customer_id: str, kind: str, *, now: float, **payload: Any) -> int:
        with self.db.transaction() as conn:
            cur = conn.execute("INSERT INTO tickets(customer_id, kind, status, payload, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                               (str(customer_id), kind, "open", _j(payload), now, now))
            return int(cur.lastrowid)

    def find_open(self, customer_id: str, kind: str) -> Optional[dict[str, Any]]:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM tickets WHERE customer_id=? AND kind=? AND status='open' ORDER BY id DESC LIMIT 1", (str(customer_id), kind)).fetchone()
        return dict(row) | {"payload": _load(row["payload"], {})} if row else None

    def close(self, ticket_id: int, *, now: float, status: str = "closed", **payload: Any) -> bool:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT payload FROM tickets WHERE id=?", (int(ticket_id),)).fetchone()
            if row is None:
                return False
            merged = _load(row["payload"], {}) | payload
            conn.execute("UPDATE tickets SET status=?, payload=?, updated_at=? WHERE id=?", (status, _j(merged), now, int(ticket_id)))
            return True

    def attach_proof(self, ticket_id: int, *, now: float, tx_hash: str, proof_url: str = "", proof_note: str = "") -> bool:
        """Move a ticket to ``awaiting_admin`` with the payment evidence attached (admin closes it after verification)."""
        return self.close(ticket_id, now=now, status="awaiting_admin", tx_hash=tx_hash, proof_url=proof_url, proof_note=proof_note)

    def list_open(self) -> list[dict[str, Any]]:
        with self.db.read() as conn:
            rows = conn.execute("SELECT * FROM tickets WHERE status IN ('open', 'awaiting_admin') ORDER BY id").fetchall()
        return [dict(r) | {"payload": _load(r["payload"], {})} for r in rows]


# ═════════════════════════════════════════════════════════════════════════════
class MetaRepo:
    def __init__(self, db: Database):
        self.db = db

    def get(self, key: str, default: str = "") -> str:
        with self.db.read() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return str(row["value"]) if row else default

    def set(self, key: str, value: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO meta(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def delete(self, key: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM meta WHERE key=?", (key,))

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.get(key, str(default)))
        except ValueError:
            return default

    def all(self) -> dict[str, str]:
        with self.db.read() as conn:
            return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta").fetchall()}
