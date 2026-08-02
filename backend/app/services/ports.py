"""Instance port allocation & conflict validation.

Since the /v1 gateway became the entry point, ports are internal plumbing —
so they auto-assign unless the operator insists. Auto-assignment is globally
unique across all instances (simple, zero surprises); explicitly-chosen ports
are validated only against instances that actually bind the same host (the
serving node), so e.g. two singles pinned to different nodes may share a port
on purpose.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_node_by_role
from ..models import TOPO_SINGLE, Instance
from .binding import effective_port

API_PORT_START = 8000
MASTER_PORT_START = 29500
# never auto-assign onto infrastructure ports (Ray GCS, Ray dashboard, portal)
RESERVED = {6379, 8080, 8265}


async def _serving_node_id(session: AsyncSession, topology: str, node_id: int | None) -> int:
    """The node whose network namespace the API port binds on. -1 = head not
    configured yet (treated as overlapping with everything head-bound)."""
    if topology == TOPO_SINGLE:
        return node_id if node_id is not None else -1
    head = await get_node_by_role(session, "head")
    return head.id if head is not None else -1


async def allocate_api_port(session: AsyncSession) -> int:
    taken = set(RESERVED)
    for inst in (await session.execute(select(Instance))).scalars():
        taken.add(inst.port)
        # An endpoint member binds the endpoint's pinned port, not its own, so
        # that is the number an auto-assignment must avoid. Skipping it would
        # hand a fresh instance a port a member already listens on.
        taken.add(effective_port(inst))
        if inst.tls_enabled:
            taken.add(inst.tls_port)
    port = API_PORT_START
    while port in taken:
        port += 1
    return port


async def allocate_master_port(session: AsyncSession) -> int:
    # only distributed instances actually bind their master_port; others just
    # carry the column default
    taken = {
        inst.master_port
        for inst in (await session.execute(select(Instance))).scalars()
        if inst.topology == "distributed"
    }
    port = MASTER_PORT_START
    while port in taken:
        port += 1
    return port


async def port_conflict(
    session: AsyncSession,
    port: int,
    topology: str,
    node_id: int | None,
    exclude_id: int | None = None,
    endpoint_id: int | None = None,
) -> str | None:
    """Human-readable conflict message when an explicit port collides with
    another instance's vLLM or TLS port on the same serving node, else None.

    ``endpoint_id`` is the endpoint the candidate belongs to. On an edit it is
    read from the row; on a create there is no row yet, so the caller passes
    the value from the payload. Without it a new member would be rejected for
    colliding with the very instance it is meant to replace.
    """
    scope = await _serving_node_id(session, topology, node_id)
    my_endpoint = endpoint_id
    if my_endpoint is None and exclude_id:
        mine = await session.get(Instance, exclude_id)
        my_endpoint = mine.endpoint_id if mine is not None else None
    for other in (await session.execute(select(Instance))).scalars():
        if other.id == exclude_id:
            continue
        # Two members of the same endpoint may claim the same port. They are
        # mutually exclusive by construction — an endpoint has one serving
        # instance and promote stops the outgoing one before starting the
        # incoming one — and sharing the port is the whole point: it lets an
        # external proxy keep a static upstream across a promotion.
        if (
            my_endpoint is not None
            and other.endpoint_id == my_endpoint
        ):
            continue
        other_scope = await _serving_node_id(session, other.topology, other.node_id)
        if scope != other_scope and -1 not in (scope, other_scope):
            continue
        if effective_port(other) == port:
            return f"Port {port} is already used by instance '{other.name}'."
        if other.tls_enabled and other.tls_port == port:
            return f"Port {port} is already used as the TLS port of instance '{other.name}'."
    if port in RESERVED:
        return f"Port {port} is reserved for cluster infrastructure."
    return None
