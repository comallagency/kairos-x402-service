"""ASGI middleware adding MPP ("Payment" HTTP auth scheme) support on top of
the existing x402 flow, without touching it. Two responsibilities:

1. Normal request, no MPP credential: pass through to the x402-wrapped app
   unchanged. If it comes back 402 (x402 didn't see a valid payment either),
   APPEND our own `WWW-Authenticate: Payment` challenge as a second header
   line alongside x402's - the mpp-specs "Multiple Payment Options" pattern
   (multiple WWW-Authenticate: Payment lines, client picks one). x402's own
   challenge body/headers are untouched.

2. Request carrying `Authorization: Payment <credential>`: this is not
   something x402's middleware understands, so it is handled entirely here,
   short-circuiting x402/IntentLoggingMiddleware for this one request. On
   success, the INNER app is invoked directly (same handler x402 would have
   called) so business logic never forks between the two payment rails - the
   handler is the single source of truth for both. On failure, a fresh 402
   with a new MPP challenge is returned; nothing is settled and no state
   changes (per draft-httpauth-payment-00's idempotency requirement for
   unpaid requests).

Route prices/assets/recipients are read fresh from build_route_configs() on
every request - never hand-copied, matching the rest of this project.
"""

import json
import logging

from app import config, db, mpp
from app.capacity import ROUTE_KEYS
from app.x402_setup import build_route_configs, resolve_payment_requirements

logger = logging.getLogger("x402.mpp_middleware")


def _route_payment_terms(route_key: str):
    route_config = build_route_configs()[f"POST /{route_key}"]
    payment_option = route_config.accepts
    if isinstance(payment_option, list):
        payment_option = payment_option[0]
    requirements = resolve_payment_requirements(payment_option)
    return str(requirements.amount), requirements.asset, requirements.pay_to, route_config.description


def _problem_body(code: str, detail: str) -> bytes:
    return json.dumps(
        {
            "type": f"https://paymentauth.org/problems/{code}",
            "title": code.replace("-", " ").title(),
            "status": 402,
            "detail": detail,
        }
    ).encode()


class MPPMiddleware:
    def __init__(self, app, inner_app):
        self.app = app
        self.inner_app = inner_app
        # MPP settlement signs/sends through a CDP managed server wallet.
        # Without its wallet secret, advertising this rail creates a challenge
        # that can verify but can never settle. Fail closed and expose only the
        # fully configured x402 rail.
        self.enabled = bool(config.CDP_WALLET_SECRET)

    async def __call__(self, scope, receive, send):
        if not self.enabled:
            await self.app(scope, receive, send)
            return
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
        authorization = headers.get(b"authorization", b"").decode("latin-1")

        if authorization.strip().lower().startswith("payment "):
            await self._handle_mpp_credential(scope, receive, send, route_key, authorization)
            return

        await self._passthrough_and_augment(scope, receive, send, route_key)

    async def _passthrough_and_augment(self, scope, receive, send, route_key):
        status_holder: dict[str, int] = {}

        async def augmenting_send(message):
            if message["type"] == "http.response.start" and message["status"] == 402:
                status_holder["augmented"] = True
                amount_atomic, asset_address, pay_to, description = _route_payment_terms(route_key)
                mpp_header = mpp.build_www_authenticate(amount_atomic, asset_address, pay_to, description)
                headers = list(message.get("headers", []))
                headers.append((b"www-authenticate", mpp_header.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, augmenting_send)

    async def _handle_mpp_credential(self, scope, receive, send, route_key, authorization):
        amount_atomic, asset_address, pay_to, description = _route_payment_terms(route_key)

        async def reject(mpp_error: mpp.MPPError):
            mpp_header = mpp.build_www_authenticate(amount_atomic, asset_address, pay_to, description)
            body = _problem_body(mpp_error.code, mpp_error.detail)
            await send(
                {
                    "type": "http.response.start",
                    "status": 402,
                    "headers": [
                        (b"content-type", b"application/problem+json"),
                        (b"cache-control", b"no-store"),
                        (b"www-authenticate", mpp_header.encode("latin-1")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            db.log_request(
                route=route_key, method=scope["method"], status="payment_failed",
                error_reason=f"mpp:{mpp_error.code}: {mpp_error.detail}"[:500],
            )

        try:
            credential = mpp.parse_credential(authorization)
            verified = mpp.verify_credential(
                credential, expected_amount=amount_atomic, expected_asset=asset_address, expected_recipient=pay_to
            )
        except mpp.MPPError as exc:
            await reject(exc)
            return

        if db.has_mpp_nonce(verified["nonce"]):
            await reject(mpp.MPPError("invalid-challenge", "This authorization has already been used"))
            return

        try:
            tx_hash = await mpp.settle_credential(verified, asset_address)
        except mpp.MPPError as exc:
            # Deliberately NOT marked consumed here: settlement can fail for
            # reasons that never touch the chain (e.g. our gas wallet is
            # unfunded) - burning the nonce on our side would permanently
            # invalidate a legitimate payer's authorization for nothing ever
            # charged. The EIP-3009 nonce is consumed on-chain by the token
            # contract itself, which is what actually prevents a double
            # transfer if two attempts ever race - this table is only a
            # fast-path cache for requests we already know succeeded.
            logger.warning("MPP settlement failed for %s: %s", route_key, exc)
            await reject(exc)
            return

        db.mark_mpp_nonce_consumed(verified["nonce"], verified["from"])

        receipt_header = mpp.build_receipt(tx_hash).encode("latin-1")

        async def receipt_injecting_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"payment-receipt", receipt_header))
                cache_control_present = any(h[0].lower() == b"cache-control" for h in headers)
                if not cache_control_present:
                    headers.append((b"cache-control", b"private"))
                message = {**message, "headers": headers}
            await send(message)

        await self.inner_app(scope, receive, receipt_injecting_send)
