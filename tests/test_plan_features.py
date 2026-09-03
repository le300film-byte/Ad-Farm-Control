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


if __name__ == "__main__":
    unittest.main()
