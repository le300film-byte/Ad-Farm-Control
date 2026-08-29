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

    def test_cmd_settings_returns_clean_embed(self):
        manager = AltStateManager({1: "Seller A"}, alt_ids=[1])
        manager.update_from_heartbeat(1, {"status": "active", "total_sent": 5, "interval_min": 3})
        inter = _Interaction()
        with mock.patch.object(control_bot_module, "state", manager), \
             mock.patch.object(config, "OWNER_IDS", {42}), \
             mock.patch.object(control_bot_module, "_cooldowns", {}):
            asyncio.run(control_bot_module.cmd_settings.callback(inter, alt=1))
        self.assertTrue(inter.response.deferred)
        embed = inter.response.messages[0][1]["embed"]
        self.assertIn("Seller A", embed.title)

    def test_cmd_rescan_and_resetcaution_dispatch(self):
        manager = AltStateManager({1: "Seller A"}, alt_ids=[1])
        inter = _Interaction()
        with mock.patch.object(control_bot_module, "state", manager), \
             mock.patch.object(config, "OWNER_IDS", {42}), \
             mock.patch.object(control_bot_module, "_cooldowns", {}), \
             mock.patch.object(control_bot_module, "_send_dm_wait_ack", new=mock.AsyncMock(return_value="✅ ACK")), \
             mock.patch.object(control_bot_module, "_log_control", new=mock.AsyncMock()):
            asyncio.run(control_bot_module.cmd_rescan_channels.callback(inter, alt=1))
            self.assertIn("rescan", inter.followup.messages[0][0][0])

            control_bot_module._cooldowns.clear()
            inter2 = _Interaction()
            asyncio.run(control_bot_module.cmd_resetcaution.callback(inter2, alt=1, channel_id="111"))
            self.assertIn("reset caution", inter2.followup.messages[0][0][0])

    def test_setchannel_and_replacechannel_persist_secrets(self):
        manager = AltStateManager({1: "Seller A"}, alt_ids=[1])
        inter = _Interaction()
        with mock.patch.object(control_bot_module, "state", manager), \
             mock.patch.object(config, "OWNER_IDS", {42}), \
             mock.patch.object(config, "ALT_REPOS", {1: "org/alt-repo"}), \
             mock.patch.object(config, "GITHUB_TOKEN", "gh-token"), \
             mock.patch.object(control_bot_module, "_cooldowns", {}), \
             mock.patch.object(control_bot_module, "_send_dm_wait_ack", new=mock.AsyncMock(return_value="✅ ACK")), \
             mock.patch.object(control_bot_module.github_api, "set_repository_secret") as set_secret, \
             mock.patch.object(control_bot_module, "_log_control", new=mock.AsyncMock()):
            asyncio.run(control_bot_module.cmd_setchannel.callback(inter, alt=1, channel_id="111222333", name="general"))
            set_secret.assert_called_with("org/alt-repo", "CHANNEL_IDS", "111222333")

            control_bot_module._cooldowns.clear()
            inter2 = _Interaction()
            asyncio.run(control_bot_module.cmd_replacechannel.callback(inter2, alt=1, old_id="111222333", new_id="444555666", name="trading"))
            set_secret.assert_called_with("org/alt-repo", "CHANNEL_IDS", "444555666")

    def test_cmd_channels_view_opens_embed(self):
        manager = AltStateManager({1: "Seller A"}, alt_ids=[1])
        manager.set_channel(1, "111222", "trading")
        inter = _Interaction()
        with mock.patch.object(control_bot_module, "state", manager), \
             mock.patch.object(config, "OWNER_IDS", {42}), \
             mock.patch.object(control_bot_module, "_cooldowns", {}):
            asyncio.run(control_bot_module.cmd_channels.callback(inter, alt=1))
        self.assertTrue(inter.response.deferred)
        embed = inter.response.messages[0][1]["embed"]
        self.assertIn("Channel Manager · Seller A", embed.title)

    def test_cmd_uploadimage_commits_to_repo(self):
        manager = AltStateManager({1: "Seller A"}, alt_ids=[1])
        inter = _Interaction()
        attachment = SimpleNamespace(
            content_type="image/png",
            size=1024,
            url="https://cdn.discordapp.com/attachments/1/2/ad.png",
            read=mock.AsyncMock(return_value=b"fake-png-data"),
        )
        with mock.patch.object(control_bot_module, "state", manager), \
             mock.patch.object(config, "OWNER_IDS", {42}), \
             mock.patch.object(config, "ALT_REPOS", {1: "org/alt-repo"}), \
             mock.patch.object(control_bot_module, "_cooldowns", {}), \
             mock.patch.object(control_bot_module.github_api, "upload_repository_file", return_value=(True, "File committed")):
            asyncio.run(control_bot_module.cmd_uploadimage.callback(inter, alt=1, image=attachment))
        self.assertTrue(inter.response.deferred)
        embed = inter.followup.messages[0][1]["embed"]
        self.assertIn("Ad Image Upload", embed.title)
        self.assertIn("Seller A", embed.description)

    def test_cmd_diagnose_and_topology(self):
        manager = AltStateManager({1: "Seller A"}, alt_ids=[1])
        manager.update_from_heartbeat(1, {"status": "active", "total_sent": 10})
        manager.record_causal_event(1, "test_event", "Test causal description")
        inter = _Interaction()
        with mock.patch.object(control_bot_module, "state", manager), \
             mock.patch.object(config, "OWNER_IDS", {42}), \
             mock.patch.object(control_bot_module, "_cooldowns", {}), \
             mock.patch.object(control_bot_module, "_fresh_state", new=mock.AsyncMock()):
            asyncio.run(control_bot_module.cmd_diagnose.callback(inter, alt=1))
        self.assertTrue(inter.response.deferred)
        embed = inter.followup.messages[0][1]["embed"]
        self.assertIn("Causal Event Explorer", embed.title)

        inter2 = _Interaction()
        with mock.patch.object(control_bot_module, "state", manager), \
             mock.patch.object(config, "OWNER_IDS", {42}), \
             mock.patch.object(control_bot_module, "_cooldowns", {}), \
             mock.patch.object(control_bot_module, "_fresh_state", new=mock.AsyncMock()):
            asyncio.run(control_bot_module.cmd_topology.callback(inter2))
        self.assertTrue(inter2.response.deferred)
        embed2 = inter2.followup.messages[0][1]["embed"]
        self.assertIn("TOPOLOGY", embed2.title)

    def test_cmd_simulate_policy_squad_and_canary(self):
        manager = AltStateManager({1: "Seller A"}, alt_ids=[1])
        inter_sim = _Interaction()
        with mock.patch.object(control_bot_module, "state", manager), \
             mock.patch.object(config, "OWNER_IDS", {42}), \
             mock.patch.object(control_bot_module, "_cooldowns", {}):
            asyncio.run(control_bot_module.cmd_simulate.callback(inter_sim, alt=1, test_rate=1.25))
        embed_sim = inter_sim.response.messages[0][1]["embed"]
        self.assertIn("Simulation", embed_sim.title)

        inter_pol = _Interaction()
        with mock.patch.object(control_bot_module, "state", manager), \
             mock.patch.object(config, "OWNER_IDS", {42}), \
             mock.patch.object(control_bot_module, "_cooldowns", {}):
            asyncio.run(control_bot_module.cmd_policy.callback(inter_pol, alt=1, template="stealth"))
        self.assertIn("STEALTH", inter_pol.response.messages[0][0][0])
        self.assertEqual(manager.get(1).policy_template, "stealth")

        inter_sq = _Interaction()
        with mock.patch.object(control_bot_module, "state", manager), \
             mock.patch.object(config, "OWNER_IDS", {42}), \
             mock.patch.object(control_bot_module, "_cooldowns", {}):
            asyncio.run(control_bot_module.cmd_squad.callback(inter_sq, action="assign", alt=1, squad_name="Alpha Sellers"))
        self.assertIn("Alpha Sellers", inter_sq.response.messages[0][0][0])
        self.assertEqual(manager.get(1).squad, "Alpha Sellers")

        inter_canary = _Interaction()
        with mock.patch.object(control_bot_module, "state", manager), \
             mock.patch.object(config, "OWNER_IDS", {42}), \
             mock.patch.object(control_bot_module, "_cooldowns", {}), \
             mock.patch.object(control_bot_module.github_api, "get_authenticated_user", return_value={"login": "test"}), \
             mock.patch.object(control_bot_module.github_api, "fetch_gist", return_value={"id": "fake"}):
            asyncio.run(control_bot_module.cmd_canary.callback(inter_canary, alt=1))
        self.assertTrue(inter_canary.response.deferred)
        embed_can = inter_canary.followup.messages[0][1]["embed"]
        self.assertIn("CANARY", embed_can.title)

    def test_cmd_reply_and_fleet_tuning_view(self):
        manager = AltStateManager({1: "Seller A"}, alt_ids=[1])
        inter_reply = _Interaction()
        with mock.patch.object(control_bot_module, "state", manager), \
             mock.patch.object(config, "OWNER_IDS", {42}), \
             mock.patch.object(control_bot_module, "_cooldowns", {}), \
             mock.patch.object(control_bot_module, "_send_dm_wait_ack", new=mock.AsyncMock(return_value="✅ ACK")):
            asyncio.run(control_bot_module.cmd_reply.callback(inter_reply, alt=1, user="123456789012345678", text="100k in stock, $0.85/1k"))
            self.assertTrue(inter_reply.response.deferred)
            self.assertIn("Reply Queued", inter_reply.followup.messages[0][0][0])

            view = control_bot_module.FleetTuningView(owner_id=42, alt_id=1)
            embed = view._build_embed()
            self.assertIn("Fleet Tuning & Settings", embed.title)


if __name__ == "__main__":
    unittest.main()
