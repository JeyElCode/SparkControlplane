"""OpenAI-compatible API gateway: ONE endpoint for external clients.

Clients call ``/v1/chat/completions`` (etc.) on the portal; the ``model`` field
routes to whichever RUNNING instance serves that name (registry name or any
``served_model_names`` alias). Responses — including SSE token streams — pass
through unbuffered. Each instance's internal API key is injected on the way
through, so clients only ever hold the gateway credential.

Auth (per operator decision): when portal auth is ON, requests need
``Authorization: Bearer <gateway token>`` (Settings → Gateway, or
``SPARK_GATEWAY_TOKEN``); a logged-in portal session also works. With auth OFF
(homelab) the gateway is open, like everything else.
"""

from __future__ import annotations

import hmac
import logging
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..config import get_settings
from ..crypto import decrypt
from ..db import get_node_by_role, get_session
from ..models import (
    ApiKey,
    INST_ERROR,
    INST_RUNNING,
    INST_STARTING,
    INST_STOPPING,
    Instance,
    InstanceSchedule,
)
from ..services import apikeys, gwstats, status_svc
from ..services.auth import COOKIE_NAME, parse_session
from ..services.ratelimit import LimitError, limiter
from ..services.templates import parse_served_model_names
from ..schemas import (
    ApiKeyCreated,
    ApiKeyIn,
    ApiKeyOut,
    ApiKeyUpdate,
    GatewayInfo,
    GatewayRequestOut,
    GatewayRoute,
    GatewayTraffic,
    GatewayTrafficRow,
)

log = logging.getLogger("spark.gateway")

router = APIRouter(prefix="/v1", tags=["gateway"])
# Operator-facing view of the same routing table. Lives under /api so the normal
# portal session guards it (AuthMiddleware) — it is not part of the
# OpenAI-compatible surface and must never require the gateway bearer.
admin_router = APIRouter(prefix="/api/gateway", tags=["gateway"])

# httpx client factory — module-level so tests can swap the transport
def _make_client(verify: bool) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0), verify=verify)


async def _gateway_auth(request: Request, session: AsyncSession) -> apikeys.Principal:
    """Resolve the caller to a principal, or raise 401.

    Returns a principal even when auth is off, so attribution and limits have
    something to key on in every mode.
    """
    supplied = request.headers.get("authorization", "")
    supplied = supplied[7:] if supplied.startswith("Bearer ") else ""

    # A per-client key wins wherever it is presented — including with portal
    # auth off, so an operator can start attributing traffic before turning
    # auth on.
    if supplied and (principal := apikeys.lookup(supplied)) is not None:
        apikeys.touch(principal.key_id)  # dict write; persisted by the collector
        return principal

    settings = get_settings()
    if settings.effective_auth_mode == "none":
        return apikeys.Principal(client=apikeys.LEGACY_CLIENT)

    token = settings.gateway_token
    if not token:
        setting = await _get_setting(session)
        token = decrypt(setting.gateway_token_enc) if setting.gateway_token_enc else None
    # Compare BYTES: compare_digest raises TypeError on non-ASCII str, and the
    # header arrives latin-1-decoded, so the wire bytes must be recovered that
    # way or a token containing æøå could never match.
    if token and supplied and hmac.compare_digest(
        apikeys.wire_bytes(supplied), token.encode("utf-8")
    ):
        return apikeys.Principal(client=apikeys.LEGACY_CLIENT)
    if parse_session(request.cookies.get(COOKIE_NAME)):
        return apikeys.Principal(client=apikeys.SESSION_CLIENT)
    raise HTTPException(
        401,
        "Gateway requires a bearer token while portal auth is enabled. "
        "Issue a per-client key in Settings → API gateway (recommended, so one "
        "client can be revoked without disturbing the others), or use the "
        "shared token, and send 'Authorization: Bearer <token>'.",
    )


async def _get_setting(session: AsyncSession):
    from ..db import get_setting

    return await get_setting(session)


def _effective_limits(principal: apikeys.Principal) -> tuple[int | None, int | None]:
    """(max_concurrent, max_rpm) for this principal: per-key override wins, else
    the global default. The operator's own portal session is exempt unless
    explicitly opted in — locking yourself out of the Playground while chasing a
    runaway client is the wrong failure mode."""
    settings = get_settings()
    if principal.client == apikeys.SESSION_CLIENT and not settings.gateway_limit_session:
        return None, None
    concurrent = (
        principal.max_concurrent
        if principal.max_concurrent is not None
        else settings.gateway_max_concurrent
    )
    rpm = principal.max_rpm if principal.max_rpm is not None else settings.gateway_max_rpm
    return (concurrent or None), (rpm or None)


