# Operations Guide

Operator runbook and troubleshooting reference for the **Spark Control Plane** —
the single-container FastAPI + React portal that automates a 2-node NVIDIA DGX
Spark vLLM cluster. This document assumes the portal container is already
running (see the [README](../README.md) for how to launch it) and focuses on
day-1 bring-up, day-2 operations, what the portal leaves behind on the nodes,
and fixing the things that go wrong.

Image: `ghcr.io/jeyelcode/spark-controlplane`. The portal reaches both nodes over
SSH on their LAN IPs; the QSFP `10.10.10.x` link carries Ray / NCCL / UCX / Gloo
traffic **and** head→worker model sync.

---

## 1. First-run sequence

The whole bring-up is driven from the UI. The order matters: setup must finish
before a model can be downloaded, and a model must be present before an instance
that uses it will start.

1. **Configure the nodes.** In **Nodes**, add the head (`spark-01`) and up to
   3 workers (`spark-02`…`spark-04`). For each: LAN IP, QSFP IP, QSFP interface
   (**Detect ports** lists the node's physical ports with link state so you can
   pick the one with the cable), SSH user, password **or** private key, and sudo
   mode (NOPASSWD or password). Click **Test connection** — it reports hostname,
   sudo, Docker and GPU detection. The head and at least one worker must exist
   before setup will run. With 2 nodes a direct QSFP cable works; with 3-4 nodes
   put a QSFP switch between them (all nodes in one subnet, default `/24`).

2. **Set the HF token + image.** In **Setup** / **Settings**, set the vLLM
   container image and your HuggingFace token. The token is needed to download
   gated/private repos; the image is pulled onto both nodes and used for Ray and
   every vLLM instance.

3. **Run the setup phases.** Run the full pipeline or step through phases one at
   a time, watching the live log. The phases run in this fixed order, each one
   idempotent (check → apply → verify) and safe to re-run:

   | Phase | What it does |
   | --- | --- |
   | `prereqs` | Connects over SSH, confirms sudo works, reports GPU (`nvidia-smi`) and disk. **Fails if sudo is not working.** |
   | `hosts` | Sets each hostname and writes `/etc/hosts` entries for both LAN names and the `-qsfp` aliases. |
   | `network` | Brings up the QSFP interface with a static IP via `nmcli` (no gateway), then pings both ways. |
   | `ssh` | Generates `~/.ssh/id_ed25519_spark` on the head and installs it on the worker for passwordless inter-node SSH. |
   | `packages` | `apt-get` installs base tools (`tmux`, `rsync`, `jq`, `iftop`, …) and ensures `~/.ssh`, `~/.cache/huggingface`, `~/models`. |
   | `docker` | Adds the SSH user to the `docker` group and reports the Docker server version. |
   | `image` | `docker pull` of the vLLM image on both nodes (can take a while). |
   | `ray` | Installs `ray-head.sh` / `ray-worker.sh` and the `spark-ray-head` / `spark-ray-worker` systemd units, and starts them. |
   | `verify` | Pings QSFP, then polls `ray status` (up to ~2 min) until **2 nodes** are reported; sets `setup_complete` on success. |

   `verify` is the gate: it flips the internal `setup_complete` flag only when
   Ray reports two joined nodes.

4. **Download a model.** In **Models**, add a HuggingFace repo (free text or a
   curated suggestion), then **Download**. The download runs the vLLM image's
   `hf` CLI in a transient container on the **head**, and by default
   **auto-syncs to the worker** over QSFP with sha256 verification. A per-node
   progress bar shows live transfer size.

5. **Create and start an instance.** In **Instances**, create either:
   - **cluster** topology — `vllm serve` inside the Ray head container, TP=2
     across both nodes, `--distributed-executor-backend ray`. Requires the model
     present on **both** nodes.
   - **single** topology — a standalone container pinned to one node, TP=1,
     `--distributed-executor-backend mp`. Requires the model on **that** node.

   **Start** the instance. The portal installs a systemd unit, then streams the
   vLLM startup journal into the job log until `/health` goes green (model
   loading can take a few minutes for large models).

6. **Verify.** On the **Dashboard**, confirm the QSFP link, Ray node count,
   per-node GPU/memory, and that the instance shows healthy. Use the
   **Playground** to chat with the served model end-to-end.

---

## 2. Node prerequisites

DGX OS ships Docker, the NVIDIA container toolkit, and the GPU driver already —
the portal does **not** install these. Per node you need:

- **SSH reachability** on the LAN IP with the credentials you enter in **Nodes**.
- **sudo** — either passwordless (`NOPASSWD`) or a sudo password supplied in the
  portal. The `prereqs` phase hard-fails if `sudo id -u` does not return `0`.
