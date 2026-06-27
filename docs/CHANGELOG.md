# Changelog

All notable changes to Spark Control Plane. Each version is published as
`ghcr.io/jeyelcode/spark-controlplane:vX.Y.Z` (multi-arch) by CI on the matching
git tag.

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
