# Security policy

## Reporting a vulnerability

Please report security issues privately via [GitHub's private vulnerability
reporting](https://github.com/JeyElCode/SparkControlplane/security/advisories/new)
rather than opening a public issue.

Please include what you were able to do, and enough detail to reproduce it. If
you have a working proof of concept, that is welcome but not required — a clear
description of the flaw is enough.

Expect an acknowledgement within a few days. This is maintained by one person,
so please be patient with fixes; anything that lets an unauthenticated caller
reach the nodes will be prioritised over everything else.

## What this software can do, and what that means

Understand the blast radius before deciding where to run it. The portal:

- holds SSH credentials for every node and **runs commands as root** via sudo,
- can power off and reboot the hardware,
- can delete model weights and tear the cluster down,
- proxies inference traffic and holds each instance's API key.

Anyone who can reach the portal *and* authenticate to it has all of that.
Anyone who can reach it while `SPARK_AUTH_MODE=none` has all of it without
authenticating.

## Deploying it safely

- **Turn auth on.** `SPARK_AUTH_MODE` accepts `password`, `ldap` or `oidc`.
  `none` is for a trusted, isolated LAN and nothing else.
- **In `oidc` mode, `SPARK_OIDC_GROUP_REQUIRED` is mandatory** — the portal
  refuses to start without it, so an entire directory cannot sign in.
- **In `ldap` mode, set `SPARK_LDAP_GROUP_REQUIRED`.** It is currently optional,
  which means an LDAP deployment that omits it authenticates every account in
  the directory. Set it.
- **Serve it over HTTPS.** `SPARK_AUTH_COOKIE_SECURE` defaults to `auto` and
  will set the Secure flag when it sees an HTTPS request (including via
  `X-Forwarded-Proto`).
- **Back up `SPARK_SECRET_KEY`.** It encrypts every stored secret. Losing it
  means re-entering all of them; leaking it means every stored credential is
  compromised.
- **Keep the QSFP fabric private.** Inter-node traffic is unencrypted by design.

## Current known limitations

Stated plainly rather than discovered later:

- **There is one authorization level.** Any authenticated session can do
  anything the portal can do. There are no roles yet.
- **There is no audit log.** Jobs record what happened but not who asked for it.
- **An account disabled in your directory is not detected.** The portal never
  re-contacts the identity provider after sign-in, so access ends when the
  session is revoked from Settings → Sessions, or when it expires (capped at 8h
  in `oidc` mode).
- **The MCP bearer token is not scoped.** If `/mcp` is enabled, that token
  grants the full control-plane surface and is not affected by session
  revocation.
- **Single process by design.** Rate limits, session revocation and job tracking
  are per-process. Run exactly one replica.

## Supported versions

The latest release. Fixes go into a new release rather than being backported.
