"""Per-client limits.

The asymmetry that matters: over-throttling is worse than under-throttling here.
Everything defaults to unlimited so an upgrade never starts rejecting traffic
that worked yesterday, and a leaked concurrency slot — which would 429 a client
forever with nothing in the UI explaining why — is the failure this file is
mostly about.
"""

from __future__ import annotations

import pytest

from app.services.ratelimit import LimitError, Limiter


@pytest.fixture()
def lim():
    return Limiter()


# --- concurrency ----------------------------------------------------------
def test_unlimited_by_default(lim):
    for _ in range(50):
        assert lim.acquire("c", 1, None) is True
    assert lim.inflight("c") == 50


def test_cap_blocks_the_next_request(lim):
    assert lim.acquire("c", 1, 2) is True
    assert lim.acquire("c", 1, 2) is True
    assert lim.acquire("c", 1, 2) is False, "third request must be refused at cap 2"
    lim.release("c", 1)
    assert lim.acquire("c", 1, 2) is True, "a freed slot must be reusable"


def test_concurrency_is_scoped_per_instance(lim):
    """KV cache is per-instance: a client using two models is not hurting
    either one, and a global cap would halve its throughput for no gain."""
    assert lim.acquire("c", instance_id=1, max_concurrent=1) is True
    assert lim.acquire("c", instance_id=2, max_concurrent=1) is True
    assert lim.acquire("c", instance_id=1, max_concurrent=1) is False


def test_clients_do_not_share_a_budget(lim):
    assert lim.acquire("a", 1, 1) is True
    assert lim.acquire("b", 1, 1) is True
    assert lim.acquire("a", 1, 1) is False


def test_double_release_cannot_raise_the_effective_cap(lim):
    """A release that runs twice must not push the counter negative — that
    would silently let the client exceed its cap."""
    lim.acquire("c", 1, 1)
    lim.release("c", 1)
    lim.release("c", 1)
    lim.release("c", 1)
    assert lim.inflight("c") == 0
    assert lim.acquire("c", 1, 1) is True
    assert lim.acquire("c", 1, 1) is False


def test_release_without_acquire_is_harmless(lim):
    lim.release("never-seen", 99)
    assert lim.inflight("never-seen") == 0


def test_snapshot_sums_across_instances(lim):
    lim.acquire("c", 1, None)
    lim.acquire("c", 2, None)
    lim.acquire("other", 1, None)
    assert lim.snapshot() == {"c": 2, "other": 1}


# --- requests per minute --------------------------------------------------
def test_rpm_allows_up_to_the_limit_then_rejects(lim):
    for i in range(5):
        lim.check_rate("c", 5, now=1000.0 + i)
    with pytest.raises(LimitError) as exc:
        lim.check_rate("c", 5, now=1005.0)
    assert exc.value.retry_after >= 1
    assert "5 requests/min" in exc.value.message


def test_rpm_window_slides(lim):
    for i in range(5):
        lim.check_rate("c", 5, now=1000.0 + i)
    # a minute later the early requests have aged out
    lim.check_rate("c", 5, now=1061.0)


def test_rpm_unlimited_when_unset(lim):
    for i in range(1000):
        lim.check_rate("c", None, now=1000.0 + i * 0.001)


def test_retry_after_points_past_the_oldest_request(lim):
    lim.check_rate("c", 1, now=1000.0)
    with pytest.raises(LimitError) as exc:
        lim.check_rate("c", 1, now=1030.0)
    # oldest was at 1000, so the window frees at 1060 -> ~30s away
    assert 25 <= exc.value.retry_after <= 35
