"""Serve profiles: capture, share, and apply known-good vLLM settings.

The security-relevant half is import. A profile arrives from a gist or a
colleague and its settings feed the vLLM command line, which becomes a
``docker run`` on the nodes with ``--gpus all``, ``--network host`` and the
models directory mounted. A shared profile may describe *how* to serve a model;
it must never get to choose *what code runs*.
"""

from __future__ import annotations

import importlib
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    import app.config as config

    config.get_settings.cache_clear()
    import app.db as db

    importlib.reload(db)
    import app.main as main

    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c
    config.get_settings.cache_clear()


# --- built-ins ------------------------------------------------------------
def test_builtins_are_seeded_and_carry_the_laguna_flags(client):
    """The Laguna profile exists because getting those flags right by hand cost
    a real bring-up; the numbers are the ones that actually served."""
    profiles = client.get("/api/profiles").json()
    by_name = {p["name"]: p for p in profiles}
    assert "laguna-fp8-dual" in by_name

    lag = by_name["laguna-fp8-dual"]
    assert lag["builtin"] is True
    assert lag["repo_id"] == "poolside/Laguna-S-2.1-FP8"
    s = lag["settings"]
    assert s["topology"] == "distributed"
    assert s["tensor_parallel_size"] == 2
    assert s["gpu_memory_utilization"] == 0.72
    assert s["max_model_len"] == 131072
    assert s["max_num_seqs"] == 8
    assert s["max_num_batched_tokens"] == 2048
    assert s["reasoning_parser"] == "poolside_v1"
    # NOT trust_remote_code: it was never in the operator's known-good set, and
    # shipping it enabled while refusing it on import would be incoherent.
    assert "trust_remote_code" not in s


def test_builtins_cannot_be_edited_or_deleted(client):
    """They are refreshed from the image on every start, so an edit would be
    silently clobbered on upgrade — better to refuse than to lose the change."""
    lag = next(p for p in client.get("/api/profiles").json() if p["builtin"])
    r = client.patch(f"/api/profiles/{lag['id']}", json={"description": "mine now"})
    assert r.status_code == 409 and "Duplicate" in r.json()["detail"]
    assert client.delete(f"/api/profiles/{lag['id']}").status_code == 409


def test_builtins_are_refreshed_not_duplicated_on_restart(tmp_path, monkeypatch):
    """Two starts must not leave two copies."""
    import app.config as config
    import app.db as db

    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    config.get_settings.cache_clear()
    importlib.reload(db)
    import app.main as main

    importlib.reload(main)
    with TestClient(main.app) as c:
        first = [p["name"] for p in c.get("/api/profiles").json()]
    with TestClient(main.app) as c:
        second = [p["name"] for p in c.get("/api/profiles").json()]
    assert first == second
    assert len(second) == len(set(second))
    config.get_settings.cache_clear()


# --- create / capture -----------------------------------------------------
def test_create_and_list(client):
    r = client.post("/api/profiles", json={
        "name": "my-qwen",
        "description": "works on our pair",
        "repo_id": "Qwen/Qwen3-30B-A3B-FP8",
        "settings": {"topology": "distributed", "gpu_memory_utilization": 0.8,
                     "max_model_len": 32768},
    })
    assert r.status_code == 201
    assert r.json()["settings"]["max_model_len"] == 32768
    assert r.json()["builtin"] is False


def test_settings_are_validated_like_a_real_instance(client):
    """A profile must not be able to hold a value a hand-typed instance would
    have been refused — otherwise the error surfaces minutes into a start."""
    r = client.post("/api/profiles", json={
        "name": "bad", "settings": {"compilation_config": "not json"},
    })
    assert r.status_code == 422 and "JSON" in r.json()["detail"]

    r = client.post("/api/profiles", json={
        "name": "bad2", "settings": {"topology": "sideways"},
    })
    assert r.status_code == 422


def test_non_serve_fields_are_refused(client):
    """Name/port/node/api_key are per-instance facts. A profile carrying them
    would be unshareable at best and a credential leak at worst."""
    for field, value in [("name", "x"), ("port", 8000), ("node_id", 1),
                         ("api_key", "secret"), ("tls_cert", "PEM")]:
        r = client.post("/api/profiles", json={
            "name": f"p-{field}", "settings": {field: value},
        })
        assert r.status_code == 422, f"{field} should not be a profile setting"


