"""Domain models. Frozen dataclasses — services return new instances, never mutate."""
from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field, replace
from typing import Any, Optional

DAY = 86_400.0


class Tier(str, enum.Enum):
    PUBLIC = "public"
    CUSTOMER = "customer"
    VIP = "vip"
    ADMIN = "admin"

    @property
    def rank(self) -> int:
        return {"public": 0, "customer": 1, "vip": 2, "admin": 3}[self.value]

    def covers(self, required: "Tier") -> bool:
        return self.rank >= required.rank


class AltStatus(str, enum.Enum):
    PENDING = "pending"        # repo provisioned, no credentials yet
    READY = "ready"            # token + channels stored, can run
    BANNED = "banned"          # ban marker seen; awaiting replacement
    MISSING = "missing"        # repo no longer exists on GitHub
    REMOVED = "removed"        # soft-deleted


class RunMode(str, enum.Enum):
    TIMED = "timed"
    LIMITLESS = "limitless"


class SyncState(str, enum.Enum):
    CLEAN = "clean"
    DIRTY = "dirty"            # DB updated but GitHub push failed → retried by sweeper


@dataclass(frozen=True)
class Customer:
    discord_id: str
    username: str
    alt_count: int
    vip: bool
    start_date: float
    expiry_date: float
    active: bool
    github_account: str = ""
    forum_id: str = ""
    thread_ids: dict[str, str] = field(default_factory=dict)   # control/dashboard/farm-logs/deals/dm-inbox
    autoreply_text: str = ""
    notes: str = ""

    def is_active(self, now: float) -> bool:
        return self.active and self.expiry_date > now

    def days_remaining(self, now: float) -> float:
        return max(0.0, (self.expiry_date - now) / DAY)

    def tier(self, now: float) -> Tier:
        if not self.is_active(now):
            return Tier.PUBLIC
        return Tier.VIP if self.vip else Tier.CUSTOMER

    def thread(self, name: str) -> str:
        return str(self.thread_ids.get(name) or "")

    def with_(self, **changes: Any) -> "Customer":
        return replace(self, **changes)


@dataclass(frozen=True)
class Alt:
    customer_id: str
    alt_index: int                 # 1..alt_count, customer-facing
    sender_alt_id: int             # globally unique ALT_ID used by send_ads.py + control gist file
    repo_owner: str
    repo_name: str
    status: AltStatus = AltStatus.PENDING
    discord_user_id: str = ""      # the alt account's own Discord id (from /users/@me)
    username: str = ""
    display_name: str = ""
    channel_ids: tuple[str, ...] = ()
    token_ciphertext: str = ""
    sync_state: SyncState = SyncState.CLEAN
    runtime_overrides: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def repo_slug(self) -> str:
        return f"{self.repo_owner}/{self.repo_name}"

    @property
    def label(self) -> str:
        return self.display_name or self.username or f"Alt {self.alt_index}"

    def with_(self, **changes: Any) -> "Alt":
        return replace(self, **changes)


@dataclass(frozen=True)
class RunState:
    customer_id: str
    alt_index: int
    mode: RunMode
    runtime_hours: int
    started_at: float
    last_dispatch_at: float
    renewals: int = 0
    payload: dict[str, Any] = field(default_factory=dict)
    run_id: Optional[int] = None
    status: str = "queued"         # queued | in_progress | completed | cancelled | failed
    conclusion: str = ""

    def with_(self, **changes: Any) -> "RunState":
        return replace(self, **changes)


@dataclass(frozen=True)
class Event:
    id: int
    discord_id: str
    event: str
    ts: float
    payload: dict[str, Any]

    @staticmethod
    def decode_payload(raw: str | None) -> dict[str, Any]:
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class Webhooks:
    """Per-customer forum webhooks (URLs are secrets)."""

    customer_id: str
    dashboard: str = ""
    logs: str = ""
    deals: str = ""
    dm: str = ""

    def as_secrets(self) -> dict[str, str]:
        return {
            "DASHBOARD_WEBHOOK_URL": self.dashboard,
            "LOG_WEBHOOK_URL": self.logs,
            "DEAL_WEBHOOK_URL": self.deals,
            "DM_WEBHOOK_URL": self.dm,
        }

    def complete(self) -> bool:
        return all((self.dashboard, self.logs, self.deals))


@dataclass(frozen=True)
class Actor:
    """Who is issuing a command (resolved by the guard)."""

    user_id: str
    tier: Tier
    customer: Optional[Customer] = None

    @property
    def is_admin(self) -> bool:
        return self.tier is Tier.ADMIN
