"""Usage history: window deltas, collector rollups, retention, aggregation API."""

from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.services.usage import K_GEN, K_PROMPT, K_REQ, K_TTFT_CNT, K_TTFT_SUM, compute_window


def test_compute_window_deltas():
    prev = {K_GEN: 1000, K_PROMPT: 200, K_REQ: 10, K_TTFT_SUM: 2.0, K_TTFT_CNT: 10}
    cur = {K_GEN: 7000, K_PROMPT: 1200, K_REQ: 30, K_TTFT_SUM: 8.0, K_TTFT_CNT: 30}
    w = compute_window(cur, prev)
    assert w["gen_tokens"] == 6000 and w["prompt_tokens"] == 1000 and w["requests"] == 20
    assert w["ttft_ms_avg"] == pytest.approx(300.0)  # (8-2)s over 20 reqs


def test_compute_window_counter_reset_counts_from_zero():
    prev = {K_GEN: 90_000, K_REQ: 500, K_TTFT_SUM: 100.0, K_TTFT_CNT: 500}
    cur = {K_GEN: 1200, K_REQ: 4, K_TTFT_SUM: 1.0, K_TTFT_CNT: 4}  # restarted
    w = compute_window(cur, prev)
    assert w["gen_tokens"] == 1200 and w["requests"] == 4
    assert w["ttft_ms_avg"] == pytest.approx(250.0)


def test_compute_window_idle():
    prev = {K_GEN: 500, K_REQ: 5}
    w = compute_window(dict(prev), prev)
    assert w["gen_tokens"] == 0 and w["requests"] == 0 and w["ttft_ms_avg"] is None


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


async def _seed_instance(name: str = "orn") -> int:
    import app.db as db
    from app.models import Instance, ModelRegistry

    async with db.SessionLocal() as s:
        model = ModelRegistry(repo_id=f"o/{name}", name=f"model-{name}", status="present")
        s.add(model)
        await s.flush()
        inst = Instance(name=name, model_id=model.id, topology="single", status="running")
        s.add(inst)
        await s.commit()
        return inst.id


def test_rollup_and_api_aggregation(client):
    import asyncio

    from app.services.telemetry import engine as telemetry
    from app.services.usage import UsageCollector

    iid = asyncio.run(_seed_instance())
    col = UsageCollector()
    try:
        telemetry._inst_counters[iid] = {K_GEN: 1000, K_PROMPT: 100, K_REQ: 5,
                                         K_TTFT_SUM: 1.0, K_TTFT_CNT: 5, "_ts": 1.0}
        assert asyncio.run(col.rollup()) == 0  # baseline window seeds only
        telemetry._inst_counters[iid] = {K_GEN: 5000, K_PROMPT: 600, K_REQ: 25,
                                         K_TTFT_SUM: 6.0, K_TTFT_CNT: 25, "_ts": 2.0}
        assert asyncio.run(col.rollup()) == 1
        # idle window writes nothing
        assert asyncio.run(col.rollup()) == 0

        data = client.get("/api/usage?days=7&bucket=day").json()
        assert len(data) == 1
        m = data[0]
        assert m["model_name"] == "model-orn"
        assert m["total_gen_tokens"] == 4000
        assert m["total_prompt_tokens"] == 500
        assert m["total_requests"] == 20
        assert len(m["points"]) == 1
        assert m["points"][0]["ttft_ms_avg"] == pytest.approx(250.0)
    finally:
        telemetry._inst_counters.pop(iid, None)


def test_retention_purges_old_rows(client):
    import asyncio

    import app.db as db
    from app.models import UsageSample
    from app.services.usage import UsageCollector

    async def seed_old():
        async with db.SessionLocal() as s:
            s.add(UsageSample(
                ts=datetime.now(timezone.utc) - timedelta(days=400),
                instance_name="old", model_name="old-model", gen_tokens=1,
            ))
            await s.commit()

    asyncio.run(seed_old())
    asyncio.run(UsageCollector().rollup())  # purge runs inside rollup
    from sqlalchemy import func, select

    async def count():
        async with db.SessionLocal() as s:
            return (await s.execute(select(func.count(UsageSample.id)))).scalar()

    assert asyncio.run(count()) == 0
