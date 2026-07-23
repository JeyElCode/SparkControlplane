from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..config import get_settings
from ..db import get_session
from ..models import Instance, InstanceSchedule
from ..services.scheduler import now_tz

router = APIRouter(prefix="/api/schedules", tags=["schedules"])

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ScheduleIn(BaseModel):
    instance_id: int
    days: list[int]
    start_time: str
    end_time: str
    enabled: bool = True

    @field_validator("days")
    @classmethod
    def _days(cls, v: list[int]) -> list[int]:
        if not v or any(d < 0 or d > 6 for d in v):
            raise ValueError("days must be a non-empty list of weekday numbers 0-6 (Mon=0)")
        return sorted(set(v))

    @field_validator("start_time", "end_time")
    @classmethod
    def _time(cls, v: str) -> str:
        if not _TIME_RE.match(v):
            raise ValueError("times must be HH:MM (24h)")
        return v


class ScheduleUpdate(BaseModel):
    days: list[int] | None = None
    start_time: str | None = None
    end_time: str | None = None
    enabled: bool | None = None

    @field_validator("days")
    @classmethod
    def _days(cls, v: list[int] | None) -> list[int] | None:
        if v is None:
            return None
        if not v or any(d < 0 or d > 6 for d in v):
            raise ValueError("days must be a non-empty list of weekday numbers 0-6 (Mon=0)")
        return sorted(set(v))

    @field_validator("start_time", "end_time")
    @classmethod
    def _time(cls, v: str | None) -> str | None:
        if v is not None and not _TIME_RE.match(v):
            raise ValueError("times must be HH:MM (24h)")
        return v


class ScheduleOut(BaseModel):
    id: int
    instance_id: int
    instance_name: str
    model_name: str
    topology: str
    status: str
    days: list[int]
    start_time: str
    end_time: str
    enabled: bool
    # Planner helpers: what this window costs while it is live.
    est_gib_per_node: float
    node_scope: str  # "all nodes" or the pinned node's name

    @classmethod
    def of(cls, s: InstanceSchedule) -> "ScheduleOut":
        settings = get_settings()
        inst = s.instance
        est = round((inst.gpu_memory_utilization or 0.0) * settings.node_memory_gib, 1)
        scope = (
            inst.node.name if inst.topology == "single" and inst.node else "all nodes"
        )
        return cls(
            id=s.id, instance_id=s.instance_id,
            instance_name=inst.name,
            model_name=inst.model.name if inst.model else "?",
            topology=inst.topology, status=inst.status,
            days=[int(d) for d in s.days.split(",") if d.strip().isdigit()],
            start_time=s.start_time, end_time=s.end_time, enabled=s.enabled,
            est_gib_per_node=est, node_scope=scope,
        )


_LOAD = (
    select(InstanceSchedule)
    .options(
        selectinload(InstanceSchedule.instance).selectinload(Instance.model),
        selectinload(InstanceSchedule.instance).selectinload(Instance.node),
    )
    .order_by(InstanceSchedule.instance_id, InstanceSchedule.start_time)
)


@router.get("", response_model=list[ScheduleOut])
async def list_schedules(session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(_LOAD)).scalars().all()
    return [ScheduleOut.of(s) for s in rows if s.instance is not None]


@router.get("/now")
async def schedules_now():
    """The scheduler's current wall clock (its timezone), for the planner UI."""
    now = now_tz()
    return {"now": now.isoformat(), "weekday": now.weekday(),
            "tz": str(now.tzinfo), "minutes": now.hour * 60 + now.minute}


@router.post("", response_model=ScheduleOut, status_code=201)
async def create_schedule(payload: ScheduleIn, session: AsyncSession = Depends(get_session)):
    inst = await session.get(Instance, payload.instance_id)
    if inst is None:
        raise HTTPException(404, "Instance not found")
    row = InstanceSchedule(
        instance_id=payload.instance_id,
        days=",".join(str(d) for d in payload.days),
        start_time=payload.start_time, end_time=payload.end_time,
        enabled=payload.enabled,
    )
    session.add(row)
    await session.commit()
    row = (
        await session.execute(_LOAD.where(InstanceSchedule.id == row.id))
    ).scalar_one()
    return ScheduleOut.of(row)


@router.patch("/{schedule_id}", response_model=ScheduleOut)
async def update_schedule(
    schedule_id: int, payload: ScheduleUpdate, session: AsyncSession = Depends(get_session)
):
    row = (
        await session.execute(_LOAD.where(InstanceSchedule.id == schedule_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Schedule not found")
    if payload.days is not None:
        row.days = ",".join(str(d) for d in payload.days)
    if payload.start_time is not None:
        row.start_time = payload.start_time
    if payload.end_time is not None:
        row.end_time = payload.end_time
    if payload.enabled is not None:
        row.enabled = payload.enabled
    await session.commit()
    return ScheduleOut.of(row)


@router.delete("/{schedule_id}", status_code=204)
async def delete_schedule(schedule_id: int, session: AsyncSession = Depends(get_session)):
    row = await session.get(InstanceSchedule, schedule_id)
    if row is None:
        raise HTTPException(404, "Schedule not found")
    await session.delete(row)
    await session.commit()
