"""gist_backup.py — durable customers.db backup / restore via a private Gist.

Phase 0.1 of TODO.md ("The Single Fatal Flaw"):

  * Gist write-through backup: after every DB write the control bot uploads a
    WAL-checkpointed single-file copy to a private GitHub Gist, guarded by one
    writer process (see :func:`acquire_run_lease`) and a monotonically
    increasing revision counter stored in the Gist metadata.
  * Restore-on-startup: on boot the bot downloads the Gist, verifies
    ``PRAGMA integrity_check`` (+ sha256), and only then replaces
    ``customers.db``.  If the newest artifact is corrupt it falls back to the
    previous revision kept in the Gist, then to Gist history revisions.
  * Fallback mode: transient failures retry 3x with 10s/20s/40s backoff; after
    that the bot keeps running on the local DB and raises a critical alert so
    operators know the durability window equals the outage length.
  * Startup lock: the first thing a booting process does is write a LOCK file
    with its run-ID and a 10-minute lease; a second boot inside the lease
    aborts with an alert instead of splitting the brain.

Only the control bot writes the Gist (single-writer discipline).  The module
is intentionally dependency-free (urllib + sqlite3) so it can run in the
control workflow and be unit-tested offline.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import sqlite3
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.error import HTTPError
from urllib.request import Request, urlopen

GITHUB_API = "https://api.github.com"

# Names of the files inside the private backup Gist.
DB_FILENAME = "customers.db.b64"
DB_PREV_FILENAME = "customers.prev.db.b64"
META_FILENAME = "db-meta.json"
LOCK_FILENAME = "LOCK"

# Startup lease: a booting bot holds the LOCK for 10 minutes and renews it
# while it runs (see timer_engine), so a second boot cannot start concurrently.
LEASE_SECONDS = int(os.environ.get("DB_GIST_LEASE_SECONDS", "600") or 600)

# Gist API fallback: retry 3× with 10/20/40s backoff (TODO 0.1).
RETRY_BACKOFFS: tuple[float, ...] = (10.0, 20.0, 40.0)

# Internal state ------------------------------------------------------------
_gist_id = (
    os.environ.get("CUSTOMERS_GIST_ID", "").strip()
    or os.environ.get("CONTROL_GIST_ID", "").strip()
)
_token = (
    os.environ.get("GIST_TOKEN", "").strip()
    or os.environ.get("GH_ADMIN_TOKEN", "").strip()
    or os.environ.get("GH_TOKEN", "").strip()
)
_api_base = os.environ.get("GITHUB_API_BASE", "").strip() or GITHUB_API

_lock = threading.RLock()
_alert_callback: Optional[Callable[[str], None]] = None

# Last known state, exposed to the health dashboard / tests.
LAST_BACKUP: dict[str, Any] = {"ok": False, "revision": 0, "at": 0.0, "error": ""}
LAST_RESTORE: dict[str, Any] = {"ok": False, "source": "", "at": 0.0, "error": ""}
LAST_LEASE: dict[str, Any] = {}

# Debounce: at most one critical alert per 15 minutes per class.
_last_alert_at: dict[str, float] = {}
_alert_min_interval = float(os.environ.get("DB_GIST_ALERT_MIN_SEC", "900") or 900)


class GistError(RuntimeError):
    """Raised when the Gist API fails (retryable)."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


# ── configuration ──────────────────────────────────────────────────────────

def set_config(gist_id: str, token: Optional[str] = None, api_base: Optional[str] = None) -> None:
    """Override configuration (used by setup.py and tests)."""
    global _gist_id, _token, _api_base
    with _lock:
        _gist_id = (gist_id or "").strip()
        if token is not None:
            _token = (token or "").strip()
        if api_base:
            _api_base = api_base.rstrip("/")


def get_config() -> dict[str, str]:
    with _lock:
        return {"gist_id": _gist_id, "api_base": _api_base, "token_set": bool(_token)}


def gist_configured() -> bool:
    with _lock:
        return bool(_gist_id and _token)


def register_alert_callback(cb: Optional[Callable[[str], None]]) -> None:
    """Register the operator alert sink (posts to #admin-alerts)."""
    global _alert_callback
    with _lock:
        _alert_callback = cb


