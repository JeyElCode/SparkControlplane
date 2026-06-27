"""Task schema for evaluations.

Capability tasks are now entirely **user-authored** (see custom_tasks.py); this
module defines the shared :class:`CapabilityTask` shape they map to, plus the
built-in **performance** prompts used to measure throughput (tokens/sec, TTFT)
per category.

Scorers a capability task may use: ``exact``, ``contains``, ``numeric``, ``mcq``,
``judge`` (LLM rubric), ``code_exec`` (sandboxed pass@1), ``tool_call`` (tool use).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CapabilityTask:
    id: str
    category: str
    name: str
    prompt: str
    scorer: str
    system: str | None = None
    answer: str | None = None
    contains: list[str] = field(default_factory=list)
    numeric_answer: float | None = None
    numeric_tol: float = 0.01
    choices: list[str] = field(default_factory=list)
    correct: str | None = None
    rubric: str | None = None
    entry_point: str | None = None
    test_code: str | None = None
    code_prefix: str | None = None
    tools: list[dict] = field(default_factory=list)
    expected_tool: str | None = None
    expected_args: dict = field(default_factory=dict)
    forbid_tool_call: bool = False
    max_tokens: int = 1024


@dataclass
class PerfTask:
    id: str
    category: str
    name: str
    prompt: str
    max_tokens: int = 512
    system: str | None = None


# --- Performance prompts (the tokens/sec tests) --------------------------
PERF_TASKS: list[PerfTask] = [
    PerfTask(
        id="perf_coding",
        category="coding",
        name="Code generation",
        prompt="Write a complete, well-documented Python implementation of an LRU cache "
        "class with get/put in O(1), including docstrings and a few usage examples.",
        max_tokens=768,
    ),
    PerfTask(
        id="perf_reasoning",
        category="reasoning",
        name="Multi-step reasoning",
        prompt="A factory has three machines. Machine A makes 120 units/hour, B makes 90, "
        "C makes 75. They run 7.5 hours/day with a 30-minute shared maintenance stop. "
        "Walk through, step by step, the total daily output, then the weekly output for a "
        "6-day week. Show your reasoning.",
        max_tokens=512,
    ),
    PerfTask(
        id="perf_textgen",
        category="textgen",
        name="Free-form generation",
        prompt="Write a detailed 500-word technical overview of how a tensor-parallel LLM "
        "inference server distributes work across multiple GPUs.",
        max_tokens=768,
    ),
    PerfTask(
        id="perf_judging",
        category="judging",
        name="Short structured verdict",
        prompt="Given two short answers to a trivia question, respond ONLY with a compact "
        "JSON object {\"winner\": 1|2, \"reason\": \"...\"}. Question: 'capital of France?' "
        "Answer 1: 'Paris'. Answer 2: 'Lyon'.",
        max_tokens=128,
    ),
]


def perf_tasks(categories: list[str]) -> list[PerfTask]:
    return [t for t in PERF_TASKS if t.category in categories]


def perf_categories() -> list[str]:
    """Distinct categories that have a performance prompt."""
    seen: list[str] = []
    for t in PERF_TASKS:
        if t.category not in seen:
            seen.append(t.category)
    return seen
