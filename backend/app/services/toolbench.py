"""Run tool-eval-bench against an instance and parse what it produces.

The quality half of the Evals page. tool-eval-bench is a published, externally
maintained tool-calling suite (69 scenarios + 15 Hard Mode); we run it against
an instance's OpenAI endpoint and store the result beside our own speed
measurements. Adopting it rather than writing scenarios means the content is
someone else's problem and the numbers are comparable to what the community
publishes.

It is DEPENDED ON, not vendored. Both projects are MIT so copying would be
legal, but a copy is a fork: every upstream fix becomes a manual merge, and our
scores stop being comparable the moment upstream moves and we don't. Pinned by
commit SHA and installed into its own venv at image build time.

Four facts about its output, each verified by running the real CLI rather than
read from the README, and each of which the obvious implementation gets wrong:

1. ``--json-file`` writes ONE pretty-printed JSON document. The JSON *Lines*
   stream is on **stderr** (``scenario_start`` / ``scenario_result`` /
   ``benchmark_complete``), and ``-`` does not mean stdout — it creates a file
   literally named ``-``.
2. ``benchmark_complete`` fires on the ERROR path too, with a null score. It
   means "the file has been written", not "the run succeeded". The error
   envelope has no ``scores`` key at all.
3. Per-scenario ``category`` is **not** in the document. ``ScenarioResult`` has
   no such attribute. It is only in the stderr ``scenario_start`` events, which
   is why this module reads both streams.
4. Infrastructure failures are EXCLUDED FROM THE DENOMINATOR and the exit code
   stays 0. Measured: a server failing every second request took ``max_points``
   from 30 to 14, ``completion_rate`` to 46.7 — and ``final_score`` still read
   normally. **A nearly-broken endpoint scores high.** Never show or gate on a
   score without ``completion_rate`` beside it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("spark.toolbench")

__all__ = [
    "CATEGORY_LABELS",
    "PINNED_SHA",
    "category_label",
    "workdir",
    "BenchResult",
    "available",
    "parse_envelope",
    "build_argv",
    "run_bench",
]

# The exact upstream commit this build was tested against. Bumping it is a
# deliberate act: scores are only comparable across runs of the same revision,
# so a silent upgrade would invalidate every trend line on the Evals page.
PINNED_SHA = "5df1e9e0cbde8d5805ef4c70e3fe2a13cec5ab9c"

# Where the Dockerfile installs it. Absent on a dev checkout, which is fine —
# `available()` reports it and the API refuses the run with a clear reason
# rather than the subprocess failing with ENOENT halfway through a job.
BENCH_BIN = os.environ.get("SPARK_TOOL_EVAL_BENCH", "/opt/tool-eval-bench/bin/tool-eval-bench")


def workdir() -> str:
    """A writable directory to run the benchmark from, created on demand.

    tool-eval-bench opens a run database at `Path.cwd() / "data" /
    "benchmarks.sqlite"` — a path relative to wherever the CLI is invoked, with
    no flag and no environment variable to override it. The portal's working
    directory is the read-only application directory, so the very first thing
    the tool did was `mkdir /app/backend/data` and die with EACCES.

    It surfaced only when someone ran an eval: `available()` probes with
    `--help`, which never constructs the repository, so the feature reported
    itself healthy right up to the moment it was used.

    Under the data directory rather than a temp dir so the run history the tool
    keeps survives a restart, and because that volume is writable by
    definition — the portal's own database lives there.
    """
    from ..config import get_settings

    path = Path(get_settings().data_dir) / "tool-eval-bench"
    path.mkdir(parents=True, exist_ok=True)
    return str(path)

# A scenario run is long; this is a backstop against a hung CLI holding a job
# open forever, not an expected duration.
DEFAULT_TIMEOUT_S = 3600

# Scenario ids are "TC-01" .. "TC-99".
_SCENARIO_RE = re.compile(r"\ATC-[0-9]{1,3}\Z")


# The suite's category keys are single letters, and the portal was rendering
# them raw — "D 83%" tells an operator nothing they can act on. The bench ships
# the names in `domain/scenarios.py::CATEGORY_LABELS`; this is that table,
# pinned alongside PINNED_SHA because it must move only when the suite does.
#
# Unknown keys render as themselves rather than being dropped or guessed at: a
# new category appearing after a suite bump should look unfamiliar, not absent.
CATEGORY_LABELS: dict[str, str] = {
    "A": "Tool Selection",
    "B": "Parameter Precision",
    "C": "Multi-Step Chains",
    "D": "Restraint & Refusal",
    "E": "Error Recovery",
    "F": "Localization",
    "G": "Structured Reasoning",
    "H": "Instruction Following",
    "I": "Context & State",
    "J": "Code Patterns",
    "K": "Safety & Boundaries",
    "L": "Toolset Scale",
    "M": "Autonomous Planning",
    "N": "Creative Composition",
    "O": "Structured Output",
    "P": "Hard Mode",
}


def category_label(key: str | None) -> str:
    """A human name for a category key, or the key itself."""
    if not key:
        return "?"
    return CATEGORY_LABELS.get(str(key).strip().upper(), str(key))


@dataclass
class ScenarioOutcome:
    scenario_id: str
    status: str                 # pass | partial | fail
    points: int                 # 2 | 1 | 0
    summary: str = ""
    category: str | None = None  # only from the stderr stream — see module docstring
    failure_kind: str | None = None
    duration_seconds: float | None = None
    excluded: bool = False       # infra failure: left the denominator
    # Everything below was already in the envelope and thrown away. It is the
    # difference between "D 83%" and "TC-11 reached for the calculator on
    # 15%x200 when mental math was sufficient".
    title: str | None = None
    note: str | None = None
    expected_behavior: str | None = None
    tool_calls: list[str] = field(default_factory=list)
    ttft_ms: float | None = None
    turn_count: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@dataclass
class BenchResult:
    """One parsed run. ``ok`` false means do not trust the numbers."""

    ok: bool = False
    error: str = ""
    composite_score: float | None = None      # 0-100
    rating: str | None = None
    completion_rate: float | None = None      # % of scenarios actually graded
    excluded_scenarios: list[str] = field(default_factory=list)
    safety_warnings: list[str] = field(default_factory=list)
    category_scores: dict[str, float] = field(default_factory=dict)
    scenarios: list[ScenarioOutcome] = field(default_factory=list)
    version: str | None = None
    config_fingerprint: str | None = None
    total_scenarios: int | None = None

    @property
    def trustworthy(self) -> bool:
        """A score with a low completion rate is not a bad score, it is an
        absent measurement wearing one. See fact 4 in the module docstring."""
        return self.ok and (self.completion_rate is None or self.completion_rate >= 95.0)

    def summary_line(self) -> str:
        if not self.ok:
            return f"tool-eval-bench failed: {self.error}"
        parts = [f"{self.composite_score:.0f}/100" if self.composite_score is not None else "no score"]
        if self.rating:
            parts.append(self.rating)
        if self.completion_rate is not None and self.completion_rate < 100:
            parts.append(f"only {self.completion_rate:.0f}% of scenarios graded")
        if self.safety_warnings:
            parts.append(f"{len(self.safety_warnings)} safety warning(s)")
        return " — ".join(parts)


def available() -> tuple[bool, str]:
    """Is the pinned CLI installed? Returns (ok, reason)."""
    if shutil.which(BENCH_BIN) or Path(BENCH_BIN).is_file():
        return True, ""
    return False, (
        "tool-eval-bench is not installed in this image. Quality runs need it; "
        "speed runs work without it."
    )


def build_argv(
    *,
    base_url: str,
    model: str,
    json_file: str,
    seed: int = 42,
    short: bool = False,
    hardmode: bool = False,
    scenarios: list[str] | None = None,
) -> list[str]:
    """The exact argv. No shell, and the API key never appears here.

    Deliberately a fixed allowlist of flags rather than a passthrough: this
    project already learned (v1.26.0) that filtering a denylist of flags into
    an upstream CLI is not defensible. Anything not listed here cannot be set.
    """
    argv = [
        BENCH_BIN, "run",
        "--base-url", base_url,
        "--model", model,
        "--seed", str(int(seed)),
        "--json-file", json_file,
    ]
    if short:
        argv.append("--short")
    if hardmode:
        argv.append("--hardmode")
    if scenarios:
        # Strictly the TC-nn shape. An earlier version stripped dashes and
        # checked isalnum(), which happily accepted "--privileged" — the
        # allowlist has to describe what is VALID, not what looks harmless.
        safe = [
            s for s in scenarios
            if _SCENARIO_RE.match(s)
        ]
        if safe:
            argv += ["--scenarios", ",".join(safe)]
    return argv


def parse_envelope(document: dict, stderr_events: list[dict] | None = None) -> BenchResult:
    """Parse the ``--json-file`` document into a BenchResult. Never raises.

    Fails LOUDLY (ok=False) on anything that moves the number — a missing
    ``scores`` key, a non-numeric score, an absent completion rate. Degrades
    QUIETLY only on presentation fields (rating, per-scenario timings), because
    a cosmetic change upstream must not block a release.
    """
    out = BenchResult()
    if not isinstance(document, dict):
        out.error = "the result file was not a JSON object"
        return out

    if "error" in document and "scores" not in document:
        out.error = str(document.get("error"))[:400]
        return out

    scores = document.get("scores")
    if not isinstance(scores, dict):
        # The error envelope has no `scores` key at all. Treating its null
        # final_score as 0 would record a real-looking failure.
        out.error = "the result had no `scores` section — the run did not complete"
        return out

    final = scores.get("final_score", document.get("final_score"))
    if not isinstance(final, (int, float)):
        out.error = f"final_score was {final!r}, not a number"
        return out
    out.composite_score = float(final)

    out.version = _str_or_none(document.get("tool_eval_bench_version"))
    out.rating = _str_or_none(scores.get("rating") or document.get("rating"))
    out.total_scenarios = _int_or_none(document.get("total_scenarios"))

    cfg = document.get("config")
    if isinstance(cfg, dict):
        out.config_fingerprint = _str_or_none(cfg.get("config_fingerprint"))

    # Absent means "nothing was excluded" — the key is conditional, not null.
    rate = scores.get("completion_rate")
    out.completion_rate = float(rate) if isinstance(rate, (int, float)) else 100.0
    excluded = scores.get("excluded_scenarios")
    out.excluded_scenarios = [str(x) for x in excluded] if isinstance(excluded, list) else []

    warn = scores.get("safety_warnings", document.get("safety_warnings"))
    out.safety_warnings = [str(w) for w in warn] if isinstance(warn, list) else []

    for entry in scores.get("category_scores") or []:
        if isinstance(entry, dict) and entry.get("category") is not None:
            pct = entry.get("percent")
            if isinstance(pct, (int, float)):
                out.category_scores[str(entry["category"])] = float(pct)

    # Category comes from the stderr stream, not the document (fact 3).
    cat_by_id: dict[str, str] = {}
    title_by_id: dict[str, str] = {}
    for ev in stderr_events or []:
        if ev.get("event") == "scenario_start" and ev.get("scenario_id"):
            sid_ev = str(ev["scenario_id"])
            if ev.get("category"):
                cat_by_id[sid_ev] = str(ev["category"])
            # The scenario's human title ("Distractor Resistance") rides the
            # same event and is the only place it appears — the envelope rows
            # carry an id and a summary of what happened, never the name of
            # the test.
            if ev.get("title"):
                title_by_id[sid_ev] = str(ev["title"])[:255]

    excluded_set = set(out.excluded_scenarios)
    for row in scores.get("scenario_results") or []:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("scenario_id") or "")
        if not sid:
            continue
        calls = row.get("tool_calls_made")
        out.scenarios.append(ScenarioOutcome(
            scenario_id=sid,
            status=str(row.get("status") or "fail"),
            points=_int_or_none(row.get("points")) or 0,
            summary=str(row.get("summary") or "")[:1000],
            category=cat_by_id.get(sid),
            failure_kind=_str_or_none(row.get("failure_kind")),
            duration_seconds=_float_or_none(row.get("duration_seconds")),
            excluded=sid in excluded_set,
            title=title_by_id.get(sid),
            note=_str_or_none(row.get("note")),
            expected_behavior=_str_or_none(row.get("expected_behavior")),
            tool_calls=[str(c)[:200] for c in calls][:20] if isinstance(calls, list) else [],
            ttft_ms=_float_or_none(row.get("ttft_ms")),
            turn_count=_int_or_none(row.get("turn_count")),
            prompt_tokens=_int_or_none(row.get("prompt_tokens")),
            completion_tokens=_int_or_none(row.get("completion_tokens")),
        ))

    out.ok = True
    return out


async def run_bench(
    argv: list[str],
    *,
    api_key: str | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    on_event=None,
) -> tuple[int, list[dict], str]:
    """Run the CLI. Returns (exit_code, stderr_events, stderr_tail).

    Notes that cost time if rediscovered:

    * The API key goes in the ENVIRONMENT, never argv — argv is world-readable
      in /proc on the node this portal can reach.
    * stderr must be drained CONCURRENTLY. It carries a line per scenario, and
      a full pipe buffer deadlocks a long run.
    * The child gets its own process group and the group is killed in
      ``finally``. uvicorn is PID 1 with no init to reap for us, so a child
      that ignores the timeout would otherwise outlive the job.
    * stderr is NOT pure JSONL — the safety gate prints plain text on it, so a
      per-line ``json.loads`` crashes on a *working* safety gate.
    """
    env = dict(os.environ)
    if api_key:
        env["TOOL_EVAL_API_KEY"] = api_key
        env["OPENAI_API_KEY"] = api_key

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.DEVNULL,   # empty when --json-file is used
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=workdir(),
        start_new_session=True,
    )

    events: list[dict] = []
    tail: list[str] = []

    async def drain() -> None:
        assert proc.stderr is not None
        async for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if not line:
                continue
            tail.append(line)
            del tail[:-40]
            if not line.startswith("{"):
                continue  # e.g. "SAFETY GATE: …", printed as plain text
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict):
                events.append(ev)
                if on_event is not None:
                    try:
                        await on_event(ev)
                    except Exception:  # noqa: BLE001 - progress must not kill the run
                        pass

    drainer = asyncio.create_task(drain())
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout_s)
    except asyncio.TimeoutError:
        tail.append(f"timed out after {timeout_s}s")
    finally:
        drainer.cancel()
        if proc.returncode is None:
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                pass
    return (proc.returncode if proc.returncode is not None else -1), events, "\n".join(tail[-20:])


def _str_or_none(v) -> str | None:
    return str(v) if isinstance(v, str) and v else None


def _int_or_none(v) -> int | None:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _float_or_none(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
