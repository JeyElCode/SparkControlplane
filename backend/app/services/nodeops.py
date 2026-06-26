"""Common node operations layered on top of :class:`SSHClient`:

* running ``docker`` whether or not the login user is in the docker group yet
  (falls back to running it as root via sudo),
* installing helper scripts and systemd units, and driving systemctl.
"""

from __future__ import annotations

import shlex

from ..ssh import RunResult, SSHClient
from ..ssh.client import LogCb


async def docker_needs_sudo(ssh: SSHClient) -> bool:
    """True if plain ``docker`` is not usable as the login user (so we must run
    it as root). Common right after adding the user to the docker group, before
    a fresh login picks up the membership."""
    res = await ssh.run("docker version --format '{{.Server.Version}}'")
    return not res.ok


async def docker(
    ssh: SSHClient,
    subcmd: str,
    *,
    need_sudo: bool | None = None,
    log_cb: LogCb | None = None,
    check: bool = False,
    timeout: float | None = None,
) -> RunResult:
    if need_sudo is None:
        need_sudo = await docker_needs_sudo(ssh)
    return await ssh.run(
        f"docker {subcmd}", sudo=need_sudo, log_cb=log_cb, check=check, timeout=timeout
    )


async def install_file(
    ssh: SSHClient, path: str, content: str, *, mode: str | None = None
) -> None:
    """Write a root-owned file (e.g. under /opt or /etc) via sudo."""
    await ssh.write_file(path, content, mode=mode, sudo=True)


async def systemctl(
    ssh: SSHClient, action: str, unit: str | None = None, *, log_cb: LogCb | None = None
) -> RunResult:
    cmd = f"systemctl {action}"
    if unit:
        cmd += f" {shlex.quote(unit)}"
    return await ssh.run(cmd, sudo=True, log_cb=log_cb)


async def daemon_reload(ssh: SSHClient, *, log_cb: LogCb | None = None) -> None:
    await ssh.run("systemctl daemon-reload", sudo=True, log_cb=log_cb, check=True)


async def install_systemd_unit(
    ssh: SSHClient,
    unit_name: str,
    content: str,
    *,
    enable: bool = True,
    start: bool = True,
    log_cb: LogCb | None = None,
) -> None:
    await install_file(ssh, f"/etc/systemd/system/{unit_name}", content, mode="644")
    await daemon_reload(ssh, log_cb=log_cb)
    if enable:
        await systemctl(ssh, "enable", unit_name, log_cb=log_cb)
    if start:
        # restart so re-installing an existing unit picks up new config
        await systemctl(ssh, "restart", unit_name, log_cb=log_cb)


async def remove_systemd_unit(
    ssh: SSHClient, unit_name: str, *, log_cb: LogCb | None = None
) -> None:
    await systemctl(ssh, "stop", unit_name, log_cb=log_cb)
    await systemctl(ssh, "disable", unit_name, log_cb=log_cb)
    await ssh.run(f"rm -f /etc/systemd/system/{shlex.quote(unit_name)}", sudo=True, log_cb=log_cb)
    await daemon_reload(ssh, log_cb=log_cb)


async def unit_active(ssh: SSHClient, unit_name: str) -> bool:
    res = await ssh.run(f"systemctl is-active {shlex.quote(unit_name)}", sudo=True)
    return res.stdout.strip() == "active"


async def unit_exists(ssh: SSHClient, unit_name: str) -> bool:
    return await ssh.exists(f"/etc/systemd/system/{unit_name}", sudo=True)
