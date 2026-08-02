from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from datetime import datetime, timezone

from ..crypto import encrypt
from ..db import get_session, get_setting
from ..models import MAX_NODES, Node
from ..schemas import (
    ConnectionTest,
    InterfaceInfo,
    JobAccepted,
    NodeCertIn,
    NodeIn,
    NodeOut,
    NodeUpdate,
)
from ..services import cluster, nodeops
from ..services.jobs import jobs
from ..ssh import pool, ssh_for_node

log = logging.getLogger("spark.nodes")

router = APIRouter(prefix="/api/nodes", tags=["nodes"])


@router.get("", response_model=list[NodeOut])
async def list_nodes(session: AsyncSession = Depends(get_session)):
    # head first, then workers in creation order
    rows = (await session.execute(select(Node).order_by(Node.role, Node.id))).scalars().all()
    return [NodeOut.of(n) for n in rows]


@router.post("", response_model=NodeOut, status_code=201)
async def create_node(payload: NodeIn, session: AsyncSession = Depends(get_session)):
    existing = list((await session.execute(select(Node))).scalars().all())
    if payload.role == "head" and any(n.role == "head" for n in existing):
        raise HTTPException(409, "A head node already exists. Edit it instead.")
    if len(existing) >= MAX_NODES:
        raise HTTPException(409, f"At most {MAX_NODES} nodes are supported (1 head + {MAX_NODES - 1} workers).")
    dupe = next(
        (n for n in existing if n.name == payload.name or n.lan_ip == payload.lan_ip
         or n.qsfp_ip == payload.qsfp_ip),
        None,
    )
    if dupe is not None:
        raise HTTPException(409, f"Node '{dupe.name}' already uses that hostname or IP.")
    node = Node(
        role=payload.role,
        name=payload.name,
        lan_ip=payload.lan_ip,
        fqdn=payload.fqdn,
        qsfp_ip=payload.qsfp_ip,
        qsfp_iface=payload.qsfp_iface,
        ssh_user=payload.ssh_user,
        ssh_port=payload.ssh_port,
        auth_method=payload.auth_method,
        sudo_mode=payload.sudo_mode,
        ssh_password_enc=encrypt(payload.ssh_password),
        ssh_private_key_enc=encrypt(payload.ssh_private_key),
        ssh_key_passphrase_enc=encrypt(payload.ssh_key_passphrase),
        sudo_password_enc=encrypt(payload.sudo_password),
    )
    session.add(node)
    await session.commit()
    await session.refresh(node)
    return NodeOut.of(node)


@router.get("/{node_id}", response_model=NodeOut)
async def get_node(node_id: int, session: AsyncSession = Depends(get_session)):
    node = await session.get(Node, node_id)
    if node is None:
        raise HTTPException(404, "Node not found")
    return NodeOut.of(node)


@router.patch("/{node_id}", response_model=NodeOut)
async def update_node(
    node_id: int, payload: NodeUpdate, session: AsyncSession = Depends(get_session)
):
    node = await session.get(Node, node_id)
    if node is None:
        raise HTTPException(404, "Node not found")
    data = payload.model_dump(exclude_unset=True)
    secret_map = {
        "ssh_password": "ssh_password_enc",
        "ssh_private_key": "ssh_private_key_enc",
        "ssh_key_passphrase": "ssh_key_passphrase_enc",
        "sudo_password": "sudo_password_enc",
    }
    for field, value in data.items():
        if field in secret_map:
            setattr(node, secret_map[field], encrypt(value))
        else:
            setattr(node, field, value)
    await session.commit()
    await pool.drop(node.id)  # connection params may have changed
    await session.refresh(node)
    return NodeOut.of(node)


@router.delete("/{node_id}", status_code=204)
async def delete_node(node_id: int, session: AsyncSession = Depends(get_session)):
    node = await session.get(Node, node_id)
    if node is None:
        raise HTTPException(404, "Node not found")
    await session.delete(node)
    await session.commit()
    await pool.drop(node_id)


