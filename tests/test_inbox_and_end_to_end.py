from __future__ import annotations

import os
import time
from unittest.mock import MagicMock, patch
import pytest

os.environ.setdefault("USER_TOKEN", "TEST_TOKEN")
os.environ.setdefault("CHANNEL_IDS", "111111111111111111")
os.environ.setdefault("AD_TYPE", "sell")
os.environ.setdefault("MESSAGE", "SELLING BB LF 2.5$/1K DM ME QUICK")

import send_ads
from setup import Bootstrap


def test_webhook_forum_thread_support_and_fallback():
    """Test send_webhook passes thread_name and falls back cleanly if channel is standard text."""
    with patch("send_ads.DM_WEBHOOK_URL", "https://discord.com/api/webhooks/123/abc"), \
         patch("send_ads._dm_forward_failures", 0), \
         patch("send_ads.creq.post") as mock_post:

        # Scenario 1: Forum channel accepts thread_name directly
        mock_resp_success = MagicMock()
        mock_resp_success.status_code = 200
        mock_post.return_value = mock_resp_success

        result = send_ads.send_webhook("Hello Buyer", username="Alt1", thread_name="🏷️ [Purchase] User123")
        assert result is True
        assert mock_post.called
        sent_payload = mock_post.call_args[1]["json"]
        assert sent_payload["thread_name"] == "🏷️ [Purchase] User123"
        assert sent_payload["content"] == "Hello Buyer"

        # Scenario 2: Standard text channel rejects thread_name with HTTP 400 -> auto fallback without thread_name
        mock_resp_400 = MagicMock()
        mock_resp_400.status_code = 400
        mock_resp_fallback = MagicMock()
        mock_resp_fallback.status_code = 200

        mock_post.side_effect = [mock_resp_400, mock_resp_fallback]
        result2 = send_ads.send_webhook("Fallback Message", username="Alt1", thread_name="Thread Not Supported")
        assert result2 is True
        assert mock_post.call_count == 3  # 1 from earlier, 2 from fallback sequence
        fallback_payload = mock_post.call_args[1]["json"]
        assert "thread_name" not in fallback_payload
        assert fallback_payload["content"] == "Fallback Message"


def test_intent_classifier_extended_taxonomy():
    """Test multi-factor regex classifier across complex buyer inquiries."""
    # 1. Bulk Purchase with Crypto & PayPal & Blade Ball
    res1 = send_ads._classify_dm_intent("Yo! Looking to buy 500k bb tokens via Crypto or PayPal. Got vouches?")
    assert res1["category"] == "🛒 Purchase Intent"
    assert res1["priority"] == "🔥 High Intent"
    assert res1["volume"] == "500k"
    assert res1["game"] == "⚔️ Blade Ball"
    assert "💳 PayPal" in res1["payments"]
    assert "🪙 Crypto" in res1["payments"]

    # 2. Price Check with CashApp & MM2
    res2 = send_ads._classify_dm_intent("What is the rate for 2.5m tokens for mm2 with cashapp?")
    assert res2["category"] == "🔄 Price Check"
    assert res2["priority"] == "🟡 Medium Intent"
    assert res2["volume"] == "2.5m"
    assert res2["game"] == "🔪 MM2"
    assert "💵 CashApp" in res2["payments"]

    # 3. Stock Check with Budget & PS99
    res3 = send_ads._classify_dm_intent("How much stock do you have available in ps99? I have a $100 budget")
    assert res3["category"] == "📦 Stock Check"
    assert res3["priority"] == "🟡 Medium Intent"
    assert res3["volume"] == "$100"
    assert res3["game"] == "🐾 Pet Sim 99"

    # 4. Vouch / Proof Request
    res4 = send_ads._classify_dm_intent("Can you show proofs and trade vouches before we deal?")
    assert res4["category"] == "🛡️ Vouch Request"
    assert res4["priority"] == "🟡 Medium Intent"

    # 5. Casual / General
    res5 = send_ads._classify_dm_intent("Hey man, what games do you play?")
    assert res5["category"] == "💬 General Inquiry"
    assert res5["priority"] == "⚪ Casual"

    # 6. Trade Offer
    res6 = send_ads._classify_dm_intent("Trading corrupt knife for blade ball tokens")
    assert res6["category"] == "🔁 Trade Offer"
    assert res6["priority"] == "🟡 Medium Intent"
    assert res6["game"] == "⚔️ Blade Ball"


