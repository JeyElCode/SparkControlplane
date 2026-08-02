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
    EvalResult,
    EvalRun,
    PerfResult,
    ClusterConfig,
    Endpoint,
    EndpointAlias,
    EndpointPromotion,
    Instance,
    InstanceSchedule,
    ServeProfile,
    ModelNodeState,
    ModelRegistry,
    Node,
    Setting,
)
from .s3lite import S3Client, S3Config

log = logging.getLogger("spark.backup")

# 2: restore no longer empties tables the bundle does not carry.
# 3: named endpoints travel. Before this, a bundle from v1.34.x carried
#    instances whose `endpoint_id` referenced a table the bundle did not
#    contain — so restoring onto a FRESH box (the case a backup exists for)
#    aborted the ENTIRE restore on a FOREIGN KEY violation, not just the
#    endpoints. Restoring over the existing database masked it, because the
#    endpoint rows were still there.
# Bundles remain compatible in both directions; the version is informational.
BUNDLE_VERSION = 3

# Parent-first order; singletons (id=1) are updated in place on restore.
_TABLES: list[tuple[str, type]] = [
    ("nodes", Node),
    ("cluster_config", ClusterConfig),
    ("settings", Setting),
    ("models", ModelRegistry),
    ("model_node_states", ModelNodeState),
    # Before instances: an instance's `endpoint_id` points here. The reverse
    # reference (`endpoints.current_instance_id`) is the circular half of the
    # pair — it is nulled on insert and patched once instances exist, which is
    # also why the table is declared with use_alter=True.
    ("endpoints", Endpoint),
    ("instances", Instance),
    ("endpoint_aliases", EndpointAlias),
    # After instances: promotions reference both ends of a handoff. This is
    # history rather than configuration, but it is the ONLY record of what
    # served an endpoint before the current instance, which is what rollback
    # reads. Losing it turns a restored endpoint into one that cannot roll back.
    ("endpoint_promotions", EndpointPromotion),
    ("instance_schedules", InstanceSchedule),
    # Serve profiles: configuration worth a bring-up, so it belongs in a backup.
    # Built-in rows travel too, which is harmless — a restore replaces the table
    # wholesale and the next start re-seeds built-ins by name, so they converge
    # on the image's version either way rather than duplicating.
    ("serve_profiles", ServeProfile),
    # Eval history. Without these a restore silently empties the evidence —
    # the scores, the trend, and anything gating a promotion on them. Ordered
    # after `instances` because eval_runs.instance_id references it.
    ("eval_runs", EvalRun),
    ("eval_results", EvalResult),
    ("perf_results", PerfResult),
]
_SINGLETONS = {"cluster_config", "settings"}


# Columns that reference a table the bundle deliberately does NOT carry. `jobs`
# is transient run history, not configuration, so it is excluded — but SQLite
# runs with `PRAGMA foreign_keys=ON` (db.py:35), so restoring a row whose
# job_id names a job that no longer exists aborts the WHOLE restore with
# "FOREIGN KEY constraint failed". Nulled on export: the job it pointed at is
# gone by definition in the restored world, so the reference carries no
# information, and every one of these columns is nullable.
_DANGLING_FK_COLUMNS: dict[str, tuple[str, ...]] = {
    "model_node_states": ("last_job_id",),
    "eval_runs": ("job_id",),
    # The job that ran the promotion. `jobs` is run history and never travels,
    # so keeping the id would fail the FK and abort the restore.
    "endpoint_promotions": ("job_id",),
}


def _row_to_dict(obj, *, table: str | None = None) -> dict:
    drop = _DANGLING_FK_COLUMNS.get(table or obj.__table__.name, ())
    out = {}
    for col in obj.__table__.columns:
        v = getattr(obj, col.name)
        if col.name in drop:
            v = None
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


