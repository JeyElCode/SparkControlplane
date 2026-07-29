"""Sessions you can actually end.

Before this, `logout` only deleted the cookie — a *request* to a cooperating
browser — so a copied cookie kept working until it expired, and there was no
action an operator could take at all.

Two tests here matter more than the rest. The restart test is written so it
cannot pass by accident: `importlib.reload(main)` does NOT re-execute
`app.services.sessions`, so a naive "restart" leaves the in-memory revocation
state intact and proves nothing — it has to be cleared explicitly. And the
clock test covers a failure that would otherwise be discovered in production,
where a backward NTP step makes the portal permanently unloggable-into.
"""

from __future__ import annotations

import asyncio
import importlib
import time

import pytest
from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SPARK_AUTH_MODE", "password")
    monkeypatch.setenv("SPARK_ADMIN_PASSWORD", "hunter2")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import app.config as config

    config.get_settings.cache_clear()
    import app.db as db

    importlib.reload(db)
    import app.main as main

    importlib.reload(main)
    from app.services import sessions

    sessions.reset_for_tests()
    return TestClient(main.app)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        c.post("/api/auth/login", json={"username": "admin", "password": "hunter2"})
        yield c
    import app.config as config

    config.get_settings.cache_clear()


def test_logged_in_session_works(client):
    assert client.get("/api/status").status_code == 200


def test_logout_kills_a_copied_cookie(client):
    """The point of the whole issue: delete_cookie is a polite request to the
    browser, so a cookie copied beforehand used to keep working."""
    stolen = client.cookies["spark_session"]
    client.post("/api/auth/logout")

    client.cookies.set("spark_session", stolen)
    assert client.get("/api/status").status_code == 401, (
        "a cookie copied before logout still worked — logout did not revoke"
    )


def test_sign_out_everywhere_kills_other_sessions(client, tmp_path, monkeypatch):
    """You cannot know which cookie leaked, so this has to be per-user."""
    first = client.cookies["spark_session"]
    # A second sign-in, as if from another browser.
    client.post("/api/auth/login", json={"username": "admin", "password": "hunter2"})
    second = client.cookies["spark_session"]
    assert first != second

    r = client.post("/api/sessions/revoke", json={})
    assert r.status_code == 200
    assert "including this one" in r.json()["detail"]

    for cookie in (first, second):
        client.cookies.set("spark_session", cookie)
        assert client.get("/api/status").status_code == 401


def test_revoking_a_named_user_is_honest_about_scope(client):
    r = client.post("/api/sessions/revoke", json={"username": "someone-else"})
    assert r.status_code == 200
    # It must not imply it disabled the directory account.
    assert "does not disable the account in your directory" in r.json()["detail"]


def test_revoke_everyone(client):
    mine = client.cookies["spark_session"]
    r = client.post("/api/sessions/revoke", json={"everyone": True})
    assert r.status_code == 200 and r.json()["subject"] == "(everyone)"
    client.cookies.set("spark_session", mine)
    assert client.get("/api/status").status_code == 401


def test_revocation_survives_a_restart(tmp_path, monkeypatch):
    """A revocation a restart undoes is not a revocation — and under GitOps a
    restart happens on every sync.

    Note the explicit reset: without it this test passes even if nothing is
    persisted, because module state survives importlib.reload(main).
    """
    with _client(tmp_path, monkeypatch) as c:
        c.post("/api/auth/login", json={"username": "admin", "password": "hunter2"})
        stolen = c.cookies["spark_session"]
        c.post("/api/sessions/revoke", json={})
        c.cookies.set("spark_session", stolen)
        assert c.get("/api/status").status_code == 401

    # Restart: same data dir, all in-memory state gone.
    from app.services import sessions

    sessions.reset_for_tests()
    assert not sessions._EPOCHS, "the fake restart did not actually clear memory"

    with _client(tmp_path, monkeypatch) as c2:
        c2.cookies.set("spark_session", stolen)
        assert c2.get("/api/status").status_code == 401, (
            "the revocation was forgotten across a restart"
        )
    import app.config as config

    config.get_settings.cache_clear()


