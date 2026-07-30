# Changelog

## v1.31.0 — the fast interconnect is actually used

- **Multi-node tensor parallelism now runs its all-reduce over RoCE instead of
  TCP.** On a two-Spark pair a 900-token generation was pushing 5.7 GB over TCP
  with the RDMA counters flat at zero, and decoding at ~27 tok/s against a
  ~66 tok/s reference. The cause was not a missing setting but a missing
  *device*: NCCL opens `/dev/infiniband/uverbs*` through libibverbs, and the
  container was never given them. `--network host` shares the network
  namespace, not `/dev`. `NCCL_IB_HCA` on its own would not have helped — it
  filters an already-enumerated device list, and the list was empty, so NCCL
  fell back to sockets without an error. The portal now discovers the RDMA
  device and the matching RoCE v2 GID index for the interconnect, maps the
  verbs devices in, and sets `NCCL_IB_HCA` / `NCCL_IB_GID_INDEX`. The Ray
  topology gets the same treatment.
- **It degrades rather than failing.** A node with no RDMA, an unreachable
  node, a probe that times out — the instance starts over TCP exactly as
  before, and the job log says which of those happened and what to check.
  Devices are discovered at launch rather than written into the script, because
  `uverbs` numbering is not stable across reboots and a stale device path would
  turn a node that served fine into a crash loop.
- **Per-instance environment variables**, so `NCCL_*`, `VLLM_*` and `HF_*`
  settings no longer require building a custom image. Editable on the instance
  form; refused on `cluster` topology, where they would reach only the rank-0
  driver and not the Ray workers. Never accepted from an imported serve profile
  — `LD_PRELOAD` in a stranger's JSON is root code execution on a DGX.
- **Fixed: the arm64 image was never actually tested.** The check added in
  v1.30.0 reported success having executed none of its assertions, because
  `docker run` does not attach stdin without `-i`. It now runs, and greps for a
  sentinel so that a check which cannot execute fails loudly instead of passing
  quietly.

If you run a `cluster`-topology instance, re-run the **Ray setup phase** after
upgrading: restarting an instance execs into the existing Ray container, which
was launched without the verbs devices, so the counters would stay at zero.

Two prerequisites this cannot establish for you, both of which produce the same
silent TCP fallback: the vLLM image must ship rdma-core (`libibverbs`), and the
RoCE port must be ACTIVE with an IP configured on the interconnect. The job log
reports what the probe found.

## v1.30.0 — the portal does the arithmetic

A control plane that asks the same questions as the command line has not earned
its place. Creating an instance meant answering nine vLLM flags whose right
values depend on arithmetic nobody should have to do by hand — whether the
weights fit one box, what fraction of unified memory to hand vLLM, and how much
context the KV cache has room for once the weights are in. Get one wrong and the
failure arrives ten minutes into a weight load, as an allocator error that names
none of it.

**None of this removes a setting.** Every field is still there, still editable,
and the advanced surface is untouched. What is gone is the *requirement to
decide*.

- **Quick launch.** A downloaded model now has a **Run** button on the Models
  page. It works out topology, memory fraction, context length and concurrency
  from the model's shape and what your nodes actually have free, shows you the
  reasoning, and starts it. One decision — which model — instead of nine.
- **"Work it out for me" in the create form**, for anyone who starts there. It
  fills the same fields and honours a topology you have already chosen, so it
  plans *around* your decision rather than replacing it.
- **Every derived value carries its reasoning.** Not a tooltip — a sentence
  naming the numbers it came from: *"This model spends 320 KiB of KV cache per
  token (80 layers × 8 KV heads × 128 dims). 53 GiB is left after the weights,
  so at 8 concurrent requests 16k tokens each fits with room to spare."* A
  recommendation you cannot audit is one you can neither trust nor learn from,
  and the first time it is wrong for your hardware the explanation is what lets
  you fix it.
- **It plans against live state, not defaults.** Memory already held by running
  *and starting* instances is subtracted first — the window in which someone
  launches a second model is exactly the ten minutes the first spends loading.
  With two nodes and two small models it spreads them rather than stacking; with
  one node it tells you the second will not fit and why.
- **It refuses to over-promise.** When the plan cannot honestly recommend
  starting the model, `feasible` is false, the one-click path is disabled, and
  the warning says which number is the problem and what to do about it. Where a
  fact is missing — a gated repo, an air-gapped portal, no `config.json` — it
  says *"context length is unverified"* and leaves the value to vLLM instead of
  inventing one.
