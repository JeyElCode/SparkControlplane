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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


def _v_fqdn(v):
    from .services.pki import normalise_fqdn

    return normalise_fqdn(v)


class NodeIn(BaseModel):
    role: Literal["head", "worker"]
    name: str
    lan_ip: str
    # The node's TLS identity: the name the cluster proxy verifies its
    # certificate against. Optional — a fleet without node certificates has
    # no use for it.
    fqdn: str | None = None

    @field_validator("fqdn")
    @classmethod
    def _fqdn(cls, v):
        return _v_fqdn(v)
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


class NodeCertIn(BaseModel):
    """A signed certificate for a node, and optionally the CA that signed it.

    `private_key` exists for operators who already hold a cert+key pair. It is
    the weaker path and not the default: `nodes` travels in the backup bundle,
    so a key the portal handles is a key written to an S3 object on a schedule.
    The CSR flow avoids it entirely — the key is generated on the node and only
    the request travels.
    """

    certificate: str
    ca_certificate: str | None = None
    private_key: str | None = None


class NodeUpdate(BaseModel):
    name: str | None = None
    lan_ip: str | None = None
    fqdn: str | None = None

    @field_validator("fqdn")
    @classmethod
    def _fqdn(cls, v):
        return _v_fqdn(v)
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
    fqdn: str | None = None
    qsfp_ip: str
    qsfp_iface: str
    mac_address: str | None = None
    ssh_user: str
    ssh_port: int
    auth_method: str
    sudo_mode: str
    hardened: bool
    has_host_key: bool = False
    # Node certificate state, for the Nodes page. The certificate itself is
    # public; the key is on the node and the portal never has it.
    cert_fqdn: str | None = None
    has_certificate: bool = False
    cert_not_after: datetime | None = None
    cert_fingerprint: str | None = None
    cert_error: str | None = None
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
            fqdn=n.fqdn,
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
            # Declared since v1.29.0 and never populated, so the Nodes page
            # always reported every node unpinned and hid the Forget-host-key
            # control — the deliberate re-trust action after a rebuild, which
            # node certificates now also depend on.
            has_host_key=bool(n.host_key),
            cert_fqdn=n.fqdn,
            has_certificate=bool(n.tls_cert_pem),
            cert_not_after=n.tls_not_after,
            cert_fingerprint=n.tls_fingerprint,
            cert_error=n.tls_last_error,
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

    # --- node certificates -------------------------------------------------
    node_cert_source: str | None = Field(default=None, pattern=r"^(none|openbao|manual)$")
    node_cert_ttl_hours: float | None = None
    node_ca_pem: str | None = None      # "" clears
    pki_url: str | None = None
    pki_mount: str | None = None
    pki_role: str | None = None
    pki_token: str | None = None        # write-only; "" clears

    @field_validator("node_cert_ttl_hours")
    @classmethod
    def _ttl(cls, v):
        if v is None:
            return None
        from .services.pki import validate_ttl_hours

        # Refused here with the reason, so the operator sees it in the form
        # rather than discovering it in a renewal job at 3am.
        return validate_ttl_hours(v)


class SettingsOut(BaseModel):
    has_hf_token: bool
    status_poll_seconds: int
    setup_complete: bool
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

    # --- node certificates -------------------------------------------------
    node_cert_source: str = "none"
    node_cert_ttl_hours: float | None = None
    # Derived, so the form can show the schedule the chosen lifetime implies
    # rather than making the operator work it out.
    cert_renew_after_hours: float | None = None
    cert_retry_window_hours: float | None = None
    has_node_ca: bool = False
    node_ca_subject: str | None = None
    pki_url: str | None = None
    pki_mount: str = "pki"
    pki_role: str | None = None
    has_pki_token: bool = False
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


# --- Serve profiles ------------------------------------------------------
class RevokedUser(BaseModel):
    username: str
    not_before: float


class RevocationStatus(BaseModel):
    loaded: bool
    global_not_before: float | None = None
    users: list[RevokedUser] = []
    revoked_session_count: int = 0
    cookie_secure_mode: str = "auto"
    cookie_secure_effective: bool = False


class RevokeIn(BaseModel):
    username: str | None = None   # omitted = the caller's own sessions
    everyone: bool = False
    reason: str | None = Field(default=None, max_length=128)


