from __future__ import annotations

import re

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..schemas import JobAccepted
from ..services import storage as storage_svc
from ..services.jobs import jobs

router = APIRouter(prefix="/api/storage", tags=["storage"])

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


@router.get("")
async def get_storage(session: AsyncSession = Depends(get_session)):
    """Per-node storage breakdown (live SSH scan — takes a few seconds)."""
    return await storage_svc.storage_report(session)


@router.post("/delete-orphan", response_model=JobAccepted)
async def delete_orphan(payload: dict = Body(...)):
    node_id, name = payload.get("node_id"), payload.get("name")
    if not isinstance(node_id, int) or not isinstance(name, str) or not _NAME_RE.match(name):
        raise HTTPException(422, "node_id (int) and a plain directory name are required")
    job_id = await jobs.start(
        "storage.delete_orphan", f"Delete orphan {name}",
        lambda h: storage_svc.delete_orphan(h, node_id, name), target=name,
    )
    return JobAccepted(job_id=job_id, message="Delete started")


@router.post("/clear-hf-cache", response_model=JobAccepted)
async def clear_hf_cache(payload: dict = Body(default={})):
    node_ids = payload.get("node_ids")
    if node_ids is not None and (
        not isinstance(node_ids, list) or any(not isinstance(n, int) for n in node_ids)
    ):
        raise HTTPException(422, "node_ids must be a list of ints (or omitted for all nodes)")
    job_id = await jobs.start(
        "storage.clear_hf_cache", "Clear HuggingFace cache",
        lambda h: storage_svc.clear_hf_cache(h, node_ids),
    )
    return JobAccepted(job_id=job_id, message="Cache clear started")
