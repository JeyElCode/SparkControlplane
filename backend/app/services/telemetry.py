"""Server-side telemetry engine.

Continuously samples every node in the background — ONE batched SSH command per
node per fast tick (GPU, CPU, memory, network counters, disk, uptime, GPU
processes, docker containers) — and keeps:

* a **latest sample** per node (dashboards read this from cache; an HTTP
  request never opens an SSH session),
* a **history ring** per node (~15 min of compact points for sparklines),
* a **slow cache** for the expensive checks (Ray status, QSFP reachability,
  per-instance systemd + /health probes) on their own cadence.

Rates (network B/s, CPU %) are derived from counter deltas between consecutive
ticks, so the loops keep running even while a node is offline — the first tick
after it returns re-seeds the baseline instead of reporting a bogus spike.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field

from sqlalchemy import select

from .. import db as _db
from ..config import get_settings
from ..db import get_cluster_config, get_setting
from ..models import Instance, Node
from ..schemas import (
    DiskUsage,
    GpuProc,
    GpuStatus,
    HistoryPoint,
    InstanceHistory,
    InstanceHistoryPoint,
    InstanceMetrics,
    NetRate,
    NodeHistory,
    NodeStatus,
    RayStatus,
    StatusSnapshot,
)
from ..ssh import ssh_for_node
from .paths import models_host_dir
from . import status_svc, templates

log = logging.getLogger("spark.telemetry")

_VIRTUAL_IFACE_PREFIXES = ("lo", "docker", "veth", "br-", "virbr", "tailscale", "cni", "flannel")


def _collector_script(models_dir: str) -> str:
    """The one batched script a fast tick runs on a node. Every section is
    best-effort (`|| true`) so a missing tool degrades that metric, not the
    whole sample."""
    import shlex

    qdir = shlex.quote(models_dir)
    return f"""
