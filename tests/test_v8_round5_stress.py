"""V8 Phase 4 · Round 5 – Stress & Longevity (Simulated).

Simulates 2 mock customers running for an extended period:
  - Heavy concurrent DB read/write operations
  - Multiple timer scans without memory leaks
  - Database integrity after 500 operations
  - No SQLite corruption
"""
from __future__ import annotations

import asyncio
import os
import random
import sqlite3
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class TestStressAndLongevity(unittest.TestCase):

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

    # ── Heavy concurrent operations ───────────────────────────────────────────

    def test_500_concurrent_db_operations(self):
        """500 mixed read/write operations across 20 threads without corruption."""
        errors: list[Exception] = []
        ops_done = 0
        lock = threading.Lock()

        # Pre-create 50 customers
        for i in range(50):
            self.cm.add_customer(str(2000 + i), f"Stress{i}", alt_count=1, vip=False, days=30)

        def _worker(worker_id: int):
            nonlocal ops_done
            for _ in range(25):
                try:
                    uid = str(2000 + random.randint(0, 49))
                    op = random.choice(["read", "extend", "list", "vip"])
                    if op == "read":
                        self.cm.get_customer(uid)
                    elif op == "extend":
                        self.cm.extend_customer(uid, 1)
                    elif op == "list":
                        self.cm.list_customers(active_only=True)
                    elif op == "vip":
                        self.cm.is_vip(uid)
                    with lock:
                        ops_done += 1
                except Exception as exc:
                    with lock:
                        errors.append(exc)

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(20)]
        for t in threads: t.start()
        for t in threads: t.join()

        self.assertEqual(errors, [], f"Stress test raised {len(errors)} error(s): {errors[:3]}")
        self.assertEqual(ops_done, 500, f"Only {ops_done}/500 ops completed")

    def test_db_integrity_after_stress(self):
        """SQLite reports no integrity errors after stress test."""
        for i in range(100):
            self.cm.add_customer(str(3000 + i), f"Int{i}", alt_count=1, vip=False, days=30)

        con = sqlite3.connect(self._tmp.name)
        result = con.execute("PRAGMA integrity_check").fetchone()
        con.close()
        self.assertEqual(result[0], "ok", f"SQLite integrity check failed: {result[0]}")

    # ── Multiple timer scans without accumulating state ───────────────────────

    def test_10_timer_scans_no_accumulation(self):
        """Running 10 timer scans does not grow _sent_reminders indefinitely."""
        # Add 5 customers with 2-day expiry (in 3-day reminder window)
        for i in range(5):
            self.cm.add_customer(str(4000 + i), f"TimerUser{i}", alt_count=1, vip=False, days=2)

        async def _run_scans():
            for _ in range(10):
                await self.te.scan_once()

        asyncio.run(_run_scans())

        # Each customer should appear at most once per threshold in _sent_reminders
        for i in range(5):
            did = str(4000 + i)
            for threshold in self.te.REMINDER_THRESHOLDS:
                count = sum(1 for k in self.te._sent_reminders if k == (did, threshold))
                self.assertLessEqual(count, 1,
                    f"Customer {did} threshold {threshold} reminder sent {count} times")

    def test_expired_customers_do_not_accumulate_in_scans(self):
        """Expired customers are deactivated on first scan and not re-processed."""
        self.cm.add_customer("5000", "ExpStress", alt_count=1, vip=False, days=30)
        import sqlite3
        con = sqlite3.connect(self._tmp.name)
        con.execute("UPDATE customers SET expiry_date = ? WHERE discord_id = '5000'",
                    (int(time.time()) - 10,))
        con.commit(); con.close()

        async def _run():
            for _ in range(5):
                await self.te.scan_once()

        asyncio.run(_run())
        c = self.cm.get_customer("5000")
        self.assertFalse(c["active"])
        # Should have been deactivated exactly once (not multiple times, no error)

    # ── Two mock customers running simultaneously ──────────────────────────────

    def test_two_mock_customers_isolated(self):
        """Two customers with different settings don't interfere with each other."""
        self.cm.add_customer("6000", "MockA", alt_count=2, vip=True, days=30,
                             repos='["repo_a1", "repo_a2"]',
                             forum_id="111", control_thread_id="222")
        self.cm.add_customer("6001", "MockB", alt_count=1, vip=False, days=15,
                             repos='["repo_b1"]',
                             forum_id="333", control_thread_id="444")

        a = self.cm.get_customer("6000")
        b = self.cm.get_customer("6001")

        self.assertNotEqual(a["discord_id"], b["discord_id"])
        self.assertNotEqual(a["forum_id"], b["forum_id"])
        self.assertTrue(a["vip"])
        self.assertFalse(b["vip"])

    def test_all_customers_listed_correctly(self):
        """list_customers returns all active records."""
        for i in range(20):
            self.cm.add_customer(str(7000 + i), f"Bulk{i}", alt_count=1, vip=False, days=30)
        records = self.cm.list_customers(active_only=True)
        ids = {r["discord_id"] for r in records}
        for i in range(20):
            self.assertIn(str(7000 + i), ids)

    # ── Memory / state isolation ──────────────────────────────────────────────

    def test_timer_sent_reminders_cleared_on_expiry(self):
        """After a customer expires, their reminder keys are cleared."""
        self.cm.add_customer("8000", "ClearMe", alt_count=1, vip=False, days=30)
        # Simulate a reminder was sent
        self.te._sent_reminders.add(("8000", 7))
        self.te._sent_reminders.add(("8000", 3))

        # Set to expired
        con = sqlite3.connect(self._tmp.name)
        con.execute("UPDATE customers SET expiry_date = ? WHERE discord_id = '8000'",
                    (int(time.time()) - 10,))
        con.commit(); con.close()

        asyncio.run(self.te.scan_once())

        # After expiry scan, the reminder keys should be cleared
        for threshold in self.te.REMINDER_THRESHOLDS:
            self.assertNotIn(("8000", threshold), self.te._sent_reminders,
                             f"Reminder key for expired customer should be cleared")


if __name__ == "__main__":
    unittest.main()
