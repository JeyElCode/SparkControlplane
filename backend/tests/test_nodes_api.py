"""Nodes API: one head, up to 3 workers, duplicate/cap rejection."""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


def _node(name: str, role: str, octet: int) -> dict:
    return {
        "role": role,
        "name": name,
        "lan_ip": f"192.168.1.{octet}",
        "qsfp_ip": f"10.10.10.{octet - 159}",
        "ssh_user": "user",
        "ssh_password": "pw",
        "sudo_mode": "nopasswd",
    }


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


def test_up_to_four_nodes_single_head(client):
    assert client.post("/api/nodes", json=_node("spark-01", "head", 160)).status_code == 201
    assert client.post("/api/nodes", json=_node("spark-02", "worker", 161)).status_code == 201
    assert client.post("/api/nodes", json=_node("spark-03", "worker", 162)).status_code == 201
    assert client.post("/api/nodes", json=_node("spark-04", "worker", 163)).status_code == 201

    # 5th node: over the cap
    r = client.post("/api/nodes", json=_node("spark-05", "worker", 164))
    assert r.status_code == 409
    # Second head: rejected
    r = client.post("/api/nodes", json=_node("spark-99", "head", 199))
    assert r.status_code == 409

    nodes = client.get("/api/nodes").json()
    assert [n["name"] for n in nodes] == ["spark-01", "spark-02", "spark-03", "spark-04"]
    assert [n["role"] for n in nodes] == ["head", "worker", "worker", "worker"]


def test_duplicate_name_or_ip_rejected(client):
    assert client.post("/api/nodes", json=_node("spark-01", "head", 160)).status_code == 201
    dup_name = _node("spark-01", "worker", 161)
    assert client.post("/api/nodes", json=dup_name).status_code == 409
    dup_ip = _node("spark-02", "worker", 161) | {"lan_ip": "192.168.1.160"}
    assert client.post("/api/nodes", json=dup_ip).status_code == 409
