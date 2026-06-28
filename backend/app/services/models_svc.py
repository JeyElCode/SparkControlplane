"""Model registry operations: validate, download (head), sync head->worker with
checksums, refresh presence, delete.

Download runs the vLLM image's ``hf`` CLI in a transient container on the head
node; sync uses ``rsync`` over the inter-node SSH set up during cluster setup.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import time

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

log = logging.getLogger("spark.models")

# Inter-node SSH key created in the setup `ssh` phase (must match phases.py).
# Used to reach the worker over the QSFP IP directly (the ~/.ssh/config alias
# only covers the LAN hostname).
INTER_NODE_KEY = "~/.ssh/id_ed25519_spark"

# If a download's on-disk size doesn't grow for this long while still clearly
# mid-transfer, treat it as stuck (almost always a stale HuggingFace .lock from
# an interrupted/duplicate download) and abort so it can be retried. Generous on
# purpose: a healthy download advances every few seconds, so zero growth for this
# many seconds is a genuine deadlock, not a slow link.
DOWNLOAD_STALL_SECONDS = 900

# Live, in-memory per-(model, node) transfer progress (0..1). Surfaced on the
# Models page so a download/sync bar is visible without opening the job dialog.
# Single-process (1 replica), so a module dict is sufficient; lost on restart.
_node_progress: dict[tuple[int, int], float] = {}


def set_node_progress(model_id: int, node_id: int, frac: float) -> None:
    _node_progress[(model_id, node_id)] = frac


def get_node_progress(model_id: int, node_id: int) -> float | None:
    return _node_progress.get((model_id, node_id))


def clear_node_progress(model_id: int, node_id: int) -> None:
    _node_progress.pop((model_id, node_id), None)


def _download_container(name: str) -> str:
    """Deterministic name for a model's download container, so an orphan left
    running (e.g. after a control-plane restart) can be found and killed before
    starting a new download into the same directory."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", name)
    return f"spark-dl-{safe}"


