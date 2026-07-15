"""Unit tests for the vLLM serve-command builder + renderers.

The headline case reproduces a real hand-run native-multinode launch (generic,
lab-agnostic values): served-model-name aliases, kv-cache-dtype, block-size,
tokenizer-mode, reasoning-parser, a single-token compilation-config, and the
distributed rendezvous flags (--nnodes/--node-rank/--master-addr/--master-port,
with --headless on workers).
"""

from __future__ import annotations

import shlex

from app.services import templates


def _tokens(cmd: str) -> list[str]:
    return shlex.split(cmd)


def test_build_distributed_head_reproduces_full_flag_set():
    cmd = templates.build_vllm_serve_cmd(
        model_container_path="/models/my-model",
        served_model_names="my-model my-model-alias org/my-model",
        port=8000,
        tensor_parallel_size=2,
        gpu_memory_utilization=0.9,
        max_num_batched_tokens=8192,
        kv_cache_dtype="fp8",
        block_size=256,
        tokenizer_mode="custom_mode",
        reasoning_parser="custom_reasoner",
        trust_remote_code=True,
        enable_tool_choice=False,
        compilation_config='{"level": 3, "cudagraph_capture_sizes": [1, 2, 4]}',
        distributed={
            "nnodes": 2, "node_rank": 0, "master_addr": "10.0.0.1",
            "master_port": 29501, "headless": False,
        },
    )
    toks = _tokens(cmd)

    # Positional model + core serving flags.
    assert toks[0:2] == ["vllm", "serve"]
    assert "/models/my-model" in toks
    assert "--tensor-parallel-size" in toks and toks[toks.index("--tensor-parallel-size") + 1] == "2"

    # Native distributed must NOT force a --distributed-executor-backend.
    assert "--distributed-executor-backend" not in toks

    # All three served-model-name aliases follow the single flag, in order.
    i = toks.index("--served-model-name")
    assert toks[i + 1:i + 4] == ["my-model", "my-model-alias", "org/my-model"]

    # First-class settings each emit their flag + value.
    assert toks[toks.index("--kv-cache-dtype") + 1] == "fp8"
    assert toks[toks.index("--block-size") + 1] == "256"
    assert toks[toks.index("--max-num-batched-tokens") + 1] == "8192"
    assert toks[toks.index("--tokenizer-mode") + 1] == "custom_mode"
    assert toks[toks.index("--reasoning-parser") + 1] == "custom_reasoner"
    assert "--trust-remote-code" in toks

    # compilation-config survives as ONE token (the whole JSON object).
    cc = toks[toks.index("--compilation-config") + 1]
    assert cc == '{"level": 3, "cudagraph_capture_sizes": [1, 2, 4]}'

    # Distributed rendezvous flags (head = rank 0, no --headless).
    assert toks[toks.index("--nnodes") + 1] == "2"
    assert toks[toks.index("--node-rank") + 1] == "0"
    assert toks[toks.index("--master-addr") + 1] == "10.0.0.1"
    assert toks[toks.index("--master-port") + 1] == "29501"
    assert "--headless" not in toks


def test_build_distributed_worker_is_headless_rank_1():
    cmd = templates.build_vllm_serve_cmd(
        model_container_path="/models/my-model",
        served_model_names="my-model",
        port=8000,
        tensor_parallel_size=2,
        gpu_memory_utilization=0.9,
        distributed={
            "nnodes": 2, "node_rank": 1, "master_addr": "10.0.0.1",
            "master_port": 29501, "headless": True,
        },
    )
    toks = _tokens(cmd)
    assert toks[toks.index("--node-rank") + 1] == "1"
    assert toks[toks.index("--master-addr") + 1] == "10.0.0.1"
    assert "--headless" in toks


def test_served_model_names_falls_back_to_single_name():
    cmd = templates.build_vllm_serve_cmd(
        model_container_path="/models/my-model",
        served_model_name="my-model",
        port=8000,
        tensor_parallel_size=1,
        distributed_backend="mp",
        gpu_memory_utilization=0.85,
    )
    toks = _tokens(cmd)
    i = toks.index("--served-model-name")
    assert toks[i + 1] == "my-model"
    # Explicit backend still emitted for single/cluster.
    assert toks[toks.index("--distributed-executor-backend") + 1] == "mp"


def test_advanced_args_boolean_and_valued_flags():
    cmd = templates.build_vllm_serve_cmd(
        model_container_path="/models/my-model",
        port=8000,
        tensor_parallel_size=1,
        gpu_memory_utilization=0.85,
        advanced_args='[{"flag": "--enable-prefix-caching", "value": null}, '
                      '{"flag": "--swap-space", "value": "8"}]',
    )
    toks = _tokens(cmd)
    assert "--enable-prefix-caching" in toks
    assert toks[toks.index("--swap-space") + 1] == "8"


def test_extra_args_appended_last_and_wins_served_name():
    cmd = templates.build_vllm_serve_cmd(
        model_container_path="/models/my-model",
        served_model_name="my-model",
        port=8000,
        tensor_parallel_size=1,
        gpu_memory_utilization=0.85,
        extra_args="--served-model-name override",
    )
    toks = _tokens(cmd)
    # The builder's own alias is suppressed when the user set one via extra_args.
    assert toks.count("--served-model-name") == 1
    assert toks[toks.index("--served-model-name") + 1] == "override"


def test_distributed_renderers_are_headless_and_serving():
    head = templates.render_instance_unit_distributed_head(
        name="my-inst", script_path="/opt/spark-controlplane/vllm-my-inst.sh"
    )
    worker = templates.render_instance_unit_distributed_worker(
        name="my-inst", script_path="/opt/spark-controlplane/vllm-my-inst-worker.sh"
    )
    assert "spark-vllm-my-inst" in head
    assert "distributed head" in head
    assert "spark-vllm-my-inst-worker" in worker
    assert "distributed worker" in worker

    run = templates.render_instance_docker_run_distributed(
        name="my-inst", role="worker", image="vllm/vllm-openai:latest",
        hf_home="/home/u/.cache/huggingface", models_dir="/home/u/models",
        shm="10.24gb", iface="eth1", host_qsfp="10.0.0.2", master_addr="10.0.0.1",
        serve_cmd="vllm serve /models/my-model --headless",
    )
    assert "--network host" in run and "--gpus all" in run
    assert "VLLM_HOST_IP=10.0.0.2" in run
    assert "MASTER_ADDR=10.0.0.1" in run
    assert "spark-vllm-my-inst-worker" in run
