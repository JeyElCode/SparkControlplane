"""vLLM instance lifecycle.

* ``cluster`` topology: ``vllm serve`` runs inside the Ray head container via
  ``docker exec`` (TP across both nodes, ``--distributed-executor-backend ray``).
  Requires the model present on BOTH nodes.
* ``single`` topology: a standalone container pinned to one node
  (``--distributed-executor-backend mp``, TP=1). Requires the model on that node.

Each instance is a systemd unit so it survives reboots when autostart is on.
"""

from __future__ import annotations

import asyncio
import shlex

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..crypto import decrypt
from ..models import (
    INST_ERROR,
    INST_RUNNING,
    INST_STARTING,
    INST_STOPPED,
    MS_PRESENT,
    TOPO_CLUSTER,
    Instance,
    ModelNodeState,
    Node,
)
from ..ssh import ssh_for_node
from . import nodeops, templates
from .jobs import JobHandle
from .paths import hf_cache_host_path, model_container_path, models_host_dir
from .parsers import tool_parser_for


async def load_instance(session: AsyncSession, instance_id: int) -> Instance | None:
    res = await session.execute(
        select(Instance)
        .options(selectinload(Instance.model), selectinload(Instance.node))
        .where(Instance.id == instance_id)
    )
    return res.scalar_one_or_none()


async def _head_node(session: AsyncSession) -> Node:
    from ..db import get_node_by_role

    head = await get_node_by_role(session, "head")
    if head is None:
        raise RuntimeError("Head node is not configured.")
    return head  # type: ignore[return-value]


def resolve_defaults(inst: Instance) -> None:
    """Fill topology-derived + auto fields in place before persisting/starting."""
    if inst.topology == TOPO_CLUSTER:
        inst.tensor_parallel_size = inst.tensor_parallel_size or 2
    else:
        inst.tensor_parallel_size = 1
    if inst.enable_tool_choice and not inst.tool_parser and inst.model is not None:
        inst.tool_parser = tool_parser_for(inst.model.repo_id)


async def _ensure_model_present(session: AsyncSession, inst: Instance, node_ids: list[int]) -> None:
    res = await session.execute(
        select(ModelNodeState).where(
            ModelNodeState.model_id == inst.model_id,
            ModelNodeState.node_id.in_(node_ids),
        )
    )
    states = {s.node_id: s for s in res.scalars()}
    for nid in node_ids:
        st = states.get(nid)
        if st is None or not st.present or st.status != MS_PRESENT:
            raise RuntimeError(
                f"Model '{inst.model.name}' is not present on node id={nid}. "
                "Download and sync the model to all required nodes first."
            )


async def start_instance(session: AsyncSession, handle: JobHandle, instance_id: int) -> str:
    inst = await load_instance(session, instance_id)
    if inst is None:
        raise RuntimeError("Instance not found.")
    resolve_defaults(inst)
    cfg = await _cluster_config(session)
    model_path = model_container_path(cfg, inst.model.name)
    api_key = decrypt(inst.api_key_enc)

    serve_cmd = templates.build_vllm_serve_cmd(
        model_container_path=model_path,
        served_model_name=inst.model.name if inst.model else None,
        port=inst.port,
        tensor_parallel_size=inst.tensor_parallel_size,
        distributed_backend="ray" if inst.topology == TOPO_CLUSTER else "mp",
        max_model_len=inst.max_model_len,
        gpu_memory_utilization=inst.gpu_memory_utilization,
        max_num_seqs=inst.max_num_seqs,
        dtype=inst.dtype,
        enable_tool_choice=inst.enable_tool_choice,
        tool_parser=inst.tool_parser,
        api_key=api_key,
        extra_args=inst.extra_args,
    )

    inst.status = INST_STARTING
    inst.last_error = None
    await session.commit()

    unit_name = templates.instance_unit_name(inst.name)

    try:
        if inst.topology == TOPO_CLUSTER:
            head = await _head_node(session)
            worker = await _other_node(session, head)
            await _ensure_model_present(session, inst, [head.id, worker.id])
            await handle.log(f"Starting cluster instance '{inst.name}' (TP={inst.tensor_parallel_size}) on the Ray head")
            ssh = await ssh_for_node(session, head)
            unit = templates.render_instance_unit_cluster(
                name=inst.name, serve_cmd=serve_cmd, port=inst.port
            )
            await nodeops.install_systemd_unit(
                ssh, unit_name, unit, enable=inst.autostart, log_cb=handle.ssh_log_cb()
            )
        else:
            node = inst.node
            if node is None:
                raise RuntimeError("Single-topology instance requires a target node.")
            await _ensure_model_present(session, inst, [node.id])
            await handle.log(f"Starting single instance '{inst.name}' (TP=1) on {node.name}")
            ssh = await ssh_for_node(session, node)
            from ..config import get_settings

            install_dir = get_settings().node_install_dir
            run_script = templates.render_instance_docker_run_single(
                name=inst.name,
                image=cfg.vllm_image,
                hf_home=hf_cache_host_path(node, cfg),
                models_dir=models_host_dir(node, cfg),
                shm=cfg.shm_size,
                serve_cmd=serve_cmd,
            )
            script_path = f"{install_dir}/vllm-{inst.name}.sh"
            await nodeops.install_file(ssh, script_path, run_script, mode="755")
            unit = templates.render_instance_unit_single(name=inst.name, script_path=script_path)
            await nodeops.install_systemd_unit(
                ssh, unit_name, unit, enable=inst.autostart, log_cb=handle.ssh_log_cb()
            )

        inst.systemd_unit = unit_name
        inst.status = INST_RUNNING
        await session.commit()
        host = await _endpoint_host(session, inst)
        await handle.log(
            f"Instance '{inst.name}' launched. Endpoint: http://{host}:{inst.port}/v1\n"
            f"Streaming vLLM startup output below (model loading can take a few minutes)…"
        )
        healthy = await _stream_startup_logs(
            handle, ssh, unit_name, f"http://{host}:{inst.port}/health"
        )
        if healthy:
            await handle.log(f"✅ '{inst.name}' is serving — /health is green.")
        else:
            await handle.log(
                f"'{inst.name}' did not report healthy within the wait window. It may still be "
                f"loading (large models), or it failed to start — check the vLLM output above and "
                f"the Status page. The systemd unit will keep retrying.",
                "error",
            )
        return f"Instance '{inst.name}' started" + ("" if healthy else " (health not yet confirmed)")
    except Exception as exc:
        inst.status = INST_ERROR
        inst.last_error = str(exc)
        await session.commit()
        raise


