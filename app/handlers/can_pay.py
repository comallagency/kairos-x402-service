"""GET|POST /can-pay — Base USDC affordability check for agent wallets."""

from __future__ import annotations

import json
import re

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import config, db
from app.receipts import Timer, effective_price, extract_payer_address, make_receipt, price_float
from app.x402_setup import ROUTE_DESCRIPTIONS

router = APIRouter()

_RPC = "https://mainnet.base.org"
_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
_TIMEOUT = 15.0
_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

SAMPLE_RESPONSE = {
    "address": "0xb3F32bdfe8D07825BC0D7387295aB1D7559BA69d",
    "network": "eip155:8453",
    "usdc": {"balance": 0.0, "balance_atomic": "0", "contract": _USDC},
    "eth": {"balance": 0.0},
    "amount_requested": 0.001,
    "can_pay": False,
    "shortfall_usdc": 0.001,
}


class CanPayError(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


async def _rpc(method: str, params: list) -> dict | str | int:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            _RPC,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        resp.raise_for_status()
        data = resp.json()
    if "error" in data:
        raise CanPayError("rpc_error")
    return data.get("result")


async def _lookup(body: dict) -> dict:
    address = (body.get("address") or body.get("wallet") or body.get("from") or "").strip()
    if not _ADDR_RE.match(address):
        raise CanPayError("invalid_address")
    try:
        amount = float(body.get("amount") if body.get("amount") is not None else 0.001)
    except (TypeError, ValueError) as exc:
        raise CanPayError("invalid_amount") from exc
    if amount < 0 or amount > 1000:
        raise CanPayError("invalid_amount")

    # balanceOf(address)
    data = "0x70a08231000000000000000000000000" + address[2:].lower()
    usdc_hex = await _rpc("eth_call", [{"to": _USDC, "data": data}, "latest"])
    eth_hex = await _rpc("eth_getBalance", [address, "latest"])
    usdc_atomic = int(usdc_hex, 16) if isinstance(usdc_hex, str) else 0
    eth_wei = int(eth_hex, 16) if isinstance(eth_hex, str) else 0
    usdc = usdc_atomic / 1_000_000
    eth = eth_wei / 1e18
    can = usdc + 1e-12 >= amount
    shortfall = 0.0 if can else round(amount - usdc, 6)
    return {
        "address": address,
        "network": "eip155:8453",
        "usdc": {
            "balance": round(usdc, 6),
            "balance_atomic": str(usdc_atomic),
            "contract": _USDC,
        },
        "eth": {"balance": round(eth, 8)},
        "amount_requested": amount,
        "can_pay": can,
        "shortfall_usdc": shortfall,
        "note": (
            "x402 exact-scheme payments are gasless for the payer (facilitator "
            "submits). can_pay is USDC-only; ETH is informational."
        ),
    }


@router.get("/can-pay/sample", openapi_extra={"security": []})
async def can_pay_sample():
    return {**SAMPLE_RESPONSE, "x402_receipt": make_receipt(None, "can-pay", 1, 0.0)}


async def _paid(request: Request, body: dict):
    payer = extract_payer_address(request)
    user_agent = request.headers.get("user-agent")
    body_excerpt = json.dumps(body)[:2000]
    method = request.method
    try:
        with Timer() as t:
            result = await _lookup(body)
    except CanPayError as exc:
        db.log_request(
            route="can-pay", method=method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason=exc.reason,
        )
        return JSONResponse({"error": {"reason": exc.reason}}, status_code=400)
    except Exception as exc:
        db.log_request(
            route="can-pay", method=method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason=str(exc)[:200],
        )
        return JSONResponse(
            {"error": {"reason": "upstream_error", "detail": str(exc)[:200]}}, status_code=502
        )
    price = effective_price(payer, price_float(config.PRICE_CAN_PAY))
    db.log_request(
        route="can-pay", method=method, status="paid", latency_ms=t.elapsed_ms,
        amount_usdc=price, payer=payer, user_agent=user_agent, body_excerpt=body_excerpt,
    )
    return {**result, "x402_receipt": make_receipt(None, "can-pay", t.elapsed_ms, price)}


@router.get("/can-pay", description=ROUTE_DESCRIPTIONS["can-pay"])
async def can_pay_get(
    request: Request,
    address: str | None = None,
    amount: float = 0.001,
):
    return await _paid(request, {"address": address, "amount": amount})


@router.post("/can-pay", description=ROUTE_DESCRIPTIONS["can-pay"])
async def can_pay_post(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _paid(request, body)
