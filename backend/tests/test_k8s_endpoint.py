"""Terminating an endpoint's TLS in Kubernetes instead of on the DGX.

The design rests on one claim, and if it is wrong the whole approach collapses
into needing cluster credentials: **a promotion is invisible from outside the
box.** Every member of an endpoint serves from the head node, and pinning
`upstream_port` means they all serve from the same port too — so an external
proxy holds a static upstream and never has to be told that the instance behind
it changed. `test_manifests_are_identical_across_a_promotion` is the one that
actually pins that claim; everything else guards a way it could quietly stop
being true.

The rest is the fallout of a port no longer belonging to one instance:
allocation must avoid it, conflict detection must permit it between co-members
and forbid it to everyone else, and every probe that used to read `inst.port`
must read the effective one or it will report a healthy instance as down.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    import app.config as config

    config.get_settings.cache_clear()
    import app.db as db

    importlib.reload(db)
    import app.main as main

    importlib.reload(main)
    with TestClientLocal(main.app) as c:
        yield c
    config.get_settings.cache_clear()


def TestClientLocal(app):
    from fastapi.testclient import TestClient

    return TestClient(app)


def _head(client):
    return client.post("/api/nodes", json={
        "role": "head", "name": "dgx-md-01", "lan_ip": "10.0.0.11",
        "qsfp_ip": "10.10.10.1", "ssh_user": "u", "ssh_password": "p",
    })


def _k8s_endpoint(client, **over):
    body = {
        "name": "prod", "hostname": "llm.skynet.telenor.net",
        "termination": "k8s", "upstream_port": 8443, "aliases": ["DSV4-DSpark"],
    }
    body.update(over)
    return client.post("/api/endpoints", json=body)


# --- what the operator declares ------------------------------------------

def test_a_k8s_endpoint_must_pin_its_upstream_port(client):
    """Without a pinned port the manifests would be a guess, and the guess
    would break the first time a member was allocated a different port."""
    r = _k8s_endpoint(client, upstream_port=None)
    assert r.status_code == 422, r.text
    assert "upstream_port" in r.text


def test_the_pin_cannot_be_removed_by_a_later_edit(client):
    """The create-time check is enforced by a Pydantic model validator; PATCH
    is a partial update, so it needs its own or the endpoint could be left
    Kubernetes-terminated with nothing to point at."""
    assert _k8s_endpoint(client).status_code == 201
    r = client.patch("/api/endpoints/prod", json={"upstream_port": None})
    assert r.status_code == 400, r.text
    assert "upstream_port" in r.text


def test_switching_an_onbox_endpoint_to_k8s_needs_a_port_too(client):
    client.post("/api/endpoints", json={
        "name": "prod", "hostname": "llm.example.net", "aliases": ["a"],
    })
    r = client.patch("/api/endpoints/prod", json={"termination": "k8s"})
    assert r.status_code == 400, r.text


def test_a_hostname_that_is_not_a_dns_name_is_refused(client):
    """It ends up in an ACME order and in generated YAML. Refuse it here rather
    than at `kubectl apply` or in a failed certificate issuance."""
    r = _k8s_endpoint(client, hostname="not a hostname!")
    assert r.status_code == 422, r.text


def test_hostnames_are_normalised(client):
    assert _k8s_endpoint(client, hostname="LLM.Skynet.Telenor.NET.").status_code == 201
    assert client.get("/api/endpoints").json()[0]["hostname"] == "llm.skynet.telenor.net"


# --- the manifests --------------------------------------------------------

def test_manifests_are_refused_for_an_onbox_endpoint(client):
    _head(client)
    client.post("/api/endpoints", json={
        "name": "prod", "hostname": "llm.example.net", "aliases": ["a"],
    })
    r = client.get("/api/endpoints/prod/manifests")
    assert r.status_code == 409
    assert "terminates TLS on the serving node" in r.text


def test_manifests_point_at_the_head_node_and_the_pinned_port(client):
    _head(client)
    _k8s_endpoint(client)
    y = client.get("/api/endpoints/prod/manifests").text
    assert "10.0.0.11" in y
    assert "port: 8443" in y
    assert "host: llm.skynet.telenor.net" in y


def test_manifests_need_a_head_node(client):
    _k8s_endpoint(client)
    r = client.get("/api/endpoints/prod/manifests")
    assert r.status_code == 409
    assert "head node" in r.text


def test_the_manifest_set_is_valid_yaml_with_the_four_expected_kinds(client):
    yaml = pytest.importorskip("yaml")
    _head(client)
    _k8s_endpoint(client)
    docs = [d for d in yaml.safe_load_all(client.get("/api/endpoints/prod/manifests").text) if d]
    kinds = {d["kind"] for d in docs}
    assert kinds == {"Service", "EndpointSlice", "Certificate", "Ingress"}

    slice_ = next(d for d in docs if d["kind"] == "EndpointSlice")
    assert slice_["endpoints"][0]["addresses"] == ["10.0.0.11"]
    # Pinned true on purpose: the cluster cannot health-check an address it does
    # not manage, and during a promotion the upstream is legitimately down for
    # the minutes it takes to load weights.
    assert slice_["endpoints"][0]["conditions"]["ready"] is True
    # The Service must have no selector — its backend is outside the cluster.
    svc = next(d for d in docs if d["kind"] == "Service")
    assert "selector" not in svc["spec"]


def test_streaming_annotations_are_present(client):
    """Without proxy-buffering off, an SSE stream is held in the proxy and
    delivered in chunks — tokens stop arriving one at a time. The default
    60s read timeout also truncates any long generation."""
    yaml = pytest.importorskip("yaml")
    _head(client)
    _k8s_endpoint(client)
    docs = list(yaml.safe_load_all(client.get("/api/endpoints/prod/manifests").text))
    ing = next(d for d in docs if d and d["kind"] == "Ingress")
    ann = ing["metadata"]["annotations"]
    assert ann["nginx.ingress.kubernetes.io/proxy-buffering"] == "off"
    assert int(ann["nginx.ingress.kubernetes.io/proxy-read-timeout"]) >= 600
    assert ann["nginx.ingress.kubernetes.io/proxy-body-size"] == "0"


def test_yaml_injection_through_a_query_parameter_is_rejected(client):
    """Namespace and issuer are interpolated into YAML. Reject rather than
    sanitise: a repaired value is a manifest that applies and does something
    other than what was asked."""
    _head(client)
    _k8s_endpoint(client)
    r = client.get(
        "/api/endpoints/prod/manifests",
        params={"namespace": "default\nkind: Evil"},
    )
    assert r.status_code == 400, r.text


def test_the_certificate_names_the_configured_issuer(client):
    yaml = pytest.importorskip("yaml")
    _head(client)
    _k8s_endpoint(client)
    docs = list(yaml.safe_load_all(
        client.get("/api/endpoints/prod/manifests",
                   params={"issuer": "telenor-ca", "issuer_kind": "Issuer"}).text
    ))
    cert = next(d for d in docs if d and d["kind"] == "Certificate")
    assert cert["spec"]["issuerRef"] == {"name": "telenor-ca", "kind": "Issuer"}
    assert cert["spec"]["dnsNames"] == ["llm.skynet.telenor.net"]


# --- the load-bearing claim ----------------------------------------------

def test_manifests_are_identical_across_a_promotion(client):
    """THE property this whole design depends on.

    If promoting changed anything in here, the portal would need cluster
    credentials to keep the manifests in step — and a promotion would have a
    window where the applied config pointed at a stopped instance. It does not,
    because both members serve from the head node on the same pinned port.
    """
    _head(client)
    _k8s_endpoint(client)
    m = client.post("/api/models", json={"repo_id": "org/dsv4"}).json()
    ep_id = client.get("/api/endpoints").json()[0]["id"]
    for name in ("id7", "id9"):
        r = client.post("/api/instances", json={
            "name": name, "model_id": m["id"], "topology": "cluster",
            "endpoint_id": ep_id,
        })
        assert r.status_code == 201, r.text

    before = client.get("/api/endpoints/prod/manifests").text
    _set_current(client, "id9")
    after = client.get("/api/endpoints/prod/manifests").text
    assert before == after


def _set_current(client, instance_name: str):
    """Move the endpoint pointer without running the real promote job, which
    would SSH to a node. What is under test is what the manifests say."""
    import asyncio

    import app.db as db
    from sqlalchemy import select

    from app.models import Endpoint, Instance

    async def _go():
        async with db.SessionLocal() as s:
            ep = (await s.execute(select(Endpoint))).scalars().first()
            inst = (
                await s.execute(select(Instance).where(Instance.name == instance_name))
            ).scalar_one()
            ep.current_instance_id = inst.id
            await s.commit()

    asyncio.run(_go())


# --- a port that belongs to the endpoint, not the instance ----------------

def test_a_member_binds_the_endpoints_pinned_port(client):
    from app.models import Endpoint, Instance
    from app.services.binding import effective_port

    inst = Instance(name="id7", port=8001)
    assert effective_port(inst) == 8001
    inst.endpoint = Endpoint(name="prod", hostname="h", upstream_port=8443)
    assert effective_port(inst) == 8443


def test_an_unpinned_endpoint_leaves_the_instance_port_alone(client):
    from app.models import Endpoint, Instance
    from app.services.binding import effective_port

    inst = Instance(name="id7", port=8001)
    inst.endpoint = Endpoint(name="prod", hostname="h")
    assert effective_port(inst) == 8001


def test_two_members_of_one_endpoint_may_share_a_port(client):
    """They can never run at once — an endpoint has one serving instance and
    promote stops the outgoing one first — and sharing the port is the entire
    reason the cluster manifests can be static."""
    _head(client)
    _k8s_endpoint(client)
    ep_id = client.get("/api/endpoints").json()[0]["id"]
    m = client.post("/api/models", json={"repo_id": "org/dsv4"}).json()
    a = client.post("/api/instances", json={
        "name": "id7", "model_id": m["id"], "topology": "cluster",
        "endpoint_id": ep_id, "port": 8443,
    })
    b = client.post("/api/instances", json={
        "name": "id9", "model_id": m["id"], "topology": "cluster",
        "endpoint_id": ep_id, "port": 8443,
    })
    assert a.status_code == 201, a.text
    assert b.status_code == 201, b.text


def test_a_non_member_may_not_take_a_members_port(client):
    """The exemption is scoped to co-members. Anything else claiming the port
    would collide for real at bind time."""
    _head(client)
    _k8s_endpoint(client)
    ep_id = client.get("/api/endpoints").json()[0]["id"]
    m = client.post("/api/models", json={"repo_id": "org/dsv4"}).json()
    client.post("/api/instances", json={
        "name": "id7", "model_id": m["id"], "topology": "cluster",
        "endpoint_id": ep_id, "port": 8443,
    })
    r = client.post("/api/instances", json={
        "name": "other", "model_id": m["id"], "topology": "cluster", "port": 8443,
    })
    assert r.status_code == 409, r.text


def test_auto_allocation_avoids_a_pinned_upstream_port(client):
    """The member's own `port` column may say 8001 while it actually binds
    8443. Allocating on the column alone would hand 8443 to a fresh instance."""
    import asyncio

    import app.db as db
    from app.models import Endpoint, Instance, ModelRegistry
    from app.services.ports import allocate_api_port

    async def _go():
        async with db.SessionLocal() as s:
            ep = Endpoint(name="prod", hostname="llm.x.net", termination="k8s",
                          upstream_port=8000)
            s.add(ep)
            await s.flush()
            m = ModelRegistry(repo_id="org/dsv4", name="dsv4")
            s.add(m)
            await s.flush()
            s.add(Instance(name="id7", model_id=m.id, topology="cluster",
                           port=9001, endpoint_id=ep.id))
            await s.commit()
        async with db.SessionLocal() as s:
            return await allocate_api_port(s)

    assert asyncio.run(_go()) != 8000


# --- TLS: what actually runs on the box ----------------------------------

def test_a_k8s_member_runs_no_sidecar_even_with_tls_enabled(client):
    """`tls_enabled` records the operator's intent to serve HTTPS. Where it is
    terminated is the endpoint's decision, and for `k8s` that is not here."""
    from app.models import Endpoint, Instance
    from app.services.binding import effective_tls

    inst = Instance(name="id7", port=8001, tls_enabled=True,
                    tls_cert_enc="own", tls_key_enc="ownkey")
    inst.endpoint = Endpoint(name="prod", hostname="h", termination="k8s",
                             upstream_port=8443)
    assert effective_tls(inst) == (False, None, None)


