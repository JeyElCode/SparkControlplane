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
#
# The first three are a deliberate PREDICTABILITY LADDER, and the reason they
# exist is that a single tok/s number hides the most important tradeoff on this
# hardware.
#
# Decode speed is not one property of a model — it depends on how predictable
# the next token is. Speculative decoding (MTP, EAGLE, n-gram, a draft model)
# proposes several tokens ahead and keeps them only when the target model
# agrees. On highly predictable text nearly every draft is accepted and
# throughput multiplies; on genuinely creative text most drafts are rejected and
# the wasted forward passes make it *slower than not speculating at all*.
#
# A real measurement on a DGX Spark pair: an MTP-enabled build scored
# 79 / 72 / 35 tok/s on predictable / code / creative against 62 / 65 / 38 for
# the build without it. Faster on two, slower on the third. Averaged into one
# number, that instance looks like a modest win; measured this way, you can see
# exactly what you are trading and for which workload.
#
#   predictable — near-zero entropy. Counting is the canonical maximum-acceptance
#                 case, so this is close to the speculative ceiling.
#   code        — structurally constrained but not memorised: the realistic
#                 middle, and the regime most operators actually care about.
#   creative    — high entropy, no right answer. The speculative floor, and the
#                 honest worst case.
PERF_TASKS: list[PerfTask] = [
    PerfTask(
        id="perf_predictable",
        category="predictable",
        name="Predictable output (speculative ceiling)",
        prompt="Count from 1 to 300. Output only the numbers separated by single "
        "spaces, on one line, with no commentary before or after.",
        # Enough headroom to actually reach 300 — truncating mid-sequence would
        # measure the truncation, not the decode rate.
        max_tokens=1200,
    ),
    PerfTask(
        id="perf_code",
        category="code",
        name="Code generation (mid predictability)",
        prompt="Write a complete, well-documented Python implementation of an LRU cache "
        "class with get/put in O(1), including docstrings and a few usage examples.",
        max_tokens=768,
    ),
    PerfTask(
        id="perf_creative",
        category="creative",
        name="Creative writing (speculative floor)",
        prompt="Write an original 500-word short story about a lighthouse keeper who "
        "discovers something unexpected in the fog. Do not reuse a familiar plot; "
        "invent the details as you go.",
        # Temperature is set per-run; the prompt itself is what makes this
        # unpredictable, so the same run config still compares like for like.
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

# The three that form the predictability ladder, in ceiling -> floor order.
# Reported together as a triple; comparing instances on any one of them alone
# is how you end up preferring a build that is worse at your actual workload.
SPEED_LADDER: tuple[str, ...] = ("predictable", "code", "creative")


def perf_tasks(categories: list[str]) -> list[PerfTask]:
    return [t for t in PERF_TASKS if t.category in categories]


def perf_categories() -> list[str]:
    """Distinct categories that have a performance prompt."""
    seen: list[str] = []
    for t in PERF_TASKS:
        if t.category not in seen:
            seen.append(t.category)
    return seen
