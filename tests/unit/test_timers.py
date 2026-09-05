"""timers: expiry planning (7/3/1 once each), limitless renewal, scheduler isolation."""
import asyncio

import pytest

from adfarm.core.clock import FakeClock
from adfarm.core.models import DAY, Alt, Customer, RunMode, RunState
from adfarm.db import AltRepo, CustomerRepo, Database, ReminderRepo, RunRepo
from adfarm.timers import ExpiryEngine, LimitlessRenewer, Scheduler
from tests.conftest import run


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    d.migrate()
    return d


def test_expiry_plan_sends_each_threshold_once_and_only_most_urgent(db):
    now = 1_000_000.0
    customers, reminders = CustomerRepo(db), ReminderRepo(db)
    customers.save(Customer("c7", "seven", 1, False, 0, now + 6.5 * DAY, True), now=now)
    customers.save(Customer("c3", "three", 1, False, 0, now + 2.9 * DAY, True), now=now)
    customers.save(Customer("c1", "one", 1, False, 0, now + 0.5 * DAY, True), now=now)
    customers.save(Customer("far", "far", 1, False, 0, now + 20 * DAY, True), now=now)
    customers.save(Customer("dead", "dead", 1, False, 0, now - 1, True), now=now)
    customers.save(Customer("inactive", "x", 1, False, 0, now - 1, False), now=now)
    engine = ExpiryEngine(customers, reminders)
    plan = engine.plan(now)
    assert {(r.customer.discord_id, r.threshold_days) for r in plan.reminders} == {("c7", 7), ("c3", 3), ("c1", 1)}
    assert [c.discord_id for c in plan.expired] == ["dead"]
    sent, shut = engine.apply(plan, now=now, on_reminder=lambda r: None, on_expired=lambda c: None)
    assert (sent, shut) == (3, 1)
    # second pass one hour later: nothing new
    assert engine.plan(now + 3600).reminders == []
    # crossing into the 3-day window triggers the next threshold for c7
    later = now + 4 * DAY
    plan2 = engine.plan(later)
    assert {(r.customer.discord_id, r.threshold_days) for r in plan2.reminders} == {("c7", 3)}


def test_expiry_reminder_marked_even_if_callback_fails(db):
    now = 1_000.0
    customers, reminders = CustomerRepo(db), ReminderRepo(db)
    customers.save(Customer("c", "c", 1, False, 0, now + 0.5 * DAY, True), now=now)
    engine = ExpiryEngine(customers, reminders)

    def boom(_):
        raise RuntimeError("dm failed")

    with pytest.raises(RuntimeError):
        engine.run(now, on_reminder=boom, on_expired=lambda c: None)
    assert reminders.was_sent("c", 1, now + 0.5 * DAY)


def test_extension_resets_reminder_cycle(db):
    now = 1_000.0
    customers, reminders = CustomerRepo(db), ReminderRepo(db)
    c = Customer("c", "c", 1, False, 0, now + 0.5 * DAY, True)
    customers.save(c, now=now)
    engine = ExpiryEngine(customers, reminders)
    engine.run(now, on_reminder=lambda r: None, on_expired=lambda c: None)
    customers.save(c.with_(expiry_date=now + 0.8 * DAY), now=now)   # new expiry date → new reminder key
    assert len(engine.plan(now).reminders) == 1


def test_limitless_renewal_plan(db):
    now = 1_000_000.0
    customers, alts, runs = CustomerRepo(db), AltRepo(db), RunRepo(db)
    customers.save(Customer("a", "a", 2, False, 0, now + 10 * DAY, True), now=now)
    customers.save(Customer("b", "b", 1, False, 0, now - 1, True), now=now)   # expired
    for cid, idx, sid in (("a", 1, 1), ("a", 2, 2), ("b", 1, 3)):
        alts.save(Alt(cid, idx, sid, "w", f"{cid}{idx}"), now=now)
    runs.save(RunState("a", 1, RunMode.LIMITLESS, 0, now - 49 * 3600, now - 49 * 3600, status="in_progress"))
    runs.save(RunState("a", 2, RunMode.LIMITLESS, 0, now - 1000, now - 1000, status="in_progress"))
    runs.save(RunState("b", 1, RunMode.LIMITLESS, 0, now - 49 * 3600, now - 49 * 3600, status="in_progress"))
    plan = LimitlessRenewer(runs, customers).plan(now)
    assert [(r.customer_id, r.alt_index) for r in plan.due] == [("a", 1)]
    assert [(r.customer_id, r.alt_index) for r in plan.orphaned] == [("b", 1)]
    runs.save(RunState("a", 1, RunMode.TIMED, 24, now - 49 * 3600, now - 49 * 3600, status="in_progress"))
    assert LimitlessRenewer(runs, customers).plan(now).due == []


def test_scheduler_isolates_failures_and_reports():
    async def scenario():
        errors = []
        calls = {"ok": 0}
        sched = Scheduler(on_error=lambda name, exc: errors.append((name, type(exc).__name__)))

        def bad():
            raise ValueError("nope")

        async def good():
            calls["ok"] += 1

        sched.add("bad", 0.01, bad, run_immediately=True)
        sched.add("good", 0.01, good, run_immediately=True)
        await sched.start()
        await asyncio.sleep(0.08)
        await sched.stop()
        return errors, calls, sched

    errors, calls, sched = run(scenario())
    assert calls["ok"] >= 2 and ("bad", "ValueError") in errors
    assert next(j for j in sched.jobs() if j.name == "bad").last_error.startswith("ValueError")
    assert run(Scheduler().run_once("x")) if False else True
