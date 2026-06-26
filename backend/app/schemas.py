"""Pydantic request/response schemas for the HTTP API.

Secrets are accepted on input but never serialized back out — instead the
``has_*`` booleans tell the UI whether a secret is stored.
"""

from __future__ import annotations

import ipaddress
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


# --- Nodes ---------------------------------------------------------------
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


class NodeOut(BaseModel):
    id: int
    role: str
    name: str
    lan_ip: str
    qsfp_ip: str
    qsfp_iface: str
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


class SettingsOut(BaseModel):
    has_hf_token: bool
    status_poll_seconds: int
    setup_complete: bool


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

    @classmethod
    def of(cls, model: m.ModelRegistry) -> "ModelOut":
        return cls(
            id=model.id,
            repo_id=model.repo_id,
            name=model.name,
            tool_parser=model.tool_parser,
            size_bytes=model.size_bytes,
            status=model.status,
            notes=model.notes,
            node_states=[ModelNodeStateOut.of(s) for s in model.node_states],
            created_at=model.created_at,
        )


# --- Instances -----------------------------------------------------------
class InstanceIn(BaseModel):
    name: str
    model_id: int
    topology: Literal["cluster", "single"] = "cluster"

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        return _v_instance_name(v)
    node_id: int | None = None  # required for single
    port: int = 8000
    tensor_parallel_size: int | None = None  # defaulted from topology
    max_model_len: int | None = None
    gpu_memory_utilization: float = 0.85
    max_num_seqs: int | None = None
    dtype: str | None = None
    enable_tool_choice: bool = True
    tool_parser: str | None = None  # auto-mapped if None and enable_tool_choice
    extra_args: str | None = None
    api_key: str | None = None
    autostart: bool = True


class InstanceUpdate(BaseModel):
    port: int | None = None
    max_model_len: int | None = None
    gpu_memory_utilization: float | None = None
    max_num_seqs: int | None = None
    dtype: str | None = None
    enable_tool_choice: bool | None = None
    tool_parser: str | None = None
    extra_args: str | None = None
    autostart: bool | None = None


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
    dtype: str | None
    enable_tool_choice: bool
    tool_parser: str | None
    extra_args: str | None
    has_api_key: bool
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
            dtype=inst.dtype,
            enable_tool_choice=inst.enable_tool_choice,
            tool_parser=inst.tool_parser,
            extra_args=inst.extra_args,
            has_api_key=bool(inst.api_key_enc),
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
    mem_budget_used_gib: float | None = None
    mem_budget_total_gib: float | None = None
    detail: str | None = None


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


class StatusSnapshot(BaseModel):
    setup_complete: bool
    qsfp_ok: bool | None = None
    ray: RayStatus
    nodes: list[NodeStatus]
    instances: list[InstanceRuntimeStatus]
    overcommit_warnings: list[str] = Field(default_factory=list)
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
