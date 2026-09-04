"""Regression tests for the five V8 bug-fix plan items.

  #1  Alt repos must be created in WORKER GitHub accounts (round-robin),
      never silently in the main account (github_dispatch / github_api /
      AltAddModal owner pick / ``/admin activate`` hard refusal / workflow env).
  #2  "❌ Ticket channel not configured." — ticket channel resolution chain
      (env → customers.db meta → panel channel → guild name search, with
      stale-id fall-through) + persistence by ``/admin ticket-panel``.
  #3  The cancelled 3-min token walkthrough video must be gone everywhere
      (bot embeds, policy module, welcome DM, docs, guide file deleted).
  #4  ``/alt`` must only ever show alts belonging to the invoking customer
      (ownership resolved from their customers.db record).
  #5  VIP DM auto-reply: schema v3 ``autoreply_text`` + migration, helpers,
      ``/vip autoreply`` command, and the #dm-inbox watcher relay.

All tests are network-free: GitHub HTTP, Discord I/O and the Gist backup
worker are mocked or bypassed via temporary databases.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import customer_manager as cm
import github_dispatch
import yaml
from control_bot import bot as control_bot_module
from control_bot import config
from control_bot import tickets as tickets_module
from control_bot.alt_state import AltStateManager

import admin_commands
import security as security_module
from control_bot import github_api
from control_bot import policy as policy_module

ROOT = Path(__file__).resolve().parents[1]

_NO_WORKER_ENV = {
    "WORKER_TOKENS": "",
    "WORKER_GITHUB_OWNERS": "",
    "WORKER_TOKENS_LIST": "",
    "WORKER_1_USER": "",
    "WORKER_1_TOKEN": "",
    "WORKER_2_USER": "",
    "WORKER_2_TOKEN": "",
    "WORKER_3_USER": "",
    "WORKER_3_TOKEN": "",
    "OPEN_TICKET_CH_ID": "",
    "TICKET_CH_ID": "",
    "DM_INBOX_CH_ID": "",
}


# ─────────────────────────────────────────────────────────────────────────────
# Fakes (same shape as tests/test_plan_features.py)
# ─────────────────────────────────────────────────────────────────────────────

class _Response:
    def __init__(self):
        self.deferred = False
        self.messages = []

    def is_done(self):
        return self.deferred

    async def send_message(self, *args, **kwargs):
        self.messages.append((args, kwargs))
        self.deferred = True

    async def defer(self, **kwargs):
        self.deferred = True

    async def edit_message(self, *args, **kwargs):
        self.messages.append((args, kwargs))
        self.deferred = True

    async def send_modal(self, *args, **kwargs):
        self.messages.append((args, kwargs))
        self.deferred = True


class _Followup:
    def __init__(self):
        self.messages = []

    async def send(self, *args, **kwargs):
        self.messages.append((args, kwargs))


def _render(obj):
    """Flatten a message payload (string or discord Embed) into text."""
    if hasattr(obj, "fields") and hasattr(obj, "description") and hasattr(obj, "title"):
        parts = [str(obj.title or ""), str(obj.description or "")]
        for field in getattr(obj, "fields", None) or []:
            parts.append(f"{getattr(field, 'name', '')} {getattr(field, 'value', '')}")
        footer = getattr(obj, "footer", None)
        parts.append(str(getattr(footer, "text", "") or ""))
        return "\n".join(parts)
    return str(obj)


class _Interaction:
    def __init__(self, user_id=42):
        self.user = SimpleNamespace(id=user_id)
        self.response = _Response()
        self.followup = _Followup()

    def text(self):
        out = []
        for args, kwargs in self.response.messages + self.followup.messages:
            for a in args:
                out.append(_render(a))
            for key in ("embed", "embeds", "content"):
                value = kwargs.get(key)
                if value is None:
                    continue
                if isinstance(value, (list, tuple)):
                    out.extend(_render(v) for v in value)
                else:
                    out.append(_render(value))
        return "\n".join(out)

    def embeds(self):
        out = []
        for args, kwargs in self.response.messages + self.followup.messages:
            for a in args:
                if hasattr(a, "fields") and hasattr(a, "description"):
                    out.append(a)
            if "embed" in kwargs and kwargs["embed"] is not None:
                out.append(kwargs["embed"])
        return out


def _run(coro):
    return asyncio.run(coro)


class _TempDBTestCase(unittest.TestCase):
    """Base: fresh temporary customers.db for every test."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_db_path = cm.DB_PATH
        cm.DB_PATH = os.path.join(self._tmpdir.name, "customers-test.db")
        cm.init_db()
        self.addCleanup(self._restore_db)

    def _restore_db(self):
        cm.DB_PATH = self._orig_db_path
        self._tmpdir.cleanup()


# ─────────────────────────────────────────────────────────────────────────────
# #1 — Worker account round-robin (github_dispatch)
# ─────────────────────────────────────────────────────────────────────────────

