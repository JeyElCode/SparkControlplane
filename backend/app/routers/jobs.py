from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..db import SessionLocal, get_session
from ..models import Job
from ..schemas import JobDetail, JobOut
from ..services.jobs import jobs as job_mgr

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

TERMINAL = {"success", "error", "cancelled"}


@router.get("", response_model=list[JobOut])
async def list_jobs(limit: int = 50, session: AsyncSession = Depends(get_session)):
    rows = (
        (await session.execute(select(Job).order_by(Job.id.desc()).limit(limit)))
        .scalars()
        .all()
    )
    return [JobOut.of(j) for j in rows]


@router.get("/{job_id}", response_model=JobDetail)
async def get_job(job_id: int, session: AsyncSession = Depends(get_session)):
    res = await session.execute(
        select(Job).options(selectinload(Job.logs)).where(Job.id == job_id)
    )
    job = res.scalar_one_or_none()
    if job is None:
        raise HTTPException(404, "Job not found")
    return JobDetail.of_detail(job)


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: int):
    ok = await job_mgr.cancel(job_id)
    if not ok:
        raise HTTPException(409, "Job is not running")
    return {"cancelled": True}


@router.websocket("/{job_id}/logs")
async def stream_logs(ws: WebSocket, job_id: int):
    await ws.accept()
    queue = job_mgr.subscribe(job_id)
    last_seq = 0
    try:
        # Backlog first (subscribed before reading, so nothing is lost).
        async with SessionLocal() as session:
            res = await session.execute(
                select(Job).options(selectinload(Job.logs)).where(Job.id == job_id)
            )
            job = res.scalar_one_or_none()
            if job is None:
                await ws.send_json({"type": "error", "text": "Job not found"})
                await ws.close()
                return
            for log in job.logs:
                await ws.send_json(
                    {
                        "type": "log",
                        "seq": log.seq,
                        "stream": log.stream,
                        "text": log.text,
                        "ts": log.ts.isoformat(),
                    }
                )
                last_seq = max(last_seq, log.seq)
            await ws.send_json({"type": "status", "status": job.status})
            finished = job.status in TERMINAL

        if finished:
            await ws.send_json({"type": "end"})
            await ws.close()
            return

        # Live tail. The pub/sub queue is bounded and lossy, so we never rely on
        # it to deliver the terminal event: a periodic timeout re-checks the
        # authoritative job state and ends the stream once it is terminal and we
        # have sent every persisted log line.
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                async with SessionLocal() as session:
                    job = await session.get(Job, job_id)
                if (
                    job is not None
                    and job.status in TERMINAL
                    and last_seq >= await job_mgr.latest_seq(job_id)
                ):
                    await ws.send_json({"type": "status", "status": job.status})
                    await ws.send_json({"type": "end"})
                    break
                continue
            if event.get("type") == "log" and event.get("seq", 0) <= last_seq:
                continue
            if event.get("type") == "log":
                last_seq = max(last_seq, event.get("seq", 0))
            await ws.send_json(event)
            if event.get("type") == "end":
                break
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 - client gone / transient
        pass
    finally:
        job_mgr.unsubscribe(job_id, queue)
