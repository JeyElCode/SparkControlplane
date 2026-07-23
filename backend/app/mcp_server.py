"""Model Context Protocol (MCP) server for the Spark Control Plane.

Exposes the full control plane over a **streamable-HTTP** MCP endpoint so it can
be attached to Claude (or any MCP client) as a skill / server. Every HTTP router
has a matching MCP *tool*, and the read-heavy surfaces (status, instances,
models, nodes) are additionally exposed as MCP *resources*.

Design notes
------------
* **No business logic is re-implemented here.** Each tool simply calls the same
  router handler the HTTP API calls, passing an explicit
  :class:`~sqlalchemy.ext.asyncio.AsyncSession` (the FastAPI ``Depends`` default
  is overridden). The Pydantic request/response schemas from :mod:`app.schemas`
  are reused verbatim as tool inputs/outputs.
* Long-running actions (start/stop/delete an instance, download/sync a model,
  cluster setup/teardown, run an eval, …) return a :class:`JobAccepted` /
  ``EvalStarted`` handle exactly like the HTTP API; poll ``job_get`` / ``job_list``
  for progress. The in-process :class:`JobManager` is shared with the HTTP app,
  so jobs kicked off over MCP run in the same worker.
* Auth is enforced by :class:`BearerAuthMiddleware`, wrapped around the mounted
  ASGI app in :mod:`app.main` — this module does not read the token itself.

The ``mcp`` SDK is imported lazily inside :func:`build_mcp_server` so importing
this module never hard-fails when the optional dependency is absent; the only
top-level imports are stdlib + first-party.
"""

from __future__ import annotations

import hmac
import json
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from .db import SessionLocal
from .routers import alerts as alerts_router
from .routers import backup as backup_router
from .routers import cluster as cluster_router
from .routers import evals as evals_router
from .routers import instances as instances_router
from .routers import jobs as jobs_router
from .routers import logs as logs_router
from .routers import models as models_router
from .routers import nodes as nodes_router
from .routers import playground as playground_router
from .routers import power as power_router
from .routers import schedules as schedules_router
from .routers import storage as storage_router
from .routers import usage as usage_router
from .schemas import (
    ActiveAlert,
    AlertOut,
    ClusterConfigIn,
    ClusterConfigOut,
    ConnectionTest,
    CustomTaskIn,
    CustomTaskOut,
    EvalRunDetail,
    EvalRunOut,
    EvalRunRequest,
    EvalStarted,
    InstanceHistory,
    InstanceIn,
    InstanceOut,
    InstanceUpdate,
    InterfaceInfo,
    JobAccepted,
    JobDetail,
    JobOut,
    ImageUpdateIn,
    ModelIn,
    ModelOut,
    ModelSuggestion,
    NodeHistory,
    NodeIn,
    NodeOut,
    NodeUpdate,
    PlaygroundRequest,
    PlaygroundResponse,
    SettingsIn,
    SettingsOut,
    SetupRequest,
    StatusSnapshot,
    TeardownRequest,
)
from .services import telemetry

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mcp.server.fastmcp import FastMCP


