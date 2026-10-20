"""Client IP capture (X-Forwarded-For, resolved into scope["client"] by
uvicorn's --proxy-headers before this middleware ever sees the request).

A contextvar, not a request argument threaded through every handler: every
db.log_request() call site stays exactly as it is - this only adds an
optional value log_request() reads if the caller doesn't pass client_ip
explicitly.
"""

from __future__ import annotations

import contextvars

_client_ip: contextvars.ContextVar[str | None] = contextvars.ContextVar("client_ip", default=None)


class ClientIpMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        token = _client_ip.set(client[0] if client else None)
        try:
            await self.app(scope, receive, send)
        finally:
            _client_ip.reset(token)


def current_client_ip() -> str | None:
    return _client_ip.get()
