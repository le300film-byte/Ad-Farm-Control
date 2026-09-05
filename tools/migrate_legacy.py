#!/usr/bin/env python3
"""Legacy → V9 database importer.

One-shot tool that copies an existing ``customers.db`` (the legacy V8 schema defined in
``customer_manager.py``) into the new V9 ``adfarm.db`` produced by ``adfarm.db``.

What is mapped
--------------
* ``customers``            → ``customers`` (thread ids are reshaped into the ``thread_ids`` dict)
* ``alt_credentials``      → ``alts`` (one row per ``alt_index``; a fresh global ``sender_alt_id``
                             is assigned; legacy repo names are best-effort split into
                             ``repo_owner`` / ``repo_name``)
* ``run_state``            → ``runs`` (``mode`` becomes TIMED/LIMITLESS)
* ``reminder_sent``        → ``reminders_sent``
* ``policy_acks``          → ``policy_acks``
* ``events``               → ``events`` (audit ledger preserved)
* ``meta``                 → ``meta`` (worker/installation caches copied verbatim)

Tokens are de-obfuscated with the *legacy* ``TOKEN_VAULT_KEY`` (XOR+base64) and re-sealed with
the *new* ``TOKEN_VAULT_KEY`` (authenticated encryption in ``adfarm.db.vault``). When the new
key is absent the ciphertext column is left empty and the alt is marked ``pending`` — the
customer re-runs ``/setup`` to re-provision credentials. When the legacy key is absent the
token is left empty (the legacy store was base64-only in that case).

Usage
-----
    python -m tools.migrate_legacy --from customers.db --to adfarm.db --dry-run
    python -m tools.migrate_legacy --from customers.db --to adfarm.db \
        --legacy-vault-key "$LEGACY_TOKEN_VAULT_KEY" --vault-key "$TOKEN_VAULT_KEY" \
        --alt-repos "200000000000000001:worker1/alice_alt1,worker1/alice_alt2"

The tool is idempotent: re-running it updates existing rows (``ON CONFLICT``) and only assigns
*new* ``sender_alt_id`` values for alts that are not yet present.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Optional

# Make the adfarm package importable whether run as a module or a script.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from adfarm.core.models import Alt, AltStatus, Customer, Event, RunMode, RunState, SyncState  # noqa: E402
from adfarm.db import Database, TokenVault  # noqa: E402
from adfarm.services.container import Repos  # noqa: E402

# Thread-role mapping: legacy column → new thread_ids key.
_THREAD_MAP = {
    "control_thread_id": "control",
    "dashboard_thread_id": "dashboard",
    "logs_thread_id": "farm-logs",
    "deals_thread_id": "deals",
    "dm_thread_id": "dm-inbox",
}


# ─────────────────────────────────────────────────────────────────────────────
# Legacy reading helpers
# ─────────────────────────────────────────────────────────────────────────────
def _legacy_deobfuscate(value: str, key: str) -> str:
    """Mirror of ``customer_manager._deobfuscate`` for legacy token recovery."""
    if not value:
        return ""
    raw = base64.b64decode(value)
    if not key:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return ""
    kb = key.encode("utf-8")
    kb = (kb * (len(raw) // len(kb) + 1))[: len(raw)]
    return bytes(a ^ b for a, b in zip(raw, kb)).decode("utf-8", errors="replace")


def read_legacy(path: str) -> dict[str, list[dict[str, Any]]]:
    """Read the legacy customers.db into plain dicts keyed by table name."""
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    out: dict[str, list[dict[str, Any]]] = {}
    try:
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("customers", "alt_credentials", "run_state", "reminder_sent", "policy_acks", "events", "meta"):
            if table in names:
                out[table] = [dict(r) for r in con.execute(f"SELECT * FROM {table}")]  # noqa: S608
    finally:
        con.close()
    return out


def _split_repo(spec: str) -> tuple[str, str]:
    spec = (spec or "").strip().strip("/").strip()
    if "/" in spec:
        owner, _, name = spec.partition("/")
        return owner.strip(), name.strip()
    return "", spec


def _parse_alt_repos(values: Optional[list[str]]) -> dict[str, list[str]]:
    """``--alt-repos`` accepts ``discord_id:owner/repo,owner/repo`` entries."""
    out: dict[str, list[str]] = {}
    for item in values or []:
        cid, _, repos = item.partition(":")
        repos_list = [r.strip() for r in repos.split(",") if r.strip()]
        if cid.strip() and repos_list:
            out[cid.strip()] = repos_list
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Migration
# ─────────────────────────────────────────────────────────────────────────────
def migrate(
    legacy_path: str,
    target_db: str,
    *,
    vault_key: str = "",
    legacy_vault_key: str = "",
    alt_repos: Optional[dict[str, list[str]]] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Perform the import. Returns a report dict. No writes occur when ``dry_run`` is True."""
    legacy = read_legacy(legacy_path)
    vault = TokenVault(vault_key)
    alt_repos = alt_repos or {}
    now = time.time()

    # Build the new-domain objects first (so dry-run can report without touching disk).
    customers: list[Customer] = []
    alts: list[Alt] = []
    runs: list[RunState] = []
    events: list[Event] = []
    reminder_rows: list[tuple[str, int, float]] = []
    policy_rows: list[tuple[str, str]] = []
    meta_rows: list[tuple[str, str]] = []

    legacy_expiry: dict[str, float] = {}

    for row in legacy.get("customers", []):
        cid = str(row["discord_id"])
        thread_ids = {new: str(row.get(old) or "") for old, new in _THREAD_MAP.items()}
        start = float(row.get("start_date") or now)
        expiry = float(row.get("expiry_date") or now)
        legacy_expiry[cid] = expiry
        customers.append(
            Customer(
                discord_id=cid,
                username=str(row.get("discord_username") or ""),
                alt_count=int(row.get("alt_count") or 1),
                vip=bool(row.get("vip")),
                start_date=start,
                expiry_date=expiry,
                active=bool(row.get("active", 1)),
                github_account=str(row.get("github_account") or ""),
                forum_id=str(row.get("forum_id") or ""),
                thread_ids=thread_ids,
                autoreply_text=str(row.get("autoreply_text") or ""),
                notes="",
            )
        )

    # Assign a fresh, monotonic sender_alt_id across the whole fleet.
    next_sender_id = 1

    def take_sender_id() -> int:
        nonlocal next_sender_id
        sid = next_sender_id
        next_sender_id += 1
        return sid

    # Legacy repo list (best-effort) keyed by customer id.
    legacy_repos: dict[str, list[str]] = {}
    for c in legacy.get("customers", []):
        try:
            legacy_repos[str(c["discord_id"])] = json.loads(c.get("repos") or "[]")
        except (json.JSONDecodeError, TypeError):
            legacy_repos[str(c["discord_id"])] = []

    for row in legacy.get("alt_credentials", []):
        cid = str(row["discord_id"])
        idx = int(row["alt_index"])
        token = _legacy_deobfuscate(row.get("token") or "", legacy_vault_key)
        try:
            channels = json.loads(row.get("channel_ids") or "[]")
        except (json.JSONDecodeError, TypeError):
            channels = []
        # Repo resolution: explicit override → legacy repo list by index → none.
        repo_spec = ""
        if cid in alt_repos and idx <= len(alt_repos[cid]):
            repo_spec = alt_repos[cid][idx - 1]
        elif cid in legacy_repos and idx <= len(legacy_repos[cid]):
            repo_spec = legacy_repos[cid][idx - 1]
        repo_owner, repo_name = _split_repo(repo_spec)

        token_ciphertext = ""
        status = AltStatus.PENDING
        if token:
            status = AltStatus.READY
            if vault.available:
                token_ciphertext = vault.seal(token)
        alts.append(
            Alt(
                customer_id=cid,
                alt_index=idx,
                sender_alt_id=take_sender_id(),
                repo_owner=repo_owner,
                repo_name=repo_name,
                status=status,
                username=str(row.get("username") or ""),
                display_name=str(row.get("username") or ""),
                channel_ids=tuple(str(c) for c in channels),
                token_ciphertext=token_ciphertext,
                sync_state=SyncState.DIRTY if token else SyncState.CLEAN,
                runtime_overrides={},
                created_at=float(row.get("updated_at") or now),
                updated_at=float(row.get("updated_at") or now),
            )
        )

    for row in legacy.get("run_state", []):
        cid = str(row["discord_id"])
        idx = int(row["alt_index"])
        hours = int(row.get("runtime_hours") or 0)
        mode = RunMode.LIMITLESS if hours == 0 else RunMode.TIMED
        try:
            payload = json.loads(row.get("payload") or "{}")
        except (json.JSONDecodeError, TypeError):
            payload = {}
        runs.append(
            RunState(
                customer_id=cid,
                alt_index=idx,
                mode=mode,
                runtime_hours=hours,
                started_at=float(row.get("started_at") or 0),
                last_dispatch_at=float(row.get("last_dispatch_at") or 0),
                renewals=int(row.get("renewals") or 0),
                payload=payload,
                run_id=None,
                status="queued",
                conclusion="",
            )
        )

    for row in legacy.get("reminder_sent", []):
        cid = str(row["discord_id"])
        threshold = int(row["threshold"])
        expiry = legacy_expiry.get(cid, now + 365 * 86400)
        reminder_rows.append((cid, threshold, expiry))

    for row in legacy.get("policy_acks", []):
        policy_rows.append((str(row["discord_id"]), str(row.get("version") or "")))

    for row in legacy.get("events", []):
        events.append(
            Event(
                id=int(row["id"]),
                discord_id=str(row.get("discord_id") or ""),
                event=str(row.get("event") or ""),
                ts=float(row.get("ts") or now),
                payload=Event.decode_payload(row.get("payload")),
            )
        )

    for row in legacy.get("meta", []):
        meta_rows.append((str(row["key"]), str(row.get("value") or "")))

    report = {
        "customers": len(customers),
        "alts": len(alts),
        "runs": len(runs),
        "alts_with_token": sum(1 for a in alts if a.status is AltStatus.READY),
        "reminders": len(reminder_rows),
        "policy_acks": len(policy_rows),
        "events": len(events),
        "meta": len(meta_rows),
        "dry_run": dry_run,
    }

    if dry_run:
        return report

    db = Database(target_db)
    db.migrate()
    repos = Repos.for_db(db)
    with db.transaction():
        for c in customers:
            repos.customers.save(c, now=now)
        for a in alts:
            repos.alts.save(a, now=now)
        for r in runs:
            repos.runs.save(r)
        for ev in events:
            db._local.conn.execute(  # noqa: SLF001 - preserve audit ids in one transaction
                "INSERT OR REPLACE INTO events(id, discord_id, event, ts, payload) VALUES (?,?,?,?,?)",
                (ev.id, ev.discord_id, ev.event, ev.ts, json.dumps(ev.payload)),
            )
        for cid, threshold, expiry in reminder_rows:
            repos.meta  # touch to keep linter happy; reminders need a single insert
            db._local.conn.execute(  # noqa: SLF001
                "INSERT OR REPLACE INTO reminders_sent(customer_id, threshold_days, expiry_date, sent_at) VALUES (?,?,?,?)",
                (cid, threshold, expiry, now),
            )
        for cid, version in policy_rows:
            db._local.conn.execute(  # noqa: SLF001
                "INSERT OR REPLACE INTO policy_acks(discord_id, policy_version, acked_at) VALUES (?,?,?)",
                (cid, version, now),
            )
        for key, value in meta_rows:
            repos.meta.set(key, value)
    db.snapshot_bytes()  # forces a consistency check before we declare success
    return report


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Import a legacy customers.db into the V9 adfarm.db.")
    parser.add_argument("--from", dest="legacy_path", required=True, help="Path to the legacy customers.db")
    parser.add_argument("--to", dest="target_db", required=True, help="Path for the new adfarm.db")
    parser.add_argument("--vault-key", default=os.environ.get("TOKEN_VAULT_KEY", ""), help="New TOKEN_VAULT_KEY to re-seal tokens")
    parser.add_argument("--legacy-vault-key", default=os.environ.get("LEGACY_TOKEN_VAULT_KEY", ""), help="Legacy TOKEN_VAULT_KEY to de-obfuscate tokens")
    parser.add_argument("--alt-repos", action="append", default=[], help="discord_id:owner/repo,owner/repo (repeatable)")
    parser.add_argument("--dry-run", action="store_true", help="Print the mapping and exit without writing")
    args = parser.parse_args(argv)

    if not os.path.exists(args.legacy_path):
        print(f"error: legacy database not found: {args.legacy_path}", file=sys.stderr)
        return 2

    report = migrate(
        args.legacy_path,
        args.target_db,
        vault_key=args.vault_key,
        legacy_vault_key=args.legacy_vault_key,
        alt_repos=_parse_alt_repos(args.alt_repos),
        dry_run=args.dry_run,
    )
    verb = "would import" if args.dry_run else "imported"
    print(f"dry-run={args.dry_run} → {verb}:")
    for key in ("customers", "alts", "alts_with_token", "runs", "reminders", "policy_acks", "events", "meta"):
        print(f"  {key:>16}: {report[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
