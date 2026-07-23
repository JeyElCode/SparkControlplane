"""Portal auth: sessions, modes, middleware enforcement, throttle, LDAP glue."""

from __future__ import annotations

import importlib
import time

import pytest
from fastapi.testclient import TestClient


def _fresh_client(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import app.config as config

    config.get_settings.cache_clear()
    import app.db as db

    importlib.reload(db)
    import app.main as main

    importlib.reload(main)
    return TestClient(main.app)


@pytest.fixture()
def open_client(tmp_path, monkeypatch):
    with _fresh_client(tmp_path, monkeypatch) as c:
        yield c
    import app.config as config

    config.get_settings.cache_clear()


@pytest.fixture()
def pw_client(tmp_path, monkeypatch):
    with _fresh_client(
        tmp_path, monkeypatch,
        SPARK_AUTH_MODE="password", SPARK_ADMIN_PASSWORD="hunter2",
        SPARK_METRICS_TOKEN="scrape-me",
    ) as c:
        yield c
    import app.config as config

    config.get_settings.cache_clear()


def test_none_mode_everything_open(open_client):
    me = open_client.get("/api/auth/me").json()
    assert me["auth_required"] is False and me["authenticated"] is True
    assert open_client.get("/api/status").status_code == 200
    assert open_client.get("/metrics").status_code == 200


def test_password_mode_guards_api_but_not_shell(pw_client):
    # guarded API
    assert pw_client.get("/api/status").status_code == 401
    assert pw_client.get("/api/nodes").status_code == 401
    # open: auth flow, health, SPA shell
    assert pw_client.get("/api/health").status_code == 200
    assert pw_client.get("/api/auth/me").json()["auth_required"] is True
    # /metrics: bearer token works, no auth doesn't
    assert pw_client.get("/metrics").status_code == 401
    assert pw_client.get("/metrics", headers={"Authorization": "Bearer scrape-me"}).status_code == 200
    assert pw_client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_password_login_logout_roundtrip(pw_client):
    bad = pw_client.post("/api/auth/login", json={"username": "admin", "password": "nope"})
    assert bad.status_code == 401
    empty = pw_client.post("/api/auth/login", json={"username": "admin", "password": "  "})
    assert empty.status_code == 401

    ok = pw_client.post("/api/auth/login", json={"username": "admin", "password": "hunter2"})
    assert ok.status_code == 200 and ok.json()["authenticated"] is True
    assert "spark_session" in pw_client.cookies

    assert pw_client.get("/api/status").status_code == 200  # cookie rides along
    me = pw_client.get("/api/auth/me").json()
    assert me["authenticated"] is True and me["user"] == "admin"

    pw_client.post("/api/auth/logout")
    pw_client.cookies.clear()
    assert pw_client.get("/api/status").status_code == 401


def test_login_throttle(pw_client):
    from app.services import auth as auth_svc

    auth_svc._FAILS.clear()
    for _ in range(auth_svc.MAX_FAILS):
        pw_client.post("/api/auth/login", json={"username": "admin", "password": "bad"})
    r = pw_client.post("/api/auth/login", json={"username": "admin", "password": "hunter2"})
    assert r.status_code == 429
    auth_svc._FAILS.clear()


def test_session_expiry_and_tamper(tmp_path, monkeypatch):
    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    import app.config as config

    config.get_settings.cache_clear()
    from app.services.auth import create_session, parse_session

    token = create_session("jorgen")
    assert parse_session(token) == "jorgen"
    assert parse_session(token + "x") is None
    assert parse_session("garbage") is None
    assert parse_session(None) is None

    monkeypatch.setenv("SPARK_AUTH_SESSION_HOURS", "0")
    config.get_settings.cache_clear()
    expired = create_session("jorgen")
    time.sleep(0.01)
    assert parse_session(expired) is None
    config.get_settings.cache_clear()


def test_misconfigured_mode_fails_closed(tmp_path, monkeypatch):
    """A typo'd/incomplete auth mode must lock the API, never open it."""
    with _fresh_client(tmp_path, monkeypatch, SPARK_AUTH_MODE="ldap") as c:  # no LDAP settings
        assert c.get("/api/status").status_code == 401
        r = c.post("/api/auth/login", json={"username": "u", "password": "p"})
        assert r.status_code == 401
        assert "disabled" in r.json()["detail"].lower() or "ldap" in r.json()["detail"].lower()
    import app.config as config

    config.get_settings.cache_clear()


def test_ldap_mode_login_with_mocked_directory(tmp_path, monkeypatch):
    with _fresh_client(
        tmp_path, monkeypatch,
        SPARK_AUTH_MODE="ldap", SPARK_LDAP_URL="ldap://ldap.example.com",
        SPARK_LDAP_USER_DN_TEMPLATE="uid={username},ou=people,dc=example,dc=com",
    ) as c:
        from app.services import auth as auth_svc

        def fake_ldap(username, password):
            if username == "jorgen" and password == "s3cret":
                return username
            raise auth_svc.AuthError("Invalid username or password.")

        monkeypatch.setattr(auth_svc, "_ldap_verify", fake_ldap)
        auth_svc._FAILS.clear()
        assert c.post("/api/auth/login", json={"username": "jorgen", "password": "wrong"}).status_code == 401
        ok = c.post("/api/auth/login", json={"username": "jorgen", "password": "s3cret"})
        assert ok.status_code == 200 and ok.json()["user"] == "jorgen"
        assert c.get("/api/status").status_code == 200
    import app.config as config

    config.get_settings.cache_clear()


def test_ldap_escaping():
    from app.services.auth import _ldap_escape_dn, _ldap_escape_filter

    assert _ldap_escape_filter("jo(r)g*en\\") == "jo\\28r\\29g\\2ae n\\5c".replace("e n", "en")
    assert _ldap_escape_dn("Doe, John") == "Doe\\, John"
    assert _ldap_escape_dn(" lead") == "\\ lead"
