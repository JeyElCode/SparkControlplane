"""The serve planner's arithmetic.

The planner exists so an operator does not have to know any of this, which means
nobody will be checking its output by hand. These tests are the only thing
standing between a wrong constant and an instance that OOMs ten minutes into a
weight load, so they assert the numbers, not just the shape.
"""

from __future__ import annotations

import pytest

from app.services import plan as P
from app.services.hfmeta import ModelShape, shape_from_config

GIB = 1024 ** 3


def node(nid: int, name: str, *, committed: float = 0.0, qsfp: bool = True, up: bool = True):
    return P.NodeFacts(
        node_id=nid, name=name, role="head" if nid == 1 else "worker",
        reachable=up, has_qsfp=qsfp, committed_gib=committed,
    )


def cluster(*nodes, mem: float = 119.0):
    return P.ClusterFacts(nodes=tuple(nodes), node_memory_gib=mem)


# Llama-3.1-8B geometry: 32 layers, 8 KV heads (GQA), 128 head dim, bf16.
SMALL = dict(num_layers=32, num_kv_heads=8, head_dim=128, torch_dtype="bfloat16",
             context_len=131072)
# Llama-3.3-70B: 80 layers, 8 KV heads, 128 dim.
LARGE = dict(num_layers=80, num_kv_heads=8, head_dim=128, torch_dtype="bfloat16",
             context_len=131072)


def model(name="m", *, gib: float | None = None, **kw):
    facts = P.ModelFacts(repo_id=f"org/{name}", name=name, tool_parser="llama3_json", **kw)
    if gib is not None:
        facts.size_bytes = int(gib * GIB)
    return facts


# --- topology ------------------------------------------------------------

def test_small_model_stays_on_one_node_leaving_the_other_free():
    r = P.plan_instance(model(gib=16, **SMALL), cluster(node(1, "a"), node(2, "b")))
    assert r.settings["topology"] == "single"
    assert r.settings["tensor_parallel_size"] == 1
    assert r.settings["node_id"] in (1, 2)
    assert r.feasible
    why = next(x.why for x in r.reasons if x.field == "topology")
    assert "16 GiB" in why and "fit" in why


def test_model_too_big_for_one_node_shards_across_both():
    r = P.plan_instance(model(gib=140, **LARGE), cluster(node(1, "a"), node(2, "b")))
    assert r.settings["topology"] == "distributed"
    assert r.settings["tensor_parallel_size"] == 2
    assert r.settings["node_id"] is None
    assert r.feasible


def test_a_model_that_fits_the_weights_but_leaves_no_cache_still_shards():
    """The trap this guards: 100 GiB of weights "fit" in 109 GiB of free memory,
    but the 9 GiB left would serve a few hundred tokens. Testing the weights
    alone would pick single-node and produce a technically-running, useless
    instance."""
    r = P.plan_instance(model(gib=92, **LARGE), cluster(node(1, "a"), node(2, "b")))
    assert r.settings["topology"] == "distributed", (
        "weights alone fit one node; the KV cache does not"
    )


def test_single_node_cluster_uses_that_node():
    r = P.plan_instance(model(gib=16, **SMALL), cluster(node(1, "solo", qsfp=False)))
    assert r.settings["topology"] == "single"
    assert r.settings["node_id"] == 1
    assert r.feasible


def test_two_nodes_without_qsfp_cannot_shard_and_says_so():
    r = P.plan_instance(
        model(gib=140, **LARGE),
        cluster(node(1, "a", qsfp=False), node(2, "b", qsfp=False)),
    )
    assert r.settings["topology"] == "single"
    assert not r.feasible
    assert any("QSFP" in w for w in r.warnings)


def test_unreachable_nodes_are_not_planned_against():
    r = P.plan_instance(
        model(gib=16, **SMALL), cluster(node(1, "a", up=False), node(2, "b"))
    )
    assert r.settings["node_id"] == 2


