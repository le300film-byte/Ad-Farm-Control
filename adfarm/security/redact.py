"""Redaction helpers — nothing that looks like a credential reaches a log line or an alert."""
from __future__ import annotations

import re

_PATTERNS = [
    re.compile(r"(gh[pousr]_[A-Za-z0-9]{20,})"),                                  # GitHub PATs
    re.compile(r"(github_pat_[A-Za-z0-9_]{20,})"),                                # fine-grained PATs
    re.compile(r"([A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{25,110})"),  # Discord tokens
    re.compile(r"(https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/\d+/)([A-Za-z0-9_-]{20,})"),  # webhook URLs
    re.compile(r"(v1:[A-Za-z0-9_=-]{60,})"),                                       # sealed vault blobs
]


def redact(text: str) -> str:
    out = str(text or "")
    for pattern in _PATTERNS:
        if pattern.groups == 2:
            out = pattern.sub(lambda m: m.group(1) + "***", out)
        else:
            out = pattern.sub(lambda m: m.group(1)[:6] + "***", out)
    return out


def mask(secret: str, keep: int = 4) -> str:
    s = str(secret or "")
    if not s:
        return ""
    if len(s) <= keep * 2:
        return "***"
    return f"{s[:keep]}…{s[-keep:]}"
