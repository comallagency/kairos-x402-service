"""POST /llm/llama - pay-per-call meta-llama/llama-4-maverick, OpenAI-compatible passthrough.

2026-10-10, corrected twice today: DeepInfra (the original 2026-09-30
pin) had silently dropped out of OpenRouter's provider list for this
model entirely - confirmed via GET /api/v1/models/{model}/endpoints,
which no longer lists DeepInfra at all. First fix re-pinned to Parasail
on LATENCY alone (0.55-0.58s, tightest of 3 live candidates) without
checking it against _price_ceiling_usd's own reference price for this
model - a full provider/margin audit done right after caught that
mistake: the reference price is $0.1875/$0.6525 per MTok (DigitalOcean's
price), while Parasail charges $0.35/$1.00 - paying Parasail while
charging the ceiling (reference x 1.10 MARKUP) would LOSE money on every
completion token (margin = 0.6525*1.10 - 1.00 = -0.282 USD/MTok). Same
problem on Novita ($0.27/$0.85 - margin = 0.6525*1.10-0.85 = -0.132
USD/MTok). DigitalOcean is the ONLY one of the 3 live candidates that is
margin-positive (by construction: it IS the reference price, so margin
= reference x 0.10 > 0 exactly) - a single-entry `order` list, not 3,
because the other two fail the margin check, not reliability (all 3
measured 100% uptime_last_30m). If broader redundancy is wanted here
later, it has to come with either a provider priced at or below
DigitalOcean's rate, or a change to how the ceiling itself is computed -
neither is in scope for this fix.

Same engine as POST /v1/chat/completions (app/handlers/llm_gateway.py):
"exact" scheme only, ceiling computed from max_tokens via that same
pricing formula. See app/handlers/llm_per_model.py for the shared code."""

from __future__ import annotations

from app.handlers.llm_per_model import make_price_fn, make_router
from app.receipts import make_receipt

MODEL = 'meta-llama/llama-4-maverick'
PROVIDER = {'order': ['DigitalOcean'], 'allow_fallbacks': False}
ROUTE_PATH = '/llm/llama'
ROUTE_KEY = 'llm/llama'

price_fn = make_price_fn(MODEL)

SAMPLE_REQUEST = {"messages": [{"role": "user", "content": "Summarize in one sentence: The Eiffel Tower is a wrought-iron lattice tower on the Champ de Mars in Paris, France. It was designed by Gustave Eiffel's engineering company and built as the entrance arch for the 1889 World's Fair. Initially criticized by some of France's leading artists and intellectuals for its design, it has become a global cultural icon of France and one of the most recognizable structures in the world, attracting millions of visitors every year."}], "max_tokens": 150}
SAMPLE_RESPONSE = {
    "id": "gen-1790781075-Lyd15GuBV6xAvA6HlOvf",
    "object": "chat.completion",
    "created": 1790781075,
    "model": "meta-llama/llama-4-maverick",
    "provider": "DigitalOcean",
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
