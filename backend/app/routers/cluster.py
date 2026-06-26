from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..crypto import encrypt
from ..db import get_cluster_config, get_session, get_setting
from ..schemas import (
    ClusterConfigIn,
    ClusterConfigOut,
    JobAccepted,
    SettingsIn,
    SettingsOut,
    SetupRequest,
    TeardownRequest,
)
from ..services import cluster
from ..services.jobs import jobs
from ..services.phases import PHASE_TITLES, PHASES_ORDER

router = APIRouter(prefix="/api/cluster", tags=["cluster"])


@router.get("/config", response_model=ClusterConfigOut)
async def get_config(session: AsyncSession = Depends(get_session)):
    return ClusterConfigOut.model_validate(await get_cluster_config(session))


@router.patch("/config", response_model=ClusterConfigOut)
async def update_config(payload: ClusterConfigIn, session: AsyncSession = Depends(get_session)):
    cfg = await get_cluster_config(session)
    for field, value in payload.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(cfg, field, value)
    await session.commit()
    await session.refresh(cfg)
    return ClusterConfigOut.model_validate(cfg)


@router.get("/settings", response_model=SettingsOut)
async def get_settings_ep(session: AsyncSession = Depends(get_session)):
    s = await get_setting(session)
    return SettingsOut(
        has_hf_token=bool(s.hf_token_enc),
        status_poll_seconds=s.status_poll_seconds,
        setup_complete=s.setup_complete,
    )


@router.patch("/settings", response_model=SettingsOut)
async def update_settings_ep(payload: SettingsIn, session: AsyncSession = Depends(get_session)):
    s = await get_setting(session)
    if payload.hf_token is not None:
        s.hf_token_enc = encrypt(payload.hf_token)
    if payload.status_poll_seconds is not None:
        s.status_poll_seconds = payload.status_poll_seconds
    await session.commit()
    return SettingsOut(
        has_hf_token=bool(s.hf_token_enc),
        status_poll_seconds=s.status_poll_seconds,
        setup_complete=s.setup_complete,
    )


@router.get("/phases")
async def list_phases():
    return [{"phase": p, "title": PHASE_TITLES[p]} for p in PHASES_ORDER]


@router.post("/setup", response_model=JobAccepted)
async def run_setup(payload: SetupRequest):
    phases = payload.phases
    title = "Cluster setup" if not phases else f"Setup: {', '.join(phases)}"
    job_id = await jobs.start("setup", title, lambda h: cluster.run_setup(h, phases))
    return JobAccepted(job_id=job_id, message="Setup started")


@router.post("/teardown", response_model=JobAccepted)
async def run_teardown(payload: TeardownRequest):
    job_id = await jobs.start("teardown", "Cluster teardown", lambda h: cluster.teardown(h, payload))
    return JobAccepted(job_id=job_id, message="Teardown started")
