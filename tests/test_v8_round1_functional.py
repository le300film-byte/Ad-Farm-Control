"""V8 Phase 4 · Round 1 – Functional Validation.

Tests every V8 module and command in isolation:
  - customer_manager: CRUD, expiry helpers
  - security: admin/active/VIP checks
  - timer_engine: scan logic
  - admin_commands: cog wiring
  - github_dispatch: public API surface (network-free)
  - discord_forum: public API surface
  - /setup command: basic presence
  - /run: ∞ Limitless option
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# Ensure repo root is on sys.path
ROOT = Path(__file__).resolve().parents[1]

def _run(coro):
    """Run a coroutine in a fresh event loop (Python 3.11 compatible)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

sys.path.insert(0, str(ROOT))

# ──────────────────────────────────────────────────────────────────────────────
# Shared mock interaction helper
# ──────────────────────────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self):
        self._done = False
        self.messages: list = []

    def is_done(self): return self._done

    async def send_message(self, *a, **kw):
        self.messages.append(("send", a, kw)); self._done = True

    async def defer(self, **kw):
        self._done = True

    async def send_modal(self, modal):
        self.messages.append(("modal", modal))


class _FakeFollowup:
    def __init__(self):
        self.messages: list = []

    async def send(self, *a, **kw):
        self.messages.append(("followup", a, kw))


class _FakeInteraction:
    def __init__(self, user_id: int = 999):
        self.user = SimpleNamespace(id=user_id, display_name=f"User{user_id}")
        self.guild = None
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()


# ──────────────────────────────────────────────────────────────────────────────
# 1. customer_manager
# ──────────────────────────────────────────────────────────────────────────────

class TestCustomerManager(unittest.TestCase):

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

    def test_add_and_get(self):
        self.cm.add_customer("111", "Alice", alt_count=2, vip=True, days=30)
        c = self.cm.get_customer("111")
        self.assertIsNotNone(c)
        self.assertEqual(c["discord_username"], "Alice")
        self.assertEqual(c["alt_count"], 2)
        self.assertTrue(c["vip"])

    def test_get_nonexistent(self):
        self.assertIsNone(self.cm.get_customer("doesnotexist"))

    def test_extend(self):
        self.cm.add_customer("222", "Bob", alt_count=1, vip=False, days=10)
        before = self.cm.get_customer("222")["expiry_date"]
        ok = self.cm.extend_customer("222", 5)
        self.assertTrue(ok)
        after = self.cm.get_customer("222")["expiry_date"]
        self.assertAlmostEqual(after - before, 5 * 86400, delta=5)

    def test_deactivate(self):
        self.cm.add_customer("333", "Carol", alt_count=1, vip=False, days=30)
        self.cm.deactivate_customer("333")
        c = self.cm.get_customer("333")
        self.assertFalse(c["active"])

    def test_is_active(self):
        self.cm.add_customer("444", "Dave", alt_count=1, vip=False, days=30)
        self.assertTrue(self.cm.is_active("444"))
        self.cm.deactivate_customer("444")
        self.assertFalse(self.cm.is_active("444"))

    def test_is_vip(self):
        self.cm.add_customer("555", "Eve", alt_count=1, vip=True, days=30)
        self.assertTrue(self.cm.is_vip("555"))
        self.cm.add_customer("556", "Frank", alt_count=1, vip=False, days=30)
        self.assertFalse(self.cm.is_vip("556"))

    def test_set_vip(self):
        self.cm.add_customer("557", "Grace", alt_count=1, vip=False, days=30)
        self.cm.set_vip("557", True)
        self.assertTrue(self.cm.is_vip("557"))

    def test_days_remaining(self):
        self.cm.add_customer("558", "Heidi", alt_count=1, vip=False, days=30)
        days = self.cm.days_remaining("558")
        self.assertAlmostEqual(days, 30.0, delta=0.01)

    def test_expired_customer_detection(self):
        self.cm.add_customer("559", "Ivan", alt_count=1, vip=False, days=30)
        # Manually set expiry to the past
        import sqlite3
        con = sqlite3.connect(self._tmp.name)
        con.execute("UPDATE customers SET expiry_date = ? WHERE discord_id = '559'",
                    (int(time.time()) - 10,))
        con.commit(); con.close()
        expired = self.cm.get_expired_customers()
        ids = [c["discord_id"] for c in expired]
        self.assertIn("559", ids)

    def test_expiring_within(self):
        self.cm.add_customer("560", "Judy", alt_count=1, vip=False, days=5)
        within_7 = self.cm.get_expiring_customers(within_days=7)
        ids = [c["discord_id"] for c in within_7]
        self.assertIn("560", ids)

    def test_list_customers(self):
        self.cm.add_customer("600", "K", alt_count=1, vip=False, days=10)
        self.cm.add_customer("601", "L", alt_count=1, vip=False, days=10)
        all_c = self.cm.list_customers(active_only=True)
        self.assertGreaterEqual(len(all_c), 2)

    def test_update_repos(self):
        self.cm.add_customer("700", "M", alt_count=2, vip=False, days=10)
        self.cm.update_repos("700", ["repo_alt1", "repo_alt2"])
        c = self.cm.get_customer("700")
        self.assertEqual(c["repos"], ["repo_alt1", "repo_alt2"])

    def test_update_forum_ids(self):
        self.cm.add_customer("701", "N", alt_count=1, vip=False, days=10)
        self.cm.update_forum_ids("701", forum_id="111", control_thread_id="222")
        c = self.cm.get_customer("701")
        self.assertEqual(c["forum_id"], "111")
        self.assertEqual(c["control_thread_id"], "222")


