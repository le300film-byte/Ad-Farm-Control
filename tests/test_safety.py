"""Focused regression tests for the sender/control safety gates.

These tests are intentionally network-free. They exercise the two failure modes
that are easiest to regress while changing the long-running workers: the
pre-READY gateway timeout and post-send typo-edit accounting.
"""
from __future__ import annotations

import asyncio
import json
import os
import unittest
from pathlib import Path
from unittest import mock


# send_ads.py validates its required configuration at import time. Keep this
# test fixture self-contained and never use real credentials or channel IDs.
os.environ.setdefault("USER_TOKEN", "TEST_TOKEN")
os.environ.setdefault("CHANNEL_IDS", "111111111111111111")
os.environ.setdefault("AD_TYPE", "sell")
os.environ.setdefault("MESSAGE", "SELLING BB LF 2.5$/1K DM ME QUICK")
os.environ.setdefault("ATTACH_IMAGE", "no")

import send_ads  # noqa: E402  (environment fixture must be installed first)
from control_bot import bot as control_bot_module  # noqa: E402


class _Timeout(Exception):
    pass


class _FakeGatewaySocket:
    def __init__(self):
        self.receives = 0
        self.timeouts: list[float] = []
        self.closed = False

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def recv(self) -> str:
        if self.receives == 0:
            self.receives += 1
            return json.dumps({"op": 10, "d": {"heartbeat_interval": 1000}})
        raise _Timeout()

    def send(self, _payload: str) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _FakeWebsocketModule:
    WebSocketTimeoutException = _Timeout

    def __init__(self, socket: _FakeGatewaySocket):
        self.socket = socket

    def create_connection(self, _url: str, **_kwargs):
        return self.socket


class _NoopThread:
    """Prevent the heartbeat helper thread from running in this unit test."""

    def __init__(self, *_args, **_kwargs):
        pass

    def start(self):
        pass


