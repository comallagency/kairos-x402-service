"""GET|POST /agent-health — operational readiness check for public agents."""

from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import config, db
from app.handlers.probe import ProbeError, _lookup as x402_probe, _validate_url
from app.receipts import Timer, effective_price, extract_payer_address, make_receipt, price_float
from app.x402_setup import ROUTE_DESCRIPTIONS

router = APIRouter()
_TIMEOUT = 10.0

SAMPLE_RESPONSE = {
    "url": "https://x402.agentindex.world/search",
    "origin": "https://x402.agentindex.world",
    "operational": True,
    "verdict": "operational",
    "score": 100,
    "latency_ms": 84,
    "target": {"reachable": True, "http_status": 402, "content_type": "application/json"},
    "x402": {"valid": True, "version": 2, "price_usdc": 0.0001, "network": "eip155:8453"},
    "discovery": {
        "openapi": {"found": True, "status": 200, "path_count": 18},
        "x402_manifest": {"found": True, "status": 200},
        "agent_card": {"found": True, "status": 200, "skill_count": 27},
        "mcp_manifest": {"found": True, "status": 200},
    },
    "issues": [],
}


async def _inspect_json(client: httpx.AsyncClient, url: str, kind: str) -> tuple[str, dict]:
    started = time.perf_counter()
    try:
        response = await client.get(
            _validate_url(url),
            headers={"User-Agent": "AgentIndex-health/1.0", "Accept": "application/json"},
        )
        latency = round((time.perf_counter() - started) * 1000)
        payload = response.json() if response.status_code < 500 else None
        result: dict = {
            "found": response.status_code == 200 and isinstance(payload, dict),
            "status": response.status_code,
            "latency_ms": latency,
        }
        if kind == "openapi" and isinstance(payload, dict):
            result["path_count"] = len(payload.get("paths") or {})
        elif kind == "agent_card" and isinstance(payload, dict):
            result["skill_count"] = len(payload.get("skills") or [])
        return kind, result
    except Exception as exc:
        return kind, {"found": False, "status": None, "error": str(exc)[:120]}


async def _lookup(body: dict) -> dict:
    url = _validate_url(body.get("url") or body.get("target") or "")
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    started = time.perf_counter()
    probe_task = x402_probe(
        {
            "url": url,
            "method": body.get("method") or "GET",
            "body": body.get("body"),
        }
    )
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
        discovery_tasks = [
            _inspect_json(client, f"{origin}/openapi.json", "openapi"),
            _inspect_json(client, f"{origin}/.well-known/x402", "x402_manifest"),
            _inspect_json(client, f"{origin}/.well-known/agent.json", "agent_card"),
            _inspect_json(client, f"{origin}/.well-known/mcp.json", "mcp_manifest"),
        ]
        probe, *discovery_rows = await asyncio.gather(probe_task, *discovery_tasks)
    latency_ms = round((time.perf_counter() - started) * 1000)
    discovery = dict(discovery_rows)

    reachable = bool(probe.get("reachable"))
    status = probe.get("http_status")
    healthy_status = reachable and status is not None and status < 500
    x402_valid = bool(probe.get("is_x402"))
    found_count = sum(1 for result in discovery.values() if result.get("found"))
    score = 0
    score += 40 if reachable else 0
    score += 15 if healthy_status else 0
    score += min(20, found_count * 5)
    score += 10 if x402_valid else 0
    score += 10 if latency_ms <= 2000 else 5 if latency_ms <= 5000 else 0
    score += 5 if found_count and healthy_status else 0
    score = min(score, 100)

    issues = []
    if not reachable:
        issues.append("target_unreachable")
    elif status is not None and status >= 500:
        issues.append("target_server_error")
    if not x402_valid:
        issues.append("no_valid_x402_challenge")
    if not discovery["openapi"].get("found"):
        issues.append("openapi_missing")
    if not discovery["agent_card"].get("found"):
        issues.append("agent_card_missing")
    if not discovery["mcp_manifest"].get("found"):
        issues.append("mcp_manifest_missing")

    verdict = "operational" if score >= 75 else "degraded" if score >= 45 else "unreachable"
    return {
        "url": url,
        "origin": origin,
        "operational": verdict == "operational",
        "verdict": verdict,
        "score": score,
        "latency_ms": latency_ms,
        "target": {
            "reachable": reachable,
            "http_status": status,
        },
        "x402": {
            "valid": x402_valid,
            "version": probe.get("x402_version"),
            "price_usdc": probe.get("price_usdc"),
            "network": probe.get("network"),
            "pay_to": probe.get("pay_to"),
        },
        "discovery": discovery,
        "issues": issues,
    }


@router.get("/agent-health/sample", openapi_extra={"security": []})
async def agent_health_sample():
    return {
        **SAMPLE_RESPONSE,
        "note": "Paid route performs fresh network and discovery checks.",
        "x402_receipt": make_receipt(None, "agent-health", 1, 0.0),
    }


async def _paid(request: Request, body: dict):
    payer = extract_payer_address(request)
    user_agent = request.headers.get("user-agent")
    body_excerpt = json.dumps(body)[:2000]
    try:
        with Timer() as timer:
            result = await _lookup(body)
    except ProbeError as exc:
        db.log_request(
            route="agent-health", method=request.method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason=exc.reason,
        )
        return JSONResponse({"error": {"reason": exc.reason}}, status_code=400)
    except Exception as exc:
        db.log_request(
            route="agent-health", method=request.method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason=str(exc)[:200],
        )
        return JSONResponse(
            {"error": {"reason": "upstream_error", "detail": str(exc)[:200]}},
            status_code=502,
        )
    price = effective_price(payer, price_float(config.PRICE_AGENT_HEALTH))
    db.log_request(
        route="agent-health", method=request.method, status="paid",
        latency_ms=timer.elapsed_ms, amount_usdc=price, payer=payer,
        user_agent=user_agent, body_excerpt=body_excerpt,
    )
    return {
        **result,
        "x402_receipt": make_receipt(None, "agent-health", timer.elapsed_ms, price),
    }


@router.get("/agent-health", description=ROUTE_DESCRIPTIONS["agent-health"])
async def agent_health_get(
    request: Request,
    url: str | None = None,
    method: str = "GET",
):
    return await _paid(request, {"url": url, "method": method})


@router.post("/agent-health", description=ROUTE_DESCRIPTIONS["agent-health"])
async def agent_health_post(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _paid(request, body)
