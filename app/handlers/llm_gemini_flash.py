"""POST /llm/gemini-flash - pay-per-call google/gemini-3.8-flash, OpenAI-compatible passthrough.

Pinned to Google AI Studio - measured 2026-09-30: 100% uptime_last_30m on its best endpoint. Real test calls found this model spends hidden reasoning tokens even on trivial prompts (72 reasoning tokens to answer \"Say OK\" at max_tokens=200) - a small max_tokens (e.g. 10) can come back with empty content, not a route bug. A clean sample capture needed max_tokens=800 (385 reasoning tokens consumed) - pass at least max_tokens=500 for a real answer.

Same engine as POST /v1/chat/completions (app/handlers/llm_gateway.py):
"exact" scheme only, ceiling computed from max_tokens via that same
pricing formula. See app/handlers/llm_per_model.py for the shared code."""

from __future__ import annotations

from app.handlers.llm_per_model import make_price_fn, make_router
from app.receipts import make_receipt

MODEL = 'google/gemini-3.8-flash'
PROVIDER = {'only': ['Google AI Studio'], 'allow_fallbacks': False}
# 2nd-provider fallback (2026-09-30): a real 10-call test measured a 34.1s
# p95 on Google AI Studio alone - "Google" (Vertex resale) measured
# 94.2-98.1% uptime_last_30m the same day, the only other real endpoint for
# this model, used if the primary exceeds PRIMARY_TIMEOUT_S (8s) - see
# app/handlers/llm_per_model.py.
FALLBACK_PROVIDER = {'only': ['Google'], 'allow_fallbacks': False}
ROUTE_PATH = '/llm/gemini-flash'
ROUTE_KEY = 'llm/gemini-flash'

price_fn = make_price_fn(MODEL)

SAMPLE_REQUEST = {"messages": [{"role": "user", "content": "Summarize in one sentence: The Eiffel Tower is a wrought-iron lattice tower on the Champ de Mars in Paris, France. It was designed by Gustave Eiffel's engineering company and built as the entrance arch for the 1889 World's Fair. Initially criticized by some of France's leading artists and intellectuals for its design, it has become a global cultural icon of France and one of the most recognizable structures in the world, attracting millions of visitors every year."}], "max_tokens": 800}
SAMPLE_RESPONSE = {
    "id": "gen-1790781304-rNL7dxQAcoXgjs4PNcX4",
    "object": "chat.completion",
    "created": 1790781304,
    "model": "google/gemini-3.8-flash",
    "provider": "Google AI Studio",
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "Originally built for the 1889 World's Fair amid early criticism, Paris's Eiffel Tower has evolved into one of the world's most recognizable cultural icons and popular tourist destinations."},
        }
    ],
    "usage": {"prompt_tokens": 98, "completion_tokens": 425, "total_tokens": 523, "cost": 0.00166725},
    "x402_receipt": make_receipt("google/gemini-3.8-flash", "llm/gemini-flash", 1700, 0.00339075),
}

router = make_router(
    route_path=ROUTE_PATH, route_key=ROUTE_KEY, model=MODEL, provider=PROVIDER,
    description_key='llm-gemini-flash', sample_request=SAMPLE_REQUEST, sample_response=SAMPLE_RESPONSE,
    fallback_provider=FALLBACK_PROVIDER,
)