@router.post("/{node_id}/test", response_model=ConnectionTest)
async def test_node(node_id: int, session: AsyncSession = Depends(get_session)):
    node = await session.get(Node, node_id)
    if node is None:
        raise HTTPException(404, "Node not found")
    return await cluster.test_node_connection(session, node)


# One awk-parsable record per physical NIC: name|operstate|carrier|speed|driver|mac.
# Virtual devices (lo, docker bridges, veths, bonds' slaves stay listed — they're
# real ports) are filtered by name pattern only, so nothing physical is hidden.
_IFACE_SCRIPT = r"""
for d in /sys/class/net/*; do
  i=$(basename "$d")
  case "$i" in lo|docker*|veth*|br-*|virbr*|tailscale*|cni*|flannel*) continue;; esac
  [ -e "$d/device" ] || continue
  state=$(cat "$d/operstate" 2>/dev/null || echo unknown)
  carrier=$(cat "$d/carrier" 2>/dev/null || echo 0)
  speed=$(cat "$d/speed" 2>/dev/null || echo -1)
  driver=$(basename "$(readlink "$d/device/driver" 2>/dev/null)" 2>/dev/null)
  mac=$(cat "$d/address" 2>/dev/null)
  echo "$i|$state|$carrier|$speed|$driver|$mac"
done
"""


@router.get("/{node_id}/interfaces", response_model=list[InterfaceInfo])
async def list_interfaces(node_id: int, session: AsyncSession = Depends(get_session)):
    """Enumerate the node's physical network ports so the UI can offer a picker
    for the QSFP interface (link state + speed shows which port has the cable)."""
    node = await session.get(Node, node_id)
    if node is None:
        raise HTTPException(404, "Node not found")
    try:
        ssh = await ssh_for_node(session, node)
        res = await ssh.run(_IFACE_SCRIPT, timeout=20)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Could not reach {node.name} over SSH: {exc}")
    if not res.ok:
        raise HTTPException(502, f"Interface listing failed: {res.stderr or res.stdout}")
    out: list[InterfaceInfo] = []
    for line in res.stdout.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 6 or not parts[0]:
            continue
        name, state, carrier, speed, driver, mac = parts
        try:
            speed_mbps = int(speed)
        except ValueError:
            speed_mbps = -1
        out.append(
            InterfaceInfo(
                name=name,
                operstate=state,
                carrier=carrier.strip() == "1",
                speed_mbps=speed_mbps if speed_mbps > 0 else None,
                driver=driver or None,
                mac=mac or None,
                # ConnectX-7 shows as mlx5_core; >=40G is a QSFP-class port either way
                qsfp_candidate=(driver == "mlx5_core") or speed_mbps >= 40000,
            )
        )
    # QSFP candidates first, link-up first, fastest first
    out.sort(key=lambda i: (not i.qsfp_candidate, not i.carrier, -(i.speed_mbps or 0), i.name))
    return out


@router.post("/{node_id}/forget-host-key", response_model=NodeOut)
async def forget_host_key(node_id: int, session: AsyncSession = Depends(get_session)):
    """Clear a node's pinned SSH host key so the next connect trusts a new one.

    The deliberate action after a legitimate rebuild. It is deliberately manual:
    a changed host key is either a reinstall or an interception, and only a
    human knows which.
    """
    node = await session.get(Node, node_id)
    if node is None:
        raise HTTPException(404, "Node not found")
    node.host_key = None
    await session.commit()
    await pool.drop(node_id)  # force a fresh connect that re-pins
    log.warning("cleared the pinned SSH host key for %s", node.name)
    return NodeOut.of(node)


