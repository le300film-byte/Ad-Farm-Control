"""RepoProvisioner — create/upload/protect/rename/delete alt repositories on worker accounts.

The sender files are read from ``new_reform/sender`` (single source) and uploaded by the REST
contents API, so provisioning never depends on a checkout of the core repo on the runner.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional

from .accounts import WorkerPool
from .client import GitHubClient
from .secrets import seal_secret

log = logging.getLogger(__name__)

SENDER_DIR = Path(__file__).resolve().parents[2] / "sender"
SENDER_FILES = {
    "send_ads.py": SENDER_DIR / "send_ads.py",
    "channel_registry.py": SENDER_DIR / "channel_registry.py",
    ".github/workflows/send_ads.yml": SENDER_DIR / "workflows" / "send_ads.yml",
    ".github/workflows/self_check.yml": SENDER_DIR / "workflows" / "self_check.yml",
}
DELETED_PREFIX = "_DELETED_"
BANNED_PREFIX = "_BANNED_"
# send_ads.py defaults IMAGE_PATH to "ad.png" inside the checked-out repository, so committing
# the customer's image under this name needs no workflow or sender change.
AD_IMAGE_PATH = "ad.png"


@dataclass
class ProvisionResult:
    owner: str
    repo: str
    created: bool
    files: list[str] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)
    variables: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


class RepoProvisioner:
    def __init__(self, pool: WorkerPool, *, sender_files: Mapping[str, Path] | None = None, private: bool = False):
        self.pool = pool
        self.sender_files = dict(sender_files or SENDER_FILES)
        self.private = private  # free GitHub accounts only get unlimited Actions minutes on public repos

    # ── create ──────────────────────────────────────────────────────────────
    def ensure_repo(self, owner: str, repo: str, *, description: str = "AdFarm alt runner") -> ProvisionResult:
        client = self.pool.client_for(owner)
        existing = client.get_repo(owner, repo)
        created = False
        if existing is None:
            client.create_repo(repo, private=self.private, description=description)
            created = True
        result = ProvisionResult(owner=owner, repo=repo, created=created)
        for rel, path in self.sender_files.items():
            try:
                content = path.read_bytes()
            except FileNotFoundError:
                result.warnings.append(f"missing local sender file: {path}")
                continue
            client.put_file(owner, repo, rel, content, f"adfarm: sync {rel}")
            result.files.append(rel)
        if created and not client.enable_secret_scanning(owner, repo):
            result.warnings.append("secret scanning could not be enabled (plan restriction)")
        return result

    def upload_sender(self, owner: str, repo: str) -> list[str]:
        client = self.pool.client_for(owner)
        done = []
        for rel, path in self.sender_files.items():
            client.put_file(owner, repo, rel, path.read_bytes(), f"adfarm: sync {rel}")
            done.append(rel)
        return done

    def upload_image(self, owner: str, repo: str, raw: bytes, *, path: str = AD_IMAGE_PATH) -> str:
        """Commit the customer's ad image into the repo (F02).

        The old path pushed the image as a repo secret named ``AD_IMAGE_B64`` which nothing ever
        read — and could never have worked: GitHub caps a secret at 48 KB. The sender reads
        ``IMAGE_PATH`` (default ``ad.png``) from the checked-out working tree, so the Contents API
        is the transport that actually reaches it. Its own cap is 1 MB, which is why
        ``MAX_IMAGE_BYTES`` matches that.
        """
        if not raw:
            raise ValueError("image payload is empty")
        client = self.pool.client_for(owner)
        client.put_file(owner, repo, path, raw, "adfarm: update ad image")
        return path

    # ── secrets / variables ─────────────────────────────────────────────────
    def set_secrets(self, owner: str, repo: str, values: Mapping[str, str], *, skip_empty: bool = True) -> list[str]:
        client = self.pool.client_for(owner)
        key = client.repo_public_key(owner, repo)
        done = []
        for name, value in values.items():
            if skip_empty and not value:
                continue
            client.put_secret(owner, repo, name, seal_secret(key["key"], str(value)), key["key_id"])
            done.append(name)
        return done

    def set_variables(self, owner: str, repo: str, values: Mapping[str, str]) -> list[str]:
        client = self.pool.client_for(owner)
        done = []
        for name, value in values.items():
            client.set_variable(owner, repo, name, str(value))
            done.append(name)
        return done

    def secret_names(self, owner: str, repo: str) -> list[str]:
        return self.pool.client_for(owner).list_secret_names(owner, repo)

    # ── lifecycle ───────────────────────────────────────────────────────────
    def exists(self, owner: str, repo: str) -> bool:
        return self.pool.client_for(owner).get_repo(owner, repo) is not None

    def soft_delete(self, owner: str, repo: str, *, prefix: str = DELETED_PREFIX) -> str:
        """Rename instead of delete (the legacy ``_DELETED_`` convention) — recoverable by admins."""
        client = self.pool.client_for(owner)
        if client.get_repo(owner, repo) is None:
            return repo
        new_name = f"{prefix}{repo}"[:100]
        client.rename_repo(owner, repo, new_name)
        return new_name

    def mark_banned(self, owner: str, repo: str) -> str:
        return self.soft_delete(owner, repo, prefix=BANNED_PREFIX)

    def hard_delete(self, owner: str, repo: str) -> bool:
        return self.pool.client_for(owner).delete_repo(owner, repo)

    def scrub_secrets(self, owner: str, repo: str, names: Iterable[str]) -> None:
        client = self.pool.client_for(owner)
        for name in names:
            try:
                client.delete_secret(owner, repo, name)
            except Exception as exc:  # pragma: no cover - best effort
                log.warning("could not delete secret %s on %s/%s: %s", name, owner, repo, exc)

    def client(self, owner: str) -> GitHubClient:
        return self.pool.client_for(owner)

    @staticmethod
    def sender_version(path: Optional[Path] = None) -> str:
        path = path or SENDER_FILES["send_ads.py"]
        try:
            for line in path.read_text(encoding="utf-8").splitlines()[:400]:
                if line.startswith("VERSION"):
                    return line.split("=", 1)[1].strip().strip('"\'')
        except FileNotFoundError:
            pass
        return "unknown"