def _served_names(inst: Instance) -> list[str]:
    """The model ids this instance actually answers to.

    Must mirror what the systemd unit launches: ``--served-model-name`` is built
    from the aliases and *replaces* the registry name (services/templates.py
    build_vllm_serve_cmd), so advertising the registry name alongside aliases
    would route a name vLLM itself 404s.
    """
    names = parse_served_model_names(inst.served_model_names)
    if not names and inst.model is not None:
        names = [inst.model.name]
    return names


async def _running_instances(session: AsyncSession) -> list[Instance]:
    return list(
        (
            await session.execute(
                select(Instance)
                .where(Instance.status == INST_RUNNING)
                .options(selectinload(Instance.model), selectinload(Instance.node))
            )
        )
        .scalars()
        .all()
    )


async def _resolve(session: AsyncSession, model: str) -> tuple[Instance, str, bool]:
    """(instance, base_url, verify) for a served model name; raises 404/503."""
    running = await _running_instances(session)
    head = await get_node_by_role(session, "head")
    for inst in running:
        if model in _served_names(inst):
            base = status_svc.instance_base_url(inst, head)
            if base is None:
                raise HTTPException(503, f"Instance '{inst.name}' has no reachable host.")
            return inst, base[0], base[1]

    # not running — maybe it exists and is just outside its live window
    all_insts = list(
        (
            await session.execute(
                select(Instance).options(selectinload(Instance.model))
            )
        ).scalars()
    )
    for inst in all_insts:
        if model in _served_names(inst):
            # Distinguish "still loading" from "crashed" from "off right now":
            # a client that retries a 3-minute model load is doing the right
            # thing, one retrying a crashed instance is not.
            if inst.status == INST_STARTING:
                took = inst.last_load_seconds
                eta = f" It usually takes about {max(1, took // 60)} min to load." if took else ""
                raise HTTPException(
                    503,
                    f"Model '{model}' is starting — the weights are still loading.{eta}",
                    headers={"Retry-After": str(max(15, min(took or 30, 300)))},
                )
            if inst.status == INST_STOPPING:
                raise HTTPException(503, f"Model '{model}' is shutting down.")
            if inst.status == INST_ERROR:
                why = f" Last error: {inst.last_error}" if inst.last_error else ""
                raise HTTPException(
                    503, f"Model '{model}' failed to start and is not serving.{why}"
                )
            scheds = list(
                (
                    await session.execute(
                        select(InstanceSchedule).where(
                            InstanceSchedule.instance_id == inst.id
                        )
                    )
                ).scalars()
            )
            from ..services.scheduler import next_window_open, now_tz

            nxt = next_window_open(scheds, now_tz()) if scheds else None
            hint = (
                f" It is scheduled to be live again at {nxt.strftime('%a %H:%M')}."
                if nxt else ""
            )
            raise HTTPException(
                503, f"Model '{model}' exists but is not running right now.{hint}"
            )
    available = sorted({n for i in running for n in _served_names(i)})
    raise HTTPException(
        404,
        f"Unknown model '{model}'. Available now: {', '.join(available) or '(none running)'}.",
    )


@router.get("/models")
async def list_models(request: Request, session: AsyncSession = Depends(get_session)):
    await _gateway_auth(request, session)
    running = await _running_instances(session)
    data = []
    for inst in running:
        for name in _served_names(inst):
            data.append({
                "id": name,
                "object": "model",
                "owned_by": "spark-controlplane",
                "root": inst.model.name if inst.model else name,
            })
    return {"object": "list", "data": data}