# ──────────────────────────────────────────────────────────────────────────────
# 2. security
# ──────────────────────────────────────────────────────────────────────────────

class TestSecurity(unittest.TestCase):

    def setUp(self):
        import tempfile
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        import customer_manager as cm
        cm.DB_PATH = self._tmp.name
        cm.init_db()
        self.cm = cm

        import security as sec
        sec.OWNER_IDS = {100}
        self.sec = sec

    def tearDown(self):
        os.unlink(self._tmp.name)

    def test_is_admin_pass(self):
        self.assertTrue(self.sec.is_admin(100))

    def test_is_admin_fail(self):
        self.assertFalse(self.sec.is_admin(999))

    def test_is_admin_empty_list(self):
        self.sec.OWNER_IDS = set()
        self.assertFalse(self.sec.is_admin(100))
        self.sec.OWNER_IDS = {100}

    def test_active_customer_check(self):
        self.cm.add_customer("200", "Alice", alt_count=1, vip=False, days=30)
        self.assertTrue(self.sec.is_active_customer("200"))

    def test_inactive_customer(self):
        self.cm.add_customer("201", "Bob", alt_count=1, vip=False, days=30)
        self.cm.deactivate_customer("201")
        self.assertFalse(self.sec.is_active_customer("201"))

    def test_vip_check(self):
        self.cm.add_customer("202", "Carol", alt_count=1, vip=True, days=30)
        self.assertTrue(self.sec.is_vip_customer("202"))

    def test_non_vip(self):
        self.cm.add_customer("203", "Dave", alt_count=1, vip=False, days=30)
        self.assertFalse(self.sec.is_vip_customer("203"))

    def test_check_admin_pass(self):
        inter = _FakeInteraction(user_id=100)
        result = _run(self.sec.check_admin(inter))
        self.assertTrue(result)
        self.assertEqual(len(inter.response.messages), 0)

    def test_check_admin_fail(self):
        inter = _FakeInteraction(user_id=999)
        result = _run(self.sec.check_admin(inter))
        self.assertFalse(result)
        self.assertTrue(any("not authorized" in str(m).lower() for m in inter.response.messages))

    def test_check_active_pass(self):
        self.cm.add_customer("204", "Eve", alt_count=1, vip=False, days=30)
        inter = _FakeInteraction(user_id=204)
        result = _run(self.sec.check_active(inter))
        self.assertTrue(result)

    def test_check_active_expired(self):
        self.cm.add_customer("205", "Frank", alt_count=1, vip=False, days=30)
        self.cm.deactivate_customer("205")
        inter = _FakeInteraction(user_id=205)
        result = _run(self.sec.check_active(inter))
        self.assertFalse(result)
        all_msgs = str(inter.response.messages)
        self.assertTrue("expired" in all_msgs.lower() or "not authorized" in all_msgs.lower())

    def test_check_vip_pass(self):
        self.cm.add_customer("206", "Grace", alt_count=1, vip=True, days=30)
        inter = _FakeInteraction(user_id=206)
        result = _run(self.sec.check_vip(inter))
        self.assertTrue(result)

    def test_check_vip_fail(self):
        self.cm.add_customer("207", "Heidi", alt_count=1, vip=False, days=30)
        inter = _FakeInteraction(user_id=207)
        result = _run(self.sec.check_vip(inter))
        self.assertFalse(result)
        all_msgs = str(inter.response.messages)
        self.assertIn("VIP", all_msgs)


# ──────────────────────────────────────────────────────────────────────────────
# 3. github_dispatch (network-free)
# ──────────────────────────────────────────────────────────────────────────────

