"""This backend serves only this Mac's own dashboard. One gate in front of every route, HTTP and WebSocket:

1. The Host header must name this Mac's loopback (127.0.0.1, localhost or [::1], any port). A DNS-rebinding page reaches the
   backend under its own host name, so it is refused here, for reads as well as writes.
2. A browser request that changes something (any method but GET, HEAD, OPTIONS), and every WebSocket, must come from a loopback
   Origin when it carries one. CORS only stops a page reading a reply; this stops the request from acting. A request with no
   Origin (the dashboard's dev proxy, curl, scripts on this Mac) still works.

The per-route guards stay as a second line.
"""

from __future__ import annotations

from urllib.parse import urlsplit

LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _host_name(value: str) -> str | None:
    """'127.0.0.1:8077' -> '127.0.0.1', '[::1]:8077' -> '::1'. None when it can't be parsed."""
    try:
        name = urlsplit(f"http://{value}").hostname
    except ValueError:
        return None
    return name.lower() if name else None


def loopback_origin(origin: str) -> bool:
    try:
        parts = urlsplit(origin)
        return parts.scheme in ("http", "https") and (parts.hostname or "").lower() in LOOPBACK
    except ValueError:
        return False


class LocalOnly:
    def __init__(self, app, allowed_hosts=LOOPBACK):
        self.app = app
        self.allowed_hosts = frozenset(h.lower() for h in allowed_hosts)

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        host = _host_name(headers.get("host", ""))
        origin = headers.get("origin")
        if host is None or host not in self.allowed_hosts:
            return await self._refuse(scope, receive, send, "this backend only answers on this Mac's loopback address")
        acts = scope["type"] == "websocket" or scope.get("method", "GET").upper() not in SAFE_METHODS
        if acts and origin is not None and not loopback_origin(origin):
            return await self._refuse(scope, receive, send, "only the local dashboard can do this")
        return await self.app(scope, receive, send)

    @staticmethod
    async def _refuse(scope, receive, send, reason: str):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008, "reason": reason})
            return
        body = ('{"detail":"%s"}' % reason).encode()
        await send({"type": "http.response.start", "status": 403, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
