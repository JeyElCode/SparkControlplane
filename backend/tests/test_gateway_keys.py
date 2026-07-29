"""Per-client gateway keys, limits, and attribution.

The invariants that matter most here are not the happy paths:
  * the shared token must keep working after an upgrade (clients are already
    configured with it),
  * a revoked key must stop working on the very next request, not at some TTL,
  * and a concurrency slot must be released on EVERY exit path — a leaked slot
    would 429 that client forever, with nothing in the UI to explain why.
"""

from __future__ import annotations

import asyncio
import importlib

import httpx
import pytest
from fastapi.testclient import TestClient


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
    from app.services import apikeys, gwstats
    from app.services.ratelimit import limiter

    with _client_fixture(tmp_path, monkeypatch) as c:
        yield c
    import app.config as config

    config.get_settings.cache_clear()
    apikeys._BY_DIGEST.clear()
    apikeys._LAST_USED.clear()
    gwstats.stats.reset()
    limiter.reset()


def _seed_instance(name: str = "lag", status: str = "running") -> int:
    import app.db as db
    from sqlalchemy import select

    from app.models import Instance, ModelRegistry, Node

    async def run():
        async with db.SessionLocal() as s:
            head = (
                await s.execute(select(Node).where(Node.role == "head"))
            ).scalar_one_or_none()
            if head is None:
                head = Node(role="head", name="h1", lan_ip="127.0.0.1",
                            qsfp_ip="10.0.0.1", ssh_user="u")
                s.add(head)
                await s.flush()
            model = ModelRegistry(repo_id=f"o/{name}", name=f"model-{name}", status="present")
            s.add(model)
            await s.flush()
            inst = Instance(name=name, model_id=model.id, topology="distributed",
                            status=status, port=18000)
            s.add(inst)
            await s.commit()
            return inst.id

    return asyncio.run(run())


def _fake_upstream(monkeypatch, *, hang: asyncio.Event | None = None):
    import app.routers.gateway as gw

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    def make(verify: bool):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(gw, "_make_client", make)


# --- key lifecycle --------------------------------------------------------
def test_key_is_shown_once_and_then_never_again(client):
    r = client.post("/api/gateway/keys", json={"label": "grafana"})
    assert r.status_code == 201
    body = r.json()
    token = body["token"]
    assert token.startswith("sk-spark-")
    assert body["prefix"] in token
    # The listing must never carry the secret back.
    listed = client.get("/api/gateway/keys").json()
    assert len(listed) == 1
    assert "token" not in listed[0]
    assert listed[0]["prefix"] == body["prefix"]


