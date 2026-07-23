# API Reference

REST + WebSocket reference for the Spark Control Plane (v1.0.11). The API is
served by the FastAPI backend under the `/api` prefix; everything else is the
built React SPA. All request and response bodies are JSON unless noted.

Field shapes below come from `backend/app/schemas.py`. Only the load-bearing
keys are listed — see the schema module for exact types and validators.

## Conventions

- **Secrets are write-only.** SSH/sudo passwords, private keys, key
  passphrases, the HuggingFace token, and per-instance API keys are accepted on
  input but never serialized back. Responses expose `has_*` booleans (e.g.
  `has_ssh_password`, `has_hf_token`, `has_api_key`) that indicate whether a
  secret is stored.
- **Input validation at the boundary.** Identifiers that flow into remote shell
  scripts, systemd unit names, and docker container names (node `name`,
  `qsfp_iface`, IPs, instance `name`, model `name`, HF `repo_id`) are validated
  strictly. Invalid values return `422`.
- **Standard errors.** `404` for missing resources, `409` for conflicts
  (duplicate node role, duplicate model, instance name collision, cancelling a
  non-running job), `400` for invalid combinations, `422` for schema/validation
  failures.

## The async job pattern

Mutating, long-running operations (anything that touches a node over SSH —
setup, teardown, hardening, model download/sync/delete, instance
start/stop/delete) do **not** block. They enqueue a background job and return
`202`-style `JobAccepted`:

```json
{ "job_id": 42, "message": "Setup started" }
```

Track the job two ways:

- **Poll** `GET /api/jobs/{job_id}` → `JobDetail` (status, progress, exit code,
  summary, and the full persisted log).
- **Stream** `WS /api/jobs/{job_id}/logs` → backlog of log lines, then a live
  tail of log/progress/status events, terminated by an `end` event.

Synchronous endpoints (everything else, plus `POST /api/nodes/{id}/test`)
return their result directly.

---

## Health

Defined in `app/main.py` (not a router).

| Method | Path | Description | Response |
|---|---|---|---|
| GET | `/api/health` | Liveness probe. | `{ "status": "ok", "version": <str> }` |
| GET | `/api/meta` | App name + version. | `{ "name": "Spark Control Plane", "version": <str> }` |

---

## Nodes

`app/routers/nodes.py` — prefix `/api/nodes`. Exactly one `head` and one
`worker` node may exist; creating a second of the same role returns `409`.

| Method | Path | Description | Request | Response |
|---|---|---|---|---|
| GET | `` | List nodes (ordered by role). | — | `NodeOut[]` |
| POST | `` | Create a node. | `NodeIn` | `201` `NodeOut` |
| GET | `/{id}` | Get one node. | — | `NodeOut` |
| PATCH | `/{id}` | Update fields; secrets re-encrypted; SSH pool entry dropped. | `NodeUpdate` (partial) | `NodeOut` |
| DELETE | `/{id}` | Delete node + drop pooled SSH connection. | — | `204` |
| POST | `/{id}/test` | **Synchronous** connection probe over SSH. | — | `ConnectionTest` |
| POST | `/{id}/harden` | Generate an ed25519 keypair, install the public key, switch the node to key auth. | — | `JobAccepted` |

**`NodeIn`** — `role` (`head`\|`worker`), `name`, `lan_ip`, `qsfp_ip`,
`qsfp_iface` (default `enp1s0f1np1`), `ssh_user`, `ssh_port` (default `22`),
`auth_method` (`password`\|`key`, default `password`), `sudo_mode`
(`nopasswd`\|`password`, default `password`); write-only secrets `ssh_password`,
`ssh_private_key`, `ssh_key_passphrase`, `sudo_password`.

**`NodeUpdate`** — same fields as `NodeIn`, all optional, except `role` cannot
be changed.

**`NodeOut`** — `id`, `role`, `name`, `lan_ip`, `qsfp_ip`, `qsfp_iface`,
`ssh_user`, `ssh_port`, `auth_method`, `sudo_mode`, `hardened`,
`has_ssh_password`, `has_ssh_key`, `has_sudo_password`, `created_at`,
`updated_at`.

**`ConnectionTest`** — `ok`, `message`, and best-effort diagnostics `hostname`,
`sudo_ok`, `docker_ok`, `gpu_ok`, `detail`.

---

## Cluster

`app/routers/cluster.py` — prefix `/api/cluster`. Cluster-wide config, global
settings, and the setup/teardown pipeline.

