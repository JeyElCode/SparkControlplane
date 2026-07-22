"""Node power controls: graceful shutdown/reboot over SSH, Wake-on-LAN.

WoL strategy: the control plane usually runs inside a pod whose network may not
reach the nodes' L2 broadcast domain, so the magic packet is sent **via a
reachable peer node over SSH** (a dependency-free python3 one-liner) whenever
one exists — the surviving Spark wakes its neighbor. Direct UDP broadcast from
this process is the fallback for single-node/flat-network setups.
"""

from __future__ import annotations

import asyncio
import re
import shlex
import socket

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import SessionLocal
from ..models import INST_RUNNING, TOPO_SINGLE, Instance, Node
from ..ssh import ssh_for_node
from .jobs import JobHandle

MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")

WOL_PORT = 9


def normalize_mac(raw: str | None) -> str | None:
    """Lowercase colon-separated MAC, or None if it isn't one."""
    if not raw:
        return None
    mac = raw.strip().lower().replace("-", ":")
    return mac if MAC_RE.match(mac) else None


def build_magic_packet(mac: str) -> bytes:
    """6x 0xFF + 16 repetitions of the MAC."""
    norm = normalize_mac(mac)
    if norm is None:
        raise ValueError(f"Not a MAC address: {mac!r}")
    raw = bytes.fromhex(norm.replace(":", ""))
    return b"\xff" * 6 + raw * 16


