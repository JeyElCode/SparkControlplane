from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import Node
from ..schemas import JobAccepted
from ..services import power
from ..services.jobs import jobs

router = APIRouter(prefix="/api/power", tags=["power"])

_NODE_ACTIONS = {"shutdown", "reboot", "wake"}


@router.get("/nodes/{node_id}/affected", response_model=list[str])
async def get_affected(node_id: int, session: AsyncSession = Depends(get_session)):
    """RUNNING instances a shutdown of this node would take down (for the UI
    confirmation dialog)."""
    node = await session.get(Node, node_id)
    if node is None:
        raise HTTPException(404, "Node not found")
    return await power.affected_instances(session, node)


@router.post("/nodes/{node_id}/{action}", response_model=JobAccepted)
async def node_power(node_id: int, action: str, session: AsyncSession = Depends(get_session)):
    if action not in _NODE_ACTIONS:
        raise HTTPException(404, f"Unknown power action '{action}'")
    node = await session.get(Node, node_id)
    if node is None:
        raise HTTPException(404, "Node not found")
    if action == "wake":
        job_id = await jobs.start(
            "power.wake", f"Wake {node.name}", lambda h: power.wake_node(h, node_id),
            node_id=node_id,
        )
        return JobAccepted(job_id=job_id, message="Wake-on-LAN started")
    reboot = action == "reboot"
    job_id = await jobs.start(
        f"power.{action}", f"{'Reboot' if reboot else 'Shut down'} {node.name}",
        lambda h: power.shutdown_node(h, node_id, reboot=reboot),
        node_id=node_id,
    )
    return JobAccepted(job_id=job_id, message=f"{action} started")


@router.post("/batch/{action}", response_model=JobAccepted)
async def batch_power(action: str):
    if action not in {"shutdown", "wake"}:
        raise HTTPException(404, f"Unknown batch power action '{action}'")
    job_id = await jobs.start(
        f"power.batch.{action}", f"Batch {action} (all nodes)",
        lambda h: power.batch_power(h, action),
    )
    return JobAccepted(job_id=job_id, message=f"Batch {action} started")