class RevokeResult(BaseModel):
    subject: str
    not_before: float
    detail: str


class ServeProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str | None = None
    repo_id: str | None = None
    # InstanceIn serve-field names -> values. Validated against InstanceIn
    # itself, so a profile can never smuggle a value a hand-typed instance
    # would have been refused.
    settings: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        return _v_instance_name(v)

    @field_validator("repo_id")
    @classmethod
    def _check_repo(cls, v: str | None) -> str | None:
        return _v_repo_id(v) if v else v


class ServeProfileUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = None
    repo_id: str | None = None
    settings: dict[str, Any] | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str | None) -> str | None:
        return _v_instance_name(v) if v else v


class ServeProfileOut(BaseModel):
    id: int
    name: str
    description: str | None = None
    repo_id: str | None = None
    settings: dict[str, Any] = {}
    builtin: bool = False
    created_at: datetime

    @classmethod
    def of(cls, row: m.ServeProfile, settings: dict) -> "ServeProfileOut":
        return cls(
            id=row.id, name=row.name, description=row.description,
            repo_id=row.repo_id, settings=settings, builtin=row.builtin,
            created_at=row.created_at,
        )


class ServeProfileExport(BaseModel):
    """The shareable document. Self-describing so an importer can tell what it
    is looking at, and versioned so the shape can change later."""

    kind: str = "spark-controlplane-serve-profiles"
    version: int = 1
    profiles: list[ServeProfileIn] = []


class ServeProfileImportResult(BaseModel):
    imported: list[str] = []
    skipped: list[str] = []       # name already taken
    # Fields removed because an imported profile may not choose what runs on the
    # nodes (vllm_image, extra_args) or was not a serve setting at all.
    dropped_fields: list[str] = []


class ProfileApply(BaseModel):
    """Merge a profile into an instance-creation payload, client-side."""

    profile_id: int


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
    # Geometry from config.json; null when it could not be read. Surfaced so the
    # UI can say "context length unverified" rather than silently guessing.
    context_len: int | None = None

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
            context_len=model.context_len,
        )



# Env-var names allowed into a container. `fullmatch` is deliberate: with a `$`
# anchor, "NCCL_DEBUG\n" passes, because Python's `$` matches before a trailing
# newline — and a newline in a key truncates the generated docker-run command.
_ENV_KEY_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
MAX_ENV_VARS = 64


def _v_env_vars(v):
    """Validate a per-instance environment map.

    This is operator-set configuration, not untrusted input — whoever can set
    it can already choose the container image — so the rules exist to keep the
    generated artifacts well-formed, not to contain a hostile operator.

    Newlines are rejected in values as well as keys. For the script path a
    newline merely truncates a command, but a `cluster`-topology instance is
    launched from a systemd `ExecStart=` line, and systemd's parser is
    line-based: a value containing a newline followed by `ExecStartPre=...`
    becomes a *second directive*, which is root code execution outside the
    container that no amount of shell quoting would prevent.
    """
    if v is None:
        return None
    if not isinstance(v, dict):
        raise ValueError("env must be an object of KEY: VALUE pairs")
    if len(v) > MAX_ENV_VARS:
        raise ValueError(f"at most {MAX_ENV_VARS} environment variables")
    out: dict[str, str] = {}
    for key, value in v.items():
        key = str(key)
        if not _ENV_KEY_RE.match(key):
            raise ValueError(
                f"invalid environment variable name {key!r}: use letters, digits "
                "and underscore, starting with a letter or underscore"
            )
        # Coerce nothing: True -> "True" is a real NCCL footgun, and silently
        # turning it into a string hides the mistake until the model is loaded.
        if not isinstance(value, str):
            raise ValueError(
                f"value for {key} must be a string (got {type(value).__name__})"
            )
        if "\n" in value or "\r" in value:
            raise ValueError(f"value for {key} must not contain a newline")
        if len(value) > 4096:
            raise ValueError(f"value for {key} is too long (max 4096 characters)")
        out[key] = value
    return out


