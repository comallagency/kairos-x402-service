"""POST /news — top headlines (Hacker News) for agents that need fresh links."""

from __future__ import annotations

import asyncio
import json

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import config, db
from app.receipts import Timer, effective_price, extract_payer_address, make_receipt, price_float
from app.x402_setup import ROUTE_DESCRIPTIONS

router = APIRouter()

_TOP = "https://hacker-news.firebaseio.com/v0/topstories.json"
_ITEM = "https://hacker-news.firebaseio.com/v0/item/{id}.json"
_TIMEOUT = 20.0
_DEFAULT_LIMIT = 10
_MAX_LIMIT = 25

SAMPLE_RESPONSE = {
    "source": "hacker-news",
    "count": 3,
    "stories": [
        {
            "id": 49765348,
            "title": "Example headline about AI agents",
            "url": "https://example.com/story",
            "score": 312,
            "by": "pg",
            "time": 1726750000,
            "comments": 88,
        }
    ],
}


class NewsError(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


async def _fetch_item(client: httpx.AsyncClient, item_id: int) -> dict | None:
    resp = await client.get(_ITEM.format(id=item_id))
    resp.raise_for_status()
    data = resp.json()
    if not data or data.get("type") != "story" or data.get("dead") or data.get("deleted"):
        return None
    return {
        "id": data.get("id"),
        "title": data.get("title"),
        "url": data.get("url") or f"https://news.ycombinator.com/item?id={data.get('id')}",
        "score": data.get("score"),
        "by": data.get("by"),
        "time": data.get("time"),
        "comments": data.get("descendants"),
    }


async def _lookup(body: dict) -> dict:
    try:
        limit = int(body.get("limit", _DEFAULT_LIMIT))
    except (TypeError, ValueError) as exc:
        raise NewsError("invalid_limit") from exc
    if limit < 1 or limit > _MAX_LIMIT:
        raise NewsError("invalid_limit")

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        top = await client.get(_TOP)
        top.raise_for_status()
        ids = top.json()
        if not isinstance(ids, list) or not ids:
            raise NewsError("upstream_empty")
        # Fetch a few extras in case some are jobs/polls/dead.
        candidates = ids[: limit + 10]
        items = await asyncio.gather(*[_fetch_item(client, i) for i in candidates])

    stories = [s for s in items if s][:limit]
    if not stories:
        raise NewsError("upstream_empty")
    return {"source": "hacker-news", "count": len(stories), "stories": stories}


@router.get("/news/sample", openapi_extra={"security": []})
async def news_sample():
    return {**SAMPLE_RESPONSE, "x402_receipt": make_receipt(None, "news", 1, 0.0)}


async def _news_paid(request: Request, body: dict):
    payer = extract_payer_address(request)
    user_agent = request.headers.get("user-agent")
    body_excerpt = json.dumps(body)[:2000]
    method = request.method

    try:
        with Timer() as t:
            result = await _lookup(body)
    except NewsError as exc:
        db.log_request(
            route="news", method=method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason=exc.reason,
        )
        code = 502 if exc.reason == "upstream_empty" else 400
        return JSONResponse({"error": {"reason": exc.reason}}, status_code=code)
    except Exception as exc:
        db.log_request(
            route="news", method=method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason=str(exc)[:200],
        )
        return JSONResponse(
            {"error": {"reason": "upstream_error", "detail": str(exc)[:200]}}, status_code=502
        )

    price = effective_price(payer, price_float(config.PRICE_NEWS))
    db.log_request(
        route="news", method=method, status="paid", latency_ms=t.elapsed_ms,
        amount_usdc=price, payer=payer, user_agent=user_agent, body_excerpt=body_excerpt,
    )
    receipt = make_receipt(None, "news", t.elapsed_ms, price)
    return {**result, "x402_receipt": receipt}


@router.get("/news", description=ROUTE_DESCRIPTIONS["news"])
async def news_get(request: Request, limit: int = 10):
    return await _news_paid(request, {"limit": limit})


@router.post("/news", description=ROUTE_DESCRIPTIONS["news"])
async def news(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _news_paid(request, body)
