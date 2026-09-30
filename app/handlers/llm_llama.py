"""POST /llm/llama - pay-per-call meta-llama/llama-4-maverick, OpenAI-compatible passthrough.

Pinned to DeepInfra - measured 2026-09-30: 99.95% uptime_last_30m, a real test call succeeded in 0.86s. Novita measured equally well that day (100% uptime, 0.50s) but has a documented history of volatility for this account on a different model (see research.py's own module docstring, 37.9% uptime on a bad day) - DeepInfra preferred as the more conservative pick when the two are this close.

Same engine as POST /v1/chat/completions (app/handlers/llm_gateway.py):
"exact" scheme only, ceiling computed from max_tokens via that same
pricing formula. See app/handlers/llm_per_model.py for the shared code."""

from __future__ import annotations

from app.handlers.llm_per_model import make_price_fn, make_router
from app.receipts import make_receipt

MODEL = 'meta-llama/llama-4-maverick'
PROVIDER = {'only': ['DeepInfra'], 'allow_fallbacks': False}
ROUTE_PATH = '/llm/llama'
ROUTE_KEY = 'llm/llama'

price_fn = make_price_fn(MODEL)

SAMPLE_REQUEST = {"messages": [{"role": "user", "content": "Say OK."}], "max_tokens": 150}
SAMPLE_RESPONSE = {
    "id": "gen-1790778763-JUTky3NBjSeIwFRlLjOF",
    "object": "chat.completion",
    "created": 1790778763,
    "model": "meta-llama/llama-4-maverick",
    "provider": "DeepInfra",
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "OK."},
        }
    ],
    "usage": {"prompt_tokens": 13, "completion_tokens": 3, "total_tokens": 16, "cost": 5e-06},
    "x402_receipt": make_receipt("meta-llama/llama-4-maverick", "llm/llama", 873, 0.001),
}

router = make_router(
    route_path=ROUTE_PATH, route_key=ROUTE_KEY, model=MODEL, provider=PROVIDER,
    description_key='llm-llama', sample_request=SAMPLE_REQUEST, sample_response=SAMPLE_RESPONSE,
)
