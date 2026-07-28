"""Status reconciliation: telling a slow model load apart from a dead one.

These are table tests over the pure decision function — no DB, no clock, `now`
passed explicitly (the style of test_alerts.py). The hard requirement they pin
down is asymmetric: a legitimate multi-minute load must NEVER be demoted, while
a genuinely dead instance must not stay `running` (the gateway routes on that
field, so a false positive there means external clients hit a dead upstream).
"""

from __future__ import annotations

import importlib

import pytest

from app.models import INST_ERROR, INST_RUNNING, INST_STARTING, INST_STOPPED
from app.services.reconcile import Observation, _Pending, decide
from app.services.telemetry import NodeSample as _Sample
from app.services.telemetry import SlowCache as _SlowCache

DEADLINE = 1800.0
UNHEALTHY = 120.0
UNIT_DEAD = 45.0
CRASHLOOP = 3


def _decide(obs, pending, now):
    return decide(
        obs,
        pending,
        now,
        start_deadline=DEADLINE,
        unhealthy_after=UNHEALTHY,
        unit_dead_after=UNIT_DEAD,
        crashloop_restarts=CRASHLOOP,
    )


def _obs(**kw):
    base = dict(
        instance_id=1,
        name="laguna",
        status=INST_STARTING,
        systemd_active=True,
        health_ok=False,
        n_restarts=0,
        node_reachable=True,
        started_at=1000.0,
        last_healthy_at=None,
    )
    base.update(kw)
    return Observation(**base)


# --- the load window must be protected ------------------------------------
def test_loading_model_is_left_alone():
    """Ten minutes into a big FP8 load: unit up, health not answering yet."""
    assert _decide(_obs(), _Pending(), now=1600.0) is None


def test_loading_is_left_alone_right_up_to_the_deadline():
    assert _decide(_obs(), _Pending(), now=1000.0 + DEADLINE - 1) is None


def test_never_healthy_past_the_deadline_becomes_error():
    v = _decide(_obs(), _Pending(), now=1000.0 + DEADLINE + 1)
    assert v is not None and v.to_status == INST_ERROR
    assert "never became healthy" in v.reason


# --- promotion -------------------------------------------------------------
def test_health_green_promotes_a_starting_instance():
    v = _decide(_obs(health_ok=True), _Pending(), now=1100.0)
    assert v is not None
    assert (v.from_status, v.to_status) == (INST_STARTING, INST_RUNNING)
    assert v.healthy_now is True


def test_health_green_wins_over_a_failed_systemd_probe():
    """A 200 from /health proves the model is servable; a flaky `systemctl`
    must not discard that."""
    v = _decide(_obs(health_ok=True, systemd_active=None), _Pending(), now=1100.0)
    assert v is not None and v.to_status == INST_RUNNING


def test_running_and_healthy_is_a_no_op():
    assert _decide(
        _obs(status=INST_RUNNING, health_ok=True, last_healthy_at=1500.0),
        _Pending(), now=1600.0,
    ) is None


# --- crash loop (vLLM OOM at load) ----------------------------------------
def test_crash_loop_is_caught_long_before_the_deadline():
    """With Restart=on-failure the unit reads `active` between restarts, so
    ActiveState alone would call an OOM 'still loading' for 30 minutes."""
    pending = _Pending()
    assert _decide(_obs(n_restarts=0), pending, now=1010.0) is None
    assert _decide(_obs(n_restarts=1), pending, now=1020.0) is None
    v = _decide(_obs(n_restarts=3), pending, now=1040.0)
    assert v is not None and v.to_status == INST_ERROR
    assert "crash loop" in v.reason and "memory" in v.reason


def test_restarts_before_this_start_do_not_count():
    """NRestarts is cumulative for the unit's lifetime; only the delta since we
    began watching indicates a loop now."""
    pending = _Pending()
    assert _decide(_obs(n_restarts=7), pending, now=1010.0) is None
    assert _decide(_obs(n_restarts=7), pending, now=1600.0) is None


def test_restarts_after_serving_are_not_a_crash_loop():
    """An instance that has served is allowed to restart (e.g. an operator
    bounced the unit) without being condemned on restart count alone."""
    pending = _Pending()
    assert _decide(
        _obs(status=INST_RUNNING, last_healthy_at=1200.0, n_restarts=9),
        pending, now=1250.0,
    ) is None


