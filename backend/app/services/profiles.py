"""Serve profiles: known-good vLLM settings, saved and shared.

Getting a large model to serve is a dozen interacting flags — context length,
GPU memory fraction, batch limits, the right reasoning/tool parsers — and one
wrong value is an out-of-memory ten minutes into a weight load. That knowledge
otherwise lives in shell history or a stranger's README.

Two rules shape everything here:

**A profile carries serve settings only.** Never a name, port, node, API key or
TLS material: those are per-instance facts, and a profile carrying them would be
unshareable at best and a credential leak at worst.

**An imported profile is untrusted input.** It arrives from a gist or a
colleague, and its fields feed ``build_vllm_serve_cmd`` and from there a
``docker run`` on the nodes with ``--gpus all``, ``--network host`` and the
models directory mounted. ``vllm_image`` in particular would be arbitrary code
execution as root on a DGX, so import drops it — along with the raw
``extra_args`` passthrough — rather than trusting a stranger's JSON. Both remain
settable by hand on the instance itself, where the operator is the author.
"""

from __future__ import annotations

import json
import logging

from ..schemas import InstanceIn

log = logging.getLogger("spark.profiles")

__all__ = [
    "PROFILE_FIELDS",
    "IMPORT_BLOCKED_FIELDS",
    "settings_from_instance",
    "sanitize_settings",
    "validate_settings",
    "BUILTIN_PROFILES",
]

# The serve-setting surface a profile may carry. Everything here is a field of
# InstanceIn; anything absent is deliberately excluded (see the module docstring).
PROFILE_FIELDS: tuple[str, ...] = (
    "topology",
    "tensor_parallel_size",
    "max_model_len",
    "gpu_memory_utilization",
    "max_num_seqs",
    "max_num_batched_tokens",
    "dtype",
    "kv_cache_dtype",
    "block_size",
    "tokenizer_mode",
    "reasoning_parser",
    "trust_remote_code",
    "enable_tool_choice",
    "tool_parser",
    "compilation_config",
    "advanced_args",
    "vllm_image",
    "extra_args",
)

# Dropped when a profile arrives from OUTSIDE this portal. Both reach a remote
# root shell: `vllm_image` picks the container that runs with --gpus all and the
# models dir mounted, and `extra_args` is a raw flag passthrough. An operator can
# still set either by hand on an instance — the difference is authorship.
IMPORT_BLOCKED_FIELDS: frozenset[str] = frozenset({
    "vllm_image",
    "extra_args",
    # advanced_args and compilation_config are dropped for the SAME reason,
    # learned the hard way: v1.26.0 tried to keep advanced_args by filtering a
    # denylist of dangerous flags, and the denylist was bypassable within
    # minutes. `--middleware <dotted.path>` makes vLLM import an arbitrary
    # object into the API server; `--tool-parser-plugin <file.py>` executes a
    # Python file; `--allowed-local-media-path /` turns the endpoint into an
    # arbitrary file read — none of which were on the list, and none of which
    # were reported as dropped. vLLM's flag surface grows every release, so any
    # such list is out of date the moment it ships.
    #
    # An allowlist of *values* is defensible; a denylist of *flags* against an
    # upstream CLI is not. Nothing is lost that matters: the recipe this feature
    # exists for (Laguna) needs none of these, and an operator can still set all
    # of them by hand on their own profile, where they are the author.
    "advanced_args",
    "compilation_config",
})

# Legitimate on a profile you wrote; refused from one you were sent, because it
# executes code from the model repo at load time and that is a decision the
# operator should make deliberately.
_IMPORT_BLOCKED_TRUE_FLAGS: frozenset[str] = frozenset({"trust_remote_code"})


def settings_from_instance(inst) -> dict:
    """Capture a running instance's serve settings as a profile body.

    Skips None so a profile says only what it means to say — a null in a profile
    would otherwise be indistinguishable from "leave the default alone".
    """
    out: dict = {}
    for field in PROFILE_FIELDS:
        value = getattr(inst, field, None)
        if value is not None:
            out[field] = value
    return out


def sanitize_settings(raw: dict, *, trusted: bool) -> tuple[dict, list[str]]:
    """Keep only known serve fields. Returns ``(settings, dropped_field_names)``.

    ``trusted=False`` (an import) additionally drops the fields that would let a
    stranger's JSON choose what runs on the nodes.
    """
    dropped: list[str] = []
    out: dict = {}
    for key, value in (raw or {}).items():
        if key not in PROFILE_FIELDS:
            dropped.append(key)
            continue
        if not trusted and key in IMPORT_BLOCKED_FIELDS:
            dropped.append(key)
            continue
        if value is None:
            continue
        if not trusted and key in _IMPORT_BLOCKED_TRUE_FLAGS and value is True:
            dropped.append(key)
            continue
        out[key] = value
    return out, dropped


def validate_settings(settings: dict) -> dict:
    """Run the settings through the real InstanceIn validators.

    Reusing the API-boundary schema is the point: a profile must not be able to
    smuggle a value that a hand-typed instance would have rejected (invalid JSON
    in compilation_config, a malformed advanced_args array, a bogus topology).
    Raises ValueError with a readable message.
    """
    try:
        # name/model_id are required by InstanceIn but are not part of a profile;
        # supply throwaway values so the *serve* fields get validated.
        probe = InstanceIn(name="profile-probe", model_id=1, **settings)
    except Exception as exc:  # noqa: BLE001 - pydantic error -> readable message
        raise ValueError(str(exc)) from None
    return {k: v for k, v in probe.model_dump().items() if k in settings}


def parse_settings(raw_json: str | None) -> dict:
    if not raw_json:
        return {}
    try:
        data = json.loads(raw_json)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


# --- built-ins -------------------------------------------------------------
# Shipped with the image, alongside the curated model catalogue in
# services/parsers.py. Kept deliberately few: every one of these is a
# configuration someone has actually run on a DGX Spark pair, not a guess.
BUILTIN_PROFILES: list[dict] = [
    {
        "name": "laguna-fp8-dual",
        "repo_id": "poolside/Laguna-S-2.1-FP8",
        "description": (
            "Laguna-S 2.1 FP8 across both Sparks (TP=2, native distributed). "
            "117 GB of FP8 weights do not fit one node — the memory fraction and "
            "the batch limits below are what leaves room for a 128k context "
            "without OOMing during the load."
        ),
        "settings": {
            "topology": "distributed",
            "tensor_parallel_size": 2,
            "gpu_memory_utilization": 0.72,
            "max_model_len": 131072,
            "max_num_seqs": 8,
            "max_num_batched_tokens": 2048,
            "reasoning_parser": "poolside_v1",
            "tool_parser": "poolside_v1",
            "enable_tool_choice": True,
        },
    },
    {
        "name": "single-node-small",
        "description": (
            "A small model pinned to one node (TP=1), leaving the other Spark "
            "free. Sensible defaults only — adjust the context length to the "
            "model."
        ),
        "settings": {
            "topology": "single",
            "tensor_parallel_size": 1,
            "gpu_memory_utilization": 0.85,
            "enable_tool_choice": True,
        },
    },
    {
        "name": "dual-node-balanced",
        "description": (
            "General-purpose two-node starting point (TP=2). A reasonable place "
            "to begin for a mid-size model, then tune the context length and "
            "memory fraction from what the Dashboard reports under load."
        ),
        "settings": {
            "topology": "distributed",
            "tensor_parallel_size": 2,
            "gpu_memory_utilization": 0.85,
            "enable_tool_choice": True,
        },
    },
]
