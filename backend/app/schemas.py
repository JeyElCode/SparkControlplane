"""Pydantic request/response schemas for the HTTP API.

Secrets are accepted on input but never serialized back out — instead the
``has_*`` booleans tell the UI whether a secret is stored.
"""

from __future__ import annotations

import ipaddress
import json
import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import models as m

# --- Shared input validators ---------------------------------------------
# These identifiers end up interpolated into remote shell scripts, systemd unit
# names, and docker container names. Validate them strictly at the API boundary
# so unsafe characters can never reach the SSH layer.
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")  # single RFC-1123 label, no dots
_IFACE_RE = re.compile(r"^[A-Za-z0-9._-]{1,15}$")
_INSTANCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,61}$")
_MODELNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*$")


def _v_hostname(v: str) -> str:
    if not _HOSTNAME_RE.match(v):
        raise ValueError("must be a valid hostname label (letters, digits, hyphen; no dots; 1–63 chars)")
    return v


def _v_ip(v: str) -> str:
    try:
        ipaddress.ip_address(v)
    except ValueError:
        raise ValueError(f"'{v}' is not a valid IP address")
    return v


def _v_iface(v: str) -> str:
    if not _IFACE_RE.match(v):
        raise ValueError("invalid network interface name (letters, digits, . _ -; 1–15 chars)")
    return v


def _v_instance_name(v: str) -> str:
    if not _INSTANCE_RE.match(v):
        raise ValueError("name must start alphanumeric and contain only letters, digits, . _ - (≤62 chars)")
    return v


def _v_model_name(v: str) -> str:
    if not _MODELNAME_RE.match(v):
        raise ValueError("model name must start alphanumeric and contain only letters, digits, . _ -")
    return v


def _v_repo_id(v: str) -> str:
    if not _REPO_RE.match(v):
        raise ValueError("invalid HuggingFace repo id")
    return v


def _v_compilation_config(v: str | None) -> str | None:
    """``--compilation-config`` is passed to vLLM as a single JSON argument, so
    it must parse as JSON. Empty/None is allowed (flag omitted)."""
    if v is None or v.strip() == "":
        return v
    try:
        json.loads(v)
    except (ValueError, TypeError):
        raise ValueError("compilation_config must be valid JSON")
    return v


def _v_advanced_args(v: str | None) -> str | None:
    """Structured passthrough: a JSON array of ``{"flag": "--x", "value": ...}``
    objects (``value`` null = a boolean flag). Empty/None is allowed."""
    if v is None or v.strip() == "":
        return v
    try:
        data = json.loads(v)
    except (ValueError, TypeError):
        raise ValueError("advanced_args must be valid JSON")
    if not isinstance(data, list):
        raise ValueError("advanced_args must be a JSON array of {flag, value} objects")
    for item in data:
        if not isinstance(item, dict) or "flag" not in item:
            raise ValueError('each advanced_args item must be an object with a "flag" key')
        flag = item["flag"]
        if not isinstance(flag, str) or not flag.startswith("-"):
            raise ValueError('advanced_args "flag" must be a string starting with "-"')
        value = item.get("value")
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise ValueError('advanced_args "value" must be a scalar (str/number/bool) or null')
    return v


# --- Telemetry -----------------------------------------------------------
class NetRate(BaseModel):
    """Live throughput of one interface (computed from /proc/net/dev deltas)."""

    iface: str
    kind: Literal["qsfp", "lan", "other"] = "other"
    rx_bps: float | None = None
    tx_bps: float | None = None


class DiskUsage(BaseModel):
    """Filesystem usage of the models directory on a node."""

    path: str
    total_bytes: int | None = None
    used_bytes: int | None = None
    free_bytes: int | None = None


class GpuProc(BaseModel):
    """A process currently using the GPU (top consumers first)."""

    pid: int
    name: str
    mem_mib: int | None = None


class XidEvent(BaseModel):
    """A GPU XID error observed in the node's kernel journal."""

    ts: float
    xid: int | None = None
    message: str


