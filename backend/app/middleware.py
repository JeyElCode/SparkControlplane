"""Auth enforcement as raw ASGI middleware (covers HTTP *and* WebSockets).

Guarded: everything under /api (including the status/logs/jobs WebSockets).
Open: /api/auth/* (the login flow itself), /api/health (liveness probes), the
SPA shell + assets (the login page must be able to load), and /mcp (its own
bearer gate). /metrics accepts `Authorization: Bearer SPARK_METRICS_TOKEN` so
Prometheus can scrape while the portal is locked.
"""

from __future__ import annotations

import hmac
from http import cookies as http_cookies

from .config import get_settings
from .services.auth import COOKIE_NAME, parse_session

_OPEN_PREFIXES = ("/api/auth/", "/mcp")
_OPEN_PATHS = {"/api/health", "/api/auth"}


def _session_user(scope) -> str | None:
    headers = dict(scope.get("headers") or [])
    raw = headers.get(b"cookie")
    if not raw:
        return None
    jar = http_cookies.SimpleCookie()
    try:
        jar.load(raw.decode("latin-1"))
    except (http_cookies.CookieError, UnicodeDecodeError):
        return None
    morsel = jar.get(COOKIE_NAME)
    return parse_session(morsel.value) if morsel else None


def _bearer(scope) -> str | None:
    headers = dict(scope.get("headers") or [])
    val = headers.get(b"authorization", b"").decode("latin-1")
    return val[7:] if val.startswith("Bearer ") else None


class AuthMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        settings = get_settings()
        if settings.effective_auth_mode == "none":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        if path in _OPEN_PATHS or path.startswith(_OPEN_PREFIXES):
            return await self.app(scope, receive, send)

        if path == "/metrics":
            token = settings.metrics_token
            supplied = _bearer(scope)
            if token and supplied and hmac.compare_digest(supplied, token):
                return await self.app(scope, receive, send)
            if _session_user(scope):  # a logged-in browser may look too
                return await self.app(scope, receive, send)
            return await self._deny(scope, receive, send)

        if not path.startswith("/api"):
            # SPA shell + static assets: serving them leaks nothing — every
            # data call they make comes back through this guard.
            return await self.app(scope, receive, send)

        if _session_user(scope):
            return await self.app(scope, receive, send)
        return await self._deny(scope, receive, send)

    async def _deny(self, scope, receive, send):
        if scope["type"] == "websocket":
            # must consume the connect event before closing
            msg = await receive()
            if msg.get("type") == "websocket.connect":
                await send({"type": "websocket.close", "code": 4401})
            return
        body = b'{"detail":"Not authenticated"}'
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})
