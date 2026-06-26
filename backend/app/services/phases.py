"""Setup phases — the SSH automation of the runbook. Each phase is idempotent
(check -> apply -> verify) and re-runnable. Logs stream to the owning job.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import ClusterConfig, Node, Setting
from ..ssh import SSHClient, ssh_for_node
from .jobs import JobHandle
from . import nodeops, templates
from .paths import hf_cache_host_path, home_dir, models_host_dir

PHASES_ORDER = [
    "prereqs", "hosts", "network", "ssh", "packages", "docker", "image", "ray", "verify",
]

PHASE_TITLES = {
    "prereqs": "Check prerequisites (SSH, sudo, GPU, disk)",
    "hosts": "Configure hostnames and /etc/hosts",
    "network": "Configure QSFP private network (nmcli)",
    "ssh": "Configure passwordless inter-node SSH",
    "packages": "Install base packages",
    "docker": "Configure Docker access",
    "image": "Pull the vLLM container image",
    "ray": "Install and start the Ray cluster (systemd)",
    "verify": "Verify the Ray cluster",
}


@dataclass
class PhaseCtx:
    session: AsyncSession
    handle: JobHandle
    head: Node
    worker: Node
    cfg: ClusterConfig
    setting: Setting

    async def log(self, msg: str) -> None:
        await self.handle.log(msg)

    @property
    def nodes(self) -> list[Node]:
        return [self.head, self.worker]

    async def ssh(self, node: Node) -> SSHClient:
        return await ssh_for_node(self.session, node)


# --- individual phases ---------------------------------------------------
async def _phase_prereqs(ctx: PhaseCtx) -> None:
    for node in ctx.nodes:
        await ctx.log(f"[{node.name}] connecting…")
        ssh = await ctx.ssh(node)
        host = (await ssh.run("hostname", check=True)).stdout.strip()
        await ctx.log(f"[{node.name}] reachable, hostname={host}")

        sudo = await ssh.run("id -u", sudo=True)
        if sudo.ok and sudo.stdout.strip() == "0":
            await ctx.log(f"[{node.name}] sudo OK")
        else:
            raise RuntimeError(
                f"[{node.name}] sudo is not working ({sudo.stderr or sudo.stdout}). "
                "Set NOPASSWD sudo or provide the sudo password for this node."
            )

        gpu = await ssh.run(
            "nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | head -1"
        )
        await ctx.log(
            f"[{node.name}] GPU: {gpu.stdout.strip() or 'nvidia-smi not available (will rely on container GPU)'}"
        )

        free = await ssh.run(f"df -h {home_dir(node.ssh_user)} | tail -1")
        await ctx.log(f"[{node.name}] disk: {free.stdout.strip()}")


async def _phase_hosts(ctx: PhaseCtx) -> None:
    entries = [
        f"{ctx.head.lan_ip} {ctx.head.name}",
        f"{ctx.worker.lan_ip} {ctx.worker.name}",
        f"{ctx.head.qsfp_ip} {ctx.head.name}-qsfp",
        f"{ctx.worker.qsfp_ip} {ctx.worker.name}-qsfp",
    ]
    for node in ctx.nodes:
        ssh = await ctx.ssh(node)
        await ctx.log(f"[{node.name}] setting hostname -> {node.name}")
        await ssh.run(f"hostnamectl set-hostname {_q(node.name)}", sudo=True, check=True)
        lines = "\n".join(
            f'grep -qxF {_q(e)} /etc/hosts || echo {_q(e)} >> /etc/hosts' for e in entries
        )
        await ssh.run(lines, sudo=True, check=True, log_cb=ctx.handle.ssh_log_cb())
        await ctx.log(f"[{node.name}] /etc/hosts updated")


async def _phase_network(ctx: PhaseCtx) -> None:
    mask = ctx.cfg.qsfp_netmask
    for node in ctx.nodes:
        ssh = await ctx.ssh(node)
        iface, ip = node.qsfp_iface, node.qsfp_ip
        cidr = f"{ip}/{mask}"
        await ctx.log(f"[{node.name}] configuring {iface} -> {cidr} (no gateway)")
        script = f"""
