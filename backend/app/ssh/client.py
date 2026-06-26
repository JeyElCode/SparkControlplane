"""Async SSH client wrapper around asyncssh.

Design notes
------------
* Every command is shipped to the node base64-encoded and decoded remotely
  (``echo <b64> | base64 -d | bash``). This means callers never have to worry
  about shell quoting of multi-line scripts, embedded quotes, ``$``, etc.
* sudo is handled two ways depending on ``sudo_mode``:
    - ``nopasswd``  -> ``sudo -n bash -c <inner>``
    - ``password``  -> ``sudo -S -p '' bash -c <inner>`` with the password fed
      as the first (and only) line of stdin.
* SSH login always uses the LAN IP; the QSFP IP is only ever used inside the
  cluster for Ray/NCCL traffic.
"""

from __future__ import annotations

import asyncio
import base64
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import asyncssh

from ..config import get_settings
from ..crypto import decrypt
from ..models import AUTH_KEY, SUDO_NOPASSWD, Node

LogCb = Callable[[str, str], Awaitable[None] | None]  # (stream, line)


@dataclass
class RunResult:
    exit_status: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_status == 0


@dataclass
class NodeConn:
    """Decrypted connection parameters for a node (no ORM/session held)."""

    id: int
    role: str
    name: str
    lan_ip: str
    qsfp_ip: str
    qsfp_iface: str
    ssh_user: str
    ssh_port: int
    auth_method: str
    password: str | None
    private_key: str | None
    key_passphrase: str | None
    sudo_mode: str
    sudo_password: str | None

    @classmethod
    def from_node(cls, node: Node) -> "NodeConn":
        return cls(
            id=node.id,
            role=node.role,
            name=node.name,
            lan_ip=node.lan_ip,
            qsfp_ip=node.qsfp_ip,
            qsfp_iface=node.qsfp_iface,
            ssh_user=node.ssh_user,
            ssh_port=node.ssh_port,
            auth_method=node.auth_method,
            password=decrypt(node.ssh_password_enc),
            private_key=decrypt(node.ssh_private_key_enc),
            key_passphrase=decrypt(node.ssh_key_passphrase_enc),
            sudo_mode=node.sudo_mode,
            sudo_password=decrypt(node.sudo_password_enc),
        )


class SSHError(RuntimeError):
    pass


class SSHClient:
    def __init__(self, conn: NodeConn):
        self.conn = conn
        self._ssh: asyncssh.SSHClientConnection | None = None
        self._lock = asyncio.Lock()

    # --- connection lifecycle -------------------------------------------
    async def connect(self) -> None:
        if self._ssh is not None:
            return
        async with self._lock:
            if self._ssh is not None:
                return
            settings = get_settings()
            opts: dict = dict(
                host=self.conn.lan_ip,
                port=self.conn.ssh_port,
                username=self.conn.ssh_user,
                known_hosts=None,  # lab cluster; host keys not pinned
                connect_timeout=settings.ssh_connect_timeout,
            )
            if self.conn.auth_method == AUTH_KEY and self.conn.private_key:
                try:
                    key = asyncssh.import_private_key(
                        self.conn.private_key, self.conn.key_passphrase or None
                    )
                except (asyncssh.KeyImportError, ValueError) as exc:
                    raise SSHError(f"Invalid SSH private key: {exc}") from exc
                opts["client_keys"] = [key]
                # Also allow password as a fallback during 'harden' transitions.
                if self.conn.password:
                    opts["password"] = self.conn.password
            else:
                opts["password"] = self.conn.password or ""
            try:
                self._ssh = await asyncssh.connect(**opts)
            except (OSError, asyncssh.Error) as exc:
                raise SSHError(f"SSH connect to {self.conn.lan_ip} failed: {exc}") from exc

    async def close(self) -> None:
        if self._ssh is not None:
            self._ssh.close()
            try:
                await self._ssh.wait_closed()
            except Exception:  # noqa: BLE001 - closing best-effort
                pass
            self._ssh = None

    async def __aenter__(self) -> "SSHClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # --- command execution ----------------------------------------------
    def _wrap(self, command: str, sudo: bool) -> tuple[str, str]:
        """Return (remote_command, stdin_prefix)."""
        b64 = base64.b64encode(command.encode()).decode()
        inner = f"echo {b64} | base64 -d | bash"
        if not sudo:
            return f"bash -c {shlex.quote(inner)}", ""
        if self.conn.sudo_mode == SUDO_NOPASSWD:
            return f"sudo -n bash -c {shlex.quote(inner)}", ""
        return f"sudo -S -p '' bash -c {shlex.quote(inner)}", (self.conn.sudo_password or "") + "\n"

    async def run(
        self,
        command: str,
        *,
        sudo: bool = False,
        timeout: float | None = None,
        check: bool = False,
        log_cb: LogCb | None = None,
    ) -> RunResult:
        """Run a (possibly multi-line) shell script on the node.

        If ``log_cb`` is provided it is invoked per output line as it streams.
        """
        await self.connect()
        assert self._ssh is not None
        remote, stdin_prefix = self._wrap(command, sudo)

        out_lines: list[str] = []
        err_lines: list[str] = []

        async def pump(reader, stream: str, sink: list[str]) -> None:
            async for line in reader:
                text = line.rstrip("\n")
                sink.append(text)
                if log_cb is not None:
                    res = log_cb(stream, text)
                    if asyncio.iscoroutine(res):
                        await res

        try:
            async with self._ssh.create_process(remote) as proc:
                if stdin_prefix:
                    proc.stdin.write(stdin_prefix)
                proc.stdin.write_eof()
                tasks = [
                    asyncio.create_task(pump(proc.stdout, "stdout", out_lines)),
                    asyncio.create_task(pump(proc.stderr, "stderr", err_lines)),
                ]
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=timeout)
                await asyncio.wait_for(proc.wait(), timeout=timeout)
                exit_status = proc.exit_status or 0
        except asyncio.TimeoutError as exc:
            raise SSHError(f"Command timed out after {timeout}s on {self.conn.name}") from exc
        except asyncssh.Error as exc:
            raise SSHError(f"SSH command failed on {self.conn.name}: {exc}") from exc

        result = RunResult(exit_status, "\n".join(out_lines), "\n".join(err_lines))
        if check and not result.ok:
            raise SSHError(
                f"Command exited {result.exit_status} on {self.conn.name}: "
                f"{result.stderr or result.stdout}"
            )
        return result

    # --- file helpers ----------------------------------------------------
    async def write_file(
        self,
        path: str,
        content: str,
        *,
        mode: str | None = None,
        sudo: bool = False,
    ) -> None:
        b64 = base64.b64encode(content.encode()).decode()
        dirq = shlex.quote(_dirname(path))
        pathq = shlex.quote(path)
        script = f"mkdir -p {dirq} && printf '%s' '{b64}' | base64 -d > {pathq}"
        if mode:
            script += f" && chmod {mode} {pathq}"
        await self.run(script, sudo=sudo, check=True)

    async def read_file(self, path: str, *, sudo: bool = False) -> str:
        res = await self.run(f"cat {shlex.quote(path)}", sudo=sudo, check=True)
        return res.stdout

    async def exists(self, path: str, *, sudo: bool = False) -> bool:
        res = await self.run(f"test -e {shlex.quote(path)} && echo y || echo n", sudo=sudo)
        return res.stdout.strip() == "y"


def _dirname(path: str) -> str:
    idx = path.rfind("/")
    return path[:idx] if idx > 0 else "/"