echo '@@gpu@@'
nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw --format=csv,noheader,nounits 2>/dev/null || true
echo '@@gpuproc@@'
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader,nounits 2>/dev/null || true
echo '@@cpu@@'
head -1 /proc/stat
echo '@@nproc@@'
nproc 2>/dev/null || true
echo '@@load@@'
cat /proc/loadavg
echo '@@mem@@'
grep -E '^(MemTotal|MemAvailable):' /proc/meminfo
echo '@@uptime@@'
cat /proc/uptime
echo '@@net@@'
cat /proc/net/dev
echo '@@defroute@@'
ip route show default 2>/dev/null | head -1
echo '@@disk@@'
df -k {qdir} 2>/dev/null | tail -1
echo '@@docker@@'
if docker ps --format '{{{{.Names}}}}' 2>/dev/null; then echo __docker_ok__; elif sudo -n docker ps --format '{{{{.Names}}}}' 2>/dev/null; then echo __docker_ok__; fi
"""


def _sections(raw: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in raw.splitlines():
        s = line.strip()
        if s.startswith("@@") and s.endswith("@@") and len(s) > 4:
            current = out.setdefault(s.strip("@"), [])
        elif current is not None and s:
            current.append(line.rstrip())
    return out


@dataclass
class CounterState:
    """Previous-tick raw counters for rate derivation."""

    ts: float = 0.0
    cpu_busy: int = 0
    cpu_total: int = 0
    net: dict[str, tuple[int, int]] = field(default_factory=dict)  # iface -> (rx, tx)


@dataclass
class NodeSample:
    """Everything one fast tick learned about a node."""

    node_id: int
    ts: float
    reachable: bool
    detail: str | None = None
    gpus: list[GpuStatus] = field(default_factory=list)
    gpu_procs: list[GpuProc] = field(default_factory=list)
    cpu_pct: float | None = None
    cpu_count: int | None = None
    loadavg_1m: float | None = None
    mem_used_mib: int | None = None
    mem_total_mib: int | None = None
    uptime_seconds: float | None = None
    net: list[NetRate] = field(default_factory=list)
    disk: DiskUsage | None = None
    docker_ok: bool | None = None
    docker_names: list[str] = field(default_factory=list)
    ray_container_up: bool | None = None


def parse_sample(
    raw: str,
    *,
    node_id: int,
    ts: float,
    qsfp_iface: str,
    models_dir: str,
    prev: CounterState,
) -> tuple[NodeSample, CounterState]:
    """Parse a collector-script output into a sample + the next counter state.

    Pure and synchronous so it's trivially testable; ``prev`` with ``ts == 0``
    means "no baseline yet" and yields None rates for this tick.
    """
    sec = _sections(raw)
    s = NodeSample(node_id=node_id, ts=ts, reachable=True)
    nxt = CounterState(ts=ts)
    dt = ts - prev.ts if prev.ts else 0.0

    # GPUs (same CSV as the old status_svc query)
    for line in sec.get("gpu", []):
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7:
            continue
        s.gpus.append(
            GpuStatus(
                index=_int(parts[0]) or 0,
                name=parts[1] or None,
                mem_used_mib=_int(parts[2]),
                mem_total_mib=_int(parts[3]),
                util_pct=_int(parts[4]),
                temp_c=_int(parts[5]),
                power_w=_float(parts[6]),
            )
        )

    for line in sec.get("gpuproc", []):
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3 and (pid := _int(parts[0])) is not None:
            name = parts[1].rsplit("/", 1)[-1] or parts[1]
            s.gpu_procs.append(GpuProc(pid=pid, name=name, mem_mib=_int(parts[2])))
    s.gpu_procs.sort(key=lambda p: -(p.mem_mib or 0))

    # CPU: busy/total jiffy deltas vs the previous tick
    for line in sec.get("cpu", []):
        parts = line.split()
        if parts and parts[0] == "cpu":
            vals = [int(v) for v in parts[1:] if v.isdigit()]
            total = sum(vals)
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
            busy = total - idle
            nxt.cpu_busy, nxt.cpu_total = busy, total
            if prev.ts and total > prev.cpu_total:
                # clamp: counters can jump backwards on a reboot mid-window
                pct = 100.0 * (busy - prev.cpu_busy) / (total - prev.cpu_total)
                s.cpu_pct = round(min(100.0, max(0.0, pct)), 1)

    if sec.get("nproc"):
        s.cpu_count = _int(sec["nproc"][0])
    if sec.get("load"):
        s.loadavg_1m = _float(sec["load"][0].split()[0])
    if sec.get("uptime"):
        s.uptime_seconds = _float(sec["uptime"][0].split()[0])

    mem: dict[str, int] = {}
    for line in sec.get("mem", []):
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            mem[key.strip()] = int(parts[0])  # kB
    if "MemTotal" in mem:
        s.mem_total_mib = mem["MemTotal"] // 1024
        if "MemAvailable" in mem:
            s.mem_used_mib = (mem["MemTotal"] - mem["MemAvailable"]) // 1024

    # Network: /proc/net/dev counters -> B/s vs previous tick
    default_iface = None
    if sec.get("defroute"):
        parts = sec["defroute"][0].split()
        if "dev" in parts:
            default_iface = parts[parts.index("dev") + 1]
    for line in sec.get("net", []):
        if ":" not in line:
            continue
        iface, _, rest = line.partition(":")
        iface = iface.strip()
        if iface.startswith(_VIRTUAL_IFACE_PREFIXES):
            continue
        vals = rest.split()
        if len(vals) < 9:
            continue
        rx, tx = int(vals[0]), int(vals[8])
        nxt.net[iface] = (rx, tx)
        kind = "qsfp" if iface == qsfp_iface else ("lan" if iface == default_iface else "other")
        rate = NetRate(iface=iface, kind=kind)
        if prev.ts and iface in prev.net and dt > 0:
            prx, ptx = prev.net[iface]
            if rx >= prx and tx >= ptx:  # counter reset -> skip this tick
                rate.rx_bps = round((rx - prx) / dt, 1)
                rate.tx_bps = round((tx - ptx) / dt, 1)
        s.net.append(rate)
    s.net.sort(key=lambda r: {"qsfp": 0, "lan": 1}.get(r.kind, 2))

    for line in sec.get("disk", []):
        vals = line.split()
        # df -k: Filesystem 1K-blocks Used Available Use% Mounted
        if len(vals) >= 4 and vals[1].isdigit():
            s.disk = DiskUsage(
                path=models_dir,
                total_bytes=int(vals[1]) * 1024,
                used_bytes=int(vals[2]) * 1024,
                free_bytes=int(vals[3]) * 1024,
            )

    if "docker" in sec:
        lines = [n.strip() for n in sec["docker"] if n.strip()]
        s.docker_ok = "__docker_ok__" in lines
        s.docker_names = [n for n in lines if n != "__docker_ok__"]

    return s, nxt


def history_point(s: NodeSample) -> HistoryPoint:
    qsfp = next((r for r in s.net if r.kind == "qsfp"), None)
    lan = next((r for r in s.net if r.kind == "lan"), None)
    return HistoryPoint(
        ts=s.ts,
        cpu_pct=s.cpu_pct,
        mem_used_mib=s.mem_used_mib,
        gpu_util_pct=max((g.util_pct or 0 for g in s.gpus), default=None) if s.gpus else None,
        gpu_mem_used_mib=sum(g.mem_used_mib or 0 for g in s.gpus) if s.gpus else None,
        qsfp_rx_bps=qsfp.rx_bps if qsfp else None,
        qsfp_tx_bps=qsfp.tx_bps if qsfp else None,
        lan_rx_bps=lan.rx_bps if lan else None,
        lan_tx_bps=lan.tx_bps if lan else None,
        disk_used_bytes=s.disk.used_bytes if s.disk else None,
    )


# --- vLLM Prometheus metrics ---------------------------------------------
def parse_prometheus(text: str) -> dict[str, float]:
    """Minimal Prometheus text-format parser: sums samples across label sets
    per metric name (a vLLM instance serves one model, so label collapse is
    safe). Ignores comments, malformed lines, and NaN."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # "name{labels} value [ts]"  or  "name value [ts]"
        brace = line.find("{")
        if brace != -1:
            end = line.rfind("}")
            if end == -1:
                continue
            name = line[:brace]
            rest = line[end + 1:].split()
        else:
            parts = line.split()
            if len(parts) < 2:
                continue
            name = parts[0]
            rest = parts[1:]
        try:
            val = float(rest[0])
        except (ValueError, IndexError):
            continue
        if val != val:  # NaN
            continue
        out[name] = out.get(name, 0.0) + val
    return out