set -e
IFACE={_q(iface)}; IP={_q(ip)}; CIDR={_q(cidr)}
ip link set "$IFACE" up || true
if ! ip -4 addr show dev "$IFACE" | grep -q "$IP/"; then
  ip addr add "$CIDR" dev "$IFACE" || true
fi
if command -v nmcli >/dev/null 2>&1; then
  # nmcli persistence is best-effort: the temporary `ip addr` above already
  # makes the link work, and ipv6.method 'disabled' is rejected by some
  # NetworkManager versions, so we fall back to 'ignore' and never fail the
  # phase on a persistence hiccup.
  set +e
  if nmcli -t -f NAME con show | grep -qx qsfp-vllm; then
    nmcli con mod qsfp-vllm ipv4.addresses "$CIDR" ipv4.method manual ipv6.method disabled connection.interface-name "$IFACE" \
      || nmcli con mod qsfp-vllm ipv4.addresses "$CIDR" ipv4.method manual ipv6.method ignore connection.interface-name "$IFACE" \
      || echo "WARNING: nmcli persist (mod) failed; temporary IP is active but the config is not persistent across reboot"
  else
    nmcli con add type ethernet ifname "$IFACE" con-name qsfp-vllm ipv4.addresses "$CIDR" ipv4.method manual ipv6.method disabled \
      || nmcli con add type ethernet ifname "$IFACE" con-name qsfp-vllm ipv4.addresses "$CIDR" ipv4.method manual ipv6.method ignore \
      || echo "WARNING: nmcli persist (add) failed; temporary IP is active but the config is not persistent across reboot"
  fi
  nmcli con up qsfp-vllm || true
  set -e
else
  echo "nmcli not found; applied temporary ip only"
fi
ip -4 addr show dev "$IFACE" | sed -n 's/^.*inet /  inet /p'
"""
        await ssh.run(script, sudo=True, check=True, log_cb=ctx.handle.ssh_log_cb())

    # verify both ways now that both ends are configured
    await _verify_qsfp(ctx, fail=False)


async def _verify_qsfp(ctx: PhaseCtx, fail: bool) -> bool:
    head_ssh = await ctx.ssh(ctx.head)
    worker_ssh = await ctx.ssh(ctx.worker)
    ok = True
    r1 = await head_ssh.run(f"ping -c 2 -W 2 {ctx.worker.qsfp_ip}")
    await ctx.log(f"[{ctx.head.name}] ping {ctx.worker.qsfp_ip}: {'OK' if r1.ok else 'FAILED'}")
    r2 = await worker_ssh.run(f"ping -c 2 -W 2 {ctx.head.qsfp_ip}")
    await ctx.log(f"[{ctx.worker.name}] ping {ctx.head.qsfp_ip}: {'OK' if r2.ok else 'FAILED'}")
    ok = r1.ok and r2.ok
    if not ok and fail:
        raise RuntimeError("QSFP connectivity check failed in both/one direction.")
    return ok


async def _phase_ssh(ctx: PhaseCtx) -> None:
    head_ssh = await ctx.ssh(ctx.head)
    worker_ssh = await ctx.ssh(ctx.worker)
    key = "~/.ssh/id_ed25519_spark"

    await ctx.log("[head] ensuring ~/.ssh and inter-node key")
    await head_ssh.run(
        f"""
mkdir -p ~/.ssh && chmod 700 ~/.ssh
if [ ! -f {key} ]; then
  ssh-keygen -t ed25519 -a 100 -N '' -f {key} -C spark-vllm
fi
""",
        check=True,
        log_cb=ctx.handle.ssh_log_cb(),
    )
    pub = (await head_ssh.run(f"cat {key}.pub", check=True)).stdout.strip()
    await ctx.log("[worker] installing head public key into authorized_keys")
    await worker_ssh.run(
        f"""
