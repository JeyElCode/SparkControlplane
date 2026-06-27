"""Built-in evaluation suites.

A capability task carries a prompt and a *scorer* describing how to grade the
model's answer:

* ``exact``     — the expected ``answer`` (normalized) appears in the response
* ``contains``  — every string in ``contains`` appears (case-insensitive)
* ``numeric``   — a number within ``numeric_tol`` of ``numeric_answer`` appears
* ``mcq``       — the model picks the right option from ``choices`` (``correct``)
* ``judge``     — an LLM judge scores the answer 0–10 against ``rubric``
* ``code_exec`` — the model writes ``entry_point``; ``test_code`` defines
                  ``check(candidate)`` which is run in a sandbox (pass@1)

Performance tasks are just prompts (per category) used to measure throughput.
The set below is a functional starter suite; it is intentionally easy to extend.
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
    max_tokens: int = 1024


@dataclass
class PerfTask:
    id: str
    category: str
    name: str
    prompt: str
    max_tokens: int = 512
    system: str | None = None


# --- Capability suites ---------------------------------------------------
_CODING: list[CapabilityTask] = [
    CapabilityTask(
        id="is_prime",
        category="coding",
        name="Primality test",
        prompt="Write a Python function `def is_prime(n: int) -> bool` that returns "
        "whether n is a prime number. Return only the function in a Python code block.",
        scorer="code_exec",
        entry_point="is_prime",
        test_code=(
            "def check(candidate):\n"
            "    assert candidate(2) is True\n"
            "    assert candidate(13) is True\n"
            "    assert candidate(97) is True\n"
            "    assert candidate(1) is False\n"
            "    assert candidate(0) is False\n"
            "    assert candidate(4) is False\n"
            "    assert candidate(100) is False\n"
        ),
    ),
    CapabilityTask(
        id="flatten",
        category="coding",
        name="Deep flatten",
        prompt="Write a Python function `def flatten(xs)` that fully flattens an "
        "arbitrarily nested list of integers into a flat list, preserving order. "
        "Return only the function in a Python code block.",
        scorer="code_exec",
        entry_point="flatten",
        test_code=(
            "def check(candidate):\n"
            "    assert candidate([1, [2, [3, 4], 5]]) == [1, 2, 3, 4, 5]\n"
            "    assert candidate([]) == []\n"
            "    assert candidate([[[1]]]) == [1]\n"
            "    assert candidate([1, 2, 3]) == [1, 2, 3]\n"
        ),
    ),
    CapabilityTask(
        id="word_count",
        category="coding",
        name="Word frequency",
        prompt="Write a Python function `def word_count(s: str) -> dict` that returns a "
        "dict mapping each lowercased whitespace-separated word to its count. "
        "Return only the function in a Python code block.",
        scorer="code_exec",
        entry_point="word_count",
        test_code=(
            "def check(candidate):\n"
            "    assert candidate('a b a') == {'a': 2, 'b': 1}\n"
            "    assert candidate('The the THE') == {'the': 3}\n"
            "    assert candidate('') == {}\n"
        ),
    ),
]

_SECURITY: list[CapabilityTask] = [
    CapabilityTask(
        id="sqli",
        category="security",
        name="Prevent SQL injection",
        prompt="Explain the most robust way to prevent SQL injection in a web "
        "application, and why.",
        scorer="judge",
        rubric="Full marks require: parameterized queries / prepared statements (or a "
        "well-used ORM) as the primary defense; mentions that string concatenation of "
        "user input is the root cause; bonus for least-privilege DB accounts and input "
        "validation as defense-in-depth. Penalize answers that rely only on escaping or "
        "blocklists.",
    ),
    CapabilityTask(
        id="clickjacking",
        category="security",
        name="Clickjacking header",
        prompt="Which HTTP response header best mitigates clickjacking?\n"
        "A) X-Frame-Options\nB) Accept-Encoding\nC) ETag\nD) Referer\n"
        "Answer with the single letter.",
        scorer="mcq",
        choices=["A", "B", "C", "D"],
        correct="A",
    ),
    CapabilityTask(
        id="password_storage",
        category="security",
        name="Secure password storage",
        prompt="Describe how to securely store user passwords in a database.",
        scorer="judge",
        rubric="Full marks require: a slow, salted password hashing algorithm "
        "(bcrypt, scrypt, or Argon2) with a per-user salt; never storing plaintext; "
        "explicitly avoiding fast/general hashes (MD5/SHA-1/SHA-256 alone). Bonus for "
        "peppering or work-factor tuning. Penalize plaintext, reversible encryption, or "
        "unsalted hashes.",
    ),
]

_REASONING: list[CapabilityTask] = [
    CapabilityTask(
        id="avg_speed",
        category="reasoning",
        name="Average speed",
        prompt="A train travels 60 km in 1.5 hours. What is its average speed in km/h? "
        "Give just the number.",
        scorer="numeric",
        numeric_answer=40.0,
        numeric_tol=0.5,
        max_tokens=256,
    ),
    CapabilityTask(
        id="sequence",
        category="reasoning",
        name="Number sequence",
        prompt="What number comes next in the sequence 2, 4, 8, 16, ...?\n"
        "A) 20\nB) 24\nC) 32\nD) 30\nAnswer with the single letter.",
        scorer="mcq",
        choices=["A", "B", "C", "D"],
        correct="C",
        max_tokens=256,
    ),
    CapabilityTask(
        id="transitive",
        category="reasoning",
        name="Transitive ordering",
        prompt="Alice is taller than Bob. Bob is taller than Carol. Who is the shortest? "
        "Explain briefly.",
        scorer="contains",
        contains=["carol"],
        max_tokens=256,
    ),
]

_JUDGING: list[CapabilityTask] = [
    CapabilityTask(
        id="pick_correct_math",
        category="judging",
        name="Pick the correct answer",
        prompt="You are grading two answers to the question 'What is 17 * 23?'.\n"
        "Answer 1: 391\nAnswer 2: 391... actually 401\n"
        "Which answer is correct? Reply with just '1' or '2'.",
        scorer="mcq",
        choices=["1", "2"],
        correct="1",
        max_tokens=128,
    ),
    CapabilityTask(
        id="pick_better_code",
        category="judging",
        name="Pick the better solution",
        prompt="Two functions claim to return the maximum of a list.\n"
        "Solution 1:\n```python\ndef m(x):\n    return x[0]\n```\n"
        "Solution 2:\n```python\ndef m(x):\n    return max(x)\n```\n"
        "Which solution is correct for any non-empty list? Reply with just '1' or '2'.",
        scorer="mcq",
        choices=["1", "2"],
        correct="2",
        max_tokens=128,
    ),
]

CAPABILITY_SUITES: dict[str, list[CapabilityTask]] = {
    "coding": _CODING,
    "security": _SECURITY,
    "reasoning": _REASONING,
    "judging": _JUDGING,
}


# --- Performance prompts -------------------------------------------------
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


def capability_tasks(categories: list[str]) -> list[CapabilityTask]:
    out: list[CapabilityTask] = []
    for c in categories:
        out.extend(CAPABILITY_SUITES.get(c, []))
    return out


def perf_tasks(categories: list[str]) -> list[PerfTask]:
    return [t for t in PERF_TASKS if t.category in categories]


def suite_summary() -> list[dict]:
    """For the API: category -> task counts."""
    out = []
    for cat, tasks in CAPABILITY_SUITES.items():
        out.append(
            {
                "category": cat,
                "capability_tasks": len(tasks),
                "perf_tasks": len([t for t in PERF_TASKS if t.category == cat]),
                "scorers": sorted({t.scorer for t in tasks}),
            }
        )
    # textgen has perf-only
    out.append(
        {"category": "textgen", "capability_tasks": 0,
         "perf_tasks": len([t for t in PERF_TASKS if t.category == "textgen"]), "scorers": []}
    )
    return out
