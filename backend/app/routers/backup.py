from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import JSONResponse

from ..services import backup as backup_svc

router = APIRouter(prefix="/api/backup", tags=["backup"])


@router.get("/export")
async def export_bundle():
    """Download the current config as a restorable JSON bundle."""
    bundle = await backup_svc.build_bundle()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    return JSONResponse(
        bundle,
        headers={"Content-Disposition":
                 f'attachment; filename="spark-backup-{stamp}.json"'},
    )


@router.post("/import")
async def import_bundle(bundle: dict = Body(...)):
    """Restore a bundle (replaces all config tables; history is untouched)."""
    try:
        return await backup_svc.apply_bundle(bundle)
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.get("/status")
async def backup_status():
    r = backup_svc.runner
    return {
        "last_ok_ts": r.last_ok_ts,
        "last_key": r.last_key,
        "last_error": r.last_error,
    }


@router.post("/run")
async def run_now():
    """Build + upload a backup to the configured S3 target immediately."""
    try:
        key = await backup_svc.run_backup()
    except Exception as exc:  # noqa: BLE001 - config/network problems -> clear message
        raise HTTPException(502, str(exc))
    backup_svc.runner.last_key = key
    backup_svc.runner.last_error = None
    import time

    backup_svc.runner.last_ok_ts = time.time()
    return {"ok": True, "key": key}


@router.get("/s3")
async def list_s3_backups():
    try:
        return await backup_svc.list_backups()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, str(exc))


@router.post("/s3-restore")
async def restore_s3(payload: dict = Body(...)):
    key = payload.get("key")
    if not key or not isinstance(key, str):
        raise HTTPException(422, "key is required")
    try:
        return await backup_svc.restore_from_s3(key)
    except json.JSONDecodeError:
        raise HTTPException(422, f"{key} is not a valid backup bundle")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, str(exc))
