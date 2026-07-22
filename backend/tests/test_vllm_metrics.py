"""vLLM Prometheus parsing + windowed rate derivation."""

from __future__ import annotations

import pytest

from app.services.telemetry import derive_instance_metrics, parse_prometheus

SCRAPE_1 = """
# HELP vllm:num_requests_running Number of requests currently running.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{model_name="/models/m"} 3.0
vllm:num_requests_waiting{model_name="/models/m"} 5.0
vllm:gpu_cache_usage_perc{model_name="/models/m"} 0.42
vllm:prompt_tokens_total{model_name="/models/m"} 10000.0
vllm:generation_tokens_total{model_name="/models/m"} 50000.0
vllm:request_success_total{finished_reason="stop",model_name="/models/m"} 90.0
vllm:request_success_total{finished_reason="length",model_name="/models/m"} 10.0
vllm:time_to_first_token_seconds_sum{model_name="/models/m"} 20.0
vllm:time_to_first_token_seconds_count{model_name="/models/m"} 100.0
vllm:e2e_request_latency_seconds_sum{model_name="/models/m"} 500.0
vllm:e2e_request_latency_seconds_count{model_name="/models/m"} 100.0
"""

# 10s later: +12000 gen tokens, +2000 prompt tokens, +20 requests,
# TTFT sum +6s over +20 requests (300ms avg), e2e +30s over +20 (1500ms avg)
SCRAPE_2 = (
    SCRAPE_1.replace('vllm:prompt_tokens_total{model_name="/models/m"} 10000.0',
                     'vllm:prompt_tokens_total{model_name="/models/m"} 12000.0')
    .replace('vllm:generation_tokens_total{model_name="/models/m"} 50000.0',
             'vllm:generation_tokens_total{model_name="/models/m"} 62000.0')
    .replace('vllm:request_success_total{finished_reason="stop",model_name="/models/m"} 90.0',
             'vllm:request_success_total{finished_reason="stop",model_name="/models/m"} 105.0')
    .replace('vllm:request_success_total{finished_reason="length",model_name="/models/m"} 10.0',
             'vllm:request_success_total{finished_reason="length",model_name="/models/m"} 15.0')
    .replace('vllm:time_to_first_token_seconds_sum{model_name="/models/m"} 20.0',
             'vllm:time_to_first_token_seconds_sum{model_name="/models/m"} 26.0')
    .replace('vllm:time_to_first_token_seconds_count{model_name="/models/m"} 100.0',
             'vllm:time_to_first_token_seconds_count{model_name="/models/m"} 120.0')
    .replace('vllm:e2e_request_latency_seconds_sum{model_name="/models/m"} 500.0',
             'vllm:e2e_request_latency_seconds_sum{model_name="/models/m"} 530.0')
    .replace('vllm:e2e_request_latency_seconds_count{model_name="/models/m"} 100.0',
             'vllm:e2e_request_latency_seconds_count{model_name="/models/m"} 120.0')
)


def test_parse_sums_label_sets_and_skips_comments():
    p = parse_prometheus(SCRAPE_1)
    assert p["vllm:num_requests_running"] == 3.0
    assert p["vllm:request_success_total"] == 100.0  # stop + length summed
    assert p["vllm:gpu_cache_usage_perc"] == pytest.approx(0.42)
    assert "# HELP" not in str(p)


def test_first_scrape_gauges_only():
    m, nxt = derive_instance_metrics(parse_prometheus(SCRAPE_1), {}, ts=100.0)
    assert m.running == 3 and m.waiting == 5
    assert m.kv_cache_pct == 42.0
    assert m.gen_tps is None and m.ttft_ms is None  # no baseline yet
    assert nxt["vllm:generation_tokens_total"] == 50000.0


def test_second_scrape_derives_rates():
    _, prev = derive_instance_metrics(parse_prometheus(SCRAPE_1), {}, ts=100.0)
    m, _ = derive_instance_metrics(parse_prometheus(SCRAPE_2), prev, ts=110.0)
    assert m.gen_tps == pytest.approx(1200.0)
    assert m.prompt_tps == pytest.approx(200.0)
    assert m.req_per_s == pytest.approx(2.0)
    assert m.ttft_ms == pytest.approx(300.0)
    assert m.e2e_ms == pytest.approx(1500.0)


def test_counter_reset_yields_no_rates():
    _, prev = derive_instance_metrics(parse_prometheus(SCRAPE_2), {}, ts=100.0)
    m, _ = derive_instance_metrics(parse_prometheus(SCRAPE_1), prev, ts=110.0)  # went backwards
    assert m.gen_tps is None and m.ttft_ms is None
    assert m.running == 3  # gauges still fine


def test_v1_kv_cache_name_supported():
    p = parse_prometheus('vllm:kv_cache_usage_perc{model_name="m"} 0.65')
    m, _ = derive_instance_metrics(p, {}, ts=1.0)
    assert m.kv_cache_pct == 65.0
