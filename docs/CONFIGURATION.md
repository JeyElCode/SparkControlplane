# Configuration Reference

This is the complete configuration reference for the Spark Control Plane (v1.27.0),
the single-container FastAPI + React portal that automates a NVIDIA DGX Spark (up to 4-node)
vLLM cluster.

Configuration comes from three layers:

1. **Environment variables** (`SPARK_*`) — read once at process start by
   `app/config.py` (pydantic-settings). These set process behaviour (data dir,
   secret key, networking, CORS) and **seed** the runtime cluster defaults on
   first boot. Changing them after first boot does **not** rewrite values already
   persisted in the database.
2. **Runtime cluster config** — the singleton `ClusterConfig` row, editable in the
   Settings page via `PATCH /api/cluster/config`.
3. **Per-node config** — one `Node` row per node (head + up to 3 workers), captured during
   setup, with all secrets encrypted at rest.

All persisted state lives under `SPARK_DATA_DIR` (a SQLite database plus the
Fernet `secret.key`).

---

## 1. Environment variables (`SPARK_*`)

Settings are loaded by pydantic-settings with env prefix `SPARK_`, so a field
named `data_dir` is set with `SPARK_DATA_DIR`. A `.env` file in the working
directory is also read (`env_file=".env"`); unknown keys are ignored
(`extra="ignore"`).

In the **Role** column below:

- **Process** — affects the running process only.
- **Seed** — copied into the singleton `ClusterConfig` row the first time the
  database is initialised; after that the runtime value (Settings page /
  `PATCH /api/cluster/config`) wins. Editing the env var later has no effect on an
  existing database.
- **Runtime fixed** — read live from the env on each use; not stored in the DB and
  not editable from the UI.