def send_wol_udp(mac: str, host: str = "255.255.255.255", port: int = WOL_PORT) -> None:
    """Send the magic packet from this process (fallback path)."""
    pkt = build_magic_packet(mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(pkt, (host, port))


# python3 ships on DGX OS; no wakeonlan/etherwake package needed on the relay.
_RELAY_WOL_PY = (
    "import socket,sys\n"
    "mac=sys.argv[1].replace(':','')\n"
    "pkt=b'\\xff'*6+bytes.fromhex(mac)*16\n"
    "s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)\n"
    "s.setsockopt(socket.SOL_SOCKET,socket.SO_BROADCAST,1)\n"
    "s.sendto(pkt,('255.255.255.255',9))\n"
    "print('magic packet sent')\n"
)


async def capture_mac(session: AsyncSession, node: Node) -> str | None:
    """Read the default-route interface's MAC off the node and persist it."""
    try:
        ssh = await ssh_for_node(session, node)
        res = await ssh.run(
            'DEV=$(ip route show default 2>/dev/null | awk \'/default/ {print $5; exit}\'); '
            '[ -n "$DEV" ] && cat "/sys/class/net/$DEV/address"',
            timeout=15,
        )
        mac = normalize_mac(res.stdout.strip()) if res.ok else None
    except Exception:  # noqa: BLE001
        return None
    if mac and node.mac_address != mac:
        node.mac_address = mac
        await session.commit()
    return mac


async def affected_instances(session: AsyncSession, node: Node) -> list[str]:
    """Names of RUNNING instances that shutting this node down would kill:
    everything multi-node, plus singles pinned to it."""
    res = await session.execute(select(Instance).where(Instance.status == INST_RUNNING))
    out = []
    for inst in res.scalars():
        if inst.topology != TOPO_SINGLE or inst.node_id == node.id:
            out.append(inst.name)
    return out


async def shutdown_node(handle: JobHandle, node_id: int, reboot: bool = False) -> str:
    verb = "Rebooting" if reboot else "Shutting down"
    async with SessionLocal() as session:
        node = await session.get(Node, node_id)
        if node is None:
            raise RuntimeError("Node not found.")
        victims = await affected_instances(session, node)
        if victims:
            await handle.log(
                f"WARNING: running instance(s) affected by this: {', '.join(victims)}", "stderr"
            )
        # Best effort: remember the MAC while the node is still up, so Wake
        # works afterwards.
        mac = node.mac_address or await capture_mac(session, node)
        if not reboot and not mac:
            await handle.log(
                "WARNING: no MAC address captured — Wake-on-LAN will not be available "
                "for this node until you power it on manually and Test connection.",
                "stderr",
            )
        await handle.log(f"[{node.name}] {verb.lower()} via systemd…")
        ssh = await ssh_for_node(session, node)
        cmd = "systemctl reboot" if reboot else "systemctl poweroff"
        # The connection may drop before the command's exit status arrives —
        # that IS success here.
        try:
            await ssh.run(cmd, sudo=True, timeout=20)
        except Exception as exc:  # noqa: BLE001
            await handle.log(f"[{node.name}] connection closed ({exc}) — expected during {verb.lower()}.")
        from ..ssh import pool

        await pool.drop(node.id)
        noun = "Reboot" if reboot else "Shutdown"
        await handle.log(f"[{node.name}] {noun.lower()} initiated. ✅")
        return f"{noun} initiated on '{node.name}'"


async def wake_node(handle: JobHandle, node_id: int) -> str:
    async with SessionLocal() as session:
        node = await session.get(Node, node_id)
        if node is None:
            raise RuntimeError("Node not found.")
        mac = normalize_mac(node.mac_address)
        if mac is None:
            raise RuntimeError(
                f"No MAC address stored for '{node.name}'. Power it on manually once and "
                "run Test connection (which captures the MAC), or set it on the Nodes page."
            )
        others = list(
            (await session.execute(select(Node).where(Node.id != node.id))).scalars()
        )
        # Try known-reachable peers first (telemetry cache) so we don't burn a
        # connect timeout on a peer that is itself powered off.
        from .telemetry import engine as telemetry_engine

        others.sort(key=lambda p: telemetry_engine.node_reachable(p.id) is not True)

        # Preferred path: relay through any reachable peer node (same L2 domain).
        for peer in others:
            try:
                ssh = await ssh_for_node(session, peer)
                res = await ssh.run(
                    f"python3 -c {shlex.quote(_RELAY_WOL_PY)} {shlex.quote(mac)}", timeout=15
                )
                if res.ok:
                    await handle.log(
                        f"[{peer.name}] sent Wake-on-LAN magic packet for {node.name} ({mac}). ✅"
                    )
                    return f"Wake-on-LAN sent to '{node.name}' via {peer.name}"
                await handle.log(
                    f"[{peer.name}] relay failed: {res.stderr.strip() or res.stdout.strip()}",
                    "stderr",
                )
            except Exception as exc:  # noqa: BLE001
                await handle.log(f"[{peer.name}] unreachable for relay: {exc}", "stderr")

        # Fallback: direct UDP from this process (works when the control plane
        # shares the nodes' broadcast domain).
        await handle.log("No reachable peer node — sending the magic packet directly…")
        try:
            await asyncio.to_thread(send_wol_udp, mac)
            await asyncio.to_thread(send_wol_udp, mac, node.lan_ip)  # unicast attempt too
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Could not send the magic packet: {exc}")
        await handle.log(
            f"Magic packet sent directly for {node.name} ({mac}). If the node does not "
            "wake, the control plane's network may not reach its broadcast domain — "
            "keep at least one other node powered to use it as a relay."
        )
        return f"Wake-on-LAN sent to '{node.name}'"


async def batch_power(handle: JobHandle, action: str) -> str:
    """Fleet-wide shutdown or wake. Shutdown does workers first, then the head;
    wake targets every node with a known MAC (skipping already-online ones is
    the caller's UI concern — an extra magic packet is harmless)."""
    async with SessionLocal() as session:
        nodes = list(
            (await session.execute(select(Node).order_by(Node.role, Node.id))).scalars()
        )
    if not nodes:
        raise RuntimeError("No nodes configured.")
    if action == "shutdown":
        ordered = [n for n in nodes if n.role == "worker"] + [n for n in nodes if n.role == "head"]
        for n in ordered:
            try:
                await shutdown_node(handle, n.id)
            except Exception as exc:  # noqa: BLE001
                await handle.log(f"[{n.name}] shutdown failed: {exc}", "stderr")
        return f"Shutdown initiated on {len(ordered)} node(s)"
    if action == "wake":
        woken = 0
        for n in nodes:
            try:
                await wake_node(handle, n.id)
                woken += 1
            except Exception as exc:  # noqa: BLE001
                await handle.log(f"[{n.name}] wake failed: {exc}", "stderr")
        if woken == 0:
            raise RuntimeError("Could not wake any node (no MACs stored?).")
        return f"Wake-on-LAN sent to {woken} node(s)"
    raise RuntimeError(f"Unknown power action: {action}")
