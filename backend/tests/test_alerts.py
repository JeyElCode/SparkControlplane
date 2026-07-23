"""Alert engine: sustain/fire/resolve state machine, config merging, webhook
payload shapes, and the API endpoints."""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from app.services.alerts import (
    DEFAULTS,
    AlertManager,
    Fact,
    build_webhook_request,
    merged_config,
)


def _fact(active: bool, rule="node_offline", subject="spark-02", sustain=60.0) -> Fact:
    return Fact(rule=rule, subject=subject, active=active, sustain=sustain,
                severity="crit", message=f"{subject} is down")


def test_fires_only_after_sustain_and_resolves_on_recovery():
    m = AlertManager()
    assert m.evaluate([_fact(True)], now=100.0) == []          # just observed
    assert m.evaluate([_fact(True)], now=130.0) == []          # 30s < 60s sustain
    t = m.evaluate([_fact(True)], now=161.0)                    # sustained
    assert len(t) == 1 and t[0].event == "fired"
    assert m.evaluate([_fact(True)], now=200.0) == []          # no re-fire while active
    assert [a["subject"] for a in m.active()] == ["spark-02"]

    t = m.evaluate([_fact(False)], now=210.0)
    assert len(t) == 1 and t[0].event == "resolved"
    assert m.active() == []
    # a new outage needs a fresh sustain window
    assert m.evaluate([_fact(True)], now=220.0) == []


def test_blip_shorter_than_sustain_never_fires():
    m = AlertManager()
    m.evaluate([_fact(True)], now=100.0)
    assert m.evaluate([_fact(False)], now=120.0) == []  # recovered before sustain
    assert m.evaluate([_fact(True)], now=130.0) == []   # counter restarted
    assert m.evaluate([_fact(True)], now=180.0) == []   # 50s < 60s


def test_vanished_subject_clears_state():
    m = AlertManager()
    m.evaluate([_fact(True)], now=0.0)
    m.evaluate([_fact(True)], now=61.0)
    assert m.active()
    m.evaluate([], now=70.0)  # node deleted — no fact for it anymore
    assert m.active() == []


def test_merged_config_ignores_junk():
    assert merged_config(None) == DEFAULTS
    cfg = merged_config('{"gpu_temp_c": 90, "nonsense": 1, "webhook_kind": "ntfy"}')
    assert cfg["gpu_temp_c"] == 90 and cfg["webhook_kind"] == "ntfy"
    assert "nonsense" not in cfg
    assert merged_config("not json") == DEFAULTS


def test_webhook_payload_shapes():
    f = _fact(True)
    body, text, headers = build_webhook_request("ntfy", "fired", f)
    assert body is None and "spark-02 is down" in text and headers["Priority"] == "high"
    body, _, _ = build_webhook_request("discord", "fired", f)
    assert "content" in body
    body, _, _ = build_webhook_request("slack", "resolved", f)
    assert "✅" in body["text"]
    body, _, _ = build_webhook_request("generic", "fired", f)
    assert body["rule"] == "node_offline" and body["event"] == "fired"


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


def test_alert_api_and_settings_roundtrip(client):
    assert client.get("/api/alerts").json() == []
    assert client.get("/api/alerts/active").json() == []
    # test webhook without a URL -> 400
    assert client.post("/api/alerts/test").status_code == 400

    r = client.patch("/api/cluster/settings", json={
        "alerts": {"gpu_temp_c": 92, "webhook_kind": "ntfy"},
        "alert_webhook_url": "https://ntfy.sh/my-topic",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["alerts"]["gpu_temp_c"] == 92
    assert body["alerts"]["webhook_kind"] == "ntfy"
    assert body["alerts"]["kv_cache_pct"] == DEFAULTS["kv_cache_pct"]  # defaults kept
    assert body["has_alert_webhook"] is True

    # unknown key rejected; clearing the webhook works
    assert client.patch("/api/cluster/settings", json={"alerts": {"bogus": 1}}).status_code == 422
    r = client.patch("/api/cluster/settings", json={"alert_webhook_url": ""})
    assert r.json()["has_alert_webhook"] is False
