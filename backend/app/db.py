"""Async SQLAlchemy engine/session setup and DB bootstrapping."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from sqlalchemy import event, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import get_settings
from .models import Base, ClusterConfig, Setting

log = logging.getLogger("spark.db")

_settings = get_settings()

# timeout (seconds) = SQLite busy timeout: writers wait for the lock instead of
# failing instantly with "database is locked" under the app's concurrent writes
# (streamed job logs, per-task eval commits, status polling, the perf sweep).
engine = create_async_engine(
    _settings.db_url, echo=False, future=True, connect_args={"timeout": 30}
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@event.listens_for(engine.sync_engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record) -> None:
    """WAL allows concurrent readers alongside a writer; busy_timeout makes
    contending writers wait. Applied on every new connection."""
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


def _add_missing_columns(conn) -> None:
    """Lightweight auto-migration: `create_all` adds new *tables* but never new
    *columns* on existing tables. For each mapped table that already exists, add
    any column present in the model but missing on disk (nullable / with its
    scalar default) via `ALTER TABLE ... ADD COLUMN`. Non-destructive."""
    insp = inspect(conn)
    existing_tables = set(insp.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # freshly created by create_all
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            coltype = col.type.compile(dialect=conn.dialect)
            ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {coltype}'
            default = col.default
            if default is not None and getattr(default, "is_scalar", False):
                val = default.arg
                if isinstance(val, bool):
                    val = 1 if val else 0
                elif isinstance(val, str):
                    val = "'" + val.replace("'", "''") + "'"
                ddl += f" DEFAULT {val}"
            conn.execute(text(ddl))
            log.warning("DB migration: added column %s.%s", table.name, col.name)


async def init_db() -> None:
    """Create tables, add any missing columns, and seed singleton rows."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)

    async with SessionLocal() as session:
        cfg = await session.get(ClusterConfig, 1)
        if cfg is None:
            session.add(
                ClusterConfig(
                    id=1,
                    cluster_name=_settings.default_cluster_name,
                    vllm_image=_settings.default_vllm_image,
                    qsfp_netmask=_settings.default_qsfp_netmask,
                    models_subdir=_settings.default_models_subdir,
                    hf_cache_subdir=_settings.default_hf_cache_subdir,
                    models_container_path=_settings.models_container_path,
                    hf_cache_container_path=_settings.hf_cache_container_path,
                    ray_port=_settings.ray_port,
                    shm_size=_settings.container_shm_size,
                )
            )
        setting = await session.get(Setting, 1)
        if setting is None:
            session.add(Setting(id=1, status_poll_seconds=_settings.status_poll_seconds))
        await session.commit()


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped session."""
    async with SessionLocal() as session:
        yield session


async def get_cluster_config(session: AsyncSession) -> ClusterConfig:
    cfg = await session.get(ClusterConfig, 1)
    assert cfg is not None, "ClusterConfig singleton missing; call init_db()"
    return cfg


async def get_setting(session: AsyncSession) -> Setting:
    setting = await session.get(Setting, 1)
    assert setting is not None, "Setting singleton missing; call init_db()"
    return setting


async def get_node_by_role(session: AsyncSession, role: str) -> "object | None":
    from .models import Node

    res = await session.execute(select(Node).where(Node.role == role))
    return res.scalar_one_or_none()
