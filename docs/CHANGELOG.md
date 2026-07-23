# Changelog

## v1.18.0 — instance schedules + week planner
- **Time-share your memory: models get live windows.** Attach weekly windows
  (days + start/end, overnight wrap supported) to any instance; the scheduler
  starts it when a window opens and stops it when it closes. Semantics are
  **edge-triggered** — a manual start/stop between edges is respected, so the
  scheduler never fights the operator — with a boot reconcile (a portal
  restart mid-window still brings the model up) and **failure retry with
  backoff** (a transiently-failed scheduled action doesn't lose its edge;
  gives up after 5 attempts). Instances without windows stay fully manual.
- **New Schedule page**: a week planner drawing every window as a block with
  the instance's estimated memory (~`gpu_memory_utilization × node budget`
  GiB/node), hour gridlines, a now-line, and **red shading on any hour where
  scheduled instances would exceed the ~119 GiB node budget** — plan your
  time-sharing visually. Click a block to edit; windows table with
  enable/disable/delete.
- API: `GET/POST /api/schedules`, `PATCH/DELETE /api/schedules/{id}`,
  `GET /api/schedules/now`. Actions run as normal jobs (`schedule.start`/
  `schedule.stop`). Env: `SPARK_SCHEDULE_TICK_SECONDS` (60),
  `SPARK_SCHEDULE_TZ` (IANA name; empty = system), `SPARK_SCHEDULE_RETRY_SECONDS`
  (120). New auto-created `instance_schedules` table.

## v1.17.0 — persistent usage history
- **Tokens-per-day per model, kept for months.** A collector rolls up each
  running instance's vLLM counters every 5 minutes (`SPARK_USAGE_ROLLUP_SECONDS`)
  into a new `usage_samples` table: generated/prompt token deltas, completed
  requests, and the window's request-weighted mean TTFT. Counter resets
  (instance restarts) count from zero; idle windows write nothing; rows purge
  after `SPARK_USAGE_RETENTION_DAYS` (default 90). Survives portal restarts —
  unlike the 15-minute in-memory sparkline rings.
- **New Usage page** (24h/7d/30d/90d): totals table per model and
  per-day/per-hour charts for generated tokens and mean TTFT.
  `GET /api/usage?days=N&bucket=day|hour` serves the aggregation.

## v1.16.0 — portal authentication (optional; password or LDAP)
- **Three auth modes via `SPARK_AUTH_MODE`** — `none` (default: open portal,
  unchanged homelab behavior), `password` (single admin credential:
  `SPARK_ADMIN_USER`/`SPARK_ADMIN_PASSWORD`), and `ldap` (bind against a
  directory: `SPARK_LDAP_URL` + either a direct-bind `SPARK_LDAP_USER_DN_TEMPLATE`
  or service-account search via `SPARK_LDAP_BIND_DN`/`_BIND_PASSWORD`/
  `_USER_SEARCH_BASE`/`_USER_FILTER`; optional `SPARK_LDAP_GROUP_REQUIRED`
  membership check, `SPARK_LDAP_START_TLS`, ldaps:// supported). Works with AD
  (`(sAMAccountName={username})`).
- **Fail-closed by design.** A misconfigured mode locks logins out rather than
  silently opening the portal; empty passwords are rejected before the LDAP
  bind (anonymous-bind trap); usernames are escaped in DNs and filters;
  per-IP login throttling (5 failures → 30s).
- **Sessions** are Fernet-encrypted HttpOnly cookies (same key as secrets at
  rest; `SPARK_AUTH_SESSION_HOURS`, `SPARK_AUTH_COOKIE_SECURE`). Enforcement
  is ASGI middleware covering **both HTTP and WebSockets**; open paths:
  the login flow, `/api/health`, the SPA shell, `/mcp` (own bearer gate) —
  and `/metrics` accepts `Authorization: Bearer SPARK_METRICS_TOKEN` so
  Prometheus can scrape while the portal is locked.
- **Login page** (auto-shown on 401), current user + sign-out in the sidebar.
  Auth config lives in env vars only — the portal can't weaken its own lock.

## v1.15.0 — thermal throttle & GPU XID detection
- **Thermal throttling** is now sampled every fast tick straight from
  nvidia-smi's clocks-event reasons (supports both the new
  `clocks_event_reasons.*` and legacy `clocks_throttle_reasons.*` field
  names). An active SW/HW thermal slowdown shows an amber **"thermal
  throttling"** badge on the node card, exports as
  `spark_gpu_thermal_throttle`, and fires a `gpu_throttle` alert when
  sustained — the silent performance killer is no longer silent.
