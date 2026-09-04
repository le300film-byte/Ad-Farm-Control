"""
control_bot.config
------------------
Configuration for the central control bot. Runtime values are read from
repository secrets/variables. Tuning values may optionally be supplied as one
JSON object in TUNING_JSON; an explicitly supplied environment variable wins
over the JSON value, and the built-in default is used as the final fallback.
"""
from __future__ import annotations

import json
import os


def _load_tuning() -> dict:
    raw = os.environ.get("TUNING_JSON", "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"⚠️ TUNING_JSON is not valid JSON ({exc}); built-in defaults will be used.")
        return {}
    if not isinstance(value, dict):
        print("⚠️ TUNING_JSON must contain a JSON object; built-in defaults will be used.")
        return {}
    return value


_TUNING = _load_tuning()


def _raw(name: str) -> str:
    """Return an explicit environment value or the matching tuning value."""
    env_value = os.environ.get(name, "").strip()
    if env_value:
        return env_value
    value = _TUNING.get(name)
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _env(name: str, default: str = "") -> str:
    return _raw(name) or default


def _int(name: str, default: int) -> int:
    raw = _raw(name)
    if not raw:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _split(name: str) -> list[str]:
    raw = _env(name)
    return [x.strip() for x in raw.split(",") if x.strip()]


def _snowflake(name: str) -> int | None:
    raw = _env(name, "0")
    try:
        return int(raw) or None
    except ValueError:
        return None


# ---------- Discord ----------
BOT_TOKEN = _env("BOT_TOKEN")
GUILD_ID = _snowflake("GUILD_ID")
CONTROL_CH_ID = _snowflake("CONTROL_CH_ID")
DASHBOARD_CH_ID = _snowflake("DASHBOARD_CH_ID")
LOG_CH_ID = _snowflake("LOG_CH_ID")
DEALS_CH_ID = _snowflake("DEALS_CH_ID")
# Optional GLOBAL #dm-inbox channel where alt runners forward buyer DMs.
# Per-customer VIP forums also get their own dm-inbox thread (dm_thread_id in
# customers.db); the VIP auto-reply watcher monitors both (V8 plan feature #5).
DM_INBOX_CH_ID = _snowflake("DM_INBOX_CH_ID")

# ---------- Authorized users ----------
OWNER_IDS: set[int] = set()
for uid in (_split("OWNER_IDS") or _split("OWNER_ID")):
    try:
        OWNER_IDS.add(int(uid))
    except ValueError:
        print(f"⚠️ config: ignoring non-numeric OWNER_IDS entry '{uid}'.")

# Filled at runtime; alts only need the controller IDs from their own config.
BOT_USER_ID = _snowflake("BOT_USER_ID")
CMD_COOLDOWN_SEC = _int("CMD_COOLDOWN_SEC", 5)

# ---------- GitHub ----------
# One shared token from `gh auth token` is used for dispatch, sync, and Gists.
GITHUB_TOKEN = _env("GH_TOKEN") or _env("GITHUB_PAT")
GITHUB_OWNER = _env("REPO_OWNER") or _env("GITHUB_OWNER")
CORE_REPO = _env("CORE_REPO") or os.environ.get("GITHUB_REPOSITORY", "").strip()
# Shared private Gist used as a control queue when an alt is not a mutual
# member of the control-bot server. The bot writes one targeted file per alt;
# the alt polls it with its already-configured GIST_TOKEN.
CONTROL_GIST_ID = _env("CONTROL_GIST_ID")
CHANNEL_STATE_GIST_ID = _env("CHANNEL_STATE_GIST_ID") or CONTROL_GIST_ID
CONTROL_HTTP_TIMEOUT = _int("CONTROL_HTTP_TIMEOUT", 20)

# Alt -> repo mapping. The bootstrap writes the unambiguous form:
#   ALT_REPOS=1:alt1-sell,2:alt2-sell,3:alt3-buy,4:alt4-buy
ALT_REPOS: dict[int, str] = {}
for pair in _split("ALT_REPOS"):
    if ":" in pair:
        k, v = pair.split(":", 1)
        try:
            ALT_REPOS[int(k)] = v.strip()
        except ValueError:
            print(f"⚠️ config: ignoring malformed ALT_REPOS pair '{pair}' (expected id:repo).")

# Alt -> Discord user ID, same 1:id format as ALT_REPOS.
ALT_DISCORD_IDS: dict[int, int] = {}
for pair in _split("ALT_DISCORD_IDS"):
    if ":" in pair:
        k, v = pair.split(":", 1)
        try:
            ALT_DISCORD_IDS[int(k)] = int(v.strip())
        except ValueError:
            print(f"⚠️ config: ignoring malformed ALT_DISCORD_IDS pair '{pair}' (expected id:userid).")

# Friendly alt names. The live ad_type from heartbeats determines seller/buyer
# presentation; there is intentionally no static market-mode configuration.
ALT_NAMES: dict[int, str] = {}
for pair in _split("ALT_NAMES"):
    if ":" in pair:
        k, v = pair.split(":", 1)
        try:
            ALT_NAMES[int(k)] = v.strip()
        except ValueError:
            print(f"⚠️ config: ignoring malformed ALT_NAMES pair '{pair}' (expected id:name).")

WORKFLOW_FILE = _env("WORKFLOW_FILE", "send_ads.yml")
SELF_CHECK_WORKFLOW = _env("SELF_CHECK_WORKFLOW", "self_check.yml")

# Only alts represented by configured repository or Discord-ID mappings are
# real alts. This prevents an orphaned name or placeholder Alt 4 entry from
# appearing when an installation contains only one to three alts.
CONFIGURED_ALT_IDS = tuple(sorted(set(ALT_REPOS) | set(ALT_DISCORD_IDS)))

# ---------- Dashboard behavior ----------
DASHBOARD_REFRESH_SEC = _int("DASHBOARD_REFRESH_SEC", 300)
OFFLINE_AFTER_SEC = _int("OFFLINE_AFTER_SEC", 900)
DASHBOARD_MSG_ID_FILE = _env("DASHBOARD_MSG_ID_FILE", "dash_msg_id.txt")
# JSON files are safe defaults for local development and ephemeral CI; set
# these to a mounted/shared path when the control bot must survive workers.
CONTROL_STATE_FILE = _env("CONTROL_STATE_FILE", ".adfarm_control_state.json")
CHANNEL_STATE_FILE = _env("CHANNEL_STATE_FILE", ".adfarm_channel_registry.json")

# ---------- Configurable market item / deal keywords ----------
# Defaults are intentionally generic. An installation may configure any asset,
# game, or service through secrets without changing code.
DEFAULT_ITEM_NAME = _env("DEFAULT_ITEM_NAME", "item")
DEFAULT_ITEM_KEYWORDS = [
    x.strip() for x in _env("DEFAULT_ITEM_KEYWORDS", "item,stock,goods,assets").split(",") if x.strip()
]
# Accept DEAL_ITEM_KEYWORDS as an explicit per-alt override too.
DEFAULT_DEAL_KEYWORDS = [
    x.strip() for x in _env("DEAL_ITEM_KEYWORDS", "item,stock,goods,assets").split(",") if x.strip()
]

# ---------- Run preview / confirmation ----------
RUN_PREVIEW_REQUIRED = _env("RUN_PREVIEW_REQUIRED", "1").lower() in {"1", "true", "yes", "on"}

# ---------- Scripting sandbox ----------
SCRIPT_TIMEOUT_SEC = _int("SCRIPT_TIMEOUT_SEC", 20)
SCRIPT_MEMORY_MB = _int("SCRIPT_MEMORY_MB", 256)
SCRIPT_CPU_SEC = _int("SCRIPT_CPU_SEC", 10)
SCRIPT_MAX_CHARS = _int("SCRIPT_MAX_CHARS", 20000)
SCRIPT_NETWORK_ENABLED = _env("SCRIPT_NETWORK_ENABLED", "0").lower() in {"1", "true", "yes", "on"}

# ---------- Continuous / shutdown ----------
CONTINUOUS_MODE = _env("CONTINUOUS_MODE", "1").lower() in {"1", "true", "yes", "on"}
SHUTDOWN_FLAG_FILE = _env("SHUTDOWN_FLAG_FILE", "/tmp/adfarm-shutdown.flag")
SHUTDOWN_GRACE_SEC = _int("SHUTDOWN_GRACE_SEC", 30)

# ---------- Health monitor ----------
HEALTH_CHECK_INTERVAL_SEC = _int("HEALTH_CHECK_INTERVAL_SEC", 300)
HEALTH_RECOVERY_RETRY_SEC = _int("HEALTH_RECOVERY_RETRY_SEC", 60)

# ---------- Log rotation ----------
LOG_ROTATION_MAX_ENTRIES = _int("LOG_ROTATION_MAX_ENTRIES", 500)

# ---------- Runtime ----------
LOG_LEVEL = _env("LOG_LEVEL", "INFO")