class SafetyRegressionTests(unittest.TestCase):
    def test_gateway_timeout_fails_closed_before_ready(self):
        socket = _FakeGatewaySocket()
        clock_values = iter((100.0, 100.0, 131.0, 131.0))

        gateway = send_ads.GatewayThread(
            "TEST_TOKEN", "Trading", "", lambda _msg: None, lambda _msg: None
        )
        with mock.patch.object(gateway, "_get_gateway_url", return_value="wss://gateway.test"), \
             mock.patch.object(send_ads, "_ws", _FakeWebsocketModule(socket), create=True), \
             mock.patch.object(send_ads.threading, "Thread", _NoopThread), \
             mock.patch.object(send_ads.time, "time", side_effect=lambda: next(clock_values, 131.0)), \
             mock.patch.object(send_ads.time, "sleep", return_value=None):
            gateway._connect_once()

        self.assertTrue(gateway.location_verify_failed)
        self.assertTrue(gateway._stop.is_set())
        self.assertTrue(send_ads._new_location_failed_event.is_set())
        self.assertTrue(socket.closed)

        # Leave module-level state clean for another test process/import.
        send_ads._new_location_failed_event.clear()

    def test_typo_edit_success_counts_only_after_edit_succeeds(self):
        original = "SELLING BB LF 2.5$/1K DM ME QUICK"
        calls: list[tuple[str, str, str]] = []

        def immediate_thread(*, target, daemon=True):
            class _Immediate:
                def start(self_inner):
                    target()

            return _Immediate()

        def fake_edit(cid, msg_id, text):
            calls.append((cid, msg_id, text))
            return True

        roll_count = 0
        def roll_after_gate():
            nonlocal roll_count
            roll_count += 1
            return 0.0 if roll_count <= 2 else 1.0

        with mock.patch.object(send_ads, "public_activity_allowed", return_value=True), \
             mock.patch.object(send_ads, "_qwerty_typo", return_value="SELLING BB LF 2.5$/1K DM ME QUICJ"), \
             mock.patch.object(send_ads.random, "random", side_effect=roll_after_gate), \
             mock.patch.object(send_ads, "edit_message", side_effect=fake_edit), \
             mock.patch.object(send_ads.threading, "Thread", side_effect=immediate_thread):
            send_ads.total_edits = 0
            send_ads.maybe_typo_edit("111111111111111111", "222222222222222222", original)

        self.assertEqual(send_ads.total_edits, 1)
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(calls[0][2], original)

        def failed_edit(_cid, _msg_id, _text):
            return False

        roll_count = 0
        def roll_after_gate_failure():
            nonlocal roll_count
            roll_count += 1
            return 0.0 if roll_count <= 2 else 1.0

        with mock.patch.object(send_ads, "public_activity_allowed", return_value=True), \
             mock.patch.object(send_ads, "_qwerty_typo", return_value="SELLING BB LF 2.5$/1K DM ME QUICJ"), \
             mock.patch.object(send_ads.random, "random", side_effect=roll_after_gate_failure), \
             mock.patch.object(send_ads, "edit_message", side_effect=failed_edit), \
             mock.patch.object(send_ads.threading, "Thread", side_effect=immediate_thread):
            send_ads.total_edits = 0
            send_ads.maybe_typo_edit("111111111111111111", "222222222222222222", original)

        self.assertEqual(send_ads.total_edits, 0)

    def test_all_slash_commands_are_registered_including_help(self):
        from control_bot import bot as control_bot_module

        names = {command.name for command in control_bot_module.bot.tree.get_commands()}
        self.assertEqual(
            names,
            {
                "run", "stop", "pause", "resume", "altadd", "altupdate", "altlist", "altremove",
                "setprice", "setmode", "setmessage", "setdealkeywords", "setdealscan", "setdealdelta", "setchannel", "replacechannel", "setinterval", "setruntime",
                "sync", "status", "logs", "deals", "pingalt", "selfcheck", "clearlogs", "runs", "refresh", "dashboard", "help",
            },
        )

    def test_state_manager_add_update_remove_alt_lifecycle_is_live_and_bounded(self):
        from control_bot.alt_state import AltStateManager

        manager = AltStateManager({1: "Configured"}, alt_ids=[1])
        self.assertTrue(manager.add_alt(2, "Second"))
        self.assertFalse(manager.add_alt(2, "Duplicate"))
        self.assertTrue(manager.update_identity(2, name="Second updated"))
        self.assertEqual(manager.get(2).name, "Second updated")
        self.assertTrue(manager.remove_alt(2))
        self.assertFalse(manager.remove_alt(2))

    def test_state_manager_ignores_orphaned_names_and_unknown_events(self):
        from control_bot.alt_state import AltStateManager

        manager = AltStateManager({1: "Configured", 4: "Orphaned name"}, alt_ids=[1])
        self.assertEqual(manager.alt_ids, [1])
        manager.update_from_heartbeat(4, {"status": "active"})
        manager.append_log(4, "must not create a phantom log buffer")
        self.assertIsNone(manager.get(4))
        self.assertEqual(manager.recent_logs(4), [])

    def test_authorized_runtime_channel_commands_update_scheduler_refs_and_ack(self):
        replies = []
        stats = {"111": {"sent": 0, "errors": 0, "skipped": 0}}
        with mock.patch.object(send_ads, "CONTROLLER_USER_IDS", {"owner"}), \
             mock.patch.object(send_ads, "CHANNEL_IDS", ["111"]), \
             mock.patch.object(send_ads, "_active_ch_ref", ["111"]), \
             mock.patch.object(send_ads, "_ch_names_ref", {"111": "old"}), \
             mock.patch.object(send_ads, "_slowmodes_ref", {"111": 0}), \
             mock.patch.object(send_ads, "_last_sent_ref", {}), \
             mock.patch.object(send_ads, "_my_last_msg_id_ref", {}), \
             mock.patch.object(send_ads, "_stats_ref", stats), \
             mock.patch.object(send_ads, "_dead_channels_ref", set()), \
             mock.patch.object(send_ads, "_next_post_ref", {}), \
             mock.patch.object(send_ads, "_controller_reply", side_effect=lambda _cid, text: replies.append(text)), \
             mock.patch.object(send_ads, "event_log"), \
             mock.patch.object(send_ads, "send_log_webhook"), \
             mock.patch.object(send_ads, "get_channel_info", return_value={"type": 0, "guild_id": "999", "name": "new-room", "rate_limit_per_user": 3}), \
             mock.patch.object(send_ads, "INTERVAL_MIN", 5), \
             mock.patch.object(send_ads, "_runtime_run_end", 0.0):
            self.assertFalse(send_ads._handle_controller_dm("dm", "other", "!setinterval 3"))
            self.assertTrue(send_ads._handle_controller_dm("dm", "owner", "!setinterval 3"))
            self.assertEqual(send_ads.INTERVAL_MIN, 3)
            self.assertTrue(send_ads._handle_controller_dm("dm", "owner", "!setchannel 222 fresh"))
            self.assertIn("222", send_ads.CHANNEL_IDS)
            self.assertIn("222", stats)
            self.assertTrue(send_ads._handle_controller_dm("dm", "owner", "!replacechannel 111 333 replacement"))
            self.assertEqual(send_ads.CHANNEL_IDS, ["333", "222"])
            self.assertNotIn("111", send_ads._ch_names_ref)
            self.assertEqual(send_ads._ch_names_ref["333"], "new-room")
            self.assertGreaterEqual(len(replies), 3)

    def test_deal_webhook_is_separate_from_dashboard_webhook(self):
        source = Path("send_ads.py").read_text(encoding="utf-8")
        start = source.index("def send_deal_webhook")
        end = source.index("def _dashboard_startup_embed", start)
        block = source[start:end]
        self.assertIn("target = DEAL_WEBHOOK_URL", block)
        self.assertNotIn("target = DASHBOARD_WEBHOOK_URL", block)

    def test_deal_scanner_requires_a_configured_item_alias(self):
        calls = []
        messages = [
            {"id": "1", "content": "SELLING BB tokens 2.10$/1k", "author": {"id": "seller"}},
            {"id": "2", "content": "SELLING Robux 2.10$/1k", "author": {"id": "seller2"}},
        ]
        with mock.patch.object(send_ads, "DEAL_SCAN_ENABLED", True), \
             mock.patch.object(send_ads, "DEAL_MY_RATE", 2.35), \
             mock.patch.object(send_ads, "DEAL_ITEM_KEYWORDS", ["Blade Ball", "BB token", "BB tokens", "BB"]), \
             mock.patch.object(send_ads, "_runtime_deal_keywords", None), \
             mock.patch.object(send_ads, "_get_active_ad_type", return_value="buy"), \
             mock.patch.object(send_ads, "_send_deal_alert", side_effect=lambda *args: calls.append(args)):
            send_ads.scan_deals("111", messages)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][-1], "BB tokens")

    def test_deal_scanner_controls_update_runtime_without_affecting_ad_mode(self):
        old_enabled = send_ads._runtime_deal_scan_enabled
        old_delta = send_ads._runtime_deal_delta
        replies = []
        try:
            with mock.patch.object(send_ads, "CONTROLLER_USER_IDS", {"owner"}), \
                 mock.patch.object(send_ads, "event_log"), \
                 mock.patch.object(send_ads, "send_log_webhook"), \
                 mock.patch.object(send_ads, "_controller_reply", side_effect=lambda _cid, text: replies.append(text)):
                self.assertTrue(send_ads._handle_controller_dm("dm", "owner", "!setdealscan off"))
                self.assertFalse(send_ads._get_active_deal_scan_enabled())
                self.assertTrue(send_ads._handle_controller_dm("dm", "owner", "!setdealdelta 0.25"))
                self.assertEqual(send_ads._get_active_deal_delta(), 0.25)
        finally:
            send_ads._runtime_deal_scan_enabled = old_enabled
            send_ads._runtime_deal_delta = old_delta

    def test_control_gist_transport_does_not_attempt_a_dm(self):
        async def run():
            with mock.patch.object(control_bot_module.config, "CONTROL_GIST_ID", "gist-id"), \
                 mock.patch.object(control_bot_module.github_api, "queue_control_command", return_value=(True, "command-id")), \
                 mock.patch.object(control_bot_module, "_send_dm_wait_ack", new=mock.AsyncMock(side_effect=AssertionError("DM must not be used"))):
                return await control_bot_module._send_control_wait_ack(1, "!replacechannel 111 222 Jace")
        result = asyncio.run(run())
        self.assertTrue(result.startswith("🕒 queued via control Gist"))

    def test_inconclusive_message_verification_cannot_enter_caution(self):
        cid = "111111111111111111"
        with send_ads._state_lock:
            send_ads._channel_verify_history.pop(cid, None)
            send_ads._caution_channels.pop(cid, None)
        send_ads._record_verification(cid, "222222222222222222", None)
        with send_ads._state_lock:
            self.assertNotIn(cid, send_ads._channel_verify_history)
            self.assertFalse(send_ads._caution_channels.get(cid, False))

    def test_deal_recency_does_not_double_count_heartbeat_totals_and_replace_is_atomic(self):
        from control_bot.alt_state import AltStateManager

        manager = AltStateManager({1: "Configured"}, alt_ids=[1])
        manager.update_from_heartbeat(1, {"deal_alerts": 4, "last_deal_ts": 123.0})
        manager.set_channel(1, "111", "old")
        manager.mark_deal_seen(1)
        alt = manager.get(1)
        self.assertEqual(alt.deal_alerts, 4)
        self.assertGreater(alt.last_deal_ts, 0)
        manager.replace_channel(1, "111", "222", "new")
        self.assertNotIn("111", alt.channels)
        self.assertEqual(alt.channels["222"]["name"], "new")

    def test_malformed_heartbeat_values_are_ignored_without_dashboard_crash(self):
        from control_bot.alt_state import AltStateManager
        from control_bot.dashboard import build_all

        manager = AltStateManager({1: "Configured"}, alt_ids=[1])
        manager.update_from_heartbeat(1, {
            "type": "heartbeat",
            "ad_type": {"unexpected": "object"},
            "rate": float("inf"),
            "total_sent": float("inf"),
            "uptime_sec": "not-a-number",
            "status": ["invalid"],
            "warnings": ["safe warning"],
        })
        alt = manager.get(1)
        self.assertIsNotNone(alt)
        self.assertEqual(alt.ad_type, "")
        self.assertIsNone(alt.rate)
        self.assertEqual(alt.total_sent, 0)
        self.assertEqual(alt.uptime_sec, 0.0)
        self.assertEqual(alt.status, "offline")
        self.assertEqual(len(build_all(manager)), 3)


if __name__ == "__main__":
    unittest.main()
