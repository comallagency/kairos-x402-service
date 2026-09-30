"""POST /llm/claude-sonnet - pay-per-call anthropic/claude-sonnet-5.5, OpenAI-compatible passthrough.

Pinned to Anthropic's own OpenRouter endpoint - measured 2026-09-30: 99.98% uptime_last_30m, a real test call succeeded in 1.33s (Google's Vertex-hosted resell measured 100%/0.82s the same day - Anthropic direct preferred as the first-party source when the two are this close).

Same engine as POST /v1/chat/completions (app/handlers/llm_gateway.py):
"exact" scheme only, ceiling computed from max_tokens via that same
pricing formula. See app/handlers/llm_per_model.py for the shared code."""

from __future__ import annotations

from app.handlers.llm_per_model import make_price_fn, make_router
from app.receipts import make_receipt

MODEL = 'anthropic/claude-sonnet-5.5'
PROVIDER = {'only': ['Anthropic'], 'allow_fallbacks': False}
ROUTE_PATH = '/llm/claude-sonnet'
ROUTE_KEY = 'llm/claude-sonnet'

price_fn = make_price_fn(MODEL)

SAMPLE_REQUEST = {"messages": [{"role": "user", "content": "Say OK."}], "max_tokens": 150}
SAMPLE_RESPONSE = {
    "id": "gen-1790778686-klDKt8CYDyvYH5k61wgJ",
    "object": "chat.completion",
    "created": 1790778686,
    "model": "anthropic/claude-sonnet-5.5",
    "provider": "Anthropic",
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "OK."},
        }
    ],
    "usage": {"prompt_tokens": 13, "completion_tokens": 5, "total_tokens": 18, "cost": 7.6e-05},
    "x402_receipt": make_receipt("anthropic/claude-sonnet-5.5", "llm/claude-sonnet", 1696, 0.001683),
}

router = make_router(
    route_path=ROUTE_PATH, route_key=ROUTE_KEY, model=MODEL, provider=PROVIDER,
    description_key='llm-claude-sonnet', sample_request=SAMPLE_REQUEST, sample_response=SAMPLE_RESPONSE,
)