def test_no_reachable_nodes_is_infeasible_not_a_crash():
    r = P.plan_instance(model(gib=16, **SMALL), cluster(node(1, "a", up=False)))
    assert not r.feasible
    assert r.settings == {}
    assert "reachable" in r.warnings[0]


def test_plan_prefers_a_node_that_already_has_the_weights():
    facts = model(gib=16, **SMALL)
    facts.present_node_ids = (2,)
    r = P.plan_instance(facts, cluster(node(1, "a"), node(2, "b")))
    assert r.settings["node_id"] == 2


# --- memory fraction -----------------------------------------------------

def test_fraction_reserves_memory_for_the_os():
    r = P.plan_instance(model(gib=16, **SMALL), cluster(node(1, "a")))
    # (119 - 10) / 119 = 0.916 -> capped at the 0.90 ceiling
    assert r.settings["gpu_memory_utilization"] == 0.90


def test_fraction_shrinks_when_another_instance_holds_memory():
    r = P.plan_instance(
        model(gib=16, **SMALL), cluster(node(1, "a", committed=60.0))
    )
    # (119 - 10 - 60) / 119 = 0.41
    assert r.settings["gpu_memory_utilization"] == pytest.approx(0.41, abs=0.01)
    why = next(x.why for x in r.reasons if x.field == "gpu_memory_utilization")
    assert "already" in why


def test_fraction_is_never_padded_up_past_what_is_free():
    """No floor on the fraction. Clamping a nearly-full node up to a
    'reasonable' 0.5 would hand vLLM memory another model is already using."""
    r = P.plan_instance(
        model(gib=8, **SMALL),
        cluster(node(1, "a", committed=90.0), node(2, "b", committed=90.0)),
    )
    assert r.settings["gpu_memory_utilization"] < 0.2


# --- context length ------------------------------------------------------

def test_context_is_derived_from_the_kv_cache_formula():
    r = P.plan_instance(model(gib=16, **SMALL), cluster(node(1, "a")))
    ctx = r.settings["max_model_len"]
    seqs = r.settings["max_num_seqs"]
    per_token = 2 * 32 * 8 * 128 * 2  # 128 KiB
    budget = 0.90 * 119 * 1 - 16 * P.WEIGHT_OVERHEAD
    assert ctx * seqs * per_token <= budget * 0.8 * GIB, "planned cache exceeds the budget"
    assert ctx <= SMALL["context_len"], "never plan past the trained window"


def test_context_never_exceeds_the_trained_window():
    small_window = dict(SMALL, context_len=8192)
    r = P.plan_instance(model(gib=16, **small_window), cluster(node(1, "a")))
    assert r.settings["max_model_len"] <= 8192


def test_a_model_leaving_too_little_cache_is_infeasible_not_quietly_tiny():
    r = P.plan_instance(
        model(gib=100, **LARGE), cluster(node(1, "a", committed=0), mem=119)
    )
    # 100 GiB weights on one 119 GiB node: shards across nothing, no room left.
    assert not r.feasible
    assert r.warnings


def test_unknown_geometry_leaves_context_to_vllm_and_warns():
    r = P.plan_instance(model(gib=16), cluster(node(1, "a")))
    assert r.settings["max_model_len"] is None
    assert any("unverified" in w for w in r.warnings)
    why = next(x.why for x in r.reasons if x.field == "max_model_len")
    assert "config.json" in why


def test_unknown_size_still_produces_a_startable_plan():
    r = P.plan_instance(model(**SMALL), cluster(node(1, "a"), node(2, "b")))
    assert r.settings["topology"] in ("single", "distributed")
    assert r.settings["gpu_memory_utilization"] > 0
    assert r.feasible


# --- reasons are the product --------------------------------------------

def test_every_derived_setting_carries_a_reason():
    r = P.plan_instance(model(gib=16, **SMALL), cluster(node(1, "a"), node(2, "b")))
    explained = {x.field for x in r.reasons}
    assert {"topology", "gpu_memory_utilization", "max_model_len"} <= explained
    for reason in r.reasons:
        assert len(reason.why) > 40, f"{reason.field} has no real explanation"


