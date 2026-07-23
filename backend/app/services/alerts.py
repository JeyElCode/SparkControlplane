"""Alerting: threshold rules evaluated over the telemetry engine's caches.

The manager ticks every few seconds, derives *facts* from the cached samples,
and runs a small state machine per (rule, subject): a condition must hold for
its ``sustain`` duration before the alert **fires** (so a reboot or a blip
doesn't page), and a recovery notification is sent when it clears. Fired and
resolved alerts are persisted to the ``alerts`` table; notifications go to an
optional webhook (ntfy / Discord / Slack / generic JSON POST).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from .. import db as _db
from ..crypto import decrypt
from ..models import INST_RUNNING, Alert
from .telemetry import engine as telemetry

log = logging.getLogger("spark.alerts")

DEFAULTS: dict = {
    "node_offline_seconds": 60,
    "instance_unhealthy_seconds": 120,
    "gpu_temp_c": 85,
    "gpu_temp_seconds": 120,
    "disk_free_pct": 10,
    "disk_seconds": 300,
    "kv_cache_pct": 95,
    "kv_cache_seconds": 120,
    "qsfp_down_seconds": 60,
    "gpu_throttle_seconds": 60,
    "xid_window_seconds": 600,
    "webhook_kind": "generic",  # generic | ntfy | discord | slack
}

EVAL_SECONDS = 5.0


def merged_config(raw_json: str | None) -> dict:
    cfg = dict(DEFAULTS)
    if raw_json:
        try:
            data = json.loads(raw_json)
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
        except (ValueError, TypeError):
            pass
    return cfg


@dataclass
class Fact:
    """One rule-condition observation for one subject."""

    rule: str
    subject: str
    active: bool           # condition currently true
    sustain: float         # seconds it must hold before firing
    severity: str
    message: str


@dataclass
class _State:
    since: float | None = None   # condition first observed true
    fired: bool = False
    alert_id: int | None = None
    message: str = ""
    severity: str = "warn"


def gather_facts(cfg: dict) -> list[Fact]:
    """Derive rule conditions from the telemetry caches. Read-only."""
    facts: list[Fact] = []
    now_names = telemetry._node_names

    for nid, s in telemetry._samples.items():
        name = now_names.get(nid, str(nid))
        facts.append(Fact(
            "node_offline", name, not s.reachable, cfg["node_offline_seconds"], "crit",
            f"Node {name} is unreachable over SSH.",
        ))
        if not s.reachable:
            continue
        for g in s.gpus:
            temp = g.temp_c
            facts.append(Fact(
                "gpu_temp", f"{name}/gpu{g.index}",
                temp is not None and temp >= cfg["gpu_temp_c"], cfg["gpu_temp_seconds"], "warn",
                f"GPU{g.index} on {name} is at {temp}°C (threshold {cfg['gpu_temp_c']}°C).",
            ))
        if s.disk and s.disk.total_bytes and s.disk.free_bytes is not None:
            free_pct = 100.0 * s.disk.free_bytes / s.disk.total_bytes
            facts.append(Fact(
                "disk_low", name, free_pct < cfg["disk_free_pct"], cfg["disk_seconds"], "warn",
                f"Models disk on {name} is {free_pct:.0f}% free "
                f"(threshold {cfg['disk_free_pct']}%).",
            ))
        facts.append(Fact(
            "gpu_throttle", name, s.gpu_throttle is True, cfg["gpu_throttle_seconds"], "warn",
            f"GPU on {name} is thermal-throttling — performance is degraded.",
        ))
        # XID errors are events, not conditions: alert while one occurred within
        # the window (fires immediately), auto-resolving after a quiet spell.
        ring = telemetry._xids.get(nid)
        last = max((e.ts for e in ring), default=0.0) if ring else 0.0
        recent = last > time.time() - cfg["xid_window_seconds"]
        last_ev = ring[-1] if ring else None
        facts.append(Fact(
            "gpu_xid", name, recent, 0, "crit",
            f"GPU XID error on {name}"
            + (f" (Xid {last_ev.xid}): {last_ev.message[:120]}" if last_ev else "")
            + " — check dmesg; the GPU/driver may be in a bad state.",
        ))

    if telemetry._slow.qsfp_ok is not None:
        facts.append(Fact(
            "qsfp_down", "cluster", telemetry._slow.qsfp_ok is False,
            cfg["qsfp_down_seconds"], "crit",
            "QSFP fabric check failed: the head cannot reach every worker.",
        ))

    for st in telemetry._slow.instances:
        if st.status != INST_RUNNING:
            continue
        facts.append(Fact(
            "instance_unhealthy", st.name, st.health_ok is False,
            cfg["instance_unhealthy_seconds"], "crit",
            f"Instance {st.name} is running but /health is failing.",
        ))
        m = telemetry._inst_metrics.get(st.instance_id)
        kv = m.kv_cache_pct if m else None
        facts.append(Fact(
            "kv_cache_full", st.name,
            kv is not None and kv >= cfg["kv_cache_pct"], cfg["kv_cache_seconds"], "warn",
            f"Instance {st.name} KV cache is at {kv:.0f}% — likely overloaded "
            f"(requests will queue).",
        ))
    return facts


@dataclass
class Transition:
    event: str  # fired | resolved
    fact: Fact


class AlertManager:
    def __init__(self) -> None:
        self._states: dict[tuple[str, str], _State] = {}
        self._task: asyncio.Task | None = None
        self._stopping = False

    # --- pure-ish core (testable) ---------------------------------------
    def evaluate(self, facts: list[Fact], now: float) -> list[Transition]:
        """Advance the per-(rule,subject) state machines; return transitions."""
        out: list[Transition] = []
        seen: set[tuple[str, str]] = set()
        for f in facts:
            key = (f.rule, f.subject)
            seen.add(key)
            st = self._states.setdefault(key, _State())
            if f.active:
                if st.since is None:
                    st.since = now
                if not st.fired and now - st.since >= f.sustain:
                    st.fired = True
                    st.message = f.message
                    st.severity = f.severity
                    out.append(Transition("fired", f))
            else:
                if st.fired:
                    out.append(Transition("resolved", f))
                self._states[key] = _State()
        # subjects that vanished (node/instance deleted): resolve silently
        for key in list(self._states.keys() - seen):
            del self._states[key]
        return out

    def active(self) -> list[dict]:
        """Currently-firing alerts, for the status snapshot banners."""
        out = []
        for (rule, subject), st in self._states.items():
            if st.fired:
                out.append({
                    "rule": rule, "subject": subject, "message": st.message,
                    "severity": st.severity, "since": st.since, "alert_id": st.alert_id,
                })
        return sorted(out, key=lambda a: a["since"] or 0)

    # --- lifecycle -------------------------------------------------------
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
            started = time.time()
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("alert tick failed")
            await asyncio.sleep(max(1.0, EVAL_SECONDS - (time.time() - started)))

    async def _tick(self) -> None:
        async with _db.SessionLocal() as session:
            setting = await _db.get_setting(session)
            cfg = merged_config(setting.alerts_json)
            webhook_url = decrypt(setting.alert_webhook_url_enc)
            transitions = self.evaluate(gather_facts(cfg), time.time())
            if not transitions:
                return
            for t in transitions:
                key = (t.fact.rule, t.fact.subject)
                if t.event == "fired":
                    row = Alert(rule=t.fact.rule, subject=t.fact.subject,
                                severity=t.fact.severity, message=t.fact.message)
                    session.add(row)
                    await session.flush()
                    if key in self._states:
                        self._states[key].alert_id = row.id
                    log.warning("ALERT fired: %s", t.fact.message)
                else:
                    res = await session.execute(
                        select(Alert).where(
                            Alert.rule == t.fact.rule, Alert.subject == t.fact.subject,
                            Alert.resolved_at.is_(None),
                        ).order_by(Alert.id.desc()).limit(1)
                    )
                    row = res.scalar_one_or_none()
                    if row is not None:
                        row.resolved_at = datetime.now(timezone.utc)
                    log.warning("alert resolved: %s/%s", t.fact.rule, t.fact.subject)
            await session.commit()
        if webhook_url:
            for t in transitions:
                await send_webhook(webhook_url, cfg.get("webhook_kind", "generic"),
                                   t.event, t.fact)


def build_webhook_request(kind: str, event: str, fact: Fact) -> tuple[dict | None, str | None, dict]:
    """(json_body, text_body, headers) for the configured webhook flavor."""
    icon = "✅" if event == "resolved" else ("🚨" if fact.severity == "crit" else "⚠️")
    text = f"{icon} [{event.upper()}] {fact.rule} — {fact.subject}: {fact.message}"
    if kind == "ntfy":
        return None, text, {
            "Title": f"Spark Control Plane: {fact.rule} {event}",
            "Priority": "high" if (fact.severity == "crit" and event == "fired") else "default",
            "Tags": "rotating_light" if event == "fired" else "white_check_mark",
        }
    if kind == "discord":
        return {"content": text}, None, {}
    if kind == "slack":
        return {"text": text}, None, {}
    return {
        "source": "spark-controlplane", "event": event, "rule": fact.rule,
        "subject": fact.subject, "severity": fact.severity, "message": fact.message,
        "ts": datetime.now(timezone.utc).isoformat(),
    }, None, {}


async def send_webhook(url: str, kind: str, event: str, fact: Fact) -> str | None:
    """POST the notification; returns an error string or None. Never raises."""
    body_json, body_text, headers = build_webhook_request(kind, event, fact)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            if body_json is not None:
                r = await client.post(url, json=body_json, headers=headers)
            else:
                r = await client.post(url, content=body_text, headers=headers)
            if r.status_code >= 300:
                err = f"webhook HTTP {r.status_code}: {r.text[:200]}"
                log.warning(err)
                return err
        return None
    except httpx.HTTPError as exc:
        log.warning("webhook send failed: %s", exc)
        return f"webhook send failed: {exc}"


manager = AlertManager()