- **The QSFP cable** physically connected between the two boxes. The portal
  assigns the static point-to-point IPs (default `/30`, no gateway) during the
  `network` phase; you only provide the cabling and the chosen QSFP IPs/iface.
- If `docker` is somehow missing, the `docker` phase logs a warning and
  continues — but Ray and vLLM will not start. Install Docker + the NVIDIA
  container toolkit before continuing.

The portal itself never runs on a node; it only needs LAN SSH to both.

---

## 3. Day-2 operations

### Download / sync models

- **Download** (Models page) pulls onto the head and auto-syncs to the worker.
- **Sync** re-runs only the head→worker `rsync` (over QSFP) for a model already
  on the head — useful if a sync failed or the worker was rebuilt. You can
  target a specific node.
- **Discover** scans every node's `~/models` dir and imports any directory not
  yet in the registry (recovering the repo id from `config.json` when possible),
  then refreshes presence so the registry mirrors what is on disk.
- **Refresh presence** re-checks per-node existence and size without transferring
  anything.

Sync always verifies safetensors checksums (best-effort) after the transfer and
records `checksum_ok` per node.

### Start / stop / delete instances

- **Start** installs/enables the systemd unit and waits for `/health`.
  `autostart` controls whether the unit is enabled (survives reboot).
- **Stop** runs `systemctl stop` on the owning node and marks the instance
  stopped. The unit is left installed.
- **Delete** stops and removes the systemd unit (best-effort) and drops the
  instance row.

A cluster instance is owned by the head node; a single instance by its target
node. Starting an instance whose model is missing on a required node fails
early with a clear *"Download and sync the model to all required nodes first."*

### Named endpoints, and where their HTTPS terminates

An **endpoint** is a stable public identity — a hostname, a set of model
aliases, and a certificate — that points at whichever instance currently serves
it. Promoting a new model onto it is one guarded action instead of editing
aliases and hand-copying a certificate you then have to remember to rotate.
Rollback is a promote in the other direction, which is why the outgoing
instance is stopped and never deleted.

Members of one endpoint are **mutually exclusive**: TP=2 is the whole box, so
only one can run at a time, and promote stops the outgoing instance before it
starts the incoming one. The endpoint is unavailable for as long as the new
model takes to load — minutes, not seconds.

`termination` decides where HTTPS is handled:

| | `onbox` (default) | `k8s` |
| --- | --- | --- |
| What terminates TLS | nginx sidecar on the serving node | a proxy in your cluster |
| Certificate | uploaded to the portal, pushed on promote | cert-manager / ACME; the portal never sees it |
| vLLM binds | loopback, behind the sidecar | the pinned `upstream_port` |
| On promote | the cert is pushed to the new node | nothing outside the box changes |

**Moving an endpoint into Kubernetes.** Set `termination` to `k8s` and pin an
`upstream_port` — the port every member binds. Then:

```bash
curl -s https://spark.example.net/api/endpoints/prod/manifests \
  -H "Cookie: $SESSION" \
  --get --data-urlencode namespace=llm --data-urlencode issuer=letsencrypt-prod
```

That returns a Service, an EndpointSlice, a cert-manager Certificate and an
Ingress. Commit them and let your GitOps apply them — **the portal holds no
cluster credentials and applies nothing.**

The manifests do **not** need regenerating when you promote. Every member of an
endpoint serves from the head node (the API node for both cluster and
distributed topologies) on the same pinned port, so a promotion changes which
process is listening there and nothing the cluster can observe. Regenerate only
if the hostname, the upstream port, or the head node's address changes.

The generated Ingress sets `proxy-buffering: off` and a one-hour read timeout.
Both matter: with the defaults, a streamed response is held in the proxy and
delivered in chunks rather than token by token, and any generation longer than
60 seconds is cut off mid-stream.

> **The hop from the proxy to vLLM is not encrypted.** On-box, that hop was
> loopback. In the cluster it is a real LAN hop carrying every prompt and
> completion, in plaintext for an instance without its own TLS. Treat the
> management network accordingly until this is closed.

### Teardown options

Teardown is **granular** — pick exactly what to remove (each is independent):

| Option | Effect |
| --- | --- |
| `stop_instances` | `systemctl stop` every vLLM instance, mark stopped. |
| `stop_ray` | Stop + **disable** the Ray units, `docker rm -f` the Ray containers, clear `setup_complete`. |
| `remove_network` | `nmcli con down/delete qsfp-vllm` and remove the temporary QSFP IP. |
| `remove_inter_node_ssh` | Delete the head's `id_ed25519_spark` keypair + its `~/.ssh/config` block, and strip the key from the worker's `authorized_keys`. |
| `remove_hosts_entries` | Remove the `/etc/hosts` lines for both node names and `-qsfp` aliases. |
| `delete_models` | **Off by default.** `rm -rf` the models dir on all nodes (uses sudo — see troubleshooting) and reset registry presence. |

