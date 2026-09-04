"""V8 bug-fix plan (round 6) — channel-aware visibility, /reset, /admin sync.

Covers the four CRITICAL/MANAGER items of V8_BUG_FIX_PLAN.md:

  #1  Channel-aware command visibility: security.classify_channel_context +
      the tier matrix, enforced through _check_perms(command=...) and
      best-effort Discord-native per-channel permissions pushed by
      control_bot.command_sync.sync_guild_commands.
  #2  Ghost alt state: the post-boot fleet sweep prunes 404-confirmed ALT_REPOS
      entries everywhere (config, secrets, live state) and /reset wipes all
      customer data + alt state with typed confirmation.
  #3  /admin appears immediately: admin cog registration is idempotent, and
      /admin sync-commands + /admin sweep-alts force a re-sync/re-verify
      without a restart.
  #4  Manager cleanup: no silent ``except: pass`` handlers remain in shipped
      modules, and the test suite leaves no artifacts in the repository root.

Network-free: GitHub HTTP, Discord I/O and the Gist worker are mocked or
bypassed via temporary state.
"""
from __future__ import annotations

import ast
import asyncio
import os
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import customer_manager as cm
import security
from control_bot import bot as control_bot_module
from control_bot import command_sync
from control_bot import config
from control_bot.alt_state import AltStateManager

ROOT = Path(__file__).resolve().parents[1]


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ─────────────────────────────────────────────────────────────────────────────
# Fakes
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

    async def send_modal(self, modal):
        self.messages.append(("modal", modal))
        self.deferred = True


class _Followup:
    def __init__(self):
        self.messages = []

    async def send(self, *args, **kwargs):
        self.messages.append((args, kwargs))


class _Interaction:
    def __init__(self, user_id=42, channel=None):
        self.user = SimpleNamespace(id=user_id, display_name=f"User{user_id}")
        self.response = _Response()
        self.followup = _Followup()
        if channel is not None:
            self.channel = channel

    def all_texts(self):
        out = []
        for stage in (self.response.messages, self.followup.messages):
            for args, kwargs in stage:
                if args:
                    out.append(str(args[0]))
                if "content" in kwargs:
                    out.append(str(kwargs["content"]))
                if "embed" in kwargs:
                    out.append(str(getattr(kwargs["embed"], "description", "")))
        return "\n".join(out)


def _guild_channel(guild_id=999, cid=1, name="general"):
    return SimpleNamespace(
        id=cid, name=name,
        guild=SimpleNamespace(id=guild_id),
        parent=None, category=None,
    )


# ─────────────────────────────────────────────────────────────────────────────
# #1 — channel context classification + tier matrix (security.py)
# ─────────────────────────────────────────────────────────────────────────────