def test_an_onbox_member_uses_the_endpoints_certificate(client):
    """The handoff #77 exists for: promoting moves the certificate without the
    key ever leaving the portal."""
    from app.models import Endpoint, Instance
    from app.services.binding import effective_tls

    inst = Instance(name="id7", port=8001, tls_enabled=True,
                    tls_cert_enc="own", tls_key_enc="ownkey")
    inst.endpoint = Endpoint(name="prod", hostname="h", termination="onbox",
                             tls_cert_enc="ep", tls_key_enc="epkey")
    assert effective_tls(inst) == (True, "ep", "epkey")


def test_a_standalone_instance_is_unaffected(client):
    from app.models import Instance
    from app.services.binding import effective_tls

    inst = Instance(name="solo", port=8001, tls_enabled=True,
                    tls_cert_enc="own", tls_key_enc="ownkey")
    assert effective_tls(inst) == (True, "own", "ownkey")


def test_probes_target_the_effective_port_and_skip_tls_for_a_k8s_member(client):
    """A k8s member has TLS requested but no sidecar listening. Probing
    https://node:tls_port would hit a closed socket and report a healthy
    instance as down — the failure mode this guards is a false alarm on
    production."""
    from app.models import Endpoint, Instance, Node
    from app.services.status_svc import instance_base_url

    head = Node(role="head", name="dgx-md-01", lan_ip="10.0.0.11", qsfp_ip="10.10.10.1")
    inst = Instance(name="id7", topology="cluster", port=8001, tls_enabled=True,
                    tls_port=443)
    inst.endpoint = Endpoint(name="prod", hostname="h", termination="k8s",
                             upstream_port=8443)
    assert instance_base_url(inst, head) == ("http://10.0.0.11:8443", True)