- **Model geometry (layers, KV heads, head dimension, trained context) is read
  from the repo's `config.json`** and cached on the model row. Fetched lazily on
  first plan, not at registration: adding a model stays a local operation that
  works without internet access.
- `POST /api/instances/plan` and the `instance_plan` MCP tool expose the same
  planner. The response is a valid create body — add a name and a model id and
  post it back — so an agent can stop guessing at `gpu_memory_utilization` too.

Two fixes found while building it:

- **`gpu_memory_utilization` was completely unvalidated.** Any float was
  accepted, including `0`, which vLLM refuses to start on, and `0.99`, which
  dies in the allocator. Now bounded to 0.1–0.95 at the API boundary, where it
  costs a second instead of ten minutes.
- **Two releases had silently never shipped an image.** `v1.14.0` and `v1.25.0`
  are the only gaps in the published tag sequence: building the React app for
  `linux/arm64` runs npm under QEMU, which intermittently raises SIGILL inside
  npm's child processes, and a surviving child then keeps the build step alive
  until the job is killed at GitHub's six-hour ceiling — after which nothing is
  pushed. Anyone who pinned either tag got a pull failure. The frontend is now
  built on the runner's own architecture (its output is static JS/CSS, identical
  whatever CPU emits it), every CI job has a timeout so a hang costs twenty
  minutes rather than six hours, and the release now **runs** the arm64 image to
  confirm it is genuinely aarch64 and serves a usable SPA — which nothing had
  ever done for the one architecture a Spark can execute.

## v1.29.0 — things that have to be true before anyone else runs this
- **CI now runs the tests.** The backend job was `pip install .` and an import
  check: 227 tests, including the upgrade-in-place simulations and the security
  regressions, had never run on a pull request, and `ruff` was a declared dev
  dependency that was never invoked. Both now run on every PR, the release
  workflow will not publish unless they pass, and CI builds `linux/arm64` too —
  previously the architecture that actually runs on a Spark was published
  without ever being built in CI. Eight pre-existing lint errors fixed to make
  the gate green. **The linter is pinned and its rule set declared explicitly** —
  the first CI run proved why: an unpinned `ruff>=0.5` picked up a newer release
  whose wider defaults turned a locally-green tree into 342 findings. A gate
  that fails for reasons the author did not cause is a gate that gets switched
  off, so the rules are now an intentional choice (defect-finding: undefined
  names, unused bindings, unresolved imports) rather than whatever the installed
  version happens to default to.
