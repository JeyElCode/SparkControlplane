from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..crypto import decrypt
from ..db import get_node_by_role, get_session
from ..models import TOPO_CLUSTER
from ..schemas import PlaygroundRequest, PlaygroundResponse
from ..services import instances as inst_svc

router = APIRouter(prefix="/api/playground", tags=["playground"])


@router.post("", response_model=PlaygroundResponse)
async def chat(payload: PlaygroundRequest, session: AsyncSession = Depends(get_session)):
    inst = await inst_svc.load_instance(session, payload.instance_id)
    if inst is None:
        raise HTTPException(404, "Instance not found")
    if inst.topology == TOPO_CLUSTER:
        node = await get_node_by_role(session, "head")
    else:
        node = inst.node
    if node is None:
        raise HTTPException(400, "Instance has no reachable host.")
    base = f"http://{node.lan_ip}:{inst.port}/v1"

    headers = {"Content-Type": "application/json"}
    api_key = decrypt(inst.api_key_enc)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # Resolve the served model id from the endpoint; fall back to the registry
    # name (the --served-model-name we serve under) if /v1/models is unreachable.
    model_id = inst.model.name if inst.model else None
    messages = []
    if payload.system:
        messages.append({"role": "system", "content": payload.system})
    messages.append({"role": "user", "content": payload.prompt})

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            try:
                m = await client.get(f"{base}/models", headers=headers)
                if m.status_code == 200:
                    data = m.json().get("data", [])
                    if data:
                        model_id = data[0].get("id", model_id)
            except httpx.HTTPError:
                pass
            r = await client.post(
                f"{base}/chat/completions",
                headers=headers,
                json={
                    "model": model_id,
                    "messages": messages,
                    "max_tokens": payload.max_tokens,
                    "temperature": payload.temperature,
                },
            )
            if r.status_code != 200:
                return PlaygroundResponse(ok=False, error=f"HTTP {r.status_code}: {r.text[:500]}")
            body = r.json()
            content = body.get("choices", [{}])[0].get("message", {}).get("content")
            return PlaygroundResponse(ok=True, content=content, raw=body)
    except httpx.HTTPError as exc:
        return PlaygroundResponse(ok=False, error=f"Request failed: {exc}")
