from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..crypto import encrypt
from ..db import get_session
from ..models import Node
from ..schemas import ConnectionTest, JobAccepted, NodeIn, NodeOut, NodeUpdate
from ..services import cluster
from ..services.jobs import jobs
from ..ssh import pool

router = APIRouter(prefix="/api/nodes", tags=["nodes"])


@router.get("", response_model=list[NodeOut])
async def list_nodes(session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(Node).order_by(Node.role))).scalars().all()
    return [NodeOut.of(n) for n in rows]


@router.post("", response_model=NodeOut, status_code=201)
async def create_node(payload: NodeIn, session: AsyncSession = Depends(get_session)):
    exists = await session.execute(select(Node).where(Node.role == payload.role))
    if exists.scalar_one_or_none() is not None:
        raise HTTPException(409, f"A {payload.role} node already exists. Edit it instead.")
    node = Node(
        role=payload.role,
        name=payload.name,
        lan_ip=payload.lan_ip,
        qsfp_ip=payload.qsfp_ip,
        qsfp_iface=payload.qsfp_iface,
        ssh_user=payload.ssh_user,
        ssh_port=payload.ssh_port,
        auth_method=payload.auth_method,
        sudo_mode=payload.sudo_mode,
        ssh_password_enc=encrypt(payload.ssh_password),
        ssh_private_key_enc=encrypt(payload.ssh_private_key),
        ssh_key_passphrase_enc=encrypt(payload.ssh_key_passphrase),
        sudo_password_enc=encrypt(payload.sudo_password),
    )
    session.add(node)
    await session.commit()
    await session.refresh(node)
    return NodeOut.of(node)


@router.get("/{node_id}", response_model=NodeOut)
async def get_node(node_id: int, session: AsyncSession = Depends(get_session)):
    node = await session.get(Node, node_id)
    if node is None:
        raise HTTPException(404, "Node not found")
    return NodeOut.of(node)


@router.patch("/{node_id}", response_model=NodeOut)
async def update_node(
    node_id: int, payload: NodeUpdate, session: AsyncSession = Depends(get_session)
):
    node = await session.get(Node, node_id)
    if node is None:
        raise HTTPException(404, "Node not found")
    data = payload.model_dump(exclude_unset=True)
    secret_map = {
        "ssh_password": "ssh_password_enc",
        "ssh_private_key": "ssh_private_key_enc",
        "ssh_key_passphrase": "ssh_key_passphrase_enc",
        "sudo_password": "sudo_password_enc",
    }
    for field, value in data.items():
        if field in secret_map:
            setattr(node, secret_map[field], encrypt(value))
        else:
            setattr(node, field, value)
    await session.commit()
    await pool.drop(node.id)  # connection params may have changed
    await session.refresh(node)
    return NodeOut.of(node)


@router.delete("/{node_id}", status_code=204)
async def delete_node(node_id: int, session: AsyncSession = Depends(get_session)):
    node = await session.get(Node, node_id)
    if node is None:
        raise HTTPException(404, "Node not found")
    await session.delete(node)
    await session.commit()
    await pool.drop(node_id)


@router.post("/{node_id}/test", response_model=ConnectionTest)
async def test_node(node_id: int, session: AsyncSession = Depends(get_session)):
    node = await session.get(Node, node_id)
    if node is None:
        raise HTTPException(404, "Node not found")
    return await cluster.test_node_connection(session, node)


@router.post("/{node_id}/harden", response_model=JobAccepted)
async def harden_node(node_id: int, session: AsyncSession = Depends(get_session)):
    node = await session.get(Node, node_id)
    if node is None:
        raise HTTPException(404, "Node not found")
    job_id = await jobs.start(
        "node.harden", f"Harden {node.name}", lambda h: cluster.harden_node(h, node_id),
        node_id=node_id,
    )
    return JobAccepted(job_id=job_id, message="Hardening started")
