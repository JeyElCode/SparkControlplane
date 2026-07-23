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
from ..models import INST_RUNNING, Instance, InstanceSchedule
from ..services import status_svc
from ..services.auth import COOKIE_NAME, parse_session

log = logging.getLogger("spark.gateway")

router = APIRouter(prefix="/v1", tags=["gateway"])

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
    names = []
    if inst.model is not None:
        names.append(inst.model.name)
    if inst.served_model_names:
        names.extend(n for n in inst.served_model_names.split() if n)
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
