"""POST /spam-check - see ROUTE_DESCRIPTIONS["spam-check"] in app/x402_setup.py."""

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
SPAM_CHECK_ENABLED = True

_INSTRUCTIONS = 'Is this text spam?'
_CRITERIA = {
    "spam": "The text is unsolicited advertising, a scam, phishing, or repetitive junk content.",
    "not_spam": "The text is genuine, legitimate content.",
}

SAMPLE_RESPONSE = {
    "label": 'not_spam',
    "probability": 0.97,
    "alternate_label": 'spam',
    "alternate_probability": 0.03,
    "engine": "jev",
}


async def _lookup(body: dict) -> dict:
    text = require_text(body)
    return await classify(text, _INSTRUCTIONS, _CRITERIA)


@router.get("/spam-check/sample", openapi_extra={"security": []})
async def spam_check_sample():
    return {
        **SAMPLE_RESPONSE,
        "note": "Static example, not a live call.",
        "x402_receipt": make_receipt(None, "spam-check", 210, 0.0),
    }


@router.post("/spam-check", description=ROUTE_DESCRIPTIONS["spam-check"])
async def spam_check_post(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    return await respond_http(
        request, body, route="spam-check", price_str=config.PRICE_SPAM_CHECK,
        lookup=lambda: _lookup(body),
    )
