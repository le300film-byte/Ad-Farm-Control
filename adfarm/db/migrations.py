"""Ordered schema migrations. Never edit an applied migration — append a new one."""
from __future__ import annotations

SCHEMA_VERSION = 1

_V1 = [
    """
    CREATE TABLE IF NOT EXISTS customers (
        discord_id        TEXT PRIMARY KEY,
        username          TEXT NOT NULL DEFAULT '',
        alt_count         INTEGER NOT NULL DEFAULT 1,
        vip               INTEGER NOT NULL DEFAULT 0,
        start_date        REAL NOT NULL,
        expiry_date       REAL NOT NULL,
        active            INTEGER NOT NULL DEFAULT 1,
        github_account    TEXT NOT NULL DEFAULT '',
        forum_id          TEXT NOT NULL DEFAULT '',
        thread_ids        TEXT NOT NULL DEFAULT '{}',
        autoreply_text    TEXT NOT NULL DEFAULT '',
        notes             TEXT NOT NULL DEFAULT '',
        created_at        REAL NOT NULL,
        updated_at        REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS alts (
        customer_id       TEXT NOT NULL REFERENCES customers(discord_id) ON DELETE CASCADE,
        alt_index         INTEGER NOT NULL,
        sender_alt_id     INTEGER NOT NULL UNIQUE,
        repo_owner        TEXT NOT NULL,
        repo_name         TEXT NOT NULL,
        status            TEXT NOT NULL DEFAULT 'pending',
        discord_user_id   TEXT NOT NULL DEFAULT '',
        username          TEXT NOT NULL DEFAULT '',
        display_name      TEXT NOT NULL DEFAULT '',
        channel_ids       TEXT NOT NULL DEFAULT '[]',
        token_ciphertext  TEXT NOT NULL DEFAULT '',
        sync_state        TEXT NOT NULL DEFAULT 'clean',
        runtime_overrides TEXT NOT NULL DEFAULT '{}',
        created_at        REAL NOT NULL,
        updated_at        REAL NOT NULL,
        PRIMARY KEY (customer_id, alt_index)
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_alts_repo ON alts(repo_owner, repo_name)",
    "CREATE INDEX IF NOT EXISTS idx_alts_status ON alts(status)",
    """
    CREATE TABLE IF NOT EXISTS runs (
        customer_id       TEXT NOT NULL,
        alt_index         INTEGER NOT NULL,
        mode              TEXT NOT NULL,
        runtime_hours     INTEGER NOT NULL DEFAULT 0,
        started_at        REAL NOT NULL,
        last_dispatch_at  REAL NOT NULL,
        renewals          INTEGER NOT NULL DEFAULT 0,
        payload           TEXT NOT NULL DEFAULT '{}',
        run_id            INTEGER,
        status            TEXT NOT NULL DEFAULT 'queued',
        conclusion        TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (customer_id, alt_index),
        FOREIGN KEY (customer_id, alt_index) REFERENCES alts(customer_id, alt_index) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS customer_webhooks (
        customer_id       TEXT PRIMARY KEY REFERENCES customers(discord_id) ON DELETE CASCADE,
        dashboard         TEXT NOT NULL DEFAULT '',
        logs              TEXT NOT NULL DEFAULT '',
        deals             TEXT NOT NULL DEFAULT '',
        dm                TEXT NOT NULL DEFAULT '',
        updated_at        REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reminders_sent (
        customer_id       TEXT NOT NULL,
        threshold_days    INTEGER NOT NULL,
        expiry_date       REAL NOT NULL,
        sent_at           REAL NOT NULL,
        PRIMARY KEY (customer_id, threshold_days, expiry_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS policy_acks (
        discord_id        TEXT NOT NULL,
        policy_version    TEXT NOT NULL,
        acked_at          REAL NOT NULL,
        PRIMARY KEY (discord_id, policy_version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        discord_id        TEXT NOT NULL DEFAULT '',
        event             TEXT NOT NULL,
        ts                REAL NOT NULL,
        payload           TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)",
    "CREATE INDEX IF NOT EXISTS idx_events_customer ON events(discord_id, ts)",
    """
    CREATE TABLE IF NOT EXISTS tickets (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id       TEXT NOT NULL,
        kind              TEXT NOT NULL,
        status            TEXT NOT NULL DEFAULT 'open',
        payload           TEXT NOT NULL DEFAULT '{}',
        created_at        REAL NOT NULL,
        updated_at        REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tickets_open ON tickets(status, customer_id)",
    """
    CREATE TABLE IF NOT EXISTS meta (
        key               TEXT PRIMARY KEY,
        value             TEXT NOT NULL
    )
    """,
]

MIGRATIONS: list[tuple[int, list[str]]] = [
    (1, _V1),
]
