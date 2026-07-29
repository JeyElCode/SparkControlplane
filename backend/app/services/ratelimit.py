"""Per-client limits for the /v1 gateway.

The system could already *detect* a runaway client — the ``kv_cache_full`` alert
fires at 95% — but had no lever to do anything about it. One looping script or
an agent gone wild could saturate the KV cache and starve every other consumer.

**Concurrency is the load-bearing limit, not requests-per-minute.** vLLM's
continuous batching means the contended resource is KV-cache blocks held by
sequences currently in the batch, and a client's in-flight request count is, to
a first approximation, its share of exactly that. It is also self-clocking:
capped at N, a client can never hold more than N sequences however long each
runs. RPM and tokens/min both let a client accumulate unbounded resident state
as long as it arrives slowly enough. RPM is kept as a cheap secondary guard
against connect storms; token limits are accounted for reporting but not
enforced, because throttling mid-generation is worse than useless.

Concurrency is scoped per **(client, instance)**: KV cache is per-instance, so a
client running two requests against a chat model and two against an embedding
model is not hurting either one, and a global cap would halve its throughput for
no protective gain.

State is a process-local singleton — no database access on the hot path. That
assumes a single portal process, which is how this ships (one container, one
uvicorn). If the portal is ever replicated, these limits become per-replica and
the accounting would need to move to shared state; that is called out in the
docs rather than pre-solved.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass

log = logging.getLogger("spark.ratelimit")

__all__ = ["LimitError", "Limiter", "limiter"]


class LimitError(Exception):
    """Raised when a request must be rejected. Carries what the caller needs to
    understand *which* limit they hit and when to come back."""

    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after = retry_after


@dataclass
class _Limits:
    max_concurrent: int | None = None  # None/0 = unlimited
    max_rpm: int | None = None


class Limiter:
    def __init__(self) -> None:
        # (client, instance_id) -> in-flight count
        self._inflight: dict[tuple[str, int], int] = defaultdict(int)
        # client -> deque of recent request start times (for RPM)
        self._recent: dict[str, deque[float]] = defaultdict(deque)

    # --- introspection (for the UI / tests) ------------------------------
    def inflight(self, client: str) -> int:
        return sum(n for (c, _), n in self._inflight.items() if c == client)

    def snapshot(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for (client, _), n in self._inflight.items():
            if n:
                out[client] += n
        return dict(out)

    def reset(self) -> None:
        self._inflight.clear()
        self._recent.clear()

    # --- enforcement ------------------------------------------------------
    def check_rate(self, client: str, max_rpm: int | None, now: float | None = None) -> None:
        """Sliding-window RPM check. Raises LimitError when over."""
        if not max_rpm:
            return
        now = time.time() if now is None else now
        window = self._recent[client]
        cutoff = now - 60.0
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= max_rpm:
            oldest = window[0]
            retry = max(1, int(oldest + 60.0 - now) + 1)
            raise LimitError(
                f"Rate limit exceeded: {max_rpm} requests/min for '{client}'.",
                retry,
            )
        window.append(now)

    def acquire(self, client: str, instance_id: int, max_concurrent: int | None) -> bool:
        """Take a concurrency slot. Returns False when the client is at its cap.

        Callers MUST pair a True return with exactly one :meth:`release` — see
        the gateway's stream cleanup, which is the only correct place for it.
        """
        if not max_concurrent:
            self._inflight[(client, instance_id)] += 1
            return True
        key = (client, instance_id)
        if self._inflight[key] >= max_concurrent:
            return False
        self._inflight[key] += 1
        return True

    def release(self, client: str, instance_id: int) -> None:
        """Free a slot. Idempotent below zero: a double release must never make
        the counter negative (which would silently raise the effective cap)."""
        key = (client, instance_id)
        if self._inflight.get(key):
            self._inflight[key] -= 1
            if not self._inflight[key]:
                del self._inflight[key]


limiter = Limiter()
