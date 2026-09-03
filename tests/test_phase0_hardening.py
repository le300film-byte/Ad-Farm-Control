"""Phase 0 hardening + Phase 1-3 feature tests (network-free).

Covers TODO 0.2-0.9 (public repos, write-proof tokens, PAT expiry, policy card,
setup wizard helpers, permission assertions, memory-clear, curl_cffi, bug fixes)
plus the Phase 1-3 code: payments, ban watch, auto-renew, metrics, ops monitors,
multi-sig, proofs.  Everything mocks the network layer.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import customer_manager as cm
import github_dispatch as gd
import timer_engine as te


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeResponse:
    def __init__(self):
        self._done = False
        self.messages = []

    def is_done(self):
        return self._done

    async def send_message(self, *a, **kw):
        self.messages.append(("send", a, kw))
        self._done = True

    async def defer(self, **kw):
        self._done = True

    async def send_modal(self, modal):
        self.messages.append(("modal", modal))


class _FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, *a, **kw):
        self.messages.append(("followup", a, kw))


class _FakeInteraction:
    def __init__(self, user_id=999, guild=None):
        self.user = SimpleNamespace(id=user_id, display_name=f"User{user_id}")
        self.guild = guild
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()


class DbTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        cm.DB_PATH = self._tmp.name
        cm.init_db()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm", ".backup"):
            try:
                os.unlink(self._tmp.name + suffix)
            except OSError:
                pass


# ──────────────────────────────────────────────────────────────────────────────
# 0.2 / 0.3 — public repos + write-proof + PAT expiry + secret protection
# ──────────────────────────────────────────────────────────────────────────────

class TestGithubDispatchHardening(unittest.TestCase):

    def test_create_repo_defaults_to_public(self):
        calls = []

        def fake_request(method, path, body=None, token=None, expected_statuses=(200, 201, 204)):
            calls.append((method, path, body))
            if method == "GET" and path.startswith("/orgs/"):
                return {"login": "worker", "type": "Organization"}
            if method == "GET" and path.startswith("/repos/"):
                return {"sha": "abc"}
            return {}

        with mock.patch.dict(os.environ, {"GH_ADMIN_TOKEN": "tok"}, clear=False):
            with mock.patch.object(gd, "_request", side_effect=fake_request):
                gd.create_repo("worker-org", "customer_alt1")
        post = [c for c in calls if c[0] == "POST"][0]
        self.assertFalse(post[2]["private"], "customer repos must be public (0.2)")

    def test_token_for_owner_prefers_fine_grained_pat(self):
        with mock.patch.dict(os.environ, {
            "WORKER_TOKENS": "org-a:pat-a,org-b:pat-b",
            "GH_ADMIN_TOKEN": "classic-token",
        }, clear=False):
            self.assertEqual(gd.token_for_owner("org-a"), "pat-a")
            self.assertEqual(gd.token_for_owner("org-b"), "pat-b")
            self.assertEqual(gd.token_for_owner("unknown"), "classic-token")

    def test_verify_github_token_proves_write_access(self):
        seq = [
            {"login": "worker-org-bot", "expires_at": "2026-12-31T00:00:00Z"},
            {"login": "worker-org", "type": "Organization"},
            {"id": 5, "full_name": "worker-org/adfarm-token-check-1"},
            {},
        ]
        with mock.patch.object(gd, "_request", side_effect=seq) as req:
            res = gd.verify_github_token("worker-org", "pat")
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["created"])
        self.assertTrue(res["deleted"])
        methods = [c.args[0] for c in req.call_args_list]
        self.assertEqual(methods, ["GET", "GET", "POST", "DELETE"])

    def test_verify_github_token_fails_loudly_on_read_only_pat(self):
        # GET /user ok, GET /orgs not found (falls back to /user/repos),
        # POST /user/repos → 403: read-only PAT cannot create repos.
        with mock.patch.object(gd, "_request", side_effect=[
            {"login": "reader"},
            RuntimeError("HTTP 404: org not found"),
            RuntimeError("HTTP 403: Resource not accessible by integration"),
        ]):
            res = gd.verify_github_token("worker-org", "read-only-pat")
        self.assertFalse(res["ok"])
        self.assertIn("create", res["error"])

    def test_check_token_status_parses_expiry_and_401(self):
        with mock.patch.object(gd, "_request", side_effect=[
            {"login": "worker", "expires_at": "2026-09-10T00:00:00Z"},
        ]):
            ok = gd.check_token_status("fine-grained-pat", owner="worker-org")
        self.assertTrue(ok["ok"])
        self.assertIsNotNone(ok["days_left"])
        if ok["days_left"] is not None:
            self.assertLessEqual(ok["days_left"], 7.01)

        with mock.patch.object(gd, "_request", side_effect=RuntimeError("HTTP 401: Bad credentials")):
            bad = gd.check_token_status("dead")
        self.assertFalse(bad["ok"])
        self.assertIn("401", bad["error"])

    def test_enable_repo_secret_protection_primary_then_fallback(self):
        with mock.patch.dict(os.environ, {"GH_ADMIN_TOKEN": "tok"}, clear=False):
            with mock.patch.object(gd, "_request", side_effect=[
                RuntimeError("HTTP 404: Not Found"),
                {"ok": True},
            ]) as req:
                res = gd.enable_repo_secret_protection("worker-org", "cust_repo")
        self.assertTrue(res["ok"], res)
        verbs = [c.args[0] for c in req.call_args_list]
        self.assertEqual(verbs, ["PUT", "PATCH"])

    def test_enable_org_secret_protection(self):
        with mock.patch.object(gd, "_request", return_value={}) as req:
            res = gd.enable_org_secret_protection("worker-org", "tok")
        self.assertTrue(res["ok"])
        self.assertEqual(req.call_args.args[0], "PATCH")

    def test_rename_banned_repo(self):
        with mock.patch.dict(os.environ, {"GH_ADMIN_TOKEN": "tok"}, clear=False):
            with mock.patch.object(gd, "_request", return_value={}) as req:
                name = gd.rename_banned_repo("worker-org", "cust_alt1")
        self.assertIn("_BANNED_", name)
        self.assertEqual(req.call_args.args[0], "PATCH")

    def test_encrypt_secret_fails_loudly_without_pynacl(self):
        with mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch.dict("sys.modules", {"nacl": None}):
            # import would fail, simulate ImportError via builtins
            import builtins
            real_import = builtins.__import__
            def fake_import(name, *a, **kw):
                if name == "nacl" or name.startswith("nacl."):
                    raise ImportError("No module named 'nacl'")
                return real_import(name, *a, **kw)
            builtins.__import__ = fake_import
            try:
                with self.assertRaisesRegex(RuntimeError, "PyNaCl is required"):
                    gd._encrypt_secret("AAAA", "secret")
            finally:
                builtins.__import__ = real_import

    def test_encrypt_secret_allows_explicit_test_fallback(self):
        with mock.patch.dict(os.environ, {"ALLOW_BASE64_SECRET_FALLBACK": "1"}, clear=False):
            enc = gd._encrypt_secret("AAAA", "secret")
        import base64
        self.assertEqual(base64.b64decode(enc), b"secret")

    def test_soft_delete_renames_and_disables_workflow(self):
        from unittest import mock as _mock
        with mock.patch.dict(os.environ, {"GH_ADMIN_TOKEN": "tok"}, clear=False):
            with _mock.patch.object(gd, "_request", return_value={}) as req:
                res = gd.soft_delete_repo("worker-org", "cust_alt1")
        self.assertTrue(res["ok"])
        self.assertIn("_DELETED_", res["quarantined_as"])
        self.assertEqual(res["undo_window_hours"], 24)
        verbs = [c.args[0] for c in req.call_args_list]
        paths = [c.args[1] for c in req.call_args_list]
        self.assertEqual(verbs, ["PATCH", "PUT"])
        self.assertIn("actions/workflows/send_ads.yml/disable", paths[1])


# ──────────────────────────────────────────────────────────────────────────────
# 0.4 — policy card + click-through ack
# ──────────────────────────────────────────────────────────────────────────────

class TestPolicy(DbTestCase):

    def test_policy_card_contains_required_clauses(self):
        from control_bot.policy import POLICY_CARD, PRIVACY_NOTICE, TOS_TEXT
        joined = POLICY_CARD + PRIVACY_NOTICE + TOS_TEXT
        for needle in ("No refunds", "48 hours", "pro-rated", "Main accounts",
                       "Crypto", "Data we store", "No SLA", "deletion"):
            self.assertIn(needle, joined)

    def test_ack_is_recorded_and_versioned(self):
        from control_bot.policy import POLICY_VERSION
        self.assertFalse(cm.has_policy_ack("123"))
        cm.ack_policy("123", POLICY_VERSION)
        self.assertTrue(cm.has_policy_ack("123", POLICY_VERSION))
        self.assertFalse(cm.has_policy_ack("123", "other-version"))

    def test_require_policy_ack_gates_wallet_sharing(self):
        from control_bot import policy
        inter = _FakeInteraction(123)
        # Not acked → denied + card + button
        ok = _run(policy.require_policy_ack(inter))
        self.assertFalse(ok)
        self.assertTrue(inter.response.messages)
        cm.ack_policy("123", policy.POLICY_VERSION)
        inter2 = _FakeInteraction(123)
        ok2 = _run(policy.require_policy_ack(inter2))
        self.assertTrue(ok2)


# ──────────────────────────────────────────────────────────────────────────────
# 0.5 — setup wizard helpers + memory clear
# ──────────────────────────────────────────────────────────────────────────────

class TestSetupWizard(DbTestCase):

    def test_channel_validation(self):
        from control_bot.bot import _valid_channel_ids
        ok, ids = _valid_channel_ids("123456789012345678, 987654321012345678,123456789012345678")
        self.assertTrue(ok)
        self.assertEqual(len(ids), 2)  # deduped
        self.assertFalse(_valid_channel_ids("")[0])
        self.assertFalse(_valid_channel_ids("abc")[0])
        self.assertFalse(_valid_channel_ids("123")[0])

    def test_token_validation_hits_discord_api(self):
        from control_bot.bot import _validate_discord_token_sync
        import urllib.request as _ur
        with mock.patch.object(_ur, "urlopen") as m:
            m.return_value.__enter__ = mock.MagicMock(return_value=mock.MagicMock(read=lambda: b'{"username":"Alt","discriminator":"1"}'))
            ok, name = _validate_discord_token_sync("valid.token")
        self.assertTrue(ok)
        self.assertIn("Alt", name)

    def test_count_modal_rejects_out_of_range(self):
        from control_bot import bot as bot_mod
        modal = bot_mod.SetupCountModal(max_count=2, owner_id=999)
        modal.count = SimpleNamespace(value="9")
        inter = _FakeInteraction(999)
        _run(modal.on_submit(inter))
        self.assertIn("between **1 and 2**", inter.response.messages[0][1][0])

    def test_finalize_clears_token_memory(self):
        from control_bot import bot as bot_mod
        cm.add_customer("999", "Cust", alt_count=1, vip=False, days=30,
                        github_account="worker-org", repos=["cust_alt1"])
        session = bot_mod.SetupSession(owner_id=999, total=1)
        session.results.append({
            "alt": 1, "token": "SECRET-TOKEN", "channels": ["111111111111111111"],
            "username": "Alt#1", "valid": True,
        })
        inter = _FakeInteraction(999)
        with mock.patch("github_dispatch.set_repo_secret", return_value=None):
            _run(bot_mod._finalize_setup(inter, session))
        self.assertEqual(session.results, [], "tokens must be cleared after upload (0.8)")
        creds = cm.get_alt_credentials("999")
        self.assertEqual(creds[0]["token"], "SECRET-TOKEN")

    def test_setup_command_registered(self):
        from control_bot import bot as bot_mod
        names = {c.name for c in bot_mod.bot.tree.get_commands()}
        self.assertIn("setup", names)
        self.assertIn("renew", names)
        self.assertIn("pause-billing", names)
        self.assertIn("proofs", names)
        self.assertIn("squad", names)

    def test_alt_count_respects_paid_limit(self):
        modal = bot_mod_ref = __import__("control_bot.bot", fromlist=["SetupCountModal"]).SetupCountModal
        m = modal(max_count=1, owner_id=1)
        m.count = SimpleNamespace(value="2")
        inter = _FakeInteraction(1)
        _run(m.on_submit(inter))
        self.assertIn("between **1 and 1**", inter.response.messages[0][1][0])


# ──────────────────────────────────────────────────────────────────────────────
# 0.6 — forum permission checks + owner assertion
# ──────────────────────────────────────────────────────────────────────────────

class _FakeRole:
    def __init__(self, name, rid=0):
        self.name = name
        self.id = rid
        if name and name.startswith("@"):
            self.id = rid or 1
    def __hash__(self):
        return hash(self.id)
    def __eq__(self, other):
        return getattr(other, "id", None) == self.id


class _FakeMember(_FakeRole):
    def __init__(self, mid):
        super().__init__(None, mid)


class TestForumHardening(unittest.TestCase):

    def test_permission_overrides_match_expected(self):
        from discord_forum import check_forum_permission_overrides
        import discord
        default_role = _FakeRole("@everyone", 1)
        bot_member = _FakeMember(2)
        customer = _FakeMember(3)
        forum = SimpleNamespace(overwrites={
            default_role: discord.PermissionOverwrite(view_channel=False),
            bot_member: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True,
                manage_messages=True, create_public_threads=True, create_private_threads=True),
            customer: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True, attach_files=True),
        })
        res = check_forum_permission_overrides(forum, customer, bot_member=bot_member)
        self.assertTrue(res["ok"], res)

    def test_permission_mismatch_is_flagged(self):
        from discord_forum import check_forum_permission_overrides
        import discord
        default_role = _FakeRole("@everyone", 1)
        customer = _FakeMember(3)
        bot_member = _FakeMember(2)
        forum = SimpleNamespace(overwrites={
            default_role: discord.PermissionOverwrite(view_channel=False),
            bot_member: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True,
                manage_messages=True, create_public_threads=True, create_private_threads=True),
            customer: discord.PermissionOverwrite(view_channel=True, send_messages=False),
        })
        res = check_forum_permission_overrides(forum, customer, bot_member=bot_member)
        self.assertFalse(res["ok"])
        self.assertTrue(any("missing" in m for m in res["mismatches"]))

    def test_owner_assertion_denies_stranger(self):
        from discord_forum import assert_forum_owner
        import discord
        member = _FakeMember(999)
        forum = SimpleNamespace(overwrites={
            member: discord.PermissionOverwrite(view_channel=True),
            _FakeMember(111): discord.PermissionOverwrite(view_channel=False),
        })
        guild = SimpleNamespace(
            get_channel=lambda _id: forum,
            get_member=lambda _id: None,
        )
        inter = _FakeInteraction(123, guild=guild)
        ok = _run(assert_forum_owner(inter, {"forum_id": "42"}))
        self.assertFalse(ok)

    def test_owner_assertion_allows_owner(self):
        from discord_forum import assert_forum_owner
        import discord
        member = _FakeMember(999)
        forum = SimpleNamespace(overwrites={member: discord.PermissionOverwrite(view_channel=True)})
        guild = SimpleNamespace(
            get_channel=lambda _id: forum,
            get_member=lambda _id: member,
        )
        inter = _FakeInteraction(999, guild=guild)
        ok = _run(assert_forum_owner(inter, {"forum_id": "42"}))
        self.assertTrue(ok)


# ──────────────────────────────────────────────────────────────────────────────
# 0.6/0.7 — admin expiry-alerts dry run + multi-sig + reminder persistence
# ──────────────────────────────────────────────────────────────────────────────

class TestAdminExtensions(DbTestCase):

    def test_dry_run_expiry_alerts(self):
        cm.add_customer("700", "Soon", alt_count=1, vip=False, days=30)
        with cm._conn() as con:
            con.execute("UPDATE customers SET expiry_date = ? WHERE discord_id='700'",
                        (int(time.time()) + 2 * 86400,))
        report = _run(te.dry_run_expiry_alerts())
        self.assertGreaterEqual(len(report["reminders"]), 1)
        self.assertTrue(report["reminders"][0]["would_send"])

    def test_multi_sig_requires_second_admin(self):
        from security import MultiSigConfirm
        ms = MultiSigConfirm(window_sec=120)
        st1, _ = ms.request("kill", 1)
        self.assertEqual(st1, "initiated")
        st2, _ = ms.request("kill", 1)
        self.assertEqual(st2, "waiting")
        st3, _ = ms.request("kill", 2)
        self.assertEqual(st3, "confirmed")

    def test_multi_sig_expires(self):
        from security import MultiSigConfirm
        ms = MultiSigConfirm(window_sec=1)
        ms.request("kill", 1)
        time.sleep(1.1)
        st, _ = ms.request("kill", 2)
        self.assertEqual(st, "initiated")


# ──────────────────────────────────────────────────────────────────────────────
# 0.8 — curl_cffi loud failure (in send_ads import behavior)
# ──────────────────────────────────────────────────────────────────────────────

class TestCurlLoudFailure(unittest.TestCase):

    def test_missing_curl_cffi_raises_actionable_error(self):
        # Run in a fresh subprocess with a fake curl_cffi-less sys.path so the
        # module-level guard really executes (import caching would otherwise
        # reuse the already-loaded send_ads module).
        import subprocess
        code = (
            "import importlib.abc, sys\n"
            "sys.path.insert(0, '@ROOT@')\n"
            "sys.modules.pop('curl_cffi', None)\n"
            "sys.modules.pop('send_ads', None)\n"
            "class _Block(importlib.abc.MetaPathFinder):\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name == 'curl_cffi' or name.startswith('curl_cffi.'):\n"
            "            raise ImportError('blocked for test')\n"
            "        return None\n"
            "sys.meta_path.insert(0, _Block())\n"
            "try:\n"
            "    import send_ads\n"
            "    print('NO_ERROR')\n"
            "except RuntimeError as e:\n"
            "    print('RUNTIME_ERROR: ' + str(e))\n"
            "except Exception as e:\n"
            "    print('OTHER: ' + type(e).__name__ + ': ' + str(e))\n"
        ).replace("@ROOT@", str(ROOT))
        env = dict(os.environ)
        env.pop("ALLOW_REQUESTS_FALLBACK", None)
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, env=env, timeout=120)
        self.assertIn("RUNTIME_ERROR", out.stdout, out.stdout + out.stderr)
        self.assertIn("curl_cffi is REQUIRED", out.stdout, out.stdout + out.stderr)
        self.assertIn("pip install", out.stdout, out.stdout + out.stderr)


# ──────────────────────────────────────────────────────────────────────────────
# Phase 1 — payments, ban watch, auto-renew, metrics
# ──────────────────────────────────────────────────────────────────────────────

class TestPhase1Features(DbTestCase):

    def test_tx_hash_extraction_and_auto_ack(self):
        from control_bot import payments
        hashes = payments.extract_tx_hashes(
            "payment sent 0x" + "a" * 64 + " for 30 days thanks!")
        self.assertEqual(len(hashes), 1)

        class FakeChannel:
            def __init__(self):
                self.sent = []
            async def send(self, text):
                self.sent.append(text)

        ch = FakeChannel()
        res = _run(payments.maybe_auto_ack(ch, "777", "0x" + "b" * 64))
        self.assertTrue(res and res["hashes"])
        self.assertTrue(ch.sent)
        self.assertIn("received your transaction hash", ch.sent[0])
        # second hash for same customer is not re-acked
        ch2 = FakeChannel()
        _run(payments.maybe_auto_ack(ch2, "777", "0x" + "c" * 64))
        self.assertEqual(ch2.sent, [])

    def test_activation_template_is_prefilled(self):
        from control_bot.payments import activation_template
        c = {"discord_id": "555", "alt_count": 2, "vip": True, "expiry_date": int(time.time()) + 30 * 86400}
        tpl = activation_template(c, "0x" + "d" * 64)
        self.assertIn("/admin activate", tpl)
        self.assertIn("alts:2", tpl)
        self.assertIn("BSCScan", tpl)

    def test_ban_detection_markers(self):
        from control_bot.ban_watch import detect_ban_events
        self.assertTrue(detect_ban_events("🛑 CRITICAL: Token invalidated/revoked/banned (HTTP 403)"))
        self.assertTrue(detect_ban_events("marking DEAD"))
        self.assertFalse(detect_ban_events("posted successfully"))

    def test_ban_handler_posts_control_alert_and_logs(self):
        from control_bot import ban_watch

        class FakeChannel:
            def __init__(self):
                self.sent = []
            async def send(self, text, **kw):
                self.sent.append(text)

        class FakeBot:
            def __init__(self):
                self.chs = {}
            def get_channel(self, cid):
                return self.chs.get(cid)

        cm.add_customer("888", "BannedUser", alt_count=1, vip=False, days=30,
                        github_account="worker-org", repos=["cust_alt1"],
                        control_thread_id="1", logs_thread_id="2")
        bot = FakeBot()
        bot.chs[1] = FakeChannel()
        bot.chs[2] = FakeChannel()
        msg = SimpleNamespace(
            content="🛑 Token invalidated (HTTP 403). Aborting.",
            channel=SimpleNamespace(id=2),
            author=SimpleNamespace(id=1, bot=True),
        )
        with mock.patch.object(ban_watch, "_handle_banned_repos", return_value={"banned": "x"}):
            handled = _run(ban_watch.handle_ban_message(bot, msg, cm.get_customer("888")))
        self.assertTrue(handled)
        self.assertTrue(bot.chs[1].sent)
        self.assertIn("banned", bot.chs[1].sent[0])
        self.assertTrue(bot.chs[2].sent)
        events = cm.get_events("alt_banned", discord_id="888")
        self.assertEqual(len(events), 1)

    def test_auto_redispatch_after_48h(self):
        from control_bot import github_api as ga
        cust_id = "901"
        cm.add_customer(cust_id, "Auto", alt_count=1, vip=False, days=60,
                        control_thread_id="42")
        old = time.time() - 49 * 3600
        with cm._conn() as con:
            con.execute(
                "INSERT INTO run_state (discord_id, alt_index, mode, runtime_hours, "
                "started_at, last_dispatch_at, renewals, payload) VALUES (?,?,?,?,?,?,0,?)",
                (cust_id, 1, "limitless", 0, old, old, json.dumps({"ad_type": "sell"})),
            )

        class FakeBot:
            def get_channel(self, _cid):
                return None

        te._bot_ref = FakeBot()
        with mock.patch.object(ga, "_repo_for", return_value="worker-org/cust_alt1"), \
             mock.patch.object(ga, "list_runs", return_value=[{"status": "completed", "conclusion": "success"}]), \
             mock.patch.object(ga, "dispatch_workflow", return_value=(True, "dispatched")), \
             mock.patch.object(te, "_send_forum_message", new=mock.AsyncMock(return_value=True)):
            n = _run(te.auto_redispatch_loop_once())
        self.assertEqual(n, 1)
        states = cm.get_run_states(cust_id)
        self.assertEqual(states[0]["renewals"], 1)
        te._bot_ref = None

    def test_ttftv_and_metrics_calculation(self):
        from control_bot import metrics
        base = time.time() - 3600
        with cm._conn() as con:
            con.execute("INSERT INTO events (discord_id, event, ts, payload) VALUES ('1','ticket_open',?, '{}')", (base,))
            con.execute("INSERT INTO events (discord_id, event, ts, payload) VALUES ('1','first_successful_post',?, '{}')", (base + 1800,))
            con.execute("INSERT INTO events (discord_id, event, ts, payload) VALUES ('2','ticket_open',?, '{}')", (base,))
            con.execute("INSERT INTO events (discord_id, event, ts, payload) VALUES ('2','first_successful_post',?, '{}')", (base + 3600,))
        ttftv = metrics.compute_ttftv()
        self.assertAlmostEqual(ttftv, 45.0, delta=0.1)
        m = metrics.compute_watch_metrics()
        self.assertIsNone(m["median_alt_survival_days"])
        self.assertIn("reminders_7d", m)
        summary = metrics.compute_weekly_summary()
        self.assertIn("Weekly Operations Summary", summary)


# ──────────────────────────────────────────────────────────────────────────────
# Phase 2 — ops monitors
# ──────────────────────────────────────────────────────────────────────────────

class TestOpsMonitors(DbTestCase):

    def test_worker_token_health_classification(self):
        from control_bot import ops
        self.assertEqual(ops.classify_token_health({"ok": True, "days_left": 30}), "ok")
        self.assertEqual(ops.classify_token_health({"ok": True, "days_left": 5}), "warning")
        self.assertEqual(ops.classify_token_health({"ok": True, "days_left": 0.5}), "critical")
        self.assertEqual(ops.classify_token_health({"ok": False, "error": "401"}), "critical")

    def test_worker_token_statuses_uses_configured_tokens(self):
        from control_bot import ops
        with mock.patch("github_dispatch.list_worker_tokens",
                        return_value=[{"owner": "org-a", "token": "tok-a"}]):
            with mock.patch("github_dispatch.check_token_status",
                            return_value={"ok": True, "login": "bot", "days_left": 3.0}):
                res = ops.worker_token_statuses()
        self.assertEqual(res[0]["owner"], "org-a")
        self.assertEqual(ops.classify_token_health(res[0]), "warning")

    def test_rss_snapshot_shape(self):
        from control_bot import ops
        snap = ops.rss_snapshot()
        self.assertIn("rss_bytes", snap)
        self.assertIn("ts", snap)

    def test_heartbeat_payload_shape(self):
        from control_bot import ops
        payload = ops.heartbeat_payload()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["service"], "adfarm-control-bot")
        self.assertIn("uptime_sec", payload)

    def test_gist_usage_stats(self):
        from control_bot import github_api as ga
        with ga._gist_stats_lock:
            ga._gist_calls.clear()
        t0 = time.perf_counter()
        ga._gist_record(429, t0)
        ga._gist_record(200, t0)
        stats = ga.gist_usage_stats()
        self.assertEqual(stats["requests_last_hour"], 2)
        self.assertEqual(stats["429_count"], 1)

    def test_nightly_token_sweep_reports_invalid(self):
        from control_bot import ops
        cm.add_customer("31", "SweepMe", alt_count=1, vip=False, days=30)
        cm.store_alt_credential("31", 1, "bad-token", ["111111111111111111"], "Alt#1")
        with mock.patch.object(ops.github_api, "fetch_discord_user_profile", return_value=(False, {"error": "401"})):
            res = _run(ops.nightly_token_sweep(None))
        self.assertEqual(res["checked"], 1)
        self.assertEqual(res["invalid"], 1)
        self.assertEqual(len(cm.get_events("nightly_token_sweep")), 1)

    def test_tune_hint_only_after_day_3(self):
        from control_bot import ops

        class FakeChannel:
            def __init__(self):
                self.sent = []
            async def send(self, text, **kw):
                self.sent.append(text)

        class FakeBot:
            def __init__(self):
                self.chs = {}
            def get_channel(self, cid):
                return self.chs.get(cid)

        cm.add_customer("41", "OldCust", alt_count=1, vip=False, days=30, control_thread_id="7")
        with cm._conn() as con:
            con.execute("UPDATE customers SET start_date = ? WHERE discord_id='41'",
                        (int(time.time()) - 4 * 86400,))
        bot = FakeBot()
        bot.chs[7] = FakeChannel()
        posted = _run(ops.post_tune_hints(bot))
        self.assertEqual(posted, 1)
        self.assertTrue(bot.chs[7].sent)
        posted2 = _run(ops.post_tune_hints(bot))
        self.assertEqual(posted2, 0)  # deduped via events

    def test_missed_external_beat_alert(self):
        from control_bot import ops
        ops._last_external_beat = time.time() - 16 * 60
        with mock.patch("control_bot.alerts.post_admin_alert", new=mock.AsyncMock(return_value=True)) as m:
            _run(ops.check_missed_external_beat(None))
        m.assert_awaited()


# ──────────────────────────────────────────────────────────────────────────────
# Phase 3 — proofs + soft-delete placeholders
# ──────────────────────────────────────────────────────────────────────────────

class TestPhase3(DbTestCase):

    def test_proof_redaction(self):
        from control_bot.proofs import redact
        out = redact("customer 123456789012345678 posted a win", "123456789012345678")
        self.assertNotIn("123456789012345678", out)
        self.assertIn("1234…", out)


if __name__ == "__main__":
    unittest.main()