def test_chat_velocity_cadence_boundary_conditions():
    """Test chat velocity calculation and slowmode floor enforcement across all edge rates."""
    from datetime import datetime, timezone, timedelta
    now_dt = datetime.now(timezone.utc)

    # Empty or zero messages -> fallback default (5.0 msgs/min, 1.00x mult)
    v_empty, mult_empty = send_ads._calculate_chat_velocity(999, [])
    assert v_empty == 5.0
    assert mult_empty == 1.0

    # 25 messages in last 30s -> ~50 msgs/min -> fast chat -> < 0.85x multiplier
    fast_msgs = [{"timestamp": (now_dt - timedelta(seconds=i * 1.2)).isoformat()} for i in range(25)]
    v_fast, mult_fast = send_ads._calculate_chat_velocity(999, fast_msgs)
    assert v_fast > 15.0
    assert mult_fast < 1.0

    # 5 messages in last 60s -> ~5 msgs/min -> normal chat -> 1.00x multiplier
    normal_msgs = [{"timestamp": (now_dt - timedelta(seconds=i * 12.0)).isoformat()} for i in range(5)]
    v_normal, mult_normal = send_ads._calculate_chat_velocity(999, normal_msgs)
    assert 3.0 <= v_normal <= 15.0
    assert mult_normal == 1.00

    # Strict slowmode hard floor guarantee
    slowmode = 120
    base_interval = 60
    # Even if fast multiplier tries to scale base_interval down,
    # the delay MUST never go below slowmode (120) + safety jitter
    effective_delay = max(slowmode + 15, base_interval * mult_fast)
    assert effective_delay >= slowmode + 15


def test_multi_alt_channel_separation_timeline():
    """Test cross-alt collision detection and time window expiration."""
    cid = 778899
    now = time.time()

    with send_ads._state_lock:
        send_ads._fleet_channel_posts.clear()
        send_ads._fleet_channel_posts[cid] = now - 30.0

    # Alt B checks 30 seconds later (separation requirement 90s)
    collided, wait_needed = send_ads._check_fleet_collision(cid, min_separation=90.0)
    assert collided is True
    assert wait_needed > 50.0

    # Alt B checks after 95 seconds have elapsed
    with patch("time.time", return_value=now + 70.0):
        collided_after, wait_after = send_ads._check_fleet_collision(cid, min_separation=90.0)
        assert collided_after is False
        assert wait_after == 0.0


def test_setup_forum_and_text_channel_discovery():
    """Verify setup.py detects both type 0 (text) and type 15 (forum) channels."""
    b = Bootstrap(non_interactive=True)
    b.guild_id = "11223344"
    b.bot_token = "dummy_token"

    mock_channels = [
        {"id": "1001", "name": "control", "type": 0},
        {"id": "1002", "name": "dm-inbox", "type": 15},  # Forum Channel
        {"id": "1003", "name": "farm-logs", "type": 0},
    ]

    with patch.object(b, "discord", return_value=(200, mock_channels)):
        assert b.ensure_channel("control") == "1001"
        assert b.ensure_channel("dm-inbox") == "1002"
        assert b.ensure_channel("farm-logs") == "1003"


def test_setup_quick_mode_defaults():
    """Verify setup.py quick mode defaults bypass non-essential prompts."""
    b = Bootstrap(quick=True)
    assert b.quick is True
    b.existing_repo_names = {"alt1-sell"}

    repo = b.select_alt_repository(1, "sell")
    assert repo == "alt1-sell"


def test_setup_upgrade_forums_targeted_channel_replacement():
    """Verify --upgrade-forums deletes and replaces only targeted farm text channels, never personal channels."""
    b = Bootstrap(non_interactive=True, upgrade_forums=True)
    b.guild_id = "11223344"
    b.bot_token = "dummy_token"

    existing_guild_channels = [
        {"id": "1001", "name": "dm-inbox", "type": 0},    # Old Text channel -> should upgrade
        {"id": "1002", "name": "general", "type": 0},     # User personal channel -> MUST NEVER TOUCH
        {"id": "1003", "name": "todo", "type": 0},        # User personal channel -> MUST NEVER TOUCH
        {"id": "1004", "name": "temp", "type": 0},        # User personal channel -> MUST NEVER TOUCH
    ]

    def mock_discord_call(method, path, *args, **kwargs):
        if method == "GET" and "/channels" in path:
            return 200, existing_guild_channels
        if method == "DELETE" and path == "/channels/1001":
            return 204, {}
        if method == "POST" and "/channels" in path:
            body = kwargs.get("body", {})
            return 201, {"id": "2001", "name": body.get("name"), "type": body.get("type")}
        return 404, {}

    with patch.object(b, "discord", side_effect=mock_discord_call):
        # 1. dm-inbox with upgrade_forums=True should delete old 1001 and create Forum channel
        new_ch_id = b.ensure_channel("dm-inbox", channel_type=15)
        assert new_ch_id == "2001"

        # 2. Re-verifying general/todo/temp channels are not touched or deleted
        assert b.ensure_channel("general", channel_type=0) == "1002"
        assert b.ensure_channel("todo", channel_type=0) == "1003"
        assert b.ensure_channel("temp", channel_type=0) == "1004"


