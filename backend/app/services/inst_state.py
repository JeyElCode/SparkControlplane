"""Instance lifecycle helpers shared by the start/stop jobs, the routers and
the status observer.

Deliberately a leaf module (imports nothing from ``services``): ``instances.py``
and ``reconcile.py`` both need it, and reconcile imports telemetry, so anything
richer would be an import cycle.

The contract it encodes: **jobs propose, the observer confirms.** A start job
moves an instance to ``starting`` and installs the unit; only
:mod:`app.services.reconcile` promotes it to ``running``, and only on proof (a
200 from ``/health``). While a job is in flight it *owns* the row, and the
observer stays out of the way.
"""

from __future__ import annotations

from datetime import datetime, timezone

__all__ = ["utcnow", "epoch", "register", "release", "in_flight", "clear_all"]


def utcnow() -> datetime:
    """Aware UTC, matching ``models._utcnow`` (created_at/updated_at)."""
    return datetime.now(timezone.utc)


def epoch(dt: datetime | None) -> float | None:
    """Seconds since the epoch for a stored timestamp.

    Values written through an aware datetime come back from SQLite naive, so
    assume UTC when tzinfo is missing rather than letting ``.timestamp()``
    silently reinterpret it as local time — on a CET host that would shift
    every deadline by an hour or two.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


# --- in-flight job registry ------------------------------------------------
# instance_id -> (action, job_id). Process-local and intentionally so: it guards
# against *this* portal double-acting on an instance. Cross-process safety comes
# from the observer's compare-and-set writes, not from here.
_IN_FLIGHT: dict[int, tuple[str, int]] = {}


def register(instance_id: int, action: str, job_id: int) -> None:
    _IN_FLIGHT[instance_id] = (action, job_id)


def release(instance_id: int) -> None:
    _IN_FLIGHT.pop(instance_id, None)


def in_flight(instance_id: int) -> tuple[str, int] | None:
    """(action, job_id) when a start/stop job currently owns this instance."""
    return _IN_FLIGHT.get(instance_id)


def clear_all() -> None:
    """Test helper — the registry is module state that would otherwise leak
    between tests."""
    _IN_FLIGHT.clear()