- **Dependencies are capped at tested majors.** Enabling the gate immediately
  found what it was for: CI resolved `mcp 2.0.0`, which moved
  `mcp.server.fastmcp`, so the MCP server failed to build — and because that
  build is deliberately fail-open, the only symptom on a fresh install is that
  `/mcp` silently does not exist. Capped at `<2` (see #67 for the 2.x port).
  Anyone whose image was built recently should check `GET /api/meta`, which
  reports `mcp_enabled`.
- **SSH host keys are pinned, trust-on-first-use.** `known_hosts=None` meant
  every connection accepted whatever key the host offered — on first contact and
  forever after — and the next thing sent over that connection is the sudo
  password. Anyone able to intercept the LAN path to a node could take root on
  it. The first successful connect now records the key and every connect after
  is verified against it; a mismatch refuses to connect and says so, naming both
  explanations (a rebuild, or an interception) and the recovery action. Nodes
  show whether they are pinned, and "Forget host key" is the deliberate
  re-trust after a legitimate reinstall. Existing installs pin on next
  connect — nothing is locked out by the upgrade.
- **The README no longer misreports security.** It said *"portal login is not
  enabled in this build… put it behind a reverse proxy with auth"* — nine
  releases stale, and the first thing an evaluator reads. It now describes the
  four auth modes, session revocation, and states plainly what `none` means.
- **A failed request no longer looks like an empty cluster.** `usePoll` exposes
  an error that Instances, Models and Nodes never rendered, so a dropped
  connection or an expired session displayed "No instances yet" — telling the
  operator something false about their hardware. All three now show what failed
  and that it is a portal problem, not a cluster one.
- **Added `SECURITY.md`** with a private disclosure route, an explicit statement
  of what the portal can do to your hardware, and the current limitations —
  single authorization level, no audit log, unscoped MCP token, single-process
  design — written down rather than discovered.

## v1.28.1 — jobs stop being stranded by a restart
- **Fixed: a portal restart left in-flight jobs recorded as `running` forever.**
  A job exists only as a task in one process, so every image rollout — which
  under GitOps is routine — orphaned whatever was running: a spinner in the UI
  that never resolved, and an in-memory `is_running()` that disagreed with the
  database. Startup now resolves them, which is safe by construction because
  the task registry is empty at that point: anything the database still calls
  live cannot be.
- **The summary is honest about what it doesn't know.** It says the portal
  stopped tracking the job and that work already sent to the nodes may have
  continued — rather than claiming the operation failed, which would be a guess.
  For an interrupted instance start, v1.24's status reconciliation independently
  reports whether the model actually came up.
- **A lost status write is now loud rather than silent.** Job bookkeeping must
  never crash a running job, so a failed write is swallowed — but that treated a
  dropped log line and a dropped *terminal status* identically, and the latter
  leaves a finished job reading as still running with no restart to explain it.
  Status transitions now retry for longer and log at ERROR when they are lost.
- Internal: `services/jobs.py` now binds the database sessionmaker late, like
  every other service. Binding it at import pinned the module to whichever
  engine existed at import time; production never noticed, but it made the
  module untestable with the standard fixture and was a trap either way.

## v1.28.0 — sessions you can actually end
- **Signing out now revokes.** Before this, `logout` only deleted the cookie —
  which is a *request* to a cooperating browser. A copy taken beforehand kept
  working until it expired, and there was no action an operator could take at
  all. Logout now kills that specific session server-side, effective on the
  very next request.
- **"Sign out all my sessions" and "Sign out a user"** in Settings → Sessions,
  for a suspected leak or an offboarding, plus a "sign out everyone" panic
  button. None of it is undone by a restart, a GitOps sync, or a backup restore
  — `session_revocations` is deliberately excluded from the backup bundle,
  because a restore replaces tables wholesale and a month-old bundle would
  otherwise resurrect a session revoked last week.
- **Rotating `SPARK_ADMIN_PASSWORD` invalidates every old session by itself**,
  with no stored state and nothing to click: the session carries a fingerprint
  of the credential config, and because settings are cached a rotation already
  requires a restart. Tightening a required group does the same.
- **In-flight WebSockets are re-checked.** The status, log and job streams
  authenticated once at the handshake and then ran for hours — the dashboard
  holds one open for as long as the tab is. A revoked session kept receiving
  live cluster telemetry until the user closed the browser. Revocation that
  applies only to *new* connections is not revocation.
- **`SPARK_AUTH_COOKIE_SECURE` is now `auto` by default** (`true`/`false` still
  force it, and existing boolean values still parse). Auto sets Secure when the
  request is HTTPS, reading `X-Forwarded-Proto` because behind an ingress
  uvicorn sees plain HTTP from the ingress pod — i.e. exactly the deployment
  where Secure matters. Settings → Sessions shows what the current request
  resolved to, so a mis-set proxy is visible rather than a mysterious login
  loop.
- **Everyone signs in once after this upgrade.** Older cookies carry no session
  id or issue time, so they cannot be checked against any of this; rather than
  leave a class of session that is structurally unrevocable, they are rejected.
- Two failure modes worth naming, because both would otherwise be discovered in
  production. An unloaded revocation list **denies** rather than allows — the
  opposite polarity to the API-key cache, since an unloaded list that said "no"
  would silently un-revoke everything — so the portal refuses to start if it
  can't read it. And a **backward clock step** (NTP correction, bad RTC,
  restored snapshot) would otherwise make every freshly minted session land
  before an existing revocation cutoff, leaving the portal permanently
  unloggable-into with no fix but editing the database; new sessions are now
  issued after any cutoff that applies to them.
- **The honest guarantee, in one sentence:** you can end any portal session
  from the portal, it takes effect on the next request and survives a restart —
  but the portal still never asks your directory anything after sign-in, so an
  account disabled in Entra or LDAP keeps access here until someone presses one
  of these buttons or the 8-hour SSO session cap expires.
- New: `GET /api/sessions`, `POST /api/sessions/revoke`; MCP `session_status`
  and `session_revoke`. One new table (`session_revocations`), created
  automatically.