def test_unloaded_revocation_list_denies_rather_than_allows(tmp_path, monkeypatch):
    """The polarity that must not be copy-pasted from apikeys: an unloaded key
    map denies (safe), an unloaded revocation map would ALLOW — silently
    un-revoking everything."""
    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SPARK_AUTH_MODE", "password")
    monkeypatch.setenv("SPARK_ADMIN_PASSWORD", "pw")
    import app.config as config

    config.get_settings.cache_clear()
    from app.services import sessions
    from app.services.auth import create_session, parse_session

    sessions.reset_for_tests()
    token = create_session("admin")
    assert parse_session(token) is None, "sessions were accepted before the list loaded"
    config.get_settings.cache_clear()


def test_backward_clock_step_cannot_lock_everyone_out(tmp_path, monkeypatch):
    """An NTP correction, a bad RTC or a restored snapshot can move the clock
    backwards. If a fresh token's `iat` lands before an existing revocation
    cutoff, every new login is instantly dead and the only fix is editing the
    database by hand."""
    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SPARK_AUTH_MODE", "password")
    monkeypatch.setenv("SPARK_ADMIN_PASSWORD", "pw")
    import app.config as config

    config.get_settings.cache_clear()
    import app.db as db

    importlib.reload(db)
    asyncio.run(db.init_db())
    from app.services import sessions
    from app.services.auth import create_session, parse_session

    sessions.reset_for_tests()
    asyncio.run(sessions.load())

    # Revoke with a cutoff an hour in the future, i.e. the clock has since
    # stepped back an hour.
    asyncio.run(sessions.revoke_user("admin"))
    sessions._EPOCHS["admin"] = time.time() + 3600

    token = create_session("admin")
    assert parse_session(token) == "admin", (
        "after a backward clock step, no new session could be created — the "
        "portal would be permanently unloggable-into"
    )
    config.get_settings.cache_clear()


def test_rotating_the_admin_password_invalidates_old_sessions(tmp_path, monkeypatch):
    """Free revocation with no stored state: the session carries a fingerprint
    of the credential config, and rotating the password already requires a
    restart because get_settings is cached."""
    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SPARK_AUTH_MODE", "password")
    monkeypatch.setenv("SPARK_ADMIN_PASSWORD", "old-password")
    import app.config as config

    config.get_settings.cache_clear()
    import app.db as db

    importlib.reload(db)
    asyncio.run(db.init_db())
    from app.services import sessions
    from app.services.auth import create_session, parse_session

    sessions.reset_for_tests()
    asyncio.run(sessions.load())
    token = create_session("admin")
    assert parse_session(token) == "admin"

    monkeypatch.setenv("SPARK_ADMIN_PASSWORD", "new-password")
    config.get_settings.cache_clear()
    assert parse_session(token) is None, "an old session survived a password rotation"
    config.get_settings.cache_clear()


def test_cookie_secure_is_auto_by_default_and_follows_the_proxy(client):
    """`auto` exists because a forced Secure cookie on a plain-HTTP homelab is
    an unexplained login loop, and its absence behind an ingress is a real
    weakness."""
    st = client.get("/api/sessions").json()
    assert st["cookie_secure_mode"] == "auto"
    assert st["cookie_secure_effective"] is False  # TestClient speaks http

    # ...and an ingress that forwards the real scheme flips it on
    st = client.get("/api/sessions", headers={"X-Forwarded-Proto": "https"}).json()
    assert st["cookie_secure_effective"] is True


def test_legacy_boolean_cookie_secure_still_parses():
    from app.config import Settings

    assert Settings(auth_cookie_secure=True).auth_cookie_secure == "true"
    assert Settings(auth_cookie_secure="false").auth_cookie_secure == "false"
    assert Settings(auth_cookie_secure="auto").auth_cookie_secure == "auto"
    with pytest.raises(ValueError):
        Settings(auth_cookie_secure="maybe")


def test_revocations_are_not_in_the_backup_bundle():
    """A restore replaces listed tables wholesale, so including this would let
    a month-old bundle un-revoke a session revoked last week."""
    from app.services.backup import _TABLES

    assert "session_revocations" not in {name for name, _ in _TABLES}
