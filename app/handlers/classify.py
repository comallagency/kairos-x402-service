"""POST /classify - text classification against caller-supplied labels
(2-20). See ROUTE_DESCRIPTIONS["classify"] in app/x402_setup.py."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app import config
from app.handlers.jev_classify import ClassifyError, classify, require_text, respond_http
from app.receipts import make_receipt
from app.x402_setup import ROUTE_DESCRIPTIONS

router = APIRouter()

# Quality gate (2026-09-30): see app/x402_setup.py's _core_route_configs().
CLASSIFY_ENABLED = True

MIN_LABELS = 2
MAX_LABELS = 20

_INSTRUCTIONS = "Which of these labels best classifies the text?"

SAMPLE_REQUEST = {
    "text": "I was charged twice for my subscription this month.",
    "labels": ["billing", "technical", "account", "other"],
}

SAMPLE_RESPONSE = {
    "input": "I was charged twice for my subscription this month.",
    "label": "billing",
    "probability": 0.81,
    "alternate_label": "technical",
    "alternate_probability": 0.14,
    "engine": "jev",
}


def _validate_labels(body: dict) -> list[str]:
    labels = body.get("labels") if isinstance(body, dict) else None
    if not isinstance(labels, list) or not (MIN_LABELS <= len(labels) <= MAX_LABELS):
        raise ClassifyError("invalid_labels")
    if not all(isinstance(l, str) and l.strip() for l in labels):
        raise ClassifyError("invalid_labels")
    labels = [l.strip() for l in labels]
    if len(set(labels)) != len(labels):
        raise ClassifyError("duplicate_labels")
    return labels


async def _lookup(body: dict) -> dict:
    text = require_text(body)
    labels = _validate_labels(body)
    criteria = {label: f"The text is best classified as: {label}" for label in labels}
    return await classify(text, _INSTRUCTIONS, criteria)


@router.get("/classify/sample", openapi_extra={"security": []})
async def classify_sample():
    return {
        **SAMPLE_RESPONSE,
        "note": "Static example, not a live call.",
        "x402_receipt": make_receipt(None, "classify", 240, 0.0),
    }


@router.post("/classify", description=ROUTE_DESCRIPTIONS["classify"])
async def classify_post(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    return await respond_http(
        request, body, route="classify", price_str=config.PRICE_CLASSIFY,
        lookup=lambda: _lookup(body),
    )
