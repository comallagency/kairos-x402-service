"""GET|POST /probe — discover whether a URL is an x402 paywall and at what price."""

from __future__ import annotations

import ipaddress
import json
import socket
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import config, db
from app.receipts import Timer, effective_price, extract_payer_address, make_receipt, price_float
from app.x402_setup import ROUTE_DESCRIPTIONS

router = APIRouter()

_TIMEOUT = 12.0
_MAX_BYTES = 64_000

SAMPLE_RESPONSE = {
    "url": "https://x402.shizu.me/weather?lat=48.85&lon=2.35",
    "reachable": True,
    "http_status": 402,
    "is_x402": True,
    "x402_version": 1,
    "price_usdc": 0.004,
    "network": "base",
    "pay_to": "0x217e5Fe265EB78b29067bF8324ef03a7D8e167C4",
    "scheme": "exact",
    "description": "Weather for any coordinate…",
}


class ProbeError(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _host_is_public(hostname: str) -> bool:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise ProbeError("dns_failed") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        ):
            return False
    return True


def _validate_url(url: str) -> str:
    if not url or not isinstance(url, str):
        raise ProbeError("missing_url")
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ProbeError("invalid_url")
    if not parsed.hostname:
        raise ProbeError("invalid_url")
    if parsed.hostname in ("localhost",) or parsed.hostname.endswith(".local"):
        raise ProbeError("blocked_host")
    if not _host_is_public(parsed.hostname):
        raise ProbeError("blocked_host")
    return url


def _atomic_to_usdc(amount: str | None) -> float | None:
    if amount is None:
        return None
    try:
        return int(amount) / 1_000_000
    except (TypeError, ValueError):
        return None


def _parse_challenge(status: int, headers: httpx.Headers, body: bytes) -> dict:
    out: dict = {
        "http_status": status,
        "is_x402": False,
        "x402_version": None,
        "price_usdc": None,
        "network": None,
        "pay_to": None,
        "scheme": None,
        "asset": None,
        "description": None,
        "accepts_count": 0,
    }
    payload = None
    pr = headers.get("payment-required") or headers.get("Payment-Required")
    if pr:
        try:
            from x402.http.utils import decode_payment_required_header

            payload = decode_payment_required_header(pr).model_dump(
                by_alias=True, exclude_none=True
            )
        except Exception:
            payload = None
    if payload is None and body:
        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
        except Exception:
            payload = None
    if not isinstance(payload, dict):
        return out

    accepts = payload.get("accepts") or []
    if not accepts and status != 402:
        return out
    out["is_x402"] = status == 402 and bool(accepts)
    out["x402_version"] = payload.get("x402Version")
    out["accepts_count"] = len(accepts) if isinstance(accepts, list) else 0
    if not accepts:
        return out
    a = accepts[0] if isinstance(accepts[0], dict) else {}
    atomic = a.get("amount") or a.get("maxAmountRequired")
    out["price_usdc"] = _atomic_to_usdc(str(atomic) if atomic is not None else None)
    out["network"] = a.get("network")
    out["pay_to"] = a.get("payTo")
    out["scheme"] = a.get("scheme")
    out["asset"] = a.get("asset")
    out["description"] = (a.get("description") or payload.get("error") or "")[:240] or None
    return out


async def _lookup(body: dict) -> dict:
    url = _validate_url(body.get("url") or body.get("target") or "")
    method = (body.get("method") or "GET").upper()
    if method not in ("GET", "POST", "PUT", "HEAD"):
        raise ProbeError("invalid_method")
    probe_body = body.get("body")
    headers = {"User-Agent": "AgentIndex-probe/1.0", "Accept": "application/json"}
    if method != "GET" and probe_body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(probe_body).encode() if not isinstance(probe_body, (bytes, bytearray)) else probe_body
    else:
        data = None

    try:
        # Redirects are not followed: a public URL could otherwise redirect the
        # probe to a private address and bypass the DNS/IP validation above.
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
            resp = await client.request(method, url, headers=headers, content=data)
            raw = resp.content[:_MAX_BYTES]
            parsed = _parse_challenge(resp.status_code, resp.headers, raw)
            return {"url": url, "method": method, "reachable": True, **parsed}
    except ProbeError:
        raise
    except httpx.TimeoutException as exc:
        raise ProbeError("timeout") from exc
    except Exception as exc:
        return {
            "url": url,
            "method": method,
            "reachable": False,
            "http_status": None,
            "is_x402": False,
            "error": str(exc)[:200],
        }


@router.get("/probe/sample", openapi_extra={"security": []})
async def probe_sample():
    return {**SAMPLE_RESPONSE, "x402_receipt": make_receipt(None, "probe", 1, 0.0)}


async def _paid(request: Request, body: dict):
    payer = extract_payer_address(request)
    user_agent = request.headers.get("user-agent")
    body_excerpt = json.dumps(body)[:2000]
    method = request.method
    try:
        with Timer() as t:
            result = await _lookup(body)
    except ProbeError as exc:
        db.log_request(
            route="probe", method=method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason=exc.reason,
        )
        return JSONResponse({"error": {"reason": exc.reason}}, status_code=400)
    except Exception as exc:
        db.log_request(
            route="probe", method=method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason=str(exc)[:200],
        )
        return JSONResponse(
            {"error": {"reason": "upstream_error", "detail": str(exc)[:200]}}, status_code=502
        )
    price = effective_price(payer, price_float(config.PRICE_PROBE))
    db.log_request(
        route="probe", method=method, status="paid", latency_ms=t.elapsed_ms,
        amount_usdc=price, payer=payer, user_agent=user_agent, body_excerpt=body_excerpt,
    )
    return {**result, "x402_receipt": make_receipt(None, "probe", t.elapsed_ms, price)}


@router.get("/probe", description=ROUTE_DESCRIPTIONS["probe"])
async def probe_get(request: Request, url: str | None = None, method: str = "GET"):
    return await _paid(request, {"url": url, "method": method})


@router.post("/probe", description=ROUTE_DESCRIPTIONS["probe"])
async def probe_post(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _paid(request, body)
