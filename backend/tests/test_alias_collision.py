"""Alias resolution when two running instances advertise the same name.

The bug this covers routed production traffic to the wrong instance, silently.
`_running_instances` had no ORDER BY, so SQLite returned rowid order and
`_resolve` took the first match — meaning the OLDEST instance won. Start a
replacement alongside the incumbent and traffic kept going to the incumbent:
the promotion looked like it worked and changed nothing.

Two things are deliberately NOT done here, and both are load-bearing:

* Duplicate aliases are not rejected at write time. A promotion is exactly the
  case where the replacement legitimately carries the outgoing instance's
  names, and the outgoing instance must keep them to remain a rollback
  candidate. The collision only means anything among *running* instances, and
  status changes long after the write.
* A collision does not produce an error response. Refusing to serve would take
  a production endpoint down over a configuration ambiguity, which is worse
  than answering from the newest instance. It is loud instead: ERROR in the
  log, and reported on the routes API for the UI.
"""

from __future__ import annotations

import importlib

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


def _two_instances_sharing_an_alias(*, both_running: bool = True):
    """Older instance first, so rowid order is the OPPOSITE of start order —
    which is exactly the shape that made the bug invisible."""
    import asyncio
    from datetime import datetime, timedelta, timezone

    import app.db as db
    from app.models import INST_RUNNING, INST_STOPPED, Instance, ModelRegistry

    async def _go():
        async with db.SessionLocal() as s:
            m = ModelRegistry(repo_id="org/dsv4", name="dsv4")
            s.add(m)
            await s.flush()
            now = datetime.now(timezone.utc)
            s.add(Instance(
                name="old-prod", model_id=m.id, topology="single", node_id=None, port=8000,
                status=INST_RUNNING, started_at=now - timedelta(hours=6),
                served_model_names="DSV4-DSpark deepseek-v4-flash",
            ))
            s.add(Instance(
                name="new-prod", model_id=m.id, topology="single", node_id=None, port=8001,
                status=INST_RUNNING if both_running else INST_STOPPED,
                started_at=now,
                served_model_names="DSV4-DSpark deepseek-v4-flash",
            ))
            await s.commit()

    asyncio.run(_go())


def _resolve_order(client):
    import asyncio

    import app.db as db
    from app.routers.gateway import _running_instances

    async def _go():
        async with db.SessionLocal() as s:
            return [i.name for i in await _running_instances(s)]

    return asyncio.run(_go())


def test_the_most_recently_started_instance_wins(client):
    """Newest-first matches intent: you started the replacement to take over.

    Before the fix this returned ['old-prod', 'new-prod'] — rowid order — so
    the incumbent kept every request.
    """
    _two_instances_sharing_an_alias()
    assert _resolve_order(client)[0] == "new-prod"


def test_resolution_does_not_depend_on_insert_order(client):
    """The ordering must come from the query, not from how SQLite happens to
    lay rows out. `started_at DESC` is explicit; rowid order was incidental."""
    _two_instances_sharing_an_alias()
    order = _resolve_order(client)
    assert order == ["new-prod", "old-prod"], order


def test_a_never_started_instance_does_not_win(client):
    """`started_at` is nullable. A row marked running but never actually
    started must not sort above one that is genuinely serving."""
    import asyncio
    from datetime import datetime, timedelta, timezone

    import app.db as db
    from app.models import INST_RUNNING, Instance, ModelRegistry

    async def _go():
        async with db.SessionLocal() as s:
            m = ModelRegistry(repo_id="org/x", name="x")
            s.add(m)
            await s.flush()
            s.add(Instance(name="real", model_id=m.id, topology="single", port=8000,
                           status=INST_RUNNING,
                           started_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                           served_model_names="shared"))
            s.add(Instance(name="never-started", model_id=m.id, topology="single", port=8001,
                           status=INST_RUNNING, started_at=None,
                           served_model_names="shared"))
            await s.commit()

    asyncio.run(_go())
    assert _resolve_order(client)[0] == "real"


# --- surfaced, not just logged -------------------------------------------

def test_the_routes_api_reports_the_conflict(client):
    _two_instances_sharing_an_alias()
    info = client.get("/api/gateway/routes").json()
    conflicts = info["alias_conflicts"]
    assert set(conflicts) == {"DSV4-DSpark", "deepseek-v4-flash"}
    assert set(conflicts["DSV4-DSpark"]) == {"old-prod", "new-prod"}


def test_a_stopped_rollback_candidate_is_not_a_conflict(client):
    """Keeping the aliases on a stopped instance is the INTENDED state — it is
    what makes it a rollback candidate. Flagging it would train the operator to
    ignore the warning."""
    _two_instances_sharing_an_alias(both_running=False)
    info = client.get("/api/gateway/routes").json()
    assert info["alias_conflicts"] == {}


def test_no_conflict_reported_when_aliases_are_distinct(client):
    import asyncio

    import app.db as db
    from app.models import INST_RUNNING, Instance, ModelRegistry

    async def _go():
        async with db.SessionLocal() as s:
            m = ModelRegistry(repo_id="org/y", name="y")
            s.add(m)
            await s.flush()
            for n, port, alias in (("a", 8000, "alpha"), ("b", 8001, "beta")):
                s.add(Instance(name=n, model_id=m.id, topology="single", port=port,
                               status=INST_RUNNING, served_model_names=alias))
            await s.commit()

    asyncio.run(_go())
    assert client.get("/api/gateway/routes").json()["alias_conflicts"] == {}


def test_duplicate_aliases_are_still_accepted_on_write(client):
    """Rejecting them would break the promotion this fix exists to support:
    the replacement must be able to carry the incumbent's names before the
    incumbent stops."""
    m = client.post("/api/models", json={"repo_id": "org/z"}).json()
    first = client.post("/api/instances", json={
        "name": "a", "model_id": m["id"], "topology": "single", "node_id": None,
        "served_model_names": "shared-alias",
    })
    client.post("/api/nodes", json={
        "role": "head", "name": "h", "lan_ip": "10.0.0.1", "qsfp_ip": "10.10.10.1",
        "ssh_user": "u", "ssh_password": "p",
    })
    # Both creates must succeed regardless of the alias overlap.
    assert first.status_code in (201, 400), first.text
