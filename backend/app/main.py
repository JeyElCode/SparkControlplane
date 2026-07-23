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
from .middleware import AuthMiddleware
from .routers import (
    alerts,
    auth,
    backup,
    cluster,
    evals,
    instances,
    jobs,
    logs,
    models,
    nodes,
    playground,
    power,
    schedules,
    status,
    usage,
)
from .ssh import pool

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("spark")

settings = get_settings()

# Build the optional MCP server once, up front, so the same instance backs both
# the ASGI mount and the session-manager lifespan below. Fail-open: a broken
# optional dependency must never take down the core API.
_mcp = None
if settings.mcp_active:
    try:
        from .mcp_server import build_mcp_server

        _mcp = build_mcp_server()
    except Exception:  # noqa: BLE001 - optional feature; keep serving the API
        log.exception("MCP enabled but failed to initialize; serving without /mcp")
        _mcp = None
elif settings.mcp_enabled:
    log.warning("MCP enabled but SPARK_MCP_TOKEN is unset; /mcp stays disabled (fail-closed).")


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
    from .services.alerts import manager as alert_manager
    from .services.telemetry import engine as telemetry_engine

    from .services.scheduler import scheduler as instance_scheduler
    from .services.usage import collector as usage_collector

    from .services.backup import runner as backup_runner

    telemetry_engine.start()
    alert_manager.start()
    usage_collector.start()
    instance_scheduler.start()
    backup_runner.start()
    task = asyncio.create_task(_startup_discover())
    # The mounted MCP sub-app's own lifespan is not run by Starlette's Mount, so
    # drive its streamable-HTTP session manager from here for its whole lifetime.
    if _mcp is not None:
        async with _mcp.session_manager.run():
            log.info("MCP server mounted at /mcp (streamable-HTTP)")
            yield
    else:
        yield
    task.cancel()
    await backup_runner.stop()
    await instance_scheduler.stop()
    await usage_collector.stop()
    await alert_manager.stop()
    await telemetry_engine.stop()
    await pool.close_all()


app = FastAPI(title="Spark Control Plane", version=__version__, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Outermost: session enforcement for /api + WebSockets (no-op in "none" mode).
app.add_middleware(AuthMiddleware)
if settings.effective_auth_mode != "none":
    log.info("Portal auth is ON (mode=%s)", settings.effective_auth_mode)

for r in (nodes, cluster, models, instances, status, playground, jobs, evals, power, logs, alerts, auth, usage, schedules, backup):
    app.include_router(r.router)


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": __version__}


@app.get("/api/meta")
async def meta():
    return {
        "name": "Spark Control Plane",
        "version": __version__,
        "mcp_enabled": _mcp is not None,
    }


@app.get("/metrics")
async def prometheus_metrics():
    """Prometheus exposition of the telemetry caches (nodes + vLLM instances).
    Registered before the SPA catch-all so it isn't swallowed by the frontend."""
    from fastapi.responses import PlainTextResponse

    from .services.telemetry import engine as telemetry_engine

    return PlainTextResponse(
        telemetry_engine.prometheus_text(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


# --- Optional MCP server -------------------------------------------------
# Mount the streamable-HTTP MCP endpoint at /mcp, behind a bearer-token gate.
# Mounted before the SPA catch-all so /mcp is not swallowed by the frontend.
if _mcp is not None:
    from .mcp_server import BearerAuthMiddleware

    app.mount(
        "/mcp",
        BearerAuthMiddleware(_mcp.streamable_http_app(), settings.mcp_token),
        name="mcp",
    )


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
