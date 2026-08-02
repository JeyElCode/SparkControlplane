"""Keeping node certificates alive, and shouting long before they are not.

Two jobs, and the second one is the important one.

**Renew** — only possible when the source is `openbao`. The portal asks the
node for a CSR, has OpenBao sign it, installs the result and reloads the
sidecars. Nothing restarts; a reload of an nginx that is not holding the model
costs nothing, which is the entire reason short-lived certificates are
affordable here.

**Warn** — runs in every mode, and is the only thing standing between an
operator and a 03:00 outage. A certificate that lapses takes every endpoint
down at once, and the cluster proxy will report it as an upstream TLS error
with no hint that a date was the cause. In `manual` mode there is no automatic
recovery at all, so the warning IS the mechanism: it has to fire early enough
that a human can get a CSR signed by whatever queue their CA lives behind,
which can be days.

The loop deliberately does nothing when the source is `none`. That is the
default, so an upgrade neither renews nor warns about anything until an
operator opts in.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from .. import db as _db
from ..crypto import decrypt
from ..models import Node
from . import nodecert, pki

log = logging.getLogger("spark.certrenew")

# How often to look. Renewal windows are measured in hours at minimum (the
# 6h floor gives a 2h window), so a 15-minute tick has many chances inside
# even the tightest permitted schedule.
TICK_SECONDS = 900


class CertRenewer:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stopping = False
        self.last_run_ts: float | None = None
        self.last_error: str | None = None
        # Published for the alert engine, which reads module-level caches
        # rather than taking a DB session — same shape as telemetry._samples.
        self.expiring: dict[int, float] = {}     # node id -> hours remaining

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
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a bad tick must not kill the loop
                log.exception("certificate renewal tick failed")
            await asyncio.sleep(TICK_SECONDS)

    async def tick(self) -> dict:
        """One pass. Returns a summary, so a test or an API can call it
        directly rather than waiting for the loop."""
        import time

        summary = {"checked": 0, "renewed": [], "failed": [], "expiring": {}}
        async with _db.SessionLocal() as session:
            setting = await _db.get_setting(session)
            source = getattr(setting, "node_cert_source", "none") or "none"
            if source == "none":
                self.expiring = {}
                return summary

            ttl_hours = getattr(setting, "node_cert_ttl_hours", None)
            nodes = list((await session.execute(select(Node))).scalars().all())
            for node in nodes:
                if not node.fqdn:
                    continue
                summary["checked"] += 1
                hours_left = _hours_remaining(node)
                if hours_left is not None:
                    summary["expiring"][node.id] = hours_left

                if not nodecert.renewal_due(node, ttl_hours):
                    continue
                if source != "openbao":
                    # Manual: nothing to do but make sure the number above is
                    # published so the alert engine can act on it.
                    continue
                try:
                    await self._renew(session, setting, node, ttl_hours)
                    summary["renewed"].append(node.name)
                    node.tls_last_error = None
                except Exception as exc:  # noqa: BLE001 - one node must not stop the rest
                    msg = str(exc)[:500]
                    node.tls_last_error = msg
                    summary["failed"].append({"node": node.name, "error": msg})
                    # Deliberately not fatal and deliberately not silent. There
                    # is a whole retry window left; the alert fires from
                    # `expiring`, driven by time remaining rather than by this
                    # failure, so one bad tick does not page anyone and a
                    # persistently bad one does.
                    log.warning("certificate renewal failed for %s: %s", node.name, msg)
            await session.commit()

        self.expiring = summary["expiring"]
        self.last_run_ts = time.time()
        return summary

    async def _renew(self, session, setting, node: Node, ttl_hours) -> None:
        from ..config import get_settings
        from ..ssh import ssh_for_node

        token = decrypt(getattr(setting, "pki_token_enc", None))
        url = getattr(setting, "pki_url", None)
        role = getattr(setting, "pki_role", None)
        if not (url and role and token):
            raise RuntimeError(
                "OpenBao is selected as the certificate source but its URL, "
                "role or token is not configured."
            )

        install_dir = get_settings().node_install_dir
        ssh = await ssh_for_node(session, node)

        res = await ssh.run(nodecert.csr_command(install_dir, node.fqdn), check=True)
        csr = (res.stdout or "").strip()
        if "BEGIN CERTIFICATE REQUEST" not in csr:
            raise RuntimeError(
                f"{node.name} did not produce a certificate signing request. "
                "Is openssl installed on the node?"
            )

        signed = await pki.sign_csr(
            url=url, token=token, mount=getattr(setting, "pki_mount", "pki") or "pki",
            role=role, csr_pem=csr, fqdn=node.fqdn, ttl_hours=ttl_hours,
        )

        # Check what came back BEFORE writing it. A CA that quietly returns a
        # certificate for a different name, or one already expired, would
        # otherwise be installed and only surface as an upstream TLS failure in
        # the cluster with nothing naming the cause.
        check = nodecert.check_certificate(signed.certificate, node)
        if not check.ok:
            raise RuntimeError(f"OpenBao returned an unusable certificate: {check.error}")

        ca_pem = signed.ca_chain or getattr(setting, "node_ca_pem", None)
        await nodecert.install(ssh, install_dir, signed.certificate, ca_pem)
        await nodecert.reload_sidecars(
            ssh, install_dir, await _sidecar_names(session, node)
        )

        info = check.info
        node.tls_cert_pem = signed.certificate
        node.tls_csr_pem = None
        node.tls_fingerprint = info.fingerprint_sha256 if info else None
        node.tls_not_after = _naive(info.not_after) if info else None
        node.tls_issued_at = datetime.now(timezone.utc).replace(tzinfo=None)
        if signed.ca_chain and not getattr(setting, "node_ca_pem", None):
            # First success also publishes the CA, since the cluster Secret
            # cannot be generated without it.
            setting.node_ca_pem = signed.ca_chain
        log.info("renewed certificate for %s (expires %s)", node.name, node.tls_not_after)


async def _sidecar_names(session, node: Node) -> list[str]:
    """Instances whose TLS sidecar runs on this node and must re-read the cert."""
    from ..models import Instance

    rows = (await session.execute(select(Instance))).scalars().all()
    names = []
    for inst in rows:
        if not inst.tls_enabled:
            continue
        if inst.topology == "single" and inst.node_id != node.id:
            continue
        if inst.topology != "single" and node.role != "head":
            continue
        names.append(inst.name)
    return names


def _hours_remaining(node: Node) -> float | None:
    if node.tls_not_after is None:
        return None
    expiry = node.tls_not_after
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return (expiry - datetime.now(timezone.utc)).total_seconds() / 3600.0


def _naive(dt):
    return dt.replace(tzinfo=None) if dt is not None else None


renewer = CertRenewer()