def test_deal_webhook_forum_thread_support():
    """Verify send_deal_webhook attaches thread_name for deals forum posts."""
    with patch("send_ads.DEAL_WEBHOOK_URL", "https://discord.com/api/webhooks/999/deals"), \
         patch("send_ads._deal_webhook_failures", 0), \
         patch("send_ads.creq.post") as mock_post:

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        embed_payload = {"title": "📈 DEAL ALERT", "fields": []}
        result = send_ads.send_deal_webhook(embed_payload, thread_name="💰 [+$0.20/1k] Blade Ball @ $0.70")
        assert result is True
        time.sleep(0.1)  # thread execution
        assert mock_post.called
        sent_json = mock_post.call_args[1]["json"]
        assert sent_json["thread_name"] == "💰 [+$0.20/1k] Blade Ball @ $0.70"
        assert "embeds" in sent_json


def test_github_api_health_and_user_profile():
    """Verify control_bot.github_api helpers execute cleanly with GITHUB_API constant."""
    from control_bot import github_api

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"login": "darkkitty-owner", "id": 12345}

    with patch("requests.get", return_value=mock_resp), \
         patch("control_bot.github_api._auth_headers", return_value={"Authorization": "token test"}):

        user = github_api.get_authenticated_user()
        assert user.get("login") == "darkkitty-owner"

        ok, latency = github_api.check_github_api_health()
        assert ok is True
        assert latency >= 0.0


def test_setup_text_channel_mode_option():
    """Verify setup.py configures standard text channels when use_forums=False."""
    b = Bootstrap(non_interactive=True, use_forums=False)
    assert b.use_forums is False
    b.guild_id = "11223344"
    b.bot_token = "dummy_token"

    mock_created = []

    def mock_discord(method, path, *args, **kwargs):
        if method == "GET" and "/webhooks" in path:
            return 200, []
        if method == "GET" and "/channels" in path:
            return 200, []
        if method == "POST" and "/webhooks" in path:
            return 201, {"id": "wh1", "token": "tok1", "url": "https://discord.com/api/webhooks/wh1/tok1"}
        if method == "POST" and "/channels" in path:
            body = kwargs.get("body", {})
            mock_created.append(body)
            return 201, {"id": "3001", "name": body.get("name"), "type": body.get("type")}
        return 404, {}

    with patch.object(b, "discord", side_effect=mock_discord):
        b.provision_discord()
        assert b.channels["dm-inbox"] == "3001"
        assert b.channels["deals"] == "3001"
        # Verify dm-inbox was requested as type 0 (Text), not type 15 (Forum)
        dm_req = next(req for req in mock_created if req.get("name") == "dm-inbox")
        assert dm_req["type"] == 0


def test_spaced_out_buyer_dm_context_memory_and_thread_continuation():
    """Verify spaced-out buyer DMs preserve context history, cumulative intent, and thread ID."""
    with patch("send_ads.DM_WEBHOOK_URL", "https://discord.com/api/webhooks/123/abc"), \
         patch("send_ads._dm_forward_failures", 0), \
         patch("send_ads._buyer_forum_threads", {}), \
         patch("send_ads._buyer_context_history", {}), \
         patch("send_ads.creq.post") as mock_post:

        mock_resp1 = MagicMock()
        mock_resp1.status_code = 200
        mock_resp1.json.return_value = {"id": "msg1", "channel_id": "forum_thread_888"}

        mock_resp2 = MagicMock()
        mock_resp2.status_code = 200
        mock_resp2.json.return_value = {"id": "msg2", "channel_id": "forum_thread_888"}

        mock_post.side_effect = [mock_resp1, mock_resp2]

        user_obj = {"id": "7788990011", "username": "BuyerAlex", "avatar": "av123"}

        # Message 1: "hi" (general greeting)
        send_ads.forward_dm_message("dm_ch_1", user_obj, "hi", [])
        assert send_ads._buyer_forum_threads.get("7788990011") == "forum_thread_888"

        # Message 2: "are you selling blade ball tokens for crypto? need 100k"
        send_ads.forward_dm_message("dm_ch_1", user_obj, "are you selling blade ball tokens for crypto? need 100k", [])

        # Verify second call continued in existing thread_id=forum_thread_888
        assert mock_post.call_count == 2
        second_call_url = mock_post.call_args_list[1][0][0]
        assert "thread_id=forum_thread_888" in second_call_url

        # Verify history context memory retained both messages
        history = send_ads._buyer_context_history.get("7788990011")
        assert len(history) == 2
        assert history[0]["text"] == "hi"
        assert "blade ball" in history[1]["text"]


