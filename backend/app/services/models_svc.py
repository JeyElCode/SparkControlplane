"""Model registry operations: validate, download (head), sync head->worker with
checksums, refresh presence, delete.

Download runs the vLLM image's ``hf`` CLI in a transient container on the head
node; sync uses ``rsync`` over the inter-node SSH set up during cluster setup.
"""

from __future__ import annotations

import shlex

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..crypto import decrypt
from ..db import get_cluster_config, get_node_by_role, get_setting
from ..models import (
    MS_ABSENT,
    MS_DOWNLOADING,
    MS_ERROR,
    MS_PRESENT,
    MS_SYNCING,
    MS_VERIFYING,
    ModelNodeState,
    ModelRegistry,
    Node,
)
from ..ssh import ssh_for_node
from . import nodeops
from .jobs import JobHandle
from .parsers import sanitize_name, tool_parser_for
from .paths import model_host_path, models_host_dir


async def validate_repo(repo_id: str) -> dict:
    """Best-effort lookup of an HF repo: existence + summed file size."""
    url = f"https://huggingface.co/api/models/{repo_id}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, params={"blobs": "true"})
            if r.status_code == 404:
                return {"ok": False, "error": "Repository not found on HuggingFace."}
            r.raise_for_status()
            data = r.json()
            size = sum(s.get("size", 0) or 0 for s in data.get("siblings", []))
            return {
                "ok": True,
                "repo_id": repo_id,
                "size_bytes": size or None,
                "gated": data.get("gated", False),
                "tool_parser": tool_parser_for(repo_id),
            }
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"Could not reach HuggingFace: {exc}"}


async def list_models_full(session: AsyncSession) -> list[ModelRegistry]:
    res = await session.execute(
        select(ModelRegistry)
        .options(selectinload(ModelRegistry.node_states).selectinload(ModelNodeState.node))
        .order_by(ModelRegistry.created_at.desc())
    )
    return list(res.scalars().all())


async def load_model(session: AsyncSession, model_id: int) -> ModelRegistry | None:
    res = await session.execute(
        select(ModelRegistry)
        .options(selectinload(ModelRegistry.node_states).selectinload(ModelNodeState.node))
        .where(ModelRegistry.id == model_id)
    )
    return res.scalar_one_or_none()


async def add_model(
    session: AsyncSession, repo_id: str, name: str | None, tool_parser: str | None
) -> ModelRegistry:
    existing = await session.execute(
        select(ModelRegistry).where(ModelRegistry.repo_id == repo_id)
    )
    if existing.scalar_one_or_none() is not None:
        raise ValueError(f"Model '{repo_id}' is already in the registry.")
    model = ModelRegistry(
        repo_id=repo_id,
        name=name or sanitize_name(repo_id),
        tool_parser=tool_parser or tool_parser_for(repo_id),
        status=MS_ABSENT,
    )
    session.add(model)
    await session.flush()
    nodes = (await session.execute(select(Node))).scalars().all()
    for node in nodes:
        session.add(ModelNodeState(model_id=model.id, node_id=node.id, status=MS_ABSENT))
    await session.commit()
    return await load_model(session, model.id)  # type: ignore[return-value]


async def _state_for(session: AsyncSession, model_id: int, node_id: int) -> ModelNodeState:
    res = await session.execute(
        select(ModelNodeState).where(
            ModelNodeState.model_id == model_id, ModelNodeState.node_id == node_id
        )
    )
    st = res.scalar_one_or_none()
    if st is None:
        st = ModelNodeState(model_id=model_id, node_id=node_id, status=MS_ABSENT)
        session.add(st)
        await session.flush()
    return st


