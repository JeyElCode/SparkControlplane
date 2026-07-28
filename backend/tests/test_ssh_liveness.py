"""A pooled SSH connection must not outlive its transport.

asyncssh keeps the connection object around after the transport dies, so the
old ``if self._ssh is not None: return`` handed out a dead connection forever:
after an sshd restart, network flap, or node reboot every command on that node
failed until the *portal* was restarted, and telemetry reported the node
permanently unreachable even once it had recovered.
"""

from __future__ import annotations

import asyncssh
import pytest

from app.models import SUDO_NOPASSWD
from app.ssh.client import NodeConn, SSHClient, SSHError


def _conn(node_id: int = 1) -> NodeConn:
    return NodeConn(
        id=node_id,
        role="head",
        name="dgx-md-01",
        lan_ip="10.0.0.1",
        qsfp_ip="10.10.10.1",
        qsfp_iface="enp1s0f1np1",
        ssh_user="spark",
        ssh_port=22,
        auth_method="password",
        password="pw",
        private_key=None,
        key_passphrase=None,
        sudo_mode=SUDO_NOPASSWD,
        sudo_password=None,
    )


class FakeConnection:
    """Stands in for asyncssh.SSHClientConnection: alive until killed."""

    def __init__(self) -> None:
        self.closed = False
        self.aborted = False

    def is_closed(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True

    def abort(self) -> None:
        self.aborted = True
        self.closed = True

    async def wait_closed(self) -> None:
        self.closed = True

    def kill(self) -> None:
        """Simulate the transport dying underneath us (sshd restart)."""
        self.closed = True


@pytest.fixture()
def connections(monkeypatch):
    """Patch asyncssh.connect to hand out FakeConnections; return the list of
    every connection handed out, newest last."""
    made: list[FakeConnection] = []

    async def fake_connect(**_opts):
        c = FakeConnection()
        made.append(c)
        return c

    monkeypatch.setattr(asyncssh, "connect", fake_connect)
    return made


async def test_reuses_a_live_connection(connections):
    client = SSHClient(_conn())
    await client.connect()
    await client.connect()
    assert len(connections) == 1, "a live connection must be reused, not redialled"


async def test_dead_connection_is_replaced_on_next_connect(connections):
    client = SSHClient(_conn())
    await client.connect()
    connections[0].kill()  # sshd restarted / link flapped

    await client.connect()

    assert len(connections) == 2, "a dead connection must be replaced, not reused"
    assert connections[0].aborted, "the dead connection should be discarded"
    assert not connections[1].is_closed()


async def test_transport_failure_evicts_so_the_next_call_reconnects(connections, monkeypatch):
    """A connection-level failure during run() must drop the connection."""
    client = SSHClient(_conn())
    await client.connect()

    def boom(_remote):
        raise asyncssh.ConnectionLost("connection lost")

    monkeypatch.setattr(connections[0], "create_process", boom, raising=False)

    with pytest.raises(SSHError):
        await client.run("hostname")

    assert client._ssh is None, "transport failure must evict the pooled connection"

    await client.connect()
    assert len(connections) == 2, "the next call must establish a fresh connection"


async def test_command_timeout_keeps_the_connection(connections, monkeypatch):
    """A slow command is not a broken connection — evicting on timeout would
    force a reconnect for every long-running script."""
    import asyncio

    client = SSHClient(_conn())
    await client.connect()

    class HangingReader:
        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(10)
            raise StopAsyncIteration

    class FakeStdin:
        def write(self, _data):
            pass

        def write_eof(self):
            pass

    class FakeProc:
        stdin = FakeStdin()
        stdout = HangingReader()
        stderr = HangingReader()
        exit_status = 0

        async def wait(self):
            await asyncio.sleep(10)

    class HangingProcess:
        async def __aenter__(self):
            return FakeProc()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(
        connections[0], "create_process", lambda _r: HangingProcess(), raising=False
    )

    with pytest.raises(SSHError, match="timed out"):
        await client.run("sleep 100", timeout=0.05)

    assert client._ssh is connections[0], "a timeout must not discard the connection"
