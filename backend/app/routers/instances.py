from __future__ import annotations

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
from ..schemas import InstanceIn, InstanceOut, InstanceUpdate, JobAccepted
from ..services import instances as inst_svc
from ..services.jobs import jobs

router = APIRouter(prefix="/api/instances", tags=["instances"])


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
    inst = Instance(
        name=payload.name,
        model_id=payload.model_id,
        topology=payload.topology,
        node_id=payload.node_id if payload.topology == TOPO_SINGLE else None,
        port=payload.port,
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
        master_port=payload.master_port,
        extra_args=payload.extra_args,
        api_key_enc=encrypt(payload.api_key),
        autostart=payload.autostart,
    )
    session.add(inst)
    try:
        await session.commit()
    except Exception as exc:  # noqa: BLE001 - unique name violation etc.
        await session.rollback()
        raise HTTPException(409, f"Could not create instance: {exc}")
    full = await inst_svc.load_instance(session, inst.id)
    inst_svc.resolve_defaults(full)
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
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(inst, field, value)
    await session.commit()
    return InstanceOut.of(inst)


@router.post("/{instance_id}/start", response_model=JobAccepted)
async def start_instance(instance_id: int, session: AsyncSession = Depends(get_session)):
    inst = await inst_svc.load_instance(session, instance_id)
    if inst is None:
        raise HTTPException(404, "Instance not found")
    name = inst.name

    async def coro(h):
        async with SessionLocal() as s:
            return await inst_svc.start_instance(s, h, instance_id)

    job_id = await jobs.start("instance.start", f"Start {name}", coro, target=name)
    return JobAccepted(job_id=job_id, message="Start requested")


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
