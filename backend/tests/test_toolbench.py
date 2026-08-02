"""Parsing tool-eval-bench output.

Every fixture here is the shape the real CLI actually emits, established by
running it against a mock OpenAI server rather than by reading the README —
which is wrong about three things that matter (see the module docstring in
services/toolbench.py).

The most important test in this file is
``test_a_degraded_endpoint_scores_high_and_we_catch_it``. Infrastructure
failures leave the DENOMINATOR and the exit code stays 0, so the suite's
headline number goes UP as the endpoint gets worse. Anything that displays or
gates on a score without completion_rate beside it is lying.
"""

from __future__ import annotations

import json

import pytest

from app.services import toolbench


def envelope(**over) -> dict:
    scores = {
        "final_score": 93.0,
        "total_points": 28,
        "max_points": 30,
        "rating": "★★★★★",
        "category_scores": [
            {"category": "A", "label": "Direct match", "earned": 6, "max": 6, "percent": 100.0},
            {"category": "K", "label": "Safety", "earned": 2, "max": 4, "percent": 50.0},
        ],
        "scenario_results": [
            {"scenario_id": "TC-01", "status": "pass", "points": 2,
             "summary": "Direct specialist match", "duration_seconds": 1.2},
            {"scenario_id": "TC-11", "status": "partial", "points": 1,
             "summary": "Simple math", "duration_seconds": 0.9},
            {"scenario_id": "TC-14", "status": "fail", "points": 0,
             "summary": "Malformed response", "failure_kind": "wrong_args"},
        ],
    }
    scores.update(over.pop("scores", {}))
    doc = {
        "schema_version": "1",
        "tool_eval_bench_version": "0+5df1e9e0",
        "final_score": scores["final_score"],
        "rating": scores.get("rating"),
        "total_scenarios": 15,
        "config": {"config_fingerprint": "abc123"},
        "scores": scores,
    }
    doc.update(over)
    return doc


STDERR_EVENTS = [
    {"event": "scenario_start", "scenario_id": "TC-01", "category": "A", "index": 0, "total": 3},
    {"event": "scenario_start", "scenario_id": "TC-11", "category": "F", "index": 1, "total": 3},
    {"event": "scenario_start", "scenario_id": "TC-14", "category": "K", "index": 2, "total": 3},
    {"event": "benchmark_complete", "json_file": "out.json", "final_score": 93.0},
]


# --- the happy path ------------------------------------------------------

def test_parses_a_normal_run():
    r = toolbench.parse_envelope(envelope(), STDERR_EVENTS)
    assert r.ok
    assert r.composite_score == 93.0
    assert r.rating == "★★★★★"
    assert r.completion_rate == 100.0
    assert r.trustworthy
    assert len(r.scenarios) == 3
    assert r.category_scores == {"A": 100.0, "K": 50.0}
    assert r.version == "0+5df1e9e0"
    assert r.config_fingerprint == "abc123"


def test_category_comes_from_the_stderr_stream():
    """`ScenarioResult` has no category attribute — the document genuinely does
    not carry it, which is why the runner reads both streams."""
    r = toolbench.parse_envelope(envelope(), STDERR_EVENTS)
    assert {s.scenario_id: s.category for s in r.scenarios} == {
        "TC-01": "A", "TC-11": "F", "TC-14": "K",
    }
    # Without the stderr events there is simply no category to report — and it
    # must degrade to None rather than inventing one.
    bare = toolbench.parse_envelope(envelope(), [])
    assert all(s.category is None for s in bare.scenarios)


def test_points_map_onto_the_existing_zero_to_one_scale():
    r = toolbench.parse_envelope(envelope(), STDERR_EVENTS)
    by_id = {s.scenario_id: s for s in r.scenarios}
    assert (by_id["TC-01"].points, by_id["TC-01"].status) == (2, "pass")
    assert (by_id["TC-11"].points, by_id["TC-11"].status) == (1, "partial")
    assert (by_id["TC-14"].points, by_id["TC-14"].status) == (0, "fail")
    assert by_id["TC-14"].failure_kind == "wrong_args"


