import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import config, db
from app.receipts import Timer, effective_price, extract_payer_address, make_receipt, price_float
from app.upstream.tokencount import count_tokens
from app.upstream.webfetch import FetchError, extract_markdown, extract_title, fetch_html
from app.x402_setup import ROUTE_DESCRIPTIONS

router = APIRouter()

SAMPLE_URL = "https://www.w3.org/"


class WebReadError(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


async def _read_url(url: str) -> dict:
    if not url or not isinstance(url, str):
        raise WebReadError("missing_url")
    try:
        html = await fetch_html(url)
    except FetchError as exc:
        raise WebReadError(str(exc)[:200])
    markdown = extract_markdown(html, url=url)
    if not markdown:
        # A real fetch that yielded no extractable article content (e.g. a
        # login wall, a bare app shell) is an explicit error, never an
        # empty "success" - the buyer paid for content, not a null field.
        raise WebReadError("no_extractable_content")
    title = extract_title(html, url=url)
    return {"url": url, "title": title, "markdown": markdown}


@router.get("/web-read/sample", openapi_extra={"security": []})
async def web_read_sample():
    with Timer() as t:
        try:
            result = await _read_url(SAMPLE_URL)
        except WebReadError as exc:
            return JSONResponse({"error": {"reason": exc.reason}}, status_code=502)
    token_count = count_tokens(result["markdown"])
    receipt = make_receipt(None, "web_fetch", t.elapsed_ms, 0.0, sources_read=1)
    return {**result, "token_count": token_count, "x402_receipt": receipt}


@router.post("/web-read", description=ROUTE_DESCRIPTIONS["web-read"])
async def web_read(request: Request):
    payer = extract_payer_address(request)
    user_agent = request.headers.get("user-agent")
    try:
        body = await request.json()
    except Exception:
        body = {}
    body_excerpt = json.dumps(body)[:2000]
    url = body.get("url")

    try:
        with Timer() as t:
            result = await _read_url(url)
    except WebReadError as exc:
        db.log_request(
            route="web-read", method="POST", status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason=exc.reason,
        )
        status_code = 400 if exc.reason in ("missing_url", "invalid_url", "no_extractable_content") else 502
        return JSONResponse({"error": {"reason": exc.reason}}, status_code=status_code)

    token_count = count_tokens(result["markdown"])
    price = effective_price(payer, price_float(config.PRICE_WEB_READ))
    db.log_request(
        route="web-read", method="POST", status="paid", latency_ms=t.elapsed_ms,
        amount_usdc=price, payer=payer, user_agent=user_agent, body_excerpt=body_excerpt,
    )
    receipt = make_receipt(None, "web_fetch", t.elapsed_ms, price, sources_read=1)
    return {**result, "token_count": token_count, "x402_receipt": receipt}
