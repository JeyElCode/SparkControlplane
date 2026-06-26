"""Background job manager.

Each long-running operation (a setup phase, a model download/sync, an instance
start) runs as a tracked :class:`~app.models.Job`. Output lines are persisted as
``JobLog`` rows *and* published to in-memory subscriber queues so the UI can
stream them live over a WebSocket.

The coroutine you pass to :meth:`JobManager.start` receives a :class:`JobHandle`
and may call ``await handle.log(...)`` / ``await handle.set_progress(...)``. It
runs in its own DB session (never the request session).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select

from ..db import SessionLocal
from ..models import (
    JOB_CANCELLED,
    JOB_ERROR,
    JOB_RUNNING,
    JOB_SUCCESS,
    Job,
    JobLog,
)

log = logging.getLogger("spark.jobs")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class JobHandle:
    """Passed to a job coroutine; lets it emit logs and progress."""

    def __init__(self, manager: "JobManager", job_id: int):
        self._mgr = manager
        self.job_id = job_id
        self._seq = 0

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def log(self, text: str, stream: str = "info") -> None:
        for line in str(text).splitlines() or [""]:
            await self._mgr._append_log(self.job_id, stream, line, self._next_seq())

    async def set_progress(self, progress: float | None) -> None:
        await self._mgr._set_progress(self.job_id, progress)

    def ssh_log_cb(self):
        """Return an (stream, line) -> coroutine callback for SSHClient.run."""

        async def cb(stream: str, line: str) -> None:
            await self._mgr._append_log(self.job_id, stream, line, self._next_seq())

        return cb


JobCoro = Callable[[JobHandle], Awaitable[Any]]


class JobManager:
    def __init__(self) -> None:
        self._subs: dict[int, set[asyncio.Queue]] = {}
        self._tasks: dict[int, asyncio.Task] = {}

    # --- creation / scheduling ------------------------------------------
    async def create(
        self, type_: str, title: str, node_id: int | None = None, target: str | None = None
    ) -> int:
        async with SessionLocal() as s:
            job = Job(type=type_, title=title, node_id=node_id, target=target)
            s.add(job)
            await s.commit()
            await s.refresh(job)
            return job.id

    async def start(
        self,
        type_: str,
        title: str,
        coro: JobCoro,
        *,
        node_id: int | None = None,
        target: str | None = None,
    ) -> int:
        job_id = await self.create(type_, title, node_id, target)
        handle = JobHandle(self, job_id)
        task = asyncio.create_task(self._run(job_id, handle, coro))
        self._tasks[job_id] = task
        return job_id

    async def _run(self, job_id: int, handle: JobHandle, coro: JobCoro) -> None:
        await self._set_status(job_id, JOB_RUNNING, started=True)
        await self._publish(job_id, {"type": "status", "status": JOB_RUNNING})
        try:
            result = await coro(handle)
            summary = result if isinstance(result, str) else None
            await self._finish(job_id, JOB_SUCCESS, exit_code=0, summary=summary)
        except asyncio.CancelledError:
            await handle.log("Job cancelled.", "stderr")
            await self._finish(job_id, JOB_CANCELLED, exit_code=130, summary="cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - jobs must capture all failures
            log.exception("Job %s failed", job_id)
            await handle.log(f"ERROR: {exc}", "stderr")
            await self._finish(job_id, JOB_ERROR, exit_code=1, summary=str(exc))
        finally:
            self._tasks.pop(job_id, None)

    async def cancel(self, job_id: int) -> bool:
        task = self._tasks.get(job_id)
        if task is None:
            return False
        task.cancel()
        return True

    # --- persistence helpers --------------------------------------------
    async def _set_status(self, job_id: int, status: str, started: bool = False) -> None:
        async with SessionLocal() as s:
            job = await s.get(Job, job_id)
            if job is None:
                return
            job.status = status
            if started:
                job.started_at = _now()
            await s.commit()

    async def _set_progress(self, job_id: int, progress: float | None) -> None:
        async with SessionLocal() as s:
            job = await s.get(Job, job_id)
            if job is None:
                return
            job.progress = progress
            await s.commit()
        await self._publish(job_id, {"type": "progress", "progress": progress})

    async def _finish(
        self, job_id: int, status: str, exit_code: int | None, summary: str | None
    ) -> None:
        async with SessionLocal() as s:
            job = await s.get(Job, job_id)
            if job is None:
                return
            job.status = status
            job.exit_code = exit_code
            job.summary = summary
            job.finished_at = _now()
            if job.status == JOB_SUCCESS and job.progress is None:
                job.progress = 1.0
            await s.commit()
        await self._publish(
            job_id,
            {"type": "status", "status": status, "exit_code": exit_code, "summary": summary},
        )
        await self._publish(job_id, {"type": "end"})

    async def _append_log(self, job_id: int, stream: str, text: str, seq: int) -> None:
        ts = _now()
        async with SessionLocal() as s:
            s.add(JobLog(job_id=job_id, seq=seq, ts=ts, stream=stream, text=text))
            await s.commit()
        await self._publish(
            job_id,
            {"type": "log", "seq": seq, "stream": stream, "text": text, "ts": ts.isoformat()},
        )

    # --- pub/sub for websocket streaming --------------------------------
    def subscribe(self, job_id: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs.setdefault(job_id, set()).add(q)
        return q

    def unsubscribe(self, job_id: int, q: asyncio.Queue) -> None:
        subs = self._subs.get(job_id)
        if subs:
            subs.discard(q)
            if not subs:
                self._subs.pop(job_id, None)

    async def _publish(self, job_id: int, event: dict[str, Any]) -> None:
        terminal = event.get("type") in ("end", "status")
        for q in list(self._subs.get(job_id, ())):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Never silently drop terminal signals: evict the oldest log
                # event and retry so the consumer can still learn the job ended.
                if terminal:
                    try:
                        q.get_nowait()
                        q.put_nowait(event)
                    except (asyncio.QueueEmpty, asyncio.QueueFull):
                        pass

    def is_running(self, job_id: int) -> bool:
        return job_id in self._tasks

    @staticmethod
    async def latest_seq(job_id: int) -> int:
        async with SessionLocal() as s:
            res = await s.execute(
                select(func.max(JobLog.seq)).where(JobLog.job_id == job_id)
            )
            return res.scalar() or 0


jobs = JobManager()
