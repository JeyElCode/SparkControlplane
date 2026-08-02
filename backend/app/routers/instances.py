from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..crypto import encrypt
from ..db import SessionLocal, get_session
from ..models import (
    INST_RUNNING,
    INST_STARTING,
    INST_STOPPING,
    TOPO_CLUSTER,
    TOPO_DISTRIBUTED,
    TOPO_SINGLE,
    Instance,
    ModelRegistry,
    Node,
)
from ..schemas import (
    InstanceIn,
    InstanceOut,
    InstanceUpdate,
    JobAccepted,
    PlanIn,
    PlanOut,
    PlanReason,
    TlsReloadIn,
)
from ..services import inst_state
from ..services import instances as inst_svc
from ..services import plan as plan_svc
from ..services.jobs import jobs

router = APIRouter(prefix="/api/instances", tags=["instances"])


@router.post("/plan", response_model=PlanOut)
async def plan_instance(payload: PlanIn, session: AsyncSession = Depends(get_session)):
    """Derive a complete serve configuration for a model, with its reasoning.

    Read-only: it creates nothing. The response is the same field set the create
    form holds, so the UI applies it and leaves everything editable — the plan
    removes the requirement to decide, not the ability to.
    """
    from ..config import get_settings
    from ..services.models_svc import capture_shape, load_model
    from ..services.telemetry import engine

    model = await inst_svc_model(session, payload.model_id)
    settings = get_settings()

    # Fetch the model's geometry the first time anyone plans with it, then
    # cache it on the row. Doing it here rather than at registration keeps
    # `add model` a local operation that works air-gapped.
    if model.num_layers is None:
        await capture_shape(session, model)

    nodes = list((await session.execute(select(Node))).scalars().all())
    instances = list((await session.execute(select(Instance))).scalars().all())
    committed = plan_svc.committed_gib_by_node(instances, nodes, settings.node_memory_gib)

    full = await load_model(session, model.id)
    present_ids = tuple(s.node_id for s in (full.node_states if full else []) if s.present)

    node_facts = tuple(
        plan_svc.NodeFacts(
            node_id=n.id,
            name=n.name,
            role=n.role,
            # None means "never sampled" — treat as reachable rather than
            # planning around a node the operator can see is fine.
            reachable=engine.node_reachable(n.id) is not False,
            has_qsfp=bool(n.qsfp_ip),
            committed_gib=committed.get(n.id, 0.0),
        )
        for n in nodes
    )

    result = plan_svc.plan_instance(
        plan_svc.ModelFacts(
            repo_id=model.repo_id,
            name=model.name,
            size_bytes=model.size_bytes,
            tool_parser=model.tool_parser,
            context_len=model.context_len,
            num_layers=model.num_layers,
            num_kv_heads=model.num_kv_heads,
            head_dim=model.head_dim,
            torch_dtype=model.torch_dtype,
            present_node_ids=present_ids,
        ),
        plan_svc.ClusterFacts(
            nodes=node_facts,
            node_memory_gib=float(settings.node_memory_gib),
        ),
        force_topology=payload.topology,
        force_node_id=payload.node_id,
        max_num_seqs=payload.max_num_seqs,
    )

    taken = {i.name for i in instances}
    return PlanOut(
        name=plan_svc.suggest_name(model.name, taken),
        settings=result.settings,
        reasons=[PlanReason(**r.__dict__) for r in result.reasons],
        warnings=result.warnings,
        feasible=result.feasible,
        summary=result.summary,
    )


async def inst_svc_model(session: AsyncSession, model_id: int) -> ModelRegistry:
    model = await session.get(ModelRegistry, model_id)
    if model is None:
        raise HTTPException(404, "Model not found")
    return model


