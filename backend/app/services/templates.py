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

import json
import re
import shlex

RAY_HEAD_CONTAINER = "spark-ray-head"
RAY_WORKER_CONTAINER = "spark-ray-worker"


def instance_container(name: str) -> str:
    return f"spark-vllm-{name}"


def distributed_worker_container(name: str) -> str:
    return f"spark-vllm-{name}-worker"


def ray_unit_name(role: str) -> str:
    return f"spark-ray-{role}.service"  # spark-ray-head.service / spark-ray-worker.service


def instance_unit_name(name: str) -> str:
    return f"spark-vllm-{name}.service"


def distributed_worker_unit_name(name: str) -> str:
    return f"spark-vllm-{name}-worker.service"


def tls_container(name: str) -> str:
    return f"spark-vllm-{name}-tls"


def tls_unit_name(name: str) -> str:
    return f"spark-vllm-{name}-tls.service"


# Every `docker run` below overrides the image entrypoint with `--entrypoint bash`
# and passes the script via `-c`/`-lc`. The vLLM images differ: the NGC image
# (nvcr.io/nvidia/vllm) uses an entrypoint that execs its args, but the Docker Hub
# `vllm/vllm-openai` image has ENTRYPOINT ["vllm","serve"], so a bare
# `<image> bash -c '…'` gets parsed as *arguments to vllm serve* and the container
# never runs our command. Overriding the entrypoint makes the launch image-agnostic
# (this matches the model-download path in models_svc.py).
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
  --entrypoint bash \\
  {shlex.quote(image)} \\
  -c {shlex.quote(
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
  --entrypoint bash \\
  {shlex.quote(image)} \\
  -c {shlex.quote(
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


def parse_served_model_names(served_model_names: str | None) -> list[str]:
    """Split the stored ``served_model_names`` blob (space/newline-separated
    aliases) into a clean, ordered list. Empty/None -> []."""
    if not served_model_names:
        return []
    return [tok for tok in re.split(r"\s+", served_model_names.strip()) if tok]


def parse_advanced_args(advanced_args: str | None) -> list[tuple[str, str | None]]:
    """Decode the structured ``advanced_args`` JSON blob into ``(flag, value)``
    tuples (value ``None`` = a boolean flag). Malformed entries are skipped —
    the payload is validated at the API boundary, this is defensive only."""
    if not advanced_args:
        return []
    try:
        data = json.loads(advanced_args)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    out: list[tuple[str, str | None]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        flag = item.get("flag")
        if not isinstance(flag, str) or not flag:
            continue
        value = item.get("value")
        out.append((flag, None if value is None else str(value)))
    return out


def build_vllm_serve_cmd(
    *, model_container_path: str, served_model_name: str | None = None,
    served_model_names: str | None = None, port: int,
    tensor_parallel_size: int,
    distributed_backend: str | None = None, max_model_len: int | None = None,
    gpu_memory_utilization: float, max_num_seqs: int | None = None,
    max_num_batched_tokens: int | None = None, dtype: str | None = None,
    kv_cache_dtype: str | None = None, block_size: int | None = None,
    tokenizer_mode: str | None = None, reasoning_parser: str | None = None,
    trust_remote_code: bool = False, enable_tool_choice: bool = False,
    tool_parser: str | None = None, compilation_config: str | None = None,
    advanced_args: str | None = None, api_key: str | None = None,
    extra_args: str | None = None,
    distributed: dict | None = None,
    api_host: str = "0.0.0.0",
) -> str:
    parts = [
        "vllm serve", shlex.quote(model_container_path),
        f"--host {shlex.quote(api_host)}", f"--port {port}",
        f"--tensor-parallel-size {tensor_parallel_size}",
    ]
    # Ray (cluster) / mp (single) need an explicit backend; native distributed
    # leaves it unset so vLLM auto-selects across the torch.distributed group.
    if distributed_backend:
        parts.append(f"--distributed-executor-backend {distributed_backend}")
    parts.append(f"--gpu-memory-utilization {gpu_memory_utilization}")
    # Serve under one or more clean aliases instead of the raw "/models/<name>"
    # container path, so API clients use a tidy model id. Prefer the explicit
    # alias list; else fall back to the single registry name. Skip if the user
    # already set one via extra_args (let theirs win).
    names = parse_served_model_names(served_model_names)
    if not names and served_model_name:
        names = [served_model_name]
    if names and not (extra_args and "--served-model-name" in extra_args):
        parts.append("--served-model-name " + " ".join(shlex.quote(n) for n in names))
    if max_model_len:
        parts.append(f"--max-model-len {max_model_len}")
    if max_num_seqs:
        parts.append(f"--max-num-seqs {max_num_seqs}")
    if max_num_batched_tokens:
        parts.append(f"--max-num-batched-tokens {max_num_batched_tokens}")
    if dtype:
        parts.append(f"--dtype {shlex.quote(dtype)}")
    if kv_cache_dtype:
        parts.append(f"--kv-cache-dtype {shlex.quote(kv_cache_dtype)}")
    if block_size:
        parts.append(f"--block-size {block_size}")
    if tokenizer_mode:
        parts.append(f"--tokenizer-mode {shlex.quote(tokenizer_mode)}")
    if reasoning_parser:
        parts.append(f"--reasoning-parser {shlex.quote(reasoning_parser)}")
    if trust_remote_code:
        parts.append("--trust-remote-code")
    if enable_tool_choice and tool_parser:
        parts.append("--enable-auto-tool-choice")
        parts.append(f"--tool-call-parser {shlex.quote(tool_parser)}")
    # `--compilation-config` takes a single JSON argument; quote it so the whole
    # object survives as ONE token through the inner `bash -lc`.
    if compilation_config:
        parts.append(f"--compilation-config {shlex.quote(compilation_config)}")
    # Structured passthrough — each row is `--flag [value]`, individually quoted.
    for flag, value in parse_advanced_args(advanced_args):
        if value is None:
            parts.append(shlex.quote(flag))
        else:
            parts.append(f"{shlex.quote(flag)} {shlex.quote(value)}")
    if api_key:
        parts.append(f"--api-key {shlex.quote(api_key)}")
    if extra_args:
        # Tokenize + quote so this passthrough can only add CLI args, never
        # inject shell syntax into the inner `bash -lc`.
        parts.extend(shlex.quote(tok) for tok in shlex.split(extra_args))
    # Native torch.distributed multi-node rendezvous. `--headless` runs a
    # worker with no API server (rank >= 1); rank 0 serves the OpenAI API.
    if distributed:
        parts.append(f"--nnodes {distributed['nnodes']}")
        parts.append(f"--node-rank {distributed['node_rank']}")
        parts.append(f"--master-addr {shlex.quote(str(distributed['master_addr']))}")
        parts.append(f"--master-port {distributed['master_port']}")
        if distributed.get("headless"):
            parts.append("--headless")
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
  --entrypoint bash \\
  {shlex.quote(image)} \\
  -lc {shlex.quote(serve_cmd)}
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


def render_instance_docker_run_distributed(
    *, name: str, role: str, image: str, hf_home: str, models_dir: str, shm: str,
    iface: str, host_qsfp: str, master_addr: str, serve_cmd: str,
) -> str:
    """Docker-run wrapper for one node of a native torch.distributed launch.

    Modeled on :func:`render_instance_docker_run_single`, but pins NCCL/Gloo/UCX
    traffic to the QSFP interface (like the Ray scripts) so the tensor-parallel
    all-reduce crosses the fast interconnect, and sets ``VLLM_HOST_IP`` /
    ``MASTER_ADDR`` for the rendezvous. ``role`` is ``head`` (rank 0, serves the
    API) or ``worker`` (rank >= 1, ``--headless``)."""
    container = instance_container(name) if role == "head" else distributed_worker_container(name)
    return f"""#!/usr/bin/env bash
set -euo pipefail
docker rm -f {container} >/dev/null 2>&1 || true
exec docker run --rm --name {container} \\
  --network host --shm-size {shm} --gpus all \\
  --ulimit memlock=-1 --ulimit stack=67108864 \\
  -v {shlex.quote(hf_home)}:/root/.cache/huggingface \\
  -v {shlex.quote(models_dir)}:/models \\
  -e VLLM_HOST_IP={shlex.quote(host_qsfp)} \\
  -e MASTER_ADDR={shlex.quote(master_addr)} \\
{_net_env_flags(iface)}
  --entrypoint bash \\
  {shlex.quote(image)} \\
  -lc {shlex.quote(serve_cmd)}
"""


def render_instance_unit_distributed_head(*, name: str, script_path: str) -> str:
    """systemd unit for the distributed head (rank 0, serves the OpenAI API)."""
    container = instance_container(name)
    return f"""[Unit]
Description=Spark Control Plane - vLLM instance {name} (distributed head)
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


def render_instance_unit_distributed_worker(*, name: str, script_path: str) -> str:
    """systemd unit for a distributed worker (rank >= 1, ``--headless``)."""
    container = distributed_worker_container(name)
    return f"""[Unit]
Description=Spark Control Plane - vLLM instance {name} (distributed worker)
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


# --- Optional TLS reverse proxy (nginx sidecar) --------------------------------
#
# When an instance has TLS enabled, an nginx container runs on the API-serving
# node (single / distributed head), terminating HTTPS on ``tls_port`` and
# reverse-proxying to vLLM on the instance ``port`` (plain HTTP, same host). vLLM
# itself is unchanged — this is purely additive, so the cert can be rotated
# (rewrite files + ``nginx -s reload``) without restarting the multi-minute model
# load. Everything runs on ``--network host`` (like the vLLM container), so the
# proxy reaches vLLM at 127.0.0.1:<port>.

TLS_CERT_FILE = "cert.pem"
TLS_KEY_FILE = "key.pem"
TLS_CONF_FILE = "nginx.conf"


def tls_dir(install_dir: str, name: str) -> str:
    """Per-instance directory on the node holding the cert, key, and nginx.conf."""
    return f"{install_dir}/tls/{name}"


def render_tls_nginx_conf(*, upstream_port: int, tls_port: int, cert_path: str, key_path: str) -> str:
    """nginx.conf for the TLS sidecar. Streaming-safe for OpenAI SSE responses:
    ``proxy_buffering off`` + long read timeout so tokens flush as they arrive,
    HTTP/1.1 upstream, and no request-body size cap (long prompts)."""
    return f"""worker_processes auto;
events {{ worker_connections 1024; }}
http {{
  access_log /dev/stdout;
  error_log  /dev/stderr;
  upstream vllm_backend {{ server 127.0.0.1:{upstream_port}; }}
  server {{
    listen {tls_port} ssl;
    http2 on;
    ssl_certificate     {cert_path};
    ssl_certificate_key {key_path};
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;
    client_max_body_size 0;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
    location / {{
      proxy_pass http://vllm_backend;
      proxy_http_version 1.1;
      proxy_set_header Host $host;
      proxy_set_header X-Real-IP $remote_addr;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto https;
      proxy_buffering off;
      proxy_cache off;
      chunked_transfer_encoding on;
    }}
  }}
}}
"""


def render_tls_docker_run(*, name: str, image: str, tls_port: int, conf_dir: str) -> str:
    """Docker-run wrapper for the nginx TLS sidecar. Host network so it can bind
    ``tls_port`` and reach vLLM on 127.0.0.1. ``conf_dir`` (holding nginx.conf +
    cert.pem + key.pem) is mounted read-only at the same path inside."""
    container = tls_container(name)
    return f"""#!/usr/bin/env bash
set -euo pipefail
docker rm -f {container} >/dev/null 2>&1 || true
exec docker run --rm --name {container} \\
  --network host \\
  -v {shlex.quote(conf_dir)}:{shlex.quote(conf_dir)}:ro \\
  {shlex.quote(image)} \\
  nginx -c {shlex.quote(f"{conf_dir}/{TLS_CONF_FILE}")} -g 'daemon off;'
"""


def render_tls_unit(*, name: str, script_path: str) -> str:
    """systemd unit for the nginx TLS sidecar. Ordered after the vLLM unit so the
    backend exists before the proxy accepts traffic."""
    container = tls_container(name)
    vllm_unit = instance_unit_name(name)
    return f"""[Unit]
Description=Spark Control Plane - vLLM instance {name} (TLS proxy)
After=docker.service network-online.target {vllm_unit}
Wants=network-online.target {vllm_unit}
Requires=docker.service

[Service]
Type=simple
ExecStart={script_path}
ExecStop=/usr/bin/docker stop {container}
ExecStopPost=-/usr/bin/docker rm -f {container}
Restart=on-failure
RestartSec=10
TimeoutStartSec=0
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
"""
