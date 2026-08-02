"""What an instance actually binds: its port, and whether it terminates TLS.

Both answers can come from the ENDPOINT rather than the instance, so they live
here — in one module that depends on nothing but the models — instead of being
re-derived at each call site. Getting one of them wrong is not a cosmetic bug:
probe the wrong port and a healthy instance reads as down; assume a sidecar
that is not there and every health check fails against a closed socket.
"""

from __future__ import annotations

from ..models import TERM_K8S, Instance


def effective_port(inst: Instance) -> int:
    """The port this instance's vLLM should bind.

    A member of an endpoint that pins `upstream_port` binds THAT, not its own
    allocated port. This is what lets an external proxy hold a static upstream
    across a promotion: every candidate serves from the head node, so pinning
    the port means the address the proxy targets never changes and only the
    process behind it does.

    Same shape as `_effective_aliases`: an endpoint member takes the endpoint's
    value. Safe because members are mutually exclusive — TP=2 is the whole box
    and promote is stop-then-start.
    """
    ep = getattr(inst, "endpoint", None)
    if ep is not None and getattr(ep, "upstream_port", None):
        return int(ep.upstream_port)
    return inst.port


def effective_tls(inst: Instance) -> tuple[bool, str | None, str | None]:
    """Does this instance terminate TLS on the box, and with what material?

    Three cases, in order:

    * A member of a `k8s` endpoint terminates NOTHING on the box. HTTPS is
      handled by a proxy in the cluster holding a cert-manager certificate the
      portal never sees, so there is no sidecar, no key push on promote, and
      vLLM binds a routable address rather than loopback.
    * A member of an `onbox` endpoint that owns a certificate uses the
      ENDPOINT's, not its own. That is the handoff #77 exists for: promoting a
      new instance moves the cert without it ever leaving the portal.
    * Anything else keeps its own.

    The instance's own `tls_enabled` still gates the sidecar in case two —
    supplying material is not the same as deciding to run a proxy.
    """
    ep = getattr(inst, "endpoint", None)
    if ep is not None and ep.termination == TERM_K8S:
        return False, None, None
    if ep is not None and inst.tls_enabled and ep.tls_cert_enc and ep.tls_key_enc:
        return True, ep.tls_cert_enc, ep.tls_key_enc
    return inst.tls_enabled, inst.tls_cert_enc, inst.tls_key_enc
