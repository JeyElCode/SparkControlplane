"""OpenID Connect: authorization code + PKCE against an external provider.

The portal never sees a password — MFA and conditional access are enforced by
the identity provider — and it deliberately keeps **no** access or refresh
token: it needs an authenticated identity, not delegated API access, and a token
it never holds is one it can never leak.

Everything here is written on the assumption that a wrong answer is an auth
bypass rather than a bug, so the controls are stated explicitly rather than
inherited from a library's defaults:

* **Discovery endpoints are pinned same-origin with the issuer.** Without this,
  a hostile or compromised discovery response relocates ``token_endpoint`` and
  receives the authorization code *and* the client secret. This is the single
  biggest hole in a naive implementation.
* **Redirects are never followed** on discovery, JWKS or token exchange — a 302
  from the pinned host would walk the request straight back off-origin.
* **Signing keys come from the discovered JWKS, never from the token.** ``jku``,
  ``x5u`` and an embedded ``jwk`` header are ignored outright.
* **Only asymmetric algorithms**, enforced at config-parse time so the
  alg-confusion attack (the IdP's public key used as an HMAC secret) is not
  expressible.
* **A stale JWKS has a hard ceiling.** Serving stale across a blip is right;
  serving it forever means a key the IdP revoked stays trusted for the length of
  an outage.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx
import jwt
from jwt import PyJWK

from ..config import get_settings

log = logging.getLogger("spark.oidc")

__all__ = [
    "OidcError",
    "Transaction",
    "new_transaction",
    "authorization_url",
    "exchange_code",
    "validate_id_token",
    "username_from_claims",
    "authorized",
    "discovery",
    "reset_caches",
    "end_session_url",
]

# A JWKS/discovery document is small; anything larger is not one, and reading it
# before finding out is a memory-exhaustion foothold on an unauthenticated path.
_MAX_DOC_BYTES = 256 * 1024
_JWKS_MIN_REFRESH_SECONDS = 60.0


class OidcError(Exception):
    """Login failed. ``str(exc)`` is safe to show the user — it never contains
    provider-supplied text (which would be reflected content) or secrets."""


# --- transaction (state / nonce / PKCE) -----------------------------------
@dataclass(frozen=True)
class Transaction:
    state: str
    nonce: str
    verifier: str

    def challenge(self) -> str:
        digest = hashlib.sha256(self.verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def new_transaction() -> Transaction:
    return Transaction(
        state=secrets.token_urlsafe(32),
        nonce=secrets.token_urlsafe(32),
        # RFC 7636: 43-128 chars from the unreserved set. token_urlsafe(64)
        # yields 86.
        verifier=secrets.token_urlsafe(64),
    )


# --- discovery + JWKS ------------------------------------------------------
@dataclass
class _Discovery:
    doc: dict
    fetched_at: float


@dataclass
class _Jwks:
    keys: dict  # kid -> jwk dict
    fetched_at: float
    last_attempt: float = 0.0


@dataclass
class _Caches:
    discovery: _Discovery | None = None
    jwks: _Jwks | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_caches = _Caches()


def reset_caches() -> None:
    """Test helper — module state would otherwise leak between cases."""
    global _caches
    _caches = _Caches()


def _client() -> httpx.AsyncClient:
    settings = get_settings()
    # follow_redirects=False everywhere: a redirect from the pinned host is
    # exactly how an attacker would escape the same-origin endpoint check.
    return httpx.AsyncClient(
        timeout=settings.oidc_http_timeout_seconds, follow_redirects=False
    )


async def _get_json(url: str) -> dict:
    async with _client() as client:
        try:
            resp = await client.get(url, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise OidcError(f"Could not reach the identity provider: {exc}") from None
    if resp.status_code != 200:
        raise OidcError(f"Identity provider returned HTTP {resp.status_code} for {url}.")
    if len(resp.content) > _MAX_DOC_BYTES:
        raise OidcError("Identity provider response was implausibly large; refusing it.")
    try:
        data = resp.json()
    except ValueError:
        raise OidcError("Identity provider returned a non-JSON document.") from None
    if not isinstance(data, dict):
        raise OidcError("Identity provider returned an unexpected document shape.")
    return data


def _same_origin(url: str, issuer: str) -> bool:
    """Endpoints must live on the issuer's own origin.

    This is the control that stops a hostile discovery document from relocating
    the token endpoint to a host that then receives the authorization code and
    the client secret.
    """
    try:
        a, b = urlparse(url), urlparse(issuer)
    except ValueError:
        return False
    return (
        a.scheme == "https"
        and a.scheme == b.scheme
        and a.netloc.lower() == b.netloc.lower()
    )


async def discovery() -> dict:
    """The provider's OpenID configuration, cached, with its endpoints pinned."""
    settings = get_settings()
    issuer = (settings.oidc_issuer or "").rstrip("/")
    cached = _caches.discovery
    if cached and time.time() - cached.fetched_at < settings.oidc_jwks_ttl_seconds:
        return cached.doc

    doc = await _get_json(f"{issuer}/.well-known/openid-configuration")

    # The issuer in the document must be exactly the one we configured — this is
    # what ties everything that follows to an identity the operator chose.
    if str(doc.get("issuer", "")).rstrip("/") != issuer:
        raise OidcError(
            "The identity provider's discovery document declares a different issuer "
            "than SPARK_OIDC_ISSUER. Refusing to continue."
        )
    for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        url = doc.get(name)
        if not isinstance(url, str) or not _same_origin(url, issuer):
            raise OidcError(
                f"The identity provider's '{name}' is not on the issuer's own origin. "
                f"Refusing it — a relocated endpoint would receive the authorization "
                f"code and the client secret."
            )
    end_session = doc.get("end_session_endpoint")
    if end_session is not None and not (
        isinstance(end_session, str) and _same_origin(end_session, issuer)
    ):
        doc.pop("end_session_endpoint", None)  # ignorable; do not fail login over it

    _caches.discovery = _Discovery(doc=doc, fetched_at=time.time())
    return doc


