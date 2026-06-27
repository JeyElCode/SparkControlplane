from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..db import get_session
from ..models import CustomTask, EvalRun
from ..schemas import (
    CatalogOut,
    CustomTaskIn,
    CustomTaskOut,
    EvalRunDetail,
    EvalRunOut,
    EvalRunRequest,
    EvalStarted,
    SuiteInfo,
)
from ..services import custom_tasks, eval_suites, evals, public_benchmarks
from ..services.instances import load_instance
from ..services.jobs import jobs

router = APIRouter(prefix="/api/evals", tags=["evals"])


@router.get("/suites", response_model=list[SuiteInfo])
async def list_suites():
    return eval_suites.suite_summary()


@router.get("/catalog", response_model=CatalogOut)
async def catalog(session: AsyncSession = Depends(get_session)):
    return CatalogOut(
        capability=eval_suites.suite_summary(),
        benchmarks=public_benchmarks.BENCHMARKS,
        custom_categories=await custom_tasks.custom_categories(session),
    )


def _apply_task(ct: CustomTask, p: CustomTaskIn) -> None:
    ct.category, ct.name, ct.prompt, ct.scorer, ct.system = p.category, p.name, p.prompt, p.scorer, p.system
    ct.answer = p.answer
    ct.contains_json = json.dumps(p.contains) if p.contains else None
    ct.numeric_answer, ct.numeric_tol = p.numeric_answer, p.numeric_tol
    ct.choices_json = json.dumps(p.choices) if p.choices else None
    ct.correct, ct.rubric = p.correct, p.rubric
    ct.entry_point, ct.test_code, ct.code_prefix = p.entry_point, p.test_code, p.code_prefix
    ct.tools_json = json.dumps(p.tools) if p.tools else None
    ct.expected_tool = p.expected_tool
    ct.expected_args_json = json.dumps(p.expected_args) if p.expected_args else None
    ct.forbid_tool_call, ct.max_tokens, ct.enabled = p.forbid_tool_call, p.max_tokens, p.enabled


@router.get("/tasks", response_model=list[CustomTaskOut])
async def list_tasks(session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(CustomTask).order_by(CustomTask.id.desc()))).scalars().all()
    return [CustomTaskOut.of(c) for c in rows]


@router.post("/tasks", response_model=CustomTaskOut, status_code=201)
async def create_task(payload: CustomTaskIn, session: AsyncSession = Depends(get_session)):
    ct = CustomTask()
    _apply_task(ct, payload)
    session.add(ct)
    await session.commit()
    await session.refresh(ct)
    return CustomTaskOut.of(ct)


@router.patch("/tasks/{task_id}", response_model=CustomTaskOut)
async def update_task(task_id: int, payload: CustomTaskIn, session: AsyncSession = Depends(get_session)):
    ct = await session.get(CustomTask, task_id)
    if ct is None:
        raise HTTPException(404, "Task not found")
    _apply_task(ct, payload)
    await session.commit()
    return CustomTaskOut.of(ct)


@router.delete("/tasks/{task_id}", status_code=204)
async def delete_task(task_id: int, session: AsyncSession = Depends(get_session)):
    ct = await session.get(CustomTask, task_id)
    if ct is None:
        raise HTTPException(404, "Task not found")
    await session.delete(ct)
    await session.commit()


@router.post("", response_model=EvalStarted)
async def create_eval(payload: EvalRunRequest, session: AsyncSession = Depends(get_session)):
    inst = await load_instance(session, payload.instance_id)
    if inst is None:
        raise HTTPException(404, "Instance not found")
    if not payload.capability and not payload.performance:
        raise HTTPException(400, "Enable capability and/or performance.")
    if not payload.categories:
        raise HTTPException(400, "Select at least one category.")

    label = f"{inst.topology} TP={inst.tensor_parallel_size} :{inst.port}"
    model_name = inst.model.name if inst.model else "?"
    name = payload.name or f"{model_name} — {inst.name}"
    judge = payload.judge.model_dump() if payload.judge else {"type": "none"}
    config = {
        "instance_id": payload.instance_id,
        "categories": payload.categories,
        "perf_reps": payload.perf_reps,
        "concurrency": payload.concurrency,
        "temperature": payload.temperature,
        "judge": judge,
        "sandbox_image": payload.sandbox_image,
    }
    run = EvalRun(
        name=name, instance_id=inst.id, model_name=model_name, instance_label=label,
        categories=",".join(payload.categories), capability=payload.capability,
        performance=payload.performance, config_json=json.dumps(config),
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
    rows = (await session.execute(select(EvalRun).order_by(EvalRun.id.desc()))).scalars().all()
    return [EvalRunOut.of(r) for r in rows]


@router.get("/{run_id}", response_model=EvalRunDetail)
async def get_eval(run_id: int, session: AsyncSession = Depends(get_session)):
    res = await session.execute(
        select(EvalRun)
        .options(selectinload(EvalRun.results), selectinload(EvalRun.perf))
        .where(EvalRun.id == run_id)
    )
    run = res.scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "Eval run not found")
    return EvalRunDetail.of_detail(run)


@router.delete("/{run_id}", status_code=204)
async def delete_eval(run_id: int, session: AsyncSession = Depends(get_session)):
    run = await session.get(EvalRun, run_id)
    if run is None:
        raise HTTPException(404, "Eval run not found")
    await session.delete(run)
    await session.commit()
