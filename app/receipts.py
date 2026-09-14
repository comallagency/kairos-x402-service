import time
from typing import Any

from app import config, db

# Real OpenRouter model IDs (with their ":free"/routing suffixes) never leave
# this process - a buyer only needs to know whether their call fell back to a
# secondary model (see `fallback_used`), not which one. Mapping is a fixed
# index into the known model list so the same real model always yields the
# same neutral tag (stable), without naming it.
_NEUTRAL_MODEL_IDS = {
    config.OPENROUTER_TRANSLATE_MODELS[0]: "model-a",
    config.OPENROUTER_TRANSLATE_MODELS[1]: "model-b",
    config.OPENROUTER_TRANSLATE_MODELS[2]: "model-c",
    config.OPENROUTER_LAST_RESORT_MODEL: "model-fallback",
}


def neutral_model_id(real_model_id: str | None) -> str | None:
    """Map a real upstream model id to a stable, neutral, non-identifying tag."""
    if real_model_id is None:
        return None
    return _NEUTRAL_MODEL_IDS.get(real_model_id, "model-unknown")


def price_float(price: str) -> float:
    """"$0.05" -> 0.05 - the one place a RouteConfig price string is parsed
    back into a float for effective_price()/db.log_request(), so a repriced
    route only needs its config.PRICE_* string updated, never a second
    hand-typed float somewhere else."""
    return float(price.replace("$", ""))


def effective_price(payer: str | None, normal_price: float) -> float:
    """What this call actually costs the buyer - a truthful read-only preview
    of app/x402_setup.py::_first_call_free_hook's decision (the x402
    before-settle hook that actually skips settlement for a wallet's first
    call, globally, across all 3 routes). Read-only: never marks the wallet
    as seen itself, so it can't race with the hook's own atomic
    check-and-record in db.mark_wallet_seen().

    Mirrors the hook's config.FREE_FIRST_CALL gate so this preview never
    promises a free call the hook won't actually grant."""
    if config.FREE_FIRST_CALL and payer and not db.has_seen_wallet(payer):
        return 0.0
    return normal_price


def make_receipt(
    model_served: str | None,
    upstream: str,
    latency_ms: int,
    price_paid_usdc: float,
    steps_executed: int | None = None,
    sources_read: int | None = None,
    fallback_used: bool | None = None,
    searches_run: int | None = None,
    segments_processed: int | None = None,
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "model_served": model_served,
        "upstream": upstream,
        "latency_ms": latency_ms,
        "price_paid_usdc": price_paid_usdc,
    }
    if steps_executed is not None:
        receipt["steps_executed"] = steps_executed
    if sources_read is not None:
        receipt["sources_read"] = sources_read
    if fallback_used is not None:
        receipt["fallback_used"] = fallback_used
    if searches_run is not None:
        receipt["searches_run"] = searches_run
    if segments_processed is not None:
        receipt["segments_processed"] = segments_processed
    return receipt


class Timer:
    def __enter__(self):
        self._start = time.monotonic()
        return self

    def __exit__(self, *exc):
        self.elapsed_ms = int((time.monotonic() - self._start) * 1000)


def extract_payer_from_payment_dict(payload: dict) -> str | None:
    """Best-effort extraction of the paying wallet address from an already-parsed
    x402 payment payload dict (shared by the HTTP header path below and the MCP
    `_meta["x402/payment"]` path in app/mcp_server.py, so both log the same
    payer shape to app.db.log_request)."""
    try:
        return (
            payload.get("payload", {}).get("authorization", {}).get("from")
            or payload.get("from")
        )
    except Exception:
        return None


def extract_payer_from_header(payment_header: str | None) -> str | None:
    """Best-effort extraction of the paying wallet address from a raw x402
    Payment-Signature (or legacy X-Payment) header value."""
    if not payment_header:
        return None
    try:
        import base64
        import json

        decoded = base64.b64decode(payment_header + "=" * (-len(payment_header) % 4))
        payload = json.loads(decoded)
        return extract_payer_from_payment_dict(payload)
    except Exception:
        return None


def extract_mpp_payer(authorization_header: str | None) -> str | None:
    """Best-effort extraction of the paying wallet address from an MPP
    `Authorization: Payment <credential>` header (see app/mpp.py) - used so
    MPP-settled calls attribute correctly on the dashboard, same as x402."""
    if not authorization_header or not authorization_header.strip().lower().startswith("payment "):
        return None
    try:
        import base64
        import json

        token = authorization_header.strip()[len("payment "):].strip()
        decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        payload = json.loads(decoded).get("payload") or {}
        return payload.get("from")
    except Exception:
        return None


def extract_payer_address(request) -> str | None:
    """Best-effort extraction of the paying wallet address from either the
    x402 payment header or an MPP Authorization: Payment credential."""
    payment_header = request.headers.get("payment-signature") or request.headers.get("x-payment")
    payer = extract_payer_from_header(payment_header)
    if payer:
        return payer
    return extract_mpp_payer(request.headers.get("authorization"))
