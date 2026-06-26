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

AUTH_PASSWORD = "password"
AUTH_KEY = "key"

SUDO_NOPASSWD = "nopasswd"
SUDO_PASSWORD = "password"

TOPO_CLUSTER = "cluster"   # vllm serve in the ray head container, TP across both nodes
TOPO_SINGLE = "single"     # standalone container pinned to one node, TP=1

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
    role: Mapped[str] = mapped_column(String(16), unique=True)  # head | worker
    name: Mapped[str] = mapped_column(String(64))               # hostname, e.g. spark-01
    lan_ip: Mapped[str] = mapped_column(String(64))
    qsfp_ip: Mapped[str] = mapped_column(String(64))
    qsfp_iface: Mapped[str] = mapped_column(String(32), default="enp1s0f1np1")

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
    qsfp_netmask: Mapped[int] = mapped_column(Integer, default=30)
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

    enable_tool_choice: Mapped[bool] = mapped_column(Boolean, default=True)
    tool_parser: Mapped[str | None] = mapped_column(String(32), nullable=True)
    extra_args: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)

    autostart: Mapped[bool] = mapped_column(Boolean, default=True)
    systemd_unit: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=INST_STOPPED)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    model: Mapped[ModelRegistry] = relationship(back_populates="instances")
    node: Mapped[Node | None] = relationship()


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
