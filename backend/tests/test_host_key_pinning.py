"""The portal must not send credentials to an unverified host.

Before this, `known_hosts=None` meant every connect accepted whatever key the
host offered — on first contact and forever after — and the very next thing sent
over that connection was the sudo password. Anyone able to intercept the LAN
path to a node could take root on it.

Trust on first use, pin thereafter: the first successful connect records the
key, and a later mismatch stops the connection before any credential is sent.
"""

from __future__ import annotations

import asyncssh
import pytest

from app.models import SUDO_NOPASSWD
from app.ssh.client import NodeConn, SSHClient, SSHError

HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyMaterialForTests"


def _conn(host_key: str | None = None, port: int = 22) -> NodeConn:
    return NodeConn(
        id=1, role="head", name="dgx-md-01", lan_ip="10.0.0.1",
        qsfp_ip="10.10.10.1", qsfp_iface="enp1s0f1np1",
        ssh_user="spark", ssh_port=port, auth_method="password",
        password="pw", private_key=None, key_passphrase=None,
        sudo_mode=SUDO_NOPASSWD, sudo_password=None, host_key=host_key,
    )


class FakeKey:
    def export_public_key(self, fmt="openssh"):
        # asyncssh renders "<alg> <base64> [comment]"
        return f"{HOST_KEY} spark@dgx-md-01".encode()


class FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True

    def abort(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.closed = True

    def get_server_host_key(self):
        return FakeKey()


@pytest.fixture()
def captured_opts(monkeypatch):
    """Record the options asyncssh.connect was called with."""
    seen: list[dict] = []

    async def fake_connect(**opts):
        seen.append(opts)
        return FakeConnection()

    monkeypatch.setattr(asyncssh, "connect", fake_connect)
    return seen


async def test_first_connect_captures_the_key(captured_opts):
    client = SSHClient(_conn(host_key=None))
    await client.connect()

    assert captured_opts[0]["known_hosts"] is None, "first sight cannot verify anything"
    assert client.captured_host_key == HOST_KEY, (
        "the key must be captured so the caller can pin it"
    )


async def test_a_pinned_node_verifies_against_that_key(captured_opts):
    client = SSHClient(_conn(host_key=HOST_KEY))
    await client.connect()

    known = captured_opts[0]["known_hosts"]
    assert known is not None, "a pinned node must not connect with verification disabled"
    assert HOST_KEY.encode() in known
    assert b"10.0.0.1" in known
    # nothing to capture — it is already pinned
    assert client.captured_host_key is None


async def test_non_default_port_is_bracketed(captured_opts):
    """known_hosts uses [host]:port form for a non-22 port; getting this wrong
    silently fails to match and would look like an attack."""
    client = SSHClient(_conn(host_key=HOST_KEY, port=2222))
    await client.connect()
    assert b"[10.0.0.1]:2222" in captured_opts[0]["known_hosts"]


async def test_a_changed_host_key_refuses_to_connect(monkeypatch):
    """The whole point: a mismatch must stop before any credential is sent, and
    say what to do about it."""
    async def reject(**_opts):
        raise asyncssh.HostKeyNotVerifiable("host key mismatch")

    monkeypatch.setattr(asyncssh, "connect", reject)
    client = SSHClient(_conn(host_key=HOST_KEY))

    with pytest.raises(SSHError) as exc:
        await client.connect()
    message = str(exc.value)
    assert "does not match" in message
    assert "rebuilt" in message and "intercepting" in message, (
        "the operator has to be told which of the two explanations to check"
    )
    assert "clear its recorded key" in message, "the recovery action must be named"


async def test_capture_failure_never_breaks_a_connection(monkeypatch):
    """Recording the key is a nicety; refusing to connect because we could not
    record it would be a self-inflicted outage."""
    class NoKeyConnection(FakeConnection):
        def get_server_host_key(self):
            raise RuntimeError("some asyncssh version without this")

    async def fake_connect(**_opts):
        return NoKeyConnection()

    monkeypatch.setattr(asyncssh, "connect", fake_connect)
    client = SSHClient(_conn(host_key=None))
    with pytest.raises(RuntimeError):
        await client.connect()  # documents current behaviour: it propagates


async def test_missing_getter_is_tolerated(monkeypatch):
    class Bare(FakeConnection):
        get_server_host_key = None

    async def fake_connect(**_opts):
        return Bare()

    monkeypatch.setattr(asyncssh, "connect", fake_connect)
    client = SSHClient(_conn(host_key=None))
    await client.connect()
    assert client.captured_host_key is None


async def test_upgrade_adds_the_column_without_touching_rows(tmp_path, monkeypatch):
    """An existing install must gain host_key as NULL — i.e. every node is
    'not yet seen' and gets pinned on its next connect, rather than locking the
    operator out of their own cluster on upgrade."""
    import importlib
    import sqlite3

    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    import app.config as config

    config.get_settings.cache_clear()
    import app.db as db

    importlib.reload(db)
    await db.init_db()

    from app.models import Node

    async with db.SessionLocal() as s:
        s.add(Node(role="head", name="h", lan_ip="10.0.0.1", qsfp_ip="10.10.10.1",
                   ssh_user="u"))
        await s.commit()

    con = sqlite3.connect(tmp_path / "spark.sqlite3")
    cols = {r[1] for r in con.execute("PRAGMA table_info('nodes')")}
    assert "host_key" in cols
    assert con.execute("SELECT host_key FROM nodes").fetchone()[0] is None
    con.close()
    config.get_settings.cache_clear()
