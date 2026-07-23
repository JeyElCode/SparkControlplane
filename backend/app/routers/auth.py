from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from ..config import get_settings
from ..services import auth as auth_svc

log = logging.getLogger("spark.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    username: str
    password: str


class MeOut(BaseModel):
    auth_mode: str          # none | password | ldap (as configured)
    auth_required: bool
    authenticated: bool
    user: str | None = None


@router.get("/me", response_model=MeOut)
async def me(request: Request):
    settings = get_settings()
    mode = settings.effective_auth_mode
    if mode == "none":
        return MeOut(auth_mode="none", auth_required=False, authenticated=True)
    user = auth_svc.parse_session(request.cookies.get(auth_svc.COOKIE_NAME))
    return MeOut(auth_mode=mode, auth_required=True,
                 authenticated=user is not None, user=user)


@router.post("/login", response_model=MeOut)
async def login(payload: LoginIn, request: Request, response: Response):
    settings = get_settings()
    mode = settings.effective_auth_mode
    if mode == "none":
        return MeOut(auth_mode="none", auth_required=False, authenticated=True)
    ip = request.client.host if request.client else "?"
    wait = auth_svc.check_throttle(ip)
    if wait > 0:
        raise HTTPException(429, f"Too many failed attempts — try again in {wait:.0f}s.")
    try:
        user = await auth_svc.verify_login(payload.username, payload.password)
    except auth_svc.AuthError as exc:
        auth_svc.record_attempt(ip, ok=False)
        raise HTTPException(401, str(exc))
    auth_svc.record_attempt(ip, ok=True)
    log.info("login ok: %s (%s mode)", user, mode)
    response.set_cookie(
        auth_svc.COOKIE_NAME,
        auth_svc.create_session(user),
        max_age=int(settings.auth_session_hours * 3600),
        httponly=True,
        samesite="lax",
        secure=settings.auth_cookie_secure,
        path="/",
    )
    return MeOut(auth_mode=mode, auth_required=True, authenticated=True, user=user)


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(auth_svc.COOKIE_NAME, path="/")
    return {"ok": True}
