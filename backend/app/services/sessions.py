"""Session revocation.

Sessions are stateless encrypted cookies, so "revoking" one cannot mean
deleting a record — there is nothing to delete. It has to be a rule that
:func:`app.services.auth.parse_session` consults, and that function runs
**synchronously on the event loop for every request, every WebSocket handshake
and every /v1 gateway call**. So the state here is memory-resident dicts,
loaded once at startup and rewritten on each change — the shape
``services/apikeys.py`` already uses.

**The failure polarity is the opposite of apikeys, and that must not be
copy-pasted away.** An unloaded API-key map denies everything, which is safe.
An unloaded revocation map would *allow* everything — silently un-revoking
every revoked session. So :func:`is_revoked` answers "yes" until the load has
actually happened, and the startup load is deliberately not wrapped in a
try/except: a portal that cannot read its revocation list must refuse to start
rather than serve with the safety off.

Three channels:

* **jti** — one specific session, from an explicit logout. The request that
  presents the cookie tells us its own id, so no registry of issued sessions is
  needed. Rows are swept once the token they kill would have expired anyway.
* **epoch** — every session for a user issued before an instant ("sign out
  everywhere", a suspected leak, offboarding). One row per revoked user, kept
  forever: growth is bounded by *distinct users ever revoked*, and an expiry
  bound that has to be derived is one more thing to get subtly wrong.
* **global epoch** — the panic button, ``subject == ""``.

A fourth channel needs no storage at all: the session carries a fingerprint of
the credential config, so rotating ``SPARK_ADMIN_PASSWORD`` invalidates every
old cookie by itself (see ``auth.config_fingerprint``).
"""

from __future__ import annotations

import logging
import secrets
import time

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import db as _db
from ..models import SessionRevocation

log = logging.getLogger("spark.sessions")

__all__ = [
    "new_jti", "load", "is_revoked", "revoke_jti", "revoke_user",
    "revoke_everyone", "status", "reset_for_tests", "GLOBAL_SUBJECT",
]

GLOBAL_SUBJECT = ""

# username -> sessions issued before this instant are dead
_EPOCHS: dict[str, float] = {}
# jti -> when the killed token would have expired (for sweeping)
_REVOKED: dict[str, float] = {}
# Until this is True, is_revoked() says yes to everything. See the module
# docstring: an unloaded map that answered "no" would silently un-revoke.
_loaded = False


def new_jti() -> str:
    return secrets.token_urlsafe(12)


def reset_for_tests() -> None:
    """Clear module state. Tests that simulate a restart MUST call this —
    ``importlib.reload(main)`` does not re-execute this module, so without it a
    'restart' test proves nothing at all."""
    global _loaded
    _EPOCHS.clear()
    _REVOKED.clear()
    _loaded = False


async def load(session: AsyncSession | None = None) -> int:
    """Read the revocation list into memory. Called once at startup."""

    async def _load(s: AsyncSession) -> int:
        global _loaded
        now = time.time()
        # Sweep jti rows whose tokens have expired on their own — from that
        # moment parse_session's own exp check rejects them without help.
        await s.execute(
            delete(SessionRevocation).where(
                SessionRevocation.kind == "jti",
                SessionRevocation.expires_at > 0,
                SessionRevocation.expires_at <= now,
            )
        )
        await s.commit()
        rows = list((await s.execute(select(SessionRevocation))).scalars().all())
        _EPOCHS.clear()
        _REVOKED.clear()
        for r in rows:
            if r.kind == "epoch":
                _EPOCHS[r.subject] = max(_EPOCHS.get(r.subject, 0.0), r.not_before)
            else:
                _REVOKED[r.subject] = r.expires_at
        _loaded = True
        return len(rows)

    if session is not None:
        return await _load(session)
    async with _db.SessionLocal() as s:
        return await _load(s)


def is_revoked(user: str, jti: str | None, issued_at: float) -> bool:
    """Sync, no I/O: three dict lookups and two float compares."""
    if not _loaded:
        return True
    if jti and jti in _REVOKED:
        return True
    if issued_at < _EPOCHS.get(GLOBAL_SUBJECT, 0.0):
        return True
    return issued_at < _EPOCHS.get(user, 0.0)


def epoch_for(user: str) -> float:
    """The instant before which this user's sessions are dead (0 = none).

    Used when minting a token, to keep a backward clock step from making the
    portal permanently unloggable-into.
    """
    return max(_EPOCHS.get(user, 0.0), _EPOCHS.get(GLOBAL_SUBJECT, 0.0))


async def _write(rows: list[SessionRevocation]) -> None:
    """Commit first, then update memory.

    Order matters and the alternative is worse in both directions: update the
    dict first and a failed commit leaves a session revoked in memory but
    silently alive again after the next restart. Commit first and a crash in
    between leaves it revoked in the database but not in memory — which the
    startup load then repairs. One of those self-heals; the other doesn't.
    """
    now = time.time()
    async with _db.SessionLocal() as s:
        for r in rows:
            s.add(r)
        await s.execute(
            delete(SessionRevocation).where(
                SessionRevocation.kind == "jti",
                SessionRevocation.expires_at > 0,
                SessionRevocation.expires_at <= now,
            )
        )
        await s.commit()
    for r in rows:
        if r.kind == "epoch":
            _EPOCHS[r.subject] = max(_EPOCHS.get(r.subject, 0.0), r.not_before)
        else:
            _REVOKED[r.subject] = r.expires_at
    for jti, exp in list(_REVOKED.items()):
        if 0 < exp <= now:
            del _REVOKED[jti]


async def revoke_jti(jti: str, expires_at: float, reason: str = "logout") -> None:
    """Kill one session — the one whose cookie was just presented."""
    if not jti:
        return
    await _write([SessionRevocation(
        kind="jti", subject=jti, not_before=0.0,
        expires_at=expires_at, reason=reason, created_at=time.time(),
    )])


async def revoke_user(user: str, reason: str = "signed out everywhere") -> float:
    """Kill every session belonging to ``user``. Returns the cutoff instant."""
    now = time.time()
    # Never move an epoch backwards: if the clock stepped back, a lower cutoff
    # would silently un-revoke sessions that were already dead.
    not_before = max(now, _EPOCHS.get(user, 0.0))
    await _write([SessionRevocation(
        kind="epoch", subject=user, not_before=not_before,
        expires_at=0.0, reason=reason, created_at=now,
    )])
    log.warning("revoked all sessions for %s (%s)", user or "(everyone)", reason)
    return not_before


async def revoke_everyone(reason: str = "signed out everyone") -> float:
    return await revoke_user(GLOBAL_SUBJECT, reason)


def status() -> dict:
    return {
        "loaded": _loaded,
        "global_not_before": _EPOCHS.get(GLOBAL_SUBJECT, 0.0) or None,
        "users": [
            {"username": u, "not_before": nb}
            for u, nb in sorted(_EPOCHS.items()) if u != GLOBAL_SUBJECT
        ],
        "revoked_session_count": len(_REVOKED),
    }
