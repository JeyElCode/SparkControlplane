"""Symmetric encryption for secrets stored at rest (SSH/sudo passwords,
private keys, HF token, vLLM API keys).

The master key comes from ``SPARK_SECRET_KEY`` if set, otherwise a key is
generated once and persisted to ``<data_dir>/secret.key`` (mode 0600). Losing
the key makes stored secrets unrecoverable — back it up if you set encrypted
secrets you care about.
"""

from __future__ import annotations

import logging
import os

from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings

log = logging.getLogger("spark.crypto")

_fernet: Fernet | None = None


def _load_or_create_key() -> bytes:
    settings = get_settings()
    if settings.secret_key:
        key = settings.secret_key.encode()
        # Validate it is a usable Fernet key.
        Fernet(key)
        return key

    path = settings.secret_key_path
    if os.path.exists(path):
        with open(path, "rb") as fh:
            return fh.read().strip()

    key = Fernet.generate_key()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(key)
    os.chmod(path, 0o600)
    log.warning(
        "Generated a new encryption key at %s. Set SPARK_SECRET_KEY or back up "
        "this file; without it, stored secrets cannot be decrypted.",
        path,
    )
    return key


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_or_create_key())
    return _fernet


def encrypt(plaintext: str | None) -> str | None:
    """Encrypt a secret. ``None``/empty passes through as ``None``."""
    if not plaintext:
        return None
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str | None) -> str | None:
    """Decrypt a secret produced by :func:`encrypt`. ``None`` passes through."""
    if not token:
        return None
    try:
        return _get_fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        log.error("Failed to decrypt a stored secret (wrong/rotated key?).")
        raise
