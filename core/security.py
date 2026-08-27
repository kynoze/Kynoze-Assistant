"""Encrypt session strings and bot tokens at rest.

Key comes from Config.SESSION_ENC_KEY (env). Nothing is hardcoded.
Existing plaintext values still decrypt as pass-through.
encrypt raises RuntimeError if no key is set (refuses plaintext store).
Optimized for Python 3.14 using PyNaCl (libsodium wrapper).
"""

from __future__ import annotations

import base64
import hashlib
import logging
from typing import Optional

from config import Config

logger = logging.getLogger(__name__)

_PREFIX = "enc:v1:"
_warned = False


def _secret() -> str:
    if hasattr(Config, "SESSION_ENC_KEY"):
        return (Config.SESSION_ENC_KEY or "").strip()
    return (getattr(Config, "SESSION_SECRET", None) or "").strip()


def _secret_box():
    global _warned
    secret = _secret()
    if not secret:
        if not _warned:
            logger.warning("SESSION_ENC_KEY is not set")
            _warned = True
        return None
    try:
        from nacl.secret import SecretBox
    except ImportError:
        if not _warned:
            logger.warning("pynacl not installed")
            _warned = True
        return None
        
    # PyNaCl SecretBox requires a strictly 32-byte key
    key = hashlib.sha256(secret.encode("utf-8")).digest()
    return SecretBox(key)


def encrypt_secret(value: Optional[str]) -> Optional[str]:
    if not value:
        return value
    if value.startswith(_PREFIX):
        return value
    box = _secret_box()
    if box is None:
        raise RuntimeError("SESSION_ENC_KEY is not set; refusing to store plaintext")
    
    # SecretBox handles nonces automatically when using .encrypt()
    encrypted_bytes = box.encrypt(value.encode("utf-8"))
    token = base64.urlsafe_b64encode(encrypted_bytes).decode("ascii")
    return _PREFIX + token


def decrypt_secret(value: Optional[str]) -> Optional[str]:
    if not value:
        return value
    if not value.startswith(_PREFIX):
        return value  # plaintext pass-through
    box = _secret_box()
    if box is None:
        logger.error("Encrypted secret found but SESSION_ENC_KEY/pynacl unavailable")
        return value
    try:
        raw = value[len(_PREFIX):]
        encrypted_bytes = base64.urlsafe_b64decode(raw.encode("ascii"))
        return box.decrypt(encrypted_bytes).decode("utf-8")
    except Exception:
        logger.exception("Failed to decrypt secret")
        return value


def encrypt_session(value: Optional[str]) -> Optional[str]:
    return encrypt_secret(value)


def decrypt_session(value: Optional[str]) -> Optional[str]:
    return decrypt_secret(value)
