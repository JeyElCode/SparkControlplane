"""Instance scheduling: start/stop models on weekly live-windows.

Motivation: unified memory is the scarce resource — with ~119 GiB per Spark you
can't keep every model resident, so models get time slots (coding model during
work hours, batch model overnight, …).

Semantics are **edge-triggered**: the scheduler acts when a window OPENS
(start) or CLOSES (stop) — a manual start/stop between edges is respected, so
the scheduler never fights the operator. On portal boot it reconciles once to
the current desired state (a restart mid-window still brings the model up).
Instances with no schedules are untouched.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .. import db as _db
from ..config import get_settings
from ..models import (
    INST_ERROR,
    INST_RUNNING,
    INST_STARTING,
    INST_STOPPED,
    InstanceSchedule,
)
from .jobs import jobs

log = logging.getLogger("spark.scheduler")


def parse_days(csv: str) -> set[int]:
    out = set()
    for part in csv.split(","):
        part = part.strip()
        if part.isdigit() and 0 <= int(part) <= 6:
            out.add(int(part))
    return out


def _minutes(hhmm: str) -> int:
    h, _, m = hhmm.partition(":")
    return int(h) * 60 + int(m)


def window_active(days: set[int], start: str, end: str, now: datetime) -> bool:
    """Is a weekly window live at ``now``? ``end <= start`` wraps past midnight
    (the window belongs to the day it STARTS on)."""
    cur = now.hour * 60 + now.minute
    s, e = _minutes(start), _minutes(end)
    today, yesterday = now.weekday(), (now.weekday() - 1) % 7
    if s < e:  # same-day window
        return today in days and s <= cur < e
    # overnight: today's tail (from s) or the spillover of yesterday's window
    if today in days and cur >= s:
        return True
    return yesterday in days and cur < e


def desired_state(schedules: list[InstanceSchedule], now: datetime) -> bool:
    """True when any enabled window is live."""
    return any(
        window_active(parse_days(s.days), s.start_time, s.end_time, now)
        for s in schedules
        if s.enabled
    )


def decide(prev: bool | None, desired: bool, status: str) -> str | None:
    """Pure transition logic -> "start" | "stop" | None.

    ``prev is None`` = first evaluation after boot: reconcile to desired.
    Otherwise only act on a desired-state EDGE, so manual overrides stick
    until the next scheduled transition.
    """
    if prev is None or desired != prev:
        if desired and status in (INST_STOPPED, INST_ERROR):
            return "start"
        if not desired and status in (INST_RUNNING, INST_STARTING):
            return "stop"
    return None


def next_window_open(schedules: list[InstanceSchedule], now: datetime) -> datetime | None:
    """Earliest future window-open time within the next week (for 'model not
    live right now' messages), or None if nothing is scheduled."""
    from datetime import timedelta

    best: datetime | None = None
    for s in schedules:
        if not s.enabled:
            continue
        days = parse_days(s.days)
        h, _, m = s.start_time.partition(":")
        for offset in range(8):
            d = now + timedelta(days=offset)
            if d.weekday() not in days:
                continue
            cand = d.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
            if cand > now and (best is None or cand < best):
                best = cand
    return best


def now_tz() -> datetime:
    settings = get_settings()
    if settings.schedule_tz:
        try:
            return datetime.now(ZoneInfo(settings.schedule_tz))
        except KeyError:
            log.warning("Invalid SPARK_SCHEDULE_TZ %r — using system time", settings.schedule_tz)
    return datetime.now().astimezone()


import os

RETRY_SECONDS = float(os.environ.get("SPARK_SCHEDULE_RETRY_SECONDS", "120"))
MAX_ATTEMPTS = 5


class Scheduler:
    def __init__(self) -> None:
        self._last_desired: dict[int, bool] = {}
        # actions issued but not yet observed to have reached their target
        # state: instance_id -> (action, last_attempt_ts, attempts). Retried
        # with backoff so a transiently-failed job doesn't eat the edge; a
        # SUCCESSFUL action clears immediately, so manual overrides afterwards
        # are still respected.
        self._pending: dict[int, tuple[str, float, int]] = {}
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
        interval = max(5.0, settings.schedule_tick_seconds)
        while not self._stopping:
            started = time.time()
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("scheduler tick failed")
            await asyncio.sleep(max(1.0, interval - (time.time() - started)))

    async def tick(self) -> list[tuple[int, str]]:
        """Evaluate all schedules once; returns [(instance_id, action)] taken."""
        from . import instances as inst_svc

        now = now_tz()
        actions: list[tuple[int, str]] = []
        async with _db.SessionLocal() as session:
            rows = list(
                (
                    await session.execute(
                        select(InstanceSchedule).options(
                            selectinload(InstanceSchedule.instance)
                        )
                    )
                )
                .scalars()
                .all()
            )
            by_instance: dict[int, list[InstanceSchedule]] = {}
            for s in rows:
                if s.instance is not None:
                    by_instance.setdefault(s.instance_id, []).append(s)

            for gone in set(self._last_desired) - by_instance.keys():
                del self._last_desired[gone]
                self._pending.pop(gone, None)

            for iid, scheds in by_instance.items():
                inst = scheds[0].instance
                desired = desired_state(scheds, now)
                action = decide(self._last_desired.get(iid), desired, inst.status)
                self._last_desired[iid] = desired
                if action is None:
                    action = self._retry_action(iid, desired, inst.status)
                    if action is None:
                        continue
                else:
                    self._pending[iid] = (action, time.time(), 1)
                actions.append((iid, action))
                name = inst.name
                log.info("schedule %s: %s (window %s)", action, name, "open" if desired else "closed")

                def _coro(iid_: int, act: str):
                    async def run(h):
                        async with _db.SessionLocal() as s:
                            if act == "start":
                                return await inst_svc.start_instance(s, h, iid_)
                            return await inst_svc.stop_instance(s, h, iid_)

                    return run

                await jobs.start(
                    f"schedule.{action}", f"Scheduled {action}: {name}",
                    _coro(iid, action), target=name,
                )
        return actions

    def _retry_action(self, iid: int, desired: bool, status: str) -> str | None:
        """Re-issue a pending action whose job evidently failed (the instance
        never reached the target state). Cleared on success or when the window
        flips, so a deliberate manual override is never fought."""
        pending = self._pending.get(iid)
        if pending is None:
            return None
        act, ts, attempts = pending
        reached = (act == "start" and status == INST_RUNNING) or (
            act == "stop" and status == INST_STOPPED
        )
        if reached or desired != (act == "start"):
            del self._pending[iid]
            return None
        if time.time() - ts < RETRY_SECONDS:
            return None
        if attempts >= MAX_ATTEMPTS:
            log.warning("schedule %s for instance %d gave up after %d attempts", act, iid, attempts)
            del self._pending[iid]
            return None
        startable = status in (INST_STOPPED, INST_ERROR)
        stoppable = status in (INST_RUNNING, INST_STARTING, INST_ERROR)
        if (act == "start" and not startable) or (act == "stop" and not stoppable):
            return None  # a job is still in flight (starting/stopping) — wait
        self._pending[iid] = (act, time.time(), attempts + 1)
        log.info("schedule retry %d/%d: %s instance %d", attempts + 1, MAX_ATTEMPTS, act, iid)
        return act


scheduler = Scheduler()
