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
    # External LLM-judge endpoint (optional) for evaluations
    judge_base_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    judge_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    judge_api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Alerting: JSON blob of thresholds/durations (defaults merged in code) and
    # an optional notification webhook (URL may embed a token -> encrypted).
    alerts_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    alert_webhook_url_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class ModelRegistry(Base):
    __tablename__ = "models"

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[str] = mapped_column(String(255), unique=True)  # HF repo id
    name: Mapped[str] = mapped_column(String(255))                  # sanitized local dir name
    tool_parser: Mapped[str | None] = mapped_column(String(32), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=MS_ABSENT)
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
    tls_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    tls_port: Mapped[int] = mapped_column(Integer, default=443)
    tls_cert_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    tls_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)

    autostart: Mapped[bool] = mapped_column(Boolean, default=True)
    systemd_unit: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=INST_STOPPED)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    model: Mapped[ModelRegistry] = relationship(back_populates="instances")
    node: Mapped[Node | None] = relationship()


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
EVAL_CATEGORIES = ("coding", "security", "reasoning", "judging", "tools")
PERF_CATEGORIES = ("coding", "reasoning", "textgen", "judging")
SCORERS = ("exact", "contains", "numeric", "mcq", "judge", "code_exec", "tool_call")


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


class CustomTask(Base):
    """A user-authored capability task. List/dict fields are stored as JSON text."""

    __tablename__ = "custom_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    prompt: Mapped[str] = mapped_column(Text)
    system: Mapped[str | None] = mapped_column(Text, nullable=True)
    scorer: Mapped[str] = mapped_column(String(16))
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    contains_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    numeric_answer: Mapped[float | None] = mapped_column(Float, nullable=True)
    numeric_tol: Mapped[float] = mapped_column(Float, default=0.01)
    choices_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    correct: Mapped[str | None] = mapped_column(String(32), nullable=True)
    rubric: Mapped[str | None] = mapped_column(Text, nullable=True)
    entry_point: Mapped[str | None] = mapped_column(String(64), nullable=True)
    test_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    code_prefix: Mapped[str | None] = mapped_column(Text, nullable=True)
    tools_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_tool: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expected_args_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    forbid_tool_call: Mapped[bool] = mapped_column(Boolean, default=False)
    max_tokens: Mapped[int] = mapped_column(Integer, default=1024)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)
