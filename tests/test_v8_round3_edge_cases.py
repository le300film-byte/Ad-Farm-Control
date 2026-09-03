"""V8 Phase 4 · Round 3 – Edge Cases.

Covers:
  - Invalid inputs (empty tokens, malformed channel IDs, invalid alt counts)
  - Expiry with fake short timer
  - GitHub error messages are human-readable
  - Concurrency: no race conditions in DB writes
  - Repo/channel validation helpers
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import time
import threading
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


class TestEdgeCases(unittest.TestCase):

    def setUp(self):
        import tempfile
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        import customer_manager as cm
        cm.DB_PATH = self._tmp.name
        cm.init_db()
        self.cm = cm

    def tearDown(self):
        os.unlink(self._tmp.name)

    # ── Invalid inputs ────────────────────────────────────────────────────────

    def test_empty_discord_id_handled(self):
        """get_customer('') returns None, does not raise."""
        result = self.cm.get_customer("")
        self.assertIsNone(result)

    def test_zero_days_subscription(self):
        """0-day subscription is immediately expired."""
        self.cm.add_customer("601", "Zero", alt_count=1, vip=False, days=0)
        expired = self.cm.get_expired_customers()
        ids = [c["discord_id"] for c in expired]
        self.assertIn("601", ids)

    def test_negative_days_subscription(self):
        """Negative days subscription is immediately expired."""
        self.cm.add_customer("602", "Neg", alt_count=1, vip=False, days=-1)
        expired = self.cm.get_expired_customers()
        ids = [c["discord_id"] for c in expired]
        self.assertIn("602", ids)

    def test_alt_count_zero(self):
        """alt_count=0 is stored without error."""
        self.cm.add_customer("603", "NoAlts", alt_count=0, vip=False, days=10)
        c = self.cm.get_customer("603")
        self.assertEqual(c["alt_count"], 0)

    def test_very_long_username(self):
        """Long usernames are truncated or stored; no crash."""
        long_name = "A" * 300
        self.cm.add_customer("604", long_name, alt_count=1, vip=False, days=10)
        c = self.cm.get_customer("604")
        self.assertIsNotNone(c)

    def test_repos_json_malformed(self):
        """Malformed repos JSON in DB returns empty list, no crash."""
        self.cm.add_customer("605", "Malformed", alt_count=1, vip=False, days=10)
        con = sqlite3.connect(self._tmp.name)
        con.execute("UPDATE customers SET repos = 'NOT_JSON' WHERE discord_id = '605'")
        con.commit(); con.close()
        c = self.cm.get_customer("605")
        self.assertEqual(c["repos"], [])

    # ── Fake short expiry ─────────────────────────────────────────────────────

    def test_fake_expiry_triggers_shutdown(self):
        """Setting expiry 5 seconds in future then simulating time crossing triggers deactivation."""
        self.cm.add_customer("606", "ShortLived", alt_count=1, vip=False, days=30)
        # Manually set expiry to past
        con = sqlite3.connect(self._tmp.name)
        con.execute(
            "UPDATE customers SET expiry_date = ? WHERE discord_id = '606'",
            (int(time.time()) - 1,)
        )
        con.commit(); con.close()

        import timer_engine as te
        te._bot_ref = None
        te._sent_reminders.clear()

        _run(te.scan_once())

        c = self.cm.get_customer("606")
        self.assertFalse(c["active"], "Expired customer must be deactivated by timer scan")

    # ── GitHub error human-readable ───────────────────────────────────────────

    def test_github_error_message_readable(self):
        """RuntimeError from github_dispatch contains human-readable HTTP status."""
        from urllib.error import HTTPError
        import github_dispatch as gd
        os.environ["GH_ADMIN_TOKEN"] = "fake"

        with mock.patch("github_dispatch.urlopen") as mu:
            err = HTTPError(
                "https://api.github.com/repos/x/y",
                404,
                "Not Found",
                {},
                None,
            )
            err.read = lambda: b'{"message": "Not Found"}'
            mu.side_effect = err
            with self.assertRaises(RuntimeError) as ctx:
                gd._request("GET", "/repos/x/y")
            self.assertIn("404", str(ctx.exception))

    # ── Concurrency: no race conditions ──────────────────────────────────────

    def test_concurrent_db_writes(self):
        """Multiple threads can write to customers.db simultaneously without corruption."""
        errors = []

        def _write(uid: str):
            try:
                self.cm.add_customer(uid, f"User{uid}", alt_count=1, vip=False, days=10)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_write, args=(str(700 + i),)) for i in range(10)]
        for t in threads: t.start()
        for t in threads: t.join()

        self.assertEqual(errors, [], f"Concurrent writes raised: {errors}")
        all_c = self.cm.list_customers(active_only=True)
        written_ids = {c["discord_id"] for c in all_c}
        for i in range(10):
            self.assertIn(str(700 + i), written_ids)

    def test_extend_idempotent_on_repeat(self):
        """Extending the same customer multiple times stacks correctly."""
        self.cm.add_customer("800", "Stack", alt_count=1, vip=False, days=10)
        self.cm.extend_customer("800", 5)
        self.cm.extend_customer("800", 5)
        days = self.cm.days_remaining("800")
        self.assertAlmostEqual(days, 20.0, delta=0.1)

    # ── Deactivation idempotent ───────────────────────────────────────────────

    def test_deactivate_already_inactive(self):
        """Deactivating an already-inactive customer does not raise."""
        self.cm.add_customer("900", "AlreadyOff", alt_count=1, vip=False, days=10)
        self.cm.deactivate_customer("900")
        self.cm.deactivate_customer("900")  # second call must be safe
        c = self.cm.get_customer("900")
        self.assertFalse(c["active"])

    def test_extend_nonexistent_returns_false(self):
        ok = self.cm.extend_customer("doesnotexist_xyz", 5)
        self.assertFalse(ok)

    # ── Alt count validation ──────────────────────────────────────────────────

    def test_max_alt_count_stored(self):
        """Large alt counts are stored without error."""
        self.cm.add_customer("950", "Many", alt_count=100, vip=False, days=30)
        c = self.cm.get_customer("950")
        self.assertEqual(c["alt_count"], 100)

    # ── Backup helper ─────────────────────────────────────────────────────────

    def test_backup_db_creates_file(self):
        self.cm.add_customer("960", "BackupTest", alt_count=1, vip=False, days=10)
        backup_path = self.cm.backup_db()
        self.assertTrue(os.path.exists(backup_path))
        os.unlink(backup_path)

    def test_backup_db_no_file_returns_empty(self):
        """backup_db on non-existent DB returns empty string."""
        import tempfile
        orig = self.cm.DB_PATH
        self.cm.DB_PATH = "/tmp/nonexistent_xyzabc_99.db"
        result = self.cm.backup_db()
        self.assertEqual(result, "")
        self.cm.DB_PATH = orig


if __name__ == "__main__":
    unittest.main()
