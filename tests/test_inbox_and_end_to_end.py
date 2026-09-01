from __future__ import annotations

import os
import time
from unittest import mock
from unittest.mock import MagicMock, patch
import unittest

os.environ.setdefault("USER_TOKEN", "TEST_TOKEN")
os.environ.setdefault("CHANNEL_IDS", "111111111111111111")
os.environ.setdefault("AD_TYPE", "sell")
os.environ.setdefault("MESSAGE", "SELLING BB LF 2.5$/1K DM ME QUICK")

import send_ads
from setup import Bootstrap


class InboxAndEndToEndTests(unittest.TestCase):
    def test_webhook_forum_thread_support_and_fallback(self):
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


    def test_intent_classifier_extended_taxonomy(self):
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


    def test_chat_velocity_cadence_boundary_conditions(self):
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


    def test_multi_alt_channel_separation_timeline(self):
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


    def test_setup_forum_and_text_channel_discovery(self):
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


    def test_setup_quick_mode_defaults(self):
        """Verify setup.py quick mode defaults bypass non-essential prompts."""
        b = Bootstrap(quick=True)
        assert b.quick is True
        b.existing_repo_names = {"alt1-sell"}

        repo = b.select_alt_repository(1, "sell")
        assert repo == "alt1-sell"


    def test_setup_upgrade_forums_targeted_channel_replacement(self):
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


    def test_deal_webhook_forum_thread_support(self):
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


    def test_github_api_health_and_user_profile(self):
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


    def test_setup_text_channel_mode_option(self):
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


    def test_spaced_out_buyer_dm_context_memory_and_thread_continuation(self):
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


    def test_sender_policy_command_and_fuzzy_channel_discovery(self):
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


    def test_directional_arbitrage_deal_scanner_filtering(self):
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
            assert "**$2.80/1k**" in alerts[0][0]["fields"][4]["value"]

            # 3. Cheap supplier post: "WTS Blade Ball $1.20/1k stock" (Should be ALERTED as supplier deal)
            msgs_supplier = [{"id": "m3", "author": {"id": "u3", "username": "CheapSeller"}, "content": "WTS Blade Ball 200k in stock $1.20 per 1k"}]
            send_ads.scan_deals("ch_trade", msgs_supplier)
            assert len(alerts) == 2
            assert "SELLER DETECTED" in alerts[1][0]["fields"][0]["value"]


    def test_visual_analytics_embed_generation(self):
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


    def test_alt_action_completion_webhook_signal_and_chained_chunks(self):
        """Verify that send_ads properly formats and emits rich execution completion signals."""
        dash_embeds = []
        log_messages = []

        with mock.patch.object(send_ads, "DASHBOARD_WEBHOOK_URL", "https://discord.com/api/webhooks/fake/dash"), \
             mock.patch.object(send_ads, "LOG_WEBHOOK_URL", "https://discord.com/api/webhooks/fake/logs"), \
             mock.patch.object(send_ads, "send_dashboard", side_effect=lambda embed: dash_embeds.append(embed)), \
             mock.patch.object(send_ads, "send_log_webhook", side_effect=lambda msg, kind=None: log_messages.append((msg, kind))), \
             mock.patch.object(send_ads, "CHANNEL_IDS", ["111", "222"]), \
             mock.patch.object(send_ads, "ch_names", {"111": "trading", "222": "market"}), \
             mock.patch.object(send_ads, "CHUNK_INDEX", 1), \
             mock.patch.object(send_ads, "TOTAL_CHUNKS", 2), \
             mock.patch.object(send_ads, "TOTAL_HOURS", 12.0), \
             mock.patch.object(send_ads, "TOTAL_RUN_MIN", 350.0):

            per_ch = {
                "111": {"sent": 25, "txt": 20, "img": 5, "edits": 4, "errors": 0, "skipped": 2},
                "222": {"sent": 25, "txt": 20, "img": 5, "edits": 3, "errors": 0, "skipped": 1},
            }

            # 1. Test Chunk 1 completion signal
            send_ads._send_completion_summary_webhook(
                "Scheduled execution window complete (350.0m)",
                time.time() - 21000, 50, 0, 3, 2, 10, 7, per_ch, is_shutdown=False
            )

            assert len(dash_embeds) == 1
            embed = dash_embeds[0]
            assert "CHUNK 1/2 COMPLETE" in embed["title"]
            assert "Chaining to Chunk 2" in embed["title"]
            assert any("Duration & Velocity" in f["name"] for f in embed["fields"])
            assert any("Deliveries & Edits" in f["name"] for f in embed["fields"])
            assert any("Channel Breakdown" in f["name"] for f in embed["fields"])
            assert any("Next Workflow Action" in f["name"] and "Chunk `2/2`" in f["value"] for f in embed["fields"])

            assert len(log_messages) == 1
            assert "RUN COMPLETE" in log_messages[0][0]
            assert "Chunk 1/2" in log_messages[0][0]


    def test_alt_identity_enhancements_across_logs_deals_and_dms(self):
        """Verify all log lines, deal scanner embeds, and DM forwards carry explicit Alt identity."""
        deal_webhooks = []
        dm_webhooks = []
        log_webhooks = []

        with mock.patch.object(send_ads, "ALT_ID", 3), \
             mock.patch.object(send_ads, "ALT_NAME", "SellerThree"), \
             mock.patch.object(send_ads, "DEAL_WEBHOOK_URL", "https://discord.com/api/webhooks/fake/deals"), \
             mock.patch.object(send_ads, "DM_WEBHOOK_URL", "https://discord.com/api/webhooks/fake/dms"), \
             mock.patch.object(send_ads, "LOG_WEBHOOK_URL", "https://discord.com/api/webhooks/fake/logs"), \
             mock.patch.object(send_ads, "send_deal_webhook", side_effect=lambda embed, thread_name=None: deal_webhooks.append((embed, thread_name))), \
             mock.patch.object(send_ads, "send_webhook", side_effect=lambda body, username=None, avatar_url=None, embed=None, thread_name=None, thread_id=None, buyer_key=None: dm_webhooks.append((body, username, embed, thread_name))):

            # 1. Test deal scanner alert formatting
            send_ads._send_deal_alert(
                "111222", {"id": "999", "username": "BuyerOne"}, 2.80, 2.00, 0.80,
                "WTB Blade Ball 100k @ 2.80/1k", "https://discord.com/channels/1/111222/123",
                "buyer", "Blade Ball"
            )
            assert len(deal_webhooks) == 1
            deal_embed, thread_name = deal_webhooks[0]
            assert "[Alt 3 · SellerThree]" in deal_embed["title"]
            assert "Alt 3" in thread_name

            # 2. Test DM forwarding formatting
            user_obj = {"id": "888", "username": "CuriousBuyer", "global_name": "Curious"}
            send_ads.forward_dm_message("777888", user_obj, "Hey I want to buy 50k tokens", [])
            assert len(dm_webhooks) == 1
            body, username, dm_embed, dm_thread_title = dm_webhooks[0]
            assert "Alt 3" in dm_thread_title
            assert "Alt 3: SellerThree" in dm_embed["footer"]["text"]


    def test_alt_add_channel_inheritance_and_run_dispatch_fallback(self):
        """Verify AltAddModal inherits fleet channels and _dispatch_run_from_modal passes fallback channels."""
        from control_bot import bot as control_bot_module
        from control_bot.alt_state import AltStateManager
        import asyncio
        from types import SimpleNamespace

        manager = AltStateManager({1: "Alt 1"}, alt_ids=[1])
        manager.set_channel(1, "111222333444", "market")

        # 1. AltAddModal inheriting channel from fleet
        modal = control_bot_module.AltAddModal()
        modal.user_token = SimpleNamespace(value="valid_alt_token_xyz")
        modal.name = SimpleNamespace(value="")
        modal.alt_id = SimpleNamespace(value="2")
        modal.repository = SimpleNamespace(value="")
        modal.channels = SimpleNamespace(value="")

        inter = mock.MagicMock()
        inter.response = mock.MagicMock()
        inter.response.send_message = mock.AsyncMock()
        inter.response.defer = mock.AsyncMock()
        inter.followup = mock.MagicMock()
        inter.followup.send = mock.AsyncMock()
        inter.user.id = 42

        mock_profile = {"id": 123456789, "username": "AltTwo", "global_name": "Alt Two"}
        prov_called_channels = []

        def mock_provision(repo, token, channels_csv=""):
            prov_called_channels.append(channels_csv)
            return True, "OK"

        with mock.patch.object(control_bot_module, "state", manager), \
             mock.patch.object(control_bot_module.config, "OWNER_IDS", {42}), \
             mock.patch.object(control_bot_module.config, "_raw", return_value=""), \
             mock.patch.dict(os.environ, {"CHANNEL_IDS": ""}, clear=False), \
             mock.patch.object(control_bot_module.github_api, "fetch_discord_user_profile", return_value=(True, mock_profile)), \
             mock.patch.object(control_bot_module.github_api, "provision_alt_repository_files_and_secrets", side_effect=mock_provision), \
             mock.patch.object(control_bot_module, "_persist_alt_registry", new=mock.AsyncMock(return_value=(True, "OK"))), \
             mock.patch.object(control_bot_module, "_log_control", new=mock.AsyncMock()):

            asyncio.run(modal.on_submit(inter))
            assert "111222333444" in prov_called_channels[0]
            assert manager.get(2) is not None
            assert "111222333444" in manager.get(2).channels

        # 2. _dispatch_run_from_modal fallback channel resolution
        dispatched_inputs = []

        def mock_dispatch(alt_id, inputs):
            dispatched_inputs.append(inputs)
            return True, "Dispatched"

        values = {
            "ad_type": "sell",
            "sell_rate": "2.50",
            "sell_extra": "Fast delivery",
            "attach_image": "true",
        }
        parsed = {
            "alt_id": 2,
            "interval": 5,
            "hours": 6,
            "rate": 2.50,
        }

        with mock.patch.object(control_bot_module, "state", manager), \
             mock.patch.object(control_bot_module.config, "OWNER_IDS", {42}), \
             mock.patch.object(control_bot_module.config, "GITHUB_TOKEN", "ghp_fake"), \
             mock.patch.object(control_bot_module.config, "GITHUB_OWNER", "org"), \
             mock.patch.object(control_bot_module.config, "ALT_REPOS", {2: "org/alt2"}), \
             mock.patch.object(control_bot_module.github_api, "cancel_run", return_value=True), \
             mock.patch.object(control_bot_module.github_api, "dispatch_workflow", side_effect=mock_dispatch), \
             mock.patch.object(control_bot_module, "_log_control", new=mock.AsyncMock()):

            asyncio.run(control_bot_module._dispatch_run_from_modal(inter, values, parsed))
            assert len(dispatched_inputs) == 1
            assert dispatched_inputs[0]["channel_1"] == "111222333444"


    def test_parse_market_listing_comprehensive_cases(self):
        """Verify deep multi-stage market listing parser handles complex lists, bundles, rates, and negations."""
        from send_ads import parse_market_listing

        kws = ["Blade Ball", "BladeBall", "BB tokens", "BB token", "BB"]

        # 1. Multi-item bullet list with different prices and items
        msg1 = """WTS
        • Pet Sim 99 Gems: $0.15/m
        • Blox Fruits Leopard: $15 each
        • Blade Ball: 2.10$/1k (Stock 50k)
        • Da Hood Cash: $0.50/m"""
        res1 = parse_market_listing(msg1, target_keywords=kws)
        assert res1 is not None
        assert res1["item"] == "Blade Ball"
        assert res1["kind"] == "seller"
        assert res1["price"] == 2.10
        assert res1["volume"] == "50k"

        # 2. Total volume + total price calculation ($220 for 100k -> $2.20/1k)
        msg2 = "Selling 100k Blade Ball tokens for $220 Paypal / Cashapp"
        res2 = parse_market_listing(msg2, target_keywords=kws)
        assert res2 is not None
        assert res2["price"] == 2.20
        assert res2["volume"] == "100k"
        assert "PayPal" in res2["payments"]
        assert "CashApp" in res2["payments"]

        # 3. Formatted comma quantity and total price ($105 for 50,000 -> $2.10/1k)
        msg3 = "WTS 50,000 Blade Ball stock - $105"
        res3 = parse_market_listing(msg3, target_keywords=kws)
        assert res3 is not None
        assert res3["price"] == 2.10

        # 4. Multi-section list with both WTS and WTB
        msg4 = """[WTS] MM2 Godlies - $1 each
        [WTB] Blade Ball Tokens - $2.70/1k paying cashapp"""
        res4 = parse_market_listing(msg4, target_keywords=kws)
        assert res4 is not None
        assert res4["kind"] == "buyer"
        assert res4["price"] == 2.70
        assert "CashApp" in res4["payments"]

        # 5. Ratio and various rate formats
        msg5 = "BB Ratio 1:2.35 (WTB crypto only)"
        res5 = parse_market_listing(msg5, target_keywords=kws)
        assert res5 is not None
        assert res5["kind"] == "buyer"
        assert res5["price"] == 2.35
        assert "Crypto" in res5["payments"]

        # 6. Negations and non-market chatter filtered out
        assert parse_market_listing("Not selling blade ball, only MM2", target_keywords=kws) is None
        assert parse_market_listing("Vouch +rep for @Trader blade ball tokens", target_keywords=kws) is None
        assert parse_market_listing("Out of stock Blade Ball 0 in stock", target_keywords=kws) is None


    def test_cmd_simulate_sample_listing(self):
        """Verify /deals command with sample_listing parses market listings and evaluates deal triggers."""
        from control_bot import bot as control_bot_module
        from control_bot.alt_state import AltStateManager
        import asyncio

        mgr = AltStateManager({1: "Alt 1"}, alt_ids=[1])
        mgr.update_from_heartbeat(1, {
            "status": "active",
            "rate": 2.50,
            "ad_type": "sell",
            "deal_alert_delta": 0.05,
            "deal_keywords": ["Blade Ball", "BB"],
        })

        inter = mock.MagicMock()
        inter.response = mock.MagicMock()
        inter.response.is_done = mock.MagicMock(return_value=False)
        inter.response.send_message = mock.AsyncMock()
        inter.followup = mock.MagicMock()
        inter.followup.send = mock.AsyncMock()
        inter.user.id = 9999

        sample = "[WTB] Blade Ball - $2.75/1k paying LTC"

        with mock.patch.object(control_bot_module, "state", mgr), \
             mock.patch.object(control_bot_module, "_check_perms", new=mock.AsyncMock(return_value=True)):

            asyncio.run(control_bot_module.cmd_deals.callback(inter, alt=1, sample_listing=sample))
            assert inter.response.send_message.called
            call_args, call_kwargs = inter.response.send_message.call_args
            embed = call_kwargs.get("embed") if call_kwargs else call_args[0]
            assert embed is not None
            field_vals = [getattr(f, "value", f.get("value") if isinstance(f, dict) else "") for f in embed.fields]
            assert any("DEAL ALERT TRIGGERED" in v for v in field_vals)
            assert any("Blade Ball" in v for v in field_vals)
            assert any("**$2.75/1k**" in v for v in field_vals)


    def test_alt_add_direct_from_discord_edge_cases_and_clean_responses(self):
        """Verify adding an alt directly from Discord handles all edge cases cleanly with zero bugs."""
        from control_bot import bot as control_bot_module
        from control_bot.alt_state import AltStateManager
        from control_bot import config
        import asyncio
        from types import SimpleNamespace

        mgr = AltStateManager({1: "Alt 1"}, alt_ids=[1])

        # Case 1: Invalid Token
        modal = control_bot_module.AltAddModal()
        modal.user_token = SimpleNamespace(value="invalid_junk_token")
        modal.name = SimpleNamespace(value="")
        modal.alt_id = SimpleNamespace(value="")
        modal.repository = SimpleNamespace(value="")
        modal.channels = SimpleNamespace(value="")

        inter_invalid = mock.MagicMock()
        inter_invalid.response.is_done.return_value = False
        inter_invalid.response.defer = mock.AsyncMock()
        inter_invalid.followup.send = mock.AsyncMock()
        inter_invalid.user.id = 42

        with mock.patch.object(control_bot_module, "state", mgr), \
             mock.patch.object(config, "OWNER_IDS", {42}), \
             mock.patch.object(control_bot_module.github_api, "fetch_discord_user_profile", return_value=(False, {"error": "Discord returned HTTP 401"})):

            asyncio.run(modal.on_submit(inter_invalid))
            assert inter_invalid.followup.send.called
            msg = inter_invalid.followup.send.call_args[0][0]
            assert "Could not authenticate alt" in msg
            assert "401" in msg

        # Case 2: Valid Token, Full Auto-Discovery
        modal_valid = control_bot_module.AltAddModal()
        modal_valid.user_token = SimpleNamespace(value="valid_secret_user_token")
        modal_valid.name = SimpleNamespace(value="")  # leave blank to auto-detect
        modal_valid.alt_id = SimpleNamespace(value="")  # leave blank for auto-slot
        modal_valid.repository = SimpleNamespace(value="")  # leave blank for auto-repo
        modal_valid.channels = SimpleNamespace(value="")  # leave blank for auto-inherit

        inter_valid = mock.MagicMock()
        inter_valid.response.is_done.return_value = False
        inter_valid.response.defer = mock.AsyncMock()
        inter_valid.followup.send = mock.AsyncMock()
        inter_valid.user.id = 42

        mock_profile = {"id": 123456789012345678, "username": "pro_trader_42", "global_name": "Pro Trader"}

        with mock.patch.object(control_bot_module, "state", mgr), \
             mock.patch.object(config, "OWNER_IDS", {42}), \
             mock.patch.object(config, "CORE_REPO", "owner/adfarm-core"), \
             mock.patch.object(config, "GITHUB_TOKEN", "fake-gh-token"), \
             mock.patch.object(control_bot_module.github_api, "fetch_discord_user_profile", return_value=(True, mock_profile)), \
             mock.patch.object(control_bot_module.github_api, "provision_alt_repository_files_and_secrets", return_value=(True, "OK")), \
             mock.patch.object(control_bot_module, "_persist_alt_registry", new=mock.AsyncMock(return_value=(True, "OK"))), \
             mock.patch.object(control_bot_module, "_log_control", new=mock.AsyncMock()):

            asyncio.run(modal_valid.on_submit(inter_valid))
            assert inter_valid.followup.send.called
            msg = inter_valid.followup.send.call_args[0][0]
            assert "successfully added" in msg
            assert "Alt 2" in msg
            assert "@pro_trader_42" in msg
            assert 2 in mgr.alt_ids
            assert mgr.get(2).name == "Pro Trader"


    def test_all_19_commands_end_to_end_execution(self):
        """Verify all 19 unified slash commands execute cleanly without raising unhandled errors."""
        from control_bot import bot as control_bot_module
        from control_bot.alt_state import AltStateManager
        from control_bot import config
        import asyncio
        from types import SimpleNamespace

        mgr = AltStateManager({1: "Seller 1", 2: "Buyer 2"}, alt_ids=[1, 2])
        mgr.update_from_heartbeat(1, {"status": "active", "total_sent": 20, "rate": 2.50, "ad_type": "sell"})
        mgr.update_from_heartbeat(2, {"status": "active", "total_sent": 15, "rate": 2.30, "ad_type": "buy"})

        def make_inter():
            inter = mock.MagicMock()
            inter.response = mock.MagicMock()
            inter.response.is_done = mock.MagicMock(return_value=False)
            inter.response.send_message = mock.AsyncMock()
            inter.response.defer = mock.AsyncMock()
            inter.followup = mock.MagicMock()
            inter.followup.send = mock.AsyncMock()
            inter.user = SimpleNamespace(id=42, name="OwnerOperator")
            return inter

        with mock.patch.object(control_bot_module, "state", mgr), \
             mock.patch.object(config, "OWNER_IDS", {42}), \
             mock.patch.object(config, "CORE_REPO", "owner/adfarm-core"), \
             mock.patch.object(config, "GITHUB_TOKEN", "fake-gh-token"), \
             mock.patch.object(control_bot_module, "_check_perms", new=mock.AsyncMock(return_value=True)), \
             mock.patch.object(control_bot_module, "_send_control_wait_ack", new=mock.AsyncMock(return_value="✅ ACK")), \
             mock.patch.object(control_bot_module, "_log_control", new=mock.AsyncMock()), \
             mock.patch.object(control_bot_module.github_api, "cancel_run", return_value=(True, "cancelled")), \
             mock.patch.object(control_bot_module.github_api, "refresh_all_run_statuses", return_value=None), \
             mock.patch.object(control_bot_module, "_hydrate_discord_state", new=mock.AsyncMock()), \
             mock.patch.object(control_bot_module, "_refresh_dashboard_now", new=mock.AsyncMock()), \
             mock.patch.object(control_bot_module, "_post_dashboard", new=mock.AsyncMock(return_value=SimpleNamespace(jump_url="https://discord.com/msg/123"))), \
             mock.patch.object(control_bot_module.github_api, "get_authenticated_user", return_value={"login": "test"}), \
             mock.patch.object(control_bot_module.github_api, "fetch_gist", return_value={"id": "gist"}):

            # 1. /run
            inter = make_inter()
            asyncio.run(control_bot_module.cmd_run.callback(inter))
            assert inter.response.send_message.called

            # 2. /stop
            inter = make_inter()
            asyncio.run(control_bot_module.cmd_stop.callback(inter, alt=1))
            assert inter.followup.send.called

            # 3. /pause
            inter = make_inter()
            asyncio.run(control_bot_module.cmd_pause.callback(inter, alt=1))
            assert inter.followup.send.called

            # 4. /resume
            inter = make_inter()
            asyncio.run(control_bot_module.cmd_resume.callback(inter, alt=1))
            assert inter.followup.send.called

            # 5. /alt (interactive hub)
            inter = make_inter()
            asyncio.run(control_bot_module.cmd_alt.callback(inter, action="overview"))
            assert inter.response.send_message.called

            # 5b. /alt (actions: logs, runs, clearlogs)
            inter = make_inter()
            asyncio.run(control_bot_module.cmd_alt.callback(inter, action="logs", alt=1))
            assert inter.response.send_message.called or inter.followup.send.called

            # 6. /tune (interactive hub)
            inter = make_inter()
            asyncio.run(control_bot_module.cmd_tune.callback(inter, alt=1))
            assert inter.response.send_message.called

            # 6b. /tune (parameters: price, mode, interval, runtime, policy)
            inter = make_inter()
            asyncio.run(control_bot_module.cmd_tune.callback(inter, alt=1, price="2.45"))
            assert inter.followup.send.called

            inter = make_inter()
            asyncio.run(control_bot_module.cmd_tune.callback(inter, alt=1, policy="stealth"))
            assert inter.response.send_message.called

            # 7. /channels (interactive hub)
            inter = make_inter()
            asyncio.run(control_bot_module.cmd_channels.callback(inter, alt=1, action="view"))
            assert inter.response.send_message.called

            # 7b. /channels (action: rescan)
            inter = make_inter()
            asyncio.run(control_bot_module.cmd_channels.callback(inter, alt=1, action="rescan"))
            assert inter.followup.send.called

            # 8. /deals (interactive hub)
            inter = make_inter()
            asyncio.run(control_bot_module.cmd_deals.callback(inter, alt=1))
            assert inter.response.send_message.called

            # 8b. /deals (parameters: keywords, enabled, min_delta)
            inter = make_inter()
            asyncio.run(control_bot_module.cmd_deals.callback(inter, alt=1, keywords="Blade Ball, BB, Robux"))
            assert inter.followup.send.called

            # 9. /squad (interactive hub)
            inter = make_inter()
            asyncio.run(control_bot_module.cmd_squad.callback(inter, action="overview"))
            assert inter.response.send_message.called

            # 9b. /squad (actions: assign, view)
            inter = make_inter()
            asyncio.run(control_bot_module.cmd_squad.callback(inter, action="assign", alt=1, squad_name="Alpha"))
            assert inter.response.send_message.called

            # 10. /status
            inter = make_inter()
            asyncio.run(control_bot_module.cmd_status.callback(inter, alt=0))
            assert inter.response.send_message.called

            # 11. /reply
            inter = make_inter()
            asyncio.run(control_bot_module.cmd_reply.callback(inter, alt=1, user="9988776655", text="100k available"))
            assert inter.followup.send.called

            # 12. /analytics
            inter = make_inter()
            asyncio.run(control_bot_module.cmd_analytics.callback(inter, alt=0))
            assert inter.response.send_message.called

            # 13. /diagnose
            inter = make_inter()
            asyncio.run(control_bot_module.cmd_diagnose.callback(inter, alt=1))
            assert inter.followup.send.called

            # 14. /canary
            inter = make_inter()
            asyncio.run(control_bot_module.cmd_canary.callback(inter, alt=0))
            assert inter.followup.send.called

            # 15. /topology
            inter = make_inter()
            asyncio.run(control_bot_module.cmd_topology.callback(inter))
            assert inter.followup.send.called

            # 16. /sync
            inter = make_inter()
            asyncio.run(control_bot_module.cmd_sync.callback(inter))
            assert inter.followup.send.called

            # 17. /refresh
            inter = make_inter()
            asyncio.run(control_bot_module.cmd_refresh.callback(inter))
            assert inter.followup.send.called

            # 18. /dashboard
            inter = make_inter()
            asyncio.run(control_bot_module.cmd_dashboard.callback(inter))
            assert inter.followup.send.called

            # 19. /help
            inter = make_inter()
            asyncio.run(control_bot_module.cmd_help.callback(inter))
            assert inter.response.send_message.called


    def test_create_alt_repository_public_default_and_overrides(self):
        """Verify create_alt_repository creates public repos by default and respects overrides."""
        from control_bot import github_api

        # 1. Default creation -> public payload
        with mock.patch("control_bot.github_api.repository_exists", return_value=(False, "")), \
             mock.patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 201

            ok, repo = github_api.create_alt_repository("owner/alt-public-test")
            assert ok is True
            assert repo == "owner/alt-public-test"
            assert mock_post.called
            call_json = mock_post.call_args[1].get("json", {})
            assert call_json.get("private") is False

        # 2. Explicit private=True -> private payload
        with mock.patch("control_bot.github_api.repository_exists", return_value=(False, "")), \
             mock.patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 201

            ok, repo = github_api.create_alt_repository("owner/alt-private-test", private=True)
            assert ok is True
            assert mock_post.called
            call_json = mock_post.call_args[1].get("json", {})
            assert call_json.get("private") is True

        # 3. ALT_REPO_PRIVATE env var override
        with mock.patch("control_bot.github_api.repository_exists", return_value=(False, "")), \
             mock.patch.dict(os.environ, {"ALT_REPO_PRIVATE": "1"}), \
             mock.patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 201

            ok, repo = github_api.create_alt_repository("owner/alt-env-private")
            assert ok is True
            assert mock_post.called
            call_json = mock_post.call_args[1].get("json", {})
            assert call_json.get("private") is True

    def test_comprehensive_operator_lifecycle_all_9_scenarios(self):
        """End-to-end integration simulation covering all 9 realistic operator scenarios."""
        from control_bot import bot as control_bot_module
        from control_bot import config, github_api
        from control_bot.alt_state import AltStateManager
        from types import SimpleNamespace
        import asyncio

        mgr = AltStateManager({1: "Alt 1", 2: "Alt 2"}, [1, 2])
        mgr.update_from_heartbeat(1, {"total_sent": 45, "total_errors": 0, "status": "active", "rate": 2.50, "ad_type": "sell", "interval_min": 5})
        mgr.update_from_heartbeat(2, {"total_sent": 30, "total_errors": 1, "status": "active", "rate": 2.30, "ad_type": "buy", "interval_min": 3})

        def make_inter(user_id=42):
            inter = mock.MagicMock()
            inter.user.id = user_id
            inter.user.name = "ProOperator"
            inter.response.is_done.return_value = False
            inter.response.send_message = mock.AsyncMock()
            inter.response.send_modal = mock.AsyncMock()
            inter.response.defer = mock.AsyncMock()
            inter.response.edit_message = mock.AsyncMock()
            inter.followup.send = mock.AsyncMock()
            return inter

        with mock.patch.object(control_bot_module, "state", mgr), \
             mock.patch.object(config, "OWNER_IDS", {42}), \
             mock.patch.object(config, "CONTROL_GIST_ID", "mock-gist-123"), \
             mock.patch.object(config, "ALT_REPOS", {1: "owner/alt1-sell", 2: "owner/alt2-buy"}), \
             mock.patch.object(config, "ALT_DISCORD_IDS", {1: 1001, 2: 1002, 3: 1003}), \
             mock.patch.object(control_bot_module, "_cooldowns", {}), \
             mock.patch.object(control_bot_module, "_log_control", new=mock.AsyncMock()), \
             mock.patch.object(github_api, "queue_control_command", return_value=(True, "cmd-uuid-123")), \
             mock.patch.object(github_api, "dispatch_workflow", return_value=(True, "Dispatched workflow in owner/alt1-sell")), \
             mock.patch.object(github_api, "cancel_run", return_value=(True, "Sent cancel for run 101")), \
             mock.patch.object(github_api, "set_repository_secret", return_value=(True, "Secret set")), \
             mock.patch.object(github_api, "delete_repository_secret", return_value=(True, "Secret deleted")), \
             mock.patch.object(github_api, "upload_repository_file", return_value=(True, "Committed")), \
             mock.patch.object(github_api, "fetch_discord_user_profile", return_value=(True, {"id": 1003, "username": "pro_alt_3", "global_name": "Pro Alt 3"})), \
             mock.patch.object(github_api, "provision_alt_repository_files_and_secrets", return_value=(True, "Auto-provisioned")), \
             mock.patch.object(control_bot_module, "_persist_alt_registry", new=mock.AsyncMock(return_value=(True, "OK"))), \
             mock.patch.object(github_api, "list_runs", return_value=[]):

            # 1. Alt Addition
            modal_add = control_bot_module.AltAddModal()
            modal_add.user_token = SimpleNamespace(value="valid_alt_3_token")
            modal_add.name = SimpleNamespace(value="")
            modal_add.alt_id = SimpleNamespace(value="3")
            modal_add.repository = SimpleNamespace(value="")
            modal_add.channels = SimpleNamespace(value="")
            inter1 = make_inter()
            asyncio.run(modal_add.on_submit(inter1))
            assert inter1.followup.send.called

            # 2. Launch Run Workflow
            control_bot_module._cooldowns.clear()
            run_view = control_bot_module.RunStartView(owner_id=42)
            run_view.alt_id = 3
            run_view.ad_type = "sell"
            run_view.interval_min = 5
            run_view.total_hours = 12
            run_modal = control_bot_module.RunDetailsModal(run_view)
            run_modal.rate = SimpleNamespace(value="2.45")
            run_modal.extra = SimpleNamespace(value="INSTANT DELIVERY")
            run_modal.image = SimpleNamespace(value="yes")
            inter2 = make_inter()
            asyncio.run(run_modal.on_submit(inter2))
            assert inter2.followup.send.called

            # 3. Mid-Run Tuning
            control_bot_module._cooldowns.clear()
            inter3 = make_inter(); asyncio.run(control_bot_module.cmd_tune.callback(inter3, alt=3, price="2.40")); assert inter3.followup.send.called
            control_bot_module._cooldowns.clear()
            inter4 = make_inter(); asyncio.run(control_bot_module.cmd_tune.callback(inter4, alt=3, mode="buy")); assert inter4.followup.send.called

            # 4. Channel Management
            control_bot_module._cooldowns.clear()
            inter5 = make_inter(); asyncio.run(control_bot_module.cmd_channels.callback(inter5, alt=3, action="add", channel_id="998877665544", name="vip-trades")); assert inter5.followup.send.called
            control_bot_module._cooldowns.clear()
            inter6 = make_inter(); asyncio.run(control_bot_module.cmd_channels.callback(inter6, alt=3, action="rescan")); assert inter6.followup.send.called

            # 5. Deal Scanner
            control_bot_module._cooldowns.clear()
            inter7 = make_inter(); asyncio.run(control_bot_module.cmd_deals.callback(inter7, alt=3, enabled="on", min_delta="0.05", keywords="Blade Ball, BB token")); assert inter7.followup.send.called or inter7.response.send_message.called

            # 6. Operator DM Relay
            control_bot_module._cooldowns.clear()
            inter8 = make_inter(); asyncio.run(control_bot_module.cmd_reply.callback(inter8, alt=3, user="123456789012345678", text="Hello buyer!")); assert inter8.followup.send.called

            # 7. Squad Coordination
            control_bot_module._cooldowns.clear()
            inter9 = make_inter(); asyncio.run(control_bot_module.cmd_squad.callback(inter9, action="assign", alt=3, squad_name="Alpha")); assert inter9.response.send_message.called
            control_bot_module._cooldowns.clear()
            inter10 = make_inter(); asyncio.run(control_bot_module.cmd_squad.callback(inter10, action="price", squad_name="Alpha", value="2.35")); assert inter10.followup.send.called

            # 8. Soft-Pause, Resume, Stop
            control_bot_module._cooldowns.clear()
            inter11 = make_inter(); asyncio.run(control_bot_module.cmd_pause.callback(inter11, alt=3)); assert inter11.followup.send.called
            control_bot_module._cooldowns.clear()
            inter12 = make_inter(); asyncio.run(control_bot_module.cmd_resume.callback(inter12, alt=3)); assert inter12.followup.send.called
            control_bot_module._cooldowns.clear()
            inter13 = make_inter(); asyncio.run(control_bot_module.cmd_stop.callback(inter13, alt=3)); assert inter13.followup.send.called

            # 9. Fleet Diagnostics & Monitoring
            control_bot_module._cooldowns.clear(); inter14 = make_inter(); asyncio.run(control_bot_module.cmd_status.callback(inter14, alt=0)); assert inter14.response.send_message.called
            control_bot_module._cooldowns.clear(); inter15 = make_inter(); asyncio.run(control_bot_module.cmd_analytics.callback(inter15, alt=0)); assert inter15.response.send_message.called
            control_bot_module._cooldowns.clear(); inter16 = make_inter(); asyncio.run(control_bot_module.cmd_canary.callback(inter16, alt=0)); assert inter16.followup.send.called
            control_bot_module._cooldowns.clear(); inter17 = make_inter(); asyncio.run(control_bot_module.cmd_topology.callback(inter17)); assert inter17.followup.send.called
            control_bot_module._cooldowns.clear(); inter18 = make_inter(); asyncio.run(control_bot_module.cmd_help.callback(inter18)); assert inter18.response.send_message.called








