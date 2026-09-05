"""Business constants and validators — the single home for every limit.

Every validator returns the normalised value or raises ``ValidationError`` with the exact text
shown to the user, so command handlers never format validation messages themselves.
"""
from __future__ import annotations

import math
import re
from typing import Iterable

from .errors import ValidationError

# ── Plan limits ──────────────────────────────────────────────────────────────
MAX_ALTS_PER_CUSTOMER = 4
MAX_CHANNELS_PER_ALT = 10
DEFAULT_SUBSCRIPTION_DAYS = 30
MAX_SUBSCRIPTION_DAYS = 366

# ── Runner limits (mirrors send_ads.yml inputs) ──────────────────────────────
INTERVALS_MIN = (3, 5)
RUNTIMES_HOURS = (0, 6, 12, 18, 24, 48)   # 0 == limitless (48 h per dispatch, auto-renewed)
LIMITLESS_RENEW_AFTER_SEC = 48 * 3600
POLICY_TEMPLATES = ("stealth", "aggressive", "peak_hour", "balanced")
AD_TYPES = ("sell", "buy")
BUY_STYLES = ("simple", "detailed")

PRICE_MIN_EXCLUSIVE = 0.0
PRICE_MAX = 20.0
DEAL_DELTA_MAX = 5.0
MAX_MESSAGE_CHARS = 1900
MAX_AUTOREPLY_CHARS = 1500
MAX_KEYWORDS = 20
MAX_KEYWORD_CHARS = 60
MAX_KEYWORDS_TOTAL_CHARS = 500
MAX_IMAGE_BYTES = 8 * 1024 * 1024
IMAGE_CONTENT_TYPES = ("image/png", "image/jpeg", "image/jpg", "image/webp")

# ── Subscription timers ──────────────────────────────────────────────────────
REMINDER_THRESHOLDS_DAYS = (7, 3, 1)
BAN_FULL_CREDIT_WINDOW_SEC = 48 * 3600

# ── Snowflakes ───────────────────────────────────────────────────────────────
_SNOWFLAKE = re.compile(r"^\d{10,20}$")
_PRICE = re.compile(r"(\d+(?:\.\d{1,2})?)")


def channel_limit_message(limit: int = MAX_CHANNELS_PER_ALT) -> str:
    return f"❌ Maximum {limit} channels per alt. Remove one before adding a new one."


def validate_snowflake(value: str, what: str = "ID") -> str:
    text = str(value or "").strip()
    if not _SNOWFLAKE.match(text):
        raise ValidationError(f"❌ {what} must be a numeric Discord ID (10-20 digits).")
    return text


def validate_channel_ids(raw: str | Iterable[str]) -> tuple[str, ...]:
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.replace("，", ",").split(",")]
    else:
        parts = [str(p).strip() for p in raw]
    clean = tuple(dict.fromkeys(p for p in parts if p))
    if not clean:
        raise ValidationError("❌ Provide at least one channel ID.")
    if len(clean) > MAX_CHANNELS_PER_ALT:
        raise ValidationError(channel_limit_message())
    for cid in clean:
        validate_snowflake(cid, "Channel ID")
    return clean


def extract_price(text: str) -> float | None:
    m = _PRICE.search(str(text or ""))
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def validate_price(text: str) -> float:
    value = extract_price(text)
    if value is None or not math.isfinite(value) or not PRICE_MIN_EXCLUSIVE < value <= PRICE_MAX:
        raise ValidationError("❌ Price must be a number between 0 and 20; example `2.30`.")
    return round(value, 2)


def validate_deal_delta(text: str) -> float:
    try:
        value = float(str(text).strip())
    except (TypeError, ValueError):
        value = -1.0
    if not math.isfinite(value) or value < 0 or value > DEAL_DELTA_MAX:
        raise ValidationError("❌ Delta must be between 0 and 5 dollars per 1k; example `0.05`.")
    return round(value, 2)


