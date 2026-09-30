"""POST /toxicity - see ROUTE_DESCRIPTIONS["toxicity"] in app/x402_setup.py."""

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
TOXICITY_ENABLED = True

_INSTRUCTIONS = 'Does this text contain toxic content?'
_CRITERIA = {
    "toxic": "The text contains hate speech, harassment, threats, or severely offensive language.",
    "not_toxic": "The text does not contain hate speech, harassment, threats, or severe offensive language.",
}

SAMPLE_REQUEST = {"text": 'I disagree with your point, but I respect your perspective.'}

SAMPLE_RESPONSE = {
    "input": 'I disagree with your point, but I respect your perspective.',
    "label": 'not_toxic',
    "probability": 0.95,
    "alternate_label": 'toxic',
    "alternate_probability": 0.05,
    "engine": "jev",
}


async def _lookup(body: dict) -> dict:
    text = require_text(body)
    return await classify(text, _INSTRUCTIONS, _CRITERIA)


@router.get("/toxicity/sample", openapi_extra={"security": []})
async def toxicity_sample():
    return {
        **SAMPLE_RESPONSE,
        "note": "Static example, not a live call.",
        "x402_receipt": make_receipt(None, "toxicity", 210, 0.0),
    }


@router.post("/toxicity", description=ROUTE_DESCRIPTIONS["toxicity"])
async def toxicity_post(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    return await respond_http(
        request, body, route="toxicity", price_str=config.PRICE_TOXICITY,
        lookup=lambda: _lookup(body),
    )
