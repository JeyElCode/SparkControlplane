"""Reconcile recorded instance status against what the nodes actually report.

Before this existed, ``start_instance`` committed ``running`` the moment the
systemd unit was installed — *before* the health wait — and nothing ever moved
it back. A model that OOM'd at load, crashed after hours of serving, or died
with its node stayed ``running`` in the database forever, which meant the /v1
gateway kept routing external traffic to a dead upstream and the scheduler
considered the start "reached" so it never retried.

The contract is **jobs propose, the observer confirms**: a start job installs
the unit and leaves the row ``starting``; only this module promotes it to
``running``, and only on proof — a 200 from ``/health``. Only this module
demotes a ``running`` instance to ``error``.

It actuates nothing. No SSH of its own either: it reads the probes the
telemetry slow tick already collects (systemd ActiveState + NRestarts +
``/health``) and writes short, single-statement transactions.

Distinguishing "still loading" from "dead" is the whole difficulty — a large
FP8 model legitimately takes many minutes — so there are four independent
signals rather than one timeout:

1. **unit dead** (``ActiveState`` inactive/failed) sustained past
   ``reconcile_unit_dead_seconds`` — a definite negative, never inferred from a
   failing ``/health`` (which is *expected* to fail during a load).
2. **crash loop** — restarts climbing while the instance has never once been
   healthy. Catches vLLM OOM-at-load in ~40s; without it the unit reads
   ``active`` between restarts and nothing fires until the start deadline.
3. **start deadline** — never healthy for ``reconcile_start_deadline_seconds``.
4. **health lost** — was healthy, now isn't, sustained past
   ``reconcile_unhealthy_seconds`` so a single missed scrape doesn't flap it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from sqlalchemy import select, update

from .. import db as _db  # late-bound: tests reload app.db
from ..config import get_settings
from ..models import (
    INST_ERROR,
    INST_RUNNING,
    INST_STARTING,
    Instance,
)
from . import inst_state

log = logging.getLogger("spark.reconcile")

__all__ = ["Observation", "Verdict", "decide", "Reconciler", "reconciler"]


@dataclass
class Observation:
    """What the probes say about one instance, right now."""

    instance_id: int
    name: str
    status: str  # as recorded in the DB at probe time
    systemd_active: bool | None  # None = could not determine (SSH failed)
    health_ok: bool | None  # None = no endpoint to probe
    n_restarts: int | None
    node_reachable: bool | None  # None = unknown
    started_at: float | None  # epoch seconds; anchor for the start deadline
    last_healthy_at: float | None  # epoch seconds


@dataclass
class Verdict:
    """A proposed status change, plus why (surfaced to the operator)."""

    instance_id: int
    from_status: str
    to_status: str
    reason: str
    healthy_now: bool = False


@dataclass
class _Pending:
    """Sustain timers — a negative signal must persist to count."""

    unit_dead_since: float | None = None
    unhealthy_since: float | None = None
    first_seen: float | None = None
    restarts_at_start: int | None = None


def decide(
    obs: Observation,
    pending: _Pending,
    now: float,
    *,
    start_deadline: float,
    unhealthy_after: float,
    unit_dead_after: float,
    crashloop_restarts: int,
) -> Verdict | None:
    """Pure transition logic. ``pending`` is mutated to carry sustain timers
    across ticks; the caller owns one per instance.

    Returns a Verdict to apply, or None to leave the row alone.
    """
    if pending.first_seen is None:
        pending.first_seen = now

    # A node we cannot reach tells us nothing about the instance on it — the
    # portal's own network could be the broken part. Never demote on that.
    if obs.node_reachable is False:
        pending.unit_dead_since = None
        pending.unhealthy_since = None
        return None

    healthy = obs.health_ok is True

    # --- positive evidence, checked first --------------------------------
    # A 200 from /health is proof the model is servable, whatever systemd says
    # (a flaky `systemctl` must never discard a green health check).
    if healthy:
        pending.unit_dead_since = None
        pending.unhealthy_since = None
        # Only promote instances the observer is responsible for: one that is
        # loading (the normal path) or one that previously failed and has since
        # recovered. A *stopped* instance whose /health still answers is a
        # shutdown still draining — resurrecting it would fight the operator and
        # put a model the operator took down back into gateway rotation.
        if obs.status in (INST_STARTING, INST_ERROR):
            return Verdict(
                obs.instance_id,
                obs.status,
                INST_RUNNING,
                "/health is green",
                healthy_now=True,
            )
        return None

    # --- negative evidence ------------------------------------------------
    unit_dead = obs.systemd_active is False
    pending.unit_dead_since = (
        (pending.unit_dead_since or now) if unit_dead else None
    )
    if obs.status == INST_RUNNING:
        pending.unhealthy_since = pending.unhealthy_since or now

    if obs.status not in (INST_STARTING, INST_RUNNING):
        return None

    # 1. The unit itself is down, and stayed down.
    if pending.unit_dead_since is not None and now - pending.unit_dead_since >= unit_dead_after:
        return Verdict(
            obs.instance_id,
            obs.status,
            INST_ERROR,
            "the systemd unit is not running",
        )

    ever_healthy = obs.last_healthy_at is not None

    # 2. Crash loop: restarts climbing while it has never once served.
    if not ever_healthy and obs.n_restarts is not None:
        if pending.restarts_at_start is None:
            pending.restarts_at_start = obs.n_restarts
        delta = obs.n_restarts - pending.restarts_at_start
        if delta >= crashloop_restarts:
            return Verdict(
                obs.instance_id,
                obs.status,
                INST_ERROR,
                f"restarted {delta} times without ever becoming healthy "
                f"(crash loop — check the vLLM logs; out-of-memory at load is the usual cause)",
            )

    # 3. Never healthy, past the start deadline.
    if not ever_healthy:
        anchor = obs.started_at or pending.first_seen
        if anchor is not None and now - anchor >= start_deadline:
            mins = int((now - anchor) // 60)
            return Verdict(
                obs.instance_id,
                obs.status,
                INST_ERROR,
                f"never became healthy within {mins} minutes of starting",
            )
        return None

    # 4. Was healthy, now isn't, for long enough to not be a blip.
    if obs.status == INST_RUNNING:
        since = pending.unhealthy_since or now
        if now - since >= unhealthy_after:
            return Verdict(
                obs.instance_id,
                obs.status,
                INST_ERROR,
                "stopped responding to /health",
            )
    return None


class Reconciler:
    """Background loop applying :func:`decide` to the telemetry probes."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._pending: dict[int, _Pending] = {}

    def start(self) -> None:
        if not get_settings().reconcile_enabled:
            log.info("status reconciliation disabled (SPARK_RECONCILE_ENABLED=false)")
            return
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

    async def _loop(self) -> None:
        settings = get_settings()
        interval = max(2.0, settings.reconcile_tick_seconds)
        while not self._stopping:
            started = time.time()
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a bad tick must not kill the loop
                log.exception("status reconcile tick failed")
            await asyncio.sleep(max(1.0, interval - (time.time() - started)))

    async def tick(self, now: float | None = None) -> list[Verdict]:
        """One pass. Returns the verdicts actually applied (for tests)."""
        from .telemetry import engine  # local: telemetry imports services widely

        now = time.time() if now is None else now
        settings = get_settings()
        slow = engine.slow_cache()
        if slow is None:
            return []
        probes = {p.instance_id: p for p in slow.instances}
        if not probes:
            return []

        applied: list[Verdict] = []
        async with _db.SessionLocal() as session:
            rows = list(
                (
                    await session.execute(
                        select(Instance).where(Instance.id.in_(probes.keys()))
                    )
                )
                .scalars()
                .all()
            )
            for inst in rows:
                probe = probes[inst.id]
                # A start/stop job owns the row while it runs; the job's own
                # commits are the truth and the observer must not race them.
                if inst_state.in_flight(inst.id) is not None:
                    self._pending.pop(inst.id, None)
                    continue
                pending = self._pending.setdefault(inst.id, _Pending())
                obs = Observation(
                    instance_id=inst.id,
                    name=inst.name,
                    status=inst.status,
                    systemd_active=probe.systemd_active,
                    health_ok=probe.health_ok,
                    n_restarts=getattr(probe, "n_restarts", None),
                    node_reachable=engine.node_reachable(probe.node_id)
                    if getattr(probe, "node_id", None) is not None
                    else None,
                    started_at=inst_state.epoch(inst.started_at),
                    last_healthy_at=inst_state.epoch(inst.last_healthy_at),
                )
                verdict = decide(
                    obs,
                    pending,
                    now,
                    start_deadline=settings.reconcile_start_deadline_seconds,
                    unhealthy_after=settings.reconcile_unhealthy_seconds,
                    unit_dead_after=settings.reconcile_unit_dead_seconds,
                    crashloop_restarts=settings.reconcile_crashloop_restarts,
                )
                if verdict is None:
                    continue
                if await self._apply(session, verdict, now):
                    applied.append(verdict)
                    self._pending.pop(inst.id, None)
            await session.commit()
        for v in applied:
            log.info(
                "instance %s: %s -> %s (%s)", v.instance_id, v.from_status, v.to_status, v.reason
            )
        return applied

    async def _apply(self, session, verdict: Verdict, now: float) -> bool:
        """Compare-and-set: only write if the row still holds the status we
        observed. A job that moved it underneath us wins, and the verdict is
        dropped rather than clobbering a fresher truth."""
        values: dict = {"status": verdict.to_status}
        if verdict.healthy_now:
            values["last_healthy_at"] = inst_state.utcnow()
            values["last_error"] = None
        else:
            values["last_error"] = f"Reconciled to {verdict.to_status}: {verdict.reason}"
        res = await session.execute(
            update(Instance)
            .where(Instance.id == verdict.instance_id, Instance.status == verdict.from_status)
            .values(**values)
        )
        return bool(res.rowcount)


reconciler = Reconciler()