- **GPU XID errors** are scanned from each node's kernel journal on the slow
  tick (cursor-based `journalctl -k`, so each event is reported once, with a
  10-minute lookback on startup). Recent XIDs appear as a red **"XID nn"**
  badge (message in the tooltip), ride `NodeStatus.recent_xids`, export as the
  `spark_gpu_xid_events_total` counter, and fire an immediate **critical**
  `gpu_xid` alert that auto-resolves after a configurable quiet window
  (`xid_window_seconds`, default 600).



## v1.14.0 — alerts & notifications
- **Threshold alerting on top of the telemetry engine.** Rules evaluated every
  ~5s from the caches: node offline, instance running-but-unhealthy, GPU
  temperature, models-disk low, KV-cache pegged (sustained = overloaded model),
  and QSFP fabric down. Every rule has a **sustain duration** so blips and
  reboots don't page, and each fired alert sends a matching **recovery**
  notification. History persists to a new `alerts` table
  (`GET /api/alerts`, `GET /api/alerts/active`).
- **Sinks:** Dashboard banners (crit = red, warn = amber) always on; optional
  **webhook** — ntfy, Discord, Slack, or generic JSON POST — with the URL
  stored encrypted (it may embed a token). `POST /api/alerts/test` sends a
  test notification.
- **Settings → Alerts card:** thresholds (GPU temp, KV %, disk free %, node
  offline seconds), webhook type + URL, Send test / Remove webhook. Config is
  merged over server defaults, unknown keys rejected.

## v1.13.1 — live-cluster fixes (Ray context, Playground/Evals reachability)
- **fix(playground+evals): distributed and TLS instances are now reachable.**
  The Playground (and the eval engine's endpoint resolution) still used the
  pre-v1.9.0 host logic: `cluster` → head, otherwise the pinned node — so a
  `distributed` instance (no pinned node) failed with *"Instance has no
  reachable host"*, and a TLS instance would have been dialed on plain
  `http://ip:port` where vLLM binds loopback. Both now use the shared
  `instance_base_url` resolution (single → pinned node; cluster/distributed →
  head; TLS → `https://ip:tls_port` via the nginx sidecar, no cert
  verification against the raw IP), and the LLM client/judge calls carry the
  verify flag. Reported from the live cluster running a distributed TLS
  instance.
- **fix(dashboard): a stopped Ray cluster is no longer painted as a fault when
  nothing needs Ray.** With only `single`/`distributed` (Ray-less) instances,
  the Ray tile showed "offline" and both node cards flagged a red "ray
  container" badge — alarming, but perfectly normal for that topology. The
  snapshot now carries `ray_required` (true only when a **cluster**-topology
  instance exists): when Ray isn't required and isn't running, the tile reads
  "not in use" and the badge turns gray "ray idle"; when it IS required and
  down, both go properly **red** (previously the tile was a soft gray even
  then). Reported from the live cluster running a distributed instance.

## v1.13.0 — live log viewer
- **On-demand `journalctl -f` from the UI.** New **Logs** buttons on every
  instance card and node card open a streaming viewer with a unit picker
  covering everything the portal manages: Ray head/worker units, each vLLM
  instance (including distributed workers on their nodes), and TLS proxies.
  `GET /api/logs/units` lists tailable units; `WS /api/logs/ws?node_id&unit`
  tails the journal on the owning node over SSH and relays lines until the
  client disconnects (unit names restricted to the `spark-` namespace, remote
  `timeout` guard, slow-client backpressure drops oldest lines instead of
  stalling the tail).

## v1.12.0 — Prometheus exporter + cluster image updates
- **`GET /metrics` (Prometheus exposition).** The portal exports its telemetry
  caches for an external Prometheus/Grafana: per-node `spark_node_*` gauges
  (up, cpu, load, memory bytes, uptime), `spark_gpu_*` (util/temp/power/memory
  per GPU), `spark_net_*` per interface (qsfp/lan tagged),
  `spark_models_disk_*`, `spark_qsfp_ok`, `spark_ray_nodes_alive`, and
  per-instance `spark_vllm_*` — token totals re-exported as **counters** so
  Prometheus computes its own rates, plus derived gauges (tok/s, queue, KV %,
  TTFT) and `spark_instance_healthy`. Dependency-free; scraping costs the
  nodes nothing (cache read).
- **Cluster image update workflow.** *Check updates* on the Settings page lists
  the registry's tags newest-first (Docker Registry v2 with anonymous bearer
  auth — works for nvcr.io, Docker Hub, ghcr.io). *Update cluster* runs a job:
  `docker pull` on every node → persist the new `vllm_image` → optionally
  re-render + restart the Ray units (waits for all nodes to rejoin) →
  rolling-restart RUNNING instances (instances pinned to their own
  `vllm_image` are skipped). New endpoints: `GET /api/cluster/image-tags`,
  `POST /api/cluster/image-update`.

