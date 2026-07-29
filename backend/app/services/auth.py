"""Portal authentication: session tokens + password/LDAP verification.

Modes (SPARK_AUTH_MODE): "none" (open, homelab default), "password" (single
admin credential), "ldap" (bind against a directory; direct-bind DN template or
service-account search+bind, optional required group). Fail-closed: anything
except "none" requires a valid session, and misconfiguration blocks logins
rather than opening the portal.

Sessions are Fernet-encrypted JSON in an HttpOnly cookie — the same key that
encrypts secrets at rest, so no extra key management.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time

from ..config import get_settings
from ..crypto import decrypt_cookie, encrypt

log = logging.getLogger("spark.auth")

COOKIE_NAME = "spark_session"

# naive per-IP login throttle: 5 straight failures -> 30s lockout
_FAILS: dict[str, tuple[int, float]] = {}
MAX_FAILS = 5
LOCKOUT_SECONDS = 30.0


class AuthError(Exception):
    """Login rejected; str(exc) is safe to show the user."""


# --- sessions -------------------------------------------------------------
# The Fernet key is shared with everything else this app encrypts — stored SSH
# passwords, private keys, and (in oidc mode) the short-lived login transaction
# cookie. Without a purpose tag, `parse_session` accepts ANY blob that decrypts
# to JSON with a username and a future exp, which makes every other ciphertext a
# candidate session. `t` is that tag; `m` binds the session to the auth mode
# that minted it, so switching to SSO cannot be defeated by a cookie issued
# under the old, weaker mode.
SESSION_TYPE = "session"
SESSION_VERSION = 3


def config_fingerprint() -> str:
    """A short digest of the credentials/authorization config.

    Binding it into the session revokes every old cookie the moment that config
    changes, with no stored state at all: `get_settings` is lru_cached, so
    rotating SPARK_ADMIN_PASSWORD (or tightening the required group) already
    requires a restart — and at that restart the old cookies simply stop
    matching. Deliberately NOT cached: caching it on the settings object's id()
    is a recycled-id bug that would silently stop revoking and pass every test
    that doesn't reload.
    """
    settings = get_settings()
    material = "\x1f".join(str(x) for x in (
        settings.effective_auth_mode,
        settings.admin_user,
        settings.admin_password or "",
        settings.ldap_url or "",
        settings.ldap_group_required or "",
        settings.oidc_issuer or "",
        settings.oidc_client_id or "",
        settings.oidc_group_required or "",
    ))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def create_session(user: str, *, mode: str | None = None) -> str:
    from . import sessions as sessions_svc

    settings = get_settings()
    now = time.time()
    # Never mint a token that is already revoked. If the clock stepped backward
    # (NTP correction, a bad RTC, a restored snapshot), `now` can land before a
    # revocation cutoff — and then every fresh login is instantly dead and the
    # portal becomes permanently unloggable-into with no way out but editing
    # the database.
    issued_at = max(now, sessions_svc.epoch_for(user) + 1.0)
    return encrypt(json.dumps({
        "t": SESSION_TYPE,
        "v": SESSION_VERSION,
        "m": mode or settings.effective_auth_mode,
        "u": user,
        "iat": issued_at,
        "jti": sessions_svc.new_jti(),
        "c": config_fingerprint(),
        "exp": issued_at + session_seconds(),
    }))


def session_seconds() -> float:
    """Session lifetime. In oidc mode this is capped, because it *is* the
    offboarding guarantee: the portal holds no refresh token and never asks the
    provider anything again, so a disabled account keeps working until its
    cookie expires."""
    settings = get_settings()
    hours = settings.auth_session_hours
    if settings.effective_auth_mode == "oidc":
        hours = min(hours, settings.oidc_max_session_hours)
    return hours * 3600


def session_claims(token: str | None) -> dict | None:
    """The validated claims of a session, or None. Same checks as
    :func:`parse_session`, which is the thin wrapper over this."""
    if not token:
        return None
    raw = decrypt_cookie(token)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        if float(data.get("exp", 0)) < time.time():
            return None
    except (ValueError, TypeError):
        return None
    user = data.get("u")
    if not (isinstance(user, str) and user):
        return None
    mode = get_settings().effective_auth_mode
    if data.get("t") != SESSION_TYPE:
        return None
    if data.get("m") and data["m"] != mode:
        return None
    # v3 added revocation. Older tokens carry no jti/iat/fingerprint and so
    # cannot be checked against any of it — reject them and make everyone log
    # in once, rather than leave a class of session that is structurally
    # unrevocable.
    if data.get("v") != SESSION_VERSION:
        return None
    if data.get("c") != config_fingerprint():
        return None
    from . import sessions as sessions_svc

    try:
        issued_at = float(data.get("iat", 0))
    except (ValueError, TypeError):
        return None
    if sessions_svc.is_revoked(user, data.get("jti"), issued_at):
        return None
    return data


def parse_session(token: str | None) -> str | None:
    """Username for a valid, unexpired, unrevoked session, else None. A cookie
    is attacker-controlled input — every failure is just "no session"."""
    claims = session_claims(token)
    return claims.get("u") if claims else None


def ws_session_valid(ws) -> bool:
    """Is the session behind this WebSocket still good?

    AuthMiddleware checks once, at the handshake. These streams then run for
    hours — the dashboard holds one open for as long as the tab is — so without
    a periodic re-check a revoked session keeps receiving live cluster
    telemetry until the user closes the tab. Revocation that only applies to
    *new* connections is not revocation.
    """
    if get_settings().effective_auth_mode == "none":
        return True
    return parse_session(ws.cookies.get(COOKIE_NAME)) is not None


async def ws_revocation_watchdog(ws, interval: float = 15.0) -> None:
    """Close ``ws`` once its session stops being valid.

    For streams whose main loop blocks on ``ws.receive()`` and therefore never
    ticks on its own. Run it as a sibling task and cancel it in the same finally
    that tears the stream down.
    """
    import asyncio as _asyncio

    while True:
        await _asyncio.sleep(interval)
        if not ws_session_valid(ws):
            try:
                await ws.close(code=4401)
            except Exception:  # noqa: BLE001 - already gone
                pass
            return


# --- login throttle -------------------------------------------------------
def check_throttle(ip: str) -> float:
    """Seconds the caller must still wait, or 0 if allowed."""
    fails, until = _FAILS.get(ip, (0, 0.0))
    return max(0.0, until - time.time()) if fails >= MAX_FAILS else 0.0


def record_attempt(ip: str, ok: bool) -> None:
    if ok:
        _FAILS.pop(ip, None)
        return
    fails, _ = _FAILS.get(ip, (0, 0.0))
    fails += 1
    until = time.time() + LOCKOUT_SECONDS if fails >= MAX_FAILS else 0.0
    _FAILS[ip] = (fails, until)
    if fails >= MAX_FAILS:
        log.warning("login throttled for %s after %d failures", ip, fails)


# --- verification ---------------------------------------------------------
async def verify_login(username: str, password: str) -> str:
    """Verify credentials for the configured mode; returns the canonical
    username. Raises AuthError on any rejection."""
    settings = get_settings()
    mode = settings.effective_auth_mode
    username = username.strip()
    if not username or not password or not password.strip():
        # empty password MUST be rejected before an LDAP bind: many servers
        # treat it as a successful anonymous bind.
        raise AuthError("Username and password are required.")
    if mode == "password":
        # Compare as UTF-8 bytes: compare_digest raises TypeError on str inputs
        # holding non-ASCII, which would 500 instead of 401 and lock out any
        # admin whose password contains æøå.
        ok_user = hmac.compare_digest(username.encode("utf-8"), settings.admin_user.encode("utf-8"))
        ok_pass = hmac.compare_digest(
            password.encode("utf-8"), (settings.admin_password or "").encode("utf-8")
        )
        if not (settings.admin_password and ok_user and ok_pass):
            raise AuthError("Invalid username or password.")
        return settings.admin_user
    if mode == "ldap":
        return await asyncio.to_thread(_ldap_verify, username, password)
    raise AuthError(f"Logins are disabled: auth mode '{mode}' is not configured correctly.")


def _ldap_escape_filter(value: str) -> str:
    out = []
    for ch in value:
        if ch in ('\\', '*', '(', ')', '\x00'):
            out.append("\\%02x" % ord(ch))
        else:
            out.append(ch)
    return "".join(out)


def _ldap_escape_dn(value: str) -> str:
    # RFC 4514 special characters in an RDN value
    out = []
    for i, ch in enumerate(value):
        if ch in ',+"\\<>;=' or (ch == "#" and i == 0) or (ch == " " and i in (0, len(value) - 1)):
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def build_ldap_tls(verify: bool, ca_file: str | None):
    """ldap3 Tls config for ldaps://STARTTLS. ldap3's own default is CERT_NONE
    (encrypted but unauthenticated — MITM-able), so we always pass an explicit
    policy: validate against the system store (or ``ca_file``) unless the
    operator explicitly opted out."""
    import ssl

    import ldap3

    return ldap3.Tls(
        validate=ssl.CERT_REQUIRED if verify else ssl.CERT_NONE,
        ca_certs_file=ca_file or None,
    )


def _ldap_verify(username: str, password: str) -> str:
    """Blocking LDAP verification (run in a thread). Returns the username."""
    try:
        import ldap3
        from ldap3.core.exceptions import LDAPException
    except ImportError:  # pragma: no cover - dependency ships in the image
        raise AuthError("LDAP support is not installed on the server.")

    settings = get_settings()
    if not settings.ldap_url:
        raise AuthError("Logins are disabled: SPARK_LDAP_URL is not set.")
    use_ssl = settings.ldap_url.lower().startswith("ldaps://")
    tls = None
    if use_ssl or settings.ldap_start_tls:
        try:
            tls = build_ldap_tls(settings.ldap_verify_cert, settings.ldap_ca_file)
        except LDAPException as exc:  # e.g. missing/unreadable CA file
            log.error("LDAP TLS configuration invalid: %s", exc)
            raise AuthError("Logins are disabled: LDAP TLS configuration is invalid "
                            "(check SPARK_LDAP_CA_FILE).")
    server = ldap3.Server(settings.ldap_url, use_ssl=use_ssl, get_info=ldap3.NONE,
                          connect_timeout=5, tls=tls)

    def _conn(user_dn: str | None, pw: str | None) -> "ldap3.Connection":
        c = ldap3.Connection(server, user=user_dn, password=pw, receive_timeout=10,
                             read_only=True)
        if settings.ldap_start_tls and not use_ssl:
            if not c.start_tls():
                raise AuthError("LDAP STARTTLS failed.")
        if not c.bind():
            raise AuthError("Invalid username or password.")
        return c

    try:
        # Resolve the user's DN: direct template, or service-account search.
        if settings.ldap_user_dn_template:
            user_dn = settings.ldap_user_dn_template.format(
                username=_ldap_escape_dn(username)
            )
        elif settings.ldap_user_search_base:
            svc = _conn(settings.ldap_bind_dn, settings.ldap_bind_password)
            flt = settings.ldap_user_filter.format(username=_ldap_escape_filter(username))
            svc.search(settings.ldap_user_search_base, flt,
                       attributes=["memberOf"], size_limit=2)
            entries = svc.entries
            svc.unbind()
            if len(entries) != 1:
                raise AuthError("Invalid username or password.")
            user_dn = entries[0].entry_dn
        else:
            raise AuthError("Logins are disabled: LDAP user lookup is not configured.")

        # The bind IS the password check.
        user_conn = _conn(user_dn, password)

        if settings.ldap_group_required:
            group = settings.ldap_group_required
            user_conn.search(user_dn, "(objectClass=*)", search_scope=ldap3.BASE,
                             attributes=["memberOf"])
            member_of = []
            if user_conn.entries:
                member_of = [str(g).lower() for g in
                             (user_conn.entries[0].memberOf.values
                              if "memberOf" in user_conn.entries[0] else [])]
            if group.lower() not in member_of:
                user_conn.unbind()
                log.warning("LDAP user %s authenticated but not in required group", username)
                raise AuthError("You are not a member of the required group.")
        user_conn.unbind()
        return username
    except AuthError:
        raise
    except LDAPException as exc:
        log.warning("LDAP error during login for %s: %s", username, exc)
        raise AuthError("Directory server error — try again or contact the admin.")
