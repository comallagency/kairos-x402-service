"""POST /due-diligence - composite check combining token-risk,
wallet-intelligence and agent-health in a single call: is this token safe,
is this wallet funded/ready, is this agent's own service actually live -
the three questions an agent asks before trusting a new counterparty,
without three separate paid calls.

Each of the 3 legs runs concurrently against the SAME internal `_lookup`
functions the standalone routes use (no duplicated logic, no second
implementation to drift from the original). `token`/`address` are both
optional on their own, and so is `url` (agent-health has nothing to check
without a target) - a leg whose required input is missing degrades to
`{"status": "skipped", ...}` rather than failing the whole call, same
philosophy as token-risk's own per-signal degradation. At least one of
address/token/url must be given, or there is nothing to check at all.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import config, db
from app.handlers.agent_health import SAMPLE_RESPONSE as AGENT_HEALTH_SAMPLE
from app.handlers.agent_health import _lookup as _agent_health_lookup
from app.handlers.probe import ProbeError
from app.handlers.token_risk import SAMPLE_RESPONSE as TOKEN_RISK_SAMPLE
from app.handlers.token_risk import _lookup as _token_risk_lookup
from app.handlers.wallet_intelligence import SAMPLE_RESPONSE as WALLET_INTELLIGENCE_SAMPLE
from app.handlers.wallet_intelligence import _lookup as _wallet_intelligence_lookup
from app.receipts import Timer, effective_price, extract_payer_address, make_receipt, price_float
from app.upstream.evm_rpc import EvmRpcError
from app.x402_setup import ROUTE_DESCRIPTIONS

router = APIRouter()


async def _token_risk_leg(token: str | None) -> dict:
    if not token:
        return {"status": "skipped", "reason": "no_address_or_token_provided"}
    try:
        return await _token_risk_lookup({"address": token})
    except EvmRpcError as exc:
        return {"status": "error", "reason": str(exc)}


async def _wallet_intelligence_leg(address: str | None, networks, amount_usdc) -> dict:
    if not address:
        return {"status": "skipped", "reason": "no_address_provided"}
    body: dict = {"address": address}
    if networks is not None:
        body["networks"] = networks
    if amount_usdc is not None:
        body["amount_usdc"] = amount_usdc
    try:
        return await _wallet_intelligence_lookup(body)
    except EvmRpcError as exc:
        return {"status": "error", "reason": str(exc)}


async def _agent_health_leg(url: str | None, method: str) -> dict:
    if not url:
        return {"status": "skipped", "reason": "no_url_provided"}
    try:
        return await _agent_health_lookup({"url": url, "method": method})
    except ProbeError as exc:
        return {"status": "error", "reason": exc.reason}


def _overall_verdict(token_risk: dict, wallet_intelligence: dict, agent_health: dict) -> str:
    """A rollup of the 3 legs' own verdicts - never invents a judgment for a
    leg that was skipped (no input given) or errored. 'avoid' beats
    'caution' beats 'acceptable'; a skipped/errored leg alone never produces
    'avoid' on its own (that would punish a caller for omitting an optional
    field), but any real error still means the overall picture is
    incomplete, so it caps the result at 'caution' at best."""
    legs = (token_risk, wallet_intelligence, agent_health)
    ran = [leg for leg in legs if leg.get("status") != "skipped"]
    if not ran:
        return "insufficient_data"

    if token_risk.get("verdict", {}).get("verdict") == "avoid":
        return "avoid"
    if agent_health.get("verdict") == "unreachable":
        return "avoid"

    if any(leg.get("status") == "error" for leg in legs):
        return "caution"
    if token_risk.get("verdict", {}).get("verdict") == "caution":
        return "caution"
    if agent_health.get("verdict") == "degraded":
        return "caution"
    readiness = wallet_intelligence.get("x402_readiness")
    if readiness and readiness.get("can_pay") is False:
        return "caution"

    return "acceptable"


async def _lookup(body: dict) -> dict:
    address = str(body.get("address") or body.get("wallet") or "").strip() or None
    token = str(body.get("token") or address or "").strip() or None
    raw_url = body.get("url") or body.get("agent_url")
    url = str(raw_url).strip() if raw_url else None
    method = body.get("method") or "GET"
    networks = body.get("networks")
    amount_usdc = body.get("amount_usdc")

    if not address and not token and not url:
        raise EvmRpcError("invalid_address")

    token_risk, wallet_intelligence, agent_health = await asyncio.gather(
        _token_risk_leg(token),
        _wallet_intelligence_leg(address, networks, amount_usdc),
        _agent_health_leg(url, method),
    )

    return {
        "address": address,
        "token": token,
        "agent_url": url,
        "overall_verdict": _overall_verdict(token_risk, wallet_intelligence, agent_health),
        "token_risk": token_risk,
        "wallet_intelligence": wallet_intelligence,
        "agent_health": agent_health,
    }


SAMPLE_RESPONSE = {
    "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "agent_url": "https://x402.agentindex.world/search",
    "overall_verdict": "caution",
    "token_risk": TOKEN_RISK_SAMPLE,
    "wallet_intelligence": WALLET_INTELLIGENCE_SAMPLE,
    "agent_health": AGENT_HEALTH_SAMPLE,
}


@router.get("/due-diligence/sample", openapi_extra={"security": []})
async def due_diligence_sample():
    return {
        **SAMPLE_RESPONSE,
        "note": "Static example, not a live call. Combines token-risk, wallet-intelligence and agent-health in one call.",
        "x402_receipt": make_receipt(None, "due-diligence", 1, 0.0, steps_executed=3),
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
        db.log_request(
            route="due-diligence", method=request.method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason=reason,
        )
        return JSONResponse({"error": {"reason": reason}}, status_code=400)

    price = effective_price(payer, price_float(config.PRICE_DUE_DILIGENCE))
    db.log_request(
        route="due-diligence", method=request.method, status="paid",
        latency_ms=timer.elapsed_ms, amount_usdc=price, payer=payer,
        user_agent=user_agent, body_excerpt=body_excerpt,
    )
    return {
        **result,
        "x402_receipt": make_receipt(None, "due-diligence", timer.elapsed_ms, price, steps_executed=3),
    }


@router.get("/due-diligence", description=ROUTE_DESCRIPTIONS["due-diligence"])
async def due_diligence_get(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    params = request.query_params
    merged = {
        "address": params.get("address") or params.get("wallet") or body.get("address") or body.get("wallet"),
        "token": params.get("token") or body.get("token"),
        "url": params.get("url") or params.get("agent_url") or body.get("url") or body.get("agent_url"),
        "method": params.get("method") or body.get("method") or "GET",
        "networks": params.get("networks") or body.get("networks"),
        "amount_usdc": params.get("amount_usdc") or body.get("amount_usdc"),
    }
    if not merged["address"] and not merged["token"] and not merged["url"]:
        merged["address"] = SAMPLE_RESPONSE["address"]
        merged["url"] = SAMPLE_RESPONSE["agent_url"]
    return await _paid(request, merged)


@router.post("/due-diligence", description=ROUTE_DESCRIPTIONS["due-diligence"])
async def due_diligence_post(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _paid(request, body)
