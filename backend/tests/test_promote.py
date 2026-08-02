"""Promoting an endpoint to a different instance, and rolling it back.

These cover the code most likely to be wrong and least likely to be exercised:
the failure paths. A promotion stops production before it starts the
replacement — that ordering is forced by the hardware, since prod-class
instances are TP=2 across the whole box — so every way the second half can fail
leaves the endpoint serving nothing until something puts it back.

`start_instance` / `stop_instance` are stubbed throughout. They SSH to a node
and drive systemd; what is under test here is the decision-making around them —
what is refused before anything stops, what is restored when the target does
not come up, and when the pointer is allowed to move.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
async def env(tmp_path, monkeypatch):
    """A database with an endpoint, two member instances, and a stubbed
    start/stop layer that records what was called."""
    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    import app.config as config

    config.get_settings.cache_clear()
    import app.db as db

    importlib.reload(db)
    await db.init_db()

    import app.services.endpoints as ep_svc
    import app.services.instances as inst_svc

    importlib.reload(ep_svc)
    monkeypatch.setattr("app.db.SessionLocal", db.SessionLocal, raising=False)
    monkeypatch.setattr(ep_svc, "SessionLocal", db.SessionLocal, raising=False)

    from app.models import (
        INST_RUNNING,
        INST_STOPPED,
        Endpoint,
        EndpointAlias,
        Instance,
        Job,
        ModelRegistry,
    )

    async with db.SessionLocal() as s:
        # endpoint_promotions.job_id is a real FK; in production jobs.start has
        # already created the row before promote() runs.
        job = Job(type="endpoint.promote", title="test", status="running")
        s.add(job)
        await s.flush()
        m = ModelRegistry(repo_id="org/dsv4", name="dsv4")
        s.add(m)
        await s.flush()
        ep = Endpoint(
            name="prod", hostname="llm.example.net", port=443,
            tls_cert_enc="enc", tls_fingerprint_sha256="AA:BB",
        )
        s.add(ep)
        await s.flush()
        s.add(EndpointAlias(endpoint_id=ep.id, alias="DSV4-DSpark", position=0))
        old = Instance(name="id7", model_id=m.id, topology="single", port=8000,
                       status=INST_RUNNING, endpoint_id=ep.id)
        new = Instance(name="id9", model_id=m.id, topology="single", port=8001,
                       status=INST_STOPPED, endpoint_id=ep.id)
        s.add_all([old, new])
        await s.flush()
        ep.current_instance_id = old.id
        await s.commit()
        ids = {"endpoint": ep.id, "old": old.id, "new": new.id, "model": m.id,
               "job": job.id}

    calls: list[tuple[str, int]] = []
    fail: dict[str, set[int]] = {"start": set()}

    async def fake_start(session, handle, instance_id):
        calls.append(("start", instance_id))
        if instance_id in fail["start"]:
            raise RuntimeError("weights failed to load")
        inst = await session.get(Instance, instance_id)
        inst.status = INST_RUNNING
        await session.commit()
        return "started"

    async def fake_stop(session, handle, instance_id):
        calls.append(("stop", instance_id))
        inst = await session.get(Instance, instance_id)
        inst.status = INST_STOPPED
        await session.commit()
        return "stopped"

    monkeypatch.setattr(inst_svc, "start_instance", fake_start)
    monkeypatch.setattr(inst_svc, "stop_instance", fake_stop)

    yield {"db": db, "ep": ep_svc, "ids": ids, "calls": calls, "fail": fail}
    config.get_settings.cache_clear()


class Handle:
    """Minimal JobHandle: promote only logs and reads job_id."""

    def __init__(self, job_id: int = 1):
        self.job_id = job_id
        self.lines: list[str] = []

    async def log(self, text, stream="info"):
        self.lines.append(f"[{stream}] {text}")

    async def set_progress(self, value):
        pass

    def text(self) -> str:
        return "\n".join(self.lines)


# --- the happy path -------------------------------------------------------

async def test_promote_stops_the_old_starts_the_new_and_flips_the_pointer(env):
    h = Handle(env['ids']['job'])
    await env["ep"].promote(h, env["ids"]["endpoint"], env["ids"]["new"])

    assert env["calls"] == [("stop", env["ids"]["old"]), ("start", env["ids"]["new"])]

    from app.models import Endpoint

    async with env["db"].SessionLocal() as s:
        ep = await s.get(Endpoint, env["ids"]["endpoint"])
        assert ep.current_instance_id == env["ids"]["new"]
        assert ep.promoted_at is not None


async def test_the_operator_is_told_the_endpoint_goes_down(env):
    """The gap is minutes long and unavoidable. Saying so beats letting them
    discover it from traffic."""
    h = Handle(env['ids']['job'])
    await env["ep"].promote(h, env["ids"]["endpoint"], env["ids"]["new"])
    assert "unavailable until the new instance finishes loading" in h.text()
    assert "NOT deleted" in h.text()


async def test_history_records_the_promotion_as_active(env):
    from sqlalchemy import select

    from app.models import PROMO_ACTIVE, EndpointPromotion

    await env["ep"].promote(Handle(env['ids']['job']), env["ids"]["endpoint"], env["ids"]["new"], "eval passed")

    async with env["db"].SessionLocal() as s:
        row = (await s.execute(select(EndpointPromotion))).scalars().one()
        assert row.status == PROMO_ACTIVE
        assert (row.to_instance_name, row.from_instance_name) == ("id9", "id7")
        assert row.reason == "eval passed"
        assert row.cert_fingerprint == "AA:BB"
        assert row.finished_at is not None


async def test_a_second_promotion_supersedes_the_first(env):
    from sqlalchemy import select

    from app.models import PROMO_ACTIVE, PROMO_SUPERSEDED, EndpointPromotion

    await env["ep"].promote(Handle(env['ids']['job']), env["ids"]["endpoint"], env["ids"]["new"])
    await env["ep"].promote(Handle(env['ids']['job']), env["ids"]["endpoint"], env["ids"]["old"])

    async with env["db"].SessionLocal() as s:
        rows = (
            await s.execute(select(EndpointPromotion).order_by(EndpointPromotion.id))
        ).scalars().all()
        assert [r.status for r in rows] == [PROMO_SUPERSEDED, PROMO_ACTIVE]
        # Exactly one active row, or "what serves prod" has two answers.
        assert sum(r.status == PROMO_ACTIVE for r in rows) == 1


# --- refused BEFORE anything stops ---------------------------------------

async def test_a_non_member_is_refused_without_touching_production(env):
    """Each refusal exists because the alternative is discovering it with
    production already stopped."""
    from app.models import Instance

    async with env["db"].SessionLocal() as s:
        stranger = Instance(name="outsider", model_id=env["ids"]["model"],
                            topology="single", port=8002, status="stopped")
        s.add(stranger)
        await s.commit()
        stranger_id = stranger.id

    with pytest.raises(RuntimeError, match="not a member"):
        await env["ep"].promote(Handle(env['ids']['job']), env["ids"]["endpoint"], stranger_id)
    assert env["calls"] == [], "production was disturbed by a promotion that could not proceed"


async def test_a_busy_instance_is_refused_without_touching_production(env):
    from app.services import inst_state

    inst_state.register(env["ids"]["new"], "start", 99)
    try:
        with pytest.raises(RuntimeError, match="busy"):
            await env["ep"].promote(Handle(env['ids']['job']), env["ids"]["endpoint"], env["ids"]["new"])
    finally:
        inst_state.release(env["ids"]["new"])
    assert env["calls"] == []


async def test_an_endpoint_without_a_certificate_cannot_serve_443(env):
    from app.models import Endpoint

    async with env["db"].SessionLocal() as s:
        ep = await s.get(Endpoint, env["ids"]["endpoint"])
        ep.tls_cert_enc = None
        await s.commit()

    with pytest.raises(RuntimeError, match="no certificate"):
        await env["ep"].promote(Handle(env['ids']['job']), env["ids"]["endpoint"], env["ids"]["new"])
    assert env["calls"] == []


async def test_promoting_the_current_holder_is_a_no_op(env):
    """Not an error — it is what a retry looks like. Restarting production to
    reach the state it is already in would be worse than doing nothing."""
    msg = await env["ep"].promote(Handle(env['ids']['job']), env["ids"]["endpoint"], env["ids"]["old"])
    assert "already serves" in msg
    assert env["calls"] == []


# --- the failure paths, which are the point of this file -----------------

async def test_a_failed_target_restores_the_previous_instance(env):
    """Production is down at this moment and that is the only thing that
    matters. The previous instance goes back up before the error is raised."""
    env["fail"]["start"].add(env["ids"]["new"])

    with pytest.raises(RuntimeError, match="has been restarted"):
        await env["ep"].promote(Handle(env['ids']['job']), env["ids"]["endpoint"], env["ids"]["new"])

    assert env["calls"] == [
        ("stop", env["ids"]["old"]),
        ("start", env["ids"]["new"]),   # failed
        ("start", env["ids"]["old"]),   # restored
    ]

    from app.models import INST_RUNNING, Endpoint, Instance

    async with env["db"].SessionLocal() as s:
        assert (await s.get(Instance, env["ids"]["old"])).status == INST_RUNNING
        ep = await s.get(Endpoint, env["ids"]["endpoint"])
        assert ep.current_instance_id == env["ids"]["old"], "the pointer moved despite failure"


async def test_a_failed_promotion_is_recorded_as_failed(env):
    from sqlalchemy import select

    from app.models import PROMO_FAILED, EndpointPromotion

    env["fail"]["start"].add(env["ids"]["new"])
    with pytest.raises(RuntimeError):
        await env["ep"].promote(Handle(env['ids']['job']), env["ids"]["endpoint"], env["ids"]["new"])

    async with env["db"].SessionLocal() as s:
        row = (await s.execute(select(EndpointPromotion))).scalars().one()
        assert row.status == PROMO_FAILED
        assert row.finished_at is not None


async def test_when_the_restore_also_fails_the_message_says_the_endpoint_is_down(env):
    """The worst case, and the one an operator must not have to infer. Both
    instances are down; the error names that plainly and says what to do."""
    env["fail"]["start"].update({env["ids"]["new"], env["ids"]["old"]})

    with pytest.raises(RuntimeError) as exc:
        await env["ep"].promote(Handle(env['ids']['job']), env["ids"]["endpoint"], env["ids"]["new"])

    message = str(exc.value)
    assert "IS DOWN" in message
    assert "start an instance manually" in message


async def test_the_pointer_only_moves_after_the_target_is_up(env):
    """Flipping first would make the API claim a handoff that had not
    happened — and the gateway would route to an instance that is not there."""
    from app.models import Endpoint

    seen: list[int | None] = []

    async def watching_start(session, handle, instance_id):
        ep = await session.get(Endpoint, env["ids"]["endpoint"])
        seen.append(ep.current_instance_id)
        from app.models import INST_RUNNING, Instance

        inst = await session.get(Instance, instance_id)
        inst.status = INST_RUNNING
        await session.commit()
        return "started"

    import app.services.instances as inst_svc

    original = inst_svc.start_instance
    inst_svc.start_instance = watching_start
    try:
        await env["ep"].promote(Handle(env['ids']['job']), env["ids"]["endpoint"], env["ids"]["new"])
    finally:
        inst_svc.start_instance = original

    assert seen == [env["ids"]["old"]], "the pointer had already moved when the target started"


# --- rollback -------------------------------------------------------------

async def test_previous_holder_finds_what_served_before(env):
    await env["ep"].promote(Handle(env['ids']['job']), env["ids"]["endpoint"], env["ids"]["new"])

    async with env["db"].SessionLocal() as s:
        prev = await env["ep"].previous_holder(s, env["ids"]["endpoint"])
    assert prev == env["ids"]["old"]


async def test_rollback_is_a_promote_aimed_backwards(env):
    from app.models import Endpoint

    await env["ep"].promote(Handle(env['ids']['job']), env["ids"]["endpoint"], env["ids"]["new"])
    env["calls"].clear()

    async with env["db"].SessionLocal() as s:
        prev = await env["ep"].previous_holder(s, env["ids"]["endpoint"])
    await env["ep"].promote(Handle(env['ids']['job']), env["ids"]["endpoint"], prev, "rollback")

    assert env["calls"] == [("stop", env["ids"]["new"]), ("start", env["ids"]["old"])]
    async with env["db"].SessionLocal() as s:
        ep = await s.get(Endpoint, env["ids"]["endpoint"])
        assert ep.current_instance_id == env["ids"]["old"]


async def test_nothing_to_roll_back_to_on_a_fresh_endpoint(env):
    async with env["db"].SessionLocal() as s:
        assert await env["ep"].previous_holder(s, env["ids"]["endpoint"]) is None


async def test_the_outgoing_instance_is_only_stopped_never_deleted(env):
    """The premise the whole rollback story rests on."""
    from app.models import INST_STOPPED, Instance

    await env["ep"].promote(Handle(env['ids']['job']), env["ids"]["endpoint"], env["ids"]["new"])

    async with env["db"].SessionLocal() as s:
        old = await s.get(Instance, env["ids"]["old"])
        assert old is not None, "the outgoing instance was deleted"
        assert old.status == INST_STOPPED
        assert old.endpoint_id == env["ids"]["endpoint"], "it must remain a member"