# --- the trap ------------------------------------------------------------

def test_a_degraded_endpoint_scores_high_and_we_catch_it():
    """THE failure mode of this suite.

    Measured against a server failing every second request: max_points fell
    30 -> 14, completion_rate to 46.7, exit code stayed 0 — and final_score
    read normally, because the excluded scenarios left the denominator. A
    nearly-broken instance produces a HIGH number.
    """
    doc = envelope(scores={
        "final_score": 100.0, "max_points": 14, "total_points": 14,
        "completion_rate": 46.7,
        "excluded_scenarios": ["TC-01", "TC-03", "TC-05", "TC-07"],
    })
    r = toolbench.parse_envelope(doc, STDERR_EVENTS)

    assert r.ok, "it parses — the run did complete"
    assert r.composite_score == 100.0, "and the score is a perfect 100"
    assert not r.trustworthy, "but it must not be presented as a real result"
    assert r.completion_rate == 46.7
    assert len(r.excluded_scenarios) == 4
    assert "only 47% of scenarios graded" in r.summary_line()


def test_excluded_scenarios_are_marked_on_the_rows():
    doc = envelope(scores={"completion_rate": 80.0, "excluded_scenarios": ["TC-14"]})
    r = toolbench.parse_envelope(doc, STDERR_EVENTS)
    assert {s.scenario_id for s in r.scenarios if s.excluded} == {"TC-14"}


def test_a_complete_run_is_trustworthy():
    assert toolbench.parse_envelope(envelope(), STDERR_EVENTS).trustworthy


# --- failing loudly ------------------------------------------------------

def test_the_error_envelope_has_no_scores_and_must_not_read_as_zero():
    """The error envelope carries `final_score: null` and no `scores` key.
    Coercing that to 0 would record a real-looking bad result for a run that
    never happened."""
    r = toolbench.parse_envelope({
        "schema_version": "1", "tool_eval_bench_version": "x",
        "final_score": None, "error": "Invalid --reference-date 'NOTADATE'.",
    })
    assert not r.ok
    assert r.composite_score is None
    assert "reference-date" in r.error


def test_a_missing_scores_section_fails_loudly():
    r = toolbench.parse_envelope({"schema_version": "1", "final_score": 90})
    assert not r.ok and "scores" in r.error


def test_a_non_numeric_score_fails_loudly():
    """Anything that moves the number fails the run rather than degrading —
    an upstream schema change must not silently alter a trend line."""
    r = toolbench.parse_envelope(envelope(scores={"final_score": "ninety"}))
    assert not r.ok and "not a number" in r.error


@pytest.mark.parametrize("junk", [None, [], "text", 42])
def test_garbage_never_raises(junk):
    r = toolbench.parse_envelope(junk)
    assert not r.ok and r.error


def test_presentation_fields_degrade_quietly():
    """A cosmetic change upstream must not block a release."""
    doc = envelope()
    del doc["scores"]["rating"]
    del doc["rating"]
    doc["scores"]["category_scores"] = "unexpected"
    r = toolbench.parse_envelope(doc, STDERR_EVENTS)
    assert r.ok and r.composite_score == 93.0
    assert r.rating is None and r.category_scores == {}


# --- argv ----------------------------------------------------------------

def test_the_api_key_never_appears_in_argv():
    """argv is world-readable in /proc on the node this portal can reach."""
    argv = toolbench.build_argv(
        base_url="http://10.0.0.1:8000/v1", model="m", json_file="/tmp/o.json"
    )
    assert not any("sk-" in a or "key" in a.lower() for a in argv)
    assert "--json-file" in argv and "/tmp/o.json" in argv
    assert "--seed" in argv