class TestChannelClassification(unittest.TestCase):
    def test_named_contexts(self):
        for name, expected in (
            ("announcements", "public"),
            ("welcome-about", "public"),
            ("pricing-plans", "public"),
            ("control", "customer"),
            ("dashboard", "customer"),
            ("farm-logs", "customer"),
            ("dm-inbox", "vip"),
            ("admin-commands", "admin"),
            ("audit-logs", "admin"),
        ):
            ch = _guild_channel(name=name)
            self.assertEqual(security.classify_channel_context(ch), expected, name)

    def test_unclassified_returns_none(self):
        self.assertIsNone(security.classify_channel_context(_guild_channel(name="random-stuff")))
        self.assertIsNone(security.classify_channel_context(None))

    def test_threads_inherit_parent_context(self):
        parent = _guild_channel(cid=7, name="dm-inbox")
        thread = _guild_channel(cid=8, name="Buyer spam cleanup")
        thread.parent = parent
        self.assertEqual(security.classify_channel_context(thread), "vip")

    def test_customer_hub_category_heuristic(self):
        cat = SimpleNamespace(name="🏢 Customer Hub")
        thread = _guild_channel(cid=9, name="whatever")
        thread.category = cat
        self.assertEqual(security.classify_channel_context(thread), "customer")

    def test_dm_channel_is_unrestricted_context(self):
        dm = SimpleNamespace(id=3, guild=None, name=None, type=None, parent=None, category=None)
        self.assertEqual(security.classify_channel_context(dm), "dm")

    def test_channel_id_override_and_env_lists(self):
        old = security.CHANNEL_RULES
        try:
            with mock.patch.dict(os.environ, {"PUBLIC_CHANNELS": "lobby,<#123456>"}):
                security.reload_channel_rules()
                self.assertIn("lobby", security.CHANNEL_RULES["public"])
                self.assertIn("123456", security.CHANNEL_RULES["public"])
                ch = _guild_channel(cid=123456, name="unrelated-name")
                self.assertEqual(security.classify_channel_context(ch), "public")
        finally:
            security.CHANNEL_RULES = old

    def test_tier_matrix(self):
        ok, _ = security.command_allowed_in_context("help", "public")
        self.assertTrue(ok)
        ok, hint = security.command_allowed_in_context("run", "public")
        self.assertFalse(ok)
        self.assertTrue(hint)
        ok, _ = security.command_allowed_in_context("run", "customer")
        self.assertTrue(ok)
        ok, _ = security.command_allowed_in_context("vip", "customer")
        self.assertTrue(ok)  # VIP ⊇ customer rooms
        ok, _ = security.command_allowed_in_context("admin", "customer")
        self.assertFalse(ok)
        ok, _ = security.command_allowed_in_context("admin", "admin")
        self.assertTrue(ok)
        ok, _ = security.command_allowed_in_context("admin", "dm")
        self.assertTrue(ok)
        ok, _ = security.command_allowed_in_context("run", None)
        self.assertTrue(ok)

    def test_tier_of_commands(self):
        self.assertEqual(security.command_tier("/help"), "public")
        self.assertEqual(security.command_tier("reset"), "admin")
        self.assertEqual(security.command_tier("admin"), "admin")
        self.assertEqual(security.command_tier("squad"), "vip")
        self.assertEqual(security.command_tier("whatever-new"), "customer")