def test_key_authenticates_and_is_attributed(client, monkeypatch):
    from app.services import gwstats

    _seed_instance()
    _fake_upstream(monkeypatch)
    token = client.post("/api/gateway/keys", json={"label": "team-a"}).json()["token"]

    r = client.post(
        "/v1/chat/completions",
        json={"model": "model-lag", "messages": []},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    clients = {rec.client for rec in gwstats.stats.recent}
    assert clients == {"team-a"}, "request was not attributed to the presenting key"


def test_revoked_key_stops_working_immediately(client, monkeypatch):
    _seed_instance()
    _fake_upstream(monkeypatch, )
    created = client.post("/api/gateway/keys", json={"label": "leaky"}).json()
    token = created["token"]

    from app.services import apikeys

    assert apikeys.lookup(token) is not None
    client.patch(f"/api/gateway/keys/{created['id']}", json={"enabled": False})
    assert apikeys.lookup(token) is None, "revocation must not wait for a cache TTL"

    deleted = client.post("/api/gateway/keys", json={"label": "gone"}).json()
    client.delete(f"/api/gateway/keys/{deleted['id']}")
    assert apikeys.lookup(deleted["token"]) is None


def test_shared_token_still_works_after_upgrade(tmp_path, monkeypatch):
    """Clients configured with the pre-key shared token must not break."""
    with _client_fixture(
        tmp_path, monkeypatch,
        SPARK_AUTH_MODE="password", SPARK_ADMIN_PASSWORD="pw",
        SPARK_GATEWAY_TOKEN="legacy-shared-token",
    ) as c:
        _seed_instance()
        _fake_upstream(monkeypatch)
        r = c.post("/v1/chat/completions",
                   json={"model": "model-lag", "messages": []},
                   headers={"Authorization": "Bearer legacy-shared-token"})
        assert r.status_code == 200
        # ...and a wrong token is still rejected
        r = c.post("/v1/chat/completions",
                   json={"model": "model-lag", "messages": []},
                   headers={"Authorization": "Bearer nope"})
        assert r.status_code == 401
    import app.config as config

    config.get_settings.cache_clear()


async def test_non_ascii_bearer_fails_closed_instead_of_raising(tmp_path, monkeypatch):
    """`compare_digest` raises TypeError on non-ASCII *str*, which would 500
    instead of 401 (the v1.23.1 bug, on the gateway path this time). Exercised
    against the auth function directly: httpx refuses to put a non-ASCII byte in
    a header at all, so it cannot be reached through TestClient.
    """
    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SPARK_AUTH_MODE", "password")
    monkeypatch.setenv("SPARK_ADMIN_PASSWORD", "pw")
    monkeypatch.setenv("SPARK_GATEWAY_TOKEN", "tok")
    import app.config as config

    config.get_settings.cache_clear()
    from fastapi import HTTPException

    import app.routers.gateway as gw

    class FakeRequest:
        headers = {"authorization": "Bearer nøkkel"}
        cookies: dict = {}

    with pytest.raises(HTTPException) as exc:
        await gw._gateway_auth(FakeRequest(), session=None)
    assert exc.value.status_code == 401
    config.get_settings.cache_clear()


# --- attribution of rejections -------------------------------------------
def test_rejected_requests_are_visible_and_not_blamed_on_the_shared_token(
    tmp_path, monkeypatch
):
    from app.services import gwstats

    gwstats.stats.reset()
    with _client_fixture(
        tmp_path, monkeypatch,
        SPARK_AUTH_MODE="password", SPARK_ADMIN_PASSWORD="pw", SPARK_GATEWAY_TOKEN="tok",
    ) as c:
        _seed_instance()
        c.post("/v1/chat/completions",
               json={"model": "model-lag", "messages": []},
               headers={"Authorization": "Bearer wrong"})
        rec = list(gwstats.stats.recent)
        assert rec and rec[-1].status == 401
        assert rec[-1].client == "unauthenticated"
    import app.config as config

    config.get_settings.cache_clear()
    gwstats.stats.reset()


def test_unknown_model_is_recorded(client):
    """404s were previously invisible — the operator found out by being told."""
    from app.services import gwstats

    _seed_instance()
    r = client.post("/v1/chat/completions", json={"model": "nope", "messages": []})
    assert r.status_code == 404
    assert [x.status for x in gwstats.stats.recent] == [404]


def test_traffic_endpoint_reports_attribution(client, monkeypatch):
    _seed_instance()
    _fake_upstream(monkeypatch)
    token = client.post("/api/gateway/keys", json={"label": "batch"}).json()["token"]
    for _ in range(3):
        client.post("/v1/chat/completions",
                    json={"model": "model-lag", "messages": []},
                    headers={"Authorization": f"Bearer {token}"})

    t = client.get("/api/gateway/traffic").json()
    row = next(r for r in t["since_start"] if r["client"] == "batch")
    assert row["requests"] == 3 and row["errors"] == 0
    assert row["model"] == "model-lag"
    assert len(t["recent"]) >= 3


# --- concurrency slots through the real gateway ---------------------------
def _streaming_upstream(monkeypatch, *, fail: bool = False):
    """An upstream that streams SSE chunks, optionally blowing up mid-stream."""
    import app.routers.gateway as gw

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
            if fail:
                raise httpx.ReadError("upstream died mid-stream")
            yield b"data: [DONE]\n\n"

        async def aclose(self):
            return None

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Stream(),
                              headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(gw, "_make_client",
                        lambda verify: httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_slot_is_released_after_a_normal_request(client, monkeypatch):
    from app.services.ratelimit import limiter

    _seed_instance()
    _fake_upstream(monkeypatch)
    token = client.post("/api/gateway/keys",
                        json={"label": "c", "max_concurrent": 1}).json()["token"]
    h = {"Authorization": f"Bearer {token}"}

    for _ in range(3):
        r = client.post("/v1/chat/completions",
                        json={"model": "model-lag", "messages": []}, headers=h)
        assert r.status_code == 200, "a cap of 1 must not block sequential requests"
    assert limiter.inflight("c") == 0


def test_slot_is_released_when_the_stream_finishes(client, monkeypatch):
    from app.services.ratelimit import limiter

    _seed_instance()
    _streaming_upstream(monkeypatch)
    token = client.post("/api/gateway/keys",
                        json={"label": "s", "max_concurrent": 1}).json()["token"]
    h = {"Authorization": f"Bearer {token}"}

    with client.stream("POST", "/v1/chat/completions",
                       json={"model": "model-lag", "messages": [], "stream": True},
                       headers=h) as r:
        assert r.status_code == 200
        list(r.iter_bytes())  # drain to completion
    assert limiter.inflight("s") == 0, "streaming slot leaked on normal completion"
    # ...and the client is not locked out afterwards
    with client.stream("POST", "/v1/chat/completions",
                       json={"model": "model-lag", "messages": [], "stream": True},
                       headers=h) as r:
        assert r.status_code == 200
        list(r.iter_bytes())


def test_slot_is_released_when_the_upstream_dies_mid_stream(client, monkeypatch):
    """The nastiest leak: the generator raises rather than returning, so the
    release must live in a finally, not after the loop."""
    from app.services.ratelimit import limiter

    _seed_instance()
    _streaming_upstream(monkeypatch, fail=True)
    token = client.post("/api/gateway/keys",
                        json={"label": "boom", "max_concurrent": 1}).json()["token"]
    h = {"Authorization": f"Bearer {token}"}

    with pytest.raises(Exception):
        with client.stream("POST", "/v1/chat/completions",
                           json={"model": "model-lag", "messages": [], "stream": True},
                           headers=h) as r:
            list(r.iter_bytes())
    assert limiter.inflight("boom") == 0, "slot leaked when the upstream failed"


def test_concurrency_cap_returns_429_with_retry_after(client, monkeypatch):
    from app.services.ratelimit import limiter

    iid = _seed_instance()
    _fake_upstream(monkeypatch)
    token = client.post("/api/gateway/keys",
                        json={"label": "cap", "max_concurrent": 1}).json()["token"]
    # Simulate one request already in flight (as a live stream would be).
    limiter.acquire("cap", iid, 1)
    r = client.post("/v1/chat/completions",
                    json={"model": "model-lag", "messages": []},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 429
    assert r.headers.get("Retry-After")
    assert "already has 1 in flight" in r.json()["detail"]
    limiter.release("cap", iid)


def test_rpm_cap_returns_429(client, monkeypatch):
    _seed_instance()
    _fake_upstream(monkeypatch)
    token = client.post("/api/gateway/keys",
                        json={"label": "fast", "max_rpm": 2}).json()["token"]
    h = {"Authorization": f"Bearer {token}"}
    for _ in range(2):
        assert client.post("/v1/chat/completions",
                           json={"model": "model-lag", "messages": []},
                           headers=h).status_code == 200
    r = client.post("/v1/chat/completions",
                    json={"model": "model-lag", "messages": []}, headers=h)
    assert r.status_code == 429 and "requests/min" in r.json()["detail"]


def test_limits_are_off_by_default(client, monkeypatch):
    """An upgrade must not start throttling traffic that worked yesterday."""
    _seed_instance()
    _fake_upstream(monkeypatch)
    token = client.post("/api/gateway/keys", json={"label": "unlimited"}).json()["token"]
    h = {"Authorization": f"Bearer {token}"}
    for _ in range(25):
        assert client.post("/v1/chat/completions",
                           json={"model": "model-lag", "messages": []},
                           headers=h).status_code == 200


async def test_non_ascii_token_actually_authenticates(tmp_path, monkeypatch):
    """The other half of the non-ASCII story: it must not merely fail closed —
    a CORRECT token containing æøå must WORK. Headers arrive latin-1-decoded,
    so recovering the wire bytes as UTF-8 (the obvious fix) yields four bytes
    where the client sent two, and the operator is locked out permanently."""
    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SPARK_AUTH_MODE", "password")
    monkeypatch.setenv("SPARK_ADMIN_PASSWORD", "pw")
    monkeypatch.setenv("SPARK_GATEWAY_TOKEN", "nøkkel-æøå")
    import app.config as config

    config.get_settings.cache_clear()
    import app.routers.gateway as gw

    class Req:
        # exactly what an ASGI server hands the app for a UTF-8 bearer
        headers = {"authorization": "Bearer " + "nøkkel-æøå".encode("utf-8").decode("latin-1")}
        cookies: dict = {}

    principal = await gw._gateway_auth(Req(), session=None)
    assert principal.client  # authenticated rather than 401
    config.get_settings.cache_clear()


async def test_flush_writes_aggregates_and_last_used(tmp_path, monkeypatch):
    """The storage half of #42: per-request rows never touch SQLite; the
    collector writes one aggregate row per (client, model) per window, and
    piggybacks the buffered last-used timestamps on the same transaction."""
    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    import app.config as config

    config.get_settings.cache_clear()
    import app.db as db

    importlib.reload(db)
    await db.init_db()

    from sqlalchemy import select

    from app.models import ApiKey, GatewaySample
    from app.services import apikeys, gwstats

    gwstats.stats.reset()
    async with db.SessionLocal() as s:
        row, _token = await apikeys.issue(s, "batch")
        key_id = row.id

    for status in (200, 200, 503):
        gwstats.stats.record(gwstats.RequestRecord(
            ts=1.0, client="batch", model="m", instance="i",
            status=status, duration_ms=100, ttfb_ms=20,
        ))
    apikeys.touch(key_id)

    written = await gwstats.gw_collector.flush()
    assert written == 1, "one aggregate row per (client, model), not per request"

    async with db.SessionLocal() as s:
        rows = list((await s.execute(select(GatewaySample))).scalars().all())
        assert len(rows) == 1
        assert (rows[0].requests, rows[0].errors) == (3, 1)
        assert rows[0].duration_ms_total == 300
        key = await s.get(ApiKey, key_id)
        assert key.last_used_at is not None, "last-used must be persisted by the flush"

    # draining is destructive for the window buckets but not the totals the
    # exporter reads
    assert await gwstats.gw_collector.flush() == 0
    assert gwstats.stats.totals[("batch", "m")].requests == 3
    gwstats.stats.reset()
    apikeys._BY_DIGEST.clear()
    config.get_settings.cache_clear()
