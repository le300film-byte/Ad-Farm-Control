"""ForumProvisioner — one private forum per customer with the standard threads **and webhooks**.

Fixes L-6: the legacy created the threads but never the webhooks, so the sender's
``DASHBOARD_WEBHOOK_URL`` etc. pointed at the operator's channels (or nowhere) and customer
threads stayed empty. Here every thread that receives sender traffic gets a webhook whose URL is
stored (sealed) in ``customer_webhooks`` and pushed to every alt repo of that customer.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..core.models import Customer, Webhooks
from .ports import DiscordPort, ForumResult, ForumSpec

THREADS = (
    ("control", "control", "📌 **#control** — Run, stop, pause and manage your alts from here."),
    ("dashboard", "dashboard", "📌 **#dashboard** — Live status dashboard for your ad farm."),
    ("farm-logs", "farm-logs", "📌 **#farm-logs** — Detailed activity logs from all your alts."),
    ("deals", "deals", "📌 **#deals** — Deal alerts and high-value match notifications."),
)
VIP_THREADS = (("dm-inbox", "dm-inbox", "📌 **#dm-inbox** — Incoming DMs forwarded from your alts (VIP only)."),)
WEBHOOK_THREADS = ("dashboard", "farm-logs", "deals", "dm-inbox")


@dataclass(frozen=True)
class ForumOutcome:
    forum_id: str
    thread_ids: dict[str, str]
    webhooks: Webhooks
    created: bool


class ForumProvisioner:
    def __init__(self, discord: DiscordPort, *, category_id: str, admin_role_id: str = ""):
        self.discord = discord
        self.category_id = category_id
        self.admin_role_id = admin_role_id

    @staticmethod
    def thread_specs(vip: bool) -> tuple[tuple[str, str, str], ...]:
        return THREADS + (VIP_THREADS if vip else ())

    @staticmethod
    def forum_name(customer: Customer) -> str:
        base = (customer.username or customer.discord_id).strip().lower().replace(" ", "-")[:80]
        return f"{base}-hub"

    async def ensure(self, customer: Customer) -> ForumOutcome:
        """Create the forum if missing; otherwise create only missing threads/webhooks (idempotent)."""
        spec = ForumSpec(
            name=self.forum_name(customer), category_id=self.category_id, customer_user_id=customer.discord_id,
            admin_role_id=self.admin_role_id, threads=self.thread_specs(customer.vip),
        )
        if customer.forum_id and await self.discord.get_channel(customer.forum_id) is not None:
            result = await self._complete_existing(customer, spec)
        else:
            result = await self.discord.create_customer_forum(spec)
        hooks = Webhooks(
            customer_id=customer.discord_id, dashboard=result.webhooks.get("dashboard", ""), logs=result.webhooks.get("farm-logs", ""),
            deals=result.webhooks.get("deals", ""), dm=result.webhooks.get("dm-inbox", ""),
        )
        return ForumOutcome(forum_id=result.forum_id, thread_ids=dict(result.thread_ids), webhooks=hooks, created=result.created)

    async def _complete_existing(self, customer: Customer, spec: ForumSpec) -> ForumResult:
        thread_ids = {k: v for k, v in customer.thread_ids.items() if v}
        missing = [t for t in spec.threads if t[0] not in thread_ids]
        if missing:
            partial = await self.discord.create_customer_forum(ForumSpec(
                name=spec.name, category_id=spec.category_id, customer_user_id=spec.customer_user_id,
                admin_role_id=spec.admin_role_id, threads=tuple(missing),
            ))
            # Adapter contract: when forum name matches an existing forum it reuses it.
            thread_ids.update(partial.thread_ids)
        webhooks = await self.discord.ensure_forum_webhooks(customer.forum_id, thread_ids)
        return ForumResult(forum_id=customer.forum_id, thread_ids=thread_ids, webhooks=webhooks, created=False)

    async def lock(self, customer: Customer) -> bool:
        if not customer.forum_id:
            return False
        return await self.discord.set_forum_readonly(customer.forum_id, customer.discord_id, True)

    async def unlock(self, customer: Customer) -> bool:
        if not customer.forum_id:
            return False
        return await self.discord.restore_forum_access(customer.forum_id, customer.discord_id)

    async def delete(self, customer: Customer) -> bool:
        if not customer.forum_id:
            return False
        return await self.discord.delete_channel(customer.forum_id)