async def _proxy(path: str, request: Request, session: AsyncSession) -> Response:
    started = time.monotonic()
    model = "?"
    # Until auth succeeds the caller is unidentified — do NOT attribute a
    # rejected request to the shared token.
    principal = apikeys.Principal(client=apikeys.ANONYMOUS_CLIENT)

    def _record(status: int, *, instance=None, ttfb=None, streamed=False, error=None) -> None:
        gwstats.stats.record(gwstats.RequestRecord(
            ts=time.time(),
            client=principal.client,
            model=model,
            instance=instance,
            status=status,
            duration_ms=int((time.monotonic() - started) * 1000),
            ttfb_ms=ttfb,
            streamed=streamed,
            error=error,
        ))

    try:
        principal = await _gateway_auth(request, session)
    except HTTPException as exc:
        # Rejections at the gate are exactly what was invisible before: a client
        # with a stale token got 401s and nobody knew until they complained.
        _record(exc.status_code, error=str(exc.detail)[:200])
        raise
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        _record(422, error="body is not JSON")
        raise HTTPException(422, "Request body must be JSON.")
    model = body.get("model") if isinstance(body.get("model"), str) else "?"
    if model == "?" or not model:
        _record(422, error="missing model")
        raise HTTPException(422, "'model' is required — see GET /v1/models for what's live.")
    try:
        inst, base, verify = await _resolve(session, model)
    except HTTPException as exc:
        _record(exc.status_code, error=str(exc.detail)[:200])
        raise

    limits = _effective_limits(principal)
    try:
        limiter.check_rate(principal.client, limits[1])
    except LimitError as exc:
        _record(429, instance=inst.name, error=exc.message)
        raise HTTPException(429, exc.message, headers={"Retry-After": str(exc.retry_after)})
    if not limiter.acquire(principal.client, inst.id, limits[0]):
        msg = (
            f"Too many concurrent requests: '{principal.client}' already has "
            f"{limits[0]} in flight against '{inst.name}'. Retry when one finishes."
        )
        _record(429, instance=inst.name, error=msg)
        raise HTTPException(429, msg, headers={"Retry-After": "5"})

    # From here on a slot is held and MUST be released on every path.
    released = False

    def _release() -> None:
        nonlocal released
        if not released:
            released = True
            limiter.release(principal.client, inst.id)

    headers = {"Content-Type": "application/json",
               **status_svc.instance_auth_headers(inst)}
    url = f"{base}/v1/{path}"

    client = _make_client(verify)
    try:
        req = client.build_request("POST", url, json=body, headers=headers)
        upstream = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        _release()
        _record(502, instance=inst.name, error=str(exc)[:200])
        raise HTTPException(502, f"Upstream instance '{inst.name}' unreachable: {exc}")

    if upstream.status_code != 200 or not body.get("stream"):
        raw = await upstream.aread()
        await upstream.aclose()
        await client.aclose()
        _release()
        _record(upstream.status_code, instance=inst.name,
                ttfb=int((time.monotonic() - started) * 1000))
        return Response(
            content=raw,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
        )

    async def relay():
        first_byte: float | None = None
        try:
            async for chunk in upstream.aiter_raw():
                if first_byte is None:
                    first_byte = time.monotonic()
                yield chunk
        finally:
            # The ONLY correct place to release: this runs on normal completion,
            # on client disconnect (the generator is closed -> GeneratorExit),
            # on an upstream error, and on task cancellation. A leaked slot here
            # would 429 that client forever, with nothing in the UI to explain
            # why — so nothing else may own this.
            await upstream.aclose()
            await client.aclose()
            _release()
            _record(
                upstream.status_code,
                instance=inst.name,
                ttfb=int((first_byte - started) * 1000) if first_byte else None,
                streamed=True,
            )

    return StreamingResponse(
        relay(),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "text/event-stream"),
    )


@router.post("/chat/completions")
async def chat_completions(request: Request, session: AsyncSession = Depends(get_session)):
    return await _proxy("chat/completions", request, session)


@router.post("/completions")
async def completions(request: Request, session: AsyncSession = Depends(get_session)):
    return await _proxy("completions", request, session)


@router.post("/embeddings")
async def embeddings(request: Request, session: AsyncSession = Depends(get_session)):
    return await _proxy("embeddings", request, session)


