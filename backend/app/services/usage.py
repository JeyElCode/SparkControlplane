"""Persistent usage history: periodic rollups of vLLM serving counters.

Every ``usage_rollup_seconds`` (default 5 min) the collector snapshots each
running instance's raw Prometheus counters (already scraped by the telemetry
engine) and writes the *delta* since the previous rollup to the
``usage_samples`` table — tokens generated, prompt tokens, completed requests,
and the window's mean TTFT. Counter resets (instance restart) count from zero
instead of producing negative deltas. Rows older than ``usage_retention_days``
are purged. This gives tokens-per-day-per-model over months, surviving portal
restarts (unlike the in-memory sparkline rings).
"""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from .. import db as _db
from ..config import get_settings
from ..models import INST_RUNNING, Instance, UsageSample
from .telemetry import engine as telemetry

log = logging.getLogger("spark.usage")

# raw counter names as stored by telemetry's per-instance scrape state
K_GEN = "vllm:generation_tokens_total"
K_PROMPT = "vllm:prompt_tokens_total"
K_REQ = "vllm:request_success_total"
K_TTFT_SUM = "vllm:time_to_first_token_seconds_sum"
K_TTFT_CNT = "vllm:time_to_first_token_seconds_count"


def _delta(cur: float | None, prev: float | None) -> float:
    """Counter delta with reset handling: a shrunk counter restarted at ~0."""
    if cur is None:
        return 0.0
    if prev is None or cur < prev:
        return cur
    return cur - prev


def compute_window(cur: dict, prev: dict) -> dict:
    """One rollup window's usage from two raw counter snapshots. Pure."""
    ttft_cnt = _delta(cur.get(K_TTFT_CNT), prev.get(K_TTFT_CNT))
    ttft_sum = _delta(cur.get(K_TTFT_SUM), prev.get(K_TTFT_SUM))
    return {
        "gen_tokens": int(_delta(cur.get(K_GEN), prev.get(K_GEN))),
        "prompt_tokens": int(_delta(cur.get(K_PROMPT), prev.get(K_PROMPT))),
        "requests": int(_delta(cur.get(K_REQ), prev.get(K_REQ))),
        "ttft_ms_avg": round(1000.0 * ttft_sum / ttft_cnt, 1) if ttft_cnt > 0 else None,
    }


class UsageCollector:
    def __init__(self) -> None:
        self._last: dict[int, dict] = {}   # instance_id -> raw counters at last rollup
        self._task: asyncio.Task | None = None
        self._stopping = False

    def start(self) -> None:
        if self._task is None:
            self._stopping = False
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except BaseException:  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        settings = get_settings()
        interval = max(10.0, settings.usage_rollup_seconds)
        # First tick just seeds baselines; deltas start with the second window.
        while not self._stopping:
            started = time.time()
            try:
                await self.rollup()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("usage rollup failed")
            await asyncio.sleep(max(5.0, interval - (time.time() - started)))

    async def rollup(self) -> int:
        """Write one usage row per instance with activity; returns rows written."""
        from datetime import datetime, timedelta, timezone

        settings = get_settings()
        written = 0
        async with _db.SessionLocal() as session:
            instances = list(
                (
                    await session.execute(
                        select(Instance)
                        .where(Instance.status == INST_RUNNING)
                        .options(selectinload(Instance.model))
                    )
                )
                .scalars()
                .all()
            )
            live_ids = {i.id for i in instances}
            for gone in set(self._last) - live_ids:
                del self._last[gone]

            for inst in instances:
                cur = dict(telemetry._inst_counters.get(inst.id) or {})
                cur.pop("_ts", None)
                if not cur:
                    continue  # not scraped yet
                prev = self._last.get(inst.id)
                self._last[inst.id] = cur
                if prev is None:
                    continue  # baseline window
                w = compute_window(cur, prev)
                if w["gen_tokens"] == 0 and w["prompt_tokens"] == 0 and w["requests"] == 0:
                    continue  # idle window — don't fill the table with zeros
                session.add(UsageSample(
                    instance_id=inst.id,
                    instance_name=inst.name,
                    model_name=inst.model.name if inst.model else "?",
                    **w,
                ))
                written += 1

            cutoff = datetime.now(timezone.utc) - timedelta(days=settings.usage_retention_days)
            await session.execute(delete(UsageSample).where(UsageSample.ts < cutoff))
            await session.commit()
        return written


collector = UsageCollector()
