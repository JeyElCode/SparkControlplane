# Spark Control Plane — Architecture

Technical reference for engineers working on the Spark Control Plane: a single-container
web portal (FastAPI + React) that provisions and operates a 2-node NVIDIA DGX Spark vLLM
cluster entirely over SSH.

- **Version:** 1.23.0
- **Image:** `ghcr.io/jeyelcode/spark-controlplane`
- **Backend:** Python 3.12, FastAPI, `asyncssh`, SQLAlchemy 2.0 async + `aiosqlite`
- **Frontend:** React + Vite + TypeScript, served as static assets by the API
- **Deployment:** user-managed (Rancher/RKE2 + ArgoCD). CI builds and publishes images on
  git tag `v*`. This document does not cover cluster rollout.

---

## 1. High-level topology

The portal is a stateless-ish web app (its only persistent state is a SQLite DB and an
encryption key under `SPARK_DATA_DIR`). It never runs on the DGX Spark nodes themselves —
it reaches both nodes over SSH on their **LAN IPs**. The dedicated **QSFP 10.10.10.x link**
is used *inside the cluster* for Ray/NCCL/UCX/Gloo traffic and for model sync (rsync).

```
                         ┌───────────────────────────────────────┐
                         │        Spark Control Plane (pod)        │
                         │  FastAPI  ──  Job manager  ──  SQLite    │
                         │     │            │              (/data)  │
                         │  React SPA   asyncssh pool              │
                         └───────┬──────────────┬─────────────────┘
                                 │ SSH (LAN IP)  │ SSH (LAN IP)
                                 ▼               ▼
                     ┌────────────────┐   ┌────────────────┐
                     │   HEAD node    │   │  WORKER node   │
                     │ spark-ray-head │   │spark-ray-worker│
                     │ spark-vllm-*   │   │ spark-vllm-*   │
                     │   (GPU x1)     │   │   (GPU x1)     │
                     └───────┬────────┘   └───────┬────────┘
                             │  QSFP 10.10.10.x/30 │
                             └─────────────────────┘
                   Ray / NCCL / UCX / Gloo  +  rsync model sync
```

Key invariants:

- **SSH login always uses the LAN IP.** The QSFP IP is only ever used *between* the nodes.
- **Both nodes are reached independently** from the portal; the portal does not bounce
  commands through the head.
- **The control plane is single-replica.** Some live state (per-node transfer progress) is
  held in an in-memory module dict and is lost on restart.

---

## 2. Component / package layout

### Backend (`backend/app`)

```
app/
  main.py            FastAPI app: lifespan, CORS, router wiring, SPA serving
  config.py          pydantic-settings (env prefix SPARK_)
  crypto.py          Fernet encrypt/decrypt for secrets at rest
  db.py              async engine/session, init_db (create tables + seed singletons)
  models.py          SQLAlchemy ORM models + string enum constants
  schemas.py         Pydantic request/response models + boundary validators
  ssh/
    client.py        SSHClient: base64-pipe command pattern, sudo -S, file helpers
    pool.py          SSHPool: one multiplexed connection per node
  services/
    jobs.py          Background Job manager + pub/sub for WS log streaming
    phases.py        The 9-phase setup pipeline (idempotent SSH automation)
    cluster.py       setup / test / harden / teardown orchestration
    templates.py     Renderers for scripts, systemd units, vllm serve commands
    nodeops.py       docker / systemctl / install-file helpers over SSHClient
    instances.py     vLLM instance lifecycle (start/stop/delete)
    models_svc.py    Model registry: validate/download/sync/discover/delete
    status_svc.py    Live status aggregation (GPU, mem, Ray, /health, budget)
    paths.py         Per-node host path derivation from ssh_user + cluster config
    parsers.py       tool-parser mapping, name sanitization, suggestion catalog
  routers/
    nodes.py cluster.py models.py instances.py status.py jobs.py playground.py
```