# --- operator-facing routing table ---------------------------------------
@admin_router.get("/routes", response_model=GatewayInfo)
async def gateway_routes(session: AsyncSession = Depends(get_session)):
    """Which model names the gateway accepts right now, and where each goes.

    Answers the daily operator question — "what can clients call?" — which the
    OpenAI-shaped /v1/models cannot, because it has nowhere to put the instance,
    node, health, or the portal-vs-vLLM disagreement that means a name is
    advertised but would 404 upstream.
    """
    settings = get_settings()
    setting = await _get_setting(session)
    head = await get_node_by_role(session, "head")
    from ..services.telemetry import engine

    rows = list(
        (
            await session.execute(
                select(Instance).options(
                    selectinload(Instance.model), selectinload(Instance.node)
                )
            )
        )
        .scalars()
        .all()
    )

    live: list[GatewayRoute] = []
    unavailable: list[GatewayRoute] = []
    for inst in rows:
        probe = engine.instance_runtime(inst.id)
        node = status_svc.instance_api_node(inst, head)
        upstream = set(probe.served_models) if probe and probe.served_models else None
        for name in _served_names(inst):
            route = GatewayRoute(
                model_name=name,
                instance_id=inst.id,
                instance=inst.name,
                status=inst.status,
                node=node.name if node else None,
                healthy=probe.health_ok if probe else None,
                confirmed_upstream=(name in upstream) if upstream is not None else None,
            )
            (live if inst.status == INST_RUNNING else unavailable).append(route)

    return GatewayInfo(
        auth_required=settings.effective_auth_mode != "none",
        token_configured=bool(settings.gateway_token or setting.gateway_token_enc),
        routes=sorted(live, key=lambda r: r.model_name),
        unavailable=sorted(unavailable, key=lambda r: r.model_name),
    )


# --- per-client API keys --------------------------------------------------
@admin_router.get("/keys", response_model=list[ApiKeyOut])
async def list_api_keys(session: AsyncSession = Depends(get_session)):
    rows = list(
        (await session.execute(select(ApiKey).order_by(ApiKey.created_at.desc())))
        .scalars()
        .all()
    )
    live = limiter.snapshot()
    return [ApiKeyOut.of(r, in_flight=live.get(r.label, 0)) for r in rows]


@admin_router.post("/keys", response_model=ApiKeyCreated, status_code=201)
async def create_api_key(payload: ApiKeyIn, session: AsyncSession = Depends(get_session)):
    """Issue a key. The token is in this response and nowhere else — it is
    stored only as a SHA-256 digest and cannot be recovered afterwards."""
    row, token = await apikeys.issue(
        session,
        payload.label,
        max_concurrent=payload.max_concurrent,
        max_rpm=payload.max_rpm,
    )
    return ApiKeyCreated(**ApiKeyOut.of(row).model_dump(), token=token)


@admin_router.patch("/keys/{key_id}", response_model=ApiKeyOut)
async def update_api_key(
    key_id: int, payload: ApiKeyUpdate, session: AsyncSession = Depends(get_session)
):
    row = await session.get(ApiKey, key_id)
    if row is None:
        raise HTTPException(404, "API key not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, field, value)
    await session.commit()
    # Disabling a key must stop working immediately, not at the next cache TTL.
    await apikeys.refresh_cache(session)
    return ApiKeyOut.of(row, in_flight=limiter.snapshot().get(row.label, 0))


@admin_router.delete("/keys/{key_id}", status_code=204)
async def delete_api_key(key_id: int, session: AsyncSession = Depends(get_session)):
    row = await session.get(ApiKey, key_id)
    if row is None:
        raise HTTPException(404, "API key not found")
    await session.delete(row)
    await session.commit()
    await apikeys.refresh_cache(session)


# --- traffic --------------------------------------------------------------
@admin_router.get("/traffic", response_model=GatewayTraffic)
async def gateway_traffic():
    """Who is calling what, live. Answers the questions the gateway could not:
    which client is using which model, what the error rate is, and what latency
    callers are actually seeing."""
    rows = []
    for (client, model), b in gwstats.stats.totals.items():
        rows.append(GatewayTrafficRow(
            client=client, model=model, requests=b.requests, errors=b.errors,
            rejected=b.rejected, prompt_tokens=b.prompt_tokens,
            completion_tokens=b.completion_tokens,
            avg_ms=round(b.duration_ms_total / b.requests, 1) if b.requests else None,
            avg_ttfb_ms=round(b.ttfb_ms_total / b.ttfb_count, 1) if b.ttfb_count else None,
        ))
    rows.sort(key=lambda r: -r.requests)
    recent = [
        GatewayRequestOut(
            ts=r.ts, client=r.client, model=r.model, instance=r.instance,
            status=r.status, duration_ms=r.duration_ms, ttfb_ms=r.ttfb_ms,
            streamed=r.streamed, error=r.error,
        )
        for r in reversed(gwstats.stats.recent)
    ]
    return GatewayTraffic(since_start=rows, recent=recent, in_flight=limiter.snapshot())
