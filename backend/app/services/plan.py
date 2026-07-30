"""Derive serve settings from the model and the cluster, and show the work.

A first-time Spark owner opening the instance form meets nine vLLM flags whose
correct values depend on arithmetic nobody should have to do: whether the
weights fit one box, what fraction of unified memory to hand vLLM, and how much
context is left for the KV cache once the weights are in. Getting one wrong is
an out-of-memory ten minutes into a weight load, which is the single most common
way this hardware wastes an afternoon.

So the planner does that arithmetic. Three rules shape it:

**It fills the form; it does not replace it.** Every value it derives lands in
the same editable field an operator would have typed into, and the full advanced
surface is untouched. Nothing here removes a capability — it removes the
*requirement to decide*.

**It shows its reasoning.** Every derived value carries a sentence naming the
numbers it came from. An operator who disagrees can see exactly which input to
argue with, and an operator who is learning gets the mental model for free. A
recommendation you cannot audit is worse than a default.

**It is conservative and pure.** No I/O: it takes a snapshot of facts and
returns a plan, which makes every branch unit-testable. Where a fact is missing
it under-promises and says which fact was missing, because the failure mode of
optimism here is a ten-minute load that dies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models import (
    INST_ACTIVE_STATES,
    TOPO_CLUSTER,
    TOPO_DISTRIBUTED,
    TOPO_SINGLE,
)

__all__ = [
    "ModelFacts",
    "NodeFacts",
    "ClusterFacts",
    "PlanResult",
    "plan_instance",
    "OS_RESERVE_GIB",
    "WEIGHT_OVERHEAD",
]

# Unified memory the OS, the driver, CUDA graphs and the container runtime need
# on a Spark regardless of what vLLM is doing. Subtracted before any fraction is
# computed — `--gpu-memory-utilization` is a fraction of *total* memory, so
# handing vLLM 0.95 on a 119 GiB box leaves under 6 GiB for everything else and
# the load dies with an allocator error that names none of this.
OS_RESERVE_GIB = 10.0

# Bounds on `--gpu-memory-utilization`. The ceiling leaves headroom even on an
# otherwise empty box; the floor exists so a plan is always a postable body
# (vLLM rejects 0) and matches the validation on InstanceIn.
MAX_MEMORY_FRACTION = 0.90
MIN_MEMORY_FRACTION = 0.10

# Weights occupy more than the checkpoint's size on disk: activation buffers,
# the CUDA graph pool and fragmentation all land in the same budget. 15% is
# what the built-in Laguna profile's known-good 0.72 fraction implies for its
# 117 GiB of FP8 weights across two nodes, and it has held for the others.
WEIGHT_OVERHEAD = 1.15

# Never plan a context shorter than this: below it a model is not useful for the
# chat and coding work these boxes get bought for, so the honest answer is a
# warning that it does not fit, not a 2k-token instance that technically starts.
MIN_USEFUL_CONTEXT = 4096

# Context lengths worth landing on, descending. Powers of two keep the KV block
# arithmetic tidy and make the number recognisable to anyone reading it.
_CONTEXT_LADDER = (262144, 131072, 65536, 32768, 16384, 8192, 4096)

# Concurrency the plan assumes. Small on purpose: a Spark pair is usually
# serving a team, not a product, and every concurrent sequence multiplies the
# KV cache. Raising it is one field in the form.
DEFAULT_MAX_NUM_SEQS = 8

_GIB = 1024 ** 3


@dataclass
class ModelFacts:
    """What we know about the model being served."""

    repo_id: str
    name: str
    size_bytes: int | None = None
    tool_parser: str | None = None
    context_len: int | None = None
    num_layers: int | None = None
    num_kv_heads: int | None = None
    head_dim: int | None = None
    torch_dtype: str | None = None
    # Nodes the weights are actually present on. A plan that picks a topology
    # spanning a node without the files produces an instance that cannot start.
    present_node_ids: tuple[int, ...] = ()

    @property
    def weights_gib(self) -> float | None:
        if not self.size_bytes or self.size_bytes <= 0:
            return None
        return self.size_bytes / _GIB

    def kv_bytes_per_token(self) -> int | None:
        if not all(
            isinstance(v, int) and v > 0
            for v in (self.num_layers, self.num_kv_heads, self.head_dim)
        ):
            return None
        elem = 1 if (self.torch_dtype or "").lower().startswith(("float8", "fp8", "int8")) else 2
        return 2 * self.num_layers * self.num_kv_heads * self.head_dim * elem  # type: ignore[operator]


@dataclass
class NodeFacts:
    node_id: int
    name: str
    role: str
    reachable: bool = True
    has_qsfp: bool = False
    # GiB already promised to instances that are running or starting on this
    # node. Planning against total memory while something else holds half of it
    # is how you OOM a model that "fit" yesterday.
    committed_gib: float = 0.0


@dataclass
class ClusterFacts:
    nodes: tuple[NodeFacts, ...]
    node_memory_gib: float = 119.0
    # Ray is only worth choosing when it is already up; the native distributed
    # path needs no daemon and is what the built-in profiles use.
    ray_up: bool = False

    @property
    def usable(self) -> tuple[NodeFacts, ...]:
        return tuple(n for n in self.nodes if n.reachable)


@dataclass
class Reason:
    """One derived field and the sentence explaining it."""

    field: str
    label: str
    value: object
    why: str


@dataclass
class PlanResult:
    settings: dict = field(default_factory=dict)
    reasons: list[Reason] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # False when the plan cannot honestly recommend starting this model here.
    # The form still opens — the operator may know something we do not — but the
    # one-click path refuses to pretend.
    feasible: bool = True
    summary: str = ""


def _min_kv_gib(model: ModelFacts) -> float:
    """Memory a node must have spare, beyond the weights, to be worth using.

    Exact when the model's geometry is known: the cache for ``MIN_USEFUL_CONTEXT``
    tokens across the default concurrency. Otherwise a flat 8 GiB — enough that
    "it fits on one node" is not a claim about the last gigabyte.
    """
    per_token = model.kv_bytes_per_token()
    if not per_token:
        return 8.0
    return (per_token * MIN_USEFUL_CONTEXT * DEFAULT_MAX_NUM_SEQS) / _GIB / 0.8


def _fmt_gib(x: float) -> str:
    return f"{x:.0f} GiB" if x >= 10 else f"{x:.1f} GiB"


def _fmt_ctx(n: int) -> str:
    if n >= 1024 and n % 1024 == 0:
        return f"{n // 1024}k"
    return str(n)


def plan_instance(
    model: ModelFacts,
    cluster: ClusterFacts,
    *,
    force_topology: str | None = None,
    force_node_id: int | None = None,
    max_num_seqs: int | None = None,
) -> PlanResult:
    """Derive a complete, startable instance configuration.

    Pure: same inputs, same plan. Callers supply the facts; see
    ``routers/instances.py::plan_instance`` for where they come from.

    The ``force_*`` arguments let an operator pin a decision and have the rest
    of the arithmetic re-derive around it — "I want this on node 2, now tell me
    what context fits there". A pinned choice is honoured even when the planner
    would not have made it, but the consequences still get reported: overriding
    the recommendation is a decision, not a way to silence the warnings.
    """
    out = PlanResult()
    nodes = cluster.usable
    per_node = cluster.node_memory_gib

    if not nodes:
        out.feasible = False
        out.warnings.append(
            "No node is reachable right now, so there is nothing to plan against. "
            "Check the Nodes page first."
        )
        out.summary = "No reachable nodes."
        return out

    # --- free memory per node -------------------------------------------
    # Headroom is what is left after the OS reserve and whatever running
    # instances have already claimed.
    free = {n.node_id: max(0.0, per_node - OS_RESERVE_GIB - n.committed_gib) for n in nodes}
    committed_total = sum(n.committed_gib for n in nodes)

    weights = model.weights_gib
    need = weights * WEIGHT_OVERHEAD if weights else None

    # --- topology --------------------------------------------------------
    # Prefer the smallest footprint that fits: one node leaves the other Spark
    # free for someone else, and single-node serving has no interconnect in the
    # critical path at all.
    present = set(model.present_node_ids)
    candidates = [n for n in nodes if not present or n.node_id in present]
    if present and not candidates:
        out.warnings.append(
            "The weights are not present on any reachable node yet — download the "
            "model first, or this instance will fail to start."
        )
        candidates = list(nodes)

    # A node "fits" the model only if it holds the weights AND enough cache to
    # be worth serving. Testing the weights alone would pick single-node for a
    # model that then starts with a 900-token context — technically running,
    # practically useless, and the operator would have no idea why.
    min_kv = _min_kv_gib(model)

    single_target: NodeFacts | None = None
    if need is not None:
        roomy = [n for n in candidates if free[n.node_id] >= need + min_kv]
        # Most free memory first, so co-located models spread rather than stack.
        roomy.sort(key=lambda n: free[n.node_id], reverse=True)
        single_target = roomy[0] if roomy else None
    elif len(candidates) == 1:
        # Unknown size and only one node: there is no other choice to make.
        single_target = candidates[0]

    multi_nodes = [n for n in nodes if not present or n.node_id in present]
    multi_ok = len(multi_nodes) >= 2 and all(n.has_qsfp for n in multi_nodes)

    # A pinned choice wins over the derived one. The memory arithmetic below
    # then runs against what the operator actually asked for, so a pin that
    # does not fit produces a warning rather than a silently different plan.
    pinned = force_topology is not None
    if force_topology == TOPO_SINGLE:
        by_id = {n.node_id: n for n in nodes}
        single_target = by_id.get(force_node_id) or max(candidates, key=lambda n: free[n.node_id])
        multi_ok = False
    elif force_topology in (TOPO_CLUSTER, TOPO_DISTRIBUTED):
        single_target = None
        multi_ok = len(multi_nodes) >= 2

    if single_target is not None:
        topology = TOPO_SINGLE
        span = [single_target]
        node_id = single_target.node_id
        if need is not None:
            why = (
                f"{_fmt_gib(weights)} of weights (≈{_fmt_gib(need)} with runtime overhead) "
                f"fit in the {_fmt_gib(free[single_target.node_id])} free on {single_target.name}, "
                f"so one node is enough and the other Spark stays free."
            )
        else:
            why = (
                f"Only {single_target.name} is available, so the model runs there with TP=1. "
                "The download size is unknown, so this is not a memory-fit judgement."
            )
    elif multi_ok:
        # Native distributed unless Ray was explicitly asked for: it needs no
        # daemon, so there is one less thing that can be down at launch.
        topology = TOPO_CLUSTER if force_topology == TOPO_CLUSTER else TOPO_DISTRIBUTED
        span = list(multi_nodes)
        node_id = None
        total_free = sum(free[n.node_id] for n in multi_nodes)
        if pinned and not all(n.has_qsfp for n in multi_nodes):
            out.warnings.append(
                "Multi-node was requested but not every node has a QSFP address — the "
                "rendezvous will fail until that is set on the Nodes page."
            )
        if need is not None:
            why = (
                f"{_fmt_gib(weights)} of weights (≈{_fmt_gib(need)} with runtime overhead) "
                f"do not fit the {_fmt_gib(max(free[n.node_id] for n in multi_nodes))} free on any single "
                f"node, so they shard across {len(multi_nodes)} nodes (TP={len(multi_nodes)}, "
                f"{_fmt_gib(total_free)} free in total) over the QSFP link. Native "
                "torch.distributed — no Ray daemon needed."
            )
        else:
            why = (
                f"The download size is unknown, so the plan spans all {len(multi_nodes)} nodes "
                f"(TP={len(multi_nodes)}) — the choice that works for a large model and merely "
                "wastes capacity on a small one. Set it to single-node if you know it fits."
            )
    else:
        # Multi-node is unavailable: either one node, or QSFP is not configured.
        target = max(candidates, key=lambda n: free[n.node_id])
        topology = TOPO_SINGLE
        span = [target]
        node_id = target.node_id
        if force_topology in (TOPO_CLUSTER, TOPO_DISTRIBUTED):
            out.warnings.append(
                f"Multi-node was requested but only {len(nodes)} node is reachable, so this "
                "plan is single-node. Register a second Spark to shard across both."
            )
        if len(nodes) >= 2 and not all(n.has_qsfp for n in multi_nodes):
            out.warnings.append(
                "More than one node is registered but not all have a QSFP address, so "
                "multi-node serving is unavailable. Finish the QSFP step on the Setup "
                "page to shard large models across both Sparks."
            )
        if need is not None and need > free[target.node_id]:
            out.feasible = False
            out.warnings.append(
                f"{_fmt_gib(weights)} of weights need about {_fmt_gib(need)}, but only "
                f"{_fmt_gib(free[target.node_id])} is free on {target.name}"
                + (
                    f" ({_fmt_gib(committed_total)} is already committed to running instances)."
                    if committed_total > 0
                    else "."
                )
                + " Stop another instance, add a second node, or pick a smaller/quantized model."
            )
        why = (
            f"Only {target.name} can take this model, so it runs there with TP=1."
        )

    tp = len(span)
    out.settings["topology"] = topology
    out.settings["tensor_parallel_size"] = tp
    out.settings["node_id"] = node_id
    out.reasons.append(
        Reason(
            "topology",
            "Topology",
            f"{topology} (TP={tp})" if topology != TOPO_SINGLE else f"single on {span[0].name}",
            why,
        )
    )

    # --- gpu memory utilization -----------------------------------------
    # The fraction is of TOTAL node memory, and is applied per node, so a
    # TP=N instance claims this fraction on every node it spans.
    span_free = min(free[n.node_id] for n in span)
    honest = span_free / per_node
    # The ceiling is a real judgement; the floor is only there because the plan
    # has to remain a postable body — vLLM rejects a fraction of zero, and an
    # unusable number in an editable field is worse than a small one next to a
    # warning. Below the floor the plan is marked infeasible rather than
    # quietly rounded up into memory another instance is holding.
    frac = round(min(MAX_MEMORY_FRACTION, max(MIN_MEMORY_FRACTION, honest)), 2)
    # "per node" is only true when the instance actually spans more than one.
    # Saying it for a TP=1 plan while the *other* node is full is a sentence
    # that reads as a claim about the cluster and is wrong about it.
    where_free = "per node" if tp > 1 else f"on {span[0].name}"
    if honest < MIN_MEMORY_FRACTION:
        out.feasible = False
        out.warnings.append(
            f"Only {_fmt_gib(max(0.0, span_free))} is free {where_free} once the OS reserve and "
            "running instances are accounted for — not enough to serve anything. Stop an "
            "instance first."
        )
    out.settings["gpu_memory_utilization"] = frac
    # Mention prior commitments only when they actually bear on this span:
    # a busy neighbour is irrelevant to a plan that does not touch it.
    span_committed = sum(n.committed_gib for n in span)
    if span_committed > 0:
        frac_why = (
            f"{_fmt_gib(span_free)} is free {where_free} — {_fmt_gib(per_node)} total, minus "
            f"{_fmt_gib(OS_RESERVE_GIB)} for the OS and driver, minus the "
            f"{_fmt_gib(span_committed)} instances already running have claimed. That is "
            f"{frac:.2f} of the box."
        )
    else:
        frac_why = (
            f"{_fmt_gib(per_node)} {where_free} minus {_fmt_gib(OS_RESERVE_GIB)} kept back for "
            f"the OS, driver and CUDA graphs leaves {_fmt_gib(span_free)}, or {frac:.2f} of the "
            "box. vLLM treats this as a hard ceiling for weights plus KV cache."
        )
    out.reasons.append(Reason("gpu_memory_utilization", "GPU memory fraction", frac, frac_why))

    # --- context length --------------------------------------------------
    kv_per_token = model.kv_bytes_per_token()
    trained = model.context_len
    budget_gib = frac * per_node * tp - (need or 0.0)

    if kv_per_token and budget_gib > 0:
        seqs = max_num_seqs or DEFAULT_MAX_NUM_SEQS
        # Leave a fifth of the remaining budget unspoken-for: vLLM's block
        # allocator, the scheduler's watermark and activation peaks all draw on
        # it, and a KV cache sized to the last byte is a load-time failure.
        usable = budget_gib * 0.8 * _GIB
        max_tokens = int(usable // kv_per_token)
        per_seq = max_tokens // seqs if seqs else max_tokens

        chosen = None
        for rung in _CONTEXT_LADDER:
            if rung <= per_seq and (trained is None or rung <= trained):
                chosen = rung
                break
        if chosen is None and trained and trained <= per_seq:
            chosen = trained  # a model whose whole window is smaller than a rung

        if chosen is None:
            out.warnings.append(
                f"After {_fmt_gib(need or 0.0)} of weights there is only "
                f"{_fmt_gib(budget_gib)} left for the KV cache — about {per_seq:,} tokens per "
                f"request at {seqs} concurrent requests, below the {_fmt_ctx(MIN_USEFUL_CONTEXT)} "
                "floor. Lower the concurrency, free a node, or use a quantized checkpoint."
            )
            out.feasible = False
            chosen = MIN_USEFUL_CONTEXT

        out.settings["max_model_len"] = chosen
        out.settings["max_num_seqs"] = seqs
        ctx_why = (
            f"This model spends {kv_per_token / 1024:.0f} KiB of KV cache per token "
            f"({model.num_layers} layers × {model.num_kv_heads} KV heads × {model.head_dim} dims). "
            f"{_fmt_gib(budget_gib)} is left after the weights, so at {seqs} concurrent requests "
            f"{_fmt_ctx(chosen)} tokens each fits with room to spare"
            + (
                f" — the model was trained for {_fmt_ctx(trained)}."
                if trained and chosen < trained
                else "."
            )
        )
        out.reasons.append(Reason("max_model_len", "Context length", chosen, ctx_why))
        out.reasons.append(
            Reason(
                "max_num_seqs",
                "Concurrent requests",
                seqs,
                f"{seqs} in flight at once. Every concurrent request needs its own "
                f"{_fmt_ctx(chosen)}-token cache, so this and the context length trade against "
                "each other — raise it if you have more users than context.",
            )
        )
    else:
        # Without the geometry we cannot size the cache. Say so and let vLLM
        # use the model's own default rather than inventing a number.
        out.settings["max_model_len"] = None
        out.settings["max_num_seqs"] = max_num_seqs or DEFAULT_MAX_NUM_SEQS
        missing = "the model's config.json could not be read"
        if budget_gib <= 0:
            missing = "the weights already fill the memory budget"
        out.reasons.append(
            Reason(
                "max_model_len",
                "Context length",
                "model default",
                f"Left to vLLM because {missing}, so the KV cache cannot be sized here. "
                "If the load fails with an out-of-memory error, set a context length "
                "explicitly and halve it until it starts.",
            )
        )
        out.warnings.append(
            "Context length is unverified for this model — "
            + missing
            + ". The instance may need a lower --max-model-len to load."
        )

    # --- the rest --------------------------------------------------------
    out.settings["enable_tool_choice"] = True
    if model.tool_parser:
        out.settings["tool_parser"] = None  # auto-mapped at build time
        out.reasons.append(
            Reason(
                "tool_parser",
                "Tool calling",
                f"on ({model.tool_parser})",
                f"OpenAI tool calls need a parser matched to the model's output format; "
                f"'{model.tool_parser}' is the one for this family.",
            )
        )
    else:
        out.reasons.append(
            Reason(
                "enable_tool_choice",
                "Tool calling",
                "on (no parser matched)",
                "No tool-call parser is known for this model family, so tool calls may not "
                "parse. Everything else works; set a parser by hand if you need them.",
            )
        )

    out.settings["autostart"] = True
    out.settings["port"] = None       # allocator picks a free one
    out.settings["master_port"] = None
    out.settings["tls_enabled"] = False

    # --- summary ---------------------------------------------------------
    where = span[0].name if topology == TOPO_SINGLE else f"{tp} nodes"
    whose = "each node's" if tp > 1 else f"{span[0].name}'s"
    ctx = out.settings.get("max_model_len")
    ctx_s = f"{_fmt_ctx(ctx)} context" if ctx else "the model's default context"
    out.summary = (
        f"Serve {model.name} on {where} with {ctx_s}, using {frac:.0%} of {whose} memory."
        if out.feasible
        else f"{model.name} does not fit this cluster as configured."
    )
    return out


def committed_gib_by_node(instances, nodes, node_memory_gib: float) -> dict[int, float]:
    """Memory already promised, per node id.

    Mirrors ``status_svc._memory_warnings``: a multi-node instance claims its
    fraction on *every* node it spans, not a share of one. Counts starting as
    well as running — a model part-way through a weight load owns that memory
    just as firmly, and it is exactly the window in which someone tries to
    launch a second one.
    """
    used = {n.id: 0.0 for n in nodes}
    for inst in instances:
        if inst.status not in INST_ACTIVE_STATES:
            continue
        share = (inst.gpu_memory_utilization or 0.0) * node_memory_gib
        if inst.topology in (TOPO_CLUSTER, TOPO_DISTRIBUTED):
            for n in nodes:
                used[n.id] += share
        elif inst.node_id in used:
            used[inst.node_id] += share
    return used


def suggest_name(model_name: str, taken: set[str]) -> str:
    """A legal, unused instance name derived from the model.

    One less field to fill in, and the derived name matches what the operator
    already calls the model.
    """
    base = re.sub(r"[^a-z0-9-]+", "-", model_name.lower()).strip("-")[:40].strip("-")
    base = base or "instance"
    if base not in taken:
        return base
    i = 2
    while f"{base}-{i}" in taken:
        i += 1
    return f"{base}-{i}"