def test_reasons_cite_the_actual_numbers():
    r = P.plan_instance(model(gib=16, **SMALL), cluster(node(1, "a")))
    ctx_why = next(x.why for x in r.reasons if x.field == "max_model_len")
    assert "32 layers" in ctx_why and "8 KV heads" in ctx_why and "128 dims" in ctx_why


def test_summary_is_a_sentence_a_person_can_act_on():
    r = P.plan_instance(model("qwen", gib=16, **SMALL), cluster(node(1, "a")))
    assert "qwen" in r.summary and r.summary.endswith(".")


# --- name suggestion -----------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Qwen3-30B-A3B-FP8", "qwen3-30b-a3b-fp8"),
        ("Llama_3.1_8B", "llama-3-1-8b"),
        ("---", "instance"),
    ],
)
def test_suggest_name_is_legal_and_derived(raw, expected):
    assert P.suggest_name(raw, set()) == expected


def test_suggest_name_avoids_collisions():
    assert P.suggest_name("main", {"main"}) == "main-2"
    assert P.suggest_name("main", {"main", "main-2"}) == "main-3"


# --- committed memory mirrors the status page ---------------------------

class FakeInst:
    def __init__(self, status, topology, gmu, node_id=None):
        self.status, self.topology = status, topology
        self.gpu_memory_utilization, self.node_id = gmu, node_id


class FakeNode:
    def __init__(self, nid):
        self.id = nid


def test_multi_node_instance_commits_memory_on_every_node():
    used = P.committed_gib_by_node(
        [FakeInst("running", "distributed", 0.5)], [FakeNode(1), FakeNode(2)], 100.0
    )
    assert used == {1: 50.0, 2: 50.0}, "a TP=2 instance holds its fraction on BOTH nodes"


def test_starting_instances_count_as_committed():
    """The window in which someone launches a second model is exactly the ten
    minutes the first one spends loading weights."""
    used = P.committed_gib_by_node(
        [FakeInst("starting", "single", 0.5, node_id=1)], [FakeNode(1)], 100.0
    )
    assert used[1] == 50.0


def test_stopped_instances_hold_nothing():
    used = P.committed_gib_by_node(
        [FakeInst("stopped", "single", 0.9, node_id=1)], [FakeNode(1)], 100.0
    )
    assert used[1] == 0.0


# --- config.json parsing -------------------------------------------------

def test_shape_from_llama_config():
    s = shape_from_config({
        "num_hidden_layers": 32, "num_attention_heads": 32,
        "num_key_value_heads": 8, "hidden_size": 4096,
        "max_position_embeddings": 131072, "torch_dtype": "bfloat16",
    })
    assert (s.num_layers, s.num_kv_heads, s.head_dim) == (32, 8, 128)
    assert s.context_len == 131072
    assert s.kv_bytes_per_token() == 2 * 32 * 8 * 128 * 2


def test_missing_kv_heads_means_multi_head_not_unknown():
    """Treating an absent num_key_value_heads as 'unknown' would be safe;
    treating it as 1 would under-count the cache by the GQA ratio and plan a
    context that OOMs. It means every query head has its own KV head."""
    s = shape_from_config({
        "num_hidden_layers": 24, "num_attention_heads": 16, "hidden_size": 2048,
    })
    assert s.num_kv_heads == 16
    assert s.head_dim == 128


def test_shape_reads_nested_text_config_of_multimodal_wrappers():
    s = shape_from_config({
        "model_type": "llava",
        "text_config": {
            "num_hidden_layers": 40, "num_attention_heads": 40,
            "num_key_value_heads": 8, "hidden_size": 5120,
            "max_position_embeddings": 32768,
        },
    })
    assert s.num_layers == 40 and s.num_kv_heads == 8 and s.context_len == 32768


def test_fp8_checkpoint_halves_the_cache_element():
    s = ModelShape(num_layers=10, num_kv_heads=4, head_dim=64, torch_dtype="float8_e4m3fn")
    assert s.kv_bytes_per_token() == 2 * 10 * 4 * 64 * 1