def _first(parsed: dict[str, float], *names: str) -> float | None:
    for n in names:
        if n in parsed:
            return parsed[n]
    return None


def derive_instance_metrics(
    parsed: dict[str, float], prev: dict[str, float], ts: float
) -> tuple["InstanceMetrics", dict[str, float]]:
    """Turn one /metrics scrape into gauges + windowed rates vs the previous
    scrape. ``prev`` holds the prior raw counters (with key "_ts"); pass ``{}``
    for the first scrape (rates come out None). Pure for testability."""
    from ..schemas import InstanceMetrics

    m = InstanceMetrics(ts=ts)
    m.running = _i(_first(parsed, "vllm:num_requests_running"))
    m.waiting = _i(_first(parsed, "vllm:num_requests_waiting"))
    kv = _first(parsed, "vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc")
    m.kv_cache_pct = round(kv * 100.0, 1) if kv is not None else None

    nxt: dict[str, float] = {"_ts": ts}
    prev_ts = prev.get("_ts")
    dt = ts - prev_ts if prev_ts else 0.0

    def rate(name: str) -> float | None:
        cur = parsed.get(name)
        if cur is None:
            return None
        nxt[name] = cur
        old = prev.get(name)
        # counter reset (instance restart) -> no rate this window
        if old is None or dt <= 0 or cur < old:
            return None
        return (cur - old) / dt

    p_tps = rate("vllm:prompt_tokens_total")
    g_tps = rate("vllm:generation_tokens_total")
    r_ps = rate("vllm:request_success_total")
    m.prompt_tps = round(p_tps, 1) if p_tps is not None else None
    m.gen_tps = round(g_tps, 1) if g_tps is not None else None
    m.req_per_s = round(r_ps, 2) if r_ps is not None else None
    m.total_generation_tokens = parsed.get("vllm:generation_tokens_total")

    def hist_avg_ms(base: str) -> float | None:
        """Mean over the last window from a histogram's _sum/_count deltas."""
        s, c = parsed.get(f"{base}_sum"), parsed.get(f"{base}_count")
        if s is None or c is None:
            return None
        nxt[f"{base}_sum"], nxt[f"{base}_count"] = s, c
        os_, oc = prev.get(f"{base}_sum"), prev.get(f"{base}_count")
        if os_ is None or oc is None or c <= oc or s < os_:
            return None
        return round(1000.0 * (s - os_) / (c - oc), 1)

    m.ttft_ms = hist_avg_ms("vllm:time_to_first_token_seconds")
    m.e2e_ms = hist_avg_ms("vllm:e2e_request_latency_seconds")
    return m, nxt