async def _jwks_keys(*, force: bool = False) -> dict:
    settings = get_settings()
    now = time.time()
    cached = _caches.jwks
    fresh = cached and now - cached.fetched_at < settings.oidc_jwks_ttl_seconds
    if cached and fresh and not force:
        return cached.keys

    async with _caches.lock:
        cached = _caches.jwks  # may have been refreshed while we waited
        if cached and now - cached.fetched_at < settings.oidc_jwks_ttl_seconds and not force:
            return cached.keys
        # Rate-limit refreshes: an unauthenticated attacker replaying tokens
        # with random `kid`s would otherwise drive one outbound fetch each.
        if cached and now - cached.last_attempt < _JWKS_MIN_REFRESH_SECONDS:
            return cached.keys
        doc = await discovery()
        try:
            raw = await _get_json(doc["jwks_uri"])
            keys = {
                k["kid"]: k
                for k in raw.get("keys", [])
                if isinstance(k, dict) and k.get("kid")
                and k.get("use", "sig") == "sig"
            }
            if not keys:
                raise OidcError("The identity provider published no usable signing keys.")
            _caches.jwks = _Jwks(keys=keys, fetched_at=now, last_attempt=now)
            return keys
        except OidcError:
            if cached is None:
                raise
            cached.last_attempt = now
            age = now - cached.fetched_at
            if age > settings.oidc_jwks_max_stale_seconds:
                # Past the ceiling, refuse rather than keep trusting keys the
                # provider may have revoked during the outage.
                raise OidcError(
                    "Cannot reach the identity provider's key set, and the cached copy "
                    "is too old to keep trusting. Logins are blocked until it returns."
                ) from None
            log.warning("JWKS refresh failed; serving cached keys (%.0fs old)", age)
            return cached.keys