def test_capture_from_an_instance(client):
    import asyncio

    import app.db as db
    from app.models import Instance, ModelRegistry

    async def seed():
        async with db.SessionLocal() as s:
            m = ModelRegistry(repo_id="o/m", name="m", status="present")
            s.add(m)
            await s.flush()
            inst = Instance(
                name="lag", model_id=m.id, topology="distributed", port=8000,
                tensor_parallel_size=2, gpu_memory_utilization=0.72,
                max_model_len=131072, max_num_seqs=8, reasoning_parser="poolside_v1",
                api_key_enc="should-not-travel",
            )
            s.add(inst)
            await s.commit()
            return inst.id

    iid = asyncio.run(seed())
    r = client.post(f"/api/profiles/from-instance/{iid}",
                    json={"name": "captured", "description": "from the live one"})
    assert r.status_code == 201
    s = r.json()["settings"]
    assert s["gpu_memory_utilization"] == 0.72 and s["max_model_len"] == 131072
    # instance-specific facts must not be captured
    assert "name" not in s and "port" not in s and "api_key" not in s


# --- sharing --------------------------------------------------------------
def test_export_round_trips_through_import(client):
    client.post("/api/profiles", json={
        "name": "shareme", "settings": {"gpu_memory_utilization": 0.8},
    })
    doc = client.get("/api/profiles/export").json()
    assert doc["kind"] == "spark-controlplane-serve-profiles"
    assert doc["version"] == 1
    # built-ins are not exported — they ship with the image on the other end too
    assert [p["name"] for p in doc["profiles"]] == ["shareme"]

    # importing into a portal that already has it skips rather than clobbers
    res = client.post("/api/profiles/import", json=doc).json()
    assert res["skipped"] == ["shareme"] and res["imported"] == []

    doc["profiles"][0]["name"] = "shareme-2"
    res = client.post("/api/profiles/import", json=doc).json()
    assert res["imported"] == ["shareme-2"]


def test_import_refuses_a_document_that_is_not_ours(client):
    assert client.post("/api/profiles/import", json={
        "kind": "something-else", "version": 1, "profiles": [],
    }).status_code == 422
    assert client.post("/api/profiles/import", json={
        "kind": "spark-controlplane-serve-profiles", "version": 99, "profiles": [],
    }).status_code == 422


def test_import_drops_fields_that_would_choose_what_runs_on_the_nodes(client):
    """THE import test. Instances run with --gpus all, --network host and the
    models dir mounted, so a profile that could set vllm_image would be remote
    code execution as root on a DGX. extra_args is a raw flag passthrough into
    the same command. Both are dropped on import — and still settable by hand,
    where the operator is the author."""
    res = client.post("/api/profiles/import", json={
        "kind": "spark-controlplane-serve-profiles",
        "version": 1,
        "profiles": [{
            "name": "trojan",
            "settings": {
                "gpu_memory_utilization": 0.8,
                "vllm_image": "attacker/backdoor:latest",
                "extra_args": "--load-format dummy",
            },
        }],
    }).json()

    assert res["imported"] == ["trojan"]
    assert set(res["dropped_fields"]) == {"trojan.vllm_image", "trojan.extra_args"}

    stored = next(p for p in client.get("/api/profiles").json() if p["name"] == "trojan")
    assert "vllm_image" not in stored["settings"], "imported profile chose the container image"
    assert "extra_args" not in stored["settings"]
    assert stored["settings"]["gpu_memory_utilization"] == 0.8  # the benign part survives


def test_locally_created_profiles_may_still_pin_an_image(client):
    """The restriction is about provenance, not the field: an operator setting
    their own image is normal, a stranger's JSON doing it is not."""
    r = client.post("/api/profiles", json={
        "name": "pinned", "settings": {"vllm_image": "nvcr.io/nvidia/vllm:26.05-py3"},
    })
    assert r.status_code == 201
    assert r.json()["settings"]["vllm_image"] == "nvcr.io/nvidia/vllm:26.05-py3"


def test_import_rejects_invalid_settings_outright(client):
    """A dropped passthrough is discarded quietly (it was never going to be
    used), but a bad value in a field that IS imported fails the document —
    better than storing a profile that errors minutes into a start."""
    r = client.post("/api/profiles/import", json={
        "kind": "spark-controlplane-serve-profiles",
        "version": 1,
        "profiles": [{"name": "bad", "settings": {"topology": "sideways"}}],
    })
    assert r.status_code == 422 and "bad" in r.json()["detail"]

    # ...whereas a malformed passthrough is simply dropped, not fatal
    r = client.post("/api/profiles/import", json={
        "kind": "spark-controlplane-serve-profiles",
        "version": 1,
        "profiles": [{"name": "ok", "settings": {
            "advanced_args": "{not-an-array}", "max_model_len": 4096,
        }}],
    })
    assert r.status_code == 200
    stored = next(p for p in client.get("/api/profiles").json() if p["name"] == "ok")
    assert stored["settings"] == {"max_model_len": 4096}


