"""Application settings, loaded from environment with sane defaults.

All settings can be overridden with environment variables prefixed ``SPARK_``,
e.g. ``SPARK_DATA_DIR=/var/lib/spark``.
"""

from __future__ import annotations

import os
import re
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
    # Bearer token for the /v1 API gateway (env override; the Settings-stored
    # token is used when this is unset). Only enforced when auth is on.
    gateway_token: str | None = Field(default=None)
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
    # TLS certificate validation for ldaps:// and STARTTLS (fail-closed: an
    # invalid cert blocks logins). Disable only for self-signed lab DCs, or
    # point ldap_ca_file at your enterprise CA bundle instead.
    ldap_verify_cert: bool = Field(default=True)
    ldap_ca_file: str | None = Field(default=None)

    # --- OIDC / SSO (auth_mode="oidc") -----------------------------------
    # Authorization code + PKCE against an OpenID provider (Entra ID, Keycloak,
    # Okta). The portal never sees a password, and MFA / conditional access are
    # enforced upstream.
    #
    # Use your TENANT issuer, not Entra's `common` endpoint: `common` returns a
    # templated issuer that can only be validated by also pinning `tid`, and a
    # half-validated issuer is worse than none.
    oidc_issuer: str | None = Field(default=None)
    oidc_client_id: str | None = Field(default=None)
    oidc_client_secret: str | None = Field(default=None)
    # The redirect URI is taken from CONFIG, never derived from the request:
    # behind an ingress the Host header is attacker-controllable, and a
    # request-derived redirect_uri is how authorization codes get delivered to
    # someone else. Must match what is registered with the IdP, byte for byte.
    oidc_redirect_url: str | None = Field(default=None)
    oidc_post_logout_redirect_url: str | None = Field(default=None)
    oidc_scopes: str = Field(default="openid profile email")
    # Which claim carries authorization, and the value a user must hold. Entra
    # APP ROLES (`roles`) are the recommended default over group GUIDs: the
    # values are strings you choose, assignment is per-application rather than
    # tenant-wide, and they are immune to the groups-overage problem where Entra
    # omits `groups` entirely for users in ~200+ groups — which would deny
    # exactly the longest-tenured accounts while working fine in testing.
    oidc_groups_claim: str = Field(default="roles")
    # REQUIRED in oidc mode. The LDAP lesson (#52): an optional group check
    # means the default deployment authenticates an entire directory into a
    # portal that SSHes to DGX nodes as root.
    oidc_group_required: str | None = Field(default=None)
    # Claim to use as the display username, first match wins.
    oidc_username_claim: str = Field(default="preferred_username email sub")
    # Signature algorithms. HS* and "none" are rejected at parse time so the
    # config surface cannot express the alg-confusion mistake at all.
    oidc_algorithms: str = Field(default="RS256")
    oidc_clock_skew_seconds: int = Field(default=60)
    oidc_jwks_ttl_seconds: int = Field(default=3600)
    # Hard ceiling on serving a stale JWKS when the IdP is unreachable. Serving
    # stale across a blip is right; serving it forever means a key the IdP
    # rotated out — or revoked after a compromise — stays trusted for as long as
    # the outage lasts.
    oidc_jwks_max_stale_seconds: int = Field(default=86400)
    oidc_http_timeout_seconds: float = Field(default=10.0)
    # Session lifetime ceiling in oidc mode. This IS the offboarding guarantee:
    # the portal holds no refresh token and never re-asks the provider, so a
    # disabled account keeps full control until its cookie expires.
    oidc_max_session_hours: float = Field(default=8.0)

    @field_validator("oidc_algorithms")
    @classmethod
    def _check_oidc_algs(cls, v: str) -> str:
        allowed = {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512",
                   "ES256", "ES384", "ES512"}
        algs = [a.strip().upper() for a in re.split(r"[,\s]+", v or "") if a.strip()]
        if not algs:
            raise ValueError("SPARK_OIDC_ALGORITHMS must list at least one algorithm")
        bad = [a for a in algs if a not in allowed]
        if bad:
            raise ValueError(
                f"unsupported ID-token algorithm(s): {', '.join(bad)}. Only asymmetric "
                f"algorithms are allowed — an HMAC algorithm here enables the "
                f"alg-confusion attack where the IdP's PUBLIC key is used as the "
                f"shared secret."
            )
        return " ".join(algs)

    @field_validator("oidc_clock_skew_seconds")
    @classmethod
    def _cap_skew(cls, v: int) -> int:
        # A generous skew allowance is a config-shaped vulnerability: it extends
        # the life of every expired token by the same amount.
        if not (0 <= v <= 300):
            raise ValueError("SPARK_OIDC_CLOCK_SKEW_SECONDS must be 0-300")
        return v

    @property
    def oidc_config_error(self) -> str | None:
        """Why oidc mode cannot serve, or None when it is usable. Fail-closed:
        a misconfigured mode blocks logins rather than falling back to
        something weaker."""
        if self.effective_auth_mode != "oidc":
            return None
        missing = [
            name for name, value in (
                ("SPARK_OIDC_ISSUER", self.oidc_issuer),
                ("SPARK_OIDC_CLIENT_ID", self.oidc_client_id),
                ("SPARK_OIDC_CLIENT_SECRET", self.oidc_client_secret),
                ("SPARK_OIDC_REDIRECT_URL", self.oidc_redirect_url),
                ("SPARK_OIDC_GROUP_REQUIRED", self.oidc_group_required),
            ) if not value
        ]
        if missing:
            return f"OIDC is not configured: set {', '.join(missing)}."
        if not str(self.oidc_issuer).startswith("https://"):
            return "SPARK_OIDC_ISSUER must be an https:// URL."
        if not str(self.oidc_redirect_url).startswith(("https://", "http://localhost")):
            return "SPARK_OIDC_REDIRECT_URL must be https:// (or http://localhost for dev)."
        return None

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
    # --- Gateway limits + observability ----------------------------------
    # Default per-client caps, overridable per API key. 0 = unlimited, which is
    # the DEFAULT: an upgrade must never start throttling traffic that was
    # working yesterday. Concurrency is per (client, instance) — KV cache is
    # per-instance, so a client using two models isn't hurting either one.
    gateway_max_concurrent: int = Field(default=0)
    gateway_max_rpm: int = Field(default=0)
    # The operator's own Playground/session traffic is exempt by default; it is
    # interactive, low-volume, and locking yourself out of your own portal while
    # debugging a runaway client is the wrong failure mode.
    gateway_limit_session: bool = Field(default=False)
    # How often in-memory gateway aggregates are flushed to gateway_samples.
    gateway_rollup_seconds: float = Field(default=300.0)
    ssh_connect_timeout: int = Field(default=15)
    # --- Instance status reconciliation (services/reconcile.py) -----------
    # The observer that corrects `Instance.status` against what the nodes
    # actually report, so the gateway stops routing to dead upstreams.
    reconcile_enabled: bool = Field(default=True)  # kill switch
    reconcile_tick_seconds: float = Field(default=10.0)
    # How long an instance may be starting without ever going healthy before it
    # is called failed. Large FP8 models legitimately take many minutes to load
    # (Laguna-class ≈ 6-10 min), so this is deliberately generous — crash-loop
    # and dead-unit detection catch real failures long before it expires.
    reconcile_start_deadline_seconds: float = Field(default=1800.0)
    # A previously-healthy instance must look dead this long before demotion,
    # so a single missed scrape or a brief GC pause doesn't flap it to error.
    reconcile_unhealthy_seconds: float = Field(default=120.0)
    # A dead systemd unit is definite, but `Restart=on-failure` means a crash
    # loop reads as "active" between restarts; hold it briefly (> RestartSec).
    reconcile_unit_dead_seconds: float = Field(default=45.0)
    # Restarts observed while never once healthy = crash loop (e.g. vLLM OOM at
    # load). This is what catches an OOM in ~40s instead of 30 minutes.
    reconcile_crashloop_restarts: int = Field(default=3)
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
