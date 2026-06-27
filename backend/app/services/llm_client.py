"""Thin OpenAI-compatible chat client with timing instrumentation, used by the
eval engine to measure TTFT, decode tokens/sec, and latency."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import httpx


@dataclass
class ChatMetrics:
    ttft_ms: float | None = None
    total_ms: float = 0.0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    tokens_per_sec: float | None = None  # decode throughput (completion / (total-ttft))


@dataclass
class ChatResult:
    ok: bool
    content: str
    metrics: ChatMetrics
    error: str | None = None


async def chat_stream(
    base_url: str,
    model: str,
    messages: list[dict],
    *,
    max_tokens: int = 512,
    temperature: float = 0.2,
    api_key: str | None = None,
    timeout: float = 600.0,
) -> ChatResult:
    """Stream a chat completion, measuring TTFT, total latency, and tokens/sec.
    ``base_url`` is the ``/v1`` root."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    t0 = time.perf_counter()
    ttft: float | None = None
    parts: list[str] = []
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST", f"{base_url.rstrip('/')}/chat/completions", json=payload, headers=headers
            ) as r:
                if r.status_code != 200:
                    body = (await r.aread()).decode(errors="replace")
                    return ChatResult(False, "", ChatMetrics(), f"HTTP {r.status_code}: {body[:300]}")
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    choices = chunk.get("choices") or []
                    if choices:
                        piece = (choices[0].get("delta") or {}).get("content")
                        if piece:
                            if ttft is None:
                                ttft = (time.perf_counter() - t0) * 1000
                            parts.append(piece)
                    usage = chunk.get("usage")
                    if usage:
                        prompt_tokens = usage.get("prompt_tokens", prompt_tokens)
                        completion_tokens = usage.get("completion_tokens", completion_tokens)
    except httpx.HTTPError as exc:
        return ChatResult(False, "", ChatMetrics(), f"request failed: {exc}")

    total = (time.perf_counter() - t0) * 1000
    text = "".join(parts)
    if completion_tokens is None:
        completion_tokens = max(1, len(text.split()))  # rough fallback if usage absent
    decode_s = max((total - (ttft or 0.0)) / 1000.0, 1e-3)
    tps = completion_tokens / decode_s if completion_tokens else None
    return ChatResult(
        True,
        text,
        ChatMetrics(ttft, total, prompt_tokens, completion_tokens, tps),
        None,
    )
