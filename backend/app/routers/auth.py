from __future__ import annotations

import asyncio
import hmac
import logging

import json

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from ..config import get_settings
from ..crypto import decrypt_cookie, encrypt
from ..services import auth as auth_svc
from ..services import oidc as oidc_svc
from ..services import sessions as sessions_svc

log = logging.getLogger("spark.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])


def cookie_secure(request: Request) -> bool:
    """Resolve the Secure flag for this request.

    Reading X-Forwarded-Proto directly is necessary, not sloppy: the image runs
    uvicorn without --forwarded-allow-ips, so behind a k8s ingress the peer is
    the ingress pod and uvicorn ignores forwarded headers — `request.url.scheme`
    reads "http" in exactly the deployment where Secure matters.

    Trusting that header *for this decision* is safe, which is unusual. The only
    lie that matters is claiming http on an HTTPS deployment, and it rides on
    the liar's own request, so it weakens only the liar's own cookie. Stripping
    Secure from a *victim's* cookie would mean injecting headers into the
    victim's request — i.e. already being on-path inside TLS.
    """
    mode = get_settings().auth_cookie_secure
    if mode == "true":
        return True
    if mode == "false":
        return False
    if request.url.scheme == "https":
        return True
    fwd = request.headers.get("x-forwarded-proto", "")
    return fwd.split(",")[0].strip().lower() == "https"


class LoginIn(BaseModel):
    username: str
    password: str


class MeOut(BaseModel):
    auth_mode: str          # none | password | ldap | oidc (as configured)
    auth_required: bool
    authenticated: bool
    user: str | None = None
    # oidc only: where the browser goes to start a sign-in, and why it can't.
    login_url: str | None = None
    config_error: str | None = None


@router.get("/me", response_model=MeOut)
async def me(request: Request):
    settings = get_settings()
    mode = settings.effective_auth_mode
    if mode == "none":
        return MeOut(auth_mode="none", auth_required=False, authenticated=True)
    user = auth_svc.parse_session(request.cookies.get(auth_svc.COOKIE_NAME))
    out = MeOut(auth_mode=mode, auth_required=True,
                authenticated=user is not None, user=user)
    if mode == "oidc":
        # Deliberately no issuer / client_id / tenant here: /api/auth/ is an
        # open prefix, so anything in this response is readable by an
        # unauthenticated scanner. A relative start URL and a generic error are
        # all the SPA needs.
        out.login_url = OIDC_LOGIN_PATH
        out.config_error = settings.oidc_config_error
    return out


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
        secure=cookie_secure(request),
        path="/",
    )
    return MeOut(auth_mode=mode, auth_required=True, authenticated=True, user=user)


@router.post("/logout")
async def logout(request: Request, response: Response):
    settings = get_settings()
    # Deleting the cookie is a *request* to a cooperating browser: a copy taken
    # beforehand keeps working until it expires. Kill the session properly. The
    # request presenting the cookie tells us its own id, so no registry of
    # issued sessions is needed.
    claims = auth_svc.session_claims(request.cookies.get(auth_svc.COOKIE_NAME))
    if claims and claims.get("jti"):
        await sessions_svc.revoke_jti(claims["jti"], float(claims.get("exp", 0)))
    response.delete_cookie(auth_svc.COOKIE_NAME, path="/")
    redirect = None
    if settings.effective_auth_mode == "oidc":
        # Ending the provider's session too — otherwise "sign out" leaves the
        # IdP happy to sign the user straight back in without a prompt.
        redirect = oidc_svc.end_session_url(settings.oidc_post_logout_redirect_url)
    return {"ok": True, "redirect": redirect}


# --- OIDC / SSO -----------------------------------------------------------
# These live under the already-open /api/auth/ prefix (middleware.py), which is
# required: the callback arrives from the identity provider with no session.
OIDC_LOGIN_PATH = "/api/auth/oidc/login"
TX_COOKIE = "spark_oidc_tx"
TX_TTL_SECONDS = 600
# Bounded concurrency on an unauthenticated endpoint that does crypto and makes
# outbound calls. Deliberately NOT a global failure counter with backoff: that
# would let anyone lock every operator out by failing callbacks in a loop.
_exchange_slots = asyncio.Semaphore(4)


def _require_oidc(settings) -> None:
    if settings.effective_auth_mode != "oidc":
        raise HTTPException(404, "OIDC is not enabled.")
    if (err := settings.oidc_config_error):
        raise HTTPException(500, err)


