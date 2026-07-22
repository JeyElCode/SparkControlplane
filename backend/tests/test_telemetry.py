"""Telemetry engine: collector-output parsing, rate derivation across ticks,
history ring points, and the cached status/history endpoints."""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from app.services.telemetry import CounterState, history_point, parse_sample

RAW_TICK_1 = """
@@gpu@@
0, NVIDIA GB10, 51200, 122880, 37, 62, 41.2
@@gpuproc@@
12345, /usr/bin/python3, 49152
@@cpu@@
cpu  1000 0 500 8000 100 0 50 0 0 0
@@nproc@@
20
@@load@@
1.25 0.80 0.60 2/1500 99999
@@mem@@
MemTotal:       131072000 kB
MemAvailable:    65536000 kB
@@uptime@@
86400.55 1700000.00
@@net@@
Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: 100 1 0 0 0 0 0 0 100 1 0 0 0 0 0 0
  enp1s0f1np1: 1000000 10 0 0 0 0 0 0 2000000 20 0 0 0 0 0 0
  enP2p1s0: 500000 5 0 0 0 0 0 0 600000 6 0 0 0 0 0 0
  docker0: 999 1 0 0 0 0 0 0 999 1 0 0 0 0 0 0
@@defroute@@
default via 192.168.1.1 dev enP2p1s0 proto dhcp metric 100
@@disk@@
/dev/nvme0n1p2 3844529104 1000000000 2844529104  27% /
@@docker@@
spark-ray-head
__docker_ok__
"""

# 10 seconds later: +100 MB rx on qsfp, +50 MB tx; cpu busy +1000 of +2000 total
RAW_TICK_2 = RAW_TICK_1.replace(
    "cpu  1000 0 500 8000 100 0 50 0 0 0", "cpu  2000 0 1500 9000 100 0 50 0 0 0"
).replace(
    "enp1s0f1np1: 1000000 10 0 0 0 0 0 0 2000000 20",
    "enp1s0f1np1: 101000000 10 0 0 0 0 0 0 52000000 20",
)


def _parse(raw: str, ts: float, prev: CounterState):
    return parse_sample(
        raw, node_id=1, ts=ts, qsfp_iface="enp1s0f1np1",
        models_dir="/home/user/models", prev=prev,
    )


def test_parse_first_tick_no_rates():
    s, nxt = _parse(RAW_TICK_1, ts=100.0, prev=CounterState())
    assert s.reachable
    assert s.gpus[0].util_pct == 37 and s.gpus[0].temp_c == 62
    assert s.gpu_procs[0].pid == 12345 and s.gpu_procs[0].name == "python3"
    assert s.cpu_pct is None  # no baseline yet
    assert s.cpu_count == 20 and s.loadavg_1m == 1.25
    assert s.mem_total_mib == 128000 and s.mem_used_mib == 64000
    assert s.uptime_seconds == pytest.approx(86400.55)
    ifaces = {r.iface: r for r in s.net}
    assert set(ifaces) == {"enp1s0f1np1", "enP2p1s0"}  # lo/docker0 filtered
    assert ifaces["enp1s0f1np1"].kind == "qsfp" and ifaces["enP2p1s0"].kind == "lan"
    assert ifaces["enp1s0f1np1"].rx_bps is None
    assert s.disk.total_bytes == 3844529104 * 1024
    assert s.docker_ok is True and s.docker_names == ["spark-ray-head"]
    assert nxt.net["enp1s0f1np1"] == (1000000, 2000000)


def test_rates_derive_from_deltas():
    _, prev = _parse(RAW_TICK_1, ts=100.0, prev=CounterState())
    s, _ = _parse(RAW_TICK_2, ts=110.0, prev=prev)
    # cpu: busy delta 2000 of total delta 3000... compute: t1 busy=1000+500+50=?
    # busy = total - idle - iowait; t1: total=9650, idle=8100 -> busy=1550
    # t2: total=12650, idle=9100 -> busy=3550 => 2000/3000 = 66.7%
    assert s.cpu_pct == pytest.approx(66.7, abs=0.1)
    q = next(r for r in s.net if r.kind == "qsfp")
    assert q.rx_bps == pytest.approx(100_000_000 / 10)
    assert q.tx_bps == pytest.approx(50_000_000 / 10)
    pt = history_point(s)
    assert pt.gpu_util_pct == 37 and pt.qsfp_rx_bps == q.rx_bps
    assert pt.mem_used_mib == 64000