mkdir -p ~/.ssh && chmod 700 ~/.ssh
touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
grep -qxF {_q(pub)} ~/.ssh/authorized_keys || echo {_q(pub)} >> ~/.ssh/authorized_keys
""",
        check=True,
    )

    cfg_block = (
        f"Host {ctx.worker.name}\n"
        f"  HostName {ctx.worker.lan_ip}\n"
        f"  User {ctx.worker.ssh_user}\n"
        f"  IdentityFile {key}\n"
        f"  StrictHostKeyChecking accept-new\n"
    )
    await head_ssh.run(
        f"""
touch ~/.ssh/config && chmod 600 ~/.ssh/config
if ! grep -q {_q('Host ' + ctx.worker.name)} ~/.ssh/config; then
  printf '%s' {_q(cfg_block)} >> ~/.ssh/config
fi
""",
        check=True,
    )
    test = await head_ssh.run(
        f"ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new {_q(ctx.worker.name)} hostname"
    )
    if test.ok:
        await ctx.log(f"[head] passwordless ssh to {ctx.worker.name} OK ({test.stdout.strip()})")
    else:
        raise RuntimeError(f"[head] passwordless ssh to worker failed: {test.stderr or test.stdout}")


async def _phase_packages(ctx: PhaseCtx) -> None:
    pkgs = "tmux screen curl wget git rsync jq htop iftop net-tools python3-pip"
    for node in ctx.nodes:
        ssh = await ctx.ssh(node)
        await ctx.log(f"[{node.name}] apt-get update && install ({pkgs})")
        await ssh.run(
            f"export DEBIAN_FRONTEND=noninteractive; apt-get update -y && apt-get install -y {pkgs}",
            sudo=True,
            check=True,
            timeout=900,
            log_cb=ctx.handle.ssh_log_cb(),
        )
        await ssh.run("mkdir -p ~/.ssh ~/.cache/huggingface ~/models && chmod 700 ~/.ssh", check=True)
        await ctx.log(f"[{node.name}] base directories ensured")


async def _phase_docker(ctx: PhaseCtx) -> None:
    for node in ctx.nodes:
        ssh = await ctx.ssh(node)
        has = await ssh.run("command -v docker")
        if not has.ok:
            await ctx.log(
                f"[{node.name}] WARNING: docker not found. DGX OS ships Docker + the "
                "NVIDIA container toolkit; install them before continuing."
            )
            continue
        await ssh.run(
            f"groupadd docker 2>/dev/null || true; usermod -aG docker {node.ssh_user}",
            sudo=True,
            check=True,
        )
        ver = await nodeops.docker(ssh, "version --format '{{.Server.Version}}'")
        await ctx.log(
            f"[{node.name}] docker server {ver.stdout.strip() or 'reachable (via sudo)'} ; "
            f"user added to docker group (re-login applies)"
        )


async def _phase_image(ctx: PhaseCtx) -> None:
    image = ctx.cfg.vllm_image
    for node in ctx.nodes:
        ssh = await ctx.ssh(node)
        await ctx.log(f"[{node.name}] docker pull {image} (this can take a while)…")
        await nodeops.docker(
            ssh, f"pull {image}", check=True, timeout=3600, log_cb=ctx.handle.ssh_log_cb()
        )
        inspect = await nodeops.docker(ssh, f"image inspect {image} >/dev/null 2>&1; echo $?")
        await ctx.log(f"[{node.name}] image present: {inspect.stdout.strip() == '0'}")


async def _phase_ray(ctx: PhaseCtx) -> None:
    settings = get_settings()
    install_dir = settings.node_install_dir
    image = ctx.cfg.vllm_image
    shm = ctx.cfg.shm_size

    head_ssh = await ctx.ssh(ctx.head)
    worker_ssh = await ctx.ssh(ctx.worker)

    head_hf = hf_cache_host_path(ctx.head, ctx.cfg)
    head_models = models_host_dir(ctx.head, ctx.cfg)
    worker_hf = hf_cache_host_path(ctx.worker, ctx.cfg)
    worker_models = models_host_dir(ctx.worker, ctx.cfg)

    await head_ssh.run(f"mkdir -p {_q(head_hf)} {_q(head_models)}", check=True)
    await worker_ssh.run(f"mkdir -p {_q(worker_hf)} {_q(worker_models)}", check=True)

    # Head
    head_script = templates.render_ray_head_script(
        image=image, hf_home=head_hf, models_dir=head_models, head_qsfp=ctx.head.qsfp_ip,
        iface=ctx.head.qsfp_iface, ray_port=ctx.cfg.ray_port, shm=shm,
        dashboard_port=settings.ray_dashboard_port,
    )
    await nodeops.install_file(head_ssh, f"{install_dir}/ray-head.sh", head_script, mode="755")
    head_unit = templates.render_ray_unit(
        role="head", script_path=f"{install_dir}/ray-head.sh",
        container=templates.RAY_HEAD_CONTAINER,
    )
    await ctx.log("[head] installing + starting spark-ray-head.service")
    await nodeops.install_systemd_unit(
        head_ssh, templates.ray_unit_name("head"), head_unit, log_cb=ctx.handle.ssh_log_cb()
    )

    # Worker
    worker_script = templates.render_ray_worker_script(
        image=image, hf_home=worker_hf, models_dir=worker_models, head_qsfp=ctx.head.qsfp_ip,
        worker_qsfp=ctx.worker.qsfp_ip, iface=ctx.worker.qsfp_iface, ray_port=ctx.cfg.ray_port,
        shm=shm,
    )
    await nodeops.install_file(worker_ssh, f"{install_dir}/ray-worker.sh", worker_script, mode="755")
    worker_unit = templates.render_ray_unit(
        role="worker", script_path=f"{install_dir}/ray-worker.sh",
        container=templates.RAY_WORKER_CONTAINER,
    )
    await ctx.log("[worker] installing + starting spark-ray-worker.service")
    await nodeops.install_systemd_unit(
        worker_ssh, templates.ray_unit_name("worker"), worker_unit, log_cb=ctx.handle.ssh_log_cb()
    )
    await ctx.log(
        "Ray containers are starting (each installs ray[default] first; allow ~1 minute). "
        "Run the verify phase to confirm both nodes joined."
    )


async def _phase_verify(ctx: PhaseCtx) -> None:
    import asyncio

    await _verify_qsfp(ctx, fail=False)
    head_ssh = await ctx.ssh(ctx.head)
    await ctx.log("Waiting for Ray to report 2 nodes…")
    ok = False
    for attempt in range(20):
        res = await nodeops.docker(head_ssh, f"exec {templates.RAY_HEAD_CONTAINER} ray status")
        if res.ok and _ray_node_count(res.stdout) >= 2:
            await ctx.log(res.stdout.strip())
            ok = True
            break
        await ctx.log(f"  …not ready yet (attempt {attempt + 1}/20)")
        await asyncio.sleep(6)

    ctx.setting.setup_complete = ok
    await ctx.session.commit()
    if ok:
        await ctx.log("Ray cluster healthy: 2 nodes joined. ✅")
    else:
        await ctx.log(
            "Ray did not report 2 nodes yet. Check the Ray service logs on both nodes "
            "(journalctl -u spark-ray-head / spark-ray-worker) and QSFP connectivity."
        )


def _ray_node_count(ray_status_output: str) -> int:
    """Count distinct alive nodes from `ray status` output by matching the
    ``node_<hash>`` ids listed under the Active section."""
    import re

    return len(set(re.findall(r"node_[0-9a-f]{8,}", ray_status_output)))


PHASE_FUNCS = {
    "prereqs": _phase_prereqs,
    "hosts": _phase_hosts,
    "network": _phase_network,
    "ssh": _phase_ssh,
    "packages": _phase_packages,
    "docker": _phase_docker,
    "image": _phase_image,
    "ray": _phase_ray,
    "verify": _phase_verify,
}


async def run_phase(ctx: PhaseCtx, name: str) -> None:
    func = PHASE_FUNCS.get(name)
    if func is None:
        raise ValueError(f"Unknown phase: {name}")
    await func(ctx)


def _q(s: str) -> str:
    import shlex

    return shlex.quote(s)