| Method | Path | Description | Request | Response |
|---|---|---|---|---|
| GET | `/config` | Get cluster config (incl. derived paths). | — | `ClusterConfigOut` |
| PATCH | `/config` | Update config fields. | `ClusterConfigIn` (partial) | `ClusterConfigOut` |
| GET | `/settings` | Get global settings. | — | `SettingsOut` |
| PATCH | `/settings` | Update HF token and/or status poll interval. | `SettingsIn` (partial) | `SettingsOut` |
| GET | `/phases` | Ordered list of setup phases. | — | `[{ "phase", "title" }]` |
| POST | `/setup` | Run the setup pipeline (all phases, or a subset). | `SetupRequest` | `JobAccepted` |
| POST | `/teardown` | Tear down the cluster (selectable scope). | `TeardownRequest` | `JobAccepted` |
| GET | `/image-tags?image=` | Registry tags for the cluster image (or `image`), newest first (anonymous Docker Registry v2; nvcr.io / Docker Hub / ghcr.io). | — | `{image, repository, current_tag, tags[]}` |
| POST | `/image-update` | Pull a new image on every node, persist it, optionally restart Ray + rolling-restart running instances. | `ImageUpdateIn` | `JobAccepted` |

**`ClusterConfigIn`** (all optional) — `cluster_name`, `vllm_image`,
`qsfp_netmask`, `models_subdir`, `hf_cache_subdir`, `shm_size`.

**`ClusterConfigOut`** — the above plus read-only derived fields
`models_container_path`, `hf_cache_container_path`, `ray_port`.

**`SettingsIn`** — `hf_token` (write-only), `status_poll_seconds`.

**`SettingsOut`** — `has_hf_token`, `status_poll_seconds`, `setup_complete`.

**`SetupRequest`** — `phases`: a list of phase names, or `null`/omitted to run
the full ordered pipeline. Valid phases (in order): `prereqs`, `hosts`,
`network`, `ssh`, `packages`, `docker`, `image`, `ray`, `verify`.

**`TeardownRequest`** (all default-valued) — `stop_instances` (default `true`),
`stop_ray` (default `true`), `remove_network` (default `false`),
`remove_inter_node_ssh` (default `false`), `remove_hosts_entries` (default
`false`), `delete_models` (default `false` — large downloads are kept unless
explicitly requested).

---

## Models

`app/routers/models.py` — prefix `/api/models`. A model is a registry row
(`repo_id` → on-disk name) plus per-node presence state. File operations are
jobs.

| Method | Path | Description | Request | Response |
|---|---|---|---|---|
| GET | `` | List registered models with per-node state. | — | `ModelOut[]` |
| GET | `/suggestions` | Curated model suggestions. | — | `ModelSuggestion[]` |
| POST | `/scan` | Discover on-disk models on the nodes, import new ones, return refreshed registry. | — | `ModelOut[]` |
| POST | `/validate` | Look up an HF repo (existence, size, gated, tool parser). | `{ "repo_id": <str> }` | `{ ok, repo_id, size_bytes, gated, tool_parser }` or `{ ok: false, error }` |
| POST | `` | Register a model. | `ModelIn` | `201` `ModelOut` |
| GET | `/{id}` | Get one model. | — | `ModelOut` |
| POST | `/{id}/download` | Download the model (query `auto_sync`, default `true`, syncs to the other node when done). | — | `JobAccepted` |
| POST | `/{id}/sync` | Sync model files to another node over the QSFP link. | `{ "target_node_id"?: <int> }` | `JobAccepted` |
| POST | `/{id}/cancel` | Stop an in-progress (or orphaned) download/sync: kill the node-side download container, clear stale HF locks, reset state to `absent` (partial files kept). Safe even with no in-memory job. | — | `JobAccepted` |
| POST | `/{id}/refresh` | Re-check on-disk presence/size; **synchronous**. | — | `ModelOut` |
| POST | `/{id}/delete` | Delete files on selected nodes; optionally drop the registry row. | `{ "node_ids"?: int[], "drop_row"?: bool }` | `JobAccepted` |
| DELETE | `/{id}` | Remove the registry row only (leaves files on disk). | — | `204` |

**`ModelIn`** — `repo_id` (required, validated HF repo id), `name` (optional
on-disk name), `tool_parser` (optional).

**`ModelOut`** — `id`, `repo_id`, `name`, `tool_parser`, `size_bytes`,
`status`, `notes`, `created_at`, and `node_states[]`.