def test_deploying_a_k8s_member_removes_the_sidecar_and_says_why(client, monkeypatch):
    """Covers the branch nothing else can reach without SSH.

    It is also where a NameError lived: the k8s check ran inside the deploy
    path, which no unit test touches, so an undefined import would have
    surfaced on the first real deploy rather than in CI.
    """
    import asyncio

    import app.db as db
    import app.services.instances as inst_svc
    from app.models import Endpoint, Instance, ModelRegistry, Node

    removed: list[str] = []

    async def fake_remove(ssh, unit, log_cb=None):
        removed.append(unit)

    monkeypatch.setattr(inst_svc.nodeops, "remove_systemd_unit", fake_remove)
    monkeypatch.setattr(inst_svc, "ssh_for_node", _async_none)

    class Handle:
        def __init__(self):
            self.lines: list[str] = []

        async def log(self, text, stream="info"):
            self.lines.append(text)

        def ssh_log_cb(self):
            return None

    handle = Handle()

    async def _go():
        async with db.SessionLocal() as s:
            m = ModelRegistry(repo_id="org/dsv4", name="dsv4")
            node = Node(role="head", name="dgx-md-01", lan_ip="10.0.0.11",
                        qsfp_ip="10.10.10.1", ssh_user="u")
            ep = Endpoint(name="prod", hostname="llm.x.net", termination="k8s",
                          upstream_port=8443)
            s.add_all([m, node, ep])
            await s.flush()
            inst = Instance(name="id7", model_id=m.id, topology="cluster",
                            port=8001, tls_enabled=True, tls_cert_enc="c",
                            tls_key_enc="k", endpoint_id=ep.id)
            s.add(inst)
            await s.commit()
            full = await inst_svc.load_instance(s, inst.id)
            await inst_svc._deploy_tls_proxy(s, handle, full, node)

    asyncio.run(_go())
    assert removed, "the on-box sidecar must be torn down, not left stale"
    assert any("terminates TLS in Kubernetes" in line for line in handle.lines)


