import json

from app import db
from app.capacity import ROUTE_KEYS
from app.receipts import extract_payer_from_header


class IntentLoggingMiddleware:
    """Wrapped directly around the x402 payment middleware (inside the capacity
    gate). The x402 SDK issues its own 402 payment challenges internally and
    never touches our db - this is the only place we can see "someone asked for
    this route but didn't pay", which is the intent signal the dashboard needs.

    The body is drained upfront rather than captured lazily: when the SDK
    rejects for lack of payment it never calls receive() at all, so a
    lazy/wrap-on-read approach would see an empty body every time.

    Three outcomes are distinguished on a final 402:
    - no Payment-Signature/X-Payment header at all -> 'unpaid' (a browse, no
      payment was ever attempted; the request body is the intent signal).
    - header present but still 402 -> 'payment_failed' (a real payment was
      attempted and rejected by verify/settle - this is the case that needs to
      be loud on the dashboard, not buried).
    - an Authorization: Payment header (the MPP client credential scheme) is
      also tracked separately (mpp_attempted) regardless of which of the above
      applies, so real MPP demand shows up in the numbers instead of being
      guessed at.

    Placed INSIDE CapacityGateMiddleware, so capacity-blocked requests (already
    logged there) never reach this layer - no double counting.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope["method"]
        path = scope["path"]
        route_key = ROUTE_KEYS.get((method, path))
        if route_key is None:
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        user_agent = headers.get(b"user-agent", b"").decode("latin-1")
        payment_header = (
            headers.get(b"payment-signature", b"").decode("latin-1")
            or headers.get(b"x-payment", b"").decode("latin-1")
            or None
        )
        authorization = headers.get(b"authorization", b"").decode("latin-1")
        mpp_attempted = authorization.strip().lower().startswith("payment")

        messages = []
        more_body = True
        while more_body:
            message = await receive()
            messages.append(message)
            if message["type"] != "http.request":
                break
            more_body = message.get("more_body", False)

        request_body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.request")

        idx = 0

        async def replay_receive():
            nonlocal idx
            if idx < len(messages):
                message = messages[idx]
                idx += 1
                return message
            return {"type": "http.disconnect"}

        status_holder: dict[str, int] = {}
        response_chunks: list[bytes] = []

        async def capturing_send(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
            elif message["type"] == "http.response.body":
                response_chunks.append(message.get("body", b""))
            await send(message)

        await self.app(scope, replay_receive, capturing_send)

        if status_holder.get("status") != 402:
            return

        if payment_header:
            payer = extract_payer_from_header(payment_header)
            error_reason = None
            try:
                response_body = json.loads(b"".join(response_chunks))
                error_reason = (response_body.get("error") if isinstance(response_body, dict) else None)
                if isinstance(error_reason, dict):
                    error_reason = json.dumps(error_reason)
            except Exception:
                pass
            db.log_request(
                route=route_key,
                method=method,
                status="payment_failed",
                payer=payer,
                user_agent=user_agent,
                error_reason=str(error_reason)[:500] if error_reason else None,
                mpp_attempted=mpp_attempted,
            )
        else:
            db.log_request(
                route=route_key,
                method=method,
                status="unpaid",
                user_agent=user_agent,
                body_excerpt=request_body.decode("utf-8", errors="replace"),
                mpp_attempted=mpp_attempted,
            )