def validate_keywords(raw: str) -> tuple[str, ...]:
    items: list[str] = []
    seen: set[str] = set()
    for part in str(raw or "").split(","):
        item = re.sub(r"\s+", " ", part.strip())[:MAX_KEYWORD_CHARS]
        key = item.casefold()
        if item and key not in seen:
            seen.add(key)
            items.append(item)
    if not items:
        raise ValidationError("❌ Provide at least one comma-separated item keyword.")
    if len(items) > MAX_KEYWORDS:
        raise ValidationError(f"❌ Use at most {MAX_KEYWORDS} item keywords.")
    if len(", ".join(items)) > MAX_KEYWORDS_TOTAL_CHARS:
        raise ValidationError(f"❌ Combined keyword length cannot exceed {MAX_KEYWORDS_TOTAL_CHARS} characters.")
    return tuple(items)


def validate_message(text: str, *, limit: int = MAX_MESSAGE_CHARS) -> str:
    clean = str(text or "").strip()
    if not clean:
        raise ValidationError("❌ Message cannot be empty.")
    if len(clean) > limit:
        raise ValidationError(f"❌ Message too long; maximum is {limit} characters.")
    return clean


def validate_autoreply(text: str) -> str:
    clean = validate_message(text, limit=MAX_AUTOREPLY_CHARS)
    # Never let a relay ping everyone.
    return re.sub(r"@(everyone|here)", r"(mention:\1)", clean, flags=re.I)


def validate_interval(value: int | str) -> int:
    try:
        iv = int(value)
    except (TypeError, ValueError):
        iv = 0
    if iv not in INTERVALS_MIN:
        raise ValidationError("❌ Interval must be 3 or 5 minutes.")
    return iv


def validate_runtime(value: int | str) -> int:
    try:
        hours = int(value)
    except (TypeError, ValueError):
        hours = -1
    if hours not in RUNTIMES_HOURS:
        raise ValidationError("❌ Runtime must be 0 (Limitless), 6, 12, 18, 24, or 48 hours.")
    return hours


def validate_ad_type(value: str) -> str:
    mode = str(value or "").strip().lower()
    if mode not in AD_TYPES:
        raise ValidationError("❌ Mode must be sell or buy.")
    return mode


def validate_policy(value: str) -> str:
    template = str(value or "").strip().lower()
    if template not in POLICY_TEMPLATES:
        raise ValidationError("❌ Policy must be one of stealth, aggressive, peak_hour, balanced.")
    return template


def validate_days(value: int | str, *, minimum: int = 1) -> int:
    try:
        days = int(value)
    except (TypeError, ValueError):
        days = 0
    if days < minimum or days > MAX_SUBSCRIPTION_DAYS:
        raise ValidationError(f"❌ Days must be between {minimum} and {MAX_SUBSCRIPTION_DAYS}.")
    return days


def validate_alt_count(value: int | str) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = 0
    if not 1 <= count <= MAX_ALTS_PER_CUSTOMER:
        raise ValidationError(f"❌ Alt count must be between 1 and {MAX_ALTS_PER_CUSTOMER}.")
    return count


def validate_alt_index(value: int | str, alt_count: int) -> int:
    try:
        idx = int(value)
    except (TypeError, ValueError):
        idx = 0
    if not 1 <= idx <= max(1, alt_count):
        raise ValidationError(f"❌ Alt must be between 1 and {max(1, alt_count)}.")
    return idx


def validate_confirmation(value: str, expected: str) -> None:
    if str(value or "").strip().upper() != expected.upper():
        raise ValidationError(f"❌ Type `{expected}` exactly in the confirmation field to proceed.")


def policy_defaults(template: str) -> dict[str, object]:
    """Runtime knobs implied by a policy template (mirrors the sender's expectations)."""
    return {
        "stealth": {"interval_min": 5, "deal_scan_enabled": False},
        "aggressive": {"interval_min": 3, "deal_scan_enabled": True, "deal_alert_delta": 0.05},
        "peak_hour": {"interval_min": 3, "deal_scan_enabled": True, "deal_alert_delta": 0.03},
        "balanced": {"interval_min": 5, "deal_scan_enabled": True, "deal_alert_delta": 0.05},
    }[template]


def repo_name_for(username: str, alt_index: int) -> str:
    safe = re.sub(r"[^a-z0-9_-]", "", str(username or "").lower().replace(" ", "_")) or "customer"
    return f"{safe[:60]}_alt{int(alt_index)}"
