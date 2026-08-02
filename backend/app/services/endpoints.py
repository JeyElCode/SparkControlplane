"""Named endpoints: the production front door as a thing you can point.

An endpoint owns a hostname, its TLS certificate, and its served-model aliases,
and holds a pointer to whichever instance is currently serving it. Promoting a
new model becomes one guarded action instead of hand-copying a certificate you
cannot read back and replicating aliases by hand on a live endpoint.

Two properties are worth stating outright because they are the reason this
exists rather than being a tidier way to store the same thing:

**An alias owned by an endpoint cannot be ambiguous.** `endpoint_aliases.alias`
is UNIQUE, so the database refuses the situation that used to route production
traffic to an arbitrary instance. Instance-level aliases still exist for
instances outside any endpoint, and those keep the newest-wins rule.

**The private key never becomes readable.** #77 asks to retrieve the current
cert so it can be handed to another instance — but the underlying need is a
*handoff*, and an endpoint that owns the cert performs the handoff without the
key leaving the portal. What is exposed is the certificate's public metadata:
subject, issuer, SANs, fingerprint and validity. Every one of those is sent in
the clear during any TLS handshake, so publishing them costs nothing and
answers the questions an operator actually has.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

log = logging.getLogger("spark.endpoints")

__all__ = [
    "CertInfo",
    "parse_certificate",
    "normalise_aliases",
    "hostname_covered",
    "ALIAS_RE",
]

# Same shape a client may put in an OpenAI `model` field. Deliberately permits
# the slash in "deepseek-ai/DeepSeek-V4-Flash" — that is a real alias in use —
# while excluding whitespace and anything that would need quoting on the vLLM
# command line.
#
# The FIRST character must be alphanumeric, which is not cosmetic: aliases are
# passed to `--served-model-name`, so an alias beginning with a dash would be
# read by the CLI as a flag rather than a value. "--flag" matched an earlier
# version of this pattern, which is the same shape of hole as the profile
# denylist that had to be replaced in v1.26.0 — describe what is VALID, not
# what looks harmless.
ALIAS_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._/-]{0,127}\Z")


class CertInfo:
    """Public metadata read off a PEM certificate. Never carries the key."""

    def __init__(self) -> None:
        self.subject: str | None = None
        self.issuer: str | None = None
        self.sans: list[str] = []
        self.fingerprint_sha256: str | None = None
        self.not_before: datetime | None = None
        self.not_after: datetime | None = None
        self.error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.fingerprint_sha256 is not None

    def days_remaining(self, now: datetime | None = None) -> int | None:
        if self.not_after is None:
            return None
        now = now or datetime.now(timezone.utc)
        na = self.not_after
        if na.tzinfo is None:
            na = na.replace(tzinfo=timezone.utc)
        return (na - now).days


def parse_certificate(pem: str | None) -> CertInfo:
    """Read a PEM certificate's public fields. Never raises.

    A malformed certificate is reported rather than thrown, because this runs
    on an upload path and a bad paste should produce a message, not a 500.
    """
    info = CertInfo()
    if not pem or "BEGIN CERTIFICATE" not in pem:
        info.error = "Not a PEM certificate (expected a BEGIN CERTIFICATE block)."
        return info
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
    except ImportError:  # pragma: no cover - cryptography is a hard dependency
        info.error = "cryptography is unavailable; cannot read the certificate."
        return info

    try:
        cert = x509.load_pem_x509_certificate(pem.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - any parse failure is a bad upload
        info.error = f"Could not read the certificate: {exc}"
        return info

    try:
        info.subject = cert.subject.rfc4514_string()[:255]
        info.issuer = cert.issuer.rfc4514_string()[:255]
    except Exception:  # noqa: BLE001
        pass

    try:
        # Colon-separated, matching what `openssl x509 -fingerprint` prints, so
        # an operator can compare it against what they have on disk.
        digest = cert.fingerprint(hashes.SHA256())
        info.fingerprint_sha256 = ":".join(f"{b:02X}" for b in digest)
    except Exception:  # noqa: BLE001
        pass

    # not_valid_before_utc replaced the naive property in cryptography 42;
    # fall back so an older wheel still yields dates rather than nothing.
    for attr, target in (("not_valid_before", "not_before"), ("not_valid_after", "not_after")):
        value = getattr(cert, f"{attr}_utc", None) or getattr(cert, attr, None)
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            setattr(info, target, value)

    try:
        ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        info.sans = [str(n) for n in ext.value.get_values_for_type(x509.DNSName)]
    except Exception:  # noqa: BLE001 - a cert with no SAN is unusual, not fatal
        info.sans = []

    return info


def hostname_covered(info: CertInfo, hostname: str) -> bool:
    """Does this certificate actually cover the endpoint's hostname?

    Checked at upload so the mismatch surfaces then, rather than as a browser
    warning after a promotion. Handles the single-label wildcard form, which is
    what real certs use.
    """
    if not hostname:
        return False
    host = hostname.lower().rstrip(".")
    names = [n.lower().rstrip(".") for n in info.sans]
    # Fall back to the subject CN only when there is no SAN at all; a cert WITH
    # SANs is defined by them, and browsers ignore the CN in that case.
    if not names and info.subject:
        for part in info.subject.split(","):
            if part.strip().upper().startswith("CN="):
                names = [part.strip()[3:].lower()]
                break
    for name in names:
        if name == host:
            return True
        if name.startswith("*."):
            # A wildcard matches exactly one label, so *.a.com covers b.a.com
            # but not c.b.a.com.
            suffix = name[1:]
            if host.endswith(suffix) and host.count(".") == name.count("."):
                return True
    return False


def normalise_aliases(raw) -> tuple[list[str], list[str]]:
    """Clean an alias list. Returns (accepted, rejected).

    Accepts a list or the legacy space/newline-separated string, so an existing
    instance's `served_model_names` can be moved onto an endpoint unchanged.
    Order is preserved and duplicates collapse to the first occurrence, because
    the order reaches the vLLM `--served-model-name` flag.
    """
    if raw is None:
        return [], []
    items: list[str]
    if isinstance(raw, str):
        items = raw.split()
    elif isinstance(raw, (list, tuple)):
        items = []
        for entry in raw:
            items.extend(str(entry).split())
    else:
        return [], [str(raw)]

    accepted: list[str] = []
    rejected: list[str] = []
    seen: set[str] = set()
    for item in items:
        item = item.strip()
        if not item:
            continue
        if not ALIAS_RE.match(item):
            rejected.append(item)
            continue
        if item in seen:
            continue
        seen.add(item)
        accepted.append(item)
    return accepted, rejected


def aliases_json(aliases: list[str]) -> str:
    return json.dumps(aliases)


# --- promotion ------------------------------------------------------------
# Everything below runs as a background JOB, never a request. The handoff is
# stop-current -> start-target -> wait for a weight load that takes minutes ->
# verify -> flip the pointer. A synchronous HTTP call cannot survive that, and
# an operator closing a browser tab must not abandon a half-swapped endpoint.

async def promote(handle, endpoint_id: int, instance_id: int, reason: str | None = None) -> str:
    """Point an endpoint at a different instance.

    The ordering is forced by the hardware, not chosen: prod-class instances are
    TP=2 across the whole box, so the outgoing one MUST stop before the incoming
    one can start. That means there is a window — minutes long, the weight load
    — where the endpoint serves nothing. This function's job is to make that
    window as short as it can be, to never leave it open silently, and to put
    the previous instance back if the new one does not come up.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from ..db import SessionLocal
    from ..models import (
        INST_RUNNING,
        PROMO_ACTIVE,
        PROMO_FAILED,
        PROMO_PENDING,
        PROMO_SUPERSEDED,
        TERM_K8S,
        Endpoint,
        EndpointPromotion,
        Instance,
    )
    from . import inst_state
    from .instances import start_instance, stop_instance

    async with SessionLocal() as session:
        # selectinload, not session.get: the alias list is read below, and
        # touching the relationship lazily emits IO outside the greenlet the
        # async layer runs in (MissingGreenlet). That would fail on the first
        # real promotion, which is the worst possible moment to discover it.
        endpoint = (
            await session.execute(
                select(Endpoint)
                .options(selectinload(Endpoint.aliases))
                .where(Endpoint.id == endpoint_id)
            )
        ).scalar_one_or_none()
        if endpoint is None:
            raise RuntimeError("Endpoint not found.")
        # Same reason as the endpoint above: `target.model.name` is read when
        # the history row is written, and a lazy load there would raise
        # MissingGreenlet mid-promotion.
        target = (
            await session.execute(
                select(Instance)
                .options(selectinload(Instance.model))
                .where(Instance.id == instance_id)
            )
        ).scalar_one_or_none()
        if target is None:
            raise RuntimeError("Target instance not found.")

        previous_id = endpoint.current_instance_id
        previous = await session.get(Instance, previous_id) if previous_id else None

        if previous_id == instance_id and target.status == INST_RUNNING:
            return f"'{target.name}' already serves {endpoint.name} and is running."

        # Refuse rather than half-swap. Each of these would otherwise be
        # discovered with production already stopped.
        if target.endpoint_id != endpoint.id:
            raise RuntimeError(
                f"'{target.name}' is not a member of endpoint '{endpoint.name}'. "
                "Add it to the endpoint first so it launches with the right "
                "aliases and certificate."
            )
        if inst_state.in_flight(instance_id):
            raise RuntimeError(f"'{target.name}' is busy with another operation.")
        if previous_id and inst_state.in_flight(previous_id):
            raise RuntimeError(
                f"'{previous.name if previous else previous_id}' is busy with "
                "another operation."
            )
        if (
            endpoint.termination != TERM_K8S
            and not endpoint.tls_cert_enc
            and endpoint.port == 443
        ):
            raise RuntimeError(
                f"Endpoint '{endpoint.name}' has no certificate, so it cannot "
                "serve HTTPS on :443. Upload one first."
            )

        aliases = [a.alias for a in endpoint.aliases]
        promo = EndpointPromotion(
            endpoint_id=endpoint.id, endpoint_name=endpoint.name,
            to_instance_id=target.id, to_instance_name=target.name,
            to_model_name=(target.model.name if target.model else ""),
            from_instance_id=previous.id if previous else None,
            from_instance_name=previous.name if previous else None,
            status=PROMO_PENDING, reason=reason,
            aliases_snapshot=aliases_json(aliases),
            cert_fingerprint=endpoint.tls_fingerprint_sha256,
            job_id=handle.job_id,
        )
        session.add(promo)
        await session.commit()

        await handle.log(
            f"Promoting '{target.name}' to endpoint '{endpoint.name}' "
            f"({endpoint.hostname}:{endpoint.port})"
        )
        if previous is not None:
            await handle.log(
                f"'{previous.name}' currently serves it and will be stopped first "
                "— the endpoint is unavailable until the new instance finishes "
                "loading. It is NOT deleted, so rolling back is a promote in "
                "the other direction."
            )

        try:
            if previous is not None and previous.status == INST_RUNNING:
                await handle.log(f"Stopping '{previous.name}'…")
                await stop_instance(session, handle, previous.id)

            await handle.log(f"Starting '{target.name}' with {len(aliases)} alias(es)…")
            await start_instance(session, handle, target.id)

        except Exception as exc:  # noqa: BLE001 - every failure is recoverable-ish
            promo.status = PROMO_FAILED
            promo.finished_at = _utcnow_naive()
            await session.commit()
            # Production is down at this point and that is the only thing that
            # matters. Try to put the previous instance back before reporting.
            if previous is not None:
                await handle.log(
                    f"'{target.name}' did not come up: {exc}. "
                    f"Restoring '{previous.name}'…", "error",
                )
                try:
                    await start_instance(session, handle, previous.id)
                    raise RuntimeError(
                        f"Promotion failed ({exc}). '{previous.name}' has been "
                        f"restarted and serves '{endpoint.name}' again."
                    ) from exc
                except Exception as restore_exc:  # noqa: BLE001
                    raise RuntimeError(
                        f"Promotion failed ({exc}) AND the previous instance "
                        f"'{previous.name}' could not be restarted "
                        f"({restore_exc}). ENDPOINT '{endpoint.name}' IS DOWN — "
                        "start an instance manually from the Instances page."
                    ) from exc
            raise RuntimeError(
                f"Promotion failed and there was no previous instance to "
                f"restore. Endpoint '{endpoint.name}' is not being served: {exc}"
            ) from exc

        # Only now does the endpoint point at the new instance. Flipping before
        # the target is up would make the API claim a handoff that had not
        # happened.
        for row in (
            await session.execute(
                select(EndpointPromotion).where(
                    EndpointPromotion.endpoint_id == endpoint.id,
                    EndpointPromotion.status == PROMO_ACTIVE,
                )
            )
        ).scalars():
            row.status = PROMO_SUPERSEDED

        endpoint.current_instance_id = target.id
        endpoint.promoted_at = _utcnow_naive()
        promo.status = PROMO_ACTIVE
        promo.finished_at = _utcnow_naive()
        await session.commit()

        await handle.log(f"'{endpoint.name}' now served by '{target.name}'.")
        return f"Promoted '{target.name}' to '{endpoint.name}'."


def _utcnow_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def previous_holder(session, endpoint_id: int) -> int | None:
    """The instance that served this endpoint before the current one.

    Read off the CURRENT promotion's `from_instance_id`, not from a superseded
    row. An earlier version looked for the most recent superseded row, which
    meant rollback silently found nothing after the FIRST promotion — the only
    row was the active one, and nothing had been superseded yet. That is
    precisely the case rollback exists for.

    Rollback is not a distinct operation; it is a promote aimed backwards, so
    this only has to answer "backwards to what".
    """
    from sqlalchemy import select

    from ..models import PROMO_ACTIVE, EndpointPromotion

    row = (
        await session.execute(
            select(EndpointPromotion)
            .where(
                EndpointPromotion.endpoint_id == endpoint_id,
                EndpointPromotion.status == PROMO_ACTIVE,
            )
            .order_by(EndpointPromotion.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return row.from_instance_id if row else None
