# Enterprise readiness — an honest assessment

**Question asked:** what would it take for this to become official, enterprise-ready
software that NVIDIA ships with DGX Spark?

**Method:** seven independent expert reviews (architecture, product security,
code quality, UX, enterprise operations, NVIDIA product fit, competitive
position), each reading the actual code, followed by a synthesis that had to
adjudicate where they disagreed. Every claim below with a `file:line` was
verified directly against the repository.

**Date:** 2026-07-29, at v1.28.1 (65 commits, 30 releases, ~22k lines).

---

## The headline: the goal is mis-specified

**"NVIDIA ships this in the box" is the wrong target — not because the code is
bad, but because of what the software is.**

Two facts decide it, and neither is fixable by writing better code.

**1. This is a substitute for NVIDIA's inference product line, not a delivery
vehicle for it.** `grep -rniE '\bnim\b|tensorrt|trt-llm|triton|nemotron'` across
`backend/app`, `frontend/src` and `docs` returns **zero hits**. The project
orchestrates upstream vLLM containers pulling weights from HuggingFace. A vendor
does not ship first-party software that routes its customers around its own
monetised path. No amount of RBAC, SBOM or accessibility work changes that
calculus.

**2. There is no evidence yet, because there hasn't been time.** First commit
2026-06-26, latest 2026-07-29 — **33 days**, one author, one physical pair of
machines. Nothing here has been run by anyone who did not write it, on hardware
other than the author's, against an identity provider other than the one he
tested. That is not a criticism of the pace; it is a statement about what a
vendor's adoption process requires and what 33 days can produce.

**On effort:** the seven lenses estimated 6–9, 6–12, 9–15 and 12–18 months
*each, scoped to their own axis*. They stack rather than parallelise. Full
OEM-readiness is realistically **3–5 person-years**. An AI assistant multiplies
mechanical work several-fold and field time, legal clearance and judgement not
at all. **Solo OEM readiness is not achievable, and a year spent pursuing it
would be a bet against a low prior.**

### The better target

**Become the de-facto reference implementation for DGX Spark multi-node
bring-up** — the thing NVIDIA's docs and DevRel link to, that other Spark owners
actually run. Reachable in months rather than years, valuable whether or not
NVIDIA ever engages, and a far better route *into* a vendor conversation than
asking one to adopt 22k lines of someone else's code.

### What is genuinely irreplaceable here

Not the 22k lines. A specific ~600–1,500 of them, and they encode operating
knowledge that is not reproducible from public documentation:

- `services/phases.py:98-190` — the QSFP fabric phase machine: interface
  pre-flight that names the real interfaces on failure, carrier detection that
  warns rather than fails, `nmcli` persistence that degrades to a temporary IP
  because `ipv6.method disabled` is rejected by some NetworkManager builds, and
  full-mesh ping verification with the reasoning written down (at 3+ nodes NCCL
  flows worker-to-worker, so a head-only check would miss a bad switch port).
- `services/templates.py` — socket-interface pinning for NCCL/UCX/Gloo/TP, so
  collectives don't silently fall back to the management LAN.
- The GB10 unified-memory path via `/proc/meminfo`, because `nvidia-smi` reports
  N/A.
- The `nvcr.io/nvidia/vllm` vs `vllm/vllm-openai` ENTRYPOINT divergence.

The rest — gateway, telemetry, evals, playground, alerting, schedules —
duplicates LiteLLM, DCGM-exporter + Grafana, and Run:ai. Good work, but at OEM
scale it is maintenance liability competing with internal roadmaps.

---

## Verified defects worth fixing regardless of any of the above

These were all confirmed directly. Each is a real problem in software running on
real hardware today.

