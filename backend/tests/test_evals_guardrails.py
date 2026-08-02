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


async def test_a_bundle_can_be_restored_after_an_eval_has_run(tmp_path, monkeypatch):
    """`jobs` is deliberately not in the bundle, but eval_runs.job_id and
    model_node_states.last_job_id are foreign keys into it — and SQLite runs
    with PRAGMA foreign_keys=ON. Carrying those ids aborts the ENTIRE restore
    with "FOREIGN KEY constraint failed", so a backup taken after any eval had
    run could not be restored at all."""
    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    import app.config as config

    config.get_settings.cache_clear()
    import app.db as db

    importlib.reload(db)
    await db.init_db()

    import app.services.backup as backup_mod

    importlib.reload(backup_mod)
    monkeypatch.setattr(backup_mod, "_db", db)

    from app.models import Job, EvalRun

    async with db.SessionLocal() as s:
        job = Job(type="eval", title="j", status="success")
        s.add(job)
        await s.flush()
        s.add(EvalRun(
            name="r", model_name="m", instance_label="l", categories="code",
            config_json="{}", status="success", job_id=job.id,
        ))
        await s.commit()

    bundle = await backup_mod.build_bundle()
    run_rows = bundle["tables"]["eval_runs"]
    assert run_rows and run_rows[0]["job_id"] is None, "dangling FK exported"

    # The real proof: restoring must not raise. Jobs are absent by design.
    await backup_mod.apply_bundle(bundle)

    async with db.SessionLocal() as s:
        from sqlalchemy import select

        assert len((await s.execute(select(EvalRun))).scalars().all()) == 1

    config.get_settings.cache_clear()


def test_the_speed_benchmark_is_exactly_the_ladder():
    """No prompts beyond the three regimes.

    Three prompts from the previous system (reasoning, textgen, judging)
    survived the rewrite and shipped as equal peers in the category picker,
    which is not what was asked for and makes the default ambiguous. The speed
    benchmark is the ladder; anything else is a different feature.
    """
    assert set(perf_categories()) == set(SPEED_LADDER)
    assert len(PERF_TASKS) == len(SPEED_LADDER)


def test_no_quality_scenarios_are_authored_here():
    """The 69 tool-calling scenarios come from tool-eval-bench, pinned. If a
    scenario were ever defined in this repo it would silently stop the scores
    being comparable to the published ones, which is the whole reason for
    depending on the suite."""
    import pathlib

    services = pathlib.Path(__file__).resolve().parents[1] / "app" / "services"
    for path in services.glob("*.py"):
        body = "\n".join(
            ln for ln in path.read_text().splitlines()
            if not ln.lstrip().startswith("#")
        )
        assert "ScenarioDefinition" not in body
        # A TC-nn literal is only legitimate inside the id-shape regex.
        if "TC-" in body and path.name != "toolbench.py":
            raise AssertionError(f"{path.name} appears to define benchmark scenarios")


# --- cross-version restore ------------------------------------------------

async def test_a_bundle_that_predates_a_table_does_not_empty_it(tmp_path, monkeypatch):
    """The data-loss bug, in the direction that bit first.

    `apply_bundle` cleared every table it manages before reinserting. A bundle
    made before v1.32.0 has no `eval_runs` key at all, so the table was emptied
    and "restored" as zero rows — reported as a successful restore. Restoring
    nothing over something is never what the operator meant.

    A missing key is distinguishable from an empty one: `build_bundle` writes a
    key for every table it manages, so `[]` means "no rows" and absence means
    "the producing version did not know this table".
    """
    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    import app.config as config

    config.get_settings.cache_clear()
    import app.db as db

    importlib.reload(db)
    await db.init_db()

    import app.services.backup as backup_mod

    importlib.reload(backup_mod)
    monkeypatch.setattr(backup_mod, "_db", db)

    from sqlalchemy import select

    from app.models import EvalRun

    async with db.SessionLocal() as s:
        s.add(EvalRun(name="historical", model_name="m", instance_label="l",
                      categories="code", config_json="{}", status="success"))
        await s.commit()

    bundle = await backup_mod.build_bundle()
    # An older producer: the key simply is not there.
    del bundle["tables"]["eval_runs"]

    result = await backup_mod.apply_bundle(bundle)

    async with db.SessionLocal() as s:
        rows = (await s.execute(select(EvalRun))).scalars().all()
        assert len(rows) == 1, "the restore emptied a table the bundle never covered"
        assert rows[0].name == "historical"

    assert "eval_runs" in result["not_in_bundle"], "the omission must be reported"
    config.get_settings.cache_clear()


async def test_an_empty_list_still_means_empty(tmp_path, monkeypatch):
    """The other half of the distinction. A table the bundle says is empty MUST
    be emptied, or a restore could never remove anything."""
    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    import app.config as config

    config.get_settings.cache_clear()
    import app.db as db

    importlib.reload(db)
    await db.init_db()

    import app.services.backup as backup_mod

    importlib.reload(backup_mod)
    monkeypatch.setattr(backup_mod, "_db", db)

    from sqlalchemy import select

    from app.models import EvalRun

    async with db.SessionLocal() as s:
        s.add(EvalRun(name="doomed", model_name="m", instance_label="l",
                      categories="code", config_json="{}", status="success"))
        await s.commit()

    bundle = await backup_mod.build_bundle()
    bundle["tables"]["eval_runs"] = []   # explicitly: there were no rows

    result = await backup_mod.apply_bundle(bundle)

    async with db.SessionLocal() as s:
        assert (await s.execute(select(EvalRun))).scalars().all() == []
    assert "eval_runs" not in result["not_in_bundle"]
    config.get_settings.cache_clear()


async def test_a_newer_bundle_does_not_empty_a_table_only_the_old_build_knows(
    tmp_path, monkeypatch
):
    """The mirror case, which matters because rollback is the only escape
    hatch: a v1.33.x bundle has no `custom_tasks`, and restoring it on an older
    build used to wipe them."""
    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    import app.config as config

    config.get_settings.cache_clear()
    import app.db as db

    importlib.reload(db)
    await db.init_db()

    import app.services.backup as backup_mod

    importlib.reload(backup_mod)
    monkeypatch.setattr(backup_mod, "_db", db)

    from sqlalchemy import select

    from app.models import Node

    async with db.SessionLocal() as s:
        s.add(Node(role="head", name="keepme", lan_ip="10.0.0.1",
                   qsfp_ip="10.10.10.1", ssh_user="u"))
        await s.commit()

    bundle = await backup_mod.build_bundle()
    del bundle["tables"]["nodes"]        # a table this build manages, bundle does not

    result = await backup_mod.apply_bundle(bundle)

    async with db.SessionLocal() as s:
        names = [n.name for n in (await s.execute(select(Node))).scalars().all()]
        assert names == ["keepme"], "a table absent from the bundle was emptied"
    assert "nodes" in result["not_in_bundle"]
    config.get_settings.cache_clear()


def test_the_bundle_version_was_bumped():
    """The table set changed in both directions; the version records which
    producer a bundle came from so a support question has an answer."""
    from app.services.backup import BUNDLE_VERSION

    assert BUNDLE_VERSION >= 2