class HistoryPoint(BaseModel):
    """One compact telemetry sample for sparklines (all optional — a metric can
    be momentarily unavailable without dropping the point)."""

    ts: float  # unix seconds
    cpu_pct: float | None = None
    mem_used_mib: int | None = None
    gpu_util_pct: int | None = None
    gpu_mem_used_mib: int | None = None
    qsfp_rx_bps: float | None = None
    qsfp_tx_bps: float | None = None
    lan_rx_bps: float | None = None
    lan_tx_bps: float | None = None
    disk_used_bytes: int | None = None


class NodeHistory(BaseModel):
    node_id: int
    name: str
    points: list[HistoryPoint] = []


class InstanceMetrics(BaseModel):
    """Live vLLM serving metrics scraped from the instance's Prometheus
    ``/metrics`` endpoint. Rates derive from counter deltas between scrapes."""

    ts: float
    running: int | None = None          # requests currently decoding
    waiting: int | None = None          # requests queued
    kv_cache_pct: float | None = None   # 0-100
    prompt_tps: float | None = None     # prompt tokens/s (prefill)
    gen_tps: float | None = None        # generation tokens/s (decode)
    req_per_s: float | None = None
    ttft_ms: float | None = None        # mean TTFT over the last window
    e2e_ms: float | None = None         # mean end-to-end latency, last window
    total_generation_tokens: float | None = None
    total_prompt_tokens: float | None = None


class InstanceHistoryPoint(BaseModel):
    ts: float
    gen_tps: float | None = None
    prompt_tps: float | None = None
    running: int | None = None
    waiting: int | None = None
    kv_cache_pct: float | None = None
    ttft_ms: float | None = None


class InstanceHistory(BaseModel):
    instance_id: int
    name: str
    points: list[InstanceHistoryPoint] = []


# --- Nodes ---------------------------------------------------------------
class InterfaceInfo(BaseModel):
    """A physical network port on a node, for the QSFP interface picker."""

    name: str
    operstate: str
    carrier: bool
    speed_mbps: int | None = None
    driver: str | None = None
    mac: str | None = None
    qsfp_candidate: bool = False


class NodeIn(BaseModel):
    role: Literal["head", "worker"]
    name: str
    lan_ip: str
    qsfp_ip: str
    qsfp_iface: str = "enp1s0f1np1"
    ssh_user: str
    ssh_port: int = 22
    auth_method: Literal["password", "key"] = "password"
    ssh_password: str | None = None
    ssh_private_key: str | None = None
    ssh_key_passphrase: str | None = None
    sudo_mode: Literal["nopasswd", "password"] = "password"
    sudo_password: str | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        return _v_hostname(v)

    @field_validator("qsfp_iface")
    @classmethod
    def _check_iface(cls, v: str) -> str:
        return _v_iface(v)

    @field_validator("lan_ip", "qsfp_ip")
    @classmethod
    def _check_ip(cls, v: str) -> str:
        return _v_ip(v)


class NodeUpdate(BaseModel):
    name: str | None = None
    lan_ip: str | None = None
    qsfp_ip: str | None = None
    qsfp_iface: str | None = None
    mac_address: str | None = None
    ssh_user: str | None = None
    ssh_port: int | None = None
    auth_method: Literal["password", "key"] | None = None
    ssh_password: str | None = None
    ssh_private_key: str | None = None
    ssh_key_passphrase: str | None = None
    sudo_mode: Literal["nopasswd", "password"] | None = None
    sudo_password: str | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str | None) -> str | None:
        return None if v is None else _v_hostname(v)

    @field_validator("qsfp_iface")
    @classmethod
    def _check_iface(cls, v: str | None) -> str | None:
        return None if v is None else _v_iface(v)

    @field_validator("lan_ip", "qsfp_ip")
    @classmethod
    def _check_ip(cls, v: str | None) -> str | None:
        return None if v is None else _v_ip(v)

    @field_validator("mac_address")
    @classmethod
    def _check_mac(cls, v: str | None) -> str | None:
        if v is None or v.strip() == "":
            return None
        from .services.power import normalize_mac

        mac = normalize_mac(v)
        if mac is None:
            raise ValueError("mac_address must look like aa:bb:cc:dd:ee:ff")
        return mac


