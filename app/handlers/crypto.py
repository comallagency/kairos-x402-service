"""POST /crypto — live spot prices for coins (USD / EUR / …)."""

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

_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"
_TIMEOUT = 20.0
_MAX_COINS = 20

# Common ticker → CoinGecko id (agents almost always send symbols).
_SYMBOL_TO_ID = {
    "btc": "bitcoin",
    "eth": "ethereum",
    "sol": "solana",
    "usdc": "usd-coin",
    "usdt": "tether",
    "bnb": "binancecoin",
    "xrp": "ripple",
    "ada": "cardano",
    "doge": "dogecoin",
    "avax": "avalanche-2",
    "dot": "polkadot",
    "matic": "matic-network",
    "pol": "polygon-ecosystem-token",
    "link": "chainlink",
    "uni": "uniswap",
    "atom": "cosmos",
    "ltc": "litecoin",
    "near": "near",
    "arb": "arbitrum",
    "op": "optimism",
    "pepe": "pepe",
    "weth": "weth",
    "dai": "dai",
}

SAMPLE_RESPONSE = {
    "vs_currency": "usd",
    "prices": [
        {"id": "bitcoin", "symbol": "btc", "price": 81218.0, "change_24h_pct": -1.2},
        {"id": "ethereum", "symbol": "eth", "price": 2634.1, "change_24h_pct": 0.4},
    ],
}

_ID_RE = re.compile(r"^[a-z0-9-]{1,64}$")


class CryptoError(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _normalize_coins(body: dict) -> list[tuple[str, str | None]]:
    """Return list of (coingecko_id, symbol_or_none)."""
    raw = body.get("coins") or body.get("ids") or body.get("coin") or body.get("symbols")
    if raw is None:
        raise CryptoError("missing_coins")
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
    elif isinstance(raw, list):
        parts = [str(p).strip() for p in raw if str(p).strip()]
    else:
        raise CryptoError("invalid_coins")
    if not parts:
        raise CryptoError("missing_coins")
    if len(parts) > _MAX_COINS:
        raise CryptoError("too_many_coins")

    out: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for part in parts:
        key = part.lower().lstrip("$")
        symbol = None
        if key in _SYMBOL_TO_ID:
            symbol = key
            cid = _SYMBOL_TO_ID[key]
        elif _ID_RE.match(key):
            cid = key
        else:
            raise CryptoError("invalid_coin")
        if cid in seen:
            continue
        seen.add(cid)
        out.append((cid, symbol))
    return out


async def _lookup(body: dict) -> dict:
    pairs = _normalize_coins(body)
    vs = (body.get("vs_currency") or body.get("vs") or "usd").strip().lower()
    if not re.match(r"^[a-z]{3,5}$", vs):
        raise CryptoError("invalid_vs_currency")
    ids = ",".join(cid for cid, _ in pairs)
    params = {
        "ids": ids,
        "vs_currencies": vs,
        "include_24hr_change": "true",
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(_PRICE_URL, params=params)
        if resp.status_code == 429:
            raise CryptoError("rate_limited")
        resp.raise_for_status()
        data = resp.json()

    # Reverse map for symbols we resolved.
    id_to_symbol = {cid: sym for cid, sym in pairs if sym}
    prices = []
    for cid, _sym in pairs:
        entry = data.get(cid)
        if not entry or vs not in entry:
            continue
        prices.append(
            {
                "id": cid,
                "symbol": id_to_symbol.get(cid),
                "price": entry.get(vs),
                "change_24h_pct": entry.get(f"{vs}_24h_change"),
            }
        )
    if not prices:
        raise CryptoError("coins_not_found")
    return {"vs_currency": vs, "prices": prices}


@router.get("/crypto/sample", openapi_extra={"security": []})
async def crypto_sample():
    return {**SAMPLE_RESPONSE, "x402_receipt": make_receipt(None, "crypto", 1, 0.0)}


async def _crypto_paid(request: Request, body: dict):
    payer = extract_payer_address(request)
    user_agent = request.headers.get("user-agent")
    body_excerpt = json.dumps(body)[:2000]
    method = request.method

    try:
        with Timer() as t:
            result = await _lookup(body)
    except CryptoError as exc:
        db.log_request(
            route="crypto", method=method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason=exc.reason,
        )
        code = 429 if exc.reason == "rate_limited" else 404 if exc.reason == "coins_not_found" else 400
        return JSONResponse({"error": {"reason": exc.reason}}, status_code=code)
    except Exception as exc:
        db.log_request(
            route="crypto", method=method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason=str(exc)[:200],
        )
        return JSONResponse(
            {"error": {"reason": "upstream_error", "detail": str(exc)[:200]}}, status_code=502
        )

    price = effective_price(payer, price_float(config.PRICE_CRYPTO))
    db.log_request(
        route="crypto", method=method, status="paid", latency_ms=t.elapsed_ms,
        amount_usdc=price, payer=payer, user_agent=user_agent, body_excerpt=body_excerpt,
    )
    receipt = make_receipt(None, "crypto", t.elapsed_ms, price)
    return {**result, "x402_receipt": receipt}


@router.get("/crypto", description=ROUTE_DESCRIPTIONS["crypto"])
async def crypto_get(
    request: Request,
    coins: str | None = None,
    vs_currency: str = "usd",
):
    body = {"vs_currency": vs_currency}
    if coins:
        body["coins"] = coins
    return await _crypto_paid(request, body)


@router.post("/crypto", description=ROUTE_DESCRIPTIONS["crypto"])
async def crypto(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _crypto_paid(request, body)
