"""GET|POST /wallet-intelligence — one paid call replaces ten EVM RPC reads."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import config, db
from app.handlers.gas_price import _lookup as gas_lookup
from app.handlers.wallet_balance import _lookup as balance_lookup
from app.receipts import Timer, effective_price, extract_payer_address, make_receipt, price_float
from app.upstream.evm_rpc import EvmRpcError, NETWORKS
from app.x402_setup import ROUTE_DESCRIPTIONS

router = APIRouter()
_DEFAULT_NETWORKS = ["base", "ethereum", "polygon", "arbitrum", "optimism"]

SAMPLE_RESPONSE = {
    "address": "0xb3F32bdfe8D07825BC0D7387295aB1D7559BA69d",
    "requested_networks": _DEFAULT_NETWORKS,
    "successful_networks": 5,
    "rpc_reads_replaced": 25,
    "x402_readiness": {
        "network": "base",
        "amount_usdc": 0.001,
        "balance_usdc": 1.25,
        "can_pay": True,
        "shortfall_usdc": 0.0,
    },
    "networks": [
        {
            "network": {"key": "base", "name": "Base", "caip2": "eip155:8453"},
            "block_number": 51500000,
            "native": {"symbol": "ETH", "balance": 0.001},
            "usdc": {"balance": 1.25, "balance_atomic": "1250000"},
            "gas_price": {"wei": "1000000", "gwei": 0.001},
            "native_transfer_estimate": {"fee_native": 0.000000021, "symbol": "ETH"},
        }
    ],
    "errors": [],
}


def _networks(body: dict) -> list[str]:
    raw = body.get("networks") or body.get("chains") or _DEFAULT_NETWORKS
    if isinstance(raw, str):
        values = [part.strip().lower() for part in raw.split(",") if part.strip()]
    elif isinstance(raw, list):
        values = [str(part).strip().lower() for part in raw if str(part).strip()]
    else:
        raise EvmRpcError("invalid_networks")
    values = list(dict.fromkeys(values))
    if not values or len(values) > len(_DEFAULT_NETWORKS):
        raise EvmRpcError("invalid_networks")
    if any(value not in NETWORKS for value in values):
        raise EvmRpcError("unsupported_network")
    return values


async def _network_snapshot(address: str, network: str) -> dict:
    balance, gas = await asyncio.gather(
        balance_lookup({"address": address, "network": network}),
        gas_lookup({"network": network}),
    )
    return {
        **balance,
        "gas_price": gas["gas_price"],
        "base_fee": gas["base_fee"],
        "native_transfer_estimate": gas["native_transfer_estimate"],
    }


async def _lookup(body: dict) -> dict:
    address = str(body.get("address") or body.get("wallet") or "").strip()
    networks = _networks(body)
    try:
        amount = float(body.get("amount_usdc", 0.001))
    except (TypeError, ValueError) as exc:
        raise EvmRpcError("invalid_amount") from exc
    if amount < 0 or amount > 1000:
        raise EvmRpcError("invalid_amount")

    results = await asyncio.gather(
        *(_network_snapshot(address, network) for network in networks),
        return_exceptions=True,
    )
    snapshots: list[dict] = []
    errors: list[dict] = []
    for network, result in zip(networks, results):
        if isinstance(result, Exception):
            reason = str(result) if isinstance(result, EvmRpcError) else "rpc_unavailable"
            errors.append({"network": network, "reason": reason})
        else:
            snapshots.append(result)
    if not snapshots:
        reason = errors[0]["reason"] if errors else "rpc_unavailable"
        raise EvmRpcError(reason)

    base = next(
        (snapshot for snapshot in snapshots if snapshot["network"]["key"] == "base"),
        None,
    )
    readiness = None
    if base is not None:
        balance = float(base["usdc"]["balance"])
        readiness = {
            "network": "base",
            "amount_usdc": amount,
            "balance_usdc": balance,
            "can_pay": balance + 1e-12 >= amount,
            "shortfall_usdc": max(0.0, round(amount - balance, 6)),
        }
    return {
        "address": address,
        "requested_networks": networks,
        "successful_networks": len(snapshots),
        "rpc_reads_replaced": len(snapshots) * 5,
        "x402_readiness": readiness,
        "networks": snapshots,
        "errors": errors,
    }


@router.get("/wallet-intelligence/sample", openapi_extra={"security": []})
async def wallet_intelligence_sample():
    return {
        **SAMPLE_RESPONSE,
        "note": "One paid call replaces wallet and gas reads on up to five EVM networks.",
        "x402_receipt": make_receipt(None, "wallet-intelligence", 1, 0.0),
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
        code = 400 if reason in {
            "invalid_address", "invalid_networks", "unsupported_network", "invalid_amount"
        } else 502
        db.log_request(
            route="wallet-intelligence", method=request.method, status="error",
            payer=payer, user_agent=user_agent, body_excerpt=body_excerpt,
            error_reason=reason,
        )
        return JSONResponse({"error": {"reason": reason}}, status_code=code)
    price = effective_price(payer, price_float(config.PRICE_WALLET_INTELLIGENCE))
    db.log_request(
        route="wallet-intelligence", method=request.method, status="paid",
        latency_ms=timer.elapsed_ms, amount_usdc=price, payer=payer,
        user_agent=user_agent, body_excerpt=body_excerpt,
    )
    return {
        **result,
        "x402_receipt": make_receipt(
            None, "wallet-intelligence", timer.elapsed_ms, price
        ),
    }


@router.get(
    "/wallet-intelligence",
    description=ROUTE_DESCRIPTIONS["wallet-intelligence"],
)
async def wallet_intelligence_get(
    request: Request,
    address: str | None = None,
    networks: str = ",".join(_DEFAULT_NETWORKS),
    amount_usdc: float = 0.001,
):
    return await _paid(
        request,
        {"address": address, "networks": networks, "amount_usdc": amount_usdc},
    )


@router.post(
    "/wallet-intelligence",
    description=ROUTE_DESCRIPTIONS["wallet-intelligence"],
)
async def wallet_intelligence_post(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _paid(request, body)