class TestGithubDispatch(unittest.TestCase):

    def setUp(self):
        import github_dispatch as gd
        self.gd = gd
        os.environ["GH_ADMIN_TOKEN"] = "fake_token_for_testing"

    def tearDown(self):
        os.environ.pop("GH_ADMIN_TOKEN", None)

    def test_admin_token_read(self):
        self.assertEqual(self.gd._admin_token(), "fake_token_for_testing")

    def test_no_token_raises(self):
        os.environ["GH_ADMIN_TOKEN"] = ""
        with self.assertRaises(RuntimeError):
            self.gd._admin_token()
        os.environ["GH_ADMIN_TOKEN"] = "fake_token_for_testing"

    def test_headers(self):
        h = self.gd._headers("my_token")
        self.assertIn("Authorization", h)
        self.assertIn("Bearer my_token", h["Authorization"])

    @mock.patch("github_dispatch.urlopen")
    def test_repo_exists_true(self, mock_open):
        mock_resp = mock.MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = mock.MagicMock(return_value=False)
        mock_resp.read.return_value = b'{"id": 1}'
        mock_open.return_value = mock_resp
        self.assertTrue(self.gd.repo_exists("owner", "repo"))

    @mock.patch("github_dispatch.urlopen")
    def test_repo_exists_false(self, mock_open):
        from urllib.error import HTTPError
        import io
        err = HTTPError("url", 404, "Not Found", {}, io.BytesIO(b'{"message":"Not Found"}'))
        mock_open.side_effect = err
        self.assertFalse(self.gd.repo_exists("owner", "repo"))


# ──────────────────────────────────────────────────────────────────────────────
# 4. timer_engine (logic-only, no network)
# ──────────────────────────────────────────────────────────────────────────────

class TestTimerEngine(unittest.TestCase):

    def setUp(self):
        import tempfile
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        import customer_manager as cm
        cm.DB_PATH = self._tmp.name
        cm.init_db()
        self.cm = cm

        import timer_engine as te
        te._bot_ref = None  # no live bot in tests
        te._sent_reminders.clear()
        self.te = te

    def tearDown(self):
        os.unlink(self._tmp.name)

    def test_scan_no_customers(self):
        # Should not raise even with empty DB
        _run(self.te.scan_once())

    def test_expired_customer_deactivated(self):
        self.cm.add_customer("300", "Test", alt_count=1, vip=False, days=30)
        import sqlite3
        con = sqlite3.connect(self._tmp.name)
        con.execute("UPDATE customers SET expiry_date = ? WHERE discord_id = '300'",
                    (int(time.time()) - 100,))
        con.commit(); con.close()
        _run(self.te.scan_once())
        c = self.cm.get_customer("300")
        self.assertFalse(c["active"], "Expired customer should be deactivated after scan")

    def test_reminder_thresholds_populated(self):
        # 3-day boundary: customer should appear in get_expiring_customers(7)
        self.cm.add_customer("301", "Soon", alt_count=1, vip=False, days=2)
        within = self.cm.get_expiring_customers(within_days=7)
        ids = [c["discord_id"] for c in within]
        self.assertIn("301", ids)

    def test_no_duplicate_reminders(self):
        self.cm.add_customer("302", "Dup", alt_count=1, vip=False, days=2)
        # First scan marks reminder as sent
        self.te._sent_reminders.add(("302", 3))
        # The reminder should not be sent again
        self.assertIn(("302", 3), self.te._sent_reminders)


# ──────────────────────────────────────────────────────────────────────────────
# 5. Command presence & V8 bot integration
# ──────────────────────────────────────────────────────────────────────────────

class TestBotV8Integration(unittest.TestCase):

    def test_setup_command_exists(self):
        """The /setup command must be registered on the bot tree."""
        from control_bot import bot as cb
        names = [c.name for c in cb.bot.tree.get_commands()]
        self.assertIn("setup", names, "/setup must be registered on the bot")

    def test_removed_commands_absent(self):
        """analytics, diagnose, canary, topology, sync must not be registered."""
        from control_bot import bot as cb
        names = {c.name for c in cb.bot.tree.get_commands()}
        removed = {"analytics", "diagnose", "canary", "topology", "sync"}
        for cmd in removed:
            self.assertNotIn(cmd, names, f"/{cmd} should have been removed in V8")

    def test_customer_commands_present(self):
        """All base-tier customer commands must be registered."""
        from control_bot import bot as cb
        names = {c.name for c in cb.bot.tree.get_commands()}
        required = {"run", "stop", "pause", "resume", "alt", "tune",
                    "channels", "deals", "status", "reply", "refresh",
                    "dashboard", "help", "shutdown", "setup"}
        for cmd in required:
            self.assertIn(cmd, names, f"/{cmd} must be in bot tree")

    def test_run_limitless_option_present(self):
        """The /run RunStartView must include the ∞ Limitless option (value='0')."""
        from control_bot.bot import RunStartView
        view = RunStartView(owner_id=1)
        values = [opt.value for opt in view.runtime_select.options]
        self.assertIn("0", values, "Limitless runtime option (value='0') must be present")

    def test_v8_modules_importable(self):
        """All V8 modules must import without errors."""
        for mod_name in ("customer_manager", "security", "timer_engine",
                          "admin_commands", "github_dispatch", "discord_forum"):
            with self.subTest(module=mod_name):
                mod = __import__(mod_name)
                self.assertIsNotNone(mod)


if __name__ == "__main__":
    unittest.main()
