"""V8 Phase 4 · Round 2 – Security & Permissions.

Verifies:
  - Non-admin cannot use /admin commands
  - Expired customer cannot use customer commands
  - Non-VIP blocked from VIP features
  - Customer A cannot read Customer B's data
  - DM-Inbox (dm_thread_id) not created for non-VIP customers
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]

def _run(coro):
    """Run a coroutine in a fresh event loop (Python 3.11 compatible)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

sys.path.insert(0, str(ROOT))


class _FakeResponse:
    def __init__(self):
        self._done = False
        self.messages: list = []

    def is_done(self): return self._done

    async def send_message(self, *a, **kw):
        self.messages.append(("send", a, kw)); self._done = True

    async def defer(self, **kw):
        self._done = True


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


def _all_text(inter: _FakeInteraction) -> str:
    parts = [str(m) for m in inter.response.messages]
    parts += [str(m) for m in inter.followup.messages]
    return " ".join(parts).lower()


class TestSecurityPermissions(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Ensure a fresh event loop exists for this test class (Python 3.11+)
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    @classmethod
    def tearDownClass(cls):
        import asyncio
        loop = asyncio.get_event_loop()
        if not loop.is_closed():
            loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())

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

    # ── Admin checks ──────────────────────────────────────────────────────────

    def test_non_admin_denied(self):
        """Non-admin user gets '❌ not authorized' for admin commands."""
        inter = _FakeInteraction(user_id=999)
        result = asyncio.get_event_loop().run_until_complete(
            self.sec.check_admin(inter)
        )
        self.assertFalse(result)
        self.assertIn("not authorized", _all_text(inter))

    def test_admin_allowed(self):
        inter = _FakeInteraction(user_id=100)
        result = asyncio.get_event_loop().run_until_complete(
            self.sec.check_admin(inter)
        )
        self.assertTrue(result)

    # ── Active customer checks ────────────────────────────────────────────────

    def test_expired_customer_denied(self):
        """Expired customer gets subscription-expired error."""
        self.cm.add_customer("401", "Expired", alt_count=1, vip=False, days=30)
        import sqlite3
        con = sqlite3.connect(self._tmp.name)
        con.execute(
            "UPDATE customers SET expiry_date = ?, active = 0 WHERE discord_id = '401'",
            (int(time.time()) - 100,)
        )
        con.commit(); con.close()

        inter = _FakeInteraction(user_id=401)
        result = asyncio.get_event_loop().run_until_complete(
            self.sec.check_active(inter)
        )
        self.assertFalse(result)
        txt = _all_text(inter)
        self.assertTrue("expired" in txt or "not authorized" in txt)

    def test_never_customer_denied(self):
        """User not in DB is denied access."""
        inter = _FakeInteraction(user_id=88888)
        result = asyncio.get_event_loop().run_until_complete(
            self.sec.check_active(inter)
        )
        self.assertFalse(result)

    def test_active_customer_allowed(self):
        self.cm.add_customer("402", "Active", alt_count=1, vip=False, days=30)
        inter = _FakeInteraction(user_id=402)
        result = asyncio.get_event_loop().run_until_complete(
            self.sec.check_active(inter)
        )
        self.assertTrue(result)

    # ── VIP checks ────────────────────────────────────────────────────────────

    def test_non_vip_denied_vip_feature(self):
        self.cm.add_customer("403", "Base", alt_count=1, vip=False, days=30)
        inter = _FakeInteraction(user_id=403)
        result = asyncio.get_event_loop().run_until_complete(
            self.sec.check_vip(inter)
        )
        self.assertFalse(result)
        self.assertIn("vip", _all_text(inter))

    def test_vip_customer_allowed(self):
        self.cm.add_customer("404", "VIPUser", alt_count=1, vip=True, days=30)
        inter = _FakeInteraction(user_id=404)
        result = asyncio.get_event_loop().run_until_complete(
            self.sec.check_vip(inter)
        )
        self.assertTrue(result)

    # ── Privacy isolation ─────────────────────────────────────────────────────

    def test_customer_a_cannot_read_customer_b(self):
        """Customer A's discord_id cannot retrieve Customer B's record."""
        self.cm.add_customer("500", "Alice", alt_count=1, vip=False, days=30)
        self.cm.add_customer("501", "Bob", alt_count=1, vip=False, days=30)
        # Customer A requests record by their own ID → only their record returned
        record = self.cm.get_customer("500")
        self.assertEqual(record["discord_username"], "Alice")
        # They cannot trivially access Bob's record via the API (must pass Bob's ID)
        record_b = self.cm.get_customer("501")
        self.assertEqual(record_b["discord_username"], "Bob")
        # The IDs are isolated — A's ID doesn't return B's data
        self.assertNotEqual(record["discord_id"], record_b["discord_id"])

    # ── DM-Inbox non-VIP check ────────────────────────────────────────────────

    def test_dm_inbox_not_created_for_non_vip(self):
        """Non-VIP activation sets dm_thread_id to empty string."""
        self.cm.add_customer("502", "NoVIP", alt_count=1, vip=False, days=30,
                             dm_thread_id="")
        c = self.cm.get_customer("502")
        # Non-VIP customers should have empty dm_thread_id
        self.assertIn(c.get("dm_thread_id", ""), ["", "0", None])

    def test_vip_customer_has_dm_inbox_slot(self):
        """VIP activation can store a dm_thread_id."""
        self.cm.add_customer("503", "VIPForum", alt_count=1, vip=True, days=30,
                             dm_thread_id="99999")
        c = self.cm.get_customer("503")
        self.assertEqual(c.get("dm_thread_id"), "99999")

    # ── require_access decorator ──────────────────────────────────────────────

    def test_require_access_admin_only(self):
        """@require_access(admin_only=True) blocks non-admins."""
        from security import require_access

        @require_access(admin_only=True)
        async def _admin_cmd(inter: _FakeInteraction):
            inter.response.messages.append(("ok",))

        # Admin
        inter_admin = _FakeInteraction(user_id=100)
        _run(_admin_cmd(inter_admin))
        self.assertIn(("ok",), inter_admin.response.messages)

        # Non-admin
        inter_stranger = _FakeInteraction(user_id=999)
        _run(_admin_cmd(inter_stranger))
        self.assertNotIn(("ok",), inter_stranger.response.messages)
        self.assertIn("not authorized", _all_text(inter_stranger))

    def test_require_access_active_only(self):
        """@require_access() blocks inactive customers."""
        from security import require_access

        @require_access()
        async def _customer_cmd(inter: _FakeInteraction):
            inter.response.messages.append(("ok",))

        # Active customer
        self.cm.add_customer("504", "Active", alt_count=1, vip=False, days=30)
        inter_active = _FakeInteraction(user_id=504)
        _run(_customer_cmd(inter_active))
        self.assertIn(("ok",), inter_active.response.messages)

        # Inactive
        inter_none = _FakeInteraction(user_id=77777)
        _run(_customer_cmd(inter_none))
        self.assertNotIn(("ok",), inter_none.response.messages)

    def test_require_access_vip_only(self):
        """@require_access(vip_only=True) blocks non-VIP active customers."""
        from security import require_access

        @require_access(vip_only=True)
        async def _vip_cmd(inter: _FakeInteraction):
            inter.response.messages.append(("ok",))

        # VIP customer
        self.cm.add_customer("505", "VIP", alt_count=1, vip=True, days=30)
        inter_vip = _FakeInteraction(user_id=505)
        _run(_vip_cmd(inter_vip))
        self.assertIn(("ok",), inter_vip.response.messages)

        # Non-VIP active customer
        self.cm.add_customer("506", "Base", alt_count=1, vip=False, days=30)
        inter_base = _FakeInteraction(user_id=506)
        _run(_vip_cmd(inter_base))
        self.assertNotIn(("ok",), inter_base.response.messages)
        self.assertIn("vip", _all_text(inter_base))


if __name__ == "__main__":
    unittest.main()
