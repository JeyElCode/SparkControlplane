"""Read a model's shape from its HuggingFace ``config.json``.

The planner in :mod:`app.services.plan` needs four numbers to size a KV cache:
layer count, KV-head count, head dimension, and the trained context window.
Those live in the model repo's ``config.json``, and every one of them has three
or four spellings in the wild — the field names below are the union of what
Llama, Qwen, Mistral, DeepSeek, Gemma and the multimodal wrappers actually use.

Everything here is best-effort by design. A missing or unparseable config must
degrade the plan to "conservative, and here's why", never fail the request:
the whole point of the feature is that a first-time operator gets a working
instance without knowing any of this.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger("spark.hfmeta")

__all__ = ["ModelShape", "shape_from_config", "fetch_shape"]

# Bytes per element, by the config's declared weight dtype. Used for the KV
# cache, which vLLM allocates in the model's compute dtype unless overridden
# with --kv-cache-dtype.
_DTYPE_BYTES: dict[str, int] = {
    "float32": 4, "float": 4, "f32": 4,
    "bfloat16": 2, "bf16": 2,
    "float16": 2, "half": 2, "f16": 2,
    "float8_e4m3fn": 1, "float8": 1, "fp8": 1,
    "int8": 1, "uint8": 1,
}


@dataclass
class ModelShape:
    """The KV-cache-relevant geometry of a model. Every field is optional —
    absence means "could not be determined", which the planner reports rather
    than guessing around."""

    context_len: int | None = None
    num_layers: int | None = None
    num_kv_heads: int | None = None
    head_dim: int | None = None
    torch_dtype: str | None = None

    @property
    def complete(self) -> bool:
        """True when the KV-cache formula can actually be evaluated."""
        return all(
            isinstance(v, int) and v > 0
            for v in (self.num_layers, self.num_kv_heads, self.head_dim)
        )

    def kv_bytes_per_token(self, kv_dtype: str | None = None) -> int | None:
        """Bytes of KV cache consumed by one token of context, all layers.

        ``2`` is the K and the V tensor. This is the same arithmetic vLLM does
        when it decides how many blocks fit, which is why an instance whose
        context is too long dies during the weight load rather than at request
        time.
        """
        if not self.complete:
            return None
        elem = _DTYPE_BYTES.get((kv_dtype or self.torch_dtype or "bfloat16").lower(), 2)
        return 2 * self.num_layers * self.num_kv_heads * self.head_dim * elem  # type: ignore[operator]


def _first_int(d: dict, *keys: str) -> int | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, bool):  # bool is an int subclass; never a dimension
            continue
        if isinstance(v, int) and v > 0:
            return v
        if isinstance(v, str):
            try:
                n = int(v)
            except ValueError:
                continue
            if n > 0:
                return n
    return None


def shape_from_config(config: dict) -> ModelShape:
    """Parse a ``config.json`` body into a :class:`ModelShape`.

    Pure and total: any shape of input produces a ModelShape, possibly empty.
    """
    if not isinstance(config, dict):
        return ModelShape()

    # Multimodal and speculative-decoding wrappers nest the language model's
    # real geometry one level down; the outer object has no layer count at all.
    inner = config
    for key in ("text_config", "language_config", "llm_config", "decoder"):
        sub = config.get(key)
        if isinstance(sub, dict) and _first_int(sub, "num_hidden_layers", "n_layer", "n_layers"):
            inner = sub
            break

    num_layers = _first_int(inner, "num_hidden_layers", "n_layer", "n_layers", "num_layers")
    num_attn_heads = _first_int(inner, "num_attention_heads", "n_head", "n_heads")
    # Absent num_key_value_heads means multi-head attention, where every query
    # head has its own KV head — NOT "unknown". Getting this wrong the other way
    # would under-count the cache by the GQA ratio (8x on Llama 3) and plan a
    # context that OOMs.
    num_kv_heads = _first_int(inner, "num_key_value_heads", "num_kv_heads", "n_kv_heads")
    if num_kv_heads is None:
        num_kv_heads = num_attn_heads

    head_dim = _first_int(inner, "head_dim", "attention_head_dim", "v_head_dim", "kv_lora_rank")
    if head_dim is None:
        hidden = _first_int(inner, "hidden_size", "n_embd", "d_model")
        if hidden and num_attn_heads:
            head_dim = hidden // num_attn_heads or None

    context_len = _first_int(
        inner, "max_position_embeddings", "max_sequence_length", "seq_length",
        "n_positions", "max_seq_len", "model_max_length",
    )

    dtype = inner.get("torch_dtype") or config.get("torch_dtype")
    if isinstance(dtype, dict):  # some configs carry a per-module mapping
        dtype = None
    quant = config.get("quantization_config")
    if isinstance(quant, dict):
        # An FP8/AWQ checkpoint still runs attention — and therefore the KV
        # cache — in the compute dtype, not the weight dtype. Leave `dtype`
        # alone; only record it when nothing else said anything.
        dtype = dtype or quant.get("activation_dtype")

    return ModelShape(
        context_len=context_len,
        num_layers=num_layers,
        num_kv_heads=num_kv_heads,
        head_dim=head_dim,
        torch_dtype=str(dtype) if isinstance(dtype, str) else None,
    )


async def fetch_shape(repo_id: str, hf_token: str | None = None) -> ModelShape:
    """Fetch and parse ``config.json`` for a HuggingFace repo.

    Returns an empty shape on any failure — gated repo without a token, network
    down, air-gapped portal, or a repo that simply has no config.json.
    """
    url = f"https://huggingface.co/{repo_id}/resolve/main/config.json"
    headers = {"Authorization": f"Bearer {hf_token}"} if hf_token else {}
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(url, headers=headers)
            if r.status_code != 200:
                log.info("config.json for %s: HTTP %s", repo_id, r.status_code)
                return ModelShape()
            return shape_from_config(r.json())
    except (httpx.HTTPError, ValueError) as exc:
        log.info("config.json for %s unavailable: %s", repo_id, exc)
        return ModelShape()
