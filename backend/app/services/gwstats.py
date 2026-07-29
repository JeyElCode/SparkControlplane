"""Gateway request observability.

``_proxy`` used to record nothing at all: no counter, no log line, no row. Usage
rollups came only from vLLM's own counters keyed by instance, so every
gateway-level outcome was invisible — a client sending a bad token (401), asking
for a model that doesn't exist (404), hitting an instance that is still loading
(503) or unreachable (502) left no trace anywhere. The operator found out when
someone complained.

**No per-request row ever reaches SQLite.** Requests are folded into in-memory
counters keyed by (client, model), plus a small ring of recent records for "what
happened at 14:32" debugging; a background task flushes the *aggregates* every 5
minutes into ``gateway_samples``, reusing the services/usage.py loop-and-purge
shape. The reason is not squeamishness about write volume: for a streamed
response the record is only complete when the generator closes — inside the
relay's ``finally``, while the client may already be disconnecting — and
awaiting a SQLite writer lock there, in contention with the telemetry loops and
the reconciler, is the worst possible place to block. Durability buys nothing
either; nobody needs the last 30 seconds of request logs to survive a crash.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete

from .. import db as _db
from ..config import get_settings
from ..models import GatewaySample
from . import apikeys

log = logging.getLogger("spark.gwstats")

__all__ = ["RequestRecord", "Stats", "stats", "GatewayStatsCollector", "gw_collector"]

RECENT_MAX = 200


@dataclass
class RequestRecord:
    """One completed gateway request, for the recent-requests ring."""

    ts: float
    client: str
    model: str
    instance: str | None
    status: int
    duration_ms: int
    ttfb_ms: int | None = None
    streamed: bool = False
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    error: str | None = None


@dataclass
class _Bucket:
    requests: int = 0
    errors: int = 0
    rejected: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms_total: int = 0
    ttfb_ms_total: int = 0
    ttfb_count: int = 0


@dataclass
class Stats:
    """Live in-memory aggregates. Cleared into the DB by the collector."""

    buckets: dict[tuple[str, str], _Bucket] = field(default_factory=lambda: defaultdict(_Bucket))
    recent: deque[RequestRecord] = field(default_factory=lambda: deque(maxlen=RECENT_MAX))
    # Cumulative since process start, for the Prometheus exporter (counters must
    # not reset when a rollup window flushes).
    totals: dict[tuple[str, str], _Bucket] = field(default_factory=lambda: defaultdict(_Bucket))

    def record(self, rec: RequestRecord) -> None:
        self.recent.append(rec)
        for target in (self.buckets, self.totals):
            b = target[(rec.client, rec.model)]
            b.requests += 1
            if rec.status >= 400:
                b.errors += 1
            if rec.status in (401, 429):
                b.rejected += 1
            b.duration_ms_total += rec.duration_ms
            if rec.ttfb_ms is not None:
                b.ttfb_ms_total += rec.ttfb_ms
                b.ttfb_count += 1
            if rec.prompt_tokens:
                b.prompt_tokens += rec.prompt_tokens
            if rec.completion_tokens:
                b.completion_tokens += rec.completion_tokens

    def drain(self) -> dict[tuple[str, str], _Bucket]:
        out = dict(self.buckets)
        self.buckets = defaultdict(_Bucket)
        return out

    def reset(self) -> None:
        self.buckets = defaultdict(_Bucket)
        self.totals = defaultdict(_Bucket)
        self.recent.clear()


stats = Stats()


class GatewayStatsCollector:
    """Flushes the in-memory aggregates to ``gateway_samples`` periodically."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stopping = False

    def start(self) -> None:
        if self._task is None:
            self._stopping = False
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        # Don't lose the final partial window on a clean shutdown.
        try:
            await self.flush()
        except Exception:  # noqa: BLE001 - shutting down anyway
            log.debug("final gateway stats flush failed", exc_info=True)

    async def _loop(self) -> None:
        interval = max(30.0, get_settings().gateway_rollup_seconds)
        while not self._stopping:
            started = time.time()
            try:
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a bad flush must not kill the loop
                log.exception("gateway stats flush failed")
            await asyncio.sleep(max(5.0, interval - (time.time() - started)))

    async def flush(self) -> int:
        """Write one row per (client, model) with traffic this window."""
        drained = stats.drain()
        if not drained and not apikeys._LAST_USED:
            return 0
        settings = get_settings()
        now = datetime.now(timezone.utc)
        async with _db.SessionLocal() as session:
            for (client, model), b in drained.items():
                session.add(GatewaySample(
                    ts=now,
                    client=client[:64],
                    model=model[:128],
                    requests=b.requests,
                    errors=b.errors,
                    rejected=b.rejected,
                    prompt_tokens=b.prompt_tokens,
                    completion_tokens=b.completion_tokens,
                    duration_ms_total=b.duration_ms_total,
                    ttfb_ms_total=b.ttfb_ms_total,
                    ttfb_count=b.ttfb_count,
                ))
            await apikeys.persist_last_used(session)
            cutoff = now - timedelta(days=settings.usage_retention_days)
            await session.execute(delete(GatewaySample).where(GatewaySample.ts < cutoff))
            await session.commit()
        return len(drained)


gw_collector = GatewayStatsCollector()