## v1.27.0 — single sign-on (OIDC)
- **`SPARK_AUTH_MODE=oidc`**: authorization code + PKCE against Entra ID,
  Keycloak or Okta. The portal never sees a password — MFA and conditional
  access are enforced by your identity provider — and it deliberately keeps
  **no** access or refresh token: it needs an authenticated identity, not
  delegated API access, and a token it never holds is one it can never leak.
  The login page in this mode has no username or password fields at all.
- **Authorization is mandatory, not optional.** `SPARK_OIDC_GROUP_REQUIRED` must
  be set or the mode refuses to serve. This is the LDAP lesson applied: an
  optional group check means the default deployment authenticates an entire
  directory into a portal that SSHes to DGX nodes as root. **App roles are the
  recommended default** over group GUIDs — the values are strings you choose,
  and they avoid Entra's *groups overage*, where a user in ~200+ groups gets no
  `groups` claim at all and would be silently denied while testing looked fine.
  That case now fails closed with an actionable message rather than a shrug.
- Controls worth naming, because each is a hole in a naive implementation:
  - **Discovery endpoints are pinned to the issuer's own origin.** Otherwise a
    hostile discovery response relocates `token_endpoint` and receives both the
    authorization code and the client secret.
  - **Redirects are never followed** on discovery, JWKS or token exchange — a
    302 from the pinned host would walk straight back off-origin.
  - **Signing keys come only from the discovered JWKS.** A `jku`, `x5u` or
    embedded `jwk` header is ignored; a token never chooses its own verifier.
  - **Only asymmetric algorithms**, rejected at config-parse time so the
    alg-confusion attack isn't expressible in the first place.
  - **`redirect_uri` comes from config, never the request** — behind an ingress
    the Host header is attacker-controllable.
  - **`azp`, not `strict_aud`**, guards against a token minted for a sibling
    application: a single-element `aud` array is legal and is what Entra emits,
    so `strict_aud` would reject spec-compliant providers.
  - **A stale key set has a hard ceiling.** Serving cached keys across a blip is
    right; serving them forever means a key the provider *revoked* stays trusted
    for the length of an outage.
  - state, nonce and the PKCE verifier live in a short-lived encrypted cookie,
    and `state` is compared *before* any network call — so a forged callback
    never costs a round trip or burns a real authorization code.
- **Sessions now carry a purpose tag and the mode that minted them.** Two
  consequences, both fixes to existing behaviour: one Fernet key encrypts
  sessions, stored SSH passwords and now the login transaction, and without a
  tag any blob decrypting to the right shape was a candidate session; and
  turning SSO on no longer leaves password-mode cookies valid, which would have
  been exactly the downgrade SSO exists to end.
- **Fixed: every tampered session cookie logged an ERROR.** Cookies are user
  input, not an operational fault — and in oidc mode the *unauthenticated*
  callback decrypts attacker-supplied cookies, so this was a log-flooding
  vector. Also fixed: a session token with trailing bytes after its base64
  padding was accepted, because Python's decoder silently discards them.
- **What SSO does *not* give you, stated plainly:** the portal never re-asks the
  provider after sign-in, so disabling an account in Entra does not immediately
  lock it out of the portal. A disabled account keeps working until its cookie
  expires — which is why oidc mode caps sessions at 8h rather than the 24h
  default, and why that cap *is* the offboarding guarantee. Sign-out does end
  the provider's session, but only for a user who clicks it.
- `pyjwt[crypto]` is now a declared dependency. It was already present via the
  MCP SDK, but depending on a transitive pin is how this silently breaks.

## v1.26.1 — profile import: allowlist, not denylist
- **Fixed: the v1.26.0 import filter was bypassable.** Profile import kept
  `advanced_args` and filtered a denylist of ten dangerous flags. A review
  demonstrated the bypass in minutes — `--middleware <dotted.path>` makes vLLM
  import an arbitrary object into the API server, `--tool-parser-plugin
  <file.py>` executes a Python file, `--allowed-local-media-path /` turns the
  inference endpoint into an arbitrary file read. None were on the list, and
  none were reported as dropped, so an operator importing a hostile profile was
  told nothing had been removed. Since vLLM renders `advanced_args` *after* its
  own flags, a duplicated flag also won on argparse last-wins.
  `compilation_config` was not filtered at all.