def _parse_env_json(raw: str | None) -> dict[str, str] | None:
    """Stored JSON -> dict for the API. Tolerant: a malformed value must not
    make the whole instance unreadable."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    return {str(k): str(v) for k, v in data.items()}



# --- Named endpoints ------------------------------------------------------
_HOSTNAME_RE = re.compile(
    r"\A[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?(\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)*\Z"
)


def _v_hostname(v: str | None) -> str | None:
    """A public DNS name, lowercased.

    Enforced rather than accepted-as-typed because this value ends up in a TLS
    certificate request and in generated Kubernetes manifests. Anything that is
    not a DNS name cannot work in either place, so it is refused at the point
    of entry instead of failing later at `kubectl apply` or in an ACME order.
    """
    if v is None:
        return None
    v = v.strip().lower().rstrip(".")
    if not v or len(v) > 255 or not _HOSTNAME_RE.match(v):
        raise ValueError(
            f"'{v}' is not a DNS hostname. Expected something like "
            "llm.example.net — letters, digits, dashes and dots."
        )
    return v


class EndpointIn(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    hostname: str = Field(min_length=1, max_length=255)
    port: int = Field(default=443, ge=1, le=65535)
    # onbox: an nginx sidecar on the serving node, cert pushed on promote.
    # k8s:   a proxy in the cluster with a cert-manager certificate. Same nginx,
    #        moved off the box; the portal never holds the key.
    termination: str = Field(default="onbox", pattern=r"^(onbox|k8s)$")
    # The port on the serving node an external proxy targets. Pinning it is
    # what lets the k8s manifests stay static across a promotion.
    upstream_port: int | None = Field(default=None, ge=1, le=65535)
    description: str | None = None
    aliases: list[str] = Field(default_factory=list)
    # Write-only, both of them. The certificate is readable back only as
    # metadata; the key is never readable at all.
    tls_cert: str | None = None
    tls_key: str | None = None


    @field_validator("hostname")
    @classmethod
    def _host(cls, v):
        return _v_hostname(v)

    @model_validator(mode="after")
    def _k8s_needs_a_pinned_port(self):
        if self.termination == "k8s" and not self.upstream_port:
            raise ValueError(
                "A Kubernetes-terminated endpoint needs an upstream_port: it is "
                "the port every member binds, and pinning it is what keeps the "
                "cluster manifests correct across a promotion."
            )
        return self


class EndpointUpdate(BaseModel):
    hostname: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    termination: str | None = Field(default=None, pattern=r"^(onbox|k8s)$")
    upstream_port: int | None = Field(default=None, ge=1, le=65535)
    description: str | None = None
    enabled: bool | None = None
    aliases: list[str] | None = None

    @field_validator("hostname")
    @classmethod
    def _host(cls, v):
        return _v_hostname(v)


class TlsUploadIn(BaseModel):
    tls_cert: str
    tls_key: str


class PromoteIn(BaseModel):
    instance_id: int
    reason: str | None = None


class EndpointPromotionOut(BaseModel):
    id: int
    endpoint_name: str
    to_instance_name: str
    to_model_name: str = ""
    from_instance_name: str | None = None
    status: str
    reason: str | None = None
    actor: str | None = None
    job_id: int | None = None
    started_at: datetime
    finished_at: datetime | None = None


class EndpointOut(BaseModel):
    """An endpoint as the API exposes it.

    Carries the certificate's PUBLIC metadata and never the key. Everything
    here is sent in the clear during a TLS handshake anyway, so publishing it
    costs nothing and answers the operator's real questions: which certificate
    is this, does it cover the hostname, when does it expire.
    """

    id: int
    name: str
    hostname: str
    port: int
    termination: str = "onbox"
    upstream_port: int | None = None
    description: str | None = None
    enabled: bool = True
    aliases: list[str] = Field(default_factory=list)
    current_instance_id: int | None = None
    current_instance: str | None = None
    promoted_at: datetime | None = None
    member_instances: list[str] = Field(default_factory=list)

    has_tls: bool = False
    tls_subject: str | None = None
    tls_issuer: str | None = None
    tls_sans: list[str] = Field(default_factory=list)
    tls_fingerprint_sha256: str | None = None
    tls_not_after: datetime | None = None
    tls_days_remaining: int | None = None

    @classmethod
    async def of(cls, ep, session) -> "EndpointOut":
        from sqlalchemy import select

        from .models import Instance

        current = (
            await session.get(Instance, ep.current_instance_id)
            if ep.current_instance_id else None
        )
        members = (
            await session.execute(select(Instance.name).where(Instance.endpoint_id == ep.id))
        ).scalars().all()
        days = None
        if ep.tls_not_after is not None:
            days = (ep.tls_not_after - datetime.utcnow()).days
        sans: list[str] = []
        if ep.tls_sans_json:
            try:
                loaded = json.loads(ep.tls_sans_json)
                sans = [str(x) for x in loaded] if isinstance(loaded, list) else []
            except ValueError:
                sans = []
        return cls(
            id=ep.id, name=ep.name, hostname=ep.hostname, port=ep.port,
            termination=ep.termination, upstream_port=ep.upstream_port,
            description=ep.description, enabled=ep.enabled,
            aliases=[a.alias for a in ep.aliases],
            current_instance_id=ep.current_instance_id,
            current_instance=current.name if current else None,
            promoted_at=ep.promoted_at, member_instances=list(members),
            has_tls=bool(ep.tls_cert_enc), tls_subject=ep.tls_subject,
            tls_issuer=ep.tls_issuer, tls_sans=sans,
            tls_fingerprint_sha256=ep.tls_fingerprint_sha256,
            tls_not_after=ep.tls_not_after, tls_days_remaining=days,
        )


# --- Serve planning ------------------------------------------------------
class PlanIn(BaseModel):
    model_id: int
    # Overrides let the planner answer "what if" without the operator having to
    # abandon a choice they have already made — pin a topology and the rest of
    # the arithmetic re-derives around it.
    topology: Literal["cluster", "single", "distributed"] | None = None
    node_id: int | None = None
    max_num_seqs: int | None = Field(default=None, ge=1, le=1024)


class PlanReason(BaseModel):
    field: str
    label: str
    value: object
    why: str


class PlanOut(BaseModel):
    """A complete, startable configuration plus the reasoning behind it.

    ``settings`` is exactly the shape the create form holds, so the UI can
    apply it wholesale and leave every field editable.
    """

    name: str
    settings: dict
    reasons: list[PlanReason]
    warnings: list[str]
    feasible: bool
    summary: str


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
    # Bounded because both ends are unusable rather than merely unwise: vLLM
    # refuses to start at 0, and above ~0.95 the load dies in the allocator
    # with an error that names none of this. Rejecting it here costs a second;
    # finding out costs the ten minutes it takes to load weights.
    gpu_memory_utilization: float = Field(default=0.85, ge=0.1, le=0.95)
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
    # Membership of a named endpoint. A member serves the ENDPOINT's aliases,
    # not its own — see services/instances.py::_effective_aliases.
    endpoint_id: int | None = None
    compilation_config: str | None = None  # JSON string, validated
    advanced_args: str | None = None
    # Per-instance container environment. See _v_env_vars for why newlines are
    # refused and services/profiles.py for why this is never accepted from an
    # imported profile.
    env_vars: dict[str, str] | None = None

    @field_validator("env_vars")
    @classmethod
    def _check_env_vars(cls, v):
        return _v_env_vars(v)

    @model_validator(mode="after")
    def _env_not_on_cluster(self):
        """Refuse env on `cluster` topology.

        Two independent reasons. Functionally it would not work: a cluster
        instance is `docker exec`ed into the long-lived Ray head container, so
        the variables would reach the rank-0 driver only, never the Ray worker
        actors that do the other half of the all-reduce — a setting that
        appears to apply and silently does not. And structurally the exec runs
        from a systemd `ExecStart=` line, where a value is one newline away
        from becoming a second unit directive executed as root on the host.
        Use `distributed` topology, which launches its own container per node.
        """
        if self.env_vars and self.topology == "cluster":
            raise ValueError(
                "Per-instance environment is not supported on 'cluster' topology "
                "(the instance runs inside the shared Ray container). Use "
                "'distributed' topology, or set the variable on the Ray cluster."
            )
        return self       # JSON array of {flag, value}
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
    gpu_memory_utilization: float | None = Field(default=None, ge=0.1, le=0.95)
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
    env_vars: dict[str, str] | None = None
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
    started_at: datetime | None = None
    last_healthy_at: datetime | None = None
    last_load_seconds: int | None = None

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
            env_vars=_parse_env_json(inst.env_vars),
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
            started_at=inst.started_at,
            last_healthy_at=inst.last_healthy_at,
            last_load_seconds=inst.last_load_seconds,
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
    node_id: int | None = None  # the API-serving node (head for cluster/distributed)
    systemd_active: bool | None = None
    health_ok: bool | None = None
    # systemd restart count — a climbing value while never healthy is a crash
    # loop rather than a slow model load.
    n_restarts: int | None = None
    served_model: str | None = None
    # Every id the instance's own /v1/models reports — lets the portal detect
    # advertising a name vLLM would 404.
    served_models: list[str] = Field(default_factory=list)
    endpoint: str | None = None
    detail: str | None = None
    metrics: InstanceMetrics | None = None
    started_at: datetime | None = None
    last_healthy_at: datetime | None = None
    last_load_seconds: int | None = None


class GatewayRoute(BaseModel):
    """One model id the gateway will accept, and where it goes."""

    model_name: str            # what a client puts in the "model" field
    instance_id: int
    instance: str
    status: str
    node: str | None = None    # the API-serving node (head for multi-node)
    healthy: bool | None = None
    # True when the instance's own /v1/models confirms it serves this id. False
    # means the portal would route it but vLLM would 404 — a real misconfig.
    confirmed_upstream: bool | None = None


class GatewayInfo(BaseModel):
    """Everything the UI needs to hand someone a working client config."""

    base_path: str = "/v1"     # joined to the portal origin by the browser
    auth_required: bool        # portal auth on -> clients need a bearer token
    token_configured: bool
    routes: list[GatewayRoute] = []
    # Names that exist but are not servable right now, with why.
    unavailable: list[GatewayRoute] = []
    # alias -> the running instances advertising it, when more than one does.
    # Surfaced rather than only logged: the gateway still answers (refusing
    # would take the endpoint down over a config ambiguity) but it is serving
    # from ONE of them, and which one is not something to leave implicit.
    alias_conflicts: dict[str, list[str]] = Field(default_factory=dict)


# --- Gateway API keys + traffic ------------------------------------------
class ApiKeyIn(BaseModel):
    label: str = Field(min_length=1, max_length=64)
    # 0 / null = unlimited (the default). Per-key overrides of the global caps.
    max_concurrent: int | None = Field(default=None, ge=0, le=1000)
    max_rpm: int | None = Field(default=None, ge=0, le=100000)


class ApiKeyUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=64)
    enabled: bool | None = None
    max_concurrent: int | None = Field(default=None, ge=0, le=1000)
    max_rpm: int | None = Field(default=None, ge=0, le=100000)


class ApiKeyOut(BaseModel):
    """A key as listed. The token itself is never included — it exists in
    exactly one response, the 201 from create."""

    id: int
    label: str
    prefix: str
    enabled: bool
    max_concurrent: int | None = None
    max_rpm: int | None = None
    last_used_at: datetime | None = None
    created_at: datetime
    in_flight: int = 0  # live, from the limiter

    @classmethod
    def of(cls, row: m.ApiKey, in_flight: int = 0) -> "ApiKeyOut":
        return cls(
            id=row.id, label=row.label, prefix=row.prefix, enabled=row.enabled,
            max_concurrent=row.max_concurrent, max_rpm=row.max_rpm,
            last_used_at=row.last_used_at, created_at=row.created_at,
            in_flight=in_flight,
        )


class ApiKeyCreated(ApiKeyOut):
    """The one and only time the caller sees the token."""

    token: str


class GatewayRequestOut(BaseModel):
    ts: float
    client: str
    model: str
    instance: str | None = None
    status: int
    duration_ms: int
    ttfb_ms: int | None = None
    streamed: bool = False
    error: str | None = None


class GatewayTrafficRow(BaseModel):
    client: str
    model: str
    requests: int
    errors: int
    rejected: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    avg_ms: float | None = None
    avg_ttfb_ms: float | None = None


class GatewayTraffic(BaseModel):
    """Live attribution: who is calling what, and how it is going."""

    since_start: list[GatewayTrafficRow] = []
    recent: list[GatewayRequestOut] = []
    in_flight: dict[str, int] = {}


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
    # extra="forbid" is the point of this model, not decoration. These fields
    # were passed by the handler and silently dropped for two releases because
    # they were never declared: Pydantic ignores unknown constructor kwargs, so
    # the API returned a smaller object than the code plainly said it did, and
    # the UI reported the quality suite as "not installed" while it was running
    # evals perfectly well. Forbidding extras turns that class of drift into a
    # loud failure at the first request instead of a silent wrong answer.
    model_config = ConfigDict(extra="forbid")

    perf_categories: list[str]        # every prompt category the speed bench offers
    speed_ladder: list[str]           # the three predictability regimes, in order
    quality_available: bool = False   # is tool-eval-bench installed in this image
    quality_suite_sha: str | None = None  # the pinned upstream commit




class EvalRunRequest(BaseModel):
    instance_id: int
    name: str | None = None
    # Defaults to the predictability ladder: one number per regime is the
    # comparison that actually distinguishes two builds (see eval_suites.py).
    categories: list[str] = Field(
        default_factory=lambda: ["predictable", "code", "creative"]
    )
    # Run the pinned tool-eval-bench suite alongside (or instead of) the speed
    # prompts. Off by default: it takes far longer than a speed run.
    quality: bool = False
    short: bool = False      # tool-eval-bench --short (15 scenarios, for a smoke check)
    hardmode: bool = False   # + the 15 Hard Mode scenarios
    seed: int = Field(default=42, ge=0, le=2**31 - 1)
    # Bounded because these two multiply into real load against a LIVE serving
    # instance, from a single-replica portal, and the Sparks have no
    # out-of-band recovery. Unbounded, `{"concurrency": [512], "perf_reps": 50}`
    # was accepted and meant 512 concurrent streams at up to the per-request
    # timeout each. vLLM queues rather than OOMs, so the damage is a saturated
    # endpoint and a job that never ends — but nothing stopped it.
    perf_reps: int = Field(default=3, ge=1, le=20)
    concurrency: list[int] = Field(default_factory=lambda: [1, 2, 4])

    @field_validator("concurrency")
    @classmethod
    def _check_concurrency(cls, v: list[int]) -> list[int]:
        if not v:
            raise ValueError("concurrency needs at least one level")
        if len(v) > 8:
            raise ValueError("at most 8 concurrency levels per run")
        for level in v:
            if level < 1 or level > 64:
                raise ValueError(f"concurrency level {level} out of range (1-64)")
        return v
    temperature: float = 0.2


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
    # Best tok/s per predictability regime, so the run LIST can show the
    # comparison that matters instead of a single peak across every category
    # and concurrency level — which is precisely the number eval_suites.py
    # argues is misleading. Derived from summary_json; empty on legacy rows.
    ladder_tps: dict[str, float] = Field(default_factory=dict)
    # Quality (tool-eval-bench). composite_score is 0-100 and MUST be read with
    # completion_rate beside it — see the note on the model column.
    quality: bool = False
    composite_score: float | None = None
    completion_rate: float | None = None
    suite_sha: str | None = None
    judge_desc: str | None
    job_id: int | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def of(cls, run: m.EvalRun) -> "EvalRunOut":
        peak = None
        ladder: dict[str, float] = {}
        if run.summary_json:
            try:
                summary = json.loads(run.summary_json)
            except ValueError:
                summary = {}
            if isinstance(summary, dict):
                peak = summary.get("peak_throughput_tps")
                raw = summary.get("ladder_tps")
                if isinstance(raw, dict):
                    ladder = {
                        str(k): float(v) for k, v in raw.items()
                        if isinstance(v, (int, float))
                    }
        return cls(
            id=run.id, name=run.name, instance_id=run.instance_id, model_name=run.model_name,
            instance_label=run.instance_label, categories=run.categories.split(",") if run.categories else [],
            capability=run.capability, performance=run.performance, status=run.status,
            overall_score=run.overall_score, peak_throughput_tps=peak, ladder_tps=ladder,
            quality=run.quality, composite_score=run.composite_score,
            completion_rate=run.completion_rate, suite_sha=run.suite_sha,
            judge_desc=run.judge_desc,
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
