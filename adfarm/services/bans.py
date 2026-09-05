"""BanService — reacts to ban markers in an alt's logs/heartbeats.

Flow: dedupe (1 h per alt) → mark banned (rename repo, drop token) → time credit → notify the
customer in #control with a re-setup hint → alert admins → prepare a fresh repo for /setup.
"""
from __future__ import annotations

import logging

from ..core.models import Alt, DAY
from ..core.rules import BAN_FULL_CREDIT_WINDOW_SEC
from ..discord.embeds import ban_notice_embed
from .container import Services

log = logging.getLogger(__name__)


class BanService:
    def __init__(self, s: Services, *, dedupe_seconds: int = 3600):
        self.s = s
        self.dedupe = int(dedupe_seconds)

    def credit_days_for(self, alt: Alt, now: float) -> float:
        """Full remaining term if banned within 48 h of first run, otherwise pro-rated by run age."""
        customer = self.s.repos.customers.get(alt.customer_id)
        if customer is None:
            return 0.0
        first_run = self.s.repos.events.recent(limit=1, discord_id=alt.customer_id, event="audit:run.start")
        run = self.s.repos.runs.get(alt.customer_id, alt.alt_index)
        started = run.started_at if run else (first_run[0].ts if first_run else now)
        age = max(0.0, now - started)
        remaining_days = customer.days_remaining(now)
        per_alt = remaining_days / max(1, customer.alt_count)
        if age <= BAN_FULL_CREDIT_WINDOW_SEC:
            return round(per_alt, 2)
        term_days = max(1.0, (customer.expiry_date - customer.start_date) / DAY)
        used_fraction = min(1.0, age / (term_days * DAY))
        return round(per_alt * (1 - used_fraction), 2)

    async def handle(self, alt: Alt, *, reason: str) -> bool:
        now = self.s.now()
        last = self.s.repos.events.last(alt.customer_id, "alt_banned")
        if last and last.payload.get("alt") == alt.alt_index and now - last.ts < self.dedupe:
            return False
        credit = self.credit_days_for(alt, now)
        banned = await self.s.alts.mark_banned(alt, reason=reason)
        if credit > 0 and self.s.customers:
            await self.s.customers.credit_days(alt.customer_id, credit, reason=f"ban alt {alt.alt_index}")
        customer = self.s.repos.customers.get(alt.customer_id)
        if customer and customer.thread("control"):
            await self.s.discord.send(customer.thread("control"), f"<@{alt.customer_id}>", embed=ban_notice_embed(banned, credit))
        if customer and customer.thread("farm-logs"):
            await self.s.discord.send(customer.thread("farm-logs"), f"🚫 **BAN DETECTED** on alt {alt.alt_index} — repo renamed, token removed, credit {credit:.1f} d applied.")
        if self.s.alerts:
            await self.s.alerts.admin(f"ban:{alt.customer_id}:{alt.alt_index}", f"Ban detected: customer {alt.customer_id} alt {alt.alt_index} ({alt.repo_slug}). Reason: {reason[:160]}", force=True)
        try:
            await self.s.alts.prepare_replacement(banned)
        except Exception as exc:
            log.warning("replacement provisioning failed: %s", exc)
        return True