class NodeOut(BaseModel):
    id: int
    role: str
    name: str
    lan_ip: str
    qsfp_ip: str
    qsfp_iface: str
    mac_address: str | None = None
    ssh_user: str
    ssh_port: int
    auth_method: str
    sudo_mode: str
    hardened: bool
    has_ssh_password: bool
    has_ssh_key: bool
    has_sudo_password: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, n: m.Node) -> "NodeOut":
        return cls(
            id=n.id,
            role=n.role,
            name=n.name,
            lan_ip=n.lan_ip,
            qsfp_ip=n.qsfp_ip,
            qsfp_iface=n.qsfp_iface,
            mac_address=n.mac_address,
            ssh_user=n.ssh_user,
            ssh_port=n.ssh_port,
            auth_method=n.auth_method,
            sudo_mode=n.sudo_mode,
            hardened=n.hardened,
            has_ssh_password=bool(n.ssh_password_enc),
            has_ssh_key=bool(n.ssh_private_key_enc),
            has_sudo_password=bool(n.sudo_password_enc),
            created_at=n.created_at,
            updated_at=n.updated_at,
        )


class ConnectionTest(BaseModel):
    ok: bool
    message: str
    hostname: str | None = None
    sudo_ok: bool | None = None
    docker_ok: bool | None = None
    gpu_ok: bool | None = None
    detail: str | None = None


# --- Cluster config / settings ------------------------------------------
class ClusterConfigIn(BaseModel):
    cluster_name: str | None = None
    vllm_image: str | None = None
    qsfp_netmask: int | None = None
    models_subdir: str | None = None
    hf_cache_subdir: str | None = None
    shm_size: str | None = None


class ImageUpdateIn(BaseModel):
    """Cluster-wide vLLM image upgrade request."""

    image: str
    restart_ray: bool = True
    restart_instances: bool = True

    @field_validator("image")
    @classmethod
    def _check_image(cls, v: str) -> str:
        v = v.strip()
        if not v or not re.fullmatch(r"[A-Za-z0-9._/:@-]+", v):
            raise ValueError("image must be a plain container reference (registry/repo:tag)")
        return v


class ClusterConfigOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    cluster_name: str
    vllm_image: str
    qsfp_netmask: int
    models_subdir: str
    hf_cache_subdir: str
    models_container_path: str
    hf_cache_container_path: str
    ray_port: int
    shm_size: str


class SettingsIn(BaseModel):
    hf_token: str | None = None
    status_poll_seconds: int | None = None
    judge_base_url: str | None = None
    judge_model: str | None = None
    judge_api_key: str | None = None  # write-only
    # Alerting: partial threshold overrides (validated/merged server-side) and
    # a write-only webhook URL ("" clears it).
    alerts: dict | None = None
    alert_webhook_url: str | None = None
    # Scheduled S3 backups ("" clears the string fields; secret is write-only)
    backup_enabled: bool | None = None
    backup_s3_endpoint: str | None = None
    backup_s3_bucket: str | None = None
    backup_s3_prefix: str | None = None
    backup_s3_region: str | None = None
    backup_s3_access_key: str | None = None
    backup_s3_secret: str | None = None
    backup_interval_hours: float | None = None
    backup_retention: int | None = None
    gateway_token: str | None = None  # write-only; "" clears


class SettingsOut(BaseModel):
    has_hf_token: bool
    status_poll_seconds: int
    setup_complete: bool
    judge_base_url: str | None = None
    judge_model: str | None = None
    has_judge_api_key: bool = False
    alerts: dict = Field(default_factory=dict)
    has_alert_webhook: bool = False
    backup_enabled: bool = False
    backup_s3_endpoint: str | None = None
    backup_s3_bucket: str | None = None
    backup_s3_prefix: str = "spark-controlplane/"
    backup_s3_region: str = "us-east-1"
    backup_s3_access_key: str | None = None
    has_backup_s3_secret: bool = False
    backup_interval_hours: float = 24.0
    backup_retention: int = 14
    has_gateway_token: bool = False


