"""GistBackup — write-through durability for the SQLite database.

Gist files (compatible naming with the legacy so operators recognise them):
  adfarm.db.b64       current snapshot (base64)
  adfarm.prev.db.b64  previous snapshot
  db-meta.json        {sha256, size, schema, saved_at, run_id, seq}
  LOCK                {run_id, acquired_at, expires_at}  — advisory lease

Design differences from the legacy:
* the Gist id is **never changed at runtime** — a 404 raises ``BackupUnavailable`` and alerts; the
  legacy auto-created a new Gist whose id was lost on the next run (L-19).
* snapshots are coalesced: many commits within ``debounce`` seconds produce one upload.
* the restore chain verifies sha256 **and** SQLite integrity before replacing the DB.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from typing import Optional, Protocol

from ..core.clock import Clock, SystemClock
from .database import Database

log = logging.getLogger(__name__)

CURRENT = "adfarm.db.b64"
PREVIOUS = "adfarm.prev.db.b64"
META = "db-meta.json"
LOCK = "LOCK"


class GistTransport(Protocol):
    """Minimal Gist operations (implemented by ``github.client.GitHubClient`` and test fakes)."""

    def get_gist(self, gist_id: str) -> dict: ...
    def update_gist(self, gist_id: str, files: dict[str, Optional[str]]) -> dict: ...
    def gist_revisions(self, gist_id: str, limit: int = 5) -> list[str]: ...
    def get_gist_revision(self, gist_id: str, sha: str) -> dict: ...


class BackupUnavailable(Exception):
    pass


@dataclass(frozen=True)
class BackupStatus:
    enabled: bool
    gist_id: str
    last_upload_at: float
    last_error: str
    pending: bool
    lease_holder: str
    lease_expires_at: float
    seq: int


class GistBackup:
    def __init__(self, db: Database, transport: GistTransport | None, gist_id: str, *, run_id: str,
                 clock: Clock | None = None, lease_ttl: int = 600, debounce: float = 5.0, retries: tuple[float, ...] = (10.0, 20.0, 40.0)):
        self.db = db
        self.transport = transport
        self.gist_id = (gist_id or "").strip()
        self.run_id = run_id
        self.clock = clock or SystemClock()
        self.lease_ttl = int(lease_ttl)
        self.debounce = float(debounce)
        self.retries = retries
        self._cv = threading.Condition()
        self._pending = False
        self._stop = False
        self._thread: threading.Thread | None = None
        self._seq = 0
        self.last_upload_at = 0.0
        self.last_error = ""
        self.lease_holder = ""
        self.lease_expires_at = 0.0

    # ── wiring ──────────────────────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        return bool(self.gist_id and self.transport is not None)

    def attach(self) -> None:
        """Register the write-through hook on the database."""
        self.db.on_commit(self.enqueue)

    def start(self) -> None:
        if not self.enabled or self._thread:
            return
        self._thread = threading.Thread(target=self._worker, name="gist-backup", daemon=True)
        self._thread.start()

    def stop(self, *, flush: bool = True) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify_all()
        if self._thread:
            self._thread.join(timeout=30)
            self._thread = None
        if flush and self._pending:
            self.flush()

    # ── write-through ───────────────────────────────────────────────────────
    def enqueue(self) -> None:
        if not self.enabled:
            return
        with self._cv:
            self._pending = True
            self._cv.notify_all()

    def flush(self) -> bool:
        """Synchronous upload (used by tests, shutdown, and ``/admin backup now``)."""
        if not self.enabled:
            return False
        with self._cv:
            self._pending = False
        for attempt, delay in enumerate((0.0,) + self.retries):
            try:
                self._upload()
                self.last_error = ""
                return True
            except BackupUnavailable as exc:
                self.last_error = str(exc)
                log.error("backup gist unavailable: %s", exc)
                return False
            except Exception as exc:  # network / API
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("backup upload failed (attempt %d): %s", attempt + 1, exc)
                if attempt < len(self.retries):
                    with self._cv:
                        self._cv.wait(self.retries[attempt])
                    if self._stop:
                        break
        return False

    def _worker(self) -> None:
        while True:
            with self._cv:
                while not self._pending and not self._stop:
                    self._cv.wait()
                if self._stop and not self._pending:
                    return
                # coalesce a burst of writes
                self._cv.wait(self.debounce)
                if self._stop and not self._pending:
                    return
            self.flush()

    def _upload(self) -> None:
        raw = self.db.snapshot_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        existing = self._get_gist()
        files = existing.get("files") or {}
        current_content = (files.get(CURRENT) or {}).get("content")
        payload: dict[str, Optional[str]] = {}
        if current_content:
            payload[PREVIOUS] = current_content
        self._seq += 1
        payload[CURRENT] = base64.b64encode(raw).decode("ascii")
        payload[META] = json.dumps({
            "sha256": digest,
            "size": len(raw),
            "schema": self.db.schema_version,
            "saved_at": self.clock.now(),
            "run_id": self.run_id,
            "seq": self._seq,
        }, indent=2)
        self.transport.update_gist(self.gist_id, payload)  # type: ignore[union-attr]
        self.last_upload_at = self.clock.now()

    def _get_gist(self) -> dict:
        try:
            return self.transport.get_gist(self.gist_id)  # type: ignore[union-attr]
        except Exception as exc:
            status = getattr(exc, "status", None)
            if status == 404:
                raise BackupUnavailable(f"backup gist {self.gist_id} not found (404) — fix ADFARM_GIST_ID; not auto-recreating") from exc
            raise

    # ── restore ─────────────────────────────────────────────────────────────
    def restore_if_missing(self) -> str:
        """Called at boot: if the local DB is absent/empty, pull the newest valid snapshot."""
        import os

        if not self.enabled:
            return "disabled"
        if os.path.exists(self.db.path) and os.path.getsize(self.db.path) > 0:
            try:
                if self.db.integrity_ok() and self.db.table_counts().get("customers", 0) > 0:
                    return "local"
            except Exception:
                pass
        return self.restore()

    def restore(self) -> str:
        """Try current → previous → last 5 revisions. Returns the source used or 'none'."""
        gist = self._get_gist()
        files = gist.get("files") or {}
        meta = _load_meta(files)
        candidates: list[tuple[str, Optional[str], Optional[str]]] = [
            ("current", (files.get(CURRENT) or {}).get("content"), meta.get("sha256")),
            ("previous", (files.get(PREVIOUS) or {}).get("content"), None),
        ]
        for name, content, sha in candidates:
            if content and self._try_restore(content, sha):
                return name
        try:
            revisions = self.transport.gist_revisions(self.gist_id, limit=5)  # type: ignore[union-attr]
        except Exception as exc:
            log.warning("could not list gist revisions: %s", exc)
            revisions = []
        for sha in revisions:
            try:
                rev = self.transport.get_gist_revision(self.gist_id, sha)  # type: ignore[union-attr]
            except Exception:
                continue
            rev_files = rev.get("files") or {}
            content = (rev_files.get(CURRENT) or {}).get("content")
            if content and self._try_restore(content, _load_meta(rev_files).get("sha256")):
                return f"revision:{sha[:7]}"
        return "none"

    def _try_restore(self, b64: str, expected_sha: Optional[str]) -> bool:
        try:
            raw = base64.b64decode(b64.encode("ascii"), validate=True)
        except Exception:
            return False
        if expected_sha and hashlib.sha256(raw).hexdigest() != expected_sha:
            log.warning("restore candidate sha256 mismatch — skipping")
            return False
        try:
            self.db.replace_with(raw)
        except Exception as exc:
            log.warning("restore candidate rejected: %s", exc)
            return False
        return True

    # ── lease ───────────────────────────────────────────────────────────────
    def acquire_lease(self) -> bool:
        """Advisory lock so two chunk runners never write the same DB concurrently."""
        if not self.enabled:
            return True
        gist = self._get_gist()
        files = gist.get("files") or {}
        now = self.clock.now()
        try:
            lock = json.loads((files.get(LOCK) or {}).get("content") or "{}")
        except ValueError:
            lock = {}
        holder = str(lock.get("run_id") or "")
        expires = float(lock.get("expires_at") or 0)
        if holder and holder != self.run_id and expires > now:
            self.lease_holder, self.lease_expires_at = holder, expires
            return False
        self._write_lock(now)
        return True

    def renew_lease(self) -> None:
        if self.enabled:
            self._write_lock(self.clock.now())

    def release_lease(self) -> None:
        if not self.enabled:
            return
        try:
            self.transport.update_gist(self.gist_id, {LOCK: json.dumps({"run_id": "", "acquired_at": 0, "expires_at": 0})})  # type: ignore[union-attr]
        except Exception as exc:  # pragma: no cover - best effort
            log.warning("lease release failed: %s", exc)
        self.lease_holder, self.lease_expires_at = "", 0.0

    def _write_lock(self, now: float) -> None:
        expires = now + self.lease_ttl
        self.transport.update_gist(self.gist_id, {LOCK: json.dumps({"run_id": self.run_id, "acquired_at": now, "expires_at": expires})})  # type: ignore[union-attr]
        self.lease_holder, self.lease_expires_at = self.run_id, expires

    # ── status ──────────────────────────────────────────────────────────────
    def status(self) -> BackupStatus:
        return BackupStatus(
            enabled=self.enabled, gist_id=self.gist_id, last_upload_at=self.last_upload_at, last_error=self.last_error,
            pending=self._pending, lease_holder=self.lease_holder, lease_expires_at=self.lease_expires_at, seq=self._seq,
        )

    def remote_meta(self) -> dict:
        if not self.enabled:
            return {}
        try:
            return _load_meta(self._get_gist().get("files") or {})
        except Exception as exc:
            return {"error": str(exc)}


def _load_meta(files: dict) -> dict:
    try:
        value = json.loads((files.get(META) or {}).get("content") or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}