| Env var | Field | Default | Role | Purpose |
|---|---|---|---|---|
| `SPARK_DATA_DIR` | `data_dir` | `/data` | Process | Directory holding the SQLite DB (`spark.sqlite3`) and `secret.key`. Created on start. |
| `SPARK_SECRET_KEY` | `secret_key` | _none_ → generated | Process | Fernet key (urlsafe base64, 32 bytes) used to encrypt secrets at rest. If unset, a key is generated and persisted to `<data_dir>/secret.key` on first start. See [§2](#2-secret-key-handling). |
| `SPARK_AUTH_MODE` | `auth_mode` | `none` | Process | Portal auth: `none` (open — homelab default), `password`, or `ldap`. **Fail-closed**: any other/misconfigured value requires auth but blocks logins. |
| `SPARK_AUTH_ENABLED` | `auth_enabled` | `false` | Process | Legacy toggle: `true` + `SPARK_ADMIN_PASSWORD` maps to `password` mode when `SPARK_AUTH_MODE` is unset. |
| `SPARK_ADMIN_USER` | `admin_user` | `admin` | Process | Username for `password` mode. |
| `SPARK_ADMIN_PASSWORD` | `admin_password` | _none_ | Process | Password for `password` mode (required for that mode). |
| `SPARK_AUTH_SESSION_HOURS` | `auth_session_hours` | `24` | Process | Session cookie lifetime. |
| `SPARK_AUTH_COOKIE_SECURE` | `auth_cookie_secure` | `false` | Process | Set `true` when the portal is served over HTTPS. |
| `SPARK_METRICS_TOKEN` | `metrics_token` | _none_ | Process | Bearer token allowing Prometheus to scrape `/metrics` while auth is on. |
| `SPARK_GATEWAY_TOKEN` | `gateway_token` | _none_ | Process | Bearer token for the `/v1` API gateway while auth is on (overrides the Settings-stored token). |
| `SPARK_LDAP_URL` | `ldap_url` | _none_ | Process | `ldap://host:389` or `ldaps://host:636` (required for `ldap` mode). |
| `SPARK_LDAP_USER_DN_TEMPLATE` | `ldap_user_dn_template` | _none_ | Process | Direct-bind DN template, e.g. `uid={username},ou=people,dc=example,dc=com`. |
| `SPARK_LDAP_BIND_DN` / `SPARK_LDAP_BIND_PASSWORD` | `ldap_bind_dn` / `ldap_bind_password` | _none_ | Process | Service account for search+bind (alternative to the DN template). |
| `SPARK_LDAP_USER_SEARCH_BASE` | `ldap_user_search_base` | _none_ | Process | Search base for the user lookup. |
| `SPARK_LDAP_USER_FILTER` | `ldap_user_filter` | `(uid={username})` | Process | User filter; Active Directory: `(sAMAccountName={username})`. |
| `SPARK_LDAP_GROUP_REQUIRED` | `ldap_group_required` | _none_ | Process | Group DN the user must be a `memberOf` to sign in. |
| `SPARK_LDAP_START_TLS` | `ldap_start_tls` | `false` | Process | Upgrade a plain `ldap://` connection with STARTTLS before binding. |
| `SPARK_LDAP_VERIFY_CERT` | `ldap_verify_cert` | `true` | Process | Validate the directory server's TLS certificate (ldaps:// and STARTTLS). Fail-closed: an invalid cert blocks logins. Disable only for self-signed lab DCs. |
| `SPARK_LDAP_CA_FILE` | `ldap_ca_file` | _none_ | Process | PEM CA bundle for validating the directory's certificate (enterprise/private CAs). |
| `SPARK_OIDC_ISSUER` | `oidc_issuer` | _none_ | Process | OpenID issuer URL (required for `oidc` mode). Entra: `https://login.microsoftonline.com/<tenant-guid>/v2.0`. **Use your tenant GUID, not `common`** — `common` returns a templated issuer that cannot be validated without also pinning `tid`. |
| `SPARK_OIDC_CLIENT_ID` / `SPARK_OIDC_CLIENT_SECRET` | — | _none_ | Process | The app registration's credentials. |
| `SPARK_OIDC_REDIRECT_URL` | `oidc_redirect_url` | _none_ | Process | Must equal the URI registered with the provider, byte for byte, e.g. `https://spark.example/api/auth/oidc/callback`. Taken from config and **never derived from the request** — behind an ingress the Host header is attacker-controllable, and a request-derived redirect URI is how authorization codes get delivered to someone else. |
| `SPARK_OIDC_GROUP_REQUIRED` | `oidc_group_required` | _none_ | Process | **Required.** The role/group a user must hold. Unlike LDAP's optional equivalent, oidc mode refuses to start without it — an optional check means authenticating an entire directory into a portal that SSHes to DGX nodes. |
| `SPARK_OIDC_GROUPS_CLAIM` | `oidc_groups_claim` | `roles` | Process | Which claim carries authorization. **App roles are recommended over group GUIDs**: values are strings you choose, assignment is per-application, and they avoid Entra's *groups overage* — above ~200 memberships Entra omits `groups` entirely, which would deny exactly your longest-tenured accounts while working fine in testing. |
| `SPARK_OIDC_SCOPES` | `oidc_scopes` | `openid profile email` | Process | Requested scopes. |
| `SPARK_OIDC_USERNAME_CLAIM` | `oidc_username_claim` | `preferred_username email sub` | Process | Claims tried in order for the display name. |
| `SPARK_OIDC_ALGORITHMS` | `oidc_algorithms` | `RS256` | Process | ID-token signature algorithms. **HMAC algorithms and `none` are rejected at startup** so the alg-confusion attack (the provider's public key used as a shared secret) is not expressible. |
| `SPARK_OIDC_CLOCK_SKEW_SECONDS` | `oidc_clock_skew_seconds` | `60` | Process | Leeway on `exp`/`iat`/`nbf`; capped at 300 — a generous skew allowance extends the life of every expired token by the same amount. |
| `SPARK_OIDC_JWKS_TTL_SECONDS` | `oidc_jwks_ttl_seconds` | `3600` | Process | Signing-key cache lifetime. |
| `SPARK_OIDC_JWKS_MAX_STALE_SECONDS` | `oidc_jwks_max_stale_seconds` | `86400` | Process | Ceiling on serving cached keys while the provider is unreachable. Serving stale across a blip is right; serving it forever means a key the provider **revoked** stays trusted for the length of the outage. |
| `SPARK_OIDC_MAX_SESSION_HOURS` | `oidc_max_session_hours` | `8` | Process | Session ceiling in oidc mode. This **is** the offboarding guarantee — see the note below. |
| `SPARK_OIDC_POST_LOGOUT_REDIRECT_URL` | `oidc_post_logout_redirect_url` | _none_ | Process | Where the provider returns the browser after a single sign-out. |

> **What SSO does and does not give you.** The portal deliberately holds no
> access or refresh token — it authenticates a human, it does not call the
> provider's APIs — so it never asks the provider anything again after sign-in.
> When an account is disabled in Entra, **the portal does not find out.** The
> honest guarantee is that a disabled account keeps working until its existing
> cookie expires, which is why oidc mode caps the session at 8h rather than the
> 24h default. Set `SPARK_OIDC_MAX_SESSION_HOURS` lower if your offboarding SLA
> is tighter. Sign-out does end the provider's session (`end_session_endpoint`),
> but only for a user who clicks it.
| `SPARK_ADMIN_PASSWORD` | `admin_password` | _none_ | Process | Admin password used when `auth_enabled` is on. No effect while auth is disabled. |
| `SPARK_HOST` | `host` | `0.0.0.0` | Process | Bind address. (Note: the container `CMD` passes `--host 0.0.0.0` to uvicorn explicitly; this field applies when you run the app yourself without that flag.) |
| `SPARK_PORT` | `port` | `8080` | Process | Listen port (same caveat as `host`). |
| `SPARK_CORS_ORIGINS` | `cors_origins` | `["http://localhost:5173"]` | Process | Allowed CORS origins. Accepts a JSON array, a single origin, or a comma-separated list. See [§3](#3-cors-origins-formats). |
| `SPARK_DEFAULT_VLLM_IMAGE` | `default_vllm_image` | `nvcr.io/nvidia/vllm:26.05-py3` | Seed → `ClusterConfig.vllm_image` | Default vLLM/Ray container image for the cluster. |
| `SPARK_DEFAULT_CLUSTER_NAME` | `default_cluster_name` | `spark-vllm` | Seed → `ClusterConfig.cluster_name` | Default cluster name. |
| `SPARK_DEFAULT_QSFP_NETMASK` | `default_qsfp_netmask` | `24` | Seed → `ClusterConfig.qsfp_netmask` | CIDR prefix length for the QSFP fabric (`/24` fits 2-4 nodes; a 2-node direct cable works with any prefix — existing deployments keep their stored value, e.g. `/30`). |
| `SPARK_DEFAULT_QSFP_IFACE` | `default_qsfp_iface` | `enp1s0f1np1` | Seed → `Node.qsfp_iface` | Default QSFP interface name on each node. |
| `SPARK_DEFAULT_MODELS_SUBDIR` | `default_models_subdir` | `models` | Seed → `ClusterConfig.models_subdir` | Host-side subdirectory (under the node's data root) where model weights live. |
| `SPARK_DEFAULT_HF_CACHE_SUBDIR` | `default_hf_cache_subdir` | `.cache/huggingface` | Seed → `ClusterConfig.hf_cache_subdir` | Host-side subdirectory for the Hugging Face cache. |
| `SPARK_MODELS_CONTAINER_PATH` | `models_container_path` | `/models` | Seed → `ClusterConfig.models_container_path` | Mount path for the models directory **inside** the serving container. |
| `SPARK_HF_CACHE_CONTAINER_PATH` | `hf_cache_container_path` | `/root/.cache/huggingface` | Seed → `ClusterConfig.hf_cache_container_path` | Mount path for the HF cache **inside** the serving container. |
| `SPARK_RAY_PORT` | `ray_port` | `6379` | Seed → `ClusterConfig.ray_port` | Ray GCS / head port. |
| `SPARK_RAY_DASHBOARD_PORT` | `ray_dashboard_port` | `8265` | Runtime fixed | Ray dashboard port. Used when rendering the Ray head startup script; **not** stored in `ClusterConfig`. |
| `SPARK_CONTAINER_SHM_SIZE` | `container_shm_size` | `10.24gb` | Seed → `ClusterConfig.shm_size` | `--shm-size` for serving containers (NCCL/IPC shared memory). See [§8](#8-container-shared-memory--nvidia-ulimits). |
| `SPARK_NODE_MEMORY_GIB` | `node_memory_gib` | `119` | Runtime fixed | Approximate unified memory per DGX Spark node (GiB), used by the memory-budget view on the dashboard. |
| `SPARK_STATUS_POLL_SECONDS` | `status_poll_seconds` | `10` | Seed → `Setting.status_poll_seconds` | Status polling interval (seconds). The runtime value lives on the `Setting` singleton. |
| `SPARK_SSH_CONNECT_TIMEOUT` | `ssh_connect_timeout` | `15` | Runtime fixed | asyncssh connect timeout (seconds) for all node operations. |
| `SPARK_TELEMETRY_FAST_SECONDS` | `telemetry_fast_seconds` | `3.0` | Runtime fixed | Telemetry engine fast tick: one batched SSH sample per node (GPU/CPU/mem/net/disk/uptime/processes). |
| `SPARK_TELEMETRY_SLOW_SECONDS` | `telemetry_slow_seconds` | `12.0` | Runtime fixed | Telemetry engine slow tick: Ray status, QSFP ping, per-instance systemd + `/health` probes. |
| `SPARK_TELEMETRY_HISTORY_MINUTES` | `telemetry_history_minutes` | `15` | Runtime fixed | Length of the in-memory per-node history ring served by `GET /api/status/history`. |
| `SPARK_USAGE_ROLLUP_SECONDS` | `usage_rollup_seconds` | `300` | Runtime fixed | Interval for persisting vLLM token/request counter rollups to the `usage_samples` table. |
| `SPARK_USAGE_RETENTION_DAYS` | `usage_retention_days` | `90` | Runtime fixed | How long usage-history rows are kept before being purged. |
| `SPARK_SCHEDULE_TICK_SECONDS` | `schedule_tick_seconds` | `60` | Runtime fixed | How often instance live-windows are evaluated. |
| `SPARK_RECONCILE_ENABLED` | `reconcile_enabled` | `true` | Runtime fixed | Reconcile recorded instance status against the nodes (promote on a healthy `/health`, demote a dead instance to `error`). Kill switch. |
| `SPARK_RECONCILE_TICK_SECONDS` | `reconcile_tick_seconds` | `10` | Runtime fixed | How often the status observer runs. Reads the telemetry caches; opens no SSH of its own. |
| `SPARK_RECONCILE_START_DEADLINE_SECONDS` | `reconcile_start_deadline_seconds` | `1800` | Runtime fixed | How long an instance may be `starting` without ever going healthy before it is called failed. Generous on purpose — a large FP8 model takes many minutes to load; crash-loop and dead-unit detection catch real failures far sooner. |
| `SPARK_RECONCILE_UNHEALTHY_SECONDS` | `reconcile_unhealthy_seconds` | `120` | Runtime fixed | How long a previously-healthy instance must fail `/health` before demotion, so one missed scrape doesn't flap it. |
| `SPARK_RECONCILE_UNIT_DEAD_SECONDS` | `reconcile_unit_dead_seconds` | `45` | Runtime fixed | How long the systemd unit must read inactive/failed before demotion (must exceed the unit's `RestartSec`). |
| `SPARK_RECONCILE_CRASHLOOP_RESTARTS` | `reconcile_crashloop_restarts` | `3` | Runtime fixed | Restarts observed while never once healthy before the instance is called a crash loop — the signal that catches an out-of-memory at model load. |
| `SPARK_GATEWAY_MAX_CONCURRENT` | `gateway_max_concurrent` | `0` | Runtime fixed | Default cap on concurrent in-flight `/v1` requests per client, per instance. `0` = unlimited (the default — an upgrade must not start throttling working traffic). Overridable per API key. Concurrency, not RPM, is the meaningful limit: with continuous batching a client's in-flight count is its share of the KV cache. |
| `SPARK_GATEWAY_MAX_RPM` | `gateway_max_rpm` | `0` | Runtime fixed | Default requests-per-minute cap per client. `0` = unlimited. A secondary guard against connect storms. |
| `SPARK_GATEWAY_LIMIT_SESSION` | `gateway_limit_session` | `false` | Runtime fixed | Apply the limits to the operator's own logged-in portal session (Playground, evals). Off by default — locking yourself out of your own portal while chasing a runaway client is the wrong failure mode. |
| `SPARK_GATEWAY_ROLLUP_SECONDS` | `gateway_rollup_seconds` | `300` | Runtime fixed | How often in-memory gateway counters are flushed to the `gateway_samples` table. Rows are aggregates per (client, model); no per-request row is ever written. Retention follows `SPARK_USAGE_RETENTION_DAYS`. |
| `SPARK_SCHEDULE_TZ` | `schedule_tz` | _system_ | Runtime fixed | IANA timezone schedule times are interpreted in (e.g. `Europe/Oslo`). |
| `SPARK_SCHEDULE_RETRY_SECONDS` | — | `120` | Process | Backoff before re-issuing a scheduled start/stop whose job failed (max 5 attempts). |
| `SPARK_NODE_INSTALL_DIR` | `node_install_dir` | `/opt/spark-controlplane` | Runtime fixed | Where helper scripts + systemd units are installed **on the nodes**. |
| `SPARK_MCP_ENABLED` | `mcp_enabled` | `false` | Process | Mount the streamable-HTTP MCP server at `/mcp`. Fail-closed: has no effect unless `SPARK_MCP_TOKEN` is also set. See [MCP.md](MCP.md). |
| `SPARK_MCP_TOKEN` | `mcp_token` | _none_ | Process | Bearer token required on every `/mcp` request. When unset the endpoint stays disabled even if `mcp_enabled` is on. |

`SPARK_FRONTEND_DIR` is also read, but by `app/main.py` (not pydantic-settings) —
see [§4](#4-frontend-dir).

### Derived paths

Three convenience properties are computed from `data_dir` and are not separately
configurable:

- `db_path` → `<data_dir>/spark.sqlite3`
- `db_url` → `sqlite+aiosqlite:///<data_dir>/spark.sqlite3`
- `secret_key_path` → `<data_dir>/secret.key`

---

## 2. Secret key handling

All secrets — SSH passwords, SSH private keys and key passphrases, sudo passwords,
the Hugging Face token, and per-instance API keys — are encrypted at rest with
[Fernet](https://cryptography.io/en/latest/fernet/). Encrypted columns end in
`_enc` and hold Fernet tokens.

Key resolution:

1. If `SPARK_SECRET_KEY` is set, that key is used.
2. Otherwise a key is generated and written to `<data_dir>/secret.key` on first
   start, and reused on subsequent starts.

**Set and back up your key.** The encryption key and the encrypted database are a
matched pair:

- If you do **not** set `SPARK_SECRET_KEY` and the `secret.key` file is lost
  (e.g. the `/data` volume is recreated without it), every stored secret becomes
  undecryptable — you will have to re-enter SSH/sudo credentials, the HF token,
  and instance API keys.
- Setting a stable `SPARK_SECRET_KEY` decouples the key from the volume, so stored
  secrets survive container re-creation even if `/data` is wiped. The
  `docker-compose.yml` calls this out as strongly recommended.

Generate a key with:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Then set it (compose example):

```yaml
environment:
  SPARK_SECRET_KEY: "<your-generated-fernet-key>"
  SPARK_DATA_DIR: /data
```

Keep the key in a secrets manager and back it up alongside (or independently of)
the `/data` volume.

---

## 3. CORS origins formats

`SPARK_CORS_ORIGINS` is parsed by a `mode="before"` validator, with `NoDecode`
applied so pydantic-settings does **not** JSON-decode the env var before the
validator runs (this prevents a boot-time crash when you pass a bare origin).

Accepted formats:

| Format | Example | Result |
|---|---|---|
| JSON array | `["https://a.example","https://b.example"]` | parsed as JSON |
| Single origin | `https://portal.example` | `["https://portal.example"]` |
| Comma-separated list | `https://a.example, https://b.example` | split on commas, trimmed; empty entries dropped |

Detection is by the leading character: a value starting with `[` is treated as
JSON; anything else is comma-split. Default is `["http://localhost:5173"]` (the
Vite dev server).

---

## 4. `SPARK_FRONTEND_DIR`

`SPARK_FRONTEND_DIR` points at the directory containing the built SPA
(`index.html` plus `assets/`). It is resolved by `app/main.py`, which checks
candidates in priority order:

1. `$SPARK_FRONTEND_DIR` (if set)
2. `app/static` (a packaged wheel that bundles the build)
3. `<repo>/frontend/dist` (editable / source checkout)

The first candidate that contains `index.html` wins; the SPA is then mounted and
unmatched non-`/api` routes fall through to `index.html`. If none is found, the
app logs a warning and **serves the API only** (no UI).

When you need to set it:

- **Official image:** not needed. The Dockerfile sets
  `SPARK_FRONTEND_DIR=/app/frontend/dist` and copies the built SPA there.
- **Bare `pip install` / running uvicorn yourself:** set it (or place the build at
  `app/static` or `<repo>/frontend/dist`) if the SPA isn't auto-discovered.
  Otherwise the portal answers API requests but serves no UI.

---

## 5. Runtime cluster config (`ClusterConfig`)

Cluster-wide settings are stored in a single `ClusterConfig` row (`id=1`), seeded
from the `SPARK_DEFAULT_*` env vars on first init and thereafter edited in the
Settings page via `PATCH /api/cluster/config`. These take effect the next time the
relevant scripts are rendered/run (Ray cluster, serving containers).

| Field | DB default | Seeded from | Purpose |
|---|---|---|---|
| `cluster_name` | `spark-vllm` | `SPARK_DEFAULT_CLUSTER_NAME` | Cluster name. |
| `vllm_image` | _(set from seed)_ | `SPARK_DEFAULT_VLLM_IMAGE` | vLLM/Ray container image. |
| `qsfp_netmask` | `30` | `SPARK_DEFAULT_QSFP_NETMASK` | CIDR prefix for the QSFP link. |
| `models_subdir` | `models` | `SPARK_DEFAULT_MODELS_SUBDIR` | Host-side models subdirectory. |
| `hf_cache_subdir` | `.cache/huggingface` | `SPARK_DEFAULT_HF_CACHE_SUBDIR` | Host-side HF cache subdirectory. |
| `models_container_path` | `/models` | `SPARK_MODELS_CONTAINER_PATH` | In-container models mount path. |
| `hf_cache_container_path` | `/root/.cache/huggingface` | `SPARK_HF_CACHE_CONTAINER_PATH` | In-container HF cache mount path. |
| `ray_port` | `6379` | `SPARK_RAY_PORT` | Ray GCS / head port. |
| `shm_size` | `10.24gb` | `SPARK_CONTAINER_SHM_SIZE` | `--shm-size` for serving containers. |

> Note: `ray_dashboard_port` and `node_memory_gib` are **not** part of
> `ClusterConfig`; they are read live from the environment (see [§1](#1-environment-variables-spark_)).

### Portal settings + secrets (`Setting`)

A separate singleton `Setting` row (`id=1`) holds portal-level state:

| Field | DB default | Purpose |
|---|---|---|
| `hf_token_enc` | _none_ | Hugging Face token, **encrypted** (Fernet). Used for gated/private model pulls. |
| `status_poll_seconds` | `10` | Status polling interval; seeded from `SPARK_STATUS_POLL_SECONDS`. |
| `setup_complete` | `false` | Whether the guided setup wizard has finished. |

---

## 6. Per-node config (`Node`)

Each node is one `Node` row, with `role` unique across the table (`head` |
`worker`). Captured during setup and editable afterwards. All credentials are
stored in `_enc` (Fernet-encrypted) columns.

| Field | Type / default | Purpose |
|---|---|---|
| `role` | `head` \| `worker` (unique) | Node role in the cluster. |
| `name` | string | Hostname, e.g. `spark-01`. |
| `lan_ip` | string | Management/LAN IP the portal SSHes to. |
| `qsfp_ip` | string | IP on the QSFP 10.10.10.x link (carries Ray/NCCL/UCX/Gloo **and** model sync). |
| `qsfp_iface` | string, default `enp1s0f1np1` | QSFP interface name on the node. |
| `ssh_user` | string | SSH username. |
| `ssh_port` | int, default `22` | SSH port. |
| `auth_method` | `password` \| `key`, default `password` | SSH auth method. |
| `ssh_password_enc` | encrypted, nullable | SSH password (when `auth_method=password`). |
| `ssh_private_key_enc` | encrypted, nullable | SSH private key (when `auth_method=key`). |
| `ssh_key_passphrase_enc` | encrypted, nullable | Passphrase for the private key, if any. |
| `sudo_mode` | `nopasswd` \| `password`, default `password` | How privileged commands are escalated. |
| `sudo_password_enc` | encrypted, nullable | Sudo password (when `sudo_mode=password`). |
| `hardened` | bool, default `false` | True once a portal-generated SSH key has been installed on the node. |

All node operations run over `asyncssh` to the node's `lan_ip:ssh_port` with the
`SPARK_SSH_CONNECT_TIMEOUT` connect timeout.

---

## 7. The `/data` volume

`SPARK_DATA_DIR` (default `/data`) is the only persistent state. It contains:

- `spark.sqlite3` — the SQLAlchemy/aiosqlite database (nodes, cluster config,
  settings, model registry, instances, jobs + logs).
- `secret.key` — the generated Fernet key, **only if** `SPARK_SECRET_KEY` was not
  supplied via env.

Persistence notes:

- The Dockerfile declares `VOLUME ["/data"]` and creates it owned by the
  unprivileged `spark` user (uid `10001`). The compose file bind-mounts
  `./data:/data`.
- The entrypoint (`docker-entrypoint.sh`) `mkdir -p`s the data dir, `chown`s it to
  `spark`, then drops privileges via `gosu`.
- Back up the whole `/data` directory. If you rely on the generated `secret.key`
  (no `SPARK_SECRET_KEY`), the key and the DB **must** be backed up together — see
  [§2](#2-secret-key-handling).

---

## 8. Container shared memory + NVIDIA ulimits

Every Ray/vLLM serving container (Ray head, Ray worker, single-node instance) is
launched with the same memory/IPC tuning, rendered in
`app/services/templates.py`:

```
--network host --shm-size <shm> --gpus all \
--ulimit memlock=-1 --ulimit stack=67108864 \
```

- **`--shm-size`** comes from `ClusterConfig.shm_size` (default `10.24gb`, seeded
  from `SPARK_CONTAINER_SHM_SIZE`). This is the `/dev/shm` size used for NCCL and
  inter-process shared memory. Adjust it in the Settings page if you hit shared
  memory errors during multi-GPU serving.
- **`--ulimit memlock=-1`** removes the locked-memory limit (unlimited), required
  so NCCL/CUDA can pin host memory for GPU transfers.
- **`--ulimit stack=67108864`** sets a 64 MiB stack limit.
- **`--gpus all`** and **`--network host`** are fixed (host networking is needed
  for Ray/NCCL/UCX over the QSFP link).

`--shm-size` is the only one of these that is configurable; the ulimits are
hard-coded in the container launch templates.