async def _reap_download(
    ssh, head: Node, cfg, model: ModelRegistry, handle: JobHandle | None = None
) -> int:
    """Kill any download container for this model and remove stale HuggingFace
    download locks. Idempotent and best-effort; returns the number of legacy
    containers removed.

    Two things make a model un-downloadable after an interrupted/duplicated
    download:

    * an **orphaned** ``hf download`` container — it survives a control-plane
      restart (the in-memory job guard is lost, but the detached ``docker run``
      keeps going) and holds the per-file ``.lock`` files forever, and
    * the **stale ``.lock`` files** themselves, left behind when a download is
      hard-killed.

    A second ``hf download`` into the same ``--local-dir`` then deadlocks
    ("Still waiting to acquire lock ..."). So we reap before (re)starting a
    download and when the user explicitly stops one.
    """
    cname = _download_container(model.name)
    # Match ONLY our own download command (repo id + exact local dir) so we never
    # touch a serving container or a different model's transfer.
    marker = f"download {model.repo_id} --local-dir /models/{model.name}"
    lock_dir = model_host_path(head, cfg, model.name) + "/.cache/huggingface/download"
    script = (
        "set +e\n"
        f"docker rm -f {shlex.quote(cname)} >/dev/null 2>&1\n"
        "killed=0\n"
        "for c in $(docker ps -q 2>/dev/null); do\n"
        f"  if docker inspect \"$c\" 2>/dev/null | grep -F -q -- {shlex.quote(marker)}; then\n"
        "    docker rm -f \"$c\" >/dev/null 2>&1 && killed=$((killed+1))\n"
        "  fi\n"
        "done\n"
        f"rm -f {shlex.quote(lock_dir)}/*.lock 2>/dev/null\n"
        'echo "reaped=$killed"\n'
    )
    try:
        # sudo: the download runs as root in the container, so the .lock files are
        # root-owned; root can also always drive docker.
        res = await ssh.run(script, sudo=True, timeout=120)
    except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
        if handle:
            await handle.log(f"  (stale-download cleanup failed: {exc})", "stderr")
        return 0
    n = 0
    for line in res.stdout.splitlines():
        if line.startswith("reaped="):
            try:
                n = int(line.split("=", 1)[1])
            except ValueError:
                n = 0
    if handle:
        await handle.log(
            f"  reaped {n} orphaned download container(s) and cleared stale HF locks"
            if n
            else "  cleared any stale HF download locks"
        )
    return n


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

    # Reap any orphaned download container + stale HF locks for this model first,
    # so a download interrupted by a control-plane restart (or a duplicate start)
    # can't deadlock the new one on the per-file .lock files. Self-healing: a
    # plain "Download" recovers a previously stuck download.
    await _reap_download(ssh, head, cfg, model, handle)

    # Total download size (for the progress bar denominator), best-effort.
    total = model.size_bytes
    if not total:
        info = await validate_repo(model.repo_id)
        if info.get("ok"):
            total = info.get("size_bytes")

    env = f"-e HF_TOKEN={shlex.quote(token)} " if token else ""
    await handle.log(
        f"[{head.name}] downloading {model.repo_id} -> {dst}"
        + (f" (~{_human(total)})" if total else "")
    )
    qrepo = shlex.quote(model.repo_id)
    qname = shlex.quote(model.name)
    # Prefer the new unified `hf` CLI; fall back to `huggingface-cli` on older images.
    dl = (
        f"if command -v hf >/dev/null 2>&1; then "
        f"hf download {qrepo} --local-dir /models/{qname}; "
        f"else huggingface-cli download {qrepo} --local-dir /models/{qname}; fi"
    )
    # --entrypoint bash skips the NGC image's startup banner (the harmless
    # "NVIDIA Driver was not detected" / "SHMEM 64MB" warnings) — a download
    # needs neither GPU nor large shared memory. --name makes the container
    # findable so an orphan can be reaped (see _reap_download).
    run = (
        f"run --rm --name {shlex.quote(_download_container(model.name))} "
        f"--network host {env}"
        f"-v {shlex.quote(models_host_dir(head, cfg))}:/models "
        f"--entrypoint bash {shlex.quote(cfg.vllm_image)} "
        f"-lc {shlex.quote(dl)}"
    )

    # Poll the target dir size while downloading so the UI shows real progress
    # (both the job dialog and the per-node bar on the Models page). The poller
    # also watches for a stall: if the size stops growing for too long while
    # clearly mid-download, the download is stuck (stale HF lock) — kill the
    # container so `hf` exits and the job fails fast with a clear message.
    stop = asyncio.Event()
    watch = {"size": -1, "changed_at": time.monotonic(), "stalled": False}

    async def _progress_poller() -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=8)
            except asyncio.TimeoutError:
                pass
            if stop.is_set():
                break
            try:
                cur = await _dir_size_bytes(ssh, dst)
            except Exception:  # noqa: BLE001 - progress is best-effort
                continue
            if cur is not None and cur != watch["size"]:
                watch["size"] = cur
                watch["changed_at"] = time.monotonic()
            frac = min(0.99, cur / total) if (cur and total) else 0.0
            # Stuck: no byte growth for the stall window while not near-complete.
            if (
                cur
                and frac < 0.99
                and (time.monotonic() - watch["changed_at"]) >= DOWNLOAD_STALL_SECONDS
            ):
                watch["stalled"] = True
                await handle.log(
                    f"  no progress for {DOWNLOAD_STALL_SECONDS // 60} min — download is "
                    "stuck (stale HuggingFace lock); aborting so it can be retried.",
                    "error",
                )
                await _reap_download(ssh, head, cfg, model, handle)
                stop.set()
                break
            if cur and total:
                set_node_progress(model_id, head.id, frac)
                await handle.set_progress(frac)
                await handle.log(f"  …{_human(cur)} / {_human(total)} ({int(frac * 100)}%)")
            elif cur:
                await handle.log(f"  …{_human(cur)} downloaded")

    poller = asyncio.create_task(_progress_poller())
    try:
        res = await nodeops.docker(ssh, run, log_cb=handle.ssh_log_cb(), timeout=14400)
    finally:
        stop.set()
        await poller
        clear_node_progress(model_id, head.id)

    if watch["stalled"]:
        head_state.status = MS_ERROR
        model.status = MS_ERROR
        await session.commit()
        raise RuntimeError(
            f"Download stalled (no progress for {DOWNLOAD_STALL_SECONDS // 60} min) and was "
            "aborted — usually a stale HuggingFace lock from an interrupted or duplicated "
            "download. The partial files were kept; click Download again to resume."
        )
    if not res.ok:
        head_state.status = MS_ERROR
        model.status = MS_ERROR
        await session.commit()
        raise RuntimeError(f"Download failed: {res.stderr[-500:] or res.stdout[-500:]}")

    await handle.set_progress(1.0)
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
        try:
            for node in others:
                await _sync_one(session, handle, model, head, node, cfg)
        except Exception:  # noqa: BLE001 - CancelledError is BaseException, not caught here
            # The head copy is present, but a worker sync failed — don't leave the
            # registry claiming the model is fully present.
            model.status = MS_ERROR
            await session.commit()
            raise
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


