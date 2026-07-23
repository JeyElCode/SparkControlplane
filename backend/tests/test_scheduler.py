"""Instance scheduling: window math, edge-triggered decisions, CRUD API, tick."""

from __future__ import annotations

import importlib
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.models import INST_ERROR, INST_RUNNING, INST_STARTING, INST_STOPPED
from app.services.scheduler import decide, desired_state, parse_days, window_active


def _dt(weekday: int, hhmm: str) -> datetime:
    # 2026-07-20 is a Monday (weekday 0)
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 7, 20 + weekday, h, m)


def test_window_active_same_day():
    days = {0, 1, 2, 3, 4}  # weekdays
    assert window_active(days, "08:00", "17:00", _dt(0, "08:00")) is True
    assert window_active(days, "08:00", "17:00", _dt(0, "16:59")) is True
    assert window_active(days, "08:00", "17:00", _dt(0, "17:00")) is False
    assert window_active(days, "08:00", "17:00", _dt(0, "07:59")) is False
    assert window_active(days, "08:00", "17:00", _dt(5, "12:00")) is False  # saturday


def test_window_active_overnight_wrap():
    days = {4}  # Friday 22:00 -> Saturday 06:00
    assert window_active(days, "22:00", "06:00", _dt(4, "23:30")) is True
    assert window_active(days, "22:00", "06:00", _dt(5, "05:59")) is True   # spillover
    assert window_active(days, "22:00", "06:00", _dt(5, "06:00")) is False
    assert window_active(days, "22:00", "06:00", _dt(5, "23:00")) is False  # sat night not scheduled
    assert window_active(days, "22:00", "06:00", _dt(4, "21:00")) is False


def test_decide_edges_and_manual_override():
    # boot reconcile
    assert decide(None, True, INST_STOPPED) == "start"
    assert decide(None, False, INST_RUNNING) == "stop"
    assert decide(None, True, INST_RUNNING) is None
    # opening edge starts (also from error), closing edge stops (also mid-start)
    assert decide(False, True, INST_STOPPED) == "start"
    assert decide(False, True, INST_ERROR) == "start"
    assert decide(True, False, INST_RUNNING) == "stop"
    assert decide(True, False, INST_STARTING) == "stop"
    # NO edge -> manual override respected
    assert decide(True, True, INST_STOPPED) is None    # user stopped mid-window
    assert decide(False, False, INST_RUNNING) is None  # user started off-window


def test_parse_days_tolerates_junk():
    assert parse_days("0,2, 4") == {0, 2, 4}
    assert parse_days("7,-1,x,3") == {3}


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


def _seed_instance(name="orn", status=INST_STOPPED) -> int:
    import asyncio

    import app.db as db
    from app.models import Instance, ModelRegistry

    async def run():
        async with db.SessionLocal() as s:
            model = ModelRegistry(repo_id=f"o/{name}", name=f"model-{name}", status="present")
            s.add(model)
            await s.flush()
            inst = Instance(name=name, model_id=model.id, topology="single",
                            status=status, gpu_memory_utilization=0.85)
            s.add(inst)
            await s.commit()
            return inst.id

    return asyncio.run(run())


def test_schedule_crud_and_planner_fields(client):
    iid = _seed_instance()
    r = client.post("/api/schedules", json={
        "instance_id": iid, "days": [0, 1, 2, 3, 4],
        "start_time": "08:00", "end_time": "17:00",
    })
    assert r.status_code == 201
    s = r.json()
    assert s["instance_name"] == "orn" and s["model_name"] == "model-orn"
    assert s["days"] == [0, 1, 2, 3, 4]
    assert s["est_gib_per_node"] == pytest.approx(0.85 * 119, abs=0.2)
    assert s["node_scope"] == "all nodes"  # single with no pinned node object

    # validation
    assert client.post("/api/schedules", json={
        "instance_id": iid, "days": [9], "start_time": "08:00", "end_time": "17:00",
    }).status_code == 422
    assert client.post("/api/schedules", json={
        "instance_id": iid, "days": [1], "start_time": "8am", "end_time": "17:00",
    }).status_code == 422
    assert client.post("/api/schedules", json={
        "instance_id": 999, "days": [1], "start_time": "08:00", "end_time": "17:00",
    }).status_code == 404

    sid = s["id"]
    upd = client.patch(f"/api/schedules/{sid}", json={"end_time": "18:30", "enabled": False})
    assert upd.status_code == 200
    assert upd.json()["end_time"] == "18:30" and upd.json()["enabled"] is False

    assert len(client.get("/api/schedules").json()) == 1
    assert client.delete(f"/api/schedules/{sid}").status_code == 204
    assert client.get("/api/schedules").json() == []


