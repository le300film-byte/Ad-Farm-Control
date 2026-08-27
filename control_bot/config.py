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

# ---------- Authorized users ----------
OWNER_IDS: set[int] = set()
for uid in _split("OWNER_IDS"):
    try:
        OWNER_IDS.add(int(uid))
    except ValueError:
        pass

# Filled at runtime; alts only need the controller IDs from their own config.
BOT_USER_ID = _snowflake("BOT_USER_ID")
CMD_COOLDOWN_SEC = _int("CMD_COOLDOWN_SEC", 5)

# ---------- GitHub ----------
# One shared token from `gh auth token` is used for dispatch, sync, and Gists.
GITHUB_TOKEN = _env("GH_TOKEN")
GITHUB_OWNER = _env("GITHUB_OWNER")
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
            pass

# Alt -> Discord user ID, same 1:id format as ALT_REPOS.
ALT_DISCORD_IDS: dict[int, int] = {}
for pair in _split("ALT_DISCORD_IDS"):
    if ":" in pair:
        k, v = pair.split(":", 1)
        try:
            ALT_DISCORD_IDS[int(k)] = int(v.strip())
        except ValueError:
            pass

# Friendly alt names. The live ad_type from heartbeats determines seller/buyer
# presentation; there is intentionally no static market-mode configuration.
ALT_NAMES: dict[int, str] = {}
for pair in _split("ALT_NAMES"):
    if ":" in pair:
        k, v = pair.split(":", 1)
        try:
            ALT_NAMES[int(k)] = v.strip()
        except ValueError:
            pass

WORKFLOW_FILE = _env("WORKFLOW_FILE", "send_ads.yml")

# Only alts represented by configured repository or Discord-ID mappings are
# real alts. This prevents an orphaned name or placeholder Alt 4 entry from
# appearing when an installation contains only one to three alts.
CONFIGURED_ALT_IDS = tuple(sorted(set(ALT_REPOS) | set(ALT_DISCORD_IDS)))

# ---------- Dashboard behavior ----------
DASHBOARD_REFRESH_SEC = _int("DASHBOARD_REFRESH_SEC", 300)
OFFLINE_AFTER_SEC = _int("OFFLINE_AFTER_SEC", 900)
DASHBOARD_MSG_ID_FILE = _env("DASHBOARD_MSG_ID_FILE", "dash_msg_id.txt")

# ---------- Runtime ----------
LOG_LEVEL = _env("LOG_LEVEL", "INFO")
