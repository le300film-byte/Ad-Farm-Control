"""Guard — the runtime gate every command passes through, plus the two-admin MultiSig."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..core.clock import Clock, SystemClock
from ..core.models import Actor, Customer, Tier
from . import policy
from .policy import ChannelKind, Decision
from .roles import resolve_actor, subscription_state

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChannelInfo:
    """Framework-neutral description of where a command was issued (built by discord/channels.py)."""

    channel_id: str = ""
    kind_hint: ChannelKind = ChannelKind.UNKNOWN   # PUBLIC/TICKET/ADMIN/DM/UNKNOWN or hub (owner set)
    hub_owner_id: str = ""                          # customer id when the channel is inside a customer forum
    name: str = ""


@dataclass
class GateResult:
    actor: Actor
    decision: Decision
    kind: ChannelKind


class Guard:
    def __init__(self, owner_ids: frozenset[str], customer_lookup: Callable[[str], Optional[Customer]], *, clock: Clock | None = None):
        self.owner_ids = frozenset(str(x) for x in owner_ids)
        self.customer_lookup = customer_lookup
        self.clock = clock or SystemClock()

    def actor_for(self, user_id: str) -> Actor:
        customer = self.customer_lookup(str(user_id))
        return resolve_actor(user_id, self.owner_ids, customer, self.clock.now())

    def classify(self, actor: Actor, channel: ChannelInfo) -> ChannelKind:
        if channel.hub_owner_id:
            return ChannelKind.OWN_HUB if channel.hub_owner_id == actor.user_id else ChannelKind.OTHER_HUB
        return channel.kind_hint

    def check(self, user_id: str, command: str, channel: ChannelInfo) -> GateResult:
        """Never raises: any internal error results in a denial (fail closed, L-14)."""
        try:
            actor = self.actor_for(user_id)
            kind = self.classify(actor, channel)
            decision = policy.decide(actor.tier, command, kind)
            if not decision.allowed and decision.reason == policy.DENY_NOT_CUSTOMER:
                # Give expired/inactive customers a more actionable message.
                state = subscription_state(actor.customer, self.clock.now())
                if state in ("expired", "inactive"):
                    decision = Decision.deny(policy.DENY_EXPIRED)
            return GateResult(actor=actor, decision=decision, kind=kind)
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("guard failure for user=%s command=%s: %s", user_id, command, exc)
            return GateResult(actor=Actor(str(user_id), Tier.PUBLIC), decision=Decision.deny(policy.DENY_FAIL_CLOSED), kind=ChannelKind.UNKNOWN)

    def is_admin(self, user_id: str) -> bool:
        return str(user_id) in self.owner_ids


@dataclass
class _Pending:
    action: str
    first_admin: str
    created_at: float
    confirmations: set[str] = field(default_factory=set)


class MultiSig:
    """Two distinct admins must confirm the same action within ``window`` seconds."""

    def __init__(self, *, window: int = 120, required: int = 2, clock: Clock | None = None):
        self.window = int(window)
        self.required = max(1, int(required))
        self.clock = clock or SystemClock()
        self._pending: dict[str, _Pending] = {}

    def confirm(self, action: str, admin_id: str) -> tuple[bool, int]:
        """Returns (approved, confirmations_so_far)."""
        now = self.clock.now()
        self._expire(now)
        entry = self._pending.get(action)
        if entry is None:
            entry = _Pending(action=action, first_admin=str(admin_id), created_at=now)
            self._pending[action] = entry
        entry.confirmations.add(str(admin_id))
        if len(entry.confirmations) >= self.required:
            del self._pending[action]
            return True, len(entry.confirmations)
        return False, len(entry.confirmations)

    def cancel(self, action: str) -> None:
        self._pending.pop(action, None)

    def pending(self, action: str) -> int:
        self._expire(self.clock.now())
        entry = self._pending.get(action)
        return len(entry.confirmations) if entry else 0

    def _expire(self, now: float) -> None:
        for key in [k for k, v in self._pending.items() if now - v.created_at > self.window]:
            del self._pending[key]
