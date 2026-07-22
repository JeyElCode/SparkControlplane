# Configuration Reference

This is the complete configuration reference for the Spark Control Plane (v1.0.11),
the single-container FastAPI + React portal that automates a 2-node NVIDIA DGX Spark
vLLM cluster.

Configuration comes from three layers:

1. **Environment variables** (`SPARK_*`) — read once at process start by
   `app/config.py` (pydantic-settings). These set process behaviour (data dir,
   secret key, networking, CORS) and **seed** the runtime cluster defaults on
   first boot. Changing them after first boot does **not** rewrite values already
   persisted in the database.
2. **Runtime cluster config** — the singleton `ClusterConfig` row, editable in the
   Settings page via `PATCH /api/cluster/config`.
3. **Per-node config** — one `Node` row per node (head + worker), captured during
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
| `SPARK_AUTH_ENABLED` | `auth_enabled` | `false` | Process | Portal login toggle. Deferred for v1 — the auth dependency is wired but a no-op until this is flipped on. |
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