def _alert(message: str, kind: str = "gist") -> None:
    """Raise a (possibly debounced) critical alert to the operator channel."""
    now = time.time()
    with _lock:
        last = _last_alert_at.get(kind, 0.0)
        if kind != "gist-backup-final" or now - last >= _alert_min_interval:
            _last_alert_at[kind] = now
            cb = _alert_callback
        else:
            cb = None
    full = f"🚨 [DB-GIST {kind.upper()}] {message}"
    if cb is not None:
        try:
            cb(full)
        except Exception:
            print(full)
    else:
        print(full)


# ── low-level Gist API ─────────────────────────────────────────────────────

def _headers() -> dict[str, str]:
    tok = _token
    if not tok:
        raise GistError("GIST_TOKEN is not configured.")
    prefix = "Bearer" if tok.lower().startswith(("ghp_", "gho_", "github_pat_", "ghu_", "ghs_")) else "token"
    return {
        "Authorization": f"{prefix} {tok}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "adfarm-control-bot",
    }


def _request(
    method: str,
    path: str,
    body: Optional[dict[str, Any]] = None,
    ok_statuses: tuple[int, ...] = (200, 201, 204),
) -> dict[str, Any]:
    url = f"{_api_base}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = Request(url, data=data, headers=_headers(), method=method)
    try:
        with urlopen(req, timeout=25) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except HTTPError as exc:
        try:
            detail = exc.read().decode(errors="replace")[:300]
        except Exception:
            detail = str(exc.reason)
        if exc.code in (401, 403, 404):
            # Not retryable: credential/ownership problem.
            raise GistError(f"HTTP {exc.code}: {detail}", status=exc.code) from exc
        raise GistError(f"HTTP {exc.code}: {detail}", status=exc.code) from exc
    except Exception as exc:  # DNS/timeouts etc — retryable
        raise GistError(f"{type(exc).__name__}: {exc}") from exc


def fetch_gist(gist_id: Optional[str] = None) -> dict[str, Any]:
    """Fetch full Gist payload (files, history, metadata)."""
    gid = gist_id or _gist_id
    if not gid:
        return {}
    return _request("GET", f"/gists/{gid}")


