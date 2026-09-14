import json

import jsonschema
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import config, db
from app.receipts import Timer, effective_price, extract_payer_address, make_receipt, neutral_model_id, price_float
from app.upstream.openrouter import OpenRouterError, chat_completion_with_fallback
from app.upstream.webfetch import FetchError, extract_markdown, fetch_html
from app.x402_setup import ROUTE_DESCRIPTIONS

router = APIRouter()

SYSTEM_PROMPT = (
    "You are a strict JSON extraction engine. You will receive source content "
    "and a JSON Schema. Extract ONLY the fields described by the schema from "
    "the content. Respond with ONLY a single JSON object conforming to the "
    "schema - no commentary, no markdown code fences. If a required field "
    "cannot be found in the content, use null for that field rather than "
    "inventing a value."
)

SAMPLE_TEXT = "Widget Pro is our flagship gadget, priced at $29.99 and currently in stock."
SAMPLE_SCHEMA = {
    "type": "object",
    "properties": {
        "product_name": {"type": "string"},
        "price": {"type": "number"},
        "in_stock": {"type": "boolean"},
    },
}

_CALLER_ERROR_REASONS = {
    "missing_url_or_text", "both_url_and_text_provided", "invalid_schema",
    "no_extractable_content", "missing_schema",
}


class ExtractError(Exception):
    def __init__(self, reason: str, detail: str | None = None):
        self.reason = reason
        self.detail = detail
        super().__init__(reason)


async def _get_content(body: dict) -> str:
    url = body.get("url")
    text = body.get("text")
    if url and text:
        raise ExtractError("both_url_and_text_provided")
    if url:
        try:
            html = await fetch_html(url)
        except FetchError as exc:
            raise ExtractError("fetch_failed", str(exc)[:200])
        markdown = extract_markdown(html, url=url)
        if not markdown:
            raise ExtractError("no_extractable_content")
        return markdown
    if text:
        return text
    raise ExtractError("missing_url_or_text")


async def _run_extract(content: str, schema: dict) -> tuple[dict, str | None]:
    try:
        jsonschema.Draft7Validator.check_schema(schema)
    except jsonschema.SchemaError as exc:
        raise ExtractError("invalid_schema", str(exc)[:200])

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps({"schema": schema, "content": content[:12000]})},
    ]
    try:
        data, _used_last_resort = await chat_completion_with_fallback(
            messages, config.OPENROUTER_TRANSLATE_MODELS, config.OPENROUTER_LAST_RESORT_MODEL, max_tokens=2000,
        )
    except OpenRouterError as exc:
        raise ExtractError("upstream_error", str(exc)[:200])

    content_str = data["choices"][0]["message"]["content"]
    model_served = data.get("model")
    try:
        parsed = json.loads(content_str)
    except json.JSONDecodeError:
        raise ExtractError("extraction_failed", "model did not return valid JSON")

    try:
        jsonschema.validate(instance=parsed, schema=schema)
    except jsonschema.ValidationError as exc:
        raise ExtractError("extraction_failed", f"result does not match schema: {exc.message[:200]}")

    return parsed, model_served


@router.get("/extract/sample", openapi_extra={"security": []})
async def extract_sample():
    if not config.OPENROUTER_API_KEY:
        return JSONResponse({"error": {"reason": "upstream_not_configured"}}, status_code=503)
    with Timer() as t:
        try:
            data, model_served = await _run_extract(SAMPLE_TEXT, SAMPLE_SCHEMA)
        except ExtractError as exc:
            return JSONResponse({"error": {"reason": exc.reason, "detail": exc.detail}}, status_code=502)
    receipt = make_receipt(neutral_model_id(model_served), "llm", t.elapsed_ms, 0.0)
    return {"data": data, "x402_receipt": receipt}


@router.post("/extract", description=ROUTE_DESCRIPTIONS["extract"])
async def extract(request: Request):
    payer = extract_payer_address(request)
    user_agent = request.headers.get("user-agent")
    try:
        body = await request.json()
    except Exception:
        body = {}
    body_excerpt = json.dumps(body)[:2000]
    schema = body.get("schema")
    if not schema or not isinstance(schema, dict):
        db.log_request(
            route="extract", method="POST", status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason="missing_schema",
        )
        return JSONResponse({"error": {"reason": "missing_schema"}}, status_code=400)

    try:
        with Timer() as t:
            content = await _get_content(body)
            data, model_served = await _run_extract(content, schema)
    except ExtractError as exc:
        db.log_request(
            route="extract", method="POST", status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason=exc.reason,
        )
        status_code = 400 if exc.reason in _CALLER_ERROR_REASONS else 502
        return JSONResponse({"error": {"reason": exc.reason, "detail": exc.detail}}, status_code=status_code)

    price = effective_price(payer, price_float(config.PRICE_EXTRACT))
    db.log_request(
        route="extract", method="POST", status="paid", latency_ms=t.elapsed_ms,
        amount_usdc=price, payer=payer, user_agent=user_agent, body_excerpt=body_excerpt,
    )
    receipt = make_receipt(neutral_model_id(model_served), "llm", t.elapsed_ms, price)
    return {"data": data, "x402_receipt": receipt}
