"""OIDC sign-in, and the controls that make it safe.

Almost every test here is a *negative* one. A working happy path proves very
little about an auth flow — the value is in what it refuses, so each control
gets a token or a request crafted specifically to defeat it.

The provider is `tests/oidc_fixture.FakeIdP`: a real RSA key, a real JWKS, and
the ability to mint tokens no correct library would produce.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.config import Settings
from tests.oidc_fixture import FakeIdP

CLIENT_ID = "spark-portal"


@pytest.fixture()
def idp():
    from app.services import oidc

    oidc.reset_caches()
    yield FakeIdP()
    oidc.reset_caches()


@pytest.fixture()
def oidc_env(monkeypatch, tmp_path, idp):
    """Point the app at the fake provider and stub its HTTP transport."""
    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SPARK_AUTH_MODE", "oidc")
    monkeypatch.setenv("SPARK_OIDC_ISSUER", idp.issuer)
    monkeypatch.setenv("SPARK_OIDC_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("SPARK_OIDC_CLIENT_SECRET", "shh")
    monkeypatch.setenv("SPARK_OIDC_REDIRECT_URL", "https://spark.example/api/auth/oidc/callback")
    monkeypatch.setenv("SPARK_OIDC_GROUP_REQUIRED", "spark-operators")
    monkeypatch.setenv("SPARK_OIDC_GROUPS_CLAIM", "roles")
    import app.config as config

    config.get_settings.cache_clear()
    yield idp
    config.get_settings.cache_clear()


def _transport(idp: FakeIdP, *, discovery_override: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json=discovery_override or idp.discovery())
        if url.endswith("/keys"):
            return httpx.Response(200, json=idp.jwks())
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _patch_http(monkeypatch, idp, **kw):
    from app.services import oidc

    monkeypatch.setattr(
        oidc, "_client", lambda: httpx.AsyncClient(transport=_transport(idp, **kw))
    )


# --- config surface refuses the dangerous shapes --------------------------
def test_hmac_algorithms_cannot_be_configured():
    """The alg-confusion attack needs HS* to be acceptable. Make it
    unexpressible rather than relying on the library to catch it."""
    for bad in ("HS256", "none", "RS256 HS256"):
        with pytest.raises(ValueError, match="alg-confusion|unsupported"):
            Settings(oidc_algorithms=bad)
    assert Settings(oidc_algorithms="rs256").oidc_algorithms == "RS256"


def test_clock_skew_is_capped():
    with pytest.raises(ValueError):
        Settings(oidc_clock_skew_seconds=3600)  # an hour of skew extends every token


def test_oidc_mode_fails_closed_until_configured(monkeypatch):
    monkeypatch.setenv("SPARK_AUTH_MODE", "oidc")
    import app.config as config

    config.get_settings.cache_clear()
    err = config.get_settings().oidc_config_error
    assert err and "SPARK_OIDC_ISSUER" in err
    config.get_settings.cache_clear()


def test_group_requirement_is_mandatory(monkeypatch, tmp_path, idp):
    """The LDAP lesson: an optional group check means the default deployment
    authenticates an entire directory into a portal that SSHes to DGX nodes."""
    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SPARK_AUTH_MODE", "oidc")
    monkeypatch.setenv("SPARK_OIDC_ISSUER", idp.issuer)
    monkeypatch.setenv("SPARK_OIDC_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("SPARK_OIDC_CLIENT_SECRET", "shh")
    monkeypatch.setenv("SPARK_OIDC_REDIRECT_URL", "https://spark.example/cb")
    import app.config as config

    config.get_settings.cache_clear()
    assert "SPARK_OIDC_GROUP_REQUIRED" in (config.get_settings().oidc_config_error or "")
    config.get_settings.cache_clear()


# --- discovery pinning ----------------------------------------------------
async def test_discovery_endpoints_must_be_on_the_issuer_origin(oidc_env, monkeypatch):
    """The biggest hole in a naive implementation: a hostile discovery document
    relocates token_endpoint, and the authorization code AND the client secret
    are delivered to the attacker."""
    from app.services import oidc

    evil = oidc_env.discovery()
    evil["token_endpoint"] = "https://attacker.example/collect"
    _patch_http(monkeypatch, oidc_env, discovery_override=evil)

    with pytest.raises(oidc.OidcError, match="issuer's own origin"):
        await oidc.discovery()


async def test_discovery_issuer_must_match_configuration(oidc_env, monkeypatch):
    from app.services import oidc

    lying = oidc_env.discovery()
    lying["issuer"] = "https://someone-else.example"
    _patch_http(monkeypatch, oidc_env, discovery_override=lying)

    with pytest.raises(oidc.OidcError, match="different issuer"):
        await oidc.discovery()


# --- ID token validation --------------------------------------------------
async def _validated(idp, monkeypatch, **token_kw):
    from app.services import oidc

    _patch_http(monkeypatch, idp)
    await oidc.discovery()
    nonce = token_kw.pop("_nonce", "n-1")
    tok = idp.id_token(aud=CLIENT_ID, nonce=nonce, **token_kw)
    return await oidc.validate_id_token(tok, nonce=nonce)


async def test_valid_token_is_accepted(oidc_env, monkeypatch):
    claims = await _validated(oidc_env, monkeypatch,
                              claims={"roles": ["spark-operators"], "preferred_username": "jorgen"})
    assert claims["sub"] == "user-guid-1"


@pytest.mark.parametrize("kwargs,match", [
    ({"alg": "none"}, "not accepted"),
    ({"alg": "HS256"}, "not accepted"),
    ({"rogue_key": True}, "failed validation"),
    ({"expires_in": -60}, "failed validation"),
    ({"issuer": "https://evil.example"}, "failed validation"),
    ({"kid": "no-such-key"}, "does not publish"),
])
async def test_bad_tokens_are_refused(oidc_env, monkeypatch, kwargs, match):
    from app.services import oidc

    with pytest.raises(oidc.OidcError, match=match):
        await _validated(oidc_env, monkeypatch, **kwargs)


async def test_token_for_another_application_is_refused(oidc_env, monkeypatch):
    from app.services import oidc

    _patch_http(monkeypatch, oidc_env)
    await oidc.discovery()
    tok = oidc_env.id_token(aud="some-other-app", nonce="n-1")
    with pytest.raises(oidc.OidcError, match="failed validation"):
        await oidc.validate_id_token(tok, nonce="n-1")


async def test_multi_audience_needs_azp_and_azp_must_be_us(oidc_env, monkeypatch):
    """`aud` as a single-element array is legal and must be accepted (strict_aud
    would wrongly reject Entra). The real control against a token minted for a
    sibling application is `azp`."""
    from app.services import oidc

    _patch_http(monkeypatch, oidc_env)
    await oidc.discovery()

    single_array = oidc_env.id_token(aud=[CLIENT_ID], nonce="n-1")
    assert (await oidc.validate_id_token(single_array, nonce="n-1"))["sub"]

    multi = oidc_env.id_token(aud=[CLIENT_ID, "other-app"], nonce="n-1")
    with pytest.raises(oidc.OidcError, match="multiple audiences"):
        await oidc.validate_id_token(multi, nonce="n-1")

    wrong_azp = oidc_env.id_token(aud=[CLIENT_ID, "other"], nonce="n-1",
                                  claims={"azp": "other"})
    with pytest.raises(oidc.OidcError, match="different application"):
        await oidc.validate_id_token(wrong_azp, nonce="n-1")


async def test_nonce_must_match_this_transaction(oidc_env, monkeypatch):
    """Without this, a token captured from any other sign-in can be replayed."""
    from app.services import oidc

    _patch_http(monkeypatch, oidc_env)
    await oidc.discovery()
    tok = oidc_env.id_token(aud=CLIENT_ID, nonce="someone-elses-nonce")
    with pytest.raises(oidc.OidcError, match="nonce"):
        await oidc.validate_id_token(tok, nonce="ours")
    # ...and a token with no nonce at all
    tok = oidc_env.id_token(aud=CLIENT_ID)
    with pytest.raises(oidc.OidcError, match="nonce"):
        await oidc.validate_id_token(tok, nonce="ours")


async def test_key_is_never_taken_from_the_token_header(oidc_env, monkeypatch):
    """A `jku`/`jwk` header naming a key location must be ignored — otherwise
    the token chooses its own verifier."""
    from app.services import oidc

    _patch_http(monkeypatch, oidc_env)
    await oidc.discovery()
    tok = oidc_env.id_token(aud=CLIENT_ID, nonce="n", rogue_key=True)
    with pytest.raises(oidc.OidcError):
        await oidc.validate_id_token(tok, nonce="n")


# --- authorization --------------------------------------------------------
@pytest.mark.parametrize("claims,expected", [
    ({"roles": ["spark-operators"]}, True),
    ({"roles": ["Spark-Operators"]}, True),          # case-insensitive
    ({"roles": "spark-operators other"}, True),      # space-delimited string
    ({"roles": ["someone-else"]}, False),
    ({"roles": []}, False),                          # the truthiness trap
    ({}, False),                                     # claim absent entirely
    ({"roles": 42}, False),                          # unexpected shape
])
def test_group_check_shapes(oidc_env, claims, expected):
    from app.services import oidc

    ok, _why = oidc.authorized(claims)
    assert ok is expected


def test_entra_group_overage_denies_with_an_actionable_reason(oidc_env):
    """Above ~200 groups Entra omits `groups` and sends a lookup reference
    instead. That denies exactly the longest-tenured accounts while working
    perfectly in testing — so it must fail closed AND say why."""
    from app.services import oidc

    monkeypatched = {"_claim_names": {"groups": "src1"},
                     "_claim_sources": {"src1": {"endpoint": "https://graph..."}}}
    ok, why = oidc.authorized(monkeypatched)
    assert ok is False
    assert "too many groups" in why and "SPARK_OIDC_GROUPS_CLAIM=roles" in why


# --- session binding ------------------------------------------------------
def test_session_from_another_mode_is_rejected_under_oidc(monkeypatch, tmp_path):
    """Enabling SSO must not leave password-mode cookies valid — that would be
    exactly the downgrade SSO is meant to end."""
    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SPARK_AUTH_MODE", "password")
    monkeypatch.setenv("SPARK_ADMIN_PASSWORD", "pw")
    import app.config as config

    config.get_settings.cache_clear()
    from app.services.auth import create_session, parse_session

    pw_cookie = create_session("admin")
    assert parse_session(pw_cookie) == "admin"

    monkeypatch.setenv("SPARK_AUTH_MODE", "oidc")
    config.get_settings.cache_clear()
    assert parse_session(pw_cookie) is None, "a password-mode session survived the switch to SSO"
    config.get_settings.cache_clear()


def test_a_non_session_fernet_blob_is_not_a_session(monkeypatch, tmp_path):
    """One Fernet key encrypts sessions, stored SSH passwords, and the OIDC
    transaction cookie. Without a purpose tag, any of them that happens to
    decrypt to the right JSON shape would be accepted as a login."""
    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SPARK_AUTH_MODE", "password")
    monkeypatch.setenv("SPARK_ADMIN_PASSWORD", "pw")
    import app.config as config

    config.get_settings.cache_clear()
    from app.crypto import encrypt
    from app.services.auth import parse_session

    import time as _t
    forged = encrypt(json.dumps({
        "t": "oidc_tx", "u": "attacker", "exp": _t.time() + 9999,
    }))
    assert parse_session(forged) is None
    config.get_settings.cache_clear()


def test_oidc_sessions_are_capped_for_offboarding(monkeypatch, tmp_path, idp):
    """The portal holds no refresh token and never re-asks the provider, so the
    session lifetime IS how long a disabled account keeps working."""
    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SPARK_AUTH_MODE", "oidc")
    monkeypatch.setenv("SPARK_AUTH_SESSION_HOURS", "24")
    import app.config as config

    config.get_settings.cache_clear()
    from app.services.auth import session_seconds

    assert session_seconds() == 8 * 3600, "oidc must cap the session below the 24h default"
    config.get_settings.cache_clear()