| Finding | Evidence |
|---|---|
| **CI never runs the tests.** The backend job is `pip install .` plus an import check. 220 tests, including upgrade-in-place simulations and security regressions, never run on a PR. `ruff` is a dev dependency and is never invoked. Zero frontend tests. | `.github/workflows/ci.yml:22-31` |
| **The release workflow is ungated** and publishes `linux/arm64` — the architecture that actually runs on a Spark — which CI never builds. | `ci.yml:44` vs `release.yml` |
| **README is nine releases stale on security.** It says *"Portal login is **not** enabled in this build… put it behind a reverse proxy with auth."* Password auth shipped in v1.19, OIDC in v1.27. Anyone evaluating this reads that first. | `README.md:202-204` |
| **Ray is unpinned** — `ray[default]>=2.9` installed inside a systemd unit with `RestartSec=5`. An upstream release can break the cluster overnight. | `services/templates.py:60` |
| **SSH accepts any host key, forever** (`known_hosts=None`), then feeds the sudo password on stdin across 29 `sudo=True` operations. Anyone who can MITM the LAN path to a Spark gets root. | `ssh/client.py:125` |
| **Nothing prevents two portals** driving one cluster. All authoritative state is process-local by design; a rolling update with `maxSurge>0` transiently runs two schedulers, two reconcilers, two telemetry engines. The single-replica requirement exists in one line of prose and is enforced nowhere — and there are no Kubernetes manifests in the repo at all. | `ratelimit.py:22`, `sessions.py:57`, `jobs.py:81`, `inst_state.py:46`, `docs/ARCHITECTURE.md:48` |
| **`/api/health` cannot fail** — it returns a hardcoded literal, so a liveness probe happily keeps a broken portal alive. | `main.py:183-185` |
| **Failed fetches render as empty states.** `usePoll` exposes an `error` field; Instances, Models and Nodes never render it, so a dropped connection or expired session shows "No instances yet". | `lib/hooks.ts:39` vs the three pages |
| **`_add_missing_columns` is not a migration system.** It only ever adds nullable columns — never a constraint, index, backfill or rename. Upgraded databases already diverge from fresh ones: `instances.node_id` has no foreign key, and `master_port`/`tls_enabled`/`tls_port` are nullable where a fresh install makes them NOT NULL. There is no `schema_version`, so nothing can even detect which shape a given install has. | `db.py:39-64` |
| **One authorization level, no audit trail.** Any valid session can power off hardware, read SSH credentials and export the config bundle. `Job` has no actor column; `grep -i audit` returns nothing. | `middleware.py:76`, `models.py:454-475` |
| **LDAP still authenticates the whole directory by default** — the mandatory-group lesson from OIDC was never back-applied. | `services/auth.py:334` (issue #52) |

---

## Roadmap

Staged so that **each stage is worth doing even if no vendor conversation ever
happens.** Nothing here bets a year on a partnership.

### Stage 0 — Make the repo tell the truth (30 days)

Fix the stale README. Make CI run `pytest` and `ruff` and gate releases on it.
Take an exclusive lock on `SPARK_DATA_DIR` so a second process refuses to boot
rather than silently double-driving the cluster. Pin Ray. Replace
`known_hosts=None` with trust-on-first-use pinning. Default `auth_mode` to
password with a generated first-run credential; make `none` an explicit opt-in
that warns at every boot. Stamp `PRAGMA user_version` and refuse to start on a
newer schema than the code knows.

*Value regardless:* highest on this list. Every item is a live defect in
software you run on hardware you own.

### Stage 1 — Installable by a stranger (6–8 weeks)

Helm chart or manifests with `replicas: 1`, `strategy: Recreate`, an RWO PVC,
real probes, a Secret for `SPARK_SECRET_KEY`. A non-Kubernetes path, because the
natural posture is "runs on the Spark you just unboxed". Python lockfile,
digest-pinned base images, drop the `npm ci || npm install` fallback that
silently discards the lockfile. SBOM, image signing, vulnerability scan, a
`NOTICE` file. A support-bundle endpoint. Make `/api/health` mean something.

*Value regardless:* this decides whether anyone other than you ever runs it.
`docs/OPERATIONS.md:184` currently tells people to `kubectl` against a Deployment
that does not exist in the repo.

### Stage 2 — RBAC and audit (8–12 weeks)

Three roles (viewer / operator / admin). An `actor` column on `Job` and an audit
table, with scheduler-initiated work attributed to `system`. Scope the MCP bearer
token — it currently grants all ~85 tools full authority and bypasses the session
gate, so session revocation does not touch it. Per-user identity in password mode.
Back-apply the mandatory-group rule to LDAP.

*Value regardless:* this is what lets a **team** use it — including inside
Telenor, which is the nearest real user base that exists. Every one of the seven
reviewers raised it independently.

### Stage 3 — Pluggable serving backend, and shrink (8–12 weeks)

Extract a `ServingBackend` interface; vLLM becomes implementation #1. Prove it
with a second (SGLang is low-risk; NIM is the strategically interesting one).
Simultaneously **cut** duplicative surface: evals, playground and the LLM-judge
compete with mature tools and generate maintenance forever.

*Value regardless:* the difference between "a vLLM launcher" and "a Spark cluster
control plane", and it hedges against vLLM being displaced. It also converts the
project from a substitute for NVIDIA's stack into a complement — which is the
only version of the NVIDIA conversation that could ever work.

### Stage 4 — Field time (start now, in parallel)

Get it running on hardware you do not own, operated by people who did not write
it. Five to ten other Spark owners. Fix what they hit.

*Value regardless:* the only stage producing something you cannot manufacture —
evidence. It also turns a portfolio piece into a project with users, which is a
materially different negotiating position.

---

## Stop doing

- **Stop building toward solo OEM readiness.** 3–5 person-years, and half of it
  (a11y retrofit, i18n, HA, multi-tenancy) is worthless if the partnership never
  materialises.
- **Stop before starting i18n, VPAT or full WCAG conformance.** Real procurement
  gates, but months of work whose entire value is contingent on an outcome with a
  low prior.
- **Stop considering HA / multi-replica / Postgres.** The in-memory state does
  foreclose HA — but this is a control plane for a 1–4 node appliance. A single
  instance with a fast restart is the *correct* architecture. Enforce it instead
  of engineering around it.
- **Stop adding features to the duplicative surface** — the gateway is a narrower
  LiteLLM, the telemetry engine a narrower DCGM-exporter.
- **Stop adding `SPARK_*` settings.** 75 fields for a 4-node appliance.
  `CONFIGURATION.md` documents them well, which masks the problem rather than
  solving it.
- **Stop building NIM integration speculatively.** Right destination, wrong first
  step. Build the interface; NIM becomes a two-week plugin when it matters.
- **Stop treating the repository as the pitch.** Nobody senior reads 22k lines.

---

## The 30-day plan

1. **Day 1, one hour — the highest-leverage item in this document.** Get written
   clarity from your manager and Telenor legal on **who owns this IP**. 28 of 65
   commits are authored as `jorgen.lindalen@telenor.no`, on a control plane for
   hardware, while employed there. Norwegian employment law and Telenor's own IP
   policy may well assign some or all of it to your employer. This is not legal
   advice — the point is that *every other item on this list is worth less until
   the answer is in writing*, and the answer is much easier to get now, while
   nothing is at stake, than after a vendor expresses interest.
2. **Day 1, thirty minutes** — fix `README.md:202`.
3. **Days 2–3** — make CI real: `pytest`, `ruff`, `needs: [ci]` on release.
4. **Day 4** — single-instance lock; pin Ray.
5. **Days 5–7** — SSH host-key TOFU.
6. **Day 8** — secure by default: password mode with a generated first-run
   credential.
7. **Days 9–10** — `PRAGMA user_version` tripwire; retention for `job_logs`.
8. **Days 11–12** — render the `usePoll` error field; two other frontend honesty
   fixes.
9. **Days 13–15** — `SECURITY.md`; rewrite `ARCHITECTURE.md`, which is stamped
   1.28.0 but is substantially a v1.5 document.
10. **Days 16–22** — write the technical note (below).
11. **Days 23–28** — record a five-minute demo: two physical Sparks from
    registered-but-unconfigured through the setup phases to a model serving
    across the fabric, phase logs streaming.
12. **Days 29–30** — publish. Repo as the footer link, not the headline.

---

## The pitch

**Who, and with what ask.** Not corporate BD, and not *"would you ship my
software"*. Approach NVIDIA DevRel and the DGX Spark product team with:

> *"I brought up a multi-node Spark cluster and found five things your
> documentation doesn't say. Can I write this up for your developer blog?"*

That costs them nothing to say yes to and puts you in the room. "Adopt my 22k-line
control plane" does not — it asks them to take on a maintenance burden, a
bus-factor-of-one dependency, and a product that routes their customers around
NIM.

**The artefact** is a one-page technical note plus a five-minute video, not a
GitHub link. The note writes itself from what you already know:

- GB10's unified memory is invisible to `nvidia-smi` and must come from
  `/proc/meminfo`.
- NCCL, UCX, Gloo and TP each need their socket interface pinned to the QSFP
  port, or collectives silently fall back to the management LAN — you lose most
  of your bandwidth with no error.
- `nvcr.io/nvidia/vllm` and `vllm/vllm-openai` have divergent `ENTRYPOINT`s, so
  the same `docker run` does two different things.
- Some NetworkManager builds reject `ipv6.method disabled`, which breaks fabric
  persistence in a way that looks like a hardware fault.
- At 3+ nodes, verifying the fabric from the head only will miss a bad switch
  port, because NCCL flows worker-to-worker.

Lead with what you *know*. The software is the proof you learned it the hard way.