# --------------------------------------------------------------------------- #
# Auth middleware
# --------------------------------------------------------------------------- #
class BearerAuthMiddleware:
    """Pure-ASGI bearer-token gate for the mounted ``/mcp`` sub-app.

    Rejects any HTTP request whose ``Authorization`` header is missing or does
    not exactly equal ``Bearer <token>`` (constant-time compare). Non-HTTP
    scopes (lifespan/websocket) are passed straight through.
    """

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self._expected = f"Bearer {token}"

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        provided = headers.get(b"authorization")
        if provided is None or not hmac.compare_digest(
            provided.decode("latin-1"), self._expected
        ):
            await self._reject(send)
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(send: Any) -> None:
        body = json.dumps(
            {"error": "unauthorized", "detail": "Missing or invalid bearer token for /mcp"}
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b"Bearer"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


# --------------------------------------------------------------------------- #
# Router-reuse helpers
# --------------------------------------------------------------------------- #
def _http_msg(exc: HTTPException) -> str:
    return f"HTTP {exc.status_code}: {exc.detail}"


async def _with_session(handler: Any, **kwargs: Any) -> Any:
    """Call a router handler inside a fresh session, translating HTTPException
    (the API's error channel) into a plain ValueError so the MCP client sees a
    clean tool error instead of an opaque 500."""
    async with SessionLocal() as session:
        try:
            return await handler(session=session, **kwargs)
        except HTTPException as exc:
            raise ValueError(_http_msg(exc)) from None


async def _no_session(handler: Any, **kwargs: Any) -> Any:
    try:
        return await handler(**kwargs)
    except HTTPException as exc:
        raise ValueError(_http_msg(exc)) from None


def _dump(obj: Any) -> Any:
    if isinstance(obj, list):
        return [o.model_dump(mode="json") for o in obj]
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    return obj


def _json(obj: Any) -> str:
    return json.dumps(_dump(obj), indent=2, default=str)


# --------------------------------------------------------------------------- #
# Server construction
# --------------------------------------------------------------------------- #
def build_mcp_server() -> "FastMCP":
    """Construct and return the streamable-HTTP MCP server (stateless).

    ``streamable_http_path='/'`` so that, once mounted at ``/mcp`` in the
    FastAPI app, the protocol endpoint is exactly ``/mcp``.
    """
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings

    from .config import get_settings

    _s = get_settings()
    # Host-header allowlist so /mcp works behind a reverse proxy/ingress. "*"
    # disables the DNS-rebinding host check (trusted-proxy mode); otherwise allow
    # the configured hosts (with any-port variants) plus localhost.
    if "*" in _s.mcp_allowed_hosts:
        _transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    else:
        _hosts = ["localhost", "127.0.0.1"]
        for h in _s.mcp_allowed_hosts:
            _hosts += [h, f"{h}:*"]
        _transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=_hosts,
            allowed_origins=_s.mcp_allowed_origins,
        )

    mcp = FastMCP(
        "spark-controlplane",
        instructions=(
            "Control plane for a DGX Spark vLLM cluster (up to 4 nodes). Tools "
            "mirror the REST API: nodes (incl. NIC detection), models, instances, "
            "cluster lifecycle, image updates, evals, jobs, playground, live "
            "telemetry + history, log tailing, power controls, alerts, usage "
            "history, instance live-window schedules, config backup/restore, and "
            "storage cleanup. Long-running actions return a job handle — poll "
            "job_get/job_list for progress. Tools marked DESTRUCTIVE (power, "
            "backup restore, orphan deletion, image update) should be confirmed "
            "with the operator before use. Read-only state is also available as "
            "spark:// resources."
        ),
        stateless_http=True,
        streamable_http_path="/",
        transport_security=_transport_security,
    )

    # ---------------- status ---------------- #
    @mcp.tool()
    async def status_get() -> StatusSnapshot:
        """Live cluster status snapshot: nodes, GPUs, Ray, running instances, warnings."""
        async with SessionLocal() as session:
            return await telemetry.engine.compose_snapshot(session)

    # ---------------- instances ---------------- #
    @mcp.tool()
    async def instance_list() -> list[InstanceOut]:
        """List all vLLM serving instances (newest first)."""
        return await _with_session(instances_router.list_instances)

    @mcp.tool()
    async def instance_get(instance_id: int) -> InstanceOut:
        """Get a single instance by id."""
        return await _with_session(instances_router.get_instance, instance_id=instance_id)

    @mcp.tool()
    async def instance_create(payload: InstanceIn) -> InstanceOut:
        """Create a new serving instance (does not start it unless autostart)."""
        return await _with_session(instances_router.create_instance, payload=payload)

    @mcp.tool()
    async def instance_update(instance_id: int, payload: InstanceUpdate) -> InstanceOut:
        """Update serve settings of a stopped instance (they apply on next start)."""
        return await _with_session(
            instances_router.update_instance, instance_id=instance_id, payload=payload
        )

    @mcp.tool()
    async def instance_start(instance_id: int) -> JobAccepted:
        """Start an instance (async job — poll job_get for progress)."""
        return await _with_session(instances_router.start_instance, instance_id=instance_id)

    @mcp.tool()
    async def instance_stop(instance_id: int) -> JobAccepted:
        """Stop a running instance (async job)."""
        return await _with_session(instances_router.stop_instance, instance_id=instance_id)

    @mcp.tool()
    async def instance_delete(instance_id: int) -> JobAccepted:
        """Delete an instance and its systemd unit (async job)."""
        return await _with_session(instances_router.delete_instance, instance_id=instance_id)

    # ---------------- models ---------------- #
    @mcp.tool()
    async def model_list() -> list[ModelOut]:
        """List the model registry with per-node presence and any active file job."""
        return await _with_session(models_router.list_models)

    @mcp.tool()
    async def model_suggestions() -> list[ModelSuggestion]:
        """Curated list of suggested HuggingFace models to add."""
        return await _no_session(models_router.suggestions)

    @mcp.tool()
    async def model_scan() -> list[ModelOut]:
        """Scan node disks and import any on-disk model not yet in the registry."""
        return await _with_session(models_router.scan)

    @mcp.tool()
    async def model_validate(repo_id: str) -> dict[str, Any]:
        """Validate a HuggingFace repo id (existence / access / approx size)."""
        return await _no_session(models_router.validate, repo_id=repo_id)

    @mcp.tool()
    async def model_register(payload: ModelIn) -> ModelOut:
        """Register a model in the catalog by HuggingFace repo id."""
        return await _with_session(models_router.add_model, payload=payload)

    @mcp.tool()
    async def model_get(model_id: int) -> ModelOut:
        """Get one registry model by id."""
        return await _with_session(models_router.get_model, model_id=model_id)

    @mcp.tool()
    async def model_download(model_id: int, auto_sync: bool = True) -> JobAccepted:
        """Download a model onto the head node (async job); optionally sync to peers."""
        return await _with_session(
            models_router.download, model_id=model_id, auto_sync=auto_sync
        )

    @mcp.tool()
    async def model_sync(model_id: int, target_node_id: int | None = None) -> JobAccepted:
        """Sync a downloaded model to worker node(s) over the QSFP link (async job)."""
        return await _with_session(
            models_router.sync, model_id=model_id, target_node_id=target_node_id
        )

    @mcp.tool()
    async def model_refresh(model_id: int) -> ModelOut:
        """Re-check on-disk presence/size of a model on every node."""
        return await _with_session(models_router.refresh, model_id=model_id)

    @mcp.tool()
    async def model_delete_files(
        model_id: int, node_ids: list[int] | None = None, drop_row: bool = False
    ) -> JobAccepted:
        """Delete a model's files from node(s) (async job); drop_row also removes the registry row."""
        return await _with_session(
            models_router.delete_files,
            model_id=model_id,
            node_ids=node_ids,
            drop_row=drop_row,
        )

    @mcp.tool()
    async def model_delete(model_id: int) -> dict[str, bool]:
        """Remove a model's registry row (does not touch on-disk files)."""
        await _with_session(models_router.remove_registry, model_id=model_id)
        return {"deleted": True}

    # ---------------- nodes ---------------- #
    @mcp.tool()
    async def node_list() -> list[NodeOut]:
        """List configured cluster nodes (head/worker)."""
        return await _with_session(nodes_router.list_nodes)

    @mcp.tool()
    async def node_get(node_id: int) -> NodeOut:
        """Get one node by id."""
        return await _with_session(nodes_router.get_node, node_id=node_id)

    @mcp.tool()
    async def node_create(payload: NodeIn) -> NodeOut:
        """Register a node (head or worker) with its SSH/QSFP connection details."""
        return await _with_session(nodes_router.create_node, payload=payload)

    @mcp.tool()
    async def node_update(node_id: int, payload: NodeUpdate) -> NodeOut:
        """Update a node's connection details / credentials."""
        return await _with_session(nodes_router.update_node, node_id=node_id, payload=payload)

    @mcp.tool()
    async def node_test(node_id: int) -> ConnectionTest:
        """Test SSH/sudo/docker/GPU reachability of a node."""
        return await _with_session(nodes_router.test_node, node_id=node_id)

    @mcp.tool()
    async def node_harden(node_id: int) -> JobAccepted:
        """Apply the node-hardening playbook to a node (async job)."""
        return await _with_session(nodes_router.harden_node, node_id=node_id)

    @mcp.tool()
    async def node_delete(node_id: int) -> dict[str, bool]:
        """Remove a node from the control plane."""
        await _with_session(nodes_router.delete_node, node_id=node_id)
        return {"deleted": True}

    # ---------------- cluster ---------------- #
    @mcp.tool()
    async def cluster_config_get() -> ClusterConfigOut:
        """Get the cluster configuration (image, subdirs, QSFP netmask, …)."""
        return await _with_session(cluster_router.get_config)

    @mcp.tool()
    async def cluster_config_patch(payload: ClusterConfigIn) -> ClusterConfigOut:
        """Patch the cluster configuration."""
        return await _with_session(cluster_router.update_config, payload=payload)

    @mcp.tool()
    async def cluster_settings_get() -> SettingsOut:
        """Get global settings (HF token presence, poll interval, judge config)."""
        return await _with_session(cluster_router.get_settings_ep)

    @mcp.tool()
    async def cluster_settings_patch(payload: SettingsIn) -> SettingsOut:
        """Patch global settings (HF token, poll interval, judge endpoint)."""
        return await _with_session(cluster_router.update_settings_ep, payload=payload)

    @mcp.tool()
    async def cluster_phases() -> list[dict[str, Any]]:
        """List the ordered cluster-setup phases."""
        return await _no_session(cluster_router.list_phases)

    @mcp.tool()
    async def cluster_setup(payload: SetupRequest) -> JobAccepted:
        """Run cluster setup — the full pipeline or a subset of phases (async job)."""
        return await _no_session(cluster_router.run_setup, payload=payload)

    @mcp.tool()
    async def cluster_teardown(payload: TeardownRequest) -> JobAccepted:
        """Tear down the cluster with the given options (async job)."""
        return await _no_session(cluster_router.run_teardown, payload=payload)

    # ---------------- evals ---------------- #
    @mcp.tool()
    async def eval_catalog() -> dict[str, Any]:
        """Available eval categories (built-in performance + custom task categories)."""
        return await _with_session(evals_router.catalog)

    @mcp.tool()
    async def eval_task_list() -> list[CustomTaskOut]:
        """List user-authored custom eval tasks."""
        return await _with_session(evals_router.list_tasks)

    @mcp.tool()
    async def eval_task_create(payload: CustomTaskIn) -> CustomTaskOut:
        """Create a custom eval task."""
        return await _with_session(evals_router.create_task, payload=payload)

    @mcp.tool()
    async def eval_task_update(task_id: int, payload: CustomTaskIn) -> CustomTaskOut:
        """Update a custom eval task."""
        return await _with_session(evals_router.update_task, task_id=task_id, payload=payload)

    @mcp.tool()
    async def eval_task_delete(task_id: int) -> dict[str, bool]:
        """Delete a custom eval task."""
        await _with_session(evals_router.delete_task, task_id=task_id)
        return {"deleted": True}

    @mcp.tool()
    async def eval_run(payload: EvalRunRequest) -> EvalStarted:
        """Start a capability/performance evaluation of an instance (async job)."""
        return await _with_session(evals_router.create_eval, payload=payload)

    @mcp.tool()
    async def eval_list() -> list[EvalRunOut]:
        """List eval runs (newest first)."""
        return await _with_session(evals_router.list_evals)

    @mcp.tool()
    async def eval_get(run_id: int) -> EvalRunDetail:
        """Get one eval run with full per-task results and perf table."""
        return await _with_session(evals_router.get_eval, run_id=run_id)

    @mcp.tool()
    async def eval_delete(run_id: int) -> dict[str, bool]:
        """Delete an eval run."""
        await _with_session(evals_router.delete_eval, run_id=run_id)
        return {"deleted": True}

    # ---------------- jobs ---------------- #
    @mcp.tool()
    async def job_list(limit: int = 50) -> list[JobOut]:
        """List recent background jobs (newest first)."""
        return await _with_session(jobs_router.list_jobs, limit=limit)

    @mcp.tool()
    async def job_get(job_id: int) -> JobDetail:
        """Get one job with its captured log lines."""
        return await _with_session(jobs_router.get_job, job_id=job_id)

    @mcp.tool()
    async def job_cancel(job_id: int) -> dict[str, Any]:
        """Cancel a running background job."""
        return await _no_session(jobs_router.cancel_job, job_id=job_id)

    # ---------------- playground ---------------- #
    @mcp.tool()
    async def playground_chat(payload: PlaygroundRequest) -> PlaygroundResponse:
        """Send a one-shot chat completion to a running instance's OpenAI endpoint."""
        return await _with_session(playground_router.chat, payload=payload)

    # ---------------- telemetry history ---------------- #
    @mcp.tool()
    async def node_history(minutes: int = 15) -> list[NodeHistory]:
        """Per-node telemetry history (CPU %, memory, GPU, QSFP/LAN B/s, disk) for sparklines/trends."""
        return telemetry.engine.history(minutes=minutes)

    @mcp.tool()
    async def instance_history(minutes: int = 15) -> list[InstanceHistory]:
        """Per-instance vLLM serving history (tokens/s, queue depth, KV-cache %, TTFT)."""
        return telemetry.engine.instance_history(minutes=minutes)

    # ---------------- node interfaces ---------------- #
    @mcp.tool()
    async def node_interfaces(node_id: int) -> list[InterfaceInfo]:
        """Enumerate a node's physical NICs (link state, speed, driver, MAC, QSFP-candidate flag)."""
        return await _with_session(nodes_router.list_interfaces, node_id=node_id)

    # ---------------- power ---------------- #
    @mcp.tool()
    async def power_affected(node_id: int) -> list[str]:
        """RUNNING instances a shutdown of this node would take down — check BEFORE power_node."""
        return await _with_session(power_router.get_affected, node_id=node_id)

    @mcp.tool()
    async def power_node(node_id: int, action: str) -> JobAccepted:
        """DESTRUCTIVE: power action on a node — 'shutdown' | 'reboot' | 'wake' (Wake-on-LAN).
        Shutdown/reboot kill any instances on the node; call power_affected first and
        confirm with the operator before shutting down a node that serves traffic."""
        return await _with_session(power_router.node_power, node_id=node_id, action=action)

    @mcp.tool()
    async def power_batch(action: str) -> JobAccepted:
        """DESTRUCTIVE: fleet-wide 'shutdown' (workers first, then head) or 'wake'.
        Shutting down the whole fleet stops every model — confirm with the operator first."""
        return await _no_session(power_router.batch_power, action=action)

    # ---------------- logs ---------------- #
    @mcp.tool()
    async def log_units() -> list[logs_router.LogUnit]:
        """Every tailable systemd unit the portal manages (Ray, vLLM instances, TLS proxies), with its node."""
        return await _with_session(logs_router.list_units)

    @mcp.tool()
    async def log_tail(node_id: int, unit: str, lines: int = 100) -> str:
        """Fetch the last N journal lines for a spark-* unit on a node (one-shot, not a live follow)."""
        import shlex

        from .models import Node
        from .ssh import ssh_for_node

        if not logs_router._UNIT_RE.match(unit):
            raise ValueError("unit must be a spark-* systemd unit (see log_units)")
        lines = max(1, min(int(lines), 2000))
        async with SessionLocal() as session:
            node = await session.get(Node, node_id)
            if node is None:
                raise ValueError("Node not found")
            ssh = await ssh_for_node(session, node)
        res = await ssh.run(
            f"journalctl -u {shlex.quote(unit)} -n {lines} --no-pager 2>&1 || true",
            sudo=True, timeout=30,
        )
        return res.stdout

    # ---------------- alerts ---------------- #
    @mcp.tool()
    async def alert_list(limit: int = 50) -> list[AlertOut]:
        """Alert history, newest first (resolved_at null = still active)."""
        return await _with_session(alerts_router.list_alerts, limit=limit)

    @mcp.tool()
    async def alert_active() -> list[ActiveAlert]:
        """Currently-firing alerts (same set as the dashboard banners)."""
        return await _no_session(alerts_router.active_alerts)

    @mcp.tool()
    async def alert_test_webhook() -> dict:
        """Send a test notification through the configured alert webhook."""
        return await _with_session(alerts_router.test_webhook)

    # ---------------- usage ---------------- #
    @mcp.tool()
    async def usage_get(days: int = 30, bucket: str = "day") -> list[usage_router.ModelUsage]:
        """Persistent serving usage per model (tokens, requests, TTFT), bucketed by 'day' or 'hour'."""
        return await _with_session(usage_router.get_usage, days=days, bucket=bucket)

    # ---------------- schedules ---------------- #
    @mcp.tool()
    async def schedule_list() -> list[schedules_router.ScheduleOut]:
        """All instance live-windows with planner fields (est GiB/node, node scope)."""
        return await _with_session(schedules_router.list_schedules)

    @mcp.tool()
    async def schedule_now() -> dict:
        """The scheduler's current wall clock (timezone, weekday, minutes)."""
        return await _no_session(schedules_router.schedules_now)

    @mcp.tool()
    async def schedule_create(payload: schedules_router.ScheduleIn) -> schedules_router.ScheduleOut:
        """Add a weekly live-window: the instance starts when it opens, stops when it closes
        (days 0-6 Mon-first; end <= start wraps past midnight)."""
        return await _with_session(schedules_router.create_schedule, payload=payload)

    @mcp.tool()
    async def schedule_update(
        schedule_id: int, payload: schedules_router.ScheduleUpdate
    ) -> schedules_router.ScheduleOut:
        """Update a live-window's days/times/enabled."""
        return await _with_session(
            schedules_router.update_schedule, schedule_id=schedule_id, payload=payload
        )

    @mcp.tool()
    async def schedule_delete(schedule_id: int) -> dict:
        """Delete a live-window (the instance becomes fully manual again)."""
        await _with_session(schedules_router.delete_schedule, schedule_id=schedule_id)
        return {"ok": True}

    # ---------------- backup ---------------- #
    @mcp.tool()
    async def backup_export() -> dict:
        """The full config bundle (nodes, instances, schedules, models, settings — secrets
        stay Fernet-encrypted). Same format the S3 backups store."""
        from .services import backup as backup_svc

        return await backup_svc.build_bundle()

    @mcp.tool()
    async def backup_import(bundle: dict) -> dict:
        """DESTRUCTIVE: restore a bundle — replaces ALL config tables (history untouched).
        Confirm with the operator before restoring over live config."""
        return await _no_session(backup_router.import_bundle, bundle=bundle)

    @mcp.tool()
    async def backup_run_now() -> dict:
        """Build + upload a backup to the configured S3 target immediately."""
        return await _no_session(backup_router.run_now)

    @mcp.tool()
    async def backup_list_s3() -> list[dict]:
        """Backups in the S3 target, newest first."""
        return await _no_session(backup_router.list_s3_backups)

    @mcp.tool()
    async def backup_restore_s3(key: str) -> dict:
        """DESTRUCTIVE: fetch a backup from S3 and restore it (replaces all config).
        Confirm with the operator first."""
        return await _no_session(backup_router.restore_s3, payload={"key": key})

    @mcp.tool()
    async def backup_status() -> dict:
        """Scheduled-backup runner state (last success, last key, last error)."""
        return await _no_session(backup_router.backup_status)

    # ---------------- storage ---------------- #
    @mcp.tool()
    async def storage_report() -> list[dict]:
        """Per-node models-disk breakdown: registry model sizes, ORPHAN directories
        (unreferenced by the registry), HF cache size, disk totals. Live SSH scan."""
        return await _with_session(storage_router.get_storage)

    @mcp.tool()
    async def storage_delete_orphan(node_id: int, name: str) -> JobAccepted:
        """DESTRUCTIVE: delete an orphaned directory in the models dir. Registered model
        names are refused (use model_delete_files for those). Confirm with the operator."""
        return await _no_session(
            storage_router.delete_orphan, payload={"node_id": node_id, "name": name}
        )

    @mcp.tool()
    async def storage_clear_hf_cache(node_ids: list[int] | None = None) -> JobAccepted:
        """Clear the HuggingFace download cache on the given nodes (all when omitted).
        Cache only — model files are untouched."""
        payload = {"node_ids": node_ids} if node_ids is not None else {}
        return await _no_session(storage_router.clear_hf_cache, payload=payload)

    # ---------------- image updates ---------------- #
    @mcp.tool()
    async def image_tags(image: str | None = None) -> dict:
        """Registry tags for the cluster vLLM image (or an explicit image ref), newest first."""
        return await _with_session(cluster_router.image_tags, image=image)

    @mcp.tool()
    async def image_update(
        image: str, restart_ray: bool = True, restart_instances: bool = True
    ) -> JobAccepted:
        """DESTRUCTIVE-ish: pull a new vLLM image on every node, persist it, optionally
        restart Ray and rolling-restart running instances (brief downtime each).
        Confirm with the operator before restarting serving instances."""
        return await _no_session(
            cluster_router.image_update,
            payload=ImageUpdateIn(
                image=image, restart_ray=restart_ray, restart_instances=restart_instances
            ),
        )

    # ------------------------------------------------------------------ #
    # Resources (read-only mirrors of the heavy-read surfaces)
    # ------------------------------------------------------------------ #
    @mcp.resource("spark://status", mime_type="application/json")
    async def res_status() -> str:
        """Live cluster status snapshot."""
        async with SessionLocal() as session:
            return _json(await telemetry.engine.compose_snapshot(session))

    @mcp.resource("spark://instances", mime_type="application/json")
    async def res_instances() -> str:
        """All serving instances."""
        return _json(await _with_session(instances_router.list_instances))

    @mcp.resource("spark://instances/{instance_id}", mime_type="application/json")
    async def res_instance(instance_id: str) -> str:
        """A single instance by id."""
        return _json(
            await _with_session(instances_router.get_instance, instance_id=int(instance_id))
        )

    @mcp.resource("spark://models", mime_type="application/json")
    async def res_models() -> str:
        """The model registry."""
        return _json(await _with_session(models_router.list_models))

    @mcp.resource("spark://models/{model_id}", mime_type="application/json")
    async def res_model(model_id: str) -> str:
        """A single registry model by id."""
        return _json(await _with_session(models_router.get_model, model_id=int(model_id)))

    @mcp.resource("spark://nodes", mime_type="application/json")
    async def res_nodes() -> str:
        """All cluster nodes."""
        return _json(await _with_session(nodes_router.list_nodes))

    @mcp.resource("spark://nodes/{node_id}", mime_type="application/json")
    async def res_node(node_id: str) -> str:
        """A single node by id."""
        return _json(await _with_session(nodes_router.get_node, node_id=int(node_id)))

    return mcp
