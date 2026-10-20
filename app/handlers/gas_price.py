"""GET|POST /gas-price — live EVM gas price and transfer-cost estimate."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import config, db
from app.receipts import Timer, effective_price, extract_payer_address, make_receipt, price_float
from app.upstream.evm_rpc import EvmRpcError, resolve_network, rpc
from app.x402_setup import ROUTE_DESCRIPTIONS

router = APIRouter()
_NATIVE_TRANSFER_GAS = 21_000

SAMPLE_RESPONSE = {
    "network": {"key": "base", "name": "Base", "caip2": "eip155:8453"},
    "block_number": 51500000,
    "gas_price": {"wei": "1000000", "gwei": 0.001},
    "base_fee": {"wei": "900000", "gwei": 0.0009},
    "native_transfer_estimate": {
        "gas_limit": 21000,
        "fee_wei": "21000000000",
        "fee_native": 0.000000021,
        "symbol": "ETH",
    },
}


async def _lookup(body: dict) -> dict:
    network = resolve_network(body.get("network") or body.get("chain"))
    gas_hex, block = await asyncio.gather(
        rpc(network, "eth_gasPrice", []),
        rpc(network, "eth_getBlockByNumber", ["latest", False]),
    )
    gas_wei = int(gas_hex, 16)
    block_number = int(block["number"], 16)
    base_fee_hex = block.get("baseFeePerGas")
    base_fee_wei = int(base_fee_hex, 16) if base_fee_hex else None
    fee_wei = gas_wei * _NATIVE_TRANSFER_GAS
    response = {
        "network": {"key": network.key, "name": network.name, "caip2": network.caip2},
        "block_number": block_number,
        "gas_price": {"wei": str(gas_wei), "gwei": gas_wei / 1e9},
        "base_fee": None,
        "native_transfer_estimate": {
            "gas_limit": _NATIVE_TRANSFER_GAS,
            "fee_wei": str(fee_wei),
            "fee_native": fee_wei / 1e18,
            "symbol": network.native_symbol,
        },
    }
    if base_fee_wei is not None:
        response["base_fee"] = {"wei": str(base_fee_wei), "gwei": base_fee_wei / 1e9}
    return response


@router.get("/gas-price/sample", openapi_extra={"security": []})
async def gas_price_sample():
    return {
        **SAMPLE_RESPONSE,
        "supported_networks": ["base", "ethereum", "polygon", "arbitrum", "optimism"],
        "x402_receipt": make_receipt(None, "gas-price", 1, 0.0),
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
        code = 400 if reason == "unsupported_network" else 502
        db.log_request(
            route="gas-price", method=request.method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason=reason,
        )
        return JSONResponse({"error": {"reason": reason}}, status_code=code)
    price = effective_price(payer, price_float(config.PRICE_GAS_PRICE))
    db.log_request(
        route="gas-price", method=request.method, status="paid",
        latency_ms=timer.elapsed_ms, amount_usdc=price, payer=payer,
        user_agent=user_agent, body_excerpt=body_excerpt,
    )
    return {
        **result,
        "x402_receipt": make_receipt(None, "gas-price", timer.elapsed_ms, price),
    }


@router.get("/gas-price", description=ROUTE_DESCRIPTIONS["gas-price"])
async def gas_price_get(request: Request, network: str = "base"):
    return await _paid(request, {"network": network})


@router.post("/gas-price", description=ROUTE_DESCRIPTIONS["gas-price"])
async def gas_price_post(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _paid(request, body)
