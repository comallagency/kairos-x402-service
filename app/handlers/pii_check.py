"""POST /pii-check - see ROUTE_DESCRIPTIONS["pii-check"] in app/x402_setup.py."""

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
PII_CHECK_ENABLED = True

_INSTRUCTIONS = 'Does this text contain personally identifiable information (PII)?'
_CRITERIA = {
    "pii_detected": "The text contains personally identifiable information: a full name combined with contact details, an email address, a phone number, a physical address, a government ID number, or a financial account number.",
    "no_pii": "The text does not contain personally identifiable information.",
}

SAMPLE_REQUEST = {"text": 'The quarterly report shows a 12% increase in revenue.'}

SAMPLE_RESPONSE = {
    "input": 'The quarterly report shows a 12% increase in revenue.',
    "label": 'no_pii',
    "probability": 0.91,
    "alternate_label": 'pii_detected',
    "alternate_probability": 0.09,
    "engine": "jev",
}


async def _lookup(body: dict) -> dict:
    text = require_text(body)
    return await classify(text, _INSTRUCTIONS, _CRITERIA)


@router.get("/pii-check/sample", openapi_extra={"security": []})
async def pii_check_sample():
    return {
        **SAMPLE_RESPONSE,
        "note": "Static example, not a live call.",
        "x402_receipt": make_receipt(None, "pii-check", 210, 0.0),
    }


@router.post("/pii-check", description=ROUTE_DESCRIPTIONS["pii-check"])
async def pii_check_post(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    return await respond_http(
        request, body, route="pii-check", price_str=config.PRICE_PII_CHECK,
        lookup=lambda: _lookup(body),
    )
