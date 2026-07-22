"""Live log streaming: on-demand `journalctl -f` for any spark-* unit.

The WebSocket tails the unit's journal on the owning node over SSH and relays
lines to the browser until the client disconnects. Unit names are restricted
to the ``spark-`` namespace this portal manages.
"""

from __future__ import annotations

import asyncio
import re
import shlex

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..db import SessionLocal, get_session
from ..models import Instance, Node
from ..services import templates
from ..ssh import ssh_for_node

router = APIRouter(prefix="/api/logs", tags=["logs"])

_UNIT_RE = re.compile(r"^spark-[A-Za-z0-9@._-]+(\.service)?$")

# Follow window: the remote `timeout` guarantees an abandoned follow dies even
# if the cancel path is missed. The client just reconnects for longer sessions.
FOLLOW_SECONDS = 3600


class LogUnit(BaseModel):
    node_id: int
    node_name: str
    unit: str
    label: str


@router.get("/units", response_model=list[LogUnit])
async def list_units(session: AsyncSession = Depends(get_session)):
    """Every tailable unit the portal manages, mapped to the node it runs on."""
    nodes = list((await session.execute(select(Node).order_by(Node.role, Node.id))).scalars())
    head = next((n for n in nodes if n.role == "head"), None)
    workers = [n for n in nodes if n.role == "worker"]
    out: list[LogUnit] = []
    for n in nodes:
        out.append(
            LogUnit(
                node_id=n.id, node_name=n.name, unit=templates.ray_unit_name(n.role),
                label=f"Ray {n.role} — {n.name}",
            )
        )
    instances = list(
        (
            await session.execute(select(Instance).options(selectinload(Instance.node)))
        ).scalars()
    )
    for inst in instances:
        api_node = inst.node if inst.topology == "single" else head
        if api_node is not None:
            out.append(
                LogUnit(
                    node_id=api_node.id, node_name=api_node.name,
                    unit=templates.instance_unit_name(inst.name),
                    label=f"vLLM {inst.name} — {api_node.name}",
                )
            )
            if inst.tls_enabled:
                out.append(
                    LogUnit(
                        node_id=api_node.id, node_name=api_node.name,
                        unit=templates.tls_unit_name(inst.name),
                        label=f"TLS proxy {inst.name} — {api_node.name}",
                    )
                )
        if inst.topology == "distributed":
            for w in workers:
                out.append(
                    LogUnit(
                        node_id=w.id, node_name=w.name,
                        unit=templates.distributed_worker_unit_name(inst.name),
                        label=f"vLLM {inst.name} worker — {w.name}",
                    )
                )
    return out


@router.websocket("/ws")
async def logs_ws(ws: WebSocket):
    await ws.accept()
    try:
        node_id = int(ws.query_params.get("node_id", ""))
    except ValueError:
        await ws.close(code=4400, reason="node_id required")
        return
    unit = ws.query_params.get("unit", "")
    if not _UNIT_RE.match(unit):
        await ws.close(code=4400, reason="invalid unit")
        return

    async with SessionLocal() as session:
        node = await session.get(Node, node_id)
        if node is None:
            await ws.close(code=4404, reason="node not found")
            return
        try:
            ssh = await ssh_for_node(session, node)
        except Exception as exc:  # noqa: BLE001
            await ws.send_text(f"[portal] cannot reach {node.name} over SSH: {exc}")
            await ws.close()
            return

    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=2000)

    async def cb(stream: str, line: str) -> None:
        try:
            queue.put_nowait(line)
        except asyncio.QueueFull:  # slow client: drop oldest, keep following
            try:
                queue.get_nowait()
                queue.put_nowait(line)
            except asyncio.QueueEmpty:
                pass

    tail = asyncio.create_task(
        ssh.run(
            f"timeout {FOLLOW_SECONDS} journalctl -u {shlex.quote(unit)} -n 200 -f --no-pager 2>&1 || true",
            sudo=True,
            log_cb=cb,
        )
    )
    def _ended(t: asyncio.Task) -> None:
        if not t.cancelled():
            try:
                queue.put_nowait("[portal] log stream ended")
            except asyncio.QueueFull:
                pass

    tail.add_done_callback(_ended)

    async def pump() -> None:
        while True:
            await ws.send_text(await queue.get())

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            msg = await ws.receive()  # detects client disconnect
            if msg.get("type") == "websocket.disconnect":
                break
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 - transport gone
        pass
    finally:
        for t in (pump_task, tail):
            if not t.done():
                t.cancel()
                try:
                    await t
                except BaseException:  # noqa: BLE001
                    pass
