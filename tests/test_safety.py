"""Focused regression tests for the sender/control safety gates.

These tests are intentionally network-free. They exercise the two failure modes
that are easiest to regress while changing the long-running workers: the
pre-READY gateway timeout and post-send typo-edit accounting.
"""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock


# send_ads.py validates its required configuration at import time. Keep this
# test fixture self-contained and never use real credentials or channel IDs.
os.environ.setdefault("USER_TOKEN", "TEST_TOKEN")
os.environ.setdefault("CHANNEL_IDS", "111111111111111111")
os.environ.setdefault("AD_TYPE", "sell")
os.environ.setdefault("MESSAGE", "SELLING BB LF 2.5$/1K DM ME QUICK")
os.environ.setdefault("ATTACH_IMAGE", "no")

import send_ads  # noqa: E402  (environment fixture must be installed first)


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
                "run", "stop", "pause", "resume", "setprice", "setmode",
                "setmessage", "setchannel", "replacechannel", "setinterval", "setruntime",
                "sync", "status", "logs", "deals", "refresh", "dashboard", "help",
            },
        )

    def test_state_manager_ignores_orphaned_names_and_unknown_events(self):
        from control_bot.alt_state import AltStateManager

        manager = AltStateManager({1: "Configured", 4: "Orphaned name"}, alt_ids=[1])
        self.assertEqual(manager.alt_ids, [1])
        manager.update_from_heartbeat(4, {"status": "active"})
        manager.append_log(4, "must not create a phantom log buffer")
        self.assertIsNone(manager.get(4))
        self.assertEqual(manager.recent_logs(4), [])

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