# --- dead unit -------------------------------------------------------------
def test_dead_unit_must_be_sustained():
    pending = _Pending()
    assert _decide(_obs(systemd_active=False), pending, now=1000.0) is None
    assert _decide(_obs(systemd_active=False), pending, now=1000.0 + UNIT_DEAD - 5) is None
    v = _decide(_obs(systemd_active=False), pending, now=1000.0 + UNIT_DEAD + 1)
    assert v is not None and v.to_status == INST_ERROR
    assert "systemd unit" in v.reason


def test_unit_recovering_clears_the_dead_timer():
    pending = _Pending()
    _decide(_obs(systemd_active=False), pending, now=1000.0)
    _decide(_obs(systemd_active=True), pending, now=1020.0)  # came back
    assert _decide(_obs(systemd_active=False), pending, now=1050.0) is None


def test_unknown_systemd_state_is_not_a_dead_unit():
    """systemd_active None means the probe failed, not that the unit is down."""
    pending = _Pending()
    for t in range(0, 300, 30):
        assert _decide(_obs(systemd_active=None), pending, now=1000.0 + t) is None


# --- losing health after serving ------------------------------------------
def test_running_instance_that_stops_answering_is_demoted_after_the_grace():
    pending = _Pending()
    obs = _obs(status=INST_RUNNING, health_ok=False, last_healthy_at=900.0)
    assert _decide(obs, pending, now=1000.0) is None
    assert _decide(obs, pending, now=1000.0 + UNHEALTHY - 10) is None
    v = _decide(obs, pending, now=1000.0 + UNHEALTHY + 1)
    assert v is not None and v.to_status == INST_ERROR
    assert "stopped responding" in v.reason


def test_a_single_missed_scrape_does_not_flap():
    pending = _Pending()
    obs_bad = _obs(status=INST_RUNNING, health_ok=False, last_healthy_at=900.0)
    obs_good = _obs(status=INST_RUNNING, health_ok=True, last_healthy_at=900.0)
    assert _decide(obs_bad, pending, now=1000.0) is None
    assert _decide(obs_good, pending, now=1010.0) is None  # recovered
    assert _decide(obs_bad, pending, now=1100.0) is None  # timer was reset


# --- never demote on our own blindness ------------------------------------
def test_unreachable_node_never_demotes():
    """If we cannot reach the node, the broken thing might be our own network.
    Demoting here would make the gateway 503 a perfectly healthy model."""
    pending = _Pending()
    obs = _obs(status=INST_RUNNING, node_reachable=False, systemd_active=None,
               health_ok=False, last_healthy_at=900.0)
    for t in range(0, 3600, 120):
        assert _decide(obs, pending, now=1000.0 + t) is None


def test_unreachable_node_clears_sustain_timers():
    """Time spent blind must not count toward a demotion once we can see again."""
    pending = _Pending()
    obs_dead = _obs(status=INST_RUNNING, systemd_active=False, last_healthy_at=900.0)
    _decide(obs_dead, pending, now=1000.0)
    _decide(_obs(status=INST_RUNNING, node_reachable=False, last_healthy_at=900.0),
            pending, now=1010.0)
    assert _decide(obs_dead, pending, now=1030.0) is None


# --- states the observer must not touch -----------------------------------
def test_stopped_instance_is_not_the_observers_business():
    assert _decide(_obs(status=INST_STOPPED, systemd_active=False), _Pending(),
                   now=99999.0) is None


def test_error_instance_is_not_re_demoted():
    assert _decide(_obs(status=INST_ERROR, systemd_active=False), _Pending(),
                   now=99999.0) is None


def test_error_instance_recovers_when_health_returns():
    """If the operator fixes it and the unit comes up, say so rather than
    making them click Start to clear a stale error."""
    v = _decide(_obs(status=INST_ERROR, health_ok=True), _Pending(), now=2000.0)
    assert v is not None and v.to_status == INST_RUNNING


# --- missing anchor (upgraded rows) ---------------------------------------
def test_row_without_started_at_uses_first_observation_as_the_anchor():
    """Rows that predate the upgrade have no started_at. They must not be
    demoted instantly on the first tick after an upgrade."""
    pending = _Pending()
    obs = _obs(started_at=None)
    assert _decide(obs, pending, now=5000.0) is None  # first sighting
    assert _decide(obs, pending, now=5000.0 + DEADLINE - 10) is None
    v = _decide(obs, pending, now=5000.0 + DEADLINE + 10)
    assert v is not None and v.to_status == INST_ERROR