async def cancel_download(session: AsyncSession, handle: JobHandle, model_id: int) -> str:
    """Stop an in-progress (or stuck) download/sync: kill the node-side download
    container, clear stale HF locks, and reset the model's in-flight state.

    Works even when the control-plane has no in-memory job for it (e.g. after a
    restart) — the recovery path for an orphaned download that no longer has a
    job to cancel. Partial files are kept so a subsequent Download resumes."""
    model = await load_model(session, model_id)
    if model is None:
        raise RuntimeError("Model not found.")
    cfg = await get_cluster_config(session)
    head = await get_node_by_role(session, "head")
    if head is None:
        raise RuntimeError("Head node is not configured.")

    await handle.log(f"Stopping transfers for '{model.name}' and cleaning up…")
    n = 0
    try:
        ssh = await ssh_for_node(session, head)
        n = await _reap_download(ssh, head, cfg, model, handle)
    except Exception as exc:  # noqa: BLE001 - still reset state even if the node is unreachable
        await handle.log(f"  (could not reach head to kill the download: {exc})", "stderr")

    for st in model.node_states:
        if st.status in (MS_DOWNLOADING, MS_SYNCING, MS_VERIFYING):
            st.status = MS_ABSENT
            st.present = False
            st.size_bytes = None
            st.checksum_ok = None
        clear_node_progress(model_id, st.node_id)
    if model.status in (MS_DOWNLOADING, MS_SYNCING, MS_VERIFYING, MS_ERROR):
        model.status = MS_ABSENT
    await session.commit()

    await handle.log(
        (f"Killed {n} download container(s). " if n else "")
        + "Stopped. Partial files were kept — click Download to resume."
    )
    return f"Stopped transfers for '{model.name}'"


