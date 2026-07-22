"""Prometheus exporter rendering + registry image-ref parsing/sorting."""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from app.services.registry import _natural_key, split_image


def test_split_image():
    assert split_image("nvcr.io/nvidia/vllm:26.05-py3") == ("nvcr.io", "nvidia/vllm", "26.05-py3")
    assert split_image("nvcr.io/nvidia/vllm") == ("nvcr.io", "nvidia/vllm", None)
    assert split_image("vllm/vllm-openai:v0.9.1") == ("registry-1.docker.io", "vllm/vllm-openai", "v0.9.1")
    assert split_image("ubuntu:24.04") == ("registry-1.docker.io", "library/ubuntu", "24.04")
    assert split_image("localhost:5000/foo/bar:dev") == ("localhost:5000", "foo/bar", "dev")
    assert split_image("ghcr.io/jeyelcode/spark-controlplane:v1.9.0") == (
        "ghcr.io", "jeyelcode/spark-controlplane", "v1.9.0"
    )


def test_natural_sort_orders_versions_numerically():
    tags = ["26.05-py3", "26.10-py3", "25.12-py3", "26.9-py3"]
    tags.sort(key=_natural_key, reverse=True)
    assert tags == ["26.10-py3", "26.9-py3", "26.05-py3", "25.12-py3"]


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


def test_metrics_endpoint_serves_prometheus_text(client):
    from app.schemas import InstanceMetrics
    from app.services.telemetry import NodeSample, engine
    from app.schemas import GpuStatus, NetRate

    engine._node_names[1] = "spark-01"
    engine._node_roles[1] = "head"
    engine._samples[1] = NodeSample(
        node_id=1, ts=100.0, reachable=True, cpu_pct=42.5, cpu_count=20,
        mem_used_mib=1024, mem_total_mib=2048,
        gpus=[GpuStatus(index=0, name="GB10", util_pct=55, temp_c=61, power_w=40.0, mem_used_mib=None, mem_total_mib=None)],
        net=[NetRate(iface="enp1", kind="qsfp", rx_bps=1000.0, tx_bps=500.0)],
    )
    engine._inst_names[7] = "ornith"
    engine._inst_metrics[7] = InstanceMetrics(
        ts=100.0, running=3, waiting=1, kv_cache_pct=64.0, gen_tps=1200.0,
        total_generation_tokens=50000.0, total_prompt_tokens=10000.0,
    )
    try:
        r = client.get("/metrics")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/plain")
        body = r.text
        assert 'spark_node_up{node="spark-01",role="head"} 1' in body
        assert 'spark_node_cpu_pct{node="spark-01",role="head"} 42.5' in body
        assert 'spark_gpu_utilization_pct{node="spark-01",role="head",gpu="0"} 55' in body
        assert 'spark_net_rx_bytes_per_second{node="spark-01",role="head",iface="enp1",kind="qsfp"} 1000' in body
        assert 'spark_vllm_generation_tokens_total{instance="ornith"} 50000' in body
        assert "# TYPE spark_vllm_generation_tokens_total counter" in body
        assert 'spark_vllm_kv_cache_pct{instance="ornith"} 64' in body
        # memory exported in bytes
        assert 'spark_node_memory_total_bytes{node="spark-01",role="head"} 2.14748e+09' in body
    finally:
        engine._samples.pop(1, None)
        engine._node_names.pop(1, None)
        engine._node_roles.pop(1, None)
        engine._inst_metrics.pop(7, None)
        engine._inst_names.pop(7, None)
