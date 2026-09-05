"""Tier resolution: who is the caller?"""
from __future__ import annotations

from typing import Optional

from ..core.models import Actor, Customer, Tier


def resolve_tier(user_id: str, owner_ids: frozenset[str], customer: Optional[Customer], now: float) -> Tier:
    if not owner_ids:
        # Fail closed: without an owner list nobody is an admin — but customers still work.
        pass
    if str(user_id) in owner_ids:
        return Tier.ADMIN
    if customer is None:
        return Tier.PUBLIC
    return customer.tier(now)


def resolve_actor(user_id: str, owner_ids: frozenset[str], customer: Optional[Customer], now: float) -> Actor:
    return Actor(user_id=str(user_id), tier=resolve_tier(user_id, owner_ids, customer, now), customer=customer)


def subscription_state(customer: Optional[Customer], now: float) -> str:
    """'none' | 'expired' | 'inactive' | 'active' — used to pick the right denial message."""
    if customer is None:
        return "none"
    if not customer.active:
        return "inactive"
    if customer.expiry_date <= now:
        return "expired"
    return "active"