def test_scheduler_tick_starts_and_stops(client, monkeypatch):
    """Boot reconcile starts a stopped instance inside its window; disabling the
    window then makes the closing edge stop it."""
    import asyncio

    from app.services import scheduler as sched_mod
    from app.services.scheduler import Scheduler

    iid = _seed_instance(name="tickme")
    client.post("/api/schedules", json={
        "instance_id": iid, "days": [0, 1, 2, 3, 4, 5, 6],
        "start_time": "00:00", "end_time": "23:59",
    })

    # the jobs manager holds a session factory from before the test DB reload —
    # fake it; tick()'s returned action list is what we assert on
    enqueued: list[str] = []

    class FakeJobs:
        async def start(self, type_, title, coro, **kw):
            enqueued.append(type_)
            return len(enqueued)

    monkeypatch.setattr(sched_mod, "jobs", FakeJobs())

    sch = Scheduler()
    actions = asyncio.run(sch.tick())
    assert actions == [(iid, "start")]

    # window closes (disable it) -> desired flips -> stop even though the fake
    # start didn't change the DB status (status field gating is per-action)
    import app.db as db
    from sqlalchemy import update as sa_update

    from app.models import Instance

    async def mark_running():
        async with db.SessionLocal() as s:
            await s.execute(sa_update(Instance).where(Instance.id == iid)
                            .values(status=INST_RUNNING))
            await s.commit()

    asyncio.run(mark_running())
    sched = client.get("/api/schedules").json()[0]
    client.patch(f"/api/schedules/{sched['id']}", json={"enabled": False})
    actions = asyncio.run(sch.tick())
    assert actions == [(iid, "stop")]

    # steady state: nothing more to do
    assert asyncio.run(sch.tick()) == []


def test_failed_action_is_retried_then_gives_up(client, monkeypatch):
    """A scheduled stop whose job fails (status stays running) is retried with
    backoff instead of the edge being lost — and success/override clears it."""
    import asyncio

    from app.services import scheduler as sched_mod
    from app.services.scheduler import Scheduler

    iid = _seed_instance(name="retryme", status=INST_RUNNING)
    client.post("/api/schedules", json={
        "instance_id": iid, "days": [0, 1, 2, 3, 4, 5, 6],
        "start_time": "00:00", "end_time": "00:01",  # effectively always closed
    })

    class FakeJobs:
        async def start(self, *a, **kw):
            return 1

    monkeypatch.setattr(sched_mod, "jobs", FakeJobs())
    fake_now = [1000.0]
    monkeypatch.setattr(sched_mod.time, "time", lambda: fake_now[0])

    sch = Scheduler()
    assert asyncio.run(sch.tick()) == [(iid, "stop")]     # boot reconcile
    assert asyncio.run(sch.tick()) == []                  # within retry window
    fake_now[0] += sched_mod.RETRY_SECONDS + 1
    assert asyncio.run(sch.tick()) == [(iid, "stop")]     # retry (job "failed")
    # instance finally reports stopped -> pending clears, no more actions
    import app.db as db
    from sqlalchemy import update as sa_update

    from app.models import Instance

    async def mark_stopped():
        async with db.SessionLocal() as s:
            await s.execute(sa_update(Instance).where(Instance.id == iid)
                            .values(status=INST_STOPPED))
            await s.commit()

    asyncio.run(mark_stopped())
    fake_now[0] += sched_mod.RETRY_SECONDS + 1
    assert asyncio.run(sch.tick()) == []
    assert iid not in sch._pending
