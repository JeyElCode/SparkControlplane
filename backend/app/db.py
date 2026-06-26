"""Async SQLAlchemy engine/session setup and DB bootstrapping."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import get_settings
from .models import Base, ClusterConfig, Setting

_settings = get_settings()

engine = create_async_engine(_settings.db_url, echo=False, future=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    """Create tables and seed singleton rows."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

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