async def _signing_key(kid: str | None, alg: str) -> PyJWK:
    keys = await _jwks_keys()
    jwk = keys.get(kid) if kid else None
    if jwk is None and kid is not None:
        keys = await _jwks_keys(force=True)  # rotation: refresh once, rate-limited
        jwk = keys.get(kid)
    if jwk is None and kid is None:
        # No kid is only tolerable when the choice is unambiguous. Trying every
        # key in turn is how a rogue key gets accepted.
        if len(keys) != 1:
            raise OidcError("ID token has no 'kid' and the provider publishes several keys.")
        jwk = next(iter(keys.values()))
    if jwk is None:
        raise OidcError("ID token was signed with a key the provider does not publish.")
    # Pass the algorithm explicitly: PyJWK otherwise derives it from the JWK's
    # own fields, and that derived value is what the signature check binds to.
    return PyJWK.from_dict(jwk, algorithm=alg)


# --- the flow --------------------------------------------------------------
def authorization_url(tx: Transaction) -> str:
    from urllib.parse import urlencode

    settings = get_settings()
    doc = _caches.discovery.doc if _caches.discovery else None
    if doc is None:
        raise OidcError("Identity provider metadata has not been loaded yet.")
    params = {
        "response_type": "code",
        "client_id": settings.oidc_client_id,
        "redirect_uri": settings.oidc_redirect_url,
        "scope": settings.oidc_scopes,
        "state": tx.state,
        "nonce": tx.nonce,
        "code_challenge": tx.challenge(),
        "code_challenge_method": "S256",  # never "plain"
        "response_mode": "query",
    }
    return f"{doc['authorization_endpoint']}?{urlencode(params)}"


async def exchange_code(code: str, verifier: str) -> str:
    """Swap an authorization code for an ID token. Returns the raw JWT."""
    settings = get_settings()
    doc = await discovery()
    data = {
        "grant_type": "authorization_code",
        "code": code,
        # Byte-identical to the authorization request, per RFC 6749.
        "redirect_uri": settings.oidc_redirect_url,
        "client_id": settings.oidc_client_id,
        "client_secret": settings.oidc_client_secret,
        "code_verifier": verifier,
    }
    async with _client() as client:
        try:
            resp = await client.post(doc["token_endpoint"], data=data)
        except httpx.HTTPError as exc:
            raise OidcError(f"Token exchange failed to reach the provider: {exc}") from None
    if resp.status_code != 200:
        # Never reflect the provider's error text back to the browser.
        log.warning("token exchange failed: HTTP %s %s", resp.status_code, resp.text[:400])
        raise OidcError("The identity provider rejected the sign-in. Please try again.")
    try:
        body = resp.json()
    except ValueError:
        raise OidcError("The identity provider returned a malformed token response.") from None
    id_token = body.get("id_token")
    if not isinstance(id_token, str) or not id_token:
        raise OidcError("The identity provider did not return an ID token.")
    # The access token is deliberately discarded: this app authenticates a
    # human, it does not call the provider's APIs on their behalf.
    return id_token