class ActiveAlert(BaseModel):
    """A currently-firing alert (for dashboard banners)."""

    rule: str
    subject: str
    severity: str = "warn"
    message: str
    since: float | None = None


class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    rule: str
    subject: str
    severity: str
    message: str
    fired_at: datetime
    resolved_at: datetime | None = None


# --- Models --------------------------------------------------------------
class ModelIn(BaseModel):
    repo_id: str
    name: str | None = None
    tool_parser: str | None = None

    @field_validator("repo_id")
    @classmethod
    def _check_repo(cls, v: str) -> str:
        return _v_repo_id(v.strip())

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str | None) -> str | None:
        return None if v is None else _v_model_name(v)


class ModelSuggestion(BaseModel):
    repo_id: str
    label: str
    approx_size_gb: float | None = None
    tool_parser: str | None = None
    note: str | None = None


class ModelNodeStateOut(BaseModel):
    node_id: int
    node_role: str
    node_name: str
    present: bool
    size_bytes: int | None
    checksum_ok: bool | None
    status: str
    progress: float | None = None  # 0..1 while downloading/syncing (live, in-memory)

    @classmethod
    def of(cls, s: m.ModelNodeState) -> "ModelNodeStateOut":
        return cls(
            node_id=s.node_id,
            node_role=s.node.role if s.node else "",
            node_name=s.node.name if s.node else "",
            present=s.present,
            size_bytes=s.size_bytes,
            checksum_ok=s.checksum_ok,
            status=s.status,
        )


class ModelOut(BaseModel):
    id: int
    repo_id: str
    name: str
    tool_parser: str | None
    size_bytes: int | None
    status: str
    notes: str | None
    node_states: list[ModelNodeStateOut]
    created_at: datetime
    active_job_id: int | None = None  # a running download/sync/delete job, if any

    @classmethod
    def of(cls, model: m.ModelRegistry) -> "ModelOut":
        # Lazy import avoids a circular import (models_svc imports schemas).
        from .services.models_svc import get_node_progress

        states = []
        for s in model.node_states:
            ns = ModelNodeStateOut.of(s)
            ns.progress = get_node_progress(model.id, s.node_id)
            states.append(ns)
        return cls(
            id=model.id,
            repo_id=model.repo_id,
            name=model.name,
            tool_parser=model.tool_parser,
            size_bytes=model.size_bytes,
            status=model.status,
            notes=model.notes,
            node_states=states,
            created_at=model.created_at,
        )


# --- Instances -----------------------------------------------------------
class InstanceIn(BaseModel):
    name: str
    model_id: int
    topology: Literal["cluster", "single", "distributed"] = "cluster"

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        return _v_instance_name(v)
    node_id: int | None = None  # required for single
    # None = auto-assign the next free port (clients use the /v1 gateway, so
    # ports are internal plumbing; explicit values are validated for conflicts).
    port: int | None = None
    tensor_parallel_size: int | None = None  # defaulted from topology
    max_model_len: int | None = None
    gpu_memory_utilization: float = 0.85
    max_num_seqs: int | None = None
    max_num_batched_tokens: int | None = None
    dtype: str | None = None
    kv_cache_dtype: str | None = None
    block_size: int | None = None
    tokenizer_mode: str | None = None
    reasoning_parser: str | None = None
    trust_remote_code: bool = False
    enable_tool_choice: bool = True
    tool_parser: str | None = None  # auto-mapped if None and enable_tool_choice
    served_model_names: str | None = None  # space/newline-separated aliases; ≥1 wins
    compilation_config: str | None = None  # JSON string, validated
    advanced_args: str | None = None       # JSON array of {flag, value}
    master_port: int | None = None         # distributed rendezvous port (None = auto)
    extra_args: str | None = None          # legacy raw passthrough
    vllm_image: str | None = None          # per-instance image override (else cluster image)
    api_key: str | None = None

    @field_validator("port", "master_port")
    @classmethod
    def _check_port_range(cls, v: int | None) -> int | None:
        if v is not None and not (1024 <= v <= 65535):
            raise ValueError("ports must be in 1024-65535 (or omitted for auto-assignment)")
        return v
    # First-class TLS: terminate HTTPS on tls_port via an on-node nginx sidecar,
    # proxying to vLLM on `port` (plain HTTP, internal). cert/key are write-only PEM.
    tls_enabled: bool = False
    tls_port: int = 443
    tls_cert: str | None = None            # write-only PEM (fullchain)
    tls_key: str | None = None             # write-only PEM (private key)
    autostart: bool = True

    @field_validator("compilation_config")
    @classmethod
    def _check_compilation_config(cls, v: str | None) -> str | None:
        return _v_compilation_config(v)

    @field_validator("advanced_args")
    @classmethod
    def _check_advanced_args(cls, v: str | None) -> str | None:
        return _v_advanced_args(v)


