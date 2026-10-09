"""POST /v1/chat/completions - OpenAI-compatible pay-per-call LLM gateway.

Reopens the OpenRouter passthrough this service used to offer before the
account balance went negative (2026-09-07). Balance is positive again
(~$7.95, 2026-09-28) - this is a fresh build, not a re-enable.

accepts[] currently offers "exact" (EIP-3009) only, priced at the
per-request ceiling (compute_ceiling_price - real prompt/completion tokens
x the model's real rate x MARKUP, floored at MIN_SETTLE_USD). No ETH, no
Permit2 allowance needed from the buyer.

"upto" (Permit2-based - settles real OpenRouter cost x MARKUP instead of
the full ceiling, cheaper for the buyer when the real call costs much less
than the worst case) was added 2026-09-28 alongside "exact" as a second
accepts[] option, then disabled the same day (UPTO_ENABLED = False below):
the route never appeared in Bazaar even an hour after a real, confirmed
on-chain settlement, and the only structural difference from every other
route that does get indexed was this second, dynamically-priced, non-exact
option - Bazaar's own SDK-side code (x402/extensions/bazaar,
x402/http/middleware/_bazaar_utils.py) never mentions "upto" at all,
suggesting that path was only ever built and tested against "exact". Not
proven (CDP's indexer is closed), but the leading hypothesis, and the
UPTO_ENABLED gate exists so this can be flipped back on for a controlled
retest, or once CDP confirms/denies the hypothesis, without re-deriving any
of the settlement logic below - compute_ceiling_price, the settle-amount
clamp, and the scheme-branch in chat_completions()/ask_model_tool all
already handle both schemes correctly and are unchanged.

Non-streaming only. GET /v1/models is free.

Free OpenRouter models (price 0 on either side, or a ":free" id suffix -
the two don't always agree exactly, both are checked) are excluded from
both GET /v1/models and POST /v1/chat/completions (2026-09-28): there is
no real ceiling to size "exact" against and nothing for "upto" to settle
beyond the MIN_SETTLE_USD floor either way, so serving them would just be
charging a buyer for someone else's free capacity. Requesting one 400s,
unbilled, with reason "free_model_not_supported" (distinct from
"unknown_model", so the buyer can tell "doesn't exist" from "exists but
excluded" apart).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from x402.http.middleware.fastapi import set_settlement_overrides

from app import config, db
from app.receipts import Timer, effective_price, extract_payer_address, extract_scheme_from_header, make_receipt
from app.upstream.openrouter import OpenRouterError, chat_completion_raw, get_models
from app.upstream.tokencount import count_tokens
from app.x402_setup import ROUTE_DESCRIPTIONS

router = APIRouter()

MARKUP = 1.10
MIN_SETTLE_USD = 0.001
MAX_TOKENS_CAP = 4096
DEFAULT_MAX_TOKENS = 1024

# 20s hard server-side deadline (2026-09-30, correction avant indexation):
# never settle without a delivered response. Any model/provider combo
# the buyer picks gets the same ceiling - unlike Pack 2's pinned routes,
# there is no fixed second provider to fall back to here (the buyer's
# model choice is arbitrary), so a miss is a plain 504, unsettled.
GLOBAL_TIMEOUT_S = 20.0

# See module docstring: flip to True to re-offer "upto" as a second
# accepts[] option (app/x402_setup.py's RouteConfig and mcp_server.py's
# ask_model_tool both read this). Everything else - compute_ceiling_price,
# the settle-amount clamp, the scheme branch in chat_completions() and
# ask_model_tool - already handles both schemes and needs no change either
# way.
UPTO_ENABLED = False


class LLMGatewayError(Exception):
    def __init__(self, reason: str, detail: str | None = None):
        self.reason = reason
        self.detail = detail
        super().__init__(reason)


def _is_free_model(model_id: str, prompt_price: float, completion_price: float) -> bool:
    """A free model has nothing for the "upto" scheme to settle (real cost
    x MARKUP floors at MIN_SETTLE_USD regardless, i.e. we'd be charging a
    buyer for someone else's free capacity) and no real ceiling to size
    "exact" against either - excluded from both GET /v1/models and
    POST /v1/chat/completions (2026-09-28). Checks both the price (0, not
    just <=0 - a negative price is caught earlier by get_models()'s own
    "-1 means variable/unpriced" filter, not this one) and OpenRouter's own
    ":free" id suffix, since the two don't always agree exactly."""
    return prompt_price == 0 or completion_price == 0 or model_id.endswith(":free")


async def _priced_models_map() -> dict[str, tuple[float, float]]:
    """{model_id: (prompt_price_per_token, completion_price_per_token)},
    upstream OpenRouter prices, free models excluded - NOT marked up (see
    _markup_models_payload for the buyer-facing, marked-up GET /v1/models
    response)."""
    models = await get_models()
    out = {}
    for m in models:
        pricing = m.get("pricing") or {}
        try:
            prompt_price, completion_price = float(pricing["prompt"]), float(pricing["completion"])
        except (KeyError, TypeError, ValueError):
            continue
        if _is_free_model(m["id"], prompt_price, completion_price):
            continue
        out[m["id"]] = (prompt_price, completion_price)
    return out


def _capped_max_tokens(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_TOKENS
    return max(1, min(value, MAX_TOKENS_CAP))


def _estimate_prompt_tokens(messages: Any) -> int:
    """Ceiling-time estimate only - the real settlement uses OpenRouter's
    own usage.prompt_tokens, not this. Rough but conservative: serializes
    the whole messages structure rather than trying to parse role/content
    shapes that might be malformed at pricing time."""
    if not messages:
        return 0
    try:
        return count_tokens(json.dumps(messages))
    except (TypeError, ValueError):
        return 0


async def _price_ceiling_usd(model: Any, max_tokens_raw: Any, messages: Any) -> float:
    """Shared by the upfront x402 "upto" price quote (app/x402_setup.py's
    compute_ceiling_price, called before the buyer has necessarily sent a
    valid model) and the post-call clamp below (so the real settlement can
    never exceed what was actually authorized). An unrecognized model - the
    request will 400, unbilled, in the handler regardless - falls back to
    the single most expensive known model's rate, never a crash, since a
    DynamicPrice callback that raises would break the 402 challenge itself
    for every buyer, not just the one sending a bad model name."""
    capped_max_tokens = _capped_max_tokens(max_tokens_raw)
    prompt_tokens = _estimate_prompt_tokens(messages)
    priced = await _priced_models_map()
    pricing = priced.get(model) if isinstance(model, str) else None
    if pricing is None:
        # Unrecognized model - the request 400s in the handler regardless
        # (unbilled), so this only has to be safely conservative, never
        # crash the price quote. max() on (prompt, completion) tuples would
        # pick whichever model has the single highest *prompt* price, not
        # necessarily the model that is actually worst-case for this
        # formula - the two prices are maximized independently instead, so
        # the fallback ceiling is never lower than any real model's cost.
        prompt_price = max((p for p, _ in priced.values()), default=0.00002)
        completion_price = max((c for _, c in priced.values()), default=0.0006)
    else:
        prompt_price, completion_price = pricing
    ceiling = (prompt_tokens * prompt_price + capped_max_tokens * completion_price) * MARKUP
    return max(ceiling, MIN_SETTLE_USD)


async def compute_ceiling_price(context) -> str:
    """x402 DynamicPrice callback for the "upto" scheme (see
    app/x402_setup.py's RouteConfig for POST /v1/chat/completions).

    The x402 SDK's FastAPIAdapter.get_body() is a stub that always returns
    None ("Body requires async access" - x402/http/middleware/fastapi.py)
    so there is no public, documented way to read a POST body from inside a
    DynamicPrice callback in this SDK version. Reaching into
    adapter._request (the underlying Starlette Request the adapter wraps)
    and awaiting .json() ourselves is the only working path; Starlette
    caches the parsed body internally, so the handler's own later
    `await request.json()` re-reads the cache rather than the (already
    consumed) ASGI stream - this does not double-read anything."""
    body: dict[str, Any] = {}
    try:
        starlette_request = context.adapter._request
        body = await starlette_request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    ceiling = await _price_ceiling_usd(body.get("model"), body.get("max_tokens"), body.get("messages"))
    return f"${ceiling:.6f}"


# --- GET /v1/models - free --------------------------------------------------

async def _markup_models_payload() -> dict[str, Any]:
    models = await get_models()
    data = []
    for m in models:
        pricing = m.get("pricing") or {}
        try:
            raw_prompt = float(pricing["prompt"])
            raw_completion = float(pricing["completion"])
        except (KeyError, TypeError, ValueError):
            continue
        if _is_free_model(m["id"], raw_prompt, raw_completion):
            continue
        prompt = raw_prompt * MARKUP
        completion = raw_completion * MARKUP
        data.append(
            {
                "id": m.get("id"),
                "name": m.get("name"),
                "context_length": m.get("context_length"),
                "pricing": {"prompt": f"{prompt:.9f}", "completion": f"{completion:.9f}"},
            }
        )
    return {"object": "list", "data": data}


@router.get(
    "/v1/models",
    openapi_extra={"security": []},
    description="Free. Lists every paid model the LLM gateway can serve, with per-token pricing (upstream cost x 1.10). Free OpenRouter models (price 0 or a \":free\" id suffix) are excluded - see POST /v1/chat/completions.",
)
async def list_models():
    payload = await _markup_models_payload()
    return JSONResponse(payload)


# --- POST /v1/chat/completions/sample - free, static ------------------------
# Captured from a real call (2026-09-28) - static, no live call for /sample.

SAMPLE_REQUEST = {
    "model": "openai/gpt-4o-mini",
    "messages": [{"role": "user", "content": "Say OK."}],
    "max_tokens": 5,
}
SAMPLE_RESPONSE = {
    "id": "gen-1790576252-UzfZJjqk0Ld9PGyd1ADA",
    "object": "chat.completion",
    "model": "openai/gpt-4o-mini",
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "OK."},
        }
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    "x402_receipt": make_receipt("openai/gpt-4o-mini", "llm-gateway", 612, 0.001),
}


