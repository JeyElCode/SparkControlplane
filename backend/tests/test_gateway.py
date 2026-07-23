"""API gateway: model routing, auth matrix, streaming passthrough, hints."""

from __future__ import annotations

import asyncio
import importlib
import json
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from app.services.scheduler import next_window_open


def _client_fixture(tmp_path, monkeypatch, **env):
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
def client(tmp_path, monkeypatch):
    with _client_fixture(tmp_path, monkeypatch) as c:
        yield c
    import app.config as config

    config.get_settings.cache_clear()


def _seed_instance(name: str, status: str = "running", aliases: str | None = None,
                   api_key: str | None = None) -> int:
    import app.db as db
    from app.crypto import encrypt
    from app.models import Instance, ModelRegistry, Node
    from sqlalchemy import select

    async def run():
        async with db.SessionLocal() as s:
            head = (
                await s.execute(select(Node).where(Node.role == "head"))
            ).scalar_one_or_none()
            if head is None:
                head = Node(role="head", name="h1", lan_ip="127.0.0.1", qsfp_ip="10.0.0.1",
                            ssh_user="u")
                s.add(head)
                await s.flush()
            model = ModelRegistry(repo_id=f"o/{name}", name=f"model-{name}", status="present")
            s.add(model)
            await s.flush()
            inst = Instance(name=name, model_id=model.id, topology="distributed",
                            status=status, port=18000, served_model_names=aliases,
                            api_key_enc=encrypt(api_key) if api_key else None)
            s.add(inst)
            await s.commit()
            return inst.id

    return asyncio.run(run())


def _fake_upstream(monkeypatch, record: dict):
    """Swap the gateway's httpx client for one hitting an in-memory vLLM."""
    import app.routers.gateway as gw

    def handler(request: httpx.Request) -> httpx.Response:
        record["url"] = str(request.url)
        record["auth"] = request.headers.get("authorization")
        payload = json.loads(request.content)
        record["body"] = payload
        if payload.get("stream"):
            class _SSE(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
                    yield b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
                    yield b"data: [DONE]\n\n"

                async def aclose(self):  # noqa: D401
                    pass

            return httpx.Response(200, stream=_SSE(),
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={
            "id": "cmpl-1", "choices": [{"message": {"content": "Hello"}}],
        })

    def make_client(verify: bool) -> httpx.AsyncClient:
        record["verify"] = verify
        return httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 timeout=30.0)

    monkeypatch.setattr(gw, "_make_client", make_client)


def test_models_and_routing(client, monkeypatch):
    _seed_instance("lag", aliases="laguna lag-alias", api_key="inner-key")
    _seed_instance("stopped-one", status="stopped")

    data = client.get("/v1/models").json()["data"]
    ids = {m["id"] for m in data}
    assert ids == {"model-lag", "laguna", "lag-alias"}  # only RUNNING instances

    record: dict = {}
    _fake_upstream(monkeypatch, record)
    r = client.post("/v1/chat/completions", json={
        "model": "laguna", "messages": [{"role": "user", "content": "hi"}],
    })
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "Hello"
    assert record["url"].endswith("/v1/chat/completions")
    assert record["auth"] == "Bearer inner-key"  # instance key injected
    assert record["body"]["model"] == "laguna"

    # unknown model -> 404 listing what's live
    r = client.post("/v1/chat/completions", json={"model": "nope", "messages": []})
    assert r.status_code == 404 and "laguna" in r.json()["detail"]
    # model on a stopped instance -> 503
    r = client.post("/v1/chat/completions", json={"model": "model-stopped-one", "messages": []})
    assert r.status_code == 503 and "not running" in r.json()["detail"]
    # missing model field -> 422
    assert client.post("/v1/chat/completions", json={"messages": []}).status_code == 422


def test_streaming_passthrough(client, monkeypatch):
    _seed_instance("s")
    record: dict = {}
    _fake_upstream(monkeypatch, record)
    with client.stream("POST", "/v1/chat/completions", json={
        "model": "model-s", "messages": [], "stream": True,
    }) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        body = b"".join(r.iter_bytes())
    assert b'"Hel"' in body and b"[DONE]" in body


def test_gateway_auth_matrix(tmp_path, monkeypatch):
    with _client_fixture(
        tmp_path, monkeypatch,
        SPARK_AUTH_MODE="password", SPARK_ADMIN_PASSWORD="pw",
        SPARK_GATEWAY_TOKEN="gw-secret",
    ) as c:
        _seed_instance("a")
        # no credential -> 401 with guidance
        r = c.get("/v1/models")
        assert r.status_code == 401 and "Bearer" in r.json()["detail"]
        # wrong bearer -> 401
        assert c.get("/v1/models", headers={"Authorization": "Bearer nope"}).status_code == 401
        # correct bearer -> 200
        assert c.get("/v1/models", headers={"Authorization": "Bearer gw-secret"}).status_code == 200
        # portal session cookie also accepted
        from app.services import auth as auth_svc

        auth_svc._FAILS.clear()
        c.post("/api/auth/login", json={"username": "admin", "password": "pw"})
        assert c.get("/v1/models").status_code == 200
    import app.config as config

    config.get_settings.cache_clear()


def test_next_window_open():
    import app.models as m

    # 2026-07-20 is a Monday; window Tue+Thu 09:00
    s = m.InstanceSchedule(instance_id=1, days="1,3", start_time="09:00",
                           end_time="17:00", enabled=True)
    now = datetime(2026, 7, 20, 12, 0)  # Monday noon
    nxt = next_window_open([s], now)
    assert (nxt.weekday(), nxt.hour) == (1, 9)  # Tuesday 09:00
    s.enabled = False
    assert next_window_open([s], now) is None