async def _async_none(*a, **kw):
    return None


# --- promote ---------------------------------------------------------------

def test_promote_does_not_demand_a_certificate_for_a_k8s_endpoint(client):
    """The :443 guard exists so an onbox endpoint cannot be promoted with no
    certificate to serve. A k8s endpoint's certificate lives in the cluster and
    the portal is never supposed to hold one."""
    import inspect as _inspect

    import app.services.endpoints as ep_svc

    src = _inspect.getsource(ep_svc.promote)
    guard = src[src.index("has no certificate") - 400:src.index("has no certificate")]
    assert "TERM_K8S" in guard, "the :443 cert guard must exempt k8s termination"


# --- a backup that can actually be restored ------------------------------

def _bundle_with_an_endpoint_member():
    """Build a bundle from a database that has an endpoint with a member,
    then return it as it would come back off disk."""
    import asyncio
    import json

    import app.db as db
    from app.models import Endpoint, EndpointAlias, Instance, ModelRegistry
    from app.services import backup

    async def _go():
        async with db.SessionLocal() as s:
            m = ModelRegistry(repo_id="org/dsv4", name="dsv4")
            ep = Endpoint(name="prod", hostname="llm.x.net", termination="k8s",
                          upstream_port=8443)
            s.add_all([m, ep])
            await s.flush()
            s.add(EndpointAlias(endpoint_id=ep.id, alias="DSV4-DSpark", position=0))
            inst = Instance(name="id7", model_id=m.id, topology="cluster",
                            port=8001, endpoint_id=ep.id)
            s.add(inst)
            await s.flush()
            ep.current_instance_id = inst.id
            await s.commit()
        return json.loads(json.dumps(await backup.build_bundle()))

    return asyncio.run(_go())


