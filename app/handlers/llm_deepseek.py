"""POST /llm/deepseek - pay-per-call deepseek/deepseek-v4-pro, OpenAI-compatible passthrough.

Pinned to Reka - measured 2026-09-30: 99.96% uptime_last_30m, by far the fastest of 3 real candidates tested (0.51s vs GMICloud's 2.60s and Novita's 4.56s for the same prompt, no hidden reasoning-token overhead observed).

Same engine as POST /v1/chat/completions (app/handlers/llm_gateway.py):
"exact" scheme only, ceiling computed from max_tokens via that same
pricing formula. See app/handlers/llm_per_model.py for the shared code."""

from __future__ import annotations

from app.handlers.llm_per_model import make_price_fn, make_router
from app.receipts import make_receipt

MODEL = 'deepseek/deepseek-v4-pro'
PROVIDER = {'only': ['Reka'], 'allow_fallbacks': False}
ROUTE_PATH = '/llm/deepseek'
ROUTE_KEY = 'llm/deepseek'

price_fn = make_price_fn(MODEL)

SAMPLE_REQUEST = {"messages": [{"role": "user", "content": "Say OK."}], "max_tokens": 150}
SAMPLE_RESPONSE = {
    "id": "gen-1790778806-YfPURU15xoucLCDculX8",
    "object": "chat.completion",
    "created": 1790778806,
    "model": "deepseek/deepseek-v4-pro",
    "provider": "Reka",
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "OK."},
        }
    ],
    "usage": {"prompt_tokens": 15, "completion_tokens": 3, "total_tokens": 18, "cost": 2.73e-05},
    "x402_receipt": make_receipt("deepseek/deepseek-v4-pro", "llm/deepseek", 2038, 0.001),
}

router = make_router(
    route_path=ROUTE_PATH, route_key=ROUTE_KEY, model=MODEL, provider=PROVIDER,
    description_key='llm-deepseek', sample_request=SAMPLE_REQUEST, sample_response=SAMPLE_RESPONSE,
)