- **Passthroughs are now dropped wholesale on import**, exactly like
  `vllm_image` and `extra_args`: `advanced_args` and `compilation_config` are
  removed and reported. A denylist of flags against an upstream CLI that grows
  every release is out of date the moment it ships; an allowlist of settings is
  not. Nothing that matters is lost — the recipe this feature exists for
  (Laguna) needs none of them, and every one remains settable by hand on a
  profile you author yourself, where you are the author rather than the
  recipient. A malformed passthrough is now discarded quietly instead of
  failing the whole document, while a bad value in a field that *is* imported
  still rejects it.
- **Removed `trust_remote_code` from the `laguna-fp8-dual` built-in.** It was
  not part of the configuration that actually served — it was added by
  implementation, not measurement — and shipping it enabled while the same
  module refuses it from an imported profile was incoherent.

## v1.26.0 — serve profiles: stop rediscovering a model's flags
- **Known-good vLLM settings, saved under a name.** Getting a large model to
  serve is a dozen interacting flags — context length, GPU memory fraction,
  batch limits, the right reasoning and tool parsers — and one wrong value is an
  out-of-memory ten minutes into a weight load. Until now that knowledge lived
  in shell history or a stranger's README: the model catalogue could tell you a
  repo's size and tool parser, but nothing about *how to serve it*.
  - **Start from a profile** when creating an instance: it prefills the serve
    settings and everything stays editable. A profile is a starting point, not
    a lock.
  - **Save as profile** on any instance captures the configuration you just got
    working — serve settings only, never the name, port, node, API key or TLS
    material, which are per-instance facts and would make a profile
    unshareable at best.
  - **Export / Import** as a JSON document, so a working configuration can be
    committed, gisted, or handed to whoever is standing up their own pair.
  - Profiles ride along in the config backup bundle.
- **Three built-ins ship with the image**, led by `laguna-fp8-dual` — the
  poolside/Laguna-S-2.1-FP8 configuration that actually served on a Spark pair
  (TP=2 distributed, `gpu_memory_utilization 0.72`, `max_model_len 131072`,
  `max_num_seqs 8`, `max_num_batched_tokens 2048`, `poolside_v1` parsers). The
  other two are deliberately generic starting points rather than invented
  numbers. Built-ins refresh from the image on every start, so they can't be
  edited in place — duplicate one and edit the copy, and an upgrade can never
  silently overwrite your work.
