# Spark Control Plane

A self-hosted web portal that automates setting up, operating, and monitoring a
**2-node NVIDIA DGX Spark vLLM cluster** — turning the manual runbook (hostnames,
QSFP networking, inter-node SSH, Docker, Ray, model download/sync, `vllm serve`,
teardown) into a few clicks, plus live status, a model manager, and a test
playground.

It ships as a single container published to
`ghcr.io/jeyelcode/spark-controlplane`.

![version](https://img.shields.io/badge/version-1.0.12-blue)
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
  - **Disk discovery** — models already present on the nodes are imported into
    the registry automatically (at startup and via a **Scan nodes** button), so
    the registry always mirrors what's on disk.
  - One **Delete** that removes the files from all nodes (via `sudo`, so
    root-owned download files are handled) and the registry entry.
- **Flexible multi-model serving** — each instance is either:
  - `cluster` topology: `vllm serve` inside the Ray head container, **TP=2 across
    both nodes** (for big models), or
  - `single` topology: a standalone container **pinned to one node, TP=1** — so
    you can run two different models at once (one per node).
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
  ghcr.io/jeyelcode/spark-controlplane:v1.0.11
```

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
4. **Instances** → create an instance (cluster or single-node) and **Start** —
   the live vLLM startup output streams until the model is serving.
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
- The QSFP network is a point-to-point link **with no gateway**.
- Portal login is **not** enabled in this build; the secret-encryption layer and
  an auth dependency hook are wired in for a future release. Put it behind a
  reverse proxy with auth if you expose it beyond your LAN.
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

CI (`.github/workflows/ci.yml`) runs the frontend typecheck + build, a backend
import smoke test, and a Docker build on every push/PR.

## Release

Pushing a `v*` tag triggers `.github/workflows/release.yml`, which builds a
multi-arch image (amd64 + arm64) and publishes it to
`ghcr.io/jeyelcode/spark-controlplane:<version>` and `:latest`.

```bash
git tag v1.0.11 && git push origin v1.0.11
```

Deployment/rollout is managed by the cluster owner (e.g. GitOps/ArgoCD); the repo
only builds and publishes the image.

## License

MIT — see [LICENSE](LICENSE).
