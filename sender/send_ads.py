"""
Discord Marketplace Ad Sender  V6  (self-bot / alt account)
==============================================================
Sends ONE ad (SELL or BUY, chosen at workflow start) to marketplace channels
with human-like timing, browser-grade TLS/HTTP2 fingerprint (curl_cffi
impersonating Chrome), WebSocket gateway connection (real online presence),
cookie+fingerprint warmup, smart cooldown (only reposts when others have
posted after you), image EXIF strip + hash randomization, post-send typo
edits, DM forwarding to a webhook, and auto-learn (remembers which message
variations get blocked by anti-spam).

V6 additions (centralized control):
  - 🎛️ REMOTE DM CONTROL: listens for !setprice / !setmode / !setmessage /
    !pause / !resume / !stop / !sync / !status DMs from CONTROLLER_USER_IDS
    (the official control bot). Commands update in-memory config mid-run and
    reply with an ack.
  - 💓 HEARTBEAT WEBHOOK: pushes a structured JSON+embed status update every
    HEARTBEAT_INTERVAL_SEC to DASHBOARD_WEBHOOK_URL so the control bot can
    build the unified dashboard without polling GitHub.
  - 📡 GIST CONFIG SYNC: polls CONTROL_GIST_ID every SYNC_GIST_INTERVAL_SEC
    for runtime overrides (price, ad_type, paused, message). The sender reads
    this file; an operator or separate authorized integration may edit it, and
    the alt picks changes up without restarting.
  - Auto-detection of controller DM: if DM author matches any id in
    CONTROLLER_USER_IDS, it's treated as a control command (not forwarded
    to the buyer-DM webhook).

V6 addition:
  - 🔄 AUTO CHANNEL DISCOVERY: when a configured channel returns 404, fetch
    guild channels, post a confirmation prompt, wait for ✅/❌ reaction,
    hot-swap the new ID without restart.

V6 additions:
  - 🚨 SHADOWBAN CAUTION MODE (F-27): per-channel rolling verification window;
    if 2/3 recent posts fail, throttle channel (2x interval, text-only, no
    reacts/edits) with red dashboard alert; exit caution after 3 survives.
  - 🛰️ MID-RUN IP HEALTH (F-21): daemon thread rechecks outbound IP every 30
    min; if WARP drops to Azure/AWS/Google, pause all public activity with a
    red dashboard alert and auto-resume when IP recovers.
  - 🆕 NEW-LOCATION DETECTION (F-29): if gateway READY never arrives within 30s
    after IDENTIFY (despite valid /@me auth), abort with exit 2 and a specific
    "verify new location" alert instead of posting blind.
  - 🚦 PROACTIVE RATE LIMITER (F-26): parses X-RateLimit-* headers after each
    response; pre-sleeps per-bucket before hitting 429 (real-client behavior).
  - 🛑 REMOTE PANIC STOP (F-32): polls a Gist flag every 2 min AND listens for
    "/panic" DMs from trusted user IDs; sets _panic_event, sends red dashboard,
    exits cleanly with code 2.
  - 📡 PASSIVE DEAL SCANNER (F-13): reuses existing read_channel() fetches (ZERO
    extra API calls); extracts competitor prices via regex; fires deal-alert
    embeds to the separate DEAL_WEBHOOK_URL when prices beat configured rate.
    Passive only — no replies/DMs/mentions; dashboard heartbeat state is separate.

V6 additions:
  - DM forwarding to a private Discord webhook (username/avatar spoof,
    attachments, clickable "Open DM" deep link, forwards both sides)
  - Public-activity auto-pause when a buyer DMs (no posts/reactions/typing
    for DM_PAUSE_MINUTES to avoid simultaneous-action fingerprints)
  - Auto-learn blocked variations: post-send verification, strike-based
    blacklist persisted across runs via an optional GitHub Gist
  - Safety valve: if BLOCKED_SAFETY_STOP consecutive variations get
    deleted → account/IP is flagged → stop with exit code 2
  - WARP/proxy geo-country check (abort if outside ALLOWED_COUNTRIES)

V6 anti-detection stack (in order of impact):
  1. curl_cffi 'chrome' impersonation — real TLS/JA3/HTTP2 fingerprint
  2. WebSocket gateway connection — IDENTIFY + heartbeats + READY (online)
  3. Cookie/x-fingerprint warmup (GET /, /app, /experiments) pre-auth
  4. Per-channel Referer + X-Discord-Idempotency-Key + message nonce
  5. allowed_mentions (no @everyone/@here ping) + suppress_embeds sometimes
  6. Channel ACKs after reads (real clients mark channels as read)
  7. Startup bootup sequence (wait, browse, read channels before first post)
  8. Text-only warmup for first N posts (images trigger stronger anti-spam)
  9. Image: EXIF strip, filename random, JPEG quality jitter, tiny pixel jitter
  10. Variation engine: emojis, typos, casing, no-emoji versions, extra phrases
  11. Typing indicator with length-scaled duration + pre-thinking pause
  12. Occasional reactions to other users' messages (low rate)
  13. Occasional post-send "typo fix" edit (like a real user correcting themselves)
  14. AFK breaks (10-30 min, 2-4 per run) + random distraction pauses
  15. Outbound IP + country check before warmup (refuses Azure/datacenter/geo-mismatch)
  16. Channel randomization order; inter-post "glance elsewhere" reads
  17. Proper 429 rate-limit handling (global cooldowns, bounded backoff)
  18. Ban detection (401/403 re-verify, exit code 2 to cancel whole workflow)
"""

import os
import sys
import time
import json
import random
import mimetypes
import base64
import re
import uuid
import io
import math
import threading
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict, deque
from urllib.parse import urlparse, quote as urlquote

try:
    # new_reform layout: the registry helper ships next to the sender
    from channel_registry import ChannelRegistryStore
except Exception:
    try:
        from control_bot.persistence import ChannelRegistryStore  # legacy layout
    except Exception:
        ChannelRegistryStore = None

# R-17 (TODO 0.8): curl_cffi is mandatory for the browser-grade TLS/HTTP2
# fingerprint. Fail LOUDLY at import with actionable guidance instead of a
# confusing "Session got unexpected keyword 'impersonate'" crash later.
# ALLOW_REQUESTS_FALLBACK=1 exists only for offline tests / emergency fallback.
try:
    from curl_cffi import requests as creq
    import curl_cffi
    _HAS_CURL_CFFI = True
except Exception as _curl_import_err:
    _HAS_CURL_CFFI = False
    if os.environ.get("ALLOW_REQUESTS_FALLBACK", "").strip() in {"1", "true", "yes", "on"}:
        import requests as creq
        print(
            "[WARN] curl_cffi unavailable — using requests fallback "
            "(browser fingerprint protection is DISABLED; do not run this in production)."
        )
    else:
        raise RuntimeError(
            "❌ curl_cffi is REQUIRED for the AdFarm sender — it provides the "
            "browser-grade TLS/JA3/HTTP2 fingerprint that keeps the alt safe. "
            f"Import failed: {type(_curl_import_err).__name__}: {_curl_import_err}\n"
            "Install with:  pip install 'curl_cffi>=0.6.0'"
        )

try:
    from PIL import Image, PngImagePlugin, ImageEnhance
    _HAS_PIL = True
except Exception:
    _HAS_PIL = False

try:
    import websocket as _ws
    _HAS_WS = True
except Exception:
    _HAS_WS = False

_SELF_TEST = "--self-test" in sys.argv
if _SELF_TEST:
    os.environ.setdefault("USER_TOKEN", "FAKE_TOKEN_FOR_SELF_TEST")
    os.environ.setdefault("CHANNEL_IDS", "000000000000000000,111111111111111111")
    os.environ.setdefault("AD_TYPE", "sell")
    os.environ.setdefault("MESSAGE", "SELLING STOCK LF 2.5$/1K DM ME QUICK")
    os.environ.setdefault("ATTACH_IMAGE", "no")

# --------------------------------------------------------------------------- #
# Optional consolidated tuning configuration                                  #
# --------------------------------------------------------------------------- #
# TUNING_JSON is a JSON object containing optional non-secret tuning values.
# Explicit environment variables still win, which lets workflow inputs and
# per-repo overrides take precedence over the shared defaults.
_TUNING = {}
_TUNING_RAW = os.environ.get("TUNING_JSON", "").strip()
if _TUNING_RAW:
    try:
        _parsed_tuning = json.loads(_TUNING_RAW)
        if isinstance(_parsed_tuning, dict):
            _TUNING = _parsed_tuning
        else:
            print("⚠️ TUNING_JSON must be a JSON object; built-in defaults will be used.")
    except json.JSONDecodeError as exc:
        print(f"⚠️ TUNING_JSON is not valid JSON ({exc}); built-in defaults will be used.")


def _tuning_raw(name):
    """Read a tuning key while preserving false/zero JSON values."""
    if name not in _TUNING:
        return ""
    value = _TUNING[name]
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


# --------------------------------------------------------------------------- #
# Logging                                                                     #
# --------------------------------------------------------------------------- #
def _ts():
    return datetime.now().strftime("%H:%M:%S")

_SECRET_PATTERNS = [
    re.compile(r'([a-zA-Z0-9_\-]{24,28}\.[a-zA-Z0-9_\-]{6}\.[a-zA-Z0-9_\-]{27,38})'),
    re.compile(r'((?:mfa\.[a-zA-Z0-9_\-]{84})|(?:[a-zA-Z0-9_\-]{59,84}))'),
    re.compile(r'(gh[pousr]_[A-Za-z0-9_]{36,255})'),
    re.compile(r'(https://discord\.com/api/webhooks/\d+/[A-Za-z0-9_\-]+)'),
]

def _mask_secrets(text):
    msg = str(text or "")
    for pat in _SECRET_PATTERNS:
        msg = pat.sub("[REDACTED_SECRET]", msg)
    return msg

def log(m, kind="INFO"):
    """Write a typed, grep-friendly operational log line with secret masking."""
    category = str(kind or "INFO").upper()[:20]
    if category == "INFO":
        upper = str(m).upper()
        if "DEAL" in upper or "🔥" in str(m):
            category = "DEAL"
        elif "CAUTION" in upper or "⚠️" in str(m):
            category = "CAUTION"
        elif "ERROR" in upper or "FAIL" in upper or "❌" in str(m):
            category = "ERROR"
        elif "CONTROL" in upper:
            category = "CONTROL"
    counts = globals().setdefault("_log_counts", {})
    counts[category] = int(counts.get(category, 0)) + 1
    if category in {"ERROR", "CAUTION", "SECURITY"}:
        globals()["_last_error"] = str(m)[:300]
    clean_msg = _mask_secrets(m)
    alt_id = globals().get("ALT_ID") or os.environ.get("ALT_ID", "")
    alt_name = globals().get("ALT_NAME") or os.environ.get("ALT_NAME", "")
    alt_tag = f"[Alt {alt_id} · {alt_name}] " if (alt_id or alt_name) else ""
    print(f"[{_ts()}] [{category}] {alt_tag}{clean_msg}", flush=True)

def event_log(kind, message):
    log(message, kind=kind)

def dbg(m):
    if DEBUG:
        log(m, kind="DEBUG")

def _env(name, default=""):
    return os.environ.get(name, "").strip() or _tuning_raw(name) or default

def _required(name):
    v = os.environ.get(name, "").strip()
    if not v:
        print(f"[{_ts()}] ❌ REQUIRED CONFIG MISSING: environment variable '{name}' is not set.", file=sys.stderr)
        print(f"         → Set it in your GitHub Actions workflow's `env:` block or as a repository secret.", file=sys.stderr)
        print(f"         → See SETUP_GUIDE.md for the full list of required variables.", file=sys.stderr)
        sys.exit(1)
    return v