async def _sync_one(
    session: AsyncSession, handle: JobHandle, model: ModelRegistry, head: Node, node: Node, cfg
) -> None:
    src = model_host_path(head, cfg, model.name)
    dst = model_host_path(node, cfg, model.name)
    state = await _state_for(session, model.id, node.id)
    state.status = MS_SYNCING
    await session.commit()

    # Sync over the QSFP high-speed link: connect to the worker's QSFP IP using
    # the inter-node key directly (the ~/.ssh/config alias only covers the LAN
    # hostname, so we pass -i and target the IP explicitly).
    target = f"{node.ssh_user}@{node.qsfp_ip}"
    sshopt = f"ssh -i {INTER_NODE_KEY} -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
    remote_prefix = f"{sshopt} {shlex.quote(target)}"
    await handle.log(
        f"[{head.name} -> {node.name}] rsync over QSFP ({node.qsfp_ip}): {src}/ -> {dst}/"
    )

    ssh = await ssh_for_node(session, head)
    src_size = await _dir_size_bytes(ssh, src)
    rsync = (
        f"{remote_prefix} {shlex.quote(f'mkdir -p {dst}')} && "
        f"rsync -aH --info=progress2 -e {shlex.quote(sshopt)} "
        f"{shlex.quote(src + '/')} {shlex.quote(f'{target}:{dst}/')}"
    )

    # Poll the destination size on the target node so sync shows progress too.
    stop = asyncio.Event()

    async def _sync_poller() -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=8)
            except asyncio.TimeoutError:
                pass
            if stop.is_set():
                break
            try:
                cur = await _dir_size_bytes(ssh, dst, remote_prefix=remote_prefix)
            except Exception:  # noqa: BLE001 - progress is best-effort
                continue
            if cur and src_size:
                frac = min(0.99, cur / src_size)
                set_node_progress(model.id, node.id, frac)
                await handle.set_progress(frac)
                await handle.log(f"  …{_human(cur)} / {_human(src_size)} ({int(frac * 100)}%)")

    poller = asyncio.create_task(_sync_poller())
    try:
        res = await ssh.run(rsync, log_cb=handle.ssh_log_cb(), timeout=14400)
    finally:
        stop.set()
        await poller
        clear_node_progress(model.id, node.id)

    if not res.ok:
        state.status = MS_ERROR
        await session.commit()
        raise RuntimeError(f"rsync to {node.name} failed: {res.stderr[-500:] or res.stdout[-500:]}")

    # Checksum verification (safetensors), best-effort.
    state.status = MS_VERIFYING
    await session.commit()
    try:
        checksum_ok = await _verify_checksums(handle, ssh, model.name, src, dst, target, sshopt)
    except Exception as exc:  # noqa: BLE001 - a verify error must not strand the node in 'verifying'
        await handle.log(f"[{node.name}] checksum verification error: {exc}", "error")
        state.status = MS_ERROR
        await session.commit()
        raise RuntimeError(f"checksum verification on {node.name} failed: {exc}")

    size = await _dir_size_bytes(ssh, dst, remote_prefix=remote_prefix)
    state.present = True
    state.size_bytes = size
    state.checksum_ok = checksum_ok
    state.status = MS_PRESENT
    await session.commit()
    await handle.log(
        f"[{node.name}] sync complete ({_human(size)}), checksum "
        f"{'verified' if checksum_ok else 'skipped/failed'}."
    )


async def _verify_checksums(handle, ssh, name, src, dst, target, sshopt) -> bool | None:
    """Verify safetensors checksums on the worker, over the same QSFP path.
    ``target`` is ``user@qsfp_ip`` and ``sshopt`` the matching ssh command."""
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
        f"scp -i {INTER_NODE_KEY} -o BatchMode=yes -o StrictHostKeyChecking=accept-new "
        f"{qsum} {shlex.quote(f'{target}:{sumfile}')}",
        check=False,
    )
    verify = await ssh.run(
        f"{sshopt} {shlex.quote(target)} "
        f"{shlex.quote(f'cd {dst} && sha256sum -c {sumfile}')}",
        log_cb=handle.ssh_log_cb(),
    )
    return verify.ok


async def refresh_presence(session: AsyncSession, model_id: int) -> None:
    model = await load_model(session, model_id)
    if model is None:
        return
    cfg = await get_cluster_config(session)
    # Do all the slow SSH probing FIRST, then write once — never hold a write
    # transaction open across SSH (that pins the SQLite write lock and starves
    # other writers → "database is locked").
    findings: list[tuple[int, bool, int | None]] = []
    for node in await _all_nodes(session):
        path = model_host_path(node, cfg, model.name)
        try:
            ssh = await ssh_for_node(session, node)
            present = await ssh.exists(path)
            size = await _dir_size_bytes(ssh, path) if present else None
        except Exception:  # noqa: BLE001 - unreachable node -> leave state
            continue
        findings.append((node.id, present, size))
    for node_id, present, size in findings:
        st = await _state_for(session, model_id, node_id)
        st.present = present
        st.size_bytes = size
        st.status = MS_PRESENT if present else MS_ABSENT
    await session.commit()


def _name_or_path_from_config(config_json: str) -> str | None:
    """Recover the original HF repo id from a model dir's config.json
    (`_name_or_path`), when it looks like an org/repo id."""
    try:
        data = json.loads(config_json)
    except (ValueError, TypeError):
        return None
    nop = data.get("_name_or_path")
    if isinstance(nop, str) and "/" in nop and not nop.startswith("/"):
        return nop.strip()
    return None


