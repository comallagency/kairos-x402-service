"""Paid verified-agent listing with a 30-day machine-readable badge."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app import config, db
from app.handlers.agent_health import _lookup
from app.receipts import Timer, extract_payer_address, make_receipt, price_float
from app.x402_setup import ROUTE_DESCRIPTIONS

router = APIRouter()
_LOCK = asyncio.Lock()
_PATH = config.DATA_DIR / "verified_agents.json"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _read() -> list[dict]:
    try:
        rows = json.loads(_PATH.read_text())
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def _active(rows: list[dict]) -> list[dict]:
    now = _now()
    active = []
    for row in rows:
        try:
            if datetime.fromisoformat(row["expires_at"]) > now:
                active.append(row)
        except Exception:
            continue
    return active


@router.get("/verified-agents.json", openapi_extra={"security": []})
async def verified_agents():
    rows = _active(_read())
    return {
        "registry": "AgentIndex Verified Agents",
        "count": len(rows),
        "agents": rows,
    }


@router.get("/verified-agents/{claim_id}.svg", include_in_schema=False)
async def verified_agent_badge(claim_id: str):
    row = next((r for r in _active(_read()) if r.get("id") == claim_id), None)
    if not row:
        return Response(status_code=404)
    score = int(row.get("score") or 0)
    color = "#2ee59d" if score >= 80 else "#ffb020" if score >= 50 else "#ff5c7a"
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="210" height="28" role="img">'
        '<rect width="210" height="28" rx="6" fill="#10151e"/>'
        f'<rect x="152" width="58" height="28" rx="6" fill="{color}"/>'
        '<text x="10" y="19" fill="#f4f7fb" font-family="sans-serif" font-size="12">'
        'AgentIndex verified</text>'
        f'<text x="181" y="19" text-anchor="middle" fill="#07110d" '
        f'font-family="sans-serif" font-weight="700" font-size="12">{score}/100</text></svg>'
    )
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "public,max-age=300"})


@router.get("/agent-claim/sample", openapi_extra={"security": []})
async def agent_claim_sample():
    return {
        "url": "https://agent.example",
        "score": 95,
        "verified_until": (_now() + timedelta(days=30)).isoformat(),
        "listing": f"{config.BASE_URL}/verified-agents.json",
        "badge": f"{config.BASE_URL}/verified-agents/example.svg",
        "price_usdc": 0.01,
    }


@router.post("/agent-claim", description=ROUTE_DESCRIPTIONS["agent-claim"])
async def agent_claim(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    url = str(payload.get("url") or "").strip()
    name = str(payload.get("name") or url)[:120]
    if not url:
        return JSONResponse({"error": {"reason": "url_required"}}, status_code=400)
    payer = extract_payer_address(request)
    with Timer() as timer:
        try:
            audit = await _lookup({"url": url, "method": payload.get("method") or "GET"})
        except Exception as exc:
            return JSONResponse(
                {"error": {"reason": "audit_failed", "detail": str(exc)[:200]}},
                status_code=422,
            )
        issued = _now()
        claim_id = hashlib.sha256(f"{url}|{payer or ''}".encode()).hexdigest()[:20]
        row = {
            "id": claim_id,
            "name": name,
            "url": audit["url"],
            "origin": audit["origin"],
            "score": audit["score"],
            "verdict": audit["verdict"],
            "operational": audit["operational"],
            "x402": audit["x402"],
            "discovery": audit["discovery"],
            "verified_at": issued.isoformat(),
            "expires_at": (issued + timedelta(days=30)).isoformat(),
            "payer": payer,
        }
        async with _LOCK:
            rows = [r for r in _active(_read()) if r.get("id") != claim_id]
            rows.append(row)
            tmp = _PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(rows, ensure_ascii=False))
            tmp.replace(_PATH)
    price = price_float(config.PRICE_AGENT_CLAIM)
    db.log_request(
        route="agent-claim",
        method="POST",
        status="paid",
        latency_ms=timer.elapsed_ms,
        amount_usdc=price,
        payer=payer,
        user_agent=request.headers.get("user-agent"),
        body_excerpt=json.dumps({"url": url, "name": name})[:2000],
    )
    return {
        **row,
        "listing": f"{config.BASE_URL}/verified-agents.json",
        "badge": f"{config.BASE_URL}/verified-agents/{claim_id}.svg",
        "x402_receipt": make_receipt(None, "agent-claim", timer.elapsed_ms, price),
    }
