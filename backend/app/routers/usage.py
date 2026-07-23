from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import UsageSample

router = APIRouter(prefix="/api/usage", tags=["usage"])


class UsagePoint(BaseModel):
    bucket: str            # ISO date (day) or date+hour
    ts: float              # bucket start, unix seconds (chart x-axis)
    gen_tokens: int
    prompt_tokens: int
    requests: int
    ttft_ms_avg: float | None = None


class ModelUsage(BaseModel):
    model_name: str
    total_gen_tokens: int
    total_prompt_tokens: int
    total_requests: int
    points: list[UsagePoint]


@router.get("", response_model=list[ModelUsage])
async def get_usage(
    days: int = Query(default=30, ge=1, le=365),
    bucket: str = Query(default="day", pattern="^(day|hour)$"),
    session: AsyncSession = Depends(get_session),
):
    """Serving usage per model, bucketed by day or hour, most-used first.
    TTFT per bucket is the request-weighted mean."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (
        await session.execute(
            select(UsageSample).where(UsageSample.ts >= cutoff).order_by(UsageSample.ts)
        )
    ).scalars().all()

    fmt = "%Y-%m-%d" if bucket == "day" else "%Y-%m-%d %H:00"
    agg: dict[str, dict[str, dict]] = {}
    for r in rows:
        ts = r.ts if r.ts.tzinfo else r.ts.replace(tzinfo=timezone.utc)
        key = ts.strftime(fmt)
        b = agg.setdefault(r.model_name, {}).setdefault(key, {
            "gen": 0, "prompt": 0, "req": 0, "ttft_w": 0.0, "ttft_n": 0,
            "ts": datetime.strptime(key, fmt).replace(tzinfo=timezone.utc).timestamp(),
        })
        b["gen"] += r.gen_tokens
        b["prompt"] += r.prompt_tokens
        b["req"] += r.requests
        if r.ttft_ms_avg is not None and r.requests > 0:
            b["ttft_w"] += r.ttft_ms_avg * r.requests
            b["ttft_n"] += r.requests

    out: list[ModelUsage] = []
    for model, buckets in agg.items():
        points = [
            UsagePoint(
                bucket=key, ts=b["ts"], gen_tokens=b["gen"], prompt_tokens=b["prompt"],
                requests=b["req"],
                ttft_ms_avg=round(b["ttft_w"] / b["ttft_n"], 1) if b["ttft_n"] else None,
            )
            for key, b in sorted(buckets.items())
        ]
        out.append(ModelUsage(
            model_name=model,
            total_gen_tokens=sum(p.gen_tokens for p in points),
            total_prompt_tokens=sum(p.prompt_tokens for p in points),
            total_requests=sum(p.requests for p in points),
            points=points,
        ))
    out.sort(key=lambda m: -m.total_gen_tokens)
    return out