**`ModelNodeStateOut`** (each entry of `node_states`) — `node_id`, `node_role`,
`node_name`, `present`, `size_bytes`, `checksum_ok`, `status`, and `progress`
(`0..1`, live in-memory value while downloading/syncing).

**`ModelSuggestion`** — `repo_id`, `label`, `approx_size_gb`, `tool_parser`,
`note`.

---

## Instances

`app/routers/instances.py` — prefix `/api/instances`. An instance is a vLLM
server definition. `cluster` topology runs `vllm serve` via `docker exec` into
`spark-ray-head` (TP=2, Ray backend); `single` topology runs a standalone
container on one node (TP=1, mp backend) and requires a `node_id`.

| Method | Path | Description | Request | Response |
|---|---|---|---|---|
| GET | `` | List instances (newest first). | — | `InstanceOut[]` |
| POST | `` | Create an instance; defaults resolved from topology/model. | `InstanceIn` | `201` `InstanceOut` |
| GET | `/{id}` | Get one instance. | — | `InstanceOut` |
| PATCH | `/{id}` | Update runtime fields. | `InstanceUpdate` (partial) | `InstanceOut` |
| POST | `/{id}/start` | Start (install + enable + start systemd unit). | — | `JobAccepted` |
| POST | `/{id}/stop` | Stop the instance. | — | `JobAccepted` |
| DELETE | `/{id}` | Job: stop, remove the systemd unit, drop the row. | — | `JobAccepted` |

**`InstanceIn`** — `name` (required), `model_id` (required), `topology`
(`cluster`\|`single`, default `cluster`), `node_id` (required for `single`),
`port` (default `8000`), `tensor_parallel_size` (defaulted from topology — 2
for cluster, 1 for single), `max_model_len`, `gpu_memory_utilization` (default
`0.85`), `max_num_seqs`, `dtype`, `enable_tool_choice` (default `true`),
`tool_parser` (auto-mapped when omitted and tool choice is on), `extra_args`,
`api_key` (write-only), `autostart` (default `true`).

**`InstanceUpdate`** (all optional) — `port`, `max_model_len`,
`gpu_memory_utilization`, `max_num_seqs`, `dtype`, `enable_tool_choice`,
`tool_parser`, `extra_args`, `autostart`.

**`InstanceOut`** — `id`, `name`, `model_id`, `model_repo_id`, `model_name`,
`topology`, `node_id`, `node_role`, `port`, `tensor_parallel_size`,
`max_model_len`, `gpu_memory_utilization`, `max_num_seqs`, `dtype`,
`enable_tool_choice`, `tool_parser`, `extra_args`, `has_api_key`, `autostart`,
`systemd_unit`, `status`, `last_error`.

---

## Schedules

`app/routers/schedules.py` — prefix `/api/schedules`. Weekly live-windows per
instance; the scheduler starts/stops instances on window edges (manual
overrides respected between edges; failed actions retried with backoff).

| Method | Path | Description | Response |
|---|---|---|---|
| GET | `` | All windows with instance context + planner fields (`est_gib_per_node`, `node_scope`). | `ScheduleOut[]` |
| GET | `/now` | Scheduler wall clock (tz, weekday, minutes) for the planner UI. | `{now, weekday, tz, minutes}` |
| POST | `` | Create a window (`days` 0-6 Mon-first, `start_time`/`end_time` HH:MM; end ≤ start wraps past midnight). | `201 ScheduleOut` |
| PATCH | `/{id}` | Update days/times/enabled. | `ScheduleOut` |
| DELETE | `/{id}` | Remove the window. | `204` |

## Usage

`app/routers/usage.py` — prefix `/api/usage`. Persistent serving history
(5-minute rollups of the vLLM counters into `usage_samples`; retention
`SPARK_USAGE_RETENTION_DAYS`).

| Method | Path | Description | Response |
|---|---|---|---|
| GET | `?days=N&bucket=day\|hour` | Per-model usage: totals + bucketed points (gen/prompt tokens, requests, request-weighted mean TTFT), most-used first. | `ModelUsage[]` |

## Auth

`app/routers/auth.py` — prefix `/api/auth`. Only active when
`SPARK_AUTH_MODE` is `password` or `ldap`; in `none` mode (default) the portal
is open. Enforcement is ASGI middleware over `/api/*` **and** the WebSockets;
open paths: `/api/auth/*`, `/api/health`, the SPA shell, `/mcp`; `/metrics`
accepts `Authorization: Bearer SPARK_METRICS_TOKEN`.

