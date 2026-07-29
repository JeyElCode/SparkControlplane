"""Per-client gateway credentials: issue, verify, revoke.

Before this, every consumer of the ``/v1`` gateway held the same bearer token,
so offboarding one client (or containing one leak) meant rotating for everyone
at once — a coordinated outage — and per-client attribution was structurally
impossible because nothing in the request identified the caller.

**Verification runs on every gateway request**, so the hot path is a dict lookup
against an in-memory map, not a database round trip. The map is loaded at
startup and refreshed whenever a key is created, edited or revoked; a revocation
therefore takes effect immediately, not after a cache TTL.

The shared token from before this existed keeps working unchanged — it simply
resolves to a "legacy shared token" principal, so an upgrade never breaks a
client that is already configured.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .. import db as _db
from ..models import ApiKey

log = logging.getLogger("spark.apikeys")

__all__ = [
    "Principal",
    "LEGACY_CLIENT",
    "SESSION_CLIENT",
    "ANONYMOUS_CLIENT",
    "generate_token",
    "digest",
    "digest_wire",
    "wire_bytes",
    "issue",
    "refresh_cache",
    "lookup",
    "principal_for_key",
    "touch",
    "persist_last_used",
]

# Names used for the two principals that are not ApiKey rows, so attribution and
# rate limiting can talk about them like any other client.
LEGACY_CLIENT = "shared token"
SESSION_CLIENT = "portal session"
# A request rejected before it could be identified. Its own bucket, so a client
# hammering with a stale token is visible rather than smeared over the shared
# token's numbers.
ANONYMOUS_CLIENT = "unauthenticated"

_PREFIX = "sk-spark-"


@dataclass(frozen=True)
class Principal:
    """Who is making this gateway request."""

    client: str                  # display name, used for metrics + attribution
    key_id: int | None = None    # None for the shared token / portal session
    max_concurrent: int | None = None  # per-key overrides; None = global default
    max_rpm: int | None = None


def generate_token() -> tuple[str, str]:
    """Return ``(token, prefix)`` for a new key.

    ``sk-`` keeps OpenAI client libraries and secret scanners happy; the
    ``spark`` marker makes a leaked key attributable to this product on sight.
    The 8-hex id is generated separately from the secret, so the stored,
    displayable prefix discloses no secret bits at all.
    """
    key_id = secrets.token_hex(4)
    prefix = f"{_PREFIX}{key_id}"
    return f"{prefix}-{secrets.token_urlsafe(32)}", prefix


def wire_bytes(header_value: str) -> bytes:
    """Recover the exact bytes a client put on the wire.

    ASGI servers decode header values as **latin-1**, so a UTF-8 token like
    ``nøkkel`` arrives as the mojibake ``nÃ¸kkel``. Re-encoding *that* as UTF-8
    yields four bytes where the client sent two, so a correct token would never
    match — a permanent 401 rather than the TypeError crash, but still wrong.
    latin-1 is lossless for anything a latin-1 decode produced, so it round-trips
    exactly.
    """
    try:
        return header_value.encode("latin-1")
    except UnicodeEncodeError:  # not from a latin-1 decode (e.g. a test passing str)
        return header_value.encode("utf-8")


def digest(token: str) -> str:
    """SHA-256 of a *stored* token string (always UTF-8)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def digest_wire(header_value: str) -> str:
    """SHA-256 of a token as presented in an Authorization header."""
    return hashlib.sha256(wire_bytes(header_value)).hexdigest()


# --- in-memory verification map -------------------------------------------
# sha256 -> Principal. Rebuilt on every mutation, so a revoked key stops working
# on the next request rather than whenever a TTL happens to expire.
_BY_DIGEST: dict[str, Principal] = {}


def _principal(row: ApiKey) -> Principal:
    return Principal(
        client=row.label,
        key_id=row.id,
        max_concurrent=row.max_concurrent,
        max_rpm=row.max_rpm,
    )


async def refresh_cache(session: AsyncSession | None = None) -> int:
    """Reload the digest map from the database. Returns the number of live keys."""

    async def _load(s: AsyncSession) -> int:
        rows = list(
            (await s.execute(select(ApiKey).where(ApiKey.enabled.is_(True))))
            .scalars()
            .all()
        )
        _BY_DIGEST.clear()
        _BY_DIGEST.update({r.token_sha256: _principal(r) for r in rows})
        return len(rows)

    if session is not None:
        return await _load(session)
    async with _db.SessionLocal() as s:
        return await _load(s)


def lookup(token: str) -> Principal | None:
    """Resolve a presented token to a principal. Pure dict lookup — no I/O.

    The digest is compared by dict membership rather than a byte-by-byte scan,
    so there is no per-candidate timing signal to exploit; and a SHA-256 of the
    presented token leaks nothing about a stored one.
    """
    if not token:
        return None
    return _BY_DIGEST.get(digest_wire(token))


def principal_for_key(row: ApiKey) -> Principal:
    return _principal(row)


# key_id -> unix ts of last use. Written on the hot path (a dict assignment),
# persisted by the gateway stats collector — a DB write per request would put
# the gateway in contention with the telemetry loops for the SQLite writer.
_LAST_USED: dict[int, float] = {}


def touch(key_id: int | None) -> None:
    if key_id is not None:
        _LAST_USED[key_id] = time.time()


async def persist_last_used(session: AsyncSession) -> int:
    """Flush buffered last-used timestamps. Called by the stats collector."""
    if not _LAST_USED:
        return 0
    pending = dict(_LAST_USED)
    _LAST_USED.clear()
    for key_id, ts in pending.items():
        await session.execute(
            update(ApiKey)
            .where(ApiKey.id == key_id)
            .values(last_used_at=datetime.fromtimestamp(ts, tz=timezone.utc))
        )
    return len(pending)


async def issue(
    session: AsyncSession,
    label: str,
    *,
    max_concurrent: int | None = None,
    max_rpm: int | None = None,
) -> tuple[ApiKey, str]:
    """Create a key. Returns ``(row, token)`` — the token is the ONLY time the
    caller can ever see it."""
    token, prefix = generate_token()
    row = ApiKey(
        label=label,
        prefix=prefix,
        token_sha256=digest(token),
        enabled=True,
        max_concurrent=max_concurrent,
        max_rpm=max_rpm,
    )
    session.add(row)
    await session.commit()
    await refresh_cache(session)
    log.info("issued gateway API key %s (%s)", prefix, label)
    return row, token
