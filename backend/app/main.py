"""FastAPI application entrypoint.

Serves the JSON API under ``/api`` and the built React SPA for everything else.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import get_settings
from .db import init_db
from .routers import cluster, instances, jobs, models, nodes, playground, status
from .ssh import pool

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("spark")


async def _startup_discover() -> None:
    """Best-effort: import any on-disk models into the registry shortly after
    boot, so the registry mirrors what's actually on the nodes."""
    try:
        await asyncio.sleep(5)
        from .db import SessionLocal
        from .services.models_svc import discover_models

        async with SessionLocal() as session:
            await discover_models(session)
    except Exception:  # noqa: BLE001 - nodes may be unset/unreachable at boot
        log.warning("Startup model discovery skipped", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    log.info("Spark Control Plane %s started", __version__)
    task = asyncio.create_task(_startup_discover())
    yield
    task.cancel()
    await pool.close_all()


app = FastAPI(title="Spark Control Plane", version=__version__, lifespan=lifespan)

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in (nodes, cluster, models, instances, status, playground, jobs):
    app.include_router(r.router)


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": __version__}


@app.get("/api/meta")
async def meta():
    return {"name": "Spark Control Plane", "version": __version__}


# --- Serve the built SPA -------------------------------------------------
# Resolve the SPA in priority order so it works for the Docker image, an editable
# source checkout, and a packaged wheel that bundles the build into app/static.
def _resolve_frontend_dir() -> Path:
    candidates = []
    env = os.environ.get("SPARK_FRONTEND_DIR")
    if env:
        candidates.append(Path(env))
    here = Path(__file__).resolve()
    candidates.append(here.parent / "static")            # packaged wheel: app/static
    candidates.append(here.parents[2] / "frontend" / "dist")  # editable / source layout
    for c in candidates:
        if (c / "index.html").is_file():
            return c
    return candidates[0] if candidates else here.parent / "static"


_frontend_dir = _resolve_frontend_dir()

if (_frontend_dir / "index.html").is_file():
    assets = _frontend_dir / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/{full_path:path}")
    async def spa(full_path: str):
        if full_path.startswith("api"):
            raise HTTPException(404, "Not found")
        candidate = _frontend_dir / full_path
        if full_path and candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(_frontend_dir / "index.html"))
else:
    log.warning("Frontend build not found at %s; serving API only.", _frontend_dir)