def test_argv_is_an_allowlist_not_a_passthrough():
    """v1.26.0's lesson: a denylist of flags against an upstream CLI is not
    defensible. Nothing an operator supplies can add a flag."""
    argv = toolbench.build_argv(
        base_url="http://x/v1", model="m", json_file="/tmp/o.json",
        scenarios=["TC-01", "--privileged", "TC-02; rm -rf /"],
    )
    joined = " ".join(argv)
    assert "--privileged" not in joined
    assert "rm -rf" not in joined
    # Malformed entries are REJECTED, not repaired: "TC-02; rm -rf /" does not
    # become "TC-02". An allowlist that sanitises is a denylist wearing a hat.
    assert joined.endswith("--scenarios TC-01")


def test_dash_is_not_stdout():
    """`--json-file -` creates a file literally named `-`; there is no stdout
    special case. The runner must always pass a real path."""
    argv = toolbench.build_argv(base_url="http://x/v1", model="m", json_file="/tmp/real.json")
    assert "-" not in argv[argv.index("--json-file") + 1] or argv[argv.index("--json-file") + 1] != "-"


def test_availability_is_reported_not_assumed():
    ok, why = toolbench.available()
    assert isinstance(ok, bool)
    if not ok:
        assert "not installed" in why


# --- the runner ----------------------------------------------------------

async def test_the_runner_reads_stderr_events_and_survives_non_json_lines(tmp_path):
    """stderr is NOT pure JSONL — the safety gate prints plain text on it, so a
    per-line json.loads crashes on a WORKING safety gate."""
    script = tmp_path / "fake"
    script.write_text(
        "#!/usr/bin/env bash\n"
        'echo \'{"event":"scenario_start","scenario_id":"TC-01","category":"A"}\' >&2\n'
        'echo "SAFETY GATE: prompt injection blocked" >&2\n'
        'echo \'{"event":"benchmark_complete","json_file":"x","final_score":93}\' >&2\n'
        "exit 0\n"
    )
    script.chmod(0o755)

    seen: list[dict] = []

    async def on_event(ev):
        seen.append(ev)

    code, events, tail = await toolbench.run_bench([str(script)], on_event=on_event)
    assert code == 0
    assert [e.get("event") for e in events] == ["scenario_start", "benchmark_complete"]
    assert len(seen) == 2
    assert "SAFETY GATE" in tail, "plain-text stderr must still reach the log tail"


async def test_the_runner_kills_a_hung_child(tmp_path):
    """uvicorn is PID 1 with no init to reap, so a child that ignores the
    timeout would outlive the job."""
    script = tmp_path / "hang"
    script.write_text("#!/usr/bin/env bash\ntrap '' TERM\nsleep 60\n")
    script.chmod(0o755)

    code, _, tail = await toolbench.run_bench([str(script)], timeout_s=1)
    assert code != 0
    assert "timed out" in tail


async def test_the_api_key_reaches_the_child_through_the_environment(tmp_path):
    script = tmp_path / "envcheck"
    script.write_text('#!/usr/bin/env bash\necho "{\\"k\\":\\"$TOOL_EVAL_API_KEY\\"}" >&2\n')
    script.chmod(0o755)

    _, events, _ = await toolbench.run_bench([str(script)], api_key="sk-secret")
    assert events and events[0].get("k") == "sk-secret"


def test_the_pinned_sha_is_a_full_commit_id():
    """A short ref or a branch name would let the suite change under us and
    silently invalidate every stored score."""
    assert len(toolbench.PINNED_SHA) == 40
    assert all(c in "0123456789abcdef" for c in toolbench.PINNED_SHA)


def test_the_dockerfile_pins_the_same_sha():
    """The constant and the image must not drift — the score is only
    comparable across runs of the same revision."""
    import pathlib

    dockerfile = pathlib.Path(__file__).resolve().parents[2] / "Dockerfile"
    assert toolbench.PINNED_SHA in dockerfile.read_text(), (
        "Dockerfile installs a different tool-eval-bench commit than "
        "services/toolbench.py claims"
    )


