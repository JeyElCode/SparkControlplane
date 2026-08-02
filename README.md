# Spark Control Plane

A self-hosted web portal that automates setting up, operating, and monitoring a
**NVIDIA DGX Spark vLLM cluster (2-4 nodes: 1 head + up to 3 workers)** —
turning the manual runbook (hostnames,
QSFP networking, inter-node SSH, Docker, Ray, model download/sync, `vllm serve`,
teardown) into a few clicks, plus live status, a model manager, and a test
playground.

It ships as a single container published to
`ghcr.io/jeyelcode/spark-controlplane`.

![version](https://img.shields.io/badge/version-1.38.0-blue)
![license](https://img.shields.io/badge/license-MIT-green)

---

## What it does

- **One-click bare-metal setup** — idempotent phases you can run all at once or
  individually: `prereqs` → hostnames & `/etc/hosts` → QSFP private network
  (`nmcli`) → passwordless inter-node SSH → base packages → Docker access → pull
  the vLLM image → start the Ray cluster (systemd) → verify. Each phase is
  re-runnable and streams live logs.
- **Model manager**
  - Add any HuggingFace repo (free-text id + curated suggestion chips, with a
    repo validator that estimates size and the right tool parser).
  - **Download** on the head via the vLLM image's `hf` CLI (falls back to
    `huggingface-cli`), then **auto-rsync to the worker over the QSFP link with
    sha256 verification**.
  - **Live per-node progress bars** for both download and sync, visible right on
    the Models page.
  - **Stuck-download recovery** — downloads use a named, single-per-model
    container and reap any orphan + stale HuggingFace `.lock` files before
    starting (so a download interrupted by a restart can't deadlock the next
    one). A **Stop** button cancels an in-progress or orphaned transfer and
    clears the locks; a stalled download (no progress for 15 min) aborts itself
    with a clear message. Partial files are kept, so re-downloading resumes.
  - **Disk discovery** — models already present on the nodes are imported into
    the registry automatically (at startup and via a **Scan nodes** button), so
    the registry always mirrors what's on disk.
  - One **Delete** that removes the files from all nodes (via `sudo`, so
    root-owned download files are handled) and the registry entry.
- **The portal does the arithmetic** — pick a model and press **Run**. Topology,
  `--gpu-memory-utilization`, `--max-model-len` and `--max-num-seqs` are derived
  from the model's KV-cache geometry and what your nodes actually have free
  (memory held by running *and starting* instances included), and every derived
  value is shown with the sentence explaining where the number came from. When
  it does not fit, it says which number is the problem instead of failing ten
  minutes into a weight load. Nothing is hidden: every field stays editable and
  the advanced surface is untouched.
- **Flexible multi-model serving** — each instance is either:
  - `cluster` topology: `vllm serve` inside the Ray head container, **TP across
    all nodes** (2-4, for big models), or
  - `single` topology: a standalone container **pinned to one node, TP=1** — so
    you can run a different model on every node at once.
  - Tool-calling parser (`hermes`, `qwen3_xml`, `llama3_json`, `mistral`,
    `kimi_k2`, …) is auto-mapped from the model name, with a per-instance
    override. Inline `?` help explains every serving knob.
  - **Start streams the live vLLM startup output** (model loading, NCCL/Ray init,
    any crash) until `/health` goes green — for easy debugging.
- **Reboot-safe** — Ray and every instance run as **systemd units** with the
  NVIDIA-recommended ulimits + shm size.
- **Live dashboard** — setup state, QSFP link, Ray node count, per-GPU
  utilization / temperature / power, **per-node unified memory** (from
  `/proc/meminfo`, since the GB10 shares LPDDR5X between CPU and GPU and reports
  no separate VRAM), instance `/health` + served model, and a per-node memory
  budget with overcommit warnings.
- **Speed benchmarking across a predictability ladder** — tokens/sec and TTFT
  measured on **predictable** (counting), **code**, and **creative** output,
  because decode speed is not one number: speculative decoding multiplies
  throughput on predictable text and *costs* you on creative text, and a single
  average hides which way an instance trades. Concurrency sweep included; runs
  are saved, charted and comparable across instances and over time. See
  [docs/EVALS.md](docs/EVALS.md).
- **Built-in playground** — smoke-test any running model from the UI.
- **Granular teardown/reset** — stop instances, stop Ray, remove network / SSH /
  hosts, and (off by default) delete downloaded models.
- **Secrets encrypted at rest** — SSH/sudo passwords, private keys, the HF token,
  and per-instance API keys are stored with Fernet encryption.
- **Background jobs** — every long-running action is a tracked job with logs
  streamed over a WebSocket; the UI reads job status from the server (a dropped
  socket is never mistaken for a failure).

---

## Documentation

| Doc | What's in it |
| --- | --- |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, data model, SSH layer, jobs, phases, topology, status aggregation |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Every env var, cluster/node config, the `/data` volume, secrets |
| [docs/API.md](docs/API.md) | REST + WebSocket API reference with curl examples |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | First-run, day-2 ops, inspecting a deployment, troubleshooting |
| [docs/EVALS.md](docs/EVALS.md) | Evaluation & benchmarking: scorers, sandbox, judge, perf, charts |
| [docs/CHANGELOG.md](docs/CHANGELOG.md) | Per-version history |

---

## Architecture (at a glance)

```
Browser ──HTTP/WS──> Spark Control Plane container (FastAPI + React SPA)
                              │ asyncssh (LAN IPs)
                ┌─────────────┴──────────────┐
                ▼                             ▼
        spark-01 (head)              spark-02 (worker)
        Ray head + cluster vLLM      Ray worker + single-node vLLM
                └──── QSFP 10.10.10.0/30 (Ray/NCCL/UCX + model sync) ────┘
```

- **Backend**: Python 3.12 / FastAPI, `asyncssh` for all node operations,
  SQLAlchemy 2.0 + SQLite for state, a background job manager with logs streamed
  over WebSocket.
- **Frontend**: React + Vite (TypeScript), served as static files by the API.
- The portal **only needs SSH (LAN) reachability to both nodes** — it never has
  to run on a node itself.
- Ray containers have **deterministic names** (`spark-ray-head` /
  `spark-ray-worker`); instances are `spark-vllm-<name>`. The Ray launch
  replicates NVIDIA's `run_cluster.sh` (pinned commit) including the
  `pip install 'ray[default]'` patch, forcing Ray/NCCL/UCX/Gloo traffic over the
  QSFP interface.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full picture.

---

## Quick start

### Run the container

```bash
docker run -d --name spark-controlplane \
  -p 8080:8080 \
  -v "$PWD/data:/data" \
  -e SPARK_SECRET_KEY="$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')" \
  ghcr.io/jeyelcode/spark-controlplane:latest
```

(Pin a specific release with `:vX.Y.Z` instead of `:latest` for reproducibility.)

or with compose:

```bash
docker compose up -d
```

Open <http://localhost:8080>.

> **Set `SPARK_SECRET_KEY`** to a stable [Fernet](https://cryptography.io/en/latest/fernet/)
> key and back it up. Without it, a key is generated into `/data/secret.key`;
> losing it makes stored secrets unrecoverable.
>
> The GHCR package is **private** by default — make it public in your GitHub
> package settings, or `docker login ghcr.io` first.

### First-run walkthrough

1. **Nodes** → add the head (`spark-01`) and worker (`spark-02`): LAN IP, QSFP
   IP, SSH user + password (or key), and sudo mode. Click **Test connection**.
   Optionally **Harden → key** to switch to key auth.
2. **Setup** → set the vLLM image + HuggingFace token, then **Run full setup**
   (or run phases one at a time) and watch the live logs.
3. **Models** → add a model and **Download** (it auto-syncs to the worker over
   QSFP). Already have models on disk? Hit **Scan nodes**.
4. **Run it** — the button next to a downloaded model. The portal works out
   topology, memory fraction, context length and concurrency from the model's
   shape and what your nodes have free, shows you the reasoning behind each
   number, and starts it. **Customize** opens the full form with those values
   filled in if you want to change any of them.
   *(Or go to **Instances** → **New instance** and build one by hand; the
   **Work it out for me** button fills in the same derived values there.)*
5. **Dashboard** / **Playground** → confirm health and chat with the model.

See [docs/OPERATIONS.md](docs/OPERATIONS.md) for the detailed runbook.

### Node prerequisites

DGX OS already ships Docker + the NVIDIA container toolkit and the GPU driver.
The portal needs, per node:

- SSH reachability on the LAN IP with the credentials you provide.
- `sudo` — either passwordless (`NOPASSWD`) or a sudo password entered in the
  portal.
- The QSFP cable connected between the two boxes (the portal assigns the static
  IPs).

---

## Configuration

Common environment variables (full list in
[docs/CONFIGURATION.md](docs/CONFIGURATION.md)):

| Variable | Default | Purpose |
| --- | --- | --- |
| `SPARK_SECRET_KEY` | generated | Fernet key for encrypting stored secrets |
| `SPARK_DATA_DIR` | `/data` | SQLite DB + secret key location |
| `SPARK_DEFAULT_VLLM_IMAGE` | `nvcr.io/nvidia/vllm:26.05-py3` | Default container image |
| `SPARK_NODE_MEMORY_GIB` | `119` | Per-node memory used for the budget view |
| `SPARK_NODE_INSTALL_DIR` | `/opt/spark-controlplane` | Where node helper scripts/units are installed |
| `SPARK_CORS_ORIGINS` | `http://localhost:5173` | Allowed CORS origins — single origin, comma list, or JSON array |
| `SPARK_FRONTEND_DIR` | auto | Path to the built SPA (set in the image; needed only for bare `pip install` runs) |

Cluster image, paths, QSFP netmask, shm size, HF token and poll interval are also
editable at runtime in **Settings**.

---

## Security

- Run the portal and the cluster only on a **trusted private network**. vLLM/Ray
  inter-node traffic is unencrypted by design and must stay on the private QSFP
  segment.
- The QSFP network has **no gateway**: 2 nodes = a direct cable; 3-4 nodes need
  a QSFP switch with every node in the same subnet (default `/24`).
- **Portal login** is configured with `SPARK_AUTH_MODE`: `none` (open — for a
  trusted LAN only), `password`, `ldap`, or `oidc` (single sign-on via Entra ID,
  Keycloak or Okta, with a mandatory group/role requirement). Sessions can be
  revoked from Settings → Sessions, and revocation survives a restart. In `none`
  mode anyone who can reach the portal has full control of your hardware — put
  it behind an authenticating proxy or turn a real mode on.
- The container runs as a **non-root** user (`spark`, uid 10001).

---

## Development

```bash
# backend
cd backend
uv venv --python 3.12 && uv pip install -e .
SPARK_DATA_DIR=./.data uvicorn app.main:app --reload --port 8080

# frontend (dev server proxies /api -> :8080)
cd frontend
npm install
npm run dev          # http://localhost:5173
npm run typecheck    # tsc --noEmit
npm run build        # production build into dist/
```

### Building the image by hand

```bash
docker buildx build -t spark-controlplane:dev --load .
```

**BuildKit is required.** The frontend stage is pinned to the *build* platform
(`FROM --platform=$BUILDPLATFORM`), which the legacy builder rejects at parse
time — so `DOCKER_BUILDKIT=0 docker build .` and Compose v1 will not work.
`docker buildx build` and `docker compose` (v2) are fine. The Dockerfile explains
why the pin is there; the short version is that building the SPA under QEMU
emulation intermittently hangs for six hours.

CI (`.github/workflows/ci.yml`) runs the frontend typecheck + build, the full
backend test suite + `ruff`, and a multi-arch Docker build on every push/PR.

## Release

Pushing a `v*` tag triggers `.github/workflows/release.yml`, which runs the test
suite, builds a multi-arch image (amd64 + arm64), publishes it to
`ghcr.io/jeyelcode/spark-controlplane:<version>` and `:latest`, and then **runs
the arm64 image** to confirm it is genuinely aarch64 and serves a usable SPA —
the only architecture a DGX Spark can execute.

```bash
git tag vX.Y.Z && git push origin vX.Y.Z
```

Deployment/rollout is managed by the cluster owner (e.g. GitOps/ArgoCD); the repo
only builds and publishes the image.

## License

MIT — see [LICENSE](LICENSE).
