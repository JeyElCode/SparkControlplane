"""Auto port assignment + explicit-port conflict validation."""

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
    with TestClient(main.app) as c:
        yield c
    config.get_settings.cache_clear()


def _setup(client) -> tuple[int, int, int]:
    """head + worker nodes and a model; returns (head_id, worker_id, model_id)."""
    h = client.post("/api/nodes", json={
        "role": "head", "name": "s1", "lan_ip": "192.168.1.160",
        "qsfp_ip": "10.10.10.1", "ssh_user": "u", "ssh_password": "p",
    }).json()
    w = client.post("/api/nodes", json={
        "role": "worker", "name": "s2", "lan_ip": "192.168.1.161",
        "qsfp_ip": "10.10.10.2", "ssh_user": "u", "ssh_password": "p",
    }).json()
    m = client.post("/api/models", json={"repo_id": "org/m"}).json()
    return h["id"], w["id"], m["id"]


def _mk(client, model_id, name, **kw):
    return client.post("/api/instances", json={
        "name": name, "model_id": model_id, "topology": "cluster", **kw,
    })


def test_auto_assignment_is_sequential_and_skips_taken(client):
    _, _, mid = _setup(client)
    a = _mk(client, mid, "a").json()
    b = _mk(client, mid, "b").json()
    assert (a["port"], b["port"]) == (8000, 8001)
    # explicit 8002 taken -> next auto lands on 8003
    c = _mk(client, mid, "c", port=8002).json()
    d = _mk(client, mid, "d").json()
    assert (c["port"], d["port"]) == (8002, 8003)
    # distributed master ports auto-assign from 29500
    e = _mk(client, mid, "e", topology="distributed").json()
    f = _mk(client, mid, "f", topology="distributed").json()
    assert (e["master_port"], f["master_port"]) == (29500, 29501)


def test_explicit_conflicts_rejected_same_host_allowed_cross_node(client):
    head_id, worker_id, mid = _setup(client)
    _mk(client, mid, "a", port=8000)
    # same serving node (head) + same port -> 409
    r = _mk(client, mid, "b", port=8000)
    assert r.status_code == 409 and "8000" in r.json()["detail"]
    # reserved infra port -> 409
    assert _mk(client, mid, "ray", port=8265).status_code == 409
    # two singles pinned to DIFFERENT nodes may share an explicit port
    s1 = _mk(client, mid, "s-head", topology="single", node_id=head_id, port=9000)
    s2 = _mk(client, mid, "s-worker", topology="single", node_id=worker_id, port=9000)
    assert s1.status_code == 201 and s2.status_code == 201
    # ...but a single pinned to the head clashes with cluster instances there
    assert _mk(client, mid, "s-clash", topology="single",
               node_id=head_id, port=8000).status_code == 409
    # port range validation
    assert _mk(client, mid, "low", port=80).status_code == 422


def test_update_conflict_and_null_port_keep(client):
    head_id, _, mid = _setup(client)
    a = _mk(client, mid, "a").json()          # 8000
    b = _mk(client, mid, "b").json()          # 8001
    r = client.patch(f"/api/instances/{b['id']}", json={"port": 8000})
    assert r.status_code == 409
    # null port in a PATCH means "keep", not "clear"
    r = client.patch(f"/api/instances/{b['id']}", json={"port": None, "max_num_seqs": 4})
    assert r.status_code == 200 and r.json()["port"] == 8001
    # moving to a free port works
    assert client.patch(f"/api/instances/{a['id']}", json={"port": 8100}).json()["port"] == 8100