async def _health_ok(url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=4) as client:
            return (await client.get(url)).status_code == 200
    except Exception:  # noqa: BLE001
        return False


async def _stream_startup_logs(
    handle: JobHandle, ssh, unit_name: str, health_url: str, timeout: int = 900
) -> bool:
    """Follow the instance's journal into the job log until /health is green or
    ``timeout`` elapses. Returns True if the endpoint became healthy.

    The remote ``timeout`` wrapper guarantees the follow can't run forever even
    if the early-cancel path is missed."""
    jtask = asyncio.create_task(
        ssh.run(
            f"timeout {timeout} journalctl -u {shlex.quote(unit_name)} -n 200 -f --no-pager 2>&1 || true",
            sudo=True,
            log_cb=handle.ssh_log_cb(),
        )
    )
    healthy = False
    waited = 0
    try:
        while not jtask.done() and waited < timeout:
            await asyncio.sleep(5)
            waited += 5
            if await _health_ok(health_url):
                healthy = True
                break
    finally:
        if not jtask.done():
            jtask.cancel()
            try:
                await jtask
            except BaseException:  # noqa: BLE001 - cancellation/cleanup is best-effort
                pass
    return healthy


async def stop_instance(session: AsyncSession, handle: JobHandle, instance_id: int) -> str:
    inst = await load_instance(session, instance_id)
    if inst is None:
        raise RuntimeError("Instance not found.")
    node = await _instance_node(session, inst)
    unit_name = inst.systemd_unit or templates.instance_unit_name(inst.name)
    await handle.log(f"Stopping instance '{inst.name}' ({unit_name})")
    ssh = await ssh_for_node(session, node)
    await nodeops.systemctl(ssh, "stop", unit_name, log_cb=handle.ssh_log_cb())
    inst.status = INST_STOPPED
    await session.commit()
    return f"Instance '{inst.name}' stopped"


async def delete_instance(session: AsyncSession, handle: JobHandle, instance_id: int) -> str:
    inst = await load_instance(session, instance_id)
    if inst is None:
        raise RuntimeError("Instance not found.")
    node = await _instance_node(session, inst)
    unit_name = inst.systemd_unit or templates.instance_unit_name(inst.name)
    name = inst.name
    await handle.log(f"Removing instance '{name}' and its systemd unit")
    try:
        ssh = await ssh_for_node(session, node)
        await nodeops.remove_systemd_unit(ssh, unit_name, log_cb=handle.ssh_log_cb())
    except Exception as exc:  # noqa: BLE001 - best-effort cleanup
        await handle.log(f"WARNING: unit cleanup failed: {exc}", "stderr")
    await session.delete(inst)
    await session.commit()
    return f"Instance '{name}' deleted"


# --- helpers -------------------------------------------------------------
async def _cluster_config(session: AsyncSession):
    from ..db import get_cluster_config

    return await get_cluster_config(session)


async def _other_node(session: AsyncSession, node: Node) -> Node:
    res = await session.execute(select(Node).where(Node.id != node.id))
    other = res.scalar_one_or_none()
    if other is None:
        raise RuntimeError("Worker node is not configured.")
    return other


async def _instance_node(session: AsyncSession, inst: Instance) -> Node:
    if inst.topology == TOPO_CLUSTER:
        return await _head_node(session)
    if inst.node is not None:
        return inst.node
    raise RuntimeError("Single-topology instance has no target node.")


async def _endpoint_host(session: AsyncSession, inst: Instance) -> str:
    node = await _instance_node(session, inst)
    return node.lan_ip
