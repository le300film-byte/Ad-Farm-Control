"""LimitlessRenewer — re-dispatch limitless runs every 48 h (GitHub-hosted jobs are capped)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..core.models import RunMode, RunState
from ..core.rules import LIMITLESS_RENEW_AFTER_SEC
from ..db.repositories import CustomerRepo, RunRepo


@dataclass(frozen=True)
class RenewalPlan:
    due: list[RunState]
    orphaned: list[RunState]   # customer expired/inactive → the run should be stopped instead


class LimitlessRenewer:
    def __init__(self, runs: RunRepo, customers: CustomerRepo, *, renew_after: int = LIMITLESS_RENEW_AFTER_SEC):
        self.runs = runs
        self.customers = customers
        self.renew_after = int(renew_after)

    def plan(self, now: float) -> RenewalPlan:
        due: list[RunState] = []
        orphaned: list[RunState] = []
        for run in self.runs.all():
            if run.mode is not RunMode.LIMITLESS or run.status in ("cancelled", "stopped", "failed"):
                continue
            customer = self.customers.get(run.customer_id)
            if customer is None or not customer.is_active(now):
                orphaned.append(run)
                continue
            if now - run.last_dispatch_at >= self.renew_after:
                due.append(run)
        return RenewalPlan(due=due, orphaned=orphaned)

    def apply(self, plan: RenewalPlan, *, on_renew: Callable[[RunState], None], on_orphan: Callable[[RunState], None]) -> tuple[int, int]:
        renewed = 0
        for run in plan.due:
            on_renew(run)
            renewed += 1
        for run in plan.orphaned:
            on_orphan(run)
        return renewed, len(plan.orphaned)

    def run(self, now: float, *, on_renew: Callable[[RunState], None], on_orphan: Callable[[RunState], None]) -> tuple[int, int]:
        return self.apply(self.plan(now), on_renew=on_renew, on_orphan=on_orphan)
