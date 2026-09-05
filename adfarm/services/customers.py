"""CustomerService — subscription lifecycle: activate, extend, deactivate, reactivate, VIP."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from ..core.errors import ConflictError, NotFound
from ..core.models import DAY, Customer, Webhooks
from ..core.rules import DEFAULT_SUBSCRIPTION_DAYS, validate_alt_count, validate_days
from ..discord.forums import ForumProvisioner
from .container import Services

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ActivationResult:
    customer: Customer
    forum_created: bool
    webhooks_complete: bool
    reactivated: bool


class CustomerService:
    def __init__(self, s: Services, forums: ForumProvisioner):
        self.s = s
        self.forums = forums

    # ── queries ─────────────────────────────────────────────────────────────
    def get(self, discord_id: str) -> Optional[Customer]:
        return self.s.repos.customers.get(discord_id)

    def require(self, discord_id: str) -> Customer:
        customer = self.get(discord_id)
        if customer is None:
            raise NotFound("❓ No customer with that Discord ID.")
        return customer

    def list(self, *, active_only: bool = False) -> list[Customer]:
        return self.s.repos.customers.all(active_only=active_only)

    def by_forum(self, forum_id: str) -> Optional[Customer]:
        return self.s.repos.customers.by_forum(forum_id)

    def by_thread(self, thread_id: str) -> Optional[tuple[Customer, str]]:
        """Locate a customer + thread role from a thread id (used by the ingestor)."""
        for customer in self.s.repos.customers.all():
            for role, tid in customer.thread_ids.items():
                if tid and tid == str(thread_id):
                    return customer, role
        return None

    # ── activation ──────────────────────────────────────────────────────────
    async def activate(self, *, discord_id: str, username: str, alt_count: int = 1, days: int = DEFAULT_SUBSCRIPTION_DAYS,
                       vip: bool = False, actor_id: str = "") -> ActivationResult:
        """Idempotent: re-activating an existing customer **extends** instead of resetting (L-18)."""
        now = self.s.now()
        alt_count = validate_alt_count(alt_count)
        days = validate_days(days)
        existing = self.get(discord_id)
        reactivated = False
        if existing is None:
            customer = Customer(discord_id=str(discord_id), username=username, alt_count=alt_count, vip=vip, start_date=now,
                                expiry_date=now + days * DAY, active=True)
        else:
            base = max(existing.expiry_date, now)
            customer = existing.with_(username=username or existing.username, alt_count=max(alt_count, existing.alt_count), vip=vip or existing.vip,
                                      expiry_date=base + days * DAY, active=True)
            reactivated = not existing.is_active(now)
        customer = self.s.repos.customers.save(customer, now=now)

        outcome = await self.forums.ensure(customer)
        customer = customer.with_(forum_id=outcome.forum_id, thread_ids=outcome.thread_ids)
        customer = self.s.repos.customers.save(customer, now=now)
        self._store_webhooks(outcome.webhooks)
        if reactivated:
            await self.forums.unlock(customer)
            self.s.repos.reminders.clear(customer.discord_id)
        await self.s.discord.grant_role(customer.discord_id, "VIP" if customer.vip else "Customer")
        if self.s.alerts:
            await self.s.alerts.audit(actor_id or "system", "customer.activate", customer_id=customer.discord_id, days=days, alts=alt_count, vip=vip, reactivated=reactivated)
        if outcome.thread_ids.get("control"):
            await self.s.discord.send(outcome.thread_ids["control"],
                                      f"👋 <@{customer.discord_id}> your hub is ready. Start with `/setup` to register your alt, then `/run`. "
                                      f"Plan: {alt_count} alt(s), expires <t:{int(customer.expiry_date)}:D>.")
        # push webhooks to any alt repos that already exist (reactivation)
        if self.s.alts:
            await self.s.alts.resync_customer(customer.discord_id)
        return ActivationResult(customer=customer, forum_created=outcome.created, webhooks_complete=outcome.webhooks.complete(), reactivated=reactivated)

    async def repair_hub(self, discord_id: str, *, actor_id: str = "") -> tuple[Customer, int]:
        """Re-create missing threads/webhooks (idempotent) and re-project them into every alt repo."""
        customer = self.require(discord_id)
        outcome = await self.forums.ensure(customer)
        customer = self.s.repos.customers.save(customer.with_(forum_id=outcome.forum_id, thread_ids=outcome.thread_ids), now=self.s.now())
        self._store_webhooks(outcome.webhooks)
        synced = await self.s.alts.resync_customer(discord_id) if self.s.alts else 0
        if self.s.alerts:
            await self.s.alerts.audit(actor_id or "system", "customer.repair_hub", customer_id=discord_id, forum_created=outcome.created, synced=synced)
        return customer, synced

    def _store_webhooks(self, hooks: Webhooks) -> None:
        vault = self.s.vault
        if vault.available:
            hooks = Webhooks(hooks.customer_id, *(vault.seal(v) if v else "" for v in (hooks.dashboard, hooks.logs, hooks.deals, hooks.dm)))
        self.s.repos.webhooks.save(hooks, now=self.s.now())

    def webhooks(self, customer_id: str) -> Optional[Webhooks]:
        hooks = self.s.repos.webhooks.get(customer_id)
        if hooks is None:
            return None
        vault = self.s.vault

        def open_(v: str) -> str:
            if not v:
                return ""
            if vault.is_sealed(v):
                return vault.try_open(v) or ""
            return v

        return Webhooks(hooks.customer_id, open_(hooks.dashboard), open_(hooks.logs), open_(hooks.deals), open_(hooks.dm))

    # ── plan changes ────────────────────────────────────────────────────────
    async def extend(self, discord_id: str, days: int, *, actor_id: str = "") -> Customer:
        days = validate_days(days)
        customer = self.require(discord_id)
        now = self.s.now()
        customer = customer.with_(expiry_date=max(customer.expiry_date, now) + days * DAY, active=True)
        customer = self.s.repos.customers.save(customer, now=now)
        self.s.repos.reminders.clear(customer.discord_id)
        if self.s.alerts:
            await self.s.alerts.audit(actor_id or "system", "customer.extend", customer_id=discord_id, days=days)
        return customer

    async def credit_days(self, discord_id: str, days: float, *, reason: str) -> Customer:
        customer = self.require(discord_id)
        now = self.s.now()
        customer = self.s.repos.customers.save(customer.with_(expiry_date=max(customer.expiry_date, now) + days * DAY), now=now)
        if self.s.alerts:
            self.s.alerts.event(discord_id, "credit", days=round(days, 2), reason=reason)
        return customer

    async def set_vip(self, discord_id: str, vip: bool, *, actor_id: str = "") -> Customer:
        customer = self.require(discord_id).with_(vip=vip)
        customer = self.s.repos.customers.save(customer, now=self.s.now())
        outcome = await self.forums.ensure(customer)   # creates #dm-inbox on upgrade
        customer = self.s.repos.customers.save(customer.with_(thread_ids=outcome.thread_ids), now=self.s.now())
        self._store_webhooks(outcome.webhooks)
        await self.s.discord.grant_role(discord_id, "VIP" if vip else "Customer")
        if not vip:
            await self.s.discord.revoke_role(discord_id, "VIP")
        if self.s.alts:
            await self.s.alts.resync_customer(discord_id)
        if self.s.alerts:
            await self.s.alerts.audit(actor_id or "system", "customer.vip", customer_id=discord_id, vip=vip)
        return customer

    async def set_alt_count(self, discord_id: str, alt_count: int, *, actor_id: str = "") -> Customer:
        alt_count = validate_alt_count(alt_count)
        customer = self.require(discord_id)
        registered = len(self.s.repos.alts.for_customer(discord_id))
        if alt_count < registered:
            raise ConflictError(f"❌ Customer has {registered} registered alt(s); remove alts before lowering the plan.")
        customer = self.s.repos.customers.save(customer.with_(alt_count=alt_count), now=self.s.now())
        if self.s.alerts:
            await self.s.alerts.audit(actor_id or "system", "customer.alt_count", customer_id=discord_id, alt_count=alt_count)
        return customer

    async def set_autoreply(self, discord_id: str, text: str) -> Customer:
        customer = self.require(discord_id).with_(autoreply_text=text)
        return self.s.repos.customers.save(customer, now=self.s.now())

    # ── deactivation ────────────────────────────────────────────────────────
    async def deactivate(self, discord_id: str, *, reason: str, actor_id: str = "", notify: bool = True) -> Customer:
        customer = self.require(discord_id)
        now = self.s.now()
        if self.s.runs:
            await self.s.runs.stop_all_for(customer.discord_id, reason=reason)
        customer = self.s.repos.customers.save(customer.with_(active=False), now=now)
        await self.forums.lock(customer)
        await self.s.discord.revoke_role(discord_id, "Customer")
        await self.s.discord.revoke_role(discord_id, "VIP")
        if notify:
            from ..discord.embeds import expiry_notice_embed

            await self.s.discord.dm(discord_id, "", embed=expiry_notice_embed(customer.expiry_date))
            if customer.thread("control"):
                await self.s.discord.send(customer.thread("control"), f"⛔ <@{discord_id}> subscription {reason}. Alts stopped; hub is read-only until renewal.")
        if self.s.alerts:
            await self.s.alerts.audit(actor_id or "system", "customer.deactivate", customer_id=discord_id, reason=reason)
        return customer

    async def purge(self, discord_id: str, *, actor_id: str, delete_forum: bool = False) -> bool:
        customer = self.require(discord_id)
        if self.s.runs:
            await self.s.runs.stop_all_for(discord_id, reason="purge")
        if self.s.alts:
            for alt in self.s.repos.alts.for_customer(discord_id, include_removed=True):
                await self.s.alts.remove(discord_id, alt.alt_index, actor_id=actor_id, soft=True)
        if delete_forum:
            await self.forums.delete(customer)
        deleted = self.s.repos.customers.delete(discord_id)
        if self.s.alerts:
            await self.s.alerts.audit(actor_id, "customer.purge", customer_id=discord_id, delete_forum=delete_forum)
        return deleted