@router.get(
    "/v1/chat/completions/sample",
    openapi_extra={"security": []},
    description="Replays a fixed request/response pair - free, no payment.",
)
async def chat_completions_sample():
    return {"request": SAMPLE_REQUEST, "response": SAMPLE_RESPONSE}


async def _unpriced_model_error(model: str) -> tuple[str, str]:
    """Distinguishes "not an OpenRouter model at all" from "it's free, and
    free models are excluded" (2026-09-28) - both land in the same
    `model not in priced` check, but the buyer sees a different, honest
    reason for each rather than one generic "unknown_model" either way."""
    all_models = await get_models()
    for m in all_models:
        if m.get("id") != model:
            continue
        pricing = m.get("pricing") or {}
        try:
            p, c = float(pricing["prompt"]), float(pricing["completion"])
        except (KeyError, TypeError, ValueError):
            break
        if _is_free_model(model, p, c):
            return "free_model_not_supported", f"{model!r} is a free model - this gateway only serves paid models. See GET /v1/models."
        break
    return "unknown_model", f"{model!r} is not in GET /v1/models."


# --- POST /v1/chat/completions - paid, "upto" scheme ------------------------

def _validate_body(body: dict) -> tuple[str, list, int] | tuple[None, None, None]:
    """Returns (model, messages, capped_max_tokens) or (None, None, None) if
    body itself is malformed in a way no specific reason below covers."""
    if not isinstance(body, dict):
        return None, None, None
    model = body.get("model")
    messages = body.get("messages")
    if not isinstance(model, str) or not model:
        return None, None, None
    if not isinstance(messages, list) or not messages:
        return None, None, None
    for m in messages:
        if not isinstance(m, dict) or "role" not in m or "content" not in m:
            return None, None, None
    return model, messages, _capped_max_tokens(body.get("max_tokens"))


