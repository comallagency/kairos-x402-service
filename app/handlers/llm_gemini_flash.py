"""POST /llm/gemini-flash - pay-per-call google/gemini-3.8-flash, OpenAI-compatible passthrough.

Pinned to Google AI Studio - measured 2026-09-30: 100% uptime_last_30m on its best endpoint. Real test calls found this model spends hidden reasoning tokens even on trivial prompts (72 reasoning tokens to answer \"Say OK\" at max_tokens=200) - a small max_tokens (e.g. 10) can come back with empty content, not a route bug. Pass a realistic max_tokens (50+) for a real answer.

Same engine as POST /v1/chat/completions (app/handlers/llm_gateway.py):
"exact" scheme only, ceiling computed from max_tokens via that same
pricing formula. See app/handlers/llm_per_model.py for the shared code."""

from __future__ import annotations

from app.handlers.llm_per_model import make_price_fn, make_router
from app.receipts import make_receipt

MODEL = 'google/gemini-3.8-flash'
PROVIDER = {'only': ['Google AI Studio'], 'allow_fallbacks': False}
ROUTE_PATH = '/llm/gemini-flash'
ROUTE_KEY = 'llm/gemini-flash'

price_fn = make_price_fn(MODEL)

SAMPLE_REQUEST = {"messages": [{"role": "user", "content": "Say OK."}], "max_tokens": 150}
SAMPLE_RESPONSE = {
    "id": "gen-1790778746-l8jlDTXmc1SqKkmdAOFt",
    "object": "chat.completion",
    "created": 1790778746,
    "model": "google/gemini-3.8-flash",
    "provider": "Google AI Studio",
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "OK."},
        }
    ],
    "usage": {"prompt_tokens": 4, "completion_tokens": 58, "total_tokens": 62, "cost": 0.0002205},
    "x402_receipt": make_receipt("google/gemini-3.8-flash", "llm/gemini-flash", 1672, 0.001),
}

router = make_router(
    route_path=ROUTE_PATH, route_key=ROUTE_KEY, model=MODEL, provider=PROVIDER,
    description_key='llm-gemini-flash', sample_request=SAMPLE_REQUEST, sample_response=SAMPLE_RESPONSE,
)
