"""Mirror x402 PaymentRequired into the JSON body of HTTP 402 responses.

x402 v2 SDK puts the challenge only in the `payment-required` header and
returns `{}` as the body (see PaymentMiddlewareASGI). Most agents that already
pay competitors (x402.shizu.me, Polymarketeer, etc.) still parse the JSON body
`accepts[]` the v1 way. An empty body looks like a broken paywall, so they
probe and leave without signing.

This middleware sits outside the payment stack and, for 402s that carry a
decodeable `payment-required` header and an empty/`{}` body, rewrites the
body to the decoded envelope. Headers are left untouched.
"""

from __future__ import annotations

import json

from x402.http.utils import decode_payment_required_header


class PaymentBodyCompatMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        status_holder: dict = {}
        headers_holder: list = []
        body_chunks: list[bytes] = []
        started = False

        async def capturing_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                headers_holder[:] = list(message.get("headers") or [])
                started = True
                # Defer start until we know whether to rewrite the body.
                return
            if message["type"] == "http.response.body":
                body_chunks.append(message.get("body") or b"")
                if message.get("more_body"):
                    return
                await self._finish(send, status_holder.get("status"), headers_holder, body_chunks)
                return
            await send(message)

        await self.app(scope, receive, capturing_send)

        # App returned without a body (unusual) — flush start if deferred.
        if started and not body_chunks and status_holder.get("status") not in (None, 402):
            await send(
                {
                    "type": "http.response.start",
                    "status": status_holder["status"],
                    "headers": headers_holder,
                }
            )
            await send({"type": "http.response.body", "body": b""})

    async def _finish(self, send, status, headers, chunks: list[bytes]):
        body = b"".join(chunks)
        if status != 402:
            await send({"type": "http.response.start", "status": status, "headers": headers})
            await send({"type": "http.response.body", "body": body})
            return

        pr_header = None
        for name, value in headers:
            if name.lower() == b"payment-required":
                pr_header = value.decode("latin-1")
                break

        if not pr_header:
            await send({"type": "http.response.start", "status": status, "headers": headers})
            await send({"type": "http.response.body", "body": body})
            return

        stripped = body.strip()
        if stripped and stripped not in (b"{}", b"null"):
            await send({"type": "http.response.start", "status": status, "headers": headers})
            await send({"type": "http.response.body", "body": body})
            return

        try:
            payment_required = decode_payment_required_header(pr_header)
            payload = payment_required.model_dump(by_alias=True, exclude_none=True)
            new_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        except Exception:
            await send({"type": "http.response.start", "status": status, "headers": headers})
            await send({"type": "http.response.body", "body": body})
            return

        new_headers = [
            (n, v)
            for n, v in headers
            if n.lower() not in (b"content-length", b"content-type")
        ]
        new_headers.append((b"content-type", b"application/json"))
        new_headers.append((b"content-length", str(len(new_body)).encode("ascii")))

        await send({"type": "http.response.start", "status": 402, "headers": new_headers})
        await send({"type": "http.response.body", "body": new_body})
