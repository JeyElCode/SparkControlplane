"""The operator-facing routing table: what can clients actually call?

/v1/models is OpenAI-shaped and has nowhere to put the instance, node, health,
or the portal-vs-vLLM disagreement that means a name is advertised but would
404 upstream. /api/gateway/routes carries all of it, and is guarded by the
portal session rather than the gateway bearer.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    import app.config as config

    config.get_settings.cache_clear()
    import app.db as db

    importlib.reload(db)
    import app.main as main

    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c
    config.get_settings.cache_clear()


def _seed(name: str, status: str = "running", aliases: str | None = None) -> int:
    import app.db as db
    from sqlalchemy import select

    from app.models import Instance, ModelRegistry, Node

    async def run():
        async with db.SessionLocal() as s:
            head = (
                await s.execute(select(Node).where(Node.role == "head"))
            ).scalar_one_or_none()
            if head is None:
                head = Node(role="head", name="dgx-md-01", lan_ip="127.0.0.1",
                            qsfp_ip="10.0.0.1", ssh_user="u")
                s.add(head)
                await s.flush()
            model = ModelRegistry(repo_id=f"o/{name}", name=f"model-{name}", status="present")
            s.add(model)
            await s.flush()
            inst = Instance(name=name, model_id=model.id, topology="distributed",
                            status=status, port=18000, served_model_names=aliases)
            s.add(inst)
            await s.commit()
            return inst.id

    return asyncio.run(run())


def test_routes_report_what_clients_can_call(client):
    _seed("lag", aliases="laguna lag-alias")
    _seed("plain")
    _seed("off", status="stopped")
    _seed("loading", status="starting")

    info = client.get("/api/gateway/routes").json()
    assert info["base_path"] == "/v1"
    live = {r["model_name"]: r for r in info["routes"]}
    # aliases replace the registry name; an instance without aliases keeps it
    assert set(live) == {"laguna", "lag-alias", "model-plain"}
    assert live["laguna"]["instance"] == "lag"
    assert live["laguna"]["node"] == "dgx-md-01"  # multi-node serves from head

    # not-servable names are listed separately with their status, so the
    # operator can see *why* a name a client expects is missing
    down = {r["model_name"]: r["status"] for r in info["unavailable"]}
    assert down == {"model-off": "stopped", "model-loading": "starting"}


def test_routes_match_v1_models_exactly(client):
    """The two views are built from the same helper and must never drift —
    a name in one and not the other is a routing bug by definition."""
    _seed("a", aliases="alpha beta")
    _seed("b")
    _seed("c", status="error")

    routes = {r["model_name"] for r in client.get("/api/gateway/routes").json()["routes"]}
    models = {m["id"] for m in client.get("/v1/models").json()["data"]}
    assert routes == models


def test_auth_flags_tell_the_ui_whether_a_token_is_needed(client):
    info = client.get("/api/gateway/routes").json()
    assert info["auth_required"] is False  # portal auth off in this fixture
    assert info["token_configured"] is False


def test_routes_endpoint_uses_the_portal_session_not_the_gateway_bearer(tmp_path, monkeypatch):
    """It lives under /api on purpose: an operator viewing the routing table is
    using the portal, not calling the OpenAI API."""
    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SPARK_AUTH_MODE", "password")
    monkeypatch.setenv("SPARK_ADMIN_PASSWORD", "pw")
    import app.config as config

    config.get_settings.cache_clear()
    import app.db as db

    importlib.reload(db)
    import app.main as main

    importlib.reload(main)
    with TestClient(main.app) as c:
        assert c.get("/api/gateway/routes").status_code == 401
        c.post("/api/auth/login", json={"username": "admin", "password": "pw"})
        r = c.get("/api/gateway/routes")
        assert r.status_code == 200
        assert r.json()["auth_required"] is True
    config.get_settings.cache_clear()