async def _handle_chat_completions(request: Request, body):
    method = request.method
    payer = extract_payer_address(request)
    user_agent = request.headers.get("user-agent")
    body_excerpt = json.dumps(body)[:2000] if isinstance(body, dict) else ""

    if isinstance(body, dict) and body.get("stream"):
        db.log_request(
            route="v1/chat/completions", method=method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason="streaming_not_supported",
        )
        return JSONResponse(
            {"error": {"reason": "streaming_not_supported", "detail": "This gateway is non-streaming only; omit stream or set it to false."}},
            status_code=400,
        )

    priced = await _priced_models_map()
    model, messages, capped_max_tokens = _validate_body(body)
    if model is None:
        db.log_request(
            route="v1/chat/completions", method=method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason="invalid_request",
        )
        return JSONResponse(
            {"error": {"reason": "invalid_request", "detail": "Body needs a string 'model' and a non-empty 'messages' array of {role, content} objects."}},
            status_code=400,
        )
    if model not in priced:
        reason, detail = await _unpriced_model_error(model)
        db.log_request(
            route="v1/chat/completions", method=method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason=reason,
        )
        return JSONResponse({"error": {"reason": reason, "detail": detail}}, status_code=400)

    ceiling = await _price_ceiling_usd(model, capped_max_tokens, messages)

    try:
        with Timer() as t:
            data = await asyncio.wait_for(chat_completion_raw(model, messages, capped_max_tokens), timeout=GLOBAL_TIMEOUT_S)
    except asyncio.TimeoutError:
        db.log_request(
            route="v1/chat/completions", method=method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason="upstream_timeout",
        )
        return JSONResponse(
            {"error": {"reason": "upstream_timeout", "detail": f"No response within {GLOBAL_TIMEOUT_S:.0f}s - not charged. Set your client timeout to 30s."}},
            status_code=504,
        )
    except OpenRouterError as exc:
        db.log_request(
            route="v1/chat/completions", method=method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason=str(exc)[:200],
        )
        return JSONResponse({"error": {"reason": "upstream_error", "detail": str(exc)[:200]}}, status_code=502)

    usage = data.get("usage") or {}
    real_cost = float(usage.get("cost") or 0.0)

    # Two accepts[] options (see app/x402_setup.py's RouteConfig for this
    # route): "exact" settles the full declared ceiling regardless of real
    # usage - the buyer already signed for exactly that amount, and
    # PaymentPayload.accepted (embedded in the payment header the buyer
    # sent) says which one they actually used, so no re-verification is
    # needed here, the middleware already did that. Only "upto" gets a
    # reduced settlement.
    payment_header = request.headers.get("payment-signature") or request.headers.get("x-payment")
    scheme = extract_scheme_from_header(payment_header)
    if scheme == "upto":
        settle_amount = min(max(real_cost * MARKUP, MIN_SETTLE_USD), ceiling)
    else:
        settle_amount = ceiling
    margin = settle_amount - real_cost
    billed = effective_price(payer, settle_amount)

    db.log_request(
        route="v1/chat/completions", method=method, status="paid", latency_ms=t.elapsed_ms,
        amount_usdc=billed, payer=payer, user_agent=user_agent, body_excerpt=body_excerpt,
        upstream_cost_usd=real_cost, margin_usd=margin,
    )

    receipt = make_receipt(model, "llm-gateway", t.elapsed_ms, billed)
    fast_response = Response(
        content=json.dumps({**data, "x402_receipt": receipt}),
        media_type="application/json",
    )
    if scheme == "upto":
        set_settlement_overrides(fast_response, {"amount": f"${settle_amount:.6f}"})
    return fast_response


@router.get("/v1/chat/completions", description=ROUTE_DESCRIPTIONS["llm-gateway"])
async def chat_completions_get(request: Request):
    # GET-twin delivery fix (2026-10-09): still OpenRouter-only, same models
    # GET /v1/models already lists - no Anthropic key, no new upstream, just
    # a delivering handler for a GET that already accepts payment. A bare
    # GET with no params replays SAMPLE_REQUEST; model/prompt params build a
    # single-user-message request.
    params = dict(request.query_params)
    if not params:
        body = dict(SAMPLE_REQUEST)
    else:
        body = {
            "model": params.get("model", SAMPLE_REQUEST["model"]),
            "messages": [{"role": "user", "content": params.get("prompt") or params.get("q") or "Say OK."}],
        }
        if "max_tokens" in params:
            body["max_tokens"] = params["max_tokens"]
    return await _handle_chat_completions(request, body)


@router.post("/v1/chat/completions", description=ROUTE_DESCRIPTIONS["llm-gateway"])
async def chat_completions(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _handle_chat_completions(request, body)
