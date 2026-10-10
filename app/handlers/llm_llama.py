"""POST /llm/llama - pay-per-call meta-llama/llama-4-maverick, OpenAI-compatible passthrough.

Re-pinned to Parasail 2026-10-10: DeepInfra (the original 2026-09-30 pin)
had silently dropped out of OpenRouter's provider list for this model
entirely - confirmed via GET /api/v1/models/{model}/endpoints, which no
longer lists DeepInfra at all (only DigitalOcean, Novita, Parasail,
Google). With PROVIDER's hard `only`/`allow_fallbacks: False`, every real
paid call since whenever that happened failed deterministically with
OpenRouter 404 "no allowed providers available" - caught via a real
buyer's (lumiere-paycheck-prober) two failed paid attempts today
(03:06 and 09:11 UTC), both verified-but-unsettled (x402 never settles a
>=400 response), which is exactly why their own catalog rates this route
C ("no confirmed paid test"). Measured today, 2 real calls each, 100%
uptime_last_30m on all three live candidates: DigitalOcean 0.87-1.22s,
Novita 0.48-1.31s, Parasail 0.55-0.58s (tightest, most consistent) -
Parasail picked on that basis, same single-pinned-provider, no-fallback
shape as before (see research.py's own docstring for why a hard pin
beats open fallback here: OpenRouter's load-balancing can silently route
to a slow/unreliable provider serving the same model id).

Same engine as POST /v1/chat/completions (app/handlers/llm_gateway.py):
"exact" scheme only, ceiling computed from max_tokens via that same
pricing formula. See app/handlers/llm_per_model.py for the shared code."""

from __future__ import annotations

from app.handlers.llm_per_model import make_price_fn, make_router
from app.receipts import make_receipt

MODEL = 'meta-llama/llama-4-maverick'
PROVIDER = {'only': ['Parasail'], 'allow_fallbacks': False}
ROUTE_PATH = '/llm/llama'
ROUTE_KEY = 'llm/llama'

price_fn = make_price_fn(MODEL)

SAMPLE_REQUEST = {"messages": [{"role": "user", "content": "Summarize in one sentence: The Eiffel Tower is a wrought-iron lattice tower on the Champ de Mars in Paris, France. It was designed by Gustave Eiffel's engineering company and built as the entrance arch for the 1889 World's Fair. Initially criticized by some of France's leading artists and intellectuals for its design, it has become a global cultural icon of France and one of the most recognizable structures in the world, attracting millions of visitors every year."}], "max_tokens": 150}
SAMPLE_RESPONSE = {
    "id": "gen-1790781075-Lyd15GuBV6xAvA6HlOvf",
    "object": "chat.completion",
    "created": 1790781075,
    "model": "meta-llama/llama-4-maverick",
    "provider": "Parasail",
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
