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
