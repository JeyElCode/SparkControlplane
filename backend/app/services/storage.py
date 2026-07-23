"""Storage inspection & cleanup for the models filesystem.

One batched SSH command per node reports: the size of every directory in the
models dir (mapped against the registry to spot **orphans** — directories no
registry row references), the HuggingFace cache size, and df for the
filesystem. Cleanup actions (delete an orphan dir, clear the HF cache) run as
jobs with the same sudo/quoting discipline as model deletion.
"""

from __future__ import annotations

import re
import shlex

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import db as _db
from ..models import ModelRegistry, Node
from ..ssh import ssh_for_node
from .jobs import JobHandle
from .paths import hf_cache_host_path, models_host_dir

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def parse_du_lines(raw: str) -> list[tuple[str, int]]:
    """`du -sk <dir>/*/` output -> [(dir_basename, bytes)]."""
    out = []
    for line in raw.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        size_kb, path = parts
        name = path.rstrip("/").rsplit("/", 1)[-1]
        if name:
            out.append((name, int(size_kb) * 1024))
    return out


def parse_df_line(raw: str) -> dict | None:
    """`df -k <dir> | tail -1` -> {total,used,free} bytes."""
    vals = raw.strip().split()
    if len(vals) >= 4 and vals[1].isdigit():
        return {
            "total_bytes": int(vals[1]) * 1024,
            "used_bytes": int(vals[2]) * 1024,
            "free_bytes": int(vals[3]) * 1024,
        }
    return None


async def storage_report(session: AsyncSession) -> list[dict]:
    """Per-node storage breakdown (live SSH; can take a few seconds per node)."""
    cfg = await _db.get_cluster_config(session)
    nodes = list((await session.execute(select(Node).order_by(Node.role, Node.id))).scalars())
    registry = {
        m.name for m in (await session.execute(select(ModelRegistry))).scalars()
    }
    out = []
    for node in nodes:
        models_dir = models_host_dir(node, cfg)
        hf_dir = hf_cache_host_path(node, cfg)
        entry: dict = {
            "node_id": node.id, "node_name": node.name, "reachable": False,
            "models_dir": models_dir, "hf_cache_dir": hf_dir,
            "models": [], "orphans": [], "hf_cache_bytes": None, "disk": None,
        }
        try:
            ssh = await ssh_for_node(session, node)
            script = (
                f"echo '@@df@@'; df -k {shlex.quote(models_dir)} 2>/dev/null | tail -1; "
                f"echo '@@dirs@@'; du -sk {shlex.quote(models_dir)}/*/ 2>/dev/null || true; "
                f"echo '@@hf@@'; du -sk {shlex.quote(hf_dir)} 2>/dev/null | tail -1 || true"
            )
            res = await ssh.run(script, sudo=True, timeout=120)
        except Exception as exc:  # noqa: BLE001
            entry["error"] = str(exc)
            out.append(entry)
            continue
        if not res.ok:
            entry["error"] = (res.stderr or res.stdout or "storage scan failed")[:300]
            out.append(entry)
            continue
        entry["reachable"] = True
        sections: dict[str, list[str]] = {}
        current = None
        for line in res.stdout.splitlines():
            s = line.strip()
            if s.startswith("@@") and s.endswith("@@"):
                current = s.strip("@")
                sections[current] = []
            elif current is not None and s:
                sections[current].append(line)
        entry["disk"] = parse_df_line("\n".join(sections.get("df", [])))
        for name, size in parse_du_lines("\n".join(sections.get("dirs", []))):
            item = {"name": name, "size_bytes": size}
            (entry["models"] if name in registry else entry["orphans"]).append(item)
        # du -sk of the cache dir itself: one "SIZE\tpath" row
        if sections.get("hf"):
            vals = sections["hf"][0].split(None, 1)
            if vals and vals[0].isdigit():
                entry["hf_cache_bytes"] = int(vals[0]) * 1024
        entry["models"].sort(key=lambda m: -m["size_bytes"])
        entry["orphans"].sort(key=lambda m: -m["size_bytes"])
        out.append(entry)
    return out


async def delete_orphan(handle: JobHandle, node_id: int, name: str) -> str:
    """Remove a directory in the models dir that no registry row references."""
    if not _NAME_RE.match(name):
        raise RuntimeError("Invalid directory name.")
    async with _db.SessionLocal() as session:
        node = await session.get(Node, node_id)
        if node is None:
            raise RuntimeError("Node not found.")
        registered = (
            await session.execute(select(ModelRegistry).where(ModelRegistry.name == name))
        ).scalar_one_or_none()
        if registered is not None:
            raise RuntimeError(
                f"'{name}' is a registered model — delete it from the Models page instead."
            )
        cfg = await _db.get_cluster_config(session)
        target = f"{models_host_dir(node, cfg)}/{name}"
        await handle.log(f"[{node.name}] deleting orphaned directory {target}")
        ssh = await ssh_for_node(session, node)
        res = await ssh.run(f"rm -rf {shlex.quote(target)}", sudo=True)
        if not res.ok:
            raise RuntimeError(f"delete failed: {res.stderr or res.stdout}")
        await handle.log(f"[{node.name}] removed. ✅")
        return f"Orphan '{name}' deleted on {node.name}"


async def clear_hf_cache(handle: JobHandle, node_ids: list[int] | None) -> str:
    """Empty the HuggingFace cache directory on the given nodes (all when None).
    Only cached downloads live there — models in the models dir are untouched."""
    async with _db.SessionLocal() as session:
        cfg = await _db.get_cluster_config(session)
        q = select(Node).order_by(Node.id)
        nodes = [n for n in (await session.execute(q)).scalars()
                 if node_ids is None or n.id in node_ids]
        if not nodes:
            raise RuntimeError("No matching nodes.")
        done = []
        for node in nodes:
            hf_dir = hf_cache_host_path(node, cfg)
            await handle.log(f"[{node.name}] clearing {hf_dir}/*")
            try:
                ssh = await ssh_for_node(session, node)
                res = await ssh.run(
                    f"rm -rf {shlex.quote(hf_dir)}/* {shlex.quote(hf_dir)}/.[!.]* 2>/dev/null; true",
                    sudo=True,
                )
                if res.ok:
                    done.append(node.name)
                    await handle.log(f"[{node.name}] cache cleared. ✅")
                else:
                    await handle.log(f"[{node.name}] failed: {res.stderr or res.stdout}", "error")
            except Exception as exc:  # noqa: BLE001
                await handle.log(f"[{node.name}] unreachable: {exc}", "error")
        if not done:
            raise RuntimeError("HF cache clear failed on every node.")
        return f"HF cache cleared on {', '.join(done)}"
