from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..crypto import decrypt
from ..db import get_session, get_setting
from ..models import Alert
from ..schemas import ActiveAlert, AlertOut
from ..services import alerts as alerts_svc

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


@router.get("", response_model=list[AlertOut])
async def list_alerts(
    limit: int = Query(default=50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
):
    """Alert history, newest first (active ones have resolved_at = null)."""
    rows = (
        await session.execute(select(Alert).order_by(Alert.id.desc()).limit(limit))
    ).scalars().all()
    return [AlertOut.model_validate(r) for r in rows]


@router.get("/active", response_model=list[ActiveAlert])
async def active_alerts():
    return [
        ActiveAlert(rule=a["rule"], subject=a["subject"], severity=a["severity"],
                    message=a["message"], since=a["since"])
        for a in alerts_svc.manager.active()
    ]


@router.post("/test")
async def test_webhook(session: AsyncSession = Depends(get_session)):
    """Send a test notification through the configured webhook."""
    setting = await get_setting(session)
    url = decrypt(setting.alert_webhook_url_enc)
    if not url:
        raise HTTPException(400, "No alert webhook configured — set one in Settings first.")
    cfg = alerts_svc.merged_config(setting.alerts_json)
    fact = alerts_svc.Fact(
        rule="test", subject="portal", active=True, sustain=0, severity="warn",
        message="This is a test notification from Spark Control Plane.",
    )
    err = await alerts_svc.send_webhook(url, cfg.get("webhook_kind", "generic"), "fired", fact)
    if err:
        raise HTTPException(502, err)
    return {"ok": True, "message": "Test notification sent."}
