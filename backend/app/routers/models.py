from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import SessionLocal, get_session
from ..models import JOB_PENDING, JOB_RUNNING, Job, ModelRegistry
from ..schemas import JobAccepted, ModelIn, ModelOut, ModelSuggestion
from ..services import models_svc
from ..services.jobs import jobs
from ..services.parsers import SUGGESTIONS

router = APIRouter(prefix="/api/models", tags=["models"])

# Only one file operation (download/sync/delete) may run per model at a time —
# concurrent hf downloads into the same dir collide on HuggingFace .lock files.
_FILE_OP_TYPES = ("model.download", "model.sync", "model.delete")


async def _active_file_job(session: AsyncSession, model_name: str) -> Job | None:
    """Return a genuinely-running download/sync/delete job for this model, if any.
    Cross-checks the in-process job manager so a stale 'running' row left by a
    crashed/restarted process doesn't block forever."""
    res = await session.execute(
        select(Job)
        .where(
            Job.target == model_name,
            Job.type.in_(_FILE_OP_TYPES),
            Job.status.in_((JOB_PENDING, JOB_RUNNING)),
        )
        .order_by(Job.id.desc())
    )
    for job in res.scalars().all():
        if jobs.is_running(job.id):
            return job
    return None


async def _guard_no_active_file_job(session: AsyncSession, model: ModelRegistry) -> None:
    busy = await _active_file_job(session, model.name)
    if busy is not None:
        op = busy.type.split(".")[-1]
        raise HTTPException(
            409,
            f"A {op} is already in progress for '{model.name}' (job #{busy.id}). "
            "Wait for it to finish before starting another.",
        )


@router.get("", response_model=list[ModelOut])
async def list_models(session: AsyncSession = Depends(get_session)):
    models = await models_svc.list_models_full(session)
    return [ModelOut.of(m) for m in models]


@router.get("/suggestions", response_model=list[ModelSuggestion])
async def suggestions():
    return SUGGESTIONS


@router.post("/scan", response_model=list[ModelOut])
async def scan(session: AsyncSession = Depends(get_session)):
    """Scan the nodes' models dirs and import any on-disk model not yet in the
    registry, then return the refreshed registry."""
    await models_svc.discover_models(session)
    models = await models_svc.list_models_full(session)
    return [ModelOut.of(m) for m in models]


@router.post("/validate")
async def validate(repo_id: str = Body(..., embed=True)):
    return await models_svc.validate_repo(repo_id)


@router.post("", response_model=ModelOut, status_code=201)
async def add_model(payload: ModelIn, session: AsyncSession = Depends(get_session)):
    try:
        model = await models_svc.add_model(
            session, payload.repo_id, payload.name, payload.tool_parser
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return ModelOut.of(model)


@router.get("/{model_id}", response_model=ModelOut)
async def get_model(model_id: int, session: AsyncSession = Depends(get_session)):
    model = await models_svc.load_model(session, model_id)
    if model is None:
        raise HTTPException(404, "Model not found")
    return ModelOut.of(model)


@router.post("/{model_id}/download", response_model=JobAccepted)
async def download(model_id: int, auto_sync: bool = True, session: AsyncSession = Depends(get_session)):
    model = await models_svc.load_model(session, model_id)
    if model is None:
        raise HTTPException(404, "Model not found")
    await _guard_no_active_file_job(session, model)
    name = model.name

    async def coro(h):
        async with SessionLocal() as s:
            return await models_svc.download_model(s, h, model_id, auto_sync=auto_sync)

    job_id = await jobs.start("model.download", f"Download {name}", coro, target=name)
    return JobAccepted(job_id=job_id, message="Download started")


@router.post("/{model_id}/sync", response_model=JobAccepted)
async def sync(
    model_id: int,
    target_node_id: int | None = Body(None, embed=True),
    session: AsyncSession = Depends(get_session),
):
    model = await models_svc.load_model(session, model_id)
    if model is None:
        raise HTTPException(404, "Model not found")
    await _guard_no_active_file_job(session, model)
    name = model.name

    async def coro(h):
        async with SessionLocal() as s:
            return await models_svc.sync_model(s, h, model_id, target_node_id)

    job_id = await jobs.start("model.sync", f"Sync {name}", coro, target=name)
    return JobAccepted(job_id=job_id, message="Sync started")


@router.post("/{model_id}/refresh", response_model=ModelOut)
async def refresh(model_id: int, session: AsyncSession = Depends(get_session)):
    await models_svc.refresh_presence(session, model_id)
    model = await models_svc.load_model(session, model_id)
    if model is None:
        raise HTTPException(404, "Model not found")
    return ModelOut.of(model)


@router.post("/{model_id}/delete", response_model=JobAccepted)
async def delete_files(
    model_id: int,
    node_ids: list[int] | None = Body(None, embed=True),
    drop_row: bool = Body(False, embed=True),
    session: AsyncSession = Depends(get_session),
):
    model = await models_svc.load_model(session, model_id)
    if model is None:
        raise HTTPException(404, "Model not found")
    await _guard_no_active_file_job(session, model)
    name = model.name

    async def coro(h):
        async with SessionLocal() as s:
            return await models_svc.delete_model_files(s, h, model_id, node_ids, drop_row)

    job_id = await jobs.start("model.delete", f"Delete {name}", coro, target=name)
    return JobAccepted(job_id=job_id, message="Delete started")


@router.delete("/{model_id}", status_code=204)
async def remove_registry(model_id: int, session: AsyncSession = Depends(get_session)):
    model = await session.get(ModelRegistry, model_id)
    if model is None:
        raise HTTPException(404, "Model not found")
    await session.delete(model)
    await session.commit()
