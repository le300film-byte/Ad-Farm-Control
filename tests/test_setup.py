"""Network-free setup validation regressions."""
from __future__ import annotations

import os
import unittest
from unittest import mock

import setup as bootstrap_module


@unittest.skip("V8: setup.py fully rebuilt as a one-time server installer. V8-specific setup tests are in test_v8_round1_functional.py")
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

    def test_interactive_setup_runtime_option_prompts(self):
        """Verify interactive setup prompts for all flags with explanation and accepts yes/no."""
        bootstrap = bootstrap_module.Bootstrap(non_interactive=False)

        # Mock yes_no answers: quick=yes, force=no, forums=yes, upgrade=yes, abort=no
        with mock.patch.object(bootstrap, "yes_no", side_effect=[True, False, True, True, False]):
            bootstrap.collect_runtime_options()

        self.assertTrue(bootstrap.quick)
        self.assertFalse(bootstrap.force)
        self.assertTrue(bootstrap.use_forums)
        self.assertTrue(bootstrap.upgrade_forums)
        self.assertFalse(bootstrap.abort_on_failure)

    def test_cli_flags_override_interactive_prompts(self):
        """Verify CLI flags override interactive prompts and do not ask for explicitly passed flags."""
        bootstrap = bootstrap_module.Bootstrap(
            non_interactive=False,
            quick=False,
            force=True,
            use_forums=False,
            upgrade_forums=False,
            abort_on_failure=True,
        )

        with mock.patch.object(bootstrap, "yes_no") as mock_yes_no:
            bootstrap.collect_runtime_options()
            # None of the flags should trigger yes_no since all were passed via CLI
            mock_yes_no.assert_not_called()

        self.assertFalse(bootstrap.quick)
        self.assertTrue(bootstrap.force)
        self.assertFalse(bootstrap.use_forums)
        self.assertFalse(bootstrap.upgrade_forums)
        self.assertTrue(bootstrap.abort_on_failure)


if __name__ == "__main__":
    unittest.main()
