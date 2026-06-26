"""Renderers for the scripts, systemd units, and vllm commands installed on the
nodes. Container names are deterministic so we can ``docker exec`` / stop them
reliably:

* ``spark-ray-head``   — Ray head container (also hosts cluster vLLM instances)
* ``spark-ray-worker`` — Ray worker container
* ``spark-vllm-<name>``— standalone single-node vLLM instance container

The Ray launch replicates NVIDIA's run_cluster.sh (pinned commit) including the
``pip install ray[default]`` patch from the runbook, forcing Ray/NCCL/UCX/Gloo
traffic over the QSFP interface.
"""

from __future__ import annotations

import shlex

RAY_HEAD_CONTAINER = "spark-ray-head"
RAY_WORKER_CONTAINER = "spark-ray-worker"


def instance_container(name: str) -> str:
    return f"spark-vllm-{name}"


def ray_unit_name(role: str) -> str:
    return f"spark-ray-{role}.service"  # spark-ray-head.service / spark-ray-worker.service


def instance_unit_name(name: str) -> str:
    return f"spark-vllm-{name}.service"


_RAY_INSTALL = (
    "pip install -q --root-user-action=ignore 'ray[default]>=2.9'"
)

_NET_ENVS = [
    ("UCX_NET_DEVICES", "{iface}"),
    ("NCCL_SOCKET_IFNAME", "{iface}"),
    ("OMPI_MCA_btl_tcp_if_include", "{iface}"),
    ("GLOO_SOCKET_IFNAME", "{iface}"),
    ("TP_SOCKET_IFNAME", "{iface}"),
    ("RAY_memory_monitor_refresh_ms", "0"),
]


def _net_env_flags(iface: str) -> str:
    parts = []
    for key, val in _NET_ENVS:
        parts.append(f'  -e {key}={shlex.quote(val.format(iface=iface))} \\')
    return "\n".join(parts)


def render_ray_head_script(
    *, image: str, hf_home: str, models_dir: str, head_qsfp: str, iface: str,
    ray_port: int, shm: str, dashboard_port: int,
) -> str:
    return f"""#!/usr/bin/env bash
set -euo pipefail
docker rm -f {RAY_HEAD_CONTAINER} >/dev/null 2>&1 || true
exec docker run --rm --name {RAY_HEAD_CONTAINER} \\
  --network host --shm-size {shm} --gpus all \\
  --ulimit memlock=-1 --ulimit stack=67108864 \\
  -v {shlex.quote(hf_home)}:/root/.cache/huggingface \\
  -v {shlex.quote(models_dir)}:/models \\
  -e VLLM_HOST_IP={shlex.quote(head_qsfp)} \\
  -e MASTER_ADDR={shlex.quote(head_qsfp)} \\
{_net_env_flags(iface)}
  {shlex.quote(image)} \\
  bash -c {shlex.quote(
      f"{_RAY_INSTALL} && ray start --block --head "
      f"--node-ip-address={head_qsfp} --port={ray_port} --dashboard-host=0.0.0.0 "
      f"--dashboard-port={dashboard_port}"
  )}
"""


def render_ray_worker_script(
    *, image: str, hf_home: str, models_dir: str, head_qsfp: str, worker_qsfp: str,
    iface: str, ray_port: int, shm: str,
) -> str:
    return f"""#!/usr/bin/env bash
set -euo pipefail
docker rm -f {RAY_WORKER_CONTAINER} >/dev/null 2>&1 || true
exec docker run --rm --name {RAY_WORKER_CONTAINER} \\
  --network host --shm-size {shm} --gpus all \\
  --ulimit memlock=-1 --ulimit stack=67108864 \\
  -v {shlex.quote(hf_home)}:/root/.cache/huggingface \\
  -v {shlex.quote(models_dir)}:/models \\
  -e VLLM_HOST_IP={shlex.quote(worker_qsfp)} \\
  -e MASTER_ADDR={shlex.quote(head_qsfp)} \\
{_net_env_flags(iface)}
  {shlex.quote(image)} \\
  bash -c {shlex.quote(
      f"{_RAY_INSTALL} && ray start --block "
      f"--address={head_qsfp}:{ray_port} --node-ip-address={worker_qsfp}"
  )}
"""


