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
import itertools
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
EMPTY_LOCK = {"run_id": "", "token": "", "acquired_at": 0, "expires_at": 0}


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
    restore_blocked: bool = False    # F06: remote snapshot could not be used, uploads are held


class GistBackup:
    def __init__(self, db: Database, transport: GistTransport | None, gist_id: str, *, run_id: str,
                 clock: Clock | None = None, lease_ttl: int = 600, debounce: float = 5.0, retries: tuple[float, ...] = (10.0, 20.0, 40.0),
                 lease_attempts: int = 3):
        self.db = db
        self.transport = transport
        self.gist_id = (gist_id or "").strip()
        self.run_id = run_id
        self.clock = clock or SystemClock()
        self.lease_ttl = int(lease_ttl)
        self.debounce = float(debounce)
        self.retries = retries
        self.lease_attempts = max(1, int(lease_attempts))
        self._cv = threading.Condition()
        self._pending = False
        self._stop = False
        self._thread: threading.Thread | None = None
        self._seq = 0
        self._lease_token = ""                       # unique per acquisition — the CAS nonce (F04)
        self._nonce = itertools.count(1)
        self.last_upload_at = 0.0
        self.last_error = ""
        self.lease_holder = ""
        self.lease_expires_at = 0.0
        self.restore_blocked = False                 # F06: set when a remote snapshot could not be used

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

    def flush(self, *, force: bool = False) -> bool:
        """Synchronous upload (used by tests, shutdown, and ``/admin backup now``).

        ``force`` bypasses the F06 safety interlock that refuses to overwrite a remote
        snapshot with an empty local database after a failed restore.
        """
        if not self.enabled:
            return False
        with self._cv:
            self._pending = False
        for attempt, delay in enumerate((0.0,) + self.retries):
            try:
                self._upload(force=force)
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

    def _upload(self, *, force: bool = False) -> None:
        raw = self.db.snapshot_bytes()
        if not force and self.restore_blocked and self._local_is_empty():
            # F06: the remote Gist holds a snapshot we could not use and the local database is
            # empty. Overwriting it now would destroy the only copy of the customer data, so we
            # hold the upload and surface the reason instead. `/admin backup sub:force` is the
            # deliberate escape hatch once the operator has confirmed the remote is useless.
            raise BackupUnavailable(
                f"refusing to overwrite the remote snapshot with an empty local database "
                f"(restore of gist {self.gist_id} failed). Fix ADFARM_GIST_ID / the snapshot, "
                f"or force it with /admin backup sub:force."
            )
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

    def _local_is_empty(self) -> bool:
        """True when the local database carries no customer rows (fresh install or failed restore)."""
        try:
            return int(self.db.table_counts().get("customers", 0)) == 0
        except Exception:
            return True

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
        had_remote_snapshot = bool((files.get(CURRENT) or {}).get("content") or (files.get(PREVIOUS) or {}).get("content"))
        candidates: list[tuple[str, Optional[str], Optional[str]]] = [
            ("current", (files.get(CURRENT) or {}).get("content"), meta.get("sha256")),
            ("previous", (files.get(PREVIOUS) or {}).get("content"), None),
        ]
        for name, content, sha in candidates:
            if content and self._try_restore(content, sha):
                self.restore_blocked = False
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
                self.restore_blocked = False
                return f"revision:{sha[:7]}"
        if had_remote_snapshot:
            # F06: a snapshot exists but nothing in it is usable. Arm the interlock so the first
            # write-through of the now-empty local database cannot clobber the remote copy.
            self.restore_blocked = True
            log.error("restore failed: gist %s has a snapshot but no candidate passed sha256 + "
                      "integrity + schema checks. Uploads are held until an operator intervenes.", self.gist_id)
        return "none"

    def _try_restore(self, b64: str, expected_sha: Optional[str]) -> bool:
        try:
            raw = base64.b64decode(b64.encode("ascii"), validate=True)
        except Exception:
            return False
        if expected_sha and hashlib.sha256(raw).hexdigest() != expected_sha:
            log.warning("restore candidate sha256 mismatch — skipping")
            return False
        # Integrity + schema are checked on a scratch copy first, so a rejected payload leaves
        # the live database exactly as it was (F06).
        if not self.db.payload_is_usable(raw):
            log.warning("restore candidate is not an intact adfarm database — skipping")
            return False
        try:
            self.db.replace_with(raw)
        except Exception as exc:
            log.warning("restore candidate rejected: %s", exc)
            return False
        return True

    # ── lease ───────────────────────────────────────────────────────────────
    def acquire_lease(self) -> bool:
        """Advisory lock so two chunk runners never write the same DB concurrently.

        F04: the Gist API has no conditional-write primitive, so the compare-and-swap is
        emulated — we write a lock carrying a unique token and then read the Gist back. Only
        the writer whose token survived owns the lease; a loser backs off and re-reads. A
        foreign lease that has not expired is never stolen.
        """
        if not self.enabled:
            return True
        token = f"{self.run_id}:{self.clock.now():.0f}:{next(self._nonce)}"
        for attempt in range(self.lease_attempts):
            now = self.clock.now()
            try:
                lock = _load_lock(self._get_gist().get("files") or {})
            except BackupUnavailable:
                raise
            except Exception as exc:
                log.warning("lease read failed (attempt %d): %s", attempt + 1, exc)
                continue
            holder = str(lock.get("run_id") or "")
            expires = float(lock.get("expires_at") or 0)
            if holder and holder != self.run_id and expires > now:
                self.lease_holder, self.lease_expires_at = holder, expires
                return False                      # somebody else owns a live lease — do not steal
            if holder == self.run_id and str(lock.get("token") or "") == token:
                self.lease_holder, self.lease_expires_at = self.run_id, expires
                return True
            try:
                self._write_lock(now, token)
            except Exception as exc:
                log.warning("lease write failed (attempt %d): %s", attempt + 1, exc)
                continue
            seen = _load_lock(self._safe_gist_files())
            if str(seen.get("run_id") or "") == self.run_id and str(seen.get("token") or "") == token:
                self._lease_token = token
                self.lease_holder = self.run_id
                self.lease_expires_at = float(seen.get("expires_at") or 0)
                return True
            log.warning("lease write lost a race (gist now shows run_id=%r) — retrying", seen.get("run_id"))
        return False

    def renew_lease(self) -> bool:
        """Extend our own lease. Returns False when the lease is held by another run — the
        caller must stop writing the database rather than fight over it (F04)."""
        if not self.enabled:
            return True
        lock = _load_lock(self._safe_gist_files())
        holder = str(lock.get("run_id") or "")
        if holder != self.run_id:
            self.lease_holder = holder
            self.lease_expires_at = float(lock.get("expires_at") or 0)
            log.warning("lease renewal refused: the lease belongs to %r (we are %r)", holder or "<none>", self.run_id)
            return False
        self._write_lock(self.clock.now())
        return True

    def release_lease(self) -> bool:
        """Release **our own** lease and nobody else's.

        F04: the previous implementation unconditionally wrote an empty lock, so a runner that
        never held the lease (rejected at boot, or restarted after a takeover) released the
        active holder's lease and opened the door to a split brain.
        """
        if not self.enabled:
            return False
        try:
            lock = _load_lock(self._get_gist().get("files") or {})
        except Exception as exc:
            log.warning("lease release failed: %s", exc)
            return False
        holder = str(lock.get("run_id") or "")
        token = str(lock.get("token") or "")
        if holder != self.run_id or (self._lease_token and token != self._lease_token):
            self.lease_holder = holder
            self.lease_expires_at = float(lock.get("expires_at") or 0)
            log.warning("lease release skipped: the lease belongs to %r (we are %r)", holder or "<none>", self.run_id)
            return False
        try:
            self.transport.update_gist(self.gist_id, {LOCK: json.dumps(dict(EMPTY_LOCK))})  # type: ignore[union-attr]
        except Exception as exc:  # pragma: no cover - best effort
            log.warning("lease release failed: %s", exc)
            return False
        self._lease_token = ""
        self.lease_holder, self.lease_expires_at = "", 0.0
        return True

    def _safe_gist_files(self) -> dict:
        try:
            return self._get_gist().get("files") or {}
        except Exception as exc:
            log.warning("gist read failed: %s", exc)
            return {}

    def _write_lock(self, now: float, token: str = "") -> None:
        expires = now + self.lease_ttl
        token = token or self._lease_token or f"{self.run_id}:{now:.0f}"
        self.transport.update_gist(self.gist_id, {LOCK: json.dumps(  # type: ignore[union-attr]
            {"run_id": self.run_id, "token": token, "acquired_at": now, "expires_at": expires})})
        self._lease_token = token
        self.lease_holder, self.lease_expires_at = self.run_id, expires

    # ── status ──────────────────────────────────────────────────────────────
    def status(self) -> BackupStatus:
        return BackupStatus(
            enabled=self.enabled, gist_id=self.gist_id, last_upload_at=self.last_upload_at, last_error=self.last_error,
            pending=self._pending, lease_holder=self.lease_holder, lease_expires_at=self.lease_expires_at, seq=self._seq,
            restore_blocked=self.restore_blocked,
        )

    def remote_meta(self) -> dict:
        if not self.enabled:
            return {}
        try:
            return _load_meta(self._get_gist().get("files") or {})
        except Exception as exc:
            return {"error": str(exc)}

    def remote_lease(self) -> dict:
        if not self.enabled:
            return {}
        return _load_lock(self._safe_gist_files())


def _load_meta(files: dict) -> dict:
    try:
        value = json.loads((files.get(META) or {}).get("content") or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _load_lock(files: dict) -> dict:
    """Parse the advisory LOCK file; any malformed content degrades to 'no lease'."""
    try:
        value = json.loads((files.get(LOCK) or {}).get("content") or "{}")
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}
