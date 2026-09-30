"""POST /llm/gpt-mini - pay-per-call openai/gpt-5.4-mini, OpenAI-compatible passthrough.

Pinned to OpenAI's own OpenRouter endpoint - measured 2026-09-30: 100% uptime_last_30m, a real test call succeeded in 0.75s. openai/gpt-5.4-mini is the model OpenRouter's own \"gpt-mini-latest\" alias currently redirects to - the real current \"mini\" tier, not a guess.

Same engine as POST /v1/chat/completions (app/handlers/llm_gateway.py):
"exact" scheme only, ceiling computed from max_tokens via that same
pricing formula. See app/handlers/llm_per_model.py for the shared code."""

from __future__ import annotations

from app.handlers.llm_per_model import make_price_fn, make_router
from app.receipts import make_receipt

MODEL = 'openai/gpt-5.4-mini'
PROVIDER = {'only': ['OpenAI'], 'allow_fallbacks': False}
ROUTE_PATH = '/llm/gpt-mini'
ROUTE_KEY = 'llm/gpt-mini'

price_fn = make_price_fn(MODEL)

SAMPLE_REQUEST = {"messages": [{"role": "user", "content": "Say OK."}], "max_tokens": 150}
SAMPLE_RESPONSE = {
    "id": "gen-1790778703-IkszK765bd62YRNv9gVV",
    "object": "chat.completion",
    "created": 1790778703,
    "model": "openai/gpt-5.4-mini",
    "provider": "OpenAI",
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "OK"},
        }
    ],
    "usage": {"prompt_tokens": 9, "completion_tokens": 5, "total_tokens": 14, "cost": 2.925e-05},
    "x402_receipt": make_receipt("openai/gpt-5.4-mini", "llm/gpt-mini", 897, 0.001),
}

router = make_router(
    route_path=ROUTE_PATH, route_key=ROUTE_KEY, model=MODEL, provider=PROVIDER,
    description_key='llm-gpt-mini', sample_request=SAMPLE_REQUEST, sample_response=SAMPLE_RESPONSE,
)
