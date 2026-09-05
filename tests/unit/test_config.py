"""config: Settings parsing, worker discovery, validation, redaction."""
import json

from adfarm.config import Settings


def test_defaults_and_problems_fail_closed():
    s = Settings.from_env({})
    problems = s.problems()
    assert any("BOT_TOKEN" in p for p in problems)
    assert any("OWNER_IDS" in p for p in problems)
    assert any("worker" in p.lower() for p in problems)
    assert s.owner_ids == frozenset()


def test_workers_from_all_three_conventions():
    env = {
        "WORKER_TOKENS": "w1:t1,w2:t2",
        "WORKER_GITHUB_OWNERS": "w2,w3",
        "WORKER_TOKENS_LIST": "t2b,t3",
        "WORKER_3_USER": "w3",
        "WORKER_3_TOKEN": "t3c",
        "WORKER_1_USER": "w4",
        "WORKER_1_TOKEN": "t4",
    }
    s = Settings.from_env(env)
    logins = [w.login for w in s.workers]
    assert logins == ["w1", "w2", "w3", "w4"]
    assert s.worker_for("W2").token == "t2"          # first definition wins
    assert s.worker_for("w3").token == "t3"


def test_tuning_json_fills_missing_values_only():
    env = {"OWNER_IDS": "1", "TUNING_JSON": json.dumps({"OWNER_IDS": "2,3", "PAYMENT_ADDRESS": "0xabc", "EXPIRY_SCAN_INTERVAL_SEC": 10, "flag": True})}
    s = Settings.from_env(env)
    assert s.owner_ids == frozenset({"1"})
    assert s.payment_address == "0xabc"
    assert s.expiry_scan_interval == 10
    assert s.extra["flag"] == "true"


def test_redacted_hides_tokens():
    s = Settings.from_env({"BOT_TOKEN": "secret", "GH_TOKEN": "ghp_x", "WORKER_TOKENS": "w1:t1", "OWNER_IDS": "1"})
    red = s.redacted()
    assert red["bot_token"] == "***" and red["github_token"] == "***"
    assert red["workers"] == ["w1:***"]
    assert "secret" not in json.dumps(red)


def test_owner_ids_ignore_non_numeric_and_controller_ids_include_owners():
    s = Settings.from_env({"OWNER_IDS": "123,abc, 456 ", "CONTROLLER_USER_IDS": "789"})
    assert s.owner_ids == frozenset({"123", "456"})
    assert s.controller_user_ids == frozenset({"123", "456", "789"})


def test_backup_gist_falls_back_to_control_gist():
    s = Settings.from_env({"CONTROL_GIST_ID": "abc"})
    assert s.backup_gist_id == "abc"
    s2 = Settings.from_env({"CONTROL_GIST_ID": "abc", "ADFARM_GIST_ID": "xyz"})
    assert s2.backup_gist_id == "xyz"
