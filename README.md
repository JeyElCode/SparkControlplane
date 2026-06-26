# Spark Control Plane

A self-hosted web portal that automates setting up, operating, and monitoring a
**2-node NVIDIA DGX Spark vLLM cluster** — turning the manual runbook (hostnames,
QSFP networking, inter-node SSH, Docker, Ray, model download/sync, `vllm serve`,
teardown) into a few clicks, plus live status and a model manager.

It ships as a single container published to
`ghcr.io/jeyelcode/spark-controlplane`.

![status](https://img.shields.io/badge/version-1.0.0-blue)

---

## What it does

- **One-click bare-metal setup** — idempotent phases you can run all at once or
  individually: hostnames & `/etc/hosts` → QSFP private network (`nmcli`) →
  passwordless inter-node SSH → base packages → Docker access → pull the vLLM
  image → start the Ray cluster (systemd) → verify.
- **Model manager** — add any HuggingFace repo (free text + curated suggestions),
  download on the head via the vLLM image's `hf` CLI, then **auto-rsync to the
  worker with sha256 verification**. Per-node presence is tracked live.
- **Flexible multi-model serving** — each instance is either:
  - `cluster` topology: `vllm serve` in the Ray head container, **TP=2 across both
    nodes** (for big models), or
  - `single` topology: a standalone container **pinned to one node, TP=1** — so you
    can run two different models at once (one per node).
  - Tool-calling parser (`hermes`, `qwen3_xml`, `kimi_k2`, …) is auto-mapped from
    the model name, with per-instance override.
- **Reboot-safe** — Ray and every instance run as **systemd units**.
- **Live dashboard** — QSFP link, Ray node count, per-node GPU utilization/memory
  (via `nvidia-smi`), instance `/health` + `/v1/models`, and a per-node memory
  budget with overcommit warnings.
- **Built-in playground** — smoke-test any running model from the UI.
- **Granular teardown/reset** — stop instances, stop Ray, remove network/SSH/hosts,
  and (off by default) delete downloaded models.
- **Secrets encrypted at rest** — SSH/sudo passwords, private keys, and the HF
  token are stored with Fernet encryption.

---

## Architecture

```
Browser ──HTTP/WS──> Spark Control Plane container (FastAPI + React SPA)
                              │ asyncssh (LAN IPs)
                ┌─────────────┴──────────────┐
                ▼                             ▼
        spark-01 (head)              spark-02 (worker)
        Ray head + cluster vLLM      Ray worker + single-node vLLM
                └────── QSFP 10.10.10.0/30 (Ray/NCCL/UCX) ──────┘
```

- **Backend**: Python 3.12 / FastAPI, `asyncssh` for all node operations,
  SQLAlchemy + SQLite for state, background jobs with streamed logs over WebSocket.
- **Frontend**: React + Vite (TypeScript), served as static files by the API.
- The portal **only needs SSH (LAN) reachability to both nodes** — it never has to
  run on a node itself.

The Ray launch replicates NVIDIA's `run_cluster.sh` (pinned commit) including the
`pip install 'ray[default]'` patch, forcing Ray/NCCL/UCX/Gloo traffic over the QSFP
interface — but with deterministic container names (`spark-ray-head` /
`spark-ray-worker`) so instances and lifecycle control are reliable.

---

## Quick start

### Run the container

```bash
docker run -d --name spark-controlplane \
  -p 8080:8080 \
  -v "$PWD/data:/data" \
  -e SPARK_SECRET_KEY="$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')" \
  ghcr.io/jeyelcode/spark-controlplane:v1.0.0
```

or with compose:

```bash
docker compose up -d
```

Open <http://localhost:8080>.

> Set `SPARK_SECRET_KEY` to a stable [Fernet](https://cryptography.io/en/latest/fernet/)
> key and back it up. Without it a key is generated into `/data/secret.key`; losing
> it makes stored secrets unrecoverable.

### First-run walkthrough

1. **Nodes** → add the head (`spark-01`) and worker (`spark-02`): LAN IP, QSFP IP,
   SSH user + password (or key), and sudo mode. Click **Test connection**.
   Optionally **Harden → key** to switch to key auth.
2. **Setup** → set the vLLM image + HuggingFace token, then **Run full setup**
   (or run phases one at a time) and watch the live logs.
3. **Models** → add a model, **Download** (auto-syncs to the worker).
4. **Instances** → create an instance (cluster or single-node), **Start**.
5. **Dashboard** / **Playground** → confirm health and chat with the model.

### Node prerequisites

DGX OS already ships Docker + the NVIDIA container toolkit and the GPU driver. The
portal needs, per node:

- SSH reachability on the LAN IP with the credentials you provide.
- `sudo` — either passwordless (`NOPASSWD`) or a sudo password entered in the portal.
- The QSFP cable connected between the two boxes (the portal assigns the static IPs).

---

## Configuration (environment variables)

| Variable | Default | Purpose |
| --- | --- | --- |
| `SPARK_SECRET_KEY` | generated | Fernet key for encrypting stored secrets |
| `SPARK_DATA_DIR` | `/data` | SQLite DB + secret key location |
| `SPARK_DEFAULT_VLLM_IMAGE` | `nvcr.io/nvidia/vllm:26.05-py3` | Default container image |
| `SPARK_NODE_MEMORY_GIB` | `119` | Per-node memory used for the budget view |
| `SPARK_NODE_INSTALL_DIR` | `/opt/spark-controlplane` | Where node helper scripts/units are installed |
| `SPARK_CORS_ORIGINS` | `http://localhost:5173` | Allowed CORS origins — a single origin, comma-separated list, or JSON array |
| `SPARK_FRONTEND_DIR` | auto | Path to the built SPA (set automatically in the image; needed only for bare `pip install` runs) |

Cluster image, paths, QSFP netmask, HF token and poll interval are also editable in
**Settings**.

---

## Security

- Run the portal and the cluster only on a **trusted private network**. vLLM/Ray
  inter-node traffic is unencrypted by design and must stay on the private QSFP
  segment.
- The QSFP network is configured as a point-to-point link **with no gateway**.
- Portal login is **not** enabled in this build; the secret-encryption hook and an
  auth dependency are wired in for a future release. Put it behind a reverse proxy
  with auth if you expose it beyond your LAN.

---

## Development

```bash
# backend
cd backend
uv venv --python 3.12 && uv pip install -e .
SPARK_DATA_DIR=./.data uvicorn app.main:app --reload --port 8080

# frontend (proxies /api to :8080)
cd frontend
npm install
npm run dev   # http://localhost:5173
```

## Release

Pushing a `v*` tag triggers `.github/workflows/release.yml`, which builds a
multi-arch image (amd64 + arm64) and publishes it to
`ghcr.io/jeyelcode/spark-controlplane:<version>` and `:latest`.

```bash
git tag v1.0.0 && git push origin v1.0.0
```

## License

MIT — see [LICENSE](LICENSE).