| Method | Path | Description | Response |
|---|---|---|---|
| GET | `/me` | Auth mode + current session state (always accessible). | `{auth_mode, auth_required, authenticated, user}` |
| POST | `/login` | Verify credentials (password compare or LDAP bind), set the HttpOnly session cookie. Per-IP throttling (5 failures → 30s → `429`). | `MeOut` |
| POST | `/logout` | Clear the session cookie. | `{ok}` |

## Alerts

`app/routers/alerts.py` — prefix `/api/alerts`. Threshold alerting evaluated
server-side from the telemetry caches (rules: `node_offline`,
`instance_unhealthy`, `gpu_temp`, `disk_low`, `kv_cache_full`, `qsfp_down`).
Active alerts also ride the status snapshot (`active_alerts`) for banners.

| Method | Path | Description | Response |
|---|---|---|---|
| GET | `?limit=N` | Alert history, newest first (`resolved_at` null = still active). | `AlertOut[]` |
| GET | `/active` | Currently-firing alerts. | `ActiveAlert[]` |
| POST | `/test` | Send a test notification through the configured webhook. | `{ok, message}` |

Thresholds/durations and the webhook (ntfy / Discord / Slack / generic JSON;
URL stored encrypted) are configured via `PATCH /api/cluster/settings`
(`alerts` partial dict + write-only `alert_webhook_url`).

## Logs

`app/routers/logs.py` — prefix `/api/logs`. Live journal tailing.

| Method | Path | Description | Response |
|---|---|---|---|
| GET | `/units` | Every tailable `spark-*` unit (Ray head/worker, vLLM instances incl. distributed workers, TLS proxies) mapped to its node. | `LogUnit[]` |
| WS | `/ws?node_id=N&unit=U` | Stream `journalctl -u U -n 200 -f` from node N until the client disconnects. Unit names must match `spark-*`. | text lines |

## Prometheus

`GET /metrics` (no `/api` prefix — standard scrape path) renders the telemetry
caches in Prometheus exposition format: `spark_node_*`, `spark_gpu_*`,
`spark_net_*`, `spark_models_disk_*`, `spark_qsfp_ok`, `spark_ray_nodes_alive`,
and per-instance `spark_vllm_*` (token totals as counters, throughput/queue/KV/
latency as gauges). Serving a scrape reads only in-memory caches.

## Power

`app/routers/power.py` — prefix `/api/power`. Node power controls (all
actions run as logged jobs).

| Method | Path | Description | Response |
|---|---|---|---|
| GET | `/nodes/{id}/affected` | RUNNING instances a shutdown of this node would take down (for confirmation UIs). | `string[]` |
| POST | `/nodes/{id}/shutdown` | Graceful `systemctl poweroff` over SSH sudo. | `JobAccepted` |
| POST | `/nodes/{id}/reboot` | Graceful `systemctl reboot` over SSH sudo. | `JobAccepted` |
| POST | `/nodes/{id}/wake` | Wake-on-LAN: magic packet relayed via a reachable peer node over SSH, falling back to direct UDP. Requires a stored `mac_address` (auto-captured on Test connection). | `JobAccepted` |
| POST | `/batch/shutdown` | Shut down all nodes, workers first then the head. | `JobAccepted` |
| POST | `/batch/wake` | Wake every node with a known MAC. | `JobAccepted` |

## Status

`app/routers/status.py` — prefix `/api/status`. Live cluster health.