async def download_model(
    session: AsyncSession, handle: JobHandle, model_id: int, auto_sync: bool = True
) -> str:
    model = await load_model(session, model_id)
    if model is None:
        raise RuntimeError("Model not found.")
    cfg = await get_cluster_config(session)
    setting = await get_setting(session)
    head = await get_node_by_role(session, "head")
    if head is None:
        raise RuntimeError("Head node is not configured.")
    token = decrypt(setting.hf_token_enc)

    dst = model_host_path(head, cfg, model.name)
    head_state = await _state_for(session, model_id, head.id)
    model.status = MS_DOWNLOADING
    head_state.status = MS_DOWNLOADING
    await session.commit()

    ssh = await ssh_for_node(session, head)
    await ssh.run(f"mkdir -p {shlex.quote(models_host_dir(head, cfg))}", check=True)

    env = f"-e HF_TOKEN={shlex.quote(token)} " if token else ""
    await handle.log(f"[{head.name}] downloading {model.repo_id} -> {dst}")
    run = (
        f"run --rm --network host {env}"
        f"-v {shlex.quote(models_host_dir(head, cfg))}:/models "
        f"{shlex.quote(cfg.vllm_image)} "
        f"hf download {shlex.quote(model.repo_id)} --local-dir /models/{shlex.quote(model.name)}"
    )
    res = await nodeops.docker(ssh, run, log_cb=handle.ssh_log_cb(), timeout=14400)
    if not res.ok:
        head_state.status = MS_ERROR
        model.status = MS_ERROR
        await session.commit()
        raise RuntimeError(f"Download failed: {res.stderr[-500:] or res.stdout[-500:]}")

    size = await _dir_size_bytes(ssh, dst)
    head_state.present = True
    head_state.size_bytes = size
    head_state.status = MS_PRESENT
    model.size_bytes = size
    model.status = MS_PRESENT
    await session.commit()
    await handle.log(f"[{head.name}] download complete ({_human(size)}).")

    if auto_sync:
        others = [n for n in await _all_nodes(session) if n.id != head.id]
        for node in others:
            await _sync_one(session, handle, model, head, node, cfg)
    return f"Model '{model.name}' downloaded" + (" and synced" if auto_sync else "")


async def sync_model(
    session: AsyncSession, handle: JobHandle, model_id: int, target_node_id: int | None = None
) -> str:
    model = await load_model(session, model_id)
    if model is None:
        raise RuntimeError("Model not found.")
    cfg = await get_cluster_config(session)
    head = await get_node_by_role(session, "head")
    if head is None:
        raise RuntimeError("Head node is not configured.")
    targets = [
        n for n in await _all_nodes(session)
        if n.id != head.id and (target_node_id is None or n.id == target_node_id)
    ]
    if not targets:
        raise RuntimeError("No target node to sync to.")
    for node in targets:
        await _sync_one(session, handle, model, head, node, cfg)
    return f"Model '{model.name}' synced to {', '.join(n.name for n in targets)}"


async def _sync_one(
    session: AsyncSession, handle: JobHandle, model: ModelRegistry, head: Node, node: Node, cfg
) -> None:
    src = model_host_path(head, cfg, model.name)
    dst = model_host_path(node, cfg, model.name)
    state = await _state_for(session, model.id, node.id)
    state.status = MS_SYNCING
    await session.commit()
    await handle.log(f"[{head.name} -> {node.name}] rsync {src}/ -> {node.name}:{dst}/")

    ssh = await ssh_for_node(session, head)
    rsync = (
        f"ssh -o BatchMode=yes {shlex.quote(node.name)} {shlex.quote(f'mkdir -p {dst}')} && "
        f"rsync -aH --info=progress2 -e 'ssh -o BatchMode=yes' "
        f"{shlex.quote(src + '/')} {shlex.quote(f'{node.name}:{dst}/')}"
    )
    res = await ssh.run(rsync, log_cb=handle.ssh_log_cb(), timeout=14400)
    if not res.ok:
        state.status = MS_ERROR
        await session.commit()
        raise RuntimeError(f"rsync to {node.name} failed: {res.stderr[-500:] or res.stdout[-500:]}")

    # Checksum verification (safetensors), best-effort.
    state.status = MS_VERIFYING
    await session.commit()
    checksum_ok = await _verify_checksums(handle, ssh, model.name, src, dst, node.name)

    size = await _dir_size_bytes(ssh, dst, remote_host=node.name)
    state.present = True
    state.size_bytes = size
    state.checksum_ok = checksum_ok
    state.status = MS_PRESENT
    await session.commit()
    await handle.log(
        f"[{node.name}] sync complete ({_human(size)}), checksum "
        f"{'verified' if checksum_ok else 'skipped/failed'}."
    )