class TlsReloadIn(BaseModel):
    """New PEM material for an in-place cert rotation (no vLLM restart)."""

    tls_cert: str  # PEM fullchain
    tls_key: str   # PEM private key


class InstanceUpdate(BaseModel):
    port: int | None = None
    max_model_len: int | None = None
    gpu_memory_utilization: float | None = None
    max_num_seqs: int | None = None
    max_num_batched_tokens: int | None = None
    dtype: str | None = None
    kv_cache_dtype: str | None = None
    block_size: int | None = None
    tokenizer_mode: str | None = None
    reasoning_parser: str | None = None
    trust_remote_code: bool | None = None
    enable_tool_choice: bool | None = None
    tool_parser: str | None = None
    served_model_names: str | None = None
    compilation_config: str | None = None
    advanced_args: str | None = None
    master_port: int | None = None
    extra_args: str | None = None
    vllm_image: str | None = None
    tls_enabled: bool | None = None
    tls_port: int | None = None
    tls_cert: str | None = None             # write-only PEM (fullchain)
    tls_key: str | None = None              # write-only PEM (private key)
    autostart: bool | None = None

    @field_validator("compilation_config")
    @classmethod
    def _check_compilation_config(cls, v: str | None) -> str | None:
        return _v_compilation_config(v)

    @field_validator("advanced_args")
    @classmethod
    def _check_advanced_args(cls, v: str | None) -> str | None:
        return _v_advanced_args(v)


class InstanceOut(BaseModel):
    id: int
    name: str
    model_id: int
    model_repo_id: str
    model_name: str
    topology: str
    node_id: int | None
    node_role: str | None
    port: int
    tensor_parallel_size: int
    max_model_len: int | None
    gpu_memory_utilization: float
    max_num_seqs: int | None
    max_num_batched_tokens: int | None
    dtype: str | None
    kv_cache_dtype: str | None
    block_size: int | None
    tokenizer_mode: str | None
    reasoning_parser: str | None
    trust_remote_code: bool
    enable_tool_choice: bool
    tool_parser: str | None
    served_model_names: str | None
    compilation_config: str | None
    advanced_args: str | None
    master_port: int
    extra_args: str | None
    vllm_image: str | None
    has_api_key: bool
    tls_enabled: bool
    tls_port: int
    has_tls_cert: bool
    autostart: bool
    systemd_unit: str | None
    status: str
    last_error: str | None

    @classmethod
    def of(cls, inst: m.Instance) -> "InstanceOut":
        return cls(
            id=inst.id,
            name=inst.name,
            model_id=inst.model_id,
            model_repo_id=inst.model.repo_id if inst.model else "",
            model_name=inst.model.name if inst.model else "",
            topology=inst.topology,
            node_id=inst.node_id,
            node_role=inst.node.role if inst.node else None,
            port=inst.port,
            tensor_parallel_size=inst.tensor_parallel_size,
            max_model_len=inst.max_model_len,
            gpu_memory_utilization=inst.gpu_memory_utilization,
            max_num_seqs=inst.max_num_seqs,
            max_num_batched_tokens=inst.max_num_batched_tokens,
            dtype=inst.dtype,
            kv_cache_dtype=inst.kv_cache_dtype,
            block_size=inst.block_size,
            tokenizer_mode=inst.tokenizer_mode,
            reasoning_parser=inst.reasoning_parser,
            trust_remote_code=inst.trust_remote_code,
            enable_tool_choice=inst.enable_tool_choice,
            tool_parser=inst.tool_parser,
            served_model_names=inst.served_model_names,
            compilation_config=inst.compilation_config,
            advanced_args=inst.advanced_args,
            master_port=inst.master_port,
            extra_args=inst.extra_args,
            vllm_image=inst.vllm_image,
            has_api_key=bool(inst.api_key_enc),
            tls_enabled=inst.tls_enabled,
            tls_port=inst.tls_port,
            has_tls_cert=bool(inst.tls_cert_enc and inst.tls_key_enc),
            autostart=inst.autostart,
            systemd_unit=inst.systemd_unit,
            status=inst.status,
            last_error=inst.last_error,
        )