def _restore_onto_a_fresh_box(bundle, tmp_path, monkeypatch):
    import asyncio

    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path / "fresh"))
    (tmp_path / "fresh").mkdir()
    import app.config as config

    config.get_settings.cache_clear()
    import app.db as db

    importlib.reload(db)
    from app.services import backup

    importlib.reload(backup)

    async def _go():
        await db.init_db()
        return await backup.apply_bundle(bundle)

    return asyncio.run(_go()), db


def test_a_backup_restores_onto_a_fresh_box(client, tmp_path, monkeypatch):
    """The case a backup exists for, and the case that was broken.

    An instance's `endpoint_id` referenced a table the bundle did not carry, so
    the FOREIGN KEY violation aborted the ENTIRE restore — every table, not
    just the endpoints. Restoring over the existing database hid it, because
    the endpoint rows were still on disk.
    """
    import asyncio

    from sqlalchemy import select

    bundle = _bundle_with_an_endpoint_member()
    assert "endpoints" in bundle["tables"]

    result, db = _restore_onto_a_fresh_box(bundle, tmp_path, monkeypatch)
    assert result["restored"]["instances"] == 1
    assert result["restored"]["endpoints"] == 1

    async def _check():
        from app.models import Endpoint, Instance

        async with db.SessionLocal() as s:
            ep = (await s.execute(select(Endpoint))).scalars().one()
            inst = (await s.execute(select(Instance))).scalars().one()
            return ep, inst

    ep, inst = asyncio.run(_check())
    assert inst.endpoint_id == ep.id
    # The circular half: restored only after instances exist.
    assert ep.current_instance_id == inst.id
    # And the settings that make the cluster manifests correct.
    assert (ep.termination, ep.upstream_port) == ("k8s", 8443)


