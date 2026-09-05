"""Seal GitHub Actions secrets with the repository public key (libsodium sealed box).

PyNaCl is the only supported path. If it is missing, ``seal_secret`` raises
``ConfigurationError`` — the bot never falls back to plaintext or to the ``gh`` CLI.
"""
from __future__ import annotations

import base64

from ..core.errors import ConfigurationError


def sealing_available() -> bool:
    try:
        import nacl.public  # noqa: F401
    except Exception:
        return False
    return True


def seal_secret(public_key_b64: str, value: str) -> str:
    try:
        from nacl import encoding, public
    except Exception as exc:  # pragma: no cover - depends on the environment
        raise ConfigurationError("PyNaCl is not installed — cannot upload GitHub secrets") from exc
    pk = public.PublicKey(public_key_b64.encode("utf-8"), encoding.Base64Encoder())
    sealed = public.SealedBox(pk).encrypt(value.encode("utf-8"))
    return base64.b64encode(sealed).decode("utf-8")


def looks_like_token(value: str) -> bool:
    """Heuristic used by validation & redaction (Discord user tokens are 50-100 chars of base64ish)."""
    if not isinstance(value, str):
        return False
    text = value.strip()
    return 50 <= len(text) <= 120 and text.count(".") >= 2 and " " not in text
