"""Helpers for computing per-node filesystem paths from a node's SSH user.

We assume a conventional home directory layout (``/home/<user>`` or ``/root``)
and derive the HuggingFace cache and models directories from the cluster config
subdir settings.
"""

from __future__ import annotations

from ..models import ClusterConfig, Node


def home_dir(ssh_user: str) -> str:
    return "/root" if ssh_user == "root" else f"/home/{ssh_user}"


def hf_cache_host_path(node: Node, cfg: ClusterConfig) -> str:
    return f"{home_dir(node.ssh_user)}/{cfg.hf_cache_subdir}"


def models_host_dir(node: Node, cfg: ClusterConfig) -> str:
    return f"{home_dir(node.ssh_user)}/{cfg.models_subdir}"


def model_host_path(node: Node, cfg: ClusterConfig, model_name: str) -> str:
    return f"{models_host_dir(node, cfg)}/{model_name}"


def model_container_path(cfg: ClusterConfig, model_name: str) -> str:
    return f"{cfg.models_container_path.rstrip('/')}/{model_name}"
