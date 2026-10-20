"""GET|POST /wallet-balance — native coin and USDC balances across EVM chains."""

from __future__ import annotations

import asyncio
import json
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import config, db
from app.receipts import Timer, effective_price, extract_payer_address, make_receipt, price_float
from app.upstream.evm_rpc import EvmRpcError, resolve_network, rpc
from app.x402_setup import ROUTE_DESCRIPTIONS

router = APIRouter()
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

SAMPLE_RESPONSE = {
    "address": "0xb3F32bdfe8D07825BC0D7387295aB1D7559BA69d",
    "network": {"key": "base", "name": "Base", "caip2": "eip155:8453"},
    "block_number": 51500000,
    "native": {"symbol": "ETH", "balance": 0.001, "balance_wei": "1000000000000000"},
    "usdc": {
        "balance": 1.25,
        "balance_atomic": "1250000",
        "decimals": 6,
        "contract": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    },
}


async def _lookup(body: dict) -> dict:
    address = str(body.get("address") or body.get("wallet") or "").strip()
    if not _ADDRESS_RE.fullmatch(address):
        raise EvmRpcError("invalid_address")
    network = resolve_network(body.get("network") or body.get("chain"))
    balance_of = "0x70a08231000000000000000000000000" + address[2:].lower()
    native_hex, usdc_hex, block_hex = await asyncio.gather(
        rpc(network, "eth_getBalance", [address, "latest"]),
        rpc(network, "eth_call", [{"to": network.usdc, "data": balance_of}, "latest"]),
        rpc(network, "eth_blockNumber", []),
    )
    native_wei = int(native_hex, 16)
    usdc_atomic = int(usdc_hex, 16)
    return {
        "address": address,
        "network": {"key": network.key, "name": network.name, "caip2": network.caip2},
        "block_number": int(block_hex, 16),
        "native": {
            "symbol": network.native_symbol,
            "balance": native_wei / 1e18,
            "balance_wei": str(native_wei),
        },
        "usdc": {
            "balance": usdc_atomic / 1_000_000,
            "balance_atomic": str(usdc_atomic),
            "decimals": 6,
            "contract": network.usdc,
        },
    }


@router.get("/wallet-balance/sample", openapi_extra={"security": []})
async def wallet_balance_sample():
    return {
        **SAMPLE_RESPONSE,
        "supported_networks": ["base", "ethereum", "polygon", "arbitrum", "optimism"],
        "x402_receipt": make_receipt(None, "wallet-balance", 1, 0.0),
    }


async def _paid(request: Request, body: dict):
    payer = extract_payer_address(request)
    user_agent = request.headers.get("user-agent")
    body_excerpt = json.dumps(body)[:2000]
    try:
        with Timer() as timer:
            result = await _lookup(body)
    except EvmRpcError as exc:
        reason = str(exc)
        code = 400 if reason in {"invalid_address", "unsupported_network"} else 502
        db.log_request(
            route="wallet-balance", method=request.method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason=reason,
        )
        return JSONResponse({"error": {"reason": reason}}, status_code=code)
    price = effective_price(payer, price_float(config.PRICE_WALLET_BALANCE))
    db.log_request(
        route="wallet-balance", method=request.method, status="paid",
        latency_ms=timer.elapsed_ms, amount_usdc=price, payer=payer,
        user_agent=user_agent, body_excerpt=body_excerpt,
    )
    return {
        **result,
        "x402_receipt": make_receipt(None, "wallet-balance", timer.elapsed_ms, price),
    }


@router.get("/wallet-balance", description=ROUTE_DESCRIPTIONS["wallet-balance"])
async def wallet_balance_get(
    request: Request,
    address: str | None = None,
    network: str = "base",
):
    return await _paid(request, {"address": address, "network": network})


@router.post("/wallet-balance", description=ROUTE_DESCRIPTIONS["wallet-balance"])
async def wallet_balance_post(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _paid(request, body)
