"""Ending portal sessions."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from ..config import get_settings
from ..schemas import RevocationStatus, RevokeIn, RevokeResult
from ..services import auth as auth_svc
from ..services import sessions as sessions_svc
from .auth import cookie_secure

log = logging.getLogger("spark.sessions")

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.get("", response_model=RevocationStatus)
async def revocation_status(request: Request):
    settings = get_settings()
    st = sessions_svc.status()
    return RevocationStatus(
        loaded=st["loaded"],
        global_not_before=st["global_not_before"],
        users=st["users"],
        revoked_session_count=st["revoked_session_count"],
        cookie_secure_mode=settings.auth_cookie_secure,
        # What this very request resolved to — the only way an operator can see
        # whether `auto` is doing what they expect behind their ingress.
        cookie_secure_effective=cookie_secure(request),
    )


@router.post("/revoke", response_model=RevokeResult)
async def revoke(payload: RevokeIn, request: Request):
    """End sessions.

    - no username: everything belonging to the caller ("sign out everywhere")
    - a username: everything belonging to that user (offboarding, leaked cookie)
    - ``everyone``: the panic button
    """
    if get_settings().effective_auth_mode == "none":
        raise HTTPException(409, "Portal auth is off, so there are no sessions to revoke.")
    me = auth_svc.parse_session(request.cookies.get(auth_svc.COOKIE_NAME))

    if payload.everyone:
        cutoff = await sessions_svc.revoke_everyone(payload.reason or "signed out everyone")
        log.warning("all portal sessions revoked by %s", me)
        return RevokeResult(
            subject="(everyone)", not_before=cutoff,
            detail="Every session is now invalid, including yours. Everyone must sign in again.",
        )

    target = (payload.username or me or "").strip()
    if not target:
        raise HTTPException(400, "No session to revoke.")
    cutoff = await sessions_svc.revoke_user(target, payload.reason or "signed out everywhere")
    mine = target == me
    return RevokeResult(
        subject=target, not_before=cutoff,
        detail=(
            "All of your sessions are now invalid, including this one."
            if mine else
            f"All sessions for '{target}' are now invalid. Note that this does not "
            f"disable the account in your directory — it only ends access here."
        ),
    )