# --- Jobs ----------------------------------------------------------------
class JobLogOut(BaseModel):
    seq: int
    ts: datetime
    stream: str
    text: str

    @classmethod
    def of(cls, log: m.JobLog) -> "JobLogOut":
        return cls(seq=log.seq, ts=log.ts, stream=log.stream, text=log.text)


class JobOut(BaseModel):
    id: int
    type: str
    title: str
    status: str
    node_id: int | None
    target: str | None
    progress: float | None
    exit_code: int | None
    summary: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime

    @classmethod
    def of(cls, job: m.Job) -> "JobOut":
        return cls(
            id=job.id,
            type=job.type,
            title=job.title,
            status=job.status,
            node_id=job.node_id,
            target=job.target,
            progress=job.progress,
            exit_code=job.exit_code,
            summary=job.summary,
            started_at=job.started_at,
            finished_at=job.finished_at,
            created_at=job.created_at,
        )


class JobDetail(JobOut):
    logs: list[JobLogOut] = Field(default_factory=list)

    @classmethod
    def of_detail(cls, job: m.Job) -> "JobDetail":
        base = JobOut.of(job).model_dump()
        return cls(**base, logs=[JobLogOut.of(log) for log in job.logs])


# --- Setup / teardown ----------------------------------------------------
PhaseName = Literal[
    "prereqs",
    "hosts",
    "network",
    "ssh",
    "packages",
    "docker",
    "image",
    "ray",
    "verify",
]


class SetupRequest(BaseModel):
    phases: list[PhaseName] | None = None  # None = run the full ordered pipeline


class PhaseStatus(BaseModel):
    phase: str
    title: str
    status: Literal["unknown", "ok", "warn", "error", "pending"] = "unknown"
    detail: str | None = None


class TeardownRequest(BaseModel):
    stop_instances: bool = True
    stop_ray: bool = True
    remove_network: bool = False
    remove_inter_node_ssh: bool = False
    remove_hosts_entries: bool = False
    delete_models: bool = False  # off by default — large downloads


# --- Status snapshot -----------------------------------------------------
class GpuStatus(BaseModel):
    index: int
    name: str | None = None
    mem_used_mib: int | None = None
    mem_total_mib: int | None = None
    util_pct: int | None = None
    temp_c: int | None = None
    power_w: float | None = None


class NodeStatus(BaseModel):
    node_id: int
    role: str
    name: str
    reachable: bool
    qsfp_link_ok: bool | None = None
    docker_ok: bool | None = None
    ray_container_up: bool | None = None
    gpus: list[GpuStatus] = Field(default_factory=list)
    # Unified system memory (DGX Spark shares LPDDR5X between CPU and GPU; the
    # GPU's FB memory is N/A in nvidia-smi, so this is the meaningful figure).
    sys_mem_used_mib: int | None = None
    sys_mem_total_mib: int | None = None
    mem_budget_used_gib: float | None = None
    mem_budget_total_gib: float | None = None
    detail: str | None = None
    # Telemetry-engine extras (None until the first sample lands)
    cpu_pct: float | None = None
    cpu_count: int | None = None
    loadavg_1m: float | None = None
    uptime_seconds: float | None = None
    net: list[NetRate] = Field(default_factory=list)
    disk: DiskUsage | None = None
    gpu_procs: list[GpuProc] = Field(default_factory=list)
    sampled_at: float | None = None  # unix seconds of the underlying sample
    gpu_throttle: bool | None = None            # active SW/HW thermal slowdown
    recent_xids: list[XidEvent] = Field(default_factory=list)


