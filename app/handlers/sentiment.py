"""POST /sentiment - see ROUTE_DESCRIPTIONS["sentiment"] in app/x402_setup.py."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app import config
from app.handlers.jev_classify import classify, require_text, respond_http
from app.receipts import make_receipt
from app.x402_setup import ROUTE_DESCRIPTIONS

router = APIRouter()

# Quality gate (2026-09-30): set to False to withdraw this route from the
# catalog (see app/x402_setup.py's _core_route_configs()) if its 20-case
# labeled test suite doesn't clear 90% accuracy.
SENTIMENT_ENABLED = True

_INSTRUCTIONS = 'What is the overall sentiment of this text?'
_CRITERIA = {
    "positive": "The text expresses a positive, favorable, or happy sentiment.",
    "negative": "The text expresses a negative, unfavorable, or unhappy sentiment.",
    "neutral": "The text is factual, neutral, or does not express a clear sentiment either way.",
}

SAMPLE_RESPONSE = {
    "label": 'positive',
    "probability": 0.93,
    "alternate_label": 'neutral',
    "alternate_probability": 0.06,
    "engine": "jev",
}


async def _lookup(body: dict) -> dict:
    text = require_text(body)
    return await classify(text, _INSTRUCTIONS, _CRITERIA)


@router.get("/sentiment/sample", openapi_extra={"security": []})
async def sentiment_sample():
    return {
        **SAMPLE_RESPONSE,
        "note": "Static example, not a live call.",
        "x402_receipt": make_receipt(None, "sentiment", 210, 0.0),
    }


@router.post("/sentiment", description=ROUTE_DESCRIPTIONS["sentiment"])
async def sentiment_post(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    return await respond_http(
        request, body, route="sentiment", price_str=config.PRICE_SENTIMENT,
        lookup=lambda: _lookup(body),
    )
