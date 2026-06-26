"""Live status aggregation: node reachability + GPU telemetry, QSFP link, Ray
cluster health, vLLM instance health, and a rough per-node memory budget.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..config import get_settings
from ..crypto import decrypt
from ..db import get_setting
from ..models import INST_RUNNING, TOPO_CLUSTER, Instance, Node
from ..schemas import (
    GpuStatus,
    InstanceRuntimeStatus,
    NodeStatus,
    RayStatus,
    StatusSnapshot,
)
from ..ssh import ssh_for_node
from . import nodeops, templates


async def snapshot(session: AsyncSession) -> StatusSnapshot:
    settings = get_settings()
    setting = await get_setting(session)
    nodes = list((await session.execute(select(Node))).scalars().all())
    instances = list(
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
    head = next((n for n in nodes if n.role == "head"), None)
    worker = next((n for n in nodes if n.role == "worker"), None)

    node_statuses = await asyncio.gather(
        *[_node_status(session, n) for n in nodes], return_exceptions=False
    )
    qsfp_ok = await _qsfp_ok(session, head, worker)
    ray = await _ray_status(session, head)
    inst_statuses = await asyncio.gather(
        *[_instance_status(session, i, head) for i in instances]
    )

    warnings = _memory_warnings(nodes, instances, node_statuses, settings.node_memory_gib)

    return StatusSnapshot(
        setup_complete=setting.setup_complete,
        qsfp_ok=qsfp_ok,
        ray=ray,
        nodes=list(node_statuses),
        instances=list(inst_statuses),
        overcommit_warnings=warnings,
        generated_at=datetime.now(timezone.utc),
    )


async def _node_status(session: AsyncSession, node: Node) -> NodeStatus:
    st = NodeStatus(node_id=node.id, role=node.role, name=node.name, reachable=False)
    try:
        ssh = await ssh_for_node(session, node)
        await ssh.run("true", check=True, timeout=10)
        st.reachable = True
    except Exception as exc:  # noqa: BLE001
        st.detail = str(exc)
        return st

    try:
        st.gpus = await _gpus(ssh)
    except Exception:  # noqa: BLE001
        st.gpus = []

    try:
        dps = await nodeops.docker(ssh, "ps --format '{{.Names}}'")
        st.docker_ok = dps.ok
        names = dps.stdout.split()
        container = templates.RAY_HEAD_CONTAINER if node.role == "head" else templates.RAY_WORKER_CONTAINER
        st.ray_container_up = container in names
    except Exception:  # noqa: BLE001
        st.docker_ok = None
    return st


async def _gpus(ssh) -> list[GpuStatus]:
    q = (
        "nvidia-smi --query-gpu=index,name,memory.used,memory.total,"
        "utilization.gpu,temperature.gpu,power.draw "
        "--format=csv,noheader,nounits"
    )
    res = await ssh.run(q)
    gpus: list[GpuStatus] = []
    if not res.ok:
        return gpus
    for line in res.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7:
            continue
        gpus.append(
            GpuStatus(
                index=_int(parts[0]) or 0,
                name=parts[1] or None,
                mem_used_mib=_int(parts[2]),
                mem_total_mib=_int(parts[3]),
                util_pct=_int(parts[4]),
                temp_c=_int(parts[5]),
                power_w=_float(parts[6]),
            )
        )
    return gpus


async def _qsfp_ok(session: AsyncSession, head: Node | None, worker: Node | None) -> bool | None:
    if head is None or worker is None:
        return None
    try:
        ssh = await ssh_for_node(session, head)
        res = await ssh.run(f"ping -c 1 -W 2 {worker.qsfp_ip}", timeout=10)
        return res.ok
    except Exception:  # noqa: BLE001
        return None


async def _ray_status(session: AsyncSession, head: Node | None) -> RayStatus:
    if head is None:
        return RayStatus(reachable=False, detail="Head node not configured")
    try:
        ssh = await ssh_for_node(session, head)
        res = await nodeops.docker(ssh, f"exec {templates.RAY_HEAD_CONTAINER} ray status", timeout=20)
        if not res.ok:
            return RayStatus(reachable=False, detail="Ray head container not running")
        import re

        nodes_alive = len(set(re.findall(r"node_[0-9a-f]{8,}", res.stdout)))
        gpu_match = re.search(r"([\d.]+)/([\d.]+)\s+GPU", res.stdout)
        gpus_total = float(gpu_match.group(2)) if gpu_match else None
        return RayStatus(
            reachable=True,
            nodes_total=nodes_alive,
            nodes_alive=nodes_alive,
            gpus_total=gpus_total,
        )
    except Exception as exc:  # noqa: BLE001
        return RayStatus(reachable=False, detail=str(exc))


async def _instance_status(
    session: AsyncSession, inst: Instance, head: Node | None
) -> InstanceRuntimeStatus:
    node = inst.node if inst.topology != TOPO_CLUSTER else head
    host = node.lan_ip if node else None
    out = InstanceRuntimeStatus(
        instance_id=inst.id,
        name=inst.name,
        status=inst.status,
        endpoint=f"http://{host}:{inst.port}/v1" if host else None,
    )
    # systemd active state
    if node and inst.systemd_unit:
        try:
            ssh = await ssh_for_node(session, node)
            out.systemd_active = await nodeops.unit_active(ssh, inst.systemd_unit)
        except Exception:  # noqa: BLE001
            out.systemd_active = None
    # HTTP health
    if host:
        headers = {}
        api_key = decrypt(inst.api_key_enc)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            async with httpx.AsyncClient(timeout=4) as client:
                h = await client.get(f"http://{host}:{inst.port}/health", headers=headers)
                out.health_ok = h.status_code == 200
                if out.health_ok:
                    m = await client.get(f"http://{host}:{inst.port}/v1/models", headers=headers)
                    if m.status_code == 200:
                        data = m.json().get("data", [])
                        if data:
                            out.served_model = data[0].get("id")
        except Exception:  # noqa: BLE001
            out.health_ok = False
    return out


def _memory_warnings(
    nodes: list[Node],
    instances: list[Instance],
    node_statuses: list[NodeStatus],
    node_total_gib: int,
) -> list[str]:
    by_id = {n.id: n for n in nodes}
    head = next((n for n in nodes if n.role == "head"), None)
    worker = next((n for n in nodes if n.role == "worker"), None)
    used: dict[int, float] = {n.id: 0.0 for n in nodes}

    for inst in instances:
        if inst.status != INST_RUNNING:
            continue
        share = inst.gpu_memory_utilization * node_total_gib
        if inst.topology == TOPO_CLUSTER:
            for n in (head, worker):
                if n:
                    used[n.id] += share
        elif inst.node_id in used:
            used[inst.node_id] += share

    # record on the node statuses for the UI
    for st in node_statuses:
        st.mem_budget_total_gib = float(node_total_gib)
        st.mem_budget_used_gib = round(used.get(st.node_id, 0.0), 1)

    warnings: list[str] = []
    for nid, u in used.items():
        if u > node_total_gib * 1.0:
            node = by_id.get(nid)
            warnings.append(
                f"Node {node.name if node else nid} is overcommitted: running instances "
                f"request ~{u:.0f} GiB of ~{node_total_gib} GiB. Lower gpu-memory-utilization "
                f"or don't co-locate models."
            )
    return warnings


def _int(s: str) -> int | None:
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def _float(s: str) -> float | None:
    try:
        return float(s)
    except (ValueError, TypeError):
        return None
