"""Config backup & restore.

The bundle is a JSON snapshot of every *configuration* table — nodes, cluster
config, settings, model registry (+ per-node states), instances, schedules,
custom eval tasks. History (jobs, eval runs, usage, alerts) is deliberately
excluded. Secrets stay in their Fernet-encrypted form, so a restore is fully
functional only with the same SPARK_SECRET_KEY; with a different key the
restore still succeeds but clears the undecryptable secrets and reports which
ones need re-entering.

Scheduled backups upload the bundle to S3-compatible storage (see s3lite) on
an interval, keeping the newest ``backup_retention`` objects.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import delete, select

from .. import db as _db
from ..crypto import decrypt
from ..models import (
    ClusterConfig,
    CustomTask,
    Instance,
    InstanceSchedule,
    ModelNodeState,
    ModelRegistry,
    Node,
    Setting,
)
from .s3lite import S3Client, S3Config

log = logging.getLogger("spark.backup")

BUNDLE_VERSION = 1

# Parent-first order; singletons (id=1) are updated in place on restore.
_TABLES: list[tuple[str, type]] = [
    ("nodes", Node),
    ("cluster_config", ClusterConfig),
    ("settings", Setting),
    ("models", ModelRegistry),
    ("model_node_states", ModelNodeState),
    ("instances", Instance),
    ("instance_schedules", InstanceSchedule),
    ("custom_tasks", CustomTask),
]
_SINGLETONS = {"cluster_config", "settings"}


def _row_to_dict(obj) -> dict:
    out = {}
    for col in obj.__table__.columns:
        v = getattr(obj, col.name)
        if isinstance(v, datetime):
            v = v.isoformat()
        out[col.name] = v
    return out


def _dict_to_kwargs(model: type, data: dict) -> dict:
    out = {}
    for col in model.__table__.columns:  # ignore unknown keys from newer/older bundles
        if col.name not in data:
            continue
        v = data[col.name]
        try:
            py_type = col.type.python_type
        except NotImplementedError:
            py_type = None
        if v is not None and py_type is datetime and isinstance(v, str):
            try:
                v = datetime.fromisoformat(v)
            except ValueError:
                v = None
        out[col.name] = v
    return out


async def build_bundle() -> dict:
    from .. import __version__

    tables: dict[str, list[dict]] = {}
    async with _db.SessionLocal() as session:
        for name, model in _TABLES:
            rows = (await session.execute(select(model))).scalars().all()
            tables[name] = [_row_to_dict(r) for r in rows]
    return {
        "kind": "spark-controlplane-backup",
        "bundle_version": BUNDLE_VERSION,
        "app_version": __version__,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tables": tables,
    }


def _clear_undecryptable(rows: list[dict], model: type, cleared: list[str]) -> None:
    for row in rows:
        for key, val in list(row.items()):
            if key.endswith("_enc") and val:
                try:
                    decrypt(val)
                except Exception:  # noqa: BLE001 - different SPARK_SECRET_KEY
                    row[key] = None
                    cleared.append(f"{model.__tablename__}.{key}")


async def apply_bundle(bundle: dict) -> dict:
    """Replace all config tables with the bundle's contents (ids preserved so
    FKs stay intact). Returns a summary incl. secrets that had to be cleared."""
    if bundle.get("kind") != "spark-controlplane-backup" or "tables" not in bundle:
        raise ValueError("Not a Spark Control Plane backup bundle.")
    tables = bundle["tables"]
    cleared: list[str] = []
    counts: dict[str, int] = {}

    async with _db.SessionLocal() as session:
        # children first
        for name, model in reversed(_TABLES):
            if name in _SINGLETONS:
                continue
            await session.execute(delete(model))
        for name, model in _TABLES:
            rows = tables.get(name) or []
            _clear_undecryptable(rows, model, cleared)
            if name in _SINGLETONS:
                if rows:
                    current = await session.get(model, 1)
                    data = _dict_to_kwargs(model, rows[0])
                    data.pop("id", None)
                    if current is None:
                        session.add(model(id=1, **data))
                    else:
                        for k, v in data.items():
                            setattr(current, k, v)
                counts[name] = min(1, len(rows))
                continue
            for row in rows:
                session.add(model(**_dict_to_kwargs(model, row)))
            counts[name] = len(rows)
        await session.commit()
    log.warning("backup restored: %s (cleared secrets: %d)", counts, len(cleared))
    return {"restored": counts, "cleared_secrets": sorted(set(cleared)),
            "app_version": bundle.get("app_version"), "created_at": bundle.get("created_at")}


# --- S3 side --------------------------------------------------------------
async def _client(session) -> tuple[S3Client, str] | None:
    setting = await _db.get_setting(session)
    secret = decrypt(setting.backup_s3_secret_enc) if setting.backup_s3_secret_enc else None
    if not (setting.backup_s3_endpoint and setting.backup_s3_bucket
            and setting.backup_s3_access_key and secret):
        return None
    cfg = S3Config(
        endpoint=setting.backup_s3_endpoint, bucket=setting.backup_s3_bucket,
        region=setting.backup_s3_region or "us-east-1",
        access_key=setting.backup_s3_access_key, secret_key=secret,
    )
    prefix = setting.backup_s3_prefix or ""
    return S3Client(cfg), prefix


async def run_backup() -> str:
    """Build + upload one backup; prune to retention. Returns the object key."""
    async with _db.SessionLocal() as session:
        pair = await _client(session)
        if pair is None:
            raise RuntimeError("S3 backup is not fully configured (endpoint/bucket/keys).")
        setting = await _db.get_setting(session)
        retention = max(1, setting.backup_retention)
    client, prefix = pair
    bundle = await build_bundle()
    key = f"{prefix}spark-backup-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%SZ')}.json"
    await client.put_object(key, json.dumps(bundle).encode())
    try:
        objs = sorted((o["key"] for o in await client.list_objects(prefix)
                       if o["key"].endswith(".json")), reverse=True)
        for old in objs[retention:]:
            await client.delete_object(old)
    except Exception:  # noqa: BLE001 - retention is best-effort
        log.warning("backup retention pruning failed", exc_info=True)
    return key


async def list_backups() -> list[dict]:
    async with _db.SessionLocal() as session:
        pair = await _client(session)
    if pair is None:
        raise RuntimeError("S3 backup is not fully configured (endpoint/bucket/keys).")
    client, prefix = pair
    objs = [o for o in await client.list_objects(prefix) if o["key"].endswith(".json")]
    return sorted(objs, key=lambda o: o["key"], reverse=True)


async def restore_from_s3(key: str) -> dict:
    async with _db.SessionLocal() as session:
        pair = await _client(session)
    if pair is None:
        raise RuntimeError("S3 backup is not fully configured (endpoint/bucket/keys).")
    client, _ = pair
    raw = await client.get_object(key)
    return await apply_bundle(json.loads(raw))


class BackupRunner:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stopping = False
        self.last_ok_ts: float | None = None
        self.last_key: str | None = None
        self.last_error: str | None = None
        self._last_attempt: float | None = None

    def start(self) -> None:
        if self._task is None:
            self._stopping = False
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except BaseException:  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                async with _db.SessionLocal() as session:
                    setting = await _db.get_setting(session)
                    enabled = setting.backup_enabled
                    interval = max(0.25, setting.backup_interval_hours) * 3600
                due = enabled and (
                    self.last_ok_ts is None or time.time() - self.last_ok_ts >= interval
                )
                # after a failure, retry every 15 min instead of every tick
                if due and self.last_error and self._last_attempt and \
                        time.time() - self._last_attempt < 900:
                    due = False
                if due:
                    self._last_attempt = time.time()
                    try:
                        self.last_key = await run_backup()
                        self.last_ok_ts = time.time()
                        self.last_error = None
                        log.info("scheduled backup uploaded: %s", self.last_key)
                    except Exception as exc:  # noqa: BLE001
                        self.last_error = str(exc)
                        log.warning("scheduled backup failed: %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("backup loop tick failed")
            await asyncio.sleep(60)


runner = BackupRunner()