def test_an_older_bundle_without_endpoints_still_restores(client, tmp_path, monkeypatch):
    """A v2 bundle carries instances but no endpoints. Dropping the membership
    is recoverable; aborting the restore is not."""
    import asyncio

    from sqlalchemy import select

    bundle = _bundle_with_an_endpoint_member()
    for name in ("endpoints", "endpoint_aliases", "endpoint_promotions"):
        bundle["tables"].pop(name)
    bundle["bundle_version"] = 2

    result, db = _restore_onto_a_fresh_box(bundle, tmp_path, monkeypatch)
    assert result["restored"]["instances"] == 1
    assert "endpoints" in result["not_in_bundle"]

    async def _check():
        from app.models import Instance

        async with db.SessionLocal() as s:
            return (await s.execute(select(Instance))).scalars().one()

    assert asyncio.run(_check()).endpoint_id is None


def test_promotion_history_travels_without_its_job(client, tmp_path, monkeypatch):
    """Rollback reads the active promotion row to find what served the endpoint
    before. Without it a restored endpoint cannot roll back at all — but
    `jobs` never travels, so the job reference has to be dropped."""
    import asyncio

    from sqlalchemy import select

    import app.db as db
    from app.models import (
        Endpoint,
        EndpointPromotion,
        Instance,
        Job,
        ModelRegistry,
        PROMO_ACTIVE,
    )
    from app.services import backup

    async def _seed():
        async with db.SessionLocal() as s:
            job = Job(type="endpoint.promote", title="t", status="done")
            m = ModelRegistry(repo_id="org/dsv4", name="dsv4")
            ep = Endpoint(name="prod", hostname="llm.x.net")
            s.add_all([job, m, ep])
            await s.flush()
            old = Instance(name="id7", model_id=m.id, topology="cluster",
                           port=8001, endpoint_id=ep.id)
            new = Instance(name="id9", model_id=m.id, topology="cluster",
                           port=8002, endpoint_id=ep.id)
            s.add_all([old, new])
            await s.flush()
            s.add(EndpointPromotion(
                endpoint_id=ep.id, endpoint_name="prod",
                to_instance_id=new.id, to_instance_name="id9",
                from_instance_id=old.id, from_instance_name="id7",
                status=PROMO_ACTIVE, job_id=job.id,
            ))
            ep.current_instance_id = new.id
            await s.commit()
        import json

        return json.loads(json.dumps(await backup.build_bundle()))

    bundle = asyncio.run(_seed())
    result, db2 = _restore_onto_a_fresh_box(bundle, tmp_path, monkeypatch)
    assert result["restored"]["endpoint_promotions"] == 1

    async def _check():
        from app.models import EndpointPromotion as EP

        async with db2.SessionLocal() as s:
            return (await s.execute(select(EP))).scalars().one()

    promo = asyncio.run(_check())
    assert promo.from_instance_name == "id7"
    assert promo.job_id is None


# --- upgrading an existing install ---------------------------------------

def test_the_new_columns_are_added_to_an_existing_endpoints_table(tmp_path, monkeypatch):
    """An install already running named endpoints has an `endpoints` table
    without these columns. `create_all` adds tables, never columns."""
    import asyncio
    import sqlite3

    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    import app.config as config

    config.get_settings.cache_clear()
    import app.db as db

    importlib.reload(db)
    asyncio.run(db.init_db())

    path = str(config.get_settings().db_path)
    con = sqlite3.connect(path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(endpoints)")}
    assert {"termination", "upstream_port"} <= cols

    # Drop them and re-run init_db, which is what an upgrade looks like.
    con.execute("ALTER TABLE endpoints DROP COLUMN termination")
    con.execute("ALTER TABLE endpoints DROP COLUMN upstream_port")
    con.execute(
        "INSERT INTO endpoints (name, hostname, port, enabled, created_at, updated_at) "
        "VALUES ('legacy', 'llm.example.net', 443, 1, '2026-01-01 00:00:00', "
        "'2026-01-01 00:00:00')"
    )
    con.commit()
    con.close()

    importlib.reload(db)
    asyncio.run(db.init_db())

    con = sqlite3.connect(path)
    row = con.execute(
        "SELECT termination, upstream_port FROM endpoints WHERE name='legacy'"
    ).fetchone()
    con.close()
    config.get_settings.cache_clear()
    # The pre-existing endpoint keeps terminating on the box. Anything else
    # would silently move a live production endpoint's TLS on upgrade.
    assert row == ("onbox", None)