Routers are thin: they validate input via `schemas.py`, call a service, and either return a
serialized model or a `JobAccepted` for long-running work. All business logic lives in
`services/`.

### Frontend (`frontend/src`)

```
src/
  main.tsx           React entry
  App.tsx            sidebar nav + react-router routes + cluster health pill
  pages/             Dashboard, Setup, Nodes, Models, Instances, Playground,
                     Teardown, Settings  (one page per nav item)
  components/        JobLogPanel (live WS log viewer), Toast, ui (Badge/Meter/…)
  lib/
    api.ts           typed fetch client + wsUrl() helper (mirrors schemas.py)
    hooks.ts         usePoll(fn, intervalMs): load-on-mount + optional polling
    format.ts        formatting + status→badge-kind helpers
  styles.css
```

The SPA talks to the same-origin `/api` surface. In dev, Vite proxies to `:8080`. The
`JobLogPanel` opens a WebSocket per job; most pages use `usePoll` for periodic refresh.

---

## 3. Data model

All tables are created by `init_db()` (`Base.metadata.create_all`). `ClusterConfig` and
`Setting` are **singletons** (`id=1`) seeded from `config.py` defaults on first start.
String "enum" columns are plain strings with constants in `models.py`; encrypted columns end
in `_enc` and hold Fernet tokens.

| Table | Model | Purpose |
|---|---|---|
| `nodes` | `Node` | One row per cluster node (`role` is unique: `head` / `worker`). Holds LAN/QSFP IPs, QSFP iface, SSH connection params, auth method, encrypted SSH password / private key / key passphrase, sudo mode + encrypted sudo password, and a `hardened` flag. |
| `cluster_config` | `ClusterConfig` | Singleton cluster-wide config editable at runtime: `cluster_name`, `vllm_image`, `qsfp_netmask`, `models_subdir`, `hf_cache_subdir`, `models_container_path`, `hf_cache_container_path`, `ray_port`, `shm_size`. |
| `settings` | `Setting` | Singleton portal settings + secrets: encrypted `hf_token_enc`, `status_poll_seconds`, and the `setup_complete` flag set by the verify phase. |
| `models` | `ModelRegistry` | One row per registered model: HF `repo_id` (unique), sanitized local `name`, `tool_parser`, total `size_bytes`, aggregate `status`, free-text `notes`. |
| `model_node_states` | `ModelNodeState` | Per-(model, node) presence: `present`, `size_bytes`, `checksum_ok`, per-node `status`, last job id. Unique on `(model_id, node_id)`; cascades from both parents. |
| `instances` | `Instance` | One vLLM instance: `name` (unique), `model_id`, `topology` (`cluster`/`single`), optional `node_id` (single only), `port`, TP size, vLLM tuning fields, tool-choice config, `extra_args`, encrypted `api_key_enc`, `autostart`, `systemd_unit`, `status`, `last_error`. |
| `jobs` | `Job` | A tracked background operation: `type` (e.g. `setup.network`, `model.download`), `title`, `status`, optional `node_id`/`target`, `progress` (0..1 when known), `exit_code`, `summary`, timestamps. |
| `job_logs` | `JobLog` | Ordered log lines for a job: `seq`, `ts`, `stream` (`info`/`stdout`/`stderr`/`error`), `text`. Cascades from `jobs`. |

Relationships: `ModelRegistry` → `ModelNodeState` (cascade delete) and → `Instance`;
`Instance` → `ModelRegistry` and optional `Node`; `Job` → `JobLog` (cascade delete).

Secrets are accepted on input but never serialized back out. Output schemas expose `has_*`
booleans (`has_ssh_password`, `has_hf_token`, `has_api_key`, …) so the UI knows a secret is
stored without ever seeing it.

---

## 4. The SSH layer (`ssh/`)

### Command pattern: base64-pipe

Every command is shipped to the node **base64-encoded and decoded remotely**:

```
echo <b64> | base64 -d | bash
```

This means callers never have to quote multi-line scripts, embedded quotes, `$`, etc. The
remote command is then wrapped depending on whether it needs root:

| Mode | Wrapper |
|---|---|
| no sudo | `bash -c <inner>` |
| sudo, `nopasswd` | `sudo -n bash -c <inner>` |
| sudo, `password` | `sudo -S -p '' bash -c <inner>` with the sudo password written as the first line of stdin |

`SSHClient.run()` creates a remote process, pumps `stdout`/`stderr` line-by-line into an
optional `log_cb(stream, line)` callback (used to stream into a job's log), and returns a
`RunResult(exit_status, stdout, stderr)`. Helpers built on top: `write_file` (also base64,
with `mkdir -p` + optional `chmod`), `read_file`, `exists`.

Connection options: `known_hosts=None` (lab cluster; host keys are not pinned),
`connect_timeout` from `SPARK_SSH_CONNECT_TIMEOUT`. Key auth imports the decrypted private
key and, during `harden` transitions, also keeps the password as a fallback.

### `NodeConn`

A frozen-ish dataclass holding **decrypted** connection params (`NodeConn.from_node`
decrypts the `_enc` columns via `crypto.decrypt`). It carries no ORM/session reference. Value
equality is what lets the pool decide whether to reconnect.

### Connection pool (`pool.py`)

`SSHPool` keeps one connected `SSHClient` per `node_id`, so repeated status polls and phase
steps reuse a single multiplexed `asyncssh` connection instead of reconnecting each time.

- `ssh_for_node(session, node)` builds a **fresh** `NodeConn` each call (re-decrypting). Because
  `NodeConn` has value equality, the pool reconnects automatically when any
  connection-affecting parameter changed — so secret rotations / edits take effect.
- A per-node `asyncio.Lock` guards connect/replace. `drop(node_id)` closes and forgets a node
  (used after `harden` switches auth method). `close_all()` runs on app shutdown.

---

## 5. Background jobs + WebSocket log streaming

Long-running operations (setup phases, model download/sync, instance start/stop/delete,
node harden) run as tracked `Job`s via the `JobManager` singleton in `services/jobs.py`.

### Lifecycle

`jobs.start(type, title, coro, …)` creates a `Job` row, schedules `coro(handle)` as an
`asyncio.Task`, and returns the job id immediately (the router responds `JobAccepted`). The
coroutine receives a `JobHandle` with:

- `await handle.log(text, stream="info")` — splits on newlines, persists each as a `JobLog`
  row, and publishes a `log` event.
- `await handle.set_progress(frac)` — persists `progress` and publishes a `progress` event.
- `handle.ssh_log_cb()` — an `(stream, line)` callback to pass to `SSHClient.run` so remote
  output streams straight into the job log.

`_run()` sets status `running`, awaits the coroutine, then finishes as `success` (a returned
`str` becomes the summary). On `CancelledError` it finishes `cancelled` (exit 130); on any
other exception it logs `ERROR: …` on the **`error`** stream (distinct from benign tool
`stderr`, so the UI only reds out genuine failures) and finishes `error` (exit 1).

### Event types (WS `/api/jobs/{id}/logs`)

```
{ type: "log",      seq, stream, text, ts }
{ type: "progress", progress }
{ type: "status",   status[, exit_code, summary] }
{ type: "end" }
```

Each coroutine runs in its **own** DB session (`SessionLocal`), never the request session.

### Reconcile-on-drop (lossy queue, authoritative DB)

The pub/sub layer (`subscribe`/`_publish`) uses **bounded** per-job queues (`maxsize=1000`).
This is deliberately lossy for log spam — but terminal events (`status`, `end`) are never
silently dropped: on `QueueFull` the publisher evicts the oldest queued event and retries so
the consumer still learns the job ended.

The WS endpoint (`routers/jobs.py`) is built so the **database is authoritative**:

1. Subscribe to the queue first (so nothing is lost), then send the full persisted log
   backlog, then the current `status`. If already terminal, send `end` and close.
2. Live tail: `queue.get()` with a 5s timeout. On timeout, re-check the authoritative `Job`
   row — if it's terminal **and** the client has received every persisted log line
   (`last_seq >= latest_seq`), send `status` + `end` and stop. This means a job can complete,
   the lossy queue can drop its terminal event, and the stream still ends correctly.
3. Log events with `seq <= last_seq` are skipped (dedupe against the backlog).

---

## 6. Setup phases pipeline (`services/phases.py`)

Setup is `POST /api/cluster/setup` with an optional `phases` array (null = run the full
ordered pipeline). It runs as a single job; each phase logs a `========== Phase: <name>`
banner. Every phase is **idempotent** (check → apply → verify) and safely re-runnable.

`PHASES_ORDER = [prereqs, hosts, network, ssh, packages, docker, image, ray, verify]`

| # | Phase | What it does |
|---|---|---|
| 1 | `prereqs` | For each node: SSH reachable, `hostname`, `sudo` works (`id -u` == 0, else fails with guidance), `nvidia-smi` GPU probe, disk free on the home dir. |
| 2 | `hosts` | `hostnamectl set-hostname`; appends LAN + `-qsfp` `/etc/hosts` entries (idempotent via `grep -qxF … || echo`). |
| 3 | `network` | Brings up the QSFP iface and assigns `qsfp_ip/<netmask>` (no gateway). Applies a temporary `ip addr` immediately, then best-effort persists via `nmcli` (falls back `ipv6.method disabled` → `ignore`, never fails the phase on a persistence hiccup). Then pings both directions (non-fatal). |
| 4 | `ssh` | On head: ensure `~/.ssh/id_ed25519_spark` keypair. Install head's pubkey into the worker's `authorized_keys`. Add a `~/.ssh/config` host alias for the worker, then verify passwordless `ssh worker hostname`. |
| 5 | `packages` | `apt-get update && install` base tools (`tmux screen curl wget git rsync jq htop iftop net-tools python3-pip`); ensure `~/.ssh ~/.cache/huggingface ~/models`. |
| 6 | `docker` | Add the SSH user to the `docker` group; report server version (via sudo if group membership hasn't taken effect yet). Warns (does not fail) if Docker is missing — DGX OS ships Docker + the NVIDIA container toolkit. |
| 7 | `image` | `docker pull <vllm_image>` on both nodes (long timeout) and verify the image is present. |
| 8 | `ray` | Render + install `ray-head.sh` / `ray-worker.sh` under `node_install_dir`, plus their systemd units, and (re)start them. Containers install `ray[default]` then `ray start`. |
| 9 | `verify` | Ping QSFP, then poll `docker exec spark-ray-head ray status` up to 20×6s waiting for **2 distinct alive nodes** (regex `node_<hash>`). Sets `Setting.setup_complete` accordingly. |

`run_phase` dispatches by name via `PHASE_FUNCS`. A `PhaseCtx` carries the session, job
handle, both nodes, cluster config, and the settings singleton, and exposes `ctx.ssh(node)`.

---

## 7. Deterministic container names + systemd units

Container names are deterministic so the portal can reliably `docker exec` / `stop` them
(`services/templates.py`):

| Container | Used for |
|---|---|
| `spark-ray-head` | Ray head container; also hosts **cluster** vLLM instances. |
| `spark-ray-worker` | Ray worker container. |
| `spark-vllm-<name>` | Standalone **single**-node vLLM instance container. |

Everything reboot-critical is a systemd unit (`nodeops.install_systemd_unit` writes to
`/etc/systemd/system`, `daemon-reload`, enable, restart):

| Unit | Notes |
|---|---|
| `spark-ray-head.service` | `ExecStart` the head launch script; `ExecStop`/`ExecStopPost` stop+rm the container. `Restart=on-failure`. |
| `spark-ray-worker.service` | Same shape, worker script. |
| `spark-vllm-<name>.service` | Per-instance. **Cluster:** `ExecStart` is `docker exec spark-ray-head bash -lc "<vllm serve>"`, `BindsTo`/`PartOf` the head unit (a head restart restarts the instance), with an `ExecStartPre` gate that waits for `ray status` to be ready, and an `ExecStop` that `pkill`s the in-container `vllm serve` (killing the `docker exec` client alone would not stop it). **Single:** `ExecStart` runs a standalone `docker run` script; `ExecStop` stops the container. |

The Ray launch replicates NVIDIA's `run_cluster.sh` (with the `pip install ray[default]`
patch) and forces all collective traffic over the QSFP iface by exporting
`UCX_NET_DEVICES`, `NCCL_SOCKET_IFNAME`, `OMPI_MCA_btl_tcp_if_include`,
`GLOO_SOCKET_IFNAME`, `TP_SOCKET_IFNAME` (plus `RAY_memory_monitor_refresh_ms=0`,
`VLLM_HOST_IP`/`MASTER_ADDR` set to the node's QSFP IP). Containers run `--network host
--gpus all`, mount the host HF cache → `/root/.cache/huggingface` and the models dir →
`/models`, with `--shm-size` from `ClusterConfig.shm_size`.

---

## 8. Cluster vs single instance topology (`services/instances.py`)

`build_vllm_serve_cmd` assembles the `vllm serve` command for both. Identifiers that reach
the shell (model name, instance name, dtype, tool parser, api key) are `shlex`-quoted, and
`extra_args` is tokenized with `shlex.split` then re-quoted so passthrough can only add CLI
args, never inject shell syntax.

### Cluster topology (`topology=cluster`)

- TP across **both** nodes (TP defaults to 2), `--distributed-executor-backend ray`.
- `vllm serve` runs **inside** the existing `spark-ray-head` container via `docker exec`.
- Requires the model **present on both nodes** (`_ensure_model_present` checks every node's
  `ModelNodeState.present` + `status == present`, else raises).
- Installed as `spark-vllm-<name>.service` on the head, bound to `spark-ray-head.service`.

### Single topology (`topology=single`)

- TP=1, `--distributed-executor-backend mp`, pinned to one `node_id`.
- A standalone `docker run` (`spark-vllm-<name>` container) launched by a generated script
  under `node_install_dir`, wrapped in `spark-vllm-<name>.service`.
- Requires the model present on **that** node only.

Start flow: set `starting`, render+install the unit, set `running`, then **stream the
instance journal** into the job log (`journalctl -u … -f`, wrapped in remote `timeout`) while
polling `/health` until green (≤900s). On exception the instance goes `error` with
`last_error`. `tool_parser` is auto-mapped from the model repo id when tool-choice is enabled
and none is set (`parsers.tool_parser_for`).

---

## 9. Model lifecycle (`services/models_svc.py`)

### Validate (`POST /api/models/validate`)

Best-effort HF API lookup (`huggingface.co/api/models/<repo>?blobs=true`): existence, summed
file size, `gated` flag, and auto-mapped `tool_parser`.

### Download (`POST /api/models/{id}/download?auto_sync=bool`)

Runs the **`hf` CLI inside a transient container on the head node**:

```
docker run --rm --network host [-e HF_TOKEN=…] \
  -v <models_dir>:/models --entrypoint bash <vllm_image> \
  -lc 'if command -v hf …; then hf download <repo> --local-dir /models/<name>;
       else huggingface-cli download …; fi'
```

`--entrypoint bash` skips the NGC image startup banner (a download needs neither GPU nor
large shm). The HF token (if set) is decrypted from `Setting.hf_token_enc` and passed as
`HF_TOKEN`. A background poller `du -sb`'s the target dir every ~8s to drive both the job
progress bar and the per-node progress dict surfaced on the Models page. On success the head
`ModelNodeState` is marked `present` with its size; with `auto_sync=true`, the model is then
rsynced to every other node.

### Sync (head → worker over QSFP, `POST /api/models/{id}/sync`)

`rsync -aH --info=progress2` from the head to `user@<worker_qsfp_ip>`, using the inter-node
key `~/.ssh/id_ed25519_spark` created in the setup `ssh` phase. The transfer therefore runs
over the **QSFP link**, targeting the QSFP IP explicitly (`-i <key>` — the `~/.ssh/config`
alias only covers the LAN hostname). A destination-size poller drives progress.

After rsync, **sha256 verification** (best-effort): the head generates
`sha256sum` over `*.safetensors`, `scp`s the sum file to the worker (over QSFP), and runs
`sha256sum -c` there. The result is stored in `ModelNodeState.checksum_ok` (`None` when there
are no safetensors to check). State transitions: `syncing → verifying → present`.

### On-disk discovery (`POST /api/models/scan`, plus startup)

`discover_models` `find`s top-level dirs in each node's models dir, recovers the original
repo id from each dir's `config.json` (`_name_or_path`), imports any directory not already in
the registry, then `refresh_presence` for every model so the registry mirrors disk. It also
runs ~5s after boot (`_startup_discover` in `main.py`), best-effort (nodes may be unreachable
at boot). `refresh_presence` re-checks `exists` + size per node.

### Delete (`POST /api/models/{id}/delete`, `DELETE /api/models/{id}`)

`rm -rf` the model dir on the selected nodes **via sudo** (download runs as root in the
container, so files are often root-owned). Updates per-node state; optionally drops the
registry row — but only when deletion succeeded on every node (otherwise discovery would just
re-import the leftovers and the failure would look silent). `DELETE /api/models/{id}` removes
only the registry row.

---

## 10. Status aggregation (`services/status_svc.py`)

`snapshot()` builds a `StatusSnapshot` by fanning out per-node probes concurrently
(`asyncio.gather`). Served at `GET /api/status` and pushed periodically over
`WS /api/status/ws?interval=N` (clamped to ≥3s).

Per node:

- **Reachability:** `ssh.run("true")` with a 10s timeout.
- **GPU telemetry:** `nvidia-smi --query-gpu=index,name,memory.used,memory.total,
  utilization.gpu,temperature.gpu,power.draw --format=csv,noheader,nounits`, parsed into
  `GpuStatus` rows.
- **Unified memory (GB10 caveat):** DGX Spark (GB10) shares LPDDR5X between CPU and GPU. The
  GPU's framebuffer memory reads as **N/A** in `nvidia-smi`, so the meaningful figure comes
  from **`/proc/meminfo`**: `MemTotal` and `MemTotal − MemAvailable` → `sys_mem_*_mib`.
- **Docker/Ray container:** `docker ps --format '{{.Names}}'` checks the expected
  `spark-ray-head` / `spark-ray-worker` is up.

Cluster-wide:

- **QSFP link:** head pings the worker's QSFP IP.
- **Ray status:** `docker exec spark-ray-head ray status` → count distinct `node_<hash>` ids
  (alive nodes) and parse `x/y GPU` for total GPUs.
- **Instance health:** systemd `is-active`, then HTTP `GET /health` (with the instance's API
  key as a bearer token if set); if healthy, `GET /v1/models` to surface the served model id.
- **Memory budget:** for each `running` instance, `gpu_memory_utilization × node_memory_gib`
  (default 119 GiB/node) is charged to the node(s) it occupies (both for cluster, one for
  single). Per-node used/total budget is attached to each `NodeStatus`; any node over budget
  produces an `overcommit_warnings` entry.

---

## 11. Secrets & crypto (`crypto.py`)

All secrets are encrypted at rest with **Fernet**: SSH passwords, SSH private keys + key
passphrases, sudo passwords, the HF token, and per-instance vLLM API keys (the `_enc`
columns).

- The master key comes from `SPARK_SECRET_KEY` (urlsafe base64, validated as a Fernet key)
  if set; otherwise a key is generated once and persisted to `<data_dir>/secret.key` (mode
  `0600`). **Losing the key makes stored secrets unrecoverable** — set/back up
  `SPARK_SECRET_KEY` in production.
- `encrypt(None|"")` → `None`; `decrypt(None)` → `None`. A decrypt failure (wrong/rotated
  key) logs and raises `InvalidToken`.
- Input schemas accept secrets; output schemas expose only `has_*` booleans. API-boundary
  regexes in `schemas.py` strictly validate every identifier that ends up in remote shell
  scripts / systemd unit names / container names (hostnames, ifaces, instance/model names,
  repo ids), so unsafe characters never reach the SSH layer.

---

## 12. Serving the SPA (`main.py`)

The same FastAPI app serves the JSON API under `/api` and the built React SPA for everything
else. The frontend directory is resolved in priority order so one image works for the Docker
build, an editable source checkout, and a packaged wheel:

1. `$SPARK_FRONTEND_DIR` (if set)
2. `app/static` (packaged wheel: build bundled into the package)
3. `../../frontend/dist` (editable / source layout)

The first candidate whose `index.html` exists wins. `/assets` is mounted as static files; a
catch-all `GET /{full_path:path}` returns a real file when it exists, otherwise falls back to
`index.html` (client-side routing). Paths starting with `api` are excluded (404) so they
never shadow the API. If no build is found, the app logs a warning and serves the API only.

---

## 13. Configuration reference (`config.py`)

Settings load from the environment via `pydantic-settings` with prefix `SPARK_` (env var =
`SPARK_<UPPER>`; `.env` is also read). Some seed the singleton `ClusterConfig` row on first
start (thereafter edit at runtime via Settings / `PATCH /api/cluster/config`); the rest are
process-level runtime settings.

| Env var | Default | Kind | Purpose |
|---|---|---|---|
| `SPARK_DATA_DIR` | `/data` | runtime | Holds `spark.sqlite3` and `secret.key`. |
| `SPARK_SECRET_KEY` | `None` → generated | runtime | Fernet master key for secrets at rest. |
| `SPARK_AUTH_ENABLED` | `false` | runtime | Portal login (deferred for v1; hook wired, no-op). |
| `SPARK_ADMIN_PASSWORD` | `None` | runtime | Reserved for the future login. |
| `SPARK_HOST` | `0.0.0.0` | runtime | Bind host. |
| `SPARK_PORT` | `8080` | runtime | Bind port. |
| `SPARK_CORS_ORIGINS` | `["http://localhost:5173"]` | runtime | Accepts a JSON array, a single origin, or a comma-separated list (a `NoDecode` validator splits it before pydantic JSON-parses). |
| `SPARK_DEFAULT_VLLM_IMAGE` | `nvcr.io/nvidia/vllm:26.05-py3` | **seed** → `ClusterConfig.vllm_image` | vLLM/Ray/download container image. |
| `SPARK_DEFAULT_CLUSTER_NAME` | `spark-vllm` | **seed** → `cluster_name` | Cluster label. |
| `SPARK_DEFAULT_QSFP_NETMASK` | `30` | **seed** → `qsfp_netmask` | QSFP CIDR prefix. |
| `SPARK_DEFAULT_QSFP_IFACE` | `enp1s0f1np1` | seed (Node default) | Default QSFP interface name for new nodes. |
| `SPARK_DEFAULT_MODELS_SUBDIR` | `models` | **seed** → `models_subdir` | Models dir under the node home. |
| `SPARK_DEFAULT_HF_CACHE_SUBDIR` | `.cache/huggingface` | **seed** → `hf_cache_subdir` | HF cache dir under the node home. |
| `SPARK_MODELS_CONTAINER_PATH` | `/models` | **seed** → `models_container_path` | Models mount inside containers. |
| `SPARK_HF_CACHE_CONTAINER_PATH` | `/root/.cache/huggingface` | **seed** → `hf_cache_container_path` | HF cache mount inside containers. |
| `SPARK_RAY_PORT` | `6379` | **seed** → `ray_port` | Ray GCS port. |
| `SPARK_RAY_DASHBOARD_PORT` | `8265` | runtime | Ray dashboard port (used by the head launch script). |
| `SPARK_CONTAINER_SHM_SIZE` | `10.24gb` | **seed** → `shm_size` | `--shm-size` for cluster/instance containers. |
| `SPARK_NODE_MEMORY_GIB` | `119` | runtime | Approx unified memory per node for the budget view. |
| `SPARK_STATUS_POLL_SECONDS` | `10` | seed → `Setting.status_poll_seconds` | Default status poll interval. |
| `SPARK_SSH_CONNECT_TIMEOUT` | `15` | runtime | `asyncssh` connect timeout (s). |
| `SPARK_NODE_INSTALL_DIR` | `/opt/spark-controlplane` | runtime | Where helper scripts + systemd units are installed on the nodes. |

`ClusterConfig` (singleton, runtime-editable) holds the cluster-wide config; the `Node` model
holds per-node config; the `Setting` singleton holds `hf_token` (encrypted) +
`status_poll_seconds` + `setup_complete`.

---

## 14. HTTP/WS API surface

All routers are mounted under `/api`. Long-running operations return `JobAccepted{job_id,
message}` and stream over the jobs WebSocket. Exact request/response shapes live in
`schemas.py`.

**Health** (`main.py`)
- `GET /api/health` · `GET /api/meta`

**Nodes** (`/api/nodes`)
- `GET ""` · `POST ""` (`NodeIn` → 201 `NodeOut`) · `GET /{id}` · `PATCH /{id}` (`NodeUpdate`)
  · `DELETE /{id}` (204)
- `POST /{id}/test` → `ConnectionTest` (synchronous: hostname, sudo, docker, GPU)
- `POST /{id}/harden` → `JobAccepted` (generate ed25519 key, install it, switch to key auth;
  password retained as fallback, pool dropped, key login verified)

**Cluster** (`/api/cluster`)
- `GET/PATCH /config` (`ClusterConfig`) · `GET/PATCH /settings` (`hf_token`,
  `status_poll_seconds`; out has `has_hf_token`, `setup_complete`)
- `GET /phases` (ordered phase list) · `POST /setup` (`SetupRequest{phases?}` → `JobAccepted`)
  · `POST /teardown` (`TeardownRequest` → `JobAccepted`)

**Models** (`/api/models`)
- `GET ""` · `GET /suggestions` · `POST /scan` · `POST /validate` (`{repo_id}`) · `POST ""`
  (`ModelIn` → 201) · `GET /{id}`
- `POST /{id}/download?auto_sync=bool` → `JobAccepted` · `POST /{id}/sync`
  (`{target_node_id?}`) → `JobAccepted` · `POST /{id}/refresh` → `ModelOut` ·
  `POST /{id}/delete` (`{node_ids?, drop_row}`) → `JobAccepted` · `DELETE /{id}` (204)

**Instances** (`/api/instances`)
- `GET ""` · `POST ""` (`InstanceIn` → 201) · `GET /{id}` · `PATCH /{id}` (`InstanceUpdate`)
- `POST /{id}/start` → `JobAccepted` · `POST /{id}/stop` → `JobAccepted` · `DELETE /{id}` →
  `JobAccepted` (stop + remove unit + drop row)

**Status** (`/api/status`)
- `GET ""` → `StatusSnapshot` · `WS /ws?interval=N` (pushes `StatusSnapshot` JSON periodically)

**Jobs** (`/api/jobs`)
- `GET ""?limit=N` · `GET /{id}` → `JobDetail` (with logs) · `POST /{id}/cancel`
- `WS /{id}/logs` — sends backlog first, then live tail; events `log` / `progress` / `status`
  / `end` (see §5)

**Playground** (`/api/playground`)
- `POST ""` (`PlaygroundRequest{instance_id,prompt,system?,max_tokens,temperature}` →
  `PlaygroundResponse`) — proxies an OpenAI-style chat completion to the instance's
  `/v1/chat/completions` (resolving the served model id and forwarding the API key).