class WorkerRoundRobinTests(unittest.TestCase):
    def setUp(self):
        github_dispatch._worker_index = 0
        self.addCleanup(setattr, github_dispatch, "_worker_index", 0)

    def test_get_workers_parses_worker_tokens(self):
        with mock.patch.dict(os.environ, {**_NO_WORKER_ENV, "WORKER_TOKENS": "org-a:pat-a, org-b:pat-b"}):
            workers = github_dispatch.get_workers()
        self.assertEqual(workers, [("org-a", "pat-a"), ("org-b", "pat-b")])

    def test_get_workers_fallback_worker_n_pairs(self):
        env = {**_NO_WORKER_ENV, "WORKER_2_USER": "worker-two", "WORKER_2_TOKEN": "pat-2"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(github_dispatch.get_workers(), [("worker-two", "pat-2")])

    def test_get_workers_fallback_owner_list(self):
        env = {
            **_NO_WORKER_ENV,
            "WORKER_GITHUB_OWNERS": "org-x,org-y",
            "WORKER_TOKENS_LIST": "tok-x,tok-y",
        }
        with mock.patch.dict(os.environ, env):
            # get_workers() only reads WORKER_TOKENS / WORKER_N_*; token_for_owner
            # is the consumer of the positional lists.
            self.assertEqual(github_dispatch.token_for_owner("org-y"), "tok-y")
            self.assertEqual(github_dispatch.token_for_owner("org-x"), "tok-x")

    def test_pick_worker_round_robins(self):
        with mock.patch.dict(os.environ, {**_NO_WORKER_ENV, "WORKER_TOKENS": "org-a:pat-a,org-b:pat-b"}):
            picks = [github_dispatch.pick_worker() for _ in range(5)]
        self.assertEqual(picks[0], ("org-a", "pat-a"))
        self.assertEqual(picks[1], ("org-b", "pat-b"))
        self.assertEqual(picks[2], ("org-a", "pat-a"))
        self.assertEqual(picks[3], ("org-b", "pat-b"))
        self.assertEqual(picks[4], ("org-a", "pat-a"))

    def test_pick_worker_without_workers_reports_empty_user(self):
        """Empty username is the signal for the loud refusal in /admin activate."""
        with mock.patch.dict(os.environ, {**_NO_WORKER_ENV, "GH_ADMIN_TOKEN": "main-pat"}):
            user, token = github_dispatch.pick_worker()
        self.assertEqual(user, "")
        self.assertEqual(token, "main-pat")

    def test_discover_repo_owner_finds_worker_account(self):
        with mock.patch.dict(os.environ, {**_NO_WORKER_ENV, "WORKER_TOKENS": "org-a:pat-a,org-b:pat-b"}), \
             mock.patch.object(github_dispatch, "repo_exists",
                               side_effect=lambda owner, repo, token=None: owner == "org-b"):
            self.assertEqual(github_dispatch.discover_repo_owner("alt7-buyer"), "org-b")

    def test_discover_repo_owner_falls_back_to_main(self):
        def fake_exists(owner, repo, token=None):
            return owner == "main-login"
        with mock.patch.dict(os.environ, {**_NO_WORKER_ENV, "WORKER_TOKENS": "org-a:pat-a", "GH_ADMIN_TOKEN": "main-pat"}), \
             mock.patch.object(github_dispatch, "repo_exists", side_effect=fake_exists), \
             mock.patch.object(github_dispatch, "_resolve_owner", return_value="main-login"):
            self.assertEqual(github_dispatch.discover_repo_owner("legacy-repo"), "main-login")

    def test_discover_repo_owner_empty_when_nowhere(self):
        with mock.patch.dict(os.environ, {**_NO_WORKER_ENV, "WORKER_TOKENS": "org-a:pat-a"}), \
             mock.patch.object(github_dispatch, "repo_exists", return_value=False), \
             mock.patch.object(github_dispatch, "_resolve_owner", return_value="main-login"):
            self.assertEqual(github_dispatch.discover_repo_owner("ghost-repo"), "")

    def test_token_for_owner_prefers_worker_pat(self):
        with mock.patch.dict(os.environ, {**_NO_WORKER_ENV, "WORKER_TOKENS": "org-a:pat-a", "GH_ADMIN_TOKEN": "main-pat"}):
            self.assertEqual(github_dispatch.token_for_owner("org-a"), "pat-a")
            self.assertEqual(github_dispatch.token_for_owner("someone-else"), "main-pat")


class WorkerTokenRoutingTests(unittest.TestCase):
    """github_api must authenticate worker-owned repos with the worker PAT."""

    def test_token_for_repo_matches_worker_forms(self):
        env = {
            **_NO_WORKER_ENV,
            "WORKER_TOKENS": "org-a:pat-a",
            "WORKER_GITHUB_OWNERS": "org-x,org-y",
            "WORKER_TOKENS_LIST": "tok-x,tok-y",
            "WORKER_1_USER": "solo-worker",
            "WORKER_1_TOKEN": "pat-solo",
        }
        with mock.patch.dict(os.environ, env):
            self.assertEqual(github_api._token_for_repo("org-a/repo"), "pat-a")
            self.assertEqual(github_api._token_for_repo("org-y/repo"), "tok-y")
            self.assertEqual(github_api._token_for_repo("solo-worker/repo"), "pat-solo")
            self.assertIsNone(github_api._token_for_repo("main-org/repo"))
            self.assertIsNone(github_api._token_for_repo("no-slug"))
            self.assertIsNone(github_api._token_for_repo(""))

    def test_auth_headers_prefer_worker_token(self):
        with mock.patch.dict(os.environ, {**_NO_WORKER_ENV, "WORKER_TOKENS": "org-a:pat-a"}), \
             mock.patch.object(config, "GITHUB_TOKEN", "main-pat"):
            self.assertEqual(github_api._auth_headers("org-a/alt1-x")["Authorization"], "Bearer pat-a")
            self.assertEqual(github_api._auth_headers("main-org/alt2")["Authorization"], "Bearer main-pat")
            self.assertEqual(github_api._auth_headers()["Authorization"], "Bearer main-pat")


class AltAddOwnerPickTests(unittest.TestCase):
    """AltAddModal auto-created repos must land in a worker account."""

    def setUp(self):
        github_dispatch._worker_index = 0
        self.addCleanup(setattr, github_dispatch, "_worker_index", 0)

    def test_pick_fleet_repo_owner_round_robins_workers(self):
        with mock.patch.dict(os.environ, {**_NO_WORKER_ENV, "WORKER_TOKENS": "org-a:pat-a,org-b:pat-b"}):
            first = control_bot_module._pick_fleet_repo_owner()
            second = control_bot_module._pick_fleet_repo_owner()
        self.assertIn(first, {"org-a", "org-b"})
        self.assertIn(second, {"org-a", "org-b"})
        self.assertNotEqual(first, second)

    def test_pick_fleet_repo_owner_falls_back_without_workers(self):
        with mock.patch.dict(os.environ, {**_NO_WORKER_ENV, "GH_ADMIN_TOKEN": ""}), \
             mock.patch.object(config, "GITHUB_OWNER", "fleet-owner"), \
             mock.patch.object(config, "CORE_REPO", "fleet-owner/core"):
            self.assertEqual(control_bot_module._pick_fleet_repo_owner(), "fleet-owner")


class ActivateRefusalTests(_TempDBTestCase):
    """/admin activate must REFUSE to create repos in the main account."""

    def _activate(self, inter, user_id=999, alts=2, github_account=""):
        cog_self = mock.MagicMock()
        cog_self._audit = mock.AsyncMock()
        user = SimpleNamespace(id=user_id, display_name="Buyer999", mention=f"<@{user_id}>")
        with mock.patch.object(admin_commands, "check_admin", new=mock.AsyncMock(return_value=True)), \
             mock.patch.object(github_dispatch, "provision_alt_repo", new=mock.MagicMock()) as prov:
            _run(admin_commands.AdminCog.admin_activate.callback(
                cog_self, inter, user=user, days=30, alts=alts, vip=False,
                github_account=github_account,
            ))
        return prov

    def test_refuses_without_workers_and_creates_nothing(self):
        inter = _Interaction(user_id=42)
        with mock.patch.dict(os.environ, {**_NO_WORKER_ENV, "GH_ADMIN_TOKEN": "main-pat"}):
            prov = self._activate(inter)
        text = inter.text()
        self.assertIn("No worker GitHub accounts are configured", text)
        self.assertIn("WORKER_TOKENS", text)
        prov.assert_not_called()

    def test_refusal_is_ephemeral_and_audited(self):
        inter = _Interaction(user_id=42)
        with mock.patch.dict(os.environ, {**_NO_WORKER_ENV, "GH_ADMIN_TOKEN": "main-pat"}):
            cog_self = mock.MagicMock()
            cog_self._audit = mock.AsyncMock()
            user = SimpleNamespace(id=999, display_name="Buyer999", mention="<@999>")
            with mock.patch.object(admin_commands, "check_admin", new=mock.AsyncMock(return_value=True)):
                _run(admin_commands.AdminCog.admin_activate.callback(
                    cog_self, inter, user=user, days=30, alts=1, vip=False, github_account="",
                ))
            cog_self._audit.assert_awaited()
            self.assertIn("Activation blocked", cog_self._audit.await_args.args[0])


class WorkflowEnvTests(unittest.TestCase):
    """The workflow must actually PASS the worker/ticket secrets to the bot."""

    def _envs(self, path):
        data = yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))
        envs = []
        for job in (data.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                if isinstance(step.get("env"), dict):
                    envs.append(step["env"])
        return envs

    def test_control_bot_workflow_passes_worker_and_ticket_env(self):
        envs = self._envs(".github/workflows/control_bot.yml")
        merged = {}
        for env in envs:
            merged.update(env)
        required = [
            "WORKER_TOKENS", "WORKER_GITHUB_OWNERS", "WORKER_TOKENS_LIST",
            "WORKER_1_USER", "WORKER_1_TOKEN",
            "WORKER_2_USER", "WORKER_2_TOKEN",
            "WORKER_3_USER", "WORKER_3_TOKEN",
            "OPEN_TICKET_CH_ID", "ADMIN_ALERTS_CH_ID", "ADMIN_CHAT_CH_ID",
            "PAYMENT_ADDRESS", "DM_INBOX_CH_ID",
        ]
        for key in required:
            self.assertIn(key, merged, f"{key} missing from control_bot.yml env")
            self.assertIn("secrets.", str(merged[key]))
        # GITHUB_OWNER must fall back to the setup.py-written REPO_OWNER secret
        self.assertEqual(
            str(merged["GITHUB_OWNER"]),
            "${{ secrets.ALT_GITHUB_OWNER || secrets.REPO_OWNER }}",
        )
        # AUDIT_LOG_CH_ID must accept both secret spellings
        self.assertIn("AUDIT_LOGS_CH_ID", str(merged["AUDIT_LOG_CH_ID"]))

    def test_staging_workflow_mirrors_worker_env(self):
        merged = {}
        for env in self._envs(".github/workflows/control_bot_staging.yml"):
            merged.update(env)
        for key in ("WORKER_TOKENS", "OPEN_TICKET_CH_ID", "GITHUB_OWNER"):
            self.assertIn(key, merged)
        self.assertEqual(
            str(merged["GITHUB_OWNER"]),
            "${{ secrets.ALT_GITHUB_OWNER || secrets.REPO_OWNER }}",
        )


# ─────────────────────────────────────────────────────────────────────────────
# #2 — Ticket channel resolution
# ─────────────────────────────────────────────────────────────────────────────

class TicketChannelResolutionTests(_TempDBTestCase):
    def _guild(self, ticket_ch_id=444):
        return SimpleNamespace(channels=[
            SimpleNamespace(id=111, name="general"),
            SimpleNamespace(id=ticket_ch_id, name="open-ticket"),
        ])

    def test_candidates_order_env_meta_panel_guild(self):
        cm.set_meta("open_ticket_ch_id", "222")
        with mock.patch.dict(os.environ, {**_NO_WORKER_ENV, "OPEN_TICKET_CH_ID": "111"}):
            got = tickets_module.resolve_ticket_channel_candidates(self._guild(), panel_channel_id="333")
        self.assertEqual(got, [111, 222, 333, 444])

    def test_candidates_meta_when_env_missing(self):
        cm.set_meta("open_ticket_ch_id", "222")
        with mock.patch.dict(os.environ, _NO_WORKER_ENV):
            got = tickets_module.resolve_ticket_channel_candidates(self._guild())
        self.assertEqual(got, [222, 444])

    def test_candidates_guild_name_search_last_resort(self):
        with mock.patch.dict(os.environ, _NO_WORKER_ENV):
            got = tickets_module.resolve_ticket_channel_candidates(self._guild(ticket_ch_id=555))
        self.assertEqual(got, [555])
        self.assertEqual(tickets_module.resolve_ticket_channel_id(self._guild(ticket_ch_id=555)), 555)

    def test_no_candidates_returns_none(self):
        with mock.patch.dict(os.environ, _NO_WORKER_ENV):
            self.assertIsNone(tickets_module.resolve_ticket_channel_id(None))

    def test_resolve_ticket_channel_skips_stale_ids(self):
        """A stale env id must fall through to the live guild channel."""
        guild = self._guild(ticket_ch_id=444)
        live_channel = SimpleNamespace(id=444, name="open-ticket")
        guild.get_channel = mock.Mock(side_effect=lambda cid: live_channel if cid == 444 else None)
        guild.fetch_channel = mock.AsyncMock(side_effect=Exception("Unknown Channel"))
        with mock.patch.dict(os.environ, {**_NO_WORKER_ENV, "OPEN_TICKET_CH_ID": "111"}):
            channel = _run(tickets_module._resolve_ticket_channel(guild))
        self.assertIs(channel, live_channel)

    def test_resolve_ticket_channel_all_stale_returns_none(self):
        guild = self._guild()
        guild.get_channel = mock.Mock(return_value=None)
        guild.fetch_channel = mock.AsyncMock(side_effect=Exception("Unknown Channel"))
        with mock.patch.dict(os.environ, {**_NO_WORKER_ENV, "OPEN_TICKET_CH_ID": "111"}):
            channel = _run(tickets_module._resolve_ticket_channel(guild))
        self.assertIsNone(channel)

    def test_remember_ticket_channel_persists_env_and_meta(self):
        with mock.patch.dict(os.environ, _NO_WORKER_ENV):
            admin_commands._remember_ticket_channel(SimpleNamespace(id=777))
            self.assertEqual(os.environ.get("OPEN_TICKET_CH_ID"), "777")
            self.assertEqual(cm.get_meta("open_ticket_ch_id", ""), "777")
            # ...and the resolver chain now finds it without any guild.
            self.assertEqual(tickets_module.resolve_ticket_channel_id(None), 777)

    def test_create_ticket_thread_signature_accepts_panel_channel(self):
        """The panel-channel fallback must be threadable end-to-end."""
        import inspect
        sig = inspect.signature(tickets_module.create_ticket_thread)
        self.assertIn("panel_channel_id", sig.parameters)
        for modal_cls in (tickets_module.PaymentModal, tickets_module.BugReportModal,
                          tickets_module.SuggestionModal):
            self.assertIn("panel_channel_id", inspect.signature(modal_cls.__init__).parameters)


# ─────────────────────────────────────────────────────────────────────────────
# #3 — Token video removed everywhere
# ─────────────────────────────────────────────────────────────────────────────

class VideoRemovalTests(unittest.TestCase):
    def test_policy_module_has_no_video_constants(self):
        self.assertFalse(hasattr(policy_module, "SETUP_VIDEO_URL"))
        self.assertFalse(hasattr(policy_module, "VIDEO_BUTTON_LABEL"))

    def test_bot_module_has_no_video_button_view(self):
        self.assertFalse(hasattr(control_bot_module, "_VideoButton"))

    def test_sources_contain_no_video_links(self):
        for rel in ("control_bot/bot.py", "admin_commands.py", "control_bot/policy.py"):
            src = (ROOT / rel).read_text(encoding="utf-8")
            # Drop full-line comments: the deliberate "V8 bug-fix (plan #3)"
            # notes explaining the removal may mention the old constant names.
            code = "\n".join(
                line for line in src.splitlines() if not line.lstrip().startswith("#")
            )
            for needle in ("SETUP_VIDEO_URL", "VIDEO_BUTTON_LABEL", "Watch the 3-min", "🎥"):
                self.assertNotIn(needle, code, f"{needle} still present in {rel}")

    def test_token_extraction_guide_deleted(self):
        self.assertFalse((ROOT / "docs" / "TOKEN_EXTRACTION_GUIDE.md").exists())

    def test_docs_mention_no_video(self):
        for rel in ("docs/SETUP_GUIDE.md", "SKILL.md"):
            src = (ROOT / rel).read_text(encoding="utf-8")
            self.assertNotIn("3-min video", src, f"video reference still in {rel}")
            self.assertNotIn("video walkthrough", src.lower())
            self.assertNotIn("🎥", src)

    def test_getstarted_guide_has_no_video(self):
        manager = AltStateManager({1: "Alt 1"}, alt_ids=[1])
        inter = _Interaction(user_id=42)
        with mock.patch.object(control_bot_module, "state", manager), \
             mock.patch.object(config, "OWNER_IDS", {42}), \
             mock.patch.object(security_module, "OWNER_IDS", {42}):
            _run(control_bot_module.cmd_getstarted.callback(inter))
        blob = inter.text().lower()
        self.assertNotIn("video", blob)
        self.assertNotIn("🎥", blob)
        self.assertIn("get started", blob)


# ─────────────────────────────────────────────────────────────────────────────
# #4 — /alt visibility filtering
# ─────────────────────────────────────────────────────────────────────────────

class AltVisibilityTests(_TempDBTestCase):
    CUSTOMER_UID = 999
    EMPTY_UID = 888

    def setUp(self):
        super().setUp()
        cm.add_customer(
            discord_id=str(self.CUSTOMER_UID), discord_username="buyer999",
            alt_count=2, vip=True, days=30,
            github_account="org-a", repos=["org-a/alt1-buyer999"],
        )
        cm.add_customer(
            discord_id=str(self.EMPTY_UID), discord_username="fresh888",
            alt_count=1, vip=False, days=30, github_account="org-b", repos=[],
        )
        self.manager = AltStateManager({1: "SellerOne", 2: "OwnerAlt"}, alt_ids=[1, 2])
        patches = [
            mock.patch.object(control_bot_module, "state", self.manager),
            mock.patch.object(config, "OWNER_IDS", {42}),
            mock.patch.object(security_module, "OWNER_IDS", {42}),
            mock.patch.object(config, "ALT_REPOS", {1: "org-a/alt1-buyer999", 2: "main-org/alt2-owner"}),
            mock.patch.object(config, "ALT_DISCORD_IDS", {1: 0, 2: 0}),
            mock.patch.dict(os.environ, _NO_WORKER_ENV),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        control_bot_module._cooldowns.clear()

    def test_owned_ids_via_repo_basename(self):
        self.assertEqual(
            control_bot_module._customer_owned_alt_ids(self.CUSTOMER_UID), {1},
        )

    def test_owned_ids_via_discord_id(self):
        with mock.patch.object(config, "ALT_DISCORD_IDS", {2: self.EMPTY_UID}):
            self.assertEqual(control_bot_module._customer_owned_alt_ids(self.EMPTY_UID), {2})

    def test_owned_ids_via_setup_credential_username(self):
        cm.store_alt_credential(str(self.EMPTY_UID), 1, "tok", [], username="OwnerAlt")
        self.assertEqual(control_bot_module._customer_owned_alt_ids(self.EMPTY_UID), {2})

    def test_owned_ids_unknown_customer(self):
        self.assertEqual(control_bot_module._customer_owned_alt_ids(123456), set())

    def test_visible_alt_ids_customer_vs_admin(self):
        is_admin, visible = control_bot_module._visible_alt_ids(self.CUSTOMER_UID)
        self.assertFalse(is_admin)
        self.assertEqual(visible, [1])
        is_admin, visible = control_bot_module._visible_alt_ids(42)
        self.assertTrue(is_admin)
        self.assertEqual(sorted(visible), [1, 2])

    def test_overview_embed_filtered_for_customer(self):
        embed = control_bot_module._build_alt_overview_embed(1, allowed_ids=[1])
        self.assertIn("SellerOne", embed.description)
        self.assertNotIn("OwnerAlt", embed.description)
        self.assertIn("belong to your account", embed.footer.text)

    def test_overview_embed_full_fleet_for_admin(self):
        embed = control_bot_module._build_alt_overview_embed(1, allowed_ids=None)
        self.assertIn("SellerOne", embed.description)
        self.assertIn("OwnerAlt", embed.description)

    def test_cmd_alt_rejects_foreign_alt(self):
        inter = _Interaction(user_id=self.CUSTOMER_UID)
        _run(control_bot_module.cmd_alt.callback(inter, action="logs", alt=2))
        self.assertIn("does not belong to your account", inter.text())

    def test_cmd_alt_customer_sees_only_own_in_hub(self):
        inter = _Interaction(user_id=self.CUSTOMER_UID)
        _run(control_bot_module.cmd_alt.callback(inter, action="overview"))
        blob = inter.text()
        self.assertIn("SellerOne", blob)
        self.assertNotIn("OwnerAlt", blob)

    def test_cmd_alt_empty_fleet_points_to_setup(self):
        inter = _Interaction(user_id=self.EMPTY_UID)
        _run(control_bot_module.cmd_alt.callback(inter, action="overview"))
        blob = inter.text()
        self.assertIn("Your Alt Accounts", blob)
        self.assertIn("/setup", blob)
        self.assertNotIn("SellerOne", blob)
        self.assertNotIn("OwnerAlt", blob)

    def test_alt_autocompleter_filters_by_invoker(self):
        completer = control_bot_module.make_alt_autocompleter("alt")
        inter = _Interaction(user_id=self.CUSTOMER_UID)
        choices = _run(completer(inter, ""))
        self.assertEqual([c.value for c in choices], [1])
        admin_inter = _Interaction(user_id=42)
        admin_choices = _run(completer(admin_inter, ""))
        self.assertEqual(sorted(c.value for c in admin_choices), [1, 2])


# ─────────────────────────────────────────────────────────────────────────────
# #5 — VIP DM auto-reply
# ─────────────────────────────────────────────────────────────────────────────

class AutoReplyStorageTests(_TempDBTestCase):
    def setUp(self):
        super().setUp()
        cm.add_customer("999", "buyer999", alt_count=1, vip=True, days=30)

    def test_schema_version_is_3(self):
        self.assertEqual(cm.SCHEMA_VERSION, 3)
        self.assertEqual(cm.MAX_AUTOREPLY_CHARS, 1500)

    def test_set_get_disable_autoreply(self):
        self.assertTrue(cm.set_autoreply("999", "  Thanks for your DM!  "))
        self.assertEqual(cm.get_autoreply("999"), "Thanks for your DM!")
        self.assertTrue(cm.set_autoreply("999", ""))
        self.assertEqual(cm.get_autoreply("999"), "")

    def test_set_autoreply_truncates_to_limit(self):
        cm.set_autoreply("999", "x" * 4000)
        self.assertEqual(len(cm.get_autoreply("999")), cm.MAX_AUTOREPLY_CHARS)

    def test_set_autoreply_unknown_customer(self):
        self.assertFalse(cm.set_autoreply("000", "hi"))

    def test_migration_upgrades_pre_v3_database(self):
        """A customers.db written by schema v2 (e.g. restored from an older
        Gist backup) must gain the autoreply_text column without data loss."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        old_path = os.path.join(tmp, "old-customers.db")
        now = int(time.time())
        con = sqlite3.connect(old_path)
        con.execute("PRAGMA user_version = 2")
        con.execute("""
            CREATE TABLE customers (
                discord_id          TEXT PRIMARY KEY,
                discord_username    TEXT,
                alt_count           INTEGER DEFAULT 1,
                vip                 BOOLEAN DEFAULT 0,
                start_date          INTEGER,
                expiry_date         INTEGER,
                active              INTEGER DEFAULT 1,
                github_account      TEXT,
                repos               TEXT,
                forum_id            TEXT,
                control_thread_id   TEXT,
                dashboard_thread_id TEXT,
                logs_thread_id      TEXT,
                dm_thread_id        TEXT,
                deals_thread_id     TEXT
            )
        """)
        con.execute(
            "INSERT INTO customers (discord_id, discord_username, alt_count, vip,"
            " start_date, expiry_date, active, repos) VALUES (?,?,?,?,?,?,1,'[]')",
            ("555", "legacy", 2, 1, now, now + 30 * 86400),
        )
        con.commit()
        con.close()

        orig = cm.DB_PATH
        cm.DB_PATH = old_path
        self.addCleanup(setattr, cm, "DB_PATH", orig)
        cm.init_db()  # must migrate in place

        cust = cm.get_customer("555")
        self.assertIsNotNone(cust)
        self.assertEqual(cust["discord_username"], "legacy")
        self.assertEqual(cm.get_autoreply("555"), "")
        self.assertTrue(cm.set_autoreply("555", "migrated ok"))
        self.assertEqual(cm.get_autoreply("555"), "migrated ok")


class VipAutoReplyCommandTests(_TempDBTestCase):
    def setUp(self):
        super().setUp()
        cm.add_customer("999", "buyer999", alt_count=1, vip=True, days=30)
        cm.add_customer("777", "normie777", alt_count=1, vip=False, days=30)
        patches = [
            mock.patch.object(config, "OWNER_IDS", {42}),
            mock.patch.object(security_module, "OWNER_IDS", {42}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_set_then_view_then_disable(self):
        inter = _Interaction(user_id=999)
        _run(control_bot_module.vip_autoreply.callback(inter, message="Away right now — leave your offer!"))
        self.assertIn("VIP auto-reply saved", inter.text())
        self.assertEqual(cm.get_autoreply("999"), "Away right now — leave your offer!")

        view_inter = _Interaction(user_id=999)
        _run(control_bot_module.vip_autoreply.callback(view_inter, message=None))
        blob = view_inter.text()
        self.assertIn("Enabled", blob)
        self.assertIn("Away right now", blob)

        off_inter = _Interaction(user_id=999)
        _run(control_bot_module.vip_autoreply.callback(off_inter, message="off"))
        self.assertIn("disabled", off_inter.text().lower())
        self.assertEqual(cm.get_autoreply("999"), "")

    def test_view_when_disabled(self):
        inter = _Interaction(user_id=999)
        _run(control_bot_module.vip_autoreply.callback(inter, message=""))
        self.assertIn("Disabled", inter.text())

    def test_rejects_overlong_message(self):
        inter = _Interaction(user_id=999)
        _run(control_bot_module.vip_autoreply.callback(inter, message="x" * 1501))
        self.assertIn("too long", inter.text())
        self.assertEqual(cm.get_autoreply("999"), "")

    def test_sanitizes_mass_mentions(self):
        inter = _Interaction(user_id=999)
        _run(control_bot_module.vip_autoreply.callback(inter, message="@everyone buy now @here"))
        stored = cm.get_autoreply("999")
        self.assertNotIn("@everyone", stored)
        self.assertNotIn("@here", stored)
        self.assertIn("(mention:everyone)", stored)
        self.assertIn("(mention:here)", stored)

    def test_non_vip_denied(self):
        inter = _Interaction(user_id=777)
        _run(control_bot_module.vip_autoreply.callback(inter, message="sneaky"))
        self.assertIn("requires VIP", inter.text())
        self.assertEqual(cm.get_autoreply("777"), "")

    def test_unknown_user_denied(self):
        inter = _Interaction(user_id=31337)
        _run(control_bot_module.vip_autoreply.callback(inter, message="hello"))
        self.assertIn("active subscription", inter.text())

    def test_vip_registered_in_roles_guide_and_help(self):
        self.assertIn("vip", control_bot_module.ROLE_VIP_COMMANDS)
        self.assertIn("vip", control_bot_module._COMMAND_GUIDE)
        manager = AltStateManager({1: "Alt 1"}, alt_ids=[1])
        inter = _Interaction(user_id=42)
        with mock.patch.object(control_bot_module, "state", manager), \
             mock.patch.object(config, "OWNER_IDS", {42}), \
             mock.patch.object(security_module, "OWNER_IDS", {42}):
            _run(control_bot_module.cmd_help.callback(inter))
        blob = inter.text()
        self.assertIn("VIP Features", blob)
        self.assertIn("`/vip`", blob)
        self.assertIn("VIP DM auto-reply", blob)


def _dm_inbox_message(channel_id=4242, author="BuyerRelay", alt=1, buyer="5551234"):
    embed = SimpleNamespace(
        title="💬 New DM",
        fields=[SimpleNamespace(
            name="⚡ Quick Reply Command",
            value=f"/reply alt:{alt} user:{buyer} text:hello there",
        )],
        footer=SimpleNamespace(text=f"Alt {alt}: SellerOne"),
    )
    return SimpleNamespace(
        author=SimpleNamespace(name=author, display_name=author),
        embeds=[embed],
        channel=SimpleNamespace(id=channel_id, name="dm-inbox"),
        content="",
    )


class VipAutoReplyWatcherTests(_TempDBTestCase):
    def setUp(self):
        super().setUp()
        cm.add_customer(
            "999", "buyer999", alt_count=1, vip=True, days=30,
            github_account="org-a", repos=["org-a/alt1-buyer999"],
        )
        cm.add_customer("777", "normie777", alt_count=1, vip=False, days=30,
                        github_account="org-a", repos=["org-a/alt9-normie777"])
        cm.set_autoreply("999", "Away right now — leave your offer!")
        self.manager = AltStateManager({1: "SellerOne", 9: "NormieAlt"}, alt_ids=[1, 9])
        control_bot_module._autoreply_last_sent.clear()
        patches = [
            mock.patch.object(control_bot_module, "state", self.manager),
            mock.patch.object(config, "OWNER_IDS", {42}),
            mock.patch.object(security_module, "OWNER_IDS", {42}),
            mock.patch.object(config, "ALT_REPOS", {1: "org-a/alt1-buyer999", 9: "org-a/alt9-normie777"}),
            mock.patch.object(config, "ALT_DISCORD_IDS", {}),
            mock.patch.object(config, "DM_INBOX_CH_ID", 4242),
            mock.patch.object(control_bot_module, "_log_control", new=mock.AsyncMock()),
            mock.patch.dict(os.environ, _NO_WORKER_ENV),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_is_dm_inbox_message_detection(self):
        self.assertTrue(control_bot_module._is_dm_inbox_message(_dm_inbox_message(channel_id=4242)))
        # name-based detection (no DM_INBOX_CH_ID configured)
        with mock.patch.object(config, "DM_INBOX_CH_ID", 0):
            self.assertTrue(control_bot_module._is_dm_inbox_message(_dm_inbox_message(channel_id=1)))
            other = SimpleNamespace(
                author=SimpleNamespace(name="x"),
                embeds=[],
                channel=SimpleNamespace(id=2, name="farm-logs"),
            )
            self.assertFalse(control_bot_module._is_dm_inbox_message(other))

    def test_customer_for_alt_reverse_lookup(self):
        cust = control_bot_module._customer_for_alt(1)
        self.assertIsNotNone(cust)
        self.assertEqual(cust["discord_id"], "999")
        self.assertIsNone(control_bot_module._customer_for_alt(12345))

    def test_relay_sends_reply_command_and_cooldown_blocks_second(self):
        ack = mock.AsyncMock(return_value="🕒 Queued control command for Alt 1 (runner picks it up within ~60s).")
        with mock.patch.object(control_bot_module, "_send_control_wait_ack", ack):
            _run(control_bot_module._maybe_vip_autoreply(_dm_inbox_message()))
            _run(control_bot_module._maybe_vip_autoreply(_dm_inbox_message()))
        self.assertEqual(ack.await_count, 1)
        args = ack.await_args.args
        self.assertEqual(args[0], 1)
        self.assertEqual(args[1], "!reply 5551234 Away right now — leave your offer!")

    def test_relay_logs_to_alt_and_customer_events(self):
        ack = mock.AsyncMock(return_value="🕒 Queued.")
        with mock.patch.object(control_bot_module, "_send_control_wait_ack", ack):
            _run(control_bot_module._maybe_vip_autoreply(_dm_inbox_message()))
        events = cm.get_events(event="vip_autoreply_sent", discord_id="999")
        self.assertTrue(events, "vip_autoreply_sent event not recorded")
        logs = self.manager.recent_logs(1, limit=50)
        self.assertTrue(any("VIP auto-reply" in " ".join(map(str, entry)) for entry in logs))

    def test_alt_echo_is_skipped(self):
        ack = mock.AsyncMock(return_value="🕒 Queued.")
        msg = _dm_inbox_message(author="SellerOne (alt)")
        with mock.patch.object(control_bot_module, "_send_control_wait_ack", ack):
            _run(control_bot_module._maybe_vip_autoreply(msg))
        ack.assert_not_awaited()

    def test_non_vip_customer_is_skipped(self):
        ack = mock.AsyncMock(return_value="🕒 Queued.")
        msg = _dm_inbox_message(alt=9, buyer="7770001")
        with mock.patch.object(control_bot_module, "_send_control_wait_ack", ack):
            _run(control_bot_module._maybe_vip_autoreply(msg))
        ack.assert_not_awaited()

    def test_disabled_autoreply_is_skipped(self):
        cm.set_autoreply("999", "")
        ack = mock.AsyncMock(return_value="🕒 Queued.")
        with mock.patch.object(control_bot_module, "_send_control_wait_ack", ack):
            _run(control_bot_module._maybe_vip_autoreply(_dm_inbox_message()))
        ack.assert_not_awaited()

    def test_message_without_quick_reply_field_is_skipped(self):
        ack = mock.AsyncMock(return_value="🕒 Queued.")
        msg = SimpleNamespace(
            author=SimpleNamespace(name="BuyerRelay", display_name="BuyerRelay"),
            embeds=[SimpleNamespace(fields=[], footer=None)],
            channel=SimpleNamespace(id=4242, name="dm-inbox"),
        )
        with mock.patch.object(control_bot_module, "_send_control_wait_ack", ack):
            _run(control_bot_module._maybe_vip_autoreply(msg))
        ack.assert_not_awaited()

    def test_footer_only_alt_without_parseable_buyer_is_skipped(self):
        """A quick-reply field without `alt:N user:ID` cannot be relayed safely."""
        ack = mock.AsyncMock(return_value="🕒 Queued.")
        embed = SimpleNamespace(
            title="💬 New DM",
            fields=[SimpleNamespace(name="⚡ Quick Reply Command", value="/reply user:5551234 text:hi")],
            footer=SimpleNamespace(text="Alt 1: SellerOne"),
        )
        msg = SimpleNamespace(
            author=SimpleNamespace(name="BuyerRelay", display_name="BuyerRelay"),
            embeds=[embed],
            channel=SimpleNamespace(id=4242, name="dm-inbox"),
        )
        with mock.patch.object(control_bot_module, "_send_control_wait_ack", ack):
            _run(control_bot_module._maybe_vip_autoreply(msg))
        ack.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