async def validate_id_token(raw: str, *, nonce: str) -> dict:
    """Verify an ID token completely, or raise. Returns its claims."""
    settings = get_settings()
    allowed = settings.oidc_algorithms.split()
    try:
        header = jwt.get_unverified_header(raw)
    except jwt.InvalidTokenError:
        raise OidcError("The ID token is malformed.") from None
    alg = header.get("alg")
    if alg not in allowed:
        # Covers "none" and every HS* variant; the config validator has already
        # made those unexpressible, so this is the second wall.
        raise OidcError(f"ID token algorithm '{alg}' is not accepted.")
    # `jku`/`x5u`/`jwk` in the header name a key location. Fetching from them
    # would let the token choose its own verifier. They are ignored entirely.

    key = await _signing_key(header.get("kid"), alg)
    try:
        claims = jwt.decode(
            raw,
            key,
            algorithms=[alg],
            audience=settings.oidc_client_id,
            issuer=(settings.oidc_issuer or "").rstrip("/"),
            leeway=settings.oidc_clock_skew_seconds,
            options={
                "require": ["iss", "aud", "exp", "iat", "sub"],
                "verify_signature": True,
                "verify_exp": True,
                "verify_nbf": True,
                "verify_iat": True,
                "verify_aud": True,
                "verify_iss": True,
            },
        )
    except jwt.InvalidTokenError as exc:
        log.warning("ID token rejected: %s", exc)
        raise OidcError("The ID token failed validation.") from None

    # `azp` rather than strict_aud: a single-element `aud` array is legal (and
    # is what Entra emits), so strict_aud would reject spec-compliant providers.
    # The threat strict_aud aims at — a token minted for a *different* client of
    # the same tenant — is properly addressed here.
    aud = claims.get("aud")
    aud_list = [aud] if isinstance(aud, str) else list(aud or [])
    azp = claims.get("azp")
    if len(aud_list) > 1 and not azp:
        raise OidcError("ID token has multiple audiences and no 'azp'; refusing it.")
    if azp is not None and azp != settings.oidc_client_id:
        raise OidcError("ID token was issued for a different application.")

    if not nonce or claims.get("nonce") != nonce:
        # Binds the token to *this* browser's transaction, which is what makes
        # a replayed or injected token useless.
        raise OidcError("ID token nonce did not match this sign-in attempt.")
    return claims


# --- identity + authorization ---------------------------------------------
def username_from_claims(claims: dict) -> str:
    for name in get_settings().oidc_username_claim.split():
        value = claims.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(claims.get("sub", "unknown"))


def authorized(claims: dict) -> tuple[bool, str]:
    """Is this user allowed in? Returns ``(ok, reason_if_not)``.

    Fail-closed at every ambiguity. The shapes that matter, each of which has
    been someone's outage: the claim absent entirely; present but an empty list
    (the case a truthiness check falls through on); a space-delimited string
    rather than an array; and Entra's *groups overage*, where a user in ~200+
    groups gets no ``groups`` claim at all — which would silently deny exactly
    the longest-tenured accounts while working perfectly in testing.
    """
    settings = get_settings()
    required = (settings.oidc_group_required or "").strip().lower()
    if not required:
        # Unreachable via the config validator; belt and braces, because the
        # alternative is authenticating an entire directory.
        return False, "No SPARK_OIDC_GROUP_REQUIRED is configured; refusing to let anyone in."

    claim_name = settings.oidc_groups_claim
    raw = claims.get(claim_name)
    if raw is None:
        if "_claim_names" in claims or "_claim_sources" in claims:
            return False, (
                f"Your '{claim_name}' claim was omitted because you are a member of too "
                f"many groups (the provider sends a lookup reference instead). Ask an "
                f"administrator to switch the portal to app roles "
                f"(SPARK_OIDC_GROUPS_CLAIM=roles)."
            )
        return False, f"Your account has no '{claim_name}' claim."
    if isinstance(raw, str):
        values = re.split(r"[,\s]+", raw)
    elif isinstance(raw, (list, tuple)):
        values = [v for v in raw if isinstance(v, str)]
    else:
        return False, f"Your '{claim_name}' claim has an unexpected shape."
    normalized = {v.strip().lower() for v in values if v and v.strip()}
    if not normalized:
        return False, f"Your '{claim_name}' claim is empty."
    if required not in normalized:
        return False, "Your account is not a member of the group required for this portal."
    return True, ""


def end_session_url(post_logout: str | None) -> str | None:
    """The provider's logout URL, when it publishes one."""
    from urllib.parse import urlencode

    doc = _caches.discovery.doc if _caches.discovery else None
    if not doc or not doc.get("end_session_endpoint"):
        return None
    params = {}
    if post_logout:
        params["post_logout_redirect_uri"] = post_logout
    url = doc["end_session_endpoint"]
    return f"{url}?{urlencode(params)}" if params else url