# A referencing column whose target table may be absent from an older bundle.
# Restoring such a row raises FOREIGN KEY and aborts the WHOLE restore — every
# other table with it — so the reference is dropped instead. Losing an
# instance's endpoint membership is recoverable; losing the restore is not.
_FK_DEPENDENCIES: dict[str, dict[str, str]] = {
    "instances": {"endpoint_id": "endpoints"},
}


def _null_orphan_fks(name: str, rows: list[dict], tables: dict) -> None:
    for column, target in _FK_DEPENDENCIES.get(name, {}).items():
        if target in tables:
            continue
        n = sum(1 for r in rows if r.get(column) is not None)
        for r in rows:
            r[column] = None
        if n:
            log.warning(
                "restore: bundle has no '%s' table, so %s.%s was dropped on %d "
                "row(s) — re-assign them after restoring",
                target, name, column, n,
            )


async def build_bundle() -> dict:
    from .. import __version__

    tables: dict[str, list[dict]] = {}
    async with _db.SessionLocal() as session:
        for name, model in _TABLES:
            rows = (await session.execute(select(model))).scalars().all()
            tables[name] = [_row_to_dict(r, table=name) for r in rows]
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
        # A table the bundle does not mention is NOT the same as a table the
        # bundle says is empty, and conflating them silently destroys data.
        #
        # `build_bundle` writes a key for every table it manages, so an empty
        # list genuinely means "there were no rows". A MISSING key means the
        # version that produced the bundle did not know the table existed. That
        # happens in both directions: a bundle from before v1.32.0 has no
        # eval_runs, and a bundle from v1.32.0 onward has no custom_tasks.
        #
        # Deleting on a missing key wiped eval history when restoring an older
        # bundle onto a newer build, and wiped custom tasks going the other way
        # — in both cases reported as a successful restore with a count of 0.
        # Restoring nothing over something is never what the operator meant.
        covered = [(n, m) for n, m in _TABLES if n in tables]
        skipped = [n for n, _ in _TABLES if n not in tables]

        # The circular half of endpoints <-> instances, in both directions.
        # Deleting instances while an endpoint still points at one violates the
        # FK, and inserting an endpoint before its instance exists violates it
        # the other way. Null it here, restore it at the end.
        current_by_endpoint: dict[str, int] = {}
        for row in tables.get("endpoints") or []:
            if row.get("current_instance_id") is not None:
                current_by_endpoint[row["name"]] = row["current_instance_id"]
                row["current_instance_id"] = None
        await session.execute(
            Endpoint.__table__.update().values(current_instance_id=None)
        )

        # children first
        for name, model in reversed(covered):
            if name in _SINGLETONS:
                continue
            await session.execute(delete(model))
        for name, model in covered:
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
            _null_orphan_fks(name, rows, tables)
            for row in rows:
                session.add(model(**_dict_to_kwargs(model, row)))
            # Flush per table so the parent-first order declared in `_TABLES`
            # is the order the database actually sees. Without it SQLAlchemy
            # sorts the whole batch by its own mapper dependency graph, which
            # cannot resolve the endpoints <-> instances cycle and put
            # endpoint_promotions after the instances it references.
            await session.flush()
            counts[name] = len(rows)
        await session.flush()

        # Now that instances exist, point each endpoint back at its server.
        for ep in (await session.execute(select(Endpoint))).scalars():
            if ep.name in current_by_endpoint:
                ep.current_instance_id = current_by_endpoint[ep.name]
        await session.commit()
    log.warning("backup restored: %s (cleared secrets: %d)", counts, len(cleared))
    if skipped:
        log.warning(
            "restore: bundle does not cover %s; existing rows left untouched",
            ", ".join(skipped),
        )
    return {"restored": counts, "cleared_secrets": sorted(set(cleared)),
            # Named explicitly so the operator sees that these were PRESERVED
            # rather than restored — a silent omission here is what made the
            # old behaviour dangerous.
            "not_in_bundle": skipped,
            "bundle_version": bundle.get("bundle_version"),
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
