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
    from ..services.alerts import merged_config

    return SettingsOut(
        has_hf_token=bool(s.hf_token_enc),
        status_poll_seconds=s.status_poll_seconds,
        setup_complete=s.setup_complete,
        alerts=merged_config(s.alerts_json),
        has_alert_webhook=bool(s.alert_webhook_url_enc),
        backup_enabled=s.backup_enabled,
        backup_s3_endpoint=s.backup_s3_endpoint,
        backup_s3_bucket=s.backup_s3_bucket,
        backup_s3_prefix=s.backup_s3_prefix,
        backup_s3_region=s.backup_s3_region,
        backup_s3_access_key=s.backup_s3_access_key,
        has_backup_s3_secret=bool(s.backup_s3_secret_enc),
        backup_interval_hours=s.backup_interval_hours,
        backup_retention=s.backup_retention,
        has_gateway_token=bool(s.gateway_token_enc),
        **_cert_settings(s),
    )


def _cert_settings(s) -> dict:
    from ..services.endpoints import parse_certificate
    from ..services.pki import lifetime_policy

    policy = None
    try:
        policy = lifetime_policy(s.node_cert_ttl_hours)
    except ValueError:
        # A lifetime stored before the rails existed, or hand-edited. Show the
        # settings rather than refusing to render the whole page.
        pass
    subject = None
    if s.node_ca_pem:
        info = parse_certificate(s.node_ca_pem)
        subject = info.subject if info.ok else None
    return {
        "node_cert_source": s.node_cert_source or "none",
        "node_cert_ttl_hours": s.node_cert_ttl_hours,
        "cert_renew_after_hours": policy.renew_after_hours if policy else None,
        "cert_retry_window_hours": policy.retry_window_hours if policy else None,
        "has_node_ca": bool(s.node_ca_pem),
        "node_ca_subject": subject,
        "pki_url": s.pki_url,
        "pki_mount": s.pki_mount or "pki",
        "pki_role": s.pki_role,
        "has_pki_token": bool(s.pki_token_enc),
    }


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
    if payload.alerts is not None:
        import json as _json

        from ..services.alerts import DEFAULTS, merged_config

        unknown = set(payload.alerts) - set(DEFAULTS)
        if unknown:
            from fastapi import HTTPException

            raise HTTPException(422, f"Unknown alert setting(s): {', '.join(sorted(unknown))}")
        merged = merged_config(s.alerts_json)
        merged.update(payload.alerts)
        s.alerts_json = _json.dumps(merged)
    if payload.alert_webhook_url is not None:
        s.alert_webhook_url_enc = (
            encrypt(payload.alert_webhook_url) if payload.alert_webhook_url else None
        )
    if payload.backup_enabled is not None:
        s.backup_enabled = payload.backup_enabled
    # nullable string fields: "" clears them
    for field in ("backup_s3_endpoint", "backup_s3_bucket", "backup_s3_access_key"):
        val = getattr(payload, field)
        if val is not None:
            setattr(s, field, val.strip() or None)
    # non-null string fields keep their value even when set to ""
    if payload.backup_s3_prefix is not None:
        s.backup_s3_prefix = payload.backup_s3_prefix.strip()
    if payload.backup_s3_region is not None and payload.backup_s3_region.strip():
        s.backup_s3_region = payload.backup_s3_region.strip()
    if payload.node_cert_source is not None:
        s.node_cert_source = payload.node_cert_source
    if payload.node_cert_ttl_hours is not None:
        s.node_cert_ttl_hours = payload.node_cert_ttl_hours
    for field in ("pki_url", "pki_role"):
        val = getattr(payload, field)
        if val is not None:
            setattr(s, field, val.strip() or None)
    if payload.pki_mount is not None and payload.pki_mount.strip():
        s.pki_mount = payload.pki_mount.strip()
    if payload.pki_token is not None:
        s.pki_token_enc = encrypt(payload.pki_token) if payload.pki_token else None
    if payload.node_ca_pem is not None:
        ca = payload.node_ca_pem.strip()
        if ca and "BEGIN CERTIFICATE" not in ca:
            from fastapi import HTTPException

            raise HTTPException(
                422,
                "That does not look like a PEM certificate. This is the CA the "
                "cluster proxy checks node certificates against, and a bad one "
                "means the proxy rejects every connection.",
            )
        s.node_ca_pem = ca or None
    if payload.backup_s3_secret is not None:
        s.backup_s3_secret_enc = (
            encrypt(payload.backup_s3_secret) if payload.backup_s3_secret else None
        )
    if payload.backup_interval_hours is not None:
        s.backup_interval_hours = max(0.25, payload.backup_interval_hours)
    if payload.backup_retention is not None:
        s.backup_retention = max(1, payload.backup_retention)
    if payload.gateway_token is not None:
        s.gateway_token_enc = encrypt(payload.gateway_token) if payload.gateway_token else None
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
