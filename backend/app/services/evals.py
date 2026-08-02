"""Evaluation engine: measures serving speed across the predictability ladder.

Runs each prompt through a concurrency sweep against a model instance,
recording TTFT, per-stream tok/s and aggregate throughput, and persists the
results for comparison across instances and over time.

The quality half is a separate suite (see docs/EVALS.md); this module owns
speed. `_finalize`'s EvalResult aggregation is the seam it attaches to.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from ..crypto import decrypt
from ..db import SessionLocal, get_node_by_role
from ..models import (
    JOB_ERROR,
    JOB_RUNNING,
    JOB_SUCCESS,
    EvalResult,
    EvalRun,
    PerfResult,
)
from pathlib import Path

from . import eval_suites, toolbench
from .instances import load_instance
from .jobs import JobHandle
from .llm_client import chat_stream


@dataclass
class Endpoint:
    base_url: str
    model: str
    api_key: str | None
    desc: str
    # False for TLS instances: the proxy cert is for the public name, and we
    # dial the node IP directly.
    verify: bool = True


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _avg(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


async def _commit(session: AsyncSession, handle: JobHandle) -> None:
    """Commit, retrying on a transient SQLite lock so it doesn't crash the run.
    WAL + busy_timeout make this rare; this is belt-and-suspenders."""
    for i in range(6):
        try:
            await session.commit()
            return
        except OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
            await asyncio.sleep(0.25 * (i + 1))
    try:
        await session.rollback()
    except Exception:  # noqa: BLE001
        pass
    await handle.log("warning: a DB write was busy and was skipped; continuing", "error")


# --- endpoint resolution -------------------------------------------------
async def _served_model_id(
    base_url: str, api_key: str | None, fallback: str, verify: bool = True
) -> str:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=10, verify=verify) as client:
            r = await client.get(f"{base_url.rstrip('/')}/models", headers=headers)
            if r.status_code == 200:
                data = r.json().get("data", [])
                if data:
                    return data[0].get("id", fallback)
    except httpx.HTTPError:
        pass
    return fallback


async def _instance_endpoint(session: AsyncSession, instance_id: int) -> Endpoint:
    inst = await load_instance(session, instance_id)
    if inst is None:
        raise RuntimeError(f"Instance {instance_id} not found.")
    # Shared resolution: single -> its pinned node; cluster AND distributed ->
    # the head; TLS -> https via the nginx sidecar (vLLM binds loopback then).
    from . import status_svc

    head = await get_node_by_role(session, "head")
    base_t = status_svc.instance_base_url(inst, head)
    if base_t is None:
        raise RuntimeError("Instance has no reachable host.")
    url, verify = base_t
    base = f"{url}/v1"
    api_key = decrypt(inst.api_key_enc)
    fallback = inst.model.name if inst.model else ""
    model_id = await _served_model_id(base, api_key, fallback, verify=verify)
    return Endpoint(
        base, model_id, api_key,
        f"{inst.name} ({inst.model.name if inst.model else '?'})", verify=verify,
    )


# --- performance ---------------------------------------------------------
async def _run_perf(session, handle, run, pt, target, concurrency, reps, cfg) -> None:
    messages = ([{"role": "system", "content": pt.system}] if pt.system else []) + [
        {"role": "user", "content": pt.prompt}
    ]
    ttfts: list[float] = []
    tps_list: list[float] = []
    lat: list[float] = []
    ptoks: list[float] = []
    ctoks: list[float] = []
    agg: list[float] = []
    for _ in range(reps):
        t0 = time.perf_counter()
        results = await asyncio.gather(
            *[
                chat_stream(
                    target.base_url, target.model, messages,
                    max_tokens=pt.max_tokens, temperature=float(cfg.get("temperature", 0.2)),
                    api_key=target.api_key, verify=target.verify,
                )
                for _ in range(concurrency)
            ]
        )
        wall = time.perf_counter() - t0
        ok = [r for r in results if r.ok]
        if not ok:
            continue
        for r in ok:
            if r.metrics.ttft_ms is not None:
                ttfts.append(r.metrics.ttft_ms)
            if r.metrics.tokens_per_sec:
                tps_list.append(r.metrics.tokens_per_sec)
            lat.append(r.metrics.total_ms)
            ptoks.append(r.metrics.prompt_tokens or 0)
            ctoks.append(r.metrics.completion_tokens or 0)
        total_completion = sum((r.metrics.completion_tokens or 0) for r in ok)
        agg.append(total_completion / wall if wall > 0 else 0.0)

    pr = PerfResult(
        run_id=run.id, category=pt.category, concurrency=concurrency, reps=reps,
        ttft_ms_avg=_avg(ttfts), decode_tps_avg=_avg(tps_list), total_latency_ms_avg=_avg(lat),
        throughput_tps=_avg(agg), prompt_tokens_avg=_avg(ptoks), completion_tokens_avg=_avg(ctoks),
    )
    if not ttfts and not agg:
        pr.error = "all requests failed"
    session.add(pr)
    await _commit(session, handle)
    await handle.log(
        f"[perf/{pt.category}] C={concurrency}: "
        f"{(_avg(agg) or 0):.0f} tok/s aggregate, {(_avg(tps_list) or 0):.0f} tok/s/stream, "
        f"TTFT {(_avg(ttfts) or 0):.0f}ms"
    )


# Bounds on what a run keeps. Big enough to hold a full 84-scenario envelope
# with its notes, small enough that a year of runs is not a storage problem.
_MAX_ENVELOPE_CHARS = 512_000
_MAX_LOG_CHARS = 64_000


# --- quality suite (tool-eval-bench) -------------------------------------
async def _run_quality(
    session: AsyncSession, handle: JobHandle, run: EvalRun, target: Endpoint, cfg: dict
) -> None:
    """Run the pinned external suite and persist its result.

    Per-scenario rows go into EvalResult, which is what relights the existing
    by-category breakdown and task table on the run detail — both already read
    that table and self-hide when it is empty.
    """
    import tempfile

    ok, why = toolbench.available()
    if not ok:
        raise RuntimeError(why)

    with tempfile.TemporaryDirectory() as tmp:
        out_path = str(Path(tmp) / "result.json")
        argv = toolbench.build_argv(
            base_url=target.base_url.rstrip("/"),
            model=target.model,
            json_file=out_path,
            seed=int(cfg.get("seed", 42)),
            short=bool(cfg.get("short")),
            hardmode=bool(cfg.get("hardmode")),
        )
        await handle.log(f"tool-eval-bench @ {toolbench.PINNED_SHA[:12]} — {len(argv)} args")

        seen = {"n": 0}

        async def on_event(ev: dict) -> None:
            if ev.get("event") == "scenario_result":
                seen["n"] += 1
                total = ev.get("total") or 0
                if total:
                    await handle.set_progress(min(seen["n"] / float(total), 1.0))
                await handle.log(
                    f"[{ev.get('scenario_id')}] {ev.get('status')}"
                )

        code, events, tail = await toolbench.run_bench(
            argv, api_key=target.api_key, on_event=on_event
        )

        try:
            document = json.loads(Path(out_path).read_text())
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"tool-eval-bench exited {code} and left no readable result. {tail}"
            ) from exc

    result = toolbench.parse_envelope(document, events)
    if not result.ok:
        raise RuntimeError(f"{result.error} (exit {code}). {tail}")

    run.quality = True
    run.composite_score = result.composite_score
    run.completion_rate = result.completion_rate
    run.suite_version = result.version
    run.suite_sha = toolbench.PINNED_SHA
    run.scenarios_run = len(result.scenarios) or None
    run.scenarios_available = result.total_scenarios
    # The suite's own output, so a run that goes wrong can be examined after
    # the fact. Bounded: `raw_log` per scenario can be large, and an eval
    # history is not a place to accumulate megabytes per row.
    envelope_text = json.dumps(document, separators=(",", ":"))
    if len(envelope_text) > _MAX_ENVELOPE_CHARS:
        envelope_text = (
            envelope_text[:_MAX_ENVELOPE_CHARS]
            + f"\n\n[truncated at {_MAX_ENVELOPE_CHARS} characters]"
        )
    run.raw_envelope = envelope_text
    run.log_tail = (tail or "")[-_MAX_LOG_CHARS:] or None

    for sc in result.scenarios:
        session.add(EvalResult(
            run_id=run.id,
            category=sc.category or "?",
            task_id=sc.scenario_id,
            # The scenario's NAME ("Distractor Resistance"), not a description
            # of what happened. The summary is the outcome and belongs beside
            # it, not in place of it.
            task_name=(sc.title or sc.scenario_id)[:255],
            scorer="tool-eval-bench",
            # 2/1/0 -> 0..1 so the existing 0..1 renderers stay correct.
            score=sc.points / 2.0,
            passed=sc.status == "pass",
            status=sc.status,
            error=sc.failure_kind,
            latency_ms=(sc.duration_seconds or 0) * 1000 or None,
            ttft_ms=sc.ttft_ms,
            turn_count=sc.turn_count,
            prompt_tokens=sc.prompt_tokens,
            completion_tokens=sc.completion_tokens,
            # What happened, and what should have. Together they turn a red
            # cell into something an operator can act on.
            judge_reason=sc.summary or sc.note or None,
            expected=sc.expected_behavior,
            tool_calls=json.dumps(sc.tool_calls) if sc.tool_calls else None,
        ))
    await _commit(session, handle)

    await handle.log(f"Quality: {result.summary_line()}")
    if not result.trustworthy:
        # The single most misleading thing this suite can produce. Infra
        # failures leave the denominator, so a high score on a broken endpoint
        # is the expected output, not an anomaly.
        await handle.log(
            f"WARNING: only {result.completion_rate:.0f}% of scenarios were graded "
            f"({len(result.excluded_scenarios)} excluded: "
            f"{', '.join(result.excluded_scenarios[:6])}). The score is computed over "
            "what ran, so treat it as unmeasured rather than good.",
            "error",
        )
    for w in result.safety_warnings:
        await handle.log(f"SAFETY: {w}", "error")


# --- orchestration -------------------------------------------------------
async def run_eval(handle: JobHandle, run_id: int) -> str:
    async with SessionLocal() as session:
        run = await session.get(EvalRun, run_id)
        if run is None:
            raise RuntimeError("Eval run not found.")
        cfg = json.loads(run.config_json)
        categories = run.categories.split(",") if run.categories else []
        run.status = JOB_RUNNING
        run.started_at = _now()
        run.job_id = handle.job_id
        await _commit(session, handle)

        target = await _instance_endpoint(session, cfg["instance_id"])
        await handle.log(f"Target: {target.desc} @ {target.base_url} (model {target.model})")

        try:
            if cfg.get("quality"):
                await handle.log("Running the tool-eval-bench quality suite…")
                await _run_quality(session, handle, run, target, cfg)

            ptasks = eval_suites.perf_tasks(categories) if categories else []
            if not ptasks and not cfg.get("quality"):
                # Refuse rather than finish green having measured nothing. The
                # obvious way to hit this is Re-run on a historical row whose
                # categories predate the current prompts.
                raise RuntimeError(
                    f"No speed prompts match {categories!r}. "
                    f"Available: {', '.join(eval_suites.perf_categories())}."
                )
            conc = cfg.get("concurrency") or [1]
            reps = int(cfg.get("perf_reps", 3))
            total = max(len(ptasks) * len(conc), 1)
            done = 0
            await handle.log(f"Measuring speed ({len(ptasks)} prompts × {conc})…")
            for pt in ptasks:
                for c in conc:
                    await _run_perf(session, handle, run, pt, target, int(c), reps, cfg)
                    done += 1
                    # Speed is now the whole run, so the bar spans 0..1. It used
                    # to start at 0.6 because the capability half owned the rest.
                    await handle.set_progress(done / total)

            peak = await _finalize(session, handle, run)
            run.status = JOB_SUCCESS
            run.finished_at = _now()
            await _commit(session, handle)
            return (
                f"Eval '{run.name}' complete — peak {peak:.0f} tok/s"
                if peak is not None
                else f"Eval '{run.name}' complete"
            )
        except Exception:
            run.status = JOB_ERROR
            run.finished_at = _now()
            await _commit(session, handle)
            raise


async def _finalize(session: AsyncSession, handle: JobHandle, run: EvalRun) -> float | None:
    """Roll the per-measurement rows into the run summary. Returns peak tok/s.

    The EvalResult query stays even though nothing writes those rows today: it
    is the seam an additional quality suite attaches to. Writing EvalResult
    rows is all it takes to relight the by-category breakdown and the task
    table, both of which self-hide when the set is empty. It costs one indexed
    query on an empty set.
    """
    res = (await session.execute(select(EvalResult).where(EvalResult.run_id == run.id))).scalars().all()
    perf = (await session.execute(select(PerfResult).where(PerfResult.run_id == run.id))).scalars().all()

    by_cat: dict[str, list[float]] = {}
    for er in res:
        by_cat.setdefault(er.category, []).append(er.score)
    cat_scores = {c: round(sum(v) / len(v), 4) for c, v in by_cat.items() if v}
    all_scores = [er.score for er in res]
    run.overall_score = round(sum(all_scores) / len(all_scores), 4) if all_scores else None

    # Every measurement failed is a FAILED run, not a successful one reporting
    # zero. `_run_perf` records the error on the row and returns normally, so
    # with speed as the only half nothing else would ever fail the run — a
    # benchmark against a dead endpoint would finish green.
    if perf and all(p.error for p in perf):
        raise RuntimeError(
            "Every measurement failed — the instance did not answer. "
            f"First error: {perf[0].error}"
        )

    # None rather than 0.0 when nothing produced a throughput: the UI tests for
    # null in one place and truthiness in another, and 0.0 renders as both
    # "no data" and "zero tok/s" on the same screen.
    tputs = [p.throughput_tps for p in perf if p.throughput_tps]
    peak = max(tputs) if tputs else None

    # Best per regime, which is the comparison that actually distinguishes two
    # builds — see the predictability-ladder note in eval_suites.py.
    ladder: dict[str, float] = {}
    for p in perf:
        if p.throughput_tps:
            ladder[p.category] = max(ladder.get(p.category, 0.0), p.throughput_tps)

    summary = {
        "category_scores": cat_scores,
        "overall": run.overall_score,
        "peak_throughput_tps": peak,
        "ladder_tps": {k: round(v, 1) for k, v in ladder.items()},
        "perf": [
            {
                "category": p.category, "concurrency": p.concurrency,
                "throughput_tps": p.throughput_tps, "decode_tps_avg": p.decode_tps_avg,
                "ttft_ms_avg": p.ttft_ms_avg, "total_latency_ms_avg": p.total_latency_ms_avg,
            }
            for p in perf
        ],
    }
    run.summary_json = json.dumps(summary)
    await _commit(session, handle)
    rungs = " / ".join(f"{k} {ladder[k]:.0f}" for k in eval_suites.SPEED_LADDER if k in ladder)
    await handle.log(f"Done. {rungs or 'no measurements'}" + (f" — peak {peak:.0f} tok/s" if peak else ""))
    return peak
