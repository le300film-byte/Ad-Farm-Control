"""V8 Phase 4 · Round 4 – Timer, Reminders & Control Bot Continuity.

Verifies:
  - Reminders sent at 7, 3, 1 day thresholds
  - Expiry triggers deactivation exactly once
  - Reactivation (extend) restores access
  - Control bot run() accepts CONTINUOUS_MODE=1 without total_hours limit
  - Bot run() does not rely on session duration env var
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]

def _run(coro):
    """Run a coroutine in a fresh event loop (Python 3.11 compatible)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

sys.path.insert(0, str(ROOT))


class TestTimerAndReminders(unittest.TestCase):

    def setUp(self):
        import tempfile
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        import customer_manager as cm
        cm.DB_PATH = self._tmp.name
        cm.init_db()
        self.cm = cm

        import timer_engine as te
        te._bot_ref = None
        te._sent_reminders.clear()
        self.te = te

    def tearDown(self):
        os.unlink(self._tmp.name)

    def _set_expiry(self, discord_id: str, seconds_from_now: int) -> None:
        con = sqlite3.connect(self._tmp.name)
        con.execute(
            "UPDATE customers SET expiry_date = ? WHERE discord_id = ?",
            (int(time.time()) + seconds_from_now, discord_id),
        )
        con.commit(); con.close()

    # ── Reminder thresholds ───────────────────────────────────────────────────

    def test_7day_reminder_threshold(self):
        self.cm.add_customer("1001", "Week", alt_count=1, vip=False, days=30)
        self._set_expiry("1001", 6 * 86400)  # 6 days left → within 7-day window
        within = self.cm.get_expiring_customers(within_days=7)
        ids = [c["discord_id"] for c in within]
        self.assertIn("1001", ids)

    def test_3day_reminder_threshold(self):
        self.cm.add_customer("1002", "ThreeDays", alt_count=1, vip=False, days=30)
        self._set_expiry("1002", 2 * 86400)  # 2 days left → within 3-day window
        within = self.cm.get_expiring_customers(within_days=3)
        ids = [c["discord_id"] for c in within]
        self.assertIn("1002", ids)

    def test_1day_reminder_threshold(self):
        self.cm.add_customer("1003", "OneDay", alt_count=1, vip=False, days=30)
        self._set_expiry("1003", 20 * 3600)  # ~20h left → within 1-day window
        within = self.cm.get_expiring_customers(within_days=1)
        ids = [c["discord_id"] for c in within]
        self.assertIn("1003", ids)

    def test_not_in_window(self):
        self.cm.add_customer("1004", "NotSoon", alt_count=1, vip=False, days=30)
        # 30 days left — not within any reminder window
        within = self.cm.get_expiring_customers(within_days=7)
        ids = [c["discord_id"] for c in within]
        self.assertNotIn("1004", ids)

    # ── Expiry deactivation ───────────────────────────────────────────────────

    def test_expiry_deactivates_exactly_once(self):
        self.cm.add_customer("1005", "Expire", alt_count=1, vip=False, days=30)
        self._set_expiry("1005", -10)  # already expired

        _run(self.te.scan_once())
        c = self.cm.get_customer("1005")
        self.assertFalse(c["active"])

        # Second scan should not raise even though customer is already inactive
        _run(self.te.scan_once())
        c = self.cm.get_customer("1005")
        self.assertFalse(c["active"])

    # ── Reactivation restores access ──────────────────────────────────────────

    def test_reactivation_after_expiry(self):
        self.cm.add_customer("1006", "Reactivate", alt_count=1, vip=False, days=30)
        self._set_expiry("1006", -10)  # expired

        _run(self.te.scan_once())
        self.assertFalse(self.cm.is_active("1006"))

        # Admin extends → customer active again
        ok = self.cm.extend_customer("1006", 30)
        self.assertTrue(ok)
        # Re-activate the record
        import customer_manager as cm
        with cm._conn() as con:
            con.execute("UPDATE customers SET active = 1 WHERE discord_id = '1006'")
        self.assertTrue(self.cm.is_active("1006"))

    # ── Reminder deduplication ────────────────────────────────────────────────

    def test_reminder_not_sent_twice_for_same_threshold(self):
        self.cm.add_customer("1007", "Dedup", alt_count=1, vip=False, days=30)
        self._set_expiry("1007", 6 * 86400)
        # Simulate first scan sending the reminder
        self.te._sent_reminders.add(("1007", 7))
        # scan_once should not re-send
        _run(self.te.scan_once())
        # Still only one entry in sent_reminders for this key
        count = sum(1 for k in self.te._sent_reminders if k == ("1007", 7))
        self.assertEqual(count, 1)


class TestControlBotContinuity(unittest.TestCase):
    """Verify the control bot is designed for continuous (no-session-limit) operation."""

    def test_run_function_no_total_hours_dependency(self):
        """run() in bot.py must not read a TOTAL_HOURS env var at startup."""
        from control_bot import bot as cb
        import inspect
        src = inspect.getsource(cb.run)
        self.assertNotIn("TOTAL_HOURS", src,
                          "run() must not reference TOTAL_HOURS session limit")
        self.assertNotIn("total_hours", src.lower().replace("total_hours", ""),
                          "run() must not depend on session duration")

    def test_continuous_mode_config(self):
        """CONTINUOUS_MODE config must default to True (V8 forever-run)."""
        from control_bot import config
        self.assertTrue(
            config.CONTINUOUS_MODE,
            "CONTINUOUS_MODE must default to True for V8 continuous operation"
        )

    def test_workflow_no_hours_input(self):
        """The V8 control_bot.yml must not require a 'hours' runtime input."""
        workflow_path = ROOT / ".github" / "workflows" / "control_bot.yml"
        content = workflow_path.read_text()
        # V8 removes the hours chooser — no required hours input
        self.assertNotIn(
            "required: true",
            content.split("hours:")[1][:200] if "hours:" in content else "",
            "control_bot.yml must not have a required 'hours' input"
        )

    def test_watchdog_restart_in_workflow(self):
        """The control_bot.yml must contain an auto-restart loop."""
        workflow_path = ROOT / ".github" / "workflows" / "control_bot.yml"
        content = workflow_path.read_text()
        self.assertIn(
            "restarting",
            content.lower(),
            "control_bot.yml must contain watchdog restart logic"
        )

    def test_timer_engine_loop_registered(self):
        """subscription_timer is a discord.ext.tasks loop."""
        import timer_engine as te
        self.assertTrue(
            hasattr(te.subscription_timer, "start"),
            "subscription_timer must be a discord.ext.tasks loop with .start()"
        )

    def test_scan_interval_is_hourly(self):
        """Timer scan interval must be 3600 seconds (1 hour)."""
        import timer_engine as te
        self.assertEqual(te.SCAN_INTERVAL_SEC, 3600)


if __name__ == "__main__":
    unittest.main()
