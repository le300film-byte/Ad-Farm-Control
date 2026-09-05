"""ExpiryEngine — pure planning of reminders and expiry shutdowns.

``plan(now)`` returns *what should happen*; ``apply`` performs it through injected callbacks.
Keeping the plan pure means the 7/3/1-day logic is unit-testable with a FakeClock and no mocks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable

from ..core.models import DAY, Customer
from ..core.rules import REMINDER_THRESHOLDS_DAYS
from ..db.repositories import CustomerRepo, ReminderRepo


@dataclass(frozen=True)
class Reminder:
    customer: Customer
    threshold_days: int
    days_left: float


@dataclass
class ExpiryPlan:
    reminders: list[Reminder] = field(default_factory=list)
    expired: list[Customer] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.reminders and not self.expired


class ExpiryEngine:
    def __init__(self, customers: CustomerRepo, reminders: ReminderRepo, *, thresholds: Iterable[int] = REMINDER_THRESHOLDS_DAYS):
        self.customers = customers
        self.reminders = reminders
        self.thresholds = tuple(sorted(set(int(t) for t in thresholds), reverse=True))

    def plan(self, now: float) -> ExpiryPlan:
        plan = ExpiryPlan()
        horizon = now + max(self.thresholds) * DAY
        for customer in self.customers.expiring_between(now, horizon):
            days_left = (customer.expiry_date - now) / DAY
            # Only the *most urgent* threshold that applies and was not yet sent for this expiry date.
            for threshold in sorted(self.thresholds):
                if days_left <= threshold:
                    if not self.reminders.was_sent(customer.discord_id, threshold, customer.expiry_date):
                        plan.reminders.append(Reminder(customer, threshold, days_left))
                    break
        plan.expired.extend(self.customers.expired(now))
        return plan

    def apply(self, plan: ExpiryPlan, *, now: float, on_reminder: Callable[[Reminder], None], on_expired: Callable[[Customer], None]) -> tuple[int, int]:
        sent = 0
        for reminder in plan.reminders:
            try:
                on_reminder(reminder)
            finally:
                # Mark even on failure: a broken DM must not spam the customer every hour.
                self.reminders.mark(reminder.customer.discord_id, reminder.threshold_days, reminder.customer.expiry_date, now=now)
            sent += 1
        shut = 0
        for customer in plan.expired:
            on_expired(customer)
            shut += 1
        return sent, shut

    def run(self, now: float, *, on_reminder: Callable[[Reminder], None], on_expired: Callable[[Customer], None]) -> tuple[int, int]:
        return self.apply(self.plan(now), now=now, on_reminder=on_reminder, on_expired=on_expired)
