"""instance_base_url: the one place playground/evals/health/metrics resolve an
instance's HTTP surface. Regression for the live-cluster report: a distributed
TLS instance got 'Instance has no reachable host' in the Playground."""

from __future__ import annotations

from app.models import Instance, Node
from app.services.status_svc import instance_api_node, instance_base_url


def _head() -> Node:
    return Node(id=1, role="head", name="dgx-md-01", lan_ip="10.88.10.30",
                qsfp_ip="10.10.10.1", ssh_user="u")


def _worker_node() -> Node:
    return Node(id=2, role="worker", name="dgx-md-02", lan_ip="10.88.10.31",
                qsfp_ip="10.10.10.2", ssh_user="u")


def test_single_plain_uses_pinned_node():
    inst = Instance(name="a", model_id=1, topology="single", port=8000, tls_enabled=False)
    inst.node = _worker_node()
    assert instance_base_url(inst, _head()) == ("http://10.88.10.31:8000", True)


def test_distributed_plain_uses_head():
    inst = Instance(name="b", model_id=1, topology="distributed", port=8000, tls_enabled=False)
    inst.node = None
    assert instance_api_node(inst, _head()).role == "head"
    assert instance_base_url(inst, _head()) == ("http://10.88.10.30:8000", True)


def test_distributed_tls_routes_via_proxy():
    """The live-cluster case: distributed + TLS on :443."""
    inst = Instance(name="dsv4-flash", model_id=1, topology="distributed",
                    port=8000, tls_enabled=True, tls_port=443)
    inst.node = None
    assert instance_base_url(inst, _head()) == ("https://10.88.10.30:443", False)


def test_cluster_uses_head():
    inst = Instance(name="c", model_id=1, topology="cluster", port=8000, tls_enabled=False)
    inst.node = None
    assert instance_base_url(inst, _head()) == ("http://10.88.10.30:8000", True)


def test_no_head_resolves_none():
    inst = Instance(name="d", model_id=1, topology="distributed", port=8000, tls_enabled=False)
    inst.node = None
    assert instance_base_url(inst, None) is None
