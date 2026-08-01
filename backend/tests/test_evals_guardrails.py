"""Guardrails on the eval subsystem, and the predictability ladder.

The eval subsystem had no tests at all — 1,532 lines across five modules and
not one of the 34 test files touched it. These are the first, and they cover
the three things that were actually broken rather than the easy surface.
"""

from __future__ import annotations

import importlib

import pytest

from app.services.eval_suites import PERF_TASKS, SPEED_LADDER, perf_categories, perf_tasks


# --- load knobs ----------------------------------------------------------

def test_concurrency_is_bounded():
    """`{"concurrency": [512], "perf_reps": 50}` was accepted, and meant 512
    concurrent streams from a single-replica portal against a LIVE serving
    instance on hardware with no out-of-band recovery."""
    from app.schemas import EvalRunRequest

    with pytest.raises(ValueError, match="out of range"):
        EvalRunRequest(instance_id=1, concurrency=[512])
    with pytest.raises(ValueError):
        EvalRunRequest(instance_id=1, perf_reps=50)


def test_concurrency_rejects_empty_and_overlong_and_zero():
    from app.schemas import EvalRunRequest

    with pytest.raises(ValueError, match="at least one"):
        EvalRunRequest(instance_id=1, concurrency=[])
    with pytest.raises(ValueError, match="at most 8"):
        EvalRunRequest(instance_id=1, concurrency=[1, 2, 3, 4, 5, 6, 7, 8, 9])
    with pytest.raises(ValueError, match="out of range"):
        EvalRunRequest(instance_id=1, concurrency=[0])


def test_sensible_values_still_pass():
    """The bound must not break ordinary use — a guardrail that blocks real
    work gets removed."""
    from app.schemas import EvalRunRequest

    r = EvalRunRequest(instance_id=1, concurrency=[1, 2, 4, 8], perf_reps=5)
    assert r.concurrency == [1, 2, 4, 8]
    assert r.perf_reps == 5


# --- the predictability ladder ------------------------------------------

def test_the_ladder_exists_and_is_the_default():
    """One tok/s number hides the speculative-decoding tradeoff: an MTP build
    measured 79/72/35 against 62/65/38 — faster on two regimes, slower on the
    third. Averaged, it looks like a plain win."""
    from app.schemas import EvalRunRequest

    assert SPEED_LADDER == ("predictable", "code", "creative")
    assert EvalRunRequest(instance_id=1).categories == list(SPEED_LADDER)


def test_every_ladder_rung_resolves_to_exactly_one_prompt():
    """A missing prompt would silently drop a regime from the comparison and
    the table would still render, just with a hole where the interesting
    number was."""
    for category in SPEED_LADDER:
        tasks = perf_tasks([category])
        assert len(tasks) == 1, f"{category} resolved to {len(tasks)} prompts"


def test_the_default_request_resolves_to_real_prompts():
    """Guards the rename that broke this once: the schema default said
    'coding' while the prompt category had become 'code', so the default run
    measured nothing at all."""
    from app.schemas import EvalRunRequest

    tasks = perf_tasks(EvalRunRequest(instance_id=1).categories)
    assert [t.id for t in tasks] == ["perf_predictable", "perf_code", "perf_creative"]


def test_perf_categories_constant_matches_the_prompts():
    """models.PERF_CATEGORIES is what the UI offers; a prompt with no entry is
    unreachable, and an entry with no prompt is a dead checkbox."""
    from app.models import PERF_CATEGORIES

    assert set(perf_categories()) == set(PERF_CATEGORIES)


def test_the_predictable_prompt_can_actually_reach_its_target():
    """Counting to 300 needs the token budget to get there — truncating
    mid-sequence would measure the truncation, not the decode rate."""
    task = next(t for t in PERF_TASKS if t.category == "predictable")
    assert "300" in task.prompt
    # ~1.2 tokens per number plus separators; 1200 leaves real headroom.
    assert task.max_tokens >= 1000, "would truncate before 300 and skew tok/s"


def test_ladder_prompts_are_actually_different_regimes():
    """The three prompts must not converge on the same kind of text, or the
    comparison measures nothing."""
    by_cat = {t.category: t.prompt.lower() for t in PERF_TASKS}
    assert "count" in by_cat["predictable"]
    assert "python" in by_cat["code"] or "implementation" in by_cat["code"]
    assert "story" in by_cat["creative"] or "original" in by_cat["creative"]
    assert len({by_cat[c] for c in SPEED_LADDER}) == 3


# --- backup + restart survival -------------------------------------------

def test_eval_history_is_in_the_backup_bundle():
    """Without these a restore silently empties the scores, the trend, and
    anything that would ever gate on them."""
    from app.services.backup import _TABLES

    names = [name for name, _ in _TABLES]
    for table in ("eval_runs", "eval_results", "perf_results"):
        assert table in names, f"{table} missing from the backup bundle"
    # eval_runs.instance_id references instances, so ordering matters on restore.
    assert names.index("instances") < names.index("eval_runs")


async def test_a_restart_does_not_strand_an_eval_as_running(tmp_path, monkeypatch):
    """EvalRun carries its own status column, so the v1.28.1 job sweep missed
    it entirely: the row read `running` forever and the UI hid both View and
    Re-run, leaving it permanently unusable."""
    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    import app.config as config

    config.get_settings.cache_clear()
    import app.db as db

    importlib.reload(db)
    await db.init_db()

    from app.models import JOB_ERROR, JOB_RUNNING, EvalRun
    from app.services.jobs import JobManager

    async with db.SessionLocal() as s:
        s.add(EvalRun(
            name="stranded", model_name="m", instance_label="l", categories="code",
            config_json="{}", status=JOB_RUNNING,
        ))
        await s.commit()

    mgr = JobManager()
    monkeypatch.setattr("app.services.jobs._db", db)
    await mgr.reconcile_orphans()

    async with db.SessionLocal() as s:
        from sqlalchemy import select

        row = (await s.execute(select(EvalRun))).scalars().one()
        assert row.status == JOB_ERROR, "eval run left stranded as running"
        assert row.finished_at is not None

    config.get_settings.cache_clear()
