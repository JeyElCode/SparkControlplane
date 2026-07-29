"""Serve profiles: reusable vLLM settings, saved and shared."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import Instance, ServeProfile
from ..schemas import (
    ServeProfileExport,
    ServeProfileImportResult,
    ServeProfileIn,
    ServeProfileOut,
    ServeProfileUpdate,
)
from ..services import profiles as prof

router = APIRouter(prefix="/api/profiles", tags=["profiles"])


def _out(row: ServeProfile) -> ServeProfileOut:
    return ServeProfileOut.of(row, prof.parse_settings(row.settings_json))


@router.get("", response_model=list[ServeProfileOut])
async def list_profiles(session: AsyncSession = Depends(get_session)):
    rows = list(
        (
            await session.execute(
                select(ServeProfile).order_by(
                    ServeProfile.builtin.desc(), ServeProfile.name
                )
            )
        )
        .scalars()
        .all()
    )
    return [_out(r) for r in rows]


@router.post("", response_model=ServeProfileOut, status_code=201)
async def create_profile(payload: ServeProfileIn, session: AsyncSession = Depends(get_session)):
    settings, dropped = prof.sanitize_settings(payload.settings, trusted=True)
    if dropped:
        raise HTTPException(422, f"Not serve settings: {', '.join(sorted(dropped))}")
    try:
        settings = prof.validate_settings(settings)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    row = ServeProfile(
        name=payload.name,
        description=payload.description,
        repo_id=payload.repo_id,
        settings_json=json.dumps(settings),
        builtin=False,
    )
    session.add(row)
    try:
        await session.commit()
    except Exception as exc:  # noqa: BLE001 - unique name
        await session.rollback()
        raise HTTPException(409, f"Could not create profile: {exc}")
    return _out(row)


@router.post("/from-instance/{instance_id}", response_model=ServeProfileOut, status_code=201)
async def profile_from_instance(
    instance_id: int, payload: ServeProfileIn, session: AsyncSession = Depends(get_session)
):
    """Capture a working instance's settings as a profile — the point being that
    the configuration you just got serving is the one worth keeping."""
    inst = await session.get(Instance, instance_id)
    if inst is None:
        raise HTTPException(404, "Instance not found")
    settings = prof.settings_from_instance(inst)
    row = ServeProfile(
        name=payload.name,
        description=payload.description,
        repo_id=payload.repo_id,
        settings_json=json.dumps(settings),
        builtin=False,
    )
    session.add(row)
    try:
        await session.commit()
    except Exception as exc:  # noqa: BLE001
        await session.rollback()
        raise HTTPException(409, f"Could not create profile: {exc}")
    return _out(row)


@router.patch("/{profile_id}", response_model=ServeProfileOut)
async def update_profile(
    profile_id: int, payload: ServeProfileUpdate, session: AsyncSession = Depends(get_session)
):
    row = await session.get(ServeProfile, profile_id)
    if row is None:
        raise HTTPException(404, "Profile not found")
    if row.builtin:
        raise HTTPException(
            409,
            "Built-in profiles can't be edited — an upgrade would overwrite the "
            "change. Duplicate it into a profile of your own instead.",
        )
    data = payload.model_dump(exclude_unset=True)
    if (raw := data.pop("settings", None)) is not None:
        settings, dropped = prof.sanitize_settings(raw, trusted=True)
        if dropped:
            raise HTTPException(422, f"Not serve settings: {', '.join(sorted(dropped))}")
        try:
            row.settings_json = json.dumps(prof.validate_settings(settings))
        except ValueError as exc:
            raise HTTPException(422, str(exc))
    for field, value in data.items():
        setattr(row, field, value)
    await session.commit()
    return _out(row)


@router.delete("/{profile_id}", status_code=204)
async def delete_profile(profile_id: int, session: AsyncSession = Depends(get_session)):
    row = await session.get(ServeProfile, profile_id)
    if row is None:
        raise HTTPException(404, "Profile not found")
    if row.builtin:
        raise HTTPException(409, "Built-in profiles can't be deleted; they ship with the image.")
    await session.delete(row)
    await session.commit()


# --- sharing ---------------------------------------------------------------
@router.get("/export", response_model=ServeProfileExport)
async def export_profiles(session: AsyncSession = Depends(get_session)):
    """Every user profile as one shareable document — commit it, gist it, send
    it to whoever is standing up their own pair."""
    rows = list(
        (
            await session.execute(
                select(ServeProfile).where(ServeProfile.builtin.is_(False))
                .order_by(ServeProfile.name)
            )
        )
        .scalars()
        .all()
    )
    return ServeProfileExport(profiles=[
        ServeProfileIn(
            name=r.name, description=r.description, repo_id=r.repo_id,
            settings=prof.parse_settings(r.settings_json),
        )
        for r in rows
    ])


@router.post("/import", response_model=ServeProfileImportResult)
async def import_profiles(
    payload: ServeProfileExport, session: AsyncSession = Depends(get_session)
):
    """Import profiles from a shared document.

    This is untrusted input: the settings feed the vLLM command line and from
    there a ``docker run`` on the nodes with ``--gpus all`` and the models
    directory mounted. ``vllm_image`` and the raw ``extra_args`` passthrough are
    dropped rather than trusted — a shared profile may describe *how* to serve a
    model, not *what code* to run.
    """
    if payload.kind != ServeProfileExport().kind:
        raise HTTPException(422, "Not a Spark Control Plane serve-profile document.")
    if payload.version != 1:
        raise HTTPException(422, f"Unsupported profile document version {payload.version}.")

    existing = {
        r.name
        for r in (await session.execute(select(ServeProfile))).scalars().all()
    }
    result = ServeProfileImportResult()
    for incoming in payload.profiles:
        if incoming.name in existing:
            result.skipped.append(incoming.name)
            continue
        settings, dropped = prof.sanitize_settings(incoming.settings, trusted=False)
        result.dropped_fields.extend(
            f"{incoming.name}.{d}" for d in dropped
        )
        try:
            settings = prof.validate_settings(settings)
        except ValueError as exc:
            raise HTTPException(422, f"Profile '{incoming.name}': {exc}")
        session.add(ServeProfile(
            name=incoming.name,
            description=incoming.description,
            repo_id=incoming.repo_id,
            settings_json=json.dumps(settings),
            builtin=False,
        ))
        existing.add(incoming.name)
        result.imported.append(incoming.name)
    await session.commit()
    return result
