"""Application settings, loaded from environment with sane defaults.

All settings can be overridden with environment variables prefixed ``SPARK_``,
e.g. ``SPARK_DATA_DIR=/var/lib/spark``.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SPARK_", env_file=".env", extra="ignore")

    # --- Persistence -----------------------------------------------------
    data_dir: str = Field(default="/data", description="Directory for sqlite db + secret key")

    # --- Security --------------------------------------------------------
    # Optional Fernet key (urlsafe base64, 32 bytes). If unset, a key is
    # generated and persisted under data_dir/secret.key on first start.
    secret_key: str | None = Field(default=None)
    # Portal login is deferred for v1; the dependency hook is wired but a no-op
    # until this is flipped on. Kept here so it is a one-line change later.
    auth_enabled: bool = Field(default=False)
    admin_password: str | None = Field(default=None)

    # --- Networking / serving --------------------------------------------
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8080)
    # Accepts a JSON array, a single origin, or a comma-separated list so setting
    # SPARK_CORS_ORIGINS=https://host doesn't crash at boot. NoDecode stops
    # pydantic-settings from JSON-parsing the env var before our validator runs.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173"]
    )

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors_origins(cls, v):
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("["):
                import json

                return json.loads(s)
            return [o.strip() for o in s.split(",") if o.strip()]
        return v

    # --- Cluster defaults (seed the singleton ClusterConfig row) ----------
    default_vllm_image: str = Field(default="nvcr.io/nvidia/vllm:26.05-py3")
    default_cluster_name: str = Field(default="spark-vllm")
    default_qsfp_netmask: int = Field(default=30)
    default_qsfp_iface: str = Field(default="enp1s0f1np1")
    default_models_subdir: str = Field(default="models")
    default_hf_cache_subdir: str = Field(default=".cache/huggingface")
    models_container_path: str = Field(default="/models")
    hf_cache_container_path: str = Field(default="/root/.cache/huggingface")
    ray_port: int = Field(default=6379)
    ray_dashboard_port: int = Field(default=8265)
    container_shm_size: str = Field(default="10.24gb")
    # Approx unified memory per DGX Spark node, GiB, for the memory budget view.
    node_memory_gib: int = Field(default=119)

    # --- MCP server (optional) -------------------------------------------
    # Expose the control plane over the Model Context Protocol (streamable-HTTP)
    # at ``/mcp`` for use as a Claude skill / MCP server. Fail-closed: the
    # endpoint is only mounted when it is both enabled AND a bearer token is
    # set, so it is never reachable without authentication.
    mcp_enabled: bool = Field(default=False, description="Mount the MCP server at /mcp")
    mcp_token: str | None = Field(
        default=None, description="Bearer token required on /mcp (SPARK_MCP_TOKEN)"
    )

    @property
    def mcp_active(self) -> bool:
        """Effective MCP state: enabled and a bearer token is configured."""
        return bool(self.mcp_enabled and self.mcp_token)

    # --- Status polling --------------------------------------------------
    status_poll_seconds: int = Field(default=10)
    ssh_connect_timeout: int = Field(default=15)
    # Where helper scripts + systemd units are installed on the nodes.
    node_install_dir: str = Field(default="/opt/spark-controlplane")

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "spark.sqlite3")

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path}"

    @property
    def secret_key_path(self) -> str:
        return os.path.join(self.data_dir, "secret.key")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    os.makedirs(settings.data_dir, exist_ok=True)
    return settings
