"""POST /intent - see ROUTE_DESCRIPTIONS["intent"] in app/x402_setup.py."""

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
INTENT_ENABLED = True

_INSTRUCTIONS = 'What is the primary intent behind this text?'
_CRITERIA = {
    "question": "The text is primarily asking a question or seeking information.",
    "request": "The text is asking for an action to be taken or a task to be done.",
    "complaint": "The text expresses dissatisfaction, frustration, or reports a problem.",
    "compliment": "The text expresses praise, satisfaction, or gratitude.",
    "other": "The text does not clearly fit question, request, complaint, or compliment.",
}

SAMPLE_RESPONSE = {
    "label": 'request',
    "probability": 0.71,
    "alternate_label": 'question',
    "alternate_probability": 0.18,
    "engine": "jev",
}


async def _lookup(body: dict) -> dict:
    text = require_text(body)
    return await classify(text, _INSTRUCTIONS, _CRITERIA)


@router.get("/intent/sample", openapi_extra={"security": []})
async def intent_sample():
    return {
        **SAMPLE_RESPONSE,
        "note": "Static example, not a live call.",
        "x402_receipt": make_receipt(None, "intent", 210, 0.0),
    }


@router.post("/intent", description=ROUTE_DESCRIPTIONS["intent"])
async def intent_post(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    return await respond_http(
        request, body, route="intent", price_str=config.PRICE_INTENT,
        lookup=lambda: _lookup(body),
    )