def update_gist_files(files: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """PATCH a Gist, preserving every file not mentioned in *files*."""
    gid = _gist_id
    if not gid:
        raise GistError("CUSTOMERS_GIST_ID is not configured.")
    return _request("PATCH", f"/gists/{gid}", body={"files": files})


def fetch_gist_revision(sha: str) -> dict[str, Any]:
    """Fetch one historical revision of the Gist (point-in-time recovery)."""
    gid = _gist_id
    if not gid:
        raise GistError("CUSTOMERS_GIST_ID is not configured.")
    return _request("GET", f"/gists/{gid}/{sha}")


# ── snapshot format ────────────────────────────────────────────────────────

def _db_path() -> Path:
    import customer_manager as cm  # lazy — avoids import cycle at module load
    return Path(cm.DB_PATH).expanduser().resolve()


def _checkpoint(db_path: Path) -> None:
    """WAL checkpoint(TRUNCATE) then close, leaving one coherent file."""
    if not db_path.exists():
        return
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass
    finally:
        con.close()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _encode_db(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _decode_db(b64: str) -> bytes:
    return base64.b64decode(b64)


def _verify_db(data: bytes) -> tuple[bool, str]:
    """Integrity-check an in-memory DB copy without touching the live path."""
    if not data:
        return False, "empty backup"
    fd, tmp = tempfile.mkstemp(suffix=".db", prefix=".dbcheck-")
    os.close(fd)
    try:
        Path(tmp).write_bytes(data)
        con = sqlite3.connect(tmp)
        try:
            row = con.execute("PRAGMA integrity_check").fetchone()
            return (row is not None and row[0] == "ok"), (row[0] if row else "no result")
        finally:
            con.close()
    except sqlite3.Error as exc:
        return False, f"sqlite: {exc}"
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ── write-through backup ───────────────────────────────────────────────────

def backup_db_to_gist(reason: str = "write") -> dict[str, Any]:
    """Upload the current customers.db (WAL-checkpointed) to the Gist.

    Returns ``{"ok": bool, "revision": int, "sha256": str, "error": str,
    "degraded": bool}``.  On terminal failure it raises a critical alert and
    returns ``{"ok": False, "degraded": True}`` — the bot keeps running on the
    local DB (durability window = outage length, documented in the runbook).
    """
    global LAST_BACKUP
    if not gist_configured():
        return {"ok": False, "reason": "not_configured", "degraded": False}
    db_path = _db_path()
    with _lock:
        last_err: Optional[Exception] = None
        for attempt in range(len(RETRY_BACKOFFS) + 1):
            try:
                result = _backup_once(db_path, reason)
                LAST_BACKUP = {"ok": True, "revision": result["revision"], "at": time.time(), "error": ""}
                return result
            except Exception as exc:  # noqa: BLE001 — GistError / OSError / sqlite
                last_err = exc
                if attempt < len(RETRY_BACKOFFS):
                    time.sleep(RETRY_BACKOFFS[attempt])
    assert last_err is not None
    LAST_BACKUP = {"ok": False, "revision": LAST_BACKUP.get("revision", 0), "at": time.time(),
                   "error": str(last_err)}
    _alert(
        f"Gist backup FAILED after {len(RETRY_BACKOFFS) + 1} attempts "
        f"({reason}): {last_err}. The bot continues on the local database — "
        f"durability window = outage length. See V8_RUNBOOKS.md §1.",
        kind="gist-backup-final",
    )
    return {"ok": False, "error": str(last_err), "degraded": True, "revision": LAST_BACKUP.get("revision", 0)}


def _backup_once(db_path: Path, reason: str) -> dict[str, Any]:
    _checkpoint(db_path)
    if not db_path.exists():
        raise GistError("customers.db does not exist yet — nothing to back up")
    data = db_path.read_bytes()
    digest = _sha256(data)

    prev_db_b64 = ""
    current_meta: dict[str, Any] = {}
    current_db_b64 = ""
    gist = fetch_gist()
    files = gist.get("files") if isinstance(gist, dict) else {}
    if isinstance(files, dict):
        meta_raw = files.get(META_FILENAME, {})
        try:
            current_meta = json.loads((meta_raw or {}).get("content") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            current_meta = {}
        current_db_b64 = (files.get(DB_FILENAME, {}) or {}).get("content", "")
        prev_db_b64 = (files.get(DB_PREV_FILENAME, {}) or {}).get("content", "")

    try:
        revision = int(current_meta.get("revision", 0) or 0) + 1
    except (TypeError, ValueError):
        revision = 1

    # Split-brain early warning: if another writer bumped the revision while
    # this process was running, single-writer discipline is broken.
    run_id = LAST_LEASE.get("run_id", os.environ.get("GITHUB_RUN_ID", ""))
    mine = current_meta.get("writer_run_id")
    if mine and run_id and mine != run_id and revision > 1:
        _alert(
            f"Possible split-brain: Gist revision {revision - 1} was written by "
            f"`{mine}` but this process is `{run_id}`. Only one control bot "
            "may write the backup Gist. Review lease state.",
            kind="split-brain",
        )

    if current_db_b64 and prev_db_b64:
        # rotate: keep one previous revision for recovery
        pass
    new_db_b64 = _encode_db(data)
    # keep old current as PREV
    prev_db_b64 = current_db_b64 or prev_db_b64

    meta = {
        "revision": revision,
        "sha256": digest,
        "bytes": len(data),
        "updated_at": time.time(),
        "writer_run_id": run_id,
        "reason": reason,
        "prev_sha256": _sha256(_decode_db(prev_db_b64)) if prev_db_b64 else "",
    }
    update_gist_files({
        META_FILENAME: {"content": json.dumps(meta, sort_keys=True)},
        DB_FILENAME: {"content": new_db_b64},
        DB_PREV_FILENAME: {"content": prev_db_b64},
    })
    return {"ok": True, "revision": revision, "sha256": digest, "bytes": len(data),
            "reason": reason, "degraded": False}


# ── restore on startup ─────────────────────────────────────────────────────

def restore_db_from_gist() -> dict[str, Any]:
    """Restore customers.db from the Gist with integrity fallback.

    Tries, in order:
      1. newest artifact (db-meta.json + customers.db.b64)
      2. previous artifact  (customers.prev.db.b64)
      3. each Gist history revision (free point-in-time recovery)
    """
    global LAST_RESTORE
    if not gist_configured():
        LAST_RESTORE = {"ok": False, "source": "", "at": time.time(), "error": "not configured"}
        return LAST_RESTORE

    db_path = _db_path()
    last_err = ""
    candidates: list[dict[str, Any]] = []
    try:
        gist = fetch_gist()
        files = gist.get("files") if isinstance(gist, dict) else {}
        meta_raw = (files.get(META_FILENAME, {}) or {}).get("content", "")
        meta = json.loads(meta_raw) if meta_raw else {}
        candidates.append({
            "label": "current", "revision": (meta or {}).get("revision", 0),
            "b64": (files.get(DB_FILENAME, {}) or {}).get("content", ""),
            "sha": (meta or {}).get("sha256", ""),
        })
        candidates.append({
            "label": "previous", "revision": int((meta or {}).get("revision", 0) or 0) - 1,
            "b64": (files.get(DB_PREV_FILENAME, {}) or {}).get("content", ""),
            "sha": (meta or {}).get("prev_sha256", ""),
        })
        for hist in (gist.get("history") or [])[:5]:
            sha = hist.get("version") or hist.get("sha")
            if sha:
                candidates.append({
                    "label": f"history:{sha[:8]}", "revision": None,
                    "b64": None, "sha": "" if not sha else sha, "fetch_sha": sha,
                })
    except Exception as exc:  # noqa: BLE001
        last_err = f"fetch failed: {exc}"

    for cand in candidates:
        try:
            b64 = cand.get("b64")
            label = cand.get("label", "?")
            if b64 is None and cand.get("fetch_sha"):
                hist = fetch_gist_revision(cand["fetch_sha"])
                hfiles = hist.get("files") if isinstance(hist, dict) else {}
                b64 = (hfiles.get(DB_FILENAME, {}) or {}).get("content", "")
                label = f"history:{cand['fetch_sha'][:8]}"
            if not b64:
                continue
            data = _decode_db(b64)
            digest = _sha256(data)
            if cand.get("sha") and cand["sha"] != digest:
                last_err = f"{label}: sha256 mismatch"
                continue
            ok, detail = _verify_db(data)
            if not ok:
                last_err = f"{label}: integrity_check failed ({detail})"
                continue
            db_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".customers.", suffix=".db", dir=str(db_path.parent))
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                # atomic replacement (POSIX)
                os.replace(tmp, db_path)
                tmp = None
            finally:
                if tmp:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
            result = {"ok": True, "source": label, "revision": cand.get("revision"),
                      "at": time.time(), "sha256": digest, "bytes": len(data)}
            LAST_RESTORE = result
            return result
        except Exception as exc:  # noqa: BLE001
            last_err = f"{cand.get('label', '?')}: {exc}"

    LAST_RESTORE = {"ok": False, "source": "", "at": time.time(), "error": last_err or "no candidates"}
    _alert(
        f"Restore-on-startup FAILED: {last_err}. The bot will continue with "
        "whatever local database exists. Investigate the Gist before the next "
        "write — see V8_RUNBOOKS.md §1.",
        kind="restore-final",
    )
    return LAST_RESTORE


# ── run-ID lease (startup lock) ────────────────────────────────────────────

def _lease_payload(run_id: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "acquired_at": time.time(),
        "expires_at": time.time() + LEASE_SECONDS,
    }


def acquire_run_lease(run_id: Optional[str] = None) -> dict[str, Any]:
    """Write the run-ID LOCK with a 10-minute lease (or refresh it).

    Returns ``{"ok": True, "lease": bool, "run_id": str}`` on success or
    ``{"ok": False, "reason": "concurrent_boot", "holder": {...}}`` when a
    second bot tries to boot inside an active lease.
    """
    global LAST_LEASE
    if not gist_configured():
        LAST_LEASE = {"ok": True, "lease": False, "run_id": run_id or "", "reason": "not_configured"}
        return LAST_LEASE
    run_id = run_id or os.environ.get("GITHUB_RUN_ID", "") or f"local-{uuid.uuid4().hex[:12]}"
    try:
        gist = fetch_gist()
        files = gist.get("files") if isinstance(gist, dict) else {}
        raw_lock = (files.get(LOCK_FILENAME, {}) or {}).get("content", "")
        holder: dict[str, Any] = {}
        if raw_lock:
            try:
                holder = json.loads(raw_lock)
            except (TypeError, ValueError, json.JSONDecodeError):
                holder = {}
        expires = float(holder.get("expires_at", 0) or 0)
        if holder and expires > time.time() and holder.get("run_id") != run_id:
            _alert(
                f"Refusing to start: another control bot holds the lease "
                f"(run `{holder.get('run_id')}`, host `{holder.get('host')}`, "
                f"pid {holder.get('pid')}) until "
                f"{datetime_iso(expires)}. Aborting to prevent split-brain.",
                kind="lease-denied",
            )
            LAST_LEASE = {"ok": False, "reason": "concurrent_boot", "holder": holder, "run_id": run_id}
            return LAST_LEASE
        lease = _lease_payload(run_id)
        update_gist_files({LOCK_FILENAME: {"content": json.dumps(lease, sort_keys=True)}})
        LAST_LEASE = {"ok": True, "lease": True, "run_id": run_id, "expires_at": lease["expires_at"]}
        return LAST_LEASE
    except Exception as exc:  # noqa: BLE001
        _alert(
            f"Startup lease could not be written ({exc}); continuing WITHOUT "
            "the Gist lock. Stop any other bot instance before continuing.",
            kind="lease-error",
        )
        LAST_LEASE = {"ok": False, "reason": f"lease_write_failed: {exc}", "run_id": run_id}
        return LAST_LEASE


def renew_run_lease(run_id: str) -> bool:
    """Refresh the lease while the bot keeps running (best-effort)."""
    if not gist_configured():
        return True
    try:
        lease = _lease_payload(run_id)
        update_gist_files({LOCK_FILENAME: {"content": json.dumps(lease, sort_keys=True)}})
        LAST_LEASE["expires_at"] = lease["expires_at"]
        return True
    except Exception as exc:  # noqa: BLE001
        _alert(f"Lease renewal failed ({exc}); another boot may now be allowed.", kind="lease-renew")
        return False


def release_run_lease(run_id: str) -> bool:
    """Release the lease on clean shutdown (best-effort)."""
    if not gist_configured():
        return True
    try:
        gist = fetch_gist()
        files = gist.get("files") if isinstance(gist, dict) else {}
        raw_lock = (files.get(LOCK_FILENAME, {}) or {}).get("content", "")
        holder = json.loads(raw_lock) if raw_lock else {}
        if holder.get("run_id") != run_id:
            return True  # already replaced/expired; never stomp another owner
        update_gist_files({LOCK_FILENAME: {"content": ""}})
        return True
    except Exception:
        return False


def datetime_iso(ts: float) -> str:
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).isoformat()


# ── background write-through queue ─────────────────────────────────────────
#
# customer_manager calls enqueue_backup(reason) after every write.  A single
# daemon worker drains the queue serially, which enforces single-writer
# discipline even when admin commands race on separate threads.

_WRITE_QUEUE: "queue.Queue[str]" = None  # type: ignore[assignment]
_WORKER_STARTED = False


def _queue() -> "queue.Queue[str]":
    global _WRITE_QUEUE
    import queue as _queue_mod
    if _WRITE_QUEUE is None:
        _WRITE_QUEUE = _queue_mod.Queue()
    return _WRITE_QUEUE


def _worker() -> None:
    while True:
        reason = _queue().get()
        try:
            if gist_configured():
                backup_db_to_gist(reason=reason)
        except Exception as exc:  # noqa: BLE001 — never let the worker die
            _alert(f"background backup worker error: {exc}", kind="worker")
        finally:
            _queue().task_done()


def ensure_backup_worker() -> None:
    global _WORKER_STARTED
    if _WORKER_STARTED:
        return
    _WORKER_STARTED = True
    t = threading.Thread(target=_worker, name="db-gist-backup", daemon=True)
    t.start()
    if gist_configured():
        print(f"[DB-GIST] write-through backup worker started → gist {_gist_id[:8]}…")


def enqueue_backup(reason: str = "write") -> None:
    """Fire-and-forget write-through backup; the worker serializes uploads."""
    if not gist_configured():
        return
    ensure_backup_worker()
    _queue().put(reason)


def flush_backups(timeout: float = 30.0) -> int:
    """Block until pending backups are uploaded (used by tests and shutdown)."""
    q = _queue()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if q.empty():
            return 0
        time.sleep(0.05)
    return q.qsize()
