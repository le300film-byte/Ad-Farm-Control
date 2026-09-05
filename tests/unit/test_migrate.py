"""Tests for the legacy → V9 database importer (tools/migrate_legacy.py)."""
from __future__ import annotations

import base64
import json
import sqlite3

import pytest

from tools.migrate_legacy import migrate

LEGACY_SCHEMA = """
CREATE TABLE customers (
    discord_id TEXT PRIMARY KEY, discord_username TEXT, alt_count INTEGER DEFAULT 1,
    vip BOOLEAN DEFAULT 0, start_date INTEGER, expiry_date INTEGER, active BOOLEAN DEFAULT 1,
    github_account TEXT, repos TEXT, forum_id TEXT, control_thread_id TEXT,
    dashboard_thread_id TEXT, logs_thread_id TEXT, dm_thread_id TEXT, deals_thread_id TEXT,
    autoreply_text TEXT DEFAULT '');
CREATE TABLE alt_credentials (
    discord_id TEXT NOT NULL, alt_index INTEGER NOT NULL, token TEXT, channel_ids TEXT,
    username TEXT, updated_at INTEGER NOT NULL, PRIMARY KEY (discord_id, alt_index));
CREATE TABLE run_state (
    discord_id TEXT NOT NULL, alt_index INTEGER NOT NULL, mode TEXT, runtime_hours INTEGER DEFAULT 0,
    started_at REAL DEFAULT 0, last_dispatch_at REAL DEFAULT 0, renewals INTEGER DEFAULT 0, payload TEXT,
    PRIMARY KEY (discord_id, alt_index));
CREATE TABLE reminder_sent (discord_id TEXT NOT NULL, threshold INTEGER NOT NULL, sent_at INTEGER NOT NULL,
    PRIMARY KEY (discord_id, threshold));
CREATE TABLE policy_acks (discord_id TEXT PRIMARY KEY, acked_at INTEGER NOT NULL, version TEXT NOT NULL);
CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, discord_id TEXT, event TEXT NOT NULL, ts REAL NOT NULL, payload TEXT);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _obfuscate(value: str, key: str) -> str:
    raw = value.encode("utf-8")
    if key:
        kb = (key.encode() * (len(raw) // len(key) + 1))[: len(raw)]
        raw = bytes(a ^ b for a, b in zip(raw, kb))
    return base64.b64encode(raw).decode("ascii")


def make_legacy(path: str, *, legacy_key: str = "") -> None:
    con = sqlite3.connect(path)
    con.executescript(LEGACY_SCHEMA)
    con.execute(
        "INSERT INTO customers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("200000000000000001", "alice", 2, 1, 1_700_000_000, 1_700_100_000, 1, "gh_acct",
         json.dumps(["worker1/alice_alt1", "worker1/alice_alt2"]), "forum-1", "ctrl-1", "dash-1",
         "logs-1", "dm-1", "deals-1", "hi there"),
    )
    con.execute(
        "INSERT INTO alt_credentials VALUES (?,?,?,?,?,?)",
        ("200000000000000001", 1, _obfuscate("supersecrettoken", legacy_key), json.dumps(["111", "222"]), "alt_one", 1_700_000_500),
    )
    con.execute(
        "INSERT INTO run_state VALUES (?,?,?,?,?,?,?,?)",
        ("200000000000000001", 1, "sell", 0, 1_700_000_900, 1_700_000_900, 3, json.dumps({"rate": 1.5})),
    )
    con.execute("INSERT INTO reminder_sent VALUES (?,?,?)", ("200000000000000001", 7, 1_700_000_000))
    con.execute("INSERT INTO policy_acks VALUES (?,?,?)", ("200000000000000001", 1_700_000_000, "v8-1"))
    con.execute("INSERT INTO events VALUES (?,?,?,?,?)", (1, "200000000000000001", "customer_activated", 1_700_000_000, "{}"))
    con.execute("INSERT INTO meta VALUES (?,?)", ("open_ticket_ch_id", "ticket-9"))
    con.commit()
    con.close()


def test_dry_run_reports_counts(tmp_path):
    legacy = tmp_path / "customers.db"
    target = tmp_path / "adfarm.db"
    make_legacy(str(legacy))
    report = migrate(str(legacy), str(target), legacy_vault_key="", dry_run=True)
    assert report["dry_run"] is True
    assert report["customers"] == 1
    assert report["alts"] == 1
    assert report["alts_with_token"] == 1
    assert report["runs"] == 1
    assert report["reminders"] == 1
    assert report["policy_acks"] == 1
    assert report["events"] == 1
    assert report["meta"] == 1
    # No target file created on dry run.
    assert not target.exists()


def test_migration_writes_rows(tmp_path):
    legacy = tmp_path / "customers.db"
    target = tmp_path / "adfarm.db"
    make_legacy(str(legacy))
    report = migrate(str(legacy), str(target), legacy_vault_key="", dry_run=False)
    assert report["customers"] == 1

    from adfarm.db import Database
    from adfarm.services.container import Repos

    db = Database(str(target))
    db.migrate()
    repos = Repos.for_db(db)

    customer = repos.customers.get("200000000000000001")
    assert customer is not None
    assert customer.username == "alice"
    assert customer.vip is True
    assert customer.thread_ids["control"] == "ctrl-1"
    assert customer.thread_ids["dm-inbox"] == "dm-1"
    assert customer.thread_ids["farm-logs"] == "logs-1"

    alt = repos.alts.get("200000000000000001", 1)
    assert alt is not None
    assert alt.status.value == "ready"
    assert alt.repo_owner == "worker1"
    assert alt.repo_name == "alice_alt1"
    assert alt.channel_ids == ("111", "222")
    assert alt.sender_alt_id >= 1

    run = repos.runs.get("200000000000000001", 1)
    assert run is not None
    assert run.mode.value == "limitless"
    assert run.renewals == 3

    assert repos.reminders.was_sent("200000000000000001", 7, customer.expiry_date)
    assert repos.policy_acks.has_acked("200000000000000001", "v8-1")
    assert repos.meta.get("open_ticket_ch_id") == "ticket-9"
    assert len(repos.events.recent(limit=10)) == 1


def test_migration_reseals_token_with_vault(tmp_path):
    legacy = tmp_path / "customers.db"
    target = tmp_path / "adfarm.db"
    make_legacy(str(legacy), legacy_key="legacy-key")
    migrate(str(legacy), str(target), vault_key="new-test-vault-key-123", legacy_vault_key="legacy-key", dry_run=False)

    from adfarm.db import Database, TokenVault
    from adfarm.services.container import Repos

    db = Database(str(target))
    repos = Repos.for_db(db)
    alt = repos.alts.get("200000000000000001", 1)
    vault = TokenVault("new-test-vault-key-123")
    # Token was de-obfuscated with the legacy key and re-sealed with the new vault.
    assert vault.is_sealed(alt.token_ciphertext)
    assert vault.open(alt.token_ciphertext) == "supersecrettoken"


def test_migration_with_alt_repos_override(tmp_path):
    legacy = tmp_path / "customers.db"
    target = tmp_path / "adfarm.db"
    make_legacy(str(legacy))  # legacy repos intentionally wrong/none here
    migrate(
        str(legacy), str(target),
        alt_repos={"200000000000000001": ["correct_owner/alice_alt1"]},
        dry_run=False,
    )
    from adfarm.db import Database
    from adfarm.services.container import Repos

    db = Database(str(target))
    alt = Repos.for_db(db).alts.get("200000000000000001", 1)
    assert alt.repo_owner == "correct_owner"
    assert alt.repo_name == "alice_alt1"
