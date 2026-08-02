"""SQLAlchemy ORM models — the persisted state of the control plane.

String "enum" columns are kept as plain strings (with constants below) to keep
schema evolution trivial. Encrypted columns end in ``_enc`` and hold Fernet
tokens produced by :mod:`app.crypto`.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# --- Roles / enums (as string constants) ---------------------------------
ROLE_HEAD = "head"
ROLE_WORKER = "worker"

# Cluster size cap: 1 head + up to 3 workers.
MAX_NODES = 4

AUTH_PASSWORD = "password"
AUTH_KEY = "key"

SUDO_NOPASSWD = "nopasswd"
SUDO_PASSWORD = "password"

TOPO_CLUSTER = "cluster"   # vllm serve in the ray head container, TP across both nodes
TOPO_SINGLE = "single"     # standalone container pinned to one node, TP=1
TOPO_DISTRIBUTED = "distributed"  # native torch.distributed multi-node, headless workers over QSFP

# Model per-node states
MS_ABSENT = "absent"
MS_DOWNLOADING = "downloading"
MS_SYNCING = "syncing"
MS_VERIFYING = "verifying"
MS_PRESENT = "present"
MS_ERROR = "error"

# Instance states
INST_STOPPED = "stopped"
INST_STARTING = "starting"
INST_RUNNING = "running"
INST_STOPPING = "stopping"
INST_ERROR = "error"

# Occupies (or is about to occupy) GPU memory on its node — a shutdown kills it
# and a fleet image update must restart it. "starting" covers the whole
# install-and-load window, which for a large FP8 model is many minutes.
INST_ACTIVE_STATES = (INST_STARTING, INST_RUNNING)
# A control-plane job owns the row; the status observer must not overrule it.
INST_INFLIGHT_STATES = (INST_STARTING, INST_STOPPING)

# Job states
JOB_PENDING = "pending"
JOB_RUNNING = "running"
JOB_SUCCESS = "success"
JOB_ERROR = "error"
JOB_CANCELLED = "cancelled"


class Node(Base):
    __tablename__ = "nodes"

    id: Mapped[int] = mapped_column(primary_key=True)
    # head | worker. Exactly one head; up to MAX_NODES-1 workers (enforced at the
    # API layer — the column is deliberately NOT unique so multiple workers fit).
    role: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(64))               # hostname, e.g. spark-01
    lan_ip: Mapped[str] = mapped_column(String(64))
    qsfp_ip: Mapped[str] = mapped_column(String(64))
    qsfp_iface: Mapped[str] = mapped_column(String(32), default="enp1s0f1np1")

    # LAN-interface MAC for Wake-on-LAN; auto-captured on Test connection,
    # manually editable. Nullable — wake is unavailable until known.
    mac_address: Mapped[str | None] = mapped_column(String(17), nullable=True)

    ssh_user: Mapped[str] = mapped_column(String(64))
    ssh_port: Mapped[int] = mapped_column(Integer, default=22)
    auth_method: Mapped[str] = mapped_column(String(16), default=AUTH_PASSWORD)
    ssh_password_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    ssh_private_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    ssh_key_passphrase_enc: Mapped[str | None] = mapped_column(Text, nullable=True)

    sudo_mode: Mapped[str] = mapped_column(String(16), default=SUDO_PASSWORD)
    sudo_password_enc: Mapped[str | None] = mapped_column(Text, nullable=True)

    hardened: Mapped[bool] = mapped_column(Boolean, default=False)  # generated key installed
    # The node's SSH host key, captured on first connect and pinned thereafter
    # ("ssh-ed25519 AAAA..."). Until this exists the portal accepts whatever key
    # the host offers, which is the window a man-in-the-middle needs — and the
    # very next thing sent over that connection is the sudo password. NULL means
    # "not yet seen"; clearing it is the deliberate re-trust action after a
    # legitimate host rebuild.
    host_key: Mapped[str | None] = mapped_column(String(512), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class ClusterConfig(Base):
    """Singleton row (id=1) holding cluster-wide configuration."""

    __tablename__ = "cluster_config"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    cluster_name: Mapped[str] = mapped_column(String(64), default="spark-vllm")
    vllm_image: Mapped[str] = mapped_column(String(255))
    qsfp_netmask: Mapped[int] = mapped_column(Integer, default=24)
    models_subdir: Mapped[str] = mapped_column(String(128), default="models")
    hf_cache_subdir: Mapped[str] = mapped_column(String(128), default=".cache/huggingface")
    models_container_path: Mapped[str] = mapped_column(String(128), default="/models")
    hf_cache_container_path: Mapped[str] = mapped_column(
        String(128), default="/root/.cache/huggingface"
    )
    ray_port: Mapped[int] = mapped_column(Integer, default=6379)
    shm_size: Mapped[str] = mapped_column(String(32), default="10.24gb")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class Setting(Base):
    """Singleton row (id=1) for portal-wide settings + secrets."""

    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    hf_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_poll_seconds: Mapped[int] = mapped_column(Integer, default=10)
    setup_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    # Alerting: JSON blob of thresholds/durations (defaults merged in code) and
    # an optional notification webhook (URL may embed a token -> encrypted).
    alerts_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    alert_webhook_url_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Scheduled config backups to S3-compatible storage (MinIO/AWS/R2/…)
    backup_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    backup_s3_endpoint: Mapped[str | None] = mapped_column(String(255), nullable=True)
    backup_s3_bucket: Mapped[str | None] = mapped_column(String(128), nullable=True)
    backup_s3_prefix: Mapped[str] = mapped_column(String(128), default="spark-controlplane/")
    backup_s3_region: Mapped[str] = mapped_column(String(64), default="us-east-1")
    backup_s3_access_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    backup_s3_secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    backup_interval_hours: Mapped[float] = mapped_column(Float, default=24.0)
    backup_retention: Mapped[int] = mapped_column(Integer, default=14)
    # Bearer token for the /v1 API gateway (required when portal auth is on)
    gateway_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class ModelRegistry(Base):
    __tablename__ = "models"

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[str] = mapped_column(String(255), unique=True)  # HF repo id
    name: Mapped[str] = mapped_column(String(255))                  # sanitized local dir name
    tool_parser: Mapped[str | None] = mapped_column(String(32), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=MS_ABSENT)

    # Geometry read from the repo's config.json (see services/hfmeta.py). These
    # are what let the planner size a KV cache instead of guessing: without
    # them it can still pick a topology, but it cannot say how long a context
    # will fit. All nullable — a gated repo, an air-gapped portal or a model
    # added before v1.30.0 simply has none, and the plan says so.
    context_len: Mapped[int | None] = mapped_column(Integer, nullable=True)
    num_layers: Mapped[int | None] = mapped_column(Integer, nullable=True)
    num_kv_heads: Mapped[int | None] = mapped_column(Integer, nullable=True)
    head_dim: Mapped[int | None] = mapped_column(Integer, nullable=True)
    torch_dtype: Mapped[str | None] = mapped_column(String(32), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    node_states: Mapped[list["ModelNodeState"]] = relationship(
        back_populates="model", cascade="all, delete-orphan"
    )
    instances: Mapped[list["Instance"]] = relationship(back_populates="model")


class ModelNodeState(Base):
    __tablename__ = "model_node_states"
    __table_args__ = (UniqueConstraint("model_id", "node_id", name="uq_model_node"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    model_id: Mapped[int] = mapped_column(ForeignKey("models.id", ondelete="CASCADE"))
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"))
    present: Mapped[bool] = mapped_column(Boolean, default=False)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checksum_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=MS_ABSENT)
    last_job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    model: Mapped[ModelRegistry] = relationship(back_populates="node_states")
    node: Mapped[Node] = relationship()


class Instance(Base):
    __tablename__ = "instances"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    model_id: Mapped[int] = mapped_column(ForeignKey("models.id"))
    topology: Mapped[str] = mapped_column(String(16), default=TOPO_CLUSTER)
    node_id: Mapped[int | None] = mapped_column(ForeignKey("nodes.id"), nullable=True)  # single only
    port: Mapped[int] = mapped_column(Integer, default=8000)

    tensor_parallel_size: Mapped[int] = mapped_column(Integer, default=2)
    max_model_len: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gpu_memory_utilization: Mapped[float] = mapped_column(Float, default=0.85)
    max_num_seqs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dtype: Mapped[str | None] = mapped_column(String(32), nullable=True)

    max_num_batched_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kv_cache_dtype: Mapped[str | None] = mapped_column(String(32), nullable=True)
    block_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokenizer_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reasoning_parser: Mapped[str | None] = mapped_column(String(32), nullable=True)
    trust_remote_code: Mapped[bool] = mapped_column(Boolean, default=False)

    enable_tool_choice: Mapped[bool] = mapped_column(Boolean, default=True)
    tool_parser: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Multiple `--served-model-name` aliases (space/newline-separated); ≥1 wins
    # over the registry name. Null falls back to the model's registry name.
    served_model_names: Mapped[str | None] = mapped_column(Text, nullable=True)
    # `--compilation-config <json>` — stored as a JSON string, validated as JSON.
    compilation_config: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Structured passthrough: JSON array of {"flag": "--x", "value": "y"|null}.
    advanced_args: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Per-instance container environment, a JSON object of KEY -> VALUE rendered
    # as `docker -e KEY=VALUE`. Exists so an operator can set NCCL_IB_*,
    # VLLM_* and friends without building a custom image. Nullable, so
    # _add_missing_columns can add it to an existing database.
    env_vars: Mapped[str | None] = mapped_column(Text, nullable=True)
    # `--master-port` for the native distributed rendezvous (distributed only).
    master_port: Mapped[int] = mapped_column(Integer, default=29500)
    # Legacy raw passthrough (kept for backward-compat; UI uses advanced_args).
    extra_args: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Optional per-instance vLLM/Ray image override. Falls back to the cluster's
    # ClusterConfig.vllm_image when unset — so most instances use the shared image
    # while one (e.g. a custom build for a specific model) can pin its own.
    vllm_image: Mapped[str | None] = mapped_column(String(255), nullable=True)
    api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Optional first-class TLS: when enabled, an nginx sidecar runs on the
    # API-serving node (single / distributed head), terminating HTTPS on
    # ``tls_port`` and reverse-proxying to vLLM on the instance ``port`` (which
    # stays plain HTTP, internal). The cert/key are stored encrypted and written
    # to the node at deploy time; they can be rotated without restarting vLLM.
    # MEMBERSHIP of a named endpoint — "launch me with its aliases and cert".
    # Distinct from Endpoint.current_instance_id, which is who serves NOW; see
    # the comment on Endpoint for why both are needed.
    endpoint_id: Mapped[int | None] = mapped_column(
        ForeignKey("endpoints.id"), nullable=True
    )
    endpoint: Mapped["Endpoint | None"] = relationship(
        foreign_keys=[endpoint_id], lazy="selectin"
    )
    tls_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    tls_port: Mapped[int] = mapped_column(Integer, default=443)
    tls_cert_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    tls_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)

    autostart: Mapped[bool] = mapped_column(Boolean, default=True)
    systemd_unit: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=INST_STOPPED)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Status reconciliation (see services/reconcile.py). `status` is a claim the
    # portal makes; these three are the evidence behind it, and they must be
    # durable so a portal restart mid-load doesn't lose the anchor and demote a
    # perfectly healthy load. All nullable: pre-upgrade rows have no history.
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_healthy_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # How long the last successful start took to go green — turns "it's been 4
    # minutes" into "it took 6 minutes last time" for both the UI and the
    # gateway's Retry-After.
    last_load_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    model: Mapped[ModelRegistry] = relationship(back_populates="instances")
    node: Mapped[Node | None] = relationship()


class SessionRevocation(Base):
    """A session, or a user's whole set of sessions, that must stop working.

    Sessions are stateless encrypted cookies, so there is nothing to delete —
    revocation has to be a rule that ``parse_session`` consults. Two kinds:

    * ``jti`` — one specific session (an explicit logout). The request that
      presents the cookie tells us its own id, so no registry of issued
      sessions is needed, and the row can be dropped once that token would have
      expired anyway.
    * ``epoch`` — every session for ``subject`` issued before ``not_before``
      ("sign out everywhere", a suspected leak, offboarding). ``subject=""`` is
      the global form. One row per revoked user: growth is bounded by the
      number of users, not by traffic or time.

    Deliberately **not** in the backup bundle: a restore replaces listed tables
    wholesale, so including this would let a month-old bundle un-revoke a
    session that was revoked last week.
    """

    __tablename__ = "session_revocations"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(8))          # "epoch" | "jti"
    subject: Mapped[str] = mapped_column(String(128), index=True)  # username, "" = all, or jti
    # Sessions issued before this instant are dead. For a jti row this is just
    # the point after which the row itself can be swept.
    not_before: Mapped[float] = mapped_column(Float)
    # For a jti row: when the token it kills expires on its own, after which
    # the row is inert and gets swept. Epoch rows use 0 and are kept forever —
    # growth is bounded by "distinct users ever revoked", which for this
    # deployment is a handful, and an expiry bound that has to be *derived* is
    # one more thing to get subtly wrong.
    expires_at: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # One time representation in a security table, deliberately: a naive/aware
    # datetime mixup is exactly how a comparison silently inverts.
    created_at: Mapped[float] = mapped_column(Float, default=0.0)


class ServeProfile(Base):
    """A named, reusable set of vLLM serve settings for a model.

    Getting a large model to serve is a matter of a dozen interacting flags
    (context length, GPU memory fraction, batch limits, the right parsers), and
    one wrong value is an out-of-memory ten minutes into a weight load. Without
    this, that knowledge lives in someone's shell history or a stranger's
    README. A profile makes it a thing you can apply, save and share.

    Deliberately holds only *serve* settings: never a name, port, node, API key
    or TLS material. Those are per-instance facts, and a profile that carried
    them would be unshareable at best and a credential leak at worst.
    """

    __tablename__ = "serve_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Optional hint: the HF repo this profile was tuned for. Free-form so a
    # profile can also be model-agnostic ("small model, single node").
    repo_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # The settings themselves, as a JSON object of InstanceIn field names. JSON
    # rather than 18 columns: the set tracks vLLM's flags, which change between
    # releases, and a profile that silently dropped an unknown key on upgrade
    # would be worse than useless.
    settings_json: Mapped[str] = mapped_column(Text, default="{}")
    # True for profiles shipped with the image. Never edited in place — an
    # upgrade replaces them, so a user edit would be silently clobbered.
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class ApiKey(Base):
    """A per-client credential for the /v1 gateway.

    The token is shown exactly once, at creation, and only its SHA-256 digest is
    stored. A password KDF (bcrypt/scrypt) would be the wrong tool: the secret is
    256 bits of CSPRNG output, not a human-chosen password, so a work factor buys
    a rounding error against an already-infeasible search — while a per-record
    salt would destroy the O(1) ``digest -> row`` lookup this needs on *every*
    gateway request.

    ``prefix`` is generated separately from the secret and stored in the clear,
    so logs, metrics labels and the UI can name a key without ever touching
    secret material.
    """

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(64))
    prefix: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    token_sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Per-key overrides; NULL = use the global default (which may itself be
    # unlimited). Set to 0 to mean "explicitly unlimited" for a trusted client.
    max_concurrent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_rpm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class GatewaySample(Base):
    """A 5-minute rollup of gateway traffic for one (client, model) pair.

    Deliberately an aggregate, never a row per request: the gateway's hot path
    must not contend for the same SQLite writer as the UI polls, the telemetry
    loops and the reconciler — least of all from inside a streaming response's
    cleanup, while the client may already be disconnecting.
    """

    __tablename__ = "gateway_samples"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)  # window end
    client: Mapped[str] = mapped_column(String(64), index=True)  # key label
    model: Mapped[str] = mapped_column(String(128))
    requests: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)      # non-2xx
    rejected: Mapped[int] = mapped_column(Integer, default=0)    # 401/429 at the gate
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    # Sum of durations, so an average survives aggregation across windows.
    duration_ms_total: Mapped[int] = mapped_column(Integer, default=0)
    ttfb_ms_total: Mapped[int] = mapped_column(Integer, default=0)
    ttfb_count: Mapped[int] = mapped_column(Integer, default=0)


class InstanceSchedule(Base):
    """A weekly live-window for an instance: on the listed weekdays the
    scheduler starts the instance at ``start_time`` and stops it at
    ``end_time`` (end <= start means the window wraps past midnight).
    Instances without schedules are fully manual, as before."""

    __tablename__ = "instance_schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    instance_id: Mapped[int] = mapped_column(
        ForeignKey("instances.id", ondelete="CASCADE"), index=True
    )
    days: Mapped[str] = mapped_column(String(32))       # csv of 0-6, Monday=0
    start_time: Mapped[str] = mapped_column(String(5))  # "HH:MM"
    end_time: Mapped[str] = mapped_column(String(5))    # "HH:MM"
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    instance: Mapped["Instance"] = relationship()


class UsageSample(Base):
    """One rollup window of serving activity for one instance (deltas of the
    vLLM counters over ~5 min). Names are snapshots so history survives
    instance/model deletion."""

    __tablename__ = "usage_samples"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    instance_name: Mapped[str] = mapped_column(String(64))
    model_name: Mapped[str] = mapped_column(String(255))
    gen_tokens: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    requests: Mapped[int] = mapped_column(Integer, default=0)
    ttft_ms_avg: Mapped[float | None] = mapped_column(Float, nullable=True)


class Alert(Base):
    """A fired alert (and its resolution) — history for the API/UI; the live
    active set is kept in memory by services/alerts.py."""

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    rule: Mapped[str] = mapped_column(String(32))       # e.g. node_offline
    subject: Mapped[str] = mapped_column(String(128))   # e.g. node/instance name
    severity: Mapped[str] = mapped_column(String(8), default="warn")  # warn | crit
    message: Mapped[str] = mapped_column(Text)
    fired_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Job(Base):
    """A long-running background operation with streamed logs."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(48))   # e.g. setup.network, model.download
    title: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default=JOB_PENDING)
    node_id: Mapped[int | None] = mapped_column(ForeignKey("nodes.id"), nullable=True)
    target: Mapped[str | None] = mapped_column(String(255), nullable=True)
    progress: Mapped[float | None] = mapped_column(Float, nullable=True)  # 0..1 when known
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    logs: Mapped[list["JobLog"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobLog.seq"
    )


class JobLog(Base):
    __tablename__ = "job_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    stream: Mapped[str] = mapped_column(String(8), default="info")  # info | stdout | stderr
    text: Mapped[str] = mapped_column(Text)

    job: Mapped[Job] = relationship(back_populates="logs")


# --- Evaluation / benchmarking -------------------------------------------
# The predictability ladder, ceiling -> floor (see eval_suites.py SPEED_LADDER).
# These three ARE the speed benchmark; there is nothing else. Historical
# PerfResult rows keep whatever category they were measured under, which is
# correct — those were different prompts.
PERF_CATEGORIES = ("predictable", "code", "creative")


class EvalRun(Base):
    """One evaluation/benchmark run against a model instance (a snapshot of the
    model + config so results stay comparable over time)."""

    __tablename__ = "eval_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    instance_id: Mapped[int | None] = mapped_column(ForeignKey("instances.id"), nullable=True)
    model_name: Mapped[str] = mapped_column(String(255))     # snapshot
    instance_label: Mapped[str] = mapped_column(String(255))  # snapshot, e.g. "cluster TP=2 :8000"
    categories: Mapped[str] = mapped_column(String(255))      # comma-separated
    capability: Mapped[bool] = mapped_column(Boolean, default=True)
    performance: Mapped[bool] = mapped_column(Boolean, default=True)
    config_json: Mapped[str] = mapped_column(Text)           # full request config
    judge_desc: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=JOB_PENDING)
    overall_score: Mapped[float | None] = mapped_column(Float, nullable=True)  # 0..1 capability mean
    # --- quality suite (tool-eval-bench) ---------------------------------
    # A separate flag from `capability`, which now means "a legacy run from
    # before the custom-task half was removed". Overloading it would make a
    # legacy run and a quality run indistinguishable.
    quality: Mapped[bool] = mapped_column(Boolean, default=False)
    # 0-100, on its own column. NEVER folded into overall_score, which is
    # documented 0..1 and multiplied by 100 by every consumer — writing 78
    # there renders as "7800%".
    composite_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Percentage of scenarios actually graded. Load-bearing, not decoration:
    # infra failures leave the DENOMINATOR and the exit code stays 0, so a
    # nearly-broken endpoint that passes its few gradable scenarios scores
    # HIGH. A composite without this beside it is not interpretable.
    completion_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    suite_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    suite_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)      # aggregates
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    results: Mapped[list["EvalResult"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    perf: Mapped[list["PerfResult"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class EvalResult(Base):
    """Per-task capability result."""

    __tablename__ = "eval_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("eval_runs.id", ondelete="CASCADE"))
    category: Mapped[str] = mapped_column(String(32))
    task_id: Mapped[str] = mapped_column(String(64))
    task_name: Mapped[str] = mapped_column(String(255))
    scorer: Mapped[str] = mapped_column(String(16))
    score: Mapped[float] = mapped_column(Float, default=0.0)   # 0..1
    passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    response: Mapped[str | None] = mapped_column(Text, nullable=True)
    judge_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    ttft_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_per_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    run: Mapped[EvalRun] = relationship(back_populates="results")


class PerfResult(Base):
    """Per-(category, concurrency) performance measurement."""

    __tablename__ = "perf_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("eval_runs.id", ondelete="CASCADE"))
    category: Mapped[str] = mapped_column(String(32))
    concurrency: Mapped[int] = mapped_column(Integer, default=1)
    reps: Mapped[int] = mapped_column(Integer, default=1)
    ttft_ms_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    decode_tps_avg: Mapped[float | None] = mapped_column(Float, nullable=True)   # per-stream tok/s
    total_latency_ms_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    throughput_tps: Mapped[float | None] = mapped_column(Float, nullable=True)   # aggregate tok/s
    prompt_tokens_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    completion_tokens_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    run: Mapped[EvalRun] = relationship(back_populates="perf")


# --- Named endpoints ------------------------------------------------------
# The production front door as a first-class object. Before this, the hostname,
# its TLS cert and its served-model aliases were all properties of whichever
# INSTANCE happened to be serving — so promoting a new model meant hand-copying
# a cert you could not read back and replicating aliases by hand, on a live
# endpoint. Worse, two instances could advertise the same alias with nothing
# arbitrating between them.
#
# The load-bearing detail is that there are TWO relations here, not one:
#
#   Instance.endpoint_id      MEMBERSHIP — "launch me with this endpoint's
#                             aliases and cert". Fixed at start time, because
#                             --served-model-name is baked into the vLLM
#                             command when the container launches.
#   Endpoint.current_instance_id  THE POINTER — who is serving right now.
#                             Flips with no restart.
#
# Collapsing them makes promotion unrepresentable: a candidate is not "current"
# at the moment it starts, so it would launch WITHOUT the production aliases,
# and flipping a pointer afterwards cannot add them to an already-running vLLM.
class Endpoint(Base):
    __tablename__ = "endpoints"
    __table_args__ = (UniqueConstraint("hostname", "port", name="uq_endpoint_host_port"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)   # "prod"
    hostname: Mapped[str] = mapped_column(String(255))           # llm.example.net
    port: Mapped[int] = mapped_column(Integer, default=443)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # The private key stays WRITE-ONLY, like the per-instance one it replaces.
    # The operator's actual need is a HANDOFF — move the cert to another
    # instance — not a read, and the endpoint owning it satisfies that without
    # the key ever leaving the portal.
    tls_cert_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    tls_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Parsed from the certificate at upload. Every one of these is transmitted
    # in the clear during any TLS handshake, so exposing them costs nothing and
    # answers the questions an operator actually has — which cert is this, does
    # it cover the hostname, when does it expire.
    tls_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tls_issuer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tls_sans_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    tls_fingerprint_sha256: Mapped[str | None] = mapped_column(String(95), nullable=True)
    tls_not_before: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    tls_not_after: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    tls_uploaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Unique: one instance hosts at most one nginx sidecar, so at most one
    # endpoint. Nullable-unique is fine in SQLite — NULLs are distinct.
    # endpoints -> instances and instances -> endpoints are mutually dependent.
    # use_alter defers THIS constraint to an ALTER after both tables exist, so
    # create_all can order them; without it SQLAlchemy cannot sort the metadata
    # and warns that it may become an error.
    current_instance_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "instances.id", ondelete="SET NULL",
            use_alter=True, name="fk_endpoint_current_instance",
        ),
        nullable=True, unique=True,
    )
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    aliases: Mapped[list["EndpointAlias"]] = relationship(
        back_populates="endpoint", cascade="all, delete-orphan",
        order_by="EndpointAlias.position",
    )


class EndpointAlias(Base):
    """One served-model alias owned by an endpoint.

    Normalised out of the space-separated text blob specifically so the
    database can enforce uniqueness. That is the whole point: an alias owned by
    exactly one endpoint cannot be ambiguous, which is the class of bug that
    silently routed production traffic to the wrong instance.
    """

    __tablename__ = "endpoint_aliases"

    id: Mapped[int] = mapped_column(primary_key=True)
    endpoint_id: Mapped[int] = mapped_column(
        ForeignKey("endpoints.id", ondelete="CASCADE"), index=True
    )
    alias: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)

    endpoint: Mapped[Endpoint] = relationship(back_populates="aliases")


class EndpointPromotion(Base):
    """What served this endpoint, when, and how the handoff went.

    Names are snapshotted alongside the FKs because history must outlive the
    instances it describes — deleting an instance must not erase the record of
    it having served production. Same reasoning as UsageSample.instance_name.
    """

    __tablename__ = "endpoint_promotions"

    id: Mapped[int] = mapped_column(primary_key=True)
    endpoint_id: Mapped[int] = mapped_column(
        ForeignKey("endpoints.id", ondelete="CASCADE"), index=True
    )
    endpoint_name: Mapped[str] = mapped_column(String(64))

    to_instance_id: Mapped[int | None] = mapped_column(
        ForeignKey("instances.id", ondelete="SET NULL"), nullable=True
    )
    to_instance_name: Mapped[str] = mapped_column(String(64))
    to_model_name: Mapped[str] = mapped_column(String(255), default="")
    from_instance_id: Mapped[int | None] = mapped_column(
        ForeignKey("instances.id", ondelete="SET NULL"), nullable=True
    )
    from_instance_name: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # pending | active | superseded | failed | rolled_back
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Nullable until RBAC exists. The column is here now so the history does
    # not have to be retro-fitted with a gap where the actor should be.
    actor: Mapped[str | None] = mapped_column(String(128), nullable=True)
    aliases_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    cert_fingerprint: Mapped[str | None] = mapped_column(String(95), nullable=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


PROMO_PENDING = "pending"
PROMO_ACTIVE = "active"
PROMO_SUPERSEDED = "superseded"
PROMO_FAILED = "failed"
PROMO_ROLLED_BACK = "rolled_back"
