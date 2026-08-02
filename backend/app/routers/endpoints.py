"""Named endpoints: CRUD, certificate upload, promote and rollback.

Promotion is a JOB, never a synchronous request. The handoff is stop-current →
start-target → wait out a weight load measured in minutes → verify → flip the
pointer, and an operator closing a browser tab must not abandon a half-swapped
production endpoint.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..crypto import encrypt
from ..db import get_session
from ..models import (
    TERM_K8S,
    Endpoint,
    EndpointAlias,
    EndpointPromotion,
    Instance,
    Node,
)
from ..schemas import (
    EndpointIn,
    EndpointOut,
    EndpointPromotionOut,
    EndpointUpdate,
    JobAccepted,
    PromoteIn,
    TlsUploadIn,
)
from ..services import endpoints as ep_svc, k8sman
from ..services.jobs import jobs

router = APIRouter(prefix="/api/endpoints", tags=["endpoints"])


async def _load(session: AsyncSession, name: str) -> Endpoint:
    row = (
        await session.execute(
            select(Endpoint)
            .options(selectinload(Endpoint.aliases))
            .where(Endpoint.name == name)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, f"Endpoint '{name}' not found")
    return row


async def _set_aliases(
    session: AsyncSession, endpoint: Endpoint, raw, *, is_new: bool = False
) -> None:
    """Replace the alias set, rejecting a name another endpoint already owns.

    The uniqueness is enforced by the database too, but catching it here turns
    an opaque IntegrityError into a message naming the conflicting endpoint.
    """
    accepted, rejected = ep_svc.normalise_aliases(raw)
    if rejected:
        raise HTTPException(
            400,
            f"Not usable as model aliases: {', '.join(rejected)}. Use letters, "
            "digits, dot, underscore, slash or dash, starting with a letter or "
            "digit.",
        )
    if not accepted:
        raise HTTPException(400, "An endpoint needs at least one alias.")

    taken = (
        await session.execute(
            select(EndpointAlias, Endpoint.name)
            .join(Endpoint, Endpoint.id == EndpointAlias.endpoint_id)
            .where(
                EndpointAlias.alias.in_(accepted),
                EndpointAlias.endpoint_id != endpoint.id,
            )
        )
    ).all()
    if taken:
        clash = ", ".join(f"{a.alias} (owned by '{n}')" for a, n in taken)
        raise HTTPException(409, f"Already in use: {clash}")

    if not is_new:
        # Touching the relationship lazily would emit IO outside the greenlet
        # SQLAlchemy's async layer runs in; the caller loads it with selectinload.
        for existing in list(endpoint.aliases):
            await session.delete(existing)
        await session.flush()
    for i, alias in enumerate(accepted):
        session.add(EndpointAlias(endpoint_id=endpoint.id, alias=alias, position=i))


def _apply_cert(endpoint: Endpoint, cert_pem: str, key_pem: str) -> None:
    """Store a certificate and its parsed public metadata.

    The key is encrypted and never read back out. The metadata is stored in the
    clear because all of it is transmitted during any TLS handshake.
    """
    info = ep_svc.parse_certificate(cert_pem)
    if not info.ok:
        raise HTTPException(400, info.error or "Unusable certificate.")
    if "PRIVATE KEY" not in (key_pem or ""):
        raise HTTPException(400, "The private key does not look like a PEM key.")
    if not ep_svc.hostname_covered(info, endpoint.hostname):
        raise HTTPException(
            400,
            f"This certificate does not cover '{endpoint.hostname}' "
            f"(it covers: {', '.join(info.sans) or info.subject or 'nothing'}). "
            "Clients would reject it, so it is refused here rather than after a "
            "promotion.",
        )

    endpoint.tls_cert_enc = encrypt(cert_pem)
    endpoint.tls_key_enc = encrypt(key_pem)
    endpoint.tls_subject = info.subject
    endpoint.tls_issuer = info.issuer
    endpoint.tls_sans_json = ep_svc.aliases_json(info.sans)
    endpoint.tls_fingerprint_sha256 = info.fingerprint_sha256
    endpoint.tls_not_before = _naive(info.not_before)
    endpoint.tls_not_after = _naive(info.not_after)
    endpoint.tls_uploaded_at = ep_svc._utcnow_naive()


def _naive(dt):
    return dt.replace(tzinfo=None) if dt is not None else None


@router.get("", response_model=list[EndpointOut])
async def list_endpoints(session: AsyncSession = Depends(get_session)):
    rows = (
        await session.execute(
            select(Endpoint).options(selectinload(Endpoint.aliases)).order_by(Endpoint.name)
        )
    ).scalars().all()
    return [await EndpointOut.of(r, session) for r in rows]


@router.post("", response_model=EndpointOut, status_code=201)
async def create_endpoint(payload: EndpointIn, session: AsyncSession = Depends(get_session)):
    if (
        await session.execute(select(Endpoint).where(Endpoint.name == payload.name))
    ).scalar_one_or_none() is not None:
        raise HTTPException(409, f"An endpoint named '{payload.name}' already exists.")

    endpoint = Endpoint(
        name=payload.name, hostname=payload.hostname, port=payload.port,
        termination=payload.termination, upstream_port=payload.upstream_port,
        description=payload.description,
    )
    session.add(endpoint)
    await session.flush()
    await _set_aliases(session, endpoint, payload.aliases, is_new=True)
    if payload.tls_cert and payload.tls_key:
        _apply_cert(endpoint, payload.tls_cert, payload.tls_key)
    await session.commit()
    return await EndpointOut.of(await _load(session, payload.name), session)


@router.patch("/{name}", response_model=EndpointOut)
async def update_endpoint(
    name: str, payload: EndpointUpdate, session: AsyncSession = Depends(get_session)
):
    endpoint = await _load(session, name)
    data = payload.model_dump(exclude_unset=True)
    aliases = data.pop("aliases", None)
    for field, value in data.items():
        setattr(endpoint, field, value)
    if endpoint.termination == TERM_K8S and not endpoint.upstream_port:
        raise HTTPException(
            400,
            "A Kubernetes-terminated endpoint needs an upstream_port — it is "
            "the port every member binds, and pinning it is what keeps the "
            "cluster manifests correct across a promotion.",
        )
    if aliases is not None:
        # Aliases reach vLLM via --served-model-name at LAUNCH, so an edit does
        # not take effect until the serving instance restarts. Said plainly
        # rather than letting the operator infer it from traffic.
        await _set_aliases(session, endpoint, aliases)
    await session.commit()
    return await EndpointOut.of(await _load(session, name), session)


@router.post("/{name}/tls", response_model=EndpointOut)
async def upload_tls(
    name: str, payload: TlsUploadIn, session: AsyncSession = Depends(get_session)
):
    """Replace the certificate. The key is write-only and never read back."""
    endpoint = await _load(session, name)
    _apply_cert(endpoint, payload.tls_cert, payload.tls_key)
    await session.commit()
    return await EndpointOut.of(await _load(session, name), session)


@router.delete("/{name}", status_code=204)
async def delete_endpoint(name: str, session: AsyncSession = Depends(get_session)):
    endpoint = await _load(session, name)
    if endpoint.current_instance_id is not None:
        raise HTTPException(
            409,
            f"'{name}' is currently served by an instance. Stop it first — "
            "deleting a live endpoint would strand the instance advertising "
            "its aliases.",
        )
    await session.delete(endpoint)
    await session.commit()


@router.get("/{name}/history", response_model=list[EndpointPromotionOut])
async def endpoint_history(name: str, session: AsyncSession = Depends(get_session)):
    endpoint = await _load(session, name)
    rows = (
        await session.execute(
            select(EndpointPromotion)
            .where(EndpointPromotion.endpoint_id == endpoint.id)
            .order_by(EndpointPromotion.started_at.desc())
            .limit(100)
        )
    ).scalars().all()
    return [EndpointPromotionOut.model_validate(r, from_attributes=True) for r in rows]


@router.get("/{name}/manifests", response_class=PlainTextResponse)
async def endpoint_manifests(
    name: str,
    namespace: str = "default",
    issuer: str = "letsencrypt-prod",
    issuer_kind: str = "ClusterIssuer",
    ingress_class: str = "nginx",
    session: AsyncSession = Depends(get_session),
):
    """The Kubernetes manifests for a `k8s`-terminated endpoint, as YAML.

    Returned for the operator to commit, NOT applied. The portal holds no
    cluster credentials by design — it describes what it wants and existing
    GitOps applies it. Nothing here needs re-applying after a promotion: every
    member serves from the head node on the same pinned port, so a handoff is
    invisible from the cluster's side.
    """
    endpoint = await _load(session, name)
    if endpoint.termination != TERM_K8S:
        raise HTTPException(
            409,
            f"'{name}' terminates TLS on the serving node, so it needs no "
            "Kubernetes manifests. Set termination to 'k8s' to move HTTPS into "
            "the cluster.",
        )
    head = (
        await session.execute(select(Node).where(Node.role == "head"))
    ).scalar_one_or_none()
    if head is None:
        raise HTTPException(
            409,
            "No head node is configured, so there is no upstream address to "
            "point the cluster at.",
        )
    try:
        return k8sman.render(
            k8sman.ManifestInput(
                endpoint=endpoint.name,
                hostname=endpoint.hostname,
                # The head serves the API for cluster and distributed
                # topologies, and a single-node member of an endpoint is
                # pinned to it as well — one address for every member is what
                # makes the manifest static.
                upstream_ip=head.lan_ip,
                upstream_port=endpoint.upstream_port or endpoint.port,
                namespace=namespace,
                issuer=issuer,
                issuer_kind=issuer_kind,
                ingress_class=ingress_class,
            )
        )
    except k8sman.ManifestError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/{name}/promote", response_model=JobAccepted)
async def promote_endpoint(
    name: str, payload: PromoteIn, session: AsyncSession = Depends(get_session)
):
    endpoint = await _load(session, name)
    target = await session.get(Instance, payload.instance_id)
    if target is None:
        raise HTTPException(404, "Instance not found")
    if target.endpoint_id != endpoint.id:
        raise HTTPException(
            400,
            f"'{target.name}' is not a member of '{name}'. Set its endpoint "
            "first so it launches with the right aliases and certificate.",
        )

    endpoint_id, instance_id, reason = endpoint.id, target.id, payload.reason

    async def coro(h):
        return await ep_svc.promote(h, endpoint_id, instance_id, reason)

    job_id = await jobs.start(
        "endpoint.promote", f"Promote {target.name} to {name}", coro, target=name
    )
    return JobAccepted(job_id=job_id)


@router.post("/{name}/rollback", response_model=JobAccepted)
async def rollback_endpoint(name: str, session: AsyncSession = Depends(get_session)):
    """Point the endpoint back at whatever served it before.

    Not a distinct operation — a promote aimed backwards. That is why the
    outgoing instance is only ever stopped, never deleted.
    """
    endpoint = await _load(session, name)
    previous_id = await ep_svc.previous_holder(session, endpoint.id)
    if previous_id is None:
        raise HTTPException(409, f"'{name}' has nothing to roll back to.")
    previous = await session.get(Instance, previous_id)
    if previous is None:
        raise HTTPException(
            409,
            "The instance that previously served this endpoint has been "
            "deleted, so there is nothing to roll back to.",
        )

    endpoint_id = endpoint.id

    async def coro(h):
        return await ep_svc.promote(h, endpoint_id, previous_id, "rollback")

    job_id = await jobs.start(
        "endpoint.promote", f"Roll {name} back to {previous.name}", coro, target=name
    )
    return JobAccepted(job_id=job_id)
