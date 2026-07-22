from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import SessionLocal, get_session
from ..schemas import NodeHistory, StatusSnapshot
from ..services.telemetry import engine

router = APIRouter(prefix="/api/status", tags=["status"])


@router.get("", response_model=StatusSnapshot)
async def get_status(session: AsyncSession = Depends(get_session)):
    """Served from the telemetry engine's caches — no SSH on the request path."""
    return await engine.compose_snapshot(session)


@router.get("/history", response_model=list[NodeHistory])
async def get_history(minutes: int = Query(default=15, ge=1, le=120)):
    """Per-node sparkline history (CPU, memory, GPU, QSFP/LAN throughput, disk)."""
    return engine.history(minutes=minutes)


@router.websocket("/ws")
async def status_ws(ws: WebSocket):
    await ws.accept()
    interval = float(ws.query_params.get("interval", "3"))
    try:
        while True:
            async with SessionLocal() as session:
                snap = await engine.compose_snapshot(session)
            await ws.send_text(snap.model_dump_json())
            await asyncio.sleep(max(2.0, interval))
    except WebSocketDisconnect:
        return
    except Exception:  # noqa: BLE001 - client gone / transient
        return
