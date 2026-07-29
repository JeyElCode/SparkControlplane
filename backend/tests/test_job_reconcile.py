"""Jobs must not be stranded 'running' by a portal restart.

A job exists only as an asyncio task in one process. Under GitOps a restart
happens on every image bump and config nudge, and each one used to leave any
in-flight job's row claiming `running` forever — a spinner in the UI that never
resolves, and an `is_running()` (in-memory) that disagreed with the database.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest


@pytest.fixture()
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    import app.config as config

    config.get_settings.cache_clear()
    import app.db as db_mod

    importlib.reload(db_mod)
    await db_mod.init_db()
    yield db_mod
    config.get_settings.cache_clear()


async def _seed(db_mod, statuses: list[str]) -> list[int]:
    from app.models import Job

    ids = []
    async with db_mod.SessionLocal() as s:
        for st in statuses:
            job = Job(type="instance.start", title=f"job-{st}", status=st)
            s.add(job)
            await s.flush()
            ids.append(job.id)
        await s.commit()
    return ids


async def test_orphans_are_failed_with_an_honest_summary(db):
    from app.models import JOB_ERROR, JOB_PENDING, JOB_RUNNING, Job
    from app.services.jobs import JobManager

    running, pending = await _seed(db, [JOB_RUNNING, JOB_PENDING])

    mgr = JobManager()
    assert await mgr.reconcile_orphans() == 2

    async with db.SessionLocal() as s:
        for jid in (running, pending):
            job = await s.get(Job, jid)
            assert job.status == JOB_ERROR
            assert job.finished_at is not None
            # It must not claim the operation failed — the work may well have
            # completed on the node; what failed is the portal's tracking.
            assert "Interrupted by a portal restart" in job.summary
            assert "may have continued" in job.summary


async def test_finished_jobs_are_untouched(db):
    from app.models import JOB_CANCELLED, JOB_ERROR, JOB_SUCCESS, Job
    from app.services.jobs import JobManager

    ids = await _seed(db, [JOB_SUCCESS, JOB_ERROR, JOB_CANCELLED])
    before = []
    async with db.SessionLocal() as s:
        for jid in ids:
            job = await s.get(Job, jid)
            before.append((job.status, job.summary, job.finished_at))

    assert await JobManager().reconcile_orphans() == 0

    async with db.SessionLocal() as s:
        for jid, prev in zip(ids, before):
            job = await s.get(Job, jid)
            assert (job.status, job.summary, job.finished_at) == prev


async def test_a_job_this_process_is_actually_running_is_left_alone(db):
    """The sweep must key on 'no task in THIS process', not on status alone —
    otherwise calling it after startup would kill live work."""
    from app.models import JOB_RUNNING, Job
    from app.services.jobs import JobManager

    (jid,) = await _seed(db, [JOB_RUNNING])
    mgr = JobManager()

    async def forever():
        await asyncio.sleep(60)

    task = asyncio.create_task(forever())
    mgr._tasks[jid] = task
    try:
        assert await mgr.reconcile_orphans() == 0
        async with db.SessionLocal() as s:
            assert (await s.get(Job, jid)).status == JOB_RUNNING
    finally:
        task.cancel()


async def test_sweep_is_idempotent(db):
    from app.models import JOB_RUNNING
    from app.services.jobs import JobManager

    await _seed(db, [JOB_RUNNING])
    mgr = JobManager()
    assert await mgr.reconcile_orphans() == 1
    assert await mgr.reconcile_orphans() == 0


async def test_a_restart_clears_the_spinner(tmp_path, monkeypatch):
    """End to end through the real app: a job left running in the database is
    resolved by the next startup, so the UI stops showing a job that will never
    finish."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    import app.config as config

    config.get_settings.cache_clear()
    import app.db as db_mod

    importlib.reload(db_mod)
    import app.main as main

    importlib.reload(main)

    with TestClient(main.app):
        pass  # first boot creates the schema

    from app.models import JOB_RUNNING

    await _seed(db_mod, [JOB_RUNNING])  # a job orphaned by the restart

    with TestClient(main.app) as c:
        jobs = c.get("/api/jobs").json()
        assert jobs, "expected the orphaned job to still be listed"
        assert all(j["status"] != JOB_RUNNING for j in jobs), (
            "a job stranded by a restart is still shown as running"
        )
    config.get_settings.cache_clear()
