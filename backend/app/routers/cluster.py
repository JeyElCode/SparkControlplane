from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..crypto import encrypt
from ..db import get_cluster_config, get_session, get_setting
from ..schemas import (
    ClusterConfigIn,
    ClusterConfigOut,
    ImageUpdateIn,
    JobAccepted,
    SettingsIn,
    SettingsOut,
    SetupRequest,
    TeardownRequest,
)
from ..services import cluster, registry
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


def _settings_out(s) -> SettingsOut:
    return SettingsOut(
        has_hf_token=bool(s.hf_token_enc),
        status_poll_seconds=s.status_poll_seconds,
        setup_complete=s.setup_complete,
        judge_base_url=s.judge_base_url,
        judge_model=s.judge_model,
        has_judge_api_key=bool(s.judge_api_key_enc),
    )


@router.get("/settings", response_model=SettingsOut)
async def get_settings_ep(session: AsyncSession = Depends(get_session)):
    return _settings_out(await get_setting(session))


@router.patch("/settings", response_model=SettingsOut)
async def update_settings_ep(payload: SettingsIn, session: AsyncSession = Depends(get_session)):
    s = await get_setting(session)
    if payload.hf_token is not None:
        s.hf_token_enc = encrypt(payload.hf_token)
    if payload.status_poll_seconds is not None:
        s.status_poll_seconds = payload.status_poll_seconds
    if payload.judge_base_url is not None:
        s.judge_base_url = payload.judge_base_url or None
    if payload.judge_model is not None:
        s.judge_model = payload.judge_model or None
    if payload.judge_api_key is not None:
        s.judge_api_key_enc = encrypt(payload.judge_api_key)
    await session.commit()
    return _settings_out(s)


@router.get("/image-tags")
async def image_tags(image: str | None = None, session: AsyncSession = Depends(get_session)):
    """Available tags for the cluster image's repository (or an explicit
    ``image``), newest first — the 'check for updates' call."""
    from fastapi import HTTPException

    if image is None:
        cfg = await get_cluster_config(session)
        image = cfg.vllm_image
    try:
        return await registry.list_tags(image)
    except Exception as exc:  # noqa: BLE001 - registry unreachable / auth quirk
        raise HTTPException(502, f"Could not list tags for {image}: {exc}")


@router.post("/image-update", response_model=JobAccepted)
async def image_update(payload: ImageUpdateIn):
    job_id = await jobs.start(
        "cluster.image_update",
        f"Update cluster image to {payload.image}",
        lambda h: cluster.update_image(
            h, payload.image, payload.restart_ray, payload.restart_instances
        ),
    )
    return JobAccepted(job_id=job_id, message="Image update started")


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