class RayNodeInfo(BaseModel):
    address: str
    alive: bool


class RayStatus(BaseModel):
    reachable: bool
    nodes_total: int | None = None
    nodes_alive: int | None = None
    gpus_total: float | None = None
    detail: str | None = None


class InstanceRuntimeStatus(BaseModel):
    instance_id: int
    name: str
    status: str
    systemd_active: bool | None = None
    health_ok: bool | None = None
    served_model: str | None = None
    endpoint: str | None = None
    detail: str | None = None
    metrics: InstanceMetrics | None = None


class StatusSnapshot(BaseModel):
    setup_complete: bool
    qsfp_ok: bool | None = None
    ray: RayStatus
    # Ray is only *required* when a cluster-topology instance exists; with only
    # single/distributed instances a stopped Ray cluster is normal, not a fault.
    ray_required: bool = False
    nodes: list[NodeStatus]
    instances: list[InstanceRuntimeStatus]
    overcommit_warnings: list[str] = Field(default_factory=list)
    active_alerts: list[ActiveAlert] = Field(default_factory=list)
    generated_at: datetime


# --- Playground ----------------------------------------------------------
class PlaygroundRequest(BaseModel):
    instance_id: int
    prompt: str
    system: str | None = None
    max_tokens: int = 256
    temperature: float = 0.7


class PlaygroundResponse(BaseModel):
    ok: bool
    content: str | None = None
    raw: dict[str, Any] | None = None
    error: str | None = None


# --- Generic job-accepted response --------------------------------------
class JobAccepted(BaseModel):
    job_id: int
    message: str


# --- Evaluations ---------------------------------------------------------
class CatalogOut(BaseModel):
    perf_categories: list[str]   # built-in throughput-test categories
    custom_categories: list[str]  # distinct categories of user-authored tasks


_SCORER = Literal["exact", "contains", "numeric", "mcq", "judge", "code_exec", "tool_call"]


class CustomTaskIn(BaseModel):
    category: str
    name: str
    prompt: str
    scorer: _SCORER
    system: str | None = None
    answer: str | None = None
    contains: list[str] = Field(default_factory=list)
    numeric_answer: float | None = None
    numeric_tol: float = 0.01
    choices: list[str] = Field(default_factory=list)
    correct: str | None = None
    rubric: str | None = None
    entry_point: str | None = None
    test_code: str | None = None
    code_prefix: str | None = None
    tools: list[dict] = Field(default_factory=list)
    expected_tool: str | None = None
    expected_args: dict[str, Any] = Field(default_factory=dict)
    forbid_tool_call: bool = False
    max_tokens: int = 1024
    enabled: bool = True


class CustomTaskOut(CustomTaskIn):
    id: int

    @classmethod
    def of(cls, ct: m.CustomTask) -> "CustomTaskOut":
        def jl(s):
            try:
                return json.loads(s) if s else []
            except ValueError:
                return []

        def jd(s):
            try:
                return json.loads(s) if s else {}
            except ValueError:
                return {}

        return cls(
            id=ct.id, category=ct.category, name=ct.name, prompt=ct.prompt, scorer=ct.scorer,
            system=ct.system, answer=ct.answer, contains=jl(ct.contains_json),
            numeric_answer=ct.numeric_answer, numeric_tol=ct.numeric_tol, choices=jl(ct.choices_json),
            correct=ct.correct, rubric=ct.rubric, entry_point=ct.entry_point, test_code=ct.test_code,
            code_prefix=ct.code_prefix, tools=jl(ct.tools_json), expected_tool=ct.expected_tool,
            expected_args=jd(ct.expected_args_json), forbid_tool_call=ct.forbid_tool_call,
            max_tokens=ct.max_tokens, enabled=ct.enabled,
        )


class JudgeConfig(BaseModel):
    type: Literal["none", "instance", "external"] = "none"
    instance_id: int | None = None


