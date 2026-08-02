"""Every job-dispatching endpoint must return a response that validates.

Issue #94: `POST /api/endpoints/{name}/promote` returned 500 while the
promotion ran to completion. `JobAccepted` requires `message`, and the promote
and rollback handlers were the only two of twenty call sites that did not pass
it — so FastAPI raised constructing the response, *after* the background job
had already been dispatched.

That combination is the nastiest shape a bug can take on a production endpoint:
the operator sees a 500, has no `job_id` to follow, concludes the promotion
failed, and takes a recovery action against a live endpoint that in fact
already swapped correctly.

It survived because `test_promote.py` covers the decision-making by calling the
service directly with `start_instance`/`stop_instance` stubbed. Excellent
coverage of what promote *decides*, zero coverage of what the router *returns* —
the response model was never constructed in a test. So the tests below go
through HTTP, and the last one guards the whole class rather than the two
instances of it.
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
    from fastapi.testclient import TestClient

    with TestClient(main.app) as c:
        yield c
    config.get_settings.cache_clear()


def _endpoint_with_two_members(client):
    """A promotable endpoint. No node is registered, so the job will fail once
    it runs — irrelevant here: the response is constructed at dispatch, which
    is exactly where #94 blew up."""
    client.post("/api/endpoints", json={
        "name": "prod", "hostname": "llm.example.net", "aliases": ["DSV4"],
        "termination": "k8s", "upstream_port": 8443,
    })
    ep_id = client.get("/api/endpoints").json()[0]["id"]
    m = client.post("/api/models", json={"repo_id": "org/dsv4"}).json()
    ids = {}
    for name in ("id7", "id9"):
        r = client.post("/api/instances", json={
            "name": name, "model_id": m["id"], "topology": "cluster",
            "endpoint_id": ep_id,
        })
        assert r.status_code == 201, r.text
        ids[name] = r.json()["id"]
    return ep_id, ids


def test_promote_returns_a_valid_response_not_a_500(client):
    """The exact call from #94."""
    _ep, ids = _endpoint_with_two_members(client)
    r = client.post("/api/endpoints/prod/promote", json={"instance_id": ids["id9"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body["job_id"], int)
    assert body["message"]


def test_the_caller_gets_a_job_id_it_can_actually_follow(client):
    """The 500 was worse than a failed promotion: the job ran, and the caller
    had no handle on it. Whatever comes back must address a real job."""
    _ep, ids = _endpoint_with_two_members(client)
    job_id = client.post(
        "/api/endpoints/prod/promote", json={"instance_id": ids["id9"]}
    ).json()["job_id"]
    assert client.get(f"/api/jobs/{job_id}").status_code == 200


def test_rollback_returns_a_valid_response(client):
    """Same handler shape, same omission, and it shares the promote path — so
    it was broken identically and needs its own assertion."""
    import asyncio

    from sqlalchemy import select

    import app.db as db
    from app.models import PROMO_ACTIVE, Endpoint, EndpointPromotion

    _ep, ids = _endpoint_with_two_members(client)

    async def _seed_history():
        async with db.SessionLocal() as s:
            ep = (await s.execute(select(Endpoint))).scalars().one()
            s.add(EndpointPromotion(
                endpoint_id=ep.id, endpoint_name="prod",
                to_instance_id=ids["id9"], to_instance_name="id9",
                from_instance_id=ids["id7"], from_instance_name="id7",
                status=PROMO_ACTIVE,
            ))
            ep.current_instance_id = ids["id9"]
            await s.commit()

    asyncio.run(_seed_history())

    r = client.post("/api/endpoints/prod/rollback")
    assert r.status_code == 200, r.text
    assert isinstance(r.json()["job_id"], int)
    assert r.json()["message"]


def test_a_refusal_is_still_a_clean_error_not_a_500(client):
    """The guards in front of promote must return their own status codes; a
    500 here would hide the reason the same way #94 hid a success."""
    _ep, _ids = _endpoint_with_two_members(client)
    assert client.post(
        "/api/endpoints/prod/promote", json={"instance_id": 9999}
    ).status_code == 404
    assert client.post("/api/endpoints/prod/rollback").status_code == 409


# --- the class, not the instance ------------------------------------------

def test_no_handler_constructs_JobAccepted_without_a_message():
    """A static sweep of every call site.

    Two of twenty were wrong for three releases, and nothing failed until a
    real promotion hit it in production. Router tests cover the endpoints that
    are easy to drive; this covers the ones that are not — a delete, a
    teardown, a power cycle — without needing a node to be reachable.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    offenders = []
    for path in root.rglob("*.py"):
        src = path.read_text()
        # `class JobAccepted(BaseModel)` is a declaration, not a construction.
        for match in re.finditer(r"(?<!class )JobAccepted\((.*?)\)", src, re.DOTALL):
            args = match.group(1)
            if args.strip() == "BaseModel" or "message" in args:
                continue
            line = src[: match.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(root.parent)}:{line}")
    assert not offenders, (
        "JobAccepted requires `message`; these construct it without one, which "
        "raises when FastAPI builds the response AFTER the job has already been "
        f"dispatched: {', '.join(offenders)}"
    )