def test_profiles_survive_a_backup_round_trip(client):
    """Profiles are configuration, so they belong in the bundle — otherwise a
    restore silently loses the settings that took a bring-up to find."""
    client.post("/api/profiles", json={
        "name": "keeper", "settings": {"max_model_len": 8192},
    })
    bundle = client.get("/api/backup/export").json()
    names = [p["name"] for p in bundle["tables"]["serve_profiles"]]
    assert "keeper" in names

    # wipe it, then restore
    pid = next(p["id"] for p in client.get("/api/profiles").json() if p["name"] == "keeper")
    client.delete(f"/api/profiles/{pid}")
    assert "keeper" not in [p["name"] for p in client.get("/api/profiles").json()]

    r = client.post("/api/backup/import", json=bundle)
    assert r.status_code == 200
    after = {p["name"]: p for p in client.get("/api/profiles").json()}
    assert after["keeper"]["settings"]["max_model_len"] == 8192
    # built-ins are still exactly one copy each, not duplicated by the restore
    builtins = [p["name"] for p in after.values() if p["builtin"]]
    assert len(builtins) == len(set(builtins))
    assert "laguna-fp8-dual" in builtins


def test_import_drops_every_flag_passthrough(client):
    """v1.26.0 tried to keep advanced_args by filtering a denylist of dangerous
    flags. The denylist was bypassable within minutes: --middleware makes vLLM
    import an arbitrary object into the API server, --tool-parser-plugin
    executes a Python file, --allowed-local-media-path turns the endpoint into
    an arbitrary file read — none were on the list, and none were reported.

    A denylist of flags against an upstream CLI that grows every release cannot
    be right. Passthroughs are now dropped wholesale on import."""
    res = client.post("/api/profiles/import", json={
        "kind": "spark-controlplane-serve-profiles",
        "version": 1,
        "profiles": [{
            "name": "trojan",
            "settings": {
                "gpu_memory_utilization": 0.8,
                "max_model_len": 8192,
                "advanced_args": json.dumps([
                    {"flag": "--middleware", "value": "evil.module:hook"},
                    {"flag": "--tool-parser-plugin", "value": "/models/evil.py"},
                    {"flag": "--allowed-local-media-path", "value": "/"},
                    {"flag": "--enable-chunked-prefill", "value": None},
                ]),
                "compilation_config": json.dumps({"level": 3}),
                "trust_remote_code": True,
            },
        }],
    }).json()

    stored = next(p for p in client.get("/api/profiles").json() if p["name"] == "trojan")
    assert "advanced_args" not in stored["settings"], "flag passthrough survived import"
    assert "compilation_config" not in stored["settings"]
    assert stored["settings"].get("trust_remote_code") is not True
    # the tuning that is the actual point of a shared profile survives
    assert stored["settings"]["gpu_memory_utilization"] == 0.8
    assert stored["settings"]["max_model_len"] == 8192
    # and the operator is told what was removed, not left to discover it
    assert set(res["dropped_fields"]) == {
        "trojan.advanced_args", "trojan.compilation_config", "trojan.trust_remote_code",
    }


def test_the_motivating_recipe_survives_import_intact(client):
    """The whole point of #56 is sharing a recipe like Laguna's. Dropping the
    passthroughs must not cost anything that recipe actually needs."""
    laguna = {
        "topology": "distributed", "tensor_parallel_size": 2,
        "gpu_memory_utilization": 0.72, "max_model_len": 131072,
        "max_num_seqs": 8, "max_num_batched_tokens": 2048,
        "reasoning_parser": "poolside_v1", "tool_parser": "poolside_v1",
        "enable_tool_choice": True,
    }
    res = client.post("/api/profiles/import", json={
        "kind": "spark-controlplane-serve-profiles", "version": 1,
        "profiles": [{"name": "laguna-shared", "settings": laguna}],
    }).json()
    assert res["imported"] == ["laguna-shared"] and res["dropped_fields"] == []
    stored = next(p for p in client.get("/api/profiles").json()
                  if p["name"] == "laguna-shared")
    assert stored["settings"] == laguna


def test_locally_authored_profiles_keep_full_control(client):
    """The restriction is provenance, not paternalism: the operator writing
    their own profile can still use every flag, including the ones import
    strips."""
    r = client.post("/api/profiles", json={
        "name": "mine",
        "settings": {
            "trust_remote_code": True,
            "advanced_args": json.dumps([{"flag": "--enable-chunked-prefill", "value": None}]),
            "compilation_config": json.dumps({"level": 3}),
            "vllm_image": "nvcr.io/nvidia/vllm:26.05-py3",
        },
    })
    assert r.status_code == 201
    got = r.json()["settings"]
    assert got["trust_remote_code"] is True
    assert "--enable-chunked-prefill" in got["advanced_args"]
    assert got["compilation_config"] == json.dumps({"level": 3})
    assert got["vllm_image"] == "nvcr.io/nvidia/vllm:26.05-py3"
