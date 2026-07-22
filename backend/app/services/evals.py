"""Evaluation engine: runs capability tasks (deterministic / judge / sandboxed
code) and performance benchmarks (single-stream + concurrency sweep) against a
model instance, scores them, and persists results for later comparison.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from ..crypto import decrypt
from ..db import SessionLocal, get_node_by_role, get_setting
from ..models import (
    JOB_ERROR,
    JOB_RUNNING,
    JOB_SUCCESS,
    EvalResult,
    EvalRun,
    PerfResult,
)
from ..ssh import ssh_for_node
from . import custom_tasks, eval_suites
from .instances import load_instance
from .jobs import JobHandle
from .llm_client import ToolCall, chat_once, chat_stream


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


async def _resolve_judge(session: AsyncSession, cfg: dict) -> Endpoint | None:
    judge = cfg.get("judge") or {}
    jtype = judge.get("type")
    if jtype == "instance" and judge.get("instance_id"):
        return await _instance_endpoint(session, int(judge["instance_id"]))
    if jtype == "external":
        s = await get_setting(session)
        base = getattr(s, "judge_base_url", None)
        model = getattr(s, "judge_model", None)
        key = decrypt(getattr(s, "judge_api_key_enc", None))
        if not base or not model:
            return None
        return Endpoint(base.rstrip("/"), model, key, f"external:{model}")
    return None


# --- deterministic scorers ----------------------------------------------
def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def _score_exact(resp: str, answer: str) -> float:
    return 1.0 if _norm(answer) in _norm(resp) else 0.0


def _score_contains(resp: str, subs: list[str]) -> float:
    low = resp.lower()
    return 1.0 if subs and all(s.lower() in low for s in subs) else 0.0


def _score_numeric(resp: str, target: float, tol: float) -> tuple[float, str | None]:
    nums = re.findall(r"-?\d+(?:\.\d+)?", resp.replace(",", ""))
    for n in nums:
        try:
            if abs(float(n) - target) <= tol:
                return 1.0, n
        except ValueError:
            continue
    return 0.0, (nums[-1] if nums else None)


def _score_mcq(resp: str, choices: list[str], correct: str) -> tuple[float, str | None]:
    lines = [ln.strip() for ln in resp.splitlines() if ln.strip()]
    cand: str | None = None
    if lines:  # prefer a bare choice on the last line
        last = lines[-1]
        for ch in choices:
            if re.fullmatch(rf"[^A-Za-z0-9]*{re.escape(ch)}[^A-Za-z0-9]*", last, re.I):
                cand = ch
                break
    if cand is None:
        m = re.search(r"\b(" + "|".join(re.escape(c) for c in choices) + r")\b", resp, re.I)
        if m:
            cand = m.group(1)
    ok = cand is not None and cand.upper() == correct.upper()
    return (1.0 if ok else 0.0), cand


# --- judge ---------------------------------------------------------------
_JUDGE_SYS = (
    "You are a strict grader. Score the answer from 0 to 10 against the rubric. "
    'Respond with ONLY a JSON object: {"score": <0-10 number>, "reason": "<one sentence>"}.'
)


async def _judge_score(judge: Endpoint, task_prompt: str, response: str, rubric: str):
    user = (
        f"TASK:\n{task_prompt}\n\nRUBRIC:\n{rubric}\n\nANSWER:\n{response}\n\n"
        "Return only the JSON object."
    )
    res = await chat_stream(
        judge.base_url,
        judge.model,
        [{"role": "system", "content": _JUDGE_SYS}, {"role": "user", "content": user}],
        max_tokens=300,
        temperature=0.0,
        api_key=judge.api_key,
        verify=judge.verify,
    )
    if not res.ok:
        return None, f"judge error: {res.error}"
    m = re.search(r"\{.*\}", res.content, re.DOTALL)
    if not m:
        return None, f"unparseable judge output: {res.content[:160]}"
    try:
        data = json.loads(m.group(0))
        score = float(data.get("score"))
        score = max(0.0, min(10.0, score))
        return score / 10.0, str(data.get("reason", ""))[:500]
    except (ValueError, TypeError):
        return None, f"bad judge json: {res.content[:160]}"


# --- tool-use scoring ----------------------------------------------------
def _score_tool_call(calls: list[ToolCall], expected_tool, expected_args, forbid):
    if forbid:
        if not calls:
            return 1.0, "no tool call — refused (correct)"
        return 0.0, f"called {calls[0].name} (should have refused)"
    if not calls:
        return 0.0, "no tool call emitted"
    call = calls[0]
    if expected_tool and call.name != expected_tool:
        return 0.0, f"called {call.name}, expected {expected_tool}"
    try:
        args = json.loads(call.arguments) if call.arguments else {}
    except (ValueError, TypeError):
        args = {}
    for k, exp in (expected_args or {}).items():
        if str(exp).lower() not in str(args.get(k, "")).lower():
            return 0.0, f"arg {k}={args.get(k)!r} missing '{exp}'"
    return 1.0, f"called {call.name}({call.arguments})"


async def _run_tool_task(session, handle, run, task, target, cfg) -> float:
    er = EvalResult(
        run_id=run.id, category=task.category, task_id=task.id, task_name=task.name,
        scorer="tool_call", prompt=task.prompt,
    )
    res = await chat_once(
        target.base_url, target.model, [{"role": "user", "content": task.prompt}],
        tools=task.tools, tool_choice="auto", max_tokens=task.max_tokens,
        temperature=float(cfg.get("temperature", 0.2)), api_key=target.api_key,
        verify=target.verify,
    )
    er.latency_ms = res.latency_ms
    er.prompt_tokens, er.completion_tokens = res.prompt_tokens, res.completion_tokens
    calls_txt = "; ".join(f"{c.name}({c.arguments})" for c in res.tool_calls) or "(none)"
    er.response = ((res.content or "") + f"\n[tool_calls] {calls_txt}")[:8000]
    if not res.ok:
        er.error, er.score, er.passed = res.error, 0.0, False
    else:
        er.score, reason = _score_tool_call(
            res.tool_calls, task.expected_tool, task.expected_args, task.forbid_tool_call
        )
        er.judge_reason, er.passed = reason, er.score >= 0.5
    session.add(er)
    await _commit(session, handle)
    await handle.log(f"[tools/{task.id}] {task.name}: score={er.score:.2f} — {er.judge_reason or er.error}")
    return er.score


# --- capability ----------------------------------------------------------
async def _run_capability_task(session, handle, run, task, target, judge, code_ssh, cfg) -> float:
    if task.scorer == "tool_call":
        return await _run_tool_task(session, handle, run, task, target, cfg)
    er = EvalResult(
        run_id=run.id, category=task.category, task_id=task.id, task_name=task.name,
        scorer=task.scorer, prompt=task.prompt,
    )
    messages = ([{"role": "system", "content": task.system}] if task.system else []) + [
        {"role": "user", "content": task.prompt}
    ]
    res = await chat_stream(
        target.base_url, target.model, messages,
        max_tokens=task.max_tokens, temperature=float(cfg.get("temperature", 0.2)),
        api_key=target.api_key, verify=target.verify,
    )
    er.response = (res.content or "")[:8000]
    if not res.ok:
        er.error, er.score, er.passed = res.error, 0.0, False
        session.add(er)
        await _commit(session, handle)
        await handle.log(f"[{task.category}/{task.id}] request error: {res.error}", "error")
        return 0.0

    m = res.metrics
    er.latency_ms, er.ttft_ms = m.total_ms, m.ttft_ms
    er.prompt_tokens, er.completion_tokens, er.tokens_per_sec = (
        m.prompt_tokens, m.completion_tokens, m.tokens_per_sec,
    )

    try:
        if task.scorer == "exact":
            er.score = _score_exact(res.content, task.answer or "")
        elif task.scorer == "contains":
            er.score = _score_contains(res.content, task.contains)
        elif task.scorer == "numeric":
            er.score, picked = _score_numeric(res.content, task.numeric_answer or 0.0, task.numeric_tol)
            er.judge_reason = f"extracted {picked}"
        elif task.scorer == "mcq":
            er.score, picked = _score_mcq(res.content, task.choices, task.correct or "")
            er.judge_reason = f"picked {picked} (correct {task.correct})"
        elif task.scorer == "judge":
            if judge is None:
                er.error, er.score = "no judge configured", 0.0
            else:
                s, reason = await _judge_score(judge, task.prompt, res.content, task.rubric or "")
                er.score = s or 0.0
                er.judge_reason = reason
                if s is None:
                    er.error = reason
        elif task.scorer == "code_exec":
            from .sandbox import extract_code, run_code_tests

            code = extract_code(res.content)
            if task.code_prefix:  # e.g. HumanEval signature the completion attaches to
                code = task.code_prefix.rstrip() + "\n" + code
            passed, detail = await run_code_tests(
                code_ssh, code=code, test_code=task.test_code or "",
                entry_point=task.entry_point or "", image=cfg.get("sandbox_image", "python:3.12-slim"),
            )
            er.score, er.passed, er.judge_reason = (1.0 if passed else 0.0), passed, detail[:1500]
        else:
            er.error, er.score = f"unknown scorer {task.scorer}", 0.0
    except Exception as exc:  # noqa: BLE001 - a scorer failure shouldn't kill the run
        er.error, er.score = f"scoring error: {exc}", 0.0

    if er.passed is None:
        er.passed = er.score >= 0.5
    session.add(er)
    await _commit(session, handle)
    tps = f"{m.tokens_per_sec:.0f} tok/s" if m.tokens_per_sec else "n/a"
    await handle.log(f"[{task.category}/{task.id}] {task.name}: score={er.score:.2f} ({tps})")
    return er.score


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
        judge = await _resolve_judge(session, cfg)
        if judge:
            run.judge_desc = judge.desc
            await handle.log(f"Judge: {judge.desc}")

        head = await get_node_by_role(session, "head")
        code_ssh = None
        if head:
            try:
                code_ssh = await ssh_for_node(session, head)
            except Exception as exc:  # noqa: BLE001 - code-exec tasks will just skip
                await handle.log(f"code execution unavailable (no SSH to head): {exc}", "error")

        try:
            if run.capability:
                tasks = await custom_tasks.load_custom(session, categories)
                await handle.log(f"Running {len(tasks)} custom capability tasks…")
                for i, task in enumerate(tasks):
                    if task.scorer == "code_exec" and code_ssh is None:
                        await handle.log(f"[{task.id}] skipped: no node for code execution", "error")
                        continue
                    await _run_capability_task(session, handle, run, task, target, judge, code_ssh, cfg)
                    await handle.set_progress((i + 1) / max(len(tasks), 1) * (0.6 if run.performance else 1.0))

            if run.performance:
                ptasks = eval_suites.perf_tasks(categories)
                conc = cfg.get("concurrency") or [1]
                reps = int(cfg.get("perf_reps", 3))
                total = max(len(ptasks) * len(conc), 1)
                done = 0
                await handle.log(f"Running performance benchmarks ({len(ptasks)} prompts × {conc})…")
                for pt in ptasks:
                    for c in conc:
                        await _run_perf(session, handle, run, pt, target, int(c), reps, cfg)
                        done += 1
                        await handle.set_progress(0.6 + done / total * 0.4)

            await _finalize(session, handle, run)
            run.status = JOB_SUCCESS
            run.finished_at = _now()
            await _commit(session, handle)
            return f"Eval '{run.name}' complete (overall {(run.overall_score or 0) * 100:.0f}%)"
        except Exception as exc:
            run.status = JOB_ERROR
            run.finished_at = _now()
            await _commit(session, handle)
            raise


async def _finalize(session: AsyncSession, handle: JobHandle, run: EvalRun) -> None:
    res = (await session.execute(select(EvalResult).where(EvalResult.run_id == run.id))).scalars().all()
    perf = (await session.execute(select(PerfResult).where(PerfResult.run_id == run.id))).scalars().all()

    by_cat: dict[str, list[float]] = {}
    for er in res:
        by_cat.setdefault(er.category, []).append(er.score)
    cat_scores = {c: round(sum(v) / len(v), 4) for c, v in by_cat.items() if v}
    all_scores = [er.score for er in res]
    run.overall_score = round(sum(all_scores) / len(all_scores), 4) if all_scores else None

    peak = max((p.throughput_tps or 0) for p in perf) if perf else None
    summary = {
        "category_scores": cat_scores,
        "overall": run.overall_score,
        "capability_tasks": len(res),
        "peak_throughput_tps": peak,
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
    await handle.log(f"Done. Overall capability {(run.overall_score or 0) * 100:.0f}%, peak {peak or 0:.0f} tok/s")