| Method | Path | Description | Response |
|---|---|---|---|
| GET | `` | Current cluster status snapshot (served from the telemetry engine's cache — no SSH on the request path). | `StatusSnapshot` |
| GET | `/history?minutes=N` | Per-node sparkline history (CPU %, memory, GPU util/mem, QSFP/LAN B/s, disk), up to the ring length (default 15 min). | `NodeHistory[]` |
| GET | `/instance-history?minutes=N` | Per-instance vLLM serving history (tokens/s, queue depth, KV-cache %, TTFT), scraped from each running instance's Prometheus `/metrics`. | `InstanceHistory[]` |
| WS | `/ws?interval=N` | Push a `StatusSnapshot` (as JSON text) every `N` seconds, from cache. | stream of `StatusSnapshot` |

The WebSocket reads `interval` from the query string (default `3`, clamped to
a minimum of `2` seconds) and sends one JSON-encoded `StatusSnapshot` per tick
until the client disconnects. Node sampling itself runs server-side on the
telemetry engine's own cadence (`SPARK_TELEMETRY_FAST_SECONDS`), independent of
connected clients.

**`StatusSnapshot`** — `setup_complete`, `qsfp_ok`, `ray` (`RayStatus`),
`nodes` (`NodeStatus[]`), `instances` (`InstanceRuntimeStatus[]`),
`overcommit_warnings` (`string[]`), `generated_at`.

- **`RayStatus`** — `reachable`, `nodes_total`, `nodes_alive`, `gpus_total`,
  `detail`.
- **`NodeStatus`** — `node_id`, `role`, `name`, `reachable`, `qsfp_link_ok`,
  `docker_ok`, `ray_container_up`, `gpus` (`GpuStatus[]`), unified-memory
  fields `sys_mem_used_mib` / `sys_mem_total_mib` /
  `mem_budget_used_gib` / `mem_budget_total_gib`, `detail`. (DGX Spark shares
  LPDDR5X between CPU and GPU; the GPU's FB memory reads N/A, so the system
  memory figures are the meaningful ones.)
- **`GpuStatus`** — `index`, `name`, `mem_used_mib`, `mem_total_mib`,
  `util_pct`, `temp_c`, `power_w`.
- **`InstanceRuntimeStatus`** — `instance_id`, `name`, `status`,
  `systemd_active`, `health_ok`, `served_model`, `endpoint`, `detail`.

---

## Jobs

`app/routers/jobs.py` — prefix `/api/jobs`. The target of every `JobAccepted`
response. Terminal statuses are `success`, `error`, `cancelled`.

| Method | Path | Description | Request | Response |
|---|---|---|---|---|
| GET | `?limit=N` | List recent jobs (newest first, default `limit=50`). | — | `JobOut[]` |
| GET | `/{id}` | Get a job with its full persisted log. | — | `JobDetail` |
| POST | `/{id}/cancel` | Cancel a running job (`409` if not running). | — | `{ "cancelled": true }` |
| WS | `/{id}/logs` | Stream backlog + live tail of job events. | — | event stream |

**`JobOut`** — `id`, `type`, `title`, `status`, `node_id`, `target`,
`progress`, `exit_code`, `summary`, `started_at`, `finished_at`, `created_at`.

**`JobDetail`** — `JobOut` plus `logs` (`JobLogOut[]`); each log line is
`{ seq, ts, stream, text }`.

### Log WebSocket events

The socket first replays the persisted backlog, sends one `status` event, then
either ends (if the job is already terminal) or live-tails. The pub/sub queue
is lossy, so the terminal `end` is driven by re-checking authoritative job state
— the stream ends once the job is terminal **and** every persisted line has been
sent.

| Event | Shape |
|---|---|
| log | `{ "type": "log", "seq": <int>, "stream": "info"\|"stdout"\|"stderr"\|"error", "text": <str>, "ts": <iso8601> }` (only genuine job failures use `error`; tool output on stderr is benign) |
| progress | `{ "type": "progress", "progress": <float 0..1> }` |
| status | `{ "type": "status", "status": <str> }` |
| end | `{ "type": "end" }` |
| error | `{ "type": "error", "text": <str> }` (e.g. unknown job id) |

---

## Playground

`app/routers/playground.py` — prefix `/api/playground`.

| Method | Path | Description | Request | Response |
|---|---|---|---|---|
| POST | `` | Proxy a single OpenAI chat completion to a running instance. | `PlaygroundRequest` | `PlaygroundResponse` |

The control plane resolves the instance endpoint (head node's LAN IP for
`cluster` topology, the instance's own node for `single`), attaches the stored
API key as a bearer token when present, resolves the served model id from the
instance's `/v1/models`, and forwards a `chat/completions` call.

**`PlaygroundRequest`** — `instance_id`, `prompt`, `system` (optional),
`max_tokens` (default `256`), `temperature` (default `0.7`).

**`PlaygroundResponse`** — `ok`; on success `content` (assistant text) and
`raw` (the upstream JSON body); on failure `error`. Upstream/transport errors
are reported as `{ "ok": false, "error": ... }` with HTTP `200`.

---

## Evals

`app/routers/evals.py` — prefix `/api/evals`. Capability + performance
evaluation of a model instance. See [EVALS.md](EVALS.md) for the concepts.

| Method | Path | Description | Request | Response |
|---|---|---|---|---|
| GET | `/catalog` | Selectable categories: performance + custom. | — | `CatalogOut` |
| POST | `` | Start an eval run (background job). | `EvalRunRequest` | `EvalStarted` |
| GET | `` | List runs (newest first). | — | `EvalRunOut[]` |
| GET | `/{id}` | Full run detail (results + perf + summary + config). | — | `EvalRunDetail` |
| DELETE | `/{id}` | Delete a run and its results. | — | `204` |
| GET | `/tasks` | List custom (user-authored) tasks. | — | `CustomTaskOut[]` |
| POST | `/tasks` | Create a custom task. | `CustomTaskIn` | `201` `CustomTaskOut` |
| PATCH | `/tasks/{id}` | Update a custom task. | `CustomTaskIn` | `CustomTaskOut` |
| DELETE | `/tasks/{id}` | Delete a custom task. | — | `204` |

**`EvalRunRequest`** — `instance_id` (required), `name?`, `categories[]`
(performance categories `coding`/`reasoning`/`textgen`/`judging`, and/or your
custom categories), `capability` (default `true`, runs custom tasks),
`performance` (default `true`), `perf_reps` (default `3`), `concurrency`
(int[], default `[1,2,4]`), `temperature` (default `0.2`), `judge`
(`{type: "none"|"instance"|"external", instance_id?}`), `sandbox_image`
(default `python:3.12-slim`).

**`CatalogOut`** — `perf_categories` (`string[]`), `custom_categories`
(`string[]`).

**`CustomTaskIn` / `CustomTaskOut`** — `category`, `name`, `prompt`, `scorer`
(`exact`/`contains`/`numeric`/`mcq`/`judge`/`code_exec`/`tool_call`) plus the
fields that scorer needs (`answer`, `contains[]`, `numeric_answer`/`numeric_tol`,
`choices[]`/`correct`, `rubric`, `entry_point`/`test_code`/`code_prefix`,
`tools[]`/`expected_tool`/`expected_args`/`forbid_tool_call`), `system?`,
`max_tokens`, `enabled`. `CustomTaskOut` adds `id`.

**`EvalStarted`** — `{ run_id, job_id, message }`. Follow `job_id` via the Jobs
API / WebSocket for live progress.

**`EvalRunOut`** — `id`, `name`, `instance_id`, `model_name`, `instance_label`,
`categories[]`, `capability`, `performance`, `status`, `overall_score` (0–1),
`peak_throughput_tps`, `judge_desc`, `job_id`, timestamps.

**`EvalRunDetail`** — `EvalRunOut` plus `summary` (category_scores, overall,
peak_throughput_tps, perf[]), `config`, `results[]` (`EvalResultOut`: category,
task_id, scorer, score, passed, response, judge_reason, latency_ms, ttft_ms,
prompt/completion tokens, tokens_per_sec, error), and `perf[]` (`PerfResultOut`:
category, concurrency, ttft_ms_avg, decode_tps_avg, total_latency_ms_avg,
throughput_tps, …).

External-judge config lives on the settings endpoints (`judge_base_url`,
`judge_model`, `judge_api_key` — the key is write-only).

---

## curl examples

Add a node:

```bash
curl -X POST http://localhost:8080/api/nodes \
  -H 'Content-Type: application/json' \
  -d '{
        "role": "head", "name": "spark-head",
        "lan_ip": "192.168.1.10", "qsfp_ip": "10.10.10.1",
        "ssh_user": "ubuntu", "ssh_password": "•••",
        "sudo_mode": "password", "sudo_password": "•••"
      }'
```

Run the full setup pipeline:

```bash
curl -X POST http://localhost:8080/api/cluster/setup \
  -H 'Content-Type: application/json' -d '{}'
# -> { "job_id": 7, "message": "Setup started" }
```

Add a model and download it (auto-syncs to the worker):

```bash
curl -X POST http://localhost:8080/api/models \
  -H 'Content-Type: application/json' \
  -d '{ "repo_id": "Qwen/Qwen2.5-7B-Instruct" }'
# -> { "id": 3, ... }

curl -X POST 'http://localhost:8080/api/models/3/download?auto_sync=true'
# -> { "job_id": 8, "message": "Download started" }
```

Create a cluster instance and start it:

```bash
curl -X POST http://localhost:8080/api/instances \
  -H 'Content-Type: application/json' \
  -d '{ "name": "qwen-7b", "model_id": 3, "topology": "cluster", "port": 8000 }'
# -> { "id": 1, ... }

curl -X POST http://localhost:8080/api/instances/1/start
# -> { "job_id": 9, "message": "Start requested" }
```

Get the current status snapshot:

```bash
curl http://localhost:8080/api/status
```
