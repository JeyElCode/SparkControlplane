"""Tool-call parser mapping and the curated model suggestion catalog.

vLLM requires both ``--enable-auto-tool-choice`` and a matching
``--tool-call-parser`` for ``tool_choice: "auto"`` to work. The mapping below
picks the right parser from a model's repo id; it can always be overridden
per instance.
"""

from __future__ import annotations

import re

from ..schemas import ModelSuggestion

# Ordered (pattern, parser). First match wins. Patterns are case-insensitive.
_PARSER_RULES: list[tuple[str, str]] = [
    (r"qwen3.*coder", "qwen3_xml"),
    (r"qwen3", "hermes"),
    (r"qwen2\.5", "hermes"),
    (r"qwen", "hermes"),
    (r"kimi[-_]?k2", "kimi_k2"),
    (r"mistral", "mistral"),
    (r"llama[-_]?3", "llama3_json"),
    (r"llama", "llama3_json"),
    (r"hermes", "hermes"),
    (r"deepseek", "deepseek_v3"),
]


def tool_parser_for(repo_id: str) -> str | None:
    """Best-guess ``--tool-call-parser`` for a HuggingFace repo id."""
    name = repo_id.lower()
    for pattern, parser in _PARSER_RULES:
        if re.search(pattern, name):
            return parser
    return None


def sanitize_name(repo_id: str) -> str:
    """Local directory name for a model: the part after the last '/', with any
    character outside ``[A-Za-z0-9._-]`` replaced by '-'. Never returns an empty
    string or a path-traversal token."""
    last = repo_id.split("/")[-1]
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", last).strip("-")
    if cleaned in ("", ".", ".."):
        cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", repo_id).strip("-") or "model"
    return cleaned


# Curated suggestions surfaced as chips in the UI. Sizes are rough FP8/quant
# footprints to budget disk; tool_parser is auto-mapped but shown for clarity.
SUGGESTIONS: list[ModelSuggestion] = [
    ModelSuggestion(
        repo_id="Qwen/Qwen3-30B-A3B-FP8",
        label="Qwen3 30B A3B (FP8)",
        approx_size_gb=33.0,
        tool_parser="hermes",
        note="MoE, fast; good general default on 2 nodes (TP=2).",
    ),
    ModelSuggestion(
        repo_id="Qwen/Qwen3-Coder-30B-A3B-Instruct",
        label="Qwen3 Coder 30B A3B",
        approx_size_gb=62.0,
        tool_parser="qwen3_xml",
        note="Coding-tuned; uses the qwen3_xml tool parser.",
    ),
    ModelSuggestion(
        repo_id="Qwen/Qwen2.5-7B-Instruct",
        label="Qwen2.5 7B Instruct",
        approx_size_gb=15.0,
        tool_parser="hermes",
        note="Small; great to pin TP=1 on a single node.",
    ),
    ModelSuggestion(
        repo_id="meta-llama/Llama-3.1-8B-Instruct",
        label="Llama 3.1 8B Instruct",
        approx_size_gb=16.0,
        tool_parser="llama3_json",
        note="Gated repo — accept the license on HF first.",
    ),
    ModelSuggestion(
        repo_id="meta-llama/Llama-3.3-70B-Instruct",
        label="Llama 3.3 70B Instruct",
        approx_size_gb=140.0,
        tool_parser="llama3_json",
        note="Large — needs both nodes (TP=2); budget disk + memory.",
    ),
    ModelSuggestion(
        repo_id="mistralai/Mistral-Small-3.2-24B-Instruct-2506",
        label="Mistral Small 3.2 24B",
        approx_size_gb=48.0,
        tool_parser="mistral",
        note="Solid mid-size general model.",
    ),
]
