"""Installing and tracking a node's TLS certificate, whatever issued it.

Everything here is deliberately independent of *where* the certificate came
from. OpenBao signing a CSR and an operator pasting one back from a corporate
CA arrive at the same place: a key on the node, a certificate that covers the
node's DNS name, an expiry the portal tracks, and an alert if it is running
out. Only the issuance step differs, and it is the smaller half.

That is why the CSR flow is the primary path rather than an OpenBao detail.
An operator with a Windows CA, an internal step-ca, or a corporate PKI ticket
queue gets the same property the automated path gets — **the private key is
generated on the node and never leaves it** — because all they have to move is
a certificate signing request and a certificate, neither of which is secret.

Uploading a key alongside a certificate is supported for operators who already
hold a pair, but it is the weaker option and the API says so: `nodes` is in the
backup bundle, so a key the portal has touched is a key that will be written to
an S3 object on a schedule.

The one thing manual mode cannot do is renew itself, which makes expiry
tracking matter *more* there, not less. A certificate that lapses at 03:00
takes every endpoint down, and the only warning anyone gets is the one this
module records.
"""

from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone

from ..models import Node
from . import nodeops, templates
from .endpoints import CertInfo, parse_certificate
from .pki import lifetime_policy

log = logging.getLogger("spark.nodecert")

__all__ = [
    "NODE_CERT_DIR",
    "CertCheck",
    "check_certificate",
    "csr_command",
    "node_cert_paths",
    "renewal_due",
]

# A SIBLING of the per-instance `tls/` tree, never a child of it.
#
# Instance directories are `rm -rf`'d by stop and delete, so a certificate
# under `tls/` would vanish when an endpoint's outgoing instance was cleaned
# up — during a promotion, which is exactly when it is needed. `tls/node` is
# not far enough: instance names allow letters, digits, dot, underscore and
# dash, so an instance literally named `node` gets `tls/node` as its own
# directory and deleting it would take the fleet's certificates with it.
# `tls-node` cannot collide with `tls/<anything>`.
NODE_CERT_SUBDIR = "tls-node"
NODE_CERT_FILE = "node-cert.pem"
NODE_KEY_FILE = "node-key.pem"
NODE_CA_FILE = "node-ca.pem"


def node_cert_paths(install_dir: str) -> tuple[str, str, str]:
    """(cert, key, ca) paths on the node. One place, so the renewer and the
    nginx config renderer cannot disagree about where the files live."""
    base = f"{install_dir.rstrip('/')}/{NODE_CERT_SUBDIR}"
    return (f"{base}/{NODE_CERT_FILE}", f"{base}/{NODE_KEY_FILE}", f"{base}/{NODE_CA_FILE}")


NODE_CERT_DIR = NODE_CERT_SUBDIR


def csr_command(install_dir: str, fqdn: str, *, key_bits: int = 3072) -> str:
    """Shell to generate a key and CSR on the node, printing the CSR.

    The key is written with mode 600 and never read back — only stdout, which
    carries the CSR, returns over SSH. `-nodes` because nginx must start
    unattended after a reboot; a passphrase would mean a human at every boot.

    RSA rather than an EC curve: this has to be signed by whatever CA the
    operator already runs, and RSA is the one thing every CA and every
    middlebox accepts. The cost is irrelevant for a handshake per connection.
    """
    cert_path, key_path, _ca = node_cert_paths(install_dir)
    base = key_path.rsplit("/", 1)[0]
    q = shlex.quote
    # `-addext` puts the SAN in the REQUEST. Most CAs regenerate SANs from
    # their own policy, but a CA that copies the CSR's extensions needs it
    # present, and a CSR without a SAN is rejected outright by some.
    return "\n".join([
        "set -e",
        f"mkdir -p {q(base)}",
        f"chmod 700 {q(base)}",
        # umask so the key is never briefly world-readable between creation
        # and chmod — on a shared box that window is enough.
        "umask 077",
        (
            f"openssl req -new -newkey rsa:{int(key_bits)} -nodes"
            f" -keyout {q(key_path + '.new')}"
            f" -subj {q('/CN=' + fqdn)}"
            f" -addext {q('subjectAltName=DNS:' + fqdn)}"
            " -outform PEM"
        ),
        # The key is staged as .new and only promoted when the signed
        # certificate arrives. Overwriting the live key at CSR time would
        # break the running proxy for however long signing takes — which in
        # manual mode can be days.
        f"chmod 600 {q(key_path + '.new')}",
        f": {q(cert_path)}",
    ])


@dataclass(frozen=True)
class CertCheck:
    """Whether a certificate may be installed on a node, and why not."""

    ok: bool
    error: str | None = None
    info: CertInfo | None = None

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return self.ok


