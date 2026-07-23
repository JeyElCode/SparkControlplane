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
import hmac
import json
import logging
import time

from ..config import get_settings
from ..crypto import decrypt, encrypt

log = logging.getLogger("spark.auth")

COOKIE_NAME = "spark_session"

# naive per-IP login throttle: 5 straight failures -> 30s lockout
_FAILS: dict[str, tuple[int, float]] = {}
MAX_FAILS = 5
LOCKOUT_SECONDS = 30.0


class AuthError(Exception):
    """Login rejected; str(exc) is safe to show the user."""


# --- sessions -------------------------------------------------------------
def create_session(user: str) -> str:
    settings = get_settings()
    exp = time.time() + settings.auth_session_hours * 3600
    return encrypt(json.dumps({"u": user, "exp": exp}))


def parse_session(token: str | None) -> str | None:
    """Username for a valid unexpired session token, else None. A cookie is
    attacker-controlled input — any decrypt/parse failure is just "no session"."""
    if not token:
        return None
    try:
        raw = decrypt(token)
    except Exception:  # noqa: BLE001 - tampered/garbage token
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if float(data.get("exp", 0)) < time.time():
            return None
        user = data.get("u")
        return user if isinstance(user, str) and user else None
    except (ValueError, TypeError):
        return None


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
        ok_user = hmac.compare_digest(username, settings.admin_user)
        ok_pass = hmac.compare_digest(password, settings.admin_password or "")
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