def render_ray_unit(*, role: str, script_path: str, container: str) -> str:
    return f"""[Unit]
Description=Spark Control Plane - Ray {role}
After=docker.service network-online.target
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
ExecStart={script_path}
ExecStop=/usr/bin/docker stop {container}
ExecStopPost=-/usr/bin/docker rm -f {container}
Restart=on-failure
RestartSec=5
TimeoutStartSec=0
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
"""


def build_vllm_serve_cmd(
    *, model_container_path: str, port: int, tensor_parallel_size: int,
    distributed_backend: str, max_model_len: int | None, gpu_memory_utilization: float,
    max_num_seqs: int | None, dtype: str | None, enable_tool_choice: bool,
    tool_parser: str | None, api_key: str | None, extra_args: str | None,
) -> str:
    parts = [
        "vllm serve", shlex.quote(model_container_path),
        "--host 0.0.0.0", f"--port {port}",
        f"--tensor-parallel-size {tensor_parallel_size}",
        f"--distributed-executor-backend {distributed_backend}",
        f"--gpu-memory-utilization {gpu_memory_utilization}",
    ]
    if max_model_len:
        parts.append(f"--max-model-len {max_model_len}")
    if max_num_seqs:
        parts.append(f"--max-num-seqs {max_num_seqs}")
    if dtype:
        parts.append(f"--dtype {shlex.quote(dtype)}")
    if enable_tool_choice and tool_parser:
        parts.append("--enable-auto-tool-choice")
        parts.append(f"--tool-call-parser {shlex.quote(tool_parser)}")
    if api_key:
        parts.append(f"--api-key {shlex.quote(api_key)}")
    if extra_args:
        # Tokenize + quote so this passthrough can only add CLI args, never
        # inject shell syntax into the inner `bash -lc`.
        parts.extend(shlex.quote(tok) for tok in shlex.split(extra_args))
    return " ".join(parts)


def render_instance_unit_cluster(*, name: str, serve_cmd: str, port: int) -> str:
    pat = f"vllm serve.*--port {port}"
    # SIGTERM, grace, then SIGKILL — killing the `docker exec` client does NOT
    # stop the in-container process, so ExecStop must do the cleanup itself.
    stop_cmd = (
        f"pkill -TERM -f {shlex.quote(pat)} || true; sleep 5; "
        f"pkill -KILL -f {shlex.quote(pat)} || true"
    )
    # BindsTo/PartOf so a head-container restart restarts this instance too; an
    # ExecStartPre gate waits (bounded) for Ray to be ready before serving.
    ready = "for i in $(seq 1 60); do ray status >/dev/null 2>&1 && exit 0; sleep 2; done; exit 0"
    return f"""[Unit]
Description=Spark Control Plane - vLLM instance {name} (cluster TP)
After={ray_unit_name('head')} docker.service
Requires={ray_unit_name('head')}
BindsTo={ray_unit_name('head')}
PartOf={ray_unit_name('head')}

[Service]
Type=simple
ExecStartPre=-/usr/bin/docker exec {RAY_HEAD_CONTAINER} bash -lc {shlex.quote(ready)}
ExecStart=/usr/bin/docker exec {RAY_HEAD_CONTAINER} bash -lc {shlex.quote(serve_cmd)}
ExecStop=-/usr/bin/docker exec {RAY_HEAD_CONTAINER} bash -lc {shlex.quote(stop_cmd)}
Restart=always
RestartSec=10
TimeoutStartSec=0
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
"""


def render_instance_docker_run_single(
    *, name: str, image: str, hf_home: str, models_dir: str, shm: str, serve_cmd: str,
) -> str:
    container = instance_container(name)
    return f"""#!/usr/bin/env bash
set -euo pipefail
docker rm -f {container} >/dev/null 2>&1 || true
exec docker run --rm --name {container} \\
  --network host --shm-size {shm} --gpus all \\
  --ulimit memlock=-1 --ulimit stack=67108864 \\
  -v {shlex.quote(hf_home)}:/root/.cache/huggingface \\
  -v {shlex.quote(models_dir)}:/models \\
  {shlex.quote(image)} \\
  bash -lc {shlex.quote(serve_cmd)}
"""


def render_instance_unit_single(*, name: str, script_path: str) -> str:
    container = instance_container(name)
    return f"""[Unit]
Description=Spark Control Plane - vLLM instance {name} (single node)
After=docker.service network-online.target
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
ExecStart={script_path}
ExecStop=/usr/bin/docker stop {container}
ExecStopPost=-/usr/bin/docker rm -f {container}
Restart=on-failure
RestartSec=10
TimeoutStartSec=0
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
"""
