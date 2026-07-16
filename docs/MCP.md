# MCP server

The Spark Control Plane can expose its **entire** API over the
[Model Context Protocol](https://modelcontextprotocol.io) (MCP) as a
**streamable-HTTP** server. This lets you drive the control plane from an MCP
client — for example attaching it to **Claude** as a custom skill / MCP server —
to list and operate nodes, models, serving instances, the cluster lifecycle,
evaluations and jobs in natural language.

The server is built on the official [`mcp`](https://pypi.org/project/mcp/)
Python SDK and mounted **inside** the existing FastAPI app, so it shares the
same process, database and background-job manager as the REST API and web UI.

---

## Enabling it

The endpoint is **fail-closed**: it is only mounted when it is both enabled
**and** a bearer token is set. If you enable it without a token, it stays off
and logs a warning.

| Env var | Default | Purpose |
|---|---|---|
| `SPARK_MCP_ENABLED` | `false` | Mount the MCP server at `/mcp`. |
| `SPARK_MCP_TOKEN` | _none_ | Bearer token required on every `/mcp` request. |
| `SPARK_MCP_ALLOWED_HOSTS` | _none_ | Host header allowlist when the app is behind a reverse proxy/ingress. Comma-separated (or JSON). Set to your external host(s) — e.g. `spark.example.com` — or the SDK's DNS-rebinding protection rejects the request with **HTTP 421 "Invalid Host header"**. `localhost`/`127.0.0.1` are always allowed. A single `*` disables the host check (trusted-proxy mode). |
| `SPARK_MCP_ALLOWED_ORIGINS` | _none_ | Optional `Origin` header allowlist (comma-separated). |

```bash
# generate a strong random token
export SPARK_MCP_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export SPARK_MCP_ENABLED=true
```

`GET /api/meta` reports the effective state so the frontend can show a badge:

```json
{ "name": "Spark Control Plane", "version": "1.3.5", "mcp_enabled": true }
```

`mcp_enabled` is `true` only when the endpoint is actually mounted (enabled
**and** a token is configured).

---

## Endpoint and auth

| | |
|---|---|
| **URL** | `<base-url>/mcp` (e.g. `https://spark-controlplane.example.com/mcp`) |
| **Transport** | Streamable HTTP (stateless) |
| **Auth header** | `Authorization: Bearer <SPARK_MCP_TOKEN>` |

Every request to `/mcp` must carry the bearer token. Requests with a missing or
incorrect token get `401 Unauthorized` (constant-time comparison) — the token is
never mounted-without, so the endpoint is never anonymous.

> **Serve it over TLS.** The bearer token is sent on every request; terminate
> HTTPS in front of the app (reverse proxy / ingress) so the token is never sent
> in cleartext. Add the client origin to `SPARK_CORS_ORIGINS` if you call it from
> a browser.

---

## Tools

Every REST router has matching MCP tools. Long-running actions (anything marked
_async job_) return a job handle — poll `job_get` / `job_list` for progress,
exactly like the HTTP API.

### Status
| Tool | Description |
|---|---|
| `status_get` | Live cluster status snapshot (nodes, GPUs, Ray, instances, warnings). |

### Instances
| Tool | Description |
|---|---|
| `instance_list` | List all serving instances. |
| `instance_get` | Get one instance by id. |
| `instance_create` | Create a serving instance. |
| `instance_update` | Update serve settings of a stopped instance. |
| `instance_start` | Start an instance _(async job)_. |
| `instance_stop` | Stop a running instance _(async job)_. |
| `instance_delete` | Delete an instance + its systemd unit _(async job)_. |

### Models
| Tool | Description |
|---|---|
| `model_list` | List the registry with per-node presence. |
| `model_suggestions` | Curated suggested models to add. |
| `model_scan` | Import on-disk models not yet in the registry. |
| `model_validate` | Validate a HuggingFace repo id. |
| `model_register` | Register a model by repo id. |
| `model_get` | Get one registry model. |
| `model_download` | Download a model to the head node _(async job)_. |
| `model_sync` | Sync a model to worker node(s) over QSFP _(async job)_. |
| `model_refresh` | Re-check on-disk presence/size on every node. |
| `model_delete_files` | Delete a model's files from node(s) _(async job)_. |
| `model_delete` | Remove a model's registry row (keeps files). |

### Nodes
| Tool | Description |
|---|---|
| `node_list` | List configured nodes. |
| `node_get` | Get one node. |
| `node_create` | Register a head/worker node. |
| `node_update` | Update connection details / credentials. |
| `node_test` | Test SSH/sudo/docker/GPU reachability. |
| `node_harden` | Apply the hardening playbook _(async job)_. |
| `node_delete` | Remove a node. |

### Cluster
| Tool | Description |
|---|---|
| `cluster_config_get` | Get the cluster configuration. |
| `cluster_config_patch` | Patch the cluster configuration. |
| `cluster_settings_get` | Get global settings (HF token presence, poll interval, judge). |
| `cluster_settings_patch` | Patch global settings. |
| `cluster_phases` | List the ordered setup phases. |
| `cluster_setup` | Run cluster setup (full or subset of phases) _(async job)_. |
| `cluster_teardown` | Tear down the cluster _(async job)_. |

### Evaluations
| Tool | Description |
|---|---|
| `eval_catalog` | Available performance + custom eval categories. |
| `eval_task_list` | List custom eval tasks. |
| `eval_task_create` | Create a custom eval task. |
| `eval_task_update` | Update a custom eval task. |
| `eval_task_delete` | Delete a custom eval task. |
| `eval_run` | Start a capability/performance eval of an instance _(async job)_. |
| `eval_list` | List eval runs. |
| `eval_get` | Get one eval run with full results. |
| `eval_delete` | Delete an eval run. |

### Jobs
| Tool | Description |
|---|---|
| `job_list` | List recent background jobs. |
| `job_get` | Get one job with its log lines. |
| `job_cancel` | Cancel a running job. |

### Playground
| Tool | Description |
|---|---|
| `playground_chat` | One-shot chat completion against a running instance's OpenAI endpoint. |

Tool inputs/outputs reuse the same Pydantic schemas as the REST API
(`app/schemas.py`), so field names and validation match `docs/API.md` exactly.

---

## Resources

The read-heavy surfaces are also exposed as MCP **resources** (read-only,
`application/json`), so a client can pull current state without a tool call:

| Resource URI | Contents |
|---|---|
| `spark://status` | Live cluster status snapshot. |
| `spark://instances` | All serving instances. |
| `spark://instances/{instance_id}` | One instance by id. |
| `spark://models` | The model registry. |
| `spark://models/{model_id}` | One registry model by id. |
| `spark://nodes` | All cluster nodes. |
| `spark://nodes/{node_id}` | One node by id. |

---

## Add it to Claude as an MCP server / skill

Any MCP client that supports **streamable HTTP with a bearer token** can attach
the server. Point it at `<base-url>/mcp` and send the token in the
`Authorization` header.

### Claude Desktop / Claude Code (`mcp` config)

```json
{
  "mcpServers": {
    "spark-controlplane": {
      "type": "http",
      "url": "https://spark-controlplane.example.com/mcp",
      "headers": {
        "Authorization": "Bearer <your-token>"
      }
    }
  }
}
```

### Claude Code (CLI)

```bash
claude mcp add spark-controlplane \
  --transport http \
  https://spark-controlplane.example.com/mcp \
  --header "Authorization: Bearer <your-token>"
```

Replace `https://spark-controlplane.example.com` with your own deployment's base
URL and `<your-token>` with the value of `SPARK_MCP_TOKEN`. Once connected,
Claude can call any tool above — e.g. _"list the serving instances"_
(`instance_list`), _"download model 3 and sync it"_ (`model_download`), or
_"run a performance eval on instance 2"_ (`eval_run`, then `job_get`).

---

## How it works

- `app/mcp_server.py` builds a `FastMCP` server (stateless streamable HTTP).
  Each tool calls the **same router handler** the REST API uses — no business
  logic is duplicated — passing an explicit async DB session.
- `app/main.py` mounts it at `/mcp` behind `BearerAuthMiddleware` (a pure-ASGI
  bearer-token gate), and drives its streamable-HTTP session manager from the
  app lifespan. The mount is conditional on `mcp_active` (enabled + token).
- Because it lives in the same process, jobs started over MCP are the same jobs
  you see in the web UI's Jobs view and via `job_list`.