## v1.11.0 — themes
- **Three themes: Dark (default), Light, OLED (true black).** Switcher in the
  topbar, persisted to localStorage and applied by a pre-paint inline script
  (no flash of the wrong theme on load). Implemented purely as CSS-variable
  overrides (`:root[data-theme=…]`) — status colors are re-tuned per theme for
  contrast (e.g. darker green/amber/red on light backgrounds), and the two
  previously hardcoded button colors moved into variables
  (`--accent-bright`/`--accent-contrast`).

## v1.10.0 — power controls
- **Graceful shutdown / reboot per node** (`systemctl poweroff`/`reboot` over
  SSH sudo) as logged jobs. The UI confirm dialog lists the RUNNING instances
  the action would take down (`GET /api/power/nodes/{id}/affected`); a dropped
  SSH connection mid-command is treated as success.
- **Wake-on-LAN with peer relay.** The magic packet is sent **via a reachable
  peer Spark over SSH** (dependency-free python3 one-liner) — essential when
  the control plane runs in a pod outside the nodes' broadcast domain — with
  direct UDP broadcast (+ unicast) as fallback. Reachable peers are tried
  first using the telemetry cache.
- **MAC auto-capture.** `Node.mac_address` (new auto-migrated column) is
  captured from the default-route interface on every **Test connection** and
  before each shutdown, and can be set manually. Wake is disabled until known.
- **Batch operations**: `POST /api/power/batch/{shutdown|wake}` — shutdown
  does workers first then the head; wake targets every node with a stored MAC.
  Nodes page gains per-node Reboot / Shut down / Wake buttons and fleet-wide
  "Wake all" / "Shut down all".

