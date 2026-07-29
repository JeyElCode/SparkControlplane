"""Symmetric encryption for secrets stored at rest (SSH/sudo passwords,
private keys, HF token, vLLM API keys).

The master key comes from ``SPARK_SECRET_KEY`` if set, otherwise a key is
generated once and persisted to ``<data_dir>/secret.key`` (mode 0600). Losing
the key makes stored secrets unrecoverable — back it up if you set encrypted
secrets you care about.
"""

from __future__ import annotations

import base64
import binascii
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


def decrypt(token: str | None, *, ttl: int | None = None) -> str | None:
    """Decrypt a secret produced by :func:`encrypt`. ``None`` passes through.

    ``ttl`` (seconds) additionally rejects a token older than that, using the
    timestamp Fernet already embeds — cheaper and harder to get wrong than a
    second expiry field in the payload.
    """
    if not token:
        return None
    try:
        return _get_fernet().decrypt(token.encode(), ttl=ttl).decode()
    except InvalidToken:
        log.error("Failed to decrypt a stored secret (wrong/rotated key?).")
        raise


def _is_canonical_b64(token: str) -> bool:
    """Reject a token that is not the *exact* base64 of its own bytes.

    Python's base64 decoder silently discards anything after the ``=`` padding,
    so ``valid_token + "junk"`` decrypts to the same plaintext. That is not an
    auth bypass — an attacker appending junk already holds a valid token — but a
    credential with more than one accepted spelling is a wart worth removing,
    and it defeats naive equality checks elsewhere.
    """
    try:
        raw = base64.urlsafe_b64decode(token.encode())
    except (ValueError, binascii.Error):
        return False
    return base64.urlsafe_b64encode(raw).decode() == token


def decrypt_cookie(token: str | None, *, ttl: int | None = None) -> str | None:
    """Decrypt attacker-supplied input (a cookie), returning None on failure.

    Separate from :func:`decrypt` for two reasons. A tampered cookie is not an
    operational problem, so it must not log at ERROR — and the OIDC callback is
    an *unauthenticated* endpoint, so a raising, logging decrypt there hands
    anyone a way to fill the log. And "wrong/rotated key?" is simply the wrong
    diagnosis for a value the user's browser supplied.
    """
    if not token or not _is_canonical_b64(token):
        return None
    try:
        return _get_fernet().decrypt(token.encode(), ttl=ttl).decode()
    except Exception:  # noqa: BLE001 - garbage/tampered/expired: all just "no"
        return None
