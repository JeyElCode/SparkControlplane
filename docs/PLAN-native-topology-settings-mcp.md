# Plan — native multi-node topology, full vLLM settings control, and MCP

Three tracks. The backend data-model + serve-command contract (Track A) is the source
of truth that the frontend (B) and MCP (C) build against.

## Motivation

The lab's live DeepSeek-V4-Flash deployment is run **by hand**, not through this app,
because the app couldn't express it:
- It's a **Ray-less native multi-node** launch (`-tp 2 --nnodes 2 --node-rank 0/1
  --master-addr <qsfp> --master-port 29501`, worker `--headless`) over the QSFP link —
  the app only knows Ray-based `cluster` topology.
- It uses flags with **no first-class field** (`--kv-cache-dtype fp8`, `--block-size 256`,
  `--max-num-batched-tokens`, `--trust-remote-code`, `--tokenizer-mode deepseek_v4`,
  `--reasoning-parser deepseek_v4`, `--compilation-config {json}`, **multiple**
  `--served-model-name` aliases) — only a fragile raw `extra_args` string.

Goals: (1) add a **native/distributed** topology as a peer of Ray `cluster`; (2) make the
extra settings **first-class + a structured editor**; (3) expose the app over **MCP** for use
as a Claude skill.

---

## Track A — backend: topology + settings (the contract)

### A1. Topology
`models.py`: add `TOPO_DISTRIBUTED = "distributed"` (native torch.distributed multi-node,
headless workers over the QSFP interconnect). Keep `single` and `cluster`.

`Node` already has `qsfp_ip` — the **head** node's `qsfp_ip` is the `--master-addr`. Ranks
are assigned by node `role` (head=0, worker=1). `nnodes` = number of participating nodes (2).

### A2. New Instance columns (all nullable/defaulted — `db.py` auto-migrates new columns)
| column | type | vLLM flag |
|---|---|---|
| `served_model_names` | Text\|None | `--served-model-name a b c` (space/newline-separated; ≥1) |
| `trust_remote_code` | bool=False | `--trust-remote-code` |
| `kv_cache_dtype` | str\|None | `--kv-cache-dtype` |
| `block_size` | int\|None | `--block-size` |
| `max_num_batched_tokens` | int\|None | `--max-num-batched-tokens` |
| `tokenizer_mode` | str\|None | `--tokenizer-mode` |
| `reasoning_parser` | str\|None | `--reasoning-parser` |
| `compilation_config` | Text\|None | `--compilation-config <json>` (stored as a JSON string; validated as JSON) |
| `advanced_args` | Text\|None | structured passthrough: JSON array of `{"flag":"--x","value":"y"\|null}` (null = boolean flag) |
| `master_port` | int\|None (default 29500) | `--master-port` (distributed only) |

Keep the existing `extra_args` column for backward-compat (append raw if present), but the UI
uses `advanced_args`. `tool_parser`/`enable_tool_choice`, `dtype`, `max_model_len`,
`max_num_seqs`, `gpu_memory_utilization`, `tensor_parallel_size` stay as-is.

### A3. `services/templates.py: build_vllm_serve_cmd`
Extend signature with the new fields plus an optional distributed spec
`distributed: {nnodes:int, node_rank:int, master_addr:str, master_port:int, headless:bool}|None`.
Emit, in a stable order:
- positional model path; `--host 0.0.0.0 --port`; `--tensor-parallel-size`;
  `--gpu-memory-utilization`;
- `--served-model-name <all aliases>` (from `served_model_names`, else registry name);
- conditionals: `--max-model-len`, `--max-num-seqs`, `--max-num-batched-tokens`,
  `--dtype`, `--kv-cache-dtype`, `--block-size`, `--tokenizer-mode`, `--reasoning-parser`,
  `--trust-remote-code` (bool), tool-calling (`--enable-auto-tool-choice --tool-call-parser`);
- `--compilation-config <shlex.quote(json)>` (single arg; **must** survive as one token);
- structured `advanced_args` (each `--flag [value]`, shlex-quoted);
- legacy `extra_args` (shlex.split passthrough) last;
- distributed spec → `--nnodes --node-rank --master-addr --master-port` and `--headless`
  on workers. `-tp`/`--tensor-parallel-size` still emitted (world size = tp).

Add unit renderers: `render_instance_unit_distributed_head` (rank 0, serves API) and
`render_instance_unit_distributed_worker` (rank 1+, `--headless`), modeled on the existing
`render_instance_docker_run_single` docker-run (`--network host --gpus all --shm-size
<cfg> --ulimit memlock=-1 --ulimit stack=...`, image = `ClusterConfig.vllm_image`) wrapped
in a systemd unit like `render_instance_unit_cluster`.

### A4. Orchestration (`services/instances.py`, `phases.py`)
For `topology == distributed`: install the head unit on the head node and a worker unit on
each worker node (over SSH, `nodeops.install_systemd_unit`), master-addr = head `qsfp_ip`,
start worker(s) first then head, health-check the head `:port/health`. Stop = stop all units.
Reuse the single/cluster start/stop/health scaffolding.

### A5. Schemas + routers
`schemas.py`: `InstanceCreate.topology: Literal["cluster","single","distributed"]`; add all
new fields to `InstanceCreate`, `InstanceUpdate`, `InstanceOut`. `routers/instances.py`
passes them through. `tensor_parallel_size` default: distributed → (nnodes×gpus_per_node or
provided), cluster → 2, single → 1.

### A6. Validation
`compilation_config` must parse as JSON; `advanced_args` must be a JSON array of
`{flag,value}`; distributed requires ≥2 nodes registered with `qsfp_ip` set.

---

## Track B — frontend (builds on A's API)
`pages/Instances.tsx` create/edit form:
- **Topology** selector: `single` / `cluster (Ray)` / `distributed (native multi-node)`, with a
  one-line helper each.
- **Advanced vLLM settings** section (collapsible): `served-model-name` aliases (chip/list),
  `trust-remote-code` (toggle), `kv-cache-dtype`, `block-size`, `max-num-batched-tokens`,
  `tokenizer-mode`, `reasoning-parser`, and a **`compilation-config` JSON editor** (validated).
- **Advanced args** structured editor: add/remove rows of `--flag` + optional value (replaces
  the raw textarea; keep raw as a fallback "expert" toggle).
- `pages/Nodes.tsx`: ensure `qsfp_ip` is shown/editable (used as master-addr). `pages/Settings.tsx`:
  show MCP enabled + token (from Track C).
Keep the existing design system/components; match current form styling.

---

## Track C — MCP server (builds on A's routers)
- Mount a **streamable-HTTP MCP** server at `/mcp` inside the FastAPI app (`main.py`), using the
  official `mcp` Python SDK (add to `backend/pyproject.toml`).
- **Full control**: tools covering every router — instances (list/get/create/update/start/stop/
  delete), models (list/scan/suggestions/validate/register/download/sync/refresh/delete), nodes
  (list/get/create/update/test/harden/delete), cluster (get/patch config+settings, phases,
  setup, teardown), evals (catalog/tasks CRUD/run/list/get/delete), jobs (list/get/cancel),
  status (get), playground (chat). Expose read-heavy things (status, instances, models, nodes)
  as MCP **resources** too. Reuse the router service functions directly (don't re-implement).
- **Auth**: bearer token from config (`SPARK_MCP_TOKEN`, falls back to a dedicated app token);
  reject `/mcp` without it. Add `mcp_enabled` + `mcp_token` to `config.py`.
- **Docs**: `docs/MCP.md` — the server URL, auth header, tool list, and how to register it as a
  Claude skill (MCP server config).

---

## Sequencing / integration
A is the contract; B and C are built against this document in parallel and integrated after A
lands (or concurrently in worktrees, reconciled at merge — shared files: `main.py`/`config.py`
between A and C). Each track opens its own PR. Build/lint each; end-to-end validation (create a
`distributed` instance dry-run + MCP `list instances`) after merge.
