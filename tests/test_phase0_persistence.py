"""Phase 0.1 — Gist write-through persistence: backup, restore, lease, handoff.

These tests simulate GitHub Gist REST calls with an in-process fake so the
whole durability path (WAL checkpoint → revisioned upload → integrity-checked
restore → run-id lease) is verified without network access.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import customer_manager as cm
import gist_backup as gb


class FakeGist:
    """Minimal Gist backend: files dict + history snapshots."""

    def __init__(self):
        self.files: dict[str, str] = {}
        self.history: list[dict] = []
        self.calls: list[tuple] = []

    def snapshot(self) -> dict:
        return {"files": {k: {"content": v} for k, v in self.files.items()},
                "history": list(self.history)}

    # routing helper installed into gb._request
    def route(self, method: str, path: str, body=None):
        self.calls.append((method, path))
        if method == "GET" and path.startswith("/gists/"):
            return self.snapshot()
        if method == "PATCH" and path.startswith("/gists/"):
            for name, info in (body or {}).get("files", {}).items():
                content = info.get("content", "")
                if content == "":
                    self.files.pop(name, None)
                else:
                    if name == gb.DB_FILENAME:
                        self.history.append({"version": f"sha-{len(self.history) + 1}"})
                    self.files[name] = content
            return self.snapshot()
        if method == "GET" and "/gists/" in path and "/" in path.split("/gists/")[1]:
            sha = path.split("/gists/")[1].split("/")[1]
            idx = int(sha.split("-")[1]) - 1 if sha.startswith("sha-") else 0
            # For simplicity return a snapshot whose files are the *current*
            # set; the restore test uses prev-file fallback instead.
            return self.snapshot()
        raise AssertionError(f"unexpected request: {method} {path}")


class GistTestCase(unittest.TestCase):
    def setUp(self):
        self._db_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._db_tmp.close()
        cm.DB_PATH = self._db_tmp.name
        cm.STORE_ALT_TOKENS = "1"
        cm.init_db()
        self.fake = FakeGist()
        gb.set_config("gist-test-123", "fake-token")
        gb.RETRY_BACKOFFS = ()  # no sleeps in tests
        gb._last_alert_at.clear()
        self._patch = mock.patch.object(gb, "_request", side_effect=self.fake.route)
        self._patch.start()

    def tearDown(self):
        # Let any queued write-through backup drain before switching config so
        # the global worker never uploads a stale test DB into another test.
        gb.flush_backups(timeout=10)
        self._patch.stop()
        gb.set_config("", "")
        for suffix in ("", "-wal", "-shm", ".backup"):
            try:
                os.unlink(self._db_tmp.name + suffix)
            except OSError:
                pass


class TestGistBackupRestore(GistTestCase):

    def test_backup_revision_monotonic_and_meta(self):
        cm.add_customer("111", "Alice", alt_count=2, vip=True, days=30)
        gb.flush_backups(timeout=5)  # write-through snapshot for the add
        r1 = gb.backup_db_to_gist("test")
        self.assertTrue(r1["ok"])
        self.assertGreaterEqual(r1["revision"], 1)
        cm.extend_customer("111", 5)
        gb.flush_backups(timeout=5)  # write-through snapshot for the extend
        r2 = gb.backup_db_to_gist("test")
        self.assertTrue(r2["ok"])
        self.assertGreater(r2["revision"], r1["revision"])
        meta = json.loads(self.fake.files[gb.META_FILENAME])
        self.assertEqual(meta["revision"], r2["revision"])
        self.assertIn("customers.db.b64", self.fake.files)
        self.assertIn("customers.prev.db.b64", self.fake.files)

    def test_write_through_after_every_db_write(self):
        cm.add_customer("222", "Bob", alt_count=1, vip=False, days=10)
        gb.flush_backups(timeout=5)
        self.assertIn(gb.DB_FILENAME, self.fake.files)
        cm.extend_customer("222", 3)
        gb.flush_backups(timeout=5)
        meta = json.loads(self.fake.files[gb.META_FILENAME])
        self.assertEqual(meta["revision"], 2)

    def test_restore_after_fresh_filesystem_chunk_handoff(self):
        """~5.8h chunk handoff: stop bot → fresh fs → restore from Gist."""
        cm.add_customer("303", "Carol", alt_count=3, vip=True, days=30)
        cm.record_event("303", "ticket_open", {})
        gb.backup_db_to_gist("handoff")
        self.assertTrue(gb.flush_backups(timeout=5) == 0)

        # Simulate the next chunk: brand-new DB path, no local file.
        fresh = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        fresh.close()
        os.unlink(fresh.name)
        cm.DB_PATH = fresh.name

        res = gb.restore_db_from_gist()
        self.assertTrue(res["ok"], res)
        cm.init_db()  # restore target already has schema; idempotent
        c = cm.get_customer("303")
        self.assertIsNotNone(c)
        self.assertEqual(c["alt_count"], 3)
        # Timer engine sees the restored customer as expiring.
        expiring = cm.get_expiring_customers(within_days=60)
        self.assertIn("303", [x["discord_id"] for x in expiring])
        os.unlink(fresh.name)

    def test_restore_falls_back_to_previous_revision_on_corruption(self):
        cm.add_customer("404", "Dave", alt_count=1, vip=False, days=30)
        gb.backup_db_to_gist("v1")
        cm.extend_customer("404", 5)
        gb.backup_db_to_gist("v2")
        # Corrupt the newest artifact.
        self.fake.files[gb.DB_FILENAME] = "!!! not base64 db !!!"
        fresh = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        fresh.close()
        os.unlink(fresh.name)
        cm.DB_PATH = fresh.name
        res = gb.restore_db_from_gist()
        self.assertTrue(res["ok"], res)
        self.assertIn(res["source"], ("previous", "history:"))
        c = cm.get_customer("404")
        self.assertIsNotNone(c)
        os.unlink(fresh.name)

    def test_not_configured_is_explicit_noop(self):
        gb.set_config("", "")
        res = gb.backup_db_to_gist("test")
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "not_configured")
        res2 = gb.restore_db_from_gist()
        self.assertFalse(res2["ok"])
        self.assertEqual(res2["error"], "not configured")

    def test_gist_failure_keeps_local_and_alerts(self):
        alerts = []
        gb.register_alert_callback(lambda msg: alerts.append(msg))
        cm.add_customer("505", "Eve", alt_count=1, vip=False, days=30)
        with mock.patch.object(gb, "_request", side_effect=gb.GistError("boom")):
            res = gb.backup_db_to_gist("test")
        self.assertFalse(res["ok"])
        self.assertTrue(res["degraded"])
        self.assertTrue(alerts)
        # Local DB still works — bot keeps running on local copy.
        self.assertIsNotNone(cm.get_customer("505"))


class TestGistLease(GistTestCase):

    def test_second_boot_within_lease_aborts(self):
        r1 = gb.acquire_run_lease("run-a")
        self.assertTrue(r1["ok"])
        self.assertTrue(r1["lease"])
        r2 = gb.acquire_run_lease("run-b")
        self.assertFalse(r2["ok"])
        self.assertEqual(r2["reason"], "concurrent_boot")
        self.assertEqual(r2["holder"]["run_id"], "run-a")

    def test_expired_lease_can_be_taken_over(self):
        gb.acquire_run_lease("run-a")
        holder = json.loads(self.fake.files[gb.LOCK_FILENAME])
        holder["expires_at"] = time.time() - 10
        self.fake.files[gb.LOCK_FILENAME] = json.dumps(holder)
        r2 = gb.acquire_run_lease("run-b")
        self.assertTrue(r2["ok"])
        self.assertEqual(r2["run_id"], "run-b")

    def test_same_run_reacquires_lease(self):
        gb.acquire_run_lease("run-x")
        r2 = gb.acquire_run_lease("run-x")
        self.assertTrue(r2["ok"])

    def test_renew_and_release(self):
        gb.acquire_run_lease("run-y")
        self.assertTrue(gb.renew_run_lease("run-y"))
        self.assertTrue(gb.release_run_lease("run-y"))
        self.assertNotIn(gb.LOCK_FILENAME, self.fake.files)


class TestReminderPersistence(unittest.TestCase):
    """R-08: reminder dedupe is persisted; restarts never re-send."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        cm.DB_PATH = self._tmp.name
        cm.init_db()
        import timer_engine as te
        te._sent_reminders.clear()

    def tearDown(self):
        os.unlink(self._tmp.name)

    def test_sent_state_survives_new_tracker_instance(self):
        import timer_engine as te
        te._sent_reminders.add(("900", 7))
        # simulate restart: fresh tracker reads from customers.db
        fresh = te.ReminderTracker()
        self.assertIn(("900", 7), fresh)
        self.assertEqual(len(fresh), 1)

    def test_restart_scans_do_not_resend(self):
        import timer_engine as te
        cm.add_customer("901", "Restart", alt_count=1, vip=False, days=30)
        con = sqlite3.connect(self._tmp.name)
        con.execute("UPDATE customers SET expiry_date = ? WHERE discord_id = '901'",
                    (int(time.time()) + 6 * 86400,))
        con.commit()
        con.close()
        dm_log = []
        async def _fake_dm(_id, msg):
            dm_log.append((_id, msg))
            return True
        te._send_dm = _fake_dm
        loop = asyncio.new_event_loop()
        loop.run_until_complete(te.scan_once())
        # "restart": fresh tracker reads persisted rows from customers.db
        te._sent_reminders = te.ReminderTracker()
        loop.run_until_complete(te.scan_once())
        loop.close()
        # Exactly one DM despite two scans + a restart.
        self.assertEqual(len(dm_log), 1)
        self.assertEqual(dm_log[0][0], "901")

    def test_clear_removes_persisted_rows(self):
        import timer_engine as te
        te._sent_reminders.add(("902", 3))
        te._sent_reminders.clear()
        self.assertEqual(len(te.ReminderTracker()), 0)


if __name__ == "__main__":
    unittest.main()
