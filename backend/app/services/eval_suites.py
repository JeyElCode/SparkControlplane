"""Prompts for the speed benchmark.

Defines the :class:`PerfTask` shape and the built-in prompts used to measure
tokens/sec and TTFT. The three that form :data:`SPEED_LADDER` are the default
and the reason this module exists in its current form — see the note above
``PERF_TASKS``.

This is the seam an additional QUALITY suite attaches to: add
``quality_tasks()`` / ``quality_categories()`` alongside ``perf_tasks()`` /
``perf_categories()``, and write ``EvalResult`` rows. The run detail's
by-category breakdown and task table already read those and self-hide when the
set is empty, so nothing else has to change to relight them.
"""

from __future__ import annotations

from dataclasses import dataclass


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