Each step logs warnings instead of aborting, so a partial teardown still
completes the rest.

### Hardening a node to key auth

From **Nodes**, **Harden → key** generates an ed25519 keypair, installs the
public key into the node's `authorized_keys`, switches the node's stored auth
method to key-based (the password is retained as a fallback), and **verifies
key login works** before committing. If the verification login fails, hardening
errors out and the node is left as-is.

---

## 4. Where things live on the nodes

| Thing | Location (default) |
| --- | --- |
| Helper scripts (`ray-head.sh`, `ray-worker.sh`, `vllm-<name>.sh`) | `/opt/spark-controlplane/` |
| systemd units | `spark-ray-head.service`, `spark-ray-worker.service`, `spark-vllm-<name>.service` |
| Downloaded models | `~/models/<model-name>` (i.e. `/home/<user>/models` or `/root/models`) |
| HuggingFace cache | `~/.cache/huggingface` |
| Inter-node SSH key (head only) | `~/.ssh/id_ed25519_spark` (+ `.pub`) |
| Ray head container | `spark-ray-head` |
| Ray worker container | `spark-ray-worker` |
| Cluster vLLM instances | run **inside** `spark-ray-head` via `docker exec` (no separate container) |
| Single-node vLLM instance | container `spark-vllm-<name>` |

The install dir is configurable via `SPARK_NODE_INSTALL_DIR`; the model and HF
cache subdirs come from cluster config (`models_subdir`, `hf_cache_subdir`). The
home dir is `/root` when the SSH user is `root`, otherwise `/home/<user>`.

Container layout to remember: Ray containers mount `~/.cache/huggingface` →
`/root/.cache/huggingface` and `~/models` → `/models`. A cluster instance's
`vllm serve` therefore loads from `/models/<name>` inside `spark-ray-head`.

---

## 5. Inspecting a running deployment

### The portal API (from your workstation)

Port-forward the portal pod, then hit the read-only API:

```bash
kubectl -n spark port-forward deploy/spark-controlplane 18080:8080
```

```bash
curl -s localhost:18080/api/health            # liveness
curl -s localhost:18080/api/jobs              # all jobs (status, type, timestamps)
curl -s localhost:18080/api/jobs/<id>         # one job's detail + captured log
```

Every long-running action (setup, download, sync, start/stop, teardown,
hardening) is a background **Job** with a streamed log. `GET /api/jobs/{id}`
returns the authoritative status and the log; the UI also tails the log live
over the WebSocket at `/api/jobs/{id}/logs`, and a job can be aborted with
`POST /api/jobs/{id}/cancel`.

### On a node (SSH)

```bash
# vLLM instance journal (single OR cluster — same unit naming)
journalctl -u spark-vllm-<name>.service -f

# Ray services
journalctl -u spark-ray-head.service -f
journalctl -u spark-ray-worker.service -f

# Container-level logs and state
docker logs -f spark-ray-head
docker logs -f spark-vllm-<name>          # single-node instances only
docker exec spark-ray-head ray status     # node count + GPU totals
nvidia-smi                                # GPU / unified-memory usage
```

For a **cluster** instance there is no `spark-vllm-<name>` container — the
process runs inside `spark-ray-head`, so use `journalctl -u
spark-vllm-<name>.service` (the unit wraps `docker exec`) or
`docker logs spark-ray-head` for the underlying container.

---

## 6. Troubleshooting

Each row is **symptom → cause → fix**. Most of these are known behaviours the
portal already handles; the table tells you why something looks the way it does.

