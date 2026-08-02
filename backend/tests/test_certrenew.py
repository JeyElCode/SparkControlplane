"""The renewal loop, and the warning that is the whole mechanism in manual mode.

Renewal only exists for `openbao`. Warning exists in every mode, and in
`manual` it is the only thing standing between an operator and a 03:00 outage:
a lapsed certificate takes every endpoint down at once, and the cluster proxy
reports it as an upstream TLS error with no hint that a date was the cause.

The loop must also be inert by default. `none` is the shipped setting, so an
upgrade must neither renew anything nor start warning about certificates
nobody has.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import importlib

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    import app.config as config

    config.get_settings.cache_clear()
    import app.db as db

    importlib.reload(db)
    asyncio.run(db.init_db())
    import app.services.certrenew as certrenew

    importlib.reload(certrenew)
    monkeypatch.setattr("app.db.SessionLocal", db.SessionLocal, raising=False)
    monkeypatch.setattr(certrenew._db, "SessionLocal", db.SessionLocal, raising=False)
    yield {"db": db, "cr": certrenew}
    config.get_settings.cache_clear()


def _seed(db, *, source="openbao", hours_old=160.0, ttl=168.0, fqdn="dgx-md-01.example.net"):
    from app.models import Node

    async def _go():
        async with db.SessionLocal() as s:
            setting = await __import__("app.db", fromlist=["x"]).get_setting(s)
            setting.node_cert_source = source
            setting.node_cert_ttl_hours = ttl
            setting.pki_url = "https://bao.example.net"
            setting.pki_role = "dgx"
            from app.crypto import encrypt

            setting.pki_token_enc = encrypt("s.token")
            now = dt.datetime.utcnow()
            s.add(Node(
                role="head", name="dgx-md-01", lan_ip="10.0.0.11",
                qsfp_ip="10.10.10.1", ssh_user="u", fqdn=fqdn,
                tls_cert_pem="x",
                tls_issued_at=now - dt.timedelta(hours=hours_old),
                tls_not_after=now + dt.timedelta(hours=ttl - hours_old),
            ))
            await s.commit()

    asyncio.run(_go())


def test_the_loop_does_nothing_when_no_source_is_configured(env):
    """`none` is the shipped default. An upgrade must not start renewing or
    warning about certificates nobody has."""
    _seed(env["db"], source="none")
    out = asyncio.run(env["cr"].renewer.tick())
    assert out["checked"] == 0
    assert out["renewed"] == []
    assert env["cr"].renewer.expiring == {}


def test_manual_mode_never_renews_but_does_report_expiry(env):
    """There is nothing to call; the number is the deliverable."""
    _seed(env["db"], source="manual", hours_old=160.0)
    out = asyncio.run(env["cr"].renewer.tick())
    assert out["renewed"] == []
    assert out["checked"] == 1
    assert list(out["expiring"].values())[0] == pytest.approx(8.0, abs=0.2)


def test_expiry_is_published_for_the_alert_engine(env):
    """Read from a module-level cache rather than a DB session, matching how
    telemetry publishes — `gather_facts` takes no session."""
    _seed(env["db"], source="manual", hours_old=100.0)
    asyncio.run(env["cr"].renewer.tick())
    assert env["cr"].renewer.expiring
    assert list(env["cr"].renewer.expiring.values())[0] == pytest.approx(68.0, abs=0.2)


def test_a_node_without_a_dns_name_is_skipped(env):
    _seed(env["db"], source="openbao", fqdn=None)
    out = asyncio.run(env["cr"].renewer.tick())
    assert out["checked"] == 0


def test_a_certificate_not_yet_due_is_left_alone(env):
    _seed(env["db"], source="openbao", hours_old=50.0)
    out = asyncio.run(env["cr"].renewer.tick())
    assert out["renewed"] == []
    assert out["failed"] == []


def test_a_failing_renewal_is_recorded_and_does_not_stop_the_pass(env, monkeypatch):
    """A bad tick must not kill the loop or the other nodes. There is a whole
    retry window left, and the alert fires from time-remaining rather than
    from this failure — so one bad tick pages nobody."""
    from sqlalchemy import select

    _seed(env["db"], source="openbao", hours_old=160.0)

    async def boom(session, node):
        raise RuntimeError("ssh unreachable")

    monkeypatch.setattr(env["cr"], "ssh_for_node", boom, raising=False)
    monkeypatch.setattr("app.ssh.ssh_for_node", boom)

    out = asyncio.run(env["cr"].renewer.tick())
    assert out["failed"], out
    assert "ssh unreachable" in out["failed"][0]["error"]

    async def _read():
        from app.models import Node

        async with env["db"].SessionLocal() as s:
            return (await s.execute(select(Node))).scalars().one()

    # Recorded on the row so the Nodes page can show it, not only logged.
    assert "ssh unreachable" in asyncio.run(_read()).tls_last_error


def test_openbao_returning_a_certificate_for_the_wrong_host_is_refused(env, monkeypatch):
    """Checked BEFORE it is written. Installed, it would surface only as an
    upstream TLS failure in the cluster with nothing naming the cause."""
    from sqlalchemy import select

    from app.services import pki

    _seed(env["db"], source="openbao", hours_old=160.0)

    class FakeSSH:
        async def run(self, cmd, check=False, timeout=None):
            class R:
                ok = True
                stdout = "-----BEGIN CERTIFICATE REQUEST-----\nx\n-----END CERTIFICATE REQUEST-----"
                stderr = ""

            return R()

    async def fake_ssh(session, node):
        return FakeSSH()

    async def fake_sign(**kw):
        return pki.SignedCert(
            certificate=_cert_for("somewhere-else.example.net"),
            ca_chain="-----BEGIN CERTIFICATE-----\nCA\n-----END CERTIFICATE-----",
        )

    monkeypatch.setattr("app.ssh.ssh_for_node", fake_ssh)
    monkeypatch.setattr(pki, "sign_csr", fake_sign)
    monkeypatch.setattr(env["cr"].pki, "sign_csr", fake_sign)

    out = asyncio.run(env["cr"].renewer.tick())
    assert out["failed"], out
    assert "does not cover" in out["failed"][0]["error"]

    async def _read():
        from app.models import Node

        async with env["db"].SessionLocal() as s:
            return (await s.execute(select(Node))).scalars().one()

    # The working certificate is untouched — a bad renewal must not be worse
    # than no renewal.
    assert asyncio.run(_read()).tls_cert_pem == "x"


def _cert_for(name: str) -> str:
    pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(days=7))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()
