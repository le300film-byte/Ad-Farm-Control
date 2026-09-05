"""telemetry: heartbeat parsing (embed + JSON), fleet state, ingestion routing, ban markers."""
import json

from adfarm.core.clock import FakeClock
from adfarm.core.models import Alt, Customer
from adfarm.telemetry import EmbedLike, FleetState, IncomingMessage, WebhookIngestor, parse_embed_heartbeat, parse_heartbeat, parse_json_heartbeat


def sender_embed(alt_id=3, name="main", status="active", channels=True) -> EmbedLike:
    """Mirror of send_ads.py `_send_heartbeat` embed layout."""
    fields = [
        ("Status", f"`{status}`"), ("Mode", "`sell`"), ("Rate", "`2.30$/1k`"), ("Cadence", "`5m`"),
        ("Activity", "Sent: `12` · Errors: `1` · Skips: `3`"), ("Deals", "`2` alert(s)"), ("Scanner", "ON · edge $0.05/1k"),
        ("Keywords", "skins, gems"), ("Uptime", "42.5 min"), ("Channels", "Active: `2/3`"), ("Message", "WTS skins cheap"),
        ("Latest issue", "HTTP 429 slow down"), ("Warnings", "w1\nw2"),
    ]
    if channels:
        fields += [
            ("Channel: 111111111111111111 · #trading", "✅ alive · sent `10` · errors `0` · slowmode `5s` · last <t:1700000000:R>"),
            ("Channel: 222222222222222222 · #market", "❌ unavailable · sent `2` · errors `1` · slowmode `0s` · last never"),
        ]
    return EmbedLike(title=f"💓 Heartbeat · {name}", footer=f"alt_id={alt_id} · V6.0 · updated 10:00:00", fields=fields)


def test_parse_embed_heartbeat_full():
    hb = parse_embed_heartbeat(sender_embed())
    assert hb.sender_alt_id == 3 and hb.alt_name == "main" and hb.version.startswith("V6.0")
    assert hb.status == "active" and hb.ad_type == "sell" and hb.rate == 2.3 and hb.interval_min == 5
    assert (hb.total_sent, hb.total_errors, hb.total_skips, hb.deal_alerts) == (12, 1, 3, 2)
    assert hb.deal_scan_enabled is True and hb.deal_alert_delta == 0.05 and hb.deal_keywords == ("skins", "gems")
    assert hb.uptime_sec == 42.5 * 60 and (hb.active_channels, hb.total_channels) == (2, 3)
    assert hb.message_preview == "WTS skins cheap" and hb.last_error.startswith("HTTP 429") and hb.warnings == ("w1", "w2")
    assert len(hb.channels) == 2 and hb.channels[0].name == "trading" and hb.channels[0].alive and hb.channels[0].last_post == 1700000000
    assert not hb.channels[1].alive and hb.channels[1].errors == 1


def test_parse_embed_rejects_non_heartbeat_and_missing_alt_id():
    assert parse_embed_heartbeat(EmbedLike(title="📈 Deal alert")) is None
    assert parse_embed_heartbeat(EmbedLike(title="💓 Heartbeat · x", footer="no id here")) is None


def test_parse_json_heartbeat_legacy_body():
    body = json.dumps({"heartbeat": True, "alt_id": 4, "status": "running", "total_sent": 5, "channels": {"1": {"name": "a", "sent": 1, "alive": False}}, "warnings": ["w"]})
    hb = parse_json_heartbeat("```json\n" + body + "\n```")
    assert hb.sender_alt_id == 4 and hb.status == "active" and hb.total_sent == 5 and hb.channels[0].alive is False and hb.warnings == ("w",)
    assert parse_json_heartbeat("{not json") is None and parse_json_heartbeat('{"type": "other"}') is None
    assert parse_heartbeat("plain", []) is None


def test_fleet_state_apply_and_stale():
    clock = FakeClock(1000)
    fleet = FleetState(clock=clock, offline_after=900)
    fleet.register(("c1", 1), 3)
    live = fleet.apply_heartbeat(parse_embed_heartbeat(sender_embed()))
    assert live.online and live.status == "active" and live.total_sent == 12 and live.channels["111111111111111111"].name == "trading"
    assert 0 < live.health_index <= 100
    assert fleet.by_sender(3) is live and fleet.for_customer("c1") == [live] and fleet.get(("c1", 1)) is live
    assert fleet.apply_heartbeat(parse_embed_heartbeat(sender_embed(alt_id=99))) is None
    clock.advance(901)
    assert fleet.mark_stale() == [("c1", 1)] and not live.online and live.status == "offline" and live.health_index == 0
    fleet.append_log(("c1", 1), "hello", kind="INFO")
    fleet.append_log(("c1", 1), "deal!", kind="DEAL")
    assert [l.text for l in fleet.recent_logs(("c1", 1), kind="DEAL")] == ["deal!"]
    fleet.clear_logs(("c1", 1))
    assert fleet.recent_logs(("c1", 1)) == []
    assert fleet.should_autoreply(("c1", 1), "buyer", 1800) and not fleet.should_autoreply(("c1", 1), "buyer", 1800)
    fleet.forget(("c1", 1))
    assert fleet.get(("c1", 1)) is None and fleet.by_sender(3) is None


