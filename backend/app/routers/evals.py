from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..db import get_session
from ..models import EvalRun
from ..schemas import (
    CatalogOut,
    EvalRunDetail,
    EvalRunOut,
    EvalRunRequest,
    EvalStarted,
)
from ..services import eval_suites, evals, toolbench
from ..services.instances import load_instance
from ..services.jobs import jobs

router = APIRouter(prefix="/api/evals", tags=["evals"])

# Recovering findings from stored envelopes happens the first time anyone looks
# at an eval, not at startup. Startup is the wrong place: it delays readiness,
# and doing it in the deferred background task left a session that could open
# after the request loop had moved on — which showed up as unrelated tests
# failing intermittently.
_backfilled = False


async def _ensure_backfilled(session: AsyncSession) -> None:
    global _backfilled
    if _backfilled:
        return
    _backfilled = True          # set first: one attempt, never a retry storm
    try:
        await evals.backfill_from_envelopes(session)
    except Exception:  # noqa: BLE001 - reading history must never fail the page
        import logging

        logging.getLogger("spark.evals").warning(
            "could not backfill eval safety findings", exc_info=True
        )


@router.get("/catalog", response_model=CatalogOut)
async def catalog(session: AsyncSession = Depends(get_session)):
    # `session` is unused now that there are no operator-authored categories to
    # look up, but it MUST stay: mcp_server calls this handler through
    # `_with_session`, which passes `session=` unconditionally. Dropping the
    # parameter makes every eval_catalog MCP call raise TypeError, and no test
    # invokes it.
    return CatalogOut(
        perf_categories=eval_suites.perf_categories(),
        speed_ladder=list(eval_suites.SPEED_LADDER),
        quality_available=toolbench.available()[0],
        quality_suite_sha=toolbench.PINNED_SHA,
    )


@router.post("", response_model=EvalStarted)
async def create_eval(payload: EvalRunRequest, session: AsyncSession = Depends(get_session)):
    inst = await load_instance(session, payload.instance_id)
    if inst is None:
        raise HTTPException(404, "Instance not found")
    if not payload.categories and not payload.quality:
        raise HTTPException(400, "Select at least one speed category, or enable the quality suite.")
    if payload.quality:
        ok, why = toolbench.available()
        if not ok:
            raise HTTPException(400, why)
    # Reject unknown categories rather than accepting them and finishing a
    # green run that measured nothing. This is the Re-run path for every
    # historical row: those runs carry categories like `coding` or `security`
    # that have no speed prompt, and `perf_tasks()` is a pure filter.
    unknown = [c for c in payload.categories if c not in eval_suites.perf_categories()]
    if unknown:
        raise HTTPException(
            400,
            f"No speed prompt for {', '.join(unknown)}. "
            f"Available: {', '.join(eval_suites.perf_categories())}.",
        )

    label = f"{inst.topology} TP={inst.tensor_parallel_size} :{inst.port}"
    model_name = inst.model.name if inst.model else "?"
    name = payload.name or f"{model_name} — {inst.name}"
    config = {
        "instance_id": payload.instance_id,
        "categories": payload.categories,
        "perf_reps": payload.perf_reps,
        "concurrency": payload.concurrency,
        "temperature": payload.temperature,
        "quality": payload.quality,
        "short": payload.short,
        "hardmode": payload.hardmode,
        "seed": payload.seed,
    }
    run = EvalRun(
        name=name, instance_id=inst.id, model_name=model_name, instance_label=label,
        categories=",".join(payload.categories),
        # Explicit: the column defaults to True, so a speed-only run would
        # otherwise claim it ran a capability half — and that flag is what
        # distinguishes a legacy run from a new one everywhere downstream.
        capability=False, performance=True,
        config_json=json.dumps(config),
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    run_id = run.id

    async def coro(h):
        return await evals.run_eval(h, run_id)

    job_id = await jobs.start("eval.run", f"Eval {name}", coro, target=name)
    return EvalStarted(run_id=run_id, job_id=job_id, message="Eval started")


@router.get("", response_model=list[EvalRunOut])
async def list_evals(session: AsyncSession = Depends(get_session)):
    await _ensure_backfilled(session)
    rows = (await session.execute(select(EvalRun).order_by(EvalRun.id.desc()))).scalars().all()
    return [EvalRunOut.of(r) for r in rows]


@router.get("/{run_id}", response_model=EvalRunDetail)
async def get_eval(run_id: int, session: AsyncSession = Depends(get_session)):
    await _ensure_backfilled(session)
    res = await session.execute(
        select(EvalRun)
        .options(selectinload(EvalRun.results), selectinload(EvalRun.perf))
        .where(EvalRun.id == run_id)
    )
    run = res.scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "Eval run not found")
    return EvalRunDetail.of_detail(run)


@router.get("/{run_id}/log", response_class=PlainTextResponse)
async def eval_log(run_id: int, session: AsyncSession = Depends(get_session)):
    """The suite's own output for a run: its progress log and result envelope.

    Kept because the file it was parsed from lives in a temporary directory
    deleted when the job ends — so before this, a run that produced a
    surprising number left nothing behind to examine.
    """
    run = await session.get(EvalRun, run_id)
    if run is None:
        raise HTTPException(404, "Eval run not found")
    if not (run.raw_envelope or run.log_tail):
        raise HTTPException(
            404,
            "No suite output was kept for this run. Runs from before v1.37.0 "
            "did not store it — re-run to capture it.",
        )
    parts = []
    if run.log_tail:
        parts.append("=== suite progress (stderr) ===\n" + run.log_tail)
    if run.raw_envelope:
        parts.append("=== result envelope (--json-file) ===\n" + run.raw_envelope)
    return "\n\n".join(parts)


@router.delete("/{run_id}", status_code=204)
async def delete_eval(run_id: int, session: AsyncSession = Depends(get_session)):
    run = await session.get(EvalRun, run_id)
    if run is None:
        raise HTTPException(404, "Eval run not found")
    await session.delete(run)
    await session.commit()
