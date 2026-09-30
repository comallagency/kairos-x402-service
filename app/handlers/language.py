"""POST /language - see ROUTE_DESCRIPTIONS["language"] in app/x402_setup.py."""

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
LANGUAGE_ENABLED = True

_INSTRUCTIONS = 'What language is this text written in?'
_CRITERIA = {
    "en": "The text is written in English.",
    "fr": "The text is written in French.",
    "es": "The text is written in Spanish.",
    "de": "The text is written in German.",
    "it": "The text is written in Italian.",
    "pt": "The text is written in Portuguese.",
    "nl": "The text is written in Dutch.",
    "ru": "The text is written in Russian.",
    "zh": "The text is written in Chinese.",
    "ja": "The text is written in Japanese.",
    "ko": "The text is written in Korean.",
    "ar": "The text is written in Arabic.",
    "hi": "The text is written in Hindi.",
    "tr": "The text is written in Turkish.",
    "pl": "The text is written in Polish.",
    "vi": "The text is written in Vietnamese.",
    "th": "The text is written in Thai.",
    "id": "The text is written in Indonesian.",
    "sv": "The text is written in Swedish.",
    "uk": "The text is written in Ukrainian.",
    "other": "The text is written in a language not listed above.",
}

SAMPLE_REQUEST = {"text": 'The quick brown fox jumps over the lazy dog.'}

SAMPLE_RESPONSE = {
    "input": 'The quick brown fox jumps over the lazy dog.',
    "label": 'en',
    "probability": 0.98,
    "alternate_label": 'other',
    "alternate_probability": 0.01,
    "engine": "jev",
}


async def _lookup(body: dict) -> dict:
    text = require_text(body)
    return await classify(text, _INSTRUCTIONS, _CRITERIA)


@router.get("/language/sample", openapi_extra={"security": []})
async def language_sample():
    return {
        **SAMPLE_RESPONSE,
        "note": "Static example, not a live call.",
        "x402_receipt": make_receipt(None, "language", 210, 0.0),
    }


@router.post("/language", description=ROUTE_DESCRIPTIONS["language"])
async def language_post(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    return await respond_http(
        request, body, route="language", price_str=config.PRICE_LANGUAGE,
        lookup=lambda: _lookup(body),
    )
