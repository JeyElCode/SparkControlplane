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
    INST_ERROR,
    INST_RUNNING,
    INST_STARTING,
    INST_STOPPING,
    Instance,
    InstanceSchedule,
)
from ..services import status_svc
from ..services.auth import COOKIE_NAME, parse_session
from ..services.templates import parse_served_model_names
from ..schemas import GatewayInfo, GatewayRoute

log = logging.getLogger("spark.gateway")

router = APIRouter(prefix="/v1", tags=["gateway"])
# Operator-facing view of the same routing table. Lives under /api so the normal
# portal session guards it (AuthMiddleware) — it is not part of the
# OpenAI-compatible surface and must never require the gateway bearer.
admin_router = APIRouter(prefix="/api/gateway", tags=["gateway"])

# httpx client factory — module-level so tests can swap the transport
def _make_client(verify: bool) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0), verify=verify)


async def _gateway_auth(request: Request, session: AsyncSession) -> None:
    settings = get_settings()
    if settings.effective_auth_mode == "none":
        return
    supplied = request.headers.get("authorization", "")
    supplied = supplied[7:] if supplied.startswith("Bearer ") else ""
    token = settings.gateway_token
    if not token:
        setting = await _get_setting(session)
        token = decrypt(setting.gateway_token_enc) if setting.gateway_token_enc else None
    if token and supplied and hmac.compare_digest(supplied, token):
        return
    if parse_session(request.cookies.get(COOKIE_NAME)):
        return
    raise HTTPException(
        401,
        "Gateway requires a bearer token while portal auth is enabled. "
        "Set one in Settings → API gateway (or SPARK_GATEWAY_TOKEN) and send "
        "'Authorization: Bearer <token>'.",
    )


async def _get_setting(session: AsyncSession):
    from ..db import get_setting

    return await get_setting(session)


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
    await _gateway_auth(request, session)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(422, "Request body must be JSON.")
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise HTTPException(422, "'model' is required — see GET /v1/models for what's live.")
    inst, base, verify = await _resolve(session, model)
    headers = {"Content-Type": "application/json",
               **status_svc.instance_auth_headers(inst)}
    url = f"{base}/v1/{path}"

    client = _make_client(verify)
    try:
        req = client.build_request("POST", url, json=body, headers=headers)
        upstream = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(502, f"Upstream instance '{inst.name}' unreachable: {exc}")

    if upstream.status_code != 200 or not body.get("stream"):
        raw = await upstream.aread()
        await upstream.aclose()
        await client.aclose()
        return Response(
            content=raw,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
        )

    async def relay():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

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
