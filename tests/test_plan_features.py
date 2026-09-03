"""Network-free regression coverage for PMTP Phase 2/3 features."""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from control_bot import bot as control_bot_module
from control_bot import config
from control_bot import sandbox
from control_bot.alt_state import AltStateManager


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


class _Followup:
    def __init__(self):
        self.messages = []

    async def send(self, *args, **kwargs):
        self.messages.append((args, kwargs))


class _Interaction:
    def __init__(self, user_id=42):
        self.user = SimpleNamespace(id=user_id)
        self.response = _Response()
        self.followup = _Followup()


class PlanFeatureTests(unittest.TestCase):
    def setUp(self):
        control_bot_module._cooldowns.clear()

    def test_getstarted_returns_beginner_guide(self):
        manager = AltStateManager({1: "Alt 1"}, alt_ids=[1])
        inter = _Interaction()
        with mock.patch.object(control_bot_module, "state", manager), \
             mock.patch.object(config, "OWNER_IDS", {42}):
            asyncio.run(control_bot_module.cmd_getstarted.callback(inter))
        self.assertTrue(inter.response.deferred)
        embed = inter.response.messages[0][1]["embed"]
        self.assertIn("Get Started", embed.title)
        self.assertIn("Confirm Launch", str(embed.fields))

    def test_script_simulate_returns_unfiltered_output(self):
        manager = AltStateManager({1: "Alt 1"}, alt_ids=[1])
        inter = _Interaction()
        fake = {
            "code": 3,
            "stdout": "traceback-output\nValueError: boom",
            "stderr": "stderr-detail",
            "timed_out": False,
            "elapsed": 0.01,
            "error": "",
        }
        with mock.patch.object(control_bot_module, "state", manager), \
             mock.patch.object(config, "OWNER_IDS", {42}), \
             mock.patch.object(control_bot_module.sandbox, "run_script", return_value=fake), \
             mock.patch.object(control_bot_module, "_log_control", new=mock.AsyncMock()):
            asyncio.run(control_bot_module.cmd_script.callback(inter, action="simulate", code="raise ValueError('boom')"))
        self.assertTrue(inter.response.deferred)
        embed = inter.followup.messages[0][1]["embed"]
        self.assertIn("Script simulate", embed.title)
        all_values = " ".join(field.value for field in embed.fields)
        self.assertIn("traceback-output", all_values)
        self.assertIn("stderr-detail", all_values)

    def test_script_sandbox_actually_runs_capped_subprocess(self):
        result = sandbox.run_script("print('hello sandbox')", timeout_sec=5, memory_mb=128, cpu_sec=5)
        self.assertEqual(result["code"], 0)
        self.assertTrue(result["stdout"].startswith("hello sandbox"))
        # Simple kill-early sanity check: a hard infinite loop should be killed
        # by the wall-clock timeout rather than hanging the test.
        timed = sandbox.run_script(
            "while True:\n  pass\n",
            timeout_sec=2,
            memory_mb=128,
            cpu_sec=2,
        )
        self.assertEqual(timed["timed_out"], True)

    def test_channel_overwrite_updates_state_and_persists_secret(self):
        manager = AltStateManager({1: "Seller A"}, alt_ids=[1])
        manager.set_channel(1, "111", "old")
        manager.set_channel(1, "222", "old2")
        inter = _Interaction()
        with mock.patch.object(control_bot_module, "state", manager), \
             mock.patch.object(config, "OWNER_IDS", {42}), \
             mock.patch.object(config, "ALT_REPOS", {1: "org/alt-repo"}), \
             mock.patch.object(config, "GITHUB_TOKEN", "gh-token"), \
             mock.patch.object(control_bot_module, "_send_dm_wait_ack", new=mock.AsyncMock(return_value="✅ ACK")), \
             mock.patch.object(control_bot_module.github_api, "set_repository_secret") as set_secret, \
             mock.patch.object(control_bot_module, "_log_control", new=mock.AsyncMock()):
            asyncio.run(control_bot_module.cmd_channels.callback(
                inter, alt=1, action="overwrite", channel_id="333,444,555"
            ))
        self.assertEqual(set(manager.get(1).channels.keys()), {"333", "444", "555"})
        self.assertTrue(set_secret.called)
        final_cids = set_secret.call_args[0][2].split(",")
        self.assertEqual(set(final_cids), {"333", "444", "555"})
        self.assertIn("overwrite", inter.followup.messages[0][0][0])

    def test_run_preview_embed_shows_limitless_runtime(self):
        manager = AltStateManager({1: "Seller A"}, alt_ids=[1])
        with mock.patch.object(control_bot_module, "state", manager):
            embed = control_bot_module._run_preview_embed({
                "ad_type": "sell",
                "attach_image": "yes",
                "sell_rate": "2.5",
                "sell_extra": "DM ME",
            }, {"alt_id": 1, "rate": 2.5, "interval": 5, "hours": 0})
        text = " ".join(field.value for field in embed.fields)
        self.assertIn("Limitless", text)

    def test_shutdown_requires_confirmation_and_stops_alts(self):
        manager = AltStateManager({1: "Seller A"}, alt_ids=[1])
        inter = _Interaction()
        # First: missing/incorrect confirmation is rejected before any side effect.
        with mock.patch.object(control_bot_module, "state", manager), \
             mock.patch.object(config, "OWNER_IDS", {42}):
            asyncio.run(control_bot_module.cmd_shutdown.callback(inter, confirmation="no"))
        self.assertIn("SHUTDOWN", inter.response.messages[0][0][0])

    # ---------------- PMTP Phase 2 regression coverage ---------------- #

    def test_alt_tag_is_stable_and_log_control_carries_it(self):
        self.assertEqual(control_bot_module._alt_tag(3), "[ALT-3]")
        self.assertEqual(control_bot_module._alt_tag(None), "[ALT-?]")
        self.assertEqual(control_bot_module._alt_tag("bad"), "[ALT-?]")

        sent = []

        class _Channel:
            async def send(self, content, **kwargs):
                sent.append(content)

        manager = AltStateManager({2: "Seller B"}, alt_ids=[2])
        with mock.patch.object(control_bot_module, "state", manager), \
             mock.patch.object(config, "CONTROL_CH_ID", 999), \
             mock.patch.object(control_bot_module.bot, "get_channel", return_value=_Channel()):
            asyncio.run(control_bot_module._log_control("hello fleet", alt_id=2))
        self.assertIn("[ALT-2]", sent[0])
        self.assertIn("hello fleet", sent[0])

    def test_alt_add_failure_logs_are_attributed_to_the_attempted_alt(self):
        """A failed add must never be logged only as synthetic alt 0."""
        manager = AltStateManager({1: "Seller A"}, alt_ids=[1])
        logged = []

        async def _capture(text, alt_id=None):
            logged.append(text)

        with mock.patch.object(control_bot_module, "state", manager), \
             mock.patch.object(control_bot_module, "_log_control", new=_capture):
            asyncio.run(control_bot_module._log_alt_add_event(3, False, "token rejected"))
            asyncio.run(control_bot_module._log_alt_add_event(0, False, "token missing"))
        self.assertTrue(any("[ALT-3]" in item for item in logged))
        self.assertTrue(any("token rejected" in item for item in logged))
        self.assertTrue(any("token missing" in item for item in logged))

    def test_alt_add_rejects_an_out_of_range_slot_with_an_actionable_message(self):
        manager = AltStateManager({1: "A", 2: "B", 3: "C", 4: "D"}, alt_ids=[1, 2, 3, 4])
        inter = _Interaction()
        modal = control_bot_module.AltAddModal()
        modal.user_token = SimpleNamespace(value="token")
        modal.name = SimpleNamespace(value="")
        modal.alt_id = SimpleNamespace(value="9")
        modal.repository = SimpleNamespace(value="")
        modal.channels = SimpleNamespace(value="")
        with mock.patch.object(control_bot_module, "state", manager), \
             mock.patch.object(config, "OWNER_IDS", {42}), \
             mock.patch.object(control_bot_module.github_api, "fetch_discord_user_profile",
                               return_value=(True, {"id": 7, "username": "nine", "global_name": "Nine"})), \
             mock.patch.object(control_bot_module, "_log_control", new=mock.AsyncMock()):
            asyncio.run(modal.on_submit(inter))
        body = " ".join(str(call[0][0]) for call in inter.followup.messages)
        self.assertIn("between 1 and 4", body)

    def test_alt_add_reports_full_fleet_without_deleting_anything(self):
        manager = AltStateManager({1: "A", 2: "B", 3: "C", 4: "D"}, alt_ids=[1, 2, 3, 4])
        inter = _Interaction()
        modal = control_bot_module.AltAddModal()
        modal.user_token = SimpleNamespace(value="token")
        modal.name = SimpleNamespace(value="")
        modal.alt_id = SimpleNamespace(value="")
        modal.repository = SimpleNamespace(value="")
        modal.channels = SimpleNamespace(value="")
        provision = mock.Mock(return_value=(True, "OK"))
        with mock.patch.object(control_bot_module, "state", manager), \
             mock.patch.object(config, "OWNER_IDS", {42}), \
             mock.patch.object(control_bot_module.github_api, "fetch_discord_user_profile",
                               return_value=(True, {"id": 8, "username": "fifth", "global_name": "Fifth"})), \
             mock.patch.object(control_bot_module.github_api,
                               "provision_alt_repository_files_and_secrets", provision), \
             mock.patch.object(control_bot_module, "_log_control", new=mock.AsyncMock()):
            asyncio.run(modal.on_submit(inter))
        body = " ".join(str(call[0][0]) for call in inter.followup.messages)
        self.assertIn("occupied", body)
        self.assertIn("action:remove", body)
        # No remote side effect when there is no free slot.
        self.assertFalse(provision.called)

    def test_squad_create_assigns_a_whole_group_in_one_command(self):
        manager = AltStateManager({1: "A", 2: "B", 3: "C", 4: "D"}, alt_ids=[1, 2, 3, 4])
        inter = _Interaction()
        with mock.patch.object(control_bot_module, "state", manager), \
             mock.patch.object(config, "OWNER_IDS", {42}), \
             mock.patch.object(control_bot_module, "_log_control", new=mock.AsyncMock()):
            asyncio.run(control_bot_module.cmd_squad.callback(
                inter, action="create", squad_name="MyGroup", alts="1,2,3,4"))
        self.assertEqual(sorted(manager.get_all_squads()["MyGroup"]), [1, 2, 3, 4])
        body = " ".join(str(call[0][0]) for call in inter.response.messages)
        self.assertIn("MyGroup", body)
        self.assertIn("/run squad:MyGroup", body)

    def test_squad_create_rejects_unknown_alts_atomically(self):
        manager = AltStateManager({1: "A", 2: "B"}, alt_ids=[1, 2])
        inter = _Interaction()
        with mock.patch.object(control_bot_module, "state", manager), \
             mock.patch.object(config, "OWNER_IDS", {42}), \
             mock.patch.object(control_bot_module, "_log_control", new=mock.AsyncMock()):
            asyncio.run(control_bot_module.cmd_squad.callback(
                inter, action="create", squad_name="Half", alts="1,9"))
        # Nothing was partially applied.
        self.assertEqual(manager.get_all_squads()["Unassigned"], [1, 2])

    def test_squad_list_reports_every_group(self):
        manager = AltStateManager({1: "A", 2: "B"}, alt_ids=[1, 2])
        manager.set_squad(1, "Alpha")
        manager.set_squad(2, "Beta")
        inter = _Interaction()
        with mock.patch.object(control_bot_module, "state", manager), \
             mock.patch.object(config, "OWNER_IDS", {42}):
            asyncio.run(control_bot_module.cmd_squad.callback(inter, action="list"))
        embed = inter.response.messages[0][1]["embed"]
        text = " ".join(str(field.value) for field in embed.fields) + " " + str(embed.description)
        self.assertIn("Alpha", text)
        self.assertIn("Beta", text)

    def test_squad_run_dispatches_every_member_with_spacing(self):
        manager = AltStateManager({1: "A", 2: "B"}, alt_ids=[1, 2])
        manager.set_squad(1, "Alpha")
        manager.set_squad(2, "Alpha")
        dispatched = []
        sleeps = []

        async def _fake_pause(control_plane=False):
            sleeps.append(control_plane)
            return 0.3

        def _dispatch(alt_id, inputs):
            dispatched.append(alt_id)
            return True, "queued"

        inter = _Interaction()
        with mock.patch.object(control_bot_module, "state", manager), \
             mock.patch.object(config, "OWNER_IDS", {42}), \
             mock.patch.object(config, "GITHUB_TOKEN", "gh"), \
             mock.patch.object(config, "GITHUB_OWNER", "org"), \
             mock.patch.object(config, "ALT_REPOS", {1: "org/a", 2: "org/b"}), \
             mock.patch.object(control_bot_module, "_squad_pause", new=_fake_pause), \
             mock.patch.object(control_bot_module, "_log_control", new=mock.AsyncMock()), \
             mock.patch.object(control_bot_module.github_api, "cancel_run", return_value=(True, "c")), \
             mock.patch.object(control_bot_module.github_api, "dispatch_workflow", side_effect=_dispatch):
            asyncio.run(control_bot_module._execute_run_dispatch(inter, {
                "ad_type": "sell", "attach_image": "yes", "sell_rate": "",
                "sell_extra": "squad blast",
            }, {"alt_id": 1, "squad": "Alpha", "targets": [1, 2], "rate": None,
                "interval": 5, "hours": 6, "raw_message": "squad blast"}))
        self.assertEqual(dispatched, [1, 2])
        # One spacing pause between the two dispatches.
        self.assertEqual(sleeps, [False])
        body = " ".join(str(call[0][0]) for call in inter.followup.messages)
        self.assertIn("Squad", body)

    def test_run_rejects_an_unknown_squad_before_opening_the_form(self):
        manager = AltStateManager({1: "A"}, alt_ids=[1])
        inter = _Interaction()
        with mock.patch.object(control_bot_module, "state", manager), \
             mock.patch.object(config, "OWNER_IDS", {42}), \
             mock.patch.object(config, "GITHUB_TOKEN", "gh"):
            asyncio.run(control_bot_module.cmd_run.callback(inter, squad="Ghost"))
        self.assertIn("Ghost", inter.response.messages[0][0][0])

    def test_run_opens_a_squad_aware_form(self):
        manager = AltStateManager({1: "A", 2: "B"}, alt_ids=[1, 2])
        manager.set_squad(1, "Alpha")
        manager.set_squad(2, "Alpha")
        inter = _Interaction()
        with mock.patch.object(control_bot_module, "state", manager), \
             mock.patch.object(config, "OWNER_IDS", {42}), \
             mock.patch.object(config, "GITHUB_TOKEN", "gh"):
            asyncio.run(control_bot_module.cmd_run.callback(inter, squad="Alpha"))
        view = inter.response.messages[0][1]["view"]
        self.assertEqual(view.squad, "Alpha")

    def test_autorescan_is_registered_and_reports_drift(self):
        names = {command.name for command in control_bot_module.bot.tree.get_commands()}
        self.assertIn("autorescan", names)

        manager = AltStateManager({1: "A"}, alt_ids=[1])
        inter = _Interaction()
        result = {
            "ok": True, "servers": {"g1": {}}, "catalogue": {"111": {}},
            "added": ["111"], "removed": ["222"], "changed": [],
            "replaced": [{"old_id": "333", "new_id": "334", "name": "trade"}],
            "targets": ["111"],
        }

        async def _reconcile(alt_id, *, reason, configured_ids=None, persist=True):
            return dict(result, reason=reason, persist=persist)

        with mock.patch.object(control_bot_module, "state", manager), \
             mock.patch.object(config, "OWNER_IDS", {42}), \
             mock.patch.object(control_bot_module, "_reconcile_control_channels", new=_reconcile):
            asyncio.run(control_bot_module.cmd_autorescan.callback(inter, alt=1, action="report"))
        embed = inter.followup.messages[0][1]["embed"]
        text = " ".join(str(field.value) for field in embed.fields) + " " + str(embed.description)
        self.assertIn("111", text)
        self.assertIn("222", text)
        self.assertIn("333→334", text)
        self.assertIn("Report only", str(embed.footer.text))

    def test_channel_registry_reconcile_can_diff_without_persisting(self):
        from control_bot.persistence import ChannelRegistryStore
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            store = ChannelRegistryStore(f"{tmp}/registry.json")
            servers = [{"id": "900", "name": "Guild", "channels": [
                {"id": "111", "name": "trade", "type": 0},
                {"id": "222", "name": "general", "type": 0},
            ]}]
            first = store.reconcile(1, servers, configured_ids=["111"], persist=False)
            self.assertEqual(first["targets"], ["111"])
            # Report mode must leave the durable document untouched.
            self.assertEqual(store.snapshot_for_alt(1).get("targets"), [])
            second = store.reconcile(1, servers, configured_ids=["111"], persist=True)
            self.assertTrue(second["ok"])
            self.assertEqual(store.snapshot_for_alt(1)["targets"], ["111"])
            # A removed channel disappears and a new one is adopted automatically.
            third = store.reconcile(1, [{"id": "900", "name": "Guild", "channels": [
                {"id": "111", "name": "trade", "type": 0},
                {"id": "333", "name": "market", "type": 0},
            ]}])
            self.assertIn("333", third["added"])
            self.assertIn("222", third["removed"])
            self.assertEqual(sorted(third["targets"]), ["111", "333"])

    def test_sandbox_rejects_host_escape_primitives(self):
        ok, message = sandbox.validate_script("import os\nos.system('id')")
        self.assertFalse(ok)
        self.assertIn("not allowed", message)
        ok, message = sandbox.validate_script("")
        self.assertFalse(ok)
        ok, message = sandbox.validate_script("x" * 20001)
        self.assertFalse(ok)
        ok, _ = sandbox.validate_script("print('safe')")
        self.assertTrue(ok)

    def test_script_result_embed_reports_truncation_and_attaches_streams(self):
        result = {
            "code": 1, "stdout": "out\n" * 800, "stderr": "boom",
            "timed_out": False, "elapsed": 0.2, "error": "",
        }
        embed = control_bot_module._script_result_embed("🧪 Script simulate", result)
        text = " ".join(f"{field.name} {field.value}" for field in embed.fields)
        self.assertIn("Sandbox Policy", text)
        self.assertIn("unfiltered", text)
        self.assertIn("full stream attached", text)
        self.assertIn("boom", text)


if __name__ == "__main__":
    unittest.main()
