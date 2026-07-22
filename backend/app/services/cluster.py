"""Cluster orchestration: run the setup pipeline, test node connections, harden
a node (install a generated key), and tear the cluster down.
"""

from __future__ import annotations

import shlex

import asyncssh
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..crypto import encrypt
from ..db import (
    SessionLocal,
    get_cluster_config,
    get_node_by_role,
    get_setting,
    get_worker_nodes,
)
from ..models import AUTH_KEY, INST_STOPPED, Instance, Node
from ..schemas import ConnectionTest, TeardownRequest
from ..ssh import pool, ssh_for_node
from . import nodeops, templates
from .jobs import JobHandle
from .paths import models_host_dir
from .phases import PHASES_ORDER, PhaseCtx, run_phase


async def run_setup(handle: JobHandle, phase_names: list[str] | None) -> str:
    names = phase_names or PHASES_ORDER
    async with SessionLocal() as session:
        head = await get_node_by_role(session, "head")
        workers = await get_worker_nodes(session)
        if head is None or not workers:
            raise RuntimeError(
                "Configure the head node and at least one worker node before running setup."
            )
        cfg = await get_cluster_config(session)
        setting = await get_setting(session)
        ctx = PhaseCtx(session=session, handle=handle, head=head, workers=workers, cfg=cfg, setting=setting)
        for name in names:
            await handle.log("")
            await handle.log(f"========== Phase: {name} ==========")
            await run_phase(ctx, name)
        await session.commit()
    return f"Setup phases complete: {', '.join(names)}"


async def test_node_connection(session: AsyncSession, node: Node) -> ConnectionTest:
    try:
        ssh = await ssh_for_node(session, node)
        host = (await ssh.run("hostname", timeout=15)).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        return ConnectionTest(ok=False, message=f"SSH failed: {exc}", detail=str(exc))

    sudo = await ssh.run("id -u", sudo=True)
    sudo_ok = sudo.ok and sudo.stdout.strip() == "0"
    docker = await nodeops.docker(ssh, "version --format '{{.Server.Version}}'")
    gpu = await ssh.run("nvidia-smi -L")
    return ConnectionTest(
        ok=True,
        message=f"Connected to {host}",
        hostname=host,
        sudo_ok=sudo_ok,
        docker_ok=docker.ok,
        gpu_ok=gpu.ok,
        detail=gpu.stdout.strip() if gpu.ok else (gpu.stderr.strip() or None),
    )


async def harden_node(handle: JobHandle, node_id: int) -> str:
    """Generate an ed25519 keypair, install the public key on the node, and
    switch the node to key auth (password is retained as a fallback)."""
    async with SessionLocal() as session:
        node = await session.get(Node, node_id)
        if node is None:
            raise RuntimeError("Node not found.")
        await handle.log(f"[{node.name}] generating ed25519 key and installing it")
        key = asyncssh.generate_private_key("ssh-ed25519", comment="spark-controlplane")
        private_pem = key.export_private_key().decode()
        public_line = key.export_public_key().decode().strip()

        ssh = await ssh_for_node(session, node)
        await ssh.run(
            f"""
mkdir -p ~/.ssh && chmod 700 ~/.ssh
touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
grep -qxF {shlex.quote(public_line)} ~/.ssh/authorized_keys || echo {shlex.quote(public_line)} >> ~/.ssh/authorized_keys
""",
            check=True,
        )
        node.ssh_private_key_enc = encrypt(private_pem)
        node.auth_method = AUTH_KEY
        node.hardened = True
        await session.commit()
        await pool.drop(node.id)

        # verify key-based login
        ssh2 = await ssh_for_node(session, node)
        res = await ssh2.run("hostname", timeout=15)
        if not res.ok:
            raise RuntimeError("Key installed but key-based login verification failed.")
        await handle.log(f"[{node.name}] key-based login verified ({res.stdout.strip()}). ✅")
        return f"Node '{node.name}' hardened to key auth"


