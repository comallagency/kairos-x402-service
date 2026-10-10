"""Shared engine for Pack 2's 5 pinned per-model LLM routes (POST
/llm/claude-sonnet, /llm/gpt-mini, /llm/gemini-flash, /llm/llama,
/llm/deepseek), added 2026-09-30. Same OpenAI-format passthrough as POST
/v1/chat/completions (app/handlers/llm_gateway.py) - same pricing formula
(ceiling from max_tokens x the model's real OpenRouter rate x MARKUP),
"exact" scheme only. The only difference from the general gateway: model
AND provider are fixed per route (not read from the request body).

2026-10-10: each route's `provider` is now an ORDERED LIST of 2-3 live
providers (OpenRouter's own native {"order": [...], "allow_fallbacks":
False} provider-routing field - tried in sequence, server-side, inside
ONE API call) instead of a single hard pin. This replaces the homemade
two-stage primary/FALLBACK_PROVIDER retry that used to live here (and the
PRIMARY_TIMEOUT_S split that came with it) - OpenRouter already does this
natively, nothing to build. Every provider in every route's list was
checked against GET /api/v1/models/{model}/endpoints for live status AND
against _price_ceiling_usd's own reference price for that model, so
margin (ceiling x MARKUP vs that provider's real per-token cost) stays
positive no matter which listed provider actually serves a given call -
see each route file's own docstring for the real numbers behind its list.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app import db
from app.handlers.llm_gateway import _capped_max_tokens, _price_ceiling_usd
from app.receipts import Timer, effective_price, extract_payer_address, make_receipt
from app.upstream.openrouter import OpenRouterError, chat_completion_raw
from app.x402_setup import ROUTE_DESCRIPTIONS

# 20s hard server-side deadline (2026-09-30, correction avant indexation):
# never settle without a delivered response. The OUTER wait_for in
# lookup() is the real ceiling here - fires even if OpenRouter's own
# provider-order routing takes longer than expected internally, so "20s
# max, always" holds by construction regardless of how many providers in
# the order list get tried server-side.
GLOBAL_TIMEOUT_S = 20.0


def _validate_messages(body: dict) -> list | None:
    messages = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(messages, list) or not messages:
        return None
    for m in messages:
        if not isinstance(m, dict) or "role" not in m or "content" not in m:
            return None
    return messages


def make_price_fn(model: str):
    """x402 DynamicPrice callback, model fixed - see
    llm_gateway.compute_ceiling_price's own docstring for why body access
    needs adapter._request.json() rather than the SDK's own get_body()."""
    async def _price(context) -> str:
        body: dict[str, Any] = {}
        try:
            starlette_request = context.adapter._request
            body = await starlette_request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        ceiling = await _price_ceiling_usd(model, body.get("max_tokens"), body.get("messages"))
        return f"${ceiling:.6f}"
    return _price


async def _attempt(model: str, provider: dict, messages: list, capped_max_tokens: int) -> dict:
    return await chat_completion_raw(model, messages, capped_max_tokens, provider=provider)


async def lookup(model: str, provider: dict, messages: list, max_tokens_raw: Any) -> tuple[dict, float, float]:
    """Returns (data, ceiling_usd, real_cost_usd). Raises OpenRouterError on
    a genuine upstream failure (mapped to 502 by the caller), or
    asyncio.TimeoutError if nothing came back within GLOBAL_TIMEOUT_S
    (mapped to 504, never settled - see make_router's _post()). `provider`
    carries the route's ordered {"order": [...], "allow_fallbacks": False}
    list - OpenRouter tries each entry in sequence server-side, inside
    this one call; no client-side retry loop needed here."""
    capped_max_tokens = _capped_max_tokens(max_tokens_raw)
    ceiling = await _price_ceiling_usd(model, capped_max_tokens, messages)
    data = await asyncio.wait_for(
        _attempt(model, provider, messages, capped_max_tokens),
        timeout=GLOBAL_TIMEOUT_S,
    )
    usage = data.get("usage") or {}
    real_cost = float(usage.get("cost") or 0.0)
    return data, ceiling, real_cost


