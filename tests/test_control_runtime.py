"""Network-free control-bot acknowledgement and freshness checks."""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from control_bot import bot as control_bot_module
from control_bot import config
from control_bot.alt_state import AltStateManager


class _Response:
    def __init__(self):
        self.messages = []
        self.deferred = False

    def is_done(self):
        return self.deferred

    async def send_message(self, *args, **kwargs):
        self.messages.append((args, kwargs))
        self.deferred = True

    async def defer(self, **kwargs):
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


class ControlRuntimeTests(unittest.TestCase):
    def test_unauthorized_control_fails_closed_and_acknowledged_update_changes_live_state(self):
        manager = AltStateManager({1: "Configured"}, alt_ids=[1])
        unauthorized = _Interaction(user_id=999)
        with mock.patch.object(control_bot_module, "state", manager), \
             mock.patch.object(config, "OWNER_IDS", {42}), \
             mock.patch.object(control_bot_module, "_cooldowns", {}):
            asyncio.run(control_bot_module.cmd_setmode.callback(unauthorized, 1, "buy"))
        self.assertIn("authorized", unauthorized.response.messages[0][0][0])
        self.assertEqual(manager.get(1).ad_type, "")

        authorized = _Interaction()
        with mock.patch.object(control_bot_module, "state", manager), \
             mock.patch.object(config, "OWNER_IDS", {42}), \
             mock.patch.object(control_bot_module, "_cooldowns", {}), \
             mock.patch.object(control_bot_module, "_send_dm_wait_ack", new=mock.AsyncMock(return_value="✅ MODE SET")), \
             mock.patch.object(control_bot_module, "_log_control", new=mock.AsyncMock()):
            asyncio.run(control_bot_module.cmd_setmode.callback(authorized, 1, "buy"))
        self.assertEqual(manager.get(1).ad_type, "buy")
        self.assertIn("MODE SET", authorized.followup.messages[0][0][0])

    def test_refresh_calls_live_github_refresh_and_returns_private_ack(self):
        inter = _Interaction()
        with mock.patch.object(config, "OWNER_IDS", {42}), \
             mock.patch.object(control_bot_module, "_cooldowns", {}), \
             mock.patch.object(control_bot_module.github_api, "refresh_all_run_statuses") as refresh, \
             mock.patch.object(control_bot_module, "_refresh_dashboard_now", new=mock.AsyncMock()) as dashboard:
            asyncio.run(control_bot_module.cmd_refresh.callback(inter))
        refresh.assert_called_once()
        dashboard.assert_awaited_once()
        self.assertTrue(inter.followup.messages[0][1]["ephemeral"])
        self.assertIn("refreshed", inter.followup.messages[0][0][0].lower())

    def test_refresh_hydrates_latest_heartbeat_from_dedicated_channel_history(self):
        manager = AltStateManager({1: "Seller A"}, alt_ids=[1])

        class FakeChannel:
            def history(self, limit=100):
                async def rows():
                    yield SimpleNamespace(
                        id=1001,
                        channel=SimpleNamespace(id=321),
                        author=SimpleNamespace(bot=True),
                        webhook_id=55,
                        content='```json\n{"type":"heartbeat","alt_id":1,"status":"active","total_sent":19}\n```',
                        embeds=[],
                    )
                return rows()

        async def run():
            with mock.patch.object(control_bot_module, "state", manager), \
                 mock.patch.object(config, "DASHBOARD_CH_ID", 321), \
                 mock.patch.object(config, "LOG_CH_ID", None), \
                 mock.patch.object(config, "DEALS_CH_ID", None), \
                 mock.patch.object(control_bot_module.bot, "get_channel", return_value=FakeChannel()):
                await control_bot_module._hydrate_discord_state()

        asyncio.run(run())
        self.assertEqual(manager.get(1).total_sent, 19)
        self.assertTrue(manager.get(1).online)

    def test_readable_heartbeat_embed_updates_live_state_without_raw_json(self):
        manager = AltStateManager({1: "yodonttryme46"}, alt_ids=[1])
        fields = [
            SimpleNamespace(name="Status", value="🟢 `active`"),
            SimpleNamespace(name="Mode", value="`buy`"),
            SimpleNamespace(name="Rate", value="$2.35/1k"),
            SimpleNamespace(name="Activity", value="Sent: `3` · Errors: `2` · Skips: `0`"),
            SimpleNamespace(name="Deals", value="`1` alert(s)"),
            SimpleNamespace(name="Keywords", value="Blade Ball, BB token, BB"),
            SimpleNamespace(name="Channels", value="Active: `2/4`"),
            SimpleNamespace(name="Channel: 154 · #trading", value="❌ unavailable · sent `0` · errors `1` · slowmode `0s` · last never"),
        ]
        message = SimpleNamespace(
            content="💓 Heartbeat · `active`",
            embeds=[SimpleNamespace(
                title="💓 Heartbeat · yodonttryme46",
                footer=SimpleNamespace(text="alt_id=1 · V6.0"),
                fields=fields,
            )],
        )
        with mock.patch.object(control_bot_module, "state", manager):
            control_bot_module._parse_dashboard_message(message)
        alt = manager.get(1)
        self.assertEqual(alt.total_sent, 3)
        self.assertEqual(alt.total_errors, 2)
        self.assertEqual(alt.deal_keywords, ["Blade Ball", "BB token", "BB"])
        self.assertEqual(alt.channels["154"]["alive"], False)

    def test_dashboard_uses_current_heartbeat_counters_and_workflow_state(self):
        from control_bot.dashboard import build_summary_embed

        manager = AltStateManager({1: "Seller A"}, alt_ids=[1])
        manager.update_from_heartbeat(1, {
            "status": "active", "total_sent": 12, "total_errors": 2,
            "deal_alerts": 4, "active_channels": 2, "total_channels": 2,
            "interval_min": 3, "runtime_hours": 48,
        })
        manager.set_workflow(1, 77, "in_progress")
        embed = build_summary_embed(manager)
        text = (embed.description or "") + "\n" + "\n".join(field.value for field in embed.fields)
        self.assertIn("sent **12**", text)
        self.assertIn("err **2**", text)
        self.assertIn("Deal alerts: **4**", text)
        self.assertIn("run `in_progress`", text)
        self.assertIn("cadence **3m/48h**", text)

    def test_normal_user_message_in_deals_channel_cannot_create_phantom_deal(self):
        manager = AltStateManager({1: "Seller A"}, alt_ids=[1])
        message = SimpleNamespace(
            id=9001,
            channel=SimpleNamespace(id=123),
            author=SimpleNamespace(bot=False, display_name="Seller A", name="Seller A"),
            content="ordinary chat message",
            embeds=[],
            webhook_id=None,
        )
        with mock.patch.object(control_bot_module, "state", manager), \
             mock.patch.object(config, "DEALS_CH_ID", 123):
            asyncio.run(control_bot_module._handle_guild_webhook_message(message))
        self.assertEqual(manager.get(1).deal_alerts, 0)
        self.assertEqual(manager.recent_logs(1), [])

    def test_separate_deal_event_updates_recency_and_typed_log_without_incrementing_total(self):
        manager = AltStateManager({1: "Seller A"}, alt_ids=[1])
        manager.update_from_heartbeat(1, {"deal_alerts": 7})
        message = SimpleNamespace(content="deal match", embeds=[])
        with mock.patch.object(control_bot_module, "state", manager):
            control_bot_module._parse_deal_message(1, message)
        alt = manager.get(1)
        self.assertEqual(alt.deal_alerts, 7)
        self.assertGreater(alt.last_deal_ts, 0)
        self.assertEqual(alt.log_counts["DEAL"], 1)


if __name__ == "__main__":
    unittest.main()