def test_counter_reset_yields_no_rate():
    _, prev = _parse(RAW_TICK_2, ts=100.0, prev=CounterState())
    s, _ = _parse(RAW_TICK_1, ts=110.0, prev=prev)  # counters went backwards
    q = next(r for r in s.net if r.kind == "qsfp")
    assert q.rx_bps is None and q.tx_bps is None


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


def test_status_and_history_endpoints_serve_from_cache(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["nodes"] == [] and body["instances"] == []
    assert body["ray_required"] is False
    h = client.get("/api/status/history?minutes=5")
    assert h.status_code == 200 and h.json() == []


def test_ray_required_only_for_cluster_topology(client):
    """Ray health is contextual: only a cluster-topology instance makes a
    stopped Ray cluster a fault."""
    import asyncio

    import app.db as db
    from app.models import Instance, ModelRegistry

    async def seed(topology: str, name: str) -> None:
        async with db.SessionLocal() as s:
            model = ModelRegistry(repo_id=f"org/{name}", name=name, status="present")
            s.add(model)
            await s.flush()
            s.add(Instance(name=name, model_id=model.id, topology=topology))
            await s.commit()

    asyncio.run(seed("distributed", "m-dist"))
    assert client.get("/api/status").json()["ray_required"] is False  # Ray-less topology

    asyncio.run(seed("cluster", "m-clu"))
    assert client.get("/api/status").json()["ray_required"] is True


class _FakeSSH:
    """Returns collector output whose counters advance every call, so the
    engine derives real rates from the second tick on."""

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, script: str, **kw):
        from types import SimpleNamespace

        if "@@gpu@@" in script:  # only collector ticks advance the counters
            self.calls += 1
        n = self.calls
        raw = RAW_TICK_1.replace(
            "cpu  1000 0 500 8000 100 0 50 0 0 0",
            f"cpu  {1000 + n * 1000} 0 500 {8000 + n * 1000} 100 0 50 0 0 0",
        ).replace(
            "enp1s0f1np1: 1000000 10 0 0 0 0 0 0 2000000 20",
            f"enp1s0f1np1: {1000000 + n * 10_000_000} 10 0 0 0 0 0 0 {2000000 + n * 5_000_000} 20",
        )
        return SimpleNamespace(ok=True, stdout=raw, stderr="")


@pytest.fixture()
def live_engine_client(tmp_path, monkeypatch):
    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SPARK_TELEMETRY_FAST_SECONDS", "1")
    monkeypatch.setenv("SPARK_TELEMETRY_SLOW_SECONDS", "2")
    import app.config as config

    config.get_settings.cache_clear()
    import app.db as db

    importlib.reload(db)
    fake = _FakeSSH()

    async def fake_ssh_for_node(session, node):
        return fake

    import app.services.status_svc as status_svc
    import app.services.telemetry as telemetry

    monkeypatch.setattr(telemetry, "ssh_for_node", fake_ssh_for_node)
    monkeypatch.setattr(status_svc, "ssh_for_node", fake_ssh_for_node)
    import app.main as main

    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c
    config.get_settings.cache_clear()


def test_engine_samples_a_node_end_to_end(live_engine_client):
    import time as _time

    c = live_engine_client
    r = c.post(
        "/api/nodes",
        json={
            "role": "head", "name": "spark-01", "lan_ip": "192.168.1.160",
            "qsfp_ip": "10.10.10.1", "qsfp_iface": "enp1s0f1np1",
            "ssh_user": "user", "ssh_password": "pw", "sudo_mode": "nopasswd",
        },
    )
    assert r.status_code == 201

    # manager tick (<=5s) discovers the node, then two fast ticks give rates
    node = None
    deadline = _time.time() + 15
    while _time.time() < deadline:
        nodes = c.get("/api/status").json()["nodes"]
        if nodes and nodes[0]["reachable"] and nodes[0].get("cpu_pct") is not None:
            node = nodes[0]
            break
        _time.sleep(0.5)
    assert node, "engine never produced a second (rate-bearing) sample"

    assert node["gpus"][0]["util_pct"] == 37
    assert node["cpu_pct"] == pytest.approx(50.0, abs=0.5)  # +1000 busy of +2000
    kinds = {n["kind"]: n for n in node["net"]}
    assert kinds["qsfp"]["rx_bps"] == pytest.approx(10_000_000, rel=0.5)
    assert node["disk"]["total_bytes"] == 3844529104 * 1024
    assert node["ray_container_up"] is True  # spark-ray-head in docker names
    assert node["uptime_seconds"] > 0

    hist = c.get("/api/status/history?minutes=5").json()
    assert hist and hist[0]["name"] == "spark-01"
    assert len(hist[0]["points"]) >= 2
    assert any(p["qsfp_rx_bps"] for p in hist[0]["points"])
