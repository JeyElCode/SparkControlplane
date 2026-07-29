"""Instance start must not claim `running` before the model is actually up.

This is the regression test for #45: the old code committed
``status = INST_RUNNING`` immediately after installing the systemd unit — long
before vLLM had loaded the weights — so the /v1 gateway would route external
traffic to an instance that might never come up, and the scheduler considered
the start "reached" and never retried.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
async def db_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    import app.config as config

    config.get_settings.cache_clear()
    import app.db as db

    importlib.reload(db)
    await db.init_db()
    yield db
    config.get_settings.cache_clear()


class FakeHandle:
    """Stands in for JobHandle."""

    job_id = 1

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    async def log(self, text: str, stream: str = "info") -> None:
        self.lines.append((stream, str(text)))

    async def set_progress(self, progress) -> None:
        pass

    def ssh_log_cb(self):
        return None


async def _seed(db):
    from app.models import Instance, ModelRegistry, Node

    async with db.SessionLocal() as s:
        # init_db() already seeded the ClusterConfig / Setting singletons.
        node = Node(role="head", name="dgx-md-01", lan_ip="127.0.0.1",
                    qsfp_ip="10.10.10.1", ssh_user="spark")
        s.add(node)
        model = ModelRegistry(repo_id="poolside/Laguna", name="laguna", status="present")
        s.add(model)
        await s.flush()
        inst = Instance(name="lag", model_id=model.id, topology="single",
                        node_id=node.id, port=8000, status="stopped")
        s.add(inst)
        await s.commit()
        return inst.id


def _patch_ssh(monkeypatch, on_health_wait):
    """Neutralize everything that touches a real node."""
    from app.services import instances as svc
    from app.services import nodeops

    class FakeSSH:
        async def run(self, *a, **kw):
            class R:
                exit_status, stdout, stderr, ok = 0, "", "", True

            return R()

    async def noop(*a, **kw):
        return None

    monkeypatch.setattr(svc, "ssh_for_node", lambda *a, **kw: _async(FakeSSH()))
    monkeypatch.setattr(svc, "_ensure_model_present", noop)
    monkeypatch.setattr(svc, "_deploy_tls_proxy", noop)
    monkeypatch.setattr(svc, "_endpoint_host", lambda *a, **kw: _async("127.0.0.1"))
    monkeypatch.setattr(nodeops, "install_systemd_unit", noop)
    monkeypatch.setattr(nodeops, "install_file", noop)
    monkeypatch.setattr(nodeops, "systemctl", noop)
    monkeypatch.setattr(svc, "_stream_startup_logs", on_health_wait)


def _async(value):
    async def run():
        return value

    return run()


async def _status(db, instance_id: int) -> str:
    from app.models import Instance

    async with db.SessionLocal() as s:
        inst = await s.get(Instance, instance_id)
        return inst.status


async def test_status_is_starting_while_the_model_loads(db_env, monkeypatch):
    """The exact regression: at the moment the health wait begins — unit
    installed, weights not loaded — the row must NOT say running."""
    db = db_env
    instance_id = await _seed(db)
    seen: dict = {}

    async def on_health_wait(handle, ssh, unit, url, verify=True):
        seen["status_during_load"] = await _status(db, instance_id)
        return True  # eventually comes up

    _patch_ssh(monkeypatch, on_health_wait)
    from app.services import instances as svc

    async with db.SessionLocal() as s:
        await svc.start_instance(s, FakeHandle(), instance_id)

    assert seen["status_during_load"] == "starting", (
        "start committed 'running' before /health was confirmed — the gateway "
        "would route traffic to a model that has not loaded yet"
    )
    # ...and once health comes back green, it is promoted with evidence.
    from app.models import Instance

    async with db.SessionLocal() as s:
        inst = await s.get(Instance, instance_id)
        assert inst.status == "running"
        assert inst.last_healthy_at is not None
        assert inst.last_load_seconds is not None and inst.last_load_seconds >= 0


async def test_health_never_green_leaves_it_starting_not_running(db_env, monkeypatch):
    """A load that outruns the wait window stays `starting` for the observer to
    judge — it must never be reported as serving."""
    db = db_env
    instance_id = await _seed(db)

    async def never_healthy(handle, ssh, unit, url, verify=True):
        return False

    _patch_ssh(monkeypatch, never_healthy)
    from app.services import instances as svc

    async with db.SessionLocal() as s:
        await svc.start_instance(s, FakeHandle(), instance_id)

    from app.models import Instance

    async with db.SessionLocal() as s:
        inst = await s.get(Instance, instance_id)
        assert inst.status == "starting"
        assert inst.last_healthy_at is None
        assert inst.started_at is not None, "the deadline anchor must be durable"


async def test_in_flight_registry_is_released(db_env, monkeypatch):
    """The observer must not be locked out of an instance forever if a start
    job raises."""
    db = db_env
    instance_id = await _seed(db)
    from app.services import inst_state

    inst_state.clear_all()

    async def explode(handle, ssh, unit, url, verify=True):
        raise RuntimeError("node fell over mid-start")

    _patch_ssh(monkeypatch, explode)
    from app.services import instances as svc

    with pytest.raises(RuntimeError):
        async with db.SessionLocal() as s:
            await svc.start_instance(s, FakeHandle(), instance_id)

    assert inst_state.in_flight(instance_id) is None
    assert await _status(db, instance_id) == "error"
