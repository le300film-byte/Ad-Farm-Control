"""SQLite database with versioned migrations and a write-through hook.

Every repository goes through ``Database.transaction()``; when the outermost transaction commits,
``on_commit`` callbacks fire (used by GistBackup to enqueue a snapshot). Connections are opened
per call in WAL mode so the bot thread and the backup thread never share a cursor.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import sqlite3
import threading
from pathlib import Path
from typing import Callable, Iterator

from .migrations import MIGRATIONS, SCHEMA_VERSION


class Database:
    def __init__(self, path: str | os.PathLike[str], *, busy_timeout: float = 30.0):
        self.path = str(path)
        # sqlite's busy timeout, exposed so a lock contention can be reproduced in tests (F05).
        self.busy_timeout = float(busy_timeout)
        self._lock = threading.RLock()
        self._local = threading.local()
        self._on_commit: list[Callable[[], None]] = []

    # ── lifecycle ───────────────────────────────────────────────────────────
    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=self.busy_timeout, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def migrate(self) -> int:
        """Apply pending migrations; returns the resulting schema version."""
        Path(self.path).parent.mkdir(parents=True, exist_ok=True) if self.path != ":memory:" else None
        with self._lock:
            conn = self.connect()
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)")
                current = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
                for version, statements in MIGRATIONS:
                    if version <= current:
                        continue
                    began = False
                    try:
                        conn.execute("BEGIN")
                        began = True
                        for stmt in statements:
                            conn.execute(stmt)
                        conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (?, strftime('%s','now'))", (version,))
                        conn.execute("COMMIT")
                    except Exception:
                        # F05: ROLLBACK only when the BEGIN actually succeeded — rolling back a
                        # non-existent transaction raises and would mask the original error.
                        if began:
                            with contextlib.suppress(Exception):
                                conn.execute("ROLLBACK")
                        raise
                    current = version
                return int(current)
            finally:
                conn.close()

    @property
    def schema_version(self) -> int:
        with self.read() as conn:
            try:
                return int(conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0])
            except sqlite3.OperationalError:
                return 0

    def on_commit(self, callback: Callable[[], None]) -> None:
        self._on_commit.append(callback)

    # ── transactions ────────────────────────────────────────────────────────
    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Re-entrant write transaction; commit hooks fire once, after the outermost commit."""
        depth = getattr(self._local, "depth", 0)
        if depth:
            self._local.depth = depth + 1
            try:
                yield self._local.conn
            finally:
                self._local.depth -= 1
            return
        with self._lock:
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
            except BaseException:
                # F05: a failed BEGIN (lock timeout, I/O error, disk full) must leave the
                # thread-local transaction state untouched. The old code set depth=1 *before*
                # BEGIN, so the raised error skipped the cleanup in the finally below and every
                # later transaction() on this thread took the re-entrant branch, reusing a
                # closed connection and never committing again.
                with contextlib.suppress(Exception):
                    conn.close()
                raise
            self._local.conn = conn
            self._local.depth = 1
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                # BaseException, not Exception: asyncio.CancelledError / KeyboardInterrupt
                # must roll back too, and a failing ROLLBACK must never mask the real error.
                with contextlib.suppress(Exception):
                    conn.execute("ROLLBACK")
                raise
            finally:
                self._local.depth = 0
                self._local.conn = None
                with contextlib.suppress(Exception):
                    conn.close()
        for cb in list(self._on_commit):
            try:
                cb()
            except Exception:  # hooks must never break a committed write
                pass

    @contextlib.contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        depth = getattr(self._local, "depth", 0)
        if depth:
            yield self._local.conn
            return
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    # ── maintenance ─────────────────────────────────────────────────────────
    def integrity_ok(self) -> bool:
        with self.read() as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            return bool(row) and str(row[0]).lower() == "ok"

    def snapshot_bytes(self) -> bytes:
        """Consistent copy of the database using the online backup API."""
        with self._lock:
            src = self.connect()
            try:
                mem = sqlite3.connect(":memory:")
                try:
                    src.backup(mem)
                    return b"".join(_serialize(mem))
                finally:
                    mem.close()
            finally:
                src.close()

    def payload_is_usable(self, raw: bytes) -> bool:
        """True when ``raw`` is an intact SQLite database that carries the adfarm schema.

        F06: restore candidates are validated on a throwaway copy *before* the live database
        file is touched, so a truncated / foreign / empty snapshot can never be swapped in and
        can never be mistaken for a legitimate starting point.
        """
        if not raw:
            return False
        tmp = f"{self.path}.verify.tmp"
        try:
            with open(tmp, "wb") as fh:
                fh.write(raw)
            conn = sqlite3.connect(tmp)
            try:
                row = conn.execute("PRAGMA integrity_check").fetchone()
                if not row or str(row[0]).lower() != "ok":
                    return False
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                return {"schema_migrations", "customers"} <= tables
            finally:
                conn.close()
        except Exception:
            return False
        finally:
            with contextlib.suppress(OSError):
                os.remove(tmp)

    def replace_with(self, raw: bytes) -> None:
        """Atomically replace the on-disk database with ``raw`` (validated first)."""
        tmp = f"{self.path}.restore.tmp"
        with open(tmp, "wb") as fh:
            fh.write(raw)
        check = sqlite3.connect(tmp)
        try:
            ok = check.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            check.close()
        if str(ok).lower() != "ok":
            os.remove(tmp)
            raise ValueError("restore payload failed integrity_check")
        with self._lock:
            for suffix in ("-wal", "-shm"):
                with contextlib.suppress(FileNotFoundError):
                    os.remove(self.path + suffix)
            shutil.move(tmp, self.path)

    def table_counts(self) -> dict[str, int]:
        with self.read() as conn:
            names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            return {n: int(conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0]) for n in names}

    def __repr__(self) -> str:  # pragma: no cover
        return f"Database(path={self.path!r}, schema={SCHEMA_VERSION})"


def _serialize(conn: sqlite3.Connection) -> Iterator[bytes]:
    if hasattr(conn, "serialize"):
        yield conn.serialize()
        return
    # Python < 3.11 fallback: dump through a temp file.
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as fh:
        tmp_path = fh.name
    disk = sqlite3.connect(tmp_path)
    try:
        conn.backup(disk)
    finally:
        disk.close()
    with open(tmp_path, "rb") as fh:
        yield fh.read()
    os.remove(tmp_path)