## v1.9.0 — live vLLM serving metrics
- **Per-instance Prometheus scraping.** The telemetry engine now scrapes every
  RUNNING instance's `/metrics` on the fast cadence and derives: **generation
  and prompt tokens/s** (counter deltas), **running/waiting request counts**,
  **KV-cache utilization %**, **requests/s**, and **mean TTFT / end-to-end
  latency** over the last window (histogram sum/count deltas; the last
  measurement is carried through idle windows). Supports both metric
  generations (`gpu_cache_usage_perc` and V1's `kv_cache_usage_perc`); an
  instance restart (counter reset) yields no bogus rates.
- **Dashboard instance table** gains live **Tokens/s (with sparkline),
  Run/Wait queue, KV cache %, and TTFT** columns. New
  `GET /api/status/instance-history?minutes=N` serves the per-instance rings;
  `InstanceRuntimeStatus` gained a `metrics` object on `/api/status`.
- **Fix: TLS and distributed instances now get health/metrics probes.** Health
  checks used plain `http://host:port` even when TLS mode binds vLLM to
  loopback (always "down"), and distributed instances (no pinned node) were
  never probed and showed no endpoint. Probes now route through the nginx
  sidecar (`https://host:tls_port`, no cert verification against the raw IP)
  and treat the head as the API node for cluster **and** distributed
  topologies.

## v1.8.0 — Dashboard v2
- **Live-streaming dashboard.** The Dashboard now rides the status WebSocket
  (with automatic reconnect and a transparent polling fallback — a "live" /
  "polling" badge shows which); updates land every ~3s instead of 8s polling.
- **Sparklines everywhere.** GPU utilization, CPU, unified memory, and QSFP/LAN
  throughput each render a 15-minute trend (from `/api/status/history`) under
  their live meter — dependency-free SVG, per-node.
- **New per-node panels:** CPU (% + cores + loadavg), network split into
  **QSFP vs LAN** with ↓/↑ rates, models-disk usage with free space, uptime in
  the card header, and a **GPU process table** (top consumers with memory) so
  "what's eating the GPU" is one glance away.
- The Ray tile now compares alive nodes against the actual cluster size
  (2-4 nodes) instead of a hardcoded 2; topbar label un-hardcoded from
  "2-node". `cpu_pct` clamped to 0-100 against counter jumps (e.g. a node
  reboot mid-window).

## v1.7.0 — telemetry engine
- **Server-side telemetry engine.** The portal now samples every node
  continuously in the background — one batched SSH command per node per tick
  (default 3s) covering GPU util/mem/temp/power, GPU processes, CPU %, load,
  unified memory, per-interface network throughput (QSFP vs LAN tagged
  separately), models-dir disk usage, uptime, and container state. Expensive
  checks (Ray status, QSFP ping, per-instance systemd + /health) run on their
  own slower cadence (default 12s). `GET /api/status` and the status WebSocket
  are now served **entirely from cache** — a dashboard request/connection no
  longer opens SSH sessions, so many concurrent viewers cost the nodes nothing.
- **History for sparklines.** ~15 min in-memory ring per node, exposed at
  `GET /api/status/history?minutes=N` (CPU, memory, GPU, QSFP/LAN B/s, disk).
- Rates are derived from counter deltas; a node going offline drops its rate
  baseline so recovery doesn't produce a bogus spike. Tunables:
  `SPARK_TELEMETRY_FAST_SECONDS`, `SPARK_TELEMETRY_SLOW_SECONDS`,
  `SPARK_TELEMETRY_HISTORY_MINUTES`.

## v1.6.0
- **Up to 4 Sparks (1 head + up to 3 workers).** The whole provisioning pipeline
  now loops over N nodes: `/etc/hosts` gets every node everywhere, the QSFP
  phase configures each node's own interface/IP and verifies **full-mesh** ping
  (worker↔worker matters once a switch is involved), inter-node SSH installs
  the head key on every worker, Ray starts head + N workers and verify waits
  for all N to join. Model auto-sync fans out head → each worker. `cluster`
  and `distributed` instances default TP to the node count (2/3/4); memory
  budgeting charges multi-node instances to every node. The Nodes page allows
  adding workers up to the cap and auto-names them `spark-0N`.
  Topology guidance: 2 nodes = direct QSFP cable; 3-4 nodes need a QSFP switch
  with all nodes in one subnet. New installs default to a `/24` QSFP subnet
  (existing deployments keep their stored netmask, e.g. `/30`).
- **Upgrade-in-place.** Startup auto-migration rebuilds the `nodes` table to
  drop the legacy `UNIQUE(role)` constraint (SQLite can't drop constraints in
  place) — all rows/ids/FKs preserved, crash-safe (single-transaction swap,
  leftover recovery). A live 2-node deployment upgrades by bumping the image
  tag; nothing on the DGX nodes changes and no phase re-run is required.
- **Any back-panel QSFP port.** New `GET /api/nodes/{id}/interfaces` enumerates
  the node's physical NICs (link state, speed, driver, MAC, QSFP-candidate
  flag). The node form gained **Detect ports** — pick the port with the cable
  from a dropdown instead of typing `enp1s0f1np1` on faith. The network phase
  pre-flights the chosen interface (clear error listing available ports if
  it doesn't exist; loud warning when it has no link).

## v1.5.1
- **fix(tls): cert rotation now actually reloads nginx.** `POST /instances/{id}/tls/reload`
  ran a bare `nginx -s reload`, but the sidecar master is started with an explicit
  `-c <conf_dir>/nginx.conf`; without the same `-c`, the reload reads the image default
  config, signals the wrong/absent pid, and the master never re-reads the new cert — the
  reload "succeeded" but was a silent no-op (the served leaf never swapped). Reload now
  passes the same `-c`, so rotation takes effect. Surfaced by the AWX auto-renewal job.


## v1.5.0
- **First-class TLS termination (nginx sidecar).** An instance can now serve
  HTTPS on a public port (default 443) while vLLM stays on its own port. When
  `tls_enabled`, the API-serving node (single / distributed head) also runs an
  nginx sidecar container (its own systemd unit) that terminates TLS on
  `tls_port` and reverse-proxies to vLLM on `127.0.0.1:<port>` — and vLLM is
  bound to loopback, so the OpenAI port is no longer network-exposed. The proxy
  is streaming-safe (`proxy_buffering off`, long read timeout, HTTP/1.1) so
  OpenAI SSE token streams pass through unbuffered. New per-instance fields
  `tls_enabled` / `tls_port` and write-only `tls_cert` / `tls_key` (PEM, stored
  encrypted; `has_tls_cert` on output). New nullable columns, auto-migrated by
  `db.py`; optional TLS section in the create/edit forms.
- **In-place cert rotation without a model restart.** `POST
  /instances/{id}/tls/reload` (allowed while running) writes the new PEM and
  runs `nginx -s reload`, so certificate renewal never triggers the multi-minute
  vLLM reload. Configurable proxy image via `SPARK_TLS_PROXY_IMAGE` (default
  `nginx:1.27-alpine`). Health checks route through the proxy when TLS is on.

## v1.4.2
- **Per-instance image override (`vllm_image`).** An instance can now pin its own
  vLLM/Ray container image instead of always using the cluster-wide
  `ClusterConfig.vllm_image`. When set, it is used for the single-node run and for
  both the head and worker units of a `distributed` topology; when unset it falls
  back to the cluster image (unchanged behaviour). This lets one instance run a
  custom/experimental build (e.g. a model-specific image) while the rest of the
  fleet stays on the shared image. New nullable column, auto-migrated by `db.py`;
  optional field in the create/edit forms.

## v1.4.1
- **fix(mcp): configurable Host allowlist for `/mcp` behind a reverse proxy.**
  `SPARK_MCP_ALLOWED_HOSTS` / `SPARK_MCP_ALLOWED_ORIGINS` feed FastMCP's
  `TransportSecuritySettings`, so `/mcp` no longer 421s ("Invalid Host header")
  behind an ingress. `*` = trusted-proxy mode; localhost always allowed.

## v1.4.0
- **Native (Ray-less) multi-node `distributed` topology.** Instances can now run
  as a native `torch.distributed` launch — a head unit (rank 0, serves the OpenAI
  API) plus a headless worker unit on each other node (rank >= 1) — joined over
  the QSFP interconnect (`--nnodes/--node-rank/--master-addr/--master-port`, with
  `--master-addr` taken from the head node's `qsfp_ip`). This is a peer of the
  existing Ray `cluster` topology and needs no Ray. Requires >= 2 nodes with a
  `qsfp_ip` set (rejected 4xx otherwise); the model must be present on every
  participating node. Tensor-parallel size defaults to the node count.
- **First-class vLLM serve settings.** New per-instance fields replace fragile
  raw `extra_args` editing: multiple `--served-model-name` aliases, and
  first-class `--kv-cache-dtype`, `--block-size`, `--max-num-batched-tokens`,
  `--tokenizer-mode`, `--reasoning-parser`, `--trust-remote-code`, plus a
  validated `--compilation-config` JSON argument (emitted as a single token) and
  a structured `advanced_args` passthrough (a JSON array of `{flag, value}` rows;
  `value: null` = a boolean flag). Legacy `extra_args` is kept for backward
  compatibility and still appended last. New columns are auto-migrated by `db.py`.

## v1.3.5
- **Models page no longer shows stale "present ✓" for offline nodes.** The model
  registry stores only *last-known* per-node presence, so when a node was
  unreachable the Models page kept showing its models as present. The page now
  cross-references the live status snapshot (the authoritative reachability
  probe): an unreachable node renders a grey **"offline"** badge (with the
  last-known state in a tooltip) instead of a green check, and a banner warns
  that presence reflects the last known state. No extra SSH cost on the
  frequently-polled `/api/models` endpoint — reachability is reused from
  `/api/status`.
- **Edit stopped instances.** Instances now have an **Edit** button (shown while
  stopped or errored) that opens a form for the serve-tuning settings (port,
  context length, GPU memory fraction, max seqs, dtype, tool parser, extra args,
  autostart). Changes apply on the next start. Editing a running/starting/
  stopping instance is rejected (409) since serve settings are baked into the
  systemd unit at start time.

## v1.3.4
- **Fix Ray head/worker crash-loop on the Docker Hub `vllm/vllm-openai` image.**
  The Ray launch scripts ran `docker run <image> bash -c '<ray script>'`, which
  only works when the image entrypoint execs its args (as the NGC image
  `nvcr.io/nvidia/vllm` does). The Docker Hub `vllm/vllm-openai` image has
  `ENTRYPOINT ["vllm","serve"]`, so the script was parsed as *arguments to
  `vllm serve`* — the container died instantly with
  `vllm serve: error: argument --compilation-config/-cc … Invalid JSON` and
  systemd restart-looped it. The Ray head/worker launch scripts and the
  single-node instance runner now override the entrypoint with
  `--entrypoint bash … -c/-lc '<script>'`, making the launch image-agnostic (this
  matches what the model-download path already did). No config change needed —
  re-run the `ray` setup phase (or restart the units) to pick up the fix.

## v1.3.3
- **Fix deleting an instance that has eval-run history.** Deletion failed with
  `FOREIGN KEY constraint failed` because eval runs reference the instance and FK
  enforcement is on (since v1.2.2's `PRAGMA foreign_keys=ON`). Deleting an
  instance now detaches its eval runs (`instance_id → NULL`) first; the runs keep
  their snapshotted model/instance labels, so the history stays readable. Also
  quieted the misleading "Unit not loaded / does not exist" warnings when
  removing an instance whose systemd unit was never installed (e.g. one created
  but never started). Includes the v1.3.2 served-model-name change.

## v1.3.2
- **Clean served model name.** Instances now pass `--served-model-name` so the
  OpenAI API reports a tidy id (the registry name, e.g. `Ornith-1.0-35B-FP8`)
  instead of the raw container path (`/models/Ornith-1.0-35B-FP8`). Use that
  short name as `"model"` in your API calls. If you set your own
  `--served-model-name` in an instance's extra args, that still wins. The
  playground and evals already resolve the id from `/v1/models`, so they adapt
  automatically. Restart an instance (Stop → Start) to pick up the new name.

## v1.3.1
- **Fix model downloads that get stuck forever ("Still waiting to acquire
  lock…").** A download interrupted by a control-plane restart left an orphaned
  `hf download` container running on the head node; the in-memory "already
  downloading" guard was lost on restart, so a retry started a *second*
  `hf download` into the same directory. The two collided on HuggingFace's
  per-file `.lock` files and deadlocked at whatever % they'd reached. Now:
  - the download container has a **deterministic name** (`spark-dl-<model>`), and
    every download **reaps any orphaned container + clears stale `.lock` files**
    for that model before starting — so a plain **Download** self-heals a
    previously stuck one (partial files are kept, so it resumes).
  - a **Stop** button (POST `/api/models/{id}/cancel`) kills the node-side
    download/sync and clears locks even when the control-plane no longer has an
    in-memory job for it (e.g. after a restart) — and resets the model's state so
    the row becomes actionable again.
  - a **stall watchdog**: if a download makes no progress for 15 min while still
    mid-transfer, it's aborted with a clear message instead of hanging silently.
  The reap is scoped to *this model's* download container only (matched by the
  exact `hf download <repo> --local-dir …` command) — it never touches the Ray
  head or a running vLLM serving container.

## v1.3.0
- **Simplified evals to what's actually useful here: custom tasks + throughput.**
  Removed the public-benchmark integration (HumanEval/GSM8K/MMLU fetch) and the
  built-in canned capability suites — read standardized benchmarks from
  HuggingFace directly if you want them. Capability evaluation is now entirely
  **your own custom tasks** (all scorers kept: exact/contains/numeric/mcq/judge/
  sandboxed code/tool-call), and the **tokens/sec performance** tests (TTFT,
  decode tok/s, concurrency sweep) stay. `GET /api/evals/catalog` now returns
  `{perf_categories, custom_categories}`; removed `GET /api/evals/suites` and the
  `benchmark_n` request field. Judge (instance or external) and the code sandbox
  are unchanged.

All notable changes to Spark Control Plane. Each version is published as
`ghcr.io/jeyelcode/spark-controlplane:vX.Y.Z` (multi-arch) by CI on the matching
git tag.

## v1.2.3
- **Evals no longer fail on a transient DB lock.** WAL alone wasn't enough — a
  write transaction held open across slow SSH could starve writers past the
  busy timeout, and a single failed log/result write crashed the whole run. Now:
  job-manager log/status writes retry and, worst case, drop the line instead of
  raising (bookkeeping never crashes a job); eval result/perf commits retry on a
  lock and continue; and `refresh_presence` does all its SSH probing *before*
  writing, so it never holds the write lock across SSH. Verified: 20 concurrent
  log writes succeed while a 2s lock is held.
- **Re-run button** on the Evals list and run detail — re-runs with the same
  instance + config.

## v1.2.2
- **Fix "database is locked" under concurrent writes.** SQLite now runs in WAL
  mode with a 30s busy timeout (+ `synchronous=NORMAL`, `foreign_keys=ON`), so
  the many concurrent writes during an eval (streamed job logs, per-task commits,
  the perf sweep, status polling) queue instead of failing instantly. An eval
  could previously error mid-run (e.g. a benchmark log line reporting "database
  is locked").
- Narrowed the benchmark-fetch try/except so a transient log-write failure can no
  longer be mis-reported as a "fetch failed".

## v1.2.1
- **Fix startup crash on an upgraded DB.** `init_db` now auto-adds missing
  columns to existing tables (`ALTER TABLE … ADD COLUMN`), not just missing
  tables. A persisted `/data` DB created before v1.1.0 lacked the
  `settings.judge_*` columns, so the app crash-looped with
  `no such column: settings.judge_base_url`. Migration is non-destructive
  (existing data preserved) and covers any future column additions.

## v1.2.0
- **Tool-use / agent eval** — new `tools` category + `tool_call` scorer: ships
  OpenAI tool definitions, checks the model calls the right function with the
  right args, and tests that it *refuses* a destructive tool. (Non-streaming
  `chat_once` captures tool calls.)
- **Custom task authoring** — author your own tasks (your repos/prompts/rubrics/
  tests/tools) in the UI (**Evals → Manage tasks**), stored in `custom_tasks` and
  run per category alongside the built-ins. `GET/POST/PATCH/DELETE /api/evals/tasks`.
- **Public benchmark subsets** — select HumanEval / GSM8K / MMLU to pull a
  configurable sample of real items from the HuggingFace datasets-server at run
  time, mapped onto our scorers (code_exec / numeric / mcq). `benchmark_n`
  controls the sample size.
- New `GET /api/evals/catalog` (built-in + benchmark + custom categories).

## v1.1.0
- **LLM evaluation & benchmarking framework** (new **Evals** page + `/api/evals`).
  - **Capability** scoring per task: deterministic (`exact`/`contains`/`numeric`/
    `mcq`), **LLM-judge** (0–10 vs rubric), and **sandboxed code execution**
    (model writes code → unit tests run in a `--network none` container on a node
    → pass@1), across coding / security / reasoning / judging.
  - **Performance**: per-category TTFT, decode tokens/sec, latency, plus a
    **concurrency sweep** for peak aggregate throughput — all via streaming.
  - **Judge** = a running instance you pick, or an external OpenAI-compatible
    endpoint (configured in Settings, key encrypted).
  - Runs persisted in SQLite (snapshotting model + config); **Evals** page with
    scorecards, per-task tables, SVG charts (capability bars, throughput-by-
    category, throughput-vs-concurrency, overall-over-time trend), and multi-run
    comparison.
  - New tables `eval_runs` / `eval_results` / `perf_results`; see
    [EVALS.md](EVALS.md).

## v1.0.13
- While a model download/sync/delete is running, the Models page now shows a
  **"View log"** button (opening that job's live log) instead of disabling the
  action buttons — so you can watch the actual download messages. The `409`
  concurrency guard from v1.0.12 still prevents starting a second operation. The
  models API now returns `active_job_id` for any model with a running job.

## v1.0.12
- **Prevent concurrent file operations on a model.** Pressing Download again
  while one was running launched a second `hf download` into the same dir, and
  the two collided on HuggingFace `.lock` files. Download/sync/delete now return
  `409` if a download/sync/delete is already running for that model (verified
  against the live job manager, so a stale row from a crashed process doesn't
  block forever), and the Models-page buttons are disabled while a model is busy.

## v1.0.11
- Dashboard shows **real per-node unified memory** from `/proc/meminfo`. The
  GB10 shares LPDDR5X between CPU and GPU and reports its GPU FB memory as `N/A`,
  so the old per-GPU memory bar showed a misleading `0/0G`. Per-GPU now shows
  util/temp/power (and a VRAM bar only when one is actually reported).

## v1.0.10
- Models page shows a **single `✓`** per node when a model is present. A distinct
  `⚠ checksum` (amber) appears only if a sync ever fails verification.

## v1.0.9
- Added inline **`?` help tooltips** on key fields: the Models "Parser" column,
  and the New-instance fields (max model length, gpu memory utilization, max num
  seqs, dtype, tool parser override), and Settings → container shm size.

## v1.0.8
- Fixed **uneven dashboard cards**. The global `.card + .card` top-margin (for
  vertically stacked cards) was also applying to cards side-by-side in a grid,
  offsetting the 2nd+ card; neutralized for grid children.

## v1.0.7
- **Start streams the live vLLM startup output** (via the instance's
  `journalctl`) until `/health` is green or a 15-minute cap — for debugging model
  loading / crashes.
- **Fixed false "error" job badges.** The log panel no longer treats a dropped
  WebSocket as a failed job; it reconciles status/logs/progress from
  `GET /api/jobs/{id}` and polls until the job actually finishes.

## v1.0.6
- **Delete model files with `sudo`.** The download runs as root inside the
  container, so model files are root-owned; deleting them as the login user hit
  "Permission denied". Delete only drops the registry row when removal succeeds
  on every node (otherwise it surfaces the failure).

## v1.0.5
- Collapsed the Models delete controls into a **single Delete** button (files on
  all nodes + registry row). The registry-only removal was futile once on-disk
  discovery re-imports leftover directories.

## v1.0.4
- **On-disk discovery.** `discover_models` scans each node's models dir and
  imports any directory not already in the registry (recovering the repo id from
  `config.json` `_name_or_path` when possible). Exposed via `POST /api/models/scan`
  + a "Scan nodes" button, and runs automatically ~5s after startup.

## v1.0.3
- **Model sync now uses the QSFP link** (worker QSFP IP + inter-node key) instead
  of the management LAN, so multi-GB copies use the high-speed interface.

## v1.0.2
- **Per-node download/sync progress bars on the Models page** (visible without
  opening the job dialog), driven by an in-memory progress registry surfaced via
  `/api/models`.
- Sync (rsync head→worker) reports progress too.
- Download container runs with `--entrypoint bash` to skip the NGC image's
  harmless "GPU not detected / 64MB SHMEM" startup banner.
- Serving containers (Ray + vLLM) pass NVIDIA's recommended
  `--ulimit memlock=-1 --ulimit stack=67108864`.

## v1.0.1
- Command **stderr is no longer rendered as errors** — only genuine job failures
  use a red error line; the job status badge is the source of truth.
- Initial **model download progress** + percent.
- Network phase: `nmcli` persistence is best-effort with an `ipv6.method ignore`
  fallback so a benign `nmcli` non-zero no longer fails the phase when the QSFP
  link is already up.
- Download command prefers the new `hf` CLI and falls back to `huggingface-cli`.

## v1.0.0
- Initial release: full setup automation over SSH (hosts, QSFP network,
  inter-node SSH, packages, Docker, image pull, Ray cluster, verify), model
  download + sync with checksums, flexible cluster/single vLLM serving as systemd
  units, live status dashboard, test playground, granular teardown, encrypted
  secrets, and multi-arch GHCR publishing via GitHub Actions.
