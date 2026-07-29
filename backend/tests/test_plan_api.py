"""The /api/instances/plan endpoint end to end.

test_plan.py covers the arithmetic in isolation; this covers the wiring —
that the endpoint gathers real cluster facts, that its output is directly
usable as a create body, and that it never reaches the network in a test.
"""

from __future__ import annotations

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

    # The planner backfills a model's geometry from HuggingFace on first use.
    # A unit test must not depend on the network being up (or on what a repo
    # currently says), so stub it — the offline path is itself under test in
    # test_plan.py.
    import app.services.models_svc as models_svc

    async def _no_fetch(session, model):
        return False

    monkeypatch.setattr(models_svc, "capture_shape", _no_fetch)

    with TestClient(main.app) as c:
        yield c
    config.get_settings.cache_clear()


def _two_nodes(client):
    client.post("/api/nodes", json={
        "role": "head", "name": "spark-01", "lan_ip": "192.168.1.160",
        "qsfp_ip": "10.10.10.1", "ssh_user": "u", "ssh_password": "p",
    })
    client.post("/api/nodes", json={
        "role": "worker", "name": "spark-02", "lan_ip": "192.168.1.161",
        "qsfp_ip": "10.10.10.2", "ssh_user": "u", "ssh_password": "p",
    })


def _model(client, repo="org/tiny", **shape):
    m = client.post("/api/models", json={"repo_id": repo}).json()
    if shape:
        # Stand in for the config.json fetch the planner would do on first use.
        import asyncio

        import app.db as db
        from app.models import ModelRegistry

        async def _set():
            async with db.SessionLocal() as s:
                row = await s.get(ModelRegistry, m["id"])
                for k, v in shape.items():
                    setattr(row, k, v)
                await s.commit()

        asyncio.run(_set())
    return m["id"]


LLAMA8B = dict(
    size_bytes=16 * 1024 ** 3, num_layers=32, num_kv_heads=8, head_dim=128,
    torch_dtype="bfloat16", context_len=131072,
)


def test_plan_returns_a_usable_configuration(client):
    _two_nodes(client)
    mid = _model(client, **LLAMA8B)

    r = client.post("/api/instances/plan", json={"model_id": mid})
    assert r.status_code == 200, r.text
    p = r.json()

    assert p["feasible"] is True
    assert p["name"]
    assert p["summary"]
    assert p["settings"]["topology"] == "single"
    assert p["settings"]["gpu_memory_utilization"] > 0
    assert any(x["field"] == "topology" for x in p["reasons"])


def test_the_plan_can_be_posted_straight_back_to_create(client):
    """The contract that makes one-click launch possible: whatever the planner
    returns is a valid create body with only a name and model id added."""
    _two_nodes(client)
    mid = _model(client, **LLAMA8B)
    p = client.post("/api/instances/plan", json={"model_id": mid}).json()

    r = client.post("/api/instances", json={**p["settings"], "name": p["name"], "model_id": mid})
    assert r.status_code == 201, r.text
    inst = r.json()
    assert inst["topology"] == p["settings"]["topology"]
    assert inst["gpu_memory_utilization"] == p["settings"]["gpu_memory_utilization"]
    # Port was left to the allocator and came back assigned.
    assert inst["port"] > 0


def _mark_all_running():
    import asyncio

    from sqlalchemy import select

    import app.db as db
    from app.models import Instance

    async def _go():
        async with db.SessionLocal() as s:
            for row in (await s.execute(select(Instance))).scalars().all():
                row.status = "running"
            await s.commit()

    asyncio.run(_go())


def test_a_second_model_lands_on_the_other_node(client):
    """Two nodes, two small models: the planner spreads rather than stacking,
    because it plans against what is actually committed, not a static default."""
    _two_nodes(client)
    mid = _model(client, **LLAMA8B)
    first = client.post("/api/instances/plan", json={"model_id": mid}).json()
    client.post("/api/instances", json={**first["settings"], "name": "first", "model_id": mid})
    _mark_all_running()

    second = client.post("/api/instances/plan", json={"model_id": mid}).json()
    assert second["settings"]["node_id"] != first["settings"]["node_id"]
    assert second["feasible"]