def test_garbage_config_yields_an_empty_shape_not_an_exception():
    for junk in ({}, {"num_hidden_layers": "abc"}, {"num_hidden_layers": True}, []):
        s = shape_from_config(junk)
        assert not s.complete
        assert s.kv_bytes_per_token() is None


# --- pinned choices ------------------------------------------------------

def test_pinning_a_node_overrides_the_recommendation():
    r = P.plan_instance(
        model(gib=16, **SMALL), cluster(node(1, "a"), node(2, "b")),
        force_topology="single", force_node_id=2,
    )
    assert r.settings["node_id"] == 2 and r.settings["topology"] == "single"


def test_pinning_multi_node_on_a_small_model_is_honoured():
    r = P.plan_instance(
        model(gib=16, **SMALL), cluster(node(1, "a"), node(2, "b")),
        force_topology="distributed",
    )
    assert r.settings["topology"] == "distributed"
    assert r.settings["tensor_parallel_size"] == 2


def test_pinning_ray_gives_cluster_topology():
    r = P.plan_instance(
        model(gib=140, **LARGE), cluster(node(1, "a"), node(2, "b")),
        force_topology="cluster",
    )
    assert r.settings["topology"] == "cluster"


def test_an_impossible_pin_warns_instead_of_silently_replanning():
    r = P.plan_instance(
        model(gib=16, **SMALL), cluster(node(1, "solo")), force_topology="distributed",
    )
    assert r.settings["topology"] == "single"
    assert any("only 1 node is reachable" in w for w in r.warnings)


def test_raising_concurrency_shortens_the_planned_context():
    base = P.plan_instance(model(gib=16, **SMALL), cluster(node(1, "a")))
    busy = P.plan_instance(
        model(gib=16, **SMALL), cluster(node(1, "a")), max_num_seqs=64
    )
    assert busy.settings["max_num_seqs"] == 64
    assert busy.settings["max_model_len"] < base.settings["max_model_len"]


# --- the explanation must describe THIS plan ----------------------------

def test_single_node_reasoning_never_claims_per_node():
    """With one node busy and the other free, "109 GiB is free per node" reads
    as a claim about the cluster and is wrong about it. A TP=1 plan must name
    the node it actually uses."""
    r = P.plan_instance(
        model(gib=33, **SMALL),
        cluster(node(1, "spark-01", committed=100.0), node(2, "spark-02")),
    )
    assert r.settings["node_id"] == 2
    why = next(x.why for x in r.reasons if x.field == "gpu_memory_utilization")
    assert "per node" not in why
    assert "spark-02" in why
    assert "spark-02's memory" in r.summary


def test_a_busy_neighbour_is_not_cited_in_a_plan_that_avoids_it():
    """Mentioning memory 'already claimed' when this plan's node has none is
    an explanation of a different plan."""
    r = P.plan_instance(
        model(gib=16, **SMALL),
        cluster(node(1, "spark-01", committed=100.0), node(2, "spark-02")),
    )
    why = next(x.why for x in r.reasons if x.field == "gpu_memory_utilization")
    assert "claimed" not in why


def test_multi_node_reasoning_does_say_per_node():
    r = P.plan_instance(
        model(gib=140, **LARGE),
        cluster(node(1, "a", committed=20.0), node(2, "b", committed=20.0)),
    )
    why = next(x.why for x in r.reasons if x.field == "gpu_memory_utilization")
    assert "per node" in why
    assert "each node's memory" in r.summary


def test_a_full_cluster_is_infeasible_and_still_postable():
    r = P.plan_instance(
        model(gib=16, **SMALL), cluster(node(1, "a", committed=108.0))
    )
    assert not r.feasible
    # Still a legal --gpu-memory-utilization, so the operator can override.
    assert r.settings["gpu_memory_utilization"] >= P.MIN_MEMORY_FRACTION
    assert any("not enough to serve anything" in w for w in r.warnings)
