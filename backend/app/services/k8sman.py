"""Kubernetes manifests for an endpoint whose TLS terminates in the cluster.

This is the nginx sidecar moved off the box. Same job — terminate HTTPS for a
public hostname and reverse-proxy to vLLM — running in Kubernetes instead, with
a cert-manager certificate the portal never sees and never pushes over SSH.

Two decisions are worth stating, because both are load-bearing.

**The portal emits YAML; it does not apply it.** No kubeconfig, no service
account, no RBAC, no in-cluster credentials. The portal describes what it
wants and the operator's existing GitOps applies it. That keeps a control
plane for GPU nodes from also being a thing that can write to the cluster, and
it means the API being unreachable degrades nothing.

**The manifests are static across a promotion.** `instance_api_node` returns
the HEAD node for both cluster and distributed topologies, so every member of
an endpoint serves from the same address — only the port would differ, and
`Endpoint.upstream_port` pins that too. So a promotion changes the process
listening on head-ip:port and nothing a proxy can observe. Nothing to re-apply,
nothing to sync, no window where the manifest points at a stopped instance.

That works because endpoint members are mutually exclusive by construction: an
endpoint has one serving instance, TP=2 is the whole box, and promote stops the
outgoing instance before starting the incoming one.

The ingress controller does the proxying, so there is no nginx Deployment here
to run and patch. A selectorless Service plus a hand-written EndpointSlice is
the standard way to point a cluster Service at an address outside it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# RFC 1123. Applied to every value interpolated into the YAML below — which is
# also why none of it needs quoting or escaping. Reject, never repair: a name
# that is not a legal Kubernetes identifier should produce an error here, not a
# manifest that fails to apply hours later.
_DNS_LABEL = re.compile(r"\A[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?\Z")
_DNS_NAME = re.compile(
    r"\A[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?(\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)*\Z"
)
_IPV4 = re.compile(r"\A(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\Z")

# Long-lived streaming responses are the whole workload here, so the defaults
# are wrong in three specific ways and every one of them is a visible failure:
#
#   proxy-buffering       on by default, which holds an SSE stream in the
#                         proxy and delivers it in chunks. Tokens stop
#                         appearing one at a time — the single most-reported
#                         symptom of putting a proxy in front of vLLM.
#   proxy-read-timeout    60s by default. A long generation is cut mid-stream.
#   proxy-body-size       1m by default; a large batch or a long conversation
#                         is rejected with 413.
_INGRESS_ANNOTATIONS = {
    "nginx.ingress.kubernetes.io/proxy-buffering": "off",
    "nginx.ingress.kubernetes.io/proxy-read-timeout": "3600",
    "nginx.ingress.kubernetes.io/proxy-send-timeout": "3600",
    "nginx.ingress.kubernetes.io/proxy-body-size": "0",
}


class ManifestError(ValueError):
    """A value could not be rendered into a manifest safely."""


@dataclass
class ManifestInput:
    endpoint: str        # endpoint name; already ^[a-z0-9][a-z0-9-]{0,63}$
    hostname: str        # public DNS name, e.g. llm.skynet.telenor.net
    upstream_ip: str     # LAN address of the serving (head) node
    upstream_port: int   # the pinned port every member binds
    namespace: str = "default"
    issuer: str = "letsencrypt-prod"
    issuer_kind: str = "ClusterIssuer"
    ingress_class: str = "nginx"


def _check(value: str, pattern: re.Pattern, what: str) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise ManifestError(
            f"{what} is not usable in a Kubernetes manifest: {value!r}. "
            "Expected lowercase letters, digits and dashes (RFC 1123)."
        )
    return value


def _check_ip(value: str) -> str:
    m = _IPV4.match(value or "")
    if not m or any(int(o) > 255 for o in m.groups()):
        raise ManifestError(
            f"'{value}' is not an IPv4 address, so it cannot be the upstream "
            "for a Kubernetes EndpointSlice."
        )
    return value


def render(mi: ManifestInput) -> str:
    """The full manifest set as one YAML document stream."""
    name = _check(mi.endpoint, _DNS_LABEL, "The endpoint name")
    ns = _check(mi.namespace, _DNS_LABEL, "The namespace")
    host = _check(mi.hostname.lower(), _DNS_NAME, "The endpoint hostname")
    issuer = _check(mi.issuer, _DNS_LABEL, "The issuer name")
    cls = _check(mi.ingress_class, _DNS_LABEL, "The ingress class")
    ip = _check_ip(mi.upstream_ip)
    if mi.issuer_kind not in ("ClusterIssuer", "Issuer"):
        raise ManifestError("issuer_kind must be 'ClusterIssuer' or 'Issuer'.")
    port = int(mi.upstream_port)
    if not 1 <= port <= 65535:
        raise ManifestError(f"{port} is not a valid port.")

    svc = f"spark-{name}"
    labels = (
        f"    app.kubernetes.io/name: {svc}\n"
        f"    app.kubernetes.io/managed-by: spark-control-plane\n"
    )
    ann = "".join(f"    {k}: \"{v}\"\n" for k, v in _INGRESS_ANNOTATIONS.items())

    return f"""# Generated by Spark Control Plane for endpoint '{name}'.
#
# Terminates HTTPS for {host} in the cluster and proxies to the DGX head node
# at {ip}:{port} — the nginx sidecar that used to run on the box.
#
# This does NOT need regenerating when you promote a different instance onto
# '{name}'. Every member of the endpoint serves from {ip}:{port}; promoting
# changes which process is listening there, which is invisible from here.
#
# Regenerate only if the endpoint's hostname, upstream port, or the head node's
# address changes.
---
# A Service with no selector: the backend is outside the cluster, so its
# addresses are declared below instead of discovered from pods.
apiVersion: v1
kind: Service
metadata:
  name: {svc}
  namespace: {ns}
  labels:
{labels}spec:
  type: ClusterIP
  ports:
    - name: http
      port: {port}
      targetPort: {port}
      protocol: TCP
---
# The address the Service resolves to. Hand-written because nothing in the
# cluster can discover a GPU node on the LAN.
apiVersion: discovery.k8s.io/v1
kind: EndpointSlice
metadata:
  name: {svc}
  namespace: {ns}
  labels:
    kubernetes.io/service-name: {svc}
{labels}addressType: IPv4
ports:
  - name: http
    port: {port}
    protocol: TCP
endpoints:
  - addresses:
      - {ip}
    conditions:
      # Deliberately pinned true. The cluster cannot health-check an address it
      # does not manage, and during a promotion the upstream is down for the
      # minutes it takes to load weights. Marking it not-ready would remove the
      # only backend and turn a slow promotion into a 503 with no recovery
      # path — the portal is what knows whether the handoff is progressing.
      ready: true
---
# cert-manager issues and renews the certificate. The portal never sees the
# key: that is the point of terminating here rather than on the box.
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: {svc}-tls
  namespace: {ns}
  labels:
{labels}spec:
  secretName: {svc}-tls
  issuerRef:
    name: {issuer}
    kind: {mi.issuer_kind}
  dnsNames:
    - {host}
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {svc}
  namespace: {ns}
  labels:
{labels}  annotations:
{ann}spec:
  ingressClassName: {cls}
  tls:
    - hosts:
        - {host}
      secretName: {svc}-tls
  rules:
    - host: {host}
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: {svc}
                port:
                  number: {port}
"""
