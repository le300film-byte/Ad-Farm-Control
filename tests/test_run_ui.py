"""Network-free regression tests for the V6 private /run setup UI."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from control_bot import bot as control_bot_module


class _Alt:
    name = "Test alt"
    ad_type = "sell"


class _State:
    alt_ids = [1]

    @staticmethod
    def get(alt_id):
        return _Alt() if alt_id == 1 else None


class RunUiTests(unittest.TestCase):
    def test_start_view_preserves_all_choices_without_nested_modals(self):
        with mock.patch.object(control_bot_module, "state", _State()):
            view = control_bot_module.RunStartView(owner_id=123)
        # Four select rows plus two action buttons; image/style are validated
        # in the mode-specific modal so Discord's five-row limit is respected.
        self.assertEqual(len(view.children), 6)
        self.assertEqual([type(child).__name__ for child in view.children[:4]], ["Select"] * 4)
        self.assertEqual([type(child).__name__ for child in view.children[4:]], ["Button", "Button"])
        self.assertEqual([o.value for o in view.alt_select.options], ["1"])
        self.assertEqual([o.value for o in view.mode_select.options], ["sell", "buy"])
        self.assertEqual([o.value for o in view.interval_select.options], ["3", "5"])
        self.assertEqual([o.value for o in view.runtime_select.options], ["6", "12", "18", "24", "48", "0"])

    def test_mode_modal_has_validation_fields_and_no_nested_modal(self):
        with mock.patch.object(control_bot_module, "state", _State()):
            view = control_bot_module.RunStartView(owner_id=123)
        view.ad_type = "sell"
        sell = control_bot_module.RunDetailsModal(view)
        view.ad_type = "buy"
        buy = control_bot_module.RunDetailsModal(view)
        self.assertEqual([item.custom_id for item in sell.children], ["sell_rate", "sell_extra", "attach_image"])
        self.assertEqual([item.custom_id for item in buy.children], ["buy_rate", "buy_rate_rap", "buy_simple_text", "buy_style", "attach_image"])
        source = __import__("pathlib").Path("control_bot/bot.py").read_text(encoding="utf-8")
        self.assertNotIn("send_modal(RunOptionsModal", source)

    def test_validation_preserves_modal_constraints(self):
        # The operator's raw message is the only required field. Rates and RAP
        # are optional metadata, exactly as the /run contract documents.
        with mock.patch.object(control_bot_module, "state", _State()):
            sell_errors, sell_parsed = control_bot_module._validate_run_values({
                "alt_id": "1", "ad_type": "sell", "sell_rate": "2.5$",
                "sell_extra": "Selling stock, DM me", "interval_min": "5",
                "total_hours": "6", "attach_image": "yes",
            })
            buy_errors, buy_parsed = control_bot_module._validate_run_values({
                "alt_id": "1", "ad_type": "buy", "buy_rate": "2.2",
                "buy_rate_rap": "1.8", "buy_style": "detailed",
                "buy_simple_text": "Buying stock, DM me", "interval_min": "3",
                "total_hours": "48", "attach_image": "no",
            })
            limitless_errors, limitless_parsed = control_bot_module._validate_run_values({
                "alt_id": "1", "ad_type": "sell", "sell_rate": "2.5$",
                "sell_extra": "Always on", "interval_min": "5",
                "total_hours": "0", "attach_image": "yes",
            })
        self.assertEqual(sell_errors, [])
        self.assertEqual(sell_parsed["rate"], 2.5)
        self.assertEqual(buy_errors, [])
        self.assertEqual(buy_parsed["rate"], 2.2)
        self.assertEqual(buy_parsed["rap"], 1.8)
        self.assertEqual(limitless_errors, [])
        self.assertEqual(limitless_parsed["hours"], 0)

    def test_raw_message_is_required_and_rates_are_optional(self):
        with mock.patch.object(control_bot_module, "state", _State()):
            # No item name, price, or RAP: only the raw copy.
            errors, parsed = control_bot_module._validate_run_values({
                "alt_id": "1", "ad_type": "sell", "sell_rate": "",
                "sell_extra": "Anyone buying today?", "interval_min": "5",
                "total_hours": "6", "attach_image": "no",
            })
            self.assertEqual(errors, [])
            self.assertIsNone(parsed["rate"])
            self.assertEqual(parsed["raw_message"], "Anyone buying today?")
            self.assertEqual(parsed["targets"], [1])

            missing, _ = control_bot_module._validate_run_values({
                "alt_id": "1", "ad_type": "sell", "sell_rate": "2.50",
                "sell_extra": "   ", "interval_min": "5",
                "total_hours": "6", "attach_image": "no",
            })
            self.assertTrue(any("raw message" in item.lower() for item in missing))

    def test_squad_targets_expand_and_unknown_squads_are_rejected(self):
        class _SquadState(_State):
            @staticmethod
            def get_squad_members(name):
                return [] if str(name).lower() == "ghost" else [
                    SimpleNamespace(alt_id=1), SimpleNamespace(alt_id=2),
                ]

            @staticmethod
            def get_all_squads():
                return {"Alpha": [1, 2]}

        with mock.patch.object(control_bot_module, "state", _SquadState()):
            errors, parsed = control_bot_module._validate_run_values({
                "alt_id": "1", "squad": "Alpha", "ad_type": "sell",
                "sell_rate": "", "sell_extra": "Squad blast",
                "interval_min": "5", "total_hours": "6", "attach_image": "no",
            })
            self.assertEqual(errors, [])
            self.assertEqual(parsed["squad"], "Alpha")
            self.assertEqual(parsed["targets"], [1, 2])

            ghost_errors, _ = control_bot_module._validate_run_values({
                "alt_id": "1", "squad": "Ghost", "ad_type": "sell",
                "sell_rate": "", "sell_extra": "Nobody home",
                "interval_min": "5", "total_hours": "6", "attach_image": "no",
            })
            self.assertTrue(any("Ghost" in item for item in ghost_errors))


if __name__ == "__main__":
    unittest.main()
