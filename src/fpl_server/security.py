"""ASGI hardening shared by the MCP HTTP endpoint and the local web (login / approval) app."""

from __future__ import annotations

import hmac
from collections.abc import Iterable

from starlette.types import ASGIApp, Message, Receive, Scope, Send


async def _plain(send: Send, status: int, body: bytes, extra: list[tuple[bytes, bytes]] | None = None):
    headers = [
        (b"content-type", b"text/plain; charset=utf-8"),
        (b"content-length", str(len(body)).encode()),
        (b"cache-control", b"no-store"),
    ] + (extra or [])
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class BearerAuthMiddleware:
    """Require `Authorization: Bearer <token>` on every HTTP request (constant-time compare)."""

    def __init__(self, app: ASGIApp, token: str):
        if not token:
            raise ValueError("BearerAuthMiddleware needs a non-empty token")
        self.app = app
        self._token = token.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        supplied = b""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                scheme, _, credential = value.partition(b" ")
                if scheme.lower() == b"bearer":
                    supplied = credential.strip()
                break
        if not supplied or not hmac.compare_digest(supplied, self._token):
            if scope["type"] == "http":
                await _plain(
                    send, 401, b"Unauthorized", [(b"www-authenticate", b'Bearer realm="fpl-mcp"')]
                )
            return
        await self.app(scope, receive, send)


class WebHardeningMiddleware:
    """Host-header allow-list (anti DNS-rebinding) + strict security headers + no caching."""

    def __init__(self, app: ASGIApp, allowed_hosts: Iterable[str]):
        self.app = app
        self._allowed = {h.lower() for h in allowed_hosts}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        host = b""
        for name, value in scope.get("headers", []):
            if name == b"host":
                host = value
                break
        if host.decode("latin-1").lower() not in self._allowed:
            await _plain(send, 421, b"Misdirected Request")
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                for name, value in _SECURITY_HEADERS:
                    if name not in present:
                        headers.append((name, value))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


_SECURITY_HEADERS: list[tuple[bytes, bytes]] = [
    (
        b"content-security-policy",
        b"default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        b"img-src data:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    ),
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"cache-control", b"no-store"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
]