def check_certificate(cert_pem: str, node: Node) -> CertCheck:
    """Refuse a certificate that cannot work, before anything is written.

    Every one of these would otherwise be discovered as an upstream TLS error
    in the cluster with no indication of the cause — and in manual mode,
    potentially days after the operator sent the CSR away.
    """
    if not node.fqdn:
        return CertCheck(
            False,
            "This node has no DNS name yet. The cluster proxy verifies the "
            "certificate against that name, so set it on the node first.",
        )
    info = parse_certificate(cert_pem)
    if not info.ok:
        return CertCheck(False, info.error or "Unusable certificate.", info)

    # X509_check_host matches dNSName SANs and ignores the Common Name, so a
    # certificate whose only mention of the node is in its CN would fail
    # verification in the cluster while looking correct in every UI.
    names = [n.lower() for n in info.sans]
    if not _covers(names, node.fqdn.lower()):
        listed = ", ".join(info.sans) or "none"
        return CertCheck(
            False,
            f"This certificate does not cover '{node.fqdn}' (its DNS names "
            f"are: {listed}). The cluster proxy matches against these names "
            "only — the Common Name is not consulted — so it would be "
            "rejected on every connection.",
            info,
        )
    if info.not_after is not None:
        now = datetime.now(timezone.utc)
        expiry = info.not_after
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry <= now:
            return CertCheck(
                False,
                f"This certificate expired on {expiry:%Y-%m-%d}. Installing it "
                "would take the endpoint down immediately.",
                info,
            )
    return CertCheck(True, None, info)


def _covers(sans: list[str], fqdn: str) -> bool:
    """Exact match, or a wildcard covering exactly one label — the same rule
    browsers apply, so a certificate accepted here behaves the same way in the
    cluster."""
    for san in sans:
        if san == fqdn:
            return True
        if san.startswith("*."):
            suffix = san[1:]                       # ".example.net"
            if fqdn.endswith(suffix) and "." not in fqdn[: -len(suffix)]:
                return True
    return False


def renewal_due(node: Node, ttl_hours: float | None) -> bool:
    """Has this node's certificate reached the age where renewal should start?

    Answered from the certificate's own validity window rather than from when
    the portal issued it, so a certificate replaced by hand outside the portal
    is still judged correctly.
    """
    if node.tls_not_after is None or node.tls_issued_at is None:
        return node.tls_cert_pem is None
    policy = lifetime_policy(ttl_hours)
    issued = node.tls_issued_at
    if issued.tzinfo is None:
        issued = issued.replace(tzinfo=timezone.utc)
    age_hours = (datetime.now(timezone.utc) - issued).total_seconds() / 3600.0
    return age_hours >= policy.renew_after_hours


async def install(ssh, install_dir: str, cert_pem: str, ca_pem: str | None) -> None:
    """Write the certificate (and CA) to the node and promote the staged key.

    Ordered so a failure never leaves nginx pointing at a key and certificate
    that do not match: the key is promoted only after the certificate lands.
    """
    cert_path, key_path, ca_path = node_cert_paths(install_dir)
    await nodeops.install_file(ssh, cert_path, cert_pem, mode="644")
    if ca_pem:
        await nodeops.install_file(ssh, ca_path, ca_pem, mode="644")
    q = shlex.quote
    # Promote atomically. `mv` within one filesystem is atomic, so nginx never
    # observes a partially-written key.
    await ssh.run(
        f"if [ -f {q(key_path + '.new')} ]; then "
        f"mv -f {q(key_path + '.new')} {q(key_path)}; fi",
        check=True,
    )


async def reload_sidecars(ssh, install_dir: str, names: list[str], log_cb=None) -> list[str]:
    """`nginx -s reload` every TLS sidecar on the node. Returns those reloaded.

    The explicit `-c` is not optional and not stylistic. The master was started
    with `-c <conf>`, and a bare `nginx -s reload` reads the image's default
    config, signals a pid that is not the running master, and exits 0 — the
    reload reports success and the old certificate stays live until the next
    restart. That is a silent no-op with a green checkmark.
    """
    reloaded = []
    for name in names:
        conf = f"{templates.tls_dir(install_dir, name)}/{templates.TLS_CONF_FILE}"
        res = await nodeops.docker(
            ssh,
            f"exec {templates.tls_container(name)} nginx -c {shlex.quote(conf)} -s reload",
        )
        if res.ok:
            reloaded.append(name)
        elif log_cb:
            log_cb(f"[{name}] reload failed: {res.stderr.strip()[:200]}")
    return reloaded