def _int(name, default):
    raw = os.environ.get(name, "").strip() or _tuning_raw(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log(f"⚠️ CONFIG: '{name}'='{raw}' is not a valid integer, falling back to default {default}")
        return default

def _float(name, default):
    raw = os.environ.get(name, "").strip() or _tuning_raw(name)
    if not raw:
        return default
    try:
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError("non-finite number")
        return value
    except ValueError:
        log(f"⚠️ CONFIG: '{name}'='{raw}' is not a valid finite number, falling back to default {default}")
        return default

def _bool(name, default=False):
    raw = (os.environ.get(name, "").strip() or _tuning_raw(name)).lower()
    if not raw:
        return default
    return raw in ("1", "yes", "true", "on", "y")

def _list(name, default):
    raw = os.environ.get(name, "").strip() or _tuning_raw(name)
    if not raw:
        return default
    return [x.strip().upper() for x in raw.split(",") if x.strip()]

# --------------------------------------------------------------------------- #
# Config                                                                      #
# --------------------------------------------------------------------------- #
VERSION = "V6.0"
USER_TOKEN    = _required("USER_TOKEN")
CHANNEL_IDS   = list(dict.fromkeys(c.strip() for c in _env("CHANNEL_IDS").split(",") if c.strip()))
if any(not c.isdigit() for c in CHANNEL_IDS):
    log("ℹ️ CHANNEL_IDS includes keyword targets; they will be resolved after authentication.")
AD_TYPE       = _required("AD_TYPE").lower()
MESSAGE       = _required("MESSAGE")
ATTACH_IMAGE  = _bool("ATTACH_IMAGE", False)
INTERVAL_MIN  = _float("INTERVAL_MIN", 5)
TOTAL_RUN_MIN = _float("TOTAL_RUN_MIN", 350)
CHUNK_INDEX   = _int("CHUNK_INDEX", 1)
TOTAL_CHUNKS  = _int("TOTAL_CHUNKS", 1)
TOTAL_HOURS   = _float("TOTAL_HOURS", 6.0)
IMAGE_PATH    = _env("IMAGE_PATH")
CUSTOM_STATUS_TEXT = _env("CUSTOM_STATUS_TEXT", "Trading")
STATUS_EMOJI       = _env("STATUS_EMOJI", "💰")
MIN_AFK_BREAKS = _int("MIN_AFK_BREAKS", 2)
MAX_AFK_BREAKS = _int("MAX_AFK_BREAKS", 4)
AFK_MIN_MIN   = _float("AFK_MIN_MIN", 10)
AFK_MAX_MIN   = _float("AFK_MAX_MIN", 30)
DISCORD_LOCALE    = _env("DISCORD_LOCALE", "en-US")
DISCORD_TIMEZONE  = _env("DISCORD_TIMEZONE", "America/New_York")
HTTPS_PROXY       = _env("HTTPS_PROXY") or _env("HTTP_PROXY")
DEBUG             = _bool("DEBUG", False)
DRY_RUN           = _bool("DRY_RUN", False)

WARMUP_POSTS      = _int("WARMUP_POSTS", 3)
RANDOM_REACT      = _bool("RANDOM_REACT", True)
STRIP_EXIF        = _bool("STRIP_EXIF", True)
IDLE_REACT_CHANCE = _float("IDLE_REACT_CHANCE", 0.10)
PROXY_CHECK       = _bool("PROXY_CHECK", True)
ENABLE_GATEWAY    = _bool("ENABLE_GATEWAY", True)
TYPO_EDIT_CHANCE  = _float("TYPO_EDIT_CHANCE", 0.18)
SUPPRESS_EMBEDS   = _bool("SUPPRESS_EMBEDS", False)
IMAGE_JITTER      = _bool("IMAGE_JITTER", True)

# V6 new
DM_WEBHOOK_URL    = _env("DM_WEBHOOK_URL")
LOG_WEBHOOK_URL   = _env("LOG_WEBHOOK_URL")  # shared #farm-logs webhook
DASHBOARD_WEBHOOK_URL = _env("DASHBOARD_WEBHOOK_URL")  # heartbeat/dashboard embeds
WEBHOOK_TIMEOUT   = _int("WEBHOOK_TIMEOUT", 20)  # seconds — applies to the four control-server webhooks
DM_WEBHOOK_TIMEOUT = _int("DM_WEBHOOK_TIMEOUT", 20)  # seconds — DM-forward webhook (user DMs)
DM_PAUSE_MINUTES  = _float("DM_PAUSE_MINUTES", 2.0)
FORWARD_OWN_DMS   = _bool("FORWARD_OWN_DMS", True)
BLOCKED_STRIKES   = _int("BLOCKED_STRIKES", 2)
BLOCKED_SAFETY_STOP = _int("BLOCKED_SAFETY_STOP", 5)
GIST_TOKEN        = _env("GIST_TOKEN")
GIST_ID           = _env("GIST_ID")
ALLOWED_COUNTRIES = _list("ALLOWED_COUNTRIES", [])  # e.g. FR,ES,NL,DE,IE,GB,PT,MA,IT

# V6 config (all optional — sane defaults if empty)
DEAL_SCAN_ENABLED    = _bool("DEAL_SCAN_ENABLED", True)
DEAL_MY_RATE         = _float("DEAL_MY_RATE", 0.0)   # 0 = auto-extract from MESSAGE
DEAL_ALERT_DELTA     = _float("DEAL_ALERT_DELTA", 0.05)  # alert only if edge >= this
# Deal alerts have their own destination; they never share the dashboard webhook.
DEAL_WEBHOOK_URL     = _env("DEAL_WEBHOOK_URL") or _env("DEALS_WEBHOOK_URL")
# Exact, case-insensitive item aliases required before a deal can alert. Keep
# the default focused on versatile multi-item markets; change it at runtime
# with !setdealkeywords or /deals (comma-separated).
def _parse_deal_keywords(raw):
    result, seen = [], set()
    for part in str(raw or "").split(","):
        value = re.sub(r"\\s+", " ", part.strip())[:60]
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            result.append(value)
    return result[:20]

# Configurable asset name and aliases. Defaults are deliberately generic so
# the runtime never assumes a particular game or item. Operators can opt into
# any names/aliases through environment variables or the control bot.
DEFAULT_ITEM_NAME = _env("DEFAULT_ITEM_NAME", "item").strip()[:60] or "item"
DEFAULT_ITEM_KEYWORDS = _parse_deal_keywords(
    _env("DEFAULT_ITEM_KEYWORDS", "item,stock,goods,assets")
)

# Built-in game alias table for the DM intent classifier. Canonical emoji-label
# per alias; operator-configured keywords remain a lower-priority fallback.
_GAME_ALIASES = {
    "blade ball": "⚔️ Blade Ball",
    "bladeball": "⚔️ Blade Ball",
    "bb": "⚔️ Blade Ball",
    "mm2": "🔪 MM2",
    "murder mystery 2": "🔪 MM2",
    "murder mystery": "🔪 MM2",
    "ps99": "🐾 Pet Sim 99",
    "pet sim 99": "🐾 Pet Sim 99",
    "pet simulator 99": "🐾 Pet Sim 99",
    "grow a garden": "🌱 Grow a Garden",
    "gag": "🌱 Grow a Garden",
    "adopt me": "🐶 Adopt Me",
    "adoptme": "🐶 Adopt Me",
    "jailbreak": "🚔 Jailbreak",
    "royale high": "👑 Royale High",
    "royalehigh": "👑 Royale High",
    "blox fruits": "🍉 Blox Fruits",
    "bloxfruits": "🍉 Blox Fruits",
    "pet sim": "🐾 Pet Sim 99",
    "fisch": "🎣 Fisch",
    "grand piece": "🏴‍☠️ Grand Piece Online",
    "gpo": "🏴‍☠️ Grand Piece Online",
}

DEAL_ITEM_KEYWORDS = _parse_deal_keywords(
    _env("DEAL_ITEM_KEYWORDS", "item,stock,goods,assets")
)

# Limitless mode: runtime runs indefinitely until the operator issues /shutdown
# (or an explicit !stop / DM panic). The sender handles 0 as "no finite wall
# clock" and keeps the AFK / keepalive / controller threads alive.
RUNTIME_LIMITLESS = _bool("RUNTIME_LIMITLESS", False)
INFINITE_AFK_PLAN_SEC = float(_float("INFINITE_AFK_PLAN_SEC", 7 * 86400))
IP_HEALTH_CHECK_INTERVAL_MIN = _float("IP_HEALTH_CHECK_INTERVAL_MIN", 30)
IP_HEALTH_PAUSE_MIN  = _float("IP_HEALTH_PAUSE_MIN", 10)
CAUTION_WINDOW       = _int("CAUTION_WINDOW", 3)     # rolling window size
CAUTION_FAIL_THRESHOLD = _int("CAUTION_FAIL_THRESHOLD", 2)  # >= fails to enter caution
CAUTION_EXIT_STREAK  = _int("CAUTION_EXIT_STREAK", 3)  # consecutive survives to exit
CAUTION_INTERVAL_MULT = _float("CAUTION_INTERVAL_MULT", 2.0)  # interval multiplier in caution
PANIC_TRUSTED_IDS    = set(x.strip() for x in _env("PANIC_TRUSTED_IDS", "").split(",") if x.strip())
PANIC_CHECK_INTERVAL_SEC = _float("PANIC_CHECK_INTERVAL_SEC", 120)
NEW_LOCATION_TIMEOUT_SEC = _float("NEW_LOCATION_TIMEOUT_SEC", 30)
RATELIMIT_PREADJUST  = _bool("RATELIMIT_PREADJUST", True)
RATELIMIT_JITTER     = _float("RATELIMIT_JITTER", 0.05)

# V6: auto channel discovery on 404 (deleted/recreated channels)
CHANNEL_NAMES        = [x.strip() for x in _env("CHANNEL_NAMES", "").split(",") if x.strip()]
CHANNEL_KEYWORDS     = [x.strip() for x in _env("CHANNEL_KEYWORDS", "").split(",") if x.strip()]
# Build a positional mapping: CHANNEL_NAMES[i] corresponds to CHANNEL_IDS[i].
# A name/keyword can also be used when an ID is not available at setup time.
_CHANNEL_NAME_BY_ID = {}
for _i, _cid in enumerate(CHANNEL_IDS):
    if _i < len(CHANNEL_NAMES):
        _CHANNEL_NAME_BY_ID[_cid] = CHANNEL_NAMES[_i].lower()
CONFIRM_USER_IDS     = set(x.strip() for x in _env("CONFIRM_USER_IDS", "").split(",") if x.strip())
CONFIRM_TIMEOUT      = _int("CONFIRM_TIMEOUT", 60)

# Clamp V6 params
if CONFIRM_TIMEOUT < 15: CONFIRM_TIMEOUT = 15
if CONFIRM_TIMEOUT > 300: CONFIRM_TIMEOUT = 300

# V6: remote control (central control bot)
CONTROLLER_USER_IDS = set(x.strip() for x in _env("CONTROLLER_USER_IDS", "").split(",") if x.strip())
ALT_ID              = _int("ALT_ID", 0)        # 1..N; shown on dashboard
ALT_NAME            = _env("ALT_NAME", f"Alt{ALT_ID if ALT_ID else '?'}")
HEARTBEAT_INTERVAL_SEC = _int("HEARTBEAT_INTERVAL_SEC", 300)
CONTROL_GIST_ID     = _env("CONTROL_GIST_ID", "")  # optional: shared gist for runtime overrides
SYNC_GIST_INTERVAL_SEC = _int("SYNC_GIST_INTERVAL_SEC", 45)
# Durable channel/server inventory. The file may live on a mounted runner
# volume; the atomic store still makes it safe on ordinary local disk.
CHANNEL_STATE_FILE   = _env("CHANNEL_STATE_FILE", ".adfarm_channel_registry.json")
CHANNEL_STATE_GIST_ID = _env("CHANNEL_STATE_GIST_ID") or CONTROL_GIST_ID
CHANNEL_STATE_GIST_FILE = f"channel_state_{ALT_ID}.json" if ALT_ID else ""
CONTROL_CMD_PREFIX  = _env("CONTROL_CMD_PREFIX", "!")
if HEARTBEAT_INTERVAL_SEC < 60: HEARTBEAT_INTERVAL_SEC = 60
if SYNC_GIST_INTERVAL_SEC < 15: SYNC_GIST_INTERVAL_SEC = 15

# Clamp V6 params to safe ranges
if CAUTION_WINDOW < 2: CAUTION_WINDOW = 2
if CAUTION_FAIL_THRESHOLD < 1: CAUTION_FAIL_THRESHOLD = 1
if CAUTION_EXIT_STREAK < 1: CAUTION_EXIT_STREAK = 1
if CAUTION_INTERVAL_MULT < 1.0: CAUTION_INTERVAL_MULT = 1.0
if DEAL_ALERT_DELTA < 0: DEAL_ALERT_DELTA = 0
if IP_HEALTH_CHECK_INTERVAL_MIN < 5: IP_HEALTH_CHECK_INTERVAL_MIN = 5
if IP_HEALTH_PAUSE_MIN < 1: IP_HEALTH_PAUSE_MIN = 1
if PANIC_CHECK_INTERVAL_SEC < 30: PANIC_CHECK_INTERVAL_SEC = 30
if NEW_LOCATION_TIMEOUT_SEC < 10: NEW_LOCATION_TIMEOUT_SEC = 10
if RATELIMIT_JITTER < 0: RATELIMIT_JITTER = 0


# --------------------------------------------------------------------------- #
# V6: Per-alt personality jitter
#
# When running multiple alts off the same codebase, identical behavioral
# constants (typo chance, react chance, typing speed, interval jitter, AFK
# frequency, image jitter count) can create a subtle cross-account linkage
# signal. If ALT_ID is set (>=1), we deterministically derive a "personality
# profile" from it and nudge each constant within a ±12% band. Each alt
# stays consistent across restarts (same seed = same personality) but alts
# differ from each other. Set PERSONALITY_JITTER=0 to disable (all alts
# behave identically to the defaults).
# --------------------------------------------------------------------------- #
PERSONALITY_JITTER = _float("PERSONALITY_JITTER", 0.12)  # max +/- 12%
_per_jitter_applied = {}
if ALT_ID >= 1 and PERSONALITY_JITTER > 0:
    # Deterministic seed per alt per parameter so adjustments are stable.
    import hashlib
    def _alt_jitter(param_name, base, lo=None, hi=None):
        h = hashlib.sha256(f"alt{ALT_ID}:{param_name}:{VERSION}".encode()).digest()
        # Map first byte to [-1.0, 1.0]
        u = h[0] / 255.0
        f = 2.0 * u - 1.0  # [-1, 1]
        new_val = base * (1.0 + f * PERSONALITY_JITTER)
        if lo is not None and new_val < lo: new_val = lo
        if hi is not None and new_val > hi: new_val = hi
        _per_jitter_applied[param_name] = new_val
        return new_val

    TYPO_EDIT_CHANCE    = _alt_jitter("typo_edit",   TYPO_EDIT_CHANCE,    0.05, 0.30)
    IDLE_REACT_CHANCE   = _alt_jitter("idle_react",  IDLE_REACT_CHANCE,   0.03, 0.20)
    RATELIMIT_JITTER    = _alt_jitter("rl_jitter",   RATELIMIT_JITTER,    0.0,  0.20)
    # AFK ranges shrink/grow slightly per alt
    _afk_min_factor     = 1.0 + (hashlib.sha256(f"alt{ALT_ID}:afk_min:{VERSION}".encode()).digest()[0]/255.0*2-1)*PERSONALITY_JITTER
    _afk_max_factor     = 1.0 + (hashlib.sha256(f"alt{ALT_ID}:afk_max:{VERSION}".encode()).digest()[0]/255.0*2-1)*PERSONALITY_JITTER
    AFK_MIN_MIN         = max(3.0,  AFK_MIN_MIN * _afk_min_factor)
    AFK_MAX_MIN         = max(AFK_MIN_MIN + 5.0, AFK_MAX_MIN * _afk_max_factor)
    MIN_AFK_BREAKS      = max(0, int(round(MIN_AFK_BREAKS * (1.0 + (hashlib.sha256(f"alt{ALT_ID}:afk_lo:{VERSION}".encode()).digest()[0]/255.0*2-1)*PERSONALITY_JITTER))))
    MAX_AFK_BREAKS      = max(MIN_AFK_BREAKS, int(round(MAX_AFK_BREAKS * (1.0 + (hashlib.sha256(f"alt{ALT_ID}:afk_hi:{VERSION}".encode()).digest()[0]/255.0*2-1)*PERSONALITY_JITTER))))
    # Clamp AFK bounds post-jitter to stay sane
    if AFK_MAX_MIN > 60: AFK_MAX_MIN = 60
    del _afk_min_factor, _afk_max_factor


if MIN_AFK_BREAKS < 0: MIN_AFK_BREAKS = 0
if MAX_AFK_BREAKS < MIN_AFK_BREAKS: MAX_AFK_BREAKS = MIN_AFK_BREAKS
if AFK_MIN_MIN < 1: AFK_MIN_MIN = 1
if AFK_MAX_MIN < AFK_MIN_MIN: AFK_MAX_MIN = AFK_MIN_MIN
if INTERVAL_MIN < 2:
    log(f"⚠️ CONFIG: INTERVAL_MIN={INTERVAL_MIN} is too aggressive (minimum safe interval is 2 min). Clamping to 2.")
    INTERVAL_MIN = 2
if DM_PAUSE_MINUTES < 0.5: DM_PAUSE_MINUTES = 0.5
if BLOCKED_STRIKES < 1: BLOCKED_STRIKES = 1
if BLOCKED_SAFETY_STOP < 2: BLOCKED_SAFETY_STOP = 2
if TOTAL_RUN_MIN < 5:
    log(f"⚠️ CONFIG: TOTAL_RUN_MIN={TOTAL_RUN_MIN} is too short (minimum safe runtime is 5 min). Clamping to 5.")
    TOTAL_RUN_MIN = 5
if TOTAL_RUN_MIN > 350:
    # Single GitHub Actions step safe runtime ceiling is 350m (~5.8h) to allow chained multi-chunk execution
    log(f"ℹ️ Chunk execution runtime capped to safe step boundary of 350 min (5.8h) for chained multi-chunk support.")
    TOTAL_RUN_MIN = 350

if AD_TYPE not in ("sell", "buy"):
    log(f"❌ CONFIG ERROR: AD_TYPE must be 'sell' or 'buy', got '{AD_TYPE}'. Check workflow inputs / AD_TYPE env var.")
    sys.exit(1)

DISCORD_MSG_LIMIT = 2000
if len(MESSAGE) > DISCORD_MSG_LIMIT:
    log(f"❌ CONFIG ERROR: MESSAGE is {len(MESSAGE)} chars (Discord limit is {DISCORD_MSG_LIMIT}). Shorten your ad copy.")
    sys.exit(1)

# Channel IDs may be blank when CHANNEL_NAMES/CHANNEL_KEYWORDS will be
# resolved safely after authentication. Main aborts if no target resolves.

# --------------------------------------------------------------------------- #
# Shared state between main + gateway thread                                  #
# --------------------------------------------------------------------------- #
_state_lock = threading.Lock()
_public_pause_until = 0.0          # epoch time — no public posts/reacts/typing until then
_dm_channel_cache = {}             # cid -> {username, avatar, id} cache for DMs
_blocked_variations = set()       # strings that have been strike-blacklisted
_strikes = defaultdict(int)       # variation_string -> strike count
_variation_scores = defaultdict(int) # variation_string -> positive survival count
_consecutive_deletions = 0        # how many DIFFERENT variations have been deleted back-to-back
_me_cache = {"id": None, "username": None, "global_name": None,
             "avatar": None, "discriminator": None}
_stop_event = threading.Event()
_last_save_to_gist = 0.0
_dm_forward_failures = 0
_avatar_base = "https://cdn.discordapp.com"

# V6 shared state
_panic_event = threading.Event()
_new_location_failed_event = threading.Event()
_caution_channels = {}           # cid -> True (thread-safe via _state_lock)
_channel_verify_history = {}     # cid -> deque(True/False)
_channel_caution_survives = {}   # cid -> consecutive survive count in caution
_rl_lock = threading.Lock()
_rl_buckets = {}                 # bucket_key -> {remaining, reset_at, limit}
_url_to_bucket = {}              # route -> bucket hash
_ip_health_lock = threading.Lock()
_ip_health_bad_until = 0.0       # epoch; public activity paused while now < this
_last_deal_alert = {}            # (cid, seller_id, price_2dp) -> last alert epoch
_deal_alert_lock = threading.Lock()
_deal_alerts_sent = 0
_last_deal_ts = 0.0
_last_error = ""
_log_counts = {}

# V6 auto-discovery shared state
_discovery_lock = threading.Lock()
_discovery_attempted = set()     # old cids for which we already tried discovery this run
_discovery_replacements = {}     # old_cid -> new_cid (successful confirmations)
_channel_id_to_guild = {}        # cid -> gid (fallback mapping for dead channels)

# V6 remote control state
_paused_by_controller = False    # /pause via DM or Gist (distinct from DM-pause)
_run_start_epoch = 0.0
_runtime_run_end = 0.0
_runtime_hours = 0               # current/next runtime choice for heartbeat state
_runtime_message = None          # overridden MESSAGE (set via !setmessage)
_runtime_rate = None             # overridden rate (set via !setprice)
_runtime_ad_type = None          # overridden ad_type (set via !setmode)
_runtime_policy_template = "balanced" # overridden policy preset
_runtime_deal_keywords = None    # overridden item aliases (set via !setdealkeywords)
_runtime_deal_scan_enabled = None  # overridden scanner toggle
_runtime_deal_delta = None          # overridden alert edge
_channel_registry = (
    ChannelRegistryStore(CHANNEL_STATE_FILE) if ChannelRegistryStore else None
)
_last_heartbeat_sent = 0.0
_dashboard_message_id = ""
_last_gist_sync = 0.0
_last_control_command_id = ""
_runtime_stats_start_sent = 0    # sent count at start of this run (for heartbeat delta)
_runtime_stats_start_err = 0
# Runtime counters (module-level so the gateway/controller thread can read them)
total_sent = total_err = total_skip = total_img = total_edits = 0
total_distractions = 0
_active_ch_ref = []              # mutable list ref (current active channels)
_ch_names_ref = {}               # dict ref
ch_names = _ch_names_ref         # module-level alias
_slowmodes_ref = {}
_last_sent_ref = {}
_my_last_msg_id_ref = {}
_stats_ref = None
_next_post_ref = {}
_dead_channels_ref = set()
_last_variation_base = ""        # track which MESSAGE we last built variations from
_variations_cache = []           # rebuilt when message changes

def public_activity_allowed():
    """True when NOT paused by controller, DM pause, IP health issue, new-location fail, or panic."""
    if _panic_event.is_set():
        return False
    if _new_location_failed_event.is_set():
        return False
    if _paused_by_controller:
        return False
    with _ip_health_lock:
        if time.time() < _ip_health_bad_until:
            return False
    with _state_lock:
        return time.time() >= _public_pause_until

def extend_dm_pause():
    """When a DM comes in, extend the public pause."""
    global _public_pause_until
    with _state_lock:
        new_until = time.time() + DM_PAUSE_MINUTES * 60
        if new_until > _public_pause_until:
            _public_pause_until = new_until
            log(f"⏸️  📥 BUYER DM DETECTED → Public activity PAUSED for {DM_PAUSE_MINUTES:.0f} min. Safe to reply; bot stays silent in public channels.")

def _sleep_chunked_respecting_pause(seconds, end_time=None):
    """Chunked sleep that returns early if we should not be doing public stuff.
    Returns True if we slept the full time, False if caller should back off."""
    if seconds <= 0:
        return True
    stop = time.time() + seconds
    while time.time() < stop:
        if _panic_event.is_set() or _stop_event.is_set():
            return False
        if end_time and time.time() >= end_time:
            return False
        if not public_activity_allowed():
            # In pause: just sleep without doing anything public
            wait = min(15, max(0.1, stop - time.time()))
            time.sleep(wait)
            continue
        time.sleep(min(5, max(0.1, stop - time.time())))
    return True

# --------------------------------------------------------------------------- #
# Browser fingerprint                                                         #
# --------------------------------------------------------------------------- #
_BROWSER = "chrome"
_DEFAULT_BUILD = 387211
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
_CHROME_VERSION_FALLBACK = "140.0.0.0"

def _scrape_build_number_and_ua(session):
    """Scrape buildNumber from discord.com/app HTML (or its JS assets)."""
    global _UA
    try:
        r = session.get("https://discord.com/app", timeout=15)
        sent_ua = r.request.headers.get("User-Agent", "") if r.request else ""
        if sent_ua and "Chrome/" in sent_ua:
            _UA = sent_ua
            m = re.search(r"Chrome/(\d+[\d.]+)", sent_ua)
            cv = m.group(1) if m else _CHROME_VERSION_FALLBACK
        else:
            cv = _CHROME_VERSION_FALLBACK
        mb = re.search(r'"buildNumber"\s*:\s*(\d{5,})', r.text)
        if mb:
            return int(mb.group(1)), cv
        scripts = re.findall(r'src="(/assets/[^"]+\.js)"', r.text)
        for s in scripts[:5]:
            try:
                rr = session.get(f"https://discord.com{s}", timeout=12)
                mb = re.search(r'buildNumber["\s:=:]+(\d{5,})', rr.text)
                if mb:
                    return int(mb.group(1)), cv
            except Exception:
                continue
        return _DEFAULT_BUILD, cv
    except Exception:
        return _DEFAULT_BUILD, _CHROME_VERSION_FALLBACK

# --------------------------------------------------------------------------- #
# Session                                                                     #
# --------------------------------------------------------------------------- #
def _build_session():
    proxy_map = {"http": HTTPS_PROXY, "https": HTTPS_PROXY} if HTTPS_PROXY else None
    if _HAS_CURL_CFFI:
        return creq.Session(impersonate=_BROWSER, proxies=proxy_map)
    return creq.Session(proxies=proxy_map)

SESSION = _build_session()

# --------------------------------------------------------------------------- #
# Warmup: cookies + fingerprint + super-props                                 #
# --------------------------------------------------------------------------- #
_X_FINGERPRINT = None
CLIENT_BUILD = _DEFAULT_BUILD
_CHROME_VER = _CHROME_VERSION_FALLBACK

def _warmup_fingerprint():
    global _X_FINGERPRINT, _UA, CLIENT_BUILD, _CHROME_VER
    log("🔑 Warming up browser session (cookies + X-Fingerprint + X-Super-Properties)...")
    try:
        log("   ℹ️  GET discord.com/ (landing page, sets initial cookies)...")
        SESSION.get("https://discord.com/", timeout=15)
        time.sleep(random.uniform(0.5, 1.2))
        log("   ℹ️  GET discord.com/app (app shell, scrapes build number)...")
        r = SESSION.get("https://discord.com/app", timeout=15)
        time.sleep(random.uniform(0.6, 1.3))
        CLIENT_BUILD, _CHROME_VER = _scrape_build_number_and_ua(SESSION)
        log("   ℹ️  GET /api/v9/experiments (fetches X-Fingerprint token)...")
        r2 = SESSION.get("https://discord.com/api/v9/experiments", timeout=10)
        if r2.status_code == 200:
            try:
                _X_FINGERPRINT = r2.json().get("fingerprint")
                log(f"   ✅ experiments endpoint → fingerprint = {(_X_FINGERPRINT[:16] + '…') if _X_FINGERPRINT else 'NONE'}")
            except Exception:
                log("   ⚠️ experiments endpoint returned non-JSON — continuing without fingerprint")
        else:
            log(f"   ⚠️ experiments endpoint returned HTTP {r2.status_code} — continuing without fingerprint")
        try:
            log("   ℹ️  POST /api/v9/science (telemetry ping, makes us look like a real client)...")
            SESSION.post("https://discord.com/api/v9/science",
                         json={"events": [], "client_track_timestamp": int(time.time()*1000)},
                         timeout=5)
            log("   ✅ science telemetry sent")
        except Exception:
            log("   ℹ️  science ping skipped (non-critical)")
        has_locale = any(c.name == "locale" for c in SESSION.cookies.jar)
        if not has_locale:
            try:
                SESSION.cookies.set("locale", DISCORD_LOCALE, domain="discord.com")
                log(f"   ✅ locale cookie set to {DISCORD_LOCALE}")
            except Exception:
                log("   ⚠️ could not set locale cookie — continuing anyway")
        else:
            log(f"   ✅ locale cookie already present ({DISCORD_LOCALE})")
    except Exception as e:
        log(f"   ⚠️ Warmup error ({type(e).__name__}: {e}) -- continuing with default browser profile")
        CLIENT_BUILD, _CHROME_VER = _DEFAULT_BUILD, _CHROME_VERSION_FALLBACK

    cv_major = _CHROME_VER.split(".")[0]
    super_props = {
        "os": "Windows",
        "browser": "Chrome",
        "device": "",
        "system_locale": DISCORD_LOCALE,
        "browser_user_agent": _UA,
        "browser_version": _CHROME_VER,
        "os_version": "10",
        "referrer": "",
        "referring_domain": "",
        "referrer_current": "",
        "referring_domain_current": "",
        "release_channel": "stable",
        "client_build_number": CLIENT_BUILD,
        "client_event_source": None,
        "design_id": 0,
    }
    sp_b64 = base64.b64encode(json.dumps(super_props, separators=(",", ":")).encode()).decode()
    headers = {
        "Authorization": USER_TOKEN,
        "Accept": "*/*",
        "Accept-Language": f"{DISCORD_LOCALE},en-US;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Origin": "https://discord.com",
        "Referer": "https://discord.com/channels/@me",
        "X-Super-Properties": sp_b64,
        "X-Debug-Options": "bugReporterEnabled",
        "X-Discord-Locale": DISCORD_LOCALE,
        "X-Discord-Timezone": DISCORD_TIMEZONE,
    }
    if _X_FINGERPRINT:
        headers["X-Fingerprint"] = _X_FINGERPRINT
    SESSION.headers.update(headers)
    log("   ─────────────────────────────────────")
    log(f"   🌐 UA         : Chrome {cv_major}")
    log(f"   🏗️  Build      : {CLIENT_BUILD}")
    log(f"   🔐 Fingerprint: {'OK' if _X_FINGERPRINT else 'NOT RECEIVED'}")
    log(f"   🍪 Cookies    : {len(list(SESSION.cookies))}")
    log(f"   🌍 Locale/TZ  : {DISCORD_LOCALE} / {DISCORD_TIMEZONE}")
    log("   ✅ Browser fingerprint ready — all subsequent requests will use these headers.")

# --------------------------------------------------------------------------- #
# API helpers                                                                 #
# --------------------------------------------------------------------------- #
_global_cooldown_until = 0.0

def sleep_chunked(seconds, end_time=None):
    if seconds <= 0:
        return
    stop = time.time() + seconds
    while time.time() < stop:
        if _panic_event.is_set() or _stop_event.is_set():
            return
        if end_time and time.time() >= end_time:
            return
        time.sleep(min(5, max(0.1, stop - time.time())))

def _apply_global_cooldown():
    global _global_cooldown_until
    # Sleep in short chunks (≤30s) so that a long global rate-limit
    # (e.g. retry_after=3600) doesn't freeze the main thread for an hour,
    # and so that KeyboardInterrupt / SystemExit can be delivered promptly.
    while True:
        if _panic_event.is_set() or _stop_event.is_set():
            return
        now = time.time()
        remaining = _global_cooldown_until - now
        if remaining <= 0:
            return
        wait = min(remaining + random.uniform(0.5, 2.0), 30.0)
        dbg(f"   ⏳ Global cooldown {wait:.1f}s (remaining ~{remaining:.0f}s)")
        time.sleep(wait)

def _make_nonce():
    DISCORD_EPOCH = 1420070400000
    ts = int(time.time() * 1000) - DISCORD_EPOCH
    incr = random.randint(0, 0xFFF)
    worker = random.randint(0, 0x1F)
    pid = random.randint(0, 0x1F)
    return str((ts << 22) | (worker << 17) | (pid << 12) | incr)

def api(method, url, retries=3, referer=None, files_mp=None, json_body=None,
        data=None, extra_headers=None):
    global _global_cooldown_until
    _429_streak = 0
    headers = {}
    # Idempotency key: send on POST /messages (new message) and PUT reactions.
    # Do NOT send on ACK, typing, reactions POST, or PATCH.
    if method.upper() == "POST" and url.rstrip("/").endswith("/messages"):
        headers["X-Discord-Idempotency-Key"] = uuid.uuid4().hex
    if method.upper() == "PUT" and "/reactions/" in url:
        headers["X-Discord-Idempotency-Key"] = uuid.uuid4().hex
    if referer:
        headers["Referer"] = referer
    if json_body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(json_body, separators=(",", ":")).encode()
    if extra_headers:
        headers.update(extra_headers)

    # F-26: proactive rate-limit wait before dispatch
    _RATELIMITER.wait(url)
    multipart_request = files_mp is not None
    for attempt in range(1, retries + 1):
        _apply_global_cooldown()
        try:
            r = SESSION.request(
                method, url,
                data=data if files_mp is None else None,
                multipart=files_mp,
                headers=headers if headers else None,
                timeout=30,
            )
            # F-26: feed response into proactive rate limiter
            try:
                _RATELIMITER.update(url, r)
            except Exception as _ignored_exc:
                print(f"[SENDER] api: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
            if multipart_request and files_mp is not None:
                try:
                    files_mp.close()
                except Exception as _ignored_exc:
                    print(f"[SENDER] api: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
                files_mp = None
        except Exception as e:
            short = url.split("/api/")[-1][:60] if "/api/" in url else url[-40:]
            log(f"   🔄 NETWORK ERROR ({method} {short}): {type(e).__name__} (attempt {attempt}/{retries})")
            if attempt < retries:
                backoff = 3 * attempt + random.uniform(0, 1)
                dbg(f"      retrying in {backoff:.1f}s...")
                time.sleep(backoff)
                continue
            log(f"   ❌ NETWORK ERROR: {method} {short} failed after {retries} attempts ({type(e).__name__})")
            return _fake_err_response(0, str(e))

        if r.status_code == 429:
            _429_streak += 1
            try:
                d = r.json()
            except Exception:
                d = {}
            raw_wait = d.get("retry_after", 8)
            try:
                raw_wait = float(raw_wait)
            except (TypeError, ValueError):
                raw_wait = 8.0
            is_global = bool(d.get("global"))
            this_wait = min(raw_wait, 600) + random.uniform(1, 3)
            scope = "GLOBAL (all requests paused)" if is_global else f"bucket {d.get('bucket','?')}"
            log(f"   ⏳ RATE LIMITED ({scope}) → waiting {this_wait:.1f}s [streak {_429_streak}/6]")
            if is_global:
                _global_cooldown_until = max(_global_cooldown_until, time.time() + raw_wait)
                log(f"   ℹ️  Global cooldown set for {raw_wait:.0f}s — all requests paused until window clears.")
            if _429_streak >= 6:
                log("   ❌ RATE LIMIT: Too many consecutive 429s ({_429_streak}). Backing off to avoid ban.".format(_429_streak=_429_streak))
                return r
            if multipart_request:
                # The upload stream has already been consumed; replaying the
                # request would create an empty/text-only duplicate.
                return r
            time.sleep(this_wait)
            continue

        if 500 <= r.status_code < 600 and attempt < retries:
            # NOTE: do NOT retry multipart uploads — files_mp stream was
            # already consumed/closed after the first send, so a retry would
            # post the JSON payload with NO image (text-only duplicate ad).
            # Real browsers don't transparently retry multipart POSTs either.
            if multipart_request:
                dbg(f"[API] 5xx on multipart upload (HTTP {r.status_code}), NOT retrying (would create text-only duplicate)")
                if files_mp is not None:
                    try: files_mp.close()
                    except Exception as _exc:
                        log(f"[API] multipart upload handle close failed: {_exc}", kind="DEBUG")
                return r
            backoff = 3 * attempt + random.uniform(0, 2)
            log(f"   🔄 DISCORD SERVER ERROR {r.status_code} (attempt {attempt}/{retries}) → retrying in {backoff:.1f}s")
            time.sleep(backoff)
            continue

        if files_mp is not None:
            try:
                files_mp.close()
            except Exception as _ignored_exc:
                print(f"[SENDER] api: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
        return r
    return _fake_err_response(0, "max retries exceeded")

def _fake_err_response(code, msg):
    r = creq.Response()
    r.status_code = code
    r._content = msg.encode() if isinstance(msg, str) else msg
    return r

# --------------------------------------------------------------------------- #
# Webhook (DM forwarding)                                                     #
# --------------------------------------------------------------------------- #
_buyer_forum_threads = {}   # user_id / channel_id -> forum thread_id for ticket continuation
_buyer_context_history = {} # user_id -> list of recent message dicts for spaced-out DM context

def _avatar_url(user):
    """Build Discord CDN avatar URL for a user."""
    uid = user.get("id")
    av = user.get("avatar")
    if av:
        ext = "gif" if av.startswith("a_") else "png"
        return f"{_avatar_base}/avatars/{uid}/{av}.{ext}?size=256"
    disc = user.get("discriminator") or "0"
    try:
        idx = int(disc) % 5
    except Exception:
        idx = 0
    return f"{_avatar_base}/embed/avatars/{idx}.png"

def send_webhook(content, username=None, avatar_url=None, embed=None, embeds=None, thread_name=None, thread_id=None, buyer_key=None):
    """Send a single message to the configured DM webhook (supports text & forum channels and thread continuation)."""
    global _dm_forward_failures
    if not DM_WEBHOOK_URL:
        return True
    if _dm_forward_failures >= 5:
        dbg("webhook: too many failures, dropping")
        return False
    payload = {"allowed_mentions": {"parse": []}}
    if content:
        payload["content"] = content
    if username:
        payload["username"] = username[:80]
    if avatar_url:
        payload["avatar_url"] = avatar_url
    if embed is not None:
        payload["embeds"] = [embed]
    elif embeds is not None:
        payload["embeds"] = embeds
    if thread_name and not thread_id:
        payload["thread_name"] = thread_name[:100]
    if not payload.get("content") and not payload.get("embeds"):
        return True
    try:
        # Route webhook POSTs through the same proxy (if set) so the
        # outbound IP is consistent with the rest of the bot. Use a
        # throwaway session (no Discord auth cookies) — webhooks use
        # their own URL token.
        wh_proxies = {"http": HTTPS_PROXY, "https": HTTPS_PROXY} if HTTPS_PROXY else None
        r = None
        target_url = DM_WEBHOOK_URL + "?wait=true"
        if thread_id:
            target_url += f"&thread_id={thread_id}"
        for attempt in range(3):
            try:
                r = creq.post(target_url,
                              json=payload, impersonate=_BROWSER, timeout=DM_WEBHOOK_TIMEOUT,
                              proxies=wh_proxies)
                if r.status_code in (200, 204):
                    _dm_forward_failures = 0
                    if buyer_key:
                        try:
                            resp_data = r.json()
                            if isinstance(resp_data, dict):
                                tid = str(resp_data.get("channel_id") or resp_data.get("thread_id") or "")
                                if tid:
                                    _buyer_forum_threads[str(buyer_key)] = tid
                        except Exception as _ignored_exc:
                            print(f"[SENDER] send_webhook: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
                    return True
                # If thread_id failed with 400 or 404 (thread deleted or archived, or text channel mode), fall back to standard POST
                if thread_id and r.status_code in (400, 404):
                    fallback_url = DM_WEBHOOK_URL + "?wait=true"
                    if thread_name:
                        payload["thread_name"] = thread_name[:100]
                    r2 = creq.post(fallback_url,
                                   json=payload, impersonate=_BROWSER, timeout=DM_WEBHOOK_TIMEOUT,
                                   proxies=wh_proxies)
                    if r2.status_code in (200, 204):
                        _dm_forward_failures = 0
                        if buyer_key:
                            try:
                                resp_data = r2.json()
                                if isinstance(resp_data, dict):
                                    tid = str(resp_data.get("channel_id") or "")
                                    if tid:
                                        _buyer_forum_threads[str(buyer_key)] = tid
                            except Exception as _ignored_exc:
                                print(f"[SENDER] send_webhook: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
                        return True
                    # If forum thread_name failed on a plain text channel
                    if r2.status_code == 400 and "thread_name" in payload:
                        plain_payload = {k: v for k, v in payload.items() if k != "thread_name"}
                        r3 = creq.post(fallback_url,
                                       json=plain_payload, impersonate=_BROWSER, timeout=DM_WEBHOOK_TIMEOUT,
                                       proxies=wh_proxies)
                        if r3.status_code in (200, 204):
                            _dm_forward_failures = 0
                            return True
                # If Discord returns 400 because thread_name was supplied on a standard text channel
                if r.status_code == 400 and "thread_name" in payload:
                    fallback_payload = {k: v for k, v in payload.items() if k != "thread_name"}
                    r2 = creq.post(DM_WEBHOOK_URL + "?wait=true",
                                   json=fallback_payload, impersonate=_BROWSER, timeout=DM_WEBHOOK_TIMEOUT,
                                   proxies=wh_proxies)
                    if r2.status_code in (200, 204):
                        _dm_forward_failures = 0
                        return True
                if 500 <= r.status_code < 600:
                    time.sleep(2 * (attempt + 1))
                    continue
                break
            except Exception as inner:
                dbg(f"webhook attempt {attempt+1} err: {type(inner).__name__}")
                time.sleep(2 * (attempt + 1))
        dbg(f"webhook failed ({getattr(r, 'status_code', '?')}): {getattr(r,'text','')[:200]}")
        _dm_forward_failures += 1
        return False
    except Exception as e:
        dbg(f"webhook exception: {type(e).__name__}: {e}")
        _dm_forward_failures += 1
        return False
        dbg(f"webhook failed ({getattr(r, 'status_code', '?')}): {getattr(r,'text','')[:200]}")
        _dm_forward_failures += 1
        return False
    except Exception as e:
        dbg(f"webhook exception: {type(e).__name__}: {e}")
        _dm_forward_failures += 1
        return False

# --------------------------------------------------------------------------- #
# Log webhook (optional — plain text action log to shared #farm-logs)     #
# --------------------------------------------------------------------------- #
_log_webhook_failures = 0

def send_log_webhook(msg, username=None, kind=None):
    """Send one typed action-log line to the shared #farm-logs webhook (or dashboard fallback).

    Discord lets each webhook message override its display name. Using
    ALT_NAME by default means the control bot can route all four alts through
    one channel without four webhook URLs.
    """
    global _log_webhook_failures
    target_url = LOG_WEBHOOK_URL or DASHBOARD_WEBHOOK_URL
    if not target_url:
        return
    if _log_webhook_failures >= 5:
        return  # stop trying after repeated failures
    category = str(kind or "INFO").upper()[:20]
    if not kind:
        upper = str(msg).upper()
        if "DEAL" in upper or "🔥" in str(msg): category = "DEAL"
        elif "CAUTION" in upper or "⚠️" in str(msg): category = "CAUTION"
        elif "ERROR" in upper or "FAIL" in upper or "❌" in str(msg): category = "ERROR"
        elif "CONTROL" in upper or "PAUSE" in upper or "RESUME" in upper: category = "CONTROL"
        elif "AFK" in upper or "☕" in str(msg): category = "AFK"
    
    alt_prefix = f"**[Alt {ALT_ID} · {ALT_NAME}]** " if ALT_ID else ""
    line = f"`[{_ts()}]` [{category}] {alt_prefix}{msg}"

    def _send():
        global _log_webhook_failures
        try:
            wh_proxies = {"http": HTTPS_PROXY, "https": HTTPS_PROXY} if HTTPS_PROXY else None
            payload = {"content": line[:2000],
                       "username": (username or f"Alt {ALT_ID}: {ALT_NAME}")[:80],
                       "allowed_mentions": {"parse": []}}
            r = creq.post(target_url + "?wait=true",
                          json=payload,
                          impersonate=_BROWSER, timeout=WEBHOOK_TIMEOUT,
                          proxies=wh_proxies)
            if r.status_code == 400:
                payload["thread_name"] = f"📜 Logs (Alt {ALT_ID}: {ALT_NAME})"
                r = creq.post(target_url + "?wait=true",
                              json=payload,
                              impersonate=_BROWSER, timeout=WEBHOOK_TIMEOUT,
                              proxies=wh_proxies)
            if r.status_code in (200, 204):
                _log_webhook_failures = 0
            elif not (500 <= r.status_code < 600):
                dbg(f"[LOG-WEBHOOK] failed (HTTP {r.status_code}): {getattr(r,'text','')[:120]}")
                _log_webhook_failures += 1
        except Exception as e:
            dbg(f"[LOG-WEBHOOK] exception: {type(e).__name__}: {e}")
            _log_webhook_failures += 1
    threading.Thread(target=_send, daemon=True).start()

# --------------------------------------------------------------------------- #
# Dashboard webhook (optional — periodic run summaries as a Discord embed)    #
# --------------------------------------------------------------------------- #
_dash_webhook_failures = 0
_last_dash_summary = 0.0  # epoch of last dashboard summary push
_dash_lock = threading.Lock()

def send_dashboard(embed_dict):
    """Send a single embed to DASHBOARD_WEBHOOK_URL (if set).

    Thread-safe: can be called from any thread (main or daemon verification).
    Failures never crash the bot and self-throttle after 5 consecutive errors.
    """
    global _dash_webhook_failures
    if not DASHBOARD_WEBHOOK_URL:
        return False
    if _dash_webhook_failures >= 5:
        return False
    try:
        wh_proxies = {"http": HTTPS_PROXY, "https": HTTPS_PROXY} if HTTPS_PROXY else None
        sender_name = f"Alt {ALT_ID}: {ALT_NAME} Dashboard" if ALT_ID else "Ad-Bot Dashboard"
        payload = {
            "username": sender_name[:80],
            "allowed_mentions": {"parse": []},
            "embeds": [embed_dict],
        }
        # Run in a daemon thread so it never blocks the main loop
        def _send():
            global _dash_webhook_failures
            try:
                r = creq.post(DASHBOARD_WEBHOOK_URL + "?wait=true",
                              json=payload, impersonate=_BROWSER, timeout=WEBHOOK_TIMEOUT,
                              proxies=wh_proxies)
                if r.status_code == 400:
                    payload_forum = dict(payload)
                    payload_forum["thread_name"] = f"📊 Dashboard (Alt {ALT_ID}: {ALT_NAME})" if ALT_ID else "📊 Live Dashboard"
                    r = creq.post(DASHBOARD_WEBHOOK_URL + "?wait=true",
                                  json=payload_forum, impersonate=_BROWSER, timeout=WEBHOOK_TIMEOUT,
                                  proxies=wh_proxies)
                if r.status_code in (200, 204):
                    _dash_webhook_failures = 0
                elif r.status_code not in (429,) and not (500 <= r.status_code < 600):
                    dbg(f"[DASH-WEBHOOK] failed (HTTP {r.status_code})")
                    _dash_webhook_failures += 1
            except Exception as e:
                dbg(f"[DASH-WEBHOOK] exception: {type(e).__name__}: {e}")
                _dash_webhook_failures += 1
        threading.Thread(target=_send, daemon=True).start()
        return True
    except Exception as e:
        dbg(f"[DASH-WEBHOOK] spawn error: {type(e).__name__}: {e}")
        return False

# --------------------------------------------------------------------------- #
# Deal-alert delivery (optional — separate deals webhook)                    #
# --------------------------------------------------------------------------- #
_deal_webhook_failures = 0

def send_deal_webhook(embed_dict, thread_name=None):
    """Send deal alerts to DEAL_WEBHOOK_URL, never to the dashboard webhook (supports text & forum channels)."""
    global _deal_webhook_failures
    target = DEAL_WEBHOOK_URL
    if not target:
        return False
    if _deal_webhook_failures >= 5:
        return False
    try:
        wh_proxies = {"http": HTTPS_PROXY, "https": HTTPS_PROXY} if HTTPS_PROXY else None
        sender_name = f"Alt {ALT_ID}: {ALT_NAME} · Deals" if ALT_ID else f"{ALT_NAME} · Deals"
        payload = {
            "username": sender_name[:80],
            "allowed_mentions": {"parse": []},
            "embeds": [embed_dict],
        }
        if thread_name:
            payload["thread_name"] = thread_name[:100]
        def _send():
            global _deal_webhook_failures
            try:
                r = creq.post(target + "?wait=true",
                              json=payload, impersonate=_BROWSER, timeout=WEBHOOK_TIMEOUT,
                              proxies=wh_proxies)
                if r.status_code in (200, 204):
                    _deal_webhook_failures = 0
                elif r.status_code == 400 and "thread_name" in payload:
                    fallback_payload = {k: v for k, v in payload.items() if k != "thread_name"}
                    r2 = creq.post(target + "?wait=true",
                                   json=fallback_payload, impersonate=_BROWSER, timeout=WEBHOOK_TIMEOUT,
                                   proxies=wh_proxies)
                    if r2.status_code in (200, 204):
                        _deal_webhook_failures = 0
                elif r.status_code not in (429,) and not (500 <= r.status_code < 600):
                    dbg(f"[DEAL-WEBHOOK] failed (HTTP {r.status_code})")
                    _deal_webhook_failures += 1
            except Exception as e:
                dbg(f"[DEAL-WEBHOOK] exception: {type(e).__name__}: {e}")
                _deal_webhook_failures += 1
        threading.Thread(target=_send, daemon=True).start()
        return True
    except Exception as e:
        dbg(f"[DEAL-WEBHOOK] spawn error: {type(e).__name__}: {e}")
        return False

def _dashboard_startup_embed(version, ad_type, ch_list, interval_min, runtime_min, variants, use_img, total_channels, active_count):
    """Build the startup dashboard embed."""
    alt_prefix = f"[Alt {ALT_ID} · {ALT_NAME}] " if ALT_ID else ""
    return {
        "title": f"🟢 {alt_prefix}STARTED {version}",
        "color": 0x57F287,  # green
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fields": [
            {"name": "Mode", "value": f"`{ad_type}`", "inline": True},
            {"name": "Interval", "value": f"~{interval_min} min/ch (±jitter)", "inline": True},
            {"name": "Runtime", "value": f"{runtime_min:.0f} min ({runtime_min/60:.1f}h)", "inline": True},
            {"name": "Channels", "value": ch_list or "—", "inline": False},
            {"name": "Active / Total", "value": f"{active_count} / {total_channels}", "inline": True},
            {"name": "Variations", "value": str(variants), "inline": True},
            {"name": "Image", "value": "ON (after warmup)" if use_img else "OFF (text-only)", "inline": True},
        ],
    }

def _dashboard_cycle_embed(cycle, elapsed_min, sent, img_attach, txt_only, edits, errs, skips, per_ch, active_count, total_channels, active_channels_set, ch_names_dict, slowmodes_dict, last_sent_dict, my_last_id_dict, in_afk_flag=False, afk_left=0.0, is_shutdown=False):
    """Build per-cycle / shutdown dashboard embed."""
    color = 0xED4245 if (errs > 0 or is_shutdown) else 0x5865F2  # red/blue
    alt_prefix = f"[Alt {ALT_ID} · {ALT_NAME}] " if ALT_ID else ""
    title = f"🏁 {alt_prefix}SHUTDOWN summary" if is_shutdown else f"📊 {alt_prefix}Cycle {cycle}"
    lines = []
    for cid in CHANNEL_IDS:
        name = ch_names_dict.get(cid, cid)
        s = per_ch[cid]
        alive = "✅" if cid in active_channels_set else "⛔"
        last_ts = last_sent_dict.get(cid)
        if last_ts:
            last_str = datetime.fromtimestamp(last_ts).strftime("%H:%M:%S")
        else:
            last_str = "—"
        lines.append(
            f"{alive} **#{name}** `{cid}`\n"
            f"   ↳ sent:{s['sent']} (💬{s['txt']}/📷{s['img']}/✏️{s['edits']})  "
            f"err:{s['errors']}  last:{last_str}"
        )
    ch_breakdown = "\n".join(lines) if lines else "—"
    afk_str = f"☕ AFK — {afk_left/60:.1f}m remaining" if in_afk_flag else "active"
    fields = [
        {"name": "Uptime", "value": f"{elapsed_min:.1f} min ({elapsed_min/60:.2f}h)", "inline": True},
        {"name": "Total sent", "value": f"**{sent}**  (📷{img_attach} / 💬{txt_only})", "inline": True},
        {"name": "✏️ Edits", "value": str(edits), "inline": True},
        {"name": "❌ Errors", "value": str(errs), "inline": True},
        {"name": "⏭️ Skips", "value": str(skips), "inline": True},
        {"name": "Channels (active/total)", "value": f"{active_count} / {total_channels}", "inline": True},
        {"name": "Status", "value": afk_str, "inline": False},
        {"name": "Per-channel", "value": ch_breakdown[:1000] or "—", "inline": False},
    ]
    return {
        "title": title,
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fields": fields,
    }

def _send_completion_summary_webhook(reason, start_ts, sent, err, skip, distractions, img, edits, per_ch, is_shutdown=False):
    """Send a rich Discord summary embed to DASHBOARD_WEBHOOK_URL and log notification to LOG_WEBHOOK_URL."""
    elapsed_sec = max(1.0, time.time() - start_ts)
    elapsed_min = elapsed_sec / 60.0
    elapsed_h = elapsed_min / 60.0

    is_chained_handoff = (CHUNK_INDEX < TOTAL_CHUNKS) and not is_shutdown and err == 0
    if is_shutdown:
        status_label = "🛑 RUN TERMINATED (Operator / Safety Stop)"
        color = 0xED4245
    elif is_chained_handoff:
        status_label = f"🏁 CHUNK {CHUNK_INDEX}/{TOTAL_CHUNKS} COMPLETE (Chaining to Chunk {CHUNK_INDEX+1})"
        color = 0x57F287
    else:
        status_label = f"🎉 ALL CHUNKS COMPLETED ({TOTAL_HOURS:.0f}h Target Reached)"
        color = 0x57F287

    total_ops = sent + err
    error_rate = (err / total_ops * 100) if total_ops > 0 else 0.0
    velocity = (sent / elapsed_h) if elapsed_h > 0 else 0.0

    fields = [
        {
            "name": "⏱️ Duration & Velocity",
            "value": f"• Elapsed: `{elapsed_min:.1f} min` ({elapsed_h:.2f}h)\n• Throughput: `{velocity:.1f} ads/hr`\n• AFK Breaks: `{distractions}` pauses",
            "inline": True,
        },
        {
            "name": "📤 Deliveries & Edits",
            "value": f"• Total Sent: `{sent}` posts\n• 💬 Text: `{sent - img}` · 📷 Image: `{img}`\n• ✏️ Typo Edits: `{edits}` successful",
            "inline": True,
        },
        {
            "name": "🛡️ Health & Reliability",
            "value": f"• Errors: `{err}` ({error_rate:.1f}% error rate)\n• Skips: `{skip}` cycles\n• Blacklisted: `{len(_blocked_variations)}`",
            "inline": True,
        },
    ]

    ch_lines = []
    for cid in CHANNEL_IDS[:8]:
        s = per_ch.get(cid, {"sent": 0, "errors": 0, "skipped": 0, "txt": 0, "img": 0, "edits": 0})
        name = ch_names.get(cid, cid) if ch_names else cid
        ch_lines.append(f"• `#{name}`: **{s['sent']}** sent (💬{s['txt']}/📷{s['img']}/✏️{s['edits']}) · ❌{s['errors']} err")
    if ch_lines:
        fields.append({
            "name": f"📂 Channel Breakdown ({len(CHANNEL_IDS)} targets)",
            "value": "\n".join(ch_lines[:6]) + (f"\n*+{len(ch_lines)-6} more...*" if len(ch_lines) > 6 else ""),
            "inline": False,
        })

    if is_chained_handoff:
        fields.append({
            "name": "⏭️ Next Workflow Action",
            "value": f"🔄 Starting Chunk `{CHUNK_INDEX + 1}/{TOTAL_CHUNKS}` in ~2 minutes (reconnect jitter).",
            "inline": False,
        })
    else:
        fields.append({
            "name": "⏭️ Next Workflow Action",
            "value": "✅ Execution run completed. Runner entering idle standby.",
            "inline": False,
        })

    embed = {
        "title": f"📊 Alt {ALT_ID} ({ALT_NAME}) — {status_label}",
        "description": f"**Status**: `{reason}`\n**Progress**: Chunk `{CHUNK_INDEX}/{TOTAL_CHUNKS}` · Budget: `{TOTAL_HOURS:.0f}h total` ({TOTAL_RUN_MIN:.0f}m/chunk)",
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fields": fields,
        "footer": {
            "text": f"adfarm-core-AI · Alt {ALT_ID} · Chunk {CHUNK_INDEX}/{TOTAL_CHUNKS}",
        },
    }

    send_dashboard(embed)
    send_log_webhook(
        f"📊 **[Alt {ALT_ID}] RUN COMPLETE**: {reason} | Sent `{sent}` ads ({sent-img} text, {img} img) | Errors `{err}` | Elapsed `{elapsed_min:.1f}m` (Chunk {CHUNK_INDEX}/{TOTAL_CHUNKS})",
        kind="CONTROL"
    )

def _format_attachments(attachments):
    """Return a string listing attachment URLs for forwarding."""
    if not attachments:
        return ""
    lines = []
    for a in attachments:
        url = a.get("url", "")
        fn = a.get("filename", "attachment")
        size = a.get("size", 0)
        if a.get("content_type", "").startswith("image/"):
            lines.append(f"🖼️ [{fn}]({url})")
        else:
            mb = size / (1024*1024) if size else 0
            size_str = f" ({mb:.1f}MB)" if mb else ""
            lines.append(f"📎 [{fn}]({url}){size_str}")
    return "\n".join(lines)

def _format_dm_ago(sec):
    if sec < 60:
        return "just now"
    if sec < 3600:
        return f"-{int(sec // 60)}m"
    if sec < 86400:
        return f"-{int(sec // 3600)}h"
    return f"-{int(sec // 86400)}d"

def forward_dm_message(channel_id, user_obj, content, attachments, is_me=False):
    """Forward a DM (one side of the conversation) to the webhook with thread and context memory."""
    if not DM_WEBHOOK_URL:
        return
    uid = str(user_obj.get("id") or channel_id or "0")
    uname = (user_obj.get("username") or user_obj.get("global_name")
             or _me_cache.get("global_name") or "unknown")
    if is_me:
        uname = f"{uname} (alt)"
    av = _avatar_url(user_obj)
    att_text = _format_attachments(attachments)
    body = content or ""
    if att_text:
        body = (body + "\n" + att_text).strip()

    # Track conversation context history per buyer across spaced-out DMs
    now_ts = time.time()
    if uid not in _buyer_context_history:
        _buyer_context_history[uid] = []
    if body and body != "*(empty — embed/attachment only)*":
        _buyer_context_history[uid].append({
            "ts": now_ts,
            "text": body[:200],
            "is_me": is_me,
            "uname": uname,
        })
        if len(_buyer_context_history[uid]) > 10:
            _buyer_context_history[uid] = _buyer_context_history[uid][-10:]

    # Discord deep link — opens the DM channel directly
    deep_link = f"https://discord.com/channels/@me/{channel_id}"
    embed = {
        "type": "rich",
        "color": 0x2F3136 if is_me else 0x57F287,  # grey for us, green for buyer
        "footer": {"text": f"Alt {ALT_ID}: {ALT_NAME} · Open DM" if ALT_ID else f"{ALT_NAME} · Open DM", "icon_url": av},
        "url": deep_link,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    thread_title = f"💬 [Alt {ALT_ID}] {uname[:20]} ({uid[-4:] if len(uid)>=4 else uid})" if ALT_ID else f"💬 {uname[:24]} ({uid[-4:] if len(uid)>=4 else uid})"

    if not is_me and body and body != "*(empty — embed/attachment only)*":
        # Aggregate recent buyer text (last 2 hours) to build cumulative intent across spaced-out DMs
        recent_texts = [
            item["text"] for item in _buyer_context_history[uid]
            if not item.get("is_me") and (now_ts - item.get("ts", 0)) < 7200
        ]
        cumulative_text = "\n".join(recent_texts) if recent_texts else body
        intent = _classify_dm_intent(cumulative_text)

        badges = [f"**Intent:** `{intent['category']}`", f"**Priority:** `{intent['priority']}`"]
        if intent.get("game"):
            badges.append(f"**Game:** `{intent['game']}`")
        if intent["volume"]:
            badges.append(f"**Vol:** `{intent['volume']}`")
        if intent["payments"]:
            badges.append(f"**Pay:** {', '.join(intent['payments'])}")

        fields = [
            {"name": "🏷️ Smart Intent Classification", "value": " · ".join(badges), "inline": False},
        ]

        # If there are spaced-out past messages from this buyer, show recent history for instant context
        history = _buyer_context_history[uid]
        if len(history) > 1:
            hist_lines = []
            for h in history[-5:]:
                role_label = f"Alt {ALT_ID}" if h.get("is_me") else "Buyer"
                ago_str = _format_dm_ago(now_ts - h.get("ts", now_ts))
                clean_h_text = h.get("text", "").replace("\n", " ")[:70]
                hist_lines.append(f"• `[{ago_str}]` **{role_label}:** \"{clean_h_text}\"")
            if hist_lines:
                fields.append({"name": "📜 Conversation Context (spaced DMs)", "value": "\n".join(hist_lines)[:1024], "inline": False})

        fields.append({"name": "⚡ Quick Reply Command", "value": f"`/reply alt:{ALT_ID} user:{uid} text:...`", "inline": False})
        embed["fields"] = fields

        tag_badge = intent.get('game') or intent['category']
        thread_title = f"🏷️ [Alt {ALT_ID}] [{tag_badge}] {uname[:18]}" if ALT_ID else f"🏷️ [{tag_badge}] {uname[:20]}"

    if not body:
        body = "*(empty — embed/attachment only)*"

    existing_thread_id = _buyer_forum_threads.get(uid) or _buyer_forum_threads.get(str(channel_id))
    send_webhook(
        body[:2000],
        username=uname[:80],
        avatar_url=av,
        embed=embed,
        thread_name=thread_title,
        thread_id=existing_thread_id,
        buyer_key=uid,
    )

# --------------------------------------------------------------------------- #
# Blocklist Gist persistence                                                  #
# --------------------------------------------------------------------------- #
_GIST_FILENAME = "blocked_variations.json"

def load_blocked_from_gist():
    if not GIST_TOKEN or not GIST_ID:
        dbg("[GIST] No GIST_TOKEN/GIST_ID configured — starting with fresh (empty) blocklist")
        return
    log(f"📚 Loading auto-learn blocklist from gist {GIST_ID[:8]}...")
    try:
        r = creq.get(f"https://api.github.com/gists/{GIST_ID}",
                     headers={"Authorization": f"token {GIST_TOKEN}",
                              "Accept": "application/vnd.github+json",
                              "User-Agent": "discord-ad-sender"},
                     impersonate=_BROWSER, timeout=15)
        if r.status_code != 200:
            log(f"⚠️ Could not fetch gist ({r.status_code}) — starting with empty blocklist")
            return
        j = r.json()
        file_info = j.get("files", {}).get(_GIST_FILENAME)
        if not file_info:
            log("   (no blocklist file in gist yet, will create on first save)")
            return
        raw = file_info.get("content") or ""
        data = json.loads(raw)
        loaded = set(data.get("blocked", []))
        scores_data = data.get("scores", {})
        with _state_lock:
            _blocked_variations.update(loaded)
            if isinstance(scores_data, dict):
                for k, v in scores_data.items():
                    try:
                        _variation_scores[k] = max(0, int(v))
                    except (TypeError, ValueError) as _ignored_exc:
                        print(f"[SENDER] load_blocked_from_gist: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
        log(f"📚 Loaded {len(loaded)} blocked variations and {len(_variation_scores)} survival scores from gist")
    except Exception as e:
        log(f"⚠️ Failed to load gist blocklist: {type(e).__name__}: {e}")

def save_blocked_to_gist(force=False):
    global _last_save_to_gist
    if not GIST_TOKEN or not GIST_ID:
        return False
    if not force and (time.time() - _last_save_to_gist) < 300:
        return False  # throttle to every 5 min
    try:
        with _state_lock:
            snapshot = list(_blocked_variations)
            scores_snapshot = {k: v for k, v in _variation_scores.items() if v > 0}
        payload = {
            "files": {
                _GIST_FILENAME: {
                    "content": json.dumps({
                        "version": VERSION,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "count": len(snapshot),
                        "blocked": snapshot,
                        "scores": scores_snapshot,
                    }, indent=2, ensure_ascii=False)
                }
            }
        }
        r = creq.patch(f"https://api.github.com/gists/{GIST_ID}",
                      headers={"Authorization": f"token {GIST_TOKEN}",
                               "Accept": "application/vnd.github+json",
                               "User-Agent": "discord-ad-sender"},
                      data=json.dumps(payload),
                      impersonate=_BROWSER, timeout=15)
        if r.status_code in (200, 201):
            _last_save_to_gist = time.time()
            dbg(f"saved {len(snapshot)} blocked variations and {len(scores_snapshot)} scores to gist")
            return True
        dbg(f"gist save failed ({r.status_code}): {getattr(r,'text','')[:200]}")
        return False
    except Exception as e:
        dbg(f"gist save exception: {e}")
        return False

def _blacklist_variation(text):
    """Add a variation to the blocklist (thread-safe, persist to gist)."""
    with _state_lock:
        if text in _blocked_variations:
            return
        _blocked_variations.add(text)
    snip = text.replace("\n", " ⏎ ")[:60]
    log(f"   🚫 Blacklisted variation: \"{snip}{'...' if len(text) > 60 else ''}\"")
    save_blocked_to_gist()

def _record_strike(text, cid, mid):
    """Record a strike for a variation. If strikes >= BLOCKED_STRIKES, blacklist.

    NOTE: this can be called from a background daemon thread (post-send
    verification), so for the safety stop we use os._exit() — threading.Thread
    swallows SystemExit raised in a child thread, so sys.exit() there only
    kills the verification thread and the bot keeps running.
    """
    global _consecutive_deletions
    with _state_lock:
        _strikes[text] += 1
        _variation_scores[text] = max(0, _variation_scores.get(text, 0) - 2)
        n = _strikes[text]
        _consecutive_deletions += 1
        consec = _consecutive_deletions
    if n >= BLOCKED_STRIKES:
        _blacklist_variation(text)
    else:
        log(f"   ⚠️ Strike {n}/{BLOCKED_STRIKES} for this variation")
    if consec >= BLOCKED_SAFETY_STOP:
        log("")
        log(f"🛑 SAFETY STOP: {consec} different variations deleted in a row.")
        log("   This means the ANTI-SPAM IS DELETING EVERYTHING — the account/IP is")
        log("   flagged, not the text. Stopping to avoid burning the alt further.")
        try:
            save_blocked_to_gist(force=True)
        except Exception as _ignored_exc:
            print(f"[SENDER] _record_strike: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
        log("   Cancel the run, age the alt more (24h+), switch proxy/IP, and retry.")
        send_log_webhook(
            f"🛑 **SAFETY STOP** `{consec}` consecutive deletions — account/IP flagged. Aborting."
        )
        # Use os._exit, not sys.exit: this path can be reached from a daemon
        # verification thread, and SystemExit in a child thread only kills
        # that thread, leaving the bot running blind.
        os._exit(2)

def _record_success(text, cid=None, mid=None):
    """Reinforce survival score when a variation survives verification."""
    global _consecutive_deletions
    with _state_lock:
        _consecutive_deletions = 0
        if text:
            _variation_scores[text] = _variation_scores.get(text, 0) + 1

def _pick_surviving_variation(variations, used=None):
    """Pick a variation using weighted random choice favoring surviving variations."""
    if not variations:
        return ""
    with _state_lock:
        blocked = set(_blocked_variations)
        scores = dict(_variation_scores)
    used_set = set(used) if used else set()
    available = [v for v in variations if v not in used_set and v not in blocked]
    if not available:
        available = [v for v in variations if v not in blocked]
    if not available:
        return ""
    weights = [1.0 + min(5.0, scores.get(v, 0) * 0.5) for v in available]
    return random.choices(available, weights=weights, k=1)[0]

def _reset_consecutive_deletions():
    """Call when a post survives verification."""
    global _consecutive_deletions
    with _state_lock:
        _consecutive_deletions = 0

# --------------------------------------------------------------------------- #
# Proactive Rate Limiter (F-26 V6)                                          #
# --------------------------------------------------------------------------- #
class RateLimiter:
    """Thread-safe per-bucket proactive rate limiter.

    Discord returns X-RateLimit-Bucket / -Remaining / -Reset-After / -Limit on
    every response. Real clients read those and avoid sending when
    Remaining==0 until Reset-After elapses. Doing this proactively avoids
    accumulating 429 responses (which hurt account trust scores).
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._buckets = {}
        self._route_to_bucket = {}

    @staticmethod
    def _route_key(url):
        """Fold minor IDs (message IDs etc.) but keep channel/guild major IDs."""
        try:
            i = url.find("/api/v")
            if i < 0:
                return url
            path = url[i:].split("?")[0]
            parts = path.split("/")
            out = []
            for idx, seg in enumerate(parts):
                if idx > 0 and parts[idx-1] in ("channels", "guilds", "webhooks"):
                    out.append(seg)
                elif re.match(r'^\d{16,20}$', seg):
                    out.append(":id")
                else:
                    out.append(seg)
            return "/".join(out)
        except Exception:
            return url

    def wait(self, url):
        if not RATELIMIT_PREADJUST:
            return
        key = self._route_key(url)
        with self._lock:
            b = self._buckets.get(key) or self._route_to_bucket.get(key)
            if not b:
                return
            remaining = b.get("remaining", 1)
            reset_at = b.get("reset_at", 0)
        if remaining <= 0:
            w = reset_at - time.time()
            if w > 0:
                jitter = w * RATELIMIT_JITTER + random.uniform(0.02, 0.15)
                dbg(f"[RL] pre-emptive wait {(w+jitter):.2f}s for {key[:60]}")
                time.sleep(w + jitter)

    def update(self, url, r):
        if not RATELIMIT_PREADJUST:
            return
        try:
            remaining_h = r.headers.get("X-RateLimit-Remaining")
            reset_h = r.headers.get("X-RateLimit-Reset-After") or r.headers.get("X-RateLimit-Reset")
            limit_h = r.headers.get("X-RateLimit-Limit")
            bucket_h = r.headers.get("X-RateLimit-Bucket")
            now = time.time()
            key = self._route_key(url)
            with self._lock:
                if bucket_h:
                    self._route_to_bucket[key] = bucket_h
                    bkey = bucket_h
                else:
                    bkey = key
                b = self._buckets.setdefault(bkey, {"remaining": 999, "reset_at": now, "limit": 999})
                if remaining_h is not None:
                    try: b["remaining"] = int(remaining_h)
                    except Exception as _exc:
                        log(f"[API] rate-limit remaining header unparsable ({remaining_h!r}): {_exc}", kind="DEBUG")
                if reset_h is not None:
                    try:
                        rv = float(reset_h)
                        if r.headers.get("X-RateLimit-Reset-After") is not None:
                            b["reset_at"] = now + rv
                        else:
                            # X-RateLimit-Reset is absolute seconds
                            if rv > 1e12: rv = rv / 1000.0
                            if rv > now + 1: b["reset_at"] = rv
                            elif rv > 0: b["reset_at"] = now + rv
                            else: b["reset_at"] = now + 1
                    except Exception:
                        b["reset_at"] = now + 5
                if limit_h is not None:
                    try: b["limit"] = int(limit_h)
                    except Exception as _exc:
                        log(f"[API] rate-limit cap header unparsable ({limit_h!r}): {_exc}", kind="DEBUG")
                if r.status_code == 429:
                    try:
                        j = r.json()
                        ra = float(j.get("retry_after", 5))
                        b["remaining"] = 0
                        b["reset_at"] = now + ra
                    except Exception:
                        b["remaining"] = 0
                        b["reset_at"] = now + 5
        except Exception as e:
            dbg(f"[RL] update error: {e}")

_RATELIMITER = RateLimiter()

# --------------------------------------------------------------------------- #
# Fine-Grained Cascading Circuit Breaker                                      #
# --------------------------------------------------------------------------- #
class CircuitBreaker:
    """Per-channel fine-grained cascading circuit breaker (CLOSED / OPEN / HALF-OPEN).

    States:
      - CLOSED: normal healthy execution
      - OPEN: tripped after threshold failures; calls fail fast with cool-off
      - HALF-OPEN: single canary attempt permitted to verify recovery
    """
    def __init__(self, failure_threshold=3, recovery_timeout=180.0):
        self._lock = threading.Lock()
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._failures = defaultdict(int)
        self._state = defaultdict(lambda: "CLOSED")
        self._tripped_at = defaultdict(float)

    def is_allowed(self, target_id: str) -> bool:
        with self._lock:
            state = self._state[target_id]
            if state == "CLOSED":
                return True
            now = time.time()
            if state == "OPEN":
                if now - self._tripped_at[target_id] >= self.recovery_timeout:
                    self._state[target_id] = "HALF-OPEN"
                    dbg(f"[CIRCUIT] {target_id} entered HALF-OPEN state (trial probe)")
                    return True
                return False
            return True

    def record_success(self, target_id: str):
        with self._lock:
            self._failures[target_id] = 0
            if self._state[target_id] != "CLOSED":
                dbg(f"[CIRCUIT] {target_id} recovered to CLOSED state")
            self._state[target_id] = "CLOSED"
            self._tripped_at[target_id] = 0.0

    def record_failure(self, target_id: str, error_code: int = 0):
        with self._lock:
            self._failures[target_id] += 1
            if self._failures[target_id] >= self.failure_threshold or error_code in (429, 403):
                self._state[target_id] = "OPEN"
                self._tripped_at[target_id] = time.time()
                cool_off = self.recovery_timeout * min(4, self._failures[target_id] - self.failure_threshold + 1)
                dbg(f"[CIRCUIT] {target_id} TRIPPED to OPEN for {cool_off:.0f}s (failures={self._failures[target_id]}, code={error_code})")

    def reset(self, target_id: str | None = None):
        with self._lock:
            if target_id:
                self._failures.pop(target_id, None)
                self._state.pop(target_id, None)
                self._tripped_at.pop(target_id, None)
            else:
                self._failures.clear()
                self._state.clear()
                self._tripped_at.clear()

_CIRCUIT_BREAKER = CircuitBreaker()

# --------------------------------------------------------------------------- #
# Shadowban Caution Mode helpers (F-27 V6)                                  #
# --------------------------------------------------------------------------- #
def _record_verification(cid, mid, survived):
    """Record an authoritative exact-message pass/fail.

    ``None`` means verification was inconclusive (timeout, API failure, or a
    permission response) and must never become a phantom deletion strike.
    """
    if survived is None:
        return
    with _state_lock:
        hist = _channel_verify_history.setdefault(cid, deque(maxlen=max(1, CAUTION_WINDOW)))
        hist.append(bool(survived))
        was_caution = _caution_channels.get(cid, False)
        if survived:
            if was_caution:
                streak = _channel_caution_survives.get(cid, 0) + 1
                _channel_caution_survives[cid] = streak
                if streak >= CAUTION_EXIT_STREAK:
                    _caution_channels.pop(cid, None)
                    _channel_caution_survives.pop(cid, None)
                    log(f"🟢 #{cid}: {streak} survives in a row — EXITING CAUTION MODE (normal cadence resumed).")
                    send_log_webhook(f"🟢 **CAUTION EXIT** channel `{cid}` after {streak} survives")
                    try:
                        send_dashboard({
                            "title": "✅ CAUTION MODE CLEARED",
                            "color": 0x57F287,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "description": (f"Channel `{cid}` has had {streak} consecutive surviving posts. "
                                            "Normal cadence, images, reactions, and edits may resume."),
                        })
                    except Exception as _ignored_exc:
                        print(f"[SENDER] _record_verification: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
        else:
            _channel_caution_survives[cid] = 0
            fails = sum(1 for v in hist if not v)
            if not was_caution and fails >= CAUTION_FAIL_THRESHOLD and len(hist) >= max(1, CAUTION_WINDOW):
                _caution_channels[cid] = True
                log(f"⚠️ #{cid}: {fails}/{len(hist)} recent posts deleted — ENTERING CAUTION MODE "
                    f"({CAUTION_INTERVAL_MULT:.1f}x interval, text-only, no reacts/edits).")
                send_log_webhook(f"🚨 **CAUTION MODE** channel `{cid}`: {fails}/{len(hist)} deleted")
                try:
                    send_dashboard({
                        "title": "🚨 CAUTION MODE ACTIVATED",
                        "color": 0xED4245,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "description": (f"Channel `{cid}`: {fails} of last {len(hist)} posts were deleted by "
                                        f"anti-spam. Throttling to {CAUTION_INTERVAL_MULT:.1f}× interval, "
                                        f"text-only, no reactions/edits until {CAUTION_EXIT_STREAK} posts survive."),
                    })
                except Exception as _ignored_exc:
                    print(f"[SENDER] _record_verification: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
    # Do not acquire _state_lock recursively: verification runs in a
    # background thread and _reset_consecutive_deletions takes that lock.
    if survived:
        _reset_consecutive_deletions()

def channel_in_caution(cid):
    with _state_lock:
        return _caution_channels.get(cid, False)

def channel_caution_multiplier(cid):
    return CAUTION_INTERVAL_MULT if channel_in_caution(cid) else 1.0

# --------------------------------------------------------------------------- #
# Auto Channel Discovery on 404 (V6)                                        #
# --------------------------------------------------------------------------- #
_guilds_cache_fetched = False
_guilds_list_cache = []

def _resolve_channel_keywords():
    """Resolve keyword-only channel targets using the authenticated account.

    This is intentionally best-effort and fail-closed: ambiguous or missing
    names are not guessed, and no channel is added unless Discord returns a
    unique text channel match. It lets a current installation start even when
    setup has a channel name but no numeric ID yet.
    """
    global CHANNEL_IDS
    numeric = [c for c in CHANNEL_IDS if c.isdigit()]
    keywords = [c for c in CHANNEL_IDS if not c.isdigit()] + CHANNEL_NAMES + CHANNEL_KEYWORDS
    keywords = list(dict.fromkeys(k.strip() for k in keywords if k.strip()))
    if not keywords:
        if not numeric:
            log("❌ CONFIG ERROR: provide CHANNEL_IDS or CHANNEL_NAMES/CHANNEL_KEYWORDS.")
        CHANNEL_IDS = numeric
        return bool(CHANNEL_IDS)
    gids = _fetch_my_guilds_fallback()
    if not gids:
        log("❌ CHANNEL RESOLUTION FAILED: no accessible guild context for keyword targets.")
        CHANNEL_IDS = numeric
        return bool(CHANNEL_IDS)
    resolved = list(numeric)
    for keyword in keywords:
        target = keyword.strip().lower().replace(" ", "-")
        matches = []
        for gid in gids:
            try:
                r = api("GET", f"https://discord.com/api/v9/guilds/{gid}/channels", retries=2)
                if r.status_code != 200:
                    continue
                data = r.json()
                for item in data if isinstance(data, list) else []:
                    if not isinstance(item, dict) or item.get("type") not in (0, 5):
                        continue
                    name = str(item.get("name") or "").lower().replace(" ", "-")
                    if name == target or target in name:
                        matches.append(item)
            except Exception as e:
                dbg(f"[CHANNEL] keyword lookup failed for {gid}/{keyword}: {e}")
        unique = {str(item.get("id")): item for item in matches if item.get("id")}
        if len(unique) != 1:
            log(f"⚠️ CHANNEL KEYWORD `{keyword}` matched {len(unique)} channels; leaving it unresolved (fail-closed).")
            continue
        item = next(iter(unique.values()))
        cid = str(item["id"])
        if cid not in resolved:
            resolved.append(cid)
        _CHANNEL_NAME_BY_ID[cid] = str(item.get("name") or keyword).lower()
        gid = item.get("guild_id")
        if gid:
            _guild_id_cache[cid] = str(gid)
            _channel_id_to_guild[cid] = str(gid)
        log(f"✅ CHANNEL KEYWORD `{keyword}` resolved → #{item.get('name') or keyword} ({cid})")
    CHANNEL_IDS = resolved
    if CHANNEL_IDS:
        return True
    log("❌ CHANNEL RESOLUTION FAILED: no unambiguous channel targets were found.")
    return False

def _fetch_my_guilds_fallback():
    """Last-resort: fetch /users/@me/guilds once to find a guild ID.
    Only called when discovery has no guild context from other channels."""
    global _guilds_cache_fetched, _guilds_list_cache
    if _guilds_cache_fetched:
        return _guilds_list_cache
    _guilds_cache_fetched = True
    try:
        r = api("GET", "https://discord.com/api/v9/users/@me/guilds", retries=2)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                _guilds_list_cache = [g.get("id") for g in data if isinstance(g, dict) and g.get("id")]
                dbg(f"[DISC] /@me/guilds fallback returned {len(_guilds_list_cache)} guild(s)")
    except Exception as e:
        dbg(f"[DISC] /@me/guilds fallback failed: {e}")
    return _guilds_list_cache


def _fetch_live_server_catalogue() -> list[dict]:
    """Fetch the authenticated account's complete eligible channel catalogue."""
    servers: list[dict] = []
    guild_ids = list(dict.fromkeys(str(gid) for gid in (_fetch_my_guilds_fallback() or []) if str(gid).isdigit()))
    # If the guild list endpoint is unavailable, retain the useful context
    # learned while probing configured channels rather than inventing a guild.
    if not guild_ids:
        guild_ids = list(dict.fromkeys(str(gid) for gid in _guild_id_cache.values() if str(gid).isdigit()))
    for gid in guild_ids:
        try:
            r = api("GET", f"https://discord.com/api/v9/guilds/{gid}/channels",
                    referer=f"https://discord.com/channels/{gid}", retries=2)
            if r.status_code != 200:
                dbg(f"[CHANNEL-STATE] guild {gid} inventory failed HTTP {r.status_code}")
                continue
            raw_channels = r.json()
            if not isinstance(raw_channels, list):
                continue
            channels = []
            for raw in raw_channels:
                if not isinstance(raw, dict) or raw.get("type") not in (0, 5):
                    continue
                cid = str(raw.get("id") or "").strip()
                if not cid.isdigit():
                    continue
                item = dict(raw)
                item["id"] = cid
                item["guild_id"] = gid
                channels.append(item)
                _guild_id_cache[cid] = gid
                _channel_id_to_guild[cid] = gid
            # Discord's guild-channel response does not include a guild name;
            # use the ID as a stable fallback and fill it from any cached
            # per-channel response when available.
            servers.append({"id": gid, "name": gid, "channels": channels})
        except Exception as exc:
            dbg(f"[CHANNEL-STATE] guild {gid} inventory failed: {type(exc).__name__}: {exc}")
    return servers


def _apply_registry_targets(result: dict) -> None:
    """Install reconciled targets into the live scheduler without resetting stats."""
    global CHANNEL_IDS
    targets = [str(cid) for cid in result.get("targets", []) if str(cid).isdigit()]
    catalogue = result.get("catalogue") if isinstance(result.get("catalogue"), dict) else {}
    with _state_lock:
        CHANNEL_IDS[:] = list(dict.fromkeys(targets))
        for cid in CHANNEL_IDS:
            record = catalogue.get(cid) if isinstance(catalogue.get(cid), dict) else {}
            if record.get("name"):
                _ch_names_ref[cid] = str(record["name"])[:80]
            if "slowmode" in record:
                try:
                    _slowmodes_ref[cid] = max(0, int(record["slowmode"]))
                except (TypeError, ValueError) as _ignored_exc:
                    print(f"[SENDER] _apply_registry_targets: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
            if _stats_ref is not None and cid not in _stats_ref:
                _stats_ref[cid] = {"sent": 0, "errors": 0, "skipped": 0,
                                   "cooldown": 0, "img": 0, "txt": 0, "edits": 0}
            if cid in catalogue:
                if cid not in _active_ch_ref:
                    _active_ch_ref.append(cid)
                _dead_channels_ref.discard(cid)
                _next_post_ref.setdefault(cid, time.time() + random.uniform(15, 40))
            else:
                _dead_channels_ref.add(cid)
                if cid in _active_ch_ref:
                    _active_ch_ref.remove(cid)
        # Keep stale IDs out of the active rotation but retain them in
        # CHANNEL_IDS so the dashboard can report an unavailable target.
        _active_ch_ref[:] = [cid for cid in _active_ch_ref if cid in catalogue and cid in CHANNEL_IDS]


def _reconcile_channel_registry(reason: str = "startup") -> dict:
    """Persist a full server scan and emit exact changes for operators."""
    if not _channel_registry or not ALT_ID:
        return {"ok": False, "error": "channel registry unavailable or ALT_ID is unset"}
    servers = _fetch_live_server_catalogue()
    if not servers:
        log(f"⚠️ [CHANNEL-STATE] {reason}: no live guild inventory returned; keeping existing targets.", kind="CHANNEL")
        return {"ok": False, "error": "no live guild inventory"}
    configured = [cid for cid in CHANNEL_IDS if str(cid).isdigit()]
    result = _channel_registry.reconcile(
        ALT_ID,
        servers,
        configured_ids=configured if configured else None,
        target_names=CHANNEL_NAMES or CHANNEL_KEYWORDS or None,
    )
    _apply_registry_targets(result)
    added = ", ".join(result.get("added", [])) or "none"
    removed = ", ".join(result.get("removed", [])) or "none"
    changed = ", ".join(result.get("changed", [])) or "none"
    replacements = ", ".join(
        f"{item.get('old_id')}→{item.get('new_id')}" for item in result.get("replaced", [])
    ) or "none"
    detail = (
        f"[CHANNEL-STATE] {reason}: servers={len(result.get('servers', {}))} "
        f"catalogue={len(result.get('catalogue', {}))} targets={len(result.get('targets', []))} "
        f"added=[{added}] removed=[{removed}] changed=[{changed}] replaced=[{replacements}]"
    )
    event_log("CHANNEL", detail)
    send_log_webhook(f"🔄 **CHANNEL REGISTRY {reason.upper()}**\n{detail}", kind="CHANNEL")
    _save_channel_registry_remote()
    return result


def _persist_runtime_targets() -> bool:
    """Persist the current target IDs/names after an accepted control update."""
    if not _channel_registry or not ALT_ID:
        return False
    names = {str(cid): str(name)[:80] for cid, name in _ch_names_ref.items() if str(cid).isdigit() and name}
    ok, _detail = _channel_registry.set_targets(ALT_ID, CHANNEL_IDS, names)
    if ok:
        _save_channel_registry_remote()
        log(f"[CHANNEL-STATE] persisted {len(CHANNEL_IDS)} runtime target(s)", kind="CHANNEL")
    else:
        log("⚠️ [CHANNEL-STATE] failed to persist runtime target update", kind="CHANNEL")
    return ok

def _load_channel_registry_remote() -> bool:
    """Hydrate the local registry from a per-alt file in the control Gist."""
    if not _channel_registry or not CHANNEL_STATE_GIST_ID or not GIST_TOKEN or not CHANNEL_STATE_GIST_FILE:
        return False
    try:
        response = creq.get(
            f"https://api.github.com/gists/{CHANNEL_STATE_GIST_ID}",
            headers={"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github+json", "User-Agent": "adfarm-sender"},
            impersonate=_BROWSER,
            timeout=WEBHOOK_TIMEOUT,
        )
        if response.status_code != 200:
            log(f"⚠️ [CHANNEL-STATE] remote load failed HTTP {response.status_code}", kind="CHANNEL")
            return False
        files = (response.json() or {}).get("files") or {}
        raw_file = files.get(CHANNEL_STATE_GIST_FILE) or {}
        content = raw_file.get("content") if isinstance(raw_file, dict) else ""
        if not content and isinstance(raw_file, dict) and raw_file.get("raw_url"):
            content_response = creq.get(raw_file["raw_url"], headers={"User-Agent": "adfarm-sender"}, impersonate=_BROWSER, timeout=WEBHOOK_TIMEOUT)
            if content_response.status_code == 200:
                content = content_response.text
        snapshot = json.loads(content) if content else None
        if not isinstance(snapshot, dict):
            return False
        ok = _channel_registry.restore_alt_snapshot(ALT_ID, snapshot)
        if ok:
            log(f"[CHANNEL-STATE] hydrated durable registry from {CHANNEL_STATE_GIST_FILE}", kind="CHANNEL")
        return ok
    except Exception as exc:
        log(f"⚠️ [CHANNEL-STATE] remote load error: {type(exc).__name__}: {exc}", kind="CHANNEL")
        return False


def _save_channel_registry_remote() -> bool:
    """Write only this alt's registry file, preserving control commands/other alts."""
    if not _channel_registry or not CHANNEL_STATE_GIST_ID or not GIST_TOKEN or not CHANNEL_STATE_GIST_FILE:
        return False
    try:
        snapshot = _channel_registry.snapshot_for_alt(ALT_ID)
        response = creq.patch(
            f"https://api.github.com/gists/{CHANNEL_STATE_GIST_ID}",
            headers={"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github+json", "User-Agent": "adfarm-sender"},
            json={"files": {CHANNEL_STATE_GIST_FILE: {"content": json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))}}},
            impersonate=_BROWSER,
            timeout=WEBHOOK_TIMEOUT,
        )
        if response.status_code not in (200, 201):
            log(f"⚠️ [CHANNEL-STATE] remote save failed HTTP {response.status_code}", kind="CHANNEL")
            return False
        return True
    except Exception as exc:
        log(f"⚠️ [CHANNEL-STATE] remote save error: {type(exc).__name__}: {exc}", kind="CHANNEL")
        return False


def _lookup_guild_for(cid):
    """Best-effort: find which guild a (possibly dead) channel belongs to.
    Returns a single guild_id string if we have high confidence (from a
    cached sibling channel), OR a list of candidate guild IDs to search when
    we're in fallback mode (e.g. startup with both channels already dead).
    `discover_channel_by_name` accepts either form."""
    # 1) direct cache for this channel
    gid = _guild_id_cache.get(cid)
    if gid:
        return gid
    gid = _channel_id_to_guild.get(cid)
    if gid:
        return gid
    # 2) any channel from the same guild (we're usually in one guild — if at
    # least one sibling channel was loaded successfully its guild_id is here).
    # De-dupe while preserving order.
    seen = set()
    for src in (_guild_id_cache, _channel_id_to_guild):
        for _g in src.values():
            if _g and _g not in seen:
                seen.add(_g)
    if len(seen) == 1:
        return next(iter(seen))
    if len(seen) > 1:
        return list(seen)
    # 3) last resort: fetch my guild list once (rare edge case where the
    # very first channel we probe at startup returns 404 and no siblings
    # have been loaded yet).
    fb = _fetch_my_guilds_fallback()
    if len(fb) == 1:
        return fb[0]
    return fb if fb else None

def _rename_channel_entry(old_cid, new_cid, new_name, ch_names, slowmodes,
                          last_sent, my_last_msg_id, stats, per_ch_state=None):
    """Rewrite all in-memory structures so new_cid takes over old_cid's slot."""
    # Update caches so verification/reactions/etc. use the new ID
    if old_cid in _guild_id_cache:
        _guild_id_cache[new_cid] = _guild_id_cache[old_cid]
    if old_cid in _channel_id_to_guild:
        _channel_id_to_guild[new_cid] = _channel_id_to_guild[old_cid]
    # Update CHANNEL_NAMES mapping if present
    if old_cid in _CHANNEL_NAME_BY_ID:
        _CHANNEL_NAME_BY_ID[new_cid] = _CHANNEL_NAME_BY_ID[old_cid]
    ch_names[new_cid] = new_name
    if old_cid in ch_names:
        ch_names.pop(old_cid, None)
    # Carry over stats so per-channel counters don't reset to 0
    if old_cid in stats:
        stats[new_cid] = stats.pop(old_cid)
    # Carry over slowmode (we'll refresh it after confirmation)
    # Carry over last-sent timestamps so we don't immediately repost
    if old_cid in last_sent:
        last_sent[new_cid] = last_sent.pop(old_cid)
    if old_cid in my_last_msg_id:
        # drop the old mid (it doesn't exist in new channel); don't carry over
        my_last_msg_id.pop(old_cid, None)

def discover_channel_by_name(guild_id, channel_name):
    """GET /guilds/{gid}/channels, return first text channel whose name matches
    (case-insensitive) or None. `guild_id` may be a single id or list of ids
    (the fallback case when no sibling channels have been loaded yet)."""
    # Normalize to a list of candidate guild ids
    if guild_id is None:
        candidates_gids = []
    elif isinstance(guild_id, (list, tuple, set)):
        candidates_gids = [g for g in guild_id if g]
    else:
        candidates_gids = [guild_id]
    if not candidates_gids:
        return None
    # Match Discord's channel name normalization (lowercase, spaces→-)
    target = channel_name.strip().lower().replace(" ", "-")
    for gid in candidates_gids:
        url = f"https://discord.com/api/v9/guilds/{gid}/channels"
        ref = f"https://discord.com/channels/{gid}"
        r = api("GET", url, referer=ref, retries=2)
        if r.status_code != 200:
            dbg(f"[DISC-FETCH] guild {gid} channels failed HTTP {r.status_code}")
            continue
        try:
            channels = r.json()
        except Exception:
            continue
        if not isinstance(channels, list):
            continue
        # First pass: exact match (after normalization) on type=0 (GUILD_TEXT)
        matches = []
        for ch in channels:
            if not isinstance(ch, dict):
                continue
            if ch.get("type") != 0:  # 0 = GUILD_TEXT
                continue
            nm = (ch.get("name") or "").strip().lower()
            if nm == target:
                matches.append(ch)
        if not matches:
            # Second pass: substring match (handles slight rename variants)
            for ch in channels:
                if not isinstance(ch, dict) or ch.get("type") != 0:
                    continue
                nm = (ch.get("name") or "").strip().lower()
                if target and (target in nm or nm in target):
                    matches.append(ch)
        if len(matches) > 1:
            log(f"   ⚠️ [DISC] {len(matches)} channels matched '{target}' in guild {gid}; using first "
                f"(#{matches[0].get('name')}).")
        if not matches:
            # Third pass: clean alphanumeric fuzzy match (handles emojis like 「💵」・market or trading﹒☆˚₊࿔)
            clean_target = re.sub(r"[^\w\s-]", "", target).strip().replace(" ", "-")
            if clean_target:
                for ch in channels:
                    if not isinstance(ch, dict) or ch.get("type") != 0:
                        continue
                    nm = (ch.get("name") or "").strip().lower()
                    clean_nm = re.sub(r"[^\w\s-]", "", nm).strip().replace(" ", "-")
                    if clean_target == clean_nm or clean_target in clean_nm or clean_nm in clean_target:
                        matches.append(ch)
        if not matches:
            continue
        new_cid = matches[0].get("id")
        new_name = matches[0].get("name", target)
        if new_cid:
            _guild_id_cache[new_cid] = gid
            _channel_id_to_guild[new_cid] = gid
        return {"id": new_cid, "name": new_name}
    return None

def _poll_reactions(cid, mid, emoji_url, trusted_only, timeout):
    """Poll for an approval/rejection from explicitly trusted users.

    Discovery is deliberately fail-closed: an empty trusted-user set never
    authorizes a channel replacement.
    """
    if not trusted_only:
        return None
    deadline = time.time() + timeout
    seen_confirm = set()
    seen_reject = set()
    ref = f"https://discord.com/channels/{_guild_id_cache.get(cid,'@me')}/{cid}"
    while time.time() < deadline:
        if _panic_event.is_set() or not public_activity_allowed() and _panic_event.is_set():
            return None
        # Check ✅
        try:
            r = api("GET",
                    f"https://discord.com/api/v9/channels/{cid}/messages/{mid}/reactions/%E2%9C%85?limit=100",
                    referer=ref, retries=1)
            if r.status_code == 200:
                data = r.json()
                users = data if isinstance(data, list) else []
                for u in users:
                    uid = u.get("id") if isinstance(u, dict) else None
                    if uid and uid not in seen_confirm:
                        seen_confirm.add(uid)
                        if not trusted_only or uid in trusted_only:
                            return "confirm"
        except Exception as _ignored_exc:
            print(f"[SENDER] _poll_reactions: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
        # Check ❌
        try:
            r = api("GET",
                    f"https://discord.com/api/v9/channels/{cid}/messages/{mid}/reactions/%E2%9D%8C?limit=100",
                    referer=ref, retries=1)
            if r.status_code == 200:
                data = r.json()
                users = data if isinstance(data, list) else []
                for u in users:
                    uid = u.get("id") if isinstance(u, dict) else None
                    if uid and uid not in seen_reject:
                        seen_reject.add(uid)
                        if not trusted_only or uid in trusted_only:
                            return "reject"
        except Exception as _ignored_exc:
            print(f"[SENDER] _poll_reactions: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
        # Sleep in short chunks so panic/stop can interrupt
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(3.0, remaining))
    return None

def confirm_channel(new_cid, channel_name, old_cid, timeout=60):
    """Send a confirmation prompt and require an explicitly trusted reaction."""
    if not CONFIRM_USER_IDS:
        log("   ⚠️ [DISC] CONFIRM_USER_IDS is empty — refusing channel discovery.")
        return "timeout"
    ref = f"https://discord.com/channels/{_guild_id_cache.get(new_cid,'@me')}/{new_cid}"
    _wait_marker = "⏳ Waiting for confirmation"
    confirm_text = (
        "🛡️ **CHANNEL CONFIRMATION REQUIRED**\n\n"
        f"I discovered a new channel named **#{channel_name}** (ID: `{new_cid}`).\n"
        f"The previous channel ID (`{old_cid}`) no longer exists (404).\n\n"
        "React with ✅ to CONFIRM this is the correct trading channel (resumes posts).\n"
        f"React with ❌ to REJECT (skip this channel).\n\n"
        f"{_wait_marker}... (timeout: {timeout}s)"
    )
    payload = {"content": confirm_text,
               "allowed_mentions": {"parse": []},
               "flags": 0}
    r = api("POST", f"https://discord.com/api/v9/channels/{new_cid}/messages",
            referer=ref, json_body=payload, retries=2)
    if r.status_code != 200:
        log(f"   ❌ [DISC] Could not send confirmation message (HTTP {r.status_code}). Aborting discovery.")
        return "rejected"
    try:
        mid = r.json().get("id")
    except Exception:
        mid = None
    if not mid:
        return "rejected"
    # Add both reactions
    for emo_enc in ("%E2%9C%85", "%E2%9D%8C"):
        try:
            api("PUT",
                f"https://discord.com/api/v9/channels/{new_cid}/messages/{mid}/reactions/{emo_enc}/@me",
                referer=ref, json_body={}, retries=1)
            time.sleep(0.4)
        except Exception as _ignored_exc:
            print(f"[SENDER] confirm_channel: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
    # Poll for reaction
    outcome = _poll_reactions(new_cid, mid, "%E2%9C%85", CONFIRM_USER_IDS, timeout)
    # Best-effort: edit the confirmation message to show outcome
    try:
        prefix = confirm_text.split(_wait_marker)[0].rstrip()
        if outcome == "confirm":
            final_text = (prefix
                          + f"\n\n✅ **CONFIRMED** — resuming posts to #{channel_name}.")
        elif outcome == "reject":
            final_text = prefix + "\n\n❌ **REJECTED** — channel skipped."
        else:
            final_text = prefix + f"\n\n⏰ **TIMEOUT** ({timeout}s) — channel skipped."
        api("PATCH", f"https://discord.com/api/v9/channels/{new_cid}/messages/{mid}",
            referer=ref, json_body={"content": final_text}, retries=1)
    except Exception as _ignored_exc:
        print(f"[SENDER] confirm_channel: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
    return {"confirm": "confirmed", "reject": "rejected"}.get(outcome, "timeout")

def try_channel_discovery(old_cid, context):
    """High-level: called when a 404 is encountered for `old_cid`.
    `context` is a dict with keys the caller needs updated on success:
       ch_names, slowmodes, last_sent, my_last_msg_id, stats,
       active_channels (list), dead_channels (set), next_post_time (dict).
    Returns new_cid on confirmed discovery, None otherwise.
    """
    global CHANNEL_IDS
    ch_name = _CHANNEL_NAME_BY_ID.get(old_cid) or _ch_names_ref.get(old_cid) or ""
    if not ch_name and context and isinstance(context.get("ch_names"), dict):
        ch_name = context["ch_names"].get(old_cid, "")
    if not ch_name and CHANNEL_NAMES:
        ch_name = CHANNEL_NAMES[0]
    if not ch_name:
        log(f"   ℹ️  [DISC] No channel name found for {old_cid} — searching guild context.")
    effective_confirm_users = CONFIRM_USER_IDS or CONTROLLER_USER_IDS
    with _discovery_lock:
        if old_cid in _discovery_attempted:
            log(f"   ℹ️  [DISC] Already attempted discovery for {old_cid} this run — not retrying.")
            return None
        _discovery_attempted.add(old_cid)
    log(f"")
    log(f"🔍 [DISC] Channel ID `{old_cid}` returned 404 — searching guild for new '#{ch_name}'...")
    send_log_webhook(f"🔍 **CHANNEL DISCOVERY** `{old_cid}` (#{ch_name}) returned 404 — searching...")
    gid = _lookup_guild_for(old_cid)
    if not gid:
        log(f"   ❌ [DISC] Could not determine guild for {old_cid} — cannot search. Skipping.")
        send_log_webhook(f"❌ **DISCOVERY FAIL** `{old_cid}` (#{ch_name}): no guild context.")
        return None
    log(f"   📡 [DISC] Fetching channels for guild {gid}...")
    found = discover_channel_by_name(gid, ch_name) if ch_name else None
    if not found and CHANNEL_KEYWORDS:
        for kw in CHANNEL_KEYWORDS:
            found = discover_channel_by_name(gid, kw)
            if found:
                break
    if not found:
        log(f"   ❌ [DISC] No matching text channel found in guild {gid} — skipping.")
        send_log_webhook(f"❌ **DISCOVERY FAIL** `{old_cid}` (#{ch_name}): channel name not found in guild.")
        return None
    new_cid = found["id"]
    new_name = found["name"]
    log(f"   🔍 [DISC] Found candidate: #{new_name} (ID: `{new_cid}`).")

    require_reaction = _bool("REQUIRE_DISCOVERY_REACTION", False)
    if require_reaction and effective_confirm_users:
        outcome = confirm_channel(new_cid, new_name, old_cid, timeout=CONFIRM_TIMEOUT)
    else:
        outcome = "confirmed"

    if outcome == "confirmed":
        log(f"   ✅ [DISC] Channel '#{new_name}' CONFIRMED — new ID: {new_cid}. Resuming posts to this channel.")
        send_log_webhook(f"✅ **CHANNEL AUTO-RECOVERED** `{old_cid}` → `{new_cid}` (#{new_name})")
        try:
            send_dashboard({
                "title": "✅ CHANNEL REPLACED",
                "color": 0x57F287,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "description": f"#{new_name} (`{new_cid}`) replaces `{old_cid}`. Posting resumed.",
            })
        except Exception as _ignored_exc:
            print(f"[SENDER] try_channel_discovery: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
        # Fetch fresh channel info (slowmode)
        try:
            info = get_channel_info(new_cid)
            if info:
                context["slowmodes"][new_cid] = info.get("rate_limit_per_user", 0)
                context["ch_names"][new_cid] = info.get("name", new_name)
        except Exception:
            context["ch_names"][new_cid] = new_name
            context["slowmodes"][new_cid] = 0
        # Rewrite live scheduler structures
        _rename_channel_entry(
            old_cid, new_cid, new_name,
            context["ch_names"], context["slowmodes"],
            context["last_sent"], context["my_last_msg_id"],
            context["stats"],
        )
        # Remove old ID from dead set if present, add new ID to active list
        context["dead_channels"].discard(old_cid)
        context["dead_channels"].discard(new_cid)
        if new_cid not in context["active_channels"]:
            context["active_channels"].append(new_cid)
        if old_cid in context["active_channels"]:
            context["active_channels"].remove(old_cid)
        context["active_channels"] = list(dict.fromkeys(context["active_channels"]))
        # Schedule next post a short human-style delay from now (not immediate)
        context["next_post_time"][new_cid] = time.time() + random.uniform(15, 40)
        if old_cid in context["next_post_time"]:
            del context["next_post_time"][old_cid]
        # Remember replacement so other code paths that still reference old_cid
        # can resolve
        with _discovery_lock:
            _discovery_replacements[old_cid] = new_cid
        # Replace the entry in CHANNEL_IDS in-place and deduplicate
        try:
            with _state_lock:
                while old_cid in CHANNEL_IDS:
                    idx = CHANNEL_IDS.index(old_cid)
                    CHANNEL_IDS[idx] = new_cid
                CHANNEL_IDS = list(dict.fromkeys(CHANNEL_IDS))
        except Exception as _ignored_exc:
            print(f"[SENDER] try_channel_discovery: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
        return new_cid
    elif outcome == "rejected":
        log(f"   ⏭️  [DISC] Channel '#{ch_name}' REJECTED — skipping.")
        send_log_webhook(f"⏭️ **CHANNEL REJECTED** `{old_cid}` (#{ch_name}) → staying skipped.")
    else:
        log(f"   ⏰ [DISC] Confirmation TIMEOUT ({CONFIRM_TIMEOUT}s) for '#{ch_name}' — skipping.")
        send_log_webhook(f"⏰ **DISCOVERY TIMEOUT** `{old_cid}` (#{ch_name}) → staying skipped.")
    return None

# --------------------------------------------------------------------------- #
# Post-send verification (auto-learn)                                         #
# --------------------------------------------------------------------------- #
def _verify_message_alive(cid, mid, text, delay=35):
    """Verify one sent message by ID; only HTTP 404 confirms deletion."""
    def _run():
        time.sleep(delay + random.uniform(-3, 8))
        outcome = None
        try:
            ref = f"https://discord.com/channels/{_guild_id_cache.get(cid,'@me')}/{cid}"
            r = SESSION.get(f"https://discord.com/api/v9/channels/{cid}/messages/{mid}",
                            referer=ref, timeout=10)
            if r.status_code == 200:
                outcome = True
                dbg(f"[VERIFY] post survived (cid={cid}, mid={mid})")
                _record_success(text, cid, mid)
            elif r.status_code == 404:
                outcome = False
                log(f"⚠️ [VERIFY] confirmed deletion (cid={cid}, mid={mid})", kind="CAUTION")
                _record_strike(text, cid, mid)
            elif r.status_code == 403:
                log(f"⚠️ [VERIFY] permission response for cid={cid}; not treating as deletion", kind="VERIFY")
            else:
                dbg(f"[VERIFY] inconclusive HTTP {r.status_code}; no caution strike")
        except Exception as e:
            dbg(f"[VERIFY] transient verification error: {type(e).__name__}: {e}")
        _record_verification(cid, mid, outcome)
    threading.Thread(target=_run, daemon=True, name=f"verify-{cid}").start()

# --------------------------------------------------------------------------- #
# Image processing                                                            #
# --------------------------------------------------------------------------- #
_IMG_EXTS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".gif": "image/gif", ".webp": "image/webp"}

def _random_img_name(original_name):
    ext = Path(original_name).suffix.lower() or ".png"
    bases = ["image", "img", "pic", "photo", "ss", "Screenshot", "trade",
             "ad", "IMG", "Image", "screenshot", "Capture", "shot"]
    return f"{random.choice(bases)}_{random.randint(1000,99999)}{ext}"

def _process_image(raw_bytes, original_name):
    ext = Path(original_name).suffix.lower() or ".png"
    mime = _IMG_EXTS.get(ext, "image/png")
    fname = _random_img_name(original_name)
    if not _HAS_PIL or not STRIP_EXIF:
        return fname, raw_bytes, mime
    try:
        with Image.open(io.BytesIO(raw_bytes)) as im:
            out = io.BytesIO()
            if IMAGE_JITTER and ext != ".gif":
                try:
                    im = im.convert("RGB") if ext in (".jpg", ".jpeg") else im
                    w, h = im.size
                    n_jitter = min(30, (w * h) // 5000)
                    px = im.load()
                    for _ in range(n_jitter):
                        x = random.randint(0, w - 1)
                        y = random.randint(0, h - 1)
                        try:
                            p = px[x, y]
                            if isinstance(p, tuple):
                                jittered = tuple(max(0, min(255, c + random.randint(-1, 1))) for c in p[:3]) + p[3:]
                                px[x, y] = jittered
                        except Exception as _ignored_exc:
                            print(f"[SENDER] _process_image: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
                except Exception as _ignored_exc:
                    print(f"[SENDER] _process_image: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
            if ext in (".jpg", ".jpeg"):
                if im.mode in ("RGBA", "P", "LA"):
                    im = im.convert("RGB")
                q = random.randint(90, 96)
                im.save(out, format="JPEG", quality=q, optimize=True,
                        subsampling="4:2:0" if q < 95 else 0)
                fname = fname.rsplit(".", 1)[0] + ".jpg"
                mime = "image/jpeg"
            elif ext == ".webp":
                im.save(out, format="WEBP", quality=random.randint(90, 96))
            elif ext == ".gif":
                return fname, raw_bytes, mime
            else:
                info = PngImagePlugin.PngInfo()
                im.save(out, format="PNG", pnginfo=info, optimize=True)
            out.seek(0)
            data = out.read()
            if len(data) > len(raw_bytes) * 1.08:
                return fname, raw_bytes, mime
            return fname, data, mime
    except Exception as e:
        dbg(f"image processing failed: {e}; using raw")
        return fname, raw_bytes, mime

# --------------------------------------------------------------------------- #
# Discord API wrappers                                                        #
# --------------------------------------------------------------------------- #
_guild_id_cache = {}

def validate_token():
    log("🔐 Authenticating with Discord (GET /users/@me)...")
    r = api("GET", "https://discord.com/api/v9/users/@me", retries=2)
    if r.status_code == 200:
        try:
            me = r.json()
            # cache my identity for webhook spoofing
            _me_cache["id"] = me.get("id")
            _me_cache["username"] = me.get("username")
            _me_cache["global_name"] = me.get("global_name")
            _me_cache["avatar"] = me.get("avatar")
            _me_cache["discriminator"] = me.get("discriminator")
            return me, None
        except Exception:
            return None, "unknown"
    try:
        msg = r.json().get("message", "")[:200]
    except Exception:
        msg = (getattr(r, "text", "") or "")[:200]
    if r.status_code in (401, 403):
        reason = "invalid"
    elif r.status_code == 0:
        reason = "network"
    elif 500 <= r.status_code < 600:
        reason = "server"
    else:
        reason = "unknown"
    log(f"❌ AUTH FAILED — status {r.status_code} ({reason}): {msg}")
    if reason == "invalid":
        log("   → Token is invalid/revoked/banned. Recopy your token or use a new alt.")
    elif reason == "network":
        log("   → Network error during auth. Check proxy/WARP connection.")
    return None, reason

def set_status():
    if not CUSTOM_STATUS_TEXT:
        log("ℹ️  No custom status configured; skipping presence update.")
        return False
    payload = {"custom_status": {"text": CUSTOM_STATUS_TEXT}}
    if STATUS_EMOJI:
        payload["custom_status"]["emoji_name"] = STATUS_EMOJI
    try:
        r = api("PATCH", "https://discord.com/api/v9/users/@me/settings",
                json_body=payload, retries=2)
        if r.status_code == 200:
            log(f"🟢 Custom status set → '{STATUS_EMOJI} {CUSTOM_STATUS_TEXT}'")
            return True
        log(f"⚠️ Failed to set custom status (HTTP {r.status_code}) — non-critical, continuing.")
    except Exception as e:
        log(f"⚠️ Status error: {type(e).__name__}: {e}")
    return False

def keepalive():
    try:
        api("GET", "https://discord.com/api/v9/users/@me", retries=1)
        dbg("[KEEPALIVE] 💓 REST keepalive ping sent (maintains NAT mapping + keeps auth fresh)")
    except Exception:
        dbg("[KEEPALIVE] ⚠️ REST keepalive failed (non-critical)")

def get_channel_info(cid):
    ref = f"https://discord.com/channels/@me/{cid}"
    r = api("GET", f"https://discord.com/api/v9/channels/{cid}", referer=ref, retries=2)
    try:
        if r.status_code == 200:
            j = r.json()
            # A runtime target must be a guild text/announcement channel. Do
            # not accept a DM, category, thread, or malformed response as a
            # scheduler channel; this also preserves fail-closed behavior when
            # no guild context is available.
            if not isinstance(j, dict) or j.get("type") not in (0, 5) or not j.get("guild_id"):
                return None
            gid = str(j["guild_id"])
            _guild_id_cache[cid] = gid
            _channel_id_to_guild[cid] = gid
            return j
    except Exception as _ignored_exc:
        print(f"[SENDER] get_channel_info: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
    return None

def ack_channel(cid, last_msg_id):
    if not last_msg_id:
        return
    ref = f"https://discord.com/channels/{_guild_id_cache.get(cid,'@me')}/{cid}"
    try:
        api("POST",
            f"https://discord.com/api/v9/channels/{cid}/messages/{last_msg_id}/ack",
            referer=ref, json_body={"token": None}, retries=1)
        dbg(f"✔️ ack #{cid} @ {last_msg_id}")
    except Exception as _ignored_exc:
        print(f"[SENDER] ack_channel: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)

def get_last_messages(cid, limit=5, force_refresh=False):
    url = f"https://discord.com/api/v9/channels/{cid}/messages?limit={limit}"
    if force_refresh:
        url += f"&_={int(time.time()*1000)}"
    ref = f"https://discord.com/channels/{_guild_id_cache.get(cid,'@me')}/{cid}"
    r = api("GET", url, referer=ref, retries=2)
    try:
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                return data
    except Exception as _ignored_exc:
        print(f"[SENDER] get_last_messages: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
    return None

def am_i_last(cid, my_id):
    """Return (i_am_last, last_author, last_snip, recent_msgs).

    NOTE: In high-traffic channels (30-50 msgs/min) we are almost NEVER the
    last message by the time we check. We only use this for the *optional*
    smart-cooldown skip (in low-traffic channels). It is NOT used to decide
    WHEN to post — that's the per-channel scheduler's job.

    We fetch 20 recent messages (not 5) to reduce false "DELETED" alarms in
    busy channels where our ad is simply buried quickly.
    """
    msgs = get_last_messages(cid, 20)
    if msgs is None or len(msgs) == 0:
        return True, "?", "?", None
    last = msgs[0]
    author = last.get("author", {}) or {}
    last_author = author.get("username") or author.get("global_name") or "?"
    last_author_id = author.get("id")
    snip = (last.get("content") or "").replace("\n", " ")[:40] or "<embed/image/empty>"
    return (last_author_id == my_id), last_author, snip, msgs

my_last_msg_id = {}

# --------------------------------------------------------------------------- #
# Chat Velocity & Traffic-Density Adaptive Cadence                            #
# --------------------------------------------------------------------------- #
_channel_velocity = {}  # cid -> (velocity_msgs_per_min: float, mult: float)

def _calculate_chat_velocity(cid, msgs):
    """Compute messages/minute and interval scaling multiplier over recent channel traffic."""
    if not msgs or len(msgs) < 2:
        res = (5.0, 1.0)
        _channel_velocity[cid] = res
        return res
    try:
        ts_list = []
        for m in msgs:
            raw_ts = m.get("timestamp")
            if raw_ts:
                cleaned = raw_ts.replace("Z", "+00:00")
                dt = datetime.fromisoformat(cleaned)
                ts_list.append(dt.timestamp())
        if len(ts_list) >= 2:
            ts_list.sort()
            span_min = max(0.2, (ts_list[-1] - ts_list[0]) / 60.0)
            velocity = len(ts_list) / span_min
            if velocity > 15.0:
                mult = max(0.65, 1.0 - min(0.35, (velocity - 15.0) * 0.02))
            elif velocity < 3.0:
                mult = min(1.80, 1.0 + (3.0 - velocity) * 0.25)
            else:
                mult = 1.0
            res = (velocity, mult)
            _channel_velocity[cid] = res
            return res
    except Exception as _ignored_exc:
        print(f"[SENDER] _calculate_chat_velocity: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
    res = (5.0, 1.0)
    _channel_velocity[cid] = res
    return res

# --------------------------------------------------------------------------- #
# Multi-Alt Channel Staggering & Collision Avoidance                          #
# --------------------------------------------------------------------------- #
_fleet_channel_posts = {}  # cid -> float (epoch of latest post by any fleet alt)

def _record_fleet_post(cid):
    with _state_lock:
        _fleet_channel_posts[cid] = time.time()

def _check_fleet_collision(cid, min_separation=90.0):
    """Check if another alt in the fleet posted to this channel recently.
    Returns (has_collision, yield_delay_seconds)."""
    with _state_lock:
        last_post = _fleet_channel_posts.get(cid, 0.0)
        if last_post <= 0:
            return False, 0.0
        now = time.time()
        elapsed = now - last_post
        if elapsed < min_separation:
            yield_delay = (min_separation - elapsed) + random.uniform(10.0, 25.0)
            return True, yield_delay
    return False, 0.0

# --------------------------------------------------------------------------- #
# Buyer DM Intent Classifier (Smart Multi-Factor & Game Recognition)          #
# --------------------------------------------------------------------------- #
def _classify_dm_intent(text):
    """Classify buyer DM intent, extract volume, detect payment methods, and identify game/item."""
    raw = str(text or "").lower()

    # 1. Volume & budget extraction. Keep the raw text intact so arbitrary
    # item names and questions remain available to the operator.
    cleaned = raw
    vol_match = re.search(
        r"(?<![A-Za-z0-9])(?:(\$\s*\d+(?:\.\d+)?)|(\d+(?:\.\d+)?\s*(?:\$|usd|eur|€|gbp|£|k|m|mil|million|tokens?|b\b|billion|rap|robux|r\$)?))\b",
        cleaned,
        re.I,
    )
    volume = None
    if vol_match:
        volume = (vol_match.group(1) or vol_match.group(2) or "").strip()
        if volume in ("$", "usd", "eur", "k", "m"):
            volume = None

    # 2. Asset recognition: built-in game aliases first (so "bb", "mm2",
    # "ps99" resolve to human-readable labels), then configuration-driven
    # deal keywords as a fallback for arbitrary items.
    game = None
    for alias in sorted(_GAME_ALIASES, key=len, reverse=True):
        if re.search(r"(?<![A-Za-z0-9])" + re.escape(alias) + r"(?![A-Za-z0-9])", raw, re.I):
            game = _GAME_ALIASES[alias]
            break
    if game is None:
        aliases = list(dict.fromkeys(DEFAULT_ITEM_KEYWORDS + _get_active_deal_keywords()))
        for alias in sorted((a for a in aliases if a), key=len, reverse=True):
            if re.search(r"(?<![A-Za-z0-9])" + re.escape(alias) + r"(?![A-Za-z0-9])", raw, re.I):
                game = f"🏷️ {alias}"
                break

    # 3. Payment Method Extraction (Word-bounded)
    payments = []
    if re.search(r"\b(paypal|pp|f&f|fnf|friends\s*&\s*family|friends\s*and\s*family|g&s)\b", raw):
        payments.append("💳 PayPal")
    if re.search(r"\b(crypto|usdt|btc|bitcoin|eth|ethereum|sol|solana|ltc|litecoin|binance|coinbase|trx|ton|usdc|wallet)\b", raw):
        payments.append("🪙 Crypto")
    if re.search(r"\b(cashapp|cash\s*app|\$cashtag)\b", raw):
        payments.append("💵 CashApp")
    if re.search(r"\b(venmo|zelle|revolut|wise|apple\s*pay|applepay|google\s*pay|gpay|skrill|bank\s*transfer|stripe)\b", raw):
        payments.append("🏦 Bank/Card")
    if re.search(r"\b(robux|r\$|tax\s*covered|w/t|wt|giftcard|gift\s*card|nitro|amazon\s*gc|steam\s*gc)\b", raw):
        payments.append("🎁 Trade/Giftcard")

    # 4. Intent Classification
    if re.search(r"\b(buy|wtb|cop|copping|grab|need|want|take|buying|bulk|how\s*much\s*for|ready|ready\s*to\s*buy|can\s*i\s*get|can\s*you\s*do|ill\s*take|i\s*take)\b", raw):
        category = "🛒 Purchase Intent"
        priority = "🔥 High Intent"
    elif re.search(r"\b(stock|in\s*stock|left|avail|available|inventory|how\s*many\s*you\s*got|how\s*much\s*you\s*got)\b", raw):
        category = "📦 Stock Check"
        priority = "🟡 Medium Intent"
    elif re.search(r"\b(price|rate|ratio|cost|how\s*much\s*is|how\s*much|\$|quote|per\s*1k|per\s*1m|/1k|/1m)\b", raw):
        category = "🔄 Price Check"
        priority = "🟡 Medium Intent"
    elif re.search(r"\b(vouch|proof|proofs|vouches|legit|trust|scam|middleman|mm|safe)\b", raw):
        category = "🛡️ Vouch Request"
        priority = "🟡 Medium Intent"
    elif re.search(r"\b(trade|trading|swap|swapping|exchange|wtt|cross\s*trade|item\s*for\s*item)\b", raw):
        category = "🔁 Trade Offer"
        priority = "🟡 Medium Intent"
    else:
        category = "💬 General Inquiry"
        priority = "⚪ Casual"

    return {
        "category": category,
        "priority": priority,
        "volume": volume,
        "payments": payments,
        "game": game,
    }

def read_channel(cid, limit=15):
    msgs = get_last_messages(cid, limit)
    if msgs is None:
        msgs = []
    dbg(f"👁️ read #{cid} ({len(msgs)} msgs)")
    if msgs:
        try:
            ack_channel(cid, msgs[0].get("id"))
        except Exception as _ignored_exc:
            print(f"[SENDER] read_channel: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
        try:
            _calculate_chat_velocity(cid, msgs)
        except Exception as _ignored_exc:
            print(f"[SENDER] read_channel: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
    # F-13: passive deal scan (ZERO extra API calls — msgs already fetched).
    if DEAL_SCAN_ENABLED and cid in CHANNEL_IDS:
        try:
            scan_deals(cid, msgs)
        except Exception as e:
            dbg(f"[DEAL] scan error: {e}")
    return msgs

# --------------------------------------------------------------------------- #
# Deep Multi-Stage Market Parser & Deal Scanner (F-13 Enhanced V6)          #
# --------------------------------------------------------------------------- #
_PAYMENT_PATTERNS = {
    "PayPal": re.compile(r"\b(?:paypal|pp|fnf|f&f)\b", re.I),
    "CashApp": re.compile(r"\b(?:cashapp|ca)\b", re.I),
    "Crypto": re.compile(r"\b(?:crypto|btc|eth|ltc|sol|usdt|usdc|binance)\b", re.I),
    "Venmo": re.compile(r"\b(?:venmo)\b", re.I),
    "Apple Pay": re.compile(r"\b(?:apple\s*pay|ap)\b", re.I),
    "Robux": re.compile(r"\b(?:robux|wt|w/o\s*tax|after\s*tax)\b", re.I),
    "Wise": re.compile(r"\b(?:wise|revolut)\b", re.I),
    "Zelle": re.compile(r"\b(?:zelle)\b", re.I),
}

_DEAL_SELL_KW = re.compile(r"\b(?:sell(?:ing)?|wts|stock|cheap|shop|offering|have|fs|for\s*sale|supplying|providing)\b", re.I)
_DEAL_BUY_KW  = re.compile(r"\b(?:buy(?:ing)?|wtb|lf(?:\s+(?:items?|stock|goods))?|need|looking\s+for|want|paying|buying\s+all|iso)\b", re.I)
_NEGATION_KW  = re.compile(r"\b(?:not\s*(?:selling|buying|trading|have|in\s*stock)|don\x27?t\s*(?:have|buy|sell|dm)|no\s*(?:stock|goods)|out\s*of\s*stock|sold\s*out|0\s*stock|scam(?:mer|med)?|vouch|\+rep|rep\b|proof)\b", re.I)

_DIRECT_RATE_PATTERNS = [
    # $2.20/1k, 2.20$/1k, 2.20/1k, 2.20 per 1k, 2.20/k, 2.20 / 1000
    re.compile(r"(?:\$|usd|eur|€|£)?\s*(\d+(?:\.\d{1,2})?)\s*(?:\$|usd|eur|€|£)?\s*(?:\/|p\/|per|for)\s*(?:1\s*)?(?:k|thousand|1000)\b", re.I),
    # rate: 2.20, ratio 1:2.20, ratio 2.20:1, rate 2.20
    re.compile(r"\b(?:rate|ratio|ratio:)\s*[:=]?\s*(?:1\s*:\s*)?(?:\$|usd|eur|€|£)?\s*(\d+(?:\.\d{1,2})?)\b", re.I),
    # 2.20 ea, 2.20 each, $2.20 each
    re.compile(r"(?:\$|usd|eur|€|£)?\s*(\d+(?:\.\d{1,2})?)\s*(?:\$|usd|eur|€|£)?\s*(?:ea|each|per\s*token)\b", re.I),
    # @ $2.20, @ 2.20/1k
    re.compile(r"(?:@|\bat)\s*(?:\$|usd|eur|€|£)?\s*(\d+(?:\.\d{1,2})?)\s*(?:\$|usd)?(?:\s*(?:\/|per)\s*(?:1\s*)?k)?\b", re.I),
    # 2.20$ or $2.20 (standalone with currency symbol)
    re.compile(r"(?:\$|usd|eur|€|£)\s*(\d+(?:\.\d{1,2})?)(?![a-zA-Z0-9])", re.I),
    re.compile(r"\b(\d+(?:\.\d{1,2})?)\s*(?:\$|usd|eur|€|£)(?![a-zA-Z0-9])", re.I),
]

_VOLUME_PATTERNS = [
    # Explicit stock/qty indicators: stock 50k, qty 100k, 50k stock, 100k tokens
    re.compile(r"\b(?:stock|qty|amount|vol|volume)\s*[:=]?\s*(\d+(?:\.\d+)?)\s*k\b", re.I),
    re.compile(r"\b(\d+(?:\.\d+)?)\s*k\s*(?:stock|tokens|left|in\s*stock|total)\b", re.I),
    # Numbers with comma separator: 50,000, 100,000
    re.compile(r"(?<!/)(?<!per\s)\b(\d{1,3}(?:,\d{3})+)\b"),
    # General k/thousand/m volume NOT part of rate denominator (/1k, per 1k)
    re.compile(r"(?<!/)(?<!per\s)(?<!p/)(?<!for\s)\b(\d+(?:\.\d+)?)\s*k\b", re.I),
    re.compile(r"(?<!/)(?<!per\s)(?<!p/)\b(\d+(?:\.\d+)?)\s*(?:thousand|mil|million|m)\b", re.I),
]
_DEAL_THROTTLE_SEC = 600  # don't re-alert same seller+price for 10min

def _match_deal_item(text):
    """Return the configured item alias found as a whole phrase, if any."""
    source = str(text or "")
    sorted_keywords = sorted(_get_active_deal_keywords(), key=len, reverse=True)
    for keyword in sorted_keywords:
        pattern = r"(?<![A-Za-z0-9])" + re.escape(keyword) + r"(?![A-Za-z0-9])"
        if re.search(pattern, source, re.I):
            return keyword
    return None

def parse_market_listing(text, target_keywords=None):
    """Deep multi-stage market listing parser.
    
    Extracts matched item keyword, trading direction (seller vs buyer),
    unit-normalized $/1k price, volume/stock, payment methods, and specific segment snippet.
    Accurately isolates the target item even from long, complex multi-item bulleted lists.
    """
    if not text or len(text) < 5:
        return None
    if target_keywords is None:
        target_keywords = _get_active_deal_keywords()
    
    sorted_keywords = sorted(target_keywords, key=len, reverse=True)

    lines = [l.strip() for l in re.split(r"[\r\n]+", text) if l.strip()]
    if not lines:
        return None

    current_section_direction = None

    for line in lines:
        upper_line = line.upper()
        if any(h in upper_line for h in ["WTS", "SELLING", "HAVE", "STOCK"]) and not any(h in upper_line for h in ["WTB", "BUYING", "LF"]):
            if ":" in line or len(line.split()) <= 3:
                current_section_direction = "seller"
        elif any(h in upper_line for h in ["WTB", "BUYING", "LF", "LOOKING FOR", "NEED"]) and not any(h in upper_line for h in ["WTS", "SELLING"]):
            if ":" in line or len(line.split()) <= 3:
                current_section_direction = "buyer"

        cleaned_line = re.sub(r"^[\s\u2022\u25aa\u25cf\u25cb\u2219\*\-\>\~]+", "", line).strip()
        if not cleaned_line:
            continue
        parts = [s.strip() for s in re.split(r"[\u2022\u25aa\u25cf\u25cb\u2219\|;\t]+", cleaned_line) if s.strip()]
        segments = []
        for part in parts:
            if "," in part:
                subparts = [sp.strip() for sp in re.split(r",\s*(?=[a-zA-Z\[\*\•\-])", part) if sp.strip()]
                segments.extend(subparts)
            else:
                segments.append(part)

        if not segments:
            segments = [cleaned_line]

        for seg in segments:
            matched_item = None
            for kw in sorted_keywords:
                pat = r"(?<![A-Za-z0-9])" + re.escape(kw) + r"(?![A-Za-z0-9])"
                if re.search(pat, seg, re.I):
                    matched_item = kw
                    break

            if not matched_item:
                continue

            if _NEGATION_KW.search(seg):
                continue

            has_sell = bool(_DEAL_SELL_KW.search(seg))
            has_buy  = bool(_DEAL_BUY_KW.search(seg))

            direction = None
            if has_sell and not has_buy:
                direction = "seller"
            elif has_buy and not has_sell:
                direction = "buyer"
            elif current_section_direction:
                direction = current_section_direction
            elif _DEAL_SELL_KW.search(text) and not _DEAL_BUY_KW.search(text):
                direction = "seller"
            elif _DEAL_BUY_KW.search(text) and not _DEAL_SELL_KW.search(text):
                direction = "buyer"

            if not direction:
                continue

            payments = [name for name, ppat in _PAYMENT_PATTERNS.items() if ppat.search(seg) or ppat.search(text)]

            volume_str = None
            vol_val = None
            for vpat in _VOLUME_PATTERNS:
                vm = vpat.search(seg)
                if vm:
                    raw_v = vm.group(1).replace(",", "")
                    try:
                        v_num = float(raw_v)
                        if "k" in vm.group(0).lower():
                            vol_val = v_num * 1000
                            volume_str = f"{v_num:g}k"
                        elif "m" in vm.group(0).lower() or "mil" in vm.group(0).lower():
                            vol_val = v_num * 1000000
                            volume_str = f"{v_num:g}m"
                        else:
                            vol_val = v_num
                            volume_str = f"{v_num:g}"
                        break
                    except Exception as _ignored_exc:
                        print(f"[SENDER] parse_market_listing: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)

            detected_rate = None

            for pat in _DIRECT_RATE_PATTERNS:
                pm = pat.search(seg)
                if pm:
                    try:
                        val = float(pm.group(1))
                        if val > 20 and vol_val and vol_val >= 1000:
                            calc_rate = (val / (vol_val / 1000.0))
                            if 0.10 <= calc_rate <= 20.0:
                                detected_rate = round(calc_rate, 2)
                                break
                        elif 0.10 <= val <= 20.0:
                            detected_rate = val
                            break
                    except Exception:
                        continue

            if detected_rate is None and vol_val and vol_val >= 1000:
                bm = re.search(r"(?:for|\$|paying|total|price)\s*[:=]?\s*(?:\$|usd)?\s*(\d+(?:\.\d{1,2})?)\s*(?:\$|usd)?", seg, re.I)
                if bm:
                    try:
                        total_p = float(bm.group(1))
                        calc_rate = total_p / (vol_val / 1000.0)
                        if 0.10 <= calc_rate <= 20.0:
                            detected_rate = round(calc_rate, 2)
                    except Exception as _ignored_exc:
                        print(f"[SENDER] parse_market_listing: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)

            if detected_rate is not None and 0.10 <= detected_rate <= 20.0:
                return {
                    "matched": True,
                    "item": matched_item,
                    "kind": direction,
                    "price": detected_rate,
                    "volume": volume_str,
                    "payments": payments,
                    "segment": seg,
                }

    return None

def _extract_my_rate():
    """Pull a baseline rate from the active message or DEAL_MY_RATE."""
    if DEAL_MY_RATE and DEAL_MY_RATE > 0:
        return DEAL_MY_RATE
    if _runtime_rate is not None:
        return _runtime_rate
    src = _get_active_message()
    for pat in _DIRECT_RATE_PATTERNS:
        m = pat.search(src)
        if m:
            try: return float(m.group(1))
            except Exception: continue
    m = re.search(r'(\d+(?:\.\d{1,2})?)', src)
    if m:
        try: return float(m.group(1))
        except Exception: return None
    return None

def _send_deal_alert(cid, seller, price, ref_rate, profit_margin, snippet, jump_url, kind, item):
    global _deal_alerts_sent, _last_deal_ts
    if not DEAL_WEBHOOK_URL:
        event_log("DEAL", "Deal matched but DEAL_WEBHOOK_URL is not configured.")
        return
    with _deal_alert_lock:
        k = (cid, str(seller.get("id") or seller.get("username")), f"{price:.2f}")
        now = time.time()
        if now - _last_deal_alert.get(k, 0) < _DEAL_THROTTLE_SEC:
            return
        _last_deal_alert[k] = now
        _deal_alerts_sent += 1
        _last_deal_ts = now
    snip = (snippet or "").replace("\n", " ⏎ ")[:180]
    pct = (profit_margin / ref_rate * 100) if ref_rate > 0 else 0.0
    alt_badge = f"[Alt {ALT_ID} · {ALT_NAME}]" if ALT_ID else f"[{ALT_NAME}]"
    if kind == "buyer":
        title = f"📈 {alt_badge} ARBITRAGE ALERT — HIGH-PAYING BUYER"
        color = 0x57F287
        edge_label = f"+${profit_margin:.2f}/1k above cost ({pct:.1f}% profit)"
        action_type = f"🔵 BUYER DETECTED ({alt_badge})"
    else:
        title = f"🎯 {alt_badge} SUPPLIER ALERT — UNDER-MARKET SELLER"
        color = 0xFEE75C
        edge_label = f"+${profit_margin:.2f}/1k discount ({pct:.1f}% discount)"
        action_type = f"🟢 SELLER DETECTED ({alt_badge})"

    user_id = str(seller.get("id") or "")
    user_handle = seller.get("username") or seller.get("global_name") or "unknown"
    user_mention = f"<@{user_id}>" if user_id else f"@{user_handle}"

    # Extract payments and volume from snippet
    payments = [name for name, ppat in _PAYMENT_PATTERNS.items() if ppat.search(snippet or "")]
    volume_str = None
    for vpat in _VOLUME_PATTERNS:
        vm = vpat.search(snippet or "")
        if vm:
            raw_v = vm.group(1).replace(",", "")
            try:
                v_num = float(raw_v)
                if "k" in vm.group(0).lower():
                    volume_str = f"{v_num:g}k"
                elif "m" in vm.group(0).lower() or "mil" in vm.group(0).lower():
                    volume_str = f"{v_num:g}m"
                else:
                    volume_str = f"{v_num:g}"
                break
            except Exception as _ignored_exc:
                print(f"[SENDER] _send_deal_alert: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)

    rate_display = f"**${price:.2f}/1k**"
    if volume_str:
        rate_display += f" (Stock/Vol: `{volume_str}`)"

    fields = [
        {"name": "Category", "value": action_type, "inline": True},
        {"name": "Target Item", "value": f"**{item}**", "inline": True},
        {"name": "Market User", "value": f"{user_mention} (`@{user_handle}`)", "inline": True},
        {"name": "Channel", "value": f"<#{cid}>", "inline": True},
        {"name": "Detected Price", "value": rate_display, "inline": True},
        {"name": "Reference Baseline", "value": f"${ref_rate:.2f}/1k", "inline": True},
        {"name": "Net Profit Edge", "value": f"**{edge_label}**", "inline": True},
    ]

    if payments:
        fields.append({"name": "Payment Methods", "value": " · ".join(f"`{p}`" for p in payments), "inline": True})

    fields.append({"name": "Chat Excerpt", "value": f"```{snip}```[🔗 Jump to Discord message]({jump_url})", "inline": False})

    if user_id and ALT_ID:
        fields.append({
            "name": "⚡ Quick Reply Command",
            "value": f"`/reply alt:{ALT_ID} user:{user_id} text:Hey, I saw your post for {item} @ ${price:.2f}/1k!`",
            "inline": False,
        })

    embed = {
        "title": title,
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "url": jump_url,
        "fields": fields,
    }
    thread_title = f"💰 [{'+$' if profit_margin>=0 else '-$'}{abs(profit_margin):.2f}/1k] {item} @ ${price:.2f} (Alt {ALT_ID})" if ALT_ID else f"💰 [{'+$' if profit_margin>=0 else '-$'}{abs(profit_margin):.2f}/1k] {item} @ ${price:.2f}"
    try:
        send_deal_webhook(embed, thread_name=thread_title)
        event_log("DEAL", f"🔥 [{action_type[:15]}] {item} — @{user_handle} in #{cid} @ ${price:.2f}/1k (margin: +${profit_margin:.2f})")
    except Exception as e:
        dbg(f"[DEAL] alert error: {e}")

def scan_deals(cid, msgs):
    """Passive directional arbitrage scan — categorizes seller vs buyer opportunities."""
    if not _get_active_deal_scan_enabled() or not msgs:
        return
    baseline_rate = _extract_my_rate()
    if not baseline_rate or baseline_rate <= 0:
        return
    active_ad_type = _get_active_ad_type()
    delta = _get_active_deal_delta()
    gid = _guild_id_cache.get(cid, "@me")
    jump_base = f"https://discord.com/channels/{gid}/{cid}/"

    keywords = _get_active_deal_keywords()

    for m in msgs[:25]:
        try:
            aid = m.get("author", {}).get("id")
            if aid == _me_cache.get("id"):
                continue
            if m.get("author", {}).get("bot"):
                continue
            content = (m.get("content") or "").strip()
            if not content or len(content) < 5:
                continue
            if content[0] in "!/-.?":
                continue

            parsed = parse_market_listing(content, target_keywords=keywords)
            if not parsed or not parsed.get("matched"):
                continue

            item = parsed["item"]
            kind = parsed["kind"]
            price = parsed["price"]
            segment = parsed.get("segment", content)

            is_deal = False
            ref_rate = baseline_rate
            margin = 0.0

            # 1. Other user is SELLING: We can BUY low if price is under our baseline/sell rate
            if kind == "seller":
                if active_ad_type == "buy" and price <= baseline_rate - delta:
                    is_deal = True
                    ref_rate = baseline_rate
                    margin = baseline_rate - price
                elif active_ad_type == "sell" and price <= baseline_rate - delta:
                    is_deal = True
                    ref_rate = baseline_rate
                    margin = baseline_rate - price

            # 2. Other user is BUYING: We can SELL high if price is above our baseline/buy rate
            elif kind == "buyer":
                if active_ad_type == "sell" and price >= baseline_rate + delta:
                    is_deal = True
                    ref_rate = baseline_rate
                    margin = price - baseline_rate
                elif active_ad_type == "buy" and price >= baseline_rate + delta:
                    is_deal = True
                    ref_rate = baseline_rate
                    margin = price - baseline_rate

            if is_deal and margin >= delta:
                _send_deal_alert(
                    cid, m.get("author", {}), price, ref_rate, margin,
                    segment, jump_base + m.get("id", ""), kind, item
                )
        except Exception as e:
            dbg(f"[DEAL] scan error: {e}")

# --------------------------------------------------------------------------- #
# Mid-run IP Health Monitor (F-21 V6)                                       #
# --------------------------------------------------------------------------- #
_DATACENTER_KWS = ("microsoft", "azure", "amazon", "aws", "google", "ovh",
                   "digitalocean", "hetzner", "oracle", "linode", "github",
                   "alibaba", "tencent", "digital ocean")

def _lookup_egress():
    """Resolve the actual egress and ask an independent risk provider.

    The workflow verifies this same route successfully with the runner's curl
    binary. Some WARP runners cannot make the two provider requests through
    curl_cffi even though normal HTTPS/curl traffic is working, which used to
    produce a false ``? / ? / ?`` failure here. Use curl for this small
    verification probe (and urllib as a local fallback), while keeping the
    provider response, hosting classification, and country checks fail-closed.
    The sender's Discord traffic still uses SESSION below.
    """
    details = {
        "ip": "?", "org": "?", "country": "", "country_name": "?",
        "verified": False, "hosting": False, "connection_type": "",
    }

    def _fetch_json(url: str):
        import subprocess
        from urllib.request import Request, ProxyHandler, build_opener

        proxy = (HTTPS_PROXY or "").strip()
        curl_cmd = ["curl", "-fsSL", "--max-time", "10", "--retry", "1"]
        if proxy:
            curl_cmd.extend(["--proxy", proxy])
        else:
            # Do not inherit an unrelated runner proxy when WARP is the route.
            curl_cmd.extend(["--noproxy", "*"])
        curl_cmd.append(url)
        try:
            raw = subprocess.check_output(curl_cmd, stderr=subprocess.DEVNULL, timeout=15)
            payload = json.loads(raw.decode("utf-8", errors="replace"))
            if isinstance(payload, dict):
                return payload
        except Exception as _ignored_exc:
            print(f"[SENDER] _fetch_json: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)

        # Fallback for local environments where curl is unavailable. An
        # explicit proxy handler keeps the lookup on the same configured route.
        try:
            handler = ProxyHandler({"http": proxy, "https": proxy}) if proxy else ProxyHandler({})
            opener = build_opener(handler)
            request = Request(url, headers={"User-Agent": "adfarm-egress-check/1"})
            with opener.open(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
            return payload if isinstance(payload, dict) else None
        except Exception:
            return None

    try:
        ip_payload = _fetch_json("https://api.ipify.org?format=json") or {}
        ip = ip_payload.get("ip")
        if not isinstance(ip, str) or not ip.strip():
            return details
        details["ip"] = ip.strip()

        payload = _fetch_json(f"https://ipwho.is/{ip}") or {}
        if payload.get("success") is False:
            return details
        connection = payload.get("connection") or {}
        security = payload.get("security") or {}
        if not isinstance(connection, dict) or not isinstance(security, dict):
            return details
        org = connection.get("org") or connection.get("isp")
        if not isinstance(org, str) or not org.strip():
            return details
        details["org"] = org.strip().lower()
        details["country"] = str(payload.get("country_code") or "").upper()
        details["country_name"] = str(payload.get("country") or "?")
        details["connection_type"] = str(connection.get("type") or "").lower()
        details["hosting"] = bool(security.get("hosting")) or any(
            marker in details["connection_type"]
            for marker in ("hosting", "datacenter", "data center")
        )
        details["verified"] = True
        return details
    except Exception:
        return details


def _check_ip_raw():
    details = _lookup_egress()
    return details["ip"], details["org"], details["country"], details["hosting"], details["verified"]

def _ip_health_monitor():
    """Daemon: re-check IP periodically; pause public activity if datacenter."""
    global _ip_health_bad_until
    while not _stop_event.is_set() and not _panic_event.is_set():
        time.sleep(IP_HEALTH_CHECK_INTERVAL_MIN * 60)
        if _stop_event.is_set() or _panic_event.is_set(): return
        ip, org, country, hosting, verified = _check_ip_raw()
        ip = ip or "?"
        org = (org or "?").strip().lower()
        country = (country or "").strip().upper()
        # A failed identity lookup or an explicit hosting classification is
        # unsafe too: do not keep posting while the route cannot be verified.
        # Keep the country policy in force after startup as well, so a proxy
        # rotation cannot silently move the alt to an unapproved location.
        country_bad = bool(ALLOWED_COUNTRIES) and country not in ALLOWED_COUNTRIES
        is_cloudflare = "cloudflare" in org or "as13335" in org
        is_bad = (not verified or (hosting and not is_cloudflare) or org == "?"
                  or any(kw in org for kw in _DATACENTER_KWS)
                  or country_bad)
        with _ip_health_lock:
            was_bad = time.time() < _ip_health_bad_until
        if is_bad:
            with _ip_health_lock:
                _ip_health_bad_until = time.time() + IP_HEALTH_PAUSE_MIN * 60
            if not was_bad:
                reason = "country policy failed" if country_bad else "datacenter/unverified organization"
                log(f"🚨 IP HEALTH: unsafe egress ({reason}) IP={ip} ORG={org} COUNTRY={country or '?'} — pausing ALL public activity {IP_HEALTH_PAUSE_MIN:.0f} min.")
                send_log_webhook(f"🚨 **IP HEALTH ALERT** unsafe egress ({reason}) IP=`{ip}` ORG=`{org}` COUNTRY=`{country or '?'}` — pausing {IP_HEALTH_PAUSE_MIN:.0f}min")
                try:
                    send_dashboard({
                        "title": "🚨 IP HEALTH — WARP DROPPED",
                        "color": 0xED4245,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "description": (f"Outbound egress became unsafe mid-run ({reason}).\n"
                                        f"IP: `{ip}`\nISP: `{org}`\nCountry: `{country or '?'}`\n"
                                        f"All public activity PAUSED {IP_HEALTH_PAUSE_MIN:.0f} min."),
                    })
                except Exception as _exc:
                    log(f"[SECURITY] ip-pause notification embed send failed: {_exc}", kind="CAUTION")
        else:
            with _ip_health_lock:
                if was_bad:
                    log(f"🟢 IP HEALTH: IP back to safe ({ip} / {org}) — resuming public activity.")
                    send_log_webhook(f"🟢 **IP RECOVERED** `{ip}` `{org}` — resuming")
                    _ip_health_bad_until = 0.0

def _start_ip_health_monitor():
    if not PROXY_CHECK or IP_HEALTH_CHECK_INTERVAL_MIN <= 0:
        log("🛰️ IP health monitor: disabled.")
        return
    threading.Thread(target=_ip_health_monitor, daemon=True, name="ip-health").start()
    log(f"🛰️ IP health monitor: STARTED (every {IP_HEALTH_CHECK_INTERVAL_MIN:.0f} min; {IP_HEALTH_PAUSE_MIN:.0f} min pause on datacenter).")

# --------------------------------------------------------------------------- #
# Remote Panic Stop (F-32 V6)                                               #
# --------------------------------------------------------------------------- #
def _check_panic_gist():
    if not GIST_TOKEN or not GIST_ID:
        return False
    try:
        r = creq.get(f"https://api.github.com/gists/{GIST_ID}",
                     headers={"Authorization": f"token {GIST_TOKEN}",
                              "Accept": "application/vnd.github+json",
                              "User-Agent": "discord-ad-sender"},
                     impersonate=_BROWSER, timeout=8)
        if r.status_code != 200: return False
        for fname, finfo in (r.json().get("files") or {}).items():
            if "panic" in fname.lower() or "stop" in fname.lower():
                raw = (finfo.get("content") or "").strip()
                if not raw: continue
                try:
                    data = json.loads(raw)
                    if data.get("panic") or data.get("stop"):
                        return str(data.get("reason") or f"gist:{fname}")
                except Exception:
                    if "panic" in raw.lower(): return f"gist:{fname}"
        return False
    except Exception:
        return False

def _panic_trigger(reason="remote"):
    if _panic_event.is_set(): return
    _panic_event.set()
    log(f"\n🛑 PANIC STOP ({reason}) — stopping public activity immediately.")
    send_log_webhook(f"🛑 **PANIC STOP**: {reason}")
    try:
        send_dashboard({
            "title": "🛑 PANIC STOP",
            "color": 0xED4245,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "description": f"Stopped by remote command: **{reason}**. Clean shutdown in progress.",
        })
    except Exception as _exc:
        log(f"[CONTROL] stop-notification embed send failed (stop still queued): {_exc}", kind="CAUTION")

def _panic_checker_loop():
    while not _panic_event.is_set() and not _stop_event.is_set():
        time.sleep(PANIC_CHECK_INTERVAL_SEC)
        if _panic_event.is_set() or _stop_event.is_set(): return
        reason = _check_panic_gist()
        if reason:
            _panic_trigger(f"gist: {reason}")
            return

def _start_panic_checker():
    threading.Thread(target=_panic_checker_loop, daemon=True, name="panic-check").start()
    if PANIC_TRUSTED_IDS:
        log(f"🛑 Panic stop: ENABLED (gist every {PANIC_CHECK_INTERVAL_SEC:.0f}s; /panic DM from {len(PANIC_TRUSTED_IDS)} trusted ID(s)).")
    else:
        log(f"🛑 Panic stop: ENABLED (gist every {PANIC_CHECK_INTERVAL_SEC:.0f}s; no trusted DM IDs).")

def _handle_panic_dm(author_id, content):
    if not PANIC_TRUSTED_IDS or author_id not in PANIC_TRUSTED_IDS:
        return
    txt = (content or "").strip().lower()
    if txt.startswith("/panic") or txt.startswith("!panic") or txt.startswith("/stop"):
        _panic_trigger(f"trusted DM uid={author_id}")

# --------------------------------------------------------------------------- #
# Remote Control Commands (V6) — DMs from the official control bot          #
# --------------------------------------------------------------------------- #
def _controller_reply(cid, text):
    """Send a short DM reply. cid here is the DM channel id."""
    try:
        ref = f"https://discord.com/channels/@me/{cid}"
        api("POST", f"https://discord.com/api/v9/channels/{cid}/messages",
            referer=ref, json_body={"content": text[:2000],
                                    "allowed_mentions": {"parse": []}}, retries=1)
    except Exception as e:
        dbg(f"[CTRL] reply failed: {e}")

def _extract_rate_value(text):
    m = re.search(r'(\d+(?:\.\d{1,2})?)', text or "")
    if m:
        try: return float(m.group(1))
        except Exception: return None
    return None

def _apply_rate_to_message(text, new_rate):
    """Replace the primary price number in an ad message with new_rate."""
    patterns = [
        (re.compile(r'(\d+(?:\.\d{1,2})?)\s*(?:\$|usd)?\s*(?:\/|p\s*\/|per|for\s*(?:1\s*k)?)\s*(?:1\s*)?k\b', re.I),
         lambda m, r: f"{r}$/1k" if "$" in m.group(0) or "usd" in m.group(0).lower() else f"{r}/1k"),
        (re.compile(r'(\d+(?:\.\d{1,2})?)\s*\$', re.I), lambda m, r: f"{r}$"),
        (re.compile(r'(?<!\d)(\d+(?:\.\d{1,2})?)(?!\d)'), lambda m, r: f"{r}"),
    ]
    for pat, repl in patterns:
        m = pat.search(text)
        if m:
            return text[:m.start(1)] + repl(m, f"{new_rate:g}") + text[m.end(1):]
    return text

def _handle_controller_dm(cid, author_id, content, *, trusted_source=False, reply=True, reply_fn=None):
    """Parse and act on a DM command from a controller user. Returns True if handled."""
    global _paused_by_controller, _runtime_message, _runtime_rate, _runtime_ad_type, _runtime_hours, _runtime_deal_keywords
    global _runtime_deal_scan_enabled, _runtime_deal_delta, _runtime_policy_template, INTERVAL_MIN
    def respond(text):
        if reply_fn is not None:
            reply_fn(text)
        elif reply:
            _controller_reply(cid, text)
    if not trusted_source and author_id not in CONTROLLER_USER_IDS:
        return False
    txt = (content or "").strip()
    if not txt:
        return True
    lower = txt.lower()
    # Split command vs args
    if not txt.startswith(CONTROL_CMD_PREFIX) and not txt.startswith("/"):
        # Not a command — ignore (don't forward to webhook either)
        respond("ℹ️ Expected a command prefixed with `!` or `/` (e.g. `!status`, `!pause`).")
        return True
    cmd, _, args = txt.lstrip("!/").partition(" ")
    cmd = cmd.lower()
    args = args.strip()

    if cmd in ("ping", "hello"):
        respond(f"✅ {ALT_NAME} ({VERSION}) is online — pong")
    elif cmd == "status":
        with _state_lock:
            paused = _public_pause_until > time.time()
        status_flag = "paused" if _paused_by_controller or paused else \
                      ("ip_pause" if time.time() < _ip_health_bad_until else
                       ("caution" if any(_caution_channels.values()) else "active"))
        r = _runtime_rate or _extract_rate_value(MESSAGE)
        at = _runtime_ad_type or AD_TYPE
        msg_preview = (_runtime_message or MESSAGE)[:80].replace("\n", " ⏎ ")
        lines = [f"✅ **{ALT_NAME}** ({VERSION})",
                 f"mode: `{at}`  rate: `{r}`  status: `{status_flag}`",
                 f"sent: {total_sent}  err: {total_err}  uptime: {(time.time()-_run_start_epoch)/60:.1f}m",
                 f"msg: ```{msg_preview}```"]
        respond("\n".join(lines))
    elif cmd == "pause":
        _paused_by_controller = True
        log("⏸️ Paused by remote controller command.")
        send_log_webhook(f"⏸️ **PAUSED** by controller DM")
        respond(f"✅ {ALT_NAME} PAUSED (public posts stopped). Use !resume to continue.")
    elif cmd == "resume":
        _paused_by_controller = False
        log("▶️ Resumed by remote controller command.")
        send_log_webhook(f"▶️ **RESUMED** by controller DM")
        respond(f"✅ {ALT_NAME} RESUMED — normal posting.")
    elif cmd in ("stop", "quit", "panic"):
        respond(f"🛑 {ALT_NAME} stopping now.")
        log("🛑 Remote stop received via DM.")
        _panic_trigger(f"controller DM uid={author_id}")
    elif cmd in ("setprice", "price", "rate"):
        new_rate = _extract_rate_value(args)
        if new_rate is None or new_rate <= 0 or new_rate > 20:
            respond(f"❌ Invalid price (got `{args}`). Use e.g. `!setprice 2.3`")
            return True
        _runtime_rate = new_rate
        # Rebuild runtime message with new rate
        base = _runtime_message or MESSAGE
        new_msg = _apply_rate_to_message(base, new_rate)
        _runtime_message = new_msg
        log(f"💰 Price updated by controller → {new_rate}$/1k")
        send_log_webhook(f"💰 **PRICE SET** → ${new_rate:.2f}/1k (controller)")
        respond(f"✅ Price updated to {new_rate}$/1k. Next post uses new rate.")
    elif cmd in ("setmode", "mode"):
        mode = args.lower().strip()
        if mode not in ("sell", "buy"):
            respond(f"❌ Mode must be 'sell' or 'buy'.")
            return True
        _runtime_ad_type = mode
        log(f"🔄 Ad type set by controller → {mode}")
        send_log_webhook(f"🔄 **MODE SET** → {mode} (controller). You should also send !setmessage with the new copy.")
        respond(f"✅ Ad type set to `{mode}`. Send `!setmessage <new copy>` to update the ad text.")
    elif cmd in ("setmessage", "message"):
        if not args:
            respond("❌ Provide the new message text.")
            return True
        if len(args) > 1900:
            respond("❌ Message too long (max 1900).")
            return True
        _runtime_message = args
        log(f"📝 Message updated by controller ({len(args)} chars)")
        send_log_webhook(f"📝 **MESSAGE UPDATED** by controller ({len(args)} chars)")
        respond(f"✅ Message updated. {len(args)} chars — next post uses new copy.")
    elif cmd in ("setinterval", "interval"):
        try:
            new_interval = int(args.strip())
        except (TypeError, ValueError):
            new_interval = 0
        if new_interval not in (3, 5):
            respond("❌ Interval must be 3 or 5 minutes.")
            return True
        global INTERVAL_MIN
        INTERVAL_MIN = new_interval
        event_log("CONTROL", f"interval updated by controller → {new_interval} minutes")
        respond(f"✅ Interval updated to {new_interval} minutes. Scheduler state changed live.")
    elif cmd in ("setruntime", "runtime"):
        try:
            new_hours = int(args.strip())
        except (TypeError, ValueError):
            new_hours = 0
        if new_hours not in (6, 12, 18, 24, 48):
            respond("❌ Runtime must be 6, 12, 18, 24, or 48 hours.")
            return True
        global _runtime_run_end
        _runtime_run_end = time.time() + new_hours * 3600
        _runtime_hours = new_hours
        event_log("CONTROL", f"runtime updated by controller → {new_hours} hours")
        respond(f"✅ Runtime end moved to {new_hours} hours from now (48-hour cap).")
    elif cmd in ("setdealkeywords", "dealkeywords"):
        keywords = _parse_deal_keywords(args)
        if not keywords:
            respond("❌ Provide at least one comma-separated item keyword, e.g. `!setdealkeywords Robux, MM2, Pet Sim, Blox Fruits, Tokens`.")
            return True
        _runtime_deal_keywords = keywords
        event_log("DEAL", f"deal item keywords updated by controller → {', '.join(keywords)}")
        send_log_webhook(f"📈 **DEAL KEYWORDS UPDATED** → {', '.join(keywords)}", kind="DEAL")
        respond(f"✅ Deal scanner now requires one of: `{', '.join(keywords)}`")
    elif cmd in ("setdealscan", "dealscan"):
        value = args.casefold().strip()
        if value in {"on", "true", "1", "enable", "enabled"}:
            _runtime_deal_scan_enabled = True
        elif value in {"off", "false", "0", "disable", "disabled"}:
            _runtime_deal_scan_enabled = False
        else:
            respond("❌ Scanner must be `on` or `off`.")
            return True
        event_log("DEAL", f"deal scanner {'enabled' if _runtime_deal_scan_enabled else 'disabled'} by controller")
        respond(f"✅ Deal scanner is now `{ 'on' if _runtime_deal_scan_enabled else 'off' }`.")
    elif cmd in ("setdealdelta", "dealdelta"):
        try:
            delta = float(args.strip())
        except (TypeError, ValueError):
            delta = -1
        if not math.isfinite(delta) or delta < 0 or delta > 5:
            respond("❌ Deal delta must be between `0` and `5` dollars per 1k, e.g. `!setdealdelta 0.05`.")
            return True
        _runtime_deal_delta = delta
        event_log("DEAL", f"deal alert delta updated by controller → ${delta:.2f}/1k")
        respond(f"✅ Deal alerts now require an edge of `${delta:.2f}/1k`.")
    elif cmd in ("reply", "senddm", "dmreply"):
        target_uid, _, reply_body = args.partition(" ")
        target_uid = re.sub(r'\D', '', target_uid.strip())
        reply_body = reply_body.strip()
        if not target_uid or not reply_body:
            respond("⚠️ Usage: `!reply <user_id> <message text>`")
            return True
        try:
            r_chan = api("POST", "https://discord.com/api/v9/users/@me/channels",
                         json_body={"recipient_id": target_uid}, retries=2)
            if r_chan.status_code in (200, 201):
                dm_cid = r_chan.json().get("id")
                if dm_cid:
                    send_typing(dm_cid, reply_body)
                    nonce = _make_nonce()
                    ref = f"https://discord.com/channels/@me/{dm_cid}"
                    payload = _make_message_payload(reply_body, nonce)
                    r_msg = api("POST", f"https://discord.com/api/v9/channels/{dm_cid}/messages",
                                referer=ref, json_body=payload, retries=2)
                    if r_msg.status_code in (200, 201):
                        respond(f"✅ Reply delivered to buyer `{target_uid}`.")
                        log(f"📤 Relayed operator reply to buyer `{target_uid}`: \"{reply_body[:50]}...\"")
                    else:
                        respond(f"❌ Failed to send DM (HTTP {r_msg.status_code}): {getattr(r_msg, 'text', '')[:100]}")
                else:
                    respond(f"❌ Could not resolve DM channel for user `{target_uid}`.")
            else:
                respond(f"❌ Could not open DM with user `{target_uid}` (HTTP {r_chan.status_code}).")
        except Exception as e:
            respond(f"❌ Error sending DM: {e}")
    elif cmd == "policy":
        t_clean = args.strip().lower()
        if t_clean not in {"stealth", "aggressive", "peak_hour", "balanced"}:
            respond("❌ Policy template must be `stealth`, `aggressive`, `peak_hour`, or `balanced`.")
            return True
        _runtime_policy_template = t_clean
        if t_clean == "stealth":
            INTERVAL_MIN = 5
            _runtime_deal_scan_enabled = False
        elif t_clean == "aggressive":
            INTERVAL_MIN = 3
            _runtime_deal_scan_enabled = True
            _runtime_deal_delta = 0.05
        elif t_clean == "peak_hour":
            INTERVAL_MIN = 3
            _runtime_deal_scan_enabled = True
            _runtime_deal_delta = 0.03
        elif t_clean == "balanced":
            INTERVAL_MIN = 5
            _runtime_deal_scan_enabled = True
            _runtime_deal_delta = 0.05
        event_log("CONTROL", f"policy template applied by controller: {t_clean.upper()} (interval={INTERVAL_MIN}m, deal_scan={'on' if _runtime_deal_scan_enabled else 'off'})")
        send_log_webhook(f"🛡️ **POLICY APPLIED** → `{t_clean.upper()}` (interval={INTERVAL_MIN}m, deals={'on' if _runtime_deal_scan_enabled else 'off'})", kind="CONTROL")
        respond(f"✅ Policy template **{t_clean.upper()}** active (interval={INTERVAL_MIN}m, deal_scan={'on' if _runtime_deal_scan_enabled else 'off'}).")
    elif cmd == "setchannels":
        # Overwrite ALL active channels tied to this alt from the controller.
        # Every ID is verified against Discord before the live scheduler list
        # is replaced, so no phantom/typo channel can enter a running sender.
        if args.strip().casefold() in {"clear", "none", "empty"}:
            raw_ids = []
        else:
            raw_ids = [part.strip() for part in args.replace(",", " ").split() if part.strip()]
        if args.strip() and not raw_ids:
            respond("❌ Use `!setchannels <id1,id2,...>` with numeric channel IDs, or `!setchannels clear`.")
            return True
        verified = []
        for cid in raw_ids:
            if not cid.isdigit():
                respond(f"❌ Channel ID `{cid}` must contain digits only. No channel table was changed.")
                return True
            info = get_channel_info(cid)
            if not info:
                respond(f"❌ Discord did not verify channel `{cid}`. No channel table was changed.")
                return True
            verified.append((cid, str(info.get("name") or cid)[:80], int(info.get("rate_limit_per_user") or 0)))
        with _state_lock:
            CHANNEL_IDS[:] = [cid for cid, _nm, _sl in verified]
            _ch_names_ref.clear()
            _slowmodes_ref.clear()
            for cid, nm, sl in verified:
                _ch_names_ref[cid] = nm
                _slowmodes_ref[cid] = sl
            _active_ch_ref[:] = [cid for cid, _nm, _sl in verified]
            _dead_channels_ref.clear()
            _next_post_ref.clear()
            for cid, _nm, _sl in verified:
                _next_post_ref[cid] = time.time() + random.uniform(20, 45)
            if _stats_ref is not None:
                _stats_ref.clear()
                for cid, _nm, _sl in verified:
                    _stats_ref.setdefault(cid, {"sent": 0, "errors": 0, "skipped": 0,
                                                "cooldown": 0, "img": 0, "txt": 0, "edits": 0})
        _persist_runtime_targets()
        event_log("CHANNEL", f"channel table overwritten by controller: {len(verified)} verified target(s)")
        send_log_webhook(f"🔁 **CHANNELS OVERWRITTEN** → `{', '.join(cid for cid, _nm, _sl in verified)}`", kind="CHANNEL")
        respond(f"✅ Channel table overwritten with {len(verified)} verified target(s): {', '.join(cid for cid, _nm, _sl in verified)}")
    elif cmd in ("setchannel", "replacechannel"):
        # Safe live channel update. The target must be numeric and readable
        # before it enters the scheduler; this avoids phantom/typo channels.
        old_cid = None
        if cmd == "replacechannel":
            parts = args.split(maxsplit=2)
            if len(parts) < 2:
                respond("❌ Use `!replacechannel <old_id> <new_id> [name]`.")
                return True
            old_cid, new_cid = parts[0], parts[1]
            label = parts[2] if len(parts) > 2 else ""
        else:
            parts = args.split(maxsplit=1)
            if not parts:
                respond("❌ Use `!setchannel <channel_id> [name]`.")
                return True
            new_cid = parts[0]
            label = parts[1] if len(parts) > 1 else ""
        if not new_cid.isdigit():
            respond("❌ Channel ID must contain digits only.")
            return True
        info = get_channel_info(new_cid)
        if not info:
            respond(f"❌ Discord did not verify channel `{new_cid}`. No runtime change made.")
            return True
        new_name = str(info.get("name") or label or new_cid)[:80]
        if old_cid and old_cid in CHANNEL_IDS and old_cid != new_cid:
            context = {"ch_names": _ch_names_ref, "slowmodes": _slowmodes_ref,
                       "last_sent": _last_sent_ref, "my_last_msg_id": _my_last_msg_id_ref,
                       "stats": _stats_ref, "active_channels": _active_ch_ref,
                       "dead_channels": _dead_channels_ref, "next_post_time": _next_post_ref}
            _rename_channel_entry(old_cid, new_cid, new_name, **{k: context[k] for k in ("ch_names", "slowmodes", "last_sent", "my_last_msg_id", "stats")})
            try:
                CHANNEL_IDS[CHANNEL_IDS.index(old_cid)] = new_cid
            except ValueError:
                CHANNEL_IDS.append(new_cid)
            if old_cid in _active_ch_ref:
                _active_ch_ref[_active_ch_ref.index(old_cid)] = new_cid
            _dead_channels_ref.discard(old_cid)
            _next_post_ref[new_cid] = time.time() + random.uniform(20, 45)
            respond(f"✅ Channel updated: `{old_cid}` → `#{new_name}` (`{new_cid}`). Scheduler will resume safely.")
        else:
            if new_cid not in CHANNEL_IDS:
                CHANNEL_IDS.append(new_cid)
            _ch_names_ref[new_cid] = new_name
            _slowmodes_ref[new_cid] = int(info.get("rate_limit_per_user") or 0)
            if _stats_ref is not None:
                _stats_ref.setdefault(new_cid, {"sent": 0, "errors": 0, "skipped": 0,
                                               "cooldown": 0, "img": 0, "txt": 0, "edits": 0})
            if new_cid not in _active_ch_ref:
                _active_ch_ref.append(new_cid)
            _dead_channels_ref.discard(new_cid)
            _next_post_ref[new_cid] = time.time() + random.uniform(20, 45)
            respond(f"✅ Channel added/updated: **#{new_name}** (`{new_cid}`). Scheduler state updated live.")
        _persist_runtime_targets()
        event_log("CHANNEL", f"channel runtime update by controller: {old_cid or 'new'} -> {new_cid}")
    elif cmd == "sync":
        # Re-read Gist config + blocklist
        load_blocked_from_gist()
        if not trusted_source:
            _sync_control_gist(force=True)
        save_blocked_to_gist(force=True)
        respond(f"✅ Sync complete. Blocklist + control gist reloaded.")
    elif cmd in ("rescan", "rescan_channels", "refreshchannels"):
        global _guilds_cache_fetched
        _guilds_cache_fetched = False
        log("🔄 Manual server channel re-scan requested by controller.", kind="CHANNEL")
        ok = _resolve_channel_keywords()
        registry_result = _reconcile_channel_registry("manual rescan") if ok else {"ok": False}
        if ok and registry_result.get("ok"):
            ch_summary = ", ".join(f"#{_ch_names_ref.get(c, c)} (`{c}`)" for c in CHANNEL_IDS)
            event_log("CHANNEL", f"channel re-scan complete: {len(CHANNEL_IDS)} target(s)")
            send_log_webhook(
                f"🔄 **CHANNELS RESCANNED** → [{ch_summary}] | "
                f"added={len(registry_result.get('added', []))} "
                f"removed={len(registry_result.get('removed', []))} "
                f"replaced={len(registry_result.get('replaced', []))}",
                kind="CHANNEL",
            )
            respond(f"✅ Re-scan complete. Active channels ({len(_active_ch_ref)}/{len(CHANNEL_IDS)}): {ch_summary}")
        elif ok:
            # PRE-001 fix: when the persistent registry is unavailable (e.g.
            # standalone alt / test mode), verify + refresh each configured
            # channel directly so names/slowmodes still update and no stale
            # data survives the rescan.
            refreshed = 0
            with _state_lock:
                for cid in [c for c in CHANNEL_IDS if str(c).isdigit()]:
                    info = get_channel_info(cid)
                    if not info:
                        continue
                    _ch_names_ref[cid] = str(info.get("name") or cid)[:80]
                    _slowmodes_ref[cid] = int(info.get("rate_limit_per_user") or 0)
                    if cid not in _active_ch_ref:
                        _active_ch_ref.append(cid)
                    _dead_channels_ref.discard(cid)
                    refreshed += 1
            event_log("CHANNEL", f"channel re-scan fallback: verified {refreshed} target(s)")
            send_log_webhook(
                f"🔄 **CHANNELS RESCANNED (direct verify)** → {refreshed} target(s) refreshed",
                kind="CHANNEL",
            )
            ch_summary = ", ".join(f"#{_ch_names_ref.get(c, c)} (`{c}`)" for c in CHANNEL_IDS)
            respond(f"✅ Re-scan complete (direct verification). Active channels ({len(_active_ch_ref)}/{len(CHANNEL_IDS)}): {ch_summary}")
        else:
            respond("⚠️ Re-scan failed: no valid channel targets resolved from CHANNEL_NAMES/CHANNEL_KEYWORDS.")
    elif cmd in ("resetcaution", "clearcaption", "clearcaution"):
        target_cid = args.strip()
        with _state_lock:
            _consecutive_deletions = 0
            if target_cid and target_cid in _caution_channels:
                _caution_channels.pop(target_cid, None)
                _channel_verify_history.pop(target_cid, None)
                _channel_caution_survives.pop(target_cid, None)
                _dead_channels_ref.discard(target_cid)
                if target_cid in CHANNEL_IDS and target_cid not in _active_ch_ref:
                    _active_ch_ref.append(target_cid)
                detail = f"channel `{target_cid}`"
            else:
                _caution_channels.clear()
                _channel_verify_history.clear()
                _channel_caution_survives.clear()
                _dead_channels_ref.clear()
                for c in CHANNEL_IDS:
                    if c not in _active_ch_ref:
                        _active_ch_ref.append(c)
                detail = "all channels"
        _CIRCUIT_BREAKER.reset(target_cid if target_cid and target_cid != "all" else None)
        event_log("CONTROL", f"caution mode reset by controller ({detail})")
        send_log_webhook(f"🚨 **CAUTION RESET** → {detail} (controller)")
        respond(f"✅ Caution mode, strike history, and channel error backoffs cleared for {detail}.")
    elif cmd == "help":
        respond("Commands: !status !pause !resume !stop !setprice <x> !setmode <sell|buy> !setmessage <text> !setdealkeywords <a,b,c> !setdealscan <on|off> !setdealdelta <0..5> !setchannel <id> [name] !replacechannel <old> <new> !policy <preset> !setinterval <3|5> !setruntime <6|12|18|24|48> !sync !ping !rescan !resetcaution [id] !reply <uid> <text>")
    else:
        respond(f"❓ Unknown command `{cmd}`. Try !help")
    return True


# --------------------------------------------------------------------------- #
# Gist-driven config sync (V6)                                              #
# --------------------------------------------------------------------------- #
def _ack_control_gist(filename, original, command_id, response_text):
    """Write a bounded acknowledgement beside a queued Gist command."""
    if not filename or not CONTROL_GIST_ID or not GIST_TOKEN:
        return
    payload = dict(original or {})
    payload["ack_id"] = str(command_id)[:80]
    payload["ack"] = str(response_text or "Command applied")[:500]
    payload["ack_at"] = time.time()
    try:
        r = creq.patch(
            f"https://api.github.com/gists/{CONTROL_GIST_ID}",
            headers={"Authorization": f"token {GIST_TOKEN}",
                     "Accept": "application/vnd.github+json",
                     "User-Agent": "discord-ad-sender"},
            json={"files": {filename: {"content": json.dumps(payload, ensure_ascii=False)}}},
            impersonate=_BROWSER, timeout=8,
        )
        if r.status_code != 200:
            dbg(f"[SYNC] control ack write failed (HTTP {r.status_code})")
    except Exception as e:
        dbg(f"[SYNC] control ack write error: {type(e).__name__}: {e}")


def _sync_control_gist(force=False):
    """Apply shared-Gist overrides and queued commands without a DM.

    ``control.json`` remains the optional broadcast override file. The control
    bot writes one command file per alt (``control_<ALT_ID>.json``), allowing
    the alt to stay out of the control server entirely. Each command has a
    unique id and is applied once per run; an acknowledgement is written back
    to the same file for audit/debugging.
    """
    global _paused_by_controller, _runtime_message, _runtime_rate
    global _runtime_ad_type, _runtime_deal_keywords, _runtime_deal_scan_enabled, _runtime_deal_delta, _last_gist_sync
    global _last_control_command_id, INTERVAL_MIN
    if not CONTROL_GIST_ID or not GIST_TOKEN:
        return
    if not force and (time.time() - _last_gist_sync) < SYNC_GIST_INTERVAL_SEC:
        return
    try:
        r = creq.get(f"https://api.github.com/gists/{CONTROL_GIST_ID}",
                     headers={"Authorization": f"token {GIST_TOKEN}",
                              "Accept": "application/vnd.github+json",
                              "User-Agent": "discord-ad-sender"},
                     impersonate=_BROWSER, timeout=8)
        if r.status_code != 200:
            dbg(f"[SYNC] control gist read failed (HTTP {r.status_code})")
            return
        files = r.json().get("files") or {}
        broadcast = {}
        targeted = {}
        targeted_filename = ""
        preferred = f"control_{ALT_ID}.json".lower()
        for fname, finfo in files.items():
            lower_name = str(fname).lower()
            if lower_name not in {"control.json", preferred}:
                continue
            raw = (finfo.get("content") or "").strip()
            if not raw:
                continue
            try:
                item = json.loads(raw)
            except Exception:
                dbg(f"[SYNC] ignored invalid JSON in {fname}")
                continue
            if not isinstance(item, dict):
                continue
            item_alt = item.get("alt_id")
            try:
                if item_alt is not None and int(item_alt) != ALT_ID:
                    continue
            except (TypeError, ValueError):
                continue
            if lower_name == preferred:
                targeted = item
                targeted_filename = str(fname)
            else:
                broadcast = item
        data = dict(broadcast)
        data.update(targeted)
        if not data:
            _last_gist_sync = time.time()
            return
        # Only apply if alt_id matches (or is absent → broadcast).
        target_alt = data.get("alt_id")
        if target_alt is not None and int(target_alt) != ALT_ID:
            _last_gist_sync = time.time()
            return
        if "paused" in data:
            _paused_by_controller = bool(data["paused"])
        if data.get("rate") is not None:
            try:
                nr = float(data["rate"])
                if 0 < nr <= 20:
                    _runtime_rate = nr
                    _runtime_message = _apply_rate_to_message(
                        _runtime_message or MESSAGE, nr
                    )
            except Exception as _ignored_exc:
                print(f"[SENDER] _sync_control_gist: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
        if data.get("ad_type") in ("sell", "buy"):
            _runtime_ad_type = data["ad_type"]
        if isinstance(data.get("message"), str) and data["message"]:
            _runtime_message = data["message"][:1900]
        if data.get("interval_min") in (3, 5):
            INTERVAL_MIN = int(data["interval_min"])
        if isinstance(data.get("deal_keywords"), list):
            keywords = _parse_deal_keywords(",".join(str(x) for x in data["deal_keywords"]))
            if keywords:
                _runtime_deal_keywords = keywords
        if isinstance(data.get("deal_scan_enabled"), bool):
            _runtime_deal_scan_enabled = data["deal_scan_enabled"]
        if data.get("deal_alert_delta") is not None:
            try:
                delta = float(data["deal_alert_delta"])
                if math.isfinite(delta) and 0 <= delta <= 5:
                    _runtime_deal_delta = delta
            except (TypeError, ValueError, OverflowError) as _ignored_exc:
                print(f"[SENDER] _sync_control_gist: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
        # Apply commands queued by the official control bot. A stop command
        # from an older run must not kill a newly started workflow.
        command_id = str(data.get("command_id") or "")
        command = str(data.get("command") or "").strip().lower()
        if command_id and command and command_id != _last_control_command_id:
            try:
                issued_at = float(data.get("issued_at") or 0)
            except (TypeError, ValueError, OverflowError):
                issued_at = 0
            stale_stop = command == "stop" and _run_start_epoch and issued_at < _run_start_epoch
            responses = []
            if stale_stop:
                handled = True
                responses.append("ℹ️ Ignored stale stop command from an earlier run.")
            else:
                command_text = "!" + command
                if data.get("args"):
                    command_text += " " + str(data["args"])[:1900]
                handled = _handle_controller_dm(
                    f"gist:{command_id[:12]}", "control-gist", command_text,
                    trusted_source=True, reply=False, reply_fn=responses.append,
                )
            if handled:
                _last_control_command_id = command_id
                _ack_control_gist(
                    targeted_filename, data, command_id,
                    responses[-1] if responses else "✅ Command applied.",
                )
                event_log("CONTROL", f"control Gist command applied: {command}")
        _last_gist_sync = time.time()
        dbg(
            f"[SYNC] control gist applied: paused={_paused_by_controller} "
            f"rate={_runtime_rate} type={_runtime_ad_type} "
            f"deal_keywords={','.join(_get_active_deal_keywords())} "
            f"deal_scan={_get_active_deal_scan_enabled()} delta={_get_active_deal_delta():.2f}"
        )
    except Exception as e:
        dbg(f"[SYNC] control gist error: {type(e).__name__}: {e}")


def _get_run_end(default_end):
    # In limitless mode the sender deliberately sets an infinite wall clock,
    # so a 0 value never accidentally clamps the run to the startup instant.
    if _runtime_run_end == float("inf"):
        return float("inf")
    return _runtime_run_end or default_end


def _get_active_message():
    """Return the effective ad message (original or runtime override)."""
    return _runtime_message if _runtime_message else MESSAGE

def _get_active_ad_type():
    return _runtime_ad_type if _runtime_ad_type else AD_TYPE

def _get_active_deal_keywords():
    return _runtime_deal_keywords if _runtime_deal_keywords is not None else DEAL_ITEM_KEYWORDS

def _get_active_deal_scan_enabled():
    return DEAL_SCAN_ENABLED if _runtime_deal_scan_enabled is None else bool(_runtime_deal_scan_enabled)

def _get_active_deal_delta():
    return DEAL_ALERT_DELTA if _runtime_deal_delta is None else float(_runtime_deal_delta)

def _get_active_rate():
    if _runtime_rate is not None:
        return _runtime_rate
    return _extract_rate_value(MESSAGE)


# --------------------------------------------------------------------------- #
# Heartbeat webhook (V6)                                                    #
# --------------------------------------------------------------------------- #
def _send_heartbeat(active_channels_list, ch_names, slowmodes, last_sent,
                    my_last_msg_id, stats, total_sent, total_err, total_skip,
                    total_img, total_edits, dead_channels=None, in_afk_flag=False):
    global _last_heartbeat_sent
    if not DASHBOARD_WEBHOOK_URL:
        return
    now = time.time()
    if now - _last_heartbeat_sent < HEARTBEAT_INTERVAL_SEC:
        return
    _last_heartbeat_sent = now
    # Determine status flag
    with _ip_health_lock:
        ip_paused = now < _ip_health_bad_until
    with _state_lock:
        dm_paused = now < _public_pause_until
    if _panic_event.is_set():
        status = "stopped"
    elif _new_location_failed_event.is_set():
        status = "error"
    elif _paused_by_controller:
        status = "paused"
    elif ip_paused:
        status = "ip_pause"
    elif any(_caution_channels.values()):
        status = "caution"
    elif dm_paused:
        status = "paused"
    elif in_afk_flag:
        status = "afk"
    else:
        status = "active"
    warnings = []
    with _state_lock:
        if any(_caution_channels.values()):
            warnings.append("Shadowban caution in at least one channel")
    if ip_paused:
        warnings.append("IP health: WARP dropped / datacenter")
    # Channel breakdown
    ch_data = {}
    for cid in CHANNEL_IDS:
        ch_data[cid] = {
            "name": ch_names.get(cid, cid),
            "sent": stats[cid]["sent"],
            "errors": stats[cid]["errors"],
            "last_post": last_sent.get(cid, 0),
            "slowmode": slowmodes.get(cid, 0),
            "alive": cid in active_channels_list,
        }
    ip_org = ""
    ip_country = ""
    registry_snapshot = _channel_registry.snapshot_for_alt(ALT_ID) if _channel_registry and ALT_ID else {}
    payload_json = {
        "heartbeat": True,
        "type": "heartbeat",
        "version": VERSION,
        "alt_id": ALT_ID,
        "alt_name": ALT_NAME,
        "ad_type": _get_active_ad_type(),
        "rate": _get_active_rate(),
        "rate_currency": "$/1k",
        "interval_min": INTERVAL_MIN,
        "policy_template": _runtime_policy_template,
        "runtime_hours": 0 if (RUNTIME_LIMITLESS or _runtime_hours == 0) else _runtime_hours or max(1, round(TOTAL_RUN_MIN / 60)),
        "message_preview": _get_active_message().split("\n")[0][:120],
        "total_sent": total_sent,
        "total_errors": total_err,
        "total_skips": total_skip,
        "total_edits": total_edits,
        "deal_alerts": _deal_alerts_sent,
        "last_deal_ts": _last_deal_ts,
        "deal_keywords": _get_active_deal_keywords(),
        "deal_scan_enabled": _get_active_deal_scan_enabled(),
        "deal_alert_delta": _get_active_deal_delta(),
        "last_error": _last_error,
        "log_counts": dict(_log_counts),
        "uptime_sec": now - _run_start_epoch if _run_start_epoch else 0,
        "active_channels": len([c for c in active_channels_list if dead_channels is None or c not in dead_channels]),
        "total_channels": len(CHANNEL_IDS),
        "last_post_ts": max(last_sent.values()) if last_sent else 0,
        "status": status,
        "warnings": warnings,
        "channels": ch_data,
        "channel_registry": {
            "version": registry_snapshot.get("version", 1),
            "updated_at": registry_snapshot.get("updated_at", 0),
            "servers": registry_snapshot.get("servers", {}),
            "targets": registry_snapshot.get("targets", CHANNEL_IDS),
        },
        "run_started_ts": _run_start_epoch,
        "ip_org": ip_org,
        "ip_country": ip_country,
        "ts": now,
    }
    # The dashboard webhook is intentionally human-readable. The control bot
    # parses these structured fields, so there is no noisy raw JSON block in
    # Discord. This also keeps the machine state and operator view separate.
    title_dot = {"active": "🟢", "paused": "🟡", "caution": "⚠️",
                 "ip_pause": "🚨", "afk": "☕", "stopped": "🔴",
                 "error": "🔴"}.get(status, "⚪")
    active_count = len([c for c in active_channels_list
                        if dead_channels is None or c not in dead_channels])
    fields = [
        {"name": "Status", "value": f"{title_dot} `{status}`", "inline": True},
        {"name": "Mode", "value": f"`{_get_active_ad_type()}`", "inline": True},
        {"name": "Rate", "value": f"${_get_active_rate()}/1k" if _get_active_rate() else "—", "inline": True},
        {"name": "Cadence", "value": f"`{INTERVAL_MIN}m`", "inline": True},
        {"name": "Activity", "value": f"Sent: `{total_sent}` · Errors: `{total_err}` · Skips: `{total_skip}`", "inline": False},
        {"name": "Deals", "value": f"`{_deal_alerts_sent}` alert(s)", "inline": True},
        {"name": "Scanner", "value": f"{'ON' if _get_active_deal_scan_enabled() else 'OFF'} · edge ${_get_active_deal_delta():.2f}/1k", "inline": True},
        {"name": "Keywords", "value": ", ".join(_get_active_deal_keywords())[:1000] or "none configured", "inline": False},
        {"name": "Uptime", "value": f"{(now-_run_start_epoch)/60:.1f} min" if _run_start_epoch else "—", "inline": True},
        {"name": "Channels", "value": f"Active: `{active_count}/{len(CHANNEL_IDS)}`", "inline": True},
        {"name": "Message", "value": _get_active_message().split("\n")[0][:1024] or "—", "inline": False},
    ]
    if _last_error:
        fields.append({"name": "Latest issue", "value": _last_error[:1024], "inline": False})
    if warnings:
        fields.append({"name": "Warnings", "value": "\n".join(warnings)[:1024], "inline": False})
    # Include a compact, readable per-channel breakdown. The parser uses the
    # numeric ID in each field name to rebuild the live channel table.
    for cid, details in list(ch_data.items())[:15]:
        alive = "✅ alive" if details["alive"] else "❌ unavailable"
        ch_name = str(details["name"] or cid)[:60]
        last_post = int(details.get("last_post") or 0)
        last_label = f"<t:{last_post}:R>" if last_post > 0 else "never"
        fields.append({
            "name": f"Channel: {cid} · #{ch_name}"[:256],
            "value": (f"{alive} · sent `{details['sent']}` · errors `{details['errors']}` · "
                      f"slowmode `{details['slowmode']}s` · last {last_label}"),
            "inline": False,
        })
    embed = {
        "title": f"💓 Heartbeat · {ALT_NAME}",
        "color": {"active":0x57F287,"paused":0xFEE75C,"caution":0xFEE75C,
                  "ip_pause":0xED4245,"afk":0x5865F2,"stopped":0xED4245,
                  "error":0xED4245}.get(status,0x2F3136),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": f"alt_id={ALT_ID} · {VERSION} · updated {datetime.now().strftime('%H:%M:%S')}"},
        "fields": fields,
    }
    heartbeat_content = (
        f"💓 **Heartbeat** · `{status}` · mode `{_get_active_ad_type()}` · "
        f"sent `{total_sent}` · errors `{total_err}` · channels `{active_count}/{len(CHANNEL_IDS)}`"
    )
    def _send():
        global _dashboard_message_id
        payload = {"username": ALT_NAME[:80],
                   "content": heartbeat_content,
                   "allowed_mentions": {"parse": []},
                   "embeds": [embed]}
        try:
            wh_proxies = {"http": HTTPS_PROXY, "https": HTTPS_PROXY} if HTTPS_PROXY else None
            response = None
            if _dashboard_message_id:
                response = creq.patch(
                    f"{DASHBOARD_WEBHOOK_URL}/messages/{_dashboard_message_id}",
                    json=payload, impersonate=_BROWSER, timeout=WEBHOOK_TIMEOUT,
                    proxies=wh_proxies,
                )
                if response.status_code == 404:
                    _dashboard_message_id = ""
            if not _dashboard_message_id:
                response = creq.post(
                    DASHBOARD_WEBHOOK_URL + "?wait=true", json=payload,
                    impersonate=_BROWSER, timeout=WEBHOOK_TIMEOUT, proxies=wh_proxies,
                )
                if response.status_code in (200, 204):
                    try:
                        _dashboard_message_id = str(response.json().get("id") or "")
                    except Exception:
                        _dashboard_message_id = ""
            if response is not None and response.status_code not in (200, 204):
                dbg(f"[HEARTBEAT] webhook failed (HTTP {response.status_code})")
        except Exception as e:
            dbg(f"[HEARTBEAT] send failed: {type(e).__name__}: {e}")
    threading.Thread(target=_send, daemon=True).start()

def _controller_heartbeat_daemon():
    """Background: sync gist periodically and emit periodic heartbeats."""
    while not _stop_event.is_set() and not _panic_event.is_set():
        time.sleep(min(SYNC_GIST_INTERVAL_SEC, 45))
        if _stop_event.is_set() or _panic_event.is_set():
            return
        _sync_control_gist()
        if DASHBOARD_WEBHOOK_URL:
            try:
                _send_heartbeat(
                    _active_ch_ref or CHANNEL_IDS,
                    _ch_names_ref or {},
                    _slowmodes_ref or {},
                    _last_sent_ref or {},
                    _my_last_msg_id_ref or {},
                    _stats_ref or {},
                    total_sent=total_sent,
                    total_err=total_err,
                    total_skip=total_skip,
                    total_img=total_img,
                    total_edits=total_edits,
                    dead_channels=_dead_channels_ref,
                )
            except Exception as e:
                dbg(f"[HEARTBEAT-BG] background heartbeat error: {e}")


# --------------------------------------------------------------------------- #
# WebSocket gateway                                                           #
# --------------------------------------------------------------------------- #
class GatewayThread(threading.Thread):
    def __init__(self, token, status_text, status_emoji, log_fn, dbg_fn):
        super().__init__(daemon=True)
        self.token = token
        self.status_text = status_text
        self.status_emoji = status_emoji
        self._ws = None
        self._stop = threading.Event()
        self._hb_interval = 41250
        self._seq = None
        self._session_id = None
        self._resume_url = None
        self._last_hb_ack = time.time()
        self.connected = threading.Event()
        self.ready_received = False
        self.location_verify_failed = False
        self._identify_sent_at = 0.0
        self.log = log_fn
        self.dbg = dbg_fn

    def stop(self):
        self._stop.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception as _ignored_exc:
            print(f"[SENDER] stop: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)

    def _get_gateway_url(self):
        try:
            r = SESSION.get("https://discord.com/api/v9/gateway", timeout=10)
            if r.status_code == 200:
                return r.json().get("url", "wss://gateway.discord.gg")
        except Exception as _ignored_exc:
            print(f"[SENDER] _get_gateway_url: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
        return "wss://gateway.discord.gg"

    def _send(self, payload):
        if not self._ws:
            return False
        try:
            self._ws.send(json.dumps(payload))
            return True
        except Exception as e:
            self.dbg(f"WS send failed: {e}")
            return False

    def _identify(self):
        identify = {
            "op": 2,
            "d": {
                "token": self.token,
                "properties": {
                    "os": "Windows",
                    "browser": "Chrome",
                    "device": "",
                    "system_locale": DISCORD_LOCALE,
                    "browser_user_agent": _UA,
                    "browser_version": _CHROME_VER,
                    "os_version": "10",
                    "referrer": "",
                    "referring_domain": "",
                    "referrer_current": "",
                    "referring_domain_current": "",
                    "release_channel": "stable",
                    "client_build_number": CLIENT_BUILD,
                    "client_event_source": None,
                },
                "compress": False,
                "large_threshold": 50,
                "capabilities": 16381 | 32768 | 65536,
                "presence": {
                    "status": "online",
                    "since": 0,
                    "activities": [{
                        "type": 4,
                        "name": "Custom Status",
                        "state": self.status_text,
                        "emoji": ({"name": self.status_emoji} if self.status_emoji else None),
                    }] if self.status_text else [],
                    "afk": False,
                },
                "client_state": {
                    "guild_hashes": {},
                    "highest_last_message_id": "0",
                    "read_state_version": -1,
                    "user_guild_settings_version": -1,
                    "user_settings_version": -1,
                    "private_channels_version": "0",
                },
            },
        }
        self._identify_sent_at = time.time()
        self.ready_received = False
        self._send(identify)

    def _handle_dm(self, d):
        """Process an incoming DM MESSAGE_CREATE from the gateway.

        Runs on the gateway thread, so it MUST NOT do blocking REST calls
        that could outlast the heartbeat interval — missed heartbeats cause
        zombie sessions. If we need to resolve a new channel's type, we
        optimistically treat it as a DM (guild_id is already None which is
        the strong signal) and fetch metadata asynchronously.
        """
        try:
            ch = d.get("channel") or {}
            cid = d.get("channel_id")
            if not cid:
                return
            ctype = ch.get("type")
            if ctype is None:
                if cid in _dm_channel_cache:
                    ctype = _dm_channel_cache[cid].get("type")
                else:
                    # guild_id being absent is already the strongest signal
                    # this is a DM. Treat as type 1 now, fetch metadata async.
                    ctype = 1

                    def _bg_fetch():
                        try:
                            info = get_channel_info(cid)
                            if info:
                                _dm_channel_cache[cid] = info
                        except Exception as _ignored_exc:
                            print(f"[SENDER] _bg_fetch: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
                    threading.Thread(target=_bg_fetch, daemon=True).start()
            if ctype != 1:
                return
            if d.get("guild_id") is not None:
                return  # safety: not a DM
            author = d.get("author") or {}
            content = d.get("content") or ""
            attachments = d.get("attachments") or []
            is_me = (author.get("id") == _me_cache.get("id"))
            # V6: Incoming DM from the control bot — handle commands
            # (never forward these to the buyer-DM webhook)
            if not is_me and author.get("id") in CONTROLLER_USER_IDS:
                try:
                    handled = _handle_controller_dm(cid, author.get("id"), content)
                except Exception as e:
                    self.dbg(f"_handle_controller_dm error: {e}")
                    handled = False
                if handled:
                    # Still check for /panic overlap
                    try:
                        _handle_panic_dm(author.get("id"), content)
                    except Exception as _ignored_exc:
                        print(f"[SENDER] _handle_dm: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
                    return
            # Incoming DM from a buyer → pause public activity + forward
            if not is_me:
                snip = (content[:60].replace("\n", " ⏎ ") or "<embed/attachment>")
                buyer_name = author.get("username") or author.get("global_name") or "?"
                self.log(f"💌 📥 BUYER DM from @{buyer_name}: \"{snip}{'...' if len(content)>60 else ''}\"")
                self.log(f"   → Public posting paused {DM_PAUSE_MINUTES:.0f} min; forwarding to webhook; deep link: https://discord.com/channels/@me/{cid}")
                send_log_webhook(
                    f"📩 **DM** from @{buyer_name} (cid=`{cid}`) → PAUSE {DM_PAUSE_MINUTES:.0f}min"
                )
                extend_dm_pause()
                # F-32: check for /panic command from trusted users
                try:
                    _handle_panic_dm(author.get("id"), content)
                except Exception as _ignored_exc:
                    print(f"[SENDER] _handle_dm: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
                # Forward off-thread so webhook POST doesn't block heartbeats
                def _fwd():
                    try:
                        forward_dm_message(cid, author, content, attachments, is_me=False)
                    except Exception as _ignored_exc:
                        print(f"[SENDER] _fwd: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
                threading.Thread(target=_fwd, daemon=True).start()
            elif FORWARD_OWN_DMS:
                def _fwd_me():
                    try:
                        forward_dm_message(cid, author, content, attachments, is_me=True)
                    except Exception as _ignored_exc:
                        print(f"[SENDER] _fwd_me: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
                threading.Thread(target=_fwd_me, daemon=True).start()
        except Exception as e:
            self.dbg(f"_handle_dm error: {type(e).__name__}: {e}")

    def run(self):
        while not self._stop.is_set():
            try:
                self._connect_once()
            except Exception as e:
                self.dbg(f"WS connect error: {type(e).__name__}: {e}")
            if self._stop.is_set():
                break
            time.sleep(random.uniform(3, 7))
        self.log("🔌 WebSocket gateway stopped")

    def _connect_once(self):
        gw_url = self._get_gateway_url()
        url = f"{gw_url}/?v=9&encoding=json"
        self.dbg(f"WS connecting to {gw_url[:50]}...")
        ws_kwargs = {"timeout": 30}
        if HTTPS_PROXY:
            try:
                pu = urlparse(HTTPS_PROXY)
                host = pu.hostname
                port = pu.port or (443 if pu.scheme == "https" else 80)
                auth = None
                if pu.username:
                    auth = (pu.username, pu.password or "")
                ws_kwargs["http_proxy_host"] = host
                ws_kwargs["http_proxy_port"] = port
                if pu.scheme == "https":
                    ws_kwargs["proxy_type"] = "http"
                if auth:
                    ws_kwargs["http_proxy_auth"] = auth
                self.dbg(f"WS via proxy {host}:{port}")
            except Exception as e:
                self.dbg(f"WS proxy parse failed: {e}")
        cookie_str = "; ".join(
            f"{c.name}={c.value}" for c in SESSION.cookies.jar if c.domain and "discord" in c.domain
        )
        ws_kwargs["header"] = [
            f"User-Agent: {_UA}",
            "Origin: https://discord.com",
            f"Cookie: {cookie_str}",
        ]
        self._ws = _ws.create_connection(url, **ws_kwargs)
        # Timeout must exceed one heartbeat interval so a quiet connection
        # doesn't get torn down between heartbeats. We then use the timeout
        # to detect zombie sessions whose heartbeats are no longer ACK'd.
        self._ws.settimeout(self._hb_interval / 1000.0 + 15)

        hello = json.loads(self._ws.recv())
        if hello.get("op") != 10:
            raise RuntimeError(f"Expected HELLO, got op={hello.get('op')}")
        self._hb_interval = hello["d"].get("heartbeat_interval", 41250)
        self.dbg(f"WS hello: hb_interval={self._hb_interval}")

        self._identify()
        self._last_hb_ack = time.time()
        # Use short receive timeouts until READY arrives so the explicit
        # new-location gate is evaluated promptly. After READY, the timeout is
        # expanded to cover a normal Discord heartbeat interval.
        self._ws.settimeout(min(10.0, max(3.0, float(NEW_LOCATION_TIMEOUT_SEC) / 3.0)))

        hb_stop = threading.Event()
        def hb_runner():
            while not hb_stop.is_set() and not self._stop.is_set():
                time.sleep(self._hb_interval / 1000.0)
                if hb_stop.is_set():
                    break
                self._send({"op": 1, "d": self._seq})
                self.dbg(f"💓 WS heartbeat seq={self._seq}")
        hb_thread = threading.Thread(target=hb_runner, daemon=True)
        hb_thread.start()

        got_ready = False
        while not self._stop.is_set():
            try:
                raw = self._ws.recv()
            except _ws.WebSocketTimeoutException:
                # Before READY, enforce the explicit new-location timeout
                # rather than waiting for a full heartbeat interval.
                if not got_ready and self._identify_sent_at:
                    elapsed = time.time() - self._identify_sent_at
                    if elapsed >= NEW_LOCATION_TIMEOUT_SEC:
                        self.location_verify_failed = True
                        self.log(f"⚠️ Gateway never sent READY after IDENTIFY (waited {elapsed:.0f}s). Likely new-location verification.")
                        self._stop.set()
                        _new_location_failed_event.set()
                        break
                # After READY, no data within one HB interval + margin is
                # normal; only reconnect when heartbeat ACKs are also stale.
                if time.time() - self._last_hb_ack > self._hb_interval / 1000.0 * 2 + 10:
                    self.dbg("WS heartbeat ACK timed out — reconnecting")
                    break
                continue
            except Exception:
                break
            if not raw:
                break
            try:
                pkt = json.loads(raw)
            except Exception:
                continue
            op = pkt.get("op")
            t = pkt.get("t")
            d = pkt.get("d")
            s = pkt.get("s")
            if s is not None:
                self._seq = s

            if op == 11:
                self._last_hb_ack = time.time()
            elif op == 9:
                self.dbg("WS invalid session; will reconnect fresh")
                self._session_id = None
                break
            elif op == 7:
                self.dbg("WS requested reconnect")
                break
            elif t == "READY":
                self._session_id = d.get("session_id")
                self._resume_url = d.get("resume_gateway_url")
                self._ws.settimeout(self._hb_interval / 1000.0 + 15)
                # Cache private channels so we know DMs later
                pcs = d.get("private_channels", [])
                for pc in pcs:
                    _dm_channel_cache[pc["id"]] = pc
                if not got_ready:
                    got_ready = True
                    self.ready_received = True
                    self.connected.set()
                    user = d.get("user", {})
                    # Refresh our identity
                    _me_cache["id"] = user.get("id")
                    _me_cache["username"] = user.get("username")
                    _me_cache["global_name"] = user.get("global_name")
                    _me_cache["avatar"] = user.get("avatar")
                    _me_cache["discriminator"] = user.get("discriminator")
                    gateway_name = user.get("username") or user.get("global_name") or "?"
                    self.log(f"🟢 Gateway online as {gateway_name} "
                             f"(session {self._session_id[:8] if self._session_id else '?'})")
            elif t == "MESSAGE_CREATE":
                # Handle DMs (non-guild, type 1)
                if not d.get("guild_id"):
                    self._handle_dm(d)
            elif t == "CHANNEL_CREATE":
                # Track newly opened DMs
                try:
                    ctype = d.get("type")
                    cid = d.get("id")
                    if ctype == 1 and cid:
                        _dm_channel_cache[cid] = d
                except Exception as _ignored_exc:
                    print(f"[SENDER] _connect_once: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)

        hb_stop.set()
        try:
            self._ws.close()
        except Exception as _ignored_exc:
            print(f"[SENDER] _connect_once: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
        # F-29: If we sent IDENTIFY but never got READY, flag new-location gate.
        if not got_ready and self._identify_sent_at > 0:
            elapsed = time.time() - self._identify_sent_at
            if elapsed >= NEW_LOCATION_TIMEOUT_SEC:
                self.location_verify_failed = True
                self.log(f"⚠️ Gateway never sent READY after IDENTIFY (waited {elapsed:.0f}s). Likely new-location verification.")
                self._stop.set()
                _new_location_failed_event.set()
        if got_ready:
            self.dbg("WS disconnected; will reconnect")
        time.sleep(random.uniform(2, 5))

_gw_thread = None

def start_gateway():
    global _gw_thread
    if not ENABLE_GATEWAY:
        log("🌐 WebSocket gateway: DISABLED by ENABLE_GATEWAY=0 (account will appear offline — suspicious!)")
        return
    if not _HAS_WS:
        log("⚠️ websocket-client not installed; gateway disabled (account will appear offline). Install websocket-client for presence.")
        return
    log("🔌 Connecting to WebSocket gateway (wss://gateway.discord.gg)...")
    try:
        _gw_thread = GatewayThread(USER_TOKEN, CUSTOM_STATUS_TEXT, STATUS_EMOJI, log, dbg)
        _gw_thread.start()
        ready_wait = max(15, int(NEW_LOCATION_TIMEOUT_SEC))
        if _gw_thread.connected.wait(timeout=ready_wait):
            log("🟢 Gateway CONNECTED → account appears ONLINE with real-time presence + DM listening.")
            if DM_WEBHOOK_URL:
                log(f"💌 DM forwarding ENABLED (webhook set; public activity auto-pauses {DM_PAUSE_MINUTES:.0f} min on buyer DM)")
        elif (_new_location_failed_event.is_set()
              or _gw_thread.location_verify_failed
              or (_gw_thread._identify_sent_at > 0
                  and time.time() - _gw_thread._identify_sent_at >= NEW_LOCATION_TIMEOUT_SEC)):
            # The worker normally sets the event itself. The elapsed-time
            # fallback closes the boundary race where the wait above expires
            # at the same moment the worker is about to report the failure.
            _new_location_failed_event.set()
            log("")
            log("🛑 FATAL: Gateway READY never arrived despite valid auth.")
            log("   ⚠️  Discord is asking you to VERIFY A NEW LOCATION before this session can connect.")
            log("   → Log into the alt on a regular browser through the SAME WARP/PROXY IP,")
            log("     approve the 'New login location' challenge, then re-run the workflow.")
            log("     Posting now WILL shadowban the alt.")
            send_log_webhook("🛑 **NEW-LOCATION VERIFICATION REQUIRED** — aborting (gateway READY never arrived).")
            try:
                send_dashboard({
                    "title": "🛑 NEW-LOCATION VERIFICATION REQUIRED",
                    "color": 0xED4245,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "description": ("Gateway HELLO arrived but READY never did — Discord is challenging this "
                                   "session (new IP/location). **ACTION:** Log into the alt from a browser on "
                                   "the same WARP/proxy IP, approve the new-location email/modal, then re-run."),
                })
                time.sleep(2)
            except Exception as _ignored_exc:
                print(f"[SENDER] start_gateway: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
            sys.exit(2)
        else:
            log(f"⚠️ Gateway still connecting after {ready_wait}s — continuing in background. Presence may appear shortly.")
    except Exception as e:
        log(f"⚠️ Gateway thread failed to start: {type(e).__name__}: {e}")

# --------------------------------------------------------------------------- #
# Typing / sending / editing                                                  #
# --------------------------------------------------------------------------- #
def typing_duration(text):
    words = len(text.split())
    chars = len(text)
    lines = text.count("\n") + 1
    cpm = random.uniform(210, 350)
    d = (chars / cpm) * 60
    if d < 1.3:
        d = random.uniform(1.2, 2.2)
    if d > 9.0:
        d = random.uniform(6.5, 9.0)
    if lines > 2:
        d += random.uniform(1.0, 3.0)
    if random.random() < 0.15:
        d += random.uniform(1.0, 3.0)
    return d

def send_typing(cid, text):
    # Don't fire typing during a DM pause (we're supposed to be busy reading DMs).
    # Still sleep the full human-style duration so callers don't rush.
    if not public_activity_allowed():
        time.sleep(typing_duration(text) + random.uniform(1.8, 4.5))
        return
    try:
        # Pre-thinking pause: 1.8-4.5s gazing at the channel before typing,
        # plus a 5% chance of a longer "hesitation" pause (1-4s) like a human
        # who second-guesses their wording.
        pre_pause = random.uniform(1.8, 4.5)
        if random.random() < 0.05:
            pre_pause += random.uniform(1.0, 4.0)
        time.sleep(pre_pause)
        ref = f"https://discord.com/channels/{_guild_id_cache.get(cid,'@me')}/{cid}"
        api("POST", f"https://discord.com/api/v9/channels/{cid}/typing",
            referer=ref, json_body={}, retries=1)
    except Exception as _ignored_exc:
        print(f"[SENDER] send_typing: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
    # Small mid-typing hesitation 8% of the time (like pausing to think).
    dur = typing_duration(text)
    if random.random() < 0.08 and dur > 3:
        split = random.uniform(0.3, 0.7)
        time.sleep(dur * split)
        time.sleep(random.uniform(0.8, 2.5))
        time.sleep(dur * (1 - split))
    else:
        time.sleep(dur)

def _make_message_payload(text, nonce, with_image=False):
    payload = {
        "content": text,
        "tts": False,
        "nonce": nonce,
        "allowed_mentions": {
            "parse": ["users", "roles"],
            "replied_user": False,
        },
    }
    flags = 0
    if SUPPRESS_EMBEDS and random.random() < 0.4:
        flags |= 4
    if flags:
        payload["flags"] = flags
    return payload

def _build_multipart(payload_dict, fname, fbytes, fmime):
    mp = curl_cffi.CurlMime()
    mp.addpart(
        name="payload_json",
        content_type="application/json",
        data=json.dumps(payload_dict, separators=(",", ":")).encode(),
    )
    mp.addpart(
        name="files[0]",
        content_type=fmime,
        filename=fname,
        data=fbytes,
    )
    return mp

def send_message(cid, text, img=None):
    # Block any public posting during a DM pause
    if not public_activity_allowed():
        return False, 0, "paused for DM", None, None
    if DRY_RUN:
        sim_id = f"sim_{int(time.time()*1000)}"
        dbg(f"[SIMULATION] send_message cid={cid} text_len={len(text)} img={bool(img)} -> sim_id={sim_id}")
        log(f"   [SIMULATION] Message rendered successfully: \"{text[:50]}...\" (simulated post to {cid})")
        return True, 200, "", sim_id, {"id": sim_id, "content": text, "channel_id": cid}
    send_typing(cid, text)
    nonce = _make_nonce()
    ref = f"https://discord.com/channels/{_guild_id_cache.get(cid,'@me')}/{cid}"
    payload = _make_message_payload(text, nonce, with_image=bool(img))
    mp = None
    try:
        if img:
            fname, fbytes, fmime = img
            mp = _build_multipart(payload, fname, fbytes, fmime)
            # NOTE: retries=1 — CurlMime streams are consumed after the first
            # send attempt and cannot be safely replayed on 429/5xx. A single
            # attempt matches real browser upload behavior (browsers don't
            # transparently retry a multipart POST mid-stream).
            r = api("POST", f"https://discord.com/api/v9/channels/{cid}/messages",
                    referer=ref, files_mp=mp, retries=1)
        else:
            r = api("POST", f"https://discord.com/api/v9/channels/{cid}/messages",
                    referer=ref, json_body=payload, retries=3)
    finally:
        if mp is not None:
            try:
                mp.close()
            except Exception as _ignored_exc:
                print(f"[SENDER] send_message: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
    if r.status_code == 200:
        try:
            msg = r.json()
            # Schedule a verification in ~35s (auto-learn)
            mid = msg.get("id")
            if mid:
                _verify_message_alive(cid, mid, text)
            return True, 200, "", mid, msg
        except Exception:
            return True, 200, "", None, None
    try:
        err = r.json().get("message", getattr(r, "text", ""))[:120]
    except Exception:
        err = str(getattr(r, "status_code", "?"))
    # If AutoMod blocked us outright (403 with message about blocked content),
    # immediately strike+blacklist (no need to wait 35s).
    if r.status_code in (400, 403) and any(kw in (err or "").lower()
            for kw in ("blocked", "automod", "flagged", "not allowed")):
        _record_strike(text, cid, None)
    return False, r.status_code, err, None, None

def edit_message(cid, msg_id, new_text):
    if not public_activity_allowed():
        return False, 0, "paused for DM"
    if DRY_RUN:
        dbg(f"[SIMULATION] edit_message cid={cid} mid={msg_id}")
        return True, 200, ""
    if not public_activity_allowed():
        return False
    ref = f"https://discord.com/channels/{_guild_id_cache.get(cid,'@me')}/{cid}"
    payload = {"content": new_text}
    try:
        time.sleep(random.uniform(5, 22))
        api("POST", f"https://discord.com/api/v9/channels/{cid}/typing",
            referer=ref, json_body={}, retries=1)
        time.sleep(random.uniform(1.0, 2.5))
        r = api("PATCH", f"https://discord.com/api/v9/channels/{cid}/messages/{msg_id}",
                referer=ref, json_body=payload, retries=2)
        return r.status_code in (200, 204)
    except Exception:
        return False

def maybe_typo_edit(cid, msg_id, original_text):
    if not msg_id:
        return
    if not public_activity_allowed():
        return
    if random.random() > TYPO_EDIT_CHANCE:
        return
    if "\n" in original_text or len(original_text) < 8:
        return
    new_text = original_text
    # A physical-key neighbor is a more natural correction than a random
    # character replacement. Keep the typo edit itself governed by the single
    # TYPO_EDIT_CHANCE roll above.
    if random.random() < 0.60:
        qwerty_variant = _qwerty_typo(new_text)
        if qwerty_variant != new_text:
            new_text = qwerty_variant
    if random.random() < 0.35:
        if not new_text.endswith((".", "!", "?")) and random.random() < 0.5:
            new_text = new_text.rstrip() + "."
    if random.random() < 0.25 and "  " in new_text:
        new_text = new_text.replace("  ", " ", 1)
    swaps = [("DM", "dm"), ("dm", "DM"), ("LF", "lf"), ("lf", "LF"),
             ("QUICK", "quick"), ("quick", "QUICK")]
    if random.random() < 0.3:
        a, b = random.choice(swaps)
        if a in new_text:
            new_text = new_text.replace(a, b, 1)
    if random.random() < 0.25 and len(new_text) < DISCORD_MSG_LIMIT - 5:
        new_text = new_text.rstrip() + random.choice([" 🔥", " ⚡", " 💸", " ✅"])
    if new_text == original_text or len(new_text) > DISCORD_MSG_LIMIT:
        return
    def _do_edit():
        global total_edits
        ok = edit_message(cid, msg_id, new_text)
        if ok:
            with _state_lock:
                total_edits += 1
                if _stats_ref is not None:
                    _stats_ref[cid]["edits"] += 1
            snip = new_text.replace("\n", " ⏎ ")[:40]
            log(f"   ✏️  #{cid}: typo-edit applied → \"{snip}...\" (msg {msg_id})")
    t = threading.Thread(target=_do_edit, daemon=True)
    t.start()

# --------------------------------------------------------------------------- #
# Message variations                                                          #
# --------------------------------------------------------------------------- #
_EMOJIS = ["🔥", "💸", "⚡", "✅", "💰", "🤑", "📈", "💎", "🔔", "👀", "🏷️", "💯"]
_SUFFIXES = ["", " ✅", " ⚡", " 🔥", " dm fast", " online now ✅",
             " quick reply ⚡", " dm me", " hmu", " quick dm",
             " in server now", " reply fast", "", "", " rn"]
_PREFIXES = ["", "💸 ", "⚡ ", "🔥 ", "✅ ", "💰 ", "", ""]
_EXTRA_PHRASES = [
    "", "", "", "",
    " still going", " online rn", " prices firm",
    " quick trade", " no lowballs", " fast replies",
    " can do any amount", " hmu", " still buying",
    " still selling", " reply fast", " in server",
]
_TYPOS_FWD = [("you", "u"), ("please", "pls"), ("to", "t"), ("for", "fr"),
             ("are", "r"), ("your", "ur"), ("be", "b")]
_TYPOS_REV = [("u", "you"), ("pls", "please"), ("ur", "your")]
_QWERTY_NEIGHBORS = {
    "q": "wa", "w": "qase", "e": "wsdr", "r": "edft", "t": "rfgy",
    "y": "tugh", "u": "yhji", "i": "ujko", "o": "iklp", "p": "ol",
    "a": "qwsz", "s": "awedxz", "d": "serfcx", "f": "drtgvc",
    "g": "ftyhbv", "h": "gyujnb", "j": "huikmn", "k": "jiolm",
    "l": "kop", "z": "asx", "x": "zsdc", "c": "xdfv", "v": "cfgb",
    "b": "vghn", "n": "bhjm", "m": "njk",
}

def _qwerty_typo(text):
    """Replace one alphabetic character with a physical-key neighbor."""
    candidates = [(idx, char) for idx, char in enumerate(text)
                  if char.lower() in _QWERTY_NEIGHBORS]
    if not candidates:
        return text
    idx, char = random.choice(candidates)
    replacement = random.choice(_QWERTY_NEIGHBORS[char.lower()])
    if char.isupper():
        replacement = replacement.upper()
    return text[:idx] + replacement + text[idx + 1:]

def build_variations(base):
    is_multiline = "\n" in base.strip()
    out = set()
    if is_multiline:
        lines = base.split("\n")
        header = lines[0]
        rest = "\n".join(lines[1:]) if len(lines) > 1 else ""
        for pre in _PREFIXES + ["🤑 ", "📈 ", ""]:
            for suf in _SUFFIXES[:7]:
                h = f"{pre}{header}{suf}".strip()
                c = h + ("\n" + rest if rest else "")
                if len(c) <= DISCORD_MSG_LIMIT:
                    out.add(c)
        for _ in range(12):
            e = random.choice(_EMOJIS)
            s = random.choice(_SUFFIXES)
            h = f"{e} {header} {s}".strip()
            c = h + ("\n" + rest if rest else "")
            if len(c) <= DISCORD_MSG_LIMIT:
                out.add(c)
    else:
        for pre in _PREFIXES:
            for suf in _SUFFIXES:
                v = f"{pre}{base}{suf}".strip()
                if len(v) <= DISCORD_MSG_LIMIT:
                    out.add(v)
        for _ in range(35):
            e1 = random.choice(_EMOJIS + ["", "", "", ""])
            extra = random.choice(_EXTRA_PHRASES)
            suf = random.choice(_SUFFIXES)
            parts = [e1, base] if e1 else [base]
            if extra:
                parts.append(extra)
            if suf:
                parts.append(suf)
            v = " ".join(parts).replace("  ", " ").strip()
            if random.random() < 0.18:
                v = v.lower()
            if random.random() < 0.08:
                a, b = random.choice(_TYPOS_FWD)
                if a in v and len(v) > 5:
                    v = v.replace(a, b, 1)
            if random.random() < 0.04:
                a, b = random.choice(_TYPOS_REV)
                if a in v:
                    v = v.replace(a, b, 1)
            if len(v) <= DISCORD_MSG_LIMIT:
                out.add(v)
        for _ in range(6):
            suf = random.choice(_SUFFIXES)
            extra = random.choice(_EXTRA_PHRASES[:5])
            parts = [base]
            if extra:
                parts.append(extra)
            if suf:
                parts.append(suf)
            v = " ".join(parts).replace("  ", " ").strip()
            if len(v) <= DISCORD_MSG_LIMIT:
                out.add(v)
    # Ensure the variation family contains a real keyboard-neighbor typo,
    # rather than relying only on the probabilistic generation loop.
    if not is_multiline:
        qwerty_variant = _qwerty_typo(base)
        if qwerty_variant != base and len(qwerty_variant) <= DISCORD_MSG_LIMIT:
            out.add(qwerty_variant)
    uniq = [v for v in out if len(v) <= DISCORD_MSG_LIMIT]
    if base in uniq:
        uniq.remove(base)
    uniq.insert(0, base)
    return uniq

# --------------------------------------------------------------------------- #
# Image loading                                                               #
# --------------------------------------------------------------------------- #
def load_image():
    if not IMAGE_PATH or not ATTACH_IMAGE:
        log("🖼️  Image: DISABLED (no IMAGE_PATH set or ATTACH_IMAGE=0) — text-only mode.")
        return None, None
    p = Path(IMAGE_PATH).expanduser()
    if not p.exists():
        log(f"⚠️ IMAGE: file not found at {IMAGE_PATH} — falling back to text-only. Check IMAGE_PATH in workflow inputs.")
        return None, None
    try:
        data = p.read_bytes()
        mime = mimetypes.guess_type(str(p))[0] or "image/png"
        size_mb = len(data) / 1024 / 1024
        if size_mb > 8:
            log(f"⚠️ IMAGE: {p.name} is {size_mb:.2f}MB (exceeds Discord's 8MB limit) — falling back to text-only. Compress the image.")
            return None, None
        log(f"🖼️  IMAGE loaded: {p.name} ({size_mb:.2f}MB, {mime})")
        if _HAS_PIL and STRIP_EXIF:
            log("   Anti-fingerprinting: EXIF stripped, filename + JPEG bytes randomized per post, ±1px RGB jitter.")
        else:
            log("   ⚠️ EXIF strip / jitter disabled (STRIP_EXIF=0 or Pillow missing) — image metadata may be identifiable.")
        return data, p.name
    except Exception as e:
        log(f"⚠️ Failed to read image: {type(e).__name__}: {e} — falling back to text-only.")
        return None, None

def make_image_payload(raw_bytes, original_name):
    return _process_image(raw_bytes, original_name)

# --------------------------------------------------------------------------- #
# AFK planner / keepalive sleep                                               #
# --------------------------------------------------------------------------- #
def plan_breaks(run_seconds):
    n = random.randint(MIN_AFK_BREAKS, MAX_AFK_BREAKS)
    if n <= 0:
        return []
    out = []
    min_start = 20 * 60
    margin_end = AFK_MAX_MIN * 60
    gap = 15 * 60
    usable = run_seconds - margin_end
    if usable < min_start + AFK_MIN_MIN * 60:
        return []
    for _ in range(n):
        for _attempt in range(100):
            bs = time.time() + random.uniform(min_start, max(min_start + 60, usable))
            bd = random.uniform(AFK_MIN_MIN, AFK_MAX_MIN) * 60
            be = bs + bd
            ok = all(be + gap < es or bs > ee + gap for es, ee in out)
            if ok:
                out.append((bs, be))
                break
    out.sort(key=lambda x: x[0])
    return out

def in_break(breaks, now):
    for s, e in breaks:
        if s <= now < e:
            return True, e - now
    return False, 0

class _KeepaliveSleep:
    def __init__(self):
        self.last_ping = time.time()
        self.last_gist_poll = time.time()

    def sleep(self, seconds, end_time=None):
        if seconds <= 0:
            return
        stop = time.time() + seconds
        while time.time() < stop:
            if _panic_event.is_set() or _stop_event.is_set():
                return
            if end_time and time.time() >= end_time:
                return
            chunk = min(15, max(1, stop - time.time()))
            time.sleep(chunk)
            if CONTROL_GIST_ID and GIST_TOKEN and time.time() - self.last_gist_poll >= 15:
                try:
                    _sync_control_gist()
                except Exception as _ignored_exc:
                    print(f"[SENDER] sleep: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
                self.last_gist_poll = time.time()
            if public_activity_allowed() and time.time() - self.last_ping >= 270:
                keepalive()
                self.last_ping = time.time()

_ksleeper = None
def sleep_with_keepalive(seconds, end_time=None):
    global _ksleeper
    if _ksleeper is None:
        _ksleeper = _KeepaliveSleep()
    _ksleeper.sleep(seconds, end_time)

# --------------------------------------------------------------------------- #
# Random reactions                                                            #
# --------------------------------------------------------------------------- #
_REACT_EMOJIS = ["🔥", "💯", "👀", "✅", "👌", "💸", "🤑", "💎"]

def maybe_react(cid, msgs, my_id):
    if not RANDOM_REACT:
        return
    if not public_activity_allowed():
        return
    if random.random() > IDLE_REACT_CHANCE:
        return
    if not msgs:
        return
    candidates = [m for m in msgs[:8]
                  if m.get("author", {}).get("id") != my_id
                  and (m.get("content") or "").strip()
                  and not (m.get("content") or "").strip().startswith("!")]
    if not candidates:
        return
    m = random.choice(candidates)
    emo = random.choice(_REACT_EMOJIS)
    import urllib.parse
    eurl = urllib.parse.quote(emo, safe="")
    url = (f"https://discord.com/api/v9/channels/{cid}/messages/"
           f"{m['id']}/reactions/{eurl}/@me")
    ref = f"https://discord.com/channels/{_guild_id_cache.get(cid,'@me')}/{cid}"
    try:
        r = api("PUT", url, referer=ref, json_body={}, retries=1)
        if r.status_code in (204, 200):
            snip = (m.get("content") or "").replace("\n", " ")[:25]
            log(f"   👌 #{cid}: reacted {emo} to recent msg → \"{snip}...\"")
    except Exception as _ignored_exc:
        print(f"[SENDER] maybe_react: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)

# --------------------------------------------------------------------------- #
# IP + country check                                                          #
# --------------------------------------------------------------------------- #
def check_proxy_ip():
    if not PROXY_CHECK:
        log("🌐 PROXY_CHECK=0 would disable verified egress checks; refusing to start.")
        sys.exit(2)
    log("🌐 Checking outbound IP and ISP/org (verifying WARP/proxy is masking datacenter IP)...")
    try:
        details = _lookup_egress()
        ip = details["ip"]
        org = details["org"]
        country = details["country"]
        country_name = details["country_name"]
        log(f"🌐 OUTBOUND IP: {ip}  |  ISP/ORG: {org}  |  COUNTRY: {country or '?'}")
        if not details["verified"]:
            log("   ❌ Independent egress/provider verification failed; refusing to continue without a verified route.")
            sys.exit(2)
        if ALLOWED_COUNTRIES and (not country or country not in ALLOWED_COUNTRIES):
            log(f"   ❌ GEO CHECK FAILED: IP is in {country_name} ({country}), which is NOT in ALLOWED_COUNTRIES.")
            log(f"   → Add '{country}' to the ALLOWED_COUNTRIES secret or retry for a new WARP IP. Aborting.")
            sys.exit(2)
        o = str(org).lower()
        is_cloudflare = "cloudflare" in o or "as13335" in o
        if details["hosting"] and not is_cloudflare:
            log("   ❌ Independent provider classified this egress as hosting/datacenter; refusing to start public activity.")
            log("      Use Cloudflare WARP or a verified residential HTTPS_PROXY and retry.")
            sys.exit(2)
        if any(kw in o for kw in _DATACENTER_KWS):
            log("   ❌ DATACENTER IP DETECTED! Refusing to start public activity.")
            log("      Use Cloudflare WARP or a verified residential HTTPS_PROXY and retry.")
            sys.exit(2)
        if is_cloudflare:
            log("   ℹ️  Cloudflare/WARP detected — traffic exits via Cloudflare (not Azure/datacenter).")
            log("      Note: WARP is VPN-class, not residential. Some strict servers may flag new accounts.")
            log("      Recommendation: text-only for the first ~10 posts, then enable images.")
        else:
            log("   ✅ Independent IP provider verified a non-datacenter egress.")
    except Exception as e:
        log(f"   ❌ IP check failed ({type(e).__name__}: {e}) — refusing to continue without verified egress.")
        sys.exit(2)

# --------------------------------------------------------------------------- #
# Self-tests                                                                  #
# --------------------------------------------------------------------------- #
def self_test():
    print("=" * 60)
    print(f"🧪 Self-test ({VERSION}, no network calls)")
    print("=" * 60)

    vs = build_variations("SELLING STOCK LF 2.5$/1K DM ME QUICK CAN DO SMALL AND BIG AMOUNTS")
    assert len(vs) >= 40, f"sell variations: {len(vs)}"
    assert len(set(vs)) == len(vs), "dupes"
    for v in vs:
        assert len(v) <= DISCORD_MSG_LIMIT
    print(f"✅ Sell variations: {len(vs)} unique")

    vb = build_variations(
        f"BUYING {DEFAULT_ITEM_NAME.upper()}:\n\n-TOKENS 2.2/1K\n\n-RAP 1.8$/1K (nlf boosted)\n\nDM me quick")
    assert len(vb) >= 6, f"buy variations: {len(vb)}"
    print(f"✅ Buy variations: {len(vb)} unique")

    assert typing_duration("hi") < typing_duration("x" * 200)
    print("✅ Typing duration scales with length")

    mp = _build_multipart({"content": "hi", "nonce": "123", "tts": False},
                          "test.png", b"PNGDATA"*10, "image/png")
    assert mp is not None
    mp.close()
    print("✅ Multipart CurlMime construction OK")

    import unittest.mock as mock
    overlaps = 0
    for seed in range(1000):
        random.seed(seed)
        with mock.patch("time.time", return_value=1_000_000):
            br = plan_breaks(6 * 3600)
        for i in range(len(br) - 1):
            if br[i][1] + 15*60 > br[i+1][0]:
                overlaps += 1
    assert overlaps == 0
    print("✅ AFK planner: zero overlaps across 1000 seeds")

    with mock.patch("time.time", return_value=1_000_000):
        assert plan_breaks(15 * 60) == []
    print("✅ AFK planner: short runs return []")

    fn, d, m = _process_image(b"rawbytes", "ad.png")
    assert fn.endswith((".png",".jpg",".jpeg",".webp",".gif"))
    assert m.startswith("image/")
    print(f"✅ Image processing: random filename {fn}")

    nonce = _make_nonce()
    assert nonce.isdigit() and len(nonce) >= 17
    print(f"✅ Message nonce looks like snowflake ({nonce[:10]}...)")

    payload = _make_message_payload("test message", nonce)
    assert payload["nonce"] == nonce
    assert "everyone" not in payload["allowed_mentions"]["parse"]
    print("✅ allowed_mentions blocks @everyone/@here pings")

    # V6: DM pause mechanics
    global _public_pause_until, _consecutive_deletions
    with _state_lock:
        saved = _public_pause_until
        _public_pause_until = time.time() + 60
    assert not public_activity_allowed(), "public pause should block"
    extend_dm_pause()
    with _state_lock:
        assert _public_pause_until > time.time() + 60, "extend_dm_pause should extend"
        _public_pause_until = 0
    assert public_activity_allowed()
    with _state_lock:
        _public_pause_until = saved
    print("✅ DM public-pause mechanics work")

    # V6: strike/blacklist and positive survival learning
    with _state_lock:
        before = len(_blocked_variations)
        _consecutive_deletions = 0
        _variation_scores.clear()
    _record_strike("__test_variation__", "0", "0")
    _record_strike("__test_variation__", "0", "0")  # 2nd strike = blacklist
    with _state_lock:
        assert "__test_variation__" in _blocked_variations
        _blocked_variations.discard("__test_variation__")
        _strikes.pop("__test_variation__", None)
        _consecutive_deletions = 0
    _record_success("__test_survivor__", "0", "0")
    with _state_lock:
        assert _variation_scores["__test_survivor__"] == 1
        assert _consecutive_deletions == 0
        _variation_scores.pop("__test_survivor__", None)
    print("✅ Strike/blacklist and positive survival learning logic work")

    # V6: webhook payload builder (avatar URLs)
    class _FakeUser(dict): pass
    fu = _FakeUser(id="123", avatar="abc123", username="tester", discriminator="0001")
    av = _avatar_url(fu)
    assert "cdn.discordapp.com/avatars/123/abc123" in av
    print("✅ CDN avatar URL construction OK")

    print()
    print("=" * 60)
    print(f"🎉 ALL SELF-TESTS PASSED ({VERSION})")
    print("=" * 60)

# --------------------------------------------------------------------------- #
# Main loop                                                                   #
# --------------------------------------------------------------------------- #
def main():
    global CHANNEL_IDS
    global _ksleeper, _gw_thread
    global total_sent, total_err, total_skip, total_img, total_edits, total_distractions
    global _run_start_epoch, _runtime_run_end, _runtime_hours, _runtime_message, _runtime_rate, _runtime_ad_type
    global _active_ch_ref, _ch_names_ref, _slowmodes_ref, _last_sent_ref, _my_last_msg_id_ref, _stats_ref
    global _next_post_ref, _dead_channels_ref
    global _last_variation_base, _variations_cache
    _ksleeper = _KeepaliveSleep()

    # Reset runtime counters at start of run
    total_sent = total_err = total_skip = total_img = total_edits = 0
    total_distractions = 0
    _runtime_hours = 0
    _runtime_message = None
    _runtime_rate = None
    _runtime_ad_type = None

    # Verify the route before any Discord warmup request can expose the runner
    # IP. The same configured SESSION is used for this check and all later REST
    # and webhook traffic.
    check_proxy_ip()
    _warmup_fingerprint()

    start = time.time()
    if RUNTIME_LIMITLESS:
        # Limitless runs until /shutdown, !stop, or a panic event. Use a very
        # large local wall-clock only for one-shot startup sleeps; the run end
        # used throughout the scheduler is infinity so it never expires.
        run_end = start + INFINITE_AFK_PLAN_SEC
        _runtime_run_end = float("inf")
        _runtime_hours = 0
        log(f"♾️  RUNTIME       : LIMITLESS until controller shutdown")
    else:
        run_end = start + TOTAL_RUN_MIN * 60
        _runtime_run_end = run_end
        _runtime_hours = max(1, round(TOTAL_RUN_MIN / 60))
    variations = build_variations(MESSAGE)
    _last_variation_base = MESSAGE
    _variations_cache = variations
    raw_image, image_name = load_image()
    use_img_ever = bool(raw_image) and ATTACH_IMAGE

    # Load persisted blocklist (if gist configured)
    load_blocked_from_gist()
    with _state_lock:
        if _blocked_variations:
            # Filter out blacklisted variations from our working list
            before = len(variations)
            variations = [v for v in variations if v not in _blocked_variations]
            log(f"🧠 Auto-learn: filtered out {before - len(variations)} previously-blocked variations "
                f"({len(variations)} usable remain)")
            if not variations:
                # If every variation was blacklisted, rebuild from scratch but warn
                log("⚠️ All base variations were blocked — resetting blocklist for THIS RUN only.")
                log("   This is a bad sign: prior runs had every message variant deleted. Consider fresh IP/copy.")
                _blocked_variations.clear()
                variations = build_variations(MESSAGE)

    last_sent = {}
    slowmodes = {}
    ch_names = {}
    channel_errors = defaultdict(int)
    dead_channels = set()
    stats = defaultdict(lambda: {"sent": 0, "errors": 0, "skipped": 0,
                                 "cooldown": 0, "img": 0, "txt": 0, "edits": 0})
    total_sent = total_err = total_skip = total_distractions = 0
    total_img = total_edits = 0
    cycle = 0
    sent_count_global = 0
    last_gist_save = 0
    returning_from_afk = False
    in_afk_logged = False

    # V6: publish mutable refs for the heartbeat/controller thread
    # (set properly after channel browse; placeholder for now)

    log("=" * 66)
    log(f"🎯 MARKETPLACE AD SENDER  {VERSION}  |  MODE: {AD_TYPE.upper()}")
    log("=" * 66)
    log(f"📌 CHANNELS ({len(CHANNEL_IDS)}):")
    for c in CHANNEL_IDS:
        log(f"   • {c}")
    log(f"⏱️  INTERVAL      : ~{INTERVAL_MIN} min/channel (±30-45% jitter, bursty cadence)")
    if RUNTIME_LIMITLESS:
        log("⌛ RUNTIME       : LIMITLESS → stops only on /shutdown / !stop / panic")
    else:
        log(f"⌛ RUNTIME       : {TOTAL_RUN_MIN:.0f} min ({TOTAL_RUN_MIN/60:.1f}h) → ends at {datetime.fromtimestamp(run_end).strftime('%Y-%m-%d %H:%M:%S')}")
    first_line = MESSAGE.split('\n')[0]
    log(f"📝 BASE MESSAGE  : \"{first_line[:70]}{'...' if len(first_line)>70 else ''}\"")
    log(f"   Variations    : {len(variations)} unique message variants generated ({len(MESSAGE)} chars base)")
    log(f"🖼️  IMAGE         : {'YES' if use_img_ever else 'NO (text-only)'}"
        + (f" (text-only warmup: first {WARMUP_POSTS} posts, then 100% attach)" if use_img_ever else ""))
    log(f"🗑️  AUTO-DELETE   : OFF — messages stack naturally (no self-delete)")
    log(f"🧠 SMART COOLDOWN: ON — only repost after someone else posts after us")
    log(f"🔌 WS GATEWAY    : {'ON (account appears ONLINE, real presence)' if ENABLE_GATEWAY and _HAS_WS else 'OFF (suspicious! — account looks offline)'}")
    log(f"✏️  TYPO EDITS    : {TYPO_EDIT_CHANCE*100:.0f}% chance after post (5-22s delay, natural correction)")
    log(f"👌 REACTIONS     : {'ON' if RANDOM_REACT else 'OFF'} (~{IDLE_REACT_CHANCE*100:.0f}% chance per cooldown read)")
    log(f"☕ AFK BREAKS     : {MIN_AFK_BREAKS}-{MAX_AFK_BREAKS} per 6h chunk, {AFK_MIN_MIN:.0f}-{AFK_MAX_MIN:.0f} min each")
    log(f"🔒 TLS/HTTP2     : curl_cffi impersonating Chrome (real JA3/HTTP2 fingerprint)")
    log(f"💌 DM FORWARDING : {'ON' if DM_WEBHOOK_URL else 'OFF'}"
        + (f" (auto-pause public activity {DM_PAUSE_MINUTES:.0f} min when buyer DMs)" if DM_WEBHOOK_URL else ""))
    log(f"📋 LOG WEBHOOK   : {'ON' if LOG_WEBHOOK_URL else 'OFF (optional action-log channel)'}")
    log(f"📊 DASHBOARD    : {'ON (periodic summaries)' if DASHBOARD_WEBHOOK_URL else 'OFF (optional)'}")
    log(f"🔥 DEAL ALERTS    : {'ON → separate deals webhook' if DEAL_WEBHOOK_URL else 'OFF (no DEAL_WEBHOOK_URL)'}", kind="DEAL")
    log(f"🎯 DEAL ITEMS     : {', '.join(_get_active_deal_keywords()) or 'NONE — no item can match'}", kind="DEAL")
    log(f"🎚️  DEAL FILTER    : {'ON' if _get_active_deal_scan_enabled() else 'OFF'} · minimum edge ${_get_active_deal_delta():.2f}/1k", kind="DEAL")
    log(f"⏱️  WEBHOOK T/O   : {WEBHOOK_TIMEOUT}s (control); {DM_WEBHOOK_TIMEOUT}s (DM forward)")
    log(f"🧠 AUTO-LEARN    : strikes={BLOCKED_STRIKES}, safety_stop={BLOCKED_SAFETY_STOP}"
        + (f", gist={GIST_ID[:8]}... (persisted across runs)" if GIST_ID else " (no gist persistence — resets each run)"))
    if ALLOWED_COUNTRIES:
        log(f"🌍 GEO CHECK     : ALLOWED_COUNTRIES = {','.join(ALLOWED_COUNTRIES)} (abort if WARP routes elsewhere)")
    log(f"🐛 DEBUG LOGS    : {'ON (verbose)' if DEBUG else 'OFF'}")
    if HTTPS_PROXY:
        log(f"🔗 PROXY         : ON (HTTPS_PROXY set, credentials hidden)")
    else:
        log(f"🔗 PROXY         : OFF (Cloudflare WARP will be used on GHA cloud)")
    log(f"🛰️ IP MONITOR    : ON (every {IP_HEALTH_CHECK_INTERVAL_MIN:.0f} min; {IP_HEALTH_PAUSE_MIN:.0f} min pause on datacenter)")
    log(f"🚦 RATE LIMITER  : {'ON' if RATELIMIT_PREADJUST else 'OFF'} (proactive 429 avoidance)")
    if _per_jitter_applied and ALT_ID >= 1:
        log(f"🎭 PERSONALITY   : alt{ALT_ID} per-alt jitter ±{PERSONALITY_JITTER*100:.0f}% applied → "
            + f"typo={TYPO_EDIT_CHANCE*100:.0f}% react={IDLE_REACT_CHANCE*100:.0f}% "
            + f"afk={MIN_AFK_BREAKS}-{MAX_AFK_BREAKS}×{AFK_MIN_MIN:.0f}-{AFK_MAX_MIN:.0f}m "
            + f"(set PERSONALITY_JITTER=0 to disable)")
    log(f"🚨 CAUTION MODE  : ON (window={CAUTION_WINDOW}, fail≥{CAUTION_FAIL_THRESHOLD}→{CAUTION_INTERVAL_MULT:.1f}x; exit after {CAUTION_EXIT_STREAK} survives)")
    _panic_dm_note = f"; /panic DM from {len(PANIC_TRUSTED_IDS)} trusted ID(s)" if PANIC_TRUSTED_IDS else "; no DM triggers"
    log(f"🛑 PANIC STOP    : ON (gist every {PANIC_CHECK_INTERVAL_SEC:.0f}s{_panic_dm_note})")
    if DEAL_SCAN_ENABLED:
        _deal_dir = {"sell": "high-buy offers", "buy": "cheap sells", "?": "off"}.get(AD_TYPE, "off")
        log(f"📡 DEAL SCANNER  : ON → alert on {_deal_dir} (passive; zero extra API calls)")
    else:
        log(f"📡 DEAL SCANNER  : OFF")
    if CHANNEL_NAMES:
        _disc_note = (f"{len(CHANNEL_NAMES)} name(s) mapped; confirmation from "
                      f"{len(CONFIRM_USER_IDS)} trusted user(s)" if CONFIRM_USER_IDS
                      else f"{len(CHANNEL_NAMES)} name(s) mapped; confirmation disabled until CONFIRM_USER_IDS is set")
        log(f"🔄 AUTO-DISCOVER : ON → {_disc_note}; timeout {CONFIRM_TIMEOUT}s")
    else:
        log(f"🔄 AUTO-DISCOVER : OFF (set CHANNEL_NAMES to enable auto-recovery on 404)")
    log("=" * 66)

    startup_phase1 = random.uniform(8, 20)
    log(f"⏳ Simulated app launch: {startup_phase1:.0f}s boot delay (simulating opening Discord app)...")
    sleep_chunked(startup_phase1, run_end)

    me, vreason = validate_token()
    if not me:
        log(f"❌ AUTH FAILED — could not authenticate (reason: {vreason}). Aborting.")
        log("   → Double-check USER_TOKEN secret. If the account was shadowbanned in a previous session, the token may still be valid but the session flagged.")
        _print_stats(start, total_sent, total_err, total_skip,
                     total_distractions, total_img, total_edits, stats)
        sys.exit(1)
    my_id = me.get("id")
    username = me.get("username") or me.get("global_name") or "unknown"
    if not my_id:
        log("❌ AUTH ERROR: Could not read user id from /users/@me response (malformed response?). Aborting.")
        sys.exit(1)
    log(f"✅ AUTH OK → Logged in as {username}")
    log(f"   USER ID       : {my_id}")
    verified = me.get('verified', False)
    mfa = me.get('mfa_enabled', False)
    log(f"   EMAIL VERIFIED: {'✅ YES' if verified else '❌ NO — higher flag risk! Verify email before long runs.'}")
    log(f"   2FA ENABLED   : {'✅ YES' if mfa else '⚠️ NO — tip: enabling 2FA raises account trust score.'}")
    _load_channel_registry_remote()

    if not _resolve_channel_keywords():
        log("❌ No usable channel targets remain after safe resolution. Aborting.")
        sys.exit(1)
    start_gateway()
    time.sleep(random.uniform(2, 5))
    set_status()

    # V6: start background safety daemons
    _start_ip_health_monitor()
    _start_panic_checker()

    # V6: start the control-gist sync daemon (polls for remote config changes)
    if CONTROL_GIST_ID and GIST_TOKEN:
        threading.Thread(target=_controller_heartbeat_daemon, daemon=True, name="ctrl-sync").start()
        log(f"🎛️  Remote control: control-Gist queue + DM fallback; polling every {SYNC_GIST_INTERVAL_SEC}s (no alt server membership required).")
    elif CONTROLLER_USER_IDS:
        threading.Thread(target=_controller_heartbeat_daemon, daemon=True, name="ctrl-sync").start()
        log(f"🎛️  Remote control: DM commands ENABLED ({len(CONTROLLER_USER_IDS)} controller id(s)); no control gist.")
    else:
        log(f"🎛️  Remote control: OFF (no CONTROLLER_USER_IDS).")
    if DASHBOARD_WEBHOOK_URL and HEARTBEAT_INTERVAL_SEC:
        log(f"💓 Heartbeat      : ON → pushes JSON+embed status every {HEARTBEAT_INTERVAL_SEC}s to dashboard webhook ({ALT_NAME} id={ALT_ID}).")
    elif DASHBOARD_WEBHOOK_URL:
        log(f"💓 Heartbeat      : ON (legacy periodic summaries; no DASHBOARD_WEBHOOK V6 structured format).")
    else:
        log(f"💓 Heartbeat      : OFF (no DASHBOARD_WEBHOOK_URL set).")
    # Mark run start
    _run_start_epoch = time.time()

    log("📡 Browsing channels (warmup reads — simulating opening each channel before posting)...")
    ok_count = 0
    # Iterate over a copy because try_channel_discovery may mutate CHANNEL_IDS
    for cid in list(CHANNEL_IDS):
        log(f"📥 Fetching channel info for {cid}...")
        # Fetch via api() so we get 429/5xx handling; then inspect status code
        probe = api("GET", f"https://discord.com/api/v9/channels/{cid}",
                    referer=f"https://discord.com/channels/@me/{cid}", retries=2)
        info = None
        fetch_code = probe.status_code
        if fetch_code == 200:
            try:
                info = probe.json()
                gid = info.get("guild_id")
                if gid:
                    _guild_id_cache[cid] = gid
                    _channel_id_to_guild[cid] = gid
            except Exception:
                info = None
        if not info:
            if fetch_code == 404:
                # Discovery is deliberately deferred until a real posting
                # attempt receives 404. Keep this configured ID in the active
                # scheduler so that the first POST is the event that can
                # trigger discovery; startup probing must never prompt an
                # operator or mutate the configured channel set.
                log(f"   ⚠️ CHANNEL {cid}: startup probe returned HTTP 404 — retaining it for a posting-time 404/discovery check.")
                ch_names.setdefault(cid, _CHANNEL_NAME_BY_ID.get(cid, cid))
                slowmodes.setdefault(cid, 0)
                sleep_chunked(random.uniform(2.0, 4.0))
                continue
            else:
                log(f"   ❌ CHANNEL {cid}: could not fetch info (HTTP {fetch_code}). Alt may not be in the server or ID is wrong. Skipping this channel.")
                dead_channels.add(cid)
                sleep_chunked(random.uniform(2.0, 4.0))
                continue
        name = info.get("name", "?")
        slowmodes[cid] = info.get("rate_limit_per_user", 0)
        ch_names[cid] = name
        gid = _guild_id_cache.get(cid,'?')
        log(f"   ✅ CHANNEL → #{name} (id={cid}) in GUILD {gid} | slowmode = {slowmodes[cid]}s")
        sleep_chunked(random.uniform(0.8, 1.8))
        if public_activity_allowed():
            log(f"   👁️  Reading channel #{name} (recent messages, marking as read)...")
            ch_msgs = read_channel(cid)
            if ch_msgs:
                last_snip = (ch_msgs[0].get("content") or "").replace("\n", " ")[:40] or "<embed/image/empty>"
                log(f"      ✅ Ack sent. Last msg visible: \"{last_snip}...\"")
        gaze = random.uniform(3.0, 9.0)
        log(f"   👀 Gazing at #{name} for {gaze:.0f}s (simulating reading chat before moving on)...")
        sleep_chunked(gaze, run_end)
        ok_count += 1

    active_channels = [c for c in CHANNEL_IDS if c not in dead_channels]
    # V6: publish refs for heartbeat/controller
    _active_ch_ref = active_channels
    _ch_names_ref = ch_names
    _slowmodes_ref = slowmodes
    _last_sent_ref = last_sent
    _my_last_msg_id_ref = my_last_msg_id
    _stats_ref = stats
    _dead_channels_ref = dead_channels
    # Reconcile the complete authenticated server inventory after the initial
    # probes. This persists every eligible channel, recovers deleted/recreated
    # target IDs by name, and logs exact additions/removals without waiting for
    # a posting failure.
    registry_result = _reconcile_channel_registry("startup")
    if registry_result.get("ok"):
        active_channels = list(_active_ch_ref)
        dead_channels = _dead_channels_ref
    if ok_count == 0 and not active_channels:
        log("❌ FATAL: No accessible channels. Verify the alt is in the servers and CHANNEL_IDS are correct. Aborting.")
        sys.exit(1)
    if dead_channels:
        log(f"⚠️  {len(dead_channels)}/{len(CHANNEL_IDS)} channels INACCESSIBLE and will be skipped for this run.")
    warmup_wait = random.uniform(40, 90)
    log(f"👀 Reading chat across accessible channels for {warmup_wait:.0f}s before first post (simulating scrolling/reading)...")
    sleep_chunked(warmup_wait, run_end)
    for cid in active_channels:
        if public_activity_allowed():
            read_channel(cid)
        sleep_chunked(random.uniform(2.0, 6.0))
    final_wait = random.uniform(8, 20)
    log(f"⌛ Final pre-post pause {final_wait:.0f}s...")
    sleep_chunked(final_wait, run_end)

    plan_seconds = INFINITE_AFK_PLAN_SEC if RUNTIME_LIMITLESS else TOTAL_RUN_MIN * 60
    breaks = plan_breaks(plan_seconds)
    log(f"☕ AFK BREAKS scheduled: {len(breaks)} (rolling {plan_seconds / 3600:.0f}h window)" if RUNTIME_LIMITLESS
        else f"☕ AFK BREAKS scheduled: {len(breaks)} (each 10-30 min, ≥15 min apart):")
    for s, e in breaks:
        log(f"   • {datetime.fromtimestamp(s).strftime('%H:%M')} → "
            f"{datetime.fromtimestamp(e).strftime('%H:%M')} ({(e-s)/60:.0f} min)")

    log("")
    log("🚀 STARTING MAIN LOOP.")
    log("👉 ACTION REQUIRED — MANUAL VERIFICATION:")
    log("   After the FIRST POST is logged, open Discord on your main/phone and")
    log("   CONFIRM you can see the ad in the channel. If you can't see it,")
    log("   anti-spam is SHADOW-DELETING it. CANCEL the run immediately. Don't")
    log("   waste the alt by continuing to post into a shadowban.")
    log("")

    # --- Startup notification to log webhook ---
    ch_list = ", ".join("#" + ch_names.get(c, str(c)) for c in active_channels)
    send_log_webhook(
        f"🟢 **STARTUP** `{VERSION}` | mode=`{AD_TYPE.upper()}` | channels=[{ch_list}] "
        f"| interval=~{INTERVAL_MIN}min (±30-45%) | runtime={TOTAL_RUN_MIN:.0f}min | "
        f"variants={len(variations)} | image={'on' if use_img_ever else 'off'} | "
        f"gateway={'on' if ENABLE_GATEWAY and _HAS_WS else 'off'}"
    )
    # --- Startup dashboard embed ---
    if DASHBOARD_WEBHOOK_URL:
        try:
            ch_list_md = "\n".join(f"• #{ch_names.get(c,c)} `{c}`" for c in active_channels)
            send_dashboard(_dashboard_startup_embed(
                VERSION, AD_TYPE.upper(), ch_list_md, INTERVAL_MIN, TOTAL_RUN_MIN,
                len(variations), use_img_ever, len(CHANNEL_IDS), len(active_channels)))
        except Exception as e:
            dbg(f"[DASHBOARD] startup embed failed: {e}")

    try:
        # ================================================================== #
        # INDEPENDENT PER-CHANNEL SCHEDULER (replaces the old global cycle)  #
        #                                                                    #
        # Each channel has its own next_post_time. The main loop picks the   #
        # channel whose next_post_time is soonest, sleeps precisely until    #
        # then (with a tiny human jitter), posts, and recomputes only that  #
        # channel's next_post_time. A slowmode on one channel NEVER blocks   #
        # another channel.                                                  #
        # ================================================================== #

        # Initialise next_post_time for every channel: 12-30s after startup
        # (replaces the "final pre-post pause"), so channels don't all fire
        # at t=0. Staggered by a 3-10s gap so messages aren't simultaneous.
        next_post_time = {}
        _next_post_ref = next_post_time
        _dead_channels_ref = dead_channels
        stagger = 0.0
        for cid in active_channels:
            next_post_time[cid] = time.time() + random.uniform(12, 30) + stagger
            stagger += random.uniform(3, 10)

        # Schedule the first dashboard summary ~60s after first expected post
        next_dashboard_time = time.time() + 60
        dashboard_interval = 30 * 60  # one dashboard summary every 30 minutes
        posts_since_last_dash = 0

        cycle = 0  # repurposed: increments on every post (not every global pass)
        last_dash_elapsed = 0.0

        # Per-channel working state
        used_variations = set()
        last_msg_id_in_channel = dict(my_last_msg_id)  # thread-local view

        log("")
        log("🧠 SCHEDULER: independent per-channel timing enabled.")
        log("   Each channel posts on its own schedule (slowmode + jitter);")
        log("   slow channels no longer block fast ones. Initial stagger set.")
        log("")

        while time.time() < _get_run_end(run_end):
            now = time.time()

            # F-32: panic event — exit main loop immediately
            if _panic_event.is_set():
                log("🛑 Panic event detected — breaking out of main loop for clean shutdown.")
                break

            # ---------- AFK handling ----------
            in_afk, afk_left = in_break(breaks, now)
            if in_afk:
                resume_ts = time.time() + afk_left
                resume_str = datetime.fromtimestamp(resume_ts).strftime('%H:%M:%S')
                log(f"☕ AFK BREAK — stepping away for {afk_left/60:.1f} min.")
                log(f"   Resuming around {resume_str}. All public posting paused (simulating being offline).")
                if not in_afk_logged:
                    send_log_webhook(f"☕ **AFK BREAK** — stepping away for {afk_left/60:.1f} min (resuming ~{resume_str}). Public posting safely paused.", kind="AFK")
                    in_afk_logged = True
                sleep_with_keepalive(min(60, afk_left), run_end)
                returning_from_afk = True
                # Reset next-post times so we don't spam a burst of overdue
                # posts the instant we come back.
                stagger = 0.0
                for cid in active_channels:
                    next_post_time[cid] = time.time() + random.uniform(15, 45) + stagger
                    stagger += random.uniform(3, 8)
                continue

            if returning_from_afk:
                in_afk_logged = False
                ret_wait = random.uniform(15, 45)
                log(f"👋 BACK FROM AFK — re-orienting for {ret_wait:.0f}s (catching up on missed messages, simulating reopening Discord)...")
                send_log_webhook("👋 **BACK FROM AFK** — catching up on chat and resuming active posting rotation.", kind="ACTIVE")
                sleep_chunked(ret_wait, run_end)
                for _cid in active_channels:
                    if time.time() >= _get_run_end(run_end):
                        break
                    if public_activity_allowed():
                        _n = ch_names.get(_cid, _cid)
                        dbg(f"[AFK-REORIENT] re-reading #{_n} ({_cid}) after AFK break")
                        read_channel(_cid)
                    sleep_chunked(random.uniform(1.5, 4.0), run_end)
                returning_from_afk = False
                log("   ✅ Re-oriented. Resuming normal activity.")
                continue

            # ---------- DM pause (don't post publicly while buyer is DMing) --
            if not public_activity_allowed():
                with _state_lock:
                    left = max(0, _public_pause_until - time.time())
                log(f"⏸️  BUYER DM PAUSE ACTIVE — {left/60:.1f}m left. Idling (no posts / reactions / typing).")
                sleep_chunked(min(30, left + 5), run_end)
                continue

            # ---------- 5% pre-cycle distraction pause (checking DMs etc.) ---
            if random.random() < 0.05 and total_sent > 0:
                dist = random.uniform(20, 60)
                total_distractions += 1
                log(f"💭 DISTRACTION PAUSE — pausing public activity for {dist:.0f}s (simulating checking DMs / another server / tabbing away).")
                sleep_with_keepalive(dist, run_end)
                if time.time() >= _get_run_end(run_end):
                    break
                continue

            # ---------- Pick next channel to post to ------------------------
            # Choose active channel with earliest next_post_time that is not
            # in error-backoff / dead / DM-paused / circuit-broken.
            candidates = [c for c in active_channels if c not in dead_channels
                          and channel_errors.get(c, 0) < 3
                          and _CIRCUIT_BREAKER.is_allowed(c)]
            if not candidates:
                log("⚠️  No eligible channels (all dead or in error backoff). Sleeping 60s...")
                sleep_chunked(60, run_end)
                continue
            cid = min(candidates, key=lambda c: next_post_time.get(c, now))
            due = next_post_time[cid]
            ch_tag = f"#{ch_names.get(cid, cid)} ({cid})"

            # ---------- Sleep until that channel is due --------------------
            wait_sec = due - now
            if wait_sec > 0:
                # Human-looking jitter: don't fire on the exact second.
                jitter = random.uniform(2, 5)
                sleep_sec = wait_sec + jitter
                # Cap sleep chunks at 30s so keepalives / interrupts / DM-pause
                # checks keep firing during long waits.
                if sleep_sec > 30:
                    log(f"⏳ Next: {ch_tag} in {wait_sec/60:.1f} min (sleeping with keepalives)...")
                else:
                    dbg(f"[SCHED] sleeping {sleep_sec:.1f}s until {ch_tag} is due")
                sleep_with_keepalive(min(sleep_sec, 30), run_end)
                continue  # loop back to re-check AFK/DM/runtime

            # If we reach here, wait_sec <= 0: the channel is due.
            remaining_min = None if _get_run_end(run_end) == float("inf") else (_get_run_end(run_end) - time.time()) / 60
            cycle += 1  # "post attempts" counter

            # ---------- Direction + warmup status header (every ~10 posts) --
            if cycle == 1 or cycle % 10 == 0:
                direction = "💰 SELL" if _get_active_ad_type() == "sell" else "🛒 BUY"
                if use_img_ever and sent_count_global < WARMUP_POSTS:
                    img_status = f"🔰 warmup {sent_count_global}/{WARMUP_POSTS} (text-only until warmup done)"
                elif use_img_ever:
                    img_status = "🖼️ image attach ENABLED (100% after warmup)"
                else:
                    img_status = "💬 text-only mode"
                log("")
                remaining_s = "∞" if remaining_min is None else f"{remaining_min:.0f} min"
                log(f"{'─'*25} Post #{cycle} [{direction}] | {remaining_s} remaining | {_ts()} {'─'*25}")
                log(f"   Status: {img_status}")

            log("")
            log(f"🔍 {ch_tag}: channel is DUE — preparing post...")

            # ---------- Re-check slowmode belt-and-braces ------------------
            slow = slowmodes.get(cid, 0)
            if slow > 0 and cid in last_sent:
                elapsed = time.time() - last_sent[cid]
                need_wait = slow - elapsed + random.uniform(2, 5)
                if need_wait > 0:
                    log(f"   ⏳ {ch_tag}: SLOWMODE belt-and-braces wait — {need_wait:.0f}s still needed. Rescheduling.")
                    next_post_time[cid] = time.time() + need_wait
                    continue

            # ---------- Multi-alt fleet collision check ---------------------
            has_collision, yield_wait = _check_fleet_collision(cid, min_separation=90.0)
            if has_collision:
                log(f"   🛡️ FLEET STAGGERING: Another fleet alt posted in {ch_tag} recently. Yielding slot for {yield_wait:.0f}s to prevent collision.")
                next_post_time[cid] = time.time() + yield_wait
                continue

            # ---------- Quick glance at recent msgs (for reactions/cooldown)
            try:
                recent = get_last_messages(cid, 20)
            except Exception:
                recent = None
            i_am_last = False
            last_author = "?"
            last_snip = ""
            if recent and len(recent) > 0:
                last2 = recent[0]
                last_author_obj = last2.get("author", {}) or {}
                i_am_last = (last_author_obj.get("id") == my_id)
                last_author = last_author_obj.get("username") or last_author_obj.get("global_name") or "?"
                last_snip = (last2.get("content") or "").replace("\n", " ")[:50] or "<embed/image/empty>"

            # ---------- Deletion detection ---------------------------------
            # A finite recent-message page cannot prove deletion: a busy
            # channel can bury a message in seconds. The exact-message
            # verification thread is authoritative and only a confirmed 404
            # can enter caution mode. Missing/failed recent reads are ignored.
            prev_id = my_last_msg_id.get(cid)
            deleted_detected = False
            if prev_id and recent is not None:
                recent_ids = {m.get("id") for m in recent}
                if prev_id not in recent_ids:
                    dbg(f"[VERIFY] {ch_tag}: previous message is outside the recent page; awaiting exact-message verification")

            # ---------- Safety-net: never go completely silent -------------
            force_post = False
            if cid in last_sent:
                since_last = time.time() - last_sent[cid]
                max_wait = max(INTERVAL_MIN*60*2.5, slowmodes.get(cid,0) + 120)
                if since_last > max_wait:
                    force_post = True
                    log(f"   🔄 {ch_tag}: SAFETY-NET TRIGGERED — last sent {since_last/60:.1f} min ago (>2.5× interval). Force-reposting.")

            # ---------- Optional smart cooldown (skip if we're LATEST) -----
            # In 30-50 msg/min channels this almost never triggers (good),
            # but in quiet channels it saves us from spamming ourselves down.
            if i_am_last and not deleted_detected and not force_post:
                stats[cid]["cooldown"] += 1
                stats[cid]["skipped"] += 1
                total_skip += 1
                log(f"   ⏭️  {ch_tag}: our ad is still the LATEST message (by @{last_author}). Cooldown — rescheduling.")
                dbg(f"      Last msg: \"{last_snip}\"")
                if recent is not None and not channel_in_caution(cid):
                    maybe_react(cid, recent[:5], my_id)
                elif recent is not None:
                    dbg(f"[CAUTION] {ch_tag}: skipping reaction while in caution mode")
                gaze = random.uniform(3, 7)
                log(f"   👀 {ch_tag}: glancing at chat for {gaze:.0f}s (simulating reading without posting)...")
                sleep_chunked(gaze, run_end)
                # Reschedule: don't check again for a while
                base_wait = max(slow if slow > 0 else INTERVAL_MIN*60, 45)
                next_post_time[cid] = time.time() + min(base_wait, INTERVAL_MIN*60) * random.uniform(0.5, 0.8)
                continue

            # ---------- Random skip (5-12% of the time, human-like) -------
            # After warmup only — don't skip warmup posts.
            post_threshold = random.uniform(0.88, 0.96)
            skip_chance_pct = (1 - post_threshold) * 100
            if sent_count_global >= WARMUP_POSTS and random.random() > post_threshold:
                stats[cid]["skipped"] += 1
                total_skip += 1
                log(f"   ↪️  {ch_tag}: RANDOM SKIP ({skip_chance_pct:.0f}% chance rolled). Skipping this pass to look human.")
                sleep_chunked(random.uniform(3, 8), run_end)
                # Reschedule: try again in 30-60s
                next_post_time[cid] = time.time() + random.uniform(30, 60)
                continue

            # ---------- V6: Rebuild variations if controller changed message
            active_msg = _get_active_message()
            if active_msg != _last_variation_base:
                log(f"📝 Active ad message updated by controller — rebuilding variations.")
                variations = build_variations(active_msg)
                used_variations.clear()
                _last_variation_base = active_msg
                # Filter blacklisted
                with _state_lock:
                    blocked_snapshot2 = set(_blocked_variations)
                variations = [v for v in variations if v not in blocked_snapshot2]
                if not variations:
                    variations = build_variations(active_msg)

            # ---------- Pick an un-blacklisted variation ------------------
            # Weighted random selection favoring proven unblocked variations
            msg = _pick_surviving_variation(variations, used_variations)
            if not msg:
                log("")
                log("🛑 CRITICAL: ALL message variations have been blacklisted by auto-learn.")
                log("   The account/IP is flagged — stopping to protect the alt.")
                send_log_webhook("🛑 **CRITICAL** all variations blacklisted — aborting.")
                save_blocked_to_gist(force=True)
                sys.exit(2)
            used_variations.add(msg)

            # ---------- Image attachment logic ----------------------------
            attach_this_post = False
            if use_img_ever and sent_count_global < WARMUP_POSTS:
                attach_this_post = False
                log(f"   🔰 WARMUP POST ({sent_count_global+1}/{WARMUP_POSTS}) — text-only to age the session before sending images")
            elif use_img_ever and sent_count_global >= WARMUP_POSTS:
                # 100% image attach after warmup. F-27: force text-only while the
                # channel is in caution mode (images draw stronger anti-spam).
                attach_this_post = not channel_in_caution(cid)
                if channel_in_caution(cid):
                    dbg(f"[CAUTION] {ch_tag}: text-only while in caution mode")

            img_payload = None
            if attach_this_post:
                log(f"   🖼️ {ch_tag}: building randomized image payload (EXIF stripped, filename randomized, JPEG q90-96, ±1px jitter)...")
                img_payload = make_image_payload(raw_image, image_name)
                if img_payload is None:
                    log(f"   ⚠️  {ch_tag}: image payload build failed; falling back to text-only.")
                    attach_this_post = False

            snip = msg.replace("\n", " ⏎ ")[:55]
            kind = "📷 IMAGE+TEXT" if attach_this_post else "💬 TEXT-ONLY"
            log(f"   📤 {ch_tag}: SENDING {kind} → \"{snip}{'...' if len(msg) > 55 else ''}\"")

            # Pre-send "thinking" + typing indicator is handled inside send_message
            ok, code, err, new_msg_id, msg_obj = send_message(cid, msg, img_payload)

            # Recompute this channel's next_post_time regardless of success/fail
            if ok:
                last_sent[cid] = time.time()
                channel_errors[cid] = 0  # reset error backoff on success
                _CIRCUIT_BREAKER.record_success(cid)
                _record_fleet_post(cid)

                # Compute next_post_time using Chat Velocity and strict slowmode floor
                c_mult = channel_caution_multiplier(cid)
                v_speed, v_mult = _channel_velocity.get(cid, (5.0, 1.0))
                base_wait = INTERVAL_MIN * 60 * random.uniform(0.70, 1.40) * c_mult * v_mult
                if slow > 0:
                    slowmode_floor = (slow + random.uniform(15, 35)) * c_mult
                    nxt = time.time() + max(base_wait, slowmode_floor)
                else:
                    nxt = time.time() + max(60, base_wait)

                if v_mult != 1.0:
                    dbg(f"[VELOCITY] {ch_tag}: traffic velocity={v_speed:.1f} msg/min -> multiplier={v_mult:.2f}x")
                if c_mult > 1.0:
                    dbg(f"[CAUTION] {ch_tag}: next post in {(nxt-time.time())/60:.1f} min ({c_mult:.1f}x caution throttle)")
                next_post_time[cid] = nxt

                total_sent += 1
                sent_count_global += 1
                posts_since_last_dash += 1
                stats[cid]["sent"] += 1
                if attach_this_post:
                    stats[cid]["img"] += 1
                    total_img += 1
                else:
                    stats[cid]["txt"] += 1
                if new_msg_id:
                    my_last_msg_id[cid] = new_msg_id
                channel_errors[cid] = 0
                log(f"   ✅ {ch_tag}: MESSAGE POSTED SUCCESSFULLY — id={new_msg_id} (run total: {total_sent})")
                dbg(f"      Full msg: {msg[:200]}{'...' if len(msg)>200 else ''}")
                log(f"   ⏭️  Next post for {ch_tag} at ~{datetime.fromtimestamp(nxt).strftime('%H:%M:%S')} (in {(nxt-time.time())/60:.1f} min).")
                send_log_webhook(
                    f"✅ **SEND** {ch_tag} | {'📷img' if attach_this_post else '💬txt'} | total=`{total_sent}` | id=`{new_msg_id}`"
                )

                if new_msg_id and not channel_in_caution(cid):
                    # maybe_typo_edit performs the single configured-probability
                    # roll. Keeping no outer roll makes 18% the effective rate.
                    t = threading.Thread(
                        target=lambda cid=cid, mid=new_msg_id, mt=msg: (
                            maybe_typo_edit(cid, mid, mt) or None
                        ),
                        daemon=True,
                    )
                    t.start()
                    log(f"   ✏️  {ch_tag}: typo-edit candidate queued ({TYPO_EDIT_CHANCE*100:.0f}% chance) — may edit 5-22s after post with a small natural correction.")
                elif new_msg_id and channel_in_caution(cid):
                    dbg(f"[CAUTION] {ch_tag}: skipping typo-edit while in caution mode")
            else:
                total_err += 1
                stats[cid]["errors"] += 1
                channel_errors[cid] += 1
                _CIRCUIT_BREAKER.record_failure(cid, code)
                log(f"   ❌ {ch_tag}: SEND FAILED — HTTP {code}: {err}")
                send_log_webhook(
                    f"❌ **FAIL** {ch_tag} | HTTP `{code}`: {err}"
                )
                # Back off this channel after an error (don't retry immediately).
                # Exponential backoff: 1 err → 2-3 min, 2 errs → 5-8 min, 3+ errs → 10-20 min.
                backoff = [0, 180, 480, 900][min(channel_errors[cid], 3)] + random.uniform(-30, 60)
                next_post_time[cid] = time.time() + max(60, backoff)
                log(f"   ⏳ {ch_tag}: backing off {backoff/60:.1f} min before retrying.")

                if code == 401 or code == 403:
                    recheck, rr = validate_token()
                    if recheck is None and rr == "invalid":
                        log("")
                        log("🛑 CRITICAL: Token invalidated/revoked/banned (HTTP 401/403 and re-auth failed).")
                        log("   Discord has flagged this alt. Stopping immediately.")
                        send_log_webhook(
                            f"🛑 **BANNED?** Token invalidated (HTTP {code}) | sent=`{total_sent}`. Aborting."
                        )
                        send_dashboard(_dashboard_cycle_embed(
                            cycle, (time.time()-start)/60, total_sent, total_img,
                            total_sent-total_img, total_edits, total_err, total_skip,
                            stats, len(active_channels), len(CHANNEL_IDS),
                            set(active_channels), ch_names, slowmodes, last_sent,
                            my_last_msg_id, is_shutdown=True))
                        save_blocked_to_gist(force=True)
                        _print_stats(start, total_sent, total_err, total_skip,
                                     total_distractions, total_img, total_edits, stats)
                        sys.exit(2)
                    elif recheck is None:
                        log(f"   ⚠️  {ch_tag}: got {code} but token re-validation also failed ({rr}). Backing off this channel.")
                    else:
                        log(f"   ⚠️  {ch_tag}: HTTP 403 but token VALID — channel inaccessible (kicked/banned/deleted). Marking DEAD.")
                        dead_channels.add(cid)
                        if cid in active_channels:
                            active_channels.remove(cid)
                        if cid in next_post_time:
                            del next_post_time[cid]
                elif code == 404:
                    # V6: attempt auto-discovery BEFORE marking dead
                    _disc_ctx = {
                        "ch_names": ch_names, "slowmodes": slowmodes,
                        "last_sent": last_sent, "my_last_msg_id": my_last_msg_id,
                        "stats": stats, "active_channels": active_channels,
                        "dead_channels": dead_channels,
                        "next_post_time": next_post_time,
                    }
                    new_cid = try_channel_discovery(cid, _disc_ctx)
                    if new_cid:
                        # Successfully replaced: schedule a retry next pass
                        # (don't re-post immediately — give the confirmation
                        # message room to breathe)
                        log(f"   🔄 {ch_tag}: channel replaced mid-run with #{ch_names.get(new_cid,new_cid)} ({new_cid}). Will post on next scheduled tick.")
                        # Reset error count for the new channel
                        channel_errors[new_cid] = 0
                    else:
                        log(f"   ⚠️  {ch_tag}: HTTP 404 and discovery did not recover. Marking DEAD.")
                        dead_channels.add(cid)
                        if cid in active_channels:
                            active_channels.remove(cid)
                        if cid in next_post_time:
                            del next_post_time[cid]

            if time.time() >= _get_run_end(run_end):
                log("   ⏱️ Runtime limit reached; exiting scheduler.")
                break

            # Periodically save blocklist
            if time.time() - last_gist_save > 300:
                if save_blocked_to_gist():
                    last_gist_save = time.time()

            if not public_activity_allowed():
                with _state_lock:
                    left = max(0, _public_pause_until - time.time())
                log(f"   📩 {ch_tag}: BUYER DM ARRIVED mid-post — pausing ALL public activity for {left/60:.1f}m.")
                continue

            # ---------- Post-send natural "gaze" behavior -----------------
            # 5% mid-send distraction
            if random.random() < 0.05 and total_sent > 3:
                mid_dist = random.uniform(45, 180)
                total_distractions += 1
                log(f"   💭 {ch_tag}: MID-SESSION DISTRACTION — pausing {mid_dist:.0f}s (DM / phone / app-switch).")
                sleep_with_keepalive(mid_dist, run_end)
                if time.time() >= _get_run_end(run_end):
                    break
            elif random.random() < 0.40 and len(active_channels) > 1:
                other = random.choice([c for c in active_channels if c != cid])
                oname = ch_names.get(other, other)
                g1 = random.uniform(3, 7)
                log(f"   👀 {ch_tag}: glancing at post for {g1:.0f}s...")
                sleep_chunked(g1, run_end)
                if public_activity_allowed():
                    log(f"   👀 Switching to #{oname} ({other}) and reading recent messages (browsing other channels after posting)...")
                    read_channel(other)
                g2 = random.uniform(3, 8)
                sleep_chunked(g2, run_end)
            elif random.random() < 0.20:
                g = random.uniform(8, 20)
                log(f"   👀 {ch_tag}: staring at chat for {g:.0f}s after posting (simulating reading responses)...")
                sleep_chunked(g, run_end)
            else:
                g = random.uniform(4, 10)
                log(f"   👀 {ch_tag}: waiting {g:.0f}s before moving on...")
                sleep_chunked(g, run_end)

            # ---------- Periodic heartbeat (V6 unified dashboard) ------
            _send_heartbeat(active_channels, ch_names, slowmodes, last_sent,
                            my_last_msg_id, stats, total_sent, total_err, total_skip,
                            total_img, total_edits, dead_channels=dead_channels,
                            in_afk_flag=bool(in_afk))

            # ---------- Periodic dashboard summary -----------------------
            if DASHBOARD_WEBHOOK_URL and time.time() >= next_dashboard_time:
                elapsed_min = (time.time() - start)/60
                in_break_now, afk_l = in_break(breaks, time.time())
                try:
                    send_dashboard(_dashboard_cycle_embed(
                        cycle, elapsed_min, total_sent, total_img,
                        total_sent-total_img, total_edits, total_err, total_skip,
                        stats, len(active_channels), len(CHANNEL_IDS),
                        set(active_channels), ch_names, slowmodes, last_sent,
                        my_last_msg_id, in_afk_flag=in_break_now, afk_left=afk_l))
                    next_dashboard_time = time.time() + dashboard_interval
                    dbg(f"[DASHBOARD] sent periodic summary (interval={dashboard_interval/60:.0f}m)")
                except Exception as e:
                    dbg(f"[DASHBOARD] error sending summary: {e}")
                    next_dashboard_time = time.time() + 300  # retry in 5 min

            # End of per-post iteration — main while-loop continues by
            # picking the next-earliest channel. There is NO global
            # "next cycle wait" because each channel has its own timer.


    except KeyboardInterrupt:
        log("\n🛑 STOPPED BY USER (Ctrl+C / workflow cancel).")
        elapsed_min = (time.time() - start)/60
        send_log_webhook(
            f"🛑 **STOPPED** by user (Ctrl+C/cancel) | sent=`{total_sent}` | elapsed=`{elapsed_min:.1f}min`"
        )
        if DASHBOARD_WEBHOOK_URL:
            try:
                send_dashboard(_dashboard_cycle_embed(
                    cycle, elapsed_min, total_sent, total_img, total_sent-total_img,
                    total_edits, total_err, total_skip, stats,
                    len(active_channels), len(CHANNEL_IDS), set(active_channels),
                    ch_names, slowmodes, last_sent, my_last_msg_id, is_shutdown=True))
                time.sleep(1.5)
            except Exception as _ignored_exc:
                print(f"[SENDER] main: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
        save_blocked_to_gist(force=True)
        _print_stats(start, total_sent, total_err, total_skip,
                     total_distractions, total_img, total_edits, stats)
        sys.exit(130)
    except SystemExit:
        save_blocked_to_gist(force=True)
        raise
    except Exception as e:
        elapsed_min = (time.time() - start)/60
        log(f"\n💥 UNHANDLED ERROR (bug?): {type(e).__name__}: {e}")
        log("   Please report this with the full log output so it can be fixed.")
        send_log_webhook(
            f"💥 **CRASH** `{type(e).__name__}`: {str(e)[:200]} | sent=`{total_sent}` | elapsed=`{elapsed_min:.1f}min`"
        )
        if DASHBOARD_WEBHOOK_URL:
            try:
                send_dashboard(_dashboard_cycle_embed(
                    cycle, elapsed_min, total_sent, total_img, total_sent-total_img,
                    total_edits, total_err, total_skip, stats,
                    len(active_channels), len(CHANNEL_IDS), set(active_channels),
                    ch_names, slowmodes, last_sent, my_last_msg_id, is_shutdown=True))
                time.sleep(1.5)
            except Exception as _ignored_exc:
                print(f"[SENDER] main: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
        save_blocked_to_gist(force=True)
        _print_stats(start, total_sent, total_err, total_skip,
                     total_distractions, total_img, total_edits, stats)
        raise

    if _panic_event.is_set():
        elapsed_min = (time.time() - start)/60
        log("\n🛑 Panic stop — run terminated by remote command.")
        _send_completion_summary_webhook(
            "🛑 Halted by remote command (panic stop)",
            start, total_sent, total_err, total_skip,
            total_distractions, total_img, total_edits, stats, is_shutdown=True
        )
        try:
            save_blocked_to_gist(force=True)
        except Exception as _ignored_exc:
            print(f"[SENDER] main: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
        _print_stats(start, total_sent, total_err, total_skip,
                     total_distractions, total_img, total_edits, stats)
        if _gw_thread is not None:
            _gw_thread.stop()
        sys.exit(2)

    elapsed_min = (time.time() - start)/60
    log("\n🏁 Reached scheduled end time — run complete.")
    _send_completion_summary_webhook(
        f"Scheduled execution window complete ({elapsed_min:.1f}m)",
        start, total_sent, total_err, total_skip,
        total_distractions, total_img, total_edits, stats, is_shutdown=False
    )
    save_blocked_to_gist(force=True)
    _print_stats(start, total_sent, total_err, total_skip,
                 total_distractions, total_img, total_edits, stats)
    if _gw_thread is not None:
        _gw_thread.stop()
    sys.exit(0)


def _print_stats(start_ts, sent, err, skip, distractions, img, edits, per_ch):
    elapsed = (time.time() - start_ts)/60
    log("=" * 66)
    log("📊 FINAL STATS")
    log("=" * 66)
    log(f"⏱️  ELAPSED      : {elapsed:.1f} min ({elapsed/60:.2f}h)")
    log(f"📤 SENT         : {sent}  (💬 text:{sent-img}  📷 image:{img})")
    log(f"✏️  EDITS        : {edits}  (typo-fix edits after post)")
    log(f"❌ ERRORS       : {err}")
    log(f"⏭️  SKIPPED      : {skip}  (cooldown + random skip + error backoff)")
    log(f"💭 DISTRACTIONS : {distractions} random human pauses")
    with _state_lock:
        bl = len(_blocked_variations)
    if bl:
        log(f"🚫 BLACKLISTED  : {bl} message variations (auto-learned as blocked by anti-spam)")
        log("   → These will not be reused in future runs (persisted to gist if configured).")
    if sent > 0 and elapsed > 0:
        log(f"📈 POST RATE    : {sent/(elapsed/60):.1f} msg/hour")
    if err > 0 and sent + err > 0:
        err_pct = err/(sent+err)*100
        warn = "  ⚠️ high — review errors above" if err_pct > 20 else ""
        log(f"⚠️  ERROR RATE   : {err_pct:.1f}%{warn}")
    if sent > 0 and err == 0 and skip > sent * 2:
        log("💡 NOTE: Most cycles were skips (cooldown or random) — this is normal for human-like cadence.")
    log("")
    log("📂 Per-channel breakdown:")
    for cid in CHANNEL_IDS:
        s = per_ch[cid]
        name = ch_names.get(cid, "?") if ch_names else "?"
        log(f"   #{name} ({cid}):")
        log(f"      ✅ sent={s['sent']}  (💬{s['txt']}/📷{s['img']}/✏️{s['edits']})  "
            f"❌err={s['errors']}  ⏭️skip={s['skipped']}  🔁cooldown={s['cooldown']}")
    log("=" * 66)


if __name__ == "__main__":
    if _SELF_TEST:
        CLIENT_BUILD = _DEFAULT_BUILD
        _CHROME_VER = _CHROME_VERSION_FALLBACK
        try:
            SESSION.cookies.set("locale", DISCORD_LOCALE, domain="discord.com")
        except Exception as _ignored_exc:
            print(f"[SENDER] <module>: ignored {type(_ignored_exc).__name__}: {_ignored_exc}")  # silent-failure cleanup (V8 plan #4)
        self_test()
    else:
        main()
