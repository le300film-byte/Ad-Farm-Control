"""WorkerPool — the three worker GitHub accounts that host customer alt repositories.

* token resolution is by **repo owner** (never "the first token in the list");
* the round-robin cursor is persisted in ``meta.worker_cursor`` so distribution survives the
  chunked 24/7 runner (L-11);
* a worker whose token no longer authenticates is skipped and reported by ``/admin health``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from ..config import WorkerAccount
from ..core.errors import ConfigurationError
from ..db.repositories import MetaRepo
from .client import GitHubClient

log = logging.getLogger(__name__)

CURSOR_KEY = "worker_cursor"


@dataclass(frozen=True)
class WorkerHealth:
    login: str
    ok: bool
    detail: str
    scopes: tuple[str, ...] = ()


class WorkerPool:
    def __init__(self, workers: tuple[WorkerAccount, ...], base_client: GitHubClient, meta: MetaRepo, *, main_login: str = "", main_client: GitHubClient | None = None):
        self._workers = tuple(workers)
        self._base = base_client
        self._meta = meta
        self._main_login = (main_login or "").lower()
        self._main_client = main_client
        self._clients: dict[str, GitHubClient] = {}

    # ── lookup ──────────────────────────────────────────────────────────────
    @property
    def logins(self) -> tuple[str, ...]:
        return tuple(w.login for w in self._workers)

    def has(self, login: str) -> bool:
        return self.get(login) is not None

    def get(self, login: str) -> Optional[WorkerAccount]:
        wanted = (login or "").lower()
        for w in self._workers:
            if w.login.lower() == wanted:
                return w
        return None

    def client_for(self, owner: str) -> GitHubClient:
        """Client authenticated as the account that owns ``owner``'s repos."""
        worker = self.get(owner)
        if worker is not None:
            if worker.login not in self._clients:
                self._clients[worker.login] = self._base.with_token(worker.token)
            return self._clients[worker.login]
        if self._main_client is not None and owner.lower() == self._main_login:
            return self._main_client
        raise ConfigurationError(f"No token configured for GitHub owner '{owner}'.")

    # ── round robin ─────────────────────────────────────────────────────────
    def pick(self, *, exclude: set[str] | None = None) -> WorkerAccount:
        if not self._workers:
            raise ConfigurationError("No worker accounts configured — cannot provision alt repositories.")
        exclude_l = {e.lower() for e in (exclude or set())}
        n = len(self._workers)
        cursor = self._meta.get_int(CURSOR_KEY, 0) % n
        # Walk the ring from the cursor; the first non-excluded worker wins, and the cursor moves past it
        # so the next pick continues the rotation (excluded workers are skipped, not re-indexed).
        for step in range(n):
            candidate = self._workers[(cursor + step) % n]
            if candidate.login.lower() not in exclude_l:
                self._meta.set(CURSOR_KEY, str((cursor + step + 1) % n))
                return candidate
        chosen = self._workers[cursor]           # everything excluded → fall back to plain rotation
        self._meta.set(CURSOR_KEY, str((cursor + 1) % n))
        return chosen

    def least_loaded(self, counts: dict[str, int]) -> WorkerAccount:
        """Alternative to pure round robin: choose the worker with the fewest live repos."""
        if not self._workers:
            raise ConfigurationError("No worker accounts configured — cannot provision alt repositories.")
        return min(self._workers, key=lambda w: (counts.get(w.login, 0), self.logins.index(w.login)))

    # ── health ──────────────────────────────────────────────────────────────
    def health(self) -> list[WorkerHealth]:
        out: list[WorkerHealth] = []
        for w in self._workers:
            client = self.client_for(w.login)
            try:
                me = client.viewer()
                scopes = client.token_scopes()
                login = str(me.get("login") or "")
                if login.lower() != w.login.lower():
                    out.append(WorkerHealth(w.login, False, f"token belongs to '{login}', not '{w.login}'", scopes))
                    continue
                missing = [s for s in ("repo", "workflow") if s not in scopes] if scopes else []
                out.append(WorkerHealth(w.login, not missing, "ok" if not missing else f"missing scopes: {', '.join(missing)}", scopes))
            except Exception as exc:
                out.append(WorkerHealth(w.login, False, f"{type(exc).__name__}: {exc}"))
        return out