def test_a_running_instance_shrinks_the_next_plan_on_a_full_node(client):
    """With nowhere to spread to, the second plan must see the memory the first
    one holds — the whole reason to plan against live state."""
    client.post("/api/nodes", json={
        "role": "head", "name": "solo", "lan_ip": "192.168.1.160",
        "qsfp_ip": "10.10.10.1", "ssh_user": "u", "ssh_password": "p",
    })
    mid = _model(client, **LLAMA8B)
    first = client.post("/api/instances/plan", json={"model_id": mid}).json()
    client.post("/api/instances", json={**first["settings"], "name": "first", "model_id": mid})
    _mark_all_running()

    second = client.post("/api/instances/plan", json={"model_id": mid}).json()
    assert (
        second["settings"]["gpu_memory_utilization"]
        < first["settings"]["gpu_memory_utilization"]
    ), "planning ignored the memory an already-running instance holds"
    assert not second["feasible"]
    assert second["warnings"]


def test_even_an_infeasible_plan_is_a_postable_body(client):
    """The plan is always valid input — an operator who knows better can still
    post it, and a 422 on our own output would be a broken promise."""
    client.post("/api/nodes", json={
        "role": "head", "name": "solo", "lan_ip": "192.168.1.160",
        "qsfp_ip": "10.10.10.1", "ssh_user": "u", "ssh_password": "p",
    })
    mid = _model(client, **LLAMA8B)
    first = client.post("/api/instances/plan", json={"model_id": mid}).json()
    client.post("/api/instances", json={**first["settings"], "name": "first", "model_id": mid})
    _mark_all_running()

    p = client.post("/api/instances/plan", json={"model_id": mid}).json()
    assert not p["feasible"]
    r = client.post("/api/instances", json={**p["settings"], "name": "second", "model_id": mid})
    assert r.status_code == 201, r.text


def test_plan_honours_a_pinned_topology(client):
    _two_nodes(client)
    mid = _model(client, **LLAMA8B)
    p = client.post(
        "/api/instances/plan", json={"model_id": mid, "topology": "distributed"}
    ).json()
    assert p["settings"]["topology"] == "distributed"


def test_plan_suggests_a_free_name(client):
    _two_nodes(client)
    mid = _model(client, **LLAMA8B)
    p1 = client.post("/api/instances/plan", json={"model_id": mid}).json()
    client.post("/api/instances", json={**p1["settings"], "name": p1["name"], "model_id": mid})
    p2 = client.post("/api/instances/plan", json={"model_id": mid}).json()
    assert p2["name"] != p1["name"], "suggested a name that is already taken"


def test_plan_for_an_unknown_model_is_404(client):
    assert client.post("/api/instances/plan", json={"model_id": 9999}).status_code == 404


def test_plan_with_no_nodes_is_infeasible_not_an_error(client):
    mid = _model(client, **LLAMA8B)
    r = client.post("/api/instances/plan", json={"model_id": mid})
    assert r.status_code == 200
    assert r.json()["feasible"] is False
    assert r.json()["warnings"]


def test_plan_creates_nothing(client):
    _two_nodes(client)
    mid = _model(client, **LLAMA8B)
    client.post("/api/instances/plan", json={"model_id": mid})
    assert client.get("/api/instances").json() == []


async def test_upgrade_adds_the_shape_columns_as_null(tmp_path, monkeypatch):
    """An install from before v1.30.0 must gain the geometry columns as NULL —
    i.e. "not yet fetched", which the planner backfills on first use. A failed
    migration here would take out the whole models table, not just planning."""
    import importlib
    import sqlite3

    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    import app.config as config

    config.get_settings.cache_clear()
    import app.db as db

    importlib.reload(db)
    await db.init_db()

    from app.models import ModelRegistry

    async with db.SessionLocal() as s:
        s.add(ModelRegistry(repo_id="org/pre-existing", name="pre-existing"))
        await s.commit()

    con = sqlite3.connect(tmp_path / "spark.sqlite3")
    cols = {r[1] for r in con.execute("PRAGMA table_info('models')")}
    assert {"context_len", "num_layers", "num_kv_heads", "head_dim", "torch_dtype"} <= cols
    row = con.execute(
        "SELECT context_len, num_layers, num_kv_heads, head_dim, torch_dtype FROM models"
    ).fetchone()
    assert row == (None, None, None, None, None)
    con.close()
    config.get_settings.cache_clear()
