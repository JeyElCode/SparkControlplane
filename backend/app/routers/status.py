from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import SessionLocal, get_session
from ..schemas import StatusSnapshot
from ..services import status_svc

router = APIRouter(prefix="/api/status", tags=["status"])


@router.get("", response_model=StatusSnapshot)
async def get_status(session: AsyncSession = Depends(get_session)):
    return await status_svc.snapshot(session)


@router.websocket("/ws")
async def status_ws(ws: WebSocket):
    await ws.accept()
    interval = float(ws.query_params.get("interval", "10"))
    try:
        while True:
            async with SessionLocal() as session:
                snap = await status_svc.snapshot(session)
            await ws.send_text(snap.model_dump_json())
            await asyncio.sleep(max(3.0, interval))
    except WebSocketDisconnect:
        return
    except Exception:  # noqa: BLE001 - client gone / transient
        return