| Symptom | Cause | Fix |
| --- | --- | --- |
| Deleting a model fails with **"Permission denied"** (older builds) | The download runs as **root** inside the transient container, so model files on disk are root-owned; a plain `rm` as the login user can't remove them. | Already handled: model delete and `delete_models` teardown run `rm -rf` **with sudo**. Just ensure sudo works for that node. If a delete partially fails it is reported, the row is **not** dropped, and discovery would re-import the leftovers — re-run delete after fixing sudo. |
| Dashboard shows **GPU memory: N/A** | DGX Spark (GB10) uses **unified memory** — there is no discrete VRAM for `nvidia-smi` to report, so `memory.total`/`memory.used` come back empty. | Expected. Read the **per-node unified-memory meter** instead (sourced from `/proc/meminfo`), and watch the per-node memory budget / overcommit warnings. |
| Model sync seems to use a "different" network than SSH | Head→worker sync deliberately runs `rsync` over the **QSFP** link (connecting to the worker's QSFP IP with the inter-node key), not the LAN. | Expected — QSFP is the high-speed path. If sync fails, confirm QSFP connectivity (Dashboard QSFP indicator / `ping <worker-qsfp>`) and that the `ssh` setup phase ran. |
| A job badge briefly showed **"error"** even though the action succeeded (older builds) | An old WebSocket bug inferred failure from a dropped log socket. | Fixed: job status now comes from the **server** (`GET /api/jobs/{id}`), not the socket. A dropped log stream no longer marks a job failed. |
| Setup `network` log: `nmcli ... ipv6.method 'disabled'` **rejected** | Some NetworkManager versions don't accept `ipv6.method disabled`. | Non-fatal: the phase automatically retries with `ipv6.method ignore`, and the temporary `ip addr` is already applied, so the link works regardless. A persistence-only failure just logs a warning. |
| Download **stuck forever**, log repeats **"Still waiting to acquire lock … model-XXXXX.safetensors.lock"** | A previous download (often interrupted by a control-plane restart) left an **orphaned `hf download` container** running on the head; a retry started a *second* download into the same dir and the two deadlocked on HuggingFace's per-file `.lock` files. | Fixed in **v1.3.1**: downloads use a single named container per model and reap any orphan + stale locks before starting, so a plain **Download** self-heals. To clear one immediately, click **Stop** on the model row (kills the node-side container + clears locks), then **Download** to resume — the partial files are kept. A download that makes no progress for 15 min now aborts itself with a clear message. |
| Download container prints **"NVIDIA driver was not detected" / "SHMEM 64MB"** banner | That's the NGC image's generic startup banner — a download needs neither a GPU nor large shared memory. | Harmless. The portal already runs the download with `--entrypoint bash` to skip the banner; you can ignore it if it appears. |
| **HuggingFace CLI version hint** in download logs | Cosmetic note from the `hf` / `huggingface-cli` tool. | Ignore. The download command prefers the newer `hf` CLI and falls back to `huggingface-cli` automatically. |
| Nodes can't pull the image: **GHCR auth / not found** | The `ghcr.io/jeyelcode/spark-controlplane` package is **private by default** on GHCR. | Make the package **public**, or `docker login ghcr.io` with a PAT that has `read:packages` on whichever host pulls it. (This affects the portal image; node image pulls use the vLLM image you configure.) |
| Stored secrets become unreadable after a redeploy / restart | `SPARK_SECRET_KEY` changed or was lost. Without it, a Fernet key is generated to `<data_dir>/secret.key`; if `/data` isn't persisted or the key rotates, all encrypted secrets (SSH/sudo passwords, keys, HF token, instance API keys) can't be decrypted. | Set a **stable** `SPARK_SECRET_KEY` and **back it up**, and persist `/data`. If lost, re-enter every secret in the UI. |
| `prereqs` phase fails: **"sudo is not working"** | The SSH user lacks NOPASSWD sudo and no sudo password was provided (or it's wrong). | Configure NOPASSWD sudo on the node, or enter the correct sudo password for that node in **Nodes**, then re-run. |
| `verify` phase: **"Ray did not report 2 nodes"** | The worker hasn't joined — QSFP down, or a Ray service crash-looping while `pip install ray[default]` runs (~1 min on first start). | Wait ~1 min and re-run `verify`. Check `journalctl -u spark-ray-head` / `spark-ray-worker` and QSFP connectivity on both nodes. |
| Instance won't start: **"Model … is not present on node id=…"** | A cluster instance needs the model on **both** nodes; a single instance on **its** node. | Download (auto-syncs) or **Sync** the model to the required node(s), then start again. |
| Instance start returns **"health not yet confirmed"** | Large models can take longer than the startup wait window to load; the unit keeps retrying. | Watch `journalctl -u spark-vllm-<name>.service` / the Status page; it usually goes green once weights finish loading. If it errored, the vLLM output in the job log shows why. |
| Dashboard warns a node is **overcommitted** | Sum of running instances' `gpu-memory-utilization` × per-node memory exceeds the node budget (`SPARK_NODE_MEMORY_GIB`, default 119). | Lower `gpu-memory-utilization`, stop an instance, or don't co-locate two models on one node. |

### Quick checks when something is "down"

1. **Dashboard first** — node reachability, QSFP link, Ray node count, instance
   `/health`. Most failures are visible there.
2. **The job log** — `GET /api/jobs/{id}` (or the job dialog) is the source of
   truth for what the last action actually did and why it failed.
3. **On the node** — `journalctl -u <unit> -f` and `docker logs` for the gritty
   detail; `docker exec spark-ray-head ray status` to confirm the cluster.