@router.post("/{node_id}/certificate/csr")
async def create_csr(node_id: int, session: AsyncSession = Depends(get_session)):
    """Generate a key ON the node and return its certificate signing request.

    The key never comes back — only stdout does, and stdout is the CSR. Take
    the request to whatever CA you run, then POST the signed certificate to
    `/certificate`. This is the same path the OpenBao renewal uses; the only
    difference is who signs.

    The new key is staged beside the live one and promoted only when a
    certificate arrives, so generating a request never disturbs a running
    proxy — which matters when signing takes days.
    """
    from ..config import get_settings
    from ..services import nodecert

    node = await session.get(Node, node_id)
    if node is None:
        raise HTTPException(404, "Node not found")
    if not node.fqdn:
        raise HTTPException(
            400,
            "Set this node's DNS name first. The cluster proxy verifies the "
            "certificate against that name, so it has to be decided before "
            "the request is generated.",
        )
    ssh = await ssh_for_node(session, node)
    res = await ssh.run(
        nodecert.csr_command(get_settings().node_install_dir, node.fqdn)
    )
    csr = (res.stdout or "").strip()
    if "BEGIN CERTIFICATE REQUEST" not in csr:
        raise HTTPException(
            502,
            f"{node.name} did not produce a signing request. Is openssl "
            f"installed on the node? It said: {(res.stderr or '').strip()[:300]}",
        )
    node.tls_csr_pem = csr
    await session.commit()
    return {"node": node.name, "fqdn": node.fqdn, "csr": csr}


@router.post("/{node_id}/certificate", response_model=NodeOut)
async def upload_certificate(
    node_id: int, payload: NodeCertIn, session: AsyncSession = Depends(get_session)
):
    """Install a signed certificate on the node.

    Works for a certificate signed from this node's CSR (the key is already
    there and gets promoted) and for one an operator holds as a cert+key pair.
    The pair path is supported but weaker, and deliberately not the default:
    `nodes` travels in the backup bundle, so a key the portal handles is a key
    written to an S3 object on a schedule.
    """
    from ..config import get_settings
    from ..services import nodecert

    node = await session.get(Node, node_id)
    if node is None:
        raise HTTPException(404, "Node not found")

    check = nodecert.check_certificate(payload.certificate, node)
    if not check.ok:
        raise HTTPException(400, check.error)
    if not payload.private_key and not node.tls_csr_pem:
        raise HTTPException(
            400,
            "There is no pending signing request for this node, so the node "
            "has no matching private key. Generate a request first, or supply "
            "the private key alongside the certificate.",
        )

    install_dir = get_settings().node_install_dir
    ssh = await ssh_for_node(session, node)
    if payload.private_key:
        _cert, key_path, _ca = nodecert.node_cert_paths(install_dir)
        await nodeops.install_file(ssh, key_path + ".new", payload.private_key, mode="600")
    await nodecert.install(ssh, install_dir, payload.certificate, payload.ca_certificate)

    reloaded = await nodecert.reload_sidecars(
        ssh, install_dir, await _sidecars_on(session, node)
    )

    info = check.info
    node.tls_cert_pem = payload.certificate
    node.tls_csr_pem = None
    node.tls_fingerprint = info.fingerprint_sha256 if info else None
    node.tls_not_after = info.not_after.replace(tzinfo=None) if info and info.not_after else None
    node.tls_issued_at = datetime.now(timezone.utc).replace(tzinfo=None)
    node.tls_last_error = None
    if payload.ca_certificate:
        setting = await get_setting(session)
        setting.node_ca_pem = payload.ca_certificate
    await session.commit()
    log.info("installed certificate on %s (reloaded: %s)", node.name, reloaded or "none")
    return NodeOut.of(node)


async def _sidecars_on(session: AsyncSession, node: Node) -> list[str]:
    from ..models import Instance

    rows = (await session.execute(select(Instance))).scalars().all()
    out = []
    for inst in rows:
        if not inst.tls_enabled:
            continue
        if inst.topology == "single" and inst.node_id != node.id:
            continue
        if inst.topology != "single" and node.role != "head":
            continue
        out.append(inst.name)
    return out


@router.post("/{node_id}/harden", response_model=JobAccepted)
async def harden_node(node_id: int, session: AsyncSession = Depends(get_session)):
    node = await session.get(Node, node_id)
    if node is None:
        raise HTTPException(404, "Node not found")
    job_id = await jobs.start(
        "node.harden", f"Harden {node.name}", lambda h: cluster.harden_node(h, node_id),
        node_id=node_id,
    )
    return JobAccepted(job_id=job_id, message="Hardening started")