@router.get("/oidc/login")
async def oidc_login(request: Request):
    """Begin a sign-in: redirect to the provider with PKCE + state + nonce."""
    settings = get_settings()
    _require_oidc(settings)
    try:
        await oidc_svc.discovery()          # populates the metadata cache
        tx = oidc_svc.new_transaction()
        url = oidc_svc.authorization_url(tx)
    except oidc_svc.OidcError as exc:
        raise HTTPException(502, str(exc))

    redirect = RedirectResponse(url, status_code=302)
    # The transaction lives in a short-lived encrypted cookie rather than
    # server-side state: a store on an unauthenticated endpoint is a memory
    # target, and it would pin the portal to a single replica. `t` tags it so it
    # can never be mistaken for a session — same Fernet key, different purpose.
    redirect.set_cookie(
        TX_COOKIE,
        encrypt(json.dumps({"t": "oidc_tx", "s": tx.state, "n": tx.nonce, "v": tx.verifier})),
        max_age=TX_TTL_SECONDS,
        httponly=True,
        # Lax, not Strict: the callback is a cross-site top-level GET from the
        # provider, and Strict would not send this cookie — which is what pushes
        # people to SameSite=None. Do not.
        samesite="lax",
        secure=cookie_secure(request),
        path="/api/auth/oidc",
    )
    return redirect


@router.get("/oidc/callback")
async def oidc_callback(request: Request):
    """Finish a sign-in. Reached with no session, by design."""
    settings = get_settings()
    _require_oidc(settings)

    if request.query_params.get("error"):
        # Never reflect the provider's error text — it would be attacker-
        # influenced content rendered in our own page.
        log.warning("oidc callback returned an error: %s",
                    request.query_params.get("error"))
        return _oidc_failure("The identity provider did not complete the sign-in.")

    code = request.query_params.get("code")
    state = request.query_params.get("state")
    # response_type=code: the callback carries a code and state, nothing else.
    # An id_token/access_token in the query is front-channel injection and is
    # ignored rather than trusted.
    if not code or not state:
        return _oidc_failure("This sign-in link is incomplete. Please start again.")

    raw_tx = decrypt_cookie(request.cookies.get(TX_COOKIE), ttl=TX_TTL_SECONDS)
    if not raw_tx:
        return _oidc_failure("Your sign-in took too long or was started elsewhere. Try again.")
    try:
        tx = json.loads(raw_tx)
    except ValueError:
        return _oidc_failure("Your sign-in could not be completed. Try again.")
    if tx.get("t") != "oidc_tx":
        return _oidc_failure("Your sign-in could not be completed. Try again.")

    # Compare state BEFORE any network call, so a forged callback never costs a
    # round trip to the provider — and never burns a real authorization code.
    if not hmac.compare_digest(str(tx.get("s", "")), state):
        log.warning("oidc callback state mismatch")
        return _oidc_failure("This sign-in could not be verified. Please start again.")

    try:
        async with _exchange_slots:
            id_token = await oidc_svc.exchange_code(code, tx.get("v", ""))
            claims = await oidc_svc.validate_id_token(id_token, nonce=tx.get("n", ""))
    except oidc_svc.OidcError as exc:
        log.warning("oidc sign-in rejected: %s", exc)
        return _oidc_failure(str(exc))

    ok, why = oidc_svc.authorized(claims)
    if not ok:
        log.warning("oidc authorization denied for sub=%s: %s", claims.get("sub"), why)
        return _oidc_failure(why)

    user = oidc_svc.username_from_claims(claims)
    log.info("login ok: %s (oidc)", user)
    # Always back to the app root: a return-path parameter is the open-redirect
    # surface, and a single-page portal loses nothing without it.
    done = RedirectResponse("/", status_code=302)
    done.set_cookie(
        auth_svc.COOKIE_NAME,
        auth_svc.create_session(user, mode="oidc"),
        max_age=int(auth_svc.session_seconds()),
        httponly=True,
        samesite="lax",
        secure=cookie_secure(request),
        path="/",
    )
    done.delete_cookie(TX_COOKIE, path="/api/auth/oidc")
    return done


def _oidc_failure(message: str) -> RedirectResponse:
    """Send the browser back to the SPA with a short, safe reason."""
    from urllib.parse import quote

    resp = RedirectResponse(f"/?sso_error={quote(message[:200])}", status_code=302)
    resp.delete_cookie(TX_COOKIE, path="/api/auth/oidc")
    return resp