@router.get("", response_model=list[InstanceOut])
async def list_instances(session: AsyncSession = Depends(get_session)):
    rows = (
        (
            await session.execute(
                select(Instance).order_by(Instance.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    out = []
    for r in rows:
        full = await inst_svc.load_instance(session, r.id)
        out.append(InstanceOut.of(full))
    return out


@router.post("", response_model=InstanceOut, status_code=201)
async def create_instance(payload: InstanceIn, session: AsyncSession = Depends(get_session)):
    model = await session.get(ModelRegistry, payload.model_id)
    if model is None:
        raise HTTPException(404, "Model not found")
    nnodes = 1
    if payload.topology == TOPO_SINGLE:
        if payload.node_id is None:
            raise HTTPException(400, "Single-topology instances require a target node_id.")
        if await session.get(Node, payload.node_id) is None:
            raise HTTPException(404, "Target node not found")
    elif payload.topology == TOPO_DISTRIBUTED:
        # Native multi-node needs ≥2 nodes registered, each with a QSFP IP set
        # (the head's is used as --master-addr for the rendezvous).
        nodes = (await session.execute(select(Node))).scalars().all()
        with_qsfp = [n for n in nodes if n.qsfp_ip]
        if len(with_qsfp) < 2:
            raise HTTPException(
                400,
                "Distributed topology requires at least 2 nodes registered with a qsfp_ip set "
                f"(found {len(with_qsfp)}).",
            )
        nnodes = len(with_qsfp)
    if payload.topology == TOPO_CLUSTER:
        default_tp = 2
    elif payload.topology == TOPO_DISTRIBUTED:
        default_tp = nnodes
    else:
        default_tp = 1
    # Ports: auto-assign unless explicitly chosen; explicit choices are
    # validated against everything else binding the same serving node.
    from ..services import ports as ports_svc

    if payload.port is None:
        port = await ports_svc.allocate_api_port(session)
    else:
        port = payload.port
        conflict = await ports_svc.port_conflict(
            session, port, payload.topology, payload.node_id,
            endpoint_id=payload.endpoint_id,
        )
        if conflict:
            raise HTTPException(409, conflict)
    if payload.master_port is None:
        master_port = (
            await ports_svc.allocate_master_port(session)
            if payload.topology == TOPO_DISTRIBUTED else 29500
        )
    else:
        master_port = payload.master_port

    if payload.tls_enabled:
        if not (payload.tls_cert and payload.tls_key):
            raise HTTPException(400, "tls_enabled requires both tls_cert and tls_key (PEM).")
        if payload.tls_port == port:
            raise HTTPException(400, "tls_port must differ from the vLLM port.")
    if payload.endpoint_id is not None:
        from ..models import Endpoint

        if await session.get(Endpoint, payload.endpoint_id) is None:
            raise HTTPException(404, f"Endpoint {payload.endpoint_id} not found")
    inst = Instance(
        name=payload.name,
        model_id=payload.model_id,
        topology=payload.topology,
        node_id=payload.node_id if payload.topology == TOPO_SINGLE else None,
        port=port,
        tensor_parallel_size=payload.tensor_parallel_size or default_tp,
        max_model_len=payload.max_model_len,
        gpu_memory_utilization=payload.gpu_memory_utilization,
        max_num_seqs=payload.max_num_seqs,
        max_num_batched_tokens=payload.max_num_batched_tokens,
        dtype=payload.dtype,
        kv_cache_dtype=payload.kv_cache_dtype,
        block_size=payload.block_size,
        tokenizer_mode=payload.tokenizer_mode,
        reasoning_parser=payload.reasoning_parser,
        trust_remote_code=payload.trust_remote_code,
        enable_tool_choice=payload.enable_tool_choice,
        tool_parser=payload.tool_parser,
        served_model_names=payload.served_model_names,
        compilation_config=payload.compilation_config,
        advanced_args=payload.advanced_args,
        env_vars=json.dumps(payload.env_vars) if payload.env_vars else None,
        master_port=master_port,
        extra_args=payload.extra_args,
        vllm_image=payload.vllm_image,
        api_key_enc=encrypt(payload.api_key),
        tls_enabled=payload.tls_enabled,
        tls_port=payload.tls_port,
        tls_cert_enc=encrypt(payload.tls_cert),
        tls_key_enc=encrypt(payload.tls_key),
        # Accepted by the schema since #77 but never written, so membership was
        # only reachable through a follow-up PATCH — and an instance created
        # without it launched with its own aliases and its own port instead of
        # the endpoint's.
        endpoint_id=payload.endpoint_id,
        autostart=payload.autostart,
    )
    session.add(inst)
    try:
        await session.commit()
    except Exception as exc:  # noqa: BLE001 - unique name violation etc.
        await session.rollback()
        raise HTTPException(409, f"Could not create instance: {exc}")
    full = await inst_svc.load_instance(session, inst.id)
    nnodes = len((await session.execute(select(Node.id))).scalars().all()) or 2
    inst_svc.resolve_defaults(full, nnodes=nnodes)
    await session.commit()
    return InstanceOut.of(full)


@router.get("/{instance_id}", response_model=InstanceOut)
async def get_instance(instance_id: int, session: AsyncSession = Depends(get_session)):
    inst = await inst_svc.load_instance(session, instance_id)
    if inst is None:
        raise HTTPException(404, "Instance not found")
    return InstanceOut.of(inst)


@router.patch("/{instance_id}", response_model=InstanceOut)
async def update_instance(
    instance_id: int, payload: InstanceUpdate, session: AsyncSession = Depends(get_session)
):
    inst = await inst_svc.load_instance(session, instance_id)
    if inst is None:
        raise HTTPException(404, "Instance not found")
    # Serve settings are baked into the systemd unit at start time, so editing a
    # live instance would silently do nothing until the next restart. Require it
    # to be stopped first, so the edit is unambiguous.
    if inst.status in (INST_RUNNING, INST_STARTING, INST_STOPPING):
        raise HTTPException(
            409,
            f"Instance '{inst.name}' is {inst.status}. Stop it before editing — "
            "serve settings only take effect on start.",
        )
    data = payload.model_dump(exclude_unset=True)
    if "port" in data and data["port"] is not None and data["port"] != inst.port:
        from ..services import ports as ports_svc

        conflict = await ports_svc.port_conflict(
            session, data["port"], inst.topology, inst.node_id, exclude_id=inst.id,
            endpoint_id=data.get("endpoint_id", inst.endpoint_id),
        )
        if conflict:
            raise HTTPException(409, conflict)
    # Write-only PEM fields map to their encrypted columns (like api_key).
    secret_map = {"tls_cert": "tls_cert_enc", "tls_key": "tls_key_enc"}
    for field, value in data.items():
        if field in ("port", "master_port") and value is None:
            continue  # ports are non-nullable; null in a PATCH means "keep"
        if field in secret_map:
            setattr(inst, secret_map[field], encrypt(value))
        elif field == "env_vars":
            # Stored as JSON text; an empty map means "clear it", which is
            # distinct from the field being absent from the PATCH.
            setattr(inst, field, json.dumps(value) if value else None)
        else:
            setattr(inst, field, value)
    if inst.tls_enabled:
        if not (inst.tls_cert_enc and inst.tls_key_enc):
            raise HTTPException(400, "tls_enabled requires both tls_cert and tls_key (PEM).")
        if inst.tls_port == inst.port:
            raise HTTPException(400, "tls_port must differ from the vLLM port.")
    await session.commit()
    return InstanceOut.of(inst)


@router.post("/{instance_id}/start", response_model=JobAccepted)
async def start_instance(instance_id: int, session: AsyncSession = Depends(get_session)):
    inst = await inst_svc.load_instance(session, instance_id)
    if inst is None:
        raise HTTPException(404, "Instance not found")
    if (flight := inst_state.in_flight(instance_id)) is not None:
        action, job_id = flight
        raise HTTPException(
            409,
            f"A {action} job is already running for '{inst.name}' (job {job_id}). "
            f"Wait for it to finish — starting twice would fight over the same unit.",
        )
    name = inst.name

    async def coro(h):
        async with SessionLocal() as s:
            return await inst_svc.start_instance(s, h, instance_id)

    job_id = await jobs.start("instance.start", f"Start {name}", coro, target=name)
    return JobAccepted(job_id=job_id, message="Start requested")


@router.post("/{instance_id}/reconcile", response_model=InstanceOut)
async def reconcile_instance(instance_id: int, session: AsyncSession = Depends(get_session)):
    """Re-probe this instance and correct its recorded status immediately,
    instead of waiting for the next observer tick. The escape hatch for when the
    portal and reality disagree."""
    inst = await inst_svc.load_instance(session, instance_id)
    if inst is None:
        raise HTTPException(404, "Instance not found")
    from ..services.reconcile import reconciler

    await reconciler.tick()
    await session.refresh(inst)
    return InstanceOut.of(inst)


@router.post("/{instance_id}/tls/reload", response_model=JobAccepted)
async def reload_tls_cert(
    instance_id: int, payload: TlsReloadIn, session: AsyncSession = Depends(get_session)
):
    """Rotate the TLS cert/key and reload nginx in place — no vLLM restart. This
    is the renewal path, so (unlike serve-setting edits) it is allowed while the
    instance is running."""
    inst = await inst_svc.load_instance(session, instance_id)
    if inst is None:
        raise HTTPException(404, "Instance not found")
    if not inst.tls_enabled:
        raise HTTPException(409, "TLS is not enabled on this instance.")
    name = inst.name
    cert, key = payload.tls_cert, payload.tls_key

    async def coro(h):
        async with SessionLocal() as s:
            return await inst_svc.rotate_tls_cert(s, h, instance_id, cert, key)

    job_id = await jobs.start("instance.tls_reload", f"Reload TLS cert {name}", coro, target=name)
    return JobAccepted(job_id=job_id, message="TLS reload requested")


@router.post("/{instance_id}/stop", response_model=JobAccepted)
async def stop_instance(instance_id: int, session: AsyncSession = Depends(get_session)):
    inst = await inst_svc.load_instance(session, instance_id)
    if inst is None:
        raise HTTPException(404, "Instance not found")
    name = inst.name

    async def coro(h):
        async with SessionLocal() as s:
            return await inst_svc.stop_instance(s, h, instance_id)

    job_id = await jobs.start("instance.stop", f"Stop {name}", coro, target=name)
    return JobAccepted(job_id=job_id, message="Stop requested")


@router.delete("/{instance_id}", response_model=JobAccepted)
async def delete_instance(instance_id: int, session: AsyncSession = Depends(get_session)):
    inst = await inst_svc.load_instance(session, instance_id)
    if inst is None:
        raise HTTPException(404, "Instance not found")
    name = inst.name

    async def coro(h):
        async with SessionLocal() as s:
            return await inst_svc.delete_instance(s, h, instance_id)

    job_id = await jobs.start("instance.delete", f"Delete {name}", coro, target=name)
    return JobAccepted(job_id=job_id, message="Delete requested")