class EvalRunRequest(BaseModel):
    instance_id: int
    name: str | None = None
    categories: list[str] = Field(default_factory=lambda: ["coding", "reasoning", "textgen", "judging"])
    capability: bool = True
    performance: bool = True
    perf_reps: int = 3
    concurrency: list[int] = Field(default_factory=lambda: [1, 2, 4])
    temperature: float = 0.2
    judge: JudgeConfig | None = None
    sandbox_image: str = "python:3.12-slim"


class EvalStarted(BaseModel):
    run_id: int
    job_id: int
    message: str


class EvalResultOut(BaseModel):
    category: str
    task_id: str
    task_name: str
    scorer: str
    score: float
    passed: bool | None
    response: str | None
    judge_reason: str | None
    latency_ms: float | None
    ttft_ms: float | None
    prompt_tokens: int | None
    completion_tokens: int | None
    tokens_per_sec: float | None
    error: str | None

    @classmethod
    def of(cls, r: m.EvalResult) -> "EvalResultOut":
        return cls(
            category=r.category, task_id=r.task_id, task_name=r.task_name, scorer=r.scorer,
            score=r.score, passed=r.passed, response=r.response, judge_reason=r.judge_reason,
            latency_ms=r.latency_ms, ttft_ms=r.ttft_ms, prompt_tokens=r.prompt_tokens,
            completion_tokens=r.completion_tokens, tokens_per_sec=r.tokens_per_sec, error=r.error,
        )


class PerfResultOut(BaseModel):
    category: str
    concurrency: int
    reps: int
    ttft_ms_avg: float | None
    decode_tps_avg: float | None
    total_latency_ms_avg: float | None
    throughput_tps: float | None
    prompt_tokens_avg: float | None
    completion_tokens_avg: float | None
    error: str | None

    @classmethod
    def of(cls, p: m.PerfResult) -> "PerfResultOut":
        return cls(
            category=p.category, concurrency=p.concurrency, reps=p.reps,
            ttft_ms_avg=p.ttft_ms_avg, decode_tps_avg=p.decode_tps_avg,
            total_latency_ms_avg=p.total_latency_ms_avg, throughput_tps=p.throughput_tps,
            prompt_tokens_avg=p.prompt_tokens_avg, completion_tokens_avg=p.completion_tokens_avg,
            error=p.error,
        )


class EvalRunOut(BaseModel):
    id: int
    name: str
    instance_id: int | None
    model_name: str
    instance_label: str
    categories: list[str]
    capability: bool
    performance: bool
    status: str
    overall_score: float | None
    peak_throughput_tps: float | None
    judge_desc: str | None
    job_id: int | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def of(cls, run: m.EvalRun) -> "EvalRunOut":
        peak = None
        if run.summary_json:
            try:
                peak = json.loads(run.summary_json).get("peak_throughput_tps")
            except ValueError:
                peak = None
        return cls(
            id=run.id, name=run.name, instance_id=run.instance_id, model_name=run.model_name,
            instance_label=run.instance_label, categories=run.categories.split(",") if run.categories else [],
            capability=run.capability, performance=run.performance, status=run.status,
            overall_score=run.overall_score, peak_throughput_tps=peak, judge_desc=run.judge_desc,
            job_id=run.job_id, created_at=run.created_at, started_at=run.started_at,
            finished_at=run.finished_at,
        )


class EvalRunDetail(EvalRunOut):
    summary: dict[str, Any] | None = None
    config: dict[str, Any] | None = None
    results: list[EvalResultOut] = Field(default_factory=list)
    perf: list[PerfResultOut] = Field(default_factory=list)

    @classmethod
    def of_detail(cls, run: m.EvalRun) -> "EvalRunDetail":
        base = EvalRunOut.of(run).model_dump()
        summary = json.loads(run.summary_json) if run.summary_json else None
        config = json.loads(run.config_json) if run.config_json else None
        return cls(
            **base, summary=summary, config=config,
            results=[EvalResultOut.of(r) for r in run.results],
            perf=[PerfResultOut.of(p) for p in run.perf],
        )
