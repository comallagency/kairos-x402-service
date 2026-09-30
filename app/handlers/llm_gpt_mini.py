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
# 2nd-provider fallback (2026-09-30, same logic as gemini-flash/deepseek):
# Azure measured 100% uptime_last_30m the same day, a real test call
# succeeded - used if the primary exceeds PRIMARY_TIMEOUT_S (8s), see
# app/handlers/llm_per_model.py.
FALLBACK_PROVIDER = {'only': ['Azure'], 'allow_fallbacks': False}
ROUTE_PATH = '/llm/gpt-mini'
ROUTE_KEY = 'llm/gpt-mini'

price_fn = make_price_fn(MODEL)

SAMPLE_REQUEST = {"messages": [{"role": "user", "content": "Summarize in one sentence: The Eiffel Tower is a wrought-iron lattice tower on the Champ de Mars in Paris, France. It was designed by Gustave Eiffel's engineering company and built as the entrance arch for the 1889 World's Fair. Initially criticized by some of France's leading artists and intellectuals for its design, it has become a global cultural icon of France and one of the most recognizable structures in the world, attracting millions of visitors every year."}], "max_tokens": 150}
SAMPLE_RESPONSE = {
    "id": "gen-1790781015-abP5jUyNb5Xs9xjmdtPX",
    "object": "chat.completion",
    "created": 1790781015,
    "model": "openai/gpt-5.4-mini",
    "provider": "OpenAI",
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "The Eiffel Tower, a wrought-iron tower in Paris built for the 1889 World's Fair, evolved from a controversial design into one of the world's most iconic landmarks."},
        }
    ],
    "usage": {"prompt_tokens": 100, "completion_tokens": 39, "total_tokens": 139, "cost": 0.0002505},
    "x402_receipt": make_receipt("openai/gpt-5.4-mini", "llm/gpt-mini", 1700, 0.001),
}

router = make_router(
    route_path=ROUTE_PATH, route_key=ROUTE_KEY, model=MODEL, provider=PROVIDER,
    description_key='llm-gpt-mini', sample_request=SAMPLE_REQUEST, sample_response=SAMPLE_RESPONSE,
    fallback_provider=FALLBACK_PROVIDER,
)
