"""Network-free setup validation regressions."""
from __future__ import annotations

import os
import unittest
from unittest import mock

import setup as bootstrap_module


class SetupValidationTests(unittest.TestCase):
    def test_noninteractive_setup_accepts_multiple_owners_and_names_only_channels(self):
        values = {
            "BOT_TOKEN": "bot-token-for-test",
            "OWNER_IDS": "123, 456,123",
            "GUILD_ID": "999999999999999999",
            "CHANNEL_IDS": "",
            "CHANNEL_NAMES": "market, trading",
        }
        bootstrap = bootstrap_module.Bootstrap(non_interactive=True)
        with mock.patch.dict(os.environ, values, clear=False), mock.patch.object(
            bootstrap, "discord", return_value=(200, {"id": "777", "username": "bot"})
        ):
            bootstrap.collect_discord_inputs()

        self.assertEqual(bootstrap.owner_ids, ["123", "456"])
        self.assertEqual(bootstrap.guild_id, values["GUILD_ID"])
        self.assertEqual(bootstrap.channel_ids, "")
        self.assertEqual(bootstrap.channel_names, "market,trading")

    def test_noninteractive_setup_rejects_empty_channel_targets(self):
        values = {
            "BOT_TOKEN": "bot-token-for-test",
            "OWNER_IDS": "123",
            "GUILD_ID": "999999999999999999",
            "CHANNEL_IDS": "",
            "CHANNEL_NAMES": "",
        }
        bootstrap = bootstrap_module.Bootstrap(non_interactive=True)
        with mock.patch.dict(os.environ, values, clear=False), mock.patch.object(
            bootstrap, "discord", return_value=(200, {"id": "777"})
        ), self.assertRaises(bootstrap_module.SetupError):
            bootstrap.collect_discord_inputs()


if __name__ == "__main__":
    unittest.main()