async def discover_models(session: AsyncSession) -> int:
    """Scan every node's models dir and import any directory that isn't in the
    registry, then refresh presence for all models so the registry mirrors disk.
    Returns the number of newly imported models."""
    cfg = await get_cluster_config(session)
    nodes = await _all_nodes(session)
    if not nodes:
        return 0

    existing_names = {
        m.name for m in (await session.execute(select(ModelRegistry))).scalars().all()
    }
    # name -> {"repo_id": str|None, "node_ids": set[int]}
    found: dict[str, dict] = {}
    for node in nodes:
        base = models_host_dir(node, cfg)
        try:
            ssh = await ssh_for_node(session, node)
            res = await ssh.run(
                f"find {shlex.quote(base)} -mindepth 1 -maxdepth 1 -type d -printf '%f\\n' 2>/dev/null"
            )
        except Exception:  # noqa: BLE001 - unreachable node -> skip
            continue
        if not res.ok:
            continue
        for line in res.stdout.splitlines():
            name = line.strip()
            if not name or name.startswith("."):
                continue
            entry = found.setdefault(name, {"repo_id": None, "node_ids": set()})
            entry["node_ids"].add(node.id)
            if entry["repo_id"] is None:
                cj = await ssh.run(
                    f"cat {shlex.quote(base + '/' + name + '/config.json')} 2>/dev/null"
                )
                if cj.ok and cj.stdout.strip():
                    entry["repo_id"] = _name_or_path_from_config(cj.stdout)

    imported = 0
    for name, info in found.items():
        if name in existing_names:
            continue
        repo_id = info["repo_id"] or name
        # ensure repo_id uniqueness; fall back to the dir name, then skip
        clash = (
            await session.execute(select(ModelRegistry).where(ModelRegistry.repo_id == repo_id))
        ).scalar_one_or_none()
        if clash is not None:
            repo_id = name
            if (
                await session.execute(
                    select(ModelRegistry).where(ModelRegistry.repo_id == repo_id)
                )
            ).scalar_one_or_none() is not None:
                continue
        try:
            model = ModelRegistry(
                repo_id=repo_id,
                name=name,
                tool_parser=tool_parser_for(repo_id),
                status=MS_PRESENT,
                notes="Imported from disk",
            )
            session.add(model)
            await session.flush()
            for n in nodes:
                session.add(ModelNodeState(model_id=model.id, node_id=n.id, status=MS_ABSENT))
            await session.commit()
            imported += 1
        except Exception:  # noqa: BLE001 - skip a problematic dir, keep going
            await session.rollback()
            continue

    # keep presence of every registry model accurate too
    for m in (await session.execute(select(ModelRegistry))).scalars().all():
        await refresh_presence(session, m.id)
    if imported:
        log.info("Model discovery imported %d model(s) from disk", imported)
    return imported


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
    all_ok = True
    for node in nodes:
        path = model_host_path(node, cfg, model.name)
        await handle.log(f"[{node.name}] rm -rf {path}")
        try:
            ssh = await ssh_for_node(session, node)
            # sudo: download runs as root in the container, so model files are
            # often root-owned — rm as the login user would hit "Permission denied".
            await ssh.run(f"rm -rf {shlex.quote(path)}", sudo=True, check=True)
            st = await _state_for(session, model_id, node.id)
            st.present = False
            st.size_bytes = None
            st.checksum_ok = None
            st.status = MS_ABSENT
        except Exception as exc:  # noqa: BLE001
            all_ok = False
            await handle.log(f"[{node.name}] delete failed: {exc}", "error")

    name = model.name
    if drop_row and all_ok:
        await session.delete(model)
    elif not all_ok:
        model.status = MS_ERROR
    else:
        model.status = MS_ABSENT
    await session.commit()
    if not all_ok:
        # don't claim success / drop the row when files remain — discovery would
        # just re-import them and the failure would look silent.
        raise RuntimeError(f"Delete of '{name}' failed on one or more nodes (see log).")
    return f"Model '{name}' deleted"


# --- helpers -------------------------------------------------------------
async def _all_nodes(session: AsyncSession) -> list[Node]:
    return list((await session.execute(select(Node))).scalars().all())


async def _dir_size_bytes(ssh, path: str, remote_prefix: str | None = None) -> int | None:
    cmd = f"du -sb {shlex.quote(path)} 2>/dev/null | cut -f1"
    if remote_prefix:
        # remote_prefix is a full "ssh -i KEY ... user@host" command
        cmd = f"{remote_prefix} {shlex.quote(cmd)}"
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