- **An imported profile may describe how to serve a model, never what code to
  run.** Instances launch with `--gpus all`, `--network host` and the models
  directory mounted, so an imported profile that could pick the container image
  would be remote code execution as root on a DGX. Import therefore drops
  `vllm_image` and the raw `extra_args` passthrough, strips
  `--served-model-name` (which would let a shared file hijack gateway routing by
  claiming another model's name) along with `--model`, `--api-key`, `--host`,
  `--load-format` and friends from `advanced_args`, and refuses to silently
  enable `trust_remote_code`. Everything removed is reported back rather than
  dropped in silence — and all of it remains settable by hand on your own
  profiles, where you are the author. Settings are validated through the same
  schema a hand-typed instance faces, so a profile can't smuggle a value the API
  would have rejected.
- New: `GET|POST /api/profiles`, `POST /api/profiles/from-instance/{id}`,
  `PATCH|DELETE /api/profiles/{id}`, `GET /api/profiles/export`,
  `POST /api/profiles/import`; MCP `profile_list` / `profile_create` /
  `profile_from_instance` / `profile_export`. One new table (`serve_profiles`),
  created automatically; no existing row is touched.

## v1.25.0 — the gateway grows up: keys, attribution, limits
- **Per-client API keys.** Every consumer used to hold the same bearer token,
  so offboarding one client or containing one leak meant rotating for everyone
  at once — a coordinated outage — and attribution was structurally impossible
  because nothing in the request identified the caller. Issue a key per client
  in Settings → API gateway; revoking one takes effect on its very next
  request. The token is shown exactly once and stored only as a SHA-256 digest.
  **The shared token keeps working**, so clients already configured with it are
  untouched by the upgrade.
  - Why SHA-256 and not bcrypt: the secret is 256 bits of CSPRNG output, not a
    human-chosen password, so a KDF work factor is a rounding error against an
    already-infeasible search — while a per-record salt would destroy the O(1)
    lookup this needs on *every* request. It is also strictly better than what
    it replaces: the shared token is stored reversibly, and travels inside every
    backup bundle.
- **Gateway traffic is visible at last.** The gateway recorded nothing — not a
  counter, not a log line — so every gateway-level outcome was invisible: a bad
  token (401), an unknown model (404), an unreachable (502) or still-loading
  (503) instance left no trace anywhere, and the operator found out when someone
  complained. Usage → Gateway traffic now shows per-client request counts,
  errors, rejections, average latency and time-to-first-byte, plus a recent-
  requests view for "what happened at 14:32". Exported to Prometheus as
  `spark_gateway_*`, labelled by client and model.
  - No per-request row ever reaches SQLite. Requests fold into in-memory
    counters; a background task flushes 5-minute *aggregates* to a new
    `gateway_samples` table. For a streamed response the record only completes
    when the generator closes — while the client may already be disconnecting —
    and awaiting a SQLite writer there, in contention with the telemetry loops
    and the reconciler, is the worst possible place to block.
- **Per-client limits, so one runaway client can't starve the rest.** The
  `kv_cache_full` alert could already *detect* saturation but offered no lever.
  Concurrency is the primary limit rather than requests/min: with continuous
  batching the contended resource is KV-cache blocks held by in-flight
  sequences, and a client's concurrent request count *is* its share of exactly
  that. It is scoped per (client, instance) — KV cache is per-instance, so a
  client using two models isn't hurting either one. Requests/min is available as
  a secondary guard. Over the cap returns 429 with `Retry-After` and says which
  limit was hit.
  - **Everything defaults to unlimited**: an upgrade must never start throttling
    traffic that worked yesterday. The operator's own portal/Playground session
    is exempt unless `SPARK_GATEWAY_LIMIT_SESSION=true`.
- **Fixed: a non-ASCII bearer token was unusable.** Header values arrive
  latin-1-decoded, so comparing them as `str` raised `TypeError` (an HTTP 500)
  on `/v1` and `/metrics` — and the obvious fix, re-encoding as UTF-8, silently
  turns that into a permanent 401 because it produces four bytes where the
  client sent two. The wire bytes are now recovered properly, so a gateway or
  metrics token containing `æøå` both fails closed when wrong *and* works when
  right.
- **Fixed: a running instance with no metrics yet crashed the whole alert
  tick.** Formatting a null KV-cache percentage raised `TypeError` out of
  `gather_facts`, which aborted the entire evaluation — silently disabling
  *every* alert rule until the first scrape landed, on every portal restart.
- New: `GET /api/gateway/traffic`, `GET|POST|PATCH|DELETE /api/gateway/keys`,
  MCP `gateway_traffic` / `gateway_key_list` / `gateway_key_revoke`. New env:
  `SPARK_GATEWAY_MAX_CONCURRENT`, `SPARK_GATEWAY_MAX_RPM`,
  `SPARK_GATEWAY_LIMIT_SESSION`, `SPARK_GATEWAY_ROLLUP_SECONDS`. Two new tables
  (`api_keys`, `gateway_samples`) are created automatically; no existing row is
  touched.

## v1.24.0 — instance status tells the truth
- **Status is now reconciled against the nodes, every ~10s.** A start no longer
  reports `running` the instant the systemd unit is installed — that happened
  *before* vLLM had loaded a single weight, so a model that OOM'd at load or
  crashed after hours of serving stayed `running` forever. The consequences
  were real: the `/v1` gateway kept routing external traffic to a dead
  upstream, and the scheduler considered the start "reached" so it never
  retried. Now the start job installs the unit and leaves the instance
  `starting`; a background observer promotes it to `running` only on proof (a
  200 from `/health`), and demotes a dead one to `error`.
- **Telling a slow load from a dead one** is the hard part, since a large FP8
  model legitimately takes many minutes. Four independent signals rather than
  one timeout: a systemd unit that stays down (45s), a **crash loop** —
  restarts climbing while the instance has never once been healthy, which is
  what catches an out-of-memory at load in ~40s instead of half an hour — a
  never-healthy deadline (30 min), and health lost after serving (2 min). An
  unreachable node never demotes anything: if we can't see the node, the broken
  thing might be our own network.
- **The gateway now says which it is.** A model still loading returns 503 with
  `Retry-After` and, once we've seen a successful load, how long that took; a
  crashed one returns 503 quoting the actual error instead of inviting a
  pointless retry. Instance cards show elapsed load time next to `starting`, so
  a long load reads as progress rather than a hang.
- **New API gateway panel** on the Instances page: the model names clients can
  actually call right now, which instance and node each one routes to, and a
  **Copy client cfg** that finally emits something usable — the portal's own
  origin plus the model name (and a bearer-token note when portal auth is on).
  The old button emitted `Base URL: :8001/v1` for cluster/distributed
  instances, which is not a URL, and pointed at instance ports the gateway
  exists to hide. `GET /api/gateway/routes` + MCP `gateway_routes` expose the
  same table.
- **Fixed: the gateway advertised a model name vLLM would reject.** When an
  instance has aliases, `--served-model-name` *replaces* the registry name, but
  the gateway listed and routed the registry name anyway — so calling it got a
  confusing 404 from vLLM itself. The routing table now mirrors exactly what
  the unit launches, and the panel flags any name the instance doesn't confirm.
- **Fixed: pooled SSH connections outlived their transport.** After an sshd
  restart, network flap, or node reboot, the cached connection was reused
  forever and every command to that node failed until the *portal* was
  restarted — telemetry reported the node permanently unreachable even once it
  had recovered. Connections are now liveness-checked before reuse and dropped
  on transport failure. A command *timeout* deliberately keeps the connection
  (a slow script is not a broken link), and a non-zero exit status never
  evicts.
- Also: a shutdown's "instances this will kill" warning and fleet image updates
  now include instances that are still loading (they occupy the GPU too);
  starting/failed instances raise the right alerts instead of falling silent;
  `POST /api/instances/{id}/reconcile` (and MCP `instance_reconcile`) re-probes
  on demand; a second start on an instance already starting is refused with 409
  rather than two jobs fighting over one unit.
- Upgrade-in-place: three nullable columns are added to `instances`
  automatically. Existing rows keep their status untouched — the observer
  decides from live evidence. New tuning knobs: `SPARK_RECONCILE_ENABLED`,
  `SPARK_RECONCILE_TICK_SECONDS`, `SPARK_RECONCILE_START_DEADLINE_SECONDS`,
  `SPARK_RECONCILE_UNHEALTHY_SECONDS`, `SPARK_RECONCILE_UNIT_DEAD_SECONDS`,
  `SPARK_RECONCILE_CRASHLOOP_RESTARTS`.

## v1.23.1 — security hotfix (upgrade immediately)
- **Fixed an unauthenticated arbitrary file read in the SPA catch-all
  (critical).** ASGI delivers the URL path with `%`-escapes decoded and `../`
  segments intact, so `GET /../../../data/secret.key` escaped the frontend
  build directory and returned the file — with no session, in any auth mode.
  In the shipped image layout that exposed the Fernet master key (which
  decrypts every stored SSH key, sudo password, HF token and instance API key,
  and can forge session cookies), the SQLite database, and any other file
  readable by the portal process. The handler now resolves the candidate path
  and requires containment in the build directory before serving it.
  Regression tests cover encoded and multi-level traversal.
- **`/openapi.json`, `/docs`, `/redoc` are no longer served when portal auth is
  on.** `AuthMiddleware` guards only `/api` and `/metrics`, so the doc routes
  were handing unauthenticated visitors the full endpoint map. They remain
  available in `none` mode, where nothing is gated anyway.
- **Non-ASCII credentials no longer 500 the login endpoint.** `compare_digest`
  raises `TypeError` on `str` inputs holding non-ASCII, so an admin password or
  username containing æøå was an unconditional lockout with an opaque error.
  Comparison is now on UTF-8 bytes (still constant-time).

## v1.23.0 — ports are plumbing now
- **Instance ports auto-assign.** With the /v1 gateway as the entry point,
  ports stopped being something the operator should think about: leave the
  Port field empty and the portal allocates the next free one (from 8000,
  skipping Ray/portal infrastructure ports and TLS ports). Distributed
  rendezvous master-ports auto-assign too (from 29500, counting only
  instances that actually bind one).
- **Explicit ports get real validation.** Choosing a port by hand now checks
  for collisions against every instance binding the same serving node —
  including TLS sidecar ports — and returns a clear 409. Two singles pinned
  to *different* nodes may deliberately share a port. `null` in a PATCH means
  "keep", never "clear".
- **Per-instance TLS demoted to "Direct-access TLS (rarely needed)".** With
  external HTTPS handled by the ingress in front of the portal and the
  gateway forwarding internally, per-model certificates only matter for
  clients that bypass the gateway — the collapsed section now says exactly
  that. (Fully functional and unchanged for existing TLS instances.)

## v1.22.0 — full MCP parity
- **28 new MCP tools** close the gap between the MCP server (frozen at the
  pre-v1.6 feature set) and everything since: telemetry + instance history,
  NIC detection, power controls (with `power_affected` preview), one-shot log
  tailing, alerts, usage history, schedule CRUD, backup/restore (incl. S3),
  storage report/cleanup, and image tag discovery / rolling updates — 74
  tools total. Destructive tools (power, restore, orphan delete, image
  update) are explicitly marked in their descriptions so agents ask before
  acting; that marking is pinned by a test.

## v1.21.0 — OpenAI-compatible API gateway
- **One endpoint for external clients.** `POST /v1/chat/completions` (plus
  `/v1/completions`, `/v1/embeddings`, `GET /v1/models`) on the portal itself:
  the `model` field routes to whichever RUNNING instance serves that name
  (registry name or any `served_model_names` alias). SSE token streams pass
  through unbuffered; each instance's internal API key is injected on the way
  through, so clients only ever hold the gateway credential. Per-instance
  ports keep working for internal use.
- **Schedule-aware errors.** Unknown model → 404 listing what's live right
  now; a model that exists but is outside its live window → 503 including
  *when its next window opens*.
- **Auth (per operator decision):** with portal auth ON, `/v1` requires
  `Authorization: Bearer <gateway token>` (Settings → API gateway, stored
  encrypted, or `SPARK_GATEWAY_TOKEN`; a logged-in portal session also
  works). With auth off (homelab default) the gateway is open. Supersedes
  the v1.0 "no unified gateway" decision.

## v1.20.1 — LDAP TLS certificate validation
- **fix(auth): LDAPS/STARTTLS connections now validate the directory's
  certificate by default.** The `ldap3` library's own default is `CERT_NONE` —
  encrypted but unauthenticated, leaving credentials exposed to an active
  man-in-the-middle. The portal now always passes an explicit TLS policy:
  `CERT_REQUIRED` against the system trust store (new default), a private CA
  via `SPARK_LDAP_CA_FILE`, or an explicit opt-out with
  `SPARK_LDAP_VERIFY_CERT=false` for self-signed lab DCs. Fail-closed
  throughout: an invalid certificate or a missing CA file blocks logins
  rather than silently downgrading.

## v1.20.0 — storage cleanup tools
- **Per-node storage breakdown** (Models page → Storage → Scan): one batched
  `du`/`df` per node reports every directory in the models dir mapped against
  the registry — flagging **orphans** (directories no registry row references)
  — plus the HuggingFace cache size and disk used/free with a meter.
- **Cleanup actions as jobs**: delete an orphan directory (name validated at
  the API boundary; a *registered* model name is refused server-side — use the
  Models delete flow for those), and clear the HF cache per node or fleet-wide
  (cached downloads only; model files untouched).
- **Free-space projection on download**: Validate now appends the head node's
  free space next to the model size, with a warning when the model wouldn't
  fit. New endpoints: `GET /api/storage`, `POST /api/storage/delete-orphan`,
  `POST /api/storage/clear-hf-cache`.

## v1.19.0 — config backup & restore (manual + scheduled S3)
- **One-file config snapshot.** `GET /api/backup/export` downloads a JSON
  bundle of every configuration table — nodes, cluster config, settings,
  model registry + per-node states, instances, schedules, custom eval tasks
  (history is deliberately excluded). Secrets travel **Fernet-encrypted**: a
  restore is fully functional with the same `SPARK_SECRET_KEY`; with a
  different key everything else restores and the response lists exactly which
  secrets were cleared and need re-entering. `POST /api/backup/import`
  replaces all config tables (ids preserved, FKs intact).
- **Scheduled backups to S3-compatible storage** (MinIO, Cloudflare R2, AWS,
  Backblaze — anything speaking S3): endpoint/bucket/prefix/region/keys
  configured in **Settings → Backups** (secret key stored encrypted),
  interval (default 24 h) and retention (keep newest N, default 14).
  Implemented with a **built-in minimal SigV4 client** (~150 lines, verified
  against AWS's published signature test vector) instead of an 80 MB boto3
  dependency. "Back up now", backup list with one-click **Restore**, and
  restore-from-file for fresh installs.



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
