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

SAMPLE_REQUEST = {"messages": [{"role": "user", "content": "Summarize in one sentence: The Eiffel Tower is a wrought-iron lattice tower on the Champ de Mars in Paris, France. It was designed by Gustave Eiffel's engineering company and built as the entrance arch for the 1889 World's Fair. Initially criticized by some of France's leading artists and intellectuals for its design, it has become a global cultural icon of France and one of the most recognizable structures in the world, attracting millions of visitors every year."}], "max_tokens": 150}
SAMPLE_RESPONSE = {
    "id": "gen-1790781075-Lyd15GuBV6xAvA6HlOvf",
    "object": "chat.completion",
    "created": 1790781075,
    "model": "meta-llama/llama-4-maverick",
    "provider": "DeepInfra",
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "The Eiffel Tower, a wrought-iron lattice tower in Paris, France, was designed by Gustave Eiffel's company for the 1889 World's Fair and has since become a global cultural icon of France, attracting millions of visitors annually."},
        }
    ],
    "usage": {"prompt_tokens": 106, "completion_tokens": 51, "total_tokens": 157, "cost": 6.2e-05},
    "x402_receipt": make_receipt("meta-llama/llama-4-maverick", "llm/llama", 1700, 0.001),
}

router = make_router(
    route_path=ROUTE_PATH, route_key=ROUTE_KEY, model=MODEL, provider=PROVIDER,
    description_key='llm-llama', sample_request=SAMPLE_REQUEST, sample_response=SAMPLE_RESPONSE,
)