class TestGateEnforcement(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        control_bot_module._cooldowns.clear()
        self.addCleanup(control_bot_module._cooldowns.clear)

    async def test_public_channel_denies_customer_command(self):
        inter = _Interaction(user_id=7, channel=_guild_channel(name="announcements"))
        with mock.patch.object(security, "OWNER_IDS", set()):
            allowed = await security.enforce_channel_gate(inter, "run")
        self.assertFalse(allowed)
        text = inter.response.messages[0][1].get("content") or inter.response.messages[0][0][0]
        self.assertIn(security.CHANNEL_GATE_DENIAL, text)

    async def test_admin_user_bypasses_gate(self):
        inter = _Interaction(user_id=42, channel=_guild_channel(name="announcements"))
        with mock.patch.object(security, "OWNER_IDS", {42}):
            self.assertTrue(await security.enforce_channel_gate(inter, "run"))

    async def test_missing_channel_allows_and_sends_nothing(self):
        inter = _Interaction(user_id=7)  # no channel attr at all (fake/DM)
        with mock.patch.object(security, "OWNER_IDS", set()):
            self.assertTrue(await security.enforce_channel_gate(inter, "run"))
        self.assertEqual(inter.response.messages, [])

    async def test_check_perms_command_kwarg_enforces_channel(self):
        mod = control_bot_module
        inter = _Interaction(user_id=7, channel=_guild_channel(name="announcements"))
        with mock.patch.object(mod.config, "OWNER_IDS", set()), \
             mock.patch.object(mod, "_V8_LOADED", True), \
             mock.patch.object(security, "OWNER_IDS", set()):
            self.assertFalse(await mod._check_perms(inter, role="customer", command="run"))
        # same user in their forum room passes the channel gate (role gate is
        # patched out by making them an owner for this assertion)
        inter2 = _Interaction(user_id=42, channel=_guild_channel(name="control"))
        with mock.patch.object(mod.config, "OWNER_IDS", {42}), \
             mock.patch.object(mod, "_V8_LOADED", True):
            self.assertTrue(await mod._check_perms(inter2, role="customer", command="run"))

    async def test_views_without_command_kwarg_are_not_gated(self):
        mod = control_bot_module
        # Button callbacks inside a customer channel call _check_perms(inter)
        # (owner legacy role) — no channel gate keyword → no channel denial.
        inter = _Interaction(user_id=42, channel=_guild_channel(name="announcements"))
        with mock.patch.object(mod.config, "OWNER_IDS", {42}), \
             mock.patch.object(mod, "_V8_LOADED", True):
            self.assertTrue(await mod._check_perms(inter))

    async def test_decorator_infers_command_name(self):
        @security.require_channel()
        async def cmd_pause_billing(inter):
            return "ran"
        inter = _Interaction(user_id=7, channel=_guild_channel(name="announcements"))
        with mock.patch.object(security, "OWNER_IDS", set()):
            result = await cmd_pause_billing(inter)
        self.assertIsNone(result)  # denied by the channel gate
        self.assertIn(security.CHANNEL_GATE_DENIAL,
                      inter.response.messages[0][1].get("content")
                      or inter.response.messages[0][0][0])


# ─────────────────────────────────────────────────────────────────────────────
# Role-set consolidation (single source of truth, back-compat aliases)
# ─────────────────────────────────────────────────────────────────────────────

class TestTierConsolidation(unittest.TestCase):
    def test_bot_aliases_match_security_canon(self):
        mod = control_bot_module
        self.assertEqual(set(mod.ROLE_PUBLIC_COMMANDS), set(security.PUBLIC_COMMANDS))
        self.assertEqual(set(mod.ROLE_CUSTOMER_COMMANDS), set(security.CUSTOMER_COMMANDS))
        self.assertEqual(set(mod.ROLE_VIP_COMMANDS), set(security.VIP_COMMANDS))

    def test_commands_for_role_delegates(self):
        for role in ("admin", "vip", "customer", "public"):
            self.assertEqual(
                control_bot_module.commands_for_role(role),
                set(security.allowed_commands_for_role(role)),
                role,
            )

    def test_new_commands_are_tiered(self):
        self.assertIn("reset", security.ADMIN_COMMANDS)
        self.assertEqual(security.command_tier("reset"), "admin")


# ─────────────────────────────────────────────────────────────────────────────
# #3 + visibility — command_sync
# ─────────────────────────────────────────────────────────────────────────────

class _FakeTree:
    def __init__(self, command_names):
        self._names = list(command_names)
        self.calls = []

    def copy_global_to(self, *, guild):
        self.calls.append(("copy", guild))

    async def sync(self, *, guild=None):
        self.calls.append(("sync", guild))
        return [SimpleNamespace(name=n) for n in self._names]

    def get_commands(self, *, guild=None):
        return [SimpleNamespace(name=n, id=100 + i) for i, n in enumerate(self._names)]


class _FakeHttp:
    def __init__(self, fail=False):
        self.pushed = []
        self.fail = fail

    async def edit_application_command_permissions(self, app_id, guild_id, command_id, payload):
        if self.fail:
            raise RuntimeError("missing scope applications.commands.permissions.update")
        self.pushed.append((app_id, guild_id, command_id, payload))


def _fake_bot(guild, command_names, http_fail=False):
    http = _FakeHttp(fail=http_fail)
    bot = SimpleNamespace(
        tree=_FakeTree(command_names),
        http=http,
        application_id=1234,
        user=SimpleNamespace(id=1234),
        get_guild=lambda gid: guild if guild and gid == guild.id else None,
    )
    return bot


class TestCommandSync(unittest.IsolatedAsyncioTestCase):
    def _guild(self, extra_channels=()):
        channels = [
            _guild_channel(name="announcements", cid=11),
            _guild_channel(name="admin-commands", cid=12),
            _guild_channel(name="control", cid=13),
            *extra_channels,
        ]
        return SimpleNamespace(id=999, name="Test Guild", channels=channels)

    def test_visibility_plan_shape(self):
        guild = self._guild()
        plan = command_sync.visibility_plan(guild)
        self.assertIn("admin", plan)
        self.assertEqual(plan["admin"]["allow"], [12])
        self.assertIn("run", plan)
        self.assertEqual(plan["run"]["deny"], [11])  # hidden in #announcements

    def test_visibility_plan_without_admin_channel(self):
        guild = SimpleNamespace(id=999, name="G", channels=[
            _guild_channel(name="general", cid=1),
            _guild_channel(name="announcements", cid=2),
        ])
        plan = command_sync.visibility_plan(guild)
        # No admin room defined → /admin is NOT locked to a room…
        self.assertEqual(plan.get("admin", {}).get("allow", []), [])
        # …but it is still hidden inside public announcement channels.
        self.assertEqual(plan["run"]["deny"], [2])
        self.assertEqual(plan["admin"]["deny"], [2])

    def test_payload_contains_everyone_deny_then_channel_allows(self):
        entry = {"allow": [12], "deny": [11]}
        payload = command_sync.build_permission_payload(entry, guild_id=999)
        perms = payload["permissions"]
        self.assertEqual(perms[0], {"type": 1, "id": "999", "permission": False})
        self.assertIn({"type": 8, "id": "12", "permission": True}, perms)
        self.assertIn({"type": 8, "id": "11", "permission": False}, perms)

    async def test_sync_guild_commands_full_path(self):
        guild = self._guild()
        bot = _fake_bot(guild, ["admin", "run", "help", "status"])
        with mock.patch.object(config, "GUILD_ID", 999):
            summary = await command_sync.sync_guild_commands(bot)
        self.assertEqual(summary["mode"], "guild")
        self.assertEqual(summary["synced"], 4)
        # /help is public → never restricted; the other three get channel rules.
        self.assertEqual(summary["visibility"]["applied"], 3)
        pushed_ids = {p[2] for p in bot.http.pushed}
        self.assertEqual(pushed_ids, {100, 101, 103})
        # all pushes target this guild + this app
        self.assertTrue(all(p[0] == 1234 and p[1] == 999 for p in bot.http.pushed))
        text = command_sync.format_sync_summary(summary)
        self.assertIn("guild", text)
        self.assertIn("visibility applied", text)

    async def test_visibility_failure_is_nonfatal_and_reported(self):
        guild = self._guild()
        bot = _fake_bot(guild, ["run"], http_fail=True)
        with mock.patch.object(config, "GUILD_ID", 999):
            summary = await command_sync.sync_guild_commands(bot)
        self.assertEqual(summary["visibility"]["applied"], 0)
        self.assertTrue(summary["visibility"]["errors"])
        self.assertEqual(summary["synced"], 1)  # sync itself still succeeded

    async def test_global_sync_mode_when_no_guild(self):
        bot = _fake_bot(None, ["run"])
        with mock.patch.object(config, "GUILD_ID", None):
            summary = await command_sync.sync_guild_commands(bot)
        self.assertEqual(summary["mode"], "global")
        self.assertEqual(summary["visibility"]["applied"], 0)


class TestAdminCogIdempotent(unittest.IsolatedAsyncioTestCase):
    async def test_double_setup_registers_admin_once(self):
        import admin_commands
        tree = control_bot_module.bot.tree
        before = [c.name for c in tree.get_commands()]
        self.assertNotIn("admin", before, "test assumption: /admin not pre-registered")
        try:
            await admin_commands.setup(control_bot_module.bot)
            await admin_commands.setup(control_bot_module.bot)  # reconnect path
            names = [c.name for c in tree.get_commands()]
            self.assertEqual(names.count("admin"), 1)
        finally:
            try:
                tree.remove_command("admin")
            except Exception as exc:
                print(f"[TEST] tree cleanup skipped: {exc}")
            try:
                await control_bot_module.bot.remove_cog("AdminCog")
            except Exception as exc:
                print(f"[TEST] cog cleanup skipped: {exc}")

    async def test_admin_group_has_sync_and_sweep_subcommands(self):
        import admin_commands
        sub_names = {c.name for c in admin_commands.AdminCog.admin_group.commands}
        self.assertIn("sync-commands", sub_names)
        self.assertIn("sweep-alts", sub_names)

    async def test_sync_commands_subcommand_calls_shared_sync(self):
        import admin_commands
        inter = _Interaction(user_id=42)
        cog = SimpleNamespace(
            bot=control_bot_module.bot,
            _audit=mock.AsyncMock(),
        )
        fake_summary = {"mode": "guild", "guild_id": 999, "synced": 7,
                        "visibility": {"applied": 3, "errors": []}}
        with mock.patch.object(admin_commands, "check_admin", new=mock.AsyncMock(return_value=True)), \
             mock.patch("control_bot.command_sync.sync_guild_commands",
                        new=mock.AsyncMock(return_value=fake_summary)):
            await admin_commands.AdminCog.admin_sync_commands.callback(cog, inter)
        text = inter.followup.messages[0][0][0]
        self.assertIn("7 command(s) synced (guild)", text)
        self.assertIn("visibility applied to 3", text)
        cog._audit.assert_awaited()


# ─────────────────────────────────────────────────────────────────────────────
# #2 — ghost alt state: sweep + /reset
# ─────────────────────────────────────────────────────────────────────────────

def _state_with(alts):
    names = {i: f"Alt {i}" for i in alts}
    return AltStateManager(alt_names=names, alt_ids=list(alts))


class TestStaleFleetSweep(unittest.IsolatedAsyncioTestCase):
    async def _sweep(self, exists_side_effect, assert_within=None):
        mod = control_bot_module
        manager = _state_with([1, 2])
        persist = mock.AsyncMock(return_value=(True, "ok"))
        with mock.patch.object(config, "GITHUB_TOKEN", "tok"), \
             mock.patch.object(config, "ALT_REPOS", {1: "o/alive", 2: "o/gone"}), \
             mock.patch.object(config, "ALT_DISCORD_IDS", {1: 11, 2: 22}), \
             mock.patch.object(config, "ALT_NAMES", {1: "A", 2: "B"}), \
             mock.patch.object(mod, "state", manager), \
             mock.patch.object(mod, "_persist_alt_registry", new=persist), \
             mock.patch.object(mod, "_log_control", new=mock.AsyncMock()), \
             mock.patch.object(mod.github_api, "repository_exists",
                               side_effect=exists_side_effect):
            summary = await mod._sweep_stale_fleet_alts()
            if assert_within is not None:
                assert_within(summary, manager, persist, config)
        return summary, manager, persist

    async def test_prunes_confirmed_404_everywhere(self):
        def side_effect(repo):
            if "gone" in repo:
                return False, "Repository was not found or is not accessible to GH_TOKEN."
            return True, "Repository exists."

        def checks(summary, manager, persist, cfg):
            self.assertEqual(summary["pruned"], [2])
            self.assertNotIn(2, manager.alt_ids)
            self.assertIn(1, manager.alt_ids)
            # secrets rebuilt WITHOUT the ghost alt
            repos, ids, names = persist.await_args.args
            self.assertEqual(repos, {1: "o/alive"})
            self.assertEqual(ids, {1: 11})
            # global in-memory config pruned too
            self.assertEqual(dict(cfg.ALT_REPOS), {1: "o/alive"})
            self.assertEqual(dict(cfg.ALT_DISCORD_IDS), {1: 11})
            self.assertEqual(dict(cfg.ALT_NAMES), {1: "A"})

        await self._sweep(side_effect, checks)

    async def test_api_error_never_prunes(self):
        def side_effect(repo):
            return False, "Repository lookup returned HTTP 403."  # rate limit etc.

        def checks(summary, manager, persist, cfg):
            self.assertEqual(summary["pruned"], [])
            self.assertEqual(set(manager.alt_ids), {1, 2})
            persist.assert_not_called()
            self.assertEqual(dict(cfg.ALT_REPOS), {1: "o/alive", 2: "o/gone"})

        await self._sweep(side_effect, checks)

    async def test_no_token_skips_entirely(self):
        mod = control_bot_module
        with mock.patch.object(config, "GITHUB_TOKEN", ""), \
             mock.patch.object(config, "ALT_REPOS", {1: "o/alive"}), \
             mock.patch.object(mod.github_api, "repository_exists") as probe:
            summary = await mod._sweep_stale_fleet_alts()
        self.assertEqual(summary["checked"], 0)
        probe.assert_not_called()


class TestResetAllData(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_db = cm.DB_PATH
        cm.DB_PATH = os.path.join(self._tmpdir.name, "customers-test.db")
        cm.init_db()

    def tearDown(self):
        cm.DB_PATH = self._orig_db
        self._tmpdir.cleanup()

    def test_reset_clears_every_customer_table_but_keeps_schema(self):
        cm.add_customer(discord_id="111", discord_username="alice", alt_count=1,
                       vip=True, days=30, github_account="w1", repos=["w1/a1"])
        cm.add_customer(discord_id="222", discord_username="bob", alt_count=1,
                        vip=False, days=5, github_account="w2", repos=[])
        cm.set_meta("open_ticket_ch_id", "555")
        cm.set_meta("some_other_key", "keep-me")

        counts = cm.reset_all_data()

        self.assertEqual(counts["customers"], 2)
        self.assertEqual(cm.list_customers(active_only=False), [])
        self.assertEqual(cm.get_meta("open_ticket_ch_id"), "")  # install caches wiped
        # schema_version stays so future restores keep versioning information
        self.assertEqual(cm.get_meta("schema_version"), str(cm.SCHEMA_VERSION))
        # the reset itself is recorded in the (otherwise wiped) event ledger
        events = cm.get_events(event="factory_reset")
        self.assertEqual(len(events), 1)

    def test_reset_on_fresh_db_is_noop_safe(self):
        counts = cm.reset_all_data()
        self.assertEqual(counts["customers"], 0)


class TestResetCommand(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_db = cm.DB_PATH
        cm.DB_PATH = os.path.join(self._tmpdir.name, "customers-test.db")
        cm.init_db()
        control_bot_module._cooldowns.clear()

    def tearDown(self):
        cm.DB_PATH = self._orig_db
        self._tmpdir.cleanup()

    async def test_requires_confirmation_first(self):
        cm.add_customer(discord_id="111", discord_username="alice", alt_count=1,
                        vip=False, days=30, github_account="w1", repos=[])
        mod = control_bot_module
        inter = _Interaction(user_id=42)
        with mock.patch.object(mod.config, "OWNER_IDS", {42}), \
             mock.patch.object(mod, "_log_control", new=mock.AsyncMock()):
            await mod.cmd_reset.callback(inter, confirmation="")
        self.assertIn("Factory reset armed", inter.all_texts())
        # nothing destroyed
        self.assertEqual(len(cm.list_customers(active_only=False)), 1)

    async def test_confirmed_reset_wipes_db_registry_and_state(self):
        cm.add_customer(discord_id="111", discord_username="alice", alt_count=1,
                        vip=False, days=30, github_account="w1", repos=[])
        mod = control_bot_module
        manager = _state_with([1, 2])
        persist = mock.AsyncMock(return_value=(True, "ok"))
        inter = _Interaction(user_id=42)
        with mock.patch.object(mod.config, "OWNER_IDS", {42}), \
             mock.patch.object(mod, "state", manager), \
             mock.patch.object(config, "ALT_REPOS", {1: "o/alive", 2: "o/gone"}), \
             mock.patch.object(config, "ALT_DISCORD_IDS", {1: 11, 2: 22}), \
             mock.patch.object(config, "ALT_NAMES", {}), \
             mock.patch.object(mod, "_persist_alt_registry", new=persist), \
             mock.patch.object(mod, "_log_control", new=mock.AsyncMock()), \
             mock.patch.object(mod.github_api, "cancel_run",
                              new=mock.MagicMock(return_value=(True, "canceled"))):
            await mod.cmd_reset.callback(inter, confirmation="RESET")
            # config-level maps emptied (inside the patched view)
            self.assertEqual(dict(config.ALT_REPOS), {})
        self.assertEqual(cm.list_customers(active_only=False), [])
        self.assertEqual(manager.alt_ids, [])  # live state emptied
        # core secrets cleared
        self.assertEqual(persist.await_args.args[0], {})
        self.assertIn("Factory reset complete", inter.followup.messages[0][0][0])

    async def test_non_owner_denied(self):
        mod = control_bot_module
        cm.add_customer(discord_id="111", discord_username="alice", alt_count=1,
                        vip=False, days=30, github_account="w1", repos=[])
        inter = _Interaction(user_id=7)
        with mock.patch.object(mod.config, "OWNER_IDS", {42}), \
             mock.patch.object(security, "OWNER_IDS", set()):
            await mod.cmd_reset.callback(inter, confirmation="RESET")
        self.assertIn("aren't authorized", inter.all_texts().lower())
        self.assertEqual(len(cm.list_customers(active_only=False)), 1)  # untouched

    async def test_reset_registered_with_channel_gate(self):
        # /reset is admin-tier: it must be refused in public rooms for
        # non-owners… but owners bypass — verify tier wiring instead:
        self.assertEqual(security.command_tier("reset"), "admin")
        names = {c.name for c in control_bot_module.bot.tree.get_commands()}
        self.assertIn("reset", names)


# ─────────────────────────────────────────────────────────────────────────────
# #4 — manager cleanup invariants
# ─────────────────────────────────────────────────────────────────────────────

_SHIPPED_MODULES = sorted(
    list(ROOT.glob("*.py")) + list((ROOT / "control_bot").glob("*.py"))
)


class TestCodebaseDiscipline(unittest.TestCase):
    def _handlers(self, path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                yield node

    def test_no_silent_except_pass_in_shipped_modules(self):
        offenders = []
        for path in _SHIPPED_MODULES:
            for node in self._handlers(path):
                if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                    offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual(offenders, [], f"silent `except … : pass` returned: {offenders}")

    def test_no_bare_except_in_shipped_modules(self):
        offenders = [
            f"{p.name}:{n.lineno}"
            for p in _SHIPPED_MODULES
            for n in self._handlers(p)
            if n.type is None
        ]
        self.assertEqual(offenders, [])

    def test_no_hardcoded_alt_repo_mapping_literals(self):
        """Alt 1/Alt 2 ghosts must not come from code — only from env/DB."""
        import re as _re
        pattern = _re.compile(r"""['"]\s*\d+\s*:\s*alt\d""")
        for path in _SHIPPED_MODULES:
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if pattern.search(line) and not stripped.startswith("#"):
                    # strip trailing comments before judging
                    code_part = line.split("#", 1)[0]
                    if pattern.search(code_part):
                        self.fail(f"hardcoded alt mapping at {path.name}:{lineno}: {stripped}")

    def test_suite_writes_no_state_into_repo_root(self):
        """conftest.py must redirect every mutable file out of the repo."""
        for name in ("customers.db", ".adfarm_control_state.json",
                     ".adfarm_channel_registry.json", "dash_msg_id.txt"):
            self.assertFalse((ROOT / name).exists(),
                             f"test run left {name} in the repo root")
        # and the redirect is active in THIS (test) process
        self.assertNotEqual(os.environ.get("CUSTOMERS_DB", "customers.db"), "customers.db")
        self.assertTrue(os.environ.get("CUSTOMERS_DB", "").startswith(str(Path(os.sep) / "tmp")),
                        "CUSTOMERS_DB must point outside the repo")

    def test_customer_manager_uses_env_db(self):
        self.assertTrue(cm.DB_PATH.startswith(str(Path(os.sep) / "tmp")))


if __name__ == "__main__":
    unittest.main()
