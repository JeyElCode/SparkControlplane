"""Storage report parsing, orphan detection, and action validation."""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.services.storage import parse_df_line, parse_du_lines


def test_parse_du_lines():
    raw = (
        "68157440\t/home/u/models/DeepSeek-V4-Flash-W4A16-FP8/\n"
        "1024\t/home/u/models/old-experiment/\n"
        "garbage line\n"
    )
    assert parse_du_lines(raw) == [
        ("DeepSeek-V4-Flash-W4A16-FP8", 68157440 * 1024),
        ("old-experiment", 1024 * 1024),
    ]
    assert parse_du_lines("") == []


def test_parse_df_line():
    d = parse_df_line("/dev/nvme0n1p2 3844529104 1544529104 2300000000 41% /")
    assert d == {"total_bytes": 3844529104 * 1024, "used_bytes": 1544529104 * 1024,
                 "free_bytes": 2300000000 * 1024}
    assert parse_df_line("no numbers here") is None


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


FAKE_SCAN = """@@df@@
/dev/nvme0n1p2 3844529104 1544529104 2300000000 41% /
@@dirs@@
68157440\t/home/u/models/model-a/
2048000\t/home/u/models/stray-dir/
@@hf@@
5242880\t/home/u/.cache/huggingface
"""


def test_report_flags_orphans(client, monkeypatch):
    client.post("/api/nodes", json={
        "role": "head", "name": "spark-01", "lan_ip": "192.168.1.160",
        "qsfp_ip": "10.10.10.1", "ssh_user": "u", "ssh_password": "pw",
    })
    client.post("/api/models", json={"repo_id": "org/model-a"})

    from app.services import storage as storage_svc

    async def fake_ssh(session, node):
        async def run(script, **kw):
            return SimpleNamespace(ok=True, stdout=FAKE_SCAN, stderr="")

        return SimpleNamespace(run=run)

    monkeypatch.setattr(storage_svc, "ssh_for_node", fake_ssh)
    report = client.get("/api/storage").json()
    assert len(report) == 1
    n = report[0]
    assert n["reachable"] is True
    assert [m["name"] for m in n["models"]] == ["model-a"]
    assert [o["name"] for o in n["orphans"]] == ["stray-dir"]
    assert n["orphans"][0]["size_bytes"] == 2048000 * 1024
    assert n["hf_cache_bytes"] == 5242880 * 1024
    assert n["disk"]["free_bytes"] == 2300000000 * 1024


def test_delete_orphan_validation(client):
    client.post("/api/nodes", json={
        "role": "head", "name": "spark-01", "lan_ip": "192.168.1.160",
        "qsfp_ip": "10.10.10.1", "ssh_user": "u", "ssh_password": "pw",
    })
    client.post("/api/models", json={"repo_id": "org/model-a"})
    node_id = client.get("/api/nodes").json()[0]["id"]

    # path traversal / bad names rejected at the API boundary
    assert client.post("/api/storage/delete-orphan",
                       json={"node_id": node_id, "name": "../etc"}).status_code == 422
    assert client.post("/api/storage/delete-orphan",
                       json={"node_id": node_id, "name": "a b"}).status_code == 422

    # a REGISTERED model name is refused by the job (service-level guard)
    from app.services.storage import delete_orphan

    class H:
        async def log(self, *a, **k): ...

    with pytest.raises(RuntimeError, match="registered model"):
        asyncio.run(delete_orphan(H(), node_id, "model-a"))
