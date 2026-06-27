"""Public benchmark subsets (HumanEval / GSM8K / MMLU), fetched at run time from
the HuggingFace datasets-server (real rows, no heavy deps) and mapped onto our
scorers. Results are cached per (benchmark, n) for the process lifetime.

These are intentionally *subsets* (a configurable sample) — a cheap objective
baseline filter, not a full leaderboard run.
"""

from __future__ import annotations

import re

import httpx

from .eval_suites import CapabilityTask

DATASETS = {
    "humaneval": {"dataset": "openai/openai_humaneval", "config": "openai_humaneval", "split": "test"},
    "gsm8k": {"dataset": "openai/gsm8k", "config": "main", "split": "test"},
    "mmlu": {"dataset": "cais/mmlu", "config": "all", "split": "test"},
}
BENCHMARKS = list(DATASETS.keys())

_cache: dict[tuple[str, int], list[CapabilityTask]] = {}


async def _rows(dataset: str, config: str, split: str, n: int) -> list[dict]:
    params = {"dataset": dataset, "config": config, "split": split, "offset": 0, "length": min(n, 100)}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get("https://datasets-server.huggingface.co/rows", params=params)
        r.raise_for_status()
        return [row.get("row", {}) for row in r.json().get("rows", [])]


def _humaneval(row: dict, i: int) -> CapabilityTask:
    return CapabilityTask(
        id=f"humaneval-{str(row.get('task_id', i)).replace('/', '-')}",
        category="humaneval", name=str(row.get("task_id", f"HumanEval#{i}")),
        prompt="Complete this Python function. Return the full function in a code block.\n\n"
        + row.get("prompt", ""),
        scorer="code_exec", entry_point=row.get("entry_point", ""), test_code=row.get("test", ""),
        max_tokens=1024,
    )


def _gsm8k(row: dict, i: int) -> CapabilityTask:
    ans = row.get("answer", "")
    m = re.search(r"####\s*([-\d,.]+)", ans)
    num = float(m.group(1).replace(",", "")) if m else 0.0
    return CapabilityTask(
        id=f"gsm8k-{i}", category="gsm8k", name=f"GSM8K #{i}",
        prompt=row.get("question", "") + "\n\nGive only the final numeric answer.",
        scorer="numeric", numeric_answer=num, numeric_tol=0.001, max_tokens=512,
    )


def _mmlu(row: dict, i: int) -> CapabilityTask:
    letters = ["A", "B", "C", "D"]
    choices = row.get("choices", []) or []
    body = "\n".join(f"{letters[j]}) {c}" for j, c in enumerate(choices) if j < 4)
    try:
        correct = letters[int(row.get("answer", 0))]
    except (ValueError, IndexError, TypeError):
        correct = "A"
    return CapabilityTask(
        id=f"mmlu-{i}", category="mmlu", name=f"MMLU #{i} ({row.get('subject', '')})",
        prompt=f"{row.get('question', '')}\n{body}\nAnswer with the single letter.",
        scorer="mcq", choices=letters, correct=correct, max_tokens=256,
    )


_MAPPERS = {"humaneval": _humaneval, "gsm8k": _gsm8k, "mmlu": _mmlu}


async def fetch(category: str, n: int = 20) -> list[CapabilityTask]:
    spec = DATASETS.get(category)
    if not spec:
        return []
    key = (category, n)
    if key in _cache:
        return _cache[key]
    rows = await _rows(spec["dataset"], spec["config"], spec["split"], n)
    tasks = [_MAPPERS[category](row, i) for i, row in enumerate(rows)]
    _cache[key] = tasks
    return tasks
