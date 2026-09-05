"""TokenVault — authenticated encryption for secrets that must live in the database.

The legacy stored alt tokens as ``base64(xor(token, key))`` which is reversible by anyone who
can read the Gist backup. This vault uses only the standard library:

* key derivation: PBKDF2-HMAC-SHA256(master, salt, 200k) → 64 bytes = enc key ‖ mac key
* confidentiality: HMAC-SHA256 counter-mode keystream (a PRF in CTR mode is a stream cipher)
* integrity: HMAC-SHA256 over version ‖ salt ‖ nonce ‖ ciphertext (encrypt-then-MAC)

Format: ``v1:<base64(salt16 ‖ nonce16 ‖ ciphertext ‖ tag32)>``.  A vault without a master key
refuses to seal (never falls back to plaintext) and reports ``available == False`` so callers can
skip the DB copy while still pushing the secret to GitHub.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

_VERSION = b"v1"
_ITERATIONS = 200_000
_SALT_LEN = 16
_NONCE_LEN = 16
_TAG_LEN = 32


class VaultError(Exception):
    pass


class TokenVault:
    def __init__(self, master_key: str | bytes | None, *, cache_size: int = 256):
        if isinstance(master_key, str):
            master_key = master_key.strip().encode("utf-8")
        self._master = bytes(master_key or b"")
        self._cache: dict[bytes, tuple[bytes, bytes]] = {}
        self._cache_size = int(cache_size)

    @property
    def available(self) -> bool:
        return len(self._master) >= 8

    # ── primitives ──────────────────────────────────────────────────────────
    def _derive(self, salt: bytes) -> tuple[bytes, bytes]:
        """PBKDF2 is deliberately slow; derived keys are memoised per salt (in-memory only)."""
        cached = self._cache.get(salt)
        if cached is not None:
            return cached
        material = hashlib.pbkdf2_hmac("sha256", self._master, salt, _ITERATIONS, dklen=64)
        keys = (material[:32], material[32:])
        if len(self._cache) >= self._cache_size:
            self._cache.pop(next(iter(self._cache)))
        self._cache[salt] = keys
        return keys

    @staticmethod
    def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
        out = bytearray()
        counter = 0
        while len(out) < length:
            out += hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
            counter += 1
        return bytes(out[:length])

    # ── API ─────────────────────────────────────────────────────────────────
    def seal(self, plaintext: str) -> str:
        if not self.available:
            raise VaultError("TOKEN_VAULT_KEY is not configured (need ≥ 8 characters)")
        data = plaintext.encode("utf-8")
        salt = secrets.token_bytes(_SALT_LEN)
        nonce = secrets.token_bytes(_NONCE_LEN)
        enc_key, mac_key = self._derive(salt)
        stream = self._keystream(enc_key, nonce, len(data))
        cipher = bytes(a ^ b for a, b in zip(data, stream))
        tag = hmac.new(mac_key, _VERSION + salt + nonce + cipher, hashlib.sha256).digest()
        blob = base64.urlsafe_b64encode(salt + nonce + cipher + tag).decode("ascii")
        return f"{_VERSION.decode()}:{blob}"

    def open(self, sealed: str) -> str:
        if not self.available:
            raise VaultError("TOKEN_VAULT_KEY is not configured")
        try:
            version, blob = sealed.split(":", 1)
            raw = base64.urlsafe_b64decode(blob.encode("ascii"))
        except (ValueError, AttributeError) as exc:
            raise VaultError("malformed sealed value") from exc
        if version.encode() != _VERSION or len(raw) < _SALT_LEN + _NONCE_LEN + _TAG_LEN:
            raise VaultError("unsupported or truncated sealed value")
        salt, nonce = raw[:_SALT_LEN], raw[_SALT_LEN:_SALT_LEN + _NONCE_LEN]
        cipher, tag = raw[_SALT_LEN + _NONCE_LEN:-_TAG_LEN], raw[-_TAG_LEN:]
        enc_key, mac_key = self._derive(salt)
        expected = hmac.new(mac_key, _VERSION + salt + nonce + cipher, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, tag):
            raise VaultError("authentication failed (wrong key or tampered value)")
        stream = self._keystream(enc_key, nonce, len(cipher))
        return bytes(a ^ b for a, b in zip(cipher, stream)).decode("utf-8")

    def try_open(self, sealed: str) -> str | None:
        if not sealed:
            return None
        try:
            return self.open(sealed)
        except VaultError:
            return None

    @staticmethod
    def is_sealed(value: str) -> bool:
        return isinstance(value, str) and value.startswith(_VERSION.decode() + ":")


def fingerprint(secret: str) -> str:
    """Stable, non-reversible short id for a secret (safe for logs)."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]