async def teardown(handle: JobHandle, req: TeardownRequest) -> str:
    async with SessionLocal() as session:
        head = await get_node_by_role(session, "head")
        workers = await get_worker_nodes(session)
        cfg = await get_cluster_config(session)
        setting = await get_setting(session)
        nodes = [n for n in (head, *workers) if n is not None]

        async def warn(node, msg):
            await handle.log(f"[{node.name if node else '?'}] WARNING: {msg}", "stderr")

        if req.stop_instances:
            await handle.log("Stopping vLLM instances…")
            insts = list((await session.execute(select(Instance))).scalars().all())
            for inst in insts:
                node = head if inst.topology == "cluster" else (
                    await session.get(Node, inst.node_id) if inst.node_id else None
                )
                if node is None or not inst.systemd_unit:
                    continue
                try:
                    ssh = await ssh_for_node(session, node)
                    await nodeops.systemctl(ssh, "stop", inst.systemd_unit, log_cb=handle.ssh_log_cb())
                    inst.status = INST_STOPPED
                except Exception as exc:  # noqa: BLE001
                    await warn(node, f"failed stopping {inst.name}: {exc}")
            await session.commit()

        if req.stop_ray:
            await handle.log("Stopping + disabling Ray services and removing containers…")
            for node in nodes:
                role = node.role
                unit = templates.ray_unit_name(role)
                container = (
                    templates.RAY_HEAD_CONTAINER if role == "head" else templates.RAY_WORKER_CONTAINER
                )
                try:
                    ssh = await ssh_for_node(session, node)
                    await nodeops.systemctl(ssh, "stop", unit, log_cb=handle.ssh_log_cb())
                    await nodeops.systemctl(ssh, "disable", unit, log_cb=handle.ssh_log_cb())
                    await nodeops.docker(ssh, f"rm -f {container}")
                except Exception as exc:  # noqa: BLE001
                    await warn(node, f"failed stopping Ray: {exc}")
            setting.setup_complete = False
            await session.commit()

        if req.remove_network:
            await handle.log("Removing QSFP network configuration…")
            for node in nodes:
                try:
                    ssh = await ssh_for_node(session, node)
                    await ssh.run(
                        f"""
if command -v nmcli >/dev/null 2>&1; then
  nmcli con down qsfp-vllm 2>/dev/null || true
  nmcli con delete qsfp-vllm 2>/dev/null || true
fi
ip addr del {shlex.quote(node.qsfp_ip)}/{int(cfg.qsfp_netmask)} dev {shlex.quote(node.qsfp_iface)} 2>/dev/null || true
""",
                        sudo=True,
                        log_cb=handle.ssh_log_cb(),
                    )
                except Exception as exc:  # noqa: BLE001
                    await warn(node, f"failed removing network: {exc}")

        if req.remove_inter_node_ssh and head is not None and workers:
            await handle.log("Removing inter-node SSH trust…")
            try:
                hssh = await ssh_for_node(session, head)
                sed_blocks = "; ".join(
                    f"sed -i {shlex.quote(f'/Host {w.name}/,+4d')} ~/.ssh/config 2>/dev/null || true"
                    for w in workers
                )
                await hssh.run(
                    "rm -f ~/.ssh/id_ed25519_spark ~/.ssh/id_ed25519_spark.pub; " + sed_blocks
                )
                for worker in workers:
                    wssh = await ssh_for_node(session, worker)
                    await wssh.run(
                        "sed -i '/spark-vllm/d' ~/.ssh/authorized_keys 2>/dev/null || true"
                    )
            except Exception as exc:  # noqa: BLE001
                await warn(head, f"failed removing inter-node ssh: {exc}")

        if req.remove_hosts_entries:
            await handle.log("Removing /etc/hosts entries…")
            for node in nodes:
                try:
                    ssh = await ssh_for_node(session, node)
                    for n2 in nodes:
                        await ssh.run(
                            f"sed -i {shlex.quote(f'/[[:space:]]{n2.name}$/d;/[[:space:]]{n2.name}-qsfp$/d')} /etc/hosts",
                            sudo=True,
                        )
                except Exception as exc:  # noqa: BLE001
                    await warn(node, f"failed editing /etc/hosts: {exc}")

        if req.delete_models:
            await handle.log("Deleting downloaded models on all nodes…")
            from ..models import ModelNodeState, ModelRegistry, MS_ABSENT

            for node in nodes:
                try:
                    ssh = await ssh_for_node(session, node)
                    # sudo: model files written by the (root) download container
                    await ssh.run(
                        f"rm -rf {shlex.quote(models_host_dir(node, cfg))}/*", sudo=True, check=False
                    )
                except Exception as exc:  # noqa: BLE001
                    await warn(node, f"failed deleting models: {exc}")
            for st in (await session.execute(select(ModelNodeState))).scalars().all():
                st.present = False
                st.size_bytes = None
                st.checksum_ok = None
                st.status = MS_ABSENT
            for model in (await session.execute(select(ModelRegistry))).scalars().all():
                model.status = MS_ABSENT
            await session.commit()

        await handle.log("Teardown complete.")
    return "Teardown complete"