def make_router(*, route_path: str, route_key: str, model: str, provider: dict, description_key: str, sample_request: dict, sample_response: dict):
    router = APIRouter()

    @router.get(f"{route_path}/sample", openapi_extra={"security": []})
    async def _sample():
        return {"request": sample_request, "response": sample_response}

    async def _paid(request: Request, body: dict):
        payer = extract_payer_address(request)
        user_agent = request.headers.get("user-agent")
        body = body if isinstance(body, dict) else {}
        body_excerpt = json.dumps(body)[:2000]
        method = request.method

        if body.get("stream"):
            db.log_request(
                route=route_key, method=method, status="error", payer=payer,
                user_agent=user_agent, body_excerpt=body_excerpt, error_reason="streaming_not_supported",
            )
            return JSONResponse(
                {"error": {"reason": "streaming_not_supported", "detail": "This route is non-streaming only; omit stream or set it to false."}},
                status_code=400,
            )

        messages = _validate_messages(body)
        if messages is None:
            db.log_request(
                route=route_key, method=method, status="error", payer=payer,
                user_agent=user_agent, body_excerpt=body_excerpt, error_reason="invalid_request",
            )
            return JSONResponse(
                {"error": {"reason": "invalid_request", "detail": "Body needs a non-empty 'messages' array of {role, content} objects."}},
                status_code=400,
            )

        try:
            with Timer() as t:
                data, ceiling, real_cost = await lookup(model, provider, messages, body.get("max_tokens"))
        except asyncio.TimeoutError:
            db.log_request(
                route=route_key, method=method, status="error", payer=payer,
                user_agent=user_agent, body_excerpt=body_excerpt, error_reason="upstream_timeout",
            )
            return JSONResponse(
                {"error": {"reason": "upstream_timeout", "detail": f"No response within {GLOBAL_TIMEOUT_S:.0f}s - not charged. Set your client timeout to 30s."}},
                status_code=504,
            )
        except OpenRouterError as exc:
            db.log_request(
                route=route_key, method=method, status="error", payer=payer,
                user_agent=user_agent, body_excerpt=body_excerpt, error_reason=str(exc)[:200],
            )
            return JSONResponse({"error": {"reason": "upstream_error", "detail": str(exc)[:200]}}, status_code=502)

        billed = effective_price(payer, ceiling)
        db.log_request(
            route=route_key, method=method, status="paid", latency_ms=t.elapsed_ms,
            amount_usdc=billed, payer=payer, user_agent=user_agent, body_excerpt=body_excerpt,
            upstream_cost_usd=real_cost, margin_usd=billed - real_cost,
        )
        receipt = make_receipt(model, route_key, t.elapsed_ms, billed)
        return Response(content=json.dumps({**data, "x402_receipt": receipt}), media_type="application/json")

    async def _body_from_request(request: Request, *, use_listing_default: bool) -> dict:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        if _validate_messages(body):
            return body
        prompt = request.query_params.get("q") or request.query_params.get("prompt")
        if prompt:
            max_tokens = request.query_params.get("max_tokens")
            out = {"messages": [{"role": "user", "content": prompt}]}
            if max_tokens:
                out["max_tokens"] = max_tokens
            return out
        if use_listing_default:
            return dict(sample_request)
        return body

    @router.get(route_path, description=ROUTE_DESCRIPTIONS[description_key])
    async def _get(request: Request):
        body = await _body_from_request(request, use_listing_default=True)
        return await _paid(request, body)

    @router.post(route_path, description=ROUTE_DESCRIPTIONS[description_key])
    async def _post(request: Request):
        body = await _body_from_request(request, use_listing_default=False)
        return await _paid(request, body)

    return router
