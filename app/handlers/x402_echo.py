"""GET|POST /x402-echo — cheapest real Base-mainnet x402 conformance endpoint ($0.001)."""

from __future__ import annotations

import json

from fastapi import APIRouter, Request

from app import config, db
from app.receipts import Timer, effective_price, extract_payer_address, make_receipt, price_float
from app.x402_setup import ROUTE_DESCRIPTIONS

router = APIRouter()

SAMPLE_RESPONSE = {
    "ok": True,
    "purpose": "x402-mainnet-conformance",
    "network": "eip155:8453",
    "price_usdc": 0.001,
    "price_atomic_usdc": "1000",
    "payer": "0xb3F32bdfe8D07825BC0D7387295aB1D7559BA69d",
    "request": {"method": "POST", "echo": {"hello": "agent"}},
}


@router.get("/x402-echo/sample", openapi_extra={"security": []})
async def x402_echo_sample():
    return {
        **SAMPLE_RESPONSE,
        "note": "Paid route settles $0.001 of Base USDC.",
        "x402_receipt": make_receipt(None, "x402-echo", 1, 0.0),
    }


async def _paid(
    request: Request,
    payload: object,
    *,
    route: str = "x402-echo",
    purpose: str = "x402-mainnet-conformance",
    configured_price: str | None = None,
):
    configured_price = configured_price or config.PRICE_X402_ECHO
    payer = extract_payer_address(request)
    user_agent = request.headers.get("user-agent")
    body_excerpt = json.dumps(payload, ensure_ascii=False)[:2000]
    with Timer() as timer:
        result = {
            "ok": True,
            "purpose": purpose,
            "network": config.X402_NETWORK,
            "price_usdc": price_float(configured_price),
            "price_atomic_usdc": str(round(price_float(configured_price) * 1_000_000)),
            "payer": payer,
            "request": {
                "method": request.method,
                "echo": payload,
                "user_agent": user_agent,
            },
        }
    price = effective_price(payer, price_float(configured_price))
    db.log_request(
        route=route, method=request.method, status="paid",
        latency_ms=timer.elapsed_ms, amount_usdc=price, payer=payer,
        user_agent=user_agent, body_excerpt=body_excerpt,
    )
    return {
        **result,
        "x402_receipt": make_receipt(None, route, timer.elapsed_ms, price),
    }


@router.get("/x402-echo", description=ROUTE_DESCRIPTIONS["x402-echo"])
async def x402_echo_get(request: Request, message: str = "hello"):
    return await _paid(request, {"message": message})


@router.post("/x402-echo", description=ROUTE_DESCRIPTIONS["x402-echo"])
async def x402_echo_post(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    return await _paid(request, payload)


@router.get("/tip/sample", openapi_extra={"security": []})
async def tip_sample():
    return {
        **SAMPLE_RESPONSE,
        "purpose": "support-agentindex",
        "price_usdc": 0.01,
        "price_atomic_usdc": "10000",
        "note": "The paid route sends a voluntary one-cent Base USDC tip.",
    }


@router.get("/tip", description=ROUTE_DESCRIPTIONS["tip"])
async def tip_get(request: Request, message: str = "Keep building"):
    return await _paid(
        request,
        {"message": message},
        route="tip",
        purpose="support-agentindex",
        configured_price=config.PRICE_TIP,
    )


@router.post("/tip", description=ROUTE_DESCRIPTIONS["tip"])
async def tip_post(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    return await _paid(
        request,
        payload,
        route="tip",
        purpose="support-agentindex",
        configured_price=config.PRICE_TIP,
    )