async def _verify_checksums(handle, ssh, name, src, dst, worker_name) -> bool | None:
    sumfile = f"/tmp/{name}.sha256"
    qsum = shlex.quote(sumfile)
    gen = await ssh.run(
        f"cd {shlex.quote(src)} && "
        f"if ls *.safetensors >/dev/null 2>&1; then "
        f"find . -type f -name '*.safetensors' -print0 | sort -z | xargs -0 sha256sum > {qsum}; "
        f"echo HAVE; else echo NONE; fi"
    )
    if "NONE" in gen.stdout:
        await handle.log("  no .safetensors files to checksum (skipping verification)")
        return None
    await ssh.run(
        f"scp -o BatchMode=yes {qsum} {shlex.quote(f'{worker_name}:{sumfile}')}", check=False
    )
    verify = await ssh.run(
        f"ssh -o BatchMode=yes {shlex.quote(worker_name)} "
        f"{shlex.quote(f'cd {dst} && sha256sum -c {sumfile}')}",
        log_cb=handle.ssh_log_cb(),
    )
    return verify.ok


async def refresh_presence(session: AsyncSession, model_id: int) -> None:
    model = await load_model(session, model_id)
    if model is None:
        return
    cfg = await get_cluster_config(session)
    for node in await _all_nodes(session):
        path = model_host_path(node, cfg, model.name)
        try:
            ssh = await ssh_for_node(session, node)
            present = await ssh.exists(path)
            size = await _dir_size_bytes(ssh, path) if present else None
        except Exception:  # noqa: BLE001 - unreachable node -> leave state
            continue
        st = await _state_for(session, model_id, node.id)
        st.present = present
        st.size_bytes = size
        st.status = MS_PRESENT if present else MS_ABSENT
    await session.commit()


async def delete_model_files(
    session: AsyncSession,
    handle: JobHandle,
    model_id: int,
    node_ids: list[int] | None,
    drop_row: bool = False,
) -> str:
    model = await load_model(session, model_id)
    if model is None:
        raise RuntimeError("Model not found.")
    cfg = await get_cluster_config(session)
    nodes = [n for n in await _all_nodes(session) if node_ids is None or n.id in node_ids]
    for node in nodes:
        path = model_host_path(node, cfg, model.name)
        await handle.log(f"[{node.name}] rm -rf {path}")
        try:
            ssh = await ssh_for_node(session, node)
            await ssh.run(f"rm -rf {shlex.quote(path)}", check=True)
            st = await _state_for(session, model_id, node.id)
            st.present = False
            st.size_bytes = None
            st.checksum_ok = None
            st.status = MS_ABSENT
        except Exception as exc:  # noqa: BLE001
            await handle.log(f"[{node.name}] WARNING: delete failed: {exc}", "stderr")
    name = model.name
    if drop_row:
        await session.delete(model)
    else:
        model.status = MS_ABSENT
    await session.commit()
    return f"Model '{name}' files removed"


# --- helpers -------------------------------------------------------------
async def _all_nodes(session: AsyncSession) -> list[Node]:
    return list((await session.execute(select(Node))).scalars().all())


async def _dir_size_bytes(ssh, path: str, remote_host: str | None = None) -> int | None:
    cmd = f"du -sb {shlex.quote(path)} 2>/dev/null | cut -f1"
    if remote_host:
        cmd = f"ssh -o BatchMode=yes {shlex.quote(remote_host)} {shlex.quote(cmd)}"
    res = await ssh.run(cmd)
    try:
        return int(res.stdout.strip().split()[0])
    except (ValueError, IndexError):
        return None


def _human(n: int | None) -> str:
    if not n:
        return "unknown"
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.1f}{u}"
        f /= 1024
    return f"{n}B"
