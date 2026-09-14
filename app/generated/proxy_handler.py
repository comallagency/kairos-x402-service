"""Generic FastAPI handler for `handler_type: http_proxy` routes: calls the
declared upstream, bills, and returns the reshaped result. Covers the
"repackage one upstream API" case that most generated routes will be
(the agentutility precedent: ~800 routes, a handful of generic wrapper
patterns, not 800 bespoke handlers) without writing new Python per route.

A route whose upstream needs real reshaping/validation beyond passthrough
gets `handler_type: custom` and its own module instead - not built here.

Mirrors the exact logging/pricing/receipt pattern already used by
app/handlers/{search,translate,jobs}.py so a generated route's accounting
looks identical to a hand-built one on the dashboard.
"""

import json

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import db
from app.generated.registry import RouteSpec
from app.receipts import Timer, effective_price, extract_payer_address, make_receipt, price_float

UPSTREAM_TIMEOUT_SECONDS = 30.0


async def call_upstream(spec: RouteSpec, body: dict) -> dict:
    method = spec.upstream.get("method", "POST").upper()
    url = spec.upstream["url"]
    async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT_SECONDS) as client:
        if method == "GET":
            resp = await client.get(url, params=body)
        else:
            resp = await client.request(method, url, json=body)
        resp.raise_for_status()
        return resp.json()


def build_proxy_router(spec: RouteSpec) -> APIRouter:
    router = APIRouter()
    path = f"/{spec.slug}"
    price = price_float(spec.price)

    @router.get(f"{path}/sample", openapi_extra={"security": []}, name=f"{spec.slug}_sample")
    async def sample():
        try:
            result = await call_upstream(spec, spec.upstream.get("sample_body", {}))
        except Exception as exc:
            return JSONResponse(
                {"error": {"reason": "upstream_error", "detail": str(exc)[:200]}}, status_code=502
            )
        receipt = make_receipt(None, "http_proxy", 0, 0.0)
        return {**result, "x402_receipt": receipt}

    @router.post(path, description=spec.description, name=spec.slug)
    async def handler(request: Request):
        payer = extract_payer_address(request)
        user_agent = request.headers.get("user-agent")
        try:
            body = await request.json()
        except Exception:
            body = {}
        body_excerpt = json.dumps(body)

        try:
            with Timer() as t:
                result = await call_upstream(spec, body)
        except Exception as exc:
            db.log_request(
                route=spec.slug, method="POST", status="error", payer=payer,
                user_agent=user_agent, body_excerpt=body_excerpt, error_reason=str(exc)[:200],
            )
            return JSONResponse(
                {"error": {"reason": "upstream_error", "detail": str(exc)[:200]}}, status_code=502
            )

        effective = effective_price(payer, price)
        db.log_request(
            route=spec.slug, method="POST", status="paid",
            latency_ms=t.elapsed_ms, amount_usdc=effective, payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt,
        )
        receipt = make_receipt(None, "http_proxy", t.elapsed_ms, effective)
        return {**result, "x402_receipt": receipt}

    return router