# --- the loop, against a real database ------------------------------------
@pytest.fixture()
async def live(tmp_path, monkeypatch):
    """A DB plus a telemetry engine primed with fake probes."""
    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    import app.config as config

    config.get_settings.cache_clear()
    import app.db as db

    importlib.reload(db)
    await db.init_db()
    from app.models import Instance, ModelRegistry, Node
    from app.services import inst_state
    from app.services.telemetry import engine

    inst_state.clear_all()
    async with db.SessionLocal() as s:
        node = Node(role="head", name="h", lan_ip="127.0.0.1", qsfp_ip="10.0.0.1", ssh_user="u")
        s.add(node)
        model = ModelRegistry(repo_id="o/m", name="m", status="present")
        s.add(model)
        await s.flush()
        inst = Instance(name="lag", model_id=model.id, topology="distributed",
                        port=8000, status=INST_STARTING)
        s.add(inst)
        await s.commit()
        ids = (inst.id, node.id)
    engine._samples.clear()
    yield db, ids, engine
    engine._slow = _SlowCache()
    engine._samples.clear()
    inst_state.clear_all()
    config.get_settings.cache_clear()


def _prime(engine, node_id, instance_id, **probe):
    """Put one instance probe into the telemetry slow cache."""
    from app.schemas import InstanceRuntimeStatus

    base = dict(instance_id=instance_id, name="lag", status=INST_STARTING,
                node_id=node_id, systemd_active=True, health_ok=False)
    base.update(probe)
    engine._slow = _SlowCache(ts=1.0, instances=[InstanceRuntimeStatus(**base)])
    engine._samples[node_id] = _Sample(node_id=node_id, ts=1.0, reachable=True)


async def test_tick_promotes_a_healthy_instance(live):
    db, (iid, nid), engine = live
    _prime(engine, nid, iid, health_ok=True)
    from app.models import Instance
    from app.services.reconcile import Reconciler

    applied = await Reconciler().tick()
    assert [v.to_status for v in applied] == [INST_RUNNING]
    async with db.SessionLocal() as s:
        inst = await s.get(Instance, iid)
        assert inst.status == INST_RUNNING
        assert inst.last_healthy_at is not None
        assert inst.last_error is None


async def test_tick_leaves_an_instance_a_job_is_working_on(live):
    """A start/stop job owns the row while it runs; the observer must not race
    its commits."""
    db, (iid, nid), engine = live
    _prime(engine, nid, iid, health_ok=True)
    from app.services import inst_state
    from app.services.reconcile import Reconciler

    inst_state.register(iid, "start", 42)
    assert await Reconciler().tick() == []
    inst_state.release(iid)
    assert len(await Reconciler().tick()) == 1


async def test_a_stopped_instance_is_never_resurrected(live):
    """An instance the operator stopped can keep answering /health for a few
    seconds while it drains. Promoting it would fight the operator and put a
    deliberately-retired model back into gateway rotation."""
    db, (iid, nid), engine = live
    _prime(engine, nid, iid, health_ok=True, status=INST_STARTING)  # stale probe
    from app.models import Instance
    from app.services.reconcile import Reconciler

    async with db.SessionLocal() as s:
        inst = await s.get(Instance, iid)
        inst.status = INST_STOPPED
        await s.commit()

    assert await Reconciler().tick() == []
    async with db.SessionLocal() as s:
        assert (await s.get(Instance, iid)).status == INST_STOPPED


async def test_error_instance_that_recovers_is_promoted(live):
    """The mirror case: a failed instance whose unit came back healthy should
    clear itself rather than making the operator click Start to reset it."""
    db, (iid, nid), engine = live
    from app.models import Instance
    from app.services.reconcile import Reconciler

    async with db.SessionLocal() as s:
        inst = await s.get(Instance, iid)
        inst.status = INST_ERROR
        inst.last_error = "died"
        await s.commit()
    _prime(engine, nid, iid, health_ok=True, status=INST_ERROR)

    assert [v.to_status for v in await Reconciler().tick()] == [INST_RUNNING]
    async with db.SessionLocal() as s:
        inst = await s.get(Instance, iid)
        assert inst.status == INST_RUNNING and inst.last_error is None


async def test_no_probes_is_a_no_op(live):
    """Before the first slow tick lands there is nothing to reconcile — and
    definitely no basis for demoting anything."""
    db, (iid, nid), engine = live
    from app.services.reconcile import Reconciler

    assert await Reconciler().tick() == []
