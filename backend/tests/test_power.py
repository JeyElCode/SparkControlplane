"""Power controls: magic-packet construction, MAC normalization, endpoints."""

from __future__ import annotations

import importlib
import socket
import threading

import pytest
from fastapi.testclient import TestClient

from app.services.power import build_magic_packet, normalize_mac, send_wol_udp


def test_normalize_mac():
    assert normalize_mac("AA-BB-CC-DD-EE-FF") == "aa:bb:cc:dd:ee:ff"
    assert normalize_mac(" aa:bb:cc:dd:ee:ff ") == "aa:bb:cc:dd:ee:ff"
    assert normalize_mac("nope") is None
    assert normalize_mac("aa:bb:cc:dd:ee") is None
    assert normalize_mac(None) is None


def test_magic_packet_layout():
    pkt = build_magic_packet("aa:bb:cc:dd:ee:ff")
    assert len(pkt) == 102  # 6 sync + 16*6
    assert pkt[:6] == b"\xff" * 6
    assert pkt[6:12] == bytes.fromhex("aabbccddeeff")
    assert pkt[6:] == bytes.fromhex("aabbccddeeff") * 16
    with pytest.raises(ValueError):
        build_magic_packet("garbage")


def test_send_wol_udp_unicast_loopback():
    """The fallback sender actually puts the right bytes on the wire."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    srv.settimeout(5)
    port = srv.getsockname()[1]
    got: list[bytes] = []

    def recv():
        data, _ = srv.recvfrom(2048)
        got.append(data)

    t = threading.Thread(target=recv)
    t.start()
    send_wol_udp("aa:bb:cc:dd:ee:ff", host="127.0.0.1", port=port)
    t.join(timeout=5)
    srv.close()
    assert got and got[0] == build_magic_packet("aa:bb:cc:dd:ee:ff")


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


def test_power_endpoints_validate(client):
    assert client.post("/api/power/nodes/999/shutdown").status_code == 404
    assert client.post("/api/power/nodes/1/frobnicate").status_code == 404
    assert client.post("/api/power/batch/frobnicate").status_code == 404

    r = client.post(
        "/api/nodes",
        json={
            "role": "head", "name": "spark-01", "lan_ip": "192.168.1.160",
            "qsfp_ip": "10.10.10.1", "ssh_user": "user", "ssh_password": "pw",
        },
    )
    node_id = r.json()["id"]
    assert client.get(f"/api/power/nodes/{node_id}/affected").json() == []

    # MAC set via PATCH is normalized; junk is rejected
    ok = client.patch(f"/api/nodes/{node_id}", json={"mac_address": "AA-BB-CC-DD-EE-FF"})
    assert ok.status_code == 200 and ok.json()["mac_address"] == "aa:bb:cc:dd:ee:ff"
    assert client.patch(f"/api/nodes/{node_id}", json={"mac_address": "junk"}).status_code == 422