def _i(v: float | None) -> int | None:
    return int(v) if v is not None else None


@dataclass
class SlowCache:
    ts: float = 0.0
    ray: RayStatus = field(default_factory=lambda: RayStatus(reachable=False))
    qsfp_ok: bool | None = None
    instances: list = field(default_factory=list)


class TelemetryEngine:
    """Owns the sampler tasks and the caches. One instance per process."""

    def __init__(self) -> None:
        self._samples: dict[int, NodeSample] = {}
        self._counters: dict[int, CounterState] = {}
        self._history: dict[int, deque[HistoryPoint]] = {}
        self._node_names: dict[int, str] = {}
        self._slow = SlowCache()
        self._inst_metrics: dict[int, InstanceMetrics] = {}
        self._inst_counters: dict[int, dict[str, float]] = {}
        self._inst_history: dict[int, deque[InstanceHistoryPoint]] = {}
        self._inst_names: dict[int, str] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._manager: asyncio.Task | None = None
        self._slow_task: asyncio.Task | None = None
        self._vllm_task: asyncio.Task | None = None
        self._stopping = False

    # --- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._manager is None:
            self._stopping = False
            self._manager = asyncio.create_task(self._manage_loop())
            self._slow_task = asyncio.create_task(self._slow_loop())
            self._vllm_task = asyncio.create_task(self._vllm_metrics_loop())

    async def stop(self) -> None:
        self._stopping = True
        tasks = [
            t
            for t in (self._manager, self._slow_task, self._vllm_task, *self._tasks.values())
            if t
        ]
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except BaseException:  # noqa: BLE001 - cancellation cleanup
                pass
        self._manager = None
        self._slow_task = None
        self._vllm_task = None
        self._tasks.clear()

    # --- loops -----------------------------------------------------------
    async def _manage_loop(self) -> None:
        """Reconcile one sampler task per configured node (nodes can be added
        or removed at runtime)."""
        while not self._stopping:
            try:
                async with _db.SessionLocal() as session:
                    rows = (await session.execute(select(Node.id, Node.name))).all()
                ids = {r[0] for r in rows}
                self._node_names = {r[0]: r[1] for r in rows}
                for nid in ids - self._tasks.keys():
                    self._tasks[nid] = asyncio.create_task(self._node_loop(nid))
                for nid in list(self._tasks.keys() - ids):
                    self._tasks.pop(nid).cancel()
                    self._samples.pop(nid, None)
                    self._counters.pop(nid, None)
                    self._history.pop(nid, None)
            except Exception:  # noqa: BLE001
                log.exception("telemetry manager tick failed")
            await asyncio.sleep(5)

    async def _node_loop(self, node_id: int) -> None:
        settings = get_settings()
        interval = max(1.0, settings.telemetry_fast_seconds)
        maxlen = max(10, int(settings.telemetry_history_minutes * 60 / interval))
        ring = self._history.setdefault(node_id, deque(maxlen=maxlen))
        while not self._stopping:
            started = time.time()
            try:
                sample = await self._sample_node(node_id)
                if sample is None:  # node vanished
                    return
                self._samples[node_id] = sample
                if sample.reachable:
                    ring.append(history_point(sample))
                else:
                    # offline: drop the rate baseline so the first tick after
                    # recovery doesn't compute rates across the outage
                    self._counters.pop(node_id, None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._samples[node_id] = NodeSample(
                    node_id=node_id, ts=started, reachable=False, detail=str(exc)
                )
                self._counters.pop(node_id, None)
            await asyncio.sleep(max(0.5, interval - (time.time() - started)))

    async def _sample_node(self, node_id: int) -> NodeSample | None:
        async with _db.SessionLocal() as session:
            node = await session.get(Node, node_id)
            if node is None:
                return None
            cfg = await get_cluster_config(session)
            models_dir = models_host_dir(node, cfg)
            qsfp_iface = node.qsfp_iface
            role = node.role
            try:
                ssh = await ssh_for_node(session, node)
            except Exception as exc:  # noqa: BLE001
                return NodeSample(
                    node_id=node_id, ts=time.time(), reachable=False, detail=str(exc)
                )
        ts = time.time()
        res = await ssh.run(_collector_script(models_dir), timeout=25)
        if not res.ok:
            return NodeSample(
                node_id=node_id, ts=ts, reachable=False,
                detail=(res.stderr or res.stdout or "collector failed").strip()[:300],
            )
        prev = self._counters.get(node_id, CounterState())
        sample, nxt = parse_sample(
            res.stdout, node_id=node_id, ts=ts, qsfp_iface=qsfp_iface,
            models_dir=models_dir, prev=prev,
        )
        self._counters[node_id] = nxt
        # ray container presence is role-dependent; annotate here
        container = (
            templates.RAY_HEAD_CONTAINER if role == "head" else templates.RAY_WORKER_CONTAINER
        )
        if sample.docker_ok:
            sample.ray_container_up = container in sample.docker_names
        return sample

    async def _slow_loop(self) -> None:
        settings = get_settings()
        interval = max(3.0, settings.telemetry_slow_seconds)
        while not self._stopping:
            started = time.time()
            try:
                async with _db.SessionLocal() as session:
                    nodes = list((await session.execute(select(Node))).scalars().all())
                    head = next((n for n in nodes if n.role == "head"), None)
                    workers = [n for n in nodes if n.role == "worker"]
                    from sqlalchemy.orm import selectinload

                    instances = list(
                        (
                            await session.execute(
                                select(Instance).options(
                                    selectinload(Instance.model), selectinload(Instance.node)
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    qsfp_ok = await status_svc._qsfp_ok(session, head, workers)
                    ray = await status_svc._ray_status(session, head)
                    inst_statuses = await asyncio.gather(
                        *[status_svc._instance_status(session, i, head) for i in instances]
                    )
                self._slow = SlowCache(
                    ts=time.time(), ray=ray, qsfp_ok=qsfp_ok, instances=list(inst_statuses)
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("telemetry slow tick failed")
            await asyncio.sleep(max(1.0, interval - (time.time() - started)))

    async def _vllm_metrics_loop(self) -> None:
        """Scrape every RUNNING instance's Prometheus /metrics on the fast
        cadence (cheap HTTP; no SSH involved)."""
        import httpx

        from ..models import INST_RUNNING

        settings = get_settings()
        interval = max(1.0, settings.telemetry_fast_seconds)
        maxlen = max(10, int(settings.telemetry_history_minutes * 60 / interval))
        while not self._stopping:
            started = time.time()
            try:
                async with _db.SessionLocal() as session:
                    from sqlalchemy.orm import selectinload

                    instances = list(
                        (
                            await session.execute(
                                select(Instance)
                                .where(Instance.status == INST_RUNNING)
                                .options(selectinload(Instance.node))
                            )
                        )
                        .scalars()
                        .all()
                    )
                    head = None
                    if any(i.topology != "single" for i in instances):
                        res = await session.execute(
                            select(Node).where(Node.role == "head").order_by(Node.id).limit(1)
                        )
                        head = res.scalar_one_or_none()

                live_ids = {i.id for i in instances}
                for gone in set(self._inst_metrics) - live_ids:
                    self._inst_metrics.pop(gone, None)
                    self._inst_counters.pop(gone, None)
                self._inst_names.update({i.id: i.name for i in instances})

                async with httpx.AsyncClient(timeout=3) as client:
                    results = await asyncio.gather(
                        *[self._scrape_instance(client, inst, head) for inst in instances],
                        return_exceptions=True,
                    )
                for inst, result in zip(instances, results):
                    if isinstance(result, InstanceMetrics):
                        # Latency averages only exist for windows where requests
                        # finished; carry the last measurement through idle
                        # windows so the UI doesn't flicker to "—".
                        old = self._inst_metrics.get(inst.id)
                        if old is not None:
                            if result.ttft_ms is None:
                                result.ttft_ms = old.ttft_ms
                            if result.e2e_ms is None:
                                result.e2e_ms = old.e2e_ms
                        self._inst_metrics[inst.id] = result
                        ring = self._inst_history.setdefault(inst.id, deque(maxlen=maxlen))
                        ring.append(
                            InstanceHistoryPoint(
                                ts=result.ts,
                                gen_tps=result.gen_tps,
                                prompt_tps=result.prompt_tps,
                                running=result.running,
                                waiting=result.waiting,
                                kv_cache_pct=result.kv_cache_pct,
                                ttft_ms=result.ttft_ms,
                            )
                        )
                    else:
                        # unreachable: drop the counter baseline so rates
                        # aren't computed across the gap after recovery
                        self._inst_metrics.pop(inst.id, None)
                        self._inst_counters.pop(inst.id, None)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("vLLM metrics tick failed")
            await asyncio.sleep(max(0.5, interval - (time.time() - started)))

    async def _scrape_instance(self, client, inst: Instance, head) -> InstanceMetrics:
        base = status_svc.instance_base_url(inst, head)
        if base is None:
            raise RuntimeError("no API node")
        url, verify = base
        # httpx clients pin verify at construction; TLS instances are rare
        # enough that a one-off client per scrape is fine.
        if not verify:
            import httpx

            async with httpx.AsyncClient(timeout=3, verify=False) as c:
                resp = await c.get(f"{url}/metrics", headers=status_svc.instance_auth_headers(inst))
        else:
            resp = await client.get(f"{url}/metrics", headers=status_svc.instance_auth_headers(inst))
        resp.raise_for_status()
        parsed = parse_prometheus(resp.text)
        prev = self._inst_counters.get(inst.id, {})
        metrics, nxt = derive_instance_metrics(parsed, prev, time.time())
        self._inst_counters[inst.id] = nxt
        return metrics

    # --- read side -------------------------------------------------------
    def node_reachable(self, node_id: int) -> bool | None:
        """Last sampled reachability, or None if never sampled."""
        s = self._samples.get(node_id)
        return None if s is None else s.reachable

    def node_status(self, node) -> NodeStatus:
        """Build the API NodeStatus for a node from the cached sample."""
        s = self._samples.get(node.id)
        st = NodeStatus(
            node_id=node.id, role=node.role, name=node.name,
            reachable=bool(s and s.reachable),
        )
        if s is None:
            st.detail = "Collecting first sample…"
            return st
        st.sampled_at = s.ts
        if not s.reachable:
            st.detail = s.detail or "Unreachable over SSH."
            return st
        st.gpus = s.gpus
        st.gpu_procs = s.gpu_procs
        st.cpu_pct = s.cpu_pct
        st.cpu_count = s.cpu_count
        st.loadavg_1m = s.loadavg_1m
        st.uptime_seconds = s.uptime_seconds
        st.sys_mem_used_mib = s.mem_used_mib
        st.sys_mem_total_mib = s.mem_total_mib
        st.net = s.net
        st.disk = s.disk
        st.docker_ok = s.docker_ok
        st.ray_container_up = s.ray_container_up
        return st

    async def compose_snapshot(self, session) -> StatusSnapshot:
        """StatusSnapshot from caches — no SSH on the request path."""
        from datetime import datetime, timezone

        settings = get_settings()
        setting = await get_setting(session)
        nodes = list((await session.execute(select(Node))).scalars().all())
        instances = list(
            (await session.execute(select(Instance))).scalars().all()
        )
        node_statuses = [self.node_status(n) for n in nodes]
        warnings = status_svc._memory_warnings(
            nodes, instances, node_statuses, settings.node_memory_gib
        )
        # slow-cached instance statuses + fresh fast-cached vLLM metrics
        inst_statuses = [s.model_copy() for s in self._slow.instances]
        for s in inst_statuses:
            s.metrics = self._inst_metrics.get(s.instance_id)
        return StatusSnapshot(
            setup_complete=setting.setup_complete,
            qsfp_ok=self._slow.qsfp_ok,
            ray=self._slow.ray,
            nodes=node_statuses,
            instances=inst_statuses,
            overcommit_warnings=warnings,
            generated_at=datetime.now(timezone.utc),
        )

    def history(self, minutes: int | None = None) -> list[NodeHistory]:
        cutoff = time.time() - minutes * 60 if minutes else 0
        out = []
        for nid, ring in self._history.items():
            pts = [p for p in ring if p.ts >= cutoff]
            out.append(
                NodeHistory(node_id=nid, name=self._node_names.get(nid, str(nid)), points=pts)
            )
        return sorted(out, key=lambda h: h.node_id)

    def instance_history(self, minutes: int | None = None) -> list[InstanceHistory]:
        cutoff = time.time() - minutes * 60 if minutes else 0
        out = []
        for iid, ring in self._inst_history.items():
            pts = [p for p in ring if p.ts >= cutoff]
            out.append(
                InstanceHistory(
                    instance_id=iid, name=self._inst_names.get(iid, str(iid)), points=pts
                )
            )
        return sorted(out, key=lambda h: h.instance_id)


engine = TelemetryEngine()


def _int(s: str) -> int | None:
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def _float(s: str) -> float | None:
    try:
        return float(s)
    except (ValueError, TypeError):
        return None
