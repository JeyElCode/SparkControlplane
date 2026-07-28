"""The SPA catch-all must never serve a file outside the frontend build dir.

ASGI servers hand the app the URL path with %-escapes already decoded and dot
segments *not* collapsed, so a request line of ``GET /../../../data/secret.key``
reaches the route as ``full_path="../../../data/secret.key"``. Before the
containment check this returned the Fernet master key (and the whole SQLite DB)
unauthenticated, which is a full control-plane compromise.

These call the route function directly with the exact string ASGI would deliver
— TestClient/httpx normalizes dot segments away client-side, so a request-level
test silently passes even against vulnerable code.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi import HTTPException


@pytest.fixture()
def spa_app(tmp_path, monkeypatch):
    """Reproduce the shipped image layout: frontend at <root>/app/frontend/dist,
    data dir at <root>/data (three levels up from the build dir)."""
    frontend = tmp_path / "app" / "frontend" / "dist"
    frontend.mkdir(parents=True)
    (frontend / "index.html").write_text("<html>spa</html>")
    (frontend / "app.js").write_text("console.log(1)")
    data = tmp_path / "data"
    data.mkdir()
    (data / "secret.key").write_text("FERNET-MASTER-KEY")
    (data / "spark.sqlite3").write_text("SQLite format 3")

    monkeypatch.setenv("SPARK_DATA_DIR", str(data))
    monkeypatch.setenv("SPARK_FRONTEND_DIR", str(frontend))
    from app import config, db, main

    config.get_settings.cache_clear()
    importlib.reload(db)
    importlib.reload(main)
    yield main, frontend
    # Reloading db/main rebinds process-wide singletons (engine, SessionLocal,
    # the app object). Undo the env *before* reloading back, or later tests in
    # the same session inherit this tmp database.
    monkeypatch.undo()
    config.get_settings.cache_clear()
    importlib.reload(db)
    importlib.reload(main)


def _route(main):
    for route in main.app.routes:
        if getattr(route, "name", None) == "spa":
            return route.endpoint
    raise AssertionError("spa catch-all route not registered")


@pytest.mark.parametrize(
    "path",
    [
        "../../../data/secret.key",
        "../../../data/spark.sqlite3",
        "static/../../../../data/secret.key",
        "../" * 12 + "etc/passwd",
    ],
)
async def test_traversal_falls_back_to_index(spa_app, path):
    main, frontend = spa_app
    resp = await _route(main)(path)
    assert resp.path == str((frontend / "index.html").resolve()), (
        f"traversal escaped the build dir: {path} -> {resp.path}"
    )


async def test_real_asset_is_still_served(spa_app):
    main, frontend = spa_app
    resp = await _route(main)("app.js")
    assert resp.path == str((frontend / "app.js").resolve())


async def test_unknown_route_serves_index(spa_app):
    main, frontend = spa_app
    resp = await _route(main)("instances")
    assert resp.path == str((frontend / "index.html").resolve())


async def test_api_path_still_404s(spa_app):
    main, _ = spa_app
    with pytest.raises(HTTPException):
        await _route(main)("api/nope")


async def test_non_ascii_password_rejects_cleanly(monkeypatch):
    """compare_digest raises TypeError on non-ASCII str, which 500s instead of
    401 and locks out any admin whose password contains æøå."""
    monkeypatch.setenv("SPARK_AUTH_MODE", "password")
    monkeypatch.setenv("SPARK_ADMIN_USER", "jørgen")
    monkeypatch.setenv("SPARK_ADMIN_PASSWORD", "påssord")
    from app import config

    config.get_settings.cache_clear()
    from app.services import auth as auth_svc

    assert await auth_svc.verify_login("jørgen", "påssord") == "jørgen"
    with pytest.raises(auth_svc.AuthError):
        await auth_svc.verify_login("jørgen", "wrøng")
    with pytest.raises(auth_svc.AuthError):
        await auth_svc.verify_login("ævil", "påssord")
    config.get_settings.cache_clear()
