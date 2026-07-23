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
    auth_enabled: bool = Field(default=False)  # legacy: true + admin_password => "password" mode
    admin_password: str | None = Field(default=None)
    # --- Authentication ---------------------------------------------------
    # "none" (default): open portal, for trusted homelab networks.
    # "password": single admin credential (SPARK_ADMIN_USER/SPARK_ADMIN_PASSWORD).
    # "ldap": bind against a directory (see SPARK_LDAP_*).
    auth_mode: str = Field(default="none")
    admin_user: str = Field(default="admin")
    auth_session_hours: float = Field(default=24.0)
    auth_cookie_secure: bool = Field(default=False)  # set true when served over HTTPS
    # Bearer token that lets Prometheus scrape /metrics while auth is on.
    metrics_token: str | None = Field(default=None)
    # LDAP: either a direct-bind DN template ({username} placeholder), or a
    # service account + search (bind_dn/bind_password + user_search_base).
    ldap_url: str | None = Field(default=None)  # ldap://host:389 or ldaps://host:636
    ldap_user_dn_template: str | None = Field(default=None)  # e.g. uid={username},ou=people,dc=x
    ldap_bind_dn: str | None = Field(default=None)
    ldap_bind_password: str | None = Field(default=None)
    ldap_user_search_base: str | None = Field(default=None)
    ldap_user_filter: str = Field(default="(uid={username})")  # AD: (sAMAccountName={username})
    ldap_group_required: str | None = Field(default=None)  # group DN the user must belong to
    ldap_start_tls: bool = Field(default=False)

    @property
    def effective_auth_mode(self) -> str:
        """Resolved mode: explicit auth_mode wins; the legacy auth_enabled flag
        (with a password set) maps to "password". FAIL-CLOSED: any value other
        than exactly "none" requires auth — a misconfigured mode (typo, missing
        LDAP settings) locks logins out rather than silently opening the portal
        (fix the env and restart to recover)."""
        mode = (self.auth_mode or "none").strip().lower()
        if mode == "none" and self.auth_enabled and self.admin_password:
            mode = "password"
        return mode

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
    # /24 fits a switched QSFP fabric (3-4 nodes) and works fine for the 2-node
    # direct cable too. Existing deployments keep their stored value (e.g. /30).
    default_qsfp_netmask: int = Field(default=24)
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
    # Image for the optional per-instance nginx TLS sidecar (SPARK_TLS_PROXY_IMAGE).
    tls_proxy_image: str = Field(default="nginx:1.27-alpine")

    # --- MCP server (optional) -------------------------------------------
    # Expose the control plane over the Model Context Protocol (streamable-HTTP)
    # at ``/mcp`` for use as a Claude skill / MCP server. Fail-closed: the
    # endpoint is only mounted when it is both enabled AND a bearer token is
    # set, so it is never reachable without authentication.
    mcp_enabled: bool = Field(default=False, description="Mount the MCP server at /mcp")
    mcp_token: str | None = Field(
        default=None, description="Bearer token required on /mcp (SPARK_MCP_TOKEN)"
    )
    # When the MCP server runs behind a reverse proxy / ingress, the SDK's
    # DNS-rebinding protection rejects any Host header it doesn't know (HTTP 421
    # "Invalid Host header"). List the external host(s) here (comma-separated or
    # JSON). Empty = localhost only. A single "*" disables the host check
    # entirely (trusted-proxy mode). localhost/127.0.0.1 are always allowed.
    mcp_allowed_hosts: Annotated[list[str], NoDecode] = Field(default_factory=list)
    mcp_allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    @field_validator("mcp_allowed_hosts", "mcp_allowed_origins", mode="before")
    @classmethod
    def _split_mcp_list(cls, v):
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("["):
                import json

                return json.loads(s)
            return [o.strip() for o in s.split(",") if o.strip()]
        return v

    @property
    def mcp_active(self) -> bool:
        """Effective MCP state: enabled and a bearer token is configured."""
        return bool(self.mcp_enabled and self.mcp_token)

    # --- Status polling --------------------------------------------------
    status_poll_seconds: int = Field(default=10)
    # Telemetry engine: continuous server-side sampling (one batched SSH command
    # per node per fast tick), so dashboards read from cache instead of opening
    # SSH sessions per request. Slow tick covers Ray / QSFP / instance health.
    telemetry_fast_seconds: float = Field(default=3.0)
    telemetry_slow_seconds: float = Field(default=12.0)
    telemetry_history_minutes: int = Field(default=15)
    # Usage history: periodic rollup of vLLM token/request counters to SQLite.
    usage_rollup_seconds: float = Field(default=300.0)
    usage_retention_days: int = Field(default=90)
    # Instance scheduling: evaluation tick and the IANA timezone schedule
    # times are interpreted in (empty = the container/system timezone).
    schedule_tick_seconds: float = Field(default=60.0)
    schedule_tz: str = Field(default="")
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