def test_sender_policy_command_and_fuzzy_channel_discovery():
    """Verify !policy changes runtime parameters and discover_channel_by_name handles emoji/decorated channel names."""
    replies = []
    def mock_reply(text):
        replies.append(text)

    # Test !policy command
    with patch("send_ads.CONTROLLER_USER_IDS", {"999"}), \
         patch("send_ads.send_log_webhook") as mock_webhook:
        handled = send_ads._handle_controller_dm("ch_1", "999", "!policy aggressive", trusted_source=True, reply_fn=mock_reply)
        assert handled is True
        assert any("AGGRESSIVE" in r for r in replies)
        assert send_ads.INTERVAL_MIN == 3
        assert send_ads._runtime_deal_scan_enabled is True

    # Test discover_channel_by_name fuzzy emoji match
    guild_channels = [
        {"id": "555111", "name": "「💵」・trade-market", "type": 0},
        {"id": "555222", "name": "chat-lounge", "type": 0},
    ]
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = guild_channels

    with patch("send_ads.api", return_value=mock_resp):
        found = send_ads.discover_channel_by_name("guild_123", "trade-market")
        assert found is not None
        assert found["id"] == "555111"


def test_directional_arbitrage_deal_scanner_filtering():
    """Verify deal scanner categorizes directional deals and ignores non-deal noise."""
    alerts = []
    def mock_deal_webhook(embed, thread_name=None):
        alerts.append((embed, thread_name))

    with patch("send_ads.DEAL_WEBHOOK_URL", "https://discord.com/api/webhooks/deals/123"), \
         patch("send_ads.DEAL_SCAN_ENABLED", True), \
         patch("send_ads.DEAL_ITEM_KEYWORDS", ["Blade Ball"]), \
         patch("send_ads.DEAL_ALERT_DELTA", 0.05), \
         patch("send_ads._runtime_rate", 2.00), \
         patch("send_ads.AD_TYPE", "sell"), \
         patch("send_ads.send_deal_webhook", side_effect=mock_deal_webhook):

        # 1. Cheap buyer post: "WTB Blade Ball $0.50/1k" (Should be REJECTED, not a deal for seller)
        msgs_cheap_buyer = [{"id": "m1", "author": {"id": "u1", "username": "Lowballer"}, "content": "WTB Blade Ball 100k for $0.50/1k"}]
        send_ads.scan_deals("ch_trade", msgs_cheap_buyer)
        assert len(alerts) == 0

        # 2. High paying buyer post: "WTB Blade Ball $2.80/1k" (Should be ALERTED as premium buyer)
        msgs_high_buyer = [{"id": "m2", "author": {"id": "u2", "username": "HighBuyer"}, "content": "WTB Blade Ball 50k at $2.80/1k fast"}]
        send_ads.scan_deals("ch_trade", msgs_high_buyer)
        assert len(alerts) == 1
        assert "BUYER DETECTED" in alerts[0][0]["fields"][0]["value"]
        assert alerts[0][0]["fields"][4]["value"] == "**$2.80/1k**"

        # 3. Cheap supplier post: "WTS Blade Ball $1.20/1k stock" (Should be ALERTED as supplier deal)
        msgs_supplier = [{"id": "m3", "author": {"id": "u3", "username": "CheapSeller"}, "content": "WTS Blade Ball 200k in stock $1.20 per 1k"}]
        send_ads.scan_deals("ch_trade", msgs_supplier)
        assert len(alerts) == 2
        assert "SELLER DETECTED" in alerts[1][0]["fields"][0]["value"]


def test_visual_analytics_embed_generation():
    """Verify build_analytics_embed generates complete metrics, gauges, and channel matrices."""
    from control_bot.alt_state import AltStateManager
    from control_bot.dashboard import build_analytics_embed

    mgr = AltStateManager({1: "Alt 1", 2: "Alt 2"}, alt_ids=[1, 2])
    mgr.update_from_heartbeat(1, {
        "status": "active",
        "total_sent": 30,
        "total_errors": 1,
        "total_skips": 4,
        "total_edits": 5,
        "deal_alerts": 2,
        "uptime_sec": 3600,
        "channels": {
            "111222": {"name": "trading", "sent": 15, "errors": 0, "slowmode": 120, "last_post": time.time()},
            "333444": {"name": "market", "sent": 15, "errors": 1, "slowmode": 60, "last_post": time.time()},
        }
    })

    embed = build_analytics_embed(mgr, target_alt=0)
    assert "ADVANCED FLEET ANALYTICS" in embed.title
    assert "Delivery Success Rate" in embed.description
    assert any("Alt Throughput & Health Gauges" in f.name for f in embed.fields)
    assert any("Channel Reliability" in f.name for f in embed.fields)
    assert any("Anti-Detection" in f.name for f in embed.fields)