def _ingestor(clock=None):
    fleet = FleetState(clock=clock or FakeClock(1000))
    alice = Customer("c1", "alice", 2, True, 0, 10**10, True, forum_id="f1", thread_ids={"dashboard": "t-dash", "farm-logs": "t-logs", "deals": "t-deals", "dm-inbox": "t-dm"})
    bob = Customer("c2", "bob", 1, False, 0, 10**10, True, forum_id="f2", thread_ids={"dashboard": "b-dash", "farm-logs": "b-logs"})
    alts = {
        "c1": [Alt("c1", 1, 3, "w", "alice_alt1", username="main", display_name="main"), Alt("c1", 2, 4, "w", "alice_alt2", username="second")],
        "c2": [Alt("c2", 1, 5, "w", "bob_alt1", username="main")],   # same ALT_NAME as alice's alt → must not collide
    }
    threads = {}
    for c in (alice, bob):
        for role, tid in c.thread_ids.items():
            threads[tid] = (c, role)
    ing = WebhookIngestor(fleet, threads.get, lambda cid: alts.get(cid, []))
    return ing, fleet


def test_ingest_routes_by_thread_then_alt_id_no_name_collision():
    ing, fleet = _ingestor()
    r1 = ing.ingest(IncomingMessage(channel_id="t-dash", author_name="main", content="💓 Heartbeat", embeds=[sender_embed(alt_id=3, name="main")]))
    r2 = ing.ingest(IncomingMessage(channel_id="b-dash", author_name="main", content="💓 Heartbeat", embeds=[sender_embed(alt_id=5, name="main")]))
    assert r1.kind == "heartbeat" and r1.key == ("c1", 1)
    assert r2.kind == "heartbeat" and r2.key == ("c2", 1)
    assert fleet.get(("c1", 1)).online and fleet.get(("c2", 1)).online
    # unknown thread / non-webhook are ignored
    assert ing.ingest(IncomingMessage(channel_id="nope", author_name="main", content="x")).kind == "ignored"
    assert ing.ingest(IncomingMessage(channel_id="t-dash", author_name="main", content="x", is_webhook=False)).kind == "ignored"


def test_ingest_logs_deals_dms_and_ban_markers():
    ing, fleet = _ingestor()
    log = ing.ingest(IncomingMessage(channel_id="t-logs", author_name="second", content="[SEND] ✅ posted to #trading (111111111111111111)"))
    assert log.kind == "log" and log.key == ("c1", 2) and not log.ban_detected
    assert fleet.recent_logs(("c1", 2))[0].kind == "SEND"
    deal = ing.ingest(IncomingMessage(channel_id="t-deals", author_name="main", content="", embeds=[EmbedLike(title="📈 Deal alert", description="skins 1.9")]))
    assert deal.kind == "deal" and fleet.recent_logs(("c1", 1), kind="DEAL")
    dm = ing.ingest(IncomingMessage(channel_id="t-dm", author_name="main", content="📩 DM from **buyer** (123456789012345678): how much?"))
    assert dm.kind == "dm" and dm.dm_author_id == "123456789012345678" and dm.dm_text == "how much?"
    ban = ing.ingest(IncomingMessage(channel_id="t-logs", author_name="main", content="[ERROR] ❌ token invalidated (HTTP 401) — exiting"))
    assert ban.kind == "log" and ban.ban_detected
    # a customer with a single alt gets everything even without a name match
    single = ing.ingest(IncomingMessage(channel_id="b-logs", author_name="whatever", content="[STARTUP] 🟢 online"))
    assert single.key == ("c2", 1) and fleet.get(("c2", 1)).online
    # ambiguous name inside a multi-alt customer → ignored rather than misattributed
    amb = ing.ingest(IncomingMessage(channel_id="t-logs", author_name="unknown-name", content="[INFO] hi"))
    assert amb.kind == "ignored"


def test_heartbeat_error_status_with_marker_flags_ban():
    ing, fleet = _ingestor()
    emb = sender_embed(alt_id=3, status="error")
    emb = EmbedLike(title=emb.title, footer=emb.footer, fields=[(n, ("Account banned / token invalidated" if n == "Latest issue" else v)) for n, v in emb.fields])
    res = ing.ingest(IncomingMessage(channel_id="t-dash", author_name="main", content="", embeds=[emb]))
    assert res.kind == "heartbeat" and res.ban_detected
