"""Node certificates from OpenBao's PKI engine, with the portal as the client.

The hop this exists for: since #86 the nginx that terminates public HTTPS runs
in Kubernetes, so the connection from that proxy to vLLM on a DGX node crosses
the management LAN. Before #86 it was loopback. Today it carries every prompt
and completion in the clear — and, because `instance_auth_headers` sends
`Authorization: Bearer`, the instance API key travels with them. That makes it
a credential exposure, not only a confidentiality one.

**Why direct issuance and not ACME.** OpenBao does ship a real RFC 8555 ACME
server, and it is the wrong tool here for a reason that is not a matter of
taste: ACME-issued certificates are capped at 90 days by a compile-time
constant (`maxAcmeCertTTL = 90 * 24h`), and a client may not request a lifetime
at all — order parameters carrying NotBefore/NotAfter are rejected outright.
Short-lived, frequently-rotated certificates are exactly what ACME denies us.

The deeper reason is that ACME's challenges exist to prove control of a name.
The portal does not need to prove that: it already holds authenticated SSH to
the node (host keys pinned since v1.29.0) and an OpenBao credential. Running
http-01 would mean opening port 80 on a GPU node to demonstrate something
already demonstrated. `pki/sign/<role>` asks for the same certificate with the
authentication we already have, and lets us choose the lifetime.

`sign`, specifically — never `issue`. `pki/issue/<role>` has OpenBao generate
the private key and return it over the network, which is the one thing this
design exists to avoid.

ACME remains the right answer for the *public* certificate in the cluster,
where cert-manager proves control of a name we really do have to prove.

**The private key never leaves the node.** It is generated there and only a CSR
comes back. This is not ceremony: `nodes` is in the backup bundle, so a
portal-held node key would be written to `Node.*_enc` and shipped offsite in
every scheduled S3 backup.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger("spark.pki")

__all__ = [
    "DEFAULT_TTL_HOURS",
    "MIN_TTL_HOURS",
    "MAX_TTL_HOURS",
    "LifetimePolicy",
    "lifetime_policy",
    "PkiError",
    "SignedCert",
    "fetch_ca_chain",
    "normalise_fqdn",
    "renew_after_hours",
    "sign_csr",
    "validate_ttl_hours",
]

# The operator picks the lifetime; these are the rails it runs on.
#
# 7 days is the default because the two obvious answers are both wrong. A
# 24-hour certificate makes OpenBao a hard availability dependency of public
# inference on a 12-hour fuse — seal it for a Friday patch window without
# auto-unseal and inference stops before Monday. A 30-day certificate leaves a
# month of undetectable interception, and on this hop lifetime IS the
# revocation mechanism: ingress-nginx emits no CRL or OCSP directive on the
# upstream path, so revoking in OpenBao changes nothing until the cert expires.
#
# At 7 days with renewal at a third of life remaining, there are ~2.3 days of
# retry headroom before anything breaks, and a compromised key is useful for at
# most a week.
DEFAULT_TTL_HOURS = 168.0        # 7 days

# Below this the renewal loop cannot keep ahead of its own tick interval, and
# an operator who picks it has built an outage generator rather than a security
# control. Refused rather than clamped: silently issuing something other than
# what was asked for is how you end up trusting a number that was never true.
MIN_TTL_HOURS = 6.0

# OpenBao's own role `max_ttl` is the real ceiling and will reject anything
# above it at issuance time. This bound exists so the refusal happens in the
# settings form, with an explanation, instead of at 3am in a renewal job.
MAX_TTL_HOURS = 8760.0           # 1 year

# Renew when a third of the lifetime remains. Two thirds elapsed is the
# standard fraction: long enough that renewals are infrequent, short enough
# that a failing renewal has time to be noticed and retried many times.
RENEW_AT_FRACTION_REMAINING = 1.0 / 3.0

# How far below the ideal we still consider healthy before shouting. Renewal is
# attempted every tick once inside the window, so a single failure is normal
# and uninteresting; running out of window is not.
WARN_AT_FRACTION_REMAINING = 1.0 / 6.0


@dataclass(frozen=True)
class LifetimePolicy:
    """The full renewal schedule implied by one chosen lifetime."""

    ttl_hours: float
    renew_after_hours: float     # age at which renewal starts being attempted
    warn_after_hours: float      # age at which a failing renewal becomes an alert
    retry_window_hours: float    # how long renewal may keep failing before expiry

    def as_bao_ttl(self) -> str:
        """OpenBao wants a Go duration string. Hours keep it exact for every
        value the settings form can produce."""
        return f"{int(round(self.ttl_hours))}h"


def lifetime_policy(ttl_hours: float | None) -> LifetimePolicy:
    """Derive the renewal schedule from the operator's chosen lifetime.

    Everything scales with the lifetime rather than being fixed, so an operator
    who chooses 24-hour certificates gets 8-hour renewal and an operator who
    chooses 30 days gets 10-day renewal, without either having to reason about
    a second number.
    """
    ttl = validate_ttl_hours(ttl_hours)
    renew_after = ttl * (1.0 - RENEW_AT_FRACTION_REMAINING)
    warn_after = ttl * (1.0 - WARN_AT_FRACTION_REMAINING)
    return LifetimePolicy(
        ttl_hours=ttl,
        renew_after_hours=renew_after,
        warn_after_hours=warn_after,
        retry_window_hours=ttl - renew_after,
    )


def validate_ttl_hours(ttl_hours: float | None) -> float:
    """The chosen lifetime, or the default. Raises ValueError with a reason.

    Refuses rather than clamps. An operator who asks for one hour and silently
    receives six has a system whose behaviour does not match its configuration,
    which is worse than an error message.
    """
    if ttl_hours is None:
        return DEFAULT_TTL_HOURS
    try:
        ttl = float(ttl_hours)
    except (TypeError, ValueError):
        raise ValueError(f"{ttl_hours!r} is not a number of hours.") from None
    if ttl != ttl or ttl in (float("inf"), float("-inf")):
        raise ValueError("Certificate lifetime must be a finite number of hours.")
    if ttl < MIN_TTL_HOURS:
        raise ValueError(
            f"A {_pretty(ttl)} certificate lifetime is too short to renew "
            f"reliably — renewal would start with only {_pretty(ttl / 3)} of "
            f"headroom, which is less than the time a node reboot can take. "
            f"The minimum is {_pretty(MIN_TTL_HOURS)}."
        )
    if ttl > MAX_TTL_HOURS:
        raise ValueError(
            f"{_pretty(ttl)} is longer than the maximum of "
            f"{_pretty(MAX_TTL_HOURS)}. This hop has no revocation — the "
            "cluster proxy checks no CRL and no OCSP — so the certificate "
            "lifetime is the only limit on how long a stolen key stays useful."
        )
    return ttl


def renew_after_hours(ttl_hours: float | None) -> float:
    return lifetime_policy(ttl_hours).renew_after_hours


def _pretty(hours: float) -> str:
    if hours >= 48:
        days = hours / 24.0
        return f"{days:.0f} day" + ("s" if round(days) != 1 else "")
    if hours >= 1:
        return f"{hours:.0f} hour" + ("s" if round(hours) != 1 else "")
    return f"{hours * 60:.0f} minutes"


# --- what a node's certificate must attest to -----------------------------

# The SAN the cluster proxy checks. nginx verifies the upstream certificate
# with X509_check_host, which compares against dNSName SANs ONLY and never
# consults the address it connected to — so a node's certificate must carry its
# DNS name, and an IP SAN would be inert here no matter what is in it.
_FQDN_RE = re.compile(
    r"\A[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?(\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)+\Z"
)


def normalise_fqdn(value: str | None) -> str | None:
    """A node's DNS name, lowercased, or None.

    Requires at least one dot. A single-label name would be accepted by
    OpenBao's `enforce_hostnames` but cannot be resolved by the cluster, and
    the failure would appear as an upstream connect error rather than anything
    naming the cause.
    """
    if value is None:
        return None
    v = value.strip().lower().rstrip(".")
    if not v:
        return None
    # An IP literal passes the label regex — every octet is a legal label — so
    # it has to be refused by name. This is the trap worth catching: a
    # certificate carrying "10.0.0.11" as a dNSName would string-match under
    # X509_check_host and look like it worked, while being an identity nothing
    # can resolve and OpenBao's enforce_hostnames may refuse to issue.
    try:
        import ipaddress

        ipaddress.ip_address(v)
    except ValueError:
        pass
    else:
        raise ValueError(
            f"'{value}' is an IP address, not a DNS name. The cluster proxy "
            "matches the certificate against DNS names only and ignores IP "
            "entries entirely, so an address here cannot be verified. Give the "
            "node a resolvable name such as dgx-md-01.example.net."
        )
    if len(v) > 253 or not _FQDN_RE.match(v):
        raise ValueError(
            f"'{value}' is not a fully-qualified DNS name. The cluster proxy "
            "verifies the node's certificate against this name, so it needs "
            "something like dgx-md-01.example.net — a bare hostname will not "
            "resolve from the cluster."
        )
    return v


# --- talking to OpenBao ---------------------------------------------------

@dataclass(frozen=True)
class SignedCert:
    certificate: str
    ca_chain: str
    serial: str | None = None


class PkiError(RuntimeError):
    """OpenBao could not sign, and the operator needs to know why."""


def _base(url: str) -> str:
    return url.rstrip("/")


async def sign_csr(
    *,
    url: str,
    token: str,
    mount: str,
    role: str,
    csr_pem: str,
    fqdn: str,
    ttl_hours: float | None,
    verify: bool | str = True,
    timeout: float = 20.0,
) -> SignedCert:
    """Have OpenBao sign a CSR the node generated.

    `sign`, not `issue`. `pki/issue/<role>` makes OpenBao generate the private
    key and hand it back over the network — which would put a node's key in
    portal memory, in the database, and from there into every scheduled S3
    backup. `pki/sign/<role>` takes the CSR the node produced and returns only
    a certificate, so the key never exists anywhere but the node.

    The SANs are sent as API parameters rather than taken from the CSR. With
    `use_csr_sans=false` on the role — which the role SHOULD have, because
    OpenBao enforces `allowed_ip_sans_cidr` only on the parameter path and
    never on the CSR path — the portal decides what the certificate attests
    to, not whatever the node happened to put in its request.
    """
    import httpx

    policy = lifetime_policy(ttl_hours)
    endpoint = f"{_base(url)}/v1/{mount.strip('/')}/sign/{role}"
    payload = {
        "csr": csr_pem,
        "common_name": fqdn,
        "alt_names": fqdn,
        "ttl": policy.as_bao_ttl(),
        "exclude_cn_from_sans": False,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=verify) as client:
            # X-Vault-Token is the canonical header for the fork; the token is
            # never logged, and errors below quote OpenBao's message, not ours.
            resp = await client.post(
                endpoint, json=payload, headers={"X-Vault-Token": token}
            )
    except Exception as exc:  # noqa: BLE001 - network, DNS, TLS all land here
        raise PkiError(
            f"Could not reach OpenBao at {_base(url)}: {exc}. The existing "
            "certificate is untouched."
        ) from exc

    if resp.status_code >= 400:
        raise PkiError(_explain(resp, endpoint, policy))
    try:
        data = resp.json()["data"]
    except Exception as exc:  # noqa: BLE001
        raise PkiError(f"OpenBao returned an unreadable response: {exc}") from exc

    cert = (data.get("certificate") or "").strip()
    if not cert:
        raise PkiError("OpenBao returned no certificate.")
    chain = data.get("ca_chain") or []
    if isinstance(chain, str):
        chain = [chain]
    if not chain and data.get("issuing_ca"):
        chain = [data["issuing_ca"]]
    return SignedCert(
        certificate=cert,
        ca_chain="\n".join(c.strip() for c in chain if c),
        serial=data.get("serial_number"),
    )


def _explain(resp, endpoint: str, policy: LifetimePolicy) -> str:
    """Turn OpenBao's error into one an operator can act on.

    The three failures below are the ones that actually happen, and each is
    indistinguishable from the others in the raw response.
    """
    try:
        errors = "; ".join(resp.json().get("errors") or []) or resp.text[:300]
    except Exception:  # noqa: BLE001
        errors = resp.text[:300]

    if resp.status_code in (401, 403):
        return (
            f"OpenBao refused the portal's token ({resp.status_code}). It needs "
            f"`update` on {endpoint.split('/v1/', 1)[-1]}. OpenBao said: {errors}"
        )
    if "ttl" in errors.lower() and "max" in errors.lower():
        return (
            f"OpenBao refused a {policy.as_bao_ttl()} certificate because the "
            f"role's max_ttl is shorter. Either raise max_ttl on the role or "
            f"lower the certificate lifetime in Settings. OpenBao said: {errors}"
        )
    if "not allowed" in errors.lower() or "allowed_domains" in errors.lower():
        return (
            "OpenBao's role does not permit this node's DNS name. Add its "
            f"domain to allowed_domains (with allow_subdomains). OpenBao said: {errors}"
        )
    return f"OpenBao refused to sign ({resp.status_code}): {errors}"


async def fetch_ca_chain(
    *, url: str, mount: str, verify: bool | str = True, timeout: float = 15.0
) -> str:
    """The CA bundle node certificates chain to.

    Unauthenticated by design — OpenBao serves the CA on a public path, and
    everything in it is public by construction. This is what becomes `ca.crt`
    in the cluster Secret; without it ingress-nginx emits no proxy_ssl_verify
    at all and the hop silently degrades to unverified HTTPS.
    """
    import httpx

    endpoint = f"{_base(url)}/v1/{mount.strip('/')}/ca_chain"
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=verify) as client:
            resp = await client.get(endpoint)
    except Exception as exc:  # noqa: BLE001
        raise PkiError(f"Could not fetch the CA chain from {endpoint}: {exc}") from exc
    if resp.status_code >= 400:
        raise PkiError(f"OpenBao returned {resp.status_code} for {endpoint}.")
    body = (resp.text or "").strip()
    if "BEGIN CERTIFICATE" not in body:
        raise PkiError(
            f"{endpoint} did not return a PEM certificate chain. Is the PKI "
            f"engine mounted at '{mount}'?"
        )
    return body
