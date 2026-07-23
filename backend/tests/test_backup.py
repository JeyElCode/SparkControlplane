"""Backup/restore: SigV4 correctness, bundle roundtrip, secrets-key mismatch,
and the S3 flow end-to-end against an in-memory fake S3."""

from __future__ import annotations

import asyncio
import importlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.services.s3lite import S3Client, S3Config, sign_request


def test_sigv4_matches_aws_documentation_vector():
    h = sign_request(
        method="GET", host="examplebucket.s3.amazonaws.com", path="/test.txt",
        query={}, region="us-east-1", access_key="AKIAIOSFODNN7EXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        payload_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        amz_date="20130524T000000Z", extra_headers={"range": "bytes=0-9"},
    )
    assert h["Authorization"].endswith(
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )


# --- in-memory fake S3 (path-style) ---------------------------------------
class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            assert "AWS4-HMAC-SHA256" in request.headers.get("Authorization", "")
            path = request.url.path.lstrip("/")  # bucket/key...
            _, _, key = path.partition("/")
            if request.method == "PUT":
                self.objects[key] = request.content
                return httpx.Response(200)
            if request.method == "GET" and "list-type" in dict(request.url.params):
                prefix = dict(request.url.params).get("prefix", "")
                items = "".join(
                    f"<Contents><Key>{k}</Key><Size>{len(v)}</Size>"
                    f"<LastModified>2026-07-23T00:00:00Z</LastModified></Contents>"
                    for k, v in sorted(self.objects.items()) if k.startswith(prefix)
                )
                xml = f'<?xml version="1.0"?><ListBucketResult>{items}</ListBucketResult>'
                return httpx.Response(200, text=xml)
            if request.method == "GET":
                if key not in self.objects:
                    return httpx.Response(404)
                return httpx.Response(200, content=self.objects[key])
            if request.method == "DELETE":
                self.objects.pop(key, None)
                return httpx.Response(204)
            return httpx.Response(400)

        return httpx.MockTransport(handler)


def _client(fake: FakeS3) -> S3Client:
    cfg = S3Config(endpoint="https://minio.test:9000", bucket="backups",
                   region="us-east-1", access_key="ak", secret_key="sk")
    return S3Client(cfg, transport=fake.transport())


def test_s3lite_roundtrip():
    fake = FakeS3()
    c = _client(fake)

    async def run():
        await c.put_object("p/a.json", b"AAA")
        await c.put_object("p/b.json", b"BBB")
        assert await c.get_object("p/a.json") == b"AAA"
        listed = await c.list_objects("p/")
        assert [o["key"] for o in listed] == ["p/a.json", "p/b.json"]
        await c.delete_object("p/a.json")
        assert [o["key"] for o in await c.list_objects("p/")] == ["p/b.json"]

    asyncio.run(run())


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


def _seed(client) -> None:
    client.post("/api/nodes", json={
        "role": "head", "name": "spark-01", "lan_ip": "192.168.1.160",
        "qsfp_ip": "10.10.10.1", "ssh_user": "u", "ssh_password": "pw",
    })
    client.post("/api/models", json={"repo_id": "org/model-a"})


def test_export_import_roundtrip_preserves_config(client):
    _seed(client)
    node_id = client.get("/api/nodes").json()[0]["id"]

    bundle = client.get("/api/backup/export").json()
    assert bundle["kind"] == "spark-controlplane-backup"
    assert len(bundle["tables"]["nodes"]) == 1
    # encrypted secret travels in encrypted form
    assert bundle["tables"]["nodes"][0]["ssh_password_enc"]

    # wreck the config, then restore
    client.delete(f"/api/nodes/{node_id}")
    assert client.get("/api/nodes").json() == []
    r = client.post("/api/backup/import", json=bundle)
    assert r.status_code == 200
    body = r.json()
    assert body["restored"]["nodes"] == 1 and body["cleared_secrets"] == []

    nodes = client.get("/api/nodes").json()
    assert nodes[0]["name"] == "spark-01" and nodes[0]["id"] == node_id
    assert nodes[0]["has_ssh_password"] is True  # same key -> secret survives
    assert len(client.get("/api/models").json()) == 1

    # garbage bundle rejected
    assert client.post("/api/backup/import", json={"nope": 1}).status_code == 422


def test_import_with_wrong_key_clears_secrets(client):
    _seed(client)
    bundle = client.get("/api/backup/export").json()
    # simulate "encrypted with a different SPARK_SECRET_KEY"
    bundle["tables"]["nodes"][0]["ssh_password_enc"] = "gAAAAABnot-a-real-token"
    r = client.post("/api/backup/import", json=bundle)
    assert r.status_code == 200
    assert "nodes.ssh_password_enc" in r.json()["cleared_secrets"]
    assert client.get("/api/nodes").json()[0]["has_ssh_password"] is False


def test_s3_backup_run_list_restore_and_retention(client, monkeypatch):
    from app.services import backup as backup_svc

    _seed(client)
    r = client.patch("/api/cluster/settings", json={
        "backup_enabled": True,
        "backup_s3_endpoint": "https://minio.test:9000",
        "backup_s3_bucket": "backups",
        "backup_s3_prefix": "cfg/",
        "backup_s3_access_key": "ak",
        "backup_s3_secret": "sk",
        "backup_retention": 2,
    })
    assert r.status_code == 200 and r.json()["has_backup_s3_secret"] is True

    fake = FakeS3()
    real_init = S3Client.__init__

    def patched_init(self, cfg, timeout=30.0, transport=None):
        real_init(self, cfg, timeout=timeout, transport=fake.transport())

    monkeypatch.setattr(S3Client, "__init__", patched_init)

    # three runs with retention 2 -> oldest pruned
    keys = []
    for _ in range(3):
        rr = client.post("/api/backup/run")
        assert rr.status_code == 200
        keys.append(rr.json()["key"])
        import time

        time.sleep(1.1)  # distinct timestamped keys
    listed = client.get("/api/backup/s3").json()
    assert len(listed) == 2
    assert listed[0]["key"] == keys[-1]  # newest first

    # wreck config, restore straight from S3
    nid = client.get("/api/nodes").json()[0]["id"]
    client.delete(f"/api/nodes/{nid}")
    rr = client.post("/api/backup/s3-restore", json={"key": keys[-1]})
    assert rr.status_code == 200
    assert client.get("/api/nodes").json()[0]["name"] == "spark-01"

    status = client.get("/api/backup/status").json()
    assert status["last_key"] == keys[-1] and status["last_error"] is None
