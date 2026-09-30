"""Shared engine for Pack 2's 5 pinned per-model LLM routes (POST
/llm/claude-sonnet, /llm/gpt-mini, /llm/gemini-flash, /llm/llama,
/llm/deepseek), added 2026-09-30. Same OpenAI-format passthrough as POST
/v1/chat/completions (app/handlers/llm_gateway.py) - same pricing formula
(ceiling from max_tokens x the model's real OpenRouter rate x MARKUP),
"exact" scheme only. The only difference from the general gateway: model
AND provider are fixed per route (not read from the request body), each
pinned to whichever OpenRouter provider measured most reliable for that
model on 2026-09-30 - see each route file's own docstring for the real
uptime/latency numbers behind the pin.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app import db
from app.handlers.llm_gateway import _capped_max_tokens, _price_ceiling_usd
from app.receipts import Timer, effective_price, extract_payer_address, make_receipt
from app.upstream.openrouter import OpenRouterError, chat_completion_raw
from app.x402_setup import ROUTE_DESCRIPTIONS


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


async def lookup(model: str, provider: dict, messages: list, max_tokens_raw: Any) -> tuple[dict, float, float]:
    """Returns (data, ceiling_usd, real_cost_usd). Raises OpenRouterError on
    upstream failure - the caller decides how that maps to an HTTP/MCP
    error, matching every other handler in this codebase."""
    capped_max_tokens = _capped_max_tokens(max_tokens_raw)
    ceiling = await _price_ceiling_usd(model, capped_max_tokens, messages)
    data = await chat_completion_raw(model, messages, capped_max_tokens, provider=provider)
    usage = data.get("usage") or {}
    real_cost = float(usage.get("cost") or 0.0)
    return data, ceiling, real_cost


def make_router(*, route_path: str, route_key: str, model: str, provider: dict, description_key: str, sample_request: dict, sample_response: dict):
    router = APIRouter()

    @router.get(f"{route_path}/sample", openapi_extra={"security": []})
    async def _sample():
        return {"request": sample_request, "response": sample_response}

    @router.post(route_path, description=ROUTE_DESCRIPTIONS[description_key])
    async def _post(request: Request):
        payer = extract_payer_address(request)
        user_agent = request.headers.get("user-agent")
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        body_excerpt = json.dumps(body)[:2000]

        if body.get("stream"):
            db.log_request(
                route=route_key, method="POST", status="error", payer=payer,
                user_agent=user_agent, body_excerpt=body_excerpt, error_reason="streaming_not_supported",
            )
            return JSONResponse(
                {"error": {"reason": "streaming_not_supported", "detail": "This route is non-streaming only; omit stream or set it to false."}},
                status_code=400,
            )

        messages = _validate_messages(body)
        if messages is None:
            db.log_request(
                route=route_key, method="POST", status="error", payer=payer,
                user_agent=user_agent, body_excerpt=body_excerpt, error_reason="invalid_request",
            )
            return JSONResponse(
                {"error": {"reason": "invalid_request", "detail": "Body needs a non-empty 'messages' array of {role, content} objects."}},
                status_code=400,
            )

        try:
            with Timer() as t:
                data, ceiling, real_cost = await lookup(model, provider, messages, body.get("max_tokens"))
        except OpenRouterError as exc:
            db.log_request(
                route=route_key, method="POST", status="error", payer=payer,
                user_agent=user_agent, body_excerpt=body_excerpt, error_reason=str(exc)[:200],
            )
            return JSONResponse({"error": {"reason": "upstream_error", "detail": str(exc)[:200]}}, status_code=502)

        billed = effective_price(payer, ceiling)
        db.log_request(
            route=route_key, method="POST", status="paid", latency_ms=t.elapsed_ms,
            amount_usdc=billed, payer=payer, user_agent=user_agent, body_excerpt=body_excerpt,
            upstream_cost_usd=real_cost, margin_usd=billed - real_cost,
        )
        receipt = make_receipt(model, route_key, t.elapsed_ms, billed)
        return Response(content=json.dumps({**data, "x402_receipt": receipt}), media_type="application/json")

    return router
