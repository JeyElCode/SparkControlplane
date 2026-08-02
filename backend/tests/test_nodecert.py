"""Node certificates, whichever CA issued them.

Manual issuance is not a degraded mode here. An operator with a corporate CA
and no OpenBao gets the same private-key property the automated path gets —
the key is generated on the node and only a CSR travels — because the only
things that move are a signing request and a certificate, neither of which is
secret. Every guard below runs identically in both modes.

What manual mode genuinely lacks is self-renewal, which makes expiry tracking
matter more rather than less: nothing will save an operator from a certificate
that lapses at 03:00 except having been told days earlier.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.models import Node
from app.services import nodecert
from app.services.nodecert import check_certificate, csr_command, renewal_due

cryptography = pytest.importorskip("cryptography")

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402


def _cert(*, cn="dgx-md-01.example.net", sans=("dgx-md-01.example.net",),
          days_valid=30, days_ago=0) -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)
    b = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(days=days_valid))
    )
    if sans:
        b = b.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(s) for s in sans]),
            critical=False,
        )
    return b.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM).decode()


def _node(fqdn="dgx-md-01.example.net") -> Node:
    return Node(role="head", name="dgx-md-01", lan_ip="10.0.0.11",
                qsfp_ip="10.10.10.1", ssh_user="u", fqdn=fqdn)


# --- what may be installed ------------------------------------------------

def test_a_certificate_covering_the_node_name_is_accepted():
    assert check_certificate(_cert(), _node()).ok


def test_a_name_only_in_the_common_name_is_refused():
    """THE trap. X509_check_host matches dNSName SANs and never consults the
    Common Name, so this certificate looks correct in every UI and is rejected
    on every connection from the cluster."""
    c = _cert(cn="dgx-md-01.example.net", sans=("something-else.example.net",))
    r = check_certificate(c, _node())
    assert not r.ok
    assert "does not cover" in r.error
    assert "Common Name is not consulted" in r.error


def test_a_certificate_for_a_different_host_is_refused():
    r = check_certificate(_cert(sans=("dgx-md-02.example.net",)), _node())
    assert not r.ok
    assert "dgx-md-02.example.net" in r.error


def test_a_wildcard_covering_one_label_is_accepted():
    assert check_certificate(_cert(sans=("*.example.net",)), _node()).ok


def test_a_wildcard_does_not_span_two_labels():
    """Same rule browsers apply, so a certificate accepted here behaves the
    same way in the cluster."""
    node = _node("dgx-md-01.dc1.example.net")
    assert not check_certificate(_cert(sans=("*.example.net",)), node).ok


def test_an_expired_certificate_is_refused():
    r = check_certificate(_cert(days_valid=1, days_ago=5), _node())
    assert not r.ok
    assert "expired" in r.error
    assert "down immediately" in r.error


def test_a_node_without_a_dns_name_cannot_take_a_certificate():
    r = check_certificate(_cert(), _node(fqdn=None))
    assert not r.ok
    assert "no DNS name" in r.error


def test_garbage_is_refused_without_raising():
    r = check_certificate("-----BEGIN CERTIFICATE-----\nnope\n", _node())
    assert not r.ok


# --- the key stays on the node -------------------------------------------

def test_the_csr_command_never_prints_the_key():
    """Only stdout comes back over SSH, and stdout is the CSR."""
    cmd = csr_command("/opt/spark", "dgx-md-01.example.net")
    assert "-keyout" in cmd
    assert "cat" not in cmd
    assert "-nodes" in cmd     # nginx must start unattended after a reboot


def test_the_new_key_is_staged_and_does_not_clobber_the_live_one():
    """In manual mode, signing can take days. Overwriting the live key when
    the CSR is generated would break the running proxy for that whole window."""
    cmd = csr_command("/opt/spark", "dgx-md-01.example.net")
    assert "node-key.pem.new" in cmd
    assert "-keyout /opt/spark/tls/node/node-key.pem\n" not in cmd


def test_the_key_is_created_with_a_restrictive_umask():
    """chmod after the fact leaves a window where the key is world-readable."""
    cmd = csr_command("/opt/spark", "dgx-md-01.example.net")
    assert "umask 077" in cmd
    assert cmd.index("umask 077") < cmd.index("openssl req")


def test_the_csr_carries_the_name_as_a_san_not_only_a_cn():
    cmd = csr_command("/opt/spark", "dgx-md-01.example.net")
    assert "subjectAltName=DNS:dgx-md-01.example.net" in cmd


def test_certificates_live_outside_every_per_instance_directory():
    """Instance directories are `rm -rf`'d by stop and delete. A certificate
    stored there would vanish when an endpoint's outgoing instance was cleaned
    up — during a promotion, which is exactly when it is needed."""
    from app.services import templates

    cert, key, ca = nodecert.node_cert_paths("/opt/spark")
    for inst_name in ("id7", "id9", "node"):
        inst_dir = templates.tls_dir("/opt/spark", inst_name)
        assert not cert.startswith(inst_dir + "/")
        assert not key.startswith(inst_dir + "/")
        assert not ca.startswith(inst_dir + "/")


# --- renewal timing -------------------------------------------------------

def _aged(hours: float) -> Node:
    n = _node()
    n.tls_cert_pem = "x"
    n.tls_issued_at = dt.datetime.utcnow() - dt.timedelta(hours=hours)
    n.tls_not_after = dt.datetime.utcnow() + dt.timedelta(hours=168 - hours)
    return n


def test_renewal_does_not_start_before_two_thirds_of_life():
    assert not renewal_due(_aged(100), 168)


def test_renewal_starts_with_a_third_of_life_left():
    assert renewal_due(_aged(113), 168)


def test_renewal_timing_follows_the_operators_chosen_lifetime():
    """A node 20 hours into a 24-hour certificate is due; the same node is not
    due on a 30-day one."""
    assert renewal_due(_aged(20), 24)
    assert not renewal_due(_aged(20), 720)


def test_a_node_with_no_certificate_is_always_due():
    n = _node()
    assert renewal_due(n, 168)


def test_a_certificate_installed_outside_the_portal_is_still_judged():
    """Timing is read off the certificate's own validity window, not off a
    portal-side record of when it issued one."""
    n = _aged(160)
    assert renewal_due(n, 168)