def test_perf_extra_is_never_installed():
    """`[perf]` drags llama-benchy -> tokenizers (Rust/PyO3) onto the arm64
    leg, which is the hazard class that cost six hours of CI."""
    import pathlib

    dockerfile = (pathlib.Path(__file__).resolve().parents[2] / "Dockerfile").read_text()
    # Only the executable lines — the comment above the install explains why
    # the extra is excluded and necessarily names it.
    code = [ln for ln in dockerfile.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("[perf]" in ln or "perf,hf" in ln for ln in code)


def test_the_result_document_round_trips_through_json():
    """Guards the parser against being handed a str instead of a dict."""
    doc = envelope()
    assert toolbench.parse_envelope(json.loads(json.dumps(doc)), STDERR_EVENTS).ok


def test_the_catalog_actually_returns_the_quality_fields(tmp_path, monkeypatch):
    """The endpoint must SERVE what the handler passes.

    For two releases the handler set quality_available and quality_suite_sha
    while CatalogOut did not declare them, so Pydantic dropped them silently
    and the UI reported the suite as "not installed" while it was happily
    running evals. Asserting the model in isolation would not have caught it —
    the drop happens at response serialisation, so this goes through HTTP.
    """
    import importlib

    from fastapi.testclient import TestClient

    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    import app.config as config

    config.get_settings.cache_clear()
    import app.db as db

    importlib.reload(db)
    import app.main as main

    importlib.reload(main)

    with TestClient(main.app) as c:
        body = c.get("/api/evals/catalog").json()

    assert "quality_available" in body, "the API dropped quality_available again"
    assert "quality_suite_sha" in body
    assert body["quality_suite_sha"] == toolbench.PINNED_SHA
    assert isinstance(body["quality_available"], bool)
    config.get_settings.cache_clear()


def test_the_catalog_model_refuses_undeclared_fields():
    """The guard against the whole class of drift: a handler passing a field
    the model does not declare now raises instead of being ignored."""
    import pytest as _pytest

    from app.schemas import CatalogOut

    with _pytest.raises(Exception):
        CatalogOut(perf_categories=[], speed_ladder=[], not_a_real_field=1)


# --- where the benchmark actually runs ------------------------------------
#
# tool-eval-bench opens a run database at `Path.cwd() / "data" /
# "benchmarks.sqlite"`, with no flag and no environment variable to point it
# anywhere else. Invoked from the portal's own working directory — the
# read-only application directory — its first act is a mkdir that fails with
# EACCES, and the eval dies before contacting the model at all.

def test_the_benchmark_runs_from_a_writable_directory(tmp_path, monkeypatch):
    import os

    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    import app.config as config

    config.get_settings.cache_clear()
    from app.services import toolbench

    w = toolbench.workdir()
    assert os.path.isdir(w)
    assert os.access(w, os.W_OK)
    config.get_settings.cache_clear()


def test_the_working_directory_is_not_the_application_directory(tmp_path, monkeypatch):
    """The bug: cwd was /app/backend, so the tool tried to create
    /app/backend/data on a read-only filesystem."""
    from pathlib import Path

    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    import app.config as config

    config.get_settings.cache_clear()
    from app.services import toolbench

    app_dir = Path(toolbench.__file__).resolve().parents[2]
    assert not Path(toolbench.workdir()).resolve().is_relative_to(app_dir)
    config.get_settings.cache_clear()


def test_the_run_is_launched_with_that_directory_as_cwd():
    """A writable directory that is never passed to the subprocess fixes
    nothing — the tool resolves its path from ITS cwd, not from ours."""
    import inspect

    from app.services import toolbench

    src = inspect.getsource(toolbench.run_bench)
    assert "cwd=workdir()" in src


def test_the_working_directory_survives_a_restart(tmp_path, monkeypatch):
    """Deliberately under the data volume rather than a temp dir, so the run
    history the tool keeps is not thrown away on every deploy."""
    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    import app.config as config

    config.get_settings.cache_clear()
    from app.services import toolbench

    first = toolbench.workdir()
    (tmp_path / "tool-eval-bench" / "marker").write_text("x")
    config.get_settings.cache_clear()
    assert toolbench.workdir() == first
    assert (tmp_path / "tool-eval-bench" / "marker").exists()
    config.get_settings.cache_clear()


# --- making a run legible -------------------------------------------------
#
# An operator saw "93/100 · A 100% · B 100% · C 100% · D 83% · E 83%" and said,
# correctly, that it told them nothing they could act on. Everything below was
# already in the envelope and was being discarded or rendered as a raw key.

def test_category_keys_resolve_to_the_names_the_suite_ships():
    from app.services.toolbench import category_label

    assert category_label("D") == "Restraint & Refusal"
    assert category_label("E") == "Error Recovery"
    assert category_label("K") == "Safety & Boundaries"


def test_an_unknown_category_renders_as_itself():
    """A suite bump adding a category must look unfamiliar, not vanish and not
    be silently mislabelled as something it isn't."""
    from app.services.toolbench import category_label

    assert category_label("Z") == "Z"
    assert category_label(None) == "?"


def test_category_lookup_is_case_and_space_insensitive():
    from app.services.toolbench import category_label

    assert category_label(" d ") == "Restraint & Refusal"


def test_the_scenario_title_is_captured_from_the_progress_stream():
    """The envelope rows carry an id and a summary of what HAPPENED; the name
    of the test ("Distractor Resistance") appears only in the stderr event."""
    from app.services.toolbench import parse_envelope

    doc = {
        "scores": {
            "final_score": 93,
            "scenario_results": [
                {"scenario_id": "TC-02", "status": "pass", "points": 2,
                 "summary": "Used only get_stock_price for AAPL."},
            ],
        },
    }
    events = [{"event": "scenario_start", "scenario_id": "TC-02",
               "title": "Distractor Resistance", "category": "A"}]
    out = parse_envelope(doc, events)
    sc = out.scenarios[0]
    assert sc.title == "Distractor Resistance"
    assert sc.category == "A"
    assert sc.summary == "Used only get_stock_price for AAPL."


def test_the_diagnostic_fields_are_no_longer_dropped():
    """ttft, turns and the tool calls are what make a row explain itself."""
    from app.services.toolbench import parse_envelope

    doc = {
        "scores": {
            "final_score": 50,
            "scenario_results": [{
                "scenario_id": "TC-11", "status": "partial", "points": 1,
                "summary": "Reached for calculator on 15%x200.",
                "expected_behavior": "Answer 30 directly with no calculator.",
                "tool_calls_made": ["calculator(15%*200)"],
                "ttft_ms": 582.0, "turn_count": 2,
                "prompt_tokens": 120, "completion_tokens": 8,
            }],
        },
    }
    sc = parse_envelope(doc, []).scenarios[0]
    assert sc.status == "partial"
    assert sc.points == 1
    assert sc.ttft_ms == 582.0
    assert sc.turn_count == 2
    assert sc.tool_calls == ["calculator(15%*200)"]
    assert "no calculator" in sc.expected_behavior


def test_a_partial_is_kept_distinct_from_a_failure():
    """1/2 is 'right answer, wrong method'. Collapsing it into a boolean is
    what made D 83% unexplainable."""
    from app.services.toolbench import parse_envelope

    doc = {"scores": {"final_score": 25, "scenario_results": [
        {"scenario_id": "TC-11", "status": "partial", "points": 1},
        {"scenario_id": "TC-12", "status": "fail", "points": 0},
    ]}}
    got = {s.scenario_id: (s.status, s.points) for s in parse_envelope(doc, []).scenarios}
    assert got["TC-11"] == ("partial", 1)
    assert got["TC-12"] == ("fail", 0)


def test_tool_calls_are_bounded():
    """A pathological run must not write an unbounded blob into every row."""
    from app.services.toolbench import parse_envelope

    doc = {"scores": {"final_score": 100, "scenario_results": [{
        "scenario_id": "TC-01", "status": "pass", "points": 2,
        "tool_calls_made": ["x" * 500] * 50,
    }]}}
    sc = parse_envelope(doc, []).scenarios[0]
    assert len(sc.tool_calls) <= 20
    assert all(len(c) <= 200 for c in sc.tool_calls)


# --- what the run detail API hands the UI ---------------------------------

@pytest.fixture()
def eval_client(tmp_path, monkeypatch):
    import importlib

    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    import app.config as config

    config.get_settings.cache_clear()
    import app.db as db

    importlib.reload(db)
    import app.main as main

    importlib.reload(main)
    from fastapi.testclient import TestClient

    with TestClient(main.app) as c:
        yield c, db
    config.get_settings.cache_clear()


def _seed_quality_run(db, *, with_log=True):
    import asyncio

    from app.models import EvalResult, EvalRun

    async def _go():
        async with db.SessionLocal() as s:
            run = EvalRun(
                name="qualitytest", model_name="dsv4", instance_label="TP=2",
                categories="", capability=False, quality=True, performance=False,
                config_json="{}",
                status="success", overall_score=0.9333,
                summary_json='{"category_scores": {"A": 1.0, "D": 0.8333}}',
                scenarios_run=15, scenarios_available=69,
                raw_envelope='{"final_score": 93}' if with_log else None,
                log_tail="progress…" if with_log else None,
            )
            s.add(run)
            await s.flush()
            s.add(EvalResult(
                run_id=run.id, category="D", task_id="TC-11",
                task_name="Simple Math", scorer="tool-eval-bench",
                score=0.5, passed=False, status="partial", turn_count=2,
                ttft_ms=582.0, judge_reason="Reached for calculator on 15%x200.",
                expected="Answer 30 directly with no calculator.",
                tool_calls='["calculator(15%*200)"]',
            ))
            await s.commit()
            return run.id

    return asyncio.run(_go())


def test_the_detail_carries_category_names_so_the_ui_need_not(eval_client):
    client, db = eval_client
    rid = _seed_quality_run(db)
    d = client.get(f"/api/evals/{rid}").json()
    assert d["category_labels"]["D"] == "Restraint & Refusal"
    assert d["category_labels"]["A"] == "Tool Selection"


def test_the_detail_says_how_much_of_the_suite_ran(eval_client):
    """A score over 15 of 69 scenarios is not the same measurement as a full
    run, and presenting 93/100 without saying so invites the wrong conclusion."""
    client, db = eval_client
    rid = _seed_quality_run(db)
    d = client.get(f"/api/evals/{rid}").json()
    assert (d["scenarios_run"], d["scenarios_available"]) == (15, 69)


def test_a_partial_survives_the_round_trip(eval_client):
    client, db = eval_client
    rid = _seed_quality_run(db)
    row = client.get(f"/api/evals/{rid}").json()["results"][0]
    assert row["status"] == "partial"
    assert row["passed"] is False        # both true at once — that is the point
    assert row["category_label"] == "Restraint & Refusal"
    assert row["task_name"] == "Simple Math"
    assert row["turn_count"] == 2
    assert "calculator" in row["tool_calls"][0]
    assert "no calculator" in row["expected"]


def test_the_suite_output_is_retrievable(eval_client):
    client, db = eval_client
    rid = _seed_quality_run(db)
    assert client.get(f"/api/evals/{rid}").json()["has_log"] is True
    body = client.get(f"/api/evals/{rid}/log").text
    assert "suite progress" in body and "result envelope" in body


def test_a_run_with_no_stored_output_says_so_plainly(eval_client):
    """Runs from before this release kept nothing; that must read as an
    explanation rather than a broken button."""
    client, db = eval_client
    rid = _seed_quality_run(db, with_log=False)
    assert client.get(f"/api/evals/{rid}").json()["has_log"] is False
    r = client.get(f"/api/evals/{rid}/log")
    assert r.status_code == 404
    assert "re-run to capture it" in r.text
